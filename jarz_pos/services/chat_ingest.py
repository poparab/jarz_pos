"""Meta webhook ingestion for the chat inbox.

Two halves, kept apart on purpose:

* **Pure** (no database): :func:`compute_signature`, :func:`verify_signature`,
  :func:`verify_subscription`, :func:`epoch_to_site` and :func:`parse_webhook`,
  which turns one raw Meta payload into a flat list of normalised events. The
  tests drive all of these with plain dicts.
* **Persist**: :func:`process_webhook` (the background job the guest endpoint
  enqueues) upserts conversations, inserts messages idempotently on
  ``external_message_id``, keeps the denormalised conversation fields current,
  and after the commit publishes realtime, enqueues media downloads and drafts.

``process_webhook`` never raises: Meta retries a webhook that errors, and a
retry storm of one malformed payload must not block the queue.

Nothing here logs the payload. A webhook body carries customer phone numbers
and message text, so the Error Log only ever gets counts and masked ids.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import json
import mimetypes
import re
from typing import Any

import frappe

from jarz_pos.services import chat_common as cc

#: Bodies above this are refused before hashing. A WhatsApp ``history`` chunk
#: (up to 180 days of chats at onboarding) is the largest payload Meta sends.
MAX_BODY_BYTES = 8 * 1024 * 1024
#: Contact names from ``smb_app_state_sync`` wait this long for their chat to arrive.
CONTACT_NAME_TTL_SECONDS = 7 * 24 * 3600
#: A Messenger / Instagram name lookup that found nothing is not retried for this long.
NAME_LOOKUP_RETRY_SECONDS = 6 * 3600
HISTORY_PROGRESS_KEY = "jarz_chat_history_progress"
#: A WhatsApp reply typed in the Meta Business Suite inbox reaches us only as status
#: updates (sent / delivered / read) for a message id we never stored: no echo, no text.
#: The first such status leaves this placeholder so the chat shows as answered.
STATUS_ONLY_BODY = (
	"Reply sent from Meta Business Suite (Meta does not share its text)\n"
	"تم الرد من Meta Business Suite (ميتا لا ترسل نص الرد)"
)
#: ``raw_json`` of a placeholder, so a later echo with the real content can fill it in.
STATUS_ONLY_MARKER = '{"source": "status_only"}'
#: A "read" can come hours after the reply, so only these open a placeholder.
_STATUS_ONLY_STATES = ("sent", "delivered")
#: Templates (marketing / utility / authentication) are automated sends, not staff replies.
_STATUS_ONLY_CATEGORIES = ("", "service")
#: Echo heuristic: an app send and its echo within this many seconds are one message.
ECHO_MATCH_SECONDS = 120
#: Meta's customer-service window (WhatsApp / Messenger / Instagram DMs).
DM_WINDOW_HOURS = 24
#: Private-reply window for a public comment.
COMMENT_WINDOW_DAYS = 7
RAW_JSON_MAX = 20000
_UTC = _dt.timezone.utc

_STATUS_RANK = {"queued": 0, "received": 0, "sent": 1, "delivered": 2, "read": 3}

_WA_MEDIA_TYPES = ("image", "audio", "video", "document", "sticker")
_ATTACHMENT_TYPES = {
	"image": "image",
	"video": "video",
	"audio": "audio",
	"file": "document",
	"sticker": "sticker",
}


# ── pure: signatures and subscription ───────────────────────────────────


def compute_signature(body: bytes, secret: str) -> str:
	"""``"sha256=<hex>"`` exactly as Meta sends it in ``X-Hub-Signature-256``."""
	if isinstance(body, str):
		body = body.encode("utf-8")
	digest = hmac.new(str(secret).encode("utf-8"), body or b"", hashlib.sha256).hexdigest()
	return f"sha256={digest}"


def verify_signature(body: bytes, header: str | None, secret: str | None) -> bool:
	"""Constant-time check of Meta's HMAC. A missing secret or header is a failure."""
	if not secret or not header:
		return False
	header = str(header).strip()
	if not header.startswith("sha256="):
		return False
	expected = compute_signature(body, secret)
	return hmac.compare_digest(expected.encode("utf-8"), header.encode("utf-8"))


def verify_subscription(args: dict[str, Any], verify_token: str | None) -> str | None:
	"""The ``hub.challenge`` to echo back, or ``None`` when the handshake is invalid."""
	if not verify_token:
		return None
	mode = str(args.get("hub.mode") or "")
	token = str(args.get("hub.verify_token") or "")
	challenge = args.get("hub.challenge")
	if mode != "subscribe" or challenge in (None, ""):
		return None
	if not hmac.compare_digest(token.encode("utf-8"), str(verify_token).encode("utf-8")):
		return None
	return str(challenge)


def mask_id(value: Any) -> str:
	"""``"2010****4268"`` style mask for ids that may be phone numbers."""
	text = str(value or "")
	if len(text) <= 6:
		return "***"
	return f"{text[:4]}****{text[-4:]}"


# ── pure: time ──────────────────────────────────────────────────────────


def epoch_to_site(ts: Any, tz: str | None = None) -> _dt.datetime:
	"""Meta timestamp (seconds, milliseconds, numeric string or ISO) to naive site time."""
	from zoneinfo import ZoneInfo

	zone = None
	if tz:
		try:
			zone = ZoneInfo(tz)
		except Exception:
			zone = None
	try:
		number = float(ts)
		if number > 1e11:  # milliseconds (Messenger / Instagram)
			number = number / 1000.0
		aware = _dt.datetime.fromtimestamp(number, tz=_UTC)
	except (TypeError, ValueError, OverflowError, OSError):
		try:
			text = str(ts).replace("Z", "+00:00")
			if len(text) >= 5 and text[-5] in "+-" and text[-3] != ":":  # +0000 form
				text = f"{text[:-2]}:{text[-2:]}"
			aware = _dt.datetime.fromisoformat(text)
			if aware.tzinfo is None:
				aware = aware.replace(tzinfo=_UTC)
		except Exception:
			aware = _dt.datetime.now(tz=_UTC)
	if zone is not None:
		aware = aware.astimezone(zone)
	return aware.replace(tzinfo=None, microsecond=0)


def _epoch(ts: Any) -> float:
	try:
		number = float(ts)
		return number / 1000.0 if number > 1e11 else number
	except (TypeError, ValueError):
		try:
			return epoch_to_site(ts, "UTC").replace(tzinfo=_UTC).timestamp()
		except Exception:
			return 0.0


# ── pure: parsing ───────────────────────────────────────────────────────


def _event(**kwargs: Any) -> dict[str, Any]:
	base = {
		"kind": "message",
		"channel": None,
		"external_id": None,
		"contact_name": None,
		"phone": None,
		"direction": "Inbound",
		"sent_via": None,
		"external_message_id": None,
		"message_type": "text",
		"body": "",
		"media_id": None,
		"media_url": None,
		"media_mime": None,
		"timestamp": 0.0,
		"referral": None,
		"raw": None,
		"post_id": None,
		"status": None,
		"error": None,
		"history": False,
		"media_update": False,
	}
	base.update(kwargs)
	return base


