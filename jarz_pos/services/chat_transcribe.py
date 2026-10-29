"""Voice notes to text, so the chat AI can read what a customer said.

Two providers, picked by the ``transcription_provider`` setting:

* **OpenAI** ``gpt-4o-transcribe`` (best on Egyptian Arabic; takes WhatsApp's
  ogg/opus and Instagram's m4a as they are; 25 MB per file) through a plain
  ``requests`` multipart POST, no SDK.
* **Gemini** ``gemini-3.8-flash`` through ``generateContent`` with the audio
  inline (base64 in a JSON body, ``requests``). The key goes in the
  ``x-goog-api-key`` header, never in the URL.

One attempt, 60 s: a voice note that fails to transcribe is drafted blind, never
retried.

Everything that can be decided without the network is a pure function
(:func:`skip_reason`, :func:`audio_filename`, :func:`vocabulary_prompt`,
:func:`build_form`, :func:`build_gemini_request`, :func:`parse_gemini_response`)
and tested alone. :func:`transcribe` never raises and never logs an API key.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Iterable
from typing import Any
from urllib.parse import quote

import frappe

from jarz_pos.services import chat_common as cc

API_URL = "https://api.openai.com/v1/audio/transcriptions"
DEFAULT_MODEL = "gpt-4o-transcribe"
LANGUAGE = "ar"
TIMEOUT_SECONDS = 60
#: OpenAI's per-file limit.
MAX_AUDIO_BYTES = 25 * 1024 * 1024
#: The model is steered by this prompt; a long one costs time for nothing.
MAX_PROMPT_CHARS = 780
#: Stored and shown; a voice note longer than this is cut.
MAX_TRANSCRIPT_CHARS = 4000

PROVIDER_OPENAI = "OpenAI"
PROVIDER_GEMINI = "Gemini"

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GEMINI_DEFAULT_MODEL = "gemini-3.8-flash"
#: Inline audio rides in the JSON body, which Gemini caps at 20 MB; base64 adds a
#: third, so 14 MiB of audio is about 18.7 MiB on the wire. Bigger files are skipped.
GEMINI_MAX_AUDIO_BYTES = 14 * 1024 * 1024
GEMINI_MAX_OUTPUT_TOKENS = 1024
#: Audio types Gemini takes inline, by stored mime type (parameters already removed).
_GEMINI_MIME = {
	"audio/ogg": "audio/ogg",
	"audio/opus": "audio/ogg",
	"application/ogg": "audio/ogg",
	"audio/mpeg": "audio/mp3",
	"audio/mp3": "audio/mp3",
	"audio/mp4": "audio/mp4",
	"audio/x-m4a": "audio/mp4",
	"audio/m4a": "audio/mp4",
	"video/mp4": "audio/mp4",
	"audio/aac": "audio/aac",
	"audio/x-aac": "audio/aac",
	"audio/wav": "audio/wav",
	"audio/x-wav": "audio/wav",
	"audio/wave": "audio/wav",
	"audio/flac": "audio/flac",
	"audio/x-flac": "audio/flac",
}
#: The same, by file extension, for a stored mime type that is missing or odd.
_GEMINI_EXTENSION_MIME = {
	"ogg": "audio/ogg",
	"opus": "audio/ogg",
	"oga": "audio/ogg",
	"mp3": "audio/mp3",
	"mpeg": "audio/mp3",
	"mpga": "audio/mp3",
	"m4a": "audio/mp4",
	"mp4": "audio/mp4",
	"aac": "audio/aac",
	"wav": "audio/wav",
	"flac": "audio/flac",
}
_GEMINI_INSTRUCTION = (
	"Transcribe this Egyptian Arabic customer voice note verbatim, in the original language and "
	"script: Arabic script for Arabic, Latin letters for English words exactly as spoken. Do not "
	"translate, summarise or add commentary. Output only the transcript. If there is no speech, "
	"output nothing."
)

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
_GOOGLE_KEY_LIKE = re.compile(r"AIza[A-Za-z0-9_\-]{8,}")


# ── pure ────────────────────────────────────────────────────────────────


def redact(text: Any, *secrets: Any) -> str:
	"""*text* with anything that looks like an API key removed. Pure.

	OpenAI (``sk-``) and Google (``AIza``) key shapes are cut by pattern; every
	value in *secrets* (the live keys) is cut literally, whatever it looks like.
	"""
	out = str(text or "")
	for secret in secrets:
		value = str(secret or "").strip()
		if len(value) >= 8:
			out = out.replace(value, "[key]")
	out = _KEY_LIKE.sub("sk-...", out)
	return _GOOGLE_KEY_LIKE.sub("AIza...", out)


def provider_of(settings: dict[str, Any]) -> str:
	"""``"OpenAI"`` or ``"Gemini"``: the chosen provider, OpenAI when unset or unknown. Pure."""
	chosen = str(settings.get("transcription_provider") or "").strip().lower()
	return PROVIDER_GEMINI if chosen == PROVIDER_GEMINI.lower() else PROVIDER_OPENAI


def model_of(settings: dict[str, Any]) -> str:
	"""The model to call: the setting, or the chosen provider's default when empty. Pure."""
	model = str(settings.get("transcription_model") or "").strip()
	if model:
		return model
	return GEMINI_DEFAULT_MODEL if provider_of(settings) == PROVIDER_GEMINI else DEFAULT_MODEL


