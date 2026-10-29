"""Voice notes to text, so the chat AI can read what a customer said.

OpenAI ``gpt-4o-transcribe`` (best on Egyptian Arabic; takes WhatsApp's
ogg/opus and Instagram's m4a as they are; 25 MB per file) through a plain
``requests`` multipart POST, no SDK. One attempt, 60 s: a voice note that
fails to transcribe is drafted blind, never retried.

Everything that can be decided without the network is a pure function
(:func:`skip_reason`, :func:`audio_filename`, :func:`vocabulary_prompt`,
:func:`build_form`) and tested alone. :func:`transcribe` never raises and never
logs the API key.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

import frappe

from jarz_pos.services import chat_common as cc

API_URL = "https://api.openai.com/v1/audio/transcriptions"
LANGUAGE = "ar"
TIMEOUT_SECONDS = 60
#: OpenAI's per-file limit.
MAX_AUDIO_BYTES = 25 * 1024 * 1024
#: The model is steered by this prompt; a long one costs time for nothing.
MAX_PROMPT_CHARS = 780
#: Stored and shown; a voice note longer than this is cut.
MAX_TRANSCRIPT_CHARS = 4000

#: Formats the endpoint accepts, by file extension.
SUPPORTED_EXTENSIONS = ("flac", "mp3", "mp4", "mpeg", "mpga", "m4a", "ogg", "wav", "webm")
_MIME_EXTENSION = {
	"audio/ogg": "ogg",
	"audio/opus": "ogg",
	"application/ogg": "ogg",
	"audio/mpeg": "mp3",
	"audio/mp3": "mp3",
	"audio/mp4": "m4a",
	"audio/x-m4a": "m4a",
	"audio/m4a": "m4a",
	"video/mp4": "mp4",
	"audio/wav": "wav",
	"audio/x-wav": "wav",
	"audio/webm": "webm",
	"audio/flac": "flac",
	"audio/x-flac": "flac",
}

_BASE_PROMPT = (
	"Voice message in Egyptian Arabic (colloquial) to Jarz, a dessert shop: orders of jars, cakes "
	"and boxes, delivery, payment by InstaPay or wallet. Words: جار، كيك، بوكس، توصيل، إنستا باي، "
	"فودافون كاش، كاش."
)

_KEY_LIKE = re.compile(r"sk-[A-Za-z0-9_\-\*\.]+")


# ── pure ────────────────────────────────────────────────────────────────


def redact(text: Any) -> str:
	"""*text* with anything that looks like an API key removed. Pure."""
	return _KEY_LIKE.sub("sk-...", str(text or ""))


def skip_reason(settings: dict[str, Any], row: dict[str, Any], size: int | None = None) -> str | None:
	"""Why this message is not transcribed, or ``None`` when it should be. Pure."""
	if not settings.get("transcribe_voice_notes"):
		return "transcription is off"
	if not str(settings.get("openai_api_key") or "").strip():
		return "no OpenAI key"
	if str(row.get("message_type") or "") != "audio":
		return "not a voice note"
	if not str(row.get("media_file") or "").strip():
		return "no file"
	if str(row.get("transcript") or "").strip():
		return "already transcribed"
	if size is not None and size > MAX_AUDIO_BYTES:
		return "file is larger than 25 MB"
	return None


def audio_filename(mime: Any, file_name: Any = None, message_name: Any = None) -> str:
	"""A file name with an extension the endpoint recognises (it sniffs by name). Pure."""
	stem = re.sub(r"[^A-Za-z0-9_-]+", "", str(message_name or "")) or "voice-note"
	ext = str(file_name or "").rsplit(".", 1)[-1].lower() if "." in str(file_name or "") else ""
	if ext not in SUPPORTED_EXTENSIONS:
		clean = str(mime or "").split(";")[0].strip().lower()
		ext = _MIME_EXTENSION.get(clean, "ogg")
	return f"{stem}.{ext}"


def vocabulary_prompt(item_names: Iterable[Any] = ()) -> str:
	"""The steering prompt: Egyptian Arabic plus the menu's names, under 800 characters. Pure."""
	prompt = _BASE_PROMPT
	names: list[str] = []
	for raw in item_names:
		name = " ".join(str(raw or "").split())
		if name and name.lower() not in {n.lower() for n in names}:
			names.append(name)
	menu = ""
	for name in names:
		candidate = f"{menu}, {name}" if menu else name
		if len(prompt) + len(" Menu: ") + len(candidate) > MAX_PROMPT_CHARS:
			break
		menu = candidate
	if menu:
		prompt = f"{prompt} Menu: {menu}"
	return prompt[:MAX_PROMPT_CHARS]


