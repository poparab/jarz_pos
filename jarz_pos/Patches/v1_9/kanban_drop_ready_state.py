"""Convert every order sitting in the removed "Ready" kanban state to "In Progress".

The owner removed the Ready column on 2026-10-13: the flow is now
Received -> In Progress -> Out for Delivery -> Delivered, with no backward
moves. The ``Ready`` Select option is dropped by the Custom Field fixture, but a
row that still stores the old value would then fail validation on its next full
save and would match no board column.

Rules (re-runnable; a second run finds nothing to do):

* ``custom_sales_invoice_state`` and the mirrored legacy ``sales_invoice_state``
  are both converted, whichever of them holds "Ready";
* raw SQL, ``modified`` untouched: this is a label correction, not an edit, so it
  must not bump the invoices' timestamps (that would trip
  TimestampMismatchError for anyone holding one open) or fire realtime hooks
  during ``bench migrate``;
* one ``Invoice State Log`` row per converted invoice (source
  ``migration:drop_ready``) so the history shows why the card changed column.
  Skipped silently when the log DocType is not on the site yet.
"""

from __future__ import annotations

import frappe

OLD_STATE = "Ready"
NEW_STATE = "In Progress"
SOURCE = "migration:drop_ready"
STATE_COLUMNS = ("custom_sales_invoice_state", "sales_invoice_state")
LOG_DOCTYPE = "Invoice State Log"


def _columns() -> list[str]:
	return [c for c in STATE_COLUMNS if frappe.db.has_column("Sales Invoice", c)]


def execute() -> None:
	columns = _columns()
	if not columns:
		return

	# Collect the affected invoices before anything is overwritten.
	affected: dict[str, str | None] = {}
	for column in columns:
		# nosemgrep: frappe-sql-format-injection -- column name comes from the fixed STATE_COLUMNS allowlist, values are bound with %s
		rows = frappe.db.sql(
			f"SELECT `name`, `pos_profile` FROM `tabSales Invoice` WHERE `{column}` = %s",
			(OLD_STATE,),
		)
		for name, pos_profile in rows:
			affected.setdefault(name, pos_profile)

	if not affected:
		return

	for column in columns:
		# nosemgrep: frappe-sql-format-injection -- column name comes from the fixed STATE_COLUMNS allowlist, values are bound with %s
		frappe.db.sql(
			f"UPDATE `tabSales Invoice` SET `{column}` = %s WHERE `{column}` = %s",
			(NEW_STATE, OLD_STATE),
		)

	if frappe.db.exists("DocType", LOG_DOCTYPE):
		from jarz_pos.services.state_log import record_state_change

		now = frappe.utils.now_datetime()
		for name, pos_profile in affected.items():
			record_state_change(
				name,
				OLD_STATE,
				NEW_STATE,
				SOURCE,
				user="Administrator",
				at=now,
				pos_profile=pos_profile,
			)

	frappe.db.commit()
	try:
		frappe.log_error(
			message=(
				f"Converted {len(affected)} invoice(s) from '{OLD_STATE}' to "
				f"'{NEW_STATE}': {', '.join(sorted(affected)[:50])}" + (" ..." if len(affected) > 50 else "")
			),
			title="Kanban Ready state dropped",
		)
	except Exception:
		pass
