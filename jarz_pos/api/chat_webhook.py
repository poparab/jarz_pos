"""Meta webhook endpoint for the chat inbox (WhatsApp Cloud API, Messenger, Instagram).

URL: ``/api/method/jarz_pos.api.chat_webhook.meta_webhook``

**Guest-callable, read this before changing anything.** Anyone on the internet
can reach it. It is authorised solely by:

* ``GET``: Meta's subscription handshake, ``hub.verify_token`` compared in
  constant time against the configured verify token;
* ``POST``: the ``X-Hub-Signature-256`` HMAC of the raw body with the Meta App
  Secret, compared in constant time. A missing secret, a missing header or a
  wrong signature is a 403. Nothing is parsed or stored before that check.

A valid POST is only enqueued (``services.chat_ingest.process_webhook``) and
answered ``EVENT_RECEIVED`` at once: Meta expects a 200 within seconds and
retries otherwise.

The function takes no parameters on purpose: ``frappe.call`` drops form keys a
signature does not declare, and the HMAC must be computed over the exact raw
bytes, so everything is read from ``frappe.request`` directly. Never log the
body: it carries customer phone numbers and messages.
"""

from __future__ import annotations

import json

import frappe

from jarz_pos.services import chat_ingest
from jarz_pos.services.chat_common import safe_log_error
from jarz_pos.services.chat_settings import get_chat_settings

_BAD_SIGNATURE_THROTTLE_KEY = "jarz_chat_webhook_bad_signature"
_BAD_SIGNATURE_THROTTLE_SECONDS = 600


def _response(body: str, status: int = 200):
	from werkzeug.wrappers import Response

	return Response(body, status=status, mimetype="text/plain")


def _log_rejection(reason: str, has_header: bool, length: int) -> None:
	"""Throttled, body-free record of a refused POST."""
	try:
		cache = frappe.cache()
		if cache.get_value(_BAD_SIGNATURE_THROTTLE_KEY):
			return
		cache.set_value(_BAD_SIGNATURE_THROTTLE_KEY, 1, expires_in_sec=_BAD_SIGNATURE_THROTTLE_SECONDS)
	except Exception:
		pass
	safe_log_error(
		"chat webhook: request refused",
		f"reason={reason} signature_header={'yes' if has_header else 'no'} body_bytes={length}",
	)


# nosemgrep: guest-whitelisted-method -- Meta webhook: every POST is authorised by the X-Hub-Signature-256 HMAC and body-size cap; GET only answers the verify-token handshake
@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
def meta_webhook():
	"""Meta subscription handshake (GET) and event delivery (POST)."""
	request = frappe.request
	settings = get_chat_settings()

	if request.method == "GET":
		challenge = chat_ingest.verify_subscription(request.args, settings.get("webhook_verify_token"))
		if challenge is None:
			return _response("Forbidden", 403)
		return _response(challenge, 200)

	if not settings.get("enabled"):
		# Acknowledge and drop: a 4xx would make Meta retry and eventually
		# disable the subscription while chat is merely switched off.
		return _response("EVENT_RECEIVED", 200)

	length = int(request.content_length or 0)
	if length > chat_ingest.MAX_BODY_BYTES:
		_log_rejection("body_too_large", bool(request.headers.get("X-Hub-Signature-256")), length)
		return _response("Payload Too Large", 413)

	body = request.get_data(cache=True) or b""
	header = request.headers.get("X-Hub-Signature-256")
	if not chat_ingest.verify_signature(body, header, settings.get("meta_app_secret")):
		reason = "no_secret" if not settings.get("meta_app_secret") else "bad_signature"
		_log_rejection(reason, bool(header), len(body))
		return _response("Forbidden", 403)

	try:
		payload = json.loads(body.decode("utf-8"))
	except Exception:
		_log_rejection("invalid_json", True, len(body))
		return _response("EVENT_RECEIVED", 200)

	try:
		# nosemgrep: whitelisted-side-effect-on-get -- GET is Meta's verify handshake and returns before this line; the enqueue runs only for HMAC-signed POSTs
		frappe.enqueue(
			"jarz_pos.services.chat_ingest.process_webhook",
			queue="short",
			timeout=300,
			payload=payload,
			enqueue_after_commit=False,
		)
	except Exception:
		safe_log_error(
			"chat webhook: enqueue failed",
			f"object={payload.get('object') if isinstance(payload, dict) else '?'}",
		)
		# 500 so Meta retries the delivery instead of losing it.
		return _response("Retry", 500)
	return _response("EVENT_RECEIVED", 200)
