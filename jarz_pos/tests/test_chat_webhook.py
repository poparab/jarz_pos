"""Chat inbox webhook rules (services/chat_ingest.py) -- pure functions, no site.

Covers Meta's HMAC signature and subscription handshake, and the parsing of
WhatsApp Cloud API, Messenger, Instagram and comment payloads into normalised
events (statuses, echoes, attachments, comment threads). Needs no chat table:
it runs before ``bench migrate`` in CI.

Plain ``unittest.TestCase`` (no FrappeTestCase, see test_task_board.py); a
stub ``frappe`` is registered when frappe is not installed.
"""

import datetime
import sys
import types
import unittest
from types import SimpleNamespace

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
	_utils.now_datetime = lambda: datetime.datetime(2026, 10, 16, 12, 0)
	_utils.nowdate = lambda: "2026-10-16"
	sys.modules["frappe.utils"] = _utils
	frappe.utils = _utils

from jarz_pos.services import chat_common as cc
from jarz_pos.services import chat_ingest as ing

SECRET = "test-app-secret"  # not a real secret


class SignatureTest(unittest.TestCase):
	def test_valid_signature(self):
		body = b'{"object":"page","entry":[]}'
		header = ing.compute_signature(body, SECRET)
		self.assertTrue(header.startswith("sha256="))
		self.assertTrue(ing.verify_signature(body, header, SECRET))

	def test_str_body_signs_like_bytes(self):
		self.assertEqual(ing.compute_signature("abc", SECRET), ing.compute_signature(b"abc", SECRET))

	def test_tampered_body_fails(self):
		header = ing.compute_signature(b"original", SECRET)
		self.assertFalse(ing.verify_signature(b"tampered", header, SECRET))

	def test_wrong_secret_fails(self):
		header = ing.compute_signature(b"x", "other-secret")
		self.assertFalse(ing.verify_signature(b"x", header, SECRET))

	def test_missing_secret_or_header_fails(self):
		header = ing.compute_signature(b"x", SECRET)
		self.assertFalse(ing.verify_signature(b"x", header, None))
		self.assertFalse(ing.verify_signature(b"x", header, ""))
		self.assertFalse(ing.verify_signature(b"x", None, SECRET))
		self.assertFalse(ing.verify_signature(b"x", "", SECRET))

	def test_header_without_prefix_fails(self):
		digest = ing.compute_signature(b"x", SECRET).split("=", 1)[1]
		self.assertFalse(ing.verify_signature(b"x", digest, SECRET))
		self.assertFalse(ing.verify_signature(b"x", f"sha1={digest}", SECRET))


class SubscriptionTest(unittest.TestCase):
	ARGS = {"hub.mode": "subscribe", "hub.verify_token": "tok", "hub.challenge": "12345"}

	def test_valid_handshake_echoes_challenge(self):
		self.assertEqual(ing.verify_subscription(self.ARGS, "tok"), "12345")

	def test_wrong_token(self):
		self.assertIsNone(ing.verify_subscription(self.ARGS, "other"))

	def test_wrong_mode(self):
		self.assertIsNone(ing.verify_subscription({**self.ARGS, "hub.mode": "unsubscribe"}, "tok"))

	def test_missing_challenge(self):
		self.assertIsNone(ing.verify_subscription({**self.ARGS, "hub.challenge": ""}, "tok"))

	def test_no_configured_token_refuses(self):
		self.assertIsNone(ing.verify_subscription(self.ARGS, None))
		self.assertIsNone(ing.verify_subscription({**self.ARGS, "hub.verify_token": ""}, ""))


class HelpersTest(unittest.TestCase):
	def test_mask_id(self):
		self.assertEqual(ing.mask_id("201111034268"), "2011****4268")
		self.assertEqual(ing.mask_id("123"), "***")
		self.assertEqual(ing.mask_id(None), "***")

	def test_epoch_seconds_and_milliseconds_agree(self):
		self.assertEqual(ing.epoch_to_site(1760608800), ing.epoch_to_site(1760608800000))
		self.assertEqual(ing.epoch_to_site("1760608800"), ing.epoch_to_site(1760608800))

	def test_epoch_without_zone_is_naive_utc(self):
		got = ing.epoch_to_site(1760608800)
		self.assertIsNone(got.tzinfo)
		self.assertEqual(got, datetime.datetime(2025, 10, 16, 10, 0))

	def test_iso_with_compact_offset(self):
		self.assertEqual(
			ing.epoch_to_site("2026-10-16T10:00:00+0000"), datetime.datetime(2026, 10, 16, 10, 0)
		)


