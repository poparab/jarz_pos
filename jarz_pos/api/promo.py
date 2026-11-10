"""Jarz POS – Promo-code preview API.

Read-only endpoints used by the Flutter / Desk clients:

* ``validate_promo_codes`` -- preview promo-code discounts against an
  in-memory cart before the invoice is created.
* ``lookup_promo_code`` -- staff type a code and see what the offer is, and
  optionally what it would do to an existing order.

Neither performs any database write.
"""

from __future__ import annotations

import datetime
import json

import frappe

from jarz_pos.services import promo_codes as _promo


def _parse_json_param(value):
	if value is None:
		return None
	if isinstance(value, (list, dict)):
		return value
	if isinstance(value, str):
		text = value.strip()
		if not text:
			return None
		try:
			return json.loads(text)
		except Exception:
			return value
	return value


@frappe.whitelist()
def validate_promo_codes():
	"""Preview promo-code discounts for an in-memory cart (read-only).

	form_dict params:
	    codes        : JSON list or CSV of promo codes
	    cart_json    : JSON list of {item_code, qty, rate, discount_percentage?}
	    customer     : optional Customer name
	    pos_profile  : optional POS Profile name
	    channel      : default "flutter"
	    pickup       : truthy flag
	    amended_from : optional Sales Invoice being amended; its own redemptions
	                   are not counted, so a single-use code the order already
	                   carries does not preview as used up (the redemption is
	                   reversed when the amendment cancels it)
	"""
	fd = frappe.form_dict

	codes = _parse_json_param(fd.get("codes"))
	cart = _parse_json_param(fd.get("cart_json")) or []
	customer = fd.get("customer")
	pos_profile_name = fd.get("pos_profile")
	channel = (fd.get("channel") or "flutter").strip().lower()

	raw_pickup = fd.get("pickup")
	if isinstance(raw_pickup, str):
		is_pickup = raw_pickup.strip().lower() in {"1", "true", "yes", "on"}
	else:
		is_pickup = bool(raw_pickup)

	if not isinstance(cart, list):
		cart = []

	# Build an unsaved Sales Invoice for evaluation only (never inserted/saved).
	invoice_doc = frappe.new_doc("Sales Invoice")
	if customer:
		invoice_doc.customer = customer
	# evaluate_promo_codes skips redemptions held by invoice_doc.name; the doc is
	# never saved, so borrowing the source's name only affects that count.
	amended_from = str(fd.get("amended_from") or "").strip()
	if amended_from and frappe.db.exists("Sales Invoice", amended_from):
		invoice_doc.name = amended_from

	# Resolve company defensively from POS profile or global defaults.
	company = None
	try:
		if pos_profile_name and frappe.db.exists("POS Profile", pos_profile_name):
			company = frappe.db.get_value("POS Profile", pos_profile_name, "company")
	except Exception:
		company = None
	if not company:
		try:
			company = frappe.defaults.get_global_default("company")
		except Exception:
			company = None
	if company:
		invoice_doc.company = company

	customer_doc = None
	if customer:
		try:
			customer_doc = frappe.get_doc("Customer", customer)
		except Exception:
			customer_doc = None

	# Populate items from the cart with rate + qty (no pricing recalculation).
	for row in cart:
		if not isinstance(row, dict):
			continue
		item_code = row.get("item_code")
		if not item_code:
			continue
		qty = float(row.get("qty") or 0)
		rate = float(row.get("rate") or 0)
		discount_pct = float(row.get("discount_percentage") or 0)
		amount = rate * qty * (1 - min(max(discount_pct, 0.0), 100.0) / 100.0)
		item_group = None
		try:
			item_group = frappe.get_cached_value("Item", item_code, "item_group")
		except Exception:
			item_group = None
		child = invoice_doc.append("items", {})
		child.item_code = item_code
		child.qty = qty
		child.rate = rate
		child.discount_percentage = discount_pct
		child.amount = round(amount, 2)
		if item_group is not None:
			child.item_group = item_group

	evaluation = _promo.evaluate_promo_codes(
		invoice_doc,
		codes,
		customer=customer,
		customer_doc=customer_doc,
		pos_profile=pos_profile_name,
		channel=channel,
		is_pickup=is_pickup,
	)

	return {
		"results": [
			{
				"code": r.code,
				"accepted": r.accepted,
				"discount_type": r.discount_type,
				"discount_amount": r.discount_amount,
				"free_delivery": r.free_delivery,
				"reason": r.reason,
			}
			for r in evaluation.results
		],
		"total_discount": evaluation.total_discount,
		"free_delivery": evaluation.free_delivery,
		"eligible_net_total": evaluation.eligible_net_total,
		"capped": evaluation.capped,
	}


# ---------------------------------------------------------------------------
# Promo-code lookup (read-only)
# ---------------------------------------------------------------------------

# Same tax-row test ``apply_promo_evaluation_to_invoice`` uses to waive
# delivery on a Free Delivery code, so ``delivery_amount`` is exactly what such
# a code would remove from the order.
_DELIVERY_TAX_PREFIXES = ("shipping income", "delivery charges")
_RECENT_USES_LIMIT = 5


def _val(obj, field_name):
	"""Read a field from a Document / frappe._dict / dict / plain object."""
	if obj is None:
		return None
	if isinstance(obj, dict):
		return obj.get(field_name)
	getter = getattr(obj, "get", None)
	if callable(getter):
		try:
			return getter(field_name)
		except Exception:
			pass
	return getattr(obj, field_name, None)


