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
DEFAULT_TRANSCRIPTION_MODEL = "gpt-4o-transcribe"

_SECRET_FIELDS = (
	"anthropic_api_key",
	"meta_app_secret",
	"webhook_verify_token",
	"whatsapp_access_token",
	"page_access_token",
	"openai_api_key",
)

_CACHE_ATTR = "jarz_chat_settings_cache"

#: Redis prefix of the cached AI draft context (menu, facts, ready answers); see
#: ``chat_drafts.build_context``. Saving the settings or a Brand Fact clears it.
DRAFT_CONTEXT_CACHE_PREFIX = "jarz_chat_ctx:"


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


#: Ready answers are pasted whole into the model prompt on every draft, so an
#: unbounded table is an unbounded prompt (cost, latency, and the model losing the
#: thread). Past these caps the lowest-priority rows are left out.
MAX_READY_ANSWERS = 50
MAX_READY_ANSWER_CHARS = 12000

_TRUNCATION_MARK = "\u2026"
_ready_cap_logged: set[tuple[int, int, int]] = set()


def _topic_key(topic: Any) -> str:
	return " ".join(str(topic or "").split()).lower()


def merge_ready_answers(
	defaults: list[dict[str, Any]],
	rows: list[dict[str, Any]] | None,
	disabled_topics: list[str] | None = None,
) -> list[dict[str, Any]]:
	"""The ready answers the model sees: the built-in *defaults* merged with the ERP *rows*.

	A row with the same topic as a default overrides it, a topic that is switched
	off in ERP (*disabled_topics*, unless an active row has it too) removes the
	default, and new topics are added. Active ERP rows come first, in the order
	they arrive (already priority-ordered), so :func:`cap_ready_answers` drops
	untouched defaults before anything staff wrote. Pure.
	"""
	rows = list(rows or [])
	own = {_topic_key(r.get("topic")) for r in rows}
	off = {_topic_key(t) for t in disabled_topics or []} - own
	rest = [d for d in defaults if _topic_key(d.get("topic")) not in own | off]
	return rows + rest


def clear_draft_context_cache() -> None:
	"""Drop every cached AI draft context (all POS profiles). Never raises."""
	try:
		frappe.cache().delete_keys(DRAFT_CONTEXT_CACHE_PREFIX)
	except Exception:
		pass


def render_ready_answer(row: dict[str, Any]) -> str:
	"""One ready answer as it appears in the prompt (also the unit the cap counts)."""
	when = f" (when: {row['when_to_use']})" if row.get("when_to_use") else ""
	return f"### {row['topic']}{when}\n{row['answer']}"


def _truncate_cleanly(text: str, limit: int) -> str:
	"""Cut *text* to at most *limit* characters at a line or word boundary."""
	if len(text) <= limit:
		return text
	if limit <= len(_TRUNCATION_MARK):
		return text[:limit]
	room = limit - len(_TRUNCATION_MARK)
	head = text[:room]
	# Prefer the last line break, then the last space, if that keeps most of the text.
	for sep in ("\n", " "):
		cut = head.rfind(sep)
		if cut >= room // 2:
			head = head[:cut]
			break
	return head.rstrip() + _TRUNCATION_MARK


def _log_ready_cap_once(total: int, kept: int, cut_chars: int) -> None:
	"""Record that the cap dropped answers; once per distinct outcome per process.

	The prompt is rebuilt every few minutes, so logging each time would bury the
	Error Log. ``log_error`` can itself raise (it needs a DB), hence the guard.
	"""
	signature = (total, kept, cut_chars)
	if signature in _ready_cap_logged:
		return
	_ready_cap_logged.add(signature)
	try:
		frappe.log_error(
			f"Jarz Chat ready answers capped for the model prompt: kept {kept} of {total} "
			f"(max {MAX_READY_ANSWERS} answers / {MAX_READY_ANSWER_CHARS} characters). "
			"Disable or shorten the lowest-priority Ready Answers to bring them under the cap.",
			"Jarz Chat ready answers capped",
		)
	except Exception:
		pass


def cap_ready_answers(
	rows: list[dict[str, Any]],
	max_answers: int = MAX_READY_ANSWERS,
	max_chars: int = MAX_READY_ANSWER_CHARS,
) -> list[dict[str, Any]]:
	"""Keep the leading *rows* that fit the prompt budget; *rows* are already in priority order.

	Whole answers are kept until the next one would pass *max_answers* or push the
	rendered total (:func:`render_ready_answer` plus a blank line between answers)
	past *max_chars*; then it stops, so a lower-priority short answer never jumps a
	higher-priority long one. If even the first answer alone is over budget its
	text is cut at a line/word boundary with an ellipsis. Logs once when anything
	is dropped or cut. Never raises.
	"""
	rows = list(rows or [])
	try:
		kept: list[dict[str, Any]] = []
		used = 0
		cut_chars = 0
		for row in rows:
			if len(kept) >= max_answers:
				break
			size = len(render_ready_answer(row)) + (2 if kept else 0)
			if used + size > max_chars:
				if kept:
					break
				# First answer alone is too long: keep the head of it.
				overhead = len(render_ready_answer({**row, "answer": ""}))
				allowed = max(0, max_chars - overhead)
				full = str(row.get("answer") or "")
				trimmed = _truncate_cleanly(full, allowed)
				cut_chars = len(full) - len(trimmed)
				kept.append({**row, "answer": trimmed})
				used = max_chars
				break
			kept.append(row)
			used += size
		if len(kept) < len(rows) or cut_chars:
			_log_ready_cap_once(len(rows), len(kept), cut_chars)
		return kept
	except Exception:
		return rows[:max_answers]


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

	# Enabled rows only, most recently modified first. The child DocType has no
	# priority field, so ties (every row saved together shares the parent's
	# ``modified``) fall back to ``idx``, the order staff arranged the grid in. The
	# sorts are stable, so the result is deterministic; then the prompt cap applies.
	ready_rows: list[tuple[str, int, dict[str, Any]]] = []
	ready_disabled: list[str] = []
	for position, row in enumerate(get("ready_answers") or []):
		getter = (
			row.get if hasattr(row, "get") else (lambda key, default=None, _r=row: getattr(_r, key, default))
		)
		topic = _clean(getter("topic"))
		answer = _clean(getter("answer"))
		active = getter("active")
		off = active not in (None, "") and not int(active or 0)
		if topic and off:
			ready_disabled.append(topic)
		if not topic or not answer or off:
			continue
		try:
			idx = int(getter("idx") or position + 1)
		except (TypeError, ValueError):
			idx = position + 1
		ready_rows.append(
			(
				str(getter("modified") or ""),
				idx,
				{"topic": topic, "when_to_use": _clean(getter("when_to_use")), "answer": answer},
			)
		)
	ready_rows.sort(key=lambda item: item[1])
	ready_rows.sort(key=lambda item: item[0], reverse=True)
	ready = cap_ready_answers([item[2] for item in ready_rows])

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
		# Topics switched off in ERP: they remove the built-in default of that topic.
		"ready_answers_disabled": ready_disabled,
		# Voice notes: off until staff switch it on (a Single ignores the JSON default).
		"transcribe_voice_notes": bool(flags.get("transcribe_voice_notes", get("transcribe_voice_notes"))),
		"transcription_model": _clean(get("transcription_model")) or DEFAULT_TRANSCRIPTION_MODEL,
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
	for field, default in (
		("enabled", False),
		("auto_draft", True),
		("mask_personal_data", True),
		("transcribe_voice_notes", False),
	):
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