def _wa_message_content(msg: dict[str, Any]) -> dict[str, Any]:
	"""``{message_type, body, media_id, media_mime, raw}`` for one WhatsApp message."""
	mtype = str(msg.get("type") or "")
	out: dict[str, Any] = {
		"message_type": "text",
		"body": "",
		"media_id": None,
		"media_mime": None,
		"raw": None,
	}
	if mtype == "text":
		out["body"] = str((msg.get("text") or {}).get("body") or "")
	elif mtype in _WA_MEDIA_TYPES:
		media = msg.get(mtype) or {}
		out["message_type"] = mtype
		out["media_id"] = media.get("id")
		out["media_mime"] = media.get("mime_type")
		out["body"] = str(
			media.get("caption") or (media.get("filename") if mtype == "document" else "") or ""
		)
	elif mtype == "location":
		loc = msg.get("location") or {}
		out["message_type"] = "location"
		out["body"] = f"{loc.get('latitude')},{loc.get('longitude')}"
	elif mtype == "reaction":
		reaction = msg.get("reaction") or {}
		out["message_type"] = "reaction"
		out["body"] = str(reaction.get("emoji") or "")
	elif mtype == "button":
		# A template quick-reply tap; a static button's payload is its text.
		button = msg.get("button") or {}
		out["body"] = str(button.get("text") or "")
		out["button_payload"] = str(button.get("payload") or "") or None
	elif mtype == "interactive":
		inter = msg.get("interactive") or {}
		reply = inter.get("button_reply") or inter.get("list_reply") or {}
		out["body"] = str(reply.get("title") or "")
	elif mtype == "media_placeholder":
		# History-sync media: the file itself only follows for the last 14 days.
		out["message_type"] = "unsupported"
		out["raw"] = {"type": "media_placeholder"}
	else:  # contacts, order, system, unsupported, unknown
		out["message_type"] = "unsupported"
		out["raw"] = msg
	return out


def _parse_whatsapp(entry: dict[str, Any], events: list[dict[str, Any]]) -> None:
	for change in entry.get("changes") or []:
		field = change.get("field")
		value = change.get("value") or {}
		if field == "messages":
			names = {
				str(c.get("wa_id")): str((c.get("profile") or {}).get("name") or "")
				for c in value.get("contacts") or []
				if c.get("wa_id")
			}
			for msg in value.get("messages") or []:
				wa_id = str(msg.get("from") or "")
				if not wa_id or not msg.get("id"):
					continue
				content = _wa_message_content(msg)
				if content["message_type"] == "reaction" and not content["body"]:
					continue  # a reaction removed
				events.append(
					_event(
						channel=cc.WHATSAPP,
						external_id=wa_id,
						contact_name=names.get(wa_id) or None,
						phone=wa_id,
						external_message_id=str(msg["id"]),
						timestamp=_epoch(msg.get("timestamp")),
						referral=msg.get("referral") or None,
						**content,
					)
				)
			for st in value.get("statuses") or []:
				if not st.get("id") or not st.get("status"):
					continue
				error = None
				errs = st.get("errors") or []
				if errs:
					first = errs[0] or {}
					error = (
						f"{first.get('code', '')}: {first.get('title') or first.get('message') or ''}".strip()
					)
				category = (st.get("pricing") or {}).get("category") or (
					(st.get("conversation") or {}).get("origin") or {}
				).get("type")
				events.append(
					_event(
						kind="status",
						channel=cc.WHATSAPP,
						external_message_id=str(st["id"]),
						status=str(st["status"]),
						recipient=str(st.get("recipient_id") or "") or None,
						category=str(category or "").lower() or None,
						error=error,
						timestamp=_epoch(st.get("timestamp")),
					)
				)
		elif field in ("smb_message_echoes", "message_echoes"):
			echoes = value.get("message_echoes") or value.get("messages") or []
			for msg in echoes:
				to = str(msg.get("to") or "")
				if not to or not msg.get("id"):
					continue
				content = _wa_message_content(msg)
				events.append(
					_event(
						channel=cc.WHATSAPP,
						external_id=to,
						phone=to,
						direction="Outbound",
						sent_via="Phone",
						external_message_id=str(msg["id"]),
						timestamp=_epoch(msg.get("timestamp")),
						**content,
					)
				)
		elif field == "history":
			_parse_wa_history(value, events)
		elif field == "smb_app_state_sync":
			_parse_wa_contacts(value, events)


def _wa_history_event(msg: dict[str, Any], thread_id: str | None, **extra: Any) -> dict[str, Any] | None:
	"""One past message from a ``history`` webhook. A business message carries ``to``."""
	if not msg.get("id"):
		return None
	outbound = bool(msg.get("to"))
	wa_id = str(thread_id or (msg.get("to") if outbound else msg.get("from")) or "")
	if not wa_id:
		return None
	content = _wa_message_content(msg)
	if content["message_type"] == "reaction" and not content["body"]:
		return None
	return _event(
		channel=cc.WHATSAPP,
		external_id=wa_id,
		phone=wa_id,
		direction="Outbound" if outbound else "Inbound",
		sent_via="Phone" if outbound else None,
		external_message_id=str(msg["id"]),
		timestamp=_epoch(msg.get("timestamp")),
		history=True,
		**content,
		**extra,
	)


def _history_errors(holder: dict[str, Any], events: list[dict[str, Any]]) -> None:
	for err in holder.get("errors") or []:
		err = err or {}
		events.append(
			_event(
				kind="history_error",
				channel=cc.WHATSAPP,
				error=f"{err.get('code', '')}: {err.get('title') or err.get('message') or ''}".strip(),
			)
		)


def _parse_wa_history(value: dict[str, Any], events: list[dict[str, Any]]) -> None:
	"""Past 1:1 chats Meta sends once after a coexistence onboarding (up to 180 days).

	Chunks carry ``threads[].messages[]``. A later ``history`` webhook carries
	``messages[]`` with the real media for placeholders from the last 14 days.
	"""
	_history_errors(value, events)
	for chunk in value.get("history") or []:
		if not isinstance(chunk, dict):
			continue
		_history_errors(chunk, events)
		meta = chunk.get("metadata") or {}
		if meta:
			events.append(
				_event(
					kind="history_progress",
					channel=cc.WHATSAPP,
					status=f"phase {meta.get('phase', '?')} chunk {meta.get('chunk_order', '?')} "
					f"progress {meta.get('progress', '?')}%",
				)
			)
		for thread in chunk.get("threads") or []:
			thread_id = str((thread or {}).get("id") or "")
			for msg in (thread or {}).get("messages") or []:
				event = _wa_history_event(msg or {}, thread_id)
				if event:
					events.append(event)
	for msg in value.get("messages") or []:
		event = _wa_history_event(msg or {}, None, media_update=True)
		if event:
			events.append(event)


