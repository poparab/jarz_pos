"""Import past chats into the inbox, so a returning customer's history is already there.

* **WhatsApp**: Meta sends up to 180 days of 1:1 chats on the webhook
  (``history`` field) and the business app's address book
  (``smb_app_state_sync``), once, after a coexistence onboarding, when
  :func:`request_whatsapp_history` asks within 24 h. ``chat_ingest`` stores them.
* **Messenger / Instagram**: :func:`backfill_page_history` walks the Page's
  Conversations API, which gives the last 20 messages of each thread.

Imported messages never push, never draft and never reopen a chat: a thread
that only exists because of history is created Closed, and reopens with its
whole past the moment the customer writes again.
"""

from __future__ import annotations

import datetime as _dt
import json
from typing import Any

import frappe

from jarz_pos.services import chat_common as cc
from jarz_pos.services import chat_ingest as ing

PLATFORMS = {cc.MESSENGER: "messenger", cc.INSTAGRAM: "instagram"}
MAX_THREADS = 2000
PAGE_SIZE = 25
BACKFILL_RESULT_KEY = "jarz_chat_backfill_result"
RESULT_TTL_SECONDS = 7 * 24 * 3600

#: Meta's synchronisation errors, in words a manager can act on.
SYNC_ERRORS = {
	"2593107": "Already requested once for this onboarding. Meta allows one request.",
	"2593108": "More than 24 hours since onboarding. Disconnect the number in the WhatsApp "
	"Business app (Settings > Account > Business Platform) and onboard it again.",
}


# ── pure ────────────────────────────────────────────────────────────────


def _iso_epoch(value: Any) -> float:
	try:
		return _dt.datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%S%z").timestamp()
	except Exception:
		return 0.0


def _attachment_content(att: dict[str, Any]) -> dict[str, Any]:
	mime = str(att.get("mime_type") or "")
	if att.get("image_data") or mime.startswith("image/"):
		mtype, url = "image", (att.get("image_data") or {}).get("url")
	elif att.get("video_data") or mime.startswith("video/"):
		mtype, url = "video", (att.get("video_data") or {}).get("url")
	elif mime.startswith("audio/"):
		mtype, url = "audio", None
	else:
		mtype, url = "document", None
	return {"message_type": mtype, "media_url": url or att.get("file_url"), "media_mime": mime or None}


def thread_events(thread: dict[str, Any], channel: str, own_ids: set[str]) -> list[dict[str, Any]]:
	"""History events for one Conversations API thread. Pure; ``[]`` when unusable."""
	own = {str(i) for i in own_ids or set() if i}
	people = (thread.get("participants") or {}).get("data") or []
	customer = next((p for p in people if str((p or {}).get("id") or "") not in own), None)
	if not customer or not customer.get("id"):
		return []
	cid = str(customer["id"])
	name = str(customer.get("name") or customer.get("username") or "").strip() or None
	events: list[dict[str, Any]] = []
	for msg in (thread.get("messages") or {}).get("data") or []:
		msg = msg or {}
		if not msg.get("id"):
			continue
		inbound = str((msg.get("from") or {}).get("id") or "") == cid
		text = str(msg.get("message") or "")
		content: dict[str, Any] = {"message_type": "text", "body": text, "raw": None}
		attachments = (msg.get("attachments") or {}).get("data") or []
		if attachments:
			content.update(_attachment_content(attachments[0] or {}))
		elif not text:
			content.update({"message_type": "unsupported", "raw": {"type": "unsupported"}})
		events.append(
			ing._event(
				channel=channel,
				external_id=cid,
				contact_name=name if inbound else None,
				direction="Inbound" if inbound else "Outbound",
				sent_via=None if inbound else "Phone",
				external_message_id=str(msg["id"]),
				timestamp=_iso_epoch(msg.get("created_time")),
				history=True,
				**content,
			)
		)
	return events


def explain_sync_error(code: Any, message: str) -> str:
	return SYNC_ERRORS.get(str(code or "").split("/")[0], message)


