"""Kill switch: Jarz Chat Settings.enabled = 0 refuses every outbound Meta write with no network call.

Plain ``unittest.TestCase``; ``requests`` and the settings are mocked, no site needed.
"""

import sys
import types
import unittest
from types import SimpleNamespace
from unittest import mock

try:  # pragma: no cover - depends on the environment
	import frappe  # noqa: F401
except ImportError:  # pragma: no cover
	frappe = types.ModuleType("frappe")
	frappe.whitelist = lambda *a, **k: (a[0] if a and callable(a[0]) else (lambda fn: fn))
	frappe._ = lambda m: m
	frappe.local = SimpleNamespace()
	frappe.db = SimpleNamespace()
	sys.modules["frappe"] = frappe

from jarz_pos.services import chat_meta_client as meta

TOKENS = {
	"whatsapp_access_token": "wa-test",
	"page_access_token": "page-test",
	"whatsapp_phone_number_id": "111",
	"page_id": "222",
	"instagram_account_id": "333",
}


def _settings(enabled):
	return {**TOKENS, "enabled": enabled}


def _ok_response():
	resp = mock.Mock(status_code=200)
	resp.json.return_value = {"messages": [{"id": "wamid.X"}], "id": "m1", "message_id": "mid.1"}
	return resp


# Every outbound write the module exposes, called with plausible arguments.
OUTBOUND = {
	"wa_send_text": lambda: meta.wa_send_text("201000000000", "hi"),
	"wa_upload_media": lambda: meta.wa_upload_media(b"x", "a.jpg", "image/jpeg"),
	"wa_send_media": lambda: meta.wa_send_media("201000000000", "image", "mid1", "cap", None),
	"wa_send_reaction": lambda: meta.wa_send_reaction("201000000000", "wamid.A", "\U0001f44d"),
	"wa_send_template": lambda: meta.wa_send_template("201000000000", "t", "ar", []),
	"wa_request_app_sync": lambda: meta.wa_request_app_sync("history"),
	"messenger_send": lambda: meta.messenger_send("psid", "hi"),
	"ig_send": lambda: meta.ig_send("igsid", "hi"),
	"comment_private_reply": lambda: meta.comment_private_reply("c1", "hi"),
	"comment_public_reply": lambda: meta.comment_public_reply("c1", "hi"),
	"delete_comment": lambda: meta.delete_comment("c1"),
	"edit_comment": lambda: meta.edit_comment("c1", "hi"),
}


class ChatKillSwitchTests(unittest.TestCase):
	def test_disabled_refuses_every_outbound_call_without_network(self):
		with (
			mock.patch.object(meta, "get_chat_settings", return_value=_settings(False)),
			mock.patch("requests.request") as net,
			mock.patch("requests.get") as net_get,
		):
			for name, call in OUTBOUND.items():
				with self.subTest(path=name):
					with self.assertRaises(meta.MetaApiError) as ctx:
						call()
					self.assertEqual(ctx.exception.code, "chat_disabled")
					self.assertIn("Customer chat is switched off on this site", str(ctx.exception))
			self.assertFalse(meta.wa_mark_read("wamid.A"))
			net.assert_not_called()
			net_get.assert_not_called()

	def test_missing_flag_counts_as_off(self):
		with (
			mock.patch.object(meta, "get_chat_settings", return_value=dict(TOKENS)),
			mock.patch("requests.request") as net,
		):
			with self.assertRaises(meta.MetaApiError):
				meta.wa_send_text("201000000000", "hi")
			net.assert_not_called()

	def test_enabled_passes_through_to_the_network(self):
		with (
			mock.patch.object(meta, "get_chat_settings", return_value=_settings(True)),
			mock.patch("requests.request", return_value=_ok_response()) as net,
		):
			meta.wa_send_text("201000000000", "hi")
			meta.wa_send_reaction("201000000000", "wamid.A", "\U0001f44d")
			meta.edit_comment("c1", "hi")
			self.assertTrue(meta.wa_mark_read("wamid.A"))
			self.assertEqual(net.call_count, 4)

	def test_reads_stay_open_when_disabled(self):
		with (
			mock.patch.object(meta, "get_chat_settings", return_value=_settings(False)),
			mock.patch("requests.request", return_value=_ok_response()) as net,
		):
			meta.wa_media_info("m1")
			net.assert_called_once()
			self.assertEqual(net.call_args[0][0], "GET")


if __name__ == "__main__":
	unittest.main()
