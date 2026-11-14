"""Tests for the Shorebird update channel (fast = beta, normal = stable).

Runs against a stubbed ``frappe`` (same pattern as test_api_app_release) so it
works on the CI logic gate, which executes before ``bench migrate`` and cannot
see the new ``mobile_fast_update_roles`` / ``update_channel`` fields.

The bias under test is "fail to normal": an unset setting, a missing column,
a lookup that raises, a guest caller - all must answer normal/stable/default,
which is the channel every device was on before this feature existed.
"""

import importlib
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

NORMAL = {"channel": "normal", "track": "stable", "force_restart": False, "source": "default"}


class ChannelTestCase(unittest.TestCase):
	def _load_module(self, settings=None, user="manager@example.com", roles=None, device_rows=None):
		values = dict(settings or {})

		fake_frappe = types.ModuleType("frappe")
		fake_frappe.whitelist = lambda *args, **kwargs: (lambda fn: fn)
		fake_frappe.log_error = MagicMock()
		fake_frappe.get_traceback = MagicMock(return_value="traceback")
		fake_frappe.db = SimpleNamespace(
			get_single_value=MagicMock(side_effect=lambda _dt, field: values.get(field)),
			set_single_value=MagicMock(),
			commit=MagicMock(),
		)
		fake_frappe.utils = SimpleNamespace(get_url=MagicMock(return_value="https://erp.example.com"))
		fake_frappe.local = SimpleNamespace(request=None, form_dict={}, response={})
		fake_frappe.session = SimpleNamespace(user=user)
		fake_frappe.get_roles = MagicMock(return_value=list(roles or []))
		fake_frappe.get_all = MagicMock(return_value=list(device_rows or []))

		original = sys.modules.get("frappe")
		sys.modules["frappe"] = fake_frappe
		try:
			sys.modules.pop("jarz_pos.api.app_release", None)
			module = importlib.import_module("jarz_pos.api.app_release")
			module = importlib.reload(module)
		finally:
			if original is not None:
				sys.modules["frappe"] = original
			else:
				sys.modules.pop("frappe", None)
		return module


class TestResolveUpdateChannel(ChannelTestCase):
	def setUp(self):
		self.m = self._load_module()

	def test_constants(self):
		self.assertEqual(self.m.TRACK_FAST, "beta")
		self.assertEqual(self.m.TRACK_NORMAL, "stable")

	def test_everything_empty_is_normal_default(self):
		self.assertEqual(self.m.resolve_update_channel(None, [], "", 0), NORMAL)
		self.assertEqual(self.m.resolve_update_channel(None, ["POS Manager"], None, None), NORMAL)

	def test_role_match_is_fast(self):
		result = self.m.resolve_update_channel("Auto", ["POS User", "POS Manager"], "POS Manager\nSystem Manager", 0)
		self.assertEqual(result, {"channel": "fast", "track": "beta", "force_restart": False, "source": "role"})

	def test_role_parsing_strips_and_drops_blanks(self):
		result = self.m.resolve_update_channel(None, ["Owner"], "\n  Owner  \r\n\n", 1)
		self.assertEqual(result["channel"], "fast")
		self.assertTrue(result["force_restart"])

	def test_role_match_is_case_sensitive(self):
		self.assertEqual(self.m.resolve_update_channel(None, ["pos manager"], "POS Manager", 1), NORMAL)

	def test_no_role_match_is_normal(self):
		self.assertEqual(self.m.resolve_update_channel(None, ["POS User"], "POS Manager", 1), NORMAL)

	def test_device_fast_override_beats_roles(self):
		result = self.m.resolve_update_channel("Fast", ["POS User"], "", 0)
		self.assertEqual(result, {"channel": "fast", "track": "beta", "force_restart": False, "source": "device"})

	def test_device_normal_override_beats_fast_role(self):
		result = self.m.resolve_update_channel("Normal", ["POS Manager"], "POS Manager", 1)
		self.assertEqual(result, {"channel": "normal", "track": "stable", "force_restart": False, "source": "device"})

	def test_auto_and_junk_override_defer_to_roles(self):
		for value in ("Auto", "", "fast", "garbage", None):
			self.assertEqual(self.m.resolve_update_channel(value, [], "POS Manager", 0), NORMAL, value)

	def test_force_restart_only_on_fast(self):
		self.assertTrue(self.m.resolve_update_channel("Fast", [], "", 1)["force_restart"])
		self.assertTrue(self.m.resolve_update_channel("Fast", [], "", "1")["force_restart"])
		self.assertFalse(self.m.resolve_update_channel("Fast", [], "", 0)["force_restart"])
		self.assertFalse(self.m.resolve_update_channel("Normal", [], "", 1)["force_restart"])

	def test_iterable_fast_roles_accepted(self):
		self.assertEqual(self.m.resolve_update_channel(None, ["A"], ["A", " "], 0)["source"], "role")

	def test_bad_input_fails_to_normal(self):
		self.assertEqual(self.m.resolve_update_channel(None, 12345, "A", 0), NORMAL)