def _num(value) -> float:
	try:
		return float(value or 0)
	except (TypeError, ValueError):
		return 0.0


def _int(value) -> int:
	try:
		return int(float(value or 0))
	except (TypeError, ValueError):
		return 0


def _text(value):
	text = str(value).strip() if value is not None else ""
	return text or None


def _as_datetime(value):
	if not value:
		return None
	if isinstance(value, datetime.datetime):
		return value
	if isinstance(value, datetime.date):
		return datetime.datetime.combine(value, datetime.time.min)
	try:
		return frappe.utils.get_datetime(value)
	except Exception:
		return None


def _datetime_str(value):
	dt = _as_datetime(value)
	return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else None


def _date_str(value):
	if not value:
		return None
	if isinstance(value, (datetime.date, datetime.datetime)):
		return value.strftime("%Y-%m-%d")
	return str(value)[:10]


def _woo_id(value):
	"""Woo order id as int, or None for empty / 0 / non-numeric."""
	text = str(value).strip() if value is not None else ""
	if not text.isdigit():
		return None
	number = int(text)
	return number or None


def _find_promo_name(code: str):
	"""Match the doc name, ``code`` or ``woo_coupon_code``, case-insensitively.

	MariaDB's default collation already compares case-insensitively; the upper
	and lower spellings are tried as well so the match never depends on it.
	"""
	candidates: list[str] = []
	for spelling in (code, code.upper(), code.lower()):
		if spelling and spelling not in candidates:
			candidates.append(spelling)
	for field_name in ("name", "code", "woo_coupon_code"):
		for spelling in candidates:
			name = frappe.db.get_value("Jarz Promo Code", {field_name: spelling}, "name")
			if name:
				return name
	return None


def _promo_status(promo, active_uses: int, now_dt) -> str:
	"""disabled -> not_started -> expired -> used_up -> active."""
	if not _int(_val(promo, "enabled")):
		return "disabled"
	active_from = _as_datetime(_val(promo, "active_from"))
	if active_from and active_from > now_dt:
		return "not_started"
	active_to = _as_datetime(_val(promo, "active_to"))
	if active_to and active_to < now_dt:
		return "expired"
	usage_limit = _int(_val(promo, "usage_limit"))
	if usage_limit > 0 and active_uses >= usage_limit:
		return "used_up"
	return "active"


def _applies_to(promo) -> str:
	return (_val(promo, "applies_to") or "Whole Order").strip() or "Whole Order"


def _scope_rows(promo, item_labels=None) -> list[dict]:
	"""The item scope the engine actually honours for this promo.

	``_eligible_net_for`` ignores the scope table on Whole Order codes, reads
	only Item rows on Specific Items codes and only Item Group rows on Item
	Groups codes; listing the other rows would advertise a scope that is not
	applied.

	``item_labels`` (item code -> item name) lets a caller describing many
	codes fetch every Item name in one query instead of one per scope row.
	"""
	applies_to = _applies_to(promo)
	wanted = {"Specific Items": "Item", "Item Groups": "Item Group"}.get(applies_to)
	if not wanted:
		return []
	out: list[dict] = []
	seen: set[str] = set()
	for row in _val(promo, "item_scope") or []:
		scope_type = (_val(row, "scope_type") or "").strip()
		if scope_type != wanted:
			continue
		name = _val(row, "item_code") if scope_type == "Item" else _val(row, "item_group")
		if not name or name in seen:
			continue
		seen.add(name)
		label = name
		if scope_type == "Item":
			if item_labels is not None:
				label = item_labels.get(name) or name
			else:
				label = frappe.db.get_value("Item", name, "item_name") or name
		out.append({"type": scope_type, "name": name, "label": label})
	return out


def _channel_list(promo) -> list[str]:
	out: list[str] = []
	for row in _val(promo, "channels") or []:
		channel = (_val(row, "channel") or "").strip().lower()
		if channel and channel not in out:
			out.append(channel)
	return out


def _code_summary_base(promo, promo_name, times_used: int, now_dt, item_labels=None) -> dict:
	"""The code description shared by ``lookup_promo_code`` and
	``lookup_customer_promos``.

	The key order is part of ``lookup_promo_code``'s response (it splices this
	dict between ``found`` and ``customer``); do not reorder.
	"""
	usage_limit = _int(_val(promo, "usage_limit"))
	return {
		"code": _val(promo, "code") or promo_name,
		"title": _text(_val(promo, "title")),
		"description": _text(_val(promo, "description")),
		"status": _promo_status(promo, times_used, now_dt),
		"discount_type": _text(_val(promo, "discount_type")),
		"discount_value": _num(_val(promo, "discount_value")),
		"max_discount_amount": _num(_val(promo, "max_discount_amount")),
		"min_order_amount": _num(_val(promo, "min_order_amount")),
		"min_on_whole_order": bool(_int(_val(promo, "min_on_whole_order"))),
		"applies_to": _applies_to(promo),
		"scope": _scope_rows(promo, item_labels),
		"channels": _channel_list(promo),
		"allow_pickup": bool(_int(_val(promo, "allow_pickup"))),
		"stackable": bool(_int(_val(promo, "stackable"))),
		"usage_limit": usage_limit,
		"times_used": times_used,
		"remaining_uses": max(usage_limit - times_used, 0) if usage_limit > 0 else None,
		"per_customer_limit": _int(_val(promo, "per_customer_limit")),
		"active_from": _datetime_str(_val(promo, "active_from")),
		"active_to": _datetime_str(_val(promo, "active_to")),
	}


