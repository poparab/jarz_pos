"""Thin Meta Graph API client for the chat inbox (WhatsApp Cloud, Messenger, Instagram).

One function per Graph call the inbox makes, nothing else. Every call:

* sends the token as an ``Authorization: Bearer`` header, never in the URL, so
  a logged URL can never carry a credential;
* times out after :data:`TIMEOUT` seconds;
* raises :class:`MetaApiError` with Meta's own message and code on any failure,
  so ``chat_send`` can store a readable error on the message row.

Error text is built from Meta's ``error.message`` / ``error.code`` only. The
request body (customer text, phone numbers) is never put into an exception.
"""

from __future__ import annotations

from typing import Any

import requests

from jarz_pos.services.chat_settings import get_chat_settings

GRAPH_BASE = "https://graph.facebook.com"
TIMEOUT = 15
#: Uploading a video of up to 25 MB needs longer than a plain call.
UPLOAD_TIMEOUT = 120
#: Hard cap on a downloaded attachment (contract: 16 MB).
MAX_MEDIA_BYTES = 16 * 1024 * 1024
HUMAN_AGENT_TAG = "HUMAN_AGENT"


class MetaApiError(Exception):
	"""A Graph API call failed. ``code`` is Meta's error code (or the HTTP status)."""

	def __init__(self, message: str, code: Any = None):
		super().__init__(message)
		self.message = message
		self.code = code

	def __str__(self) -> str:  # pragma: no cover - trivial
		return f"{self.message} (code {self.code})" if self.code not in (None, "") else self.message


def _settings() -> dict[str, Any]:
	return get_chat_settings()


def _url(path: str) -> str:
	version = _settings().get("graph_api_version") or "v24.0"
	return f"{GRAPH_BASE}/{version}/{path.lstrip('/')}"


def _error_from_response(resp: Any) -> MetaApiError:
	code: Any = getattr(resp, "status_code", None)
	message = f"Meta API HTTP {code}"
	try:
		data = resp.json()
		err = (data or {}).get("error") or {}
		if err:
			message = str(err.get("error_user_msg") or err.get("message") or message)[:500]
			code = err.get("code", code)
			sub = err.get("error_subcode")
			if sub:
				code = f"{code}/{sub}"
	except Exception:
		pass
	return MetaApiError(message, code)


def _request(
	method: str,
	path_or_url: str,
	token: str,
	*,
	json_body: dict | None = None,
	params: dict | None = None,
	form: dict | None = None,
	files: dict | None = None,
) -> dict[str, Any]:
	"""One Graph call. ``form`` / ``files`` make it a multipart upload (``requests`` sets the boundary)."""
	if str(method).upper() != "GET":
		# GETs only read (history, media info, contact names) and cannot message a customer.
		_require_chat_enabled()
	if not token:
		raise MetaApiError("Meta access token is not configured in Jarz Chat Settings", "config")
	url = path_or_url if path_or_url.startswith("http") else _url(path_or_url)
	try:
		resp = requests.request(
			method,
			url,
			headers={"Authorization": f"Bearer {token}"},
			json=json_body,
			params=params,
			data=form,
			files=files,
			timeout=UPLOAD_TIMEOUT if files else TIMEOUT,
		)
	except requests.RequestException as exc:
		raise MetaApiError(f"Meta API unreachable: {type(exc).__name__}", "network") from None
	if resp.status_code >= 400:
		raise _error_from_response(resp)
	try:
		return resp.json() or {}
	except Exception:
		return {}


CHAT_OFF_MESSAGE = "Customer chat is switched off on this site"


def _require_chat_enabled() -> None:
	"""Kill switch: refuse any outbound write while Jarz Chat Settings.enabled is off.

	Every send, media upload, reaction, template, read receipt, comment reply / edit / delete
	and app-sync call goes through :func:`_request`, so one check there keeps a site that
	still stores a real token (staging) from ever reaching a customer. Raised before any
	network call.
	"""
	if not _settings().get("enabled"):
		raise MetaApiError(CHAT_OFF_MESSAGE, "chat_disabled")


