from __future__ import annotations

from typing import Any
from uuid import uuid4

import frappe

from jarz_pos.observability.sentry_bootstrap import capture_exception

DEFAULT_UNEXPECTED_USER_MESSAGE = (
	"We could not complete this action. Please try again or contact support with the reference code."
)


def unexpected_error_response(
	error: Exception,
	*,
	summary: str,
	context: str | None = None,
	user_message: str = DEFAULT_UNEXPECTED_USER_MESSAGE,
	http_status_code: int = 500,
	details: dict[str, Any] | None = None,
	tags: dict[str, str] | None = None,
) -> dict[str, Any]:
	error_id = capture_exception(
		error,
		source="unexpected_error_response",
		summary=summary,
		details=_merge_context(details, context),
		tags=tags,
	) or _fallback_error_id()

	_set_http_status(http_status_code)
	_set_error_id(error_id)

	response = {
		"success": False,
		"error": True,
		"error_code": "UNEXPECTED_SERVER_ERROR",
		"message": user_message,
		"user_message": user_message,
		"error_id": error_id,
		"timestamp": frappe.utils.now(),
	}
	if context:
		response["context"] = context
	return response


def expected_error_response(
	*,
	error_code: str,
	user_message: str,
	http_status_code: int = 400,
	context: str | None = None,
) -> dict[str, Any]:
	_set_http_status(http_status_code)

	response = {
		"success": False,
		"error": True,
		"error_code": error_code,
		"message": user_message,
		"user_message": user_message,
		"timestamp": frappe.utils.now(),
	}
	if context:
		response["context"] = context
	return response


def _merge_context(
	details: dict[str, Any] | None,
	context: str | None,
) -> dict[str, Any] | None:
	merged = dict(details or {})
	if context:
		merged.setdefault("context", context)
	return merged or None


def _set_http_status(http_status_code: int) -> None:
	if getattr(frappe.local, "response", None):
		frappe.local.response.http_status_code = http_status_code


def _set_error_id(error_id: str) -> None:
	if not hasattr(frappe, "flags"):
		frappe.flags = frappe._dict()
	frappe.flags.jarz_error_id = error_id


def _fallback_error_id() -> str:
	return uuid4().hex[:12].upper()