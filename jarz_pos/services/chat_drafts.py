"""AI reply drafts for the chat inbox.

Claude writes a *suggestion*; a moderator approves, edits or rejects every one.
Nothing in this module sends anything.

The model never types a price. It writes placeholders such as
``{{price:ITEM_CODE}}`` and this module fills them from the live POS price
list, so a hallucinated number cannot reach a customer unnoticed: an unknown
placeholder becomes ``[؟]`` with a warning, and a number the model typed by
itself raises "AI typed a number — check it".

Planted drafts (``planted_draft_percent``) are the quality check on the
moderators: occasionally the Pending draft carries one price raised by 20-40%
while the true draft waits Held behind it. Sending the planted draft unedited is
blocked server-side; see ``api/chat.send_draft``.

Pure helpers (masking, placeholder filling, the planted mutation, the edit
distance, cost) carry no database access and are unit-tested directly.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import io
import json
import random
import re
import time
from collections.abc import Iterable
from typing import Any

import frappe

from jarz_pos.services import chat_common as cc
from jarz_pos.services.chat_settings import cap_ready_answers, render_ready_answer

INTENTS = ("Inquiry", "Order", "Complaint", "Other")
HISTORY_MESSAGES = 30
#: Follow-ups and personal offers older than this are left out of the profile.
FOLLOWUP_LOOKBACK_DAYS = 30
#: The latest order's board state is shown only while the order is this fresh.
RECENT_ORDER_DAYS = 2
#: A flavour is off the AI's menu while every branch together holds fewer jars
#: than this (paused flavours keep a stray jar or two, e.g. Mango 2026-10-29).
MENU_MIN_STOCK = 3
#: Messages shown from each of the customer's other chats (another channel).
OTHER_CHAT_MESSAGES = 12
#: Body prefix of the placeholder chat_ingest stores for a Business Suite reply.
SUITE_PLACEHOLDER_PREFIX = "Reply sent from Meta Business Suite"
#: What each follow-up template says, for the model (the copy lives at Meta).
FOLLOWUP_GIST = {
	"day32": "a 'we miss you, a month since your last order' WhatsApp message (no offer)",
	"day50": "a 'we have not seen you in a while' WhatsApp message carrying the personal offer",
}
CONTEXT_CACHE_SECONDS = 600
# Sonnet/Haiku 5.5 think by default and thinking counts against max_tokens: 600 cut
# Sonnet off before it called submit_draft. Effort "low" keeps thinking short.
MAX_TOKENS = 2000
DRAFT_EFFORT = "low"
UNKNOWN = "[؟]"
TYPED_NUMBER_WARNING = "AI typed a number — check it"
EDIT_DISTANCE_CAP = 2000

#: USD per million tokens: (input, output, cache read), from
#: platform.claude.com/docs/en/about-claude/pricing (checked 2026-10-17).
#: Cache read is per model: 0.1x input on Haiku 5.5, 0.05x on Sonnet 5.5.
PRICES = {
	"claude-haiku-5-5": (0.10, 0.50, 0.01),
	"claude-sonnet-5-5": (2.00, 10.00, 0.10),
}
#: A 5-minute cache write is 1.25x the input price on every model.
CACHE_WRITE_FACTOR = 1.25

_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z_]+)\s*(?::\s*([^}]*?))?\s*\}\}")
_URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# Seven or more digits (Western or Arabic-Indic), optionally +, single
# separators between digits: 0111 103 4268 / +20-111-1034268 / ٠١١١٠٣٤٢٦٨.
_PHONE = re.compile(r"\+?\d(?:[ \-.()]{0,2}\d){6,}")
_NUMBER = re.compile(r"(?<![\w.])(\d+(?:[.,]\d+)?)(?![\w])")
_CURRENCY = r"(?:جنيه|جنيهات|جنية|ج\.م|ج\b|EGP|LE|L\.E|pounds?|egp)"
_CURRENCY_AFTER = re.compile(r"(\d+(?:[.,]\d+)?)\s*" + _CURRENCY, re.IGNORECASE)
_CURRENCY_BEFORE = re.compile(_CURRENCY + r"\s*(\d+(?:[.,]\d+)?)", re.IGNORECASE)
_UNIT_AFTER = re.compile(
	r"^\s*(?:%|جرام|جم|غرام|g\b|gm|gr|kg|كيلو|ml|مل|لتر|دقيقة|دقايق|دقائق|ساعة|ساعات|"
	r"min|mins|minutes?|hours?|hrs?|يوم|أيام|ايام|days?|قطعة|قطع|حبة|pcs|pieces?|"
	r"سعرة|سعرات|سعر حراري|كالوري|kcal|calories?|cal\b)",
	re.IGNORECASE,
)

SYSTEM_PROMPT = """You draft replies for Jarz, an Egyptian dessert brand (jars, cakes, bundles), \
to customers writing on WhatsApp, Instagram, Messenger or under ads and posts. A human moderator \
reviews every draft before anything is sent.

Rules:
1. Language: write Egyptian colloquial Arabic (عامية مصرية), the way a Cairo shop talks to its \
customers on chat: «دلوقتي»، «عايز»، «ازاي»، «إمتى»، «كده»، «بنوصل»، «هيوصلك». Never Modern \
Standard Arabic (no «سوف»، «لدينا»، «يمكنكم»، «نود»، «حالياً لا يتوفر»). A customer who writes \
Franco ("3ayez a3raf el as3ar") gets Egyptian Arabic in Arabic script. Only a customer who writes \
in English gets English. Address the customer as «حضرتك» and «يا فندم». Keep English brand, \
bundle and flavour names in English script (Jarz, Signature Trio, Lotus); say برطمان or جار, \
never جرة. Match their greeting («وعليكم السلام»، «مساء النور»). Never mirror pet names \
(«حبيبتي»، «يا روحي»). 1-3 short sentences: the moderators who sell best write about ten words.
2. NEVER type a price, fee or amount as a number. Use only these placeholders, exactly:
   {{price:ITEM_CODE}} for an item or bundle price (ITEM_CODE from the menu),
   {{size_prices}} for the price of each jar size, one line (every flavour costs the same per size),
   {{delivery_fee:TERRITORY_NAME}} for a delivery fee (TERRITORY_NAME from the delivery list),
   {{free_delivery_threshold}} for the free-delivery minimum,
   {{customer_first_name}} to greet the customer by name,
   {{menu}} for the full priced menu, only when they ask for the menu or the flavours,
   {{offers}} for every live bundle with what is inside and its price, plus free delivery: only \
when they ask about offers or boxes, or want 3 jars or more,
   {{delivery_areas}} for the list of areas we deliver to,
   {{customer_offer:CODE}} for the wording of a personal offer listed in the customer profile.
   {{menu}}, {{offers}} and {{delivery_areas}} expand to several lines: put each on its own line, \
use each at most once, never retype or summarise what it contains, and never send one as the \
whole reply: a human line comes before it and a question after it.
   Personal data in the conversation is masked as [PHONE], [EMAIL], [LINK], [CUSTOMER]; never \
repeat those tokens.
3. Suggest an upsell only from the upsell candidates list, and only when it fits naturally. Put \
it in upsell_item_code / upsell_line, not in the reply.
4. No health, nutrition, diet, sugar-free or allergy-safety claims beyond the fixed wording in \
the voice notes below. Asked whether it suits diabetics or a diet, or about allergens: say a \
team member will confirm.
5. Complaints (late, wrong, damaged, bad taste, rude courier...): apologise sincerely, ask for \
the order details if missing, promise that the team will look into it. Never promise a refund, \
replacement, discount or compensation. Set is_complaint true.
6. Do not invent products, flavours, branches, opening hours or delivery times that are not in \
the context.
7. When you are unsure, or the request needs a human (custom order, payment problem, refund \
request, angry customer), still write a safe holding reply and fill needs_human_reason.
8. Delivery time is always «من 30 دقيقة لحد ساعتين بالكتير» ("30 minutes to 2 hours at most"). \
Never promise faster, never a fixed hour. We deliver inside the listed areas only: no other \
governorates.
9. When a ready answer below matches the message, start from it.
10. Never say «مش متواجدين», never answer in an English auto-reply style, never point to a \
photo or a highlight for the prices, and never send the customer to the website instead of \
helping: we take the order here in the chat; orderjarz.com is only an extra option.
11. Collaboration, influencer or job requests: thank them, ask for their portfolio link or CV, \
say the team will get back to them; no sales pitch. Cafés, companies, wholesale: mention \
«اسم الكافيه بتاعك. والـ 7 طبقات علينا.», ask for the café or company name, area and phone, \
and hand over (needs_human_reason). Never quote wholesale prices, margins or minimums.
12. The customer profile is what Jarz already knows about this customer. Never ask for what it \
already gives: with an area on file, quote {{delivery_fee:AREA}} for it and only confirm it \
(«نوصلك على المهندسين زي المرة اللي فاتت؟»); with an address on file, ask them to confirm it \
instead of sending it again. Use their past orders to suggest what they like. The notes from \
past chats and the messages from their other chats are background too: use them, but never \
quote them back or say you have them.
13. Personal offers and follow-ups in the profile were sent by Jarz itself. When the customer \
mentions «هدية»، «الكود»، «العرض» or «الرسالة», that is the offer: state it with \
{{customer_offer:CODE}}, tell them we apply the code when they order here in the chat, and \
follow its facts (minimum order, delivery fee, expiry). A promo code that is neither in the \
profile nor in the promo code list is not ours: say a team member will check it. Never invent \
a code or an offer.
14. A line «Jarz (sent from Business Suite, text not available)» is a reply a team member sent \
that we cannot read: do not repeat a greeting or a question it probably already covered.