def _wa_token() -> str:
	return _settings().get("whatsapp_access_token") or ""


def _page_token() -> str:
	return _settings().get("page_access_token") or ""


# ── WhatsApp Cloud API ──────────────────────────────────────────────────


def wa_address(to: Any) -> dict[str, str]:
	"""Who a WhatsApp send goes to: ``{"to": <phone>}`` or ``{"recipient": <user id>}``.

	A customer who hides their number reaches us with a business-scoped user id such as
	``EG.1234567890123456`` instead of a phone. Meta reads ``to`` as a phone number only, so
	that id there became the digits after ``EG.`` and failed with 131026 "Message
	undeliverable"; the id has to go in ``recipient`` (and ``to`` must be absent, since Meta
	uses ``to`` when both are sent).
	"""
	value = str(to or "").strip()
	digits = value.lstrip("+").replace(" ", "").replace("-", "")
	if digits.isdigit():
		return {"to": digits}
	return {"recipient": value}


def wa_send_text(to: str, text: str) -> str:
	"""Send a free-form text inside the 24h window. Returns the wamid."""
	phone_id = _settings().get("whatsapp_phone_number_id")
	if not phone_id:
		raise MetaApiError("WhatsApp phone number id is not configured", "config")
	data = _request(
		"POST",
		f"{phone_id}/messages",
		_wa_token(),
		json_body={
			"messaging_product": "whatsapp",
			"recipient_type": "individual",
			**wa_address(to),
			"type": "text",
			"text": {"preview_url": False, "body": text},
		},
	)
	messages = data.get("messages") or []
	wamid = (messages[0] or {}).get("id") if messages else None
	if not wamid:
		raise MetaApiError("WhatsApp accepted the call but returned no message id", "no_id")
	return str(wamid)


def _wa_phone_id() -> str:
	phone_id = _settings().get("whatsapp_phone_number_id")
	if not phone_id:
		raise MetaApiError("WhatsApp phone number id is not configured", "config")
	return str(phone_id)


def wa_upload_media(content: bytes, filename: str, mime: str) -> str:
	"""Upload a file to WhatsApp (``POST /<phone_number_id>/media``). Returns the media id."""
	data = _request(
		"POST",
		f"{_wa_phone_id()}/media",
		_wa_token(),
		form={"messaging_product": "whatsapp", "type": mime},
		files={"file": (filename, content, mime)},
	)
	media_id = data.get("id")
	if not media_id:
		raise MetaApiError("WhatsApp accepted the upload but returned no media id", "no_id")
	return str(media_id)


def wa_media_payload(media_type: str, media_id: str, caption: str | None, filename: str | None) -> dict:
	"""The type-specific object of a WhatsApp media message. Audio takes no caption."""
	payload: dict[str, Any] = {"id": media_id}
	if media_type != "audio" and caption:
		payload["caption"] = caption
	if media_type == "document" and filename:
		payload["filename"] = filename
	return payload


def wa_send_media(
	to: str, media_type: str, media_id: str, caption: str | None = None, filename: str | None = None
) -> str:
	"""Send an uploaded image / video / audio / document inside the 24h window. Returns the wamid."""
	data = _request(
		"POST",
		f"{_wa_phone_id()}/messages",
		_wa_token(),
		json_body={
			"messaging_product": "whatsapp",
			"recipient_type": "individual",
			**wa_address(to),
			"type": media_type,
			media_type: wa_media_payload(media_type, media_id, caption, filename),
		},
	)
	messages = data.get("messages") or []
	wamid = (messages[0] or {}).get("id") if messages else None
	if not wamid:
		raise MetaApiError("WhatsApp accepted the call but returned no message id", "no_id")
	return str(wamid)


