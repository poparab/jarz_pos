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
import io
import json
import random
import re
import time
from collections.abc import Iterable
from typing import Any

import frappe

from jarz_pos.services import chat_common as cc

INTENTS = ("Inquiry", "Order", "Complaint", "Other")
HISTORY_MESSAGES = 20
CONTEXT_CACHE_SECONDS = 600
MAX_TOKENS = 600
UNKNOWN = "[؟]"
TYPED_NUMBER_WARNING = "AI typed a number — check it"
EDIT_DISTANCE_CAP = 2000

#: USD per million tokens: (input, output).
PRICES = {
	"claude-haiku-5-5": (1.0, 5.0),
	"claude-sonnet-5-5": (3.0, 15.0),
}
CACHE_READ_FACTOR = 0.1
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
	r"min|mins|minutes?|hours?|hrs?|يوم|أيام|ايام|days?|قطعة|قطع|حبة|pcs|pieces?)",
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
never جرة. Short (1-4 sentences) unless you use {{menu}}, {{offers}} or {{delivery_areas}}.
2. NEVER type a price, fee or amount as a number. Use only these placeholders, exactly:
   {{price:ITEM_CODE}} for an item or bundle price (ITEM_CODE from the menu),
   {{delivery_fee:TERRITORY_NAME}} for a delivery fee (TERRITORY_NAME from the delivery list),
   {{free_delivery_threshold}} for the free-delivery minimum,
   {{customer_first_name}} to greet the customer by name,
   {{menu}} for the full priced menu (when they ask for the menu or "the prices"),
   {{offers}} for every live bundle with what is inside and its price, plus free delivery,
   {{delivery_areas}} for the list of areas we deliver to.
   Each block placeholder expands to several lines: put it on its own line, use each one at most \
once, and never retype or summarise what it contains.
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

Jarz voice (classy, warm, never pushy):
- Lead with the feeling, then the fact. Sell the moment the customer wants to be in: guests \
arriving and the table done without effort, a gift that shows you thought about them, the \
reward after a long week, the first spoon through the layers. One feeling per reply, matched \
to what they said: a gift → the person receiving it; guests → the gathering; a treat for \
themselves → they earned it.
- The product facts you may lean on: 7 visible layers in the cheesecakes, a reusable glass jar \
with a lid, ready to eat with no plate or cutting, natural ingredients «مكونات طبيعية», no \
preservatives «من غير أي مواد حافظة», sugar «سكر أهدى». Never "100%", never "sealed", never \
"light", "diet", "healthy" or a sugar number.
- Thank them the way they thank the hands: «تسلم إيدكم» → «تسلم حضرتك، ده من ذوقك 💛». Mirror \
their warmth; never a corporate template.
- Desire, not pressure: no countdowns, «العرض هيخلص», "limited", "hurry". Say prices plainly, \
never apologetically. If they find it pricey, say it is «costly شوية عشان المكونات طبيعية» and \
point to a bundle in {{offers}} that fits, without arguing.
- Recommend: when they hesitate or ask what to choose, name 1-2 flavours (Blueberry and \
Chocolate Hazelnut are the favourites) or the bundle that fits their headcount.
- Close every sale-shaped reply with one easy next step: ask for the order, or name, phone and \
detailed address in one message.
- Emoji: 1-2, from 💛 🍰 🚚 😍 🥰 ✨. None in a complaint reply.
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

BLOCK_PLACEHOLDERS = ("menu", "offers", "delivery_areas")