def _parse_wa_contacts(value: dict[str, Any], events: list[dict[str, Any]]) -> None:
	"""Names from the WhatsApp Business app's address book (``smb_app_state_sync``)."""
	for item in value.get("state_sync") or []:
		item = item or {}
		if item.get("type") != "contact" or item.get("action") == "remove":
			continue
		contact = item.get("contact") or {}
		phone = "".join(ch for ch in str(contact.get("phone_number") or "") if ch.isdigit())
		name = str(contact.get("full_name") or contact.get("first_name") or "").strip()
		if phone and name:
			events.append(
				_event(kind="contact", channel=cc.WHATSAPP, external_id=phone, phone=phone, contact_name=name)
			)


def is_history_payload(payload: Any) -> bool:
	"""True when a WhatsApp payload carries history sync (it goes to the long queue)."""
	try:
		if payload.get("object") != "whatsapp_business_account":
			return False
		return any(
			change.get("field") == "history"
			for entry in payload.get("entry") or []
			for change in entry.get("changes") or []
		)
	except Exception:
		return False


def _messaging_attachments(message: dict[str, Any]) -> list[dict[str, Any]]:
	out = []
	for att in message.get("attachments") or []:
		atype = str(att.get("type") or "")
		payload = att.get("payload") or {}
		mapped = _ATTACHMENT_TYPES.get(atype)
		if mapped:
			out.append({"message_type": mapped, "media_url": payload.get("url"), "raw": None})
		else:  # share, story_mention, ig_reel, template, fallback, location ...
			out.append({"message_type": "unsupported", "media_url": None, "raw": att})
	return out


def _parse_messaging(
	entry: dict[str, Any], channel: str, own_ids: set[str], events: list[dict[str, Any]]
) -> None:
	account_id = str(entry.get("id") or "")
	for item in entry.get("messaging") or []:
		sender = str((item.get("sender") or {}).get("id") or "")
		recipient = str((item.get("recipient") or {}).get("id") or "")
		ts = _epoch(item.get("timestamp") or entry.get("time"))

		delivery = item.get("delivery")
		if delivery:
			for mid in delivery.get("mids") or []:
				events.append(
					_event(
						kind="status",
						channel=channel,
						external_message_id=str(mid),
						status="delivered",
						timestamp=ts,
					)
				)
			continue

		message = item.get("message")
		postback = item.get("postback")
		if not message and not postback:
			continue  # read, reaction, referral-only, optin ...

		is_echo = bool(message and message.get("is_echo"))
		if not is_echo and own_ids and sender in own_ids and sender != recipient:
			is_echo = True
		customer = recipient if is_echo else sender
		if not customer or customer == account_id:
			continue
		direction = "Outbound" if is_echo else "Inbound"
		sent_via = "Phone" if is_echo else None
		referral = item.get("referral") or (message or {}).get("referral") or (postback or {}).get("referral")

		if postback:
			mid = postback.get("mid") or f"pb-{customer}-{int(ts * 1000)}"
			events.append(
				_event(
					channel=channel,
					external_id=customer,
					direction=direction,
					sent_via=sent_via,
					external_message_id=str(mid),
					body=str(postback.get("title") or postback.get("payload") or ""),
					timestamp=ts,
					referral=referral,
				)
			)
			continue

		if message.get("is_deleted") or message.get("is_unsupported"):
			if message.get("is_unsupported") and message.get("mid"):
				events.append(
					_event(
						channel=channel,
						external_id=customer,
						direction=direction,
						sent_via=sent_via,
						external_message_id=str(message["mid"]),
						message_type="unsupported",
						timestamp=ts,
						raw={"is_unsupported": True},
					)
				)
			continue

		mid = str(message.get("mid") or "")
		if not mid:
			continue
		text = str(message.get("text") or "")
		attachments = _messaging_attachments(message)
		if not attachments:
			events.append(
				_event(
					channel=channel,
					external_id=customer,
					direction=direction,
					sent_via=sent_via,
					external_message_id=mid,
					body=text,
					timestamp=ts,
					referral=referral,
				)
			)
			continue
		for index, att in enumerate(attachments):
			events.append(
				_event(
					channel=channel,
					external_id=customer,
					direction=direction,
					sent_via=sent_via,
					external_message_id=mid if index == 0 else f"{mid}#{index}",
					message_type=att["message_type"],
					media_url=att["media_url"],
					raw=att["raw"],
					body=text if index == 0 else "",
					timestamp=ts,
					referral=referral if index == 0 else None,
				)
			)


def _parse_feed(entry: dict[str, Any], own_ids: set[str], events: list[dict[str, Any]]) -> None:
	page_id = str(entry.get("id") or "")
	for change in entry.get("changes") or []:
		if change.get("field") != "feed":
			continue
		value = change.get("value") or {}
		if value.get("item") != "comment" or value.get("verb") != "add":
			continue
		comment_id = str(value.get("comment_id") or "")
		post_id = str(value.get("post_id") or "")
		parent_id = str(value.get("parent_id") or "")
		if not comment_id:
			continue
		root = comment_id if (not parent_id or parent_id == post_id) else parent_id
		author = value.get("from") or {}
		author_id = str(author.get("id") or "")
		outbound = bool(author_id) and (author_id == page_id or author_id in own_ids)
		events.append(
			_event(
				channel=cc.FACEBOOK_COMMENT,
				external_id=root,
				contact_name=None if outbound else (author.get("name") or None),
				direction="Outbound" if outbound else "Inbound",
				sent_via="Phone" if outbound else None,
				external_message_id=comment_id,
				message_type="comment",
				body=str(value.get("message") or ""),
				timestamp=_epoch(value.get("created_time") or entry.get("time")),
				post_id=post_id or None,
			)
		)


def _parse_ig_comments(entry: dict[str, Any], own_ids: set[str], events: list[dict[str, Any]]) -> None:
	account_id = str(entry.get("id") or "")
	for change in entry.get("changes") or []:
		if change.get("field") not in ("comments", "live_comments"):
			continue
		value = change.get("value") or {}
		comment_id = str(value.get("id") or "")
		if not comment_id:
			continue
		root = str(value.get("parent_id") or "") or comment_id
		author = value.get("from") or {}
		author_id = str(author.get("id") or "")
		outbound = bool(author_id) and (author_id == account_id or author_id in own_ids)
		events.append(
			_event(
				channel=cc.INSTAGRAM_COMMENT,
				external_id=root,
				contact_name=None if outbound else (author.get("username") or None),
				direction="Outbound" if outbound else "Inbound",
				sent_via="Phone" if outbound else None,
				external_message_id=comment_id,
				message_type="comment",
				body=str(value.get("text") or ""),
				timestamp=_epoch(value.get("timestamp") or entry.get("time")),
				post_id=str((value.get("media") or {}).get("id") or "") or None,
			)
		)