What sells (from 1,800 past Jarz chats):
- Answer the price with the numbers first, through {{size_prices}}. Never «حسب الحجم» or \
«حسب النكهة» without them: customers who got that asked a second time or left.
- Ask «تحب وسط ولا كبير؟» rather than an open «تحب تطلب إيه؟».
- Ask their area early («حضرتك في أنهي منطقة؟») unless the profile already gives it: it \
unlocks the delivery fee and the order.
- Suggest a box in one short line when they want 3 jars or more, or are hosting or gifting.
- Name 1-2 flavours when they hesitate.
- When they are ready, ask for name, phone and detailed address in ONE message.

Jarz voice (classy, warm, never pushy):
- Lead with the feeling, then the fact. Sell the moment the customer wants to be in: guests \
arriving and the table done without effort, a gift that shows you thought about them, the \
reward after a long week, the first spoon through the layers. One feeling per reply, matched \
to what they said: a gift → the person receiving it; guests → the gathering; a treat for \
themselves → they earned it.
- The product facts you may lean on: 7 layers in the cheesecakes only (Tiramisu and Molten \
Cake are «بطبقات»), a reusable glass jar with a lid, ready to eat with no plate or cutting \
(every guest gets their own jar), natural ingredients «مكونات طبيعية», no preservatives «من \
غير أي مواد حافظة», sugar «سكر أهدى», «شيكولاتة حقيقية» for Chocolate Hazelnut and Molten Cake \
only. Sizes: وسط (Medium) 212 ML, كبير (Large) 330 ML. Storage, word for word: «بتقعد 7 أيام في التلاجة من \
يوم ما توصلك» and «بتقعد 3 شهور في الفريزر». Never "100%", "sealed", "light", "diet", \
"healthy", a sugar number, gram weights, or "Belgian".
- Flavour names: say Chocolate Hazelnut, never Nutella. Never offer Kunafa or Carrot Cake, \
even if they appear in the menu.
- Thank them the way they thank the hands: «تسلم إيدكم» → «تسلم حضرتك، ده من ذوقك 💛». Mirror \
their warmth; never a corporate template.
- Desire, not pressure: no countdowns, «العرض هيخلص», "limited", "hurry". Say prices plainly, \
never apologetically. If they find it pricey, say it is «costly شوية عشان المكونات طبيعية» and \
point to a bundle in {{offers}} that fits, without arguing.
- Recommend: when they hesitate or ask what to choose, name 1-2 flavours (Blueberry and \
Chocolate Hazelnut are the favourites) or the bundle that fits their headcount.
- Close every sale-shaped reply with one easy next step: ask for the order, or name, phone and \
detailed address in one message.
- Emoji: 1-2, from 💛 ♥️ 🥰 😍 🍰 🚚 ✨; they matter (warm human replies sold several times \
more than cold ones). In a complaint or apology, only 🙏.
Always answer by calling submit_draft."""

TOOL = {
	"name": "submit_draft",
	"description": "Submit the reply draft for the moderator to review.",
	"input_schema": {
		"type": "object",
		"properties": {
			"reply": {"type": "string", "description": "The message to the customer, with placeholders."},
			"intent": {"type": "string", "enum": list(INTENTS)},
			"is_complaint": {"type": "boolean"},
			"upsell_item_code": {"type": ["string", "null"]},
			"upsell_line": {"type": ["string", "null"]},
			"needs_human_reason": {"type": ["string", "null"]},
		},
		"required": ["reply", "intent", "is_complaint"],
	},
}


# ── pure helpers ────────────────────────────────────────────────────────


def to_western_digits(text: Any) -> str:
	return str(text or "").translate(_DIGITS)


def is_arabic(text: Any) -> bool:
	return any("؀" <= ch <= "ۿ" for ch in str(text or ""))


def _name_terms(names: Iterable[str | None]) -> list[str]:
	terms: set[str] = set()
	for name in names:
		clean = " ".join(str(name or "").split())
		if len(clean) < 2:
			continue
		terms.add(clean)
		for part in clean.split(" "):
			if len(part) >= 3:
				terms.add(part)
	return sorted(terms, key=len, reverse=True)


def mask_text(text: Any, names: Iterable[str | None] = ()) -> str:
	"""Replace phones, emails, links and the customer's names with tokens."""
	out = str(text or "")
	if not out:
		return out
	out = _URL.sub("[LINK]", out)
	out = _EMAIL.sub("[EMAIL]", out)

	def _phone(match: re.Match) -> str:
		digits = sum(ch.isdigit() for ch in match.group(0))
		return "[PHONE]" if digits >= 7 else match.group(0)

	out = _PHONE.sub(_phone, out)
	for term in _name_terms(names):
		out = re.sub(r"(?<!\w)" + re.escape(term) + r"(?!\w)", "[CUSTOMER]", out, flags=re.IGNORECASE)
	return out


def _num(amount: Any) -> str:
	try:
		value = float(amount)
	except (TypeError, ValueError):
		return str(amount)
	if abs(value - round(value)) < 0.005:
		return str(int(round(value)))
	return f"{value:.2f}"


def format_egp(amount: Any, arabic: bool = True) -> str:
	return f"{_num(amount)} جنيه" if arabic else f"{_num(amount)} EGP"


def resolve_item_code(ref: str, ctx: dict[str, Any]) -> str | None:
	"""Exact code, then case-insensitive code, then item name."""
	ref = str(ref or "").strip()
	if not ref:
		return None
	prices = ctx.get("prices") or {}
	if ref in prices:
		return ref
	low = ref.lower()
	for code in prices:
		if str(code).lower() == low:
			return code
	names = ctx.get("item_names") or {}
	return names.get(low)


def _resolve_territory(ref: str, ctx: dict[str, Any]) -> float | None:
	fees = ctx.get("delivery_fees") or {}
	low = str(ref or "").strip().lower()
	if low in fees:
		return fees[low]
	return None


def fill_placeholders(
	template: Any, ctx: dict[str, Any], overrides: dict[str, float] | None = None
) -> tuple[str, list[str], list[tuple[str, float]]]:
	"""Fill ``{{...}}`` placeholders. Returns ``(text, warnings, filled_prices)``.

	``overrides`` replaces the price of an item code (the planted mutation).
	``filled_prices`` lists every ``(code, price)`` actually filled, in order.
	"""
	text = str(template or "")
	arabic = bool(ctx.get("arabic", is_arabic(text)))
	warnings: list[str] = []
	filled: list[tuple[str, float]] = []
	overrides = overrides or {}

	def _sub(match: re.Match) -> str:
		key = match.group(1).strip().lower()
		arg = (match.group(2) or "").strip()
		if key == "price":
			code = resolve_item_code(arg, ctx)
			if code is None:
				warnings.append(f"Unknown item in price placeholder: {arg or '?'}")
				return UNKNOWN
			price = overrides.get(code, (ctx.get("prices") or {}).get(code))
			if price is None:
				warnings.append(f"No price for {code}")
				return UNKNOWN
			filled.append((code, float(price)))
			return format_egp(price, arabic)
		if key == "delivery_fee":
			fee = _resolve_territory(arg, ctx)
			if fee is None:
				warnings.append(f"Unknown area in delivery fee placeholder: {arg or '?'}")
				return UNKNOWN
			return format_egp(fee, arabic)
		if key == "free_delivery_threshold":
			threshold = ctx.get("free_delivery_threshold")
			if not threshold:
				warnings.append("No free-delivery offer is active")
				return UNKNOWN
			return format_egp(threshold, arabic)
		if key == "customer_first_name":
			return str(ctx.get("customer_first_name") or "")
		if key == "customer_offer":
			offers = ctx.get("customer_offers") or {}
			wanted = arg.replace("-", "").upper()
			offer = next((t for c, t in offers.items() if c.replace("-", "").upper() == wanted), None)
			if offer is None and not arg and len(offers) == 1:
				offer = next(iter(offers.values()))
			if not offer:
				warnings.append(
					f"No personal offer {arg} for this customer"
					if arg
					else "No personal offer for this customer"
				)
				return UNKNOWN
			return offer
		if key in BLOCK_PLACEHOLDERS:
			block = ((ctx.get("blocks") or {}).get("ar" if arabic else "en") or {}).get(key)
			if not block:
				warnings.append(f"No live data for {{{{{key}}}}}")
				return UNKNOWN
			return block
		warnings.append(f"Unknown placeholder: {match.group(0)}")
		return UNKNOWN

	out = _PLACEHOLDER.sub(_sub, text)
	# An empty first name leaves "أهلا ،" / "Hi ,": tidy the gap it left.
	out = re.sub(r"[ \t]+([،,!.؟?])", r"\1", out)
	out = re.sub(r"[ \t]{2,}", " ", out).strip()
	return out, warnings, filled