def wa_payload(value, field="messages"):
	return {
		"object": "whatsapp_business_account",
		"entry": [{"id": "WABA", "changes": [{"field": field, "value": value}]}],
	}


def wa_messages(*messages, name="Sara Ali", wa_id="201111034268"):
	return wa_payload(
		{
			"messaging_product": "whatsapp",
			"metadata": {"phone_number_id": "PNID"},
			"contacts": [{"wa_id": wa_id, "profile": {"name": name}}],
			"messages": list(messages),
		}
	)


class WhatsAppParseTest(unittest.TestCase):
	def test_text_message(self):
		events = ing.parse_webhook(
			wa_messages(
				{
					"from": "201111034268",
					"id": "wamid.1",
					"timestamp": "1760608800",
					"type": "text",
					"text": {"body": "عايزة أطلب"},
				}
			)
		)
		self.assertEqual(len(events), 1)
		e = events[0]
		self.assertEqual(e["kind"], "message")
		self.assertEqual(e["channel"], cc.WHATSAPP)
		self.assertEqual(e["external_id"], "201111034268")
		self.assertEqual(e["phone"], "201111034268")
		self.assertEqual(e["contact_name"], "Sara Ali")
		self.assertEqual(e["direction"], "Inbound")
		self.assertEqual(e["external_message_id"], "wamid.1")
		self.assertEqual(e["message_type"], "text")
		self.assertEqual(e["body"], "عايزة أطلب")
		self.assertEqual(e["timestamp"], 1760608800.0)

	def test_image_with_caption(self):
		e = ing.parse_webhook(
			wa_messages(
				{
					"from": "201111034268",
					"id": "wamid.2",
					"timestamp": "1",
					"type": "image",
					"image": {"id": "MEDIA1", "mime_type": "image/jpeg", "caption": "this one"},
				}
			)
		)[0]
		self.assertEqual(e["message_type"], "image")
		self.assertEqual(e["media_id"], "MEDIA1")
		self.assertEqual(e["media_mime"], "image/jpeg")
		self.assertEqual(e["body"], "this one")

	def test_location(self):
		e = ing.parse_webhook(
			wa_messages(
				{
					"from": "201111034268",
					"id": "wamid.3",
					"timestamp": "1",
					"type": "location",
					"location": {"latitude": 30.05, "longitude": 31.33},
				}
			)
		)[0]
		self.assertEqual(e["message_type"], "location")
		self.assertEqual(e["body"], "30.05,31.33")

	def test_removed_reaction_is_skipped(self):
		events = ing.parse_webhook(
			wa_messages(
				{
					"from": "201111034268",
					"id": "wamid.4",
					"timestamp": "1",
					"type": "reaction",
					"reaction": {"message_id": "wamid.x", "emoji": ""},
				},
				{
					"from": "201111034268",
					"id": "wamid.5",
					"timestamp": "1",
					"type": "reaction",
					"reaction": {"message_id": "wamid.x", "emoji": "❤"},
				},
			)
		)
		self.assertEqual([e["external_message_id"] for e in events], ["wamid.5"])
		self.assertEqual(events[0]["message_type"], "reaction")

	def test_button_and_interactive_become_text(self):
		events = ing.parse_webhook(
			wa_messages(
				{
					"from": "201111034268",
					"id": "wamid.6",
					"timestamp": "1",
					"type": "button",
					"button": {"text": "Yes"},
				},
				{
					"from": "201111034268",
					"id": "wamid.7",
					"timestamp": "1",
					"type": "interactive",
					"interactive": {"type": "list_reply", "list_reply": {"id": "a", "title": "Nutella"}},
				},
			)
		)
		self.assertEqual(
			[(e["message_type"], e["body"]) for e in events], [("text", "Yes"), ("text", "Nutella")]
		)

	def test_quick_reply_keeps_its_payload(self):
		e = ing.parse_webhook(
			wa_messages(
				{
					"from": "201111034268",
					"id": "wamid.8",
					"timestamp": "1",
					"type": "button",
					"button": {"text": "إيقاف الرسائل", "payload": "إيقاف الرسائل"},
				}
			)
		)[0]
		self.assertEqual((e["body"], e["button_payload"]), ("إيقاف الرسائل", "إيقاف الرسائل"))

	def test_unknown_type_is_unsupported_with_raw(self):
		msg = {"from": "201111034268", "id": "wamid.8", "timestamp": "1", "type": "contacts", "contacts": []}
		e = ing.parse_webhook(wa_messages(msg))[0]
		self.assertEqual(e["message_type"], "unsupported")
		self.assertEqual(e["raw"], msg)

	def test_message_without_id_is_skipped(self):
		self.assertEqual(
			ing.parse_webhook(wa_messages({"from": "201111034268", "type": "text", "text": {"body": "x"}})),
			[],
		)

	def test_status_with_error(self):
		events = ing.parse_webhook(
			wa_payload(
				{
					"statuses": [
						{"id": "wamid.out1", "status": "delivered", "timestamp": "2", "recipient_id": "2011"},
						{
							"id": "wamid.out2",
							"status": "failed",
							"timestamp": "3",
							"recipient_id": "2011",
							"errors": [{"code": 131047, "title": "Re-engagement message"}],
						},
					]
				}
			)
		)
		self.assertEqual([e["kind"] for e in events], ["status", "status"])
		self.assertEqual(events[0]["status"], "delivered")
		self.assertIsNone(events[0]["error"])
		self.assertEqual(events[1]["status"], "failed")
		self.assertEqual(events[1]["error"], "131047: Re-engagement message")
		self.assertEqual(events[0]["recipient"], "2011")

	def test_status_keeps_its_pricing_category(self):
		events = ing.parse_webhook(
			wa_payload(
				{
					"statuses": [
						{
							"id": "wamid.s1",
							"status": "sent",
							"timestamp": "2",
							"recipient_id": "2011",
							"pricing": {"billable": False, "category": "service"},
						},
						{
							"id": "wamid.s2",
							"status": "sent",
							"timestamp": "2",
							"recipient_id": "2011",
							"conversation": {"id": "c", "origin": {"type": "utility"}},
						},
						{"id": "wamid.s3", "status": "sent", "timestamp": "2", "recipient_id": "2011"},
					]
				}
			)
		)
		self.assertEqual([e["category"] for e in events], ["service", "utility", None])

	def test_business_app_echo_is_outbound_from_phone(self):
		events = ing.parse_webhook(
			wa_payload(
				{
					"message_echoes": [
						{
							"from": "PNID",
							"to": "201111034268",
							"id": "wamid.echo",
							"timestamp": "5",
							"type": "text",
							"text": {"body": "تمام"},
						}
					]
				},
				field="smb_message_echoes",
			)
		)
		self.assertEqual(len(events), 1)
		e = events[0]
		self.assertEqual(e["direction"], "Outbound")
		self.assertEqual(e["sent_via"], "Phone")
		self.assertEqual(e["external_id"], "201111034268")
		self.assertEqual(e["body"], "تمام")


