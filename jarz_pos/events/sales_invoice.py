"""
Sales Invoice event handlers for Kanban profile seeding and realtime notifications.

These handlers are referenced in hooks.py. Implemented as safe no-ops with
lightweight behavior so integrations depending on these hooks don't fail.

You can extend these later for full real-time UI updates.
"""

from __future__ import annotations

from typing import Any, Optional

try:
	import frappe
except Exception:  # pragma: no cover - allows import-time safety in non-Frappe contexts
	frappe = None  # type: ignore


def _safe_publish(event: str, message: dict[str, Any]) -> None:
	"""Publish a realtime message if frappe is available; ignore failures."""
	try:
		if frappe:
			frappe.publish_realtime(event, message)
	except Exception:
		# Avoid breaking document lifecycle if realtime publish fails
		try:
			if frappe:
				frappe.log_error(frappe.get_traceback(), f"jarz_pos realtime publish failed: {event}")
		except Exception:
			pass


def sync_kanban_profile(doc: Any, method: Optional[str] = None) -> None:
	"""Seed Kanban profile from POS Profile on draft invoices only.

	- Draft invoices keep `custom_kanban_profile` aligned with `pos_profile`.
	- Submitted invoices preserve `custom_kanban_profile` so post-submit
	  branch reassignment remains intact.
	- Resolve territory-based shipping expense into `custom_shipping_expense`
	  so every consumer reads the same persisted value.
	- Safe no-op if fields are missing.
	"""
	try:
		pos_profile = getattr(doc, "pos_profile", None)
		docstatus = int(getattr(doc, "docstatus", 0) or 0)
		if docstatus == 0 and pos_profile and hasattr(doc, "custom_kanban_profile"):
			if getattr(doc, "custom_kanban_profile", None) != pos_profile:
				setattr(doc, "custom_kanban_profile", pos_profile)
	except Exception:
		if frappe:
			frappe.log_error(frappe.get_traceback(), "sync_kanban_profile failed")

	# Stamp territory-based shipping expense when not already set
	try:
		if hasattr(doc, "custom_shipping_expense"):
			current = float(getattr(doc, "custom_shipping_expense", 0) or 0)
			if current <= 0 and getattr(doc, "territory", None):
				from jarz_pos.services.delivery_handling import _get_delivery_expense_amount
				expense = _get_delivery_expense_amount(doc) or 0.0
				if expense > 0:
					doc.custom_shipping_expense = expense
	except Exception:
		if frappe:
			frappe.log_error(frappe.get_traceback(), "sync_shipping_expense failed")


def publish_new_invoice(doc: Any, method: Optional[str] = None) -> None:
	"""Notify listeners a Sales Invoice has been submitted."""
	try:
		from jarz_pos.api import notifications as _notifications  # local import to avoid circulars

		_notifications.handle_invoice_submission(doc)
	except Exception:
		# Fall back to legacy event payload if enhanced notification fails
		_safe_publish(
			"jarz_pos:new_invoice",
			{"name": getattr(doc, "name", None), "status": getattr(doc, "status", None)},
		)
		if frappe:
			frappe.log_error(frappe.get_traceback(), "handle_invoice_submission failed")


def publish_state_change_if_needed(doc: Any, method: Optional[str] = None) -> None:
	"""Emit a generic state-change notification for already-submitted invoices.

	Intentionally lightweight. Frontend can refetch details by name.
	"""
	_safe_publish("jarz_pos:invoice_state", {"name": getattr(doc, "name", None), "status": getattr(doc, "status", None)})


def mark_cancelled_invoice_workflow_fields(doc: Any, method: Optional[str] = None) -> None:
	"""Keep Jarz workflow fields aligned whenever a Sales Invoice is cancelled."""
	if not frappe or not doc or not getattr(doc, "name", None):
		return

	try:
		meta = frappe.get_meta("Sales Invoice")
		updates: dict[str, Any] = {}

		for fieldname in ("custom_sales_invoice_state", "sales_invoice_state", "custom_state", "state"):
			if not meta.get_field(fieldname):
				continue
			if str(getattr(doc, fieldname, None) or "").strip() != "Cancelled":
				updates[fieldname] = "Cancelled"

		if meta.get_field("custom_acceptance_status"):
			current_acceptance = str(getattr(doc, "custom_acceptance_status", None) or "").strip()
			if current_acceptance != "Accepted":
				updates["custom_acceptance_status"] = "Accepted"

			accepted_by = getattr(getattr(frappe, "session", None), "user", None)
			if meta.get_field("custom_accepted_by") and accepted_by and not getattr(doc, "custom_accepted_by", None):
				updates["custom_accepted_by"] = accepted_by

			if meta.get_field("custom_accepted_on") and not getattr(doc, "custom_accepted_on", None):
				updates["custom_accepted_on"] = frappe.utils.now_datetime()

		if not updates:
			return

		frappe.db.set_value("Sales Invoice", doc.name, updates, update_modified=False)
		for fieldname, value in updates.items():
			setattr(doc, fieldname, value)
	except Exception:
		if frappe:
			frappe.log_error(frappe.get_traceback(), "mark_cancelled_invoice_workflow_fields failed")


def validate_invoice_before_submit(doc: Any, method: Optional[str] = None) -> None:
	"""Placeholder for pre-submit validations (e.g., bundle checks).

	Currently a no-op to avoid interrupting flows. Add validations here later.
	"""
	return None