# ── WhatsApp ────────────────────────────────────────────────────────────


def request_whatsapp_history() -> dict[str, Any]:
	"""Ask Meta for the business app's contacts, then its past chats. Never raises."""
	from jarz_pos.services import chat_meta_client as meta

	out: dict[str, Any] = {}
	# Contacts first, so names are cached before the chats that use them arrive.
	for sync_type in ("smb_app_state_sync", "history"):
		try:
			data = meta.wa_request_app_sync(sync_type)
			out[sync_type] = {"ok": True, "request_id": data.get("request_id")}
		except meta.MetaApiError as exc:
			out[sync_type] = {
				"ok": False,
				"code": exc.code,
				"error": explain_sync_error(exc.code, exc.message),
			}
	return out


# ── Messenger / Instagram ───────────────────────────────────────────────


def _store_result(channel: str, result: dict[str, Any]) -> None:
	try:
		frappe.cache().set(
			frappe.cache().make_key(f"{BACKFILL_RESULT_KEY}:{channel}"),
			json.dumps({**result, "at": cc.dt_str(cc.now())}),
			ex=RESULT_TTL_SECONDS,
		)
	except Exception:
		pass


def backfill_page_history(channel: str, max_threads: int = MAX_THREADS) -> dict[str, Any]:
	"""Background job: import the Page's past Messenger or Instagram threads. Never raises.

	Safe to run again: messages are keyed on Meta's message id, so a second run
	only adds what is new.
	"""
	result: dict[str, Any] = {"channel": channel, "threads": 0, "messages": 0, "errors": 0}
	try:
		if frappe.session.user in (None, "", "Guest"):
			# nosemgrep: frappe-setuser -- background job (RQ worker) enqueued by a System Manager; worker calls frappe.destroy() after every job
			frappe.set_user("Administrator")
		from jarz_pos.services import chat_meta_client as meta
		from jarz_pos.services.chat_settings import get_chat_settings

		platform = PLATFORMS.get(channel)
		if not platform:
			raise ValueError(f"No history import for channel {channel}")
		settings = get_chat_settings(refresh=True)
		own = ing._own_ids(settings)
		tz = ing._site_tz()
		after = None
		while result["threads"] < int(max_threads):
			data = meta.page_conversations(platform, after=after, limit=PAGE_SIZE)
			threads = data.get("data") or []
			for thread in threads:
				result["threads"] += 1
				for index, event in enumerate(thread_events(thread or {}, channel, own)):
					savepoint = f"chat_hist_{index}"
					try:
						frappe.db.savepoint(savepoint)
						if ing._persist_message(event, settings, tz):
							result["messages"] += 1
					except Exception:
						result["errors"] += 1
						try:
							frappe.db.rollback(save_point=savepoint)
						except Exception:
							pass
				frappe.db.commit()
			paging = data.get("paging") or {}
			after = (paging.get("cursors") or {}).get("after")
			if not threads or not after or not paging.get("next"):
				break
		result["status"] = "done"
	except Exception as exc:
		try:
			frappe.db.rollback()
		except Exception:
			pass
		result["status"] = "failed"
		result["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
		cc.safe_log_error("chat: history backfill failed", f"channel={channel} {result['error']}")
	_store_result(channel, result)
	return result


def history_status() -> dict[str, Any]:
	"""Latest WhatsApp sync step and the last Messenger / Instagram backfill results."""
	out: dict[str, Any] = {}
	keys = {"whatsapp": ing.HISTORY_PROGRESS_KEY}
	keys.update({channel: f"{BACKFILL_RESULT_KEY}:{channel}" for channel in PLATFORMS})
	for label, key in keys.items():
		try:
			raw = frappe.cache().get(frappe.cache().make_key(key))
			if isinstance(raw, bytes):
				raw = raw.decode("utf-8", "ignore")
			out[label] = json.loads(raw) if raw else None
		except Exception:
			out[label] = None
	return out