def _customer_name(customer):
	if not customer:
		return None
	return frappe.db.get_value("Customer", customer, "customer_name") or customer


def _recent_uses(promo_name: str) -> list[dict]:
	rows = frappe.get_all(
		"Jarz Promo Redemption",
		filters={"promo_code": promo_name},
		fields=["sales_invoice", "woo_order_id", "customer", "status", "creation"],
		order_by="creation desc",
		limit_page_length=_RECENT_USES_LIMIT,
	)
	invoice_names = [r.get("sales_invoice") for r in rows if r.get("sales_invoice")]
	invoices: dict = {}
	if invoice_names:
		for inv in frappe.get_all(
			"Sales Invoice",
			filters={"name": ["in", invoice_names]},
			fields=["name", "customer_name", "posting_date", "woo_order_id"],
		):
			invoices[inv.get("name")] = inv

	out: list[dict] = []
	for row in rows:
		inv = invoices.get(row.get("sales_invoice")) or {}
		customer_name = inv.get("customer_name") or _customer_name(row.get("customer"))
		out.append(
			{
				"invoice": row.get("sales_invoice"),
				"woo_order_id": _woo_id(row.get("woo_order_id")) or _woo_id(inv.get("woo_order_id")),
				"customer_name": customer_name,
				"date": _date_str(inv.get("posting_date") or row.get("creation")),
				"status": row.get("status"),
			}
		)
	return out


def _resolve_invoice_name(order):
	"""A Woo order number (latest submitted, else draft) or a Sales Invoice name."""
	text = str(order or "").strip().lstrip("#").strip()
	if not text:
		return None
	if text.isdigit():
		woo_id = int(text)
		for docstatus in (1, 0):
			names = frappe.get_all(
				"Sales Invoice",
				filters={"woo_order_id": woo_id, "docstatus": docstatus, "is_return": 0},
				pluck="name",
				order_by="creation desc",
				limit_page_length=1,
			)
			if names:
				return names[0]
	return frappe.db.exists("Sales Invoice", text) or None


def _delivery_amount(invoice_doc) -> float:
	total = 0.0
	for tax in _val(invoice_doc, "taxes") or []:
		desc = (_val(tax, "description") or "").strip().lower()
		if desc.startswith(_DELIVERY_TAX_PREFIXES):
			total += _num(_val(tax, "tax_amount"))
	return round(total, 2)


def _can_read_invoice(doc) -> bool:
	"""Same gate the Kanban reads use: Sales Invoice read permission on this
	document, then the user's branch (POS profile) scope."""
	try:
		if not frappe.has_permission("Sales Invoice", ptype="read", doc=doc):
			return False
	except Exception:
		return False
	try:
		_ensure_branch_access(doc)
	except Exception:
		return False
	return True


def _ensure_branch_access(doc) -> None:
	"""Raises when the order belongs to a branch the user does not work at."""
	from jarz_pos.utils.access_control import ensure_profile_scoped_invoice_access

	ensure_profile_scoped_invoice_access(doc, action_label="checking a promo code on this order")


_TERRITORY_INCOME_FIELDS = ("shipping_income", "delivery_income", "courier_income", "shipping_income_amount")


def _territory_delivery_income(territory) -> float:
	"""The territory's configured delivery fee (same fields Kanban reads)."""
	if not territory:
		return 0.0
	try:
		terr = frappe.get_doc("Territory", territory)
	except Exception:
		return 0.0
	for field_name in _TERRITORY_INCOME_FIELDS:
		value = _val(terr, field_name)
		if value is not None:
			return round(_num(value), 2)
	return 0.0


def _waived_delivery(doc, current_codes, delivery_amount: float, is_pickup: bool) -> float:
	"""Delivery fee a current Free Delivery code already removed from the order.

	apply_promo_evaluation_to_invoice drops the shipping rows and does not add
	the fee to custom_promo_discount_total, so replacing that code must put the
	fee back. Uses the order's own delivery override, else its territory fee."""
	if is_pickup or delivery_amount > 0 or not current_codes:
		return 0.0
	has_free_delivery = False
	for code in current_codes:
		name = _find_promo_name(code)
		if name and (frappe.db.get_value("Jarz Promo Code", name, "discount_type") or "") == "Free Delivery":
			has_free_delivery = True
			break
	if not has_free_delivery:
		return 0.0
	override = round(_num(_val(doc, "custom_delivery_income")), 2)
	if override > 0:
		return override
	return _territory_delivery_income(_val(doc, "territory"))


