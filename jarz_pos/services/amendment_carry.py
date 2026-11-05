"""Carry the source order's approved pricing onto an amendment replacement.

An amendment cancels the submitted invoice and creates a fresh one through
``create_pos_invoice``, which re-prices every line from the price list. Anything
the source carried *on top of* the price list was therefore dropped silently:

* per-line custom prices (``custom_rate_override``, audited as
  ``[CUSTOM LINE PRICING]``) -- neither the app's edit cart nor the server-side
  ``reuse_source_cart`` rebuild sent them, so kept lines went back to list price
  (order 19041, 2026-10-18: six free cakes came back at EGP 160 each);
* header discounts -- POS promo codes, Woo coupons and Woo non-coupon discounts --
  because the amendment job never passed ``promo_codes`` at all;
* the ``[ZERO SHIPPING OVERRIDE]`` audit marker (the money itself was kept).

The operator can also change the codes while amending (order 20029, 2026-10-30:
a win-back code added on two amendments was dropped both times, because the
endpoint only ever forwarded the source's codes). :func:`with_requested_promo_codes`
makes the app's list authoritative when it sends one.

The amendment job (``api/manager.py::_run_invoice_amendment_job``) builds a
*carry* from the cancelled source with :func:`build_amendment_carry`, injects the
line prices into the cart with :func:`apply_source_line_pricing`, and publishes the
carry on ``frappe.flags.jarz_amendment_carry``. Invoice creation honours it only
through :func:`trusted_carry`, i.e. only inside that job and only for the source it
names: ``create_pos_invoice`` is whitelisted and ``amended_from`` is client-supplied,
but flags are server-only.
"""

from __future__ import annotations

from typing import Any, Callable

import frappe
from frappe.utils import flt

from jarz_pos.utils.invoice_utils import POS_AUDIT_MARKERS_FIELD

CUSTOM_LINE_PRICING_MARKER = "[CUSTOM LINE PRICING]"
# A bundle line sold at a cashier's discount or custom price. The bundle's parent row
# keeps the list price, so the charged price is only recoverable from its children.
BUNDLE_PRICING_MARKER = "[BUNDLE PRICING]"
ZERO_SHIPPING_MARKER = "[ZERO SHIPPING OVERRIDE]"
DISCOUNT_CARRIED_MARKER = "[AMENDMENT DISCOUNT CARRIED]"

_RATE_TOLERANCE = 0.005
_LINE_DISCOUNT_FIELDS = ("discount_percentage", "discount_amount")


def _get(row: Any, fieldname: str) -> Any:
	if isinstance(row, dict):
		return row.get(fieldname)
	getter = getattr(row, "get", None)
	if callable(getter):
		return getter(fieldname)
	return getattr(row, fieldname, None)


def _is_plain_row(row: Any) -> bool:
	"""A line priced on its own: not a bundle parent/child, not a free item."""
	for fieldname in ("is_bundle_parent", "is_bundle_child", "is_free_item"):
		if _get(row, fieldname):
			return False
	for fieldname in ("parent_bundle", "bundle_code"):
		if str(_get(row, fieldname) or "").strip():
			return False
	return bool(str(_get(row, "item_code") or "").strip())


def source_has_marker(source: Any, marker: str) -> bool:
	"""True when the source's audit markers carry ``marker``.

	``remarks`` is read only for a legacy POS invoice written before the markers
	field existed. On a Woo order it is the customer's own note, so a customer who
	typed a marker into it would otherwise turn an amendment's re-pricing off.
	"""
	markers = str(_get(source, POS_AUDIT_MARKERS_FIELD) or "")
	if markers.strip():
		return marker in markers
	if _get(source, "woo_order_id"):
		return False
	return marker in str(_get(source, "remarks") or "")