#: Used when Jarz Chat Settings has no Ready Answers rows. Written from the
#: replies Jarz moderators sent most often on Instagram, in their own format.
DEFAULT_READY_ANSWERS: list[dict[str, str]] = [
	{
		"topic": "Greeting + menu",
		"when_to_use": "First message, «السلام عليكم», «المنيو», «عايز أطلب», a reply to an ad",
		"answer": "أهلاً بيك في Jarz 💛 عندنا عروض حلوة دلوقتي على المنيو\n\n📜 المنيو:\n{{menu}}\n\n"
		"🎁 العروض:\n{{offers}}\n\nتحب تطلب إيه؟ 🍰",
	},
	{
		"topic": "Prices",
		"when_to_use": "«بكام»، «الأسعار»، «سعر البرطمان الكبير»",
		"answer": "الأسعار يا فندم:\n{{menu}}\n\nولو هتاخد أكتر من برطمان، العروض أوفر لحضرتك:\n{{offers}}",
	},
	{
		"topic": "Offers / bundles",
		"when_to_use": "«في عروض؟»، «العرض ده لسه شغال؟»، asking about a box or a bundle",
		"answer": "أكيد يا فندم 😍 دي العروض الشغالة دلوقتي:\n{{offers}}\n\n"
		"تقدر تختار النكهات اللي تحبها جوه البوكس. تحب نجهزلك أنهي واحد؟",
	},
	{
		"topic": "Delivery areas",
		"when_to_use": "«بتوصلوا فين؟»، «بتوصلوا [منطقة]؟»، «التوصيل بكام؟»",
		"answer": "بنوصل حالياً للمناطق دي:\n{{delivery_areas}}\n\n"
		"ابعتلنا منطقتك ونقولك مصاريف التوصيل، واحنا جاهزين نخدمك 🍰✨",
	},
	{
		"topic": "Outside our areas",
		"when_to_use": "An area that is not in the delivery list, another governorate",
		"answer": "للأسف يا فندم مش بنوصل للمنطقة دي حالياً 😔 "
		"لو في عنوان تاني لحضرتك في المناطق دي هنوصله بكل سرور:\n{{delivery_areas}}",
	},
	{
		"topic": "Delivery time",
		"when_to_use": "«هيوصل إمتى؟»، «عندي ضيوف»، «محتاجه النهارده»",
		"answer": "الأوردر بيوصل لحضرتك من 30 دقيقة لحد ساعتين بالكتير من تأكيده 🚚 "
		"ولو محتاجه في معاد معين قولنا.",
	},
	{
		"topic": "How to order",
		"when_to_use": "The customer chose what they want, or asks how to order",
		"answer": "تمام يا فندم 💛 ابعتلنا في رسالة واحدة: الاسم، رقم الموبايل، والعنوان بالتفصيل "
		"(المنطقة، الشارع، رقم العمارة والشقة)، ونأكدلك الأوردر على طول.",
	},
	{
		"topic": "Compliment",
		"when_to_use": "«تحفة»، «تسلم إيدكم»، praise after an order",
		"answer": "تسلم حضرتك 💛 احنا مبسوطين جداً إن Jarz عجبك، وكلامك ده بيفرق معانا. "
		"مستنيينك في الأوردر الجاي 🥰",
	},
	{
		"topic": "Complaint",
		"when_to_use": "Late, melted, broken jar, wrong item, taste",
		"answer": "حقك علينا يا فندم، وآسفين جداً على اللي حصل. ممكن رقم الأوردر أو الاسم والرقم "
		"اللي اتطلب بيه؟ هنراجع الموضوع حالاً ونرجعلك.",
	},
	{
		"topic": "Gift",
		"when_to_use": "A gift, delivery to someone else, «من غير فاتورة»",
		"answer": "فكرة حلوة جداً 💛 نقدر نوصلها للشخص اللي حضرتك عايز تهاديه. "
		"ابعتلنا اسمه ورقمه والعنوان بالتفصيل، وقولنا لو في أي طلب خاص للهدية.",
	},
	{
		"topic": "Large or company order",
		"when_to_use": "An event, a company, a very large quantity, a custom request",
		"answer": "يا أهلاً بيك 💛 للطلبات الكبيرة والشركات واحد من الفريق هيتواصل مع حضرتك "
		"ويظبط معاك كل التفاصيل. ممكن تقولنا العدد تقريباً والمعاد؟",
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
	price_in, price_out = PRICES.get(str(model or ""), PRICES["claude-haiku-5-5"])
	total = (
		int(input_tokens or 0) * price_in
		+ int(output_tokens or 0) * price_out
		+ int(cache_read or 0) * price_in * CACHE_READ_FACTOR
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
	cache_key = f"jarz_chat_ctx:v2:{profile}"
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
	bundles: list[dict[str, Any]] = []
	if profile:
		from jarz_pos.api import pos as pos_api

		try:
			with _quiet():
				products = pos_api.get_profile_products(profile) or []
		except Exception:
			products = []
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
		}
		for lang, arabic in (("ar", True), ("en", False))
	}

	ready_lines = []
	for row in settings.get("ready_answers") or DEFAULT_READY_ANSWERS:
		when = f" (when: {row['when_to_use']})" if row.get("when_to_use") else ""
		ready_lines.append(f"### {row['topic']}{when}\n{row['answer']}")

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
	lines = []
	for msg in messages:
		who = "Customer" if msg.get("direction") == "Inbound" else "Jarz"
		mtype = str(msg.get("message_type") or "text")
		body = str(msg.get("body") or "").strip()
		if mtype == "location":
			body = ""
		if mask and body:
			body = mask_text(body, names)
		label = _MEDIA_LABEL.get(mtype)
		text = f"{label} {body}".strip() if label else body
		if text:
			lines.append(f"{who}: {text}")
	return lines


def _history_block(customer: str | None) -> str:
	if not customer:
		return "Customer history: not linked to a customer record."
	card = cc.customer_card(customer) or {}
	top = cc.top_items(customer, 3)
	return (
		"Customer history: "
		f"{card.get('total_orders') or 0} previous orders"
		+ (f", last on {card.get('last_order_date')}" if card.get("last_order_date") else "")
		+ (f", usually orders: {', '.join(top)}" if top else "")
		+ "."
	)


# ── Claude call ─────────────────────────────────────────────────────────


def _call_claude(api_key: str, model: str, context_text: str, user_text: str) -> dict[str, Any]:
	import anthropic

	client = anthropic.Anthropic(api_key=api_key, timeout=20.0, max_retries=1)
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
		tool_choice={"type": "tool", "name": "submit_draft"},
	)
	latency = int((time.monotonic() - started) * 1000)
	tool_input = None
	for block in resp.content or []:
		if getattr(block, "type", None) == "tool_use":
			tool_input = getattr(block, "input", None)
			break
	usage = getattr(resp, "usage", None)
	return {
		"input": tool_input,
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
		cc.CONVERSATION, conversation, ["name", "contact_name", "customer", "channel"], as_dict=True
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
		user_parts = [
			f"Channel: {conv.channel}",
			_history_block(conv.customer),
			"Conversation (oldest first):",
			"\n".join(lines) or "(no text messages)",
		]
		if hint:
			hint_text = mask_text(hint, names) if mask else str(hint)
			user_parts.append(f"Moderator's instruction for this draft: {hint_text[:500]}")
		user_parts.append("Draft the next reply to the customer's latest message.")
		result = _call_claude(api_key, model, context["text"], "\n\n".join(user_parts))
	except Exception as exc:
		message = f"{type(exc).__name__}: {str(exc)[:500]}"
		cc.safe_log_error("chat: draft generation failed", f"conversation={conversation} {message}")
		return _failed(conversation, for_message, model, message)

	if not result.get("input"):
		return _failed(conversation, for_message, model, "The model returned no draft")

	clean, warnings = sanitize_tool_input(result["input"], context.get("candidates") or [])
	template = clean["reply"]
	ctx = dict(context)
	ctx["customer_first_name"] = first_name(customer_name, conv.contact_name)
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