def wa_send_reaction(to: str, wamid: str, emoji: str) -> str | None:
	"""React to the customer's message ``wamid``; an empty ``emoji`` removes our reaction.

	Returns the reaction's own message id (WhatsApp sends status updates for it), or ``None``.
	"""
	data = _request(
		"POST",
		f"{_wa_phone_id()}/messages",
		_wa_token(),
		json_body={
			"messaging_product": "whatsapp",
			"recipient_type": "individual",
			**wa_address(to),
			"type": "reaction",
			"reaction": {"message_id": wamid, "emoji": emoji},
		},
	)
	messages = (data.get("messages") or []) if isinstance(data, dict) else []
	own = (messages[0] or {}).get("id") if messages else None
	return str(own) if own else None


def wa_send_template(to: str, name: str, language: str, components: list[dict[str, Any]]) -> str:
	"""Send an approved template (opens a conversation outside the 24h window). Returns the wamid."""
	phone_id = _settings().get("whatsapp_phone_number_id")
	if not phone_id:
		raise MetaApiError("WhatsApp phone number id is not configured", "config")
	data = _request(
		"POST",
		f"{phone_id}/messages",
		_wa_token(),
		json_body={
			"messaging_product": "whatsapp",
			"recipient_type": "individual",
			**wa_address(to),
			"type": "template",
			"template": {"name": name, "language": {"code": language}, "components": components},
		},
	)
	messages = data.get("messages") or []
	wamid = (messages[0] or {}).get("id") if messages else None
	if not wamid:
		raise MetaApiError("WhatsApp accepted the call but returned no message id", "no_id")
	return str(wamid)


def wa_mark_read(wamid: str) -> bool:
	"""Blue ticks for one inbound message. Best effort: returns False on failure."""
	phone_id = _settings().get("whatsapp_phone_number_id")
	if not phone_id or not wamid:
		return False
	try:
		_request(
			"POST",
			f"{phone_id}/messages",
			_wa_token(),
			json_body={"messaging_product": "whatsapp", "status": "read", "message_id": wamid},
		)
		return True
	except MetaApiError:
		return False


def wa_media_info(media_id: str) -> dict[str, Any]:
	"""``{url, mime_type, file_size, sha256}`` for a WhatsApp media id."""
	return _request("GET", str(media_id), _wa_token())


def wa_request_app_sync(sync_type: str) -> dict[str, Any]:
	"""Ask Meta to send the WhatsApp Business app's past chats or contacts.

	``sync_type`` is ``history`` or ``smb_app_state_sync``. Coexistence numbers
	only, once each, within 24 h of onboarding (errors 2593107 / 2593108); the
	data then arrives on the webhook.
	"""
	phone_id = _settings().get("whatsapp_phone_number_id")
	if not phone_id:
		raise MetaApiError("WhatsApp phone number id is not configured in Jarz Chat Settings", "config")
	return _request(
		"POST",
		f"{phone_id}/smb_app_data",
		_wa_token(),
		json_body={"messaging_product": "whatsapp", "sync_type": sync_type},
	)


#: Messages per thread the Conversations API returns with full detail.
PAGE_THREAD_MESSAGES = 20


def page_conversations(platform: str, after: str | None = None, limit: int = 25) -> dict[str, Any]:
	"""One page of the Facebook Page's Messenger or Instagram threads, newest first.

	Each thread carries its participants and its last :data:`PAGE_THREAD_MESSAGES`
	messages, which is all the detail Meta gives for older messages.
	"""
	page_id = _settings().get("facebook_page_id")
	if not page_id:
		raise MetaApiError("Facebook Page id is not configured in Jarz Chat Settings", "config")
	params = {
		"platform": platform,
		"limit": int(limit),
		"fields": "participants,updated_time,"
		f"messages.limit({PAGE_THREAD_MESSAGES})"
		"{id,created_time,from,to,message,attachments{mime_type,name,image_data,video_data,file_url}}",
	}
	if after:
		params["after"] = after
	return _request("GET", f"{page_id}/conversations", _page_token(), params=params)


