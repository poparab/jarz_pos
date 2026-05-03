from __future__ import annotations

import threading
from collections.abc import Mapping
from typing import Any

import frappe

try:
	import sentry_sdk
except ImportError:  # pragma: no cover
	sentry_sdk = None

from jarz_pos.observability.request_context import (
	current_job_context,
	current_request_context,
	normalize_environment,
)

_INITIALIZED = False
_INIT_LOCK = threading.Lock()
_SENSITIVE_KEYS = (
	"password",
	"authorization",
	"cookie",
	"sid",
	"secret",
	"token",
	"webhook-signature",
	"webhook_signature",
)


def before_request(*args, **kwargs):  # noqa: ANN002, ANN003
	ensure_sentry()


def after_request(response=None, request=None, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ARG001
	error_id = getattr(getattr(frappe, "flags", None), "jarz_error_id", None)
	if response is not None and error_id:
		response.headers["X-Error-Id"] = str(error_id)


def before_job(method=None, kwargs=None, transaction_type=None, *args, **ignored):  # noqa: ANN001, ANN002, ANN003, ARG001
	ensure_sentry()


def after_job(method=None, kwargs=None, result=None, *args, **ignored):  # noqa: ANN001, ANN002, ANN003, ARG001
	return None


def ensure_sentry() -> bool:
	global _INITIALIZED

	if _INITIALIZED:
		return True
	if not is_sentry_enabled():
		return False

	with _INIT_LOCK:
		if _INITIALIZED:
			return True

		if sentry_sdk is None:
			return False

		sentry_sdk.init(
			dsn=str(frappe.conf.get("sentry_dsn_backend") or "").strip(),
			environment=current_environment(),
			release=_release_name(),
			send_default_pii=False,
			traces_sample_rate=0.0,
			before_send=_before_send,
		)
		_INITIALIZED = True

	return True


def is_sentry_enabled() -> bool:
	return sentry_sdk is not None and _as_bool(frappe.conf.get("sentry_enabled")) and bool(
		str(frappe.conf.get("sentry_dsn_backend") or "").strip()
	)


def current_environment() -> str:
	return normalize_environment(frappe.conf.get("sentry_environment"))


def capture_exception(
	error: Exception,
	*,
	source: str,
	summary: str,
	details: dict[str, Any] | None = None,
	tags: dict[str, str] | None = None,
) -> str | None:
	if not ensure_sentry() or sentry_sdk is None:
		return None

	with sentry_sdk.push_scope() as scope:
		_apply_scope(scope, source=source, summary=summary, details=details, tags=tags)
		event_id = sentry_sdk.capture_exception(error)

	return str(event_id) if event_id else None


def capture_message(
	*,
	message: str,
	source: str,
	summary: str,
	details: dict[str, Any] | None = None,
	tags: dict[str, str] | None = None,
	level: str = "warning",
) -> str | None:
	if not ensure_sentry() or sentry_sdk is None:
		return None

	with sentry_sdk.push_scope() as scope:
		_apply_scope(scope, source=source, summary=summary, details=details, tags=tags)
		event_id = sentry_sdk.capture_message(message, level=level)

	return str(event_id) if event_id else None


def _apply_scope(
	scope,
	*,
	source: str,
	summary: str,
	details: dict[str, Any] | None,
	tags: dict[str, str] | None,
) -> None:
	context = _current_scope_context()

	scope.set_tag("environment", current_environment())
	scope.set_tag("source", source)
	if context.get("site"):
		scope.set_tag("site", str(context["site"]))
	if context.get("backend_app"):
		scope.set_tag("backend_app", str(context["backend_app"]))
	if context.get("service"):
		scope.set_tag("service", str(context["service"]))

	if tags:
		for key, value in tags.items():
			if value:
				scope.set_tag(key, str(value))

	scope.set_context("jarz_summary", {"value": summary})
	if details:
		scope.set_context("jarz_details", _sanitize_mapping(details))
	if context.get("request"):
		scope.set_context("request_context", context["request"])
	if context.get("job"):
		scope.set_context("job_context", context["job"])
	if _allow_staff_email() and context.get("staff_user"):
		scope.set_context("staff_user", {"email": context["staff_user"]})


def _current_scope_context() -> dict[str, Any]:
	if getattr(frappe.local, "job", None):
		job_context = current_job_context(
			method_name=getattr(frappe.local.job, "method", None),
			kwargs=getattr(frappe.local.job, "kwargs", None),
			transaction_type="job",
		)
		return {
			"site": job_context.get("site"),
			"backend_app": job_context.get("backend_app"),
			"service": job_context.get("service"),
			"job": {
				"method_name": job_context.get("method_name"),
				"job_keys": job_context.get("job_keys"),
			},
			"staff_user": job_context.get("staff_email"),
		}

	request_context = current_request_context()
	return {
		"site": request_context.get("site"),
		"backend_app": request_context.get("backend_app"),
		"service": request_context.get("service"),
		"request": {
			"command": request_context.get("command"),
			"path": request_context.get("path"),
			"method": request_context.get("method"),
		},
		"staff_user": request_context.get("staff_email"),
	}


def _before_send(event: dict[str, Any], hint: Mapping[str, Any]) -> dict[str, Any]:  # noqa: ARG001
	request = event.get("request") or {}
	if request:
		headers = request.get("headers") or {}
		if isinstance(headers, Mapping):
			request["headers"] = {
				key: value
				for key, value in headers.items()
				if not _is_sensitive_key(str(key))
			}
		request["data"] = _sanitize_value("request_data", request.get("data"))
		event["request"] = request

	extra = event.get("extra") or {}
	if isinstance(extra, Mapping):
		event["extra"] = _sanitize_mapping(extra)

	if not _allow_staff_email():
		event.pop("user", None)

	return event


def _sanitize_mapping(values: Mapping[str, Any]) -> dict[str, Any]:
	return {
		str(key): _sanitize_value(str(key), value)
		for key, value in values.items()
	}


def _sanitize_value(key: str, value: Any) -> Any:
	if _is_sensitive_key(key):
		return "<redacted>"
	if value is None or isinstance(value, (bool, int, float)):
		return value
	if isinstance(value, str):
		return value if len(value) <= 4000 else f"{value[:4000]}...<truncated>"
	if isinstance(value, Mapping):
		return _sanitize_mapping(value)
	if isinstance(value, (list, tuple, set)):
		return [_sanitize_value(key, item) for item in list(value)[:20]]
	return str(value)


def _is_sensitive_key(key: str) -> bool:
	normalized = key.lower()
	return any(token in normalized for token in _SENSITIVE_KEYS)


def _allow_staff_email() -> bool:
	return _as_bool(frappe.conf.get("sentry_staff_email_enabled"))


def _release_name() -> str | None:
	value = str(frappe.conf.get("sentry_release_backend") or "").strip()
	return value or None


def _as_bool(value: Any) -> bool:
	if isinstance(value, bool):
		return value
	if value is None:
		return False
	return str(value).strip().lower() in {"1", "true", "yes", "on"}