def parse_webhook(payload: dict[str, Any], own_ids: set[str] | None = None) -> list[dict[str, Any]]:
	"""Flatten one Meta webhook payload into normalised events. Pure; never raises.

	``own_ids`` are our Page / Instagram account / phone-number ids, used to
	recognise our own comments and messages as Outbound.
	"""
	events: list[dict[str, Any]] = []
	own = {str(i) for i in (own_ids or set()) if i}
	try:
		if not isinstance(payload, dict):
			return events
		obj = payload.get("object")
		for entry in payload.get("entry") or []:
			if not isinstance(entry, dict):
				continue
			try:
				if obj == "whatsapp_business_account":
					_parse_whatsapp(entry, events)
				elif obj == "page":
					_parse_messaging(entry, cc.MESSENGER, own, events)
					_parse_feed(entry, own, events)
				elif obj == "instagram":
					_parse_messaging(entry, cc.INSTAGRAM, own, events)
					_parse_ig_comments(entry, own, events)
			except Exception:
				continue
	except Exception:
		return events
	return events


def should_draft(event: dict[str, Any]) -> bool:
	"""Inbound text, comment, or image with a caption gets an AI draft."""
	if event.get("kind") != "message" or event.get("direction") != "Inbound":
		return False
	mtype = event.get("message_type")
	if mtype in ("text", "comment"):
		return bool(str(event.get("body") or "").strip())
	if mtype == "image":
		return bool(str(event.get("body") or "").strip())
	return False


# ── persist ─────────────────────────────────────────────────────────────


def _site_tz() -> str | None:
	try:
		return frappe.utils.get_system_timezone()
	except Exception:
		return None


def _own_ids(settings: dict[str, Any]) -> set[str]:
	return {
		str(settings.get(k))
		for k in ("facebook_page_id", "instagram_account_id", "whatsapp_phone_number_id")
		if settings.get(k)
	}


def _match_customer(phone: str | None) -> str | None:
	if not phone:
		return None
	try:
		from jarz_pos.utils.phone import phone_variants

		variants = phone_variants(phone)
		if not variants:
			return None
		rows = frappe.get_all(
			"Customer",
			filters={"mobile_no": ["in", variants], "disabled": 0},
			pluck="name",
			order_by="modified desc",
			limit_page_length=1,
			ignore_permissions=True,
		)
		return rows[0] if rows else None
	except Exception:
		return None


#: An Egyptian mobile as customers type it: 010…, 2010…, +2010…, or 10… (no trunk zero).
_TYPED_MOBILE = re.compile(r"(?<!\d)(?:\+?20|0)?1[0125]\d{8}(?!\d)")
_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


def typed_phones(text: Any) -> list[str]:
	"""Egyptian mobile numbers typed in a message, in order, deduplicated. Pure.

	Arabic-Indic digits and single spaces, dashes or dots between digits are
	accepted ("0111 103 4268", "٠١١١٠٣٤٢٦٨").
	"""
	clean = str(text or "").translate(_ARABIC_DIGITS)
	clean = re.sub(r"(?<=\d)[ \-.]{1,2}(?=\d)", "", clean)
	return list(dict.fromkeys(_TYPED_MOBILE.findall(clean)))


def link_by_typed_phone(body: Any, current_phone: str | None = None) -> dict[str, Any]:
	"""Conversation updates linking a Messenger / Instagram chat to the customer whose
	mobile the customer typed. ``{}`` unless the numbers point at exactly one customer.

	A number can belong to someone else (a gift's recipient); staff relink by
	hand, and a chat already linked is never touched (callers check).
	"""
	phones = typed_phones(body)
	if not phones:
		return {}
	try:
		from jarz_pos.utils.phone import normalize_phone, phone_variants

		variants = sorted({v for p in phones for v in phone_variants(p)})
		customers = frappe.get_all(
			"Customer",
			filters={"mobile_no": ["in", variants], "disabled": 0},
			pluck="name",
			limit_page_length=3,
			ignore_permissions=True,
		)
	except Exception:
		return {}
	if len(set(customers)) != 1:
		return {}
	updates = {"customer": customers[0], "last_order": _last_order(customers[0])}
	if not current_phone:
		updates["phone"] = normalize_phone(phones[0]) or None
	return updates


def link_chats_by_typed_phone(dry_run: bool = True) -> dict[str, int]:
	"""Link every unlinked chat whose customer typed a mobile that belongs to
	exactly one customer. Re-runnable."""
	result = {"checked": 0, "linked": 0}
	convs = frappe.get_all(
		cc.CONVERSATION,
		filters={"customer": ["is", "not set"]},
		fields=["name", "phone"],
		limit_page_length=0,
	)
	for conv in convs:
		result["checked"] += 1
		bodies = frappe.get_all(
			cc.MESSAGE,
			filters={"conversation": conv.name, "direction": "Inbound", "body": ["like", "%1%"]},
			pluck="body",
			order_by="timestamp asc",
			limit_page_length=500,
		)
		updates = link_by_typed_phone("\n".join(b or "" for b in bodies), conv.phone)
		if not updates:
			continue
		result["linked"] += 1
		if not dry_run:
			frappe.db.set_value(cc.CONVERSATION, conv.name, updates, update_modified=False)
	if not dry_run:
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- one-off backfill
	return result


def _last_order(customer: str | None) -> str | None:
	if not customer:
		return None
	try:
		rows = frappe.get_all(
			"Sales Invoice",
			filters={"customer": customer, "docstatus": 1},
			pluck="name",
			order_by="posting_date desc, creation desc",
			limit_page_length=1,
			ignore_permissions=True,
		)
		return rows[0] if rows else None
	except Exception:
		return None


def _find_conversation(channel: str, external_id: str) -> Any:
	key = f"{channel}:{external_id}"
	name = frappe.db.get_value(cc.CONVERSATION, {"channel_key": key}, "name")
	if not name:
		return None
	fields = [*cc.SUMMARY_FIELDS, "alert_count", "source_post_id", "ad_referral", "last_outbound_at"]
	return frappe.db.get_value(cc.CONVERSATION, name, fields, as_dict=True)


def _contact_key(phone: str) -> str:
	return frappe.cache().make_key(f"jarz_chat_contact:{phone}")


def _cached_contact_name(phone: str | None) -> str | None:
	if not phone:
		return None
	try:
		value = frappe.cache().get(_contact_key(phone))
		if isinstance(value, bytes):
			value = value.decode("utf-8", "ignore")
		return str(value) if value else None
	except Exception:
		return None