def _order_check(promo_name: str, invoice_name: str, now_dt, order_text: str = "") -> dict:
	"""Evaluate this one code against the real invoice (never saved)."""
	doc = frappe.get_doc("Sales Invoice", invoice_name)
	if not _can_read_invoice(doc):
		return {"error": "not_permitted", "order": order_text or invoice_name}

	grand_total = round(_num(_val(doc, "grand_total")), 2)
	current_discount = round(_num(_val(doc, "custom_promo_discount_total")), 2)
	delivery_amount = _delivery_amount(doc)
	current_codes = _promo._normalize_codes(_val(doc, "custom_promo_codes"))
	customer = _val(doc, "customer")

	customer_doc = None
	if customer:
		try:
			customer_doc = frappe.get_doc("Customer", customer)
		except Exception:
			customer_doc = None

	# Woo orders evaluate as "woo" (apply_promo_codes_before_validate). POS-app
	# orders are stamped custom_order_channel="flutter" and create_pos_invoice
	# evaluates them as such; anything unstamped falls back to the hook's "desk".
	if _val(doc, "woo_order_id"):
		channel = "woo"
	else:
		channel = (str(_val(doc, "custom_order_channel") or "").strip().lower()) or "desk"
	is_pickup = bool(_val(doc, "custom_is_pickup") or _val(doc, "is_pickup") or 0)
	waived_delivery = _waived_delivery(doc, current_codes, delivery_amount, is_pickup)

	evaluation = _promo.evaluate_promo_codes(
		doc,
		[promo_name],
		customer=customer,
		customer_doc=customer_doc,
		pos_profile=_val(doc, "pos_profile"),
		channel=channel,
		is_pickup=is_pickup,
		now_dt=now_dt,
	)
	result = evaluation.results[0] if evaluation.results else None
	accepted = bool(result and result.accepted)
	discount_amount = round(_num(result.discount_amount), 2) if accepted else 0.0
	free_delivery = bool(accepted and result.free_delivery)

	if accepted:
		# The order as if this code replaced its current codes: their discount
		# and any delivery fee they waived come back, then this code applies.
		new_total = grand_total + current_discount + waived_delivery - discount_amount
		if free_delivery:
			new_total -= delivery_amount + waived_delivery
		new_total = max(round(new_total, 2), 0.0)
		reason = None
	else:
		new_total = grand_total
		reason = (result.reason if result else None) or "not accepted"

	return {
		"invoice": _val(doc, "name") or invoice_name,
		"woo_order_id": _woo_id(_val(doc, "woo_order_id")),
		"customer_name": _val(doc, "customer_name") or _customer_name(customer),
		"docstatus": _int(_val(doc, "docstatus")),
		"invoice_status": _val(doc, "status") or "",
		"current_total": grand_total,
		"current_codes": current_codes,
		"current_promo_discount": current_discount,
		"delivery_amount": delivery_amount,
		"waived_delivery": waived_delivery,
		"accepted": accepted,
		"reason": reason,
		"discount_amount": discount_amount,
		"free_delivery": free_delivery,
		"new_total": new_total,
	}


@frappe.whitelist()
def lookup_promo_code(code=None, order=None):
	"""Describe a promo code for POS staff, optionally checked against an order.

	Read-only; any logged-in user (the Jarz Promo Code DocType itself is
	System Manager / Sales Manager only, so records are read without a
	permission check and only this summary is exposed).

	Params:
	    code  : required. Matched case-insensitively against the doc name,
	            ``code`` and ``woo_coupon_code``.
	    order : optional. A WooCommerce order number (Sales Invoice
	            ``woo_order_id``; latest submitted, else latest draft) or a
	            Sales Invoice name.

	Unknown codes return ``{"found": False, "code": <CODE>}``; nothing raises
	except an empty ``code``.
	"""
	raw_code = str(code or "").strip()
	if not raw_code:
		frappe.throw(frappe._("Enter a promo code to look up."))

	promo_name = _find_promo_name(raw_code)
	if not promo_name:
		return {"found": False, "code": raw_code.upper()}

	promo = frappe.get_doc("Jarz Promo Code", promo_name)
	now_dt = frappe.utils.now_datetime()

	times_used = _promo._active_redemption_count(promo_name)
	customer = _text(_val(promo, "customer"))

	order_text = str(order or "").strip()
	order_check = None
	if order_text:
		invoice_name = _resolve_invoice_name(order_text)
		if invoice_name:
			order_check = _order_check(promo_name, invoice_name, now_dt, order_text)
		else:
			order_check = {"error": "order_not_found", "order": order_text}

	return {
		"found": True,
		**_code_summary_base(promo, promo_name, times_used, now_dt),
		"customer": customer,
		"customer_name": _customer_name(customer),
		"customer_phone": _text(_val(promo, "customer_phone")),
		"woo_coupon_code": _text(_val(promo, "woo_coupon_code")),
		"on_website": bool(_text(_val(promo, "woo_coupon_id"))),
		"recent_uses": _recent_uses(promo_name),
		"order_check": order_check,
	}


# ---------------------------------------------------------------------------
# Customer promo lookup (feature promo-customer-lookup)
#
# "Do I have a promo code?" -- staff type a customer's name or phone and see
# that customer's personal codes, the public codes they can still use, and
# the codes they used before. Read-only, any logged-in user, same policy as
# ``lookup_promo_code``. Every list is fetched in a handful of batched
# queries; nothing runs per customer or per code.
# ---------------------------------------------------------------------------

_LOOKUP_MIN_QUERY = 3
_PHONE_MIN_DIGITS = 6
_PHONE_FULL_DIGITS = 10  # _phone_key length; below it, match the key's tail
_PHONE_PREFILTER_DIGITS = 7
_PHONE_MATCH_ROW_CAP = 500
_CUSTOMER_CAP = 10
_PUBLIC_CAP = 15
_INACTIVE_CAP = 10
_USED_CAP = 20
# Separators a stored number may carry; stripped in SQL before the LIKE so a
# number saved as "0122 549-5533" still contains the typed digit tail.
_PHONE_STRIP_CHARS = (" ", "-", "(", ")", ".", "/")
_QUERY_PHONE_PUNCT = frozenset("+-()")
# Arabic-Indic (U+0660..) and Extended Arabic-Indic (U+06F0..) digits -> ASCII.
_EASTERN_DIGITS = {base + i: str(i) for base in (0x0660, 0x06F0) for i in range(10)}
_PROMO_LIST_FIELDS = [
	"name", "code", "title", "description", "enabled", "discount_type", "discount_value",
	"max_discount_amount", "min_order_amount", "min_on_whole_order", "applies_to",
	"allow_pickup", "stackable", "priority", "usage_limit", "per_customer_limit",
	"active_from", "active_to", "customer", "customer_phone", "woo_coupon_code", "woo_coupon_id",
]
_EPOCH = datetime.datetime(1970, 1, 1)