def page_payload(*messaging, obj="page", changes=None, entry_id="PAGE1"):
	entry = {"id": entry_id, "time": 1760608800000}
	if messaging:
		entry["messaging"] = list(messaging)
	if changes:
		entry["changes"] = changes
	return {"object": obj, "entry": [entry]}


class MessagingParseTest(unittest.TestCase):
	def test_messenger_text(self):
		e = ing.parse_webhook(
			page_payload(
				{
					"sender": {"id": "PSID1"},
					"recipient": {"id": "PAGE1"},
					"timestamp": 1760608800000,
					"message": {"mid": "m_1", "text": "hello"},
				}
			)
		)[0]
		self.assertEqual(e["channel"], cc.MESSENGER)
		self.assertEqual(e["external_id"], "PSID1")
		self.assertEqual(e["direction"], "Inbound")
		self.assertEqual(e["body"], "hello")
		self.assertEqual(e["timestamp"], 1760608800.0)

	def test_echo_belongs_to_the_recipient(self):
		e = ing.parse_webhook(
			page_payload(
				{
					"sender": {"id": "PAGE1"},
					"recipient": {"id": "PSID1"},
					"timestamp": 1,
					"message": {"mid": "m_2", "text": "reply from phone", "is_echo": True},
				}
			)
		)[0]
		self.assertEqual(e["external_id"], "PSID1")
		self.assertEqual(e["direction"], "Outbound")
		self.assertEqual(e["sent_via"], "Phone")

	def test_delivery_becomes_status(self):
		events = ing.parse_webhook(
			page_payload(
				{
					"sender": {"id": "PSID1"},
					"recipient": {"id": "PAGE1"},
					"timestamp": 1,
					"delivery": {"mids": ["m_a", "m_b"]},
				}
			)
		)
		self.assertEqual(
			[(e["kind"], e["external_message_id"], e["status"]) for e in events],
			[("status", "m_a", "delivered"), ("status", "m_b", "delivered")],
		)

	def test_read_event_is_ignored(self):
		self.assertEqual(
			ing.parse_webhook(
				page_payload(
					{
						"sender": {"id": "PSID1"},
						"recipient": {"id": "PAGE1"},
						"timestamp": 1,
						"read": {"watermark": 1},
					}
				)
			),
			[],
		)

	def test_attachments_split_with_suffixed_ids(self):
		events = ing.parse_webhook(
			page_payload(
				{
					"sender": {"id": "PSID1"},
					"recipient": {"id": "PAGE1"},
					"timestamp": 1,
					"message": {
						"mid": "m_3",
						"text": "look",
						"attachments": [
							{"type": "image", "payload": {"url": "https://cdn.example/a.jpg"}},
							{"type": "file", "payload": {"url": "https://cdn.example/b.pdf"}},
							{"type": "share", "payload": {"url": "https://example/post"}},
						],
					},
				}
			)
		)
		self.assertEqual([e["external_message_id"] for e in events], ["m_3", "m_3#1", "m_3#2"])
		self.assertEqual([e["message_type"] for e in events], ["image", "document", "unsupported"])
		self.assertEqual(events[0]["body"], "look")
		self.assertEqual(events[1]["body"], "")
		self.assertEqual(events[0]["media_url"], "https://cdn.example/a.jpg")
		self.assertIsNone(events[2]["media_url"])
		self.assertIsNotNone(events[2]["raw"])

	def test_postback_becomes_text(self):
		e = ing.parse_webhook(
			page_payload(
				{
					"sender": {"id": "PSID1"},
					"recipient": {"id": "PAGE1"},
					"timestamp": 1,
					"postback": {"mid": "m_pb", "title": "Menu", "payload": "MENU"},
				}
			)
		)[0]
		self.assertEqual((e["message_type"], e["body"], e["external_message_id"]), ("text", "Menu", "m_pb"))

	def test_instagram_dm_channel(self):
		e = ing.parse_webhook(
			page_payload(
				{
					"sender": {"id": "IGSID1"},
					"recipient": {"id": "IGACC"},
					"timestamp": 1,
					"message": {"mid": "ig_1", "text": "price?"},
				},
				obj="instagram",
				entry_id="IGACC",
			)
		)[0]
		self.assertEqual(e["channel"], cc.INSTAGRAM)
		self.assertEqual(e["external_id"], "IGSID1")


