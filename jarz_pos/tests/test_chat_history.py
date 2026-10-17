"""Chat history import (services/chat_history.py + the history paths of chat_ingest) -- mocked, no site.

Pins that WhatsApp history chunks become silent past messages (business
messages by ``to``, media placeholders as unsupported, progress and refusals
recorded), that address-book contacts are read, that history goes to the long
queue, that a Messenger / Instagram thread maps to the right customer and
direction, that an imported message never touches status, unread, waiting or
push and only moves the list order when newer, that quiet actions publish
nothing, and that the Page backfill pages through and survives a bad message.

Plain ``unittest.TestCase`` (no FrappeTestCase, see test_task_board.py); a
stub ``frappe`` is registered when frappe is not installed.
"""

import datetime
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
	_utils.now_datetime = lambda: datetime.datetime(2026, 10, 17, 12, 0)
	_utils.nowdate = lambda: "2026-10-17"
	sys.modules["frappe.utils"] = _utils
	frappe.utils = _utils

from jarz_pos.services import chat_common as cc
from jarz_pos.services import chat_history as ch
from jarz_pos.services import chat_ingest as ing

NOW = datetime.datetime(2026, 10, 17, 12, 0)


def history_payload(value):
	return {
		"object": "whatsapp_business_account",
		"entry": [{"id": "WABA", "changes": [{"field": "history", "value": value}]}],
	}


class WhatsAppHistoryParseTest(unittest.TestCase):
	def setUp(self):
		value = {
			"messaging_product": "whatsapp",
			"history": [
				{
					"metadata": {"phase": 0, "chunk_order": 1, "progress": 40},
					"threads": [
						{
							"id": "201111034268",
							"messages": [
								{
									"from": "201111034268",
									"id": "wamid.IN",
									"timestamp": "1760000000",
									"type": "text",
									"text": {"body": "عايز تورتة"},
								},
								{
									"from": "201061332266",
									"to": "201111034268",
									"id": "wamid.OUT",
									"timestamp": "1760000100",
									"type": "text",
									"text": {"body": "تمام"},
								},
								{
									"from": "201111034268",
									"id": "wamid.PH",
									"timestamp": "1760000200",
									"type": "media_placeholder",
								},
							],
						}
					],
				}
			],
		}
		self.events = ing.parse_webhook(history_payload(value))

	def by_kind(self, kind):
		return [e for e in self.events if e["kind"] == kind]

	def test_inbound_and_business_messages(self):
		messages = {e["external_message_id"]: e for e in self.by_kind("message")}
		self.assertEqual(messages["wamid.IN"]["direction"], "Inbound")
		self.assertEqual(messages["wamid.OUT"]["direction"], "Outbound")
		self.assertEqual(messages["wamid.OUT"]["sent_via"], "Phone")
		for event in messages.values():
			self.assertTrue(event["history"])
			self.assertEqual(event["external_id"], "201111034268")
			self.assertEqual(event["channel"], cc.WHATSAPP)

	def test_media_placeholder_is_unsupported(self):
		messages = {e["external_message_id"]: e for e in self.by_kind("message")}
		self.assertEqual(messages["wamid.PH"]["message_type"], "unsupported")
		self.assertEqual(messages["wamid.PH"]["raw"], {"type": "media_placeholder"})

	def test_progress_is_recorded(self):
		(progress,) = self.by_kind("history_progress")
		self.assertIn("progress 40%", progress["status"])

	def test_refusal_becomes_error_event(self):
		events = ing.parse_webhook(
			history_payload({"history": [{"errors": [{"code": 2593109, "title": "History sync declined"}]}]})
		)
		(err,) = [e for e in events if e["kind"] == "history_error"]
		self.assertIn("2593109", err["error"])

	def test_later_media_is_a_media_update(self):
		events = ing.parse_webhook(
			history_payload(
				{
					"messages": [
						{
							"from": "201111034268",
							"id": "wamid.PH",
							"timestamp": "1760000200",
							"type": "image",
							"image": {"id": "MEDIA1", "mime_type": "image/jpeg"},
						}
					]
				}
			)
		)
		(event,) = events
		self.assertTrue(event["media_update"])
		self.assertEqual(event["media_id"], "MEDIA1")

	def test_is_history_payload(self):
		self.assertTrue(ing.is_history_payload(history_payload({})))
		self.assertFalse(
			ing.is_history_payload(
				{"object": "whatsapp_business_account", "entry": [{"changes": [{"field": "messages"}]}]}
			)
		)
		self.assertFalse(ing.is_history_payload({"object": "page", "entry": []}))
		self.assertFalse(ing.is_history_payload(None))