def skip_reason(settings: dict[str, Any], row: dict[str, Any], size: int | None = None) -> str | None:
	"""Why this message is not transcribed, or ``None`` when it should be. Pure."""
	if not settings.get("transcribe_voice_notes"):
		return "transcription is off"
	gemini = provider_of(settings) == PROVIDER_GEMINI
	if not str(settings.get("gemini_api_key" if gemini else "openai_api_key") or "").strip():
		return "no Gemini key" if gemini else "no OpenAI key"
	if str(row.get("message_type") or "") != "audio":
		return "not a voice note"
	if not str(row.get("media_file") or "").strip():
		return "no file"
	if str(row.get("transcript") or "").strip():
		return "already transcribed"
	if gemini and size is not None and size > GEMINI_MAX_AUDIO_BYTES:
		return "file is larger than 14 MB (Gemini inline limit)"
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


def gemini_mime(mime: Any, file_name: Any = None) -> str | None:
	"""The audio mime type to send Gemini, or ``None`` when it takes none like this. Pure.

	The stored mime type wins (parameters such as ``codecs=opus`` dropped), then the
	file extension. ``audio/opus`` goes as ``audio/ogg``.
	"""
	clean = str(mime or "").split(";")[0].strip().lower()
	if clean in _GEMINI_MIME:
		return _GEMINI_MIME[clean]
	name = str(file_name or "")
	ext = name.rsplit(".", 1)[-1].strip().lower() if "." in name else ""
	return _GEMINI_EXTENSION_MIME.get(ext)


def gemini_instruction(vocabulary: str = "") -> str:
	"""The text part of the Gemini request: what to do, then the menu vocabulary hint. Pure."""
	hint = " ".join(str(vocabulary or "").split())
	if not hint:
		return _GEMINI_INSTRUCTION
	return f"{_GEMINI_INSTRUCTION}\n\nBackground, only to spell names right (never output it): {hint}"


def build_gemini_request(
	model: str, mime: str, content: bytes, instruction: str
) -> tuple[str, dict[str, Any]]:
	"""The URL and JSON body of the Gemini call. The key is in neither; it is a header. Pure."""
	name = str(model or "").strip()
	name = name[len("models/") :] if name.startswith("models/") else name
	url = GEMINI_URL.format(model=quote(name or GEMINI_DEFAULT_MODEL, safe=""))
	body = {
		"contents": [
			{
				"role": "user",
				"parts": [
					{"text": instruction},
					{"inline_data": {"mime_type": mime, "data": base64.b64encode(content).decode("ascii")}},
				],
			}
		],
		"generationConfig": {"temperature": 0, "maxOutputTokens": GEMINI_MAX_OUTPUT_TOKENS},
	}
	return url, body


