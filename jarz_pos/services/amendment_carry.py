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


def source_promo_codes(source: Any) -> list[str]:
	from jarz_pos.services import promo_codes as _promo_codes

	try:
		return _promo_codes._normalize_codes(_promo_codes._parse_codes_value(_get(source, "custom_promo_codes")))
	except Exception:
		return []


def build_amendment_carry(source: Any, *, zero_shipping: bool) -> dict[str, Any]:
	"""Everything the replacement must inherit from ``source`` beyond its cart."""
	return {
		"source": str(_get(source, "name") or "").strip(),
		"line_rates": source_line_rate_overrides(source),
		"line_discounts": source_line_discounts(source),
		"source_qty": source_item_quantities(source),
		"promo_codes": source_promo_codes(source),
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
	stripped: list[Any] = []
	for item in cart_items:
		if not isinstance(item, dict) or item.get("is_bundle"):
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
	"""
	if not carry:
		return 0.0
	source_discount = flt(carry.get("header_discount"))
	if source_discount <= 0:
		return 0.0
	results = list(getattr(promo_eval, "results", None) or [])
	if promo_codes_list and results and all(getattr(r, "accepted", False) for r in results):
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