# ── live blocks: {{menu}}, {{offers}}, {{delivery_areas}} ──────────────

BLOCK_PLACEHOLDERS = ("menu", "offers", "delivery_areas", "size_prices")

#: Used when Jarz Chat Settings has no Ready Answers rows. Written from 1,800
#: past Instagram chats: the short replies that ended in an order, checked
#: against the Approved Facts Registry.
DEFAULT_READY_ANSWERS: list[dict[str, str]] = [
	{
		"topic": "Greeting",
		"when_to_use": "First message, «السلام عليكم», «مساء الخير», a reply to an ad or a post",
		"answer": "وعليكم السلام، أهلاً بحضرتك في Jarz 💛 تحب تطلب لنفسك ولا لعزومة أو هدية؟",
	},
	{
		"topic": "Menu",
		"when_to_use": "«المنيو»، «إيه الأنواع اللي عندكم؟»، «تفاصيل»",
		"answer": "أكيد يا فندم 💛 ده المنيو، وكل النكهات بنفس السعر حسب الحجم:\n{{menu}}\n\n"
		"تحب وسط ولا كبير؟",
	},
	{
		"topic": "Prices",
		"when_to_use": "«بكام»، «الأسعار»، «سعر البرطمان الكبير»",
		"answer": "كل النكهات بنفس السعر يا فندم: {{size_prices}} 💛 "
		"ولو هتاخد 3 برطمانات أو أكتر، البوكسات أوفر. تحب وسط ولا كبير؟",
	},
	{
		"topic": "Sizes",
		"when_to_use": "«الأحجام إيه؟»، «الوسط كام ملي؟»، «يكفي كام فرد؟»",
		"answer": "عندنا حجمين في برطمان زجاج بغطا: الوسط 212 ML والكبير 330 ML، "
		"والأسعار {{size_prices}} 💛 تحب أنهي حجم؟",
	},
	{
		"topic": "Offers / bundles",
		"when_to_use": "«في عروض؟»، «العرض ده لسه شغال؟»، a box, 3 jars or more, guests",
		"answer": "أكيد يا فندم 😍 دي البوكسات الشغالة دلوقتي:\n{{offers}}\n\n"
		"وتختار النكهات اللي تحبها جوه البوكس. تحب نجهزلك أنهي واحد؟",
	},
	{
		"topic": "Recommendation / first time",
		"when_to_use": "«ترشحلي إيه؟»، «أول مرة أجرب»، hesitating between flavours",
		"answer": "لو أول مرة، Blueberry و Chocolate Hazelnut أكتر اتنين الناس بتحبهم 💛 "
		"تحب فاكهة ولا شيكولاتة؟",
	},
	{
		"topic": "Delivery areas",
		"when_to_use": "«بتوصلوا فين؟»، «بتوصلوا [منطقة]؟»، «التوصيل بكام؟»",
		"answer": "بنوصل للمناطق دي يا فندم:\n{{delivery_areas}}\n\n"
		"حضرتك في أنهي منطقة؟ نقولك مصاريف التوصيل على طول 🚚",
	},
	{
		"topic": "Outside our areas",
		"when_to_use": "An area that is not in the delivery list, another governorate",
		"answer": "للأسف يا فندم مش بنوصل للمنطقة دي دلوقتي. "
		"لو في عنوان تاني لحضرتك في المناطق دي هنوصله بكل سرور 💛\n{{delivery_areas}}",
	},
	{
		"topic": "Delivery time",
		"when_to_use": "«هيوصل إمتى؟»، «عندي ضيوف»، «محتاجه النهارده»",
		"answer": "الأوردر بيوصل من 30 دقيقة لحد ساعتين بالكتير من تأكيده 🚚 " "حضرتك في أنهي منطقة؟",
	},
	{
		"topic": "How to order",
		"when_to_use": "The customer chose what they want, «أطلب إزاي؟»، «عايز أعمل أوردر»",
		"answer": "تقدر تطلب هنا في الشات على طول 💛 ابعتلنا في رسالة واحدة: النكهات والحجم، "
		"الاسم، رقم الموبايل، والعنوان بالتفصيل، ونأكدلك الأوردر.",
	},
	{
		"topic": "Website problem",
		"when_to_use": "«الموقع مش بيفتح»، «مش لاقي منطقتي على السايت»، checkout trouble",
		"answer": "ولا يهمك يا فندم، نعملك الأوردر من هنا 💛 ابعتلنا النكهات والحجم مع الاسم "
		"والرقم والعنوان بالتفصيل، وهنبلغ الفريق يراجع الموقع.",
	},
	{
		"topic": "Payment",
		"when_to_use": "«الدفع إزاي؟»، «ينفع InstaPay؟»، «كاش؟»",
		"answer": "متاح الدفع كاش عند الاستلام أو InstaPay، وكمان أونلاين على orderjarz.com 💛 "
		"تحب تأكد الأوردر بأنهي طريقة؟",
	},
	{
		"topic": "Storage",
		"when_to_use": "«بيقعد قد إيه؟»، «أحطه في التلاجة؟»، «ينفع فريزر؟»",
		"answer": "بتقعد 7 أيام في التلاجة من يوم ما توصلك، وبتقعد 3 شهور في الفريزر 💛 "
		"وتابع تعليمات التقديم اللي جاية مع الأوردر.",
	},
	{
		"topic": "Forwarded post or photo",
		"when_to_use": "A shared post, an ad, a photo with no text",
		"answer": "أهلاً بحضرتك 💛 ده من الـ Cheesecake بتاعنا بالـ 7 طبقات. "
		"تحب أقولك الأسعار ولا تختار النكهة الأول؟",
	},
	{
		"topic": "Compliment",
		"when_to_use": "«تحفة»، «تسلم إيدكم»، praise after an order",
		"answer": "تسلم حضرتك 💛 ده من ذوقك، وكلامك بيفرق معانا جداً. مستنيينك في الأوردر الجاي 🥰",
	},
	{
		"topic": "Late order",
		"when_to_use": "«الأوردر اتأخر»، «المندوب فين؟»",
		"answer": "حقك علينا يا فندم 🙏 ممكن رقم الموبايل اللي اتطلب بيه الأوردر؟ "
		"هنراجع مع المندوب حالاً ونطمنك.",
	},
	{
		"topic": "Complaint",
		"when_to_use": "Melted, broken jar, wrong item, taste",
		"answer": "حقك علينا يا فندم، وآسفين جداً على اللي حصل 🙏 ممكن رقم الأوردر أو الاسم "
		"والرقم اللي اتطلب بيه؟ هنراجع الموضوع حالاً ونرجعلك.",
	},
	{
		"topic": "Gift",
		"when_to_use": "A gift, delivery to someone else, a birthday",
		"answer": "فكرة حلوة جداً 💛 نوصلها للشخص اللي حضرتك عايز تهاديه. "
		"ابعتلنا اسمه ورقمه والعنوان بالتفصيل، وقولنا لو في أي طلب خاص للهدية.",
	},
	{
		"topic": "Large or event order",
		"when_to_use": "An event, a very large quantity, a custom request",
		"answer": "يا أهلاً بيك 💛 للطلبات الكبيرة واحد من الفريق هيتواصل مع حضرتك ويظبط معاك "
		"كل التفاصيل. ممكن العدد تقريباً والمعاد؟",
	},
	{
		"topic": "Café / company (B2B)",
		"when_to_use": "A café, a restaurant, a company, wholesale, «جملة»",
		"answer": "أهلاً بحضرتك 💛 اسم الكافيه بتاعك. والـ 7 طبقات علينا. "
		"ممكن اسم الكافيه أو الشركة والمنطقة ورقم حضرتك؟ وفريق المبيعات هيتواصل معاك.",
	},
	{
		"topic": "Collaboration / job",
		"when_to_use": "Blogger, UGC, influencer, «متاح شغل؟»، a CV",
		"answer": "ميرسي جداً لاهتمامك بـ Jarz 💛 ابعتلنا لينك البورتفوليو أو الـ CV، "
		"والفريق هيرجعلك في أقرب وقت.",
	},
]
SIZE_AR = {"small": "صغير", "medium": "وسط", "large": "كبير"}
#: Territories that are bookkeeping, not places a courier goes.
_NOT_AN_AREA = {"all territories", "egypt", "rest of the world"}


