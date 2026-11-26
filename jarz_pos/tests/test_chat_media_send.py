"""Staff media send + reactions (services/chat_media_send.py, chat_meta_client, chat_ingest) -- mocked, no site.

Pins: type detection and per-channel caps, comment / Instagram-document refusals, the exact Graph
call shape per channel (WhatsApp upload + send, Messenger multipart, Instagram URL, reactions),
the Instagram token link, a failed Meta call keeping a ``failed`` row, react_to_message validation,
customer reactions attaching to their target (and the fallbacks), get_conversation's new fields and
load_message before ``bench migrate`` (the reaction columns do not exist yet).

Plain ``unittest.TestCase``; a stub ``frappe`` is registered when frappe is not installed.
"""

import datetime
import json
import sys
import types
import unittest
from types import SimpleNamespace
from unittest import mock

try:  # pragma: no cover - depends on the environment
	import frappe
except ImportError:  # pragma: no cover
	frappe = types.ModuleType("frappe")
	frappe.whitelist = lambda *a, **k: (a[0] if a and callable(a[0]) else (lambda fn: fn))
	frappe._ = lambda m: m
	frappe.local = SimpleNamespace()
	frappe.session = SimpleNamespace(user="Guest")
	frappe.db = SimpleNamespace()
	frappe.log_error = lambda *a, **k: None
	frappe.get_traceback = lambda *a, **k: ""
	sys.modules["frappe"] = frappe
	_utils = types.ModuleType("frappe.utils")
	_utils.get_datetime = lambda d=None: d
	_utils.now_datetime = lambda: datetime.datetime(2026, 11, 16, 18, 0)
	_utils.nowdate = lambda: "2026-11-16"
	_utils.get_url = lambda *a, **k: "https://erp.example.com"
	sys.modules["frappe.utils"] = _utils
	frappe.utils = _utils

from jarz_pos.api import chat as api
from jarz_pos.services import chat_common as cc
from jarz_pos.services import chat_ingest as ing
from jarz_pos.services import chat_media_send as ms
from jarz_pos.services import chat_meta_client as meta
from jarz_pos.services import chat_send as cs

MB = 1024 * 1024
NOW = datetime.datetime(2026, 11, 16, 18, 0)
H = datetime.timedelta(hours=1)


class Refused(Exception):
	"""What the patched ``frappe.throw`` raises."""


class _D(dict):
	__getattr__ = dict.get


def _throw(msg, exc=None, *a, **k):
	raise Refused(str(msg))


def conv(channel=cc.WHATSAPP, **over):
	base = {
		"name": "CONV1",
		"channel": channel,
		"external_id": "201111034268" if channel == cc.WHATSAPP else "PSID1",
		"status": "Open",
		"assigned_to": None,
		"window_expires_at": NOW + 2 * H,
		"last_inbound_at": NOW - 2 * H,
		"private_reply_sent": 0,
	}
	base.update(over)
	return _D(base)


class FakeCache:
	def __init__(self):
		self.store = {}

	def make_key(self, key):
		return f"site|{key}"

	def set(self, key, value, ex=None):
		self.store[key] = value.encode() if isinstance(value, str) else value

	def get(self, key):
		return self.store.get(key)


class Harness(unittest.TestCase):
	"""Patches frappe + the common helpers; ``self.db`` records writes."""

	def setUp(self):
		self.sets = []
		self.docs = []
		self.rows = {}
		self.published = []
		self.cache = FakeCache()
		self.has_cols = True

		def get_value(doctype, name=None, fields=None, as_dict=False):
			if isinstance(name, dict):
				return self.rows.get((doctype, "filter"))
			return self.rows.get((doctype, name))

		def set_value(doctype, name, field, value=None, update_modified=True):
			self.sets.append((doctype, name, field, value))

		def get_doc(values, name=None):
			if isinstance(values, str):  # get_doc("File", name)
				return SimpleNamespace(get_content=lambda: b"BYTES")
			doc = SimpleNamespace(**values) if isinstance(values, dict) else values
			doc.name = f"{values.get('doctype', 'X')}-{len(self.docs) + 1}"
			if values.get("doctype") == "File":
				doc.file_url = "/private/files/" + values["file_name"]
			doc.insert = lambda **k: None
			doc.get_content = lambda: b"BYTES"
			self.docs.append(values)
			return doc

		db = SimpleNamespace(get_value=get_value, set_value=set_value, commit=lambda: None)
		patches = [
			mock.patch.object(frappe, "db", db, create=True),
			mock.patch.object(frappe, "throw", _throw, create=True),
			mock.patch.object(frappe, "get_doc", get_doc, create=True),
			mock.patch.object(frappe, "cache", lambda: self.cache, create=True),
			mock.patch.object(frappe, "DoesNotExistError", Refused, create=True),
			mock.patch.object(cc, "now", lambda: NOW),
			mock.patch.object(cc, "has_reaction_columns", lambda: self.has_cols),
			mock.patch.object(
				cc, "load_message", lambda name: {"name": name, "status": self._status(name)}
			),
			mock.patch.object(cc, "publish_chat_update", lambda *a, **k: self.published.append((a, k))),
			mock.patch.object(cc, "supersede_live_drafts", lambda *a, **k: []),
			mock.patch.object(meta, "_settings", lambda: {"enabled": True, "whatsapp_phone_number_id": "PN1"}),
		]
		for p in patches:
			p.start()
			self.addCleanup(p.stop)

	def _status(self, name):
		for _doctype, n, field, _value in reversed(self.sets):
			if n == name and isinstance(field, dict) and "status" in field:
				return field["status"]
		return None

	def with_conv(self, c):
		self.rows[(cc.CONVERSATION, c.name)] = c

	def message_sets(self):
		return [s for s in self.sets if s[0] == cc.MESSAGE]


# ── pure rules ──────────────────────────────────────────────────────────


