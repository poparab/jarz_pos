from __future__ import annotations

import os
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
_RELEASE_NAME_CACHE: str | None = None
_RELEASE_NAME_CACHED = False
_RELEASE_PREFIX = "jarz-pos-backend"
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
	"""Release tag for Sentry, e.g. ``jarz-pos-backend@<sha>``.

	``site_config.sentry_release_backend`` still wins when it is set, but nothing in
	the deploy tooling ever writes that key — so for years both servers reported a
	release pinned to whatever commit happened to be current when the key was first
	typed in (bbc8f39, 335 commits stale). Stack-trace line numbers then no longer
	match the running code and "first seen in release" is meaningless.

	OPERATIONAL NOTE: because the key still wins, this fallback stays INERT on any
	site whose site_config carries the stale value. Deleting
	``sentry_release_backend`` from staging's and production's site_config is what
	switches the release tag over to the deployed commit.

	When the key is absent we read the deployed commit out of the app's own ``.git``
	directory BY FILE READ. Deliberately not
	``frappe.utils.change_log.get_app_last_commit_ref()``: that shells out to git with
	a cwd-relative path, and since the deploy pulls with ``sudo git`` the repo is not
	owned by the ``frappe`` process user, so git refuses it under ``safe.directory``.

	Everything is wrapped so this can only ever return a string or None. It is called
	from ``ensure_sentry()`` inside the ``before_request`` hook: an exception escaping
	here would break every HTTP request on the site.
	"""
	global _RELEASE_NAME_CACHE, _RELEASE_NAME_CACHED

	try:
		configured = str(frappe.conf.get("sentry_release_backend") or "").strip()
	except Exception:  # pragma: no cover - conf is always readable in practice
		configured = ""
	if configured:
		return configured

	if _RELEASE_NAME_CACHED:
		return _RELEASE_NAME_CACHE

	commit = _read_app_commit_sha()
	_RELEASE_NAME_CACHE = f"{_RELEASE_PREFIX}@{commit}" if commit else None
	_RELEASE_NAME_CACHED = True
	return _RELEASE_NAME_CACHE


def _reset_release_name_cache() -> None:
	"""Drop the memoized release name (tests only)."""
	global _RELEASE_NAME_CACHE, _RELEASE_NAME_CACHED

	_RELEASE_NAME_CACHE = None
	_RELEASE_NAME_CACHED = False


def _read_app_commit_sha() -> str | None:
	"""Deployed commit of the ``jarz_pos`` app, read straight off ``.git``."""
	try:
		app_dir = os.path.dirname(frappe.get_app_path("jarz_pos"))
		git_dir = _resolve_git_dir(app_dir)
		if not git_dir:
			return None

		with open(os.path.join(git_dir, "HEAD"), encoding="utf-8") as handle:
			head = handle.read().strip()
		if not head:
			return None

		# Detached HEAD: the file holds the sha itself.
		if not head.startswith("ref:"):
			return head if _is_commit_sha(head) else None

		ref = head.split(":", 1)[1].strip()
		if not ref:
			return None

		# Loose ref first (.git/refs/heads/main), then the packed-refs table.
		loose_ref = os.path.join(git_dir, *ref.split("/"))
		try:
			with open(loose_ref, encoding="utf-8") as handle:
				sha = handle.read().strip()
			if _is_commit_sha(sha):
				return sha
		except OSError:
			pass

		return _read_packed_ref(git_dir, ref)
	except Exception:
		return None


def _resolve_git_dir(app_dir: str) -> str | None:
	"""``<app>/.git`` — following the ``gitdir:`` pointer when it is a file."""
	git_path = os.path.join(app_dir, ".git")
	if os.path.isdir(git_path):
		return git_path
	if os.path.isfile(git_path):
		with open(git_path, encoding="utf-8") as handle:
			pointer = handle.read().strip()
		if pointer.startswith("gitdir:"):
			target = pointer.split(":", 1)[1].strip()
			if not os.path.isabs(target):
				target = os.path.join(app_dir, target)
			return target if os.path.isdir(target) else None
	return None


def _read_packed_ref(git_dir: str, ref: str) -> str | None:
	packed_refs = os.path.join(git_dir, "packed-refs")
	try:
		with open(packed_refs, encoding="utf-8") as handle:
			for raw_line in handle:
				line = raw_line.strip()
				# "#" is the header, "^" is a tag's peeled object.
				if not line or line.startswith("#") or line.startswith("^"):
					continue
				parts = line.split()
				if len(parts) != 2:
					continue
				sha, name = parts[0], parts[1]
				if name == ref and _is_commit_sha(sha):
					return sha
	except OSError:
		return None
	return None


def _is_commit_sha(value: str) -> bool:
	candidate = str(value or "").strip()
	if not 7 <= len(candidate) <= 64:
		return False
	return all(character in "0123456789abcdefABCDEF" for character in candidate)


def _as_bool(value: Any) -> bool:
	if isinstance(value, bool):
		return value
	if value is None:
		return False
	return str(value).strip().lower() in {"1", "true", "yes", "on"}