def _size_label(group: str, arabic: bool) -> str:
	return SIZE_AR.get(group.strip().lower(), group) if arabic else group


def _strip_suffix(name: str, *suffixes: str) -> str:
	for suffix in suffixes:
		if suffix and name.lower().endswith(" " + suffix.lower()):
			return name[: -len(suffix) - 1].strip()
	return name


#: Catalog spellings that differ from the customer-facing name (Facts Registry).
_DISPLAY_NAME = {"redvelvet": "Red Velvet", "carrot cake": "Carrot Cake"}


def _flavour(name: str, group: str) -> str:
	flavour = _strip_suffix(name, group)
	return _DISPLAY_NAME.get(flavour.lower(), flavour)


def split_by_stock(
	products: Iterable[dict[str, Any]], stock: dict[str, float], minimum: float = 0
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
	"""``(available, paused)``. Pure.

	*stock* holds the branch total of every stock item; an item missing from it
	(a non-stock item) stays available. When no item reaches *minimum* the stock
	data is not trusted and nothing is paused: an empty menu is worse than a
	stale one.
	"""
	items = list(products)
	minimum = minimum or MENU_MIN_STOCK
	if not any(float(q or 0) >= minimum for q in stock.values()):
		return items, []
	available, paused = [], []
	for item in items:
		qty = stock.get(str(item.get("id") or ""))
		(available if qty is None or float(qty or 0) >= minimum else paused).append(item)
	return available, paused


def paused_names(paused: Iterable[dict[str, Any]]) -> list[str]:
	"""``Mango (Large)`` for each priced paused item, catalog order. Pure."""
	names = []
	for item in paused:
		try:
			if float(item.get("price") or 0) <= 0:
				continue
		except (TypeError, ValueError):
			continue
		group = str(item.get("item_group") or "")
		label = _flavour(str(item.get("name") or item.get("id")), group)
		names.append(f"{label} ({group})" if group else label)
	return list(dict.fromkeys(names))


def _branch_stock(codes: Iterable[str]) -> dict[str, float]:
	"""Stock of each stock item summed over every enabled POS profile's warehouse."""
	codes = sorted({c for c in codes if c})
	if not codes:
		return {}
	try:
		warehouses = sorted(
			{w for w in frappe.get_all("POS Profile", filters={"disabled": 0}, pluck="warehouse") if w}
		)
		stock_items = frappe.get_all(
			"Item", filters={"name": ["in", codes], "is_stock_item": 1}, pluck="name", ignore_permissions=True
		)
		if not warehouses or not stock_items:
			return {}
		out = dict.fromkeys(stock_items, 0.0)
		for code, qty in frappe.db.sql(
			"""select item_code, sum(actual_qty) from `tabBin`
			where warehouse in %(wh)s and item_code in %(codes)s group by item_code""",
			{"wh": tuple(warehouses), "codes": tuple(stock_items)},
		):
			out[code] = float(qty or 0)
		return out
	except Exception:
		return {}


def render_menu(products: Iterable[dict[str, Any]], arabic: bool) -> str:
	"""Priced items grouped by item group: one line per group when the group
	shares one price ("وسط 120 جنيه: Lotus، Mango"), else one line per item.
	Flavour names stay in English script in both languages (brand rule).
	Zero-priced items (components, samples) are left out."""
	groups: dict[str, list[dict[str, Any]]] = {}
	for item in products:
		try:
			price = float(item.get("price") or 0)
		except (TypeError, ValueError):
			continue
		if price <= 0:
			continue
		groups.setdefault(str(item.get("item_group") or ""), []).append({**item, "price": price})
	lines = []
	for group, items in groups.items():
		size = _size_label(group, arabic)
		labels = [_flavour(str(i.get("name") or i.get("id")), group) for i in items]
		if len({round(i["price"], 2) for i in items}) == 1 and group:
			names = ("، " if arabic else ", ").join(dict.fromkeys(labels))
			lines.append(f"• {size} {format_egp(items[0]['price'], arabic)}: {names}")
		else:
			for item, label in zip(items, labels, strict=True):
				lines.append(f"• {label} {size}".rstrip() + f": {format_egp(item['price'], arabic)}")
	return "\n".join(lines)


def render_size_prices(products: Iterable[dict[str, Any]], arabic: bool) -> str:
	"""One line, cheapest size first: «وسط 120 جنيه، كبير 160 جنيه». A size whose
	flavours differ in price shows its lowest price as «من ...»."""
	groups: dict[str, list[float]] = {}
	for item in products:
		try:
			price = float(item.get("price") or 0)
		except (TypeError, ValueError):
			continue
		group = str(item.get("item_group") or "").strip()
		if price > 0 and group:
			groups.setdefault(group, []).append(price)
	parts = []
	for group, values in sorted(groups.items(), key=lambda kv: min(kv[1])):
		amount = format_egp(min(values), arabic)
		if len({round(v, 2) for v in values}) > 1:
			amount = f"من {amount}" if arabic else f"from {amount}"
		parts.append(f"{_size_label(group, arabic)} {amount}")
	return ("، " if arabic else ", ").join(parts)


def render_offers(bundles: Iterable[dict[str, Any]], threshold: float | None, arabic: bool) -> str:
	"""Each bundle with what is inside, its price and a free-delivery mark."""
	lines = []
	for bundle in bundles:
		try:
			price = float(bundle.get("price") or 0)
		except (TypeError, ValueError):
			continue
		if price <= 0:
			continue
		inside = " + ".join(
			f"{int(g.get('quantity') or 0)} {_size_label(str(g.get('group_name') or ''), arabic)}"
			for g in bundle.get("item_groups") or []
			if int(g.get("quantity") or 0) > 0
		)
		line = f"• {bundle.get('name')}"
		if inside:
			line += f" ({inside})"
		line += f" = {format_egp(price, arabic)}"
		if bundle.get("free_shipping"):
			line += " + توصيل مجاني 🚚" if arabic else " + free delivery 🚚"
		lines.append(line)
	if threshold:
		lines.append(
			f"🚚 توصيل مجاني لأي طلب فوق {format_egp(threshold, True)}"
			if arabic
			else f"🚚 Free delivery on any order above {format_egp(threshold, False)}"
		)
	return "\n".join(lines)


def render_delivery_areas(
	territories: Iterable[dict[str, Any]], group_labels: dict[str, dict[str, str]], arabic: bool
) -> str:
	"""The places we deliver to, one name each: sub-areas of a district fold into
	the district ("الشيخ زايد"), bookkeeping and zero-fee territories drop out.
	Names are Arabic in both languages when one exists: the English names are
	internal codes (EGNASRCITY)."""
	seen: dict[str, None] = {}
	for row in territories:
		name = str(row.get("name") or "").strip()
		if not name or name.startswith("_") or name.lower() in _NOT_AN_AREA:
			continue
		try:
			fee = float(row.get("delivery_income") or 0)
		except (TypeError, ValueError):
			continue
		if fee <= 0:
			continue
		parent = str(row.get("parent_territory") or "").strip()
		if parent and parent.lower() not in _NOT_AN_AREA and parent in group_labels:
			labels = group_labels[parent]
		else:
			labels = {"en": name, "ar": str(row.get("custom_territory_name_ar") or "").strip()}
		label = labels.get("ar") or labels.get("en") or name
		seen.setdefault(label, None)
	return ("، " if arabic else ", ").join(seen)


def typed_number_warnings(template: Any) -> list[str]:
	"""``[TYPED_NUMBER_WARNING]`` when the model wrote a money-like number itself."""
	stripped = to_western_digits(_PLACEHOLDER.sub(" ", str(template or "")))
	if _CURRENCY_AFTER.search(stripped) or _CURRENCY_BEFORE.search(stripped):
		return [TYPED_NUMBER_WARNING]
	for match in _NUMBER.finditer(stripped):
		try:
			value = float(match.group(1).replace(",", "."))
		except ValueError:
			continue
		if value < 30:
			continue
		if _UNIT_AFTER.match(stripped[match.end() :]):
			continue
		return [TYPED_NUMBER_WARNING]
	return []


def sanitize_tool_input(data: Any, candidates: Iterable[str]) -> tuple[dict[str, Any], list[str]]:
	"""Coerce the tool call into the stored shape; drop an upsell not on the list."""
	data = data if isinstance(data, dict) else {}
	warnings: list[str] = []
	intent = str(data.get("intent") or "").strip().title()
	if intent not in INTENTS:
		intent = "Other"
	is_complaint = bool(data.get("is_complaint")) or intent == "Complaint"
	candidate_map = {str(c).lower(): str(c) for c in candidates if c}
	upsell_code = str(data.get("upsell_item_code") or "").strip() or None
	upsell_line = str(data.get("upsell_line") or "").strip() or None
	if upsell_code:
		match = candidate_map.get(upsell_code.lower())
		if match is None:
			warnings.append(f"Upsell {upsell_code} is not on the upsell list — dropped")
			upsell_code = None
			upsell_line = None
		else:
			upsell_code = match
	else:
		upsell_line = None
	if is_complaint:
		upsell_code = None
		upsell_line = None
	reason = str(data.get("needs_human_reason") or "").strip() or None
	return (
		{
			"reply": str(data.get("reply") or "").strip(),
			"intent": intent,
			"is_complaint": is_complaint,
			"upsell_item_code": upsell_code,
			"upsell_line": upsell_line,
			"needs_human_reason": reason,
		},
		warnings,
	)


def round_to_5(value: float) -> int:
	return int(5 * round(float(value) / 5.0))


def plant_mutation(
	template: str, ctx: dict[str, Any], rng: random.Random | None = None
) -> tuple[str, str, str] | None:
	"""Raise one filled price by 20-40% (rounded to 5).

	Returns ``(planted_text, planted_error, wrong_value)`` or ``None`` when the
	template fills no price.
	"""
	rng = rng or random.Random()
	_text, _warnings, filled = fill_placeholders(template, ctx)
	candidates = [(code, price) for code, price in filled if price and price > 0]
	if not candidates:
		return None
	code, price = rng.choice(candidates)
	wrong = round_to_5(price * rng.uniform(1.2, 1.4))
	if wrong <= price:
		wrong = round_to_5(price) + 5
	planted, _w, _f = fill_placeholders(template, ctx, overrides={code: float(wrong)})
	error = f"price {code}: {_num(price)} → {wrong}"
	return planted, error, str(wrong)


def planted_wrong_value(planted_error: Any) -> str | None:
	text = str(planted_error or "")
	if "→" not in text:
		return None
	value = text.rsplit("→", 1)[1].strip()
	return value or None


def planted_caught(final_text: Any, wrong_value: Any) -> bool:
	"""True when the wrong number no longer appears in what the moderator sent."""
	wrong = to_western_digits(wrong_value).strip()
	if not wrong:
		return True
	text = to_western_digits(final_text)
	return re.search(r"(?<!\d)" + re.escape(wrong) + r"(?!\d)", text) is None


def levenshtein(a: Any, b: Any, cap: int = EDIT_DISTANCE_CAP) -> int:
	s = str(a or "")[:cap]
	t = str(b or "")[:cap]
	# Common prefix / suffix cost nothing; trimming them keeps the DP small.
	start = 0
	while start < len(s) and start < len(t) and s[start] == t[start]:
		start += 1
	s, t = s[start:], t[start:]
	end = 0
	while end < len(s) and end < len(t) and s[-1 - end] == t[-1 - end]:
		end += 1
	if end:
		s, t = s[: len(s) - end], t[: len(t) - end]
	if not s:
		return len(t)
	if not t:
		return len(s)
	if len(s) < len(t):
		s, t = t, s
	previous = list(range(len(t) + 1))
	for i, cs in enumerate(s, 1):
		current = [i]
		for j, ct in enumerate(t, 1):
			current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (cs != ct)))
		previous = current
	return previous[-1]