class CommentParseTest(unittest.TestCase):
	def feed(self, value):
		return page_payload(changes=[{"field": "feed", "value": value}])

	def test_top_level_comment_roots_on_itself(self):
		e = ing.parse_webhook(
			self.feed(
				{
					"item": "comment",
					"verb": "add",
					"comment_id": "C1",
					"post_id": "P1",
					"parent_id": "P1",
					"message": "how much?",
					"from": {"id": "U1", "name": "Mona"},
					"created_time": 1760608800,
				}
			)
		)[0]
		self.assertEqual(e["channel"], cc.FACEBOOK_COMMENT)
		self.assertEqual(e["external_id"], "C1")
		self.assertEqual(e["message_type"], "comment")
		self.assertEqual(e["contact_name"], "Mona")
		self.assertEqual(e["post_id"], "P1")
		self.assertEqual(e["direction"], "Inbound")

	def test_reply_roots_on_parent(self):
		e = ing.parse_webhook(
			self.feed(
				{
					"item": "comment",
					"verb": "add",
					"comment_id": "C2",
					"post_id": "P1",
					"parent_id": "C1",
					"message": "and delivery?",
					"from": {"id": "U1", "name": "Mona"},
				}
			)
		)[0]
		self.assertEqual(e["external_id"], "C1")

	def test_page_own_comment_is_outbound(self):
		e = ing.parse_webhook(
			self.feed(
				{
					"item": "comment",
					"verb": "add",
					"comment_id": "C3",
					"post_id": "P1",
					"parent_id": "C1",
					"message": "check your DMs",
					"from": {"id": "PAGE1", "name": "Jarz"},
				}
			)
		)[0]
		self.assertEqual(e["direction"], "Outbound")
		self.assertEqual(e["sent_via"], "Phone")
		self.assertIsNone(e["contact_name"])

	def test_edits_and_reactions_are_ignored(self):
		self.assertEqual(
			ing.parse_webhook(
				self.feed({"item": "comment", "verb": "edited", "comment_id": "C1", "post_id": "P1"})
			),
			[],
		)
		self.assertEqual(
			ing.parse_webhook(self.feed({"item": "reaction", "verb": "add", "post_id": "P1"})), []
		)

	def test_instagram_comment_and_reply(self):
		payload = page_payload(
			obj="instagram",
			entry_id="IGACC",
			changes=[
				{
					"field": "comments",
					"value": {
						"id": "IC1",
						"text": "yum",
						"from": {"id": "U9", "username": "mona.k"},
						"media": {"id": "MEDIA9"},
					},
				},
				{
					"field": "comments",
					"value": {
						"id": "IC2",
						"parent_id": "IC1",
						"text": "thanks!",
						"from": {"id": "IGACC", "username": "jarz"},
						"media": {"id": "MEDIA9"},
					},
				},
			],
		)
		events = ing.parse_webhook(payload)
		self.assertEqual([e["channel"] for e in events], [cc.INSTAGRAM_COMMENT] * 2)
		self.assertEqual([e["external_id"] for e in events], ["IC1", "IC1"])
		self.assertEqual([e["direction"] for e in events], ["Inbound", "Outbound"])
		self.assertEqual(events[0]["contact_name"], "mona.k")
		self.assertEqual(events[0]["post_id"], "MEDIA9")