class WhatsAppContactsParseTest(unittest.TestCase):
	def test_contacts(self):
		payload = {
			"object": "whatsapp_business_account",
			"entry": [
				{
					"changes": [
						{
							"field": "smb_app_state_sync",
							"value": {
								"state_sync": [
									{
										"type": "contact",
										"action": "add",
										"contact": {
											"full_name": "Mona Adel",
											"phone_number": "+20 111 103 4268",
										},
									},
									{
										"type": "contact",
										"action": "remove",
										"contact": {"full_name": "Gone", "phone_number": "201000000000"},
									},
									{"type": "contact", "action": "add", "contact": {"phone_number": "2012"}},
								]
							},
						}
					]
				}
			],
		}
		events = ing.parse_webhook(payload)
		self.assertEqual(len(events), 1)
		self.assertEqual(events[0]["kind"], "contact")
		self.assertEqual(events[0]["external_id"], "201111034268")
		self.assertEqual(events[0]["contact_name"], "Mona Adel")


class PageThreadEventsTest(unittest.TestCase):
	def thread(self, *messages):
		return {
			"participants": {"data": [{"id": "PAGE1", "name": "Jarz"}, {"id": "PSID9", "name": "Omar"}]},
			"messages": {"data": list(messages)},
		}

	def test_customer_and_direction(self):
		events = ch.thread_events(
			self.thread(
				{
					"id": "m2",
					"created_time": "2026-09-01T10:05:00+0000",
					"from": {"id": "PAGE1"},
					"message": "اهلا",
				},
				{
					"id": "m1",
					"created_time": "2026-09-01T10:00:00+0000",
					"from": {"id": "PSID9"},
					"message": "hi",
				},
			),
			cc.MESSENGER,
			{"PAGE1"},
		)
		by_id = {e["external_message_id"]: e for e in events}
		self.assertEqual(by_id["m1"]["direction"], "Inbound")
		self.assertEqual(by_id["m1"]["contact_name"], "Omar")
		self.assertEqual(by_id["m2"]["direction"], "Outbound")
		self.assertEqual(by_id["m2"]["sent_via"], "Phone")
		for event in events:
			self.assertEqual(event["external_id"], "PSID9")
			self.assertTrue(event["history"])
		self.assertEqual(
			by_id["m1"]["timestamp"],
			datetime.datetime(2026, 9, 1, 10, 0, tzinfo=datetime.timezone.utc).timestamp(),
		)

	def test_attachment_and_empty(self):
		events = ch.thread_events(
			self.thread(
				{
					"id": "m1",
					"created_time": "2026-09-01T10:00:00+0000",
					"from": {"id": "PSID9"},
					"attachments": {
						"data": [{"mime_type": "image/jpeg", "image_data": {"url": "https://cdn/x"}}]
					},
				},
				{"id": "m2", "created_time": "2026-09-01T10:01:00+0000", "from": {"id": "PSID9"}},
				{"created_time": "2026-09-01T10:02:00+0000", "from": {"id": "PSID9"}, "message": "no id"},
			),
			cc.INSTAGRAM,
			{"PAGE1"},
		)
		self.assertEqual([e["message_type"] for e in events], ["image", "unsupported"])
		self.assertEqual(events[0]["media_url"], "https://cdn/x")

	def test_no_customer(self):
		thread = {"participants": {"data": [{"id": "PAGE1"}]}, "messages": {"data": [{"id": "m1"}]}}
		self.assertEqual(ch.thread_events(thread, cc.MESSENGER, {"PAGE1"}), [])

	def test_sync_error_words(self):
		self.assertIn("one request", ch.explain_sync_error("2593107", "raw"))
		self.assertIn("onboard it again", ch.explain_sync_error("2593108/0", "raw"))
		self.assertEqual(ch.explain_sync_error(100, "raw"), "raw")


class HistoryPersistTest(unittest.TestCase):
	def run_persist(self, event, conv):
		doc = SimpleNamespace(name="MSG1", insert=mock.Mock())
		db = SimpleNamespace(set_value=mock.Mock())
		with (
			mock.patch.object(frappe, "get_doc", return_value=doc, create=True),
			mock.patch.object(frappe, "db", db),
			mock.patch.object(cc, "now", return_value=NOW),
		):
			action = ing._persist_history_message(event, conv, event["_when"])
		return action, db.set_value

	def conv(self, **values):
		base = {
			"name": "CONV1",
			"last_message_at": NOW - datetime.timedelta(days=1),
			"last_inbound_at": NOW - datetime.timedelta(days=1),
			"last_outbound_at": None,
			"window_expires_at": None,
		}
		base.update(values)
		return frappe._dict(base) if hasattr(frappe, "_dict") else _Dict(base)

	def event(self, when, direction="Inbound"):
		return {
			**ing._event(
				channel=cc.WHATSAPP,
				external_id="201111034268",
				direction=direction,
				external_message_id="wamid.X",
				body="old message",
				history=True,
			),
			"_when": when,
		}

	def test_older_message_moves_nothing_but_is_silent(self):
		action, set_value = self.run_persist(self.event(NOW - datetime.timedelta(days=30)), self.conv())
		set_value.assert_not_called()
		self.assertFalse(action["push"])
		self.assertTrue(action["quiet"])
		self.assertFalse(action["draft"])

	def test_newer_message_moves_order_only(self):
		action, set_value = self.run_persist(self.event(NOW - datetime.timedelta(hours=2)), self.conv())
		updates = set_value.call_args.args[2]
		self.assertEqual(updates["last_message_preview"], "old message")
		self.assertIn("last_inbound_at", updates)
		self.assertIn("window_expires_at", updates)
		for field in ("status", "unread_count", "waiting_since", "alert_sent_at"):
			self.assertNotIn(field, updates)
		self.assertEqual(set_value.call_args.kwargs.get("update_modified"), False)

	def test_expired_window_is_not_reopened(self):
		_action, set_value = self.run_persist(
			self.event(NOW - datetime.timedelta(hours=30), direction="Outbound"),
			self.conv(last_message_at=None),
		)
		updates = set_value.call_args.args[2]
		self.assertNotIn("window_expires_at", updates)
		self.assertEqual(updates["last_direction"], "Outbound")

	def test_media_for_unknown_message_is_dropped(self):
		event = ing._event(
			channel=cc.WHATSAPP,
			external_id="2011",
			external_message_id="wamid.Z",
			media_update=True,
			history=True,
		)
		with mock.patch.object(ing, "_message_exists", return_value=False):
			self.assertIsNone(ing._persist_message(event, {}, None))