class DetectAndCapsTest(Harness):
	def test_type_by_mime_then_extension(self):
		self.assertEqual(ms.media_type_for("image/png"), "image")
		self.assertEqual(ms.media_type_for("video/mp4"), "video")
		self.assertEqual(ms.media_type_for("audio/mp4"), "audio")
		self.assertEqual(ms.media_type_for("application/pdf"), "document")
		self.assertEqual(ms.normalize_mime("application/octet-stream", "note.m4a"), "audio/mp4")
		self.assertEqual(ms.normalize_mime("", "photo.JPG"), "image/jpeg")
		self.assertEqual(ms.normalize_mime("audio/ogg; codecs=opus", "x"), "audio/ogg")
		self.assertEqual(ms.normalize_mime("audio/mp3", "x"), "audio/mpeg")

	def test_whatsapp_caps_per_type(self):
		self.assertEqual(ms.max_bytes(cc.WHATSAPP, "image"), 5 * MB)
		self.assertEqual(ms.max_bytes(cc.WHATSAPP, "video"), 16 * MB)
		self.assertEqual(ms.max_bytes(cc.WHATSAPP, "audio"), 16 * MB)
		self.assertEqual(ms.max_bytes(cc.WHATSAPP, "document"), 25 * MB)
		self.assertEqual(ms.max_bytes(cc.MESSENGER, "document"), 25 * MB)
		self.assertEqual(ms.max_bytes(cc.INSTAGRAM, "image"), 5 * MB)

	def test_ok_and_too_big(self):
		self.assertEqual(ms.validate_media(cc.WHATSAPP, "a.jpg", "image/jpeg", 5 * MB), ("image", "image/jpeg"))
		with self.assertRaises(Refused) as ctx:
			ms.validate_media(cc.WHATSAPP, "a.jpg", "image/jpeg", 5 * MB + 1)
		self.assertIn("too big", str(ctx.exception))
		self.assertEqual(ms.validate_media(cc.MESSENGER, "v.mp4", "video/mp4", 16 * MB)[0], "video")
		with self.assertRaises(Refused):
			ms.validate_media(cc.MESSENGER, "v.mp4", "video/mp4", 17 * MB)
		with self.assertRaises(Refused):
			ms.validate_media(cc.MESSENGER, "a.pdf", "application/pdf", 26 * MB)

	def test_whatsapp_refuses_unlisted_types(self):
		for mime in ("image/webp", "image/heic", "video/webm", "audio/wav"):
			with self.assertRaises(Refused, msg=mime):
				ms.validate_media(cc.WHATSAPP, "f", mime, 1000)
		self.assertEqual(ms.validate_media(cc.WHATSAPP, "x.zip", "application/zip", 1000)[0], "document")
		self.assertEqual(ms.validate_media(cc.WHATSAPP, "v.3gp", "video/3gpp", 1000)[0], "video")

	def test_comment_channels_and_unknown_refused(self):
		for channel in (cc.FACEBOOK_COMMENT, cc.INSTAGRAM_COMMENT, "Telegram"):
			with self.assertRaises(Refused):
				ms.validate_media(channel, "a.jpg", "image/jpeg", 1000)

	def test_instagram_document_refused(self):
		with self.assertRaises(Refused) as ctx:
			ms.validate_media(cc.INSTAGRAM, "menu.pdf", "application/pdf", 1000)
		self.assertIn("Instagram", str(ctx.exception))
		self.assertEqual(ms.validate_media(cc.INSTAGRAM, "a.png", "image/png", 1000)[0], "image")

	def test_empty_file_refused(self):
		with self.assertRaises(Refused):
			ms.validate_media(cc.WHATSAPP, "a.jpg", "image/jpeg", 0)

	def test_body_rules(self):
		self.assertEqual(ms.stored_body("audio", "v.m4a", "ignored"), "")
		self.assertEqual(ms.stored_body("image", "a.jpg", "nice"), "nice")
		self.assertEqual(ms.stored_body("document", "menu.pdf", "see"), "menu.pdf\nsee")
		self.assertEqual(ms.stored_body("document", "menu.pdf", ""), "menu.pdf")

	def test_filename_is_sanitised(self):
		self.assertEqual(ms.safe_filename("../../etc/passwd", "text/plain"), "passwd.txt")
		self.assertEqual(ms.safe_filename("", "image/png"), "file.png")
		self.assertEqual(ms.safe_filename("a b.jpg", "image/jpeg"), "a b.jpg")

	def test_choices_per_channel(self):
		self.assertEqual(ms.reaction_choices(cc.WHATSAPP), ["❤️", "👍", "😂", "😮", "😢", "🙏"])
		self.assertEqual(ms.reaction_choices(cc.INSTAGRAM), ["❤️"])
		self.assertEqual(ms.reaction_choices(cc.FACEBOOK_COMMENT), [])
		self.assertFalse(ms.MESSENGER_REACTIONS_ENABLED)
		self.assertEqual(ms.reaction_choices(cc.MESSENGER), [])
		with mock.patch.object(ms, "MESSENGER_REACTIONS_ENABLED", True):
			self.assertEqual(ms.reaction_choices(cc.MESSENGER), ["❤️"])

	def test_can_send_media(self):
		self.assertTrue(all(ms.can_send_media(c) for c in (cc.WHATSAPP, cc.MESSENGER, cc.INSTAGRAM)))
		self.assertFalse(ms.can_send_media(cc.FACEBOOK_COMMENT))
		self.assertFalse(ms.can_send_media("?"))


# ── Graph call shapes ───────────────────────────────────────────────────