def decide_sent_status(draft_text: Any, final_text: Any) -> tuple[str, int]:
	"""``("Sent As-Is", 0)`` when the stripped texts match, else ``("Sent Edited", distance)``."""
	a = str(draft_text or "").strip()
	b = str(final_text or "").strip()
	if a == b:
		return "Sent As-Is", 0
	return "Sent Edited", levenshtein(a, b)


def complaint_requires_edit(is_complaint: Any, draft_text: Any, final_text: Any) -> bool:
	"""A complaint draft may not be sent unedited."""
	return bool(int(is_complaint or 0)) and str(draft_text or "").strip() == str(final_text or "").strip()


def cost_usd(
	model: str, input_tokens: Any, output_tokens: Any, cache_read: Any = 0, cache_write: Any = 0
) -> float:
	price_in, price_out, price_read = PRICES.get(str(model or ""), PRICES["claude-haiku-5-5"])
	total = (
		int(input_tokens or 0) * price_in
		+ int(output_tokens or 0) * price_out
		+ int(cache_read or 0) * price_read
		+ int(cache_write or 0) * price_in * CACHE_WRITE_FACTOR
	)
	return round(total / 1_000_000, 6)


def first_name(*names: Any) -> str:
	for name in names:
		clean = " ".join(str(name or "").split())
		if clean and not clean.startswith("+") and not clean[:1].isdigit():
			return clean.split(" ")[0]
	return ""


# ── business context ────────────────────────────────────────────────────


@contextlib.contextmanager
def _quiet():
	"""``api.pos`` prints a line per item; keep worker logs readable."""
	with contextlib.redirect_stdout(io.StringIO()):
		yield


def _territory_fields(*base: str) -> list[str]:
	fields = list(base)
	try:
		if frappe.db.has_column("Territory", "custom_territory_name_ar"):
			fields.append("custom_territory_name_ar")
	except Exception:
		pass
	return fields


def _territories() -> list[dict[str, Any]]:
	fields = _territory_fields("name", "delivery_income", "parent_territory")
	try:
		rows = frappe.get_all(
			"Territory", filters={"is_group": 0}, fields=fields, order_by="lft asc", ignore_permissions=True
		)
	except Exception:
		return []
	return [r for r in rows if r.get("delivery_income") not in (None, "")]


def _territory_group_labels() -> dict[str, dict[str, str]]:
	try:
		rows = frappe.get_all(
			"Territory", filters={"is_group": 1}, fields=_territory_fields("name"), ignore_permissions=True
		)
	except Exception:
		return {}
	return {r.name: {"en": r.name, "ar": str(r.get("custom_territory_name_ar") or "").strip()} for r in rows}


def _free_delivery_threshold() -> float | None:
	try:
		if not frappe.db.exists("DocType", "Jarz Promotion Rule"):
			return None
		from jarz_pos.services.delivery_promotions import _is_rule_active

		names = frappe.get_all(
			"Jarz Promotion Rule",
			filters={"enabled": 1, "promotion_scope": "Delivery", "rule_type": "Free Delivery"},
			pluck="name",
			ignore_permissions=True,
		)
		best = None
		for name in names:
			rule = frappe.get_doc("Jarz Promotion Rule", name)
			if not _is_rule_active(rule):
				continue
			if any(getattr(rule, f, None) for f in ("territory", "customer_group", "pos_profile")):
				continue
			if (getattr(rule, "threshold_basis", None) or "Merchandise Subtotal") != "Merchandise Subtotal":
				continue
			channels = {
				str(row.channel or "").strip().lower()
				for row in (getattr(rule, "channels", None) or [])
				if str(row.channel or "").strip()
			}
			if channels and not channels & {"flutter", "pos"}:
				continue
			minimum = float(getattr(rule, "minimum_threshold", 0) or 0)
			if minimum > 0 and (best is None or minimum < best):
				best = minimum
		return best
	except Exception:
		return None