class RobustnessTest(unittest.TestCase):
	def test_garbage_never_raises(self):
		self.assertEqual(ing.parse_webhook(None), [])
		self.assertEqual(ing.parse_webhook([]), [])
		self.assertEqual(ing.parse_webhook({"object": "unknown", "entry": [{"id": 1}]}), [])
		self.assertEqual(ing.parse_webhook({"object": "page", "entry": ["junk", None]}), [])


class _Row(dict):
	"""``frappe._dict`` stand-in: attribute and ``.get`` access."""

	__getattr__ = dict.get


def wa_status(status="sent", category="service", recipient="201111034268", ts=1760608800.0):
	return {
		"kind": "status",
		"channel": cc.WHATSAPP,
		"external_message_id": "wamid.suite",
		"status": status,
		"recipient": recipient,
		"category": category,
		"error": None,
		"timestamp": ts,
	}


class StatusOnlyReplyTest(unittest.TestCase):
	"""A Business Suite reply reaches us only as statuses for an id we never stored."""

	def setUp(self):
		from unittest import mock

		self.mock = mock

	def test_service_status_becomes_outbound_placeholder(self):
		reply = ing._status_only_reply_event(wa_status())
		self.assertEqual(reply["kind"], "message")
		self.assertEqual(reply["direction"], "Outbound")
		self.assertEqual(reply["sent_via"], "Phone")
		self.assertEqual(reply["external_id"], "201111034268")
		self.assertEqual(reply["external_message_id"], "wamid.suite")
		self.assertEqual(reply["body"], ing.STATUS_ONLY_BODY)
		self.assertEqual(reply["status"], "sent")
		self.assertTrue(reply["status_only"])
		self.assertIsNotNone(ing._status_only_reply_event(wa_status("delivered", category=None)))

	def test_statuses_that_are_not_a_staff_reply(self):
		for event in (
			wa_status(category="utility"),  # order template
			wa_status(category="marketing"),
			wa_status("read"),  # may come hours after the reply
			wa_status("failed"),
			wa_status(recipient=None),
			{**wa_status(), "channel": cc.MESSENGER},
		):
			self.assertIsNone(ing._status_only_reply_event(event), event)

	def _record(self, conv, in_flight=False):
		mock = self.mock
		with (
			mock.patch.object(ing, "_find_conversation", return_value=conv),
			mock.patch.object(ing, "_app_send_in_flight", return_value=in_flight),
			mock.patch.object(ing, "_persist_message", return_value={"conversation": "CHAT-1"}) as persist,
		):
			action = ing._record_status_only_reply(wa_status(), {}, "UTC")
		return action, persist

	def test_records_reply_for_known_chat(self):
		conv = _Row(name="CHAT-1", last_inbound_at=datetime.datetime(2025, 10, 16, 9, 0))
		action, persist = self._record(conv)
		self.assertEqual(action, {"conversation": "CHAT-1"})
		self.assertTrue(persist.call_args.args[0]["status_only"])

	def test_never_opens_a_chat_from_a_bare_status(self):
		action, persist = self._record(None)
		self.assertIsNone(action)
		persist.assert_not_called()

	def test_skips_when_customer_wrote_after_the_reply(self):
		conv = _Row(name="CHAT-1", last_inbound_at=datetime.datetime(2025, 10, 16, 11, 0))
		action, persist = self._record(conv)
		self.assertIsNone(action)
		persist.assert_not_called()

	def test_skips_while_a_jarz_send_is_in_flight(self):
		action, persist = self._record(_Row(name="CHAT-1"), in_flight=True)
		self.assertIsNone(action)
		persist.assert_not_called()

	def test_placeholder_marks_chat_answered(self):
		mock = self.mock
		conv = _Row(name="CHAT-1", status="Open", unread_count=2, alert_count=1)
		doc = mock.MagicMock()
		doc.name = "MSG-1"
		db = mock.MagicMock()
		with (
			mock.patch.object(ing, "_message_exists", return_value=False),
			mock.patch.object(ing, "_find_conversation", return_value=conv),
			mock.patch.object(ing, "_is_app_echo", return_value=False),
			mock.patch.object(ing.frappe, "get_doc", return_value=doc, create=True) as get_doc,
			mock.patch.object(ing.frappe, "db", db),
			mock.patch.object(cc, "supersede_live_drafts", return_value=["D1"]) as supersede,
		):
			action = ing._persist_message(ing._status_only_reply_event(wa_status("delivered")), {}, "UTC")
		fields = get_doc.call_args.args[0]
		self.assertEqual(fields["raw_json"], ing.STATUS_ONLY_MARKER)
		self.assertEqual(fields["status"], "delivered")
		self.assertEqual(fields["sent_via"], "Phone")
		self.assertEqual(fields["direction"], "Outbound")
		updates = db.set_value.call_args.args[2]
		self.assertEqual(updates["status"], "Answered")
		self.assertEqual(updates["unread_count"], 0)
		self.assertEqual(updates["alert_count"], 0)
		self.assertIsNone(updates["waiting_since"])
		supersede.assert_called_once_with("CHAT-1")
		self.assertFalse(action["push"])
		self.assertFalse(action["draft"])

	def test_late_echo_fills_in_the_placeholder(self):
		mock = self.mock
		db = mock.MagicMock()
		db.get_value.side_effect = [
			_Row(name="MSG-1", conversation="CHAT-1", raw_json=ing.STATUS_ONLY_MARKER),
			cc.preview(ing.STATUS_ONLY_BODY, "text"),
		]
		echo = {"direction": "Outbound", "external_message_id": "wamid.suite", "body": "تمام", "message_type": "text"}
		with mock.patch.object(ing.frappe, "db", db):
			action = ing._upgrade_status_only(echo)
		msg_update = db.set_value.call_args_list[0].args[2]
		self.assertEqual(msg_update["body"], "تمام")
		self.assertIsNone(msg_update["raw_json"])
		self.assertEqual(db.set_value.call_args_list[1].args[2:4], ("last_message_preview", "تمام"))
		self.assertEqual(action["message"], "MSG-1")
		self.assertFalse(action["quiet"])

	def test_echo_of_a_real_message_changes_nothing(self):
		mock = self.mock
		db = mock.MagicMock()
		db.get_value.return_value = _Row(name="MSG-1", conversation="CHAT-1", raw_json=None)
		echo = {"direction": "Outbound", "external_message_id": "wamid.x", "body": "hi"}
		with mock.patch.object(ing.frappe, "db", db):
			self.assertIsNone(ing._upgrade_status_only(echo))
			self.assertIsNone(ing._upgrade_status_only({**echo, "direction": "Inbound"}))
		db.set_value.assert_not_called()