def _lookup_dm_name(channel: str, external_id: str | None) -> str | None:
	"""Ask Meta for a DM customer's name (their webhooks carry only an id).

	Best effort: at most one attempt per person every
	:data:`NAME_LOOKUP_RETRY_SECONDS`, and a Graph failure just leaves the chat unnamed.
	"""
	if channel not in (cc.MESSENGER, cc.INSTAGRAM) or not external_id:
		return None
	try:
		key = frappe.cache().make_key(f"jarz_chat_name_lookup:{channel}:{external_id}")
		if frappe.cache().get(key):
			return None
		frappe.cache().set(key, "1", ex=NAME_LOOKUP_RETRY_SECONDS)
	except Exception:
		pass
	from jarz_pos.services import chat_meta_client as meta

	platform = "instagram" if channel == cc.INSTAGRAM else "messenger"
	try:
		name = meta.dm_contact_name(platform, external_id)
	except Exception:
		return None
	return name[:140] if name else None


def _create_conversation(event: dict[str, Any], when: _dt.datetime) -> Any:
	channel = event["channel"]
	external_id = event["external_id"]
	phone = None
	customer = None
	contact_name = event.get("contact_name") or ""
	if channel == cc.WHATSAPP:
		from jarz_pos.utils.phone import normalize_phone

		phone = normalize_phone(event.get("phone") or external_id) or None
		customer = _match_customer(phone)
		contact_name = contact_name or _cached_contact_name(external_id) or ""
	doc = frappe.get_doc(
		{
			"doctype": cc.CONVERSATION,
			"channel": channel,
			"external_id": external_id,
			"channel_key": f"{channel}:{external_id}",
			"contact_name": contact_name[:140] or None,
			"phone": phone,
			"customer": customer,
			"last_order": _last_order(customer),
			# A chat that only exists because of imported history is not waiting on anyone.
			"status": "Closed" if event.get("history") else "Open",
			"unread_count": 0,
			"alert_count": 0,
			"source_post_id": event.get("post_id"),
			"ad_referral": json.dumps(event["referral"], ensure_ascii=False)[:2000]
			if event.get("referral")
			else None,
		}
	)
	try:
		doc.insert(ignore_permissions=True)
	except (frappe.DuplicateEntryError, frappe.UniqueValidationError):
		# A parallel worker created it between our read and insert.
		return _find_conversation(channel, external_id)
	return _find_conversation(channel, external_id)


def _message_exists(external_message_id: str | None) -> bool:
	if not external_message_id:
		return False
	return bool(frappe.db.exists(cc.MESSAGE, {"external_message_id": external_message_id}))


def _is_app_echo(conversation: str, body: str, when: _dt.datetime) -> bool:
	"""An app send whose echo arrived before its own external id was stored."""
	if not body:
		return False
	low = when - _dt.timedelta(seconds=ECHO_MATCH_SECONDS)
	high = when + _dt.timedelta(seconds=ECHO_MATCH_SECONDS)
	rows = frappe.get_all(
		cc.MESSAGE,
		filters={
			"conversation": conversation,
			"direction": "Outbound",
			"sent_via": "App",
			"timestamp": ["between", [low, high]],
		},
		fields=["name", "body"],
		limit_page_length=10,
		ignore_permissions=True,
	)
	return any(str(r.body or "").strip() == body.strip() for r in rows)


def _apply_status(event: dict[str, Any]) -> str | None:
	name = frappe.db.get_value(cc.MESSAGE, {"external_message_id": event["external_message_id"]}, "name")
	if not name:
		# A follow-up template has no chat message row: its status goes on the touch.
		from jarz_pos.services import customer_followups as followups

		followups.record_template_status(event["external_message_id"], event.get("status"), event.get("error"))
		return None
	row = frappe.db.get_value(cc.MESSAGE, name, ["status", "direction", "conversation", "sent_via"], as_dict=True)
	if not row or row.direction != "Outbound":
		return None
	if row.sent_via == "System":
		# A follow-up template shown in the chat: its touch still tracks delivered / read.
		from jarz_pos.services import customer_followups as followups

		followups.record_template_status(event["external_message_id"], event.get("status"), event.get("error"))
	new = str(event.get("status") or "")
	if new == "failed":
		frappe.db.set_value(
			cc.MESSAGE,
			name,
			{"status": "failed", "error": (event.get("error") or "failed")[:500]},
			update_modified=True,
		)
		return row.conversation
	if new in _STATUS_RANK and _STATUS_RANK[new] > _STATUS_RANK.get(str(row.status or ""), 0):
		frappe.db.set_value(cc.MESSAGE, name, "status", new, update_modified=True)
		return row.conversation
	return None


def _status_only_reply_event(event: dict[str, Any]) -> dict[str, Any] | None:
	"""The placeholder Outbound message for a status about a WhatsApp reply we never saw.

	``None`` unless the status could only come from a staff reply: WhatsApp, a
	recipient, an early status (sent / delivered) and a service conversation
	rather than a template.
	"""
	if event.get("channel") != cc.WHATSAPP or not event.get("external_message_id"):
		return None
	recipient = str(event.get("recipient") or "")
	if not recipient or str(event.get("status") or "") not in _STATUS_ONLY_STATES:
		return None
	if str(event.get("category") or "") not in _STATUS_ONLY_CATEGORIES:
		return None
	return _event(
		channel=cc.WHATSAPP,
		external_id=recipient,
		phone=recipient,
		direction="Outbound",
		sent_via="Phone",
		external_message_id=event["external_message_id"],
		message_type="text",
		body=STATUS_ONLY_BODY,
		timestamp=event.get("timestamp") or 0.0,
		status=event.get("status"),
		status_only=True,
	)


def _app_send_in_flight(conversation: str, when: _dt.datetime) -> bool:
	"""A Jarz send whose message id is not stored yet: its status is not an outside reply."""
	low = when - _dt.timedelta(seconds=ECHO_MATCH_SECONDS)
	high = when + _dt.timedelta(seconds=ECHO_MATCH_SECONDS)
	return bool(
		frappe.get_all(
			cc.MESSAGE,
			filters={
				"conversation": conversation,
				"direction": "Outbound",
				"sent_via": "App",
				"status": "queued",
				"timestamp": ["between", [low, high]],
			},
			fields=["name"],
			limit_page_length=1,
			ignore_permissions=True,
		)
	)


def _record_status_only_reply(
	event: dict[str, Any], settings: dict[str, Any], tz: str | None
) -> dict[str, Any] | None:
	"""Store a Business Suite reply from its status update. Only for a chat we already have."""
	reply = _status_only_reply_event(event)
	if reply is None:
		return None
	conv = _find_conversation(cc.WHATSAPP, reply["external_id"])
	if conv is None:
		return None
	when = epoch_to_site(reply["timestamp"], tz) if reply.get("timestamp") else cc.now()
	last_in = cc.to_dt(conv.get("last_inbound_at"))
	if last_in is not None and when < last_in:
		# The customer wrote after this reply went out: it does not answer them.
		return None
	if _app_send_in_flight(conv.name, when):
		return None
	return _persist_message(reply, settings, tz)


