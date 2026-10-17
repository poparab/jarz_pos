"""Jarz Chat Settings, read once per request as a plain dict.

Every chat service reads its configuration through :func:`get_chat_settings`
rather than ``frappe.get_single``: the dict carries the fallbacks the contract
fixes (a Single ignores the JSON ``default`` of a field nobody has saved, see
``utils/settings_utils.py``), the decrypted secrets, and it is cached on
``frappe.local`` so one webhook batch does not reload the Single per event.

The secrets in the returned dict are live credentials. Never log the dict,
never return it from an endpoint, never put it in a realtime payload.
"""

from __future__ import annotations

from typing import Any

import frappe

SETTINGS_DOCTYPE = "Jarz Chat Settings"

DEFAULT_MODEL = "claude-haiku-5-5"
ALLOWED_MODELS = ("claude-haiku-5-5", "claude-sonnet-5-5")
DEFAULT_GRAPH_VERSION = "v24.0"
DEFAULT_ALERT_AFTER_MINUTES = 10
DEFAULT_ALERT_REPEAT_MINUTES = 10
PLANTED_PERCENT_MAX = 20.0

_SECRET_FIELDS = (
	"anthropic_api_key",
	"meta_app_secret",
	"webhook_verify_token",
	"whatsapp_access_token",
	"page_access_token",
)

_CACHE_ATTR = "jarz_chat_settings_cache"


def _clean(value: Any) -> str:
	return str(value or "").strip()


def _int(value: Any, default: int, minimum: int = 1) -> int:
	try:
		number = int(value)
	except (TypeError, ValueError):
		return default
	return number if number >= minimum else default


def _secret(doc: Any, fieldname: str) -> str:
	try:
		return _clean(doc.get_password(fieldname, raise_exception=False))
	except Exception:
		return ""


def build_settings(doc: Any, flags: dict[str, Any] | None = None) -> dict[str, Any]:
	"""Normalise a Settings document into the contract dict.

	``flags`` carries the never-written-aware Check / Float values read through
	``settings_utils`` (the document itself reports 0 for a Check nobody saved).
	Kept separate from :func:`get_chat_settings` so it is testable with a stub.
	"""
	flags = flags or {}
	get = doc.get if hasattr(doc, "get") else (lambda key, default=None: getattr(doc, key, default))

	model = _clean(get("draft_model")) or DEFAULT_MODEL
	if model not in ALLOWED_MODELS:
		model = DEFAULT_MODEL

	try:
		planted = float(flags.get("planted_draft_percent", get("planted_draft_percent") or 0) or 0)
	except (TypeError, ValueError):
		planted = 0.0
	planted = max(0.0, min(PLANTED_PERCENT_MAX, planted))

	moderators: list[dict[str, Any]] = []
	for row in get("moderators") or []:
		user = _clean(row.get("user") if hasattr(row, "get") else getattr(row, "user", None))
		if not user:
			continue
		alerts = row.get("receive_alerts") if hasattr(row, "get") else getattr(row, "receive_alerts", 1)
		moderators.append({"user": user, "receive_alerts": 1 if alerts in (None, "") else int(alerts or 0)})

	upsell: list[dict[str, Any]] = []
	for row in get("upsell_items") or []:
		getter = (
			row.get if hasattr(row, "get") else (lambda key, default=None, _r=row: getattr(_r, key, default))
		)
		code = _clean(getter("item_code"))
		active = getter("active")
		if not code or (active not in (None, "") and not int(active or 0)):
			continue
		upsell.append({"item_code": code, "pitch": _clean(getter("pitch"))})

	ready: list[dict[str, Any]] = []
	for row in get("ready_answers") or []:
		getter = (
			row.get if hasattr(row, "get") else (lambda key, default=None, _r=row: getattr(_r, key, default))
		)
		topic = _clean(getter("topic"))
		answer = _clean(getter("answer"))
		active = getter("active")
		if not topic or not answer or (active not in (None, "") and not int(active or 0)):
			continue
		ready.append({"topic": topic, "when_to_use": _clean(getter("when_to_use")), "answer": answer})

	settings: dict[str, Any] = {
		"enabled": bool(flags.get("enabled", get("enabled"))),
		"auto_draft": bool(flags.get("auto_draft", True)),
		"alert_after_minutes": _int(get("alert_after_minutes"), DEFAULT_ALERT_AFTER_MINUTES),
		"alert_repeat_minutes": _int(get("alert_repeat_minutes"), DEFAULT_ALERT_REPEAT_MINUTES),
		"moderators": moderators,
		"draft_model": model,
		"mask_personal_data": bool(flags.get("mask_personal_data", True)),
		"default_pos_profile": _clean(get("default_pos_profile")),
		"brand_instructions": _clean(get("brand_instructions")),
		"planted_draft_percent": planted,
		"upsell_items": upsell,
		"ready_answers": ready,
		"graph_api_version": _clean(get("graph_api_version")) or DEFAULT_GRAPH_VERSION,
		"whatsapp_phone_number_id": _clean(get("whatsapp_phone_number_id")),
		"whatsapp_business_account_id": _clean(get("whatsapp_business_account_id")),
		"facebook_page_id": _clean(get("facebook_page_id")),
		"instagram_account_id": _clean(get("instagram_account_id")),
	}
	for field in _SECRET_FIELDS:
		settings[field] = _secret(doc, field)
	return settings


def _read_flags() -> dict[str, Any]:
	from jarz_pos.utils.settings_utils import single_flag, single_float

	flags: dict[str, Any] = {}
	for field, default in (("enabled", False), ("auto_draft", True), ("mask_personal_data", True)):
		try:
			flags[field] = single_flag(SETTINGS_DOCTYPE, field, default)
		except Exception:
			flags[field] = default
	try:
		flags["planted_draft_percent"] = single_float(SETTINGS_DOCTYPE, "planted_draft_percent", 0.0)
	except Exception:
		flags["planted_draft_percent"] = 0.0
	return flags


def get_chat_settings(refresh: bool = False) -> dict[str, Any]:
	"""The settings dict, cached on ``frappe.local`` for the rest of the request.

	Never raises: a missing table (DocType not migrated yet) yields a disabled
	default dict, which every caller treats as "chat is off".
	"""
	local = getattr(frappe, "local", None)
	if not refresh and local is not None:
		cached = getattr(local, _CACHE_ATTR, None)
		if isinstance(cached, dict):
			return cached
	try:
		doc = frappe.get_cached_doc(SETTINGS_DOCTYPE) if not refresh else frappe.get_single(SETTINGS_DOCTYPE)
		settings = build_settings(doc, _read_flags())
	except Exception:
		settings = build_settings({}, {"enabled": False})
	if local is not None:
		try:
			setattr(local, _CACHE_ATTR, settings)
		except Exception:
			pass
	return settings


def clear_cache() -> None:
	local = getattr(frappe, "local", None)
	if local is not None and hasattr(local, _CACHE_ATTR):
		try:
			delattr(local, _CACHE_ATTR)
		except Exception:
			pass
