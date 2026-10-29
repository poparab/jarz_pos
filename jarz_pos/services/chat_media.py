"""Customer photos for the chat AI, and the job order around media messages.

Two things live here because both AI calls that read a chat (the reply draft and
Start order) and the ingestion queue need the same answers:

* :func:`image_blocks` picks the newest inbound photos of a transcript window,
  reads them and returns Claude image content blocks, each behind a short text
  label that ties it to its line of the transcript. It never raises: a photo it
  cannot read simply stays a ``[image]`` line.
* :func:`plan_jobs` decides, for one stored inbound message, which background
  jobs ingestion enqueues. A photo or voice note is not drafted until its file
  is downloaded (and a voice note transcribed), so the AI never races the
  download and never drafts the same message twice.
"""

from __future__ import annotations

import base64
import mimetypes
from collections.abc import Callable, Iterable
from typing import Any

import frappe

from jarz_pos.services import chat_common as cc

#: What Claude accepts as an image.
IMAGE_MIMES = ("image/jpeg", "image/png", "image/gif", "image/webp")
#: The AI sees at most this many photos per call.
MAX_IMAGES = 3
#: Per photo, as stored. Base64 adds a third, and the API limit is 5 MB per image.
MAX_IMAGE_BYTES = int(3.5 * 1024 * 1024)
#: Unreadable photos tried before giving up (bounds the file reads of one call).
MAX_READ_ATTEMPTS = 6

#: Message types whose AI draft waits for the downloaded file.
WAIT_FOR_MEDIA = ("image", "audio")

_FILE_PREFIXES = ("/private/files/", "/files/")


# ── pure ────────────────────────────────────────────────────────────────


def _clean_mime(value: Any) -> str:
	mime = str(value or "").split(";")[0].strip().lower()
	return "image/jpeg" if mime == "image/jpg" else mime


def sniff_mime(data: bytes | None) -> str | None:
	"""The image type from the file's first bytes, or ``None``. Pure.

	The stored ``media_mime`` comes from the channel and can be wrong or empty; the
	API refuses an image whose declared type differs from its data.
	"""
	head = bytes(data or b"")[:16]
	if head.startswith(b"\xff\xd8\xff"):
		return "image/jpeg"
	if head.startswith(b"\x89PNG\r\n\x1a\n"):
		return "image/png"
	if head[:6] in (b"GIF87a", b"GIF89a"):
		return "image/gif"
	if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
		return "image/webp"
	return None


def photo_candidates(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
	"""Inbound photo rows that could be shown to the model, newest first. Pure.

	Only ``image`` messages from the customer (a sticker is its own type) whose
	file is stored and is not obviously another media type.
	"""
	found = []
	for row in rows:
		if row.get("direction") != "Inbound" or str(row.get("message_type") or "") != "image":
			continue
		url = str(row.get("media_file") or "")
		if not url.startswith(_FILE_PREFIXES):
			continue
		mime = _clean_mime(row.get("media_mime")) or _clean_mime(mimetypes.guess_type(url)[0])
		if mime and mime not in IMAGE_MIMES:
			continue
		found.append(row)
	found.reverse()
	return found


def photo_label(row: dict[str, Any], number: int | None = None) -> str:
	"""The text before a photo: which line of the conversation it is. Pure."""
	when = cc.to_dt(row.get("timestamp"))
	at = when.strftime("[%a %d %b %H:%M]") if when else ""
	if number is not None:
		return f"Photo from message [{number}] {at}".strip()
	return f"Photo from the customer's message at {at}".strip() if at else "Photo from the customer"


def image_block(data: bytes, mime: str) -> dict[str, Any]:
	return {
		"type": "image",
		"source": {"type": "base64", "media_type": mime, "data": base64.b64encode(data).decode("ascii")},
	}


def user_content(user_text: str, blocks: list[dict[str, Any]]) -> str | list[dict[str, Any]]:
	"""The user turn: the plain text, or the photos (labelled) followed by the text. Pure.

	Photos come first because the model reads an image better before the question
	about it; the cached system context stays untouched either way.
	"""
	if not blocks:
		return user_text
	return [*blocks, {"type": "text", "text": user_text}]


def plan_jobs(action: dict[str, Any]) -> dict[str, bool]:
	"""Which jobs ingestion enqueues for one stored message. Pure.

	``action`` is what ``_persist_message`` returned (``media``, ``draft``,
	``message_type``, ``push``). A photo or voice note that has a file to download
	is drafted by the download job once the file (and, for audio, the
	transcript) is in; every other message drafts at once, as before. Only
	``push`` messages (live inbound) are transcribed, never imported history.
	"""
	media = bool(action.get("media"))
	draft = bool(action.get("draft"))
	mtype = str(action.get("message_type") or "")
	after_download = media and draft and mtype in WAIT_FOR_MEDIA
	return {
		"download": media,
		"draft_now": draft and not after_download,
		"draft_after_download": after_download,
		"transcribe": media and mtype == "audio" and bool(action.get("push")),
	}


# ── reading the files ───────────────────────────────────────────────────


def read_image(file_url: str) -> bytes | None:
	"""The stored bytes of a photo, or ``None`` (missing, too big, unreadable). Never raises."""
	try:
		doc = frappe.get_doc("File", {"file_url": file_url})
		size = doc.get("file_size")
		if size and int(size) > MAX_IMAGE_BYTES:
			return None
		data = doc.get_content()
		if not isinstance(data, bytes | bytearray) or len(data) > MAX_IMAGE_BYTES:
			return None
		return bytes(data)
	except Exception:
		return None


def image_blocks(
	rows: Iterable[dict[str, Any]],
	numbers: dict[Any, int] | None = None,
	read: Callable[[str], bytes | None] = read_image,
) -> list[dict[str, Any]]:
	"""Label + image content blocks for the newest photos of *rows* (oldest first). Never raises.

	At most :data:`MAX_IMAGES`, chosen newest first and returned in chat order.
	*numbers* maps a row's ``name`` to its ``[n]`` in the numbered transcript
	(Start order); without it the label gives the time. A photo that cannot be
	read, is too big or is not really an image is skipped and the next older one
	takes its place.
	"""
	chosen: list[tuple[dict[str, Any], bytes, str]] = []
	try:
		attempts = 0
		for row in photo_candidates(rows):
			if len(chosen) >= MAX_IMAGES or attempts >= MAX_READ_ATTEMPTS:
				break
			attempts += 1
			data = read(str(row.get("media_file") or ""))
			if not data or len(data) > MAX_IMAGE_BYTES:
				continue
			mime = sniff_mime(data)
			if mime:
				chosen.append((row, data, mime))
		blocks: list[dict[str, Any]] = []
		for row, data, mime in reversed(chosen):
			number = (numbers or {}).get(row.get("name"))
			blocks.append({"type": "text", "text": photo_label(row, number)})
			blocks.append(image_block(data, mime))
		return blocks
	except Exception:
		return []
