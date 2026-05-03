from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import frappe


def normalize_environment(raw_value: str | None) -> str:
	normalized = (raw_value or "").strip().lower()
	if normalized in {"prod", "production"}:
		return "production"
	if normalized in {"staging", "test", "testing"}:
		return "staging"
	if normalized == "local":
		return "local"
	return normalized or "local"


def infer_backend_app(
	*,
	command: str | None = None,
	path: str | None = None,
	method_name: str | None = None,
) -> str:
	combined = " ".join(
		segment for segment in [command, path, method_name] if segment
	).lower()

	if "jarz_woocommerce_integration" in combined:
		return "jarz_woocommerce_integration"
	if "jarz_pos" in combined:
		return "jarz_pos"
	if "erpnext" in combined:
		return "erpnext"
	return "frappe"


def current_request_context() -> dict[str, Any]:
	request = getattr(frappe.local, "request", None)
	command = _extract_command()
	path = getattr(request, "path", None)
	method = getattr(request, "method", None)
	staff_email = _current_user_email()

	return {
		"site": getattr(frappe.local, "site", None),
		"service": "request",
		"command": command,
		"path": path,
		"method": method,
		"backend_app": infer_backend_app(command=command, path=path),
		"staff_email": staff_email,
	}


def current_job_context(
	*,
	method_name: str | None,
	kwargs: Mapping[str, Any] | None,
	transaction_type: str | None,
) -> dict[str, Any]:
	job = getattr(frappe.local, "job", None)
	resolved_method = method_name or getattr(job, "method", None)
	resolved_kwargs = dict(kwargs or getattr(job, "kwargs", None) or {})
	staff_email = getattr(job, "user", None)

	return {
		"site": getattr(frappe.local, "site", None),
		"service": _infer_job_service(resolved_method, transaction_type),
		"method_name": resolved_method,
		"backend_app": infer_backend_app(method_name=resolved_method),
		"job_keys": sorted(str(key) for key in resolved_kwargs.keys())[:20],
		"staff_email": staff_email if isinstance(staff_email, str) else None,
	}


def _extract_command() -> str | None:
	form_dict = getattr(frappe.local, "form_dict", None)
	if form_dict is None:
		return None

	if isinstance(form_dict, Mapping):
		value = form_dict.get("cmd")
		return value if isinstance(value, str) else None

	get_method = getattr(form_dict, "get", None)
	if callable(get_method):
		value = get_method("cmd")
		return value if isinstance(value, str) else None

	return None


def _current_user_email() -> str | None:
	user = getattr(getattr(frappe, "session", None), "user", None)
	return user if isinstance(user, str) and user else None


def _infer_job_service(method_name: str | None, transaction_type: str | None) -> str:
	normalized = (method_name or "").lower()
	if "scheduler" in normalized or "cron" in normalized:
		return "scheduler"
	if transaction_type == "job":
		return "job"
	return "request"