class GraphShapeTest(Harness):
	def call(self, fn, *args, response=None, **kwargs):
		resp = SimpleNamespace(status_code=200, json=lambda: response or {})
		with (
			mock.patch.object(meta.requests, "request", return_value=resp) as req,
			mock.patch.object(
				meta,
				"_settings",
				lambda: {"enabled": True, "whatsapp_phone_number_id": "PN1", "facebook_page_id": "PAGE1", "graph_api_version": "v24.0"},
			),
			mock.patch.object(meta, "_wa_token", lambda: "WA"),
			mock.patch.object(meta, "_page_token", lambda: "PG"),
		):
			result = fn(*args, **kwargs)
		return result, req.call_args

	def test_wa_upload(self):
		media_id, call = self.call(meta.wa_upload_media, b"IMG", "a.jpg", "image/jpeg", response={"id": "M1"})
		self.assertEqual(media_id, "M1")
		self.assertEqual(call.args[:2], ("POST", "https://graph.facebook.com/v24.0/PN1/media"))
		self.assertEqual(call.kwargs["data"], {"messaging_product": "whatsapp", "type": "image/jpeg"})
		self.assertEqual(call.kwargs["files"], {"file": ("a.jpg", b"IMG", "image/jpeg")})
		self.assertIsNone(call.kwargs["json"])
		self.assertEqual(call.kwargs["headers"], {"Authorization": "Bearer WA"})

	def test_wa_send_media_shapes(self):
		reply = {"messages": [{"id": "wamid.1"}]}
		_, call = self.call(meta.wa_send_media, "+2011", "image", "M1", "hi", None, response=reply)
		self.assertEqual(
			call.kwargs["json"],
			{
				"messaging_product": "whatsapp",
				"recipient_type": "individual",
				"to": "2011",
				"type": "image",
				"image": {"id": "M1", "caption": "hi"},
			},
		)
		_, call = self.call(meta.wa_send_media, "2011", "audio", "M2", "ignored", None, response=reply)
		self.assertEqual(call.kwargs["json"]["audio"], {"id": "M2"})
		_, call = self.call(meta.wa_send_media, "2011", "video", "M3", "clip", None, response=reply)
		self.assertEqual(call.kwargs["json"]["video"], {"id": "M3", "caption": "clip"})
		wamid, call = self.call(meta.wa_send_media, "2011", "document", "M4", "see", "menu.pdf", response=reply)
		self.assertEqual(wamid, "wamid.1")
		self.assertEqual(call.kwargs["json"]["document"], {"id": "M4", "caption": "see", "filename": "menu.pdf"})

	def test_wa_reaction(self):
		_, call = self.call(meta.wa_send_reaction, "+2011", "wamid.X", "👍")
		self.assertEqual(
			call.kwargs["json"],
			{
				"messaging_product": "whatsapp",
				"recipient_type": "individual",
				"to": "2011",
				"type": "reaction",
				"reaction": {"message_id": "wamid.X", "emoji": "👍"},
			},
		)

	def test_username_user_goes_in_recipient_not_to(self):
		"""A WhatsApp user who hides their number: Meta read the id in ``to`` as a phone (131026)."""
		reply = {"messages": [{"id": "wamid.1"}]}
		bsuid = "EG.1234567890123456"
		sends = [
			(meta.wa_send_text, (bsuid, "hi")),
			(meta.wa_send_media, (bsuid, "image", "M1", None, None)),
			(meta.wa_send_reaction, (bsuid, "wamid.X", "👍")),
			(meta.wa_send_template, (bsuid, "t", "ar", [])),
		]
		for fn, args in sends:
			_, call = self.call(fn, *args, response=reply)
			body = call.kwargs["json"]
			self.assertEqual(body["recipient"], bsuid, fn.__name__)
			self.assertNotIn("to", body, fn.__name__)
			self.assertEqual(body["recipient_type"], "individual")
		_, call = self.call(meta.wa_send_text, "+20 111-034", "hi", response=reply)
		self.assertEqual(call.kwargs["json"]["to"], "20111034")
		self.assertNotIn("recipient", call.kwargs["json"])
		self.assertEqual(meta.wa_address("EG.ENT.99"), {"recipient": "EG.ENT.99"})

	def test_messenger_multipart(self):
		mid, call = self.call(
			meta.messenger_send_attachment,
			"PSID1",
			"file",
			b"PDF",
			"menu.pdf",
			"application/pdf",
			response={"message_id": "m_1", "recipient_id": "PSID1"},
		)
		self.assertEqual(mid, "m_1")
		self.assertEqual(call.args[:2], ("POST", "https://graph.facebook.com/v24.0/PAGE1/messages"))
		form = call.kwargs["data"]
		self.assertEqual(json.loads(form["recipient"]), {"id": "PSID1"})
		self.assertEqual(
			json.loads(form["message"]), {"attachment": {"type": "file", "payload": {"is_reusable": False}}}
		)
		self.assertEqual(form["messaging_type"], "RESPONSE")
		self.assertNotIn("tag", form)
		self.assertEqual(call.kwargs["files"], {"filedata": ("menu.pdf", b"PDF", "application/pdf")})
		self.assertIsNone(call.kwargs["json"])

	def test_messenger_multipart_with_human_agent_tag(self):
		_, call = self.call(
			meta.messenger_send_attachment,
			"P",
			"image",
			b"x",
			"a.jpg",
			"image/jpeg",
			tag="HUMAN_AGENT",
			response={"message_id": "m"},
		)
		self.assertEqual(call.kwargs["data"]["messaging_type"], "MESSAGE_TAG")
		self.assertEqual(call.kwargs["data"]["tag"], "HUMAN_AGENT")

	def test_instagram_url_attachment(self):
		mid, call = self.call(
			meta.ig_send_attachment_url, "IGSID1", "image", "https://x/y", response={"message_id": "m_2"}
		)
		self.assertEqual(mid, "m_2")
		self.assertEqual(
			call.kwargs["json"],
			{
				"recipient": {"id": "IGSID1"},
				"message": {"attachment": {"type": "image", "payload": {"url": "https://x/y"}}},
				"messaging_type": "RESPONSE",
			},
		)

	def test_dm_reaction_shapes(self):
		_, call = self.call(meta.dm_send_reaction, "U1", "m_9", "love", instagram=True)
		self.assertEqual(
			call.kwargs["json"],
			{
				"recipient": {"id": "U1"},
				"sender_action": "react",
				"payload": {"message_id": "m_9", "reaction": "love"},
			},
		)
		_, call = self.call(meta.dm_send_reaction, "U1", "m_9", None)
		self.assertEqual(
			call.kwargs["json"],
			{"recipient": {"id": "U1"}, "sender_action": "unreact", "payload": {"message_id": "m_9"}},
		)

	def test_meta_error_is_raised_readably(self):
		resp = SimpleNamespace(
			status_code=400, json=lambda: {"error": {"message": "Bad media", "code": 131053}}
		)
		with (
			mock.patch.object(meta.requests, "request", return_value=resp),
			mock.patch.object(meta, "_wa_token", lambda: "WA"),
		):
			with self.assertRaises(meta.MetaApiError) as ctx:
				meta.wa_upload_media(b"x", "a.jpg", "image/jpeg")
		self.assertEqual(ctx.exception.code, 131053)