class TestGetUpdateChannel(ChannelTestCase):
	def test_unset_settings_is_normal(self):
		m = self._load_module(settings={}, roles=["POS Manager", "System Manager"])
		self.assertEqual(m.get_update_channel(), NORMAL)
		# Feature off: no need to even read roles.
		m.frappe.get_roles.assert_not_called()

	def test_role_gives_fast_with_force_restart(self):
		m = self._load_module(
			settings={"mobile_fast_update_roles": "POS Manager", "mobile_fast_force_restart": 1},
			roles=["POS Manager"],
		)
		self.assertEqual(
			m.get_update_channel(),
			{"channel": "fast", "track": "beta", "force_restart": True, "source": "role"},
		)

	def test_guest_is_normal(self):
		m = self._load_module(
			settings={"mobile_fast_update_roles": "Guest"}, user="Guest", roles=["Guest"],
			device_rows=[{"name": "D1", "user": "Guest", "update_channel": "Fast"}],
		)
		self.assertEqual(m.get_update_channel(token="tok"), NORMAL)

	def test_device_override_for_same_user(self):
		m = self._load_module(
			settings={"mobile_fast_force_restart": 1},
			user="staff@example.com",
			device_rows=[{"name": "D1", "user": "staff@example.com", "update_channel": "Fast"}],
		)
		self.assertEqual(
			m.get_update_channel(token="tok"),
			{"channel": "fast", "track": "beta", "force_restart": True, "source": "device"},
		)
		args, kwargs = m.frappe.get_all.call_args
		self.assertEqual(args[0], "Jarz Mobile Device")
		self.assertEqual(kwargs["filters"], {"token": "tok"})

	def test_device_normal_override_demotes_manager(self):
		m = self._load_module(
			settings={"mobile_fast_update_roles": "POS Manager"},
			roles=["POS Manager"],
			device_rows=[{"name": "D1", "user": "manager@example.com", "update_channel": "Normal"}],
		)
		self.assertEqual(m.get_update_channel(token="tok")["source"], "device")
		self.assertEqual(m.get_update_channel(token="tok")["channel"], "normal")

	def test_device_row_of_other_user_is_ignored(self):
		m = self._load_module(
			settings={},
			user="staff@example.com",
			device_rows=[{"name": "D1", "user": "manager@example.com", "update_channel": "Fast"}],
		)
		self.assertEqual(m.get_update_channel(token="tok"), NORMAL)

	def test_auto_device_row_falls_through_to_roles(self):
		m = self._load_module(
			settings={"mobile_fast_update_roles": "POS Manager"},
			roles=["POS Manager"],
			device_rows=[{"name": "D1", "user": "manager@example.com", "update_channel": "Auto"}],
		)
		self.assertEqual(m.get_update_channel(token="tok")["source"], "role")

	def test_no_token_skips_device_lookup(self):
		m = self._load_module(settings={})
		m.get_update_channel()
		m.get_update_channel(token="   ")
		m.frappe.get_all.assert_not_called()

	def test_device_lookup_failure_falls_back_to_roles(self):
		"""e.g. the update_channel column does not exist yet (pre-migrate)."""
		m = self._load_module(
			settings={"mobile_fast_update_roles": "POS Manager"}, roles=["POS Manager"]
		)
		m.frappe.get_all.side_effect = Exception("Unknown column 'update_channel'")
		self.assertEqual(m.get_update_channel(token="tok")["source"], "role")

	def test_settings_failure_is_normal(self):
		m = self._load_module(roles=["POS Manager"])
		m.frappe.db.get_single_value.side_effect = Exception("db down")
		self.assertEqual(m.get_update_channel(), NORMAL)

	def test_get_roles_failure_is_normal_and_never_raises(self):
		m = self._load_module(settings={"mobile_fast_update_roles": "POS Manager"})
		m.frappe.get_roles.side_effect = Exception("boom")
		m.frappe.log_error.side_effect = Exception("log_error can raise too")
		self.assertEqual(m.get_update_channel(), NORMAL)

	def test_missing_session_is_normal(self):
		m = self._load_module(settings={"mobile_fast_update_roles": "POS Manager"}, roles=["POS Manager"])
		del m.frappe.session
		self.assertEqual(m.get_update_channel(), NORMAL)