def _upgrade_status_only(event: dict[str, Any]) -> dict[str, Any] | None:
	"""Give a status-only placeholder the real content of its late echo."""
	if event.get("direction") != "Outbound" or not (event.get("body") or event.get("media_id")):
		return None
	row = frappe.db.get_value(
		cc.MESSAGE,
		{"external_message_id": event.get("external_message_id")},
		["name", "conversation", "raw_json"],
		as_dict=True,
	)
	if not row or row.raw_json != STATUS_ONLY_MARKER:
		return None
	body = str(event.get("body") or "")
	mtype = event.get("message_type") or "text"
	frappe.db.set_value(
		cc.MESSAGE,
		row.name,
		{
			"message_type": mtype,
			"body": body,
			"media_id": event.get("media_id"),
			"media_mime": event.get("media_mime"),
			"raw_json": None,
		},
		update_modified=True,
	)
	placeholder = cc.preview(STATUS_ONLY_BODY, "text")
	if frappe.db.get_value(cc.CONVERSATION, row.conversation, "last_message_preview") == placeholder:
		frappe.db.set_value(
			cc.CONVERSATION,
			row.conversation,
			"last_message_preview",
			cc.preview(body, mtype),
			update_modified=False,
		)
	return {
		"conversation": row.conversation,
		"message": row.name,
		"draft": False,
		"media": bool(event.get("media_id")) and mtype in _WA_MEDIA_TYPES,
		"media_url": None,
		"superseded": False,
		"push": False,
		"quiet": bool(event.get("history")),
	}


def _apply_contact(event: dict[str, Any]) -> str | None:
	"""Name a WhatsApp chat from the business's address book. Returns the conversation touched.

	The name is also cached, because the address book may sync before the
	history that creates the chat. A name already set from the address book or
	a customer profile is kept; only an empty or phone-only name is replaced.
	"""
	phone = str(event.get("external_id") or "")
	name = str(event.get("contact_name") or "").strip()[:140]
	if not phone or not name:
		return None
	try:
		frappe.cache().set(_contact_key(phone), name, ex=CONTACT_NAME_TTL_SECONDS)
	except Exception:
		pass
	conv = frappe.db.get_value(
		cc.CONVERSATION, {"channel_key": f"{cc.WHATSAPP}:{phone}"}, ["name", "contact_name"], as_dict=True
	)
	if not conv:
		return None
	current = str(conv.contact_name or "").strip()
	if current and any(ch.isalpha() for ch in current):
		return None
	frappe.db.set_value(cc.CONVERSATION, conv.name, "contact_name", name, update_modified=False)
	return conv.name


def _apply_media_update(event: dict[str, Any]) -> dict[str, Any] | None:
	"""Fill a history media placeholder with the real file Meta sent afterwards."""
	if not event.get("media_id"):
		return None
	row = frappe.db.get_value(
		cc.MESSAGE,
		{"external_message_id": event.get("external_message_id")},
		["name", "conversation", "message_type", "body"],
		as_dict=True,
	)
	if not row or row.message_type != "unsupported":
		return None
	frappe.db.set_value(
		cc.MESSAGE,
		row.name,
		{
			"message_type": event.get("message_type"),
			"media_id": event.get("media_id"),
			"media_mime": event.get("media_mime"),
			"body": row.body or str(event.get("body") or ""),
			"raw_json": None,
		},
		update_modified=False,
	)
	return {
		"conversation": row.conversation,
		"message": row.name,
		"draft": False,
		"media": True,
		"media_url": None,
		"superseded": False,
		"push": False,
		"quiet": True,
	}


def _persist_history_message(event: dict[str, Any], conv: Any, when: _dt.datetime) -> dict[str, Any] | None:
	"""Insert one imported past message without disturbing the live inbox.

	Status, waiting time, unread count, alerts and drafts stay as they are; no
	push and no realtime event. Only the list order (last message) and the real
	customer-service window move, and only when this message is newer.
	"""
	inbound = event.get("direction") == "Inbound"
	body = str(event.get("body") or "")
	media_url = event.get("media_url")
	stored_url = media_url if media_url and len(str(media_url)) <= 140 else None
	raw = None
	if event.get("message_type") == "unsupported" and event.get("raw") is not None:
		try:
			raw = json.dumps(event["raw"], ensure_ascii=False)[:RAW_JSON_MAX]
		except Exception:
			raw = None
	msg = frappe.get_doc(
		{
			"doctype": cc.MESSAGE,
			"conversation": conv.name,
			"direction": "Inbound" if inbound else "Outbound",
			"external_message_id": event.get("external_message_id"),
			"message_type": event.get("message_type") or "text",
			"body": body,
			"media_id": event.get("media_id"),
			"media_url": stored_url,
			"media_mime": event.get("media_mime"),
			"sent_via": "" if inbound else (event.get("sent_via") or "Phone"),
			"status": "received" if inbound else "sent",
			"timestamp": when,
			"raw_json": raw,
		}
	)
	try:
		msg.insert(ignore_permissions=True)
	except (frappe.DuplicateEntryError, frappe.UniqueValidationError):
		return None

	updates: dict[str, Any] = {}
	last_at = cc.to_dt(conv.get("last_message_at"))
	if last_at is None or when >= last_at:
		updates.update(
			{
				"last_message_at": when,
				"last_message_preview": cc.preview(body, event.get("message_type")),
				"last_direction": "Inbound" if inbound else "Outbound",
			}
		)
	stamp = "last_inbound_at" if inbound else "last_outbound_at"
	current = cc.to_dt(conv.get(stamp))
	if current is None or when > current:
		updates[stamp] = when
	if inbound and event["channel"] not in cc.COMMENT_CHANNELS:
		expires = when + _dt.timedelta(hours=DM_WINDOW_HOURS)
		window = cc.to_dt(conv.get("window_expires_at"))
		if expires > cc.now() and (window is None or expires > window):
			updates["window_expires_at"] = expires
	if updates:
		frappe.db.set_value(cc.CONVERSATION, conv.name, updates, update_modified=False)
	return {
		"conversation": conv.name,
		"message": msg.name,
		"draft": False,
		"media": bool(event.get("media_id")),
		"media_url": None,
		"superseded": False,
		"push": False,
		"quiet": True,
	}


