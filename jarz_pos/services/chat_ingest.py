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
from typing import Any

import frappe

from jarz_pos.services import chat_common as cc

#: Bodies above this are refused before hashing (Meta batches stay far below).
MAX_BODY_BYTES = 2 * 1024 * 1024
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
		out["body"] = str((msg.get("button") or {}).get("text") or "")
	elif mtype == "interactive":
		inter = msg.get("interactive") or {}
		reply = inter.get("button_reply") or inter.get("list_reply") or {}
		out["body"] = str(reply.get("title") or "")
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
				events.append(
					_event(
						kind="status",
						channel=cc.WHATSAPP,
						external_message_id=str(st["id"]),
						status=str(st["status"]),
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
	fields = [*cc.SUMMARY_FIELDS, "alert_count", "source_post_id", "ad_referral"]
	return frappe.db.get_value(cc.CONVERSATION, name, fields, as_dict=True)


def _create_conversation(event: dict[str, Any], when: _dt.datetime) -> Any:
	channel = event["channel"]
	external_id = event["external_id"]
	phone = None
	customer = None
	if channel == cc.WHATSAPP:
		from jarz_pos.utils.phone import normalize_phone

		phone = normalize_phone(event.get("phone") or external_id) or None
		customer = _match_customer(phone)
	doc = frappe.get_doc(
		{
			"doctype": cc.CONVERSATION,
			"channel": channel,
			"external_id": external_id,
			"channel_key": f"{channel}:{external_id}",
			"contact_name": (event.get("contact_name") or "")[:140] or None,
			"phone": phone,
			"customer": customer,
			"last_order": _last_order(customer),
			"status": "Open",
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
		return None
	row = frappe.db.get_value(cc.MESSAGE, name, ["status", "direction", "conversation"], as_dict=True)
	if not row or row.direction != "Outbound":
		return None
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


def _persist_message(
	event: dict[str, Any], settings: dict[str, Any], tz: str | None
) -> dict[str, Any] | None:
	"""Insert one message event. Returns post-commit actions or ``None`` when skipped."""
	if not event.get("channel") or not event.get("external_id"):
		return None
	if _message_exists(event.get("external_message_id")):
		return None
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

	body = str(event.get("body") or "")
	if not inbound and _is_app_echo(conv.name, body, when):
		return None

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
			"sent_via": event.get("sent_via") or ("" if inbound else "Phone"),
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

	superseded: list[str] = []
	mtype = event.get("message_type")
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
		if event["channel"] == cc.WHATSAPP and not conv.get("customer"):
			customer = _match_customer(conv.get("phone"))
			if customer:
				updates["customer"] = customer
				updates["last_order"] = _last_order(customer)
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
		"draft": bool(settings.get("auto_draft")) and should_draft(event),
		"media": bool(event.get("media_id") or media_url)
		and mtype in ("image", "audio", "video", "document", "sticker"),
		"media_url": media_url if media_url and not stored_url else None,
		"superseded": bool(superseded),
		"push": inbound and mtype != "reaction",
	}


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
				if event.get("kind") == "status":
					conv = _apply_status(event)
					if conv:
						status_conversations.add(conv)
						result["statuses"] += 1
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


def _after_commit(actions: list[dict[str, Any]], status_conversations: set[str]) -> None:
	for action in actions:
		try:
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
