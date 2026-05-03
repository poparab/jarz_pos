from __future__ import annotations

import frappe

from jarz_pos.observability import error_response
from jarz_pos.observability.request_context import infer_backend_app, normalize_environment
from jarz_pos.observability import sentry_bootstrap


def test_normalize_environment_aliases() -> None:
	assert normalize_environment("prod") == "production"
	assert normalize_environment("production") == "production"
	assert normalize_environment("staging") == "staging"
	assert normalize_environment("testing") == "staging"


def test_infer_backend_app_prefers_custom_app_prefixes() -> None:
	assert (
		infer_backend_app(command="jarz_woocommerce_integration.api.orders.woo_order_webhook")
		== "jarz_woocommerce_integration"
	)
	assert (
		infer_backend_app(path="/api/method/jarz_pos.api.test_connection.ping")
		== "jarz_pos"
	)


def test_before_send_redacts_sensitive_fields() -> None:
	event = {
		"request": {
			"headers": {
				"Authorization": "Bearer secret",
				"X-Test": "ok",
			},
			"data": {
				"password": "hidden",
				"customer": "CUST-0001",
			},
		},
		"extra": {
			"token": "private-token",
			"safe": "value",
		},
	}

	sanitized = sentry_bootstrap._before_send(event, {})

	assert sanitized["request"]["headers"] == {"X-Test": "ok"}
	assert sanitized["request"]["data"]["password"] == "<redacted>"
	assert sanitized["extra"]["token"] == "<redacted>"


def test_unexpected_error_response_sets_status_and_error_id(monkeypatch) -> None:
	previous_local = getattr(frappe, "local", None)
	previous_flags = getattr(frappe, "flags", None)

	monkeypatch.setattr(error_response, "capture_exception", lambda *args, **kwargs: None)
	monkeypatch.setattr(frappe.utils, "now", lambda: "2026-05-03 00:00:00")

	try:
		frappe.local = frappe._dict(response=frappe._dict())
		frappe.flags = frappe._dict()

		result = error_response.unexpected_error_response(
			ValueError("boom"),
			summary="Unit test exception",
			context="Unit Test",
		)
	finally:
		frappe.local = previous_local
		frappe.flags = previous_flags

	assert result["error_code"] == "UNEXPECTED_SERVER_ERROR"
	assert result["context"] == "Unit Test"
	assert result["error_id"]
	assert result["user_message"]