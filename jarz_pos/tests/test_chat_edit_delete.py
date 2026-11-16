"""Delete / edit a sent comment reply, and a customer's edit / delete (feature chat-message-edit-delete).

Refusal rules, Graph call shapes, success writes + publish, "Meta error changes nothing", the
pre-migrate column guard, the ``can_*`` flags, and the webhook shapes for customer edits / deletes.
No site: ``frappe`` is stubbed when it is not installed.
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
	sys.modules["frappe.utils"] = _utils
	frappe.utils = _utils

from jarz_pos.services import chat_common as cc
from jarz_pos.services import chat_ingest as ing
from jarz_pos.services import chat_meta_client as meta
from jarz_pos.services import chat_revise as rv

NOW = datetime.datetime(2026, 11, 16, 18, 0)


class Refused(Exception):
	"""What the patched ``frappe.throw`` raises."""


class _D(dict):
	__getattr__ = dict.get


def _throw(msg, exc=None, *a, **k):
	raise Refused(str(msg))


def msg_row(**over):
	base = {
		"name": "MSG1",
		"conversation": "CONV1",
		"direction": "Outbound",
		"status": "sent",
		"body": "Thanks!",
		"external_message_id": "1351417523779662_99001",
		"deleted_at": None,
		"edited_at": None,
		"original_body": None,
	}
	base.update(over)
	return _D(base)


class Harness(unittest.TestCase):
	def setUp(self):
		self.sets = []
		self.published = []
		self.row = msg_row()
		self.channel = cc.FACEBOOK_COMMENT
		self.has_cols = True
		self.calls = []
		self.meta_error = None

		def get_value(doctype, name=None, fields=None, as_dict=False):
			if doctype == cc.MESSAGE:
				return self.row
			return _D({"name": "CONV1", "channel": self.channel, "external_id": "POST_1"})

		def set_value(doctype, name, field, value=None, update_modified=True):
			self.sets.append((doctype, name, field, value))

		db = SimpleNamespace(get_value=get_value, set_value=set_value, has_column=lambda *a: self.has_cols)
		patches = [
			mock.patch.object(frappe, "db", db, create=True),
			mock.patch.object(frappe, "throw", _throw, create=True),
			mock.patch.object(frappe, "DoesNotExistError", Refused, create=True),
			mock.patch.object(cc, "now", lambda: NOW),
			mock.patch.object(cc, "has_revision_columns", lambda: self.has_cols),
			mock.patch.object(cc, "load_message", lambda name: {"name": name}),
			mock.patch.object(cc, "publish_chat_update", lambda *a, **k: self.published.append((a, k))),
			mock.patch.object(meta, "_settings", lambda: {"page_access_token": "PTOKEN"}),
			mock.patch.object(meta, "_request", self._request),
		]
		for p in patches:
			p.start()
			self.addCleanup(p.stop)

	def _request(self, method, path, token, **kw):
		self.calls.append((method, path, token, kw))
		if self.meta_error:
			raise self.meta_error
		return {"success": True}

	def written(self):
		"""Everything set_value wrote (a dict write is flattened)."""
		out = {}
		for _d_type, _name, field, value in self.sets:
			if isinstance(field, dict):
				out.update(field)
			else:
				out[field] = value
		return out


class DeleteTest(Harness):
	def test_facebook_comment_delete_calls_graph_and_marks_deleted(self):
		out = rv.delete_message("MSG1")
		self.assertEqual(self.calls, [("DELETE", "1351417523779662_99001", "PTOKEN", {})])
		w = self.written()
		self.assertEqual(w["deleted_at"], NOW)
		self.assertEqual(w["original_body"], "Thanks!")
		self.assertNotIn("body", w)  # the text is kept
		self.assertEqual(out, {"message": {"name": "MSG1"}})
		self.assertEqual(self.published[0][0][:2], ("CONV1", "message"))

	def test_instagram_comment_delete_allowed(self):
		self.channel = cc.INSTAGRAM_COMMENT
		rv.delete_message("MSG1")
		self.assertEqual(self.calls[0][0:2], ("DELETE", "1351417523779662_99001"))

	def test_inbound_refused(self):
		self.row = msg_row(direction="Inbound")
		with self.assertRaises(Refused):
			rv.delete_message("MSG1")
		self.assertEqual(self.calls, [])

	def test_dm_refused_with_correction_text(self):
		for channel in (cc.WHATSAPP, cc.MESSENGER, cc.INSTAGRAM):
			self.channel = channel
			with self.assertRaises(Refused) as ctx:
				rv.delete_message("MSG1")
			text = str(ctx.exception)
			self.assertIn("ابعت تصحيح بدالها", text)
			self.assertIn("send a correction instead", text)
		self.assertEqual(self.calls, [])

	def test_already_deleted_refused(self):
		self.row = msg_row(deleted_at=NOW)
		with self.assertRaises(Refused):
			rv.delete_message("MSG1")
		self.assertEqual(self.calls, [])

	def test_failed_message_refused(self):
		self.row = msg_row(status="failed")
		with self.assertRaises(Refused):
			rv.delete_message("MSG1")

	def test_meta_error_changes_nothing(self):
		self.meta_error = meta.MetaApiError("Unsupported delete request", "100")
		with self.assertRaises(Refused) as ctx:
			rv.delete_message("MSG1")
		self.assertIn("Delete failed", str(ctx.exception))
		self.assertEqual(self.sets, [])
		self.assertEqual(self.published, [])

	def test_pre_migrate_refused_before_calling_meta(self):
		self.has_cols = False
		with self.assertRaises(Refused):
			rv.delete_message("MSG1")
		self.assertEqual(self.calls, [])
		self.assertEqual(self.sets, [])


class EditTest(Harness):
	def test_facebook_edit_posts_message_and_keeps_original(self):
		rv.edit_message("MSG1", "  Thanks a lot!  ")
		self.assertEqual(self.calls[0][0:2], ("POST", "1351417523779662_99001"))
		self.assertEqual(self.calls[0][3], {"json_body": {"message": "Thanks a lot!"}})
		w = self.written()
		self.assertEqual(w["body"], "Thanks a lot!")
		self.assertEqual(w["edited_at"], NOW)
		self.assertEqual(w["original_body"], "Thanks!")
		self.assertEqual(self.published[0][0][:2], ("CONV1", "message"))

	def test_second_edit_keeps_first_original(self):
		self.row = msg_row(original_body="First", edited_at=NOW)
		rv.edit_message("MSG1", "Third")
		self.assertNotIn("original_body", self.written())

	def test_instagram_comment_edit_refused(self):
		self.channel = cc.INSTAGRAM_COMMENT
		with self.assertRaises(Refused):
			rv.edit_message("MSG1", "x")
		self.assertEqual(self.calls, [])

	def test_dm_edit_refused(self):
		self.channel = cc.MESSENGER
		with self.assertRaises(Refused) as ctx:
			rv.edit_message("MSG1", "x")
		self.assertIn("send a correction instead", str(ctx.exception))

	def test_empty_and_unchanged_text_refused(self):
		for text in ("", "   ", "Thanks!"):
			with self.assertRaises(Refused):
				rv.edit_message("MSG1", text)
		self.assertEqual(self.calls, [])

	def test_meta_error_changes_nothing(self):
		self.meta_error = meta.MetaApiError("boom", "1")
		with self.assertRaises(Refused):
			rv.edit_message("MSG1", "New")
		self.assertEqual(self.sets, [])
		self.assertEqual(self.published, [])

	def test_pre_migrate_refused(self):
		self.has_cols = False
		with self.assertRaises(Refused):
			rv.edit_message("MSG1", "New")
		self.assertEqual(self.calls, [])


class MetaClientTest(unittest.TestCase):
	def test_call_shapes(self):
		with (
			mock.patch.object(meta, "_settings", lambda: {"page_access_token": "T"}),
			mock.patch.object(meta, "_request", return_value={}) as req,
		):
			meta.delete_comment("C1", instagram=True)
			meta.edit_comment("C2", "hello")
		self.assertEqual(req.call_args_list[0].args, ("DELETE", "C1", "T"))
		self.assertEqual(req.call_args_list[1].args, ("POST", "C2", "T"))
		self.assertEqual(req.call_args_list[1].kwargs, {"json_body": {"message": "hello"}})


class FlagsTest(unittest.TestCase):
	def flags(self, channel, **over):
		row = {
			"name": "M",
			"direction": "Outbound",
			"status": "delivered",
			"external_message_id": "1351417523779662_99002",
			"deleted_at": None,
			**over,
		}
		with mock.patch.object(cc, "full_name", lambda u: None):
			out = cc.serialize_message(_D(row), channel)
		return out["can_delete"], out["can_edit"]

	def test_facebook_comment_both(self):
		self.assertEqual(self.flags(cc.FACEBOOK_COMMENT), (True, True))

	def test_instagram_comment_delete_only(self):
		self.assertEqual(self.flags(cc.INSTAGRAM_COMMENT), (True, False))

	def test_dms_neither(self):
		for ch in (cc.WHATSAPP, cc.MESSENGER, cc.INSTAGRAM):
			self.assertEqual(self.flags(ch), (False, False))

	def test_other_conditions_switch_both_off(self):
		for over in (
			{"direction": "Inbound"},
			{"status": "failed"},
			{"status": "queued"},
			{"external_message_id": None},
			{"deleted_at": NOW},
		):
			self.assertEqual(self.flags(cc.FACEBOOK_COMMENT, **over), (False, False), over)

	def test_shape_has_new_keys_none_before_migrate(self):
		with mock.patch.object(cc, "full_name", lambda u: None):
			out = cc.serialize_message(_D({"name": "M", "direction": "Inbound"}), cc.WHATSAPP)
		self.assertIsNone(out["deleted_at"])
		self.assertIsNone(out["edited_at"])
		self.assertIsNone(out["original_body"])
		self.assertFalse(out["can_delete"])


def wa_payload(message):
	message = {"from": "201111034268", "id": "wamid.REV", "timestamp": "1760608800", **message}
	value = {
		"messaging_product": "whatsapp",
		"metadata": {"phone_number_id": "PNID"},
		"contacts": [{"wa_id": "201111034268", "profile": {"name": "Sara"}}],
		"messages": [message],
	}
	return {
		"object": "whatsapp_business_account",
		"entry": [{"id": "WABA", "changes": [{"field": "messages", "value": value}]}],
	}


def dm_payload(item, obj="page"):
	base = {"sender": {"id": "PSID1"}, "recipient": {"id": "PAGE1"}, "timestamp": 1760608800000}
	return {"object": obj, "entry": [{"id": "PAGE1", "time": 1, "messaging": [{**base, **item}]}]}


class WebhookParseTest(unittest.TestCase):
	def one(self, payload):
		events = ing.parse_webhook(payload)
		self.assertEqual(len(events), 1)
		return events[0]

	def test_wa_revoke_type(self):
		ev = self.one(wa_payload({"type": "revoke", "revoke": {"original_message_id": "wamid.OLD"}}))
		self.assertEqual(ev["kind"], "revision")
		self.assertEqual((ev["revision"], ev["revision_target"]), ("delete", "wamid.OLD"))

	def test_wa_edit_object(self):
		edit = {"original_message_id": "wamid.OLD", "message": {"type": "text", "text": {"body": "fixed"}}}
		ev = self.one(wa_payload({"type": "edit", "edit": edit}))
		self.assertEqual((ev["revision"], ev["revision_target"], ev["body"]), ("edit", "wamid.OLD", "fixed"))

	def test_wa_context_edited(self):
		msg = {"type": "text", "text": {"body": "new text"}, "context": {"id": "wamid.OLD"}, "edited": True}
		ev = self.one(wa_payload(msg))
		self.assertEqual((ev["revision"], ev["revision_target"], ev["body"]), ("edit", "wamid.OLD", "new text"))

	def test_wa_plain_reply_with_context_is_still_a_message(self):
		msg = {"type": "text", "text": {"body": "yes"}, "context": {"id": "wamid.OLD"}}
		self.assertEqual(self.one(wa_payload(msg))["kind"], "message")

	def test_wa_unknown_revoke_shape_logged_by_structure_only(self):
		with mock.patch.object(cc, "safe_log_error") as log:
			ev = ing.parse_webhook(wa_payload({"type": "revoke", "revoke": {"secret_text": "hello"}}))
		self.assertEqual(ev, [])
		logged = log.call_args.args[1]
		self.assertNotIn("hello", logged)
		self.assertIn("secret_text", json.loads(logged)["message"]["revoke"])

	def test_messenger_message_edit(self):
		ev = self.one(dm_payload({"message_edit": {"mid": "m_1", "text": "better", "num_edit": 1}}))
		self.assertEqual(
			(ev["kind"], ev["channel"], ev["external_id"], ev["revision"]),
			("revision", cc.MESSENGER, "PSID1", "edit"),
		)
		self.assertEqual((ev["revision_target"], ev["body"]), ("m_1", "better"))

	def test_instagram_message_edit(self):
		ev = self.one(dm_payload({"message_edit": {"mid": "m_1", "text": "x"}}, obj="instagram"))
		self.assertEqual(ev["channel"], cc.INSTAGRAM)

	def test_is_deleted_marks_instead_of_bubble(self):
		for obj, channel in (("page", cc.MESSENGER), ("instagram", cc.INSTAGRAM)):
			ev = self.one(dm_payload({"message": {"mid": "m_9", "is_deleted": True}}, obj=obj))
			self.assertEqual((ev["kind"], ev["revision"], ev["revision_target"]), ("revision", "delete", "m_9"))
			self.assertEqual(ev["channel"], channel)

	def test_message_edit_without_mid_logged_and_dropped(self):
		with mock.patch.object(cc, "safe_log_error") as log:
			ev = ing.parse_webhook(dm_payload({"message_edit": {"text": "x"}}))
		self.assertEqual(ev, [])
		self.assertTrue(log.called)


class ApplyRevisionTest(Harness):
	def setUp(self):
		super().setUp()
		self.row = _D(
			{"name": "MSG7", "conversation": "CONV1", "body": "old", "original_body": None, "deleted_at": None}
		)

	def event(self, revision, body=""):
		return {"kind": "revision", "revision": revision, "revision_target": "m_1", "body": body}

	def test_customer_edit_sets_body_and_original(self):
		self.assertTrue(rv.apply_customer_revision(self.event("edit", "new")))
		w = self.written()
		self.assertEqual((w["body"], w["original_body"], w["edited_at"]), ("new", "old", NOW))
		self.assertEqual(self.published[0][0][:2], ("CONV1", "message"))

	def test_customer_delete_keeps_body(self):
		self.assertTrue(rv.apply_customer_revision(self.event("delete")))
		w = self.written()
		self.assertEqual((w["deleted_at"], w["original_body"]), (NOW, "old"))
		self.assertNotIn("body", w)

	def test_unknown_target_or_pre_migrate_does_nothing(self):
		original = self.row
		self.row = None
		self.assertFalse(rv.apply_customer_revision(self.event("delete")))
		self.row = original
		self.has_cols = False
		self.assertFalse(rv.apply_customer_revision(self.event("delete")))
		self.assertEqual(self.sets, [])


if __name__ == "__main__":  # pragma: no cover
	unittest.main()


class PrivateReplyIsNotACommentTest(unittest.TestCase):
	"""In a comment chat our private reply is a DM ("m_..."): Meta cannot delete or edit it."""

	def test_dm_mid_in_comment_chat_has_no_flags(self):
		row = {
			"name": "M",
			"direction": "Outbound",
			"status": "sent",
			"external_message_id": "m_AbC123-xyz",
			"deleted_at": None,
		}
		with mock.patch.object(cc, "full_name", lambda u: None):
			out = cc.serialize_message(_D(row), cc.FACEBOOK_COMMENT)
		self.assertEqual((out["can_delete"], out["can_edit"]), (False, False))

	def test_instagram_comment_id_is_digits_only(self):
		self.assertTrue(cc._COMMENT_ID.match("17890000000000001"))
		self.assertTrue(cc._COMMENT_ID.match("1351417523779662_99002"))
		self.assertFalse(cc._COMMENT_ID.match("aWdfRzE6"))