def source_line_rate_overrides(source: Any) -> dict[str, float]:
	"""``{item_code: unit price}`` for the source's custom-priced plain lines.

	Only an invoice audited ``[CUSTOM LINE PRICING]`` had a manager-approved price
	off the list; every other invoice's ``price_list_rate`` simply *is* the list, and
	re-pricing it from today's list is the existing, intended behaviour. An item the
	source carries at two different prices is ambiguous and left out.
	"""
	if not source_has_marker(source, CUSTOM_LINE_PRICING_MARKER):
		return {}
	rates: dict[str, float] = {}
	conflicting: set[str] = set()
	for row in _get(source, "items") or []:
		if not _is_plain_row(row):
			continue
		code = str(_get(row, "item_code")).strip()
		rate = flt(_get(row, "price_list_rate"))
		if rate <= 0 and flt(_get(row, "rate")) > 0:
			# No list price but a charged rate: not a free line, so not carried as one.
			continue
		if code in rates and abs(rates[code] - rate) > _RATE_TOLERANCE:
			conflicting.add(code)
		rates.setdefault(code, rate)
	for code in conflicting:
		rates.pop(code, None)
	return rates


def source_line_discounts(source: Any) -> dict[str, dict[str, float]]:
	"""``{item_code: {field: value}}`` for manual line discounts on plain lines."""
	discounts: dict[str, dict[str, float]] = {}
	for row in _get(source, "items") or []:
		if not _is_plain_row(row):
			continue
		code = str(_get(row, "item_code")).strip()
		for fieldname in _LINE_DISCOUNT_FIELDS:
			value = flt(_get(row, fieldname))
			if value > 0:
				discounts.setdefault(code, {})[fieldname] = value
	return discounts


def source_item_quantities(source: Any) -> dict[str, float]:
	quantities: dict[str, float] = {}
	for row in _get(source, "items") or []:
		if not _is_plain_row(row):
			continue
		code = str(_get(row, "item_code")).strip()
		quantities[code] = quantities.get(code, 0.0) + flt(_get(row, "qty"))
	return quantities


def source_bundle_rates(source: Any) -> tuple[dict[str, float], dict[str, float]]:
	"""``({bundle item: charged price of ONE bundle}, {bundle item: bundle qty})``.

	Only for a source audited ``[BUNDLE PRICING]``: its bundle was discounted or
	custom-priced, and every amendment re-priced it at list because no cart rebuild
	carries that (the parent row stores the list price; the discount lives in the
	children). BundleProcessor writes a parent row followed by its own children, so
	children are grouped by order. Anything that does not read that way, or a bundle
	held twice at two different prices, is left out rather than guessed.
	"""
	if not source_has_marker(source, BUNDLE_PRICING_MARKER):
		return {}, {}
	groups: list[list[Any]] = []
	for row in _get(source, "items") or []:
		if _get(row, "is_bundle_parent"):
			groups.append([row, 0.0])
		elif _get(row, "is_bundle_child"):
			if not groups:
				return {}, {}
			parent = groups[-1][0]
			if str(_get(row, "parent_bundle") or "") != str(_get(parent, "bundle_code") or ""):
				return {}, {}
			groups[-1][1] += flt(_get(row, "amount"))
	rates: dict[str, float] = {}
	quantities: dict[str, float] = {}
	conflicting: set[str] = set()
	for parent, children_total in groups:
		code = str(_get(parent, "item_code") or "").strip()
		qty = flt(_get(parent, "qty"))
		if not code or qty <= 0:
			continue
		rate = flt(children_total / qty, 6)
		if code in rates and abs(rates[code] - rate) > _RATE_TOLERANCE:
			conflicting.add(code)
		rates.setdefault(code, rate)
		quantities[code] = quantities.get(code, 0.0) + qty
	for code in conflicting:
		rates.pop(code, None)
		quantities.pop(code, None)
	return rates, quantities


def bundle_item_code(code: str) -> str:
	"""The ERPNext item of a cart bundle row, whose code may be the Jarz Bundle name.

	The server rebuild sends the Jarz Bundle record name; the app sends its id; the
	source's parent row stores the ERPNext item. Carry is keyed by the latter.
	"""
	code = str(code or "").strip()
	if not code:
		return code
	try:
		item = frappe.db.get_value("Jarz Bundle", code, "erpnext_item")
	except Exception:
		item = None
	return str(item or code).strip()


def _row_bundle_rate(row: dict) -> float | None:
	for fieldname in ("price_list_rate", "rate", "price"):
		value = row.get(fieldname)
		if value not in (None, ""):
			return flt(value)
	return None


