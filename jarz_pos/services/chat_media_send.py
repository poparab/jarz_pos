"""Staff send a photo / video / voice note / file, and react to a customer's message.

Feature ``chat-media-send``. Only ever called from a human action
(``api/chat.send_media`` / ``react_to_message``).

Media
-----
The file is saved as a **private** File attached to the conversation, the send route
is decided exactly like text (:func:`chat_send.decide_send_route`: WhatsApp 24 h
window, Messenger / Instagram 24 h), the Outbound row is written ``queued`` and
committed before the Graph call, then flipped to ``sent`` / ``failed``.

* **WhatsApp**: upload to ``/<phone_number_id>/media``, then a media message by id.
* **Messenger**: one multipart ``/me/messages`` call with ``filedata``. The Page's echo
  webhook carries the same mid, which is stored as the row's ``external_message_id``,
  so ``chat_ingest._message_exists`` drops the echo.
* **Instagram**: URL attachments only (no upload). The private file is exposed for an
  hour through a random token (:func:`media_link_url`) served by the guest endpoint
  ``api.chat.media_link``. Documents are refused. UNCERTAIN: whether Meta also accepts
  an uploaded file for Instagram; flip :data:`IG_USES_URL_ATTACHMENT` when it does.
* Messenger / Instagram attachments cannot carry a caption, so a caption goes out as a
  separate text message (its own Outbound row) right after the file.

Reactions
---------
WhatsApp takes any emoji. Instagram only ``love``. Messenger support is UNCERTAIN, see
:data:`MESSENGER_REACTIONS_ENABLED`.
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import secrets
from typing import Any

import frappe
from frappe import _

from jarz_pos.services import chat_common as cc
from jarz_pos.services import chat_send as cs

MB = 1024 * 1024

#: Server-side caps by media type (contract). A channel may only lower them.
MEDIA_CAPS = {"image": 5 * MB, "video": 16 * MB, "audio": 16 * MB, "document": 25 * MB}
#: Per-channel ceilings (Messenger 25 MB for everything; Instagram image 8, video / audio 25).
CHANNEL_CAPS = {
	cc.WHATSAPP: {"image": 5 * MB, "video": 16 * MB, "audio": 16 * MB, "document": 100 * MB},
	cc.MESSENGER: {"image": 25 * MB, "video": 25 * MB, "audio": 25 * MB, "document": 25 * MB},
	cc.INSTAGRAM: {"image": 8 * MB, "video": 25 * MB, "audio": 25 * MB},
}

#: MIME types WhatsApp Cloud API accepts per media type. Documents: anything.
WA_ALLOWED = {
	"image": ("image/jpeg", "image/png"),
	"video": ("video/mp4", "video/3gpp"),
	"audio": ("audio/aac", "audio/amr", "audio/mpeg", "audio/mp4", "audio/ogg"),
}
#: Instagram DM attachment types. UNCERTAIN list: widen here if Meta accepts more.
IG_ALLOWED = {
	"image": ("image/jpeg", "image/png", "image/gif"),
	"video": ("video/mp4", "video/quicktime", "video/webm", "video/ogg"),
	"audio": ("audio/aac", "audio/mp4", "audio/mpeg", "audio/wav", "audio/ogg"),
}

WA_CAPTION_MAX = 1024

#: UNCERTAIN: Meta's docs do not clearly list Instagram DM file upload; URL attachments
#: are documented. True = send a public link (needs ``media_link``).
IG_USES_URL_ATTACHMENT = True

#: UNCERTAIN: Messenger ``sender_action: react``. Off until verified against the real Page.
MESSENGER_REACTIONS_ENABLED = False

WA_REACTIONS = ["❤️", "\U0001f44d", "\U0001f602", "\U0001f62e", "\U0001f622", "\U0001f64f"]
HEART = "❤️"
#: Messenger / Instagram name the reaction, they do not take the emoji.
DM_REACTION_NAMES = {HEART: "love"}

MEDIA_LINK_SECONDS = 3600
MEDIA_LINK_PREFIX = "jarz_chat_media_link:"
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{20,80}$")

_MIME_ALIASES = {
	"audio/mp3": "audio/mpeg",
	"audio/x-m4a": "audio/mp4",
	"audio/m4a": "audio/mp4",
	"audio/x-aac": "audio/aac",
	"video/x-m4v": "video/mp4",
	"image/jpg": "image/jpeg",
	"image/pjpeg": "image/jpeg",
}
_EXT_MIMES = {
	".m4a": "audio/mp4",
	".aac": "audio/aac",
	".amr": "audio/amr",
	".ogg": "audio/ogg",
	".opus": "audio/ogg",
	".mp3": "audio/mpeg",
	".3gp": "video/3gpp",
	".mp4": "video/mp4",
	".mov": "video/quicktime",
	".jpg": "image/jpeg",
	".jpeg": "image/jpeg",
	".png": "image/png",
	".pdf": "application/pdf",
}


# ── pure: type, caps, choices ───────────────────────────────────────────


def normalize_mime(mime: Any, filename: str = "") -> str:
	"""Lower-case MIME without parameters; falls back to the file extension."""
	clean = str(mime or "").split(";")[0].strip().lower()
	ext = os.path.splitext(str(filename or ""))[1].lower()
	if not clean or clean == "application/octet-stream":
		clean = _EXT_MIMES.get(ext) or mimetypes.guess_type(f"x{ext}")[0] or clean or ""
	return _MIME_ALIASES.get(clean, clean)


def media_type_for(mime: str) -> str:
	"""``image|video|audio|document`` for a normalized MIME."""
	family = mime.split("/", 1)[0]
	return family if family in ("image", "video", "audio") else "document"


def max_bytes(channel: str, media_type: str) -> int:
	return min(MEDIA_CAPS[media_type], CHANNEL_CAPS.get(channel, {}).get(media_type, 0))


def can_send_media(channel: str) -> bool:
	return channel in CHANNEL_CAPS


def reaction_choices(channel: str) -> list[str]:
	if channel == cc.WHATSAPP:
		return list(WA_REACTIONS)
	if channel == cc.INSTAGRAM:
		return [HEART]
	if channel == cc.MESSENGER and MESSENGER_REACTIONS_ENABLED:
		return [HEART]
	return []


def _plain(emoji: str) -> str:
	return str(emoji or "").replace("️", "").strip()


def canonical_reaction(channel: str, emoji: Any) -> str | None:
	"""The channel's own spelling of *emoji*, ``""`` for removal, ``None`` when not allowed."""
	clean = str(emoji or "").strip()
	if not clean:
		return ""
	for choice in reaction_choices(channel):
		if _plain(choice) == _plain(clean):
			return choice
	return None