class _Dict(dict):
	__getattr__ = dict.get


class QuietAfterCommitTest(unittest.TestCase):
	def test_quiet_action_publishes_nothing(self):
		with (
			mock.patch.object(cc, "publish_chat_update") as publish,
			mock.patch.object(cc, "load_message", return_value={}),
			mock.patch.object(frappe, "enqueue", create=True),
			mock.patch.object(frappe, "db", SimpleNamespace(commit=lambda: None)),
		):
			ing._after_commit([{"conversation": "c1", "message": "m1", "push": False, "quiet": True}], set())
		publish.assert_not_called()


class RequestWhatsAppHistoryTest(unittest.TestCase):
	def test_each_request_reports_alone(self):
		from jarz_pos.services import chat_meta_client as meta

		def fake(sync_type):
			if sync_type == "history":
				raise meta.MetaApiError("too late", "2593108")
			return {"request_id": "R1"}

		with mock.patch.object(meta, "wa_request_app_sync", side_effect=fake) as call:
			out = ch.request_whatsapp_history()
		self.assertEqual([c.args[0] for c in call.call_args_list], ["smb_app_state_sync", "history"])
		self.assertEqual(out["smb_app_state_sync"], {"ok": True, "request_id": "R1"})
		self.assertFalse(out["history"]["ok"])
		self.assertIn("onboard it again", out["history"]["error"])


class BackfillTest(unittest.TestCase):
	def test_pages_through_and_survives_a_bad_message(self):
		from jarz_pos.services import chat_meta_client as meta
		from jarz_pos.services import chat_settings

		def thread(psid, *ids):
			return {
				"participants": {"data": [{"id": "PAGE1"}, {"id": psid, "name": psid}]},
				"messages": {
					"data": [
						{
							"id": i,
							"created_time": "2026-09-01T10:00:00+0000",
							"from": {"id": psid},
							"message": i,
						}
						for i in ids
					]
				},
			}

		pages = [
			{
				"data": [thread("A", "a1", "a2")],
				"paging": {"cursors": {"after": "C1"}, "next": "https://next"},
			},
			{"data": [thread("B", "bad", "b2")], "paging": {"cursors": {"after": "C2"}}},
		]

		def persist(event, settings, tz):
			if event["external_message_id"] == "bad":
				raise ValueError("boom")
			return {"conversation": "c", "message": event["external_message_id"]}

		db = SimpleNamespace(savepoint=mock.Mock(), rollback=mock.Mock(), commit=mock.Mock())
		with (
			mock.patch.object(meta, "page_conversations", side_effect=pages) as fetch,
			mock.patch.object(chat_settings, "get_chat_settings", return_value={"facebook_page_id": "PAGE1"}),
			mock.patch.object(ing, "_persist_message", side_effect=persist),
			mock.patch.object(ing, "_site_tz", return_value=None),
			mock.patch.object(ch, "_store_result") as store,
			mock.patch.object(frappe, "db", db),
			mock.patch.object(frappe, "session", SimpleNamespace(user="Administrator")),
		):
			result = ch.backfill_page_history(cc.MESSENGER)
		self.assertEqual(result["status"], "done")
		self.assertEqual((result["threads"], result["messages"], result["errors"]), (2, 3, 1))
		self.assertEqual(fetch.call_args_list[1].kwargs["after"], "C1")
		self.assertEqual(db.commit.call_count, 2)
		store.assert_called_once()

	def test_unknown_channel_fails_cleanly(self):
		with (
			mock.patch.object(ch, "_store_result"),
			mock.patch.object(cc, "safe_log_error"),
			mock.patch.object(frappe, "db", SimpleNamespace(rollback=mock.Mock())),
			mock.patch.object(frappe, "session", SimpleNamespace(user="Administrator")),
		):
			result = ch.backfill_page_history(cc.WHATSAPP)
		self.assertEqual(result["status"], "failed")


if __name__ == "__main__":
	unittest.main()