# ── send_media flow ─────────────────────────────────────────────────────


class SendMediaFlowTest(Harness):
	def run_send(self, c, name="a.jpg", mime="image/jpeg", content=b"IMG", caption="hello"):
		self.with_conv(c)
		return ms.send_media(c.name, "mod@x.com", name, content, mime, caption)

	def test_whatsapp_upload_then_send_and_conversation_updated(self):
		with (
			mock.patch.object(meta, "wa_upload_media", return_value="M1") as up,
			mock.patch.object(meta, "wa_send_media", return_value="wamid.9") as send,
		):
			msg = self.run_send(conv())
		up.assert_called_once_with(b"IMG", "a.jpg", "image/jpeg")
		send.assert_called_once_with("201111034268", "image", "M1", "hello", None)
		self.assertEqual(msg["status"], "sent")
		inserted = [d for d in self.docs if d.get("doctype") == cc.MESSAGE][0]
		self.assertEqual(
			(inserted["direction"], inserted["message_type"], inserted["body"], inserted["status"]),
			("Outbound", "image", "hello", "queued"),
		)
		self.assertEqual(inserted["media_file"], "/private/files/a.jpg")
		file_doc = [d for d in self.docs if d.get("doctype") == "File"][0]
		self.assertEqual(file_doc["is_private"], 1)
		self.assertEqual(file_doc["attached_to_doctype"], cc.CONVERSATION)
		self.assertEqual(file_doc["attached_to_name"], "CONV1")
		self.assertIn((cc.MESSAGE, "Jarz Chat Message-2", {"status": "sent", "external_message_id": "wamid.9"}, None), self.sets)
		conv_set = [s for s in self.sets if s[0] == cc.CONVERSATION][0][2]
		self.assertEqual(conv_set["status"], "Answered")
		self.assertEqual(conv_set["last_message_preview"], "hello")
		self.assertEqual(conv_set["assigned_to"], "mod@x.com")
		self.assertTrue(self.published)

	def test_whatsapp_document_body_and_audio_without_caption(self):
		with (
			mock.patch.object(meta, "wa_upload_media", return_value="M1"),
			mock.patch.object(meta, "wa_send_media", return_value="w") as send,
		):
			self.run_send(conv(), "menu.pdf", "application/pdf", b"PDF", "the menu")
			send.assert_called_with("201111034268", "document", "M1", "the menu", "menu.pdf")
			inserted = [d for d in self.docs if d.get("doctype") == cc.MESSAGE][0]
			self.assertEqual(inserted["body"], "menu.pdf\nthe menu")
			self.run_send(conv(), "v.m4a", "audio/mp4", b"A", "ignored")
			send.assert_called_with("201111034268", "audio", "M1", None, None)

	def test_whatsapp_window_closed_refused_before_anything_is_saved(self):
		with self.assertRaises(Refused) as ctx:
			self.run_send(conv(window_expires_at=NOW - H))
		self.assertIn("24-hour", str(ctx.exception))
		self.assertEqual(self.docs, [])

	def test_comment_channel_refused(self):
		with self.assertRaises(Refused):
			self.run_send(conv(cc.FACEBOOK_COMMENT))
		self.assertEqual(self.docs, [])

	def test_failed_meta_call_keeps_a_failed_row_and_friendly_error(self):
		with mock.patch.object(meta, "wa_upload_media", side_effect=meta.MetaApiError("Media upload failed", 131053)):
			with self.assertRaises(Refused) as ctx:
				self.run_send(conv())
		self.assertIn("Sending failed", str(ctx.exception))
		failed = [s for s in self.message_sets() if isinstance(s[2], dict) and s[2].get("status") == "failed"]
		self.assertEqual(len(failed), 1)
		self.assertIn("Media upload failed", failed[0][2]["error"])
		self.assertEqual([s for s in self.sets if s[0] == cc.CONVERSATION], [])
		self.assertTrue(self.published)

	def test_messenger_stores_the_returned_mid_for_echo_dedupe(self):
		c = conv(cc.MESSENGER)
		with mock.patch.object(meta, "messenger_send_attachment", return_value="m_77") as send:
			self.run_send(c, "menu.pdf", "application/pdf", b"PDF", "")
		send.assert_called_once_with("PSID1", "file", b"PDF", "menu.pdf", "application/pdf", tag=None)
		self.assertIn(
			(cc.MESSAGE, "Jarz Chat Message-2", {"status": "sent", "external_message_id": "m_77"}, None), self.sets
		)

	def test_messenger_echo_of_that_mid_is_a_duplicate(self):
		# The echo webhook carries our mid: _message_exists finds the stored row and drops it.
		with mock.patch.object(frappe.db, "exists", create=True, return_value="Jarz Chat Message-2") as ex:
			self.assertTrue(ing._message_exists("m_77"))
		ex.assert_called_once_with(cc.MESSAGE, {"external_message_id": "m_77"})

	def test_messenger_caption_goes_as_its_own_text(self):
		c = conv(cc.MESSENGER)
		with (
			mock.patch.object(meta, "messenger_send_attachment", return_value="m_1"),
			mock.patch.object(cs, "send_text") as text,
		):
			self.run_send(c, "a.jpg", "image/jpeg", b"IMG", "nice one")
		text.assert_called_once_with("CONV1", "nice one", "mod@x.com")
		inserted = [d for d in self.docs if d.get("doctype") == cc.MESSAGE][0]
		self.assertEqual(inserted["body"], "")

	def test_messenger_caption_failure_does_not_fail_the_file(self):
		c = conv(cc.MESSENGER)
		with (
			mock.patch.object(meta, "messenger_send_attachment", return_value="m_1"),
			mock.patch.object(cs, "send_text", side_effect=Refused("x")),
		):
			msg = self.run_send(c, "a.jpg", "image/jpeg", b"IMG", "cap")
		self.assertEqual(msg["status"], "sent")

	def test_messenger_24h_closed_refused(self):
		with self.assertRaises(Refused) as ctx:
			self.run_send(conv(cc.MESSENGER, last_inbound_at=NOW - 30 * H), "a.jpg")
		self.assertIn("24 hours", str(ctx.exception))

	def test_instagram_uses_a_tokenised_public_url(self):
		c = conv(cc.INSTAGRAM)
		import frappe.utils as frappe_utils

		with (
			mock.patch.object(meta, "ig_send_attachment_url", return_value="m_ig") as send,
			# Under a real site (CI) get_url is the site's own host, not the stub's.
			mock.patch.object(frappe_utils, "get_url", return_value="https://erp.example.com", create=True),
		):
			self.run_send(c, "a.png", "image/png", b"PNG", "")
		args = send.call_args
		self.assertEqual(args.args[:2], ("PSID1", "image"))
		url = args.args[2]
		self.assertTrue(url.startswith("https://erp.example.com/api/method/jarz_pos.api.chat.media_link?token="))
		token = url.split("token=")[1]
		stored = json.loads(self.cache.store[f"site|{ms.MEDIA_LINK_PREFIX}{token}"])
		self.assertEqual(stored["file_url"], "/private/files/a.png")
		self.assertEqual(ms.MEDIA_LINK_SECONDS, 3600)

	def test_instagram_document_refused(self):
		with self.assertRaises(Refused):
			self.run_send(conv(cc.INSTAGRAM), "menu.pdf", "application/pdf", b"PDF", "")
		self.assertEqual(self.docs, [])