def dm_contact_name(platform: str, user_id: str) -> str | None:
	"""The display name of a Messenger / Instagram DM customer, or ``None``.

	DM webhooks carry only the PSID / IGSID. Messenger refuses ``GET /{psid}``
	without the Business Asset User Profile Access feature, but the Page's own
	thread with that person lists them as a participant. Instagram answers
	``GET /{igsid}`` with ``name`` and ``username``.
	"""
	if platform == "instagram":
		data = _request("GET", str(user_id), _page_token(), params={"fields": "name,username"})
		return str(data.get("name") or data.get("username") or "").strip() or None
	page_id = _settings().get("facebook_page_id")
	if not page_id:
		raise MetaApiError("Facebook Page id is not configured in Jarz Chat Settings", "config")
	data = _request(
		"GET",
		f"{page_id}/conversations",
		_page_token(),
		params={"platform": "messenger", "user_id": str(user_id), "fields": "participants"},
	)
	for thread in data.get("data") or []:
		for person in (thread.get("participants") or {}).get("data") or []:
			if str(person.get("id")) == str(user_id):
				return str(person.get("name") or "").strip() or None
	return None


def download(url: str, token: str | None = None) -> tuple[bytes, str]:
	"""Fetch a media URL. Returns ``(content, content_type)``.

	WhatsApp media URLs need the bearer token; Instagram / Messenger CDN URLs
	are pre-signed and must NOT get it (``token=None``). Streams and stops at
	:data:`MAX_MEDIA_BYTES`.
	"""
	headers = {"Authorization": f"Bearer {token}"} if token else {}
	try:
		resp = requests.get(url, headers=headers, timeout=TIMEOUT, stream=True)
	except requests.RequestException as exc:
		raise MetaApiError(f"Media download failed: {type(exc).__name__}", "network") from None
	try:
		if resp.status_code >= 400:
			raise MetaApiError(f"Media download HTTP {resp.status_code}", resp.status_code)
		length = resp.headers.get("Content-Length")
		if length and str(length).isdigit() and int(length) > MAX_MEDIA_BYTES:
			raise MetaApiError("Media is larger than 16 MB", "too_large")
		chunks: list[bytes] = []
		total = 0
		for chunk in resp.iter_content(chunk_size=65536):
			if not chunk:
				continue
			total += len(chunk)
			if total > MAX_MEDIA_BYTES:
				raise MetaApiError("Media is larger than 16 MB", "too_large")
			chunks.append(chunk)
		content_type = str(resp.headers.get("Content-Type") or "").split(";")[0].strip()
		return b"".join(chunks), content_type
	finally:
		try:
			resp.close()
		except Exception:
			pass


# ── Messenger / Instagram ───────────────────────────────────────────────


def _send_api_body(recipient: dict[str, Any], text: str, tag: str | None) -> dict[str, Any]:
	body: dict[str, Any] = {"recipient": recipient, "message": {"text": text}}
	if tag:
		body["messaging_type"] = "MESSAGE_TAG"
		body["tag"] = tag
	else:
		body["messaging_type"] = "RESPONSE"
	return body


def _send_api(recipient: dict[str, Any], text: str, tag: str | None, instagram: bool) -> str:
	settings = _settings()
	# Instagram messaging on the Messenger Platform (Facebook Login, Page token)
	# goes through the linked Page's /messages edge, exactly like Messenger. The
	# IG account id is only the fallback for an Instagram-Login token.
	account = settings.get("facebook_page_id")
	if instagram and not account:
		account = settings.get("instagram_account_id")
	data = _request(
		"POST",
		f"{account or 'me'}/messages",
		_page_token(),
		json_body=_send_api_body(recipient, text, tag),
	)
	mid = data.get("message_id") or data.get("id")
	if not mid:
		raise MetaApiError("Meta accepted the call but returned no message id", "no_id")
	return str(mid)


def messenger_send(psid: str, text: str, tag: str | None = None) -> str:
	"""Send a Messenger reply. ``tag="HUMAN_AGENT"`` between 24h and 7 days."""
	return _send_api({"id": psid}, text, tag, instagram=False)


def ig_send(igsid: str, text: str, tag: str | None = None) -> str:
	"""Send an Instagram DM reply. ``tag="HUMAN_AGENT"`` between 24h and 7 days."""
	return _send_api({"id": igsid}, text, tag, instagram=True)