class DmNameLookupTest(unittest.TestCase):
	"""Messenger / Instagram webhooks carry only an id; the name comes from Graph."""

	def setUp(self):
		from unittest import mock

		from jarz_pos.services import chat_meta_client as meta

		self.mock = mock
		self.meta = meta

	def test_messenger_name_comes_from_the_thread_participants(self):
		thread = {
			"data": [
				{"participants": {"data": [{"id": "PAGE", "name": "JARZ"}, {"id": "PSID1", "name": "Sara Ali"}]}}
			]
		}
		with (
			self.mock.patch.object(self.meta, "_settings", return_value={"facebook_page_id": "PAGE"}),
			self.mock.patch.object(self.meta, "_request", return_value=thread) as req,
		):
			self.assertEqual(self.meta.dm_contact_name("messenger", "PSID1"), "Sara Ali")
			self.assertIsNone(self.meta.dm_contact_name("messenger", "OTHER"))
		self.assertEqual(req.call_args.args[1], "PAGE/conversations")
		self.assertEqual(req.call_args.kwargs["params"]["user_id"], "OTHER")

	def test_instagram_falls_back_to_username(self):
		with self.mock.patch.object(self.meta, "_request", return_value={"username": "mona.k"}):
			self.assertEqual(self.meta.dm_contact_name("instagram", "IGSID"), "mona.k")

	def test_lookup_is_dm_only_and_never_raises(self):
		with self.mock.patch.object(self.meta, "dm_contact_name", side_effect=self.meta.MetaApiError("x", 100)):
			self.assertIsNone(ing._lookup_dm_name(cc.MESSENGER, "PSID1"))
		with self.mock.patch.object(self.meta, "dm_contact_name", return_value="Sara") as lookup:
			self.assertIsNone(ing._lookup_dm_name(cc.WHATSAPP, "201000000000"))
			self.assertIsNone(ing._lookup_dm_name(cc.FACEBOOK_COMMENT, "c1"))
			lookup.assert_not_called()


class ShouldDraftTest(unittest.TestCase):
	def ev(self, **kw):
		base = {"kind": "message", "direction": "Inbound", "message_type": "text", "body": "hi"}
		base.update(kw)
		return base

	def test_inbound_text_and_comment(self):
		self.assertTrue(ing.should_draft(self.ev()))
		self.assertTrue(ing.should_draft(self.ev(message_type="comment")))

	def test_captioned_image_only(self):
		self.assertTrue(ing.should_draft(self.ev(message_type="image", body="this one?")))
		self.assertFalse(ing.should_draft(self.ev(message_type="image", body="")))

	def test_no_draft_cases(self):
		self.assertFalse(ing.should_draft(self.ev(body="   ")))
		self.assertFalse(ing.should_draft(self.ev(direction="Outbound")))
		self.assertFalse(ing.should_draft(self.ev(kind="status")))
		self.assertFalse(ing.should_draft(self.ev(message_type="audio", body="")))
		self.assertFalse(ing.should_draft(self.ev(message_type="reaction", body="x")))


if __name__ == "__main__":
	unittest.main()
