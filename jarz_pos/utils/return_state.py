"""Shared SQL / filter fragments for "fully returned" Sales Invoices.

Business rule: a post-dispatch order that has been fully returned counts as a
CANCELLED order in every report and analytics endpoint.  A return never cancels
the original invoice (docstatus stays 1, ``is_return`` 0); it posts a credit
note and stamps the original with ``custom_return_status = 'Fully Returned'``.

Wherever a query treats a cancelled invoice (``docstatus = 2``) in some way,
treat a fully returned original the same way, using these helpers.  The column
is schema-probed so a bench without the custom field keeps working.
"""

from __future__ import annotations

import frappe

FULLY_RETURNED = "Fully Returned"


def has_return_status_col() -> bool:
	try:
		return bool(frappe.db.has_column("Sales Invoice", "custom_return_status"))
	except Exception:
		return False


def fully_returned_sql(alias: str = "si") -> str:
	"""SQL condition: ``alias`` is a fully returned ORIGINAL invoice (alias may be empty)."""
	if not has_return_status_col():
		return "(1 = 0)"
	p = f"{alias}." if alias else ""
	return (
		f"({p}is_return = 0 AND {p}docstatus = 1 "
		f"AND IFNULL({p}`custom_return_status`, '') = '{FULLY_RETURNED}')"
	)


def not_fully_returned_sql(alias: str = "si") -> str:
	"""SQL condition: ``alias`` is NOT a fully returned original invoice."""
	if not has_return_status_col():
		return "(1 = 1)"
	return f"NOT {fully_returned_sql(alias)}"


def not_fully_returned_filters() -> list:
	"""frappe.get_all/get_list filter rows excluding fully returned originals.

	Frappe renders ``!=`` as ``ifnull(col, '') != 'Fully Returned'`` so NULLs
	are kept.
	"""
	if not has_return_status_col():
		return []
	return [["custom_return_status", "!=", FULLY_RETURNED]]


def has_return_courier_paid_col() -> bool:
	try:
		return bool(frappe.db.has_column("Sales Invoice", "custom_return_courier_paid"))
	except Exception:
		return False


def counted_order_sql(alias: str = "si") -> str:
	"""SQL condition: ``alias`` counts as an order (not a fully returned original).

	Use it inside ``CASE WHEN`` for counts, pickup/delivery splits, per-order
	averages and billed shipping income of reports that must still see the row
	for its shipping expense.
	"""
	return not_fully_returned_sql(alias)


def returned_trip_unpaid_sql(alias: str = "si") -> str:
	"""SQL condition: ``alias`` is a fully returned original whose courier trip was NOT paid.

	The trip is unpaid only when a submitted credit note against the original
	carries ``custom_return_courier_paid = 0``; a missing value means paid (the
	return dialog default). ``alias`` must be a real, non-empty table alias so
	the subquery can reference the outer ``<alias>.name``.
	"""
	if not alias:
		raise ValueError("returned_trip_unpaid_sql needs a non-empty alias")
	if not has_return_status_col() or not has_return_courier_paid_col():
		return "(1 = 0)"
	return (
		f"({fully_returned_sql(alias)} AND EXISTS ("
		"SELECT 1 FROM `tabSales Invoice` rcn "
		f"WHERE rcn.return_against = {alias}.name AND rcn.docstatus = 1 AND rcn.is_return = 1 "
		"AND IFNULL(rcn.custom_return_courier_paid, 1) = 0))"
	)


def shipping_expense_sql(alias: str = "si") -> str:
	"""SQL expression: the shipping expense of ``alias`` (0 for a fully returned, unpaid trip)."""
	return (
		f"(CASE WHEN {returned_trip_unpaid_sql(alias)} THEN 0 "
		f"ELSE {alias}.custom_shipping_expense END)"
	)
