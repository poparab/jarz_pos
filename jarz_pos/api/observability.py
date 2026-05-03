from __future__ import annotations

import frappe
from frappe import _

from jarz_pos.observability.error_response import unexpected_error_response
from jarz_pos.observability.request_context import normalize_environment
from jarz_pos.observability.sentry_bootstrap import capture_message, is_sentry_enabled


def _require_system_manager() -> None:
	roles = set(frappe.get_roles(frappe.session.user))
	if frappe.session.user == "Guest" or "System Manager" not in roles:
		raise frappe.PermissionError(_("System Manager access is required."))


@frappe.whitelist(allow_guest=False)
def sentry_status() -> dict[str, object]:
	_require_system_manager()
	return {
		"success": True,
		"data": {
			"enabled": is_sentry_enabled(),
			"environment": normalize_environment(frappe.conf.get("sentry_environment")),
			"site": frappe.local.site,
		},
	}


@frappe.whitelist(allow_guest=False)
def trigger_test_exception() -> dict[str, object]:
	_require_system_manager()

	try:
		raise RuntimeError("Jarz observability synthetic test exception")
	except Exception as error:  # noqa: BLE001
		frappe.log_error(
			message=frappe.get_traceback(),
			title="Jarz Observability Synthetic Test Exception",
		)
		return unexpected_error_response(
			error,
			summary="Synthetic observability test exception",
			context="Jarz Observability",
			details={"source": "jarz_pos.api.observability.trigger_test_exception"},
		)


@frappe.whitelist(allow_guest=False)
def trigger_test_message() -> dict[str, object]:
	_require_system_manager()

	event_id = capture_message(
		message="Jarz observability synthetic test message",
		source="jarz_pos.api.observability.trigger_test_message",
		summary="Synthetic observability test message",
		details={"source": "jarz_pos.api.observability.trigger_test_message"},
		level="warning",
	)

	return {
		"success": True,
		"data": {
			"enabled": is_sentry_enabled(),
			"event_id": event_id,
		},
	}