def build_form(model: str, prompt: str) -> dict[str, str]:
	"""The non-file fields of the multipart request. Pure."""
	return {"model": model, "language": LANGUAGE, "response_format": "json", "prompt": prompt}


def clean_transcript(text: Any) -> str:
	"""One tidy block of text from the API's answer. Pure."""
	body = "\n".join(" ".join(line.split()) for line in str(text or "").strip().splitlines())
	return body.strip()[:MAX_TRANSCRIPT_CHARS]


# ── network ─────────────────────────────────────────────────────────────


def _post(api_key: str, filename: str, content_type: str, content: bytes, form: dict[str, str]) -> str:
	"""One multipart POST. Returns the transcript text; raises on any failure."""
	import requests

	resp = requests.post(
		API_URL,
		headers={"Authorization": f"Bearer {api_key}"},
		data=form,
		files={"file": (filename, content, content_type)},
		timeout=TIMEOUT_SECONDS,
	)
	if resp.status_code != 200:
		detail = ""
		try:
			err = (resp.json() or {}).get("error") or {}
			detail = f" {err.get('type') or ''} {err.get('code') or ''} {str(err.get('message') or '')[:160]}"
		except Exception:
			pass
		raise RuntimeError(f"OpenAI returned HTTP {resp.status_code}{redact(detail)}")
	return str((resp.json() or {}).get("text") or "")


def _menu_names(settings: dict[str, Any]) -> list[str]:
	"""Item names of the menu, for the steering prompt. Never raises."""
	try:
		from jarz_pos.services import chat_drafts as drafts

		context = drafts.build_context(settings)
		return [str(n) for n in (context.get("item_names") or {}) if n]
	except Exception:
		return []


def _refresh_preview(conversation: str, message: str, timestamp: Any, text: str) -> None:
	"""Show the words instead of ``[voice note]`` in the inbox list, when this is the last message."""
	state = frappe.db.get_value(
		cc.CONVERSATION, conversation, ["last_message_preview", "last_message_at"], as_dict=True
	)
	if not state or state.last_message_preview != cc.preview("", "audio"):
		return
	if cc.dt_str(state.last_message_at) != cc.dt_str(timestamp):
		return
	frappe.db.set_value(
		cc.CONVERSATION,
		conversation,
		"last_message_preview",
		cc.preview("", "audio", transcript=text),
		update_modified=False,
	)


def transcribe(message_name: str) -> str | None:
	"""Transcribe a downloaded voice note into its ``transcript`` field. Never raises.

	Returns the text, or ``None`` when it was skipped or failed (the reason is
	logged without the key; the message stays a ``[voice note]`` for the AI).
	"""
	try:
		from jarz_pos.services.chat_settings import get_chat_settings

		settings = get_chat_settings()
		row = frappe.db.get_value(
			cc.MESSAGE,
			message_name,
			["name", "conversation", "message_type", "media_file", "media_mime", "transcript", "timestamp"],
			as_dict=True,
		)
		if not row or skip_reason(settings, row):
			return None
		file_doc = frappe.get_doc("File", {"file_url": row.media_file})
		size = file_doc.get("file_size")
		if skip_reason(settings, row, int(size) if size else None):
			return None
		content = file_doc.get_content()
		if not isinstance(content, bytes | bytearray) or not content:
			return None
		if len(content) > MAX_AUDIO_BYTES:
			return None
		filename = audio_filename(row.media_mime, file_doc.get("file_name"), row.name)
		model = str(settings.get("transcription_model") or "gpt-4o-transcribe")
		form = build_form(model, vocabulary_prompt(_menu_names(settings)))
		content_type = str(row.media_mime or "").split(";")[0].strip() or "application/octet-stream"
		text = clean_transcript(
			_post(str(settings["openai_api_key"]), filename, content_type, bytes(content), form)
		)
		if not text:
			return None
		frappe.db.set_value(cc.MESSAGE, row.name, "transcript", text, update_modified=False)
		_refresh_preview(row.conversation, row.name, row.timestamp, text)
		frappe.db.commit()
		cc.publish_chat_update(row.conversation, "message", message=cc.load_message(row.name))
		frappe.db.commit()
		return text
	except Exception as exc:
		try:
			frappe.db.rollback()
		except Exception:
			pass
		cc.safe_log_error(
			"chat: voice note transcription failed",
			f"message={message_name} {type(exc).__name__}: {redact(str(exc))[:300]}",
		)
		return None