def _epoch(value) -> float:
	dt = _as_datetime(value)
	return (dt - _EPOCH).total_seconds() if isinstance(dt, datetime.datetime) else 0.0


def _stamp(text) -> float:
	"""Seconds for a ``_datetime_str`` value (summaries carry strings)."""
	if not text:
		return 0.0
	try:
		return (datetime.datetime.strptime(str(text), "%Y-%m-%d %H:%M:%S") - _EPOCH).total_seconds()
	except ValueError:
		return 0.0


def _phone_query_digits(query: str):
	"""The query's digits when it is a phone number (>= 6 digits once spaces,
	``+``, ``-`` and brackets are removed), else None. Arabic-Indic digits
	typed on an Arabic keyboard count as digits."""
	compact = "".join(
		ch for ch in query.translate(_EASTERN_DIGITS) if not ch.isspace() and ch not in _QUERY_PHONE_PUNCT
	)
	if compact.isascii() and compact.isdigit() and len(compact) >= _PHONE_MIN_DIGITS:
		return compact
	return None


def _phone_matcher(digits: str):
	"""(match(value) -> bool, digit tail for the SQL prefilter).

	10+ digits: the stored number's ``_phone_key`` must equal the query's.
	Fewer: staff typed part of the number (often its start, "0122549"), so
	the key must contain it once a leading 0 is dropped (keys carry no 0).
	"""
	# "0122549553" is 10 digits but still one short of a local 11-digit number.
	full = len(digits) > _PHONE_FULL_DIGITS or (len(digits) == _PHONE_FULL_DIGITS and not digits.startswith("0"))
	if full:
		wanted = _promo._phone_key(digits)

		def _match(value) -> bool:
			return bool(value) and bool(wanted) and _promo._phone_key(value) == wanted

		tail = wanted
	else:
		part = digits[1:] if digits.startswith("0") else digits

		def _match(value) -> bool:
			return bool(value) and part in _promo._phone_key(value)

		tail = part
	return _match, tail[-_PHONE_PREFILTER_DIGITS:]


def _digits_only_sql(column: str) -> str:
	expr = f"IFNULL({column}, '')"
	for ch in _PHONE_STRIP_CHARS:
		expr = f"REPLACE({expr}, '{ch}', '')"
	return expr


def _like_escape(text: str) -> str:
	return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _customer_rows(names) -> dict:
	names = [n for n in names if n]
	if not names:
		return {}
	rows = frappe.get_all(
		"Customer",
		filters={"name": ["in", names]},
		fields=["name", "customer_name", "mobile_no"],
	)
	return {row.get("name"): row for row in rows or []}


def _customers_matching_phone(matches, tail: str):
	"""Customers whose ``mobile_no`` or a linked Contact Phone matches.

	A LIKE on the separator-stripped column narrows the rows; the exact
	``_phone_key`` rule is then applied in Python. Returns (customer rows by
	name, the phone keys those matching numbers carry).
	"""
	pattern = f"%{tail}%"
	matched: dict[str, list] = {}
	mobile_rows = frappe.db.sql(
		f"""
		SELECT name, mobile_no
		FROM `tabCustomer`
		WHERE {_digits_only_sql("mobile_no")} LIKE %(pattern)s
		LIMIT {_PHONE_MATCH_ROW_CAP}
		""",
		{"pattern": pattern},
		as_dict=True,
	)
	for row in mobile_rows or []:
		if matches(row.get("mobile_no")):
			matched.setdefault(row.get("name"), []).append(row.get("mobile_no"))
	contact_rows = frappe.db.sql(
		f"""
		SELECT dl.link_name AS customer, cp.phone AS phone
		FROM `tabContact Phone` cp
		INNER JOIN `tabDynamic Link` dl
			ON dl.parent = cp.parent
			AND dl.parenttype = 'Contact'
			AND dl.link_doctype = 'Customer'
		WHERE cp.parenttype = 'Contact'
			AND {_digits_only_sql("cp.phone")} LIKE %(pattern)s
		LIMIT {_PHONE_MATCH_ROW_CAP}
		""",
		{"pattern": pattern},
		as_dict=True,
	)
	for row in contact_rows or []:
		if row.get("customer") and matches(row.get("phone")):
			matched.setdefault(row.get("customer"), []).append(row.get("phone"))

	rows = _customer_rows(list(matched))  # drops links to deleted customers
	keys = {_promo._phone_key(value) for name in rows for value in matched.get(name, [])}
	keys.discard("")
	return rows, keys