def _persist_message(
	event: dict[str, Any], settings: dict[str, Any], tz: str | None
) -> dict[str, Any] | None:
	"""Insert one message event. Returns post-commit actions or ``None`` when skipped."""
	if not event.get("channel") or not event.get("external_id"):
		return None
	if _message_exists(event.get("external_message_id")):
		if event.get("media_update"):
			return _apply_media_update(event)
		return _upgrade_status_only(event)
	if event.get("media_update"):
		# Media for a placeholder we never stored: its sender side is unknowable here.
		return None
	if event.get("history"):
		when = epoch_to_site(event.get("timestamp") or 0, tz) if event.get("timestamp") else cc.now()
		conv = _find_conversation(event["channel"], event["external_id"])
		if conv is None:
			conv = _create_conversation(event, when)
			if conv is None:
				return None
		return _persist_history_message(event, conv, when)
	when = epoch_to_site(event.get("timestamp") or 0, tz) if event.get("timestamp") else cc.now()
	inbound = event.get("direction") == "Inbound"

	conv = _find_conversation(event["channel"], event["external_id"])
	if conv is None:
		if not inbound and event["channel"] in (cc.MESSENGER, cc.INSTAGRAM):
			# An echo of a DM to someone with no thread here (typically our own
			# private reply to a comment). Not a conversation to open.
			return None
		conv = _create_conversation(event, when)
		if conv is None:
			return None
		if inbound and event["channel"] == cc.WHATSAPP:
			# Often an answer to a follow-up template: put it in the chat first.
			from jarz_pos.services import customer_followups as followups

			followups.show_templates_in_new_chat(conv)

	body = str(event.get("body") or "")
	if not inbound and _is_app_echo(conv.name, body, when):
		return None

	media_url = event.get("media_url")
	stored_url = media_url if media_url and len(str(media_url)) <= 140 else None
	raw = STATUS_ONLY_MARKER if event.get("status_only") else None
	if event.get("message_type") == "unsupported" and event.get("raw") is not None:
		try:
			raw = json.dumps(event["raw"], ensure_ascii=False)[:RAW_JSON_MAX]
		except Exception:
			raw = None

	msg = frappe.get_doc(
		{
			"doctype": cc.MESSAGE,
			"conversation": conv.name,
			"direction": "Inbound" if inbound else "Outbound",
			"external_message_id": event.get("external_message_id"),
			"message_type": event.get("message_type") or "text",
			"body": body,
			"media_id": event.get("media_id"),
			"media_url": stored_url,
			"media_mime": event.get("media_mime"),
			"sent_via": event.get("sent_via") or ("" if inbound else "Phone"),
			"status": "received" if inbound else (event.get("status") if event.get("status_only") else "sent"),
			"timestamp": when,
			"raw_json": raw,
		}
	)
	try:
		msg.insert(ignore_permissions=True)
	except (frappe.DuplicateEntryError, frappe.UniqueValidationError):
		return None

	updates: dict[str, Any] = {}
	last_at = cc.to_dt(conv.get("last_message_at"))
	newest = last_at is None or when >= last_at
	if newest:
		updates.update(
			{
				"last_message_at": when,
				"last_message_preview": cc.preview(body, event.get("message_type")),
				"last_direction": "Inbound" if inbound else "Outbound",
			}
		)
	if event.get("contact_name") and inbound:
		updates["contact_name"] = str(event["contact_name"])[:140]
	elif inbound and not conv.get("contact_name"):
		looked_up = _lookup_dm_name(event["channel"], event["external_id"])
		if looked_up:
			updates["contact_name"] = looked_up

	superseded: list[str] = []
	mtype = event.get("message_type")
	stop = inbound and event["channel"] == cc.WHATSAPP and _apply_stop_reply(event, conv, body)
	if inbound:
		updates["unread_count"] = int(conv.get("unread_count") or 0) + 1
		last_in = cc.to_dt(conv.get("last_inbound_at"))
		if last_in is None or when > last_in:
			updates["last_inbound_at"] = when
		if mtype != "reaction":
			if conv.get("status") != "Open":
				updates["status"] = "Open"
			if not conv.get("waiting_since") or conv.get("status") != "Open":
				updates["waiting_since"] = when
		if event["channel"] in cc.COMMENT_CHANNELS:
			if not conv.get("window_expires_at"):
				updates["window_expires_at"] = when + _dt.timedelta(days=COMMENT_WINDOW_DAYS)
		else:
			expires = when + _dt.timedelta(hours=DM_WINDOW_HOURS)
			current = cc.to_dt(conv.get("window_expires_at"))
			if current is None or expires > current:
				updates["window_expires_at"] = expires
		if event.get("referral") and not conv.get("ad_referral"):
			try:
				updates["ad_referral"] = json.dumps(event["referral"], ensure_ascii=False)[:2000]
			except Exception:
				pass
		if event.get("post_id") and not conv.get("source_post_id"):
			updates["source_post_id"] = event["post_id"]
		if not conv.get("customer"):
			link: dict[str, Any] = {}
			if event["channel"] == cc.WHATSAPP:
				customer = _match_customer(conv.get("phone"))
				if customer:
					link = {"customer": customer, "last_order": _last_order(customer)}
			# Otherwise the mobile they typed (Messenger, or a WhatsApp number we do not know).
			updates.update(link or link_by_typed_phone(body, conv.get("phone")))
	else:
		updates.update(
			{
				"status": "Answered" if conv.get("status") != "Closed" else "Closed",
				"waiting_since": None,
				"alert_sent_at": None,
				"alert_count": 0,
				"unread_count": 0,
				"last_outbound_at": when,
			}
		)
		superseded = cc.supersede_live_drafts(conv.name)

	if updates:
		frappe.db.set_value(cc.CONVERSATION, conv.name, updates, update_modified=True)

	return {
		"conversation": conv.name,
		"message": msg.name,
		"draft": bool(settings.get("auto_draft")) and should_draft(event) and not stop,
		"media": bool(event.get("media_id") or media_url)
		and mtype in ("image", "audio", "video", "document", "sticker"),
		"media_url": media_url if media_url and not stored_url else None,
		"superseded": bool(superseded),
		"push": inbound and mtype != "reaction" and not stop,
		"stop_reply": bool(stop),
	}


def _apply_stop_reply(event: dict[str, Any], conv: Any, body: str) -> bool:
	"""Opt the sender out of follow-ups when they tapped "Stop messages". Never raises."""
	from jarz_pos.services import customer_followups as followups

	if not (followups.is_stop_reply(event.get("button_payload")) or followups.is_stop_reply(body)):
		return False
	try:
		followups.opt_out_phone(conv.get("phone") or event.get("phone") or event.get("external_id"))
		return True
	except Exception:
		cc.safe_log_error("chat: stop-messages opt-out failed")
		return False