class MediaLinkTest(Harness):
	def setUp(self):
		super().setUp()
		wb = types.ModuleType("werkzeug.wrappers")

		class Response:
			def __init__(self, body=b"", status=200, mimetype=None):
				self.body, self.status_code, self.mimetype, self.headers = body, status, mimetype, {}

		wb.Response = Response
		patcher = mock.patch.dict(sys.modules, {"werkzeug": types.ModuleType("werkzeug"), "werkzeug.wrappers": wb})
		patcher.start()
		self.addCleanup(patcher.stop)

	def mint(self):
		return ms.media_link_url("/private/files/a.png", "image/png", "a.png").split("token=")[1]

	def test_unknown_and_malformed_tokens_are_404(self):
		self.assertEqual(api.media_link("nope").status_code, 404)
		self.assertEqual(api.media_link(None).status_code, 404)
		self.assertEqual(api.media_link("x" * 30).status_code, 404)

	def test_live_token_streams_the_file_inline(self):
		token = self.mint()
		self.rows[("File", "filter")] = "FILE1"
		res = api.media_link(token)
		self.assertEqual((res.status_code, res.body, res.mimetype), (200, b"BYTES", "image/png"))
		self.assertIn("inline", res.headers["Content-Disposition"])

	def test_file_not_attached_to_a_conversation_is_404(self):
		token = self.mint()  # no File row found for (file_url, Jarz Chat Conversation)
		self.assertEqual(api.media_link(token).status_code, 404)

	def test_expired_token_is_404(self):
		token = self.mint()
		self.cache.store.clear()
		self.assertEqual(api.media_link(token).status_code, 404)

	def test_endpoint_is_guest_get_only(self):
		allowed = getattr(frappe, "allowed_http_methods_for_whitelisted_func", None)
		if allowed is not None:  # real frappe only
			self.assertEqual(allowed.get(api.media_link), ["GET"])
			self.assertIn(api.media_link, frappe.guest_methods)


# ── react_to_message ────────────────────────────────────────────────────