def _customers_matching_name(text: str, limit: int) -> list[str]:
	"""Customer names whose ``customer_name`` or docname contains ``text``
	(disabled included), most recent submitted order first, then name."""
	rows = frappe.db.sql(
		"""
		SELECT c.name AS name
		FROM `tabCustomer` c
		LEFT JOIN `tabSales Invoice` si
			ON si.customer = c.name
			AND si.docstatus = 1
			AND si.is_return = 0
		WHERE c.customer_name LIKE %(like)s OR c.name LIKE %(like)s
		GROUP BY c.name
		ORDER BY MAX(si.posting_date) IS NULL, MAX(si.posting_date) DESC, MAX(si.creation) DESC, c.name ASC
		LIMIT %(limit)s
		""",
		{"like": f"%{_like_escape(text)}%", "limit": int(limit)},
		as_dict=True,
	)
	return [row.get("name") for row in rows or [] if row.get("name")]


def _order_stats(names) -> dict:
	"""Submitted, non-return Sales Invoice stats per customer. Woo order ids
	only grow, so MAX is the customer's latest Woo order."""
	names = [n for n in names if n]
	if not names:
		return {}
	rows = frappe.db.sql(
		"""
		SELECT customer,
			COUNT(*) AS order_count,
			MAX(posting_date) AS last_order_date,
			MAX(creation) AS last_creation,
			MAX(woo_order_id) AS last_woo_order_id
		FROM `tabSales Invoice`
		WHERE docstatus = 1 AND is_return = 0 AND customer IN %(customers)s
		GROUP BY customer
		""",
		{"customers": tuple(names)},
		as_dict=True,
	)
	return {row.get("customer"): row for row in rows or []}


def _recent_first_key(name, stats: dict):
	row = stats.get(name) or {}
	last = _as_datetime(row.get("last_order_date"))
	return (last is None, -_epoch(last), -_epoch(row.get("last_creation")), name or "")


def _contact_phones_for(names) -> dict:
	"""Every Contact Phone of the Contacts linked to each customer, primary
	mobile first."""
	names = [n for n in names if n]
	if not names:
		return {}
	rows = frappe.db.sql(
		"""
		SELECT dl.link_name AS customer, cp.phone AS phone
		FROM `tabDynamic Link` dl
		INNER JOIN `tabContact Phone` cp
			ON cp.parent = dl.parent
			AND cp.parenttype = 'Contact'
		WHERE dl.parenttype = 'Contact'
			AND dl.link_doctype = 'Customer'
			AND dl.link_name IN %(customers)s
		ORDER BY dl.link_name, cp.is_primary_mobile_no DESC, cp.is_primary_phone DESC, cp.parent, cp.idx
		""",
		{"customers": tuple(names)},
		as_dict=True,
	)
	out: dict[str, list] = {}
	for row in rows or []:
		phone = _text(row.get("phone"))
		if phone and phone not in out.setdefault(row.get("customer"), []):
			out[row.get("customer")].append(phone)
	return out


def _promo_rows(filters, order_by=None) -> list:
	kwargs = {"filters": filters, "fields": list(_PROMO_LIST_FIELDS)}
	if order_by:
		kwargs["order_by"] = order_by
	return list(frappe.get_all("Jarz Promo Code", **kwargs) or [])


def _phone_locked_promos() -> list:
	return _promo_rows({"customer_phone": ["is", "set"]}, order_by="creation desc")


def _pseudo_customer_groups(phone_promos, matches, owned_keys) -> list[dict]:
	"""Phone-locked codes matching the query whose ``customer`` is empty or
	does not resolve, grouped by phone key, skipping keys a real matched
	Customer already owns."""
	candidates = [row for row in phone_promos if matches(row.get("customer_phone"))]
	linked = sorted({c for c in (_text(r.get("customer")) for r in candidates) if c})
	existing = set()
	if linked:
		existing = set(frappe.get_all("Customer", filters={"name": ["in", linked]}, pluck="name") or [])
	groups: dict[str, dict] = {}
	for row in candidates:
		customer = _text(row.get("customer"))
		if customer and customer in existing:
			continue
		key = _promo._phone_key(row.get("customer_phone"))
		if not key or key in owned_keys:
			continue
		group = groups.setdefault(key, {"phone": _text(row.get("customer_phone")), "promos": []})
		group["promos"].append(row)
	return list(groups.values())


def _attach_promo_children(promos: dict) -> None:
	"""Load ``item_scope`` and ``channels`` for every promo in two queries."""
	for row in promos.values():
		row["item_scope"] = []
		row["channels"] = []
	names = list(promos)
	if not names:
		return
	for child in frappe.get_all(
		"Jarz Promo Code Item",
		filters={"parenttype": "Jarz Promo Code", "parentfield": "item_scope", "parent": ["in", names]},
		fields=["parent", "scope_type", "item_code", "item_group"],
		order_by="idx asc",
	) or []:
		row = promos.get(child.get("parent"))
		if row is not None:
			row["item_scope"].append(child)
	for child in frappe.get_all(
		"Jarz Promotion Rule Channel",
		filters={"parenttype": "Jarz Promo Code", "parentfield": "channels", "parent": ["in", names]},
		fields=["parent", "channel"],
		order_by="idx asc",
	) or []:
		row = promos.get(child.get("parent"))
		if row is not None:
			row["channels"].append(child)


def _item_labels(promos: dict) -> dict:
	codes = sorted(
		{
			child.get("item_code")
			for row in promos.values()
			for child in row.get("item_scope") or []
			if child.get("scope_type") == "Item" and child.get("item_code")
		}
	)
	if not codes:
		return {}
	rows = frappe.get_all("Item", filters={"name": ["in", codes]}, fields=["name", "item_name"])
	return {row.get("name"): row.get("item_name") for row in rows or []}