class TestDuplicateDeviceKeepsOverride(unittest.TestCase):
	"""Re-registration prunes duplicate token rows; a Fast/Normal override must survive."""

	def _load_notifications(self):
		fake_frappe = types.ModuleType("frappe")
		fake_frappe._ = lambda message: message
		fake_frappe.whitelist = lambda *args, **kwargs: (lambda fn: fn)
		fake_frappe.throw = MagicMock(side_effect=Exception)
		fake_frappe.log_error = MagicMock()
		fake_frappe.get_traceback = MagicMock(return_value="traceback")
		fake_frappe.logger = MagicMock(return_value=SimpleNamespace(info=MagicMock()))
		fake_frappe.delete_doc = MagicMock()
		fake_frappe.get_all = MagicMock(return_value=[])
		fake_frappe.get_doc = MagicMock(return_value=SimpleNamespace(name="DEV-KEEP"))
		fake_frappe.db = SimpleNamespace(set_value=MagicMock())
		fake_frappe.session = SimpleNamespace(user="operator@example.com")
		fake_frappe.utils = SimpleNamespace(now_datetime=MagicMock(return_value="NOW"))
		fake_frappe.conf = {}

		original = sys.modules.get("frappe")
		sys.modules["frappe"] = fake_frappe
		try:
			sys.modules.pop("jarz_pos.api.notifications", None)
			module = importlib.import_module("jarz_pos.api.notifications")
			module = importlib.reload(module)
		finally:
			if original is not None:
				sys.modules["frappe"] = original
			else:
				sys.modules.pop("frappe", None)
		return module

	ROWS = [
		{"name": "DEV-KEEP", "user": "operator@example.com", "enabled": 1, "modified": "2"},
		{"name": "DEV-OLD", "user": "operator@example.com", "enabled": 0, "modified": "1"},
	]
	PAYLOAD = {"token": "tok", "user": "operator@example.com", "enabled": 1}

	def _written_values(self, n):
		args, _kwargs = n.frappe.db.set_value.call_args
		self.assertEqual(args[1], "DEV-KEEP")
		return args[2]

	def test_kept_auto_row_inherits_override_from_pruned_row(self):
		n = self._load_notifications()
		n.frappe.get_all.side_effect = [
			list(self.ROWS),
			[
				{"name": "DEV-KEEP", "user": "operator@example.com", "update_channel": "Auto"},
				{"name": "DEV-OLD", "user": "operator@example.com", "update_channel": "Fast"},
			],
		]
		n._upsert_mobile_device(dict(self.PAYLOAD))
		self.assertEqual(self._written_values(n)["update_channel"], "Fast")
		n.frappe.delete_doc.assert_called_once_with(
			"Jarz Mobile Device", "DEV-OLD", ignore_permissions=True, force=True
		)

	def test_kept_override_is_not_replaced(self):
		n = self._load_notifications()
		n.frappe.get_all.side_effect = [
			list(self.ROWS),
			[
				{"name": "DEV-KEEP", "user": "operator@example.com", "update_channel": "Normal"},
				{"name": "DEV-OLD", "user": "operator@example.com", "update_channel": "Fast"},
			],
		]
		n._upsert_mobile_device(dict(self.PAYLOAD))
		self.assertNotIn("update_channel", self._written_values(n))

	def test_lookup_failure_leaves_registration_unchanged(self):
		n = self._load_notifications()
		n.frappe.get_all.side_effect = [list(self.ROWS), Exception("Unknown column 'update_channel'")]
		n._upsert_mobile_device(dict(self.PAYLOAD))
		self.assertNotIn("update_channel", self._written_values(n))

	def test_single_row_never_writes_update_channel(self):
		n = self._load_notifications()
		n.frappe.get_all.side_effect = [[dict(self.ROWS[0])]]
		n._upsert_mobile_device(dict(self.PAYLOAD))
		self.assertNotIn("update_channel", self._written_values(n))
		self.assertEqual(n.frappe.get_all.call_count, 1)


if __name__ == "__main__":
	unittest.main()
