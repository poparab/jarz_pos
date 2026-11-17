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
