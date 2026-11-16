"""Delivered / read receipts on Messenger and Instagram, and per-message live status updates.

Messenger ``read`` / ``delivery`` watermarks and the Instagram ``read`` mid become one
``read_upto`` event; ``_apply_read_upto`` raises earlier Outbound rows only. The webhook
then publishes each changed message (capped), not just the conversation summary.

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

WM_MS = 1760608800000


def messaging(item, obj="page", own=("PAGE1",)):
	payload = {"object": obj, "entry": [{"id": "PAGE1", "time": 1, "messaging": [item]}]}
	return ing.parse_webhook(payload, set(own))


class ParseTest(unittest.TestCase):
	def test_messenger_read_watermark(self):
		item = {"sender": {"id": "PSID1"}, "recipient": {"id": "PAGE1"}, "timestamp": WM_MS + 5,
			"read": {"watermark": WM_MS}}
		(e,) = messaging(item)
		self.assertEqual(e["kind"], "read_upto")
		self.assertEqual((e["channel"], e["external_id"], e["status"]), (cc.MESSENGER, "PSID1", "read"))
		self.assertEqual(e["timestamp"], WM_MS / 1000.0)
		self.assertIsNone(e["external_message_id"])

	def test_messenger_delivery_watermark_without_mids(self):
		item = {"sender": {"id": "PSID1"}, "recipient": {"id": "PAGE1"}, "timestamp": WM_MS,
			"delivery": {"watermark": WM_MS}}
		(e,) = messaging(item)
		self.assertEqual((e["kind"], e["status"], e["timestamp"]), ("read_upto", "delivered", WM_MS / 1000.0))

	def test_messenger_delivery_with_mids_stays_per_message(self):
		item = {"sender": {"id": "PSID1"}, "recipient": {"id": "PAGE1"}, "timestamp": WM_MS,
			"delivery": {"mids": ["m_1", "m_2"], "watermark": WM_MS}}
		events = messaging(item)
		self.assertEqual([e["kind"] for e in events], ["status", "status"])

	def test_instagram_read_by_mid(self):
		item = {"sender": {"id": "IGSID1"}, "recipient": {"id": "PAGE1"}, "timestamp": WM_MS,
			"read": {"mid": "mid.abc"}}
		(e,) = messaging(item, obj="instagram")
		self.assertEqual((e["kind"], e["channel"], e["status"]), ("read_upto", cc.INSTAGRAM, "read"))
		self.assertEqual((e["external_id"], e["external_message_id"]), ("IGSID1", "mid.abc"))
		self.assertEqual(e["timestamp"], 0.0)

	def test_instagram_read_accepts_a_watermark(self):
		item = {"sender": {"id": "IGSID1"}, "recipient": {"id": "PAGE1"}, "timestamp": WM_MS,
			"read": {"watermark": WM_MS}}
		(e,) = messaging(item, obj="instagram")
		self.assertEqual(e["timestamp"], WM_MS / 1000.0)

	def test_our_own_side_is_ignored(self):
		for sender, recipient in (("PAGE1", "PSID1"), ("IGACC", "IGSID1")):
			item = {"sender": {"id": sender}, "recipient": {"id": recipient}, "timestamp": WM_MS,
				"read": {"watermark": WM_MS}}
			self.assertEqual(messaging(item, own=("PAGE1", "IGACC")), [])
		echo_delivery = {"sender": {"id": "PAGE1"}, "recipient": {"id": "PSID1"}, "timestamp": WM_MS,
			"delivery": {"watermark": WM_MS}}
		self.assertEqual(messaging(echo_delivery), [])

	def test_empty_read_is_dropped(self):
		item = {"sender": {"id": "PSID1"}, "recipient": {"id": "PAGE1"}, "timestamp": WM_MS, "read": {}}
		self.assertEqual(messaging(item), [])


class ApplyTest(unittest.TestCase):
	def run_apply(self, event, names=("m3", "m2", "m1"), stamp=None, conv="C1"):
		db = SimpleNamespace()
		db.get_value = mock.Mock(side_effect=lambda dt, flt, field, *a, **k: (
			conv if dt == cc.CONVERSATION else stamp))
		db.set_value = mock.Mock()
		get_all = mock.Mock(return_value=list(names))
		with (
			mock.patch.object(frappe, "db", db),
			mock.patch.object(frappe, "get_all", get_all, create=True),
		):
			out = ing._apply_read_upto(event, "UTC")
		return out, db, get_all

	def event(self, **kw):
		base = {"kind": "read_upto", "channel": cc.MESSENGER, "external_id": "PSID1",
			"status": "read", "timestamp": WM_MS / 1000.0, "external_message_id": None}
		return {**base, **kw}

	def test_only_outbound_lower_ranks_before_the_cutoff(self):
		out, db, get_all = self.run_apply(self.event())
		self.assertEqual(out, ("C1", ["m3", "m2", "m1"]))
		filters = get_all.call_args.kwargs["filters"]
		self.assertEqual(filters["direction"], "Outbound")
		self.assertEqual(filters["conversation"], "C1")
		self.assertEqual(filters["status"], ["in", ["sent", "delivered"]])
		self.assertEqual(filters["timestamp"][0], "<=")
		self.assertEqual(filters["timestamp"][1], ing.epoch_to_site(WM_MS / 1000.0, "UTC"))
		self.assertNotIn("failed", filters["status"][1])
		self.assertEqual(get_all.call_args.kwargs["order_by"], "timestamp desc, creation desc")
		db.set_value.assert_called_once_with(cc.MESSAGE, filters, "status", "read", update_modified=True)

	def test_delivered_only_lifts_sent(self):
		_, _, get_all = self.run_apply(self.event(status="delivered"))
		self.assertEqual(get_all.call_args.kwargs["filters"]["status"], ["in", ["sent"]])

	def test_instagram_mid_sets_the_cutoff_from_that_message(self):
		stamp = datetime.datetime(2026, 11, 16, 12, 0, 0)
		out, db, get_all = self.run_apply(
			self.event(channel=cc.INSTAGRAM, timestamp=0.0, external_message_id="mid.abc"), stamp=stamp
		)
		self.assertEqual(get_all.call_args.kwargs["filters"]["timestamp"], ["<=", stamp])
		self.assertIsNotNone(out)

	def test_unknown_mid_and_no_watermark_does_nothing(self):
		out, db, get_all = self.run_apply(
			self.event(timestamp=0.0, external_message_id="mid.zzz"), stamp=None
		)
		self.assertIsNone(out)
		db.set_value.assert_not_called()

	def test_nothing_to_update_skips_the_write(self):
		out, db, _ = self.run_apply(self.event(), names=())
		self.assertIsNone(out)
		db.set_value.assert_not_called()

	def test_no_conversation_does_nothing(self):
		out, db, _ = self.run_apply(self.event(), conv=None)
		self.assertIsNone(out)
		db.set_value.assert_not_called()

	def test_returned_names_are_capped_but_all_rows_update(self):
		names = [f"m{i}" for i in range(80)]
		out, db, _ = self.run_apply(self.event(), names=names)
		self.assertEqual(len(out[1]), ing.RECEIPT_NAMES_CAP)
		self.assertEqual(out[1][0], "m0")
		db.set_value.assert_called_once()


class PublishTest(unittest.TestCase):
	def publish(self, conv, names):
		published = []
		with (
			mock.patch.object(cc, "load_message", side_effect=lambda n: {"name": n}),
			mock.patch.object(
				cc, "publish_chat_update",
				side_effect=lambda c, k, **kw: published.append((c, k, kw.get("message"))),
			),
		):
			ing._publish_status_change(conv, names)
		return published

	def test_one_message_event_per_changed_message(self):
		out = self.publish("C1", ["m2", "m1"])
		self.assertEqual(out, [("C1", "message", {"name": "m2"}), ("C1", "message", {"name": "m1"})])

	def test_capped_per_conversation(self):
		out = self.publish("C1", [f"m{i}" for i in range(50)])
		self.assertEqual(len(out), ing.STATUS_PUBLISH_CAP)
		self.assertEqual(out[0][2], {"name": "m0"})

	def test_falls_back_to_the_summary_event(self):
		published = []
		with (
			mock.patch.object(cc, "load_message", return_value=None),
			mock.patch.object(
				cc, "publish_chat_update", side_effect=lambda c, k, **kw: published.append((c, k))
			),
		):
			ing._publish_status_change("C1", ["m1"])
		self.assertEqual(published, [("C1", "status")])

	def test_after_commit_publishes_changed_messages_not_a_second_summary(self):
		with (
			mock.patch.object(ing, "_publish_status_change") as pub,
			mock.patch.object(frappe, "db", SimpleNamespace(commit=lambda: None)),
		):
			ing._after_commit([], {"C1"}, {"C1": ["m1"]})
		pub.assert_called_once_with("C1", ["m1"])


class WebhookTest(unittest.TestCase):
	def test_read_upto_event_runs_in_a_savepoint_and_publishes(self):
		item = {"sender": {"id": "PSID1"}, "recipient": {"id": "PAGE1"}, "timestamp": WM_MS,
			"read": {"watermark": WM_MS}}
		payload = {"object": "page", "entry": [{"id": "PAGE1", "time": 1, "messaging": [item]}]}
		db = SimpleNamespace(savepoint=mock.Mock(), commit=mock.Mock(), rollback=mock.Mock())
		with (
			mock.patch.object(frappe, "db", db),
			mock.patch.object(frappe, "session", SimpleNamespace(user="Administrator")),
			mock.patch("jarz_pos.services.chat_settings.get_chat_settings", return_value={"enabled": True}),
			mock.patch.object(ing, "_own_ids", return_value={"PAGE1"}),
			mock.patch.object(ing, "_apply_read_upto", return_value=("C1", ["m2", "m1"])) as apply,
			mock.patch.object(ing, "_after_commit") as after,
		):
			result = ing.process_webhook(payload)
		apply.assert_called_once()
		db.savepoint.assert_called_once()
		self.assertEqual(result["statuses"], 2)
		after.assert_called_once_with([], {"C1"}, {"C1": ["m2", "m1"]})


if __name__ == "__main__":
	unittest.main()