def _mb(size: int) -> str:
	return f"{size / MB:g} MB"


def validate_media(channel: str, filename: str, mime: Any, size: int) -> tuple[str, str]:
	"""``(media_type, mime)`` or a readable ``frappe.throw``. Pure: no database."""
	if channel in cc.COMMENT_CHANNELS or channel not in CHANNEL_CAPS:
		frappe.throw(_("Photos and files cannot be sent on a comment thread. Reply with text."))
	norm = normalize_mime(mime, filename)
	media_type = media_type_for(norm)
	if not size:
		frappe.throw(_("The file is empty."))
	if channel == cc.INSTAGRAM and media_type == "document":
		frappe.throw(_("Instagram does not accept files. Send a photo, video or voice note."))
	allowed = None
	if channel == cc.WHATSAPP:
		allowed = WA_ALLOWED.get(media_type)
	elif channel == cc.INSTAGRAM:
		allowed = IG_ALLOWED.get(media_type)
	if allowed is not None and norm not in allowed:
		frappe.throw(
			_("This file type ({0}) is not accepted on {1}. Allowed: {2}.").format(
				norm or "?", channel, ", ".join(allowed)
			)
		)
	limit = max_bytes(channel, media_type)
	if size > limit:
		frappe.throw(
			_("The file is too big: {0}. The limit for this type on {1} is {2}.").format(
				_mb(size), channel, _mb(limit)
			)
		)
	return media_type, norm


def safe_filename(filename: Any, mime: str) -> str:
	name = os.path.basename(str(filename or "").replace("\\", "/")).strip()
	name = re.sub(r"[^\w.\- ()؀-ۿ]", "_", name)[:120].strip(" .")
	if not name:
		name = "file"
	if not os.path.splitext(name)[1]:
		name += mimetypes.guess_extension(mime) or ""
	return name


def _attachment_type(media_type: str) -> str:
	"""Messenger's attachment type word for our media type."""
	return "file" if media_type == "document" else media_type


def stored_body(media_type: str, filename: str, caption: str) -> str:
	"""Audio carries no caption; a document body is ``filename`` then the caption."""
	if media_type == "audio":
		return ""
	if media_type == "document":
		return f"{filename}\n{caption}" if caption else filename
	return caption