def process_webhook(payload: Any = None) -> dict[str, Any]:
	"""Background job: persist one verified webhook payload. Never raises."""
	result = {"events": 0, "messages": 0, "statuses": 0, "errors": 0}
	try:
		if frappe.session.user in (None, "", "Guest"):
			# nosemgrep: frappe-setuser -- background job (RQ worker) enqueued after the HMAC-verified webhook; worker calls frappe.destroy() after every job
			frappe.set_user("Administrator")
		if isinstance(payload, str | bytes):
			payload = json.loads(payload)
		from jarz_pos.services.chat_settings import get_chat_settings

		settings = get_chat_settings(refresh=True)
		if not settings.get("enabled"):
			return result
		events = parse_webhook(payload or {}, _own_ids(settings))
		result["events"] = len(events)
		tz = _site_tz()
		actions: list[dict[str, Any]] = []
		status_conversations: set[str] = set()
		for index, event in enumerate(events):
			savepoint = f"chat_evt_{index}"
			try:
				frappe.db.savepoint(savepoint)
				kind = event.get("kind")
				if kind == "status":
					conv = _apply_status(event)
					if conv:
						status_conversations.add(conv)
						result["statuses"] += 1
					else:
						action = _record_status_only_reply(event, settings, tz)
						if action:
							actions.append(action)
							result["messages"] += 1
				elif kind == "contact":
					if _apply_contact(event):
						result["contacts"] = result.get("contacts", 0) + 1
				elif kind == "history_progress":
					_note_history_progress(event.get("status"))
				elif kind == "history_error":
					# 2593109: the owner chose "Don't share chats" on the phone.
					_note_history_progress(f"error {event.get('error')}")
					cc.safe_log_error("chat: WhatsApp history sync refused", str(event.get("error") or ""))
				else:
					action = _persist_message(event, settings, tz)
					if action:
						actions.append(action)
						result["messages"] += 1
			except Exception:
				result["errors"] += 1
				try:
					frappe.db.rollback(save_point=savepoint)
				except Exception:
					pass
				cc.safe_log_error(
					"chat: webhook event failed",
					f"channel={event.get('channel')} kind={event.get('kind')} "
					f"id={mask_id(event.get('external_message_id'))}\n{frappe.get_traceback()}",
				)
		frappe.db.commit()
		_after_commit(actions, status_conversations)
	except Exception:
		result["errors"] += 1
		try:
			frappe.db.rollback()
		except Exception:
			pass
		cc.safe_log_error("chat: process_webhook failed")
	return result


def _note_history_progress(text: Any) -> None:
	"""Remember the latest history-sync step for ``import_chat_history`` status reads."""
	try:
		frappe.cache().set(
			frappe.cache().make_key(HISTORY_PROGRESS_KEY),
			json.dumps({"at": cc.dt_str(cc.now()), "step": str(text or "")[:200]}),
			ex=CONTACT_NAME_TTL_SECONDS,
		)
	except Exception:
		pass


def _confirm_stop(conversation: str) -> None:
	# One free-text reply inside the 24h window the tap just opened; it lands
	# in the inbox as an outbound message, so the thread reads Answered.
	from jarz_pos.services import chat_send
	from jarz_pos.services.customer_followups import STOP_CONFIRMATION

	try:
		chat_send.send_text(conversation, STOP_CONFIRMATION, "Administrator")
	except Exception:
		cc.safe_log_error("chat: stop-messages confirmation failed")


def _after_commit(actions: list[dict[str, Any]], status_conversations: set[str]) -> None:
	for action in actions:
		try:
			if not action.get("quiet"):
				# Imported history stays silent: thousands of rows must not flood realtime.
				cc.publish_chat_update(
					action["conversation"], "message", message=cc.load_message(action["message"])
				)
			if action.get("media"):
				frappe.enqueue(
					"jarz_pos.services.chat_ingest.download_media",
					queue="short",
					timeout=120,
					message_name=action["message"],
					url=action.get("media_url"),
					enqueue_after_commit=False,
				)
			if action.get("draft"):
				frappe.enqueue(
					"jarz_pos.services.chat_drafts.generate_draft_job",
					queue="default",
					timeout=180,
					conversation=action["conversation"],
					for_message=action["message"],
					enqueue_after_commit=False,
				)
		except Exception:
			cc.safe_log_error("chat: post-commit action failed")
		if action.get("stop_reply"):
			_confirm_stop(action["conversation"])
		if action.get("push"):
			from jarz_pos.services.chat_alerts import push_new_message

			push_new_message(action["conversation"], action["message"])
	for conv in status_conversations:
		cc.publish_chat_update(conv, "status")
	try:
		frappe.db.commit()  # flushes the after_commit realtime queue
	except Exception:
		pass


# ── media ───────────────────────────────────────────────────────────────


def _extension(mime: str | None, message_type: str | None) -> str:
	ext = mimetypes.guess_extension(str(mime or "").split(";")[0].strip()) if mime else None
	if ext == ".jpe":
		ext = ".jpg"
	if not ext:
		ext = {"image": ".jpg", "audio": ".ogg", "video": ".mp4", "sticker": ".webp"}.get(
			str(message_type or ""), ".bin"
		)
	return ext


def download_media(message_name: str, url: str | None = None) -> str | None:
	"""Background job: fetch a message's attachment into a private File. Never raises.

	``url`` carries an Instagram / Messenger CDN URL too long for the 140-char
	``media_url`` field. Returns the private file URL, or ``None``.
	"""
	try:
		if frappe.session.user in (None, "", "Guest"):
			# nosemgrep: frappe-setuser -- background job (RQ worker) enqueued server-side; worker calls frappe.destroy() after every job
			frappe.set_user("Administrator")
		row = frappe.db.get_value(
			cc.MESSAGE,
			message_name,
			["name", "conversation", "message_type", "media_id", "media_url", "media_mime", "media_file"],
			as_dict=True,
		)
		if not row or row.media_file:
			return None
		from jarz_pos.services import chat_meta_client as meta
		from jarz_pos.services.chat_settings import get_chat_settings

		mime = row.media_mime
		if row.media_id:
			info = meta.wa_media_info(row.media_id)
			size = info.get("file_size")
			if size and str(size).isdigit() and int(size) > meta.MAX_MEDIA_BYTES:
				raise meta.MetaApiError("Media is larger than 16 MB", "too_large")
			mime = mime or info.get("mime_type")
			content, ctype = meta.download(info.get("url"), get_chat_settings().get("whatsapp_access_token"))
		else:
			source = url or row.media_url
			if not source:
				return None
			content, ctype = meta.download(source, None)
		mime = mime or ctype or None
		file_doc = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": f"chat-{row.name}{_extension(mime, row.message_type)}",
				"is_private": 1,
				"content": content,
				"attached_to_doctype": cc.MESSAGE,
				"attached_to_name": row.name,
				"attached_to_field": "media_file",
			}
		)
		file_doc.insert(ignore_permissions=True)
		frappe.db.set_value(
			cc.MESSAGE, row.name, {"media_file": file_doc.file_url, "media_mime": mime}, update_modified=False
		)
		frappe.db.commit()
		cc.publish_chat_update(row.conversation, "message", message=cc.load_message(row.name))
		frappe.db.commit()
		return file_doc.file_url
	except Exception as exc:
		try:
			frappe.db.rollback()
			frappe.db.set_value(
				cc.MESSAGE, message_name, "error", f"media: {str(exc)[:300]}", update_modified=False
			)
			frappe.db.commit()
		except Exception:
			pass
		cc.safe_log_error(
			"chat: media download failed", f"message={message_name} {type(exc).__name__}: {str(exc)[:300]}"
		)
		return None