def apply_source_bundle_pricing(
	cart: Any,
	bundle_rates: dict[str, float],
	bundle_qty: dict[str, float] | None = None,
	resolve_code: Callable[[str], str] = bundle_item_code,
) -> list[str]:
	"""Give each kept bundle line the source's charged price; return the codes changed.

	Same rules as :func:`apply_source_line_pricing`: a row that sends its own
	``custom_rate_override`` or line discount is the operator's choice and is never
	touched, a row already at that price needs nothing, and only the source's bundle
	quantity inherits the price (extra units are split off at list). Mutates ``cart``.
	"""
	carried: list[str] = []
	if not isinstance(cart, list) or not bundle_rates:
		return carried
	budget = dict(bundle_qty or {})
	out: list[Any] = []
	for row in cart:
		out.append(row)
		if not isinstance(row, dict) or not row.get("is_bundle"):
			continue
		if any(row.get(field) not in (None, "") for field in ("custom_rate_override", *_LINE_DISCOUNT_FIELDS)):
			continue
		code = resolve_code(str(row.get("item_code") or ""))
		if code not in bundle_rates:
			continue
		override = flt(bundle_rates[code])
		current = _row_bundle_rate(row)
		if current is not None and abs(current - override) <= _RATE_TOLERANCE:
			continue
		qty_key = _row_qty_key(row)
		qty = flt(row.get(qty_key) or 1)
		remaining = flt(budget.get(code))
		if remaining <= 1e-9:
			continue
		if qty > remaining + 1e-9:
			extra = dict(row)
			extra[qty_key] = round(qty - remaining, 6)
			row[qty_key] = remaining
			out.append(extra)
			qty = remaining
		budget[code] = remaining - qty
		row["custom_rate_override"] = override
		if code not in carried:
			carried.append(code)
	cart[:] = out
	return carried


def source_promo_codes(source: Any) -> list[str]:
	from jarz_pos.services import promo_codes as _promo_codes

	try:
		return _promo_codes._normalize_codes(_promo_codes._parse_codes_value(_get(source, "custom_promo_codes")))
	except Exception:
		return []


def parse_requested_promo_codes(value: Any) -> list[str] | None:
	"""The operator's promo codes for an amendment, or None when the app sent none.

	None (an older app that never sends the field) means "keep the source's codes";
	an empty list means the operator removed them all.
	"""
	if value is None:
		return None
	from jarz_pos.services import promo_codes as _promo_codes

	return _promo_codes._normalize_codes(_promo_codes._parse_codes_value(value))


def with_requested_promo_codes(
	carry: dict[str, Any], requested: list[str] | None, *, keep_source: bool = False
) -> dict[str, Any]:
	"""Make the operator's codes the ones the replacement is evaluated with.

	Removing a source code also drops the source's header discount from the carry:
	that discount came from the codes, so carrying it would put back what the
	operator just took off. Added codes leave it alone; :func:`apply_header_discount_carry`
	then keeps whichever discount is larger.

	``keep_source``: the app did not know the order's codes (a draft restored from
	local storage keeps none), so ``requested`` only ADDS to them and removes nothing.
	"""
	if requested is None:
		return carry
	source_codes = carry.get("source_promo_codes") or []
	if keep_source:
		requested = list(source_codes) + [code for code in requested if code not in source_codes]
	updated = dict(carry)
	updated["promo_codes"] = list(requested)
	if any(code not in requested for code in source_codes):
		updated["header_discount"] = 0.0
		updated["woo_discount_total"] = 0.0
	return updated


def build_amendment_carry(source: Any, *, zero_shipping: bool) -> dict[str, Any]:
	"""Everything the replacement must inherit from ``source`` beyond its cart."""
	codes = source_promo_codes(source)
	bundle_rates, bundle_qty = source_bundle_rates(source)
	return {
		"source": str(_get(source, "name") or "").strip(),
		"line_rates": source_line_rate_overrides(source),
		"bundle_rates": bundle_rates,
		"bundle_qty": bundle_qty,
		"line_discounts": source_line_discounts(source),
		"source_qty": source_item_quantities(source),
		"promo_codes": codes,
		"source_promo_codes": list(codes),
		"header_discount": round(flt(_get(source, "discount_amount")), 2),
		"woo_discount_total": round(flt(_get(source, "custom_promo_woo_discount_total")), 2),
		"zero_shipping_marker": bool(zero_shipping and source_has_marker(source, ZERO_SHIPPING_MARKER)),
	}