def build_context(settings: dict[str, Any]) -> dict[str, Any]:
	"""Menu, delivery fees, free-delivery offer and upsell candidates.

	Returns ``{"text": <stable prompt block>, "prices", "item_names",
	"delivery_fees", "free_delivery_threshold", "candidates"}``. Cached in Redis
	for 10 minutes per POS profile: it is the cached prefix of every prompt.
	"""
	profile = settings.get("default_pos_profile") or ""
	cache_key = f"jarz_chat_ctx:v5:{profile}"
	try:
		cached = frappe.cache().get_value(cache_key)
		if cached:
			data = json.loads(cached) if isinstance(cached, str | bytes) else cached
			if isinstance(data, dict) and data.get("text"):
				return data
	except Exception:
		pass

	prices: dict[str, float] = {}
	names: dict[str, str] = {}
	menu_lines: list[str] = []
	products: list[dict[str, Any]] = []
	paused: list[dict[str, Any]] = []
	bundles: list[dict[str, Any]] = []
	if profile:
		from jarz_pos.api import pos as pos_api

		try:
			with _quiet():
				products = pos_api.get_profile_products(profile) or []
		except Exception:
			products = []
		# Paused flavours (no stock at any branch) leave the menu, the size
		# prices and the code list, so the model cannot offer them.
		products, paused = split_by_stock(products, _branch_stock(str(p.get("id") or "") for p in products))
		for item in products:
			code = str(item.get("id") or "")
			if not code:
				continue
			price = float(item.get("price") or 0)
			prices[code] = price
			names[str(item.get("name") or "").strip().lower()] = code
			menu_lines.append(f"{code} | {item.get('name') or code} | {item.get('item_group') or ''}")
		try:
			with _quiet():
				bundles = pos_api.get_profile_bundles(profile) or []
		except Exception:
			bundles = []
		for bundle in bundles:
			code = str(bundle.get("erpnext_item") or "")
			if not code:
				continue
			prices[code] = float(bundle.get("price") or 0)
			names[str(bundle.get("name") or "").strip().lower()] = code
			free = " | free delivery" if bundle.get("free_shipping") else ""
			menu_lines.append(f"{code} | {bundle.get('name') or code} | Bundle{free}")

	fees: dict[str, float] = {}
	territory_lines: list[str] = []
	territories = _territories()
	for row in territories:
		fee = float(row.get("delivery_income") or 0)
		fees[str(row.name).strip().lower()] = fee
		label = str(row.name)
		arabic = str(row.get("custom_territory_name_ar") or "").strip()
		if arabic:
			fees[arabic.lower()] = fee
			label = f"{row.name} / {arabic}"
		territory_lines.append(label)

	threshold = _free_delivery_threshold()

	candidates: list[str] = []
	upsell_lines: list[str] = []
	for row in settings.get("upsell_items") or []:
		code = row.get("item_code")
		if not code:
			continue
		candidates.append(code)
		label = next((n for n, c in names.items() if c == code), "") or code
		pitch = row.get("pitch") or ""
		upsell_lines.append(f"{code} | {label}" + (f" | {pitch}" if pitch else ""))

	group_labels = _territory_group_labels()
	blocks = {
		lang: {
			"menu": render_menu(products, arabic),
			"offers": render_offers(bundles, threshold, arabic),
			"delivery_areas": render_delivery_areas(territories, group_labels, arabic),
			"size_prices": render_size_prices(products, arabic),
		}
		for lang, arabic in (("ar", True), ("en", False))
	}

	# Settings already arrive ordered and capped; capping again here guards any
	# settings dict built another way, and is a no-op (no log) when nothing drops.
	ready_lines = [
		render_ready_answer(row)
		for row in cap_ready_answers(settings.get("ready_answers") or DEFAULT_READY_ANSWERS)
	]

	paused_list = paused_names(paused)
	parts = [
		"# Jarz business context",
		"Prices are never shown to you; use {{price:ITEM_CODE}}.",
		"## Menu (ITEM_CODE | name | group)",
		"\n".join(menu_lines) or "(menu unavailable: tell the customer a team member will share the menu)",
		"## Delivery areas (use {{delivery_fee:TERRITORY_NAME}} with the exact name)",
		"\n".join(territory_lines) or "(no delivery areas configured)",
		"## Free delivery",
		"An order above {{free_delivery_threshold}} gets free delivery."
		if threshold
		else "No free-delivery offer is active.",
		"## Paused flavours (out of stock at every branch)",
		"Never offer these. Asked for one, say it is paused and coming back soon, and suggest a "
		"flavour from the menu:\n" + ", ".join(paused_list)
		if paused_list
		else "(none: every menu flavour is available)",
		"## Promo codes open to every customer (personal codes are in the customer profile)",
		"\n".join(_public_promo_lines()) or "(none running)",
		"## Upsell candidates (ITEM_CODE | name | pitch)",
		"\n".join(upsell_lines) or "(none: do not upsell)",
		"## Ready answers",
		"Jarz's saved replies. When the customer's message matches one, adapt it: keep its "
		"placeholders and its offer, fit the wording to the conversation, never paste two at once.",
		"\n\n".join(ready_lines),
	]
	brand = settings.get("brand_instructions")
	if brand:
		parts += ["## Brand instructions", str(brand)]
	data = {
		"text": "\n".join(parts),
		"prices": prices,
		"item_names": names,
		"delivery_fees": fees,
		"free_delivery_threshold": threshold,
		"candidates": candidates,
		"blocks": blocks,
	}
	try:
		frappe.cache().set_value(
			cache_key, json.dumps(data, ensure_ascii=False), expires_in_sec=CONTEXT_CACHE_SECONDS
		)
	except Exception:
		pass
	return data


# ── conversation transcript ─────────────────────────────────────────────


_MEDIA_LABEL = {
	"image": "[image]",
	"audio": "[voice note]",
	"video": "[video]",
	"document": "[document]",
	"sticker": "[sticker]",
	"location": "[location]",
	"reaction": "[reaction]",
	"unsupported": "[unsupported message]",
}


def transcript_lines(
	messages: Iterable[dict[str, Any]], mask: bool, names: Iterable[str | None]
) -> list[str]:
	"""``[Thu 29 Oct 19:02] Customer: ...`` lines, oldest first as given.

	The time prefix lets the model tell a reply a minute ago from one last week;
	a Business Suite placeholder becomes a plain marker instead of its bilingual
	"text not shared" notice.
	"""
	lines = []
	for msg in messages:
		who = "Customer" if msg.get("direction") == "Inbound" else "Jarz"
		mtype = str(msg.get("message_type") or "text")
		body = str(msg.get("body") or "").strip()
		if mtype == "location":
			body = ""
		if who == "Jarz" and body.startswith(SUITE_PLACEHOLDER_PREFIX):
			who, body = "Jarz (sent from Business Suite, text not available)", ""
			text = "..."
		else:
			if mask and body:
				body = mask_text(body, names)
			label = _MEDIA_LABEL.get(mtype)
			text = f"{label} {body}".strip() if label else body
		if text:
			when = _stamp(msg.get("timestamp"))
			lines.append(f"{when}{who}: {text}")
	return lines


def _stamp(value: Any) -> str:
	when = cc.to_dt(value)
	return when.strftime("[%a %d %b %H:%M] ") if when else ""


# ── customer profile ────────────────────────────────────────────────────


def describe_promo(promo: dict[str, Any], item_names: Iterable[str] = ()) -> str:
	"""The facts of a promo code in plain English, for the model. Pure."""
	kind = str(promo.get("discount_type") or "")
	value = float(promo.get("discount_value") or 0)
	items = [str(n) for n in item_names if n]
	if kind == "Free Delivery":
		what = "free delivery"
	elif kind == "Fixed Amount":
		what = f"{value:g} EGP off"
	elif value >= 100:
		what = "free (100% off)"
	else:
		what = f"{value:g}% off"
		if float(promo.get("max_discount_amount") or 0):
			what += f" up to {float(promo['max_discount_amount']):g} EGP"
	if items:
		what += " on " + ", ".join(items[:5])
	elif str(promo.get("applies_to") or "") not in ("", "Whole Order"):
		what += " on selected items"
	else:
		what += " on the order"
	parts = [what]
	minimum = float(promo.get("min_order_amount") or 0)
	parts.append(f"minimum order {minimum:g} EGP" if minimum else "no minimum order")
	if kind != "Free Delivery":
		parts.append("delivery fee charged as usual")
	if int(promo.get("allow_pickup") or 0):
		parts.append("pickup allowed")
	until = cc.to_dt(promo.get("active_to"))
	if until:
		parts.append("valid until " + until.strftime("%d %b %Y"))
	return "; ".join(parts)


def render_customer_profile(profile: dict[str, Any] | None, now: Any = None) -> str:
	"""The "Customer profile" block of the prompt. Pure.

	Money amounts are left out on purpose: the model must never type a number,
	and a lifetime total invites it to.
	"""
	if not profile:
		return "Customer profile: not linked to a customer record (nothing is known about them yet)."
	now_dt = cc.to_dt(now)
	lines = ["Customer profile (from Jarz records; never ask for what is already here):"]
	area = profile.get("area")
	if area:
		label = f"{area} / {profile['area_ar']}" if profile.get("area_ar") else str(area)
		if profile.get("area_deliverable"):
			lines.append(
				f"- Area on file: {label}. Quote {{{{delivery_fee:{area}}}}} for it and confirm it instead of asking."
			)
		else:
			lines.append(
				f"- Area on file: {label} (not one of our delivery areas: confirm where to deliver)."
			)
	if profile.get("address_on_file"):
		lines.append("- A delivery address is on file from an earlier order: ask them to confirm it.")
	orders = int(profile.get("total_orders") or 0)
	if orders:
		line = f"- Orders so far: {orders}"
		last = cc.to_dt(profile.get("last_order_date"))
		if last:
			line += f"; last on {last.strftime('%d %b %Y')}"
			if now_dt:
				line += f" ({max((now_dt.date() - last.date()).days, 0)} days ago)"
		if profile.get("last_order_items"):
			line += ": " + ", ".join(profile["last_order_items"][:6])
		lines.append(line + ".")
		if profile.get("last_order_state"):
			lines.append(f"- Their latest order is at «{profile['last_order_state']}» on our board.")
	else:
		lines.append("- No orders yet: a new customer.")
	if profile.get("usual_items"):
		lines.append("- Usually orders: " + ", ".join(profile["usual_items"][:3]) + ".")
	for offer in profile.get("offers") or []:
		lines.append(
			f"- Personal offer code {offer['code']}: {offer['facts']}. "
			f"Its wording is {{{{customer_offer:{offer['code']}}}}}."
		)
	for touch in profile.get("followups") or []:
		when = cc.to_dt(touch.get("at"))
		stamp = when.strftime("%a %d %b %H:%M") if when else "recently"
		gist = FOLLOWUP_GIST.get(str(touch.get("step") or ""), f"a '{touch.get('step')}' follow-up")
		line = f"- {stamp}: Jarz sent them {gist}"
		if touch.get("offer_code"):
			line += f" (code {touch['offer_code']})"
		if touch.get("outcome"):
			line += f", outcome: {touch['outcome']}"
		lines.append(line + ".")
	return "\n".join(lines)


