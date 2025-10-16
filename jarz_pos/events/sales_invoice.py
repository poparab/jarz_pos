"""
Sales Invoice event handlers (minimal shims)

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
	"""Mirror POS Profile into a custom field used by Kanban, if present.

	- If doc has `pos_profile` and `custom_kanban_profile`, keep them in sync.
	- Safe no-op if fields are missing.
	"""
	try:
		pos_profile = getattr(doc, "pos_profile", None)
		if pos_profile and hasattr(doc, "custom_kanban_profile"):
			if getattr(doc, "custom_kanban_profile", None) != pos_profile:
				setattr(doc, "custom_kanban_profile", pos_profile)
	except Exception:
		if frappe:
			frappe.log_error(frappe.get_traceback(), "sync_kanban_profile failed")


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


def validate_invoice_before_submit(doc: Any, method: Optional[str] = None) -> None:
	"""Placeholder for pre-submit validations (e.g., bundle checks).

	Currently a no-op to avoid interrupting flows. Add validations here later.
	"""
	return None

