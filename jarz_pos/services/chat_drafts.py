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
1. Reply in the customer's language. Default to Egyptian Arabic. Short (1-4 sentences), warm, \
friendly, in the Jarz voice. No long lists unless the customer asked for the menu.
2. NEVER type a price, fee or amount as a number. Use only these placeholders, exactly:
   {{price:ITEM_CODE}} for an item or bundle price (ITEM_CODE from the menu),
   {{delivery_fee:TERRITORY_NAME}} for a delivery fee (TERRITORY_NAME from the delivery list),
   {{free_delivery_threshold}} for the free-delivery minimum,
   {{customer_first_name}} to greet the customer by name.
   Personal data in the conversation is masked as [PHONE], [EMAIL], [LINK], [CUSTOMER]; never \
repeat those tokens.
3. Suggest an upsell only from the upsell candidates list, and only when it fits naturally. Put \
it in upsell_item_code / upsell_line, not in the reply.
4. No health, nutrition, diet, sugar-free or allergy-safety claims. If asked, say a team member \
will confirm.
5. Complaints (late, wrong, damaged, bad taste, rude courier...): apologise sincerely, ask for \
the order details if missing, promise that the team will look into it. Never promise a refund, \
replacement, discount or compensation. Set is_complaint true.
6. Do not invent products, flavours, branches, opening hours or delivery times that are not in \
the context.
7. When you are unsure, or the request needs a human (custom order, payment problem, refund \
request, angry customer), still write a safe holding reply and fill needs_human_reason.
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
		warnings.append(f"Unknown placeholder: {match.group(0)}")
		return UNKNOWN

	out = _PLACEHOLDER.sub(_sub, text)
	# An empty first name leaves "أهلا ،" / "Hi ,": tidy the gap it left.
	out = re.sub(r"[ \t]+([،,!.؟?])", r"\1", out)
	out = re.sub(r"[ \t]{2,}", " ", out).strip()
	return out, warnings, filled


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


def _territories() -> list[dict[str, Any]]:
	fields = ["name", "delivery_income"]
	try:
		if frappe.db.has_column("Territory", "custom_territory_name_ar"):
			fields.append("custom_territory_name_ar")
	except Exception:
		pass
	try:
		rows = frappe.get_all(
			"Territory", filters={"is_group": 0}, fields=fields, order_by="name asc", ignore_permissions=True
		)
	except Exception:
		return []
	return [r for r in rows if r.get("delivery_income") not in (None, "")]


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
	cache_key = f"jarz_chat_ctx:{profile}"
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
	if profile:
		from jarz_pos.api import pos as pos_api

		try:
			with _quiet():
				products = pos_api.get_profile_products(profile)
		except Exception:
			products = []
		for item in products or []:
			code = str(item.get("id") or "")
			if not code:
				continue
			price = float(item.get("price") or 0)
			prices[code] = price
			names[str(item.get("name") or "").strip().lower()] = code
			menu_lines.append(f"{code} | {item.get('name') or code} | {item.get('item_group') or ''}")
		try:
			with _quiet():
				bundles = pos_api.get_profile_bundles(profile)
		except Exception:
			bundles = []
		for bundle in bundles or []:
			code = str(bundle.get("erpnext_item") or "")
			if not code:
				continue
			prices[code] = float(bundle.get("price") or 0)
			names[str(bundle.get("name") or "").strip().lower()] = code
			free = " | free delivery" if bundle.get("free_shipping") else ""
			menu_lines.append(f"{code} | {bundle.get('name') or code} | Bundle{free}")

	fees: dict[str, float] = {}
	territory_lines: list[str] = []
	for row in _territories():
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