# ── public link for Instagram ───────────────────────────────────────────


def media_link_url(file_url: str, mime: str, filename: str) -> str:
	"""A random, one-hour public URL for a private conversation file."""
	token = secrets.token_urlsafe(24)
	cache = frappe.cache()
	cache.set(
		cache.make_key(MEDIA_LINK_PREFIX + token),
		json.dumps({"file_url": file_url, "mime": mime, "filename": filename}),
		ex=MEDIA_LINK_SECONDS,
	)
	from frappe.utils import get_url

	return f"{get_url().rstrip('/')}/api/method/jarz_pos.api.chat.media_link?token={token}"


def resolve_media_token(token: Any) -> tuple[bytes, str, str] | None:
	"""``(content, mime, filename)`` for a live token, else ``None``. Never raises."""
	token = str(token or "")
	if not _TOKEN_RE.match(token):
		return None
	try:
		cache = frappe.cache()
		raw = cache.get(cache.make_key(MEDIA_LINK_PREFIX + token))
		if not raw:
			return None
		info = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
		name = frappe.db.get_value(
			"File",
			{"file_url": info.get("file_url"), "attached_to_doctype": cc.CONVERSATION},
			"name",
		)
		if not name:
			return None
		content = frappe.get_doc("File", name).get_content()
		if isinstance(content, str):
			content = content.encode()
		mime = str(info.get("mime") or "application/octet-stream")
		return content, mime, str(info.get("filename") or "file")
	except Exception:
		return None


# ── send media ──────────────────────────────────────────────────────────


def _call_meta(
	conv: Any,
	route: dict[str, Any],
	media_type: str,
	content: bytes,
	mime: str,
	filename: str,
	caption: str,
	file_url: str,
) -> str:
	from jarz_pos.services import chat_meta_client as meta

	if conv.channel == cc.WHATSAPP:
		media_id = meta.wa_upload_media(content, filename, mime)
		return meta.wa_send_media(
			conv.external_id,
			media_type,
			media_id,
			caption if media_type != "audio" else None,
			filename if media_type == "document" else None,
		)
	if conv.channel == cc.MESSENGER:
		return meta.messenger_send_attachment(
			conv.external_id, _attachment_type(media_type), content, filename, mime, tag=route["tag"]
		)
	url = media_link_url(file_url, mime, filename)
	return meta.ig_send_attachment_url(conv.external_id, media_type, url, tag=route["tag"])


def send_media(
	conversation: str, user: str, filename: str, content: bytes, mime: Any, caption: Any = ""
) -> dict[str, Any]:
	"""Send one file on *conversation* as *user*. Returns the Message shape."""
	conv = frappe.db.get_value(
		cc.CONVERSATION,
		conversation,
		[
			"name",
			"channel",
			"external_id",
			"status",
			"assigned_to",
			"window_expires_at",
			"last_inbound_at",
			"private_reply_sent",
		],
		as_dict=True,
	)
	if not conv:
		frappe.throw(_("Conversation {0} not found.").format(conversation), frappe.DoesNotExistError)

	media_type, norm = validate_media(conv.channel, filename, mime, len(content or b""))
	clean_name = safe_filename(filename, norm)
	text = str(caption or "").strip()
	if media_type == "audio":
		text = ""
	if conv.channel == cc.WHATSAPP and len(text) > WA_CAPTION_MAX:
		frappe.throw(
			_("The caption is too long: {0} characters, the limit is {1}.").format(len(text), WA_CAPTION_MAX)
		)
	if text:
		cs.validate_text(conv.channel, text)

	at = cc.now()
	route = cs.decide_send_route(
		conv.channel, at, conv.window_expires_at, conv.last_inbound_at, conv.private_reply_sent
	)
	if not route["ok"]:
		frappe.throw(cs._route_error(route["error"]))

	file_doc = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": clean_name,
			"content": content,
			"is_private": 1,
			"attached_to_doctype": cc.CONVERSATION,
			"attached_to_name": conv.name,
		}
	)
	file_doc.insert(ignore_permissions=True)
	file_url = file_doc.file_url

	# Messenger / Instagram cannot caption an attachment: the caption follows as its own text.
	separate_caption = conv.channel in (cc.MESSENGER, cc.INSTAGRAM) and bool(text)
	body = stored_body(media_type, clean_name, "" if separate_caption else text)
	msg = frappe.get_doc(
		{
			"doctype": cc.MESSAGE,
			"conversation": conv.name,
			"direction": "Outbound",
			"message_type": media_type,
			"body": body,
			"media_file": file_url,
			"media_mime": norm,
			"sent_via": "App",
			"sent_by": user,
			"status": "queued",
			"timestamp": at,
		}
	)
	msg.insert(ignore_permissions=True)
	frappe.db.commit()

	try:
		external_id = _call_meta(conv, route, media_type, content, norm, clean_name, text, file_url)
	except Exception as exc:
		error = cs.friendly_send_error(exc, route["route"])[:500]
		frappe.db.set_value(cc.MESSAGE, msg.name, {"status": "failed", "error": error}, update_modified=True)
		frappe.db.commit()
		cc.publish_chat_update(conv.name, "message", message=cc.load_message(msg.name))
		frappe.db.commit()
		frappe.throw(_("Sending failed: {0}").format(error))

	frappe.db.set_value(
		cc.MESSAGE, msg.name, {"status": "sent", "external_message_id": external_id}, update_modified=True
	)
	updates: dict[str, Any] = {
		"status": "Answered",
		"waiting_since": None,
		"alert_sent_at": None,
		"alert_count": 0,
		"unread_count": 0,
		"last_outbound_at": at,
		"last_message_at": at,
		"last_message_preview": cc.preview(body, media_type),
		"last_direction": "Outbound",
	}
	if not conv.assigned_to:
		updates["assigned_to"] = user
	frappe.db.set_value(cc.CONVERSATION, conv.name, updates, update_modified=True)
	cc.supersede_live_drafts(conv.name)

	message = cc.load_message(msg.name)
	cc.publish_chat_update(conv.name, "message", message=message)
	if separate_caption:
		_send_caption_text(conv.name, text, user)
	return message


