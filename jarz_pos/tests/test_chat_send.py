"""Chat inbox send rules (services/chat_send.py) -- pure functions, no site.

Pins the reply-window decision per channel (WhatsApp 24h then template only;
Messenger / Instagram 24h, then the HUMAN_AGENT tag up to 7 days; a comment
gets one private reply while its window is open, then public replies) and the
per-channel text limits. Needs no chat table: it runs before ``bench migrate``
in CI. Plain ``unittest.TestCase``; a stub ``frappe`` is registered when
frappe is not installed.
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

if not hasattr(frappe, "throw"):  # pragma: no cover - only under a bare stub

	class _StubValidationError(Exception):
		pass

	def _stub_throw(msg, exc=_StubValidationError, *a, **k):
		raise exc(msg) if isinstance(exc, type) and issubclass(exc, Exception) else _StubValidationError(msg)

	frappe.throw = _stub_throw

from jarz_pos.services import chat_common as cc
from jarz_pos.services import chat_send as s

NOW = datetime.datetime(2026, 10, 16, 12, 0)
H = datetime.timedelta(hours=1)
D = datetime.timedelta(days=1)


def route(channel, expires=None, last_in=None, private_sent=0):
	return s.decide_send_route(channel, NOW, expires, last_in, private_sent)


class WhatsAppRouteTest(unittest.TestCase):
	def test_open_window(self):
		r = route(cc.WHATSAPP, expires=NOW + 2 * H, last_in=NOW - 22 * H)
		self.assertEqual((r["ok"], r["route"], r["tag"], r["error"]), (True, s.ROUTE_WA, None, None))

	def test_closed_window_needs_a_template(self):
		r = route(cc.WHATSAPP, expires=NOW - 1 * H, last_in=NOW - 25 * H)
		self.assertEqual((r["ok"], r["error"]), (False, "wa_window_closed"))

	def test_no_window_at_all(self):
		self.assertEqual(route(cc.WHATSAPP)["error"], "wa_window_closed")

	def test_exact_expiry_is_closed(self):
		self.assertFalse(route(cc.WHATSAPP, expires=NOW)["ok"])


class DirectMessageRouteTest(unittest.TestCase):
	def test_within_24h_is_a_plain_reply(self):
		for channel in (cc.MESSENGER, cc.INSTAGRAM):
			r = route(channel, last_in=NOW - 23 * H)
			self.assertEqual((r["ok"], r["route"], r["tag"]), (True, s.ROUTE_DM, None), channel)

	def test_day_two_to_seven_uses_human_agent_tag(self):
		for channel in (cc.MESSENGER, cc.INSTAGRAM):
			r = route(channel, last_in=NOW - 3 * D)
			self.assertEqual((r["ok"], r["route"], r["tag"]), (True, s.ROUTE_DM, "HUMAN_AGENT"), channel)
			r = route(channel, last_in=NOW - 24 * H)
			self.assertEqual(r["tag"], "HUMAN_AGENT", channel)

	def test_after_seven_days_is_closed(self):
		for channel in (cc.MESSENGER, cc.INSTAGRAM):
			r = route(channel, last_in=NOW - 7 * D)
			self.assertEqual((r["ok"], r["error"]), (False, "dm_window_closed"), channel)

	def test_no_inbound_message_yet(self):
		r = route(cc.MESSENGER)
		self.assertEqual((r["ok"], r["error"]), (False, "no_inbound"))


class CommentRouteTest(unittest.TestCase):
	def test_first_reply_in_window_is_private(self):
		for channel in (cc.FACEBOOK_COMMENT, cc.INSTAGRAM_COMMENT):
			r = route(channel, expires=NOW + 3 * D, private_sent=0)
			self.assertEqual((r["ok"], r["route"]), (True, s.ROUTE_PRIVATE), channel)

	def test_after_the_private_reply_it_is_public(self):
		r = route(cc.FACEBOOK_COMMENT, expires=NOW + 3 * D, private_sent=1)
		self.assertEqual((r["ok"], r["route"]), (True, s.ROUTE_PUBLIC))

	def test_expired_private_window_falls_back_to_public(self):
		r = route(cc.INSTAGRAM_COMMENT, expires=NOW - 1 * H, private_sent=0)
		self.assertEqual((r["ok"], r["route"]), (True, s.ROUTE_PUBLIC))

	def test_unknown_channel(self):
		r = route("Telegram")
		self.assertEqual((r["ok"], r["error"]), (False, "unknown_channel"))


class TextLimitTest(unittest.TestCase):
	def test_limits(self):
		self.assertEqual(s.text_limit(cc.WHATSAPP), 4096)
		self.assertEqual(s.text_limit(cc.MESSENGER), 2000)
		self.assertEqual(s.text_limit(cc.INSTAGRAM), 1000)
		self.assertEqual(s.text_limit(cc.FACEBOOK_COMMENT), 4096)

	def test_validate_strips_and_accepts_the_limit(self):
		self.assertEqual(s.validate_text(cc.WHATSAPP, "  hi  "), "hi")
		self.assertEqual(len(s.validate_text(cc.INSTAGRAM, "x" * 1000)), 1000)

	def test_validate_rejects_empty_and_too_long(self):
		with self.assertRaises(Exception):
			s.validate_text(cc.WHATSAPP, "   ")
		with self.assertRaises(Exception):
			s.validate_text(cc.WHATSAPP, None)
		with self.assertRaises(Exception):
			s.validate_text(cc.INSTAGRAM, "x" * 1001)
		with self.assertRaises(Exception):
			s.validate_text(cc.MESSENGER, "x" * 2001)


if __name__ == "__main__":
	unittest.main()