def _dm_account(instagram: bool) -> str:
	"""The edge account for a DM call: the linked Page for both (see :func:`_send_api`)."""
	settings = _settings()
	account = settings.get("facebook_page_id")
	if instagram and not account:
		account = settings.get("instagram_account_id")
	return str(account or "me")


def _dm_mid(data: dict[str, Any]) -> str:
	mid = data.get("message_id") or data.get("id")
	if not mid:
		raise MetaApiError("Meta accepted the call but returned no message id", "no_id")
	return str(mid)


def messenger_send_attachment(
	psid: str, attachment_type: str, content: bytes, filename: str, mime: str, tag: str | None = None
) -> str:
	"""Upload-and-send a file to a Messenger user (multipart ``filedata``). Returns the mid.

	``attachment_type`` is Meta's ``image|video|audio|file``. The Page's echo webhook later
	carries this same mid, which is how the inbox de-duplicates it.
	"""
	import json

	form = {
		"recipient": json.dumps({"id": psid}),
		"message": json.dumps({"attachment": {"type": attachment_type, "payload": {"is_reusable": False}}}),
		"messaging_type": "MESSAGE_TAG" if tag else "RESPONSE",
	}
	if tag:
		form["tag"] = tag
	data = _request(
		"POST",
		f"{_dm_account(False)}/messages",
		_page_token(),
		form=form,
		files={"filedata": (filename, content, mime)},
	)
	return _dm_mid(data)


def ig_send_attachment_url(igsid: str, attachment_type: str, url: str, tag: str | None = None) -> str:
	"""Send an Instagram DM attachment by public URL (``image|video|audio``). Returns the mid."""
	body: dict[str, Any] = {
		"recipient": {"id": igsid},
		"message": {"attachment": {"type": attachment_type, "payload": {"url": url}}},
		"messaging_type": "MESSAGE_TAG" if tag else "RESPONSE",
	}
	if tag:
		body["tag"] = tag
	return _dm_mid(_request("POST", f"{_dm_account(True)}/messages", _page_token(), json_body=body))


def dm_send_reaction(
	user_id: str, mid: str, reaction: str | None, instagram: bool = False
) -> None:
	"""React to (``reaction="love"``) or un-react (``None``) a customer's DM ``mid``."""
	body: dict[str, Any] = {"recipient": {"id": user_id}}
	if reaction:
		body["sender_action"] = "react"
		body["payload"] = {"message_id": mid, "reaction": reaction}
	else:
		body["sender_action"] = "unreact"
		body["payload"] = {"message_id": mid}
	_request("POST", f"{_dm_account(instagram)}/messages", _page_token(), json_body=body)


def comment_private_reply(comment_id: str, text: str, instagram: bool = False) -> str:
	"""Private (DM) reply to a public comment. Meta allows one per comment, within 7 days."""
	return _send_api({"comment_id": comment_id}, text, None, instagram=instagram)


def comment_public_reply(comment_id: str, text: str, instagram: bool = False) -> str:
	"""Public reply under a comment. Facebook: ``/{id}/comments``; Instagram: ``/{id}/replies``."""
	edge = "replies" if instagram else "comments"
	data = _request("POST", f"{comment_id}/{edge}", _page_token(), json_body={"message": text})
	cid = data.get("id")
	if not cid:
		raise MetaApiError("Meta accepted the reply but returned no comment id", "no_id")
	return str(cid)


def delete_comment(comment_id: str, instagram: bool = False) -> None:
	"""Delete a comment our Page / account wrote: ``DELETE /{comment_id}`` with the Page token.

	Same token as :func:`comment_public_reply`. Facebook and Instagram both use the same edge.
	"""
	_request("DELETE", f"{comment_id}", _page_token())


def edit_comment(comment_id: str, text: str) -> None:
	"""Edit a Facebook comment our Page wrote: ``POST /{comment_id}`` with ``message``.

	Instagram's Graph API has no comment edit; callers refuse it before getting here.
	"""
	_request("POST", f"{comment_id}", _page_token(), json_body={"message": text})