def _send_caption_text(conversation: str, text: str, user: str) -> None:
	"""The caption of a Messenger / Instagram file. The file is already sent, so a failure
	here is kept as its own ``failed`` text row (``send_text`` records it) and not raised."""
	try:
		cs.send_text(conversation, text, user)
	except Exception:
		pass


# ── reactions ───────────────────────────────────────────────────────────


def react(message_name: str, emoji: Any, user: str) -> dict[str, Any]:
	"""Put (or, with ``""``, remove) our reaction on a customer's message."""
	row = frappe.db.get_value(
		cc.MESSAGE,
		message_name,
		["name", "conversation", "direction", "external_message_id"],
		as_dict=True,
	)
	if not row:
		frappe.throw(_("Message {0} not found.").format(message_name), frappe.DoesNotExistError)
	if row.direction != "Inbound" or not row.external_message_id:
		frappe.throw(_("Only the customer's messages can be reacted to."))
	conv = frappe.db.get_value(
		cc.CONVERSATION,
		row.conversation,
		["name", "channel", "external_id", "window_expires_at", "last_inbound_at", "private_reply_sent"],
		as_dict=True,
	)
	if not conv:
		frappe.throw(_("Conversation {0} not found.").format(row.conversation), frappe.DoesNotExistError)
	if not reaction_choices(conv.channel):
		frappe.throw(_("Reactions are not available on {0}.").format(conv.channel))
	chosen = canonical_reaction(conv.channel, emoji)
	if chosen is None:
		frappe.throw(
			_("This emoji is not allowed on {0}. Allowed: {1}.").format(
				conv.channel, " ".join(reaction_choices(conv.channel))
			)
		)
	if not cc.has_reaction_columns():
		frappe.throw(_("Reactions are not available until the server update finishes. Try again soon."))
	route = cs.decide_send_route(
		conv.channel, cc.now(), conv.window_expires_at, conv.last_inbound_at, conv.private_reply_sent
	)
	if not route["ok"]:
		frappe.throw(cs._route_error(route["error"]))

	from jarz_pos.services import chat_meta_client as meta

	try:
		if conv.channel == cc.WHATSAPP:
			meta.wa_send_reaction(conv.external_id, row.external_message_id, chosen)
		else:
			name = DM_REACTION_NAMES.get(chosen) if chosen else None
			meta.dm_send_reaction(
				conv.external_id, row.external_message_id, name, instagram=conv.channel == cc.INSTAGRAM
			)
	except Exception as exc:
		frappe.throw(_("Reaction failed: {0}").format(cs.friendly_send_error(exc, route["route"])[:300]))

	frappe.db.set_value(cc.MESSAGE, row.name, "staff_reaction", chosen or None, update_modified=True)
	message = cc.load_message(row.name)
	cc.publish_chat_update(conv.name, "message", message=message)
	return {"message": message}