class ReactTest(Harness):
	def setUp(self):
		super().setUp()
		self.rows[(cc.MESSAGE, "M1")] = _D(
			{"name": "M1", "conversation": "CONV1", "direction": "Inbound", "external_message_id": "wamid.IN"}
		)

	def go(self, c, emoji):
		self.with_conv(c)
		return ms.react("M1", emoji, "mod@x.com")

	def test_whatsapp_reaction_sent_and_stored(self):
		with mock.patch.object(meta, "wa_send_reaction") as send:
			out = self.go(conv(), "👍")
		send.assert_called_once_with("201111034268", "wamid.IN", "👍")
		self.assertIn((cc.MESSAGE, "M1", "staff_reaction", "👍"), self.sets)
		self.assertEqual(out["message"]["name"], "M1")
		self.assertTrue(self.published)

	def test_heart_without_variation_selector_matches(self):
		with mock.patch.object(meta, "wa_send_reaction") as send:
			self.go(conv(), "❤")
		send.assert_called_once_with("201111034268", "wamid.IN", "❤️")

	def test_empty_emoji_removes(self):
		with mock.patch.object(meta, "wa_send_reaction") as send:
			self.go(conv(), "")
		send.assert_called_once_with("201111034268", "wamid.IN", "")
		self.assertIn((cc.MESSAGE, "M1", "staff_reaction", None), self.sets)

	def test_outbound_or_idless_message_refused(self):
		self.rows[(cc.MESSAGE, "M1")] = _D(
			{"name": "M1", "conversation": "CONV1", "direction": "Outbound", "external_message_id": "w"}
		)
		with self.assertRaises(Refused):
			self.go(conv(), "👍")
		self.rows[(cc.MESSAGE, "M1")] = _D(
			{"name": "M1", "conversation": "CONV1", "direction": "Inbound", "external_message_id": None}
		)
		with self.assertRaises(Refused):
			self.go(conv(), "👍")

	def test_emoji_not_allowed_on_channel(self):
		with mock.patch.object(meta, "wa_send_reaction") as send, self.assertRaises(Refused):
			self.go(conv(), "🎉")
		send.assert_not_called()
		with mock.patch.object(meta, "dm_send_reaction") as dm, self.assertRaises(Refused):
			self.go(conv(cc.INSTAGRAM), "👍")
		dm.assert_not_called()

	def test_messenger_disabled_refuses_even_removal(self):
		with mock.patch.object(meta, "dm_send_reaction") as dm:
			for emoji in ("❤️", ""):
				with self.assertRaises(Refused):
					self.go(conv(cc.MESSENGER), emoji)
		dm.assert_not_called()

	def test_messenger_enabled_maps_heart_to_love(self):
		with (
			mock.patch.object(ms, "MESSENGER_REACTIONS_ENABLED", True),
			mock.patch.object(meta, "dm_send_reaction") as dm,
		):
			self.go(conv(cc.MESSENGER), "❤️")
			dm.assert_called_once_with("PSID1", "wamid.IN", "love", instagram=False)
			with self.assertRaises(Refused):
				self.go(conv(cc.MESSENGER), "👍")

	def test_instagram_love_and_unreact(self):
		with mock.patch.object(meta, "dm_send_reaction") as dm:
			self.go(conv(cc.INSTAGRAM), "❤️")
			dm.assert_called_with("PSID1", "wamid.IN", "love", instagram=True)
			self.go(conv(cc.INSTAGRAM), "")
			dm.assert_called_with("PSID1", "wamid.IN", None, instagram=True)

	def test_comment_channel_cannot_react(self):
		with self.assertRaises(Refused):
			self.go(conv(cc.FACEBOOK_COMMENT), "👍")

	def test_window_closed_refused(self):
		with mock.patch.object(meta, "wa_send_reaction") as send, self.assertRaises(Refused):
			self.go(conv(window_expires_at=NOW - H), "👍")
		send.assert_not_called()
		with self.assertRaises(Refused):
			self.go(conv(cc.INSTAGRAM, last_inbound_at=NOW - 30 * H), "❤️")

	def test_refused_before_migrate(self):
		self.has_cols = False
		with mock.patch.object(meta, "wa_send_reaction") as send, self.assertRaises(Refused):
			self.go(conv(), "👍")
		send.assert_not_called()

	def test_meta_refusal_is_readable_and_nothing_stored(self):
		with mock.patch.object(meta, "wa_send_reaction", side_effect=meta.MetaApiError("nope", 100)):
			with self.assertRaises(Refused) as ctx:
				self.go(conv(), "👍")
		self.assertIn("Reaction failed", str(ctx.exception))
		self.assertEqual([s for s in self.sets if s[2] == "staff_reaction"], [])


# ── customer reactions (webhook) ────────────────────────────────────────


def wa_payload(*messages):
	value = {"messaging_product": "whatsapp", "messages": list(messages)}
	return {"object": "whatsapp_business_account", "entry": [{"id": "W", "changes": [{"field": "messages", "value": value}]}]}


def dm_payload(item, obj="page"):
	return {"object": obj, "entry": [{"id": "PAGE1", "time": 1, "messaging": [item]}]}


class CustomerReactionParseTest(unittest.TestCase):
	def test_whatsapp_reaction_carries_its_target(self):
		msg = {"from": "2011", "id": "wamid.R", "timestamp": "1", "type": "reaction",
			"reaction": {"message_id": "wamid.OURS", "emoji": "👍"}}
		(event,) = ing.parse_webhook(wa_payload(msg))
		self.assertEqual((event["kind"], event["message_type"], event["body"]), ("message", "reaction", "👍"))
		self.assertEqual(event["reaction_target"], "wamid.OURS")

	def test_whatsapp_removed_reaction_becomes_a_clear_event(self):
		msg = {"from": "2011", "id": "wamid.R2", "timestamp": "1", "type": "reaction",
			"reaction": {"message_id": "wamid.OURS", "emoji": ""}}
		(event,) = ing.parse_webhook(wa_payload(msg))
		self.assertEqual((event["kind"], event["body"], event["reaction_target"]), ("reaction", "", "wamid.OURS"))

	def test_messenger_reaction(self):
		item = {"sender": {"id": "PSID1"}, "recipient": {"id": "PAGE1"}, "timestamp": 1760608800000,
			"reaction": {"mid": "m_ours", "action": "react", "reaction": "love", "emoji": "❤️"}}
		(event,) = ing.parse_webhook(dm_payload(item))
		self.assertEqual((event["kind"], event["channel"], event["external_id"]), ("reaction", cc.MESSENGER, "PSID1"))
		self.assertEqual((event["reaction_target"], event["body"]), ("m_ours", "❤️"))

	def test_instagram_reaction_name_without_emoji_and_unreact(self):
		base = {"sender": {"id": "IG1"}, "recipient": {"id": "PAGE1"}, "timestamp": 1760608800000}
		(event,) = ing.parse_webhook(dm_payload({**base, "reaction": {"mid": "m_o", "action": "react", "reaction": "love"}}, "instagram"))
		self.assertEqual((event["channel"], event["body"]), (cc.INSTAGRAM, "❤️"))
		(event,) = ing.parse_webhook(dm_payload({**base, "reaction": {"mid": "m_o", "action": "unreact"}}, "instagram"))
		self.assertEqual((event["kind"], event["body"]), ("reaction", ""))

	def test_our_own_reaction_echo_is_ignored(self):
		item = {"sender": {"id": "PAGE1"}, "recipient": {"id": "PSID1"}, "timestamp": 1,
			"reaction": {"mid": "m_x", "action": "react", "reaction": "love"}}
		self.assertEqual(ing.parse_webhook(dm_payload(item)), [])