def _promo_items(code: str) -> list[str]:
	rows = frappe.get_all(
		"Jarz Promo Code Item",
		filters={"parent": code, "parenttype": "Jarz Promo Code"},
		fields=["item_code", "item_group"],
		ignore_permissions=True,
	)
	names = []
	for row in rows:
		if row.item_code:
			names.append(frappe.db.get_value("Item", row.item_code, "item_name") or row.item_code)
		elif row.item_group:
			names.append(str(row.item_group))
	return names


def _usable_promo(promo: dict[str, Any], now: Any) -> bool:
	if not int(promo.get("enabled") or 0):
		return False
	if int(promo.get("usage_limit") or 0) and int(promo.get("times_used") or 0) >= int(promo["usage_limit"]):
		return False
	start, end = cc.to_dt(promo.get("active_from")), cc.to_dt(promo.get("active_to"))
	return (start is None or start <= now) and (end is None or end > now)


_PROMO_FIELDS = [
	"name",
	"enabled",
	"discount_type",
	"discount_value",
	"max_discount_amount",
	"min_order_amount",
	"applies_to",
	"allow_pickup",
	"usage_limit",
	"times_used",
	"active_from",
	"active_to",
]


def _customer_offers(customer: str, now: Any) -> list[dict[str, Any]]:
	"""Usable personal promo codes: ``[{code, facts, wording}]``."""
	from jarz_pos.services import customer_followups as fu

	rows = frappe.get_all(
		"Jarz Promo Code",
		filters={"customer": customer, "enabled": 1},
		fields=_PROMO_FIELDS,
		order_by="creation desc",
		limit_page_length=5,
		ignore_permissions=True,
	)
	offers = []
	for row in rows:
		if not _usable_promo(row, now):
			continue
		facts = describe_promo(row, _promo_items(row.name))
		try:
			wording = fu.offer_text_for_code(row.name)
		except Exception:
			wording = ""
		offers.append({"code": row.name, "facts": facts, "wording": wording or f"الكود {row.name}"})
	return offers


def _recent_followups(customer: str, now: Any) -> list[dict[str, Any]]:
	since = now - _dt.timedelta(days=FOLLOWUP_LOOKBACK_DAYS)
	rows = frappe.get_all(
		"Jarz Follow-up Touch",
		filters={"customer": customer, "status": "Done", "done_at": [">=", since]},
		fields=["step", "channel", "done_at", "offer_code", "outcome"],
		order_by="done_at asc",
		limit_page_length=5,
		ignore_permissions=True,
	)
	return [
		{
			"step": r.step,
			"at": r.done_at,
			"offer_code": r.offer_code,
			"outcome": r.outcome if r.channel != "Message" else None,
		}
		for r in rows
	]


def _last_invoice(customer: str) -> dict[str, Any] | None:
	fields = ["name", "posting_date", "customer_address"]
	try:
		if frappe.db.has_column("Sales Invoice", "custom_sales_invoice_state"):
			fields.append("custom_sales_invoice_state")
	except Exception:
		pass
	rows = frappe.get_all(
		"Sales Invoice",
		filters={"customer": customer, "docstatus": 1, "is_return": 0},
		fields=fields,
		order_by="posting_date desc, creation desc",
		limit_page_length=1,
		ignore_permissions=True,
	)
	if not rows:
		return None
	inv = rows[0]
	inv["items"] = frappe.get_all(
		"Sales Invoice Item",
		filters={"parent": inv.name, "parenttype": "Sales Invoice"},
		pluck="item_name",
		order_by="idx asc",
		ignore_permissions=True,
	)
	return inv


def _other_chat_parts(conv: Any, mask: bool, names: list[Any]) -> list[str]:
	"""The latest messages from the same customer's other chats (another channel). Never raises."""
	if not conv.get("customer"):
		return []
	try:
		others = frappe.get_all(
			cc.CONVERSATION,
			filters={"customer": conv.customer, "name": ["!=", conv.name]},
			fields=["name", "channel"],
			order_by="last_message_at desc",
			limit_page_length=3,
			ignore_permissions=True,
		)
		parts = []
		for other in others:
			rows = frappe.get_all(
				cc.MESSAGE,
				filters={"conversation": other.name},
				fields=["direction", "message_type", "body", "timestamp"],
				order_by="timestamp desc, creation desc",
				limit_page_length=OTHER_CHAT_MESSAGES,
				ignore_permissions=True,
			)
			rows.reverse()
			lines = transcript_lines(rows, mask, names)
			if lines:
				parts.append(
					f"Their latest messages in another chat ({other.channel}), oldest first:\n"
					+ "\n".join(lines)
				)
		return parts
	except Exception:
		return []


def _public_promo_lines() -> list[str]:
	"""``CODE | facts`` for every usable promo code not tied to one customer."""
	try:
		rows = frappe.get_all(
			"Jarz Promo Code",
			filters={"enabled": 1, "customer": ["is", "not set"]},
			fields=_PROMO_FIELDS,
			order_by="creation desc",
			limit_page_length=20,
			ignore_permissions=True,
		)
		now = cc.now()
		return [
			f"{r.name} | {describe_promo(r, _promo_items(r.name))}" for r in rows if _usable_promo(r, now)
		]
	except Exception:
		return []


def customer_profile(customer: str | None, now: Any = None) -> dict[str, Any] | None:
	"""Everything the draft should know about *customer*. Never raises; parts that fail are left out."""
	if not customer:
		return None
	now = cc.to_dt(now) or cc.now()
	card = cc.customer_card(customer)
	if not card:
		return None
	profile: dict[str, Any] = {
		"total_orders": card.get("total_orders") or 0,
		"last_order_date": card.get("last_order_date"),
		"usual_items": cc.top_items(customer, 3),
	}
	territory = card.get("territory")
	if territory:
		try:
			row = frappe.db.get_value(
				"Territory", territory, _territory_fields("name", "is_group", "delivery_income"), as_dict=True
			)
		except Exception:
			row = None
		if row and territory != "All Territories":
			profile["area"] = row.name
			profile["area_ar"] = str(row.get("custom_territory_name_ar") or "").strip() or None
			profile["area_deliverable"] = not int(row.is_group or 0) and row.delivery_income not in (None, "")
	parts = (
		("last", lambda: _last_invoice(customer)),
		("offers", lambda: _customer_offers(customer, now)),
		("followups", lambda: _recent_followups(customer, now)),
	)
	for key, load in parts:
		try:
			value = load()
		except Exception:
			continue
		if key == "last" and value:
			profile["last_order_items"] = [str(n) for n in value.get("items") or [] if n]
			placed = cc.to_dt(value.get("posting_date"))
			if placed and (now.date() - placed.date()).days <= RECENT_ORDER_DAYS:
				profile["last_order_state"] = value.get("custom_sales_invoice_state") or None
			profile["address_on_file"] = bool(value.get("customer_address"))
		elif key != "last":
			profile[key] = value
	return profile


# ── Claude call ─────────────────────────────────────────────────────────


def draft_input_from_content(content: Any) -> tuple[dict[str, Any] | None, bool]:
	"""The submit_draft input from a response, or the plain-text answer as a fallback.

	Returns ``(input, from_text)``. tool_choice stays "auto" (Sonnet 5.5 and Opus 5.5
	reject a forced tool with a 400), so the model may answer in text instead.
	"""
	texts: list[str] = []
	for block in content or []:
		kind = getattr(block, "type", None)
		if kind == "tool_use" and getattr(block, "name", "submit_draft") == "submit_draft":
			return getattr(block, "input", None), False
		if kind == "text":
			texts.append(str(getattr(block, "text", "") or ""))
	text = "\n".join(t.strip() for t in texts if t.strip())
	if not text:
		return None, False
	return {"reply": text, "intent": "Other", "is_complaint": False}, True