def _redemption_totals(promo_names) -> dict:
	"""Active redemptions per code, all customers (as ``_active_redemption_count``)."""
	if not promo_names:
		return {}
	rows = frappe.db.sql(
		"""
		SELECT promo_code, COUNT(*) AS n
		FROM `tabJarz Promo Redemption`
		WHERE status = 'Active' AND promo_code IN %(codes)s
		GROUP BY promo_code
		""",
		{"codes": tuple(promo_names)},
		as_dict=True,
	)
	return {row.get("promo_code"): _int(row.get("n")) for row in rows or []}


def _customer_redemption_counts(promo_names, customers) -> dict:
	"""Active redemptions per (code, customer) for the listed customers."""
	if not promo_names or not customers:
		return {}
	rows = frappe.db.sql(
		"""
		SELECT promo_code, customer, COUNT(*) AS n
		FROM `tabJarz Promo Redemption`
		WHERE status = 'Active' AND promo_code IN %(codes)s AND customer IN %(customers)s
		GROUP BY promo_code, customer
		""",
		{"codes": tuple(promo_names), "customers": tuple(customers)},
		as_dict=True,
	)
	return {(row.get("promo_code"), row.get("customer")): _int(row.get("n")) for row in rows or []}


def _used_code_rows(customers) -> dict:
	"""Each customer's redemptions (all statuses), newest first, capped."""
	if not customers:
		return {}
	rows = frappe.get_all(
		"Jarz Promo Redemption",
		filters={"customer": ["in", list(customers)]},
		fields=["promo_code", "sales_invoice", "customer", "status", "discount_applied", "woo_order_id", "creation"],
		order_by="creation desc",
	) or []
	per_customer: dict[str, list] = {}
	for row in rows:
		bucket = per_customer.setdefault(row.get("customer"), [])
		if len(bucket) < _USED_CAP:
			bucket.append(row)

	invoice_names = sorted({r.get("sales_invoice") for b in per_customer.values() for r in b if r.get("sales_invoice")})
	invoices: dict = {}
	if invoice_names:
		for inv in frappe.get_all(
			"Sales Invoice",
			filters={"name": ["in", invoice_names]},
			fields=["name", "posting_date", "grand_total", "woo_order_id"],
		) or []:
			invoices[inv.get("name")] = inv

	out: dict[str, list] = {}
	for customer, bucket in per_customer.items():
		uses = []
		for row in bucket:
			inv = invoices.get(row.get("sales_invoice"))
			uses.append(
				{
					"code": row.get("promo_code"),
					"invoice": row.get("sales_invoice"),
					"woo_order_id": _woo_id(row.get("woo_order_id")) or _woo_id((inv or {}).get("woo_order_id")),
					"date": _date_str((inv or {}).get("posting_date") or row.get("creation")),
					"status": row.get("status"),
					"discount": _num(row.get("discount_applied")),
					"order_total": (
						_num(inv.get("grand_total")) if inv and inv.get("grand_total") is not None else None
					),
				}
			)
		out[customer] = uses
	return out


def _is_personal(promo) -> bool:
	return bool(_text(_val(promo, "customer")) or _promo._phone_key(_val(promo, "customer_phone")))


def _active_sort_key(summary: dict):
	"""Usable now before not-started, then soonest to expire, open-ended last."""
	active_to = summary.get("active_to")
	return (summary["status"] != "active", active_to is None, _stamp(active_to), summary.get("code") or "")


def _inactive_sort_key(summary: dict):
	"""Newest ``active_to`` first, codes without one last."""
	active_to = summary.get("active_to")
	return (active_to is None, -_stamp(active_to), summary.get("code") or "")