def _row_qty_key(row: dict) -> str:
	return "quantity" if "quantity" in row and "qty" not in row else "qty"


def apply_source_line_pricing(
	cart: Any,
	line_rates: dict[str, float],
	resolve_rate: Callable[[str], float | None],
	source_qty: dict[str, float] | None = None,
) -> list[str]:
	"""Give each kept plain line the source's custom price; return the codes changed.

	A row that already sends its own ``custom_rate_override`` is the operator's
	choice for THIS edit and is never touched. A row whose source price equals what
	the list resolves to today needs no override. Only the source's quantity of an
	item inherits its price: raising a free line from 1 to 3 splits it into 1 unit at
	the source price and 2 at list, so the extra units are neither given away
	silently nor sent to the manager gate as a "custom price". Mutates ``cart``.
	"""
	carried: list[str] = []
	if not isinstance(cart, list) or not line_rates:
		return carried
	budget = dict(source_qty) if source_qty is not None else None
	out: list[Any] = []
	for row in cart:
		out.append(row)
		if not isinstance(row, dict) or row.get("is_bundle"):
			continue
		code = str(row.get("item_code") or "").strip()
		if row.get("custom_rate_override") not in (None, ""):
			continue
		if code not in line_rates:
			continue
		override = flt(line_rates[code])
		try:
			current = resolve_rate(code)
		except Exception:
			current = None
		if current is not None and abs(flt(current) - override) <= _RATE_TOLERANCE:
			continue
		if budget is not None:
			qty_key = _row_qty_key(row)
			qty = flt(row.get(qty_key) or 1)
			remaining = flt(budget.get(code))
			if remaining <= 1e-9:
				continue
			if qty > remaining + 1e-9:
				extra = dict(row)
				extra[qty_key] = round(qty - remaining, 6)
				row[qty_key] = remaining
				out.append(extra)
				qty = remaining
			budget[code] = remaining - qty
		row["custom_rate_override"] = override
		if code not in carried:
			carried.append(code)
	cart[:] = out
	return carried


def trusted_carry(amended_from: str | None) -> dict[str, Any] | None:
	"""The amendment job's carry for ``amended_from``, or None outside that job."""
	source = str(amended_from or "").strip()
	if not source:
		return None
	try:
		if str(frappe.flags.get("jarz_amendment_source") or "").strip() != source:
			return None
		carry = frappe.flags.get("jarz_amendment_carry")
	except Exception:
		return None
	if not isinstance(carry, dict) or str(carry.get("source") or "").strip() != source:
		return None
	return carry