def _call_claude(api_key: str, model: str, context_text: str, user_text: str) -> dict[str, Any]:
	import anthropic

	client = anthropic.Anthropic(api_key=api_key, timeout=45.0, max_retries=1)
	started = time.monotonic()
	resp = client.messages.create(
		model=model,
		max_tokens=MAX_TOKENS,
		system=[
			{"type": "text", "text": SYSTEM_PROMPT},
			{"type": "text", "text": context_text, "cache_control": {"type": "ephemeral"}},
		],
		messages=[{"role": "user", "content": user_text}],
		tools=[TOOL],
		tool_choice={"type": "auto"},
		extra_body={"output_config": {"effort": DRAFT_EFFORT}},
	)
	latency = int((time.monotonic() - started) * 1000)
	tool_input, from_text = draft_input_from_content(resp.content)
	usage = getattr(resp, "usage", None)
	return {
		"input": tool_input,
		"from_text": from_text,
		"stop_reason": getattr(resp, "stop_reason", None),
		"latency_ms": latency,
		"input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
		"output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
		"cache_read_tokens": int(getattr(usage, "cache_read_input_tokens", 0) or 0),
		"cache_write_tokens": int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
	}


# ── pipeline ────────────────────────────────────────────────────────────


def _insert_draft(values: dict[str, Any]) -> str:
	doc = frappe.get_doc({"doctype": cc.DRAFT, **values})
	doc.insert(ignore_permissions=True)
	return doc.name


def _latest_inbound(conversation: str) -> str | None:
	rows = frappe.get_all(
		cc.MESSAGE,
		filters={"conversation": conversation, "direction": "Inbound"},
		pluck="name",
		order_by="timestamp desc, creation desc",
		limit_page_length=1,
		ignore_permissions=True,
	)
	return rows[0] if rows else None


def _failed(conversation: str, for_message: str | None, model: str, error: str) -> str:
	cc.supersede_live_drafts(conversation)
	name = _insert_draft(
		{
			"conversation": conversation,
			"for_message": for_message,
			"status": "Failed",
			"model": model,
			"error": str(error)[:1000],
			"draft_text": "",
		}
	)
	cc.publish_chat_update(conversation, "draft", draft=cc.load_draft(name))
	return name


def generate_draft(conversation: str, for_message: str | None = None, hint: str | None = None) -> str:
	"""Ask Claude for a reply to *conversation*. Returns the Draft name the moderator sees.

	A failure still produces a Draft (status Failed, with the error) and a
	realtime event, so the app can show "draft failed, write it yourself".
	"""
	from jarz_pos.services.chat_settings import get_chat_settings

	settings = get_chat_settings()
	model = settings.get("draft_model") or "claude-haiku-5-5"
	conv = frappe.db.get_value(
		cc.CONVERSATION,
		conversation,
		["name", "contact_name", "customer", "channel", "customer_summary"],
		as_dict=True,
	)
	if not conv:
		frappe.throw(f"Conversation {conversation} not found", frappe.DoesNotExistError)
	for_message = for_message or _latest_inbound(conversation)

	api_key = settings.get("anthropic_api_key")
	if not api_key:
		return _failed(
			conversation, for_message, model, "Anthropic API key is not configured in Jarz Chat Settings"
		)

	try:
		customer_name = (
			frappe.db.get_value("Customer", conv.customer, "customer_name") if conv.customer else None
		)
		rows = frappe.get_all(
			cc.MESSAGE,
			filters={"conversation": conversation},
			fields=["direction", "message_type", "body", "timestamp"],
			order_by="timestamp desc, creation desc",
			limit_page_length=HISTORY_MESSAGES,
			ignore_permissions=True,
		)
		rows.reverse()
		mask = bool(settings.get("mask_personal_data", True))
		names = [conv.contact_name, customer_name]
		lines = transcript_lines(rows, mask, names)
		context = build_context(settings)
		now = cc.now()
		profile = customer_profile(conv.customer, now)
		user_parts = [
			f"Channel: {conv.channel}",
			"Now: " + now.strftime("%a %d %b %Y %H:%M"),
			render_customer_profile(profile, now),
		]
		if conv.customer_summary:
			user_parts.append(
				"Notes on this customer from all their past chats and orders (rewritten nightly):\n"
				+ str(conv.customer_summary)
			)
		user_parts += _other_chat_parts(conv, mask, names)
		user_parts += ["Conversation (oldest first):", "\n".join(lines) or "(no text messages)"]
		if hint:
			hint_text = mask_text(hint, names) if mask else str(hint)
			user_parts.append(f"Moderator's instruction for this draft: {hint_text[:500]}")
		user_parts.append(
			"Draft the next reply to the customer's latest message and submit it with submit_draft."
		)
		result = _call_claude(api_key, model, context["text"], "\n\n".join(user_parts))
	except Exception as exc:
		message = f"{type(exc).__name__}: {str(exc)[:500]}"
		cc.safe_log_error("chat: draft generation failed", f"conversation={conversation} {message}")
		return _failed(conversation, for_message, model, message)

	if not result.get("input"):
		reason = "The model returned no draft"
		if result.get("stop_reason") == "max_tokens":
			reason += " (it ran out of tokens)"
		return _failed(conversation, for_message, model, reason)

	clean, warnings = sanitize_tool_input(result["input"], context.get("candidates") or [])
	if result.get("from_text"):
		warnings.append("The model answered without the draft tool: check the reply and intent")
	template = clean["reply"]
	ctx = dict(context)
	ctx["customer_first_name"] = first_name(customer_name, conv.contact_name)
	ctx["customer_offers"] = {o["code"]: o["wording"] for o in (profile or {}).get("offers") or []}
	ctx["arabic"] = is_arabic(template) or not template
	draft_text, fill_warnings, filled = fill_placeholders(template, ctx)
	warnings += fill_warnings + typed_number_warnings(template)
	upsell_text = None
	if clean["upsell_line"]:
		upsell_text, upsell_warnings, _f = fill_placeholders(clean["upsell_line"], ctx)
		warnings += upsell_warnings + typed_number_warnings(clean["upsell_line"])
	if not draft_text:
		warnings.append("The draft is empty")
	warnings = list(dict.fromkeys(warnings))

	base = {
		"conversation": conversation,
		"for_message": for_message,
		"template_text": template,
		"draft_text": draft_text,
		"upsell_item": clean["upsell_item_code"],
		"upsell_text": upsell_text,
		"intent": clean["intent"],
		"is_complaint": 1 if clean["is_complaint"] else 0,
		"needs_human_reason": clean["needs_human_reason"],
		"warnings": "\n".join(warnings),
		"model": model,
		"input_tokens": result["input_tokens"],
		"output_tokens": result["output_tokens"],
		"cache_read_tokens": result["cache_read_tokens"],
		"latency_ms": result["latency_ms"],
		"cost_usd": cost_usd(
			model,
			result["input_tokens"],
			result["output_tokens"],
			result["cache_read_tokens"],
			result["cache_write_tokens"],
		),
	}

	cc.supersede_live_drafts(conversation)

	visible = None
	percent = float(settings.get("planted_draft_percent") or 0)
	if percent > 0 and filled and not clean["is_complaint"] and random.random() < percent / 100.0:
		mutation = plant_mutation(template, ctx)
		if mutation:
			planted_text, planted_error, _wrong = mutation
			real = _insert_draft({**base, "status": "Held", "is_planted": 0})
			visible = _insert_draft(
				{
					**base,
					"status": "Pending",
					"draft_text": planted_text,
					"is_planted": 1,
					"planted_error": planted_error,
					"real_draft": real,
					# Token cost is booked once, on the real draft.
					"input_tokens": 0,
					"output_tokens": 0,
					"cache_read_tokens": 0,
					"cost_usd": 0,
				}
			)
	if visible is None:
		visible = _insert_draft({**base, "status": "Pending", "is_planted": 0})

	frappe.db.set_value(
		cc.CONVERSATION,
		conversation,
		{"intent": clean["intent"], "is_complaint": 1 if clean["is_complaint"] else 0},
		update_modified=False,
	)
	cc.publish_chat_update(conversation, "draft", draft=cc.load_draft(visible))
	return visible


def generate_draft_job(conversation: str, for_message: str | None = None) -> str | None:
	"""Background job enqueued by ingestion. Never raises.

	Debounced by data: when a newer inbound message has arrived since this job
	was queued, that message's own job drafts the reply, so this one stops.
	"""
	try:
		if frappe.session.user in (None, "", "Guest"):
			# nosemgrep: frappe-setuser -- background job (RQ worker) promotes a Guest enqueuer; worker calls frappe.destroy() after every job, so nothing leaks
			frappe.set_user("Administrator")
		if for_message:
			latest = _latest_inbound(conversation)
			if latest and latest != for_message:
				return None
		state = frappe.db.get_value(cc.CONVERSATION, conversation, ["status", "last_direction"], as_dict=True)
		if not state or state.status == "Closed" or state.last_direction == "Outbound":
			return None
		name = generate_draft(conversation, for_message=for_message)
		frappe.db.commit()
		return name
	except Exception:
		try:
			frappe.db.rollback()
		except Exception:
			pass
		cc.safe_log_error("chat: draft job failed", f"conversation={conversation}\n{frappe.get_traceback()}")
		return None