def parse_gemini_response(data: Any) -> tuple[str, str | None]:
	"""``(text, problem)`` from a Gemini answer. Pure.

	*text* is the first candidate's text parts joined (thought parts left out).
	*problem* says why there is none when the request was blocked or the answer was
	malformed; it is ``None`` when the text is there, or when the model heard no
	speech and rightly said nothing.
	"""
	if not isinstance(data, dict):
		return "", "response is not a JSON object"
	candidates = data.get("candidates") or []
	candidate = candidates[0] if candidates and isinstance(candidates[0], dict) else None
	if candidate is None:
		feedback = data.get("promptFeedback")
		block = feedback.get("blockReason") if isinstance(feedback, dict) else None
		return "", f"blocked: {block}" if block else "no candidates"
	content = candidate.get("content") if isinstance(candidate.get("content"), dict) else {}
	parts = content.get("parts") or []
	text = "".join(
		str(part.get("text") or "") for part in parts if isinstance(part, dict) and not part.get("thought")
	)
	if text.strip():
		return text, None
	finish = str(candidate.get("finishReason") or "")
	if finish and finish not in ("STOP", "MAX_TOKENS"):
		return "", f"blocked: {finish}"
	return "", None


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
		raise RuntimeError(f"OpenAI returned HTTP {resp.status_code}{redact(detail, api_key)}")
	return str((resp.json() or {}).get("text") or "")


def _post_gemini(api_key: str, model: str, mime: str, content: bytes, instruction: str) -> str:
	"""One Gemini ``generateContent`` call. Returns the transcript text; raises on any failure.

	A blocked or malformed answer is logged (without the key) and returns ``""``.
	"""
	import requests

	url, body = build_gemini_request(model, mime, content, instruction)
	resp = requests.post(
		url,
		headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
		json=body,
		timeout=TIMEOUT_SECONDS,
	)
	if resp.status_code != 200:
		detail = ""
		try:
			err = (resp.json() or {}).get("error") or {}
			detail = f" {err.get('status') or ''} {str(err.get('message') or '')[:160]}"
		except Exception:
			pass
		raise RuntimeError(f"Gemini returned HTTP {resp.status_code}{redact(detail, api_key)}")
	text, problem = parse_gemini_response(resp.json())
	if problem:
		cc.safe_log_error("chat: voice note transcription got no text", redact(f"Gemini {problem}", api_key))
	return text


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
	secrets: list[str] = []
	try:
		from jarz_pos.services.chat_settings import get_chat_settings

		settings = get_chat_settings()
		secrets = [str(settings.get("openai_api_key") or ""), str(settings.get("gemini_api_key") or "")]
		gemini = provider_of(settings) == PROVIDER_GEMINI
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
		if len(content) > (GEMINI_MAX_AUDIO_BYTES if gemini else MAX_AUDIO_BYTES):
			return None
		prompt = vocabulary_prompt(_menu_names(settings))
		if gemini:
			mime = gemini_mime(row.media_mime, file_doc.get("file_name"))
			if not mime:
				cc.safe_log_error(
					"chat: voice note transcription skipped",
					f"message={message_name} Gemini takes no audio of type {redact(row.media_mime)[:60]!r}",
				)
				return None
			raw = _post_gemini(
				str(settings["gemini_api_key"]),
				model_of(settings),
				mime,
				bytes(content),
				gemini_instruction(prompt),
			)
		else:
			filename = audio_filename(row.media_mime, file_doc.get("file_name"), row.name)
			form = build_form(model_of(settings), prompt)
			content_type = str(row.media_mime or "").split(";")[0].strip() or "application/octet-stream"
			raw = _post(str(settings["openai_api_key"]), filename, content_type, bytes(content), form)
		text = clean_transcript(raw)
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
			f"message={message_name} {type(exc).__name__}: {redact(str(exc), *secrets)[:300]}",
		)
		return None
