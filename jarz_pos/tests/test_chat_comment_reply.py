"""Replies to Facebook / Instagram comments (chat_send + the comment path of chat_ingest) -- mocked, no site.

Pins the 2026-11-16 fix for CHAT-07779: a person who replied inside someone else's
comment thread got four "(#10901) Activity replying time expired" failures, because
the private reply named the thread's first comment (another person's, older than
7 days) instead of their own. Now the private reply names the person's newest
comment, a new comment re-opens the 7-day window and the one-private-reply
allowance, a refusal Meta will repeat switches the chat to public replies, and
staff read plain Arabic / English instead of Meta's text.

Plain ``unittest.TestCase``; a stub ``frappe`` is registered when frappe is not installed.
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
	_utils.now_datetime = lambda: datetime.datetime(2026, 11, 16, 18, 0)
	_utils.nowdate = lambda: "2026-11-16"
	sys.modules["frappe.utils"] = _utils
	frappe.utils = _utils

from jarz_pos.services import chat_common as cc
from jarz_pos.services import chat_ingest as ing
from jarz_pos.services import chat_send as s

NOW = datetime.datetime(2026, 11, 16, 18, 7, 42)


class _Dict(dict):
	__getattr__ = dict.get


def adict(values):
	return frappe._dict(values) if hasattr(frappe, "_dict") else _Dict(values)


class MetaErr(Exception):
	def __init__(self, message, code=None):
		super().__init__(message)
		self.code = code

	def __str__(self):
		return f"{self.args[0]} (code {self.code})"


class PrivateReplyTargetTest(unittest.TestCase):
	def conv(self):
		return adict({"name": "CHAT-07779", "external_id": "ROOT_NADA", "channel": cc.FACEBOOK_COMMENT})

	def test_targets_the_persons_newest_comment_not_the_thread_root(self):
		with mock.patch.object(frappe, "get_all", return_value=["C_MOSTAFA"], create=True) as get_all:
			self.assertEqual(s.private_reply_target(self.conv()), "C_MOSTAFA")
		kwargs = get_all.call_args.kwargs
		self.assertEqual(kwargs["filters"]["direction"], "Inbound")
		self.assertEqual(kwargs["filters"]["message_type"], "comment")
		self.assertTrue(kwargs["order_by"].startswith("timestamp desc"))

	def test_falls_back_to_the_thread_root(self):
		with mock.patch.object(frappe, "get_all", return_value=[], create=True):
			self.assertEqual(s.private_reply_target(self.conv()), "ROOT_NADA")
		with mock.patch.object(frappe, "get_all", side_effect=RuntimeError("db"), create=True):
			self.assertEqual(s.private_reply_target(self.conv()), "ROOT_NADA")

	def test_call_meta_sends_the_private_reply_to_that_comment(self):
		meta = types.ModuleType("jarz_pos.services.chat_meta_client")
		meta.comment_private_reply = mock.Mock(return_value="m_1")
		meta.comment_public_reply = mock.Mock(return_value="c_1")
		import jarz_pos.services as services_pkg

		# Both: `from jarz_pos.services import chat_meta_client` reads the package attribute
		# first once another test module has imported the real client.
		with (
			mock.patch.dict(sys.modules, {"jarz_pos.services.chat_meta_client": meta}),
			mock.patch.object(services_pkg, "chat_meta_client", meta, create=True),
			mock.patch.object(s, "private_reply_target", return_value="C_MOSTAFA"),
		):
			s._call_meta(self.conv(), s.ROUTE_PRIVATE, None, "hi")
			s._call_meta(self.conv(), s.ROUTE_PUBLIC, None, "hi")
		meta.comment_private_reply.assert_called_once_with("C_MOSTAFA", "hi", instagram=False)
		# A public reply stays on the thread root, so it lands in the same thread.
		meta.comment_public_reply.assert_called_once_with("ROOT_NADA", "hi", instagram=False)


class SendErrorTest(unittest.TestCase):
	def test_closed_private_reply_codes(self):
		self.assertTrue(s.private_reply_closed(MetaErr("Activity replying time expired", 10901)))
		self.assertTrue(s.private_reply_closed(MetaErr("Activity already replied to", "10900")))
		self.assertTrue(s.private_reply_closed(MetaErr("x", "10901/2018278")))
		self.assertTrue(
			s.private_reply_closed(MetaErr("Private replies to Facebook Pages are not allowed.", 100))
		)
		self.assertFalse(s.private_reply_closed(MetaErr("Meta API unreachable", "network")))
		self.assertFalse(s.private_reply_closed(RuntimeError("boom")))

	def test_expired_comment_reads_plainly_in_both_languages(self):
		text = s.friendly_send_error(MetaErr("Activity replying time expired", 10901), s.ROUTE_PRIVATE)
		self.assertIn("7 أيام", text)
		self.assertIn("more than 7 days old", text)
		self.assertIn("Send again", text)
		self.assertIn("10901", text)  # Meta's own words stay at the end for support

	def test_already_replied_and_page_author(self):
		self.assertIn(
			"already got its private message",
			s.friendly_send_error(MetaErr("Activity already replied to", 10900), s.ROUTE_PRIVATE),
		)
		self.assertIn(
			"written by a Page",
			s.friendly_send_error(
				MetaErr("Private replies to Facebook Pages are not allowed", 100), s.ROUTE_PRIVATE
			),
		)

	def test_permission_error_points_to_business_suite(self):
		text = s.friendly_send_error(
			MetaErr("(#200) The permission(s) pages_manage_engagement are not available", 200), s.ROUTE_PUBLIC
		)
		self.assertIn("Meta Business Suite", text)

	def test_other_errors_pass_through(self):
		self.assertEqual(s.friendly_send_error(RuntimeError("boom"), s.ROUTE_DM), "boom")


class SendTextFallbackTest(unittest.TestCase):
	def run_send(self, exc):
		conv = adict(
			{
				"name": "CHAT-07779",
				"channel": cc.FACEBOOK_COMMENT,
				"external_id": "ROOT_NADA",
				"status": "Open",
				"assigned_to": None,
				"window_expires_at": NOW + datetime.timedelta(days=7),
				"last_inbound_at": NOW,
				"private_reply_sent": 0,
			}
		)
		db = SimpleNamespace(
			get_value=mock.Mock(return_value=conv), set_value=mock.Mock(), commit=mock.Mock()
		)
		doc = SimpleNamespace(name="MSG1", insert=mock.Mock())

		class Thrown(Exception):
			pass

		def throw(msg, *a, **k):
			raise Thrown(msg)

		with (
			mock.patch.object(frappe, "db", db),
			mock.patch.object(frappe, "get_doc", return_value=doc, create=True),
			mock.patch.object(frappe, "throw", side_effect=throw, create=True),
			mock.patch.object(cc, "now", return_value=NOW + datetime.timedelta(minutes=2)),
			mock.patch.object(cc, "load_message", return_value={}),
			mock.patch.object(cc, "publish_chat_update"),
			mock.patch.object(s, "_call_meta", side_effect=exc),
		):
			with self.assertRaises(Thrown) as ctx:
				s.send_text("CHAT-07779", "hello", "staff@jarz")
		return db.set_value, str(ctx.exception)

	def test_expired_private_reply_switches_the_chat_to_public(self):
		set_value, message = self.run_send(MetaErr("Activity replying time expired", 10901))
		calls = [c.args for c in set_value.call_args_list]
		self.assertIn((cc.CONVERSATION, "CHAT-07779", "private_reply_sent", 1), calls)
		failed = next(c for c in calls if c[0] == cc.MESSAGE)
		self.assertEqual(failed[2]["status"], "failed")
		self.assertIn("more than 7 days old", failed[2]["error"])
		self.assertIn("Send again", message)

	def test_network_failure_keeps_the_private_route(self):
		set_value, _message = self.run_send(MetaErr("Meta API unreachable", "network"))
		calls = [c.args for c in set_value.call_args_list]
		self.assertFalse(any(c[0] == cc.CONVERSATION for c in calls))


class IngestCommentWindowTest(unittest.TestCase):
	def run_persist(self, conv, when):
		event = ing._event(
			channel=cc.FACEBOOK_COMMENT,
			external_id="ROOT_NADA",
			contact_name="Mostafa Megahed",
			direction="Inbound",
			external_message_id="C_NEW",
			message_type="comment",
			body="اشتريه منين؟",
		)
		event["timestamp"] = None
		doc = SimpleNamespace(name="MSG1", insert=mock.Mock())
		db = SimpleNamespace(set_value=mock.Mock())
		with (
			mock.patch.object(ing, "_message_exists", return_value=False),
			mock.patch.object(ing, "_find_conversation", return_value=conv),
			mock.patch.object(frappe, "get_doc", return_value=doc, create=True),
			mock.patch.object(frappe, "db", db),
			mock.patch.object(cc, "now", return_value=when),
			mock.patch.object(ing, "link_by_typed_phone", return_value={}),
		):
			ing._persist_message(event, {}, None)
		return db.set_value.call_args.args[2]

	def conv(self, **values):
		base = {
			"name": "CHAT-07779",
			"channel": cc.FACEBOOK_COMMENT,
			"contact_name": "Mostafa Megahed",
			"customer": "CUST-1",
			"status": "Answered",
			"last_message_at": NOW - datetime.timedelta(days=9),
			"last_inbound_at": NOW - datetime.timedelta(days=9),
			"window_expires_at": NOW - datetime.timedelta(days=2),
			"private_reply_sent": 1,
			"source_post_id": "P1",
		}
		base.update(values)
		return adict(base)

	def test_new_comment_reopens_the_window_and_the_private_reply(self):
		updates = self.run_persist(self.conv(), NOW)
		self.assertEqual(updates["window_expires_at"], NOW + datetime.timedelta(days=ing.COMMENT_WINDOW_DAYS))
		self.assertEqual(updates["private_reply_sent"], 0)

	def test_first_comment_of_a_new_chat_opens_the_window(self):
		updates = self.run_persist(
			self.conv(
				last_inbound_at=None, last_message_at=None, window_expires_at=None, private_reply_sent=0
			),
			NOW,
		)
		self.assertEqual(updates["window_expires_at"], NOW + datetime.timedelta(days=ing.COMMENT_WINDOW_DAYS))
		self.assertNotIn("private_reply_sent", updates)

	def test_an_older_comment_changes_neither(self):
		updates = self.run_persist(self.conv(last_inbound_at=NOW), NOW - datetime.timedelta(hours=1))
		self.assertNotIn("window_expires_at", updates)
		self.assertNotIn("private_reply_sent", updates)


if __name__ == "__main__":
	unittest.main()
