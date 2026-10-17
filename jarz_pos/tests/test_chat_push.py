"""Chat inbox instant push (services/chat_alerts.py push_new_message) -- mocked, no site.

Pins that a customer message pushes moderators at once with the contact as
title and the message as body, that a burst from one chat gives one push per
throttle window, that a test-run suppression and a failure never raise, that
the webhook job only pushes for inbound non-reaction messages, and that the
"Open" tab's ``exclude_closed`` filter keeps Answered chats but drops Closed.

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

import jarz_pos.api as jarz_api
from jarz_pos.services import chat_alerts as ca
from jarz_pos.services import chat_common as cc
from jarz_pos.services import chat_ingest as ing


class FakeCache:
	"""Just enough of RedisWrapper for ``set(..., ex=, nx=True)``."""

	def __init__(self):
		self.keys = {}

	def make_key(self, key):
		return f"site|{key}"

	def set(self, key, value, ex=None, nx=False):
		if nx and key in self.keys:
			return None
		self.keys[key] = value
		return True


def fake_notifications(suppressed=False):
	n = types.ModuleType("jarz_pos.api.notifications")
	n.CHAT_WAITING_NOTIFICATION_TYPE = "chat_waiting"
	n.outbound_alerts_suppressed = mock.Mock(return_value=suppressed)
	n._get_token_targets_for_users = mock.Mock(return_value=(["tok1", "tok2"], {"tok1": "Android"}))
	n._get_vapid_subscriptions_for_users = mock.Mock(return_value=[])
	n._send_fcm_notifications = mock.Mock(return_value={"success_count": 2})
	n._send_vapid_notifications = mock.Mock(return_value={"success_count": 0})
	return n


class PushNewMessageTest(unittest.TestCase):
	def setUp(self):
		self.n = fake_notifications()
		self.cache = FakeCache()
		rows = {
			cc.CONVERSATION: SimpleNamespace(channel="WhatsApp", contact_name="Mona", assigned_to=None),
			cc.MESSAGE: SimpleNamespace(body="عايز اطلب  تشيز كيك", message_type="text"),
		}
		db = SimpleNamespace(get_value=lambda doctype, name, fields, as_dict=False: rows.get(doctype))
		self.patches = [
			mock.patch.dict(sys.modules, {"jarz_pos.api.notifications": self.n}),
			mock.patch.object(jarz_api, "notifications", self.n, create=True),
			mock.patch.object(frappe, "cache", lambda: self.cache, create=True),
			mock.patch.object(frappe, "db", db),
			mock.patch.object(ca, "_alert_recipients", lambda assigned_to: ["mod@example.com"]),
		]
		for p in self.patches:
			p.start()

	def tearDown(self):
		for p in reversed(self.patches):
			p.stop()

	def test_pushes_contact_and_message(self):
		self.assertEqual(ca.push_new_message("conv1", "msg1"), 2)
		data = self.n._send_fcm_notifications.call_args[0][1]
		self.assertEqual(data["type"], "chat_waiting")
		self.assertEqual(data["notification_id"], "chat-conv1")
		self.assertEqual(data["route"], "/chats/conv1")
		self.assertEqual(data["conversation"], "conv1")
		self.assertEqual(data["title"], "Mona")
		self.assertIn("تشيز كيك", data["body"])

	def test_burst_is_throttled_per_conversation(self):
		ca.push_new_message("conv1", "msg1")
		self.assertEqual(ca.push_new_message("conv1", "msg2"), 0)
		self.assertEqual(self.n._send_fcm_notifications.call_count, 1)
		ca.push_new_message("conv2", "msg3")
		self.assertEqual(self.n._send_fcm_notifications.call_count, 2)

	def test_suppressed_sends_nothing(self):
		self.n.outbound_alerts_suppressed.return_value = True
		self.assertEqual(ca.push_new_message("conv1", "msg1"), 0)
		self.n._send_fcm_notifications.assert_not_called()

	def test_failure_never_raises(self):
		self.n._send_fcm_notifications.side_effect = RuntimeError("fcm down")
		with mock.patch.object(cc, "safe_log_error") as log:
			self.assertEqual(ca.push_new_message("conv1", "msg1"), 0)
		log.assert_called_once()

	def test_no_recipients_sends_nothing(self):
		with mock.patch.object(ca, "_alert_recipients", lambda assigned_to: []):
			self.assertEqual(ca.push_new_message("conv1", "msg1"), 0)
		self.n._send_fcm_notifications.assert_not_called()


class MessageTextTest(unittest.TestCase):
	def test_title_falls_back_to_channel(self):
		self.assertEqual(ca.message_text("", "hi", "Instagram"), ("Instagram", "hi"))

	def test_body_is_trimmed(self):
		title, body = ca.message_text("Ali", "x" * 300)
		self.assertEqual(title, "Ali")
		self.assertEqual(len(body), 100)

	def test_empty_body(self):
		self.assertEqual(ca.message_text("Ali", "  "), ("Ali", "New message"))


class AfterCommitPushTest(unittest.TestCase):
	def run_after_commit(self, action):
		with (
			mock.patch.object(cc, "publish_chat_update"),
			mock.patch.object(cc, "load_message", return_value={}),
			mock.patch.object(frappe, "enqueue", create=True),
			mock.patch.object(frappe, "db", SimpleNamespace(commit=lambda: None)),
			mock.patch.object(ca, "push_new_message") as push,
		):
			ing._after_commit([action], set())
		return push

	def test_inbound_pushes(self):
		push = self.run_after_commit({"conversation": "c1", "message": "m1", "push": True})
		push.assert_called_once_with("c1", "m1")

	def test_outbound_or_reaction_does_not_push(self):
		push = self.run_after_commit({"conversation": "c1", "message": "m1", "push": False})
		push.assert_not_called()


class ExcludeClosedFilterTest(unittest.TestCase):
	def setUp(self):
		try:
			from jarz_pos.api import chat
		except Exception as exc:  # pragma: no cover - stub frappe lacks the api deps
			self.skipTest(f"jarz_pos.api.chat not importable here: {exc}")
		self.chat = chat

	def filters_for(self, **kwargs):
		with (
			mock.patch.object(self.chat, "_gate"),
			mock.patch.object(frappe, "get_all", return_value=[], create=True) as get_all,
			mock.patch.object(frappe.db, "count", return_value=0, create=True),
			mock.patch.object(cc, "pending_draft_conversations", return_value=set()),
		):
			self.chat.get_conversations(**kwargs)
		return get_all.call_args.kwargs["filters"]

	def test_open_tab_drops_closed_only(self):
		self.assertIn(
			[cc.CONVERSATION, "status", "!=", "Closed"], self.filters_for(status="all", exclude_closed=1)
		)

	def test_all_without_flag_is_unfiltered(self):
		self.assertEqual(self.filters_for(status="all"), [])

	def test_flag_ignored_with_explicit_status(self):
		self.assertEqual(
			self.filters_for(status="Answered", exclude_closed="1"),
			[[cc.CONVERSATION, "status", "=", "Answered"]],
		)


if __name__ == "__main__":
	unittest.main()
