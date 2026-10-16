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
	method: str, path_or_url: str, token: str, *, json_body: dict | None = None, params: dict | None = None
) -> dict[str, Any]:
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
			timeout=TIMEOUT,
		)
	except requests.RequestException as exc:
		raise MetaApiError(f"Meta API unreachable: {type(exc).__name__}", "network") from None
	if resp.status_code >= 400:
		raise _error_from_response(resp)
	try:
		return resp.json() or {}
	except Exception:
		return {}


def _wa_token() -> str:
	return _settings().get("whatsapp_access_token") or ""


def _page_token() -> str:
	return _settings().get("page_access_token") or ""


# ── WhatsApp Cloud API ──────────────────────────────────────────────────


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
			"to": str(to or "").lstrip("+"),
			"type": "text",
			"text": {"preview_url": False, "body": text},
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