def strip_carried_pricing_for_gate(cart_items: Any, carry: dict[str, Any] | None) -> Any:
	"""Copy of ``cart_items`` without pricing the source already had approved.

	Used ONLY to decide whether the manager gate applies: a cashier editing a
	manager-priced order (moving the slot, adding an item) is not making a pricing
	decision, so the inherited price must not lock them out. A field is dropped only
	when it matches the source's value for that item, and only up to the source's
	quantity -- raising a free line from 1 to 10 is a new decision and stays gated.
	"""
	if not carry or not isinstance(cart_items, list):
		return cart_items
	line_rates = carry.get("line_rates") or {}
	line_discounts = carry.get("line_discounts") or {}
	budget = dict(carry.get("source_qty") or {})
	bundle_rates = carry.get("bundle_rates") or {}
	bundle_budget = dict(carry.get("bundle_qty") or {})
	stripped: list[Any] = []
	for item in cart_items:
		if isinstance(item, dict) and item.get("is_bundle"):
			# Only the price apply_source_bundle_pricing put back, within the source's
			# bundle quantity; a new discount or price on a bundle stays gated.
			override = item.get("custom_rate_override")
			if override not in (None, "") and bundle_rates:
				code = bundle_item_code(str(item.get("item_code") or ""))
				qty = flt(item.get("qty") or item.get("quantity") or 1)
				remaining = flt(bundle_budget.get(code))
				if (
					code in bundle_rates
					and qty <= remaining + 1e-9
					and abs(flt(override) - flt(bundle_rates[code])) <= _RATE_TOLERANCE
				):
					item = dict(item)
					item.pop("custom_rate_override", None)
					bundle_budget[code] = remaining - qty
			stripped.append(item)
			continue
		if not isinstance(item, dict):
			stripped.append(item)
			continue
		code = str(item.get("item_code") or "").strip()
		qty = flt(item.get("qty") or item.get("quantity") or 1)
		remaining = flt(budget.get(code))
		if qty > remaining + 1e-9:
			stripped.append(item)
			continue
		copy = dict(item)
		matched = False
		override = copy.get("custom_rate_override")
		if override not in (None, "") and code in line_rates:
			if abs(flt(override) - flt(line_rates[code])) <= _RATE_TOLERANCE:
				copy.pop("custom_rate_override", None)
				matched = True
		source_discounts = line_discounts.get(code) or {}
		for fieldname in _LINE_DISCOUNT_FIELDS:
			value = copy.get(fieldname)
			if value in (None, ""):
				continue
			if flt(value) == 0 or abs(flt(value) - flt(source_discounts.get(fieldname))) <= _RATE_TOLERANCE:
				copy.pop(fieldname, None)
				matched = True
		if matched:
			budget[code] = remaining - qty
		stripped.append(copy)
	return stripped


def _line_total(invoice_doc: Any) -> float:
	total = 0.0
	for item in _get(invoice_doc, "items") or []:
		amount = _get(item, "amount")
		if amount in (None, ""):
			amount = flt(_get(item, "rate")) * flt(_get(item, "qty"))
		total += flt(amount)
	return total


def apply_header_discount_carry(
	invoice_doc: Any,
	carry: dict[str, Any] | None,
	promo_eval: Any,
	promo_codes_list: list[str],
) -> float:
	"""Keep the source's header discount when re-evaluation cannot reproduce it.

	The source's promo codes are re-evaluated against the edited cart first, and when
	EVERY code is accepted that result stands -- a percentage promo on a smaller cart
	is rightly smaller. Otherwise (a Woo coupon the POS channel rejects, a code whose
	window closed after the order was placed, a Woo non-coupon discount with no code
	at all) the source's discount amount is kept, capped at the new line total.
	Returns the amount carried, 0 when nothing was changed.

	A source discount with no code behind it (a Woo non-coupon discount) is not
	re-evaluated by any code, so a code the operator adds does not replace it: the
	larger of the two stands.
	"""
	if not carry:
		return 0.0
	source_discount = flt(carry.get("header_discount"))
	if source_discount <= 0:
		return 0.0
	source_codes = carry.get("source_promo_codes", carry.get("promo_codes")) or []
	results = list(getattr(promo_eval, "results", None) or [])
	# Judged on the SOURCE's codes only: a code the operator added and the engine
	# rejected says nothing about whether the source discount was reproduced, and
	# letting it trip the carry would put the full source amount back on a smaller cart.
	source_results = {
		str(getattr(r, "code", "") or "").strip().upper(): bool(getattr(r, "accepted", False)) for r in results
	}
	if (
		source_codes
		and promo_codes_list
		and all(source_results.get(str(code).strip().upper()) for code in source_codes)
	):
		return 0.0
	current = flt(_get(invoice_doc, "discount_amount"))
	if current >= source_discount - _RATE_TOLERANCE:
		return 0.0
	carried = round(min(source_discount, max(_line_total(invoice_doc), 0.0)), 2)
	if carried <= current:
		return 0.0
	invoice_doc.apply_discount_on = "Grand Total"
	invoice_doc.discount_amount = carried
	invoice_doc.additional_discount_percentage = 0
	if promo_codes_list:
		invoice_doc.custom_promo_discount_total = carried
		invoice_doc.custom_promo_applied = 1
	woo_total = flt(carry.get("woo_discount_total"))
	if woo_total > 0:
		invoice_doc.custom_promo_woo_discount_total = woo_total
	return carried