class CustomerReactionApplyTest(Harness):
	def event(self, body="👍", target="wamid.OURS", kind="message"):
		return {"kind": kind, "reaction_target": target, "body": body, "channel": cc.WHATSAPP}

	def test_attached_to_the_target_and_published(self):
		self.rows[(cc.MESSAGE, "filter")] = _D({"name": "M9", "conversation": "CONV1"})
		self.assertTrue(ing._apply_customer_reaction(self.event()))
		self.assertIn((cc.MESSAGE, "M9", "customer_reaction", "👍"), self.sets)
		self.assertEqual(self.published[0][0][:2], ("CONV1", "message"))

	def test_empty_body_clears(self):
		self.rows[(cc.MESSAGE, "filter")] = _D({"name": "M9", "conversation": "CONV1"})
		self.assertTrue(ing._apply_customer_reaction(self.event(body="", kind="reaction")))
		self.assertIn((cc.MESSAGE, "M9", "customer_reaction", None), self.sets)

	def test_target_missing_falls_back(self):
		self.assertFalse(ing._apply_customer_reaction(self.event()))
		self.assertEqual(self.sets, [])

	def test_columns_missing_falls_back(self):
		self.has_cols = False
		self.rows[(cc.MESSAGE, "filter")] = _D({"name": "M9", "conversation": "CONV1"})
		self.assertFalse(ing._apply_customer_reaction(self.event()))

	def run_webhook(self, event):
		settings = {"enabled": 1}
		with (
			mock.patch.object(ing, "parse_webhook", return_value=[event]),
			mock.patch.object(ing, "_own_ids", return_value=set()),
			mock.patch.object(ing, "_site_tz", return_value=None),
			mock.patch.object(ing, "_after_commit"),
			mock.patch.object(ing, "_persist_message", return_value={"conversation": "C", "message": "M"}) as persist,
			mock.patch.object(frappe, "session", SimpleNamespace(user="Administrator")),
			mock.patch.object(frappe.db, "savepoint", create=True),
			mock.patch.object(frappe.db, "rollback", create=True),
			mock.patch.dict(sys.modules, {"jarz_pos.services.chat_settings": SimpleNamespace(get_chat_settings=lambda refresh=False: settings)}),
			mock.patch.object(ing.frappe.db, "commit", create=True),
		):
			result = ing.process_webhook({})
		return result, persist

	def test_whatsapp_reaction_with_unknown_target_keeps_the_standalone_message(self):
		result, persist = self.run_webhook(self.event())
		persist.assert_called_once()
		self.assertEqual(result["messages"], 1)

	def test_whatsapp_reaction_with_target_is_not_a_bubble(self):
		self.rows[(cc.MESSAGE, "filter")] = _D({"name": "M9", "conversation": "CONV1"})
		result, persist = self.run_webhook(self.event())
		persist.assert_not_called()
		self.assertEqual((result["messages"], result.get("reactions")), (0, 1))

	def test_messenger_reaction_with_unknown_target_is_skipped(self):
		result, persist = self.run_webhook({**self.event(kind="reaction"), "channel": cc.MESSENGER})
		persist.assert_not_called()
		self.assertEqual((result["messages"], result["errors"]), (0, 0))


# ── get_conversation / load_message ─────────────────────────────────────


class ConversationShapeTest(Harness):
	def get(self, channel):
		c = _D({"name": "CONV1", "channel": channel, "unread_count": 0, "customer": None})
		with (
			mock.patch.object(api, "_gate"),
			mock.patch.object(api, "_conversation_or_throw", return_value=c),
			mock.patch.object(frappe, "get_all", create=True, return_value=[]) as get_all,
			mock.patch.object(cc, "load_summary", return_value={"name": "CONV1", "channel": channel}),
			mock.patch.object(cc, "pending_draft", return_value=None),
			mock.patch.object(cc, "customer_card", return_value=None),
			mock.patch.object(api, "_customer_suggestion", return_value=None),
		):
			out = api.get_conversation("CONV1")
		return out, get_all

	def test_whatsapp(self):
		out, get_all = self.get(cc.WHATSAPP)
		self.assertTrue(out["conversation"]["can_send_media"])
		self.assertEqual(out["conversation"]["reaction_choices"], ["❤️", "👍", "😂", "😮", "😢", "🙏"])
		self.assertIn("staff_reaction", get_all.call_args.kwargs["fields"])

	def test_instagram_messenger_comment(self):
		self.assertEqual(self.get(cc.INSTAGRAM)[0]["conversation"]["reaction_choices"], ["❤️"])
		out = self.get(cc.MESSENGER)[0]["conversation"]
		self.assertEqual((out["can_send_media"], out["reaction_choices"]), (True, []))
		out = self.get(cc.INSTAGRAM_COMMENT)[0]["conversation"]
		self.assertEqual((out["can_send_media"], out["reaction_choices"]), (False, []))

	def test_before_migrate_the_message_query_omits_the_new_columns(self):
		self.has_cols = False
		_, get_all = self.get(cc.WHATSAPP)
		fields = get_all.call_args.kwargs["fields"]
		self.assertNotIn("staff_reaction", fields)
		self.assertNotIn("customer_reaction", fields)