@frappe.whitelist()
def lookup_customer_promos(query=None):
	"""Promo codes a customer can use, found by their name or phone.

	Read-only; any logged-in user (same policy as ``lookup_promo_code``:
	records are read without a permission check and only this summary is
	exposed).

	Params:
	    query : required, >= 3 characters. All digits (ignoring spaces, ``+``,
	            ``-`` and brackets) with >= 6 digits -> phone search on
	            ``_phone_key``; otherwise a name search.

	Response: ``{"query", "match_type", "truncated", "customers": [...]}``;
	see ``.scratch/promo-customer-lookup/CONTRACT.md`` for each entry's shape.
	No match returns ``customers: []``.
	"""
	text = str(query or "").strip()
	if len(text) < _LOOKUP_MIN_QUERY:
		frappe.throw(frappe._("Enter at least 3 characters of a name or phone number."))

	now_dt = frappe.utils.now_datetime()
	phone_digits = _phone_query_digits(text)
	phone_promos = None
	targets: list[tuple] = []  # ("customer", name) | ("pseudo", group)

	if phone_digits:
		match_type = "phone"
		matches, tail = _phone_matcher(phone_digits)
		customers, owned_keys = _customers_matching_phone(matches, tail)
		stats = _order_stats(list(customers))
		ordered = sorted(customers, key=lambda name: _recent_first_key(name, stats))
		phone_promos = _phone_locked_promos()
		targets = [("customer", name) for name in ordered]
		targets += [("pseudo", group) for group in _pseudo_customer_groups(phone_promos, matches, owned_keys)]
	else:
		match_type = "name"
		names = _customers_matching_name(text, _CUSTOMER_CAP + 1)
		targets = [("customer", name) for name in names]
		customers = {}
		stats = {}

	truncated = len(targets) > _CUSTOMER_CAP
	targets = targets[:_CUSTOMER_CAP]
	if not targets:
		return {"query": text, "match_type": match_type, "truncated": truncated, "customers": []}

	real_names = [value for kind, value in targets if kind == "customer"]
	if match_type == "name":
		customers = _customer_rows(real_names)
		stats = _order_stats(real_names)
		real_names = [name for name in real_names if name in customers]

	# Phone keys per customer: mobile_no + every linked contact phone.
	contact_phones = _contact_phones_for(real_names)
	keys_by_customer: dict[str, set] = {}
	phone_of: dict[str, object] = {}
	for name in real_names:
		phones = contact_phones.get(name, [])
		mobile = _text((customers.get(name) or {}).get("mobile_no"))
		phone_of[name] = mobile or (phones[0] if phones else None)
		keys = {_promo._phone_key(value) for value in [mobile, *phones] if value}
		keys.discard("")
		keys_by_customer[name] = keys

	# Personal codes: locked to the docname, or to one of the customer's phone
	# keys (the rule ``_customer_lock_ok`` applies).
	promos: dict[str, dict] = {}
	linked_rows = _promo_rows({"customer": ["in", real_names]}) if real_names else []
	if phone_promos is None and any(keys_by_customer.values()):
		phone_promos = _phone_locked_promos()
	keyed_phone_promos = [(_promo._phone_key(row.get("customer_phone")), row) for row in phone_promos or []]
	personal_names: dict[str, list] = {}
	for name in real_names:
		picked = [row for row in linked_rows if row.get("customer") == name]
		keys = keys_by_customer[name]
		if keys:
			picked += [row for key, row in keyed_phone_promos if key and key in keys]
		listed: list[str] = []
		for row in picked:
			promo_name = row.get("name")
			if promo_name and promo_name not in listed:
				listed.append(promo_name)
				promos.setdefault(promo_name, row)
		personal_names[name] = listed
	for kind, group in targets:
		if kind == "pseudo":
			for row in group["promos"]:
				promos.setdefault(row.get("name"), row)

	public_rows = _promo_rows(
		{"customer": ["is", "not set"], "customer_phone": ["is", "not set"], "enabled": 1}
	)
	public_names: list[str] = []
	for row in public_rows:
		promo_name = row.get("name")
		if promo_name and promo_name not in promos:
			promos[promo_name] = row
			public_names.append(promo_name)

	_attach_promo_children(promos)
	item_labels = _item_labels(promos)
	totals = _redemption_totals(list(promos))
	per_customer = _customer_redemption_counts(list(promos), real_names)
	base = {
		promo_name: _code_summary_base(row, promo_name, totals.get(promo_name, 0), now_dt, item_labels)
		for promo_name, row in promos.items()
	}
	public_names.sort(
		key=lambda n: (
			-_int(promos[n].get("priority")),
			base[n]["active_to"] is None,
			_stamp(base[n]["active_to"]),
			base[n]["code"] or "",
		)
	)

	def _summary(promo_name, customer):
		row = promos[promo_name]
		out = dict(base[promo_name])
		used = per_customer.get((promo_name, customer), 0) if customer else 0
		limit = out["per_customer_limit"]
		out["personal"] = _is_personal(row)
		out["customer_times_used"] = used
		out["customer_remaining_uses"] = max(limit - used, 0) if limit > 0 else None
		out["on_website"] = bool(_text(row.get("woo_coupon_id")))
		out["woo_coupon_code"] = _text(row.get("woo_coupon_code"))
		return out

	def _entry(customer, customer_name, phone, stats_row, personal, uses):
		summaries = [_summary(promo_name, customer) for promo_name in personal]
		active = sorted((s for s in summaries if s["status"] in ("active", "not_started")), key=_active_sort_key)
		inactive = sorted((s for s in summaries if s["status"] not in ("active", "not_started")), key=_inactive_sort_key)
		public = []
		for promo_name in public_names:
			summary = _summary(promo_name, customer)
			if summary["status"] != "active":
				continue
			if summary["customer_remaining_uses"] is not None and summary["customer_remaining_uses"] <= 0:
				continue
			public.append(summary)
			if len(public) >= _PUBLIC_CAP:
				break
		stats_row = stats_row or {}
		return {
			"customer": customer,
			"customer_name": customer_name,
			"phone": phone,
			"order_count": _int(stats_row.get("order_count")),
			"last_order_date": _date_str(stats_row.get("last_order_date")),
			"last_woo_order_id": _woo_id(stats_row.get("last_woo_order_id")),
			"active_codes": active,
			"public_codes": public,
			"inactive_codes": inactive[:_INACTIVE_CAP],
			"used_codes": uses,
		}

	used_by_customer = _used_code_rows(real_names)
	entries = []
	for kind, value in targets:
		if kind == "customer":
			if value not in customers:
				continue
			row = customers[value]
			entries.append(
				_entry(
					value,
					_text(row.get("customer_name")) or value,
					phone_of.get(value),
					stats.get(value),
					personal_names.get(value, []),
					used_by_customer.get(value, []),
				)
			)
		else:
			entries.append(
				_entry(None, None, value["phone"], None, [row.get("name") for row in value["promos"]], [])
			)

	return {"query": text, "match_type": match_type, "truncated": truncated, "customers": entries}
