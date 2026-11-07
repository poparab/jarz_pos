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


def _scope_rows(promo) -> list[dict]:
	"""The item scope the engine actually honours for this promo.

	``_eligible_net_for`` ignores the scope table on Whole Order codes, reads
	only Item rows on Specific Items codes and only Item Group rows on Item
	Groups codes; listing the other rows would advertise a scope that is not
	applied.
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
	usage_limit = _int(_val(promo, "usage_limit"))
	remaining_uses = max(usage_limit - times_used, 0) if usage_limit > 0 else None
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
		"scope": _scope_rows(promo),
		"channels": _channel_list(promo),
		"allow_pickup": bool(_int(_val(promo, "allow_pickup"))),
		"stackable": bool(_int(_val(promo, "stackable"))),
		"usage_limit": usage_limit,
		"times_used": times_used,
		"remaining_uses": remaining_uses,
		"per_customer_limit": _int(_val(promo, "per_customer_limit")),
		"active_from": _datetime_str(_val(promo, "active_from")),
		"active_to": _datetime_str(_val(promo, "active_to")),
		"customer": customer,
		"customer_name": _customer_name(customer),
		"customer_phone": _text(_val(promo, "customer_phone")),
		"woo_coupon_code": _text(_val(promo, "woo_coupon_code")),
		"on_website": bool(_text(_val(promo, "woo_coupon_id"))),
		"recent_uses": _recent_uses(promo_name),
		"order_check": order_check,
	}