class LoadMessageGuardTest(unittest.TestCase):
	def test_column_check_never_raises_and_defaults_to_missing(self):
		with mock.patch.object(frappe, "db", SimpleNamespace(), create=True):
			self.assertFalse(cc.has_reaction_columns())
		with mock.patch.object(frappe, "db", SimpleNamespace(has_column=lambda *a: True), create=True):
			self.assertTrue(cc.has_reaction_columns())

	def test_load_message_without_the_columns(self):
		seen = {}

		def get_value(doctype, name, fields, as_dict=False):
			seen["fields"] = fields
			return _D({"name": name, "direction": "Outbound", "message_type": "image"})

		db = SimpleNamespace(has_column=lambda *a: False, get_value=get_value)
		with mock.patch.object(frappe, "db", db, create=True):
			msg = cc.load_message("M1")
		self.assertNotIn("staff_reaction", seen["fields"])
		self.assertIsNone(msg["staff_reaction"])
		self.assertIsNone(msg["customer_reaction"])

	def test_load_message_with_the_columns(self):
		def get_value(doctype, name, fields, as_dict=False):
			return _D({"name": name, "staff_reaction": "👍", "customer_reaction": "❤️"})

		db = SimpleNamespace(has_column=lambda *a: True, get_value=get_value)
		with mock.patch.object(frappe, "db", db, create=True):
			msg = cc.load_message("M1")
		self.assertEqual((msg["staff_reaction"], msg["customer_reaction"]), ("👍", "❤️"))


class TranscriptTest(unittest.TestCase):
	def test_outbound_media_rows_read_as_labels(self):
		from jarz_pos.services import chat_drafts as d

		lines = d.transcript_lines(
			[
				{"direction": "Outbound", "message_type": "image", "body": "", "timestamp": NOW},
				{"direction": "Outbound", "message_type": "document", "body": "menu.pdf\nsee", "timestamp": NOW},
				{"direction": "Outbound", "message_type": "audio", "body": "", "timestamp": NOW},
			],
			False,
			[],
		)
		self.assertEqual(len(lines), 3)
		self.assertIn("Jarz: [image]", lines[0])
		self.assertIn("menu.pdf", lines[1])


if __name__ == "__main__":  # pragma: no cover
	unittest.main()


class ReactionIsNotAReplyTest(Harness):
	"""2026-11-16 (CHAT-07784): a ❤️ from the app (or the phone) got its own WhatsApp id; its
	"sent" status became a "Reply sent from Meta Business Suite" placeholder that marked the
	chat Answered. Reaction ids are now remembered and their placeholders removed."""

	call = GraphShapeTest.call

	def setUp(self):
		super().setUp()
		self.rows[(cc.MESSAGE, "M1")] = _D(
			{"name": "M1", "conversation": "CONV1", "direction": "Inbound", "external_message_id": "wamid.IN"}
		)

	def test_wa_reaction_returns_its_own_id(self):
		own, _ = self.call(
			meta.wa_send_reaction, "+2011", "wamid.X", "👍", response={"messages": [{"id": "wamid.R"}]}
		)
		self.assertEqual(own, "wamid.R")
		none, _ = self.call(meta.wa_send_reaction, "+2011", "wamid.X", "👍", response={})
		self.assertIsNone(none)

	def test_app_reaction_id_is_remembered_and_any_placeholder_dropped(self):
		self.with_conv(conv())
		with (
			mock.patch.object(meta, "wa_send_reaction", return_value="wamid.R"),
			mock.patch.object(cc, "remember_not_a_reply") as remember,
			mock.patch.object(ing, "drop_status_only_placeholder") as drop,
		):
			ms.react("M1", "❤️", "mod@x.com")
		remember.assert_called_once_with("wamid.R")
		drop.assert_called_once_with("wamid.R")

	def test_status_of_a_remembered_reaction_makes_no_placeholder(self):
		event = {
			"kind": "status",
			"channel": cc.WHATSAPP,
			"external_message_id": "wamid.R",
			"recipient": "2011",
			"status": "sent",
			"category": "service",
			"timestamp": 1.0,
		}
		with (
			mock.patch.object(cc, "is_not_a_reply", return_value=True),
			mock.patch.object(ing, "_find_conversation") as find,
		):
			self.assertIsNone(ing._record_status_only_reply(event, {}, None))
		find.assert_not_called()

	def test_phone_reaction_echo_is_ours_not_the_customers(self):
		self.rows[(cc.MESSAGE, "filter")] = _D({"name": "M9", "conversation": "CONV1"})
		event = {
			"kind": "message",
			"direction": "Outbound",
			"channel": cc.WHATSAPP,
			"external_message_id": "wamid.R2",
			"reaction_target": "wamid.IN",
			"body": "❤️",
		}
		with (
			mock.patch.object(cc, "remember_not_a_reply") as remember,
			mock.patch.object(ing, "drop_status_only_placeholder") as drop,
		):
			self.assertTrue(ing._apply_staff_reaction_echo(event))
		remember.assert_called_once_with("wamid.R2")
		drop.assert_called_once_with("wamid.R2")
		self.assertIn((cc.MESSAGE, "M9", "staff_reaction", "❤️"), self.sets)
		self.assertNotIn((cc.MESSAGE, "M9", "customer_reaction", "❤️"), self.sets)

	def test_echo_parse_keeps_direction_and_target(self):
		value = {
			"message_echoes": [
				{
					"to": "201111034268",
					"id": "wamid.R3",
					"timestamp": "1760608800",
					"type": "reaction",
					"reaction": {"message_id": "wamid.IN", "emoji": "❤️"},
				}
			]
		}
		payload = {
			"object": "whatsapp_business_account",
			"entry": [{"id": "WABA", "changes": [{"field": "smb_message_echoes", "value": value}]}],
		}
		events = ing.parse_webhook(payload)
		self.assertEqual(events[0]["direction"], "Outbound")
		self.assertEqual(events[0]["reaction_target"], "wamid.IN")
