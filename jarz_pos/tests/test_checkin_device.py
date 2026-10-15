"""Buddy-punch flagging: the device token rules and the attendance carry-through.

What matters here is the policy, not the plumbing:

* the SAME phone for the SAME person must never be flagged (a false flag trains
  managers to ignore the column);
* a shared phone outranks a new phone;
* nothing in the fingerprinting may ever raise -- it runs inside a check-in, and
  a failure there would stop a real person clocking in.
"""

import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from jarz_pos.services import attendance as attendance_service
from jarz_pos.services import checkin_device as dev

MOD = "jarz_pos.services.checkin_device"
TOKEN = "AbCdEfGhIjKlMnOpQrStUv"


class _Doc(SimpleNamespace):
    def get(self, key, default=None):
        return getattr(self, key, default)


def _doc(employee="HR-EMP-1"):
    return _Doc(employee=employee, time=datetime(2026, 10, 14, 13, 2), device_id=None)


def _run(registered, others=(), cookie=TOKEN, session_user="a@x.com", user_id="a@x.com"):
    """Run ``stamp_device`` with the world mocked; return (doc, set_value mock, cookie mgr)."""
    doc = _doc()
    request = SimpleNamespace(
        cookies={dev.COOKIE: cookie} if cookie else {},
        headers={"User-Agent": "UA/1.0"},
    )
    manager = MagicMock()
    meta = MagicMock()
    meta.has_field.return_value = True
    with patch(f"{MOD}.frappe") as fr, patch(f"{MOD}._others_same_day", return_value=list(others)):
        fr.local = SimpleNamespace(request=request, cookie_manager=manager, request_ip="1.2.3.4")
        fr.get_meta.return_value = meta
        fr.session = SimpleNamespace(user=session_user)
        fr.db.get_value.return_value = {"user_id": user_id, dev.EMPLOYEE_DEVICE_FIELD: registered}
        dev.stamp_device(doc)
        return doc, fr.db.set_value, manager


class TestDeviceRules(unittest.TestCase):
    def test_first_check_in_registers_without_flag(self):
        doc, set_value, _ = _run(registered=None)
        self.assertFalse(doc.get("custom_device_flag"))
        values = set_value.call_args[0][2]
        self.assertEqual(values[dev.EMPLOYEE_DEVICE_FIELD], TOKEN)
        self.assertIn(dev.EMPLOYEE_REGISTERED_ON_FIELD, values)
        self.assertEqual(doc.device_id, TOKEN[:12])
        self.assertEqual(doc.custom_device_token, TOKEN)

    def test_same_device_no_flag_no_reregistration(self):
        doc, set_value, _ = _run(registered=TOKEN)
        self.assertFalse(doc.get("custom_device_flag"))
        set_value.assert_not_called()

    def test_different_device_flags_new_and_does_not_reregister(self):
        doc, set_value, _ = _run(registered="SomeOtherToken123")
        self.assertEqual(doc.custom_device_flag, dev.FLAG_NEW)
        # The detail is shown on the attendance board: masked, never the cookie.
        self.assertNotIn(TOKEN, doc.custom_device_flag_detail)
        self.assertIn("…" + TOKEN[-dev.TOKEN_VISIBLE_TAIL:], doc.custom_device_flag_detail)
        # The full token stays on the check-in record for the approving manager.
        self.assertEqual(doc.custom_device_token, TOKEN)
        set_value.assert_not_called()

    def test_shared_device_outranks_new_device(self):
        others = [{"employee": "HR-EMP-7", "employee_name": "Ahmed", "time": datetime(2026, 10, 14, 12, 0)}]
        doc, set_value, _ = _run(registered="SomeOtherToken123", others=others)
        self.assertEqual(doc.custom_device_flag, dev.FLAG_SHARED)
        self.assertIn("HR-EMP-7 (Ahmed) at 12:00", doc.custom_device_flag_detail)
        set_value.assert_not_called()

    def test_shared_phone_is_not_registered_to_an_unregistered_employee(self):
        others = [{"employee": "HR-EMP-7", "employee_name": "Ahmed", "time": datetime(2026, 10, 14, 12, 0)}]
        doc, set_value, _ = _run(registered=None, others=others)
        self.assertEqual(doc.custom_device_flag, dev.FLAG_SHARED)
        set_value.assert_not_called()

    def test_missing_cookie_issues_one_and_sets_it_long_lived(self):
        doc, set_value, manager = _run(registered="SomeOtherToken123", cookie=None)
        args, kwargs = manager.set_cookie.call_args
        self.assertEqual(args[0], dev.COOKIE)
        self.assertNotEqual(args[1], TOKEN)
        self.assertTrue(kwargs["httponly"])
        self.assertEqual(kwargs["samesite"], "Lax")
        self.assertGreater((kwargs["expires"] - datetime.now()).days, 365 * 4)
        # Just-issued token + a different registered device is still "New Device".
        self.assertEqual(doc.custom_device_flag, dev.FLAG_NEW)

    def test_non_http_call_is_left_alone(self):
        doc = _doc()
        with patch(f"{MOD}.frappe") as fr:
            fr.local = SimpleNamespace()  # no request
            dev.stamp_device(doc)
        self.assertFalse(doc.get("custom_device_flag"))
        self.assertFalse(doc.get("custom_device_token"))

    def test_checkin_keyed_in_for_somebody_else_is_skipped(self):
        doc, set_value, manager = _run(registered=None, session_user="manager@x.com")
        self.assertFalse(doc.get("custom_device_token"))
        set_value.assert_not_called()
        manager.set_cookie.assert_not_called()

    def test_garbage_cookie_is_replaced(self):
        doc, _, manager = _run(registered=None, cookie="x;drop table")
        self.assertNotEqual(manager.set_cookie.call_args[0][1], "x;drop table")


class TestTokenMasking(unittest.TestCase):
    def test_mask_keeps_only_the_tail(self):
        self.assertEqual(dev.mask_token(TOKEN), "…" + TOKEN[-6:])
        self.assertEqual(dev.mask_token("abc"), "…")
        self.assertEqual(dev.mask_token(None), "")
        self.assertEqual(dev.mask_token(""), "")

    def test_a_legacy_detail_with_the_full_token_is_masked(self):
        legacy = (
            f"Different phone from the one registered for this employee (device token ({TOKEN})). "
            "To approve it, copy the Device Token from this check-in."
        )
        masked = dev.mask_tokens_in_detail(legacy)
        self.assertNotIn(TOKEN, masked)
        self.assertIn(f"token (…{TOKEN[-6:]})", masked)

    def test_names_in_parentheses_and_non_strings_are_untouched(self):
        shared = "Same phone checked in HR-EMP-7 (AhmedMohamedAli) at 12:00 today"
        self.assertEqual(dev.mask_tokens_in_detail(shared), shared)
        self.assertIsNone(dev.mask_tokens_in_detail(None))
        self.assertEqual(dev.mask_tokens_in_detail(""), "")


class TestNeverRaises(unittest.TestCase):
    def test_exception_inside_fingerprinting_is_swallowed(self):
        doc = _doc()
        with patch(f"{MOD}._stamp", side_effect=RuntimeError("boom")), patch(f"{MOD}.frappe") as fr:
            fr.log_error.side_effect = RuntimeError("log_error raised too")
            dev.stamp_device(doc)  # must not raise

    def test_db_failure_mid_stamp_is_swallowed(self):
        doc = _doc()
        with patch(f"{MOD}.frappe") as fr:
            fr.local = SimpleNamespace(request=SimpleNamespace(cookies={}, headers={}))
            fr.get_meta.side_effect = RuntimeError("db gone")
            dev.stamp_device(doc)


class TestAttendanceCarriesFlags(unittest.TestCase):
    def _group(self, *rows):
        return attendance_service.group_checkins(
            [
                {
                    "name": f"C{i}",
                    "employee": "HR-EMP-1",
                    "time": datetime(2026, 10, 14, 13 + i, 0),
                    "log_type": "IN",
                    **row,
                }
                for i, row in enumerate(rows)
            ]
        )[("HR-EMP-1", "2026-10-14")]

    def test_flags_are_distinct_ordered_and_details_joined(self):
        group = self._group(
            {"custom_device_flag": "New Device", "custom_device_flag_detail": "new phone"},
            {"custom_device_flag": "Shared Device", "custom_device_flag_detail": "same as EMP-7"},
            {"custom_device_flag": "New Device", "custom_device_flag_detail": "new phone"},
            {},
        )
        self.assertEqual(attendance_service.device_flags_for(group), ["Shared Device", "New Device"])
        self.assertEqual(attendance_service.device_detail_for(group), "new phone | same as EMP-7")

    def test_a_legacy_full_token_detail_is_masked_on_the_board(self):
        group = self._group(
            {"custom_device_flag": "New Device", "custom_device_flag_detail": f"device token ({TOKEN})"},
        )
        detail = attendance_service.device_detail_for(group)
        self.assertNotIn(TOKEN, detail)
        self.assertIn(TOKEN[-6:], detail)

    def test_cell_carries_new_keys_and_old_keys_survive(self):
        group = self._group({"custom_device_flag": "New Device", "custom_device_flag_detail": "d"})
        cell = attendance_service.build_cell(
            date_str="2026-10-14", shift_type="S", shift_location="L",
            scheduled_start="13:00", scheduled_end="22:00", group=group,
            day_off_row=None, is_holiday=False, is_cover=False, exempt=False,
            grace=10, today=datetime(2026, 10, 15).date(),
        )
        self.assertEqual(cell["device_flags"], ["New Device"])
        self.assertEqual(cell["device_flag_detail"], "d")
        for key in ("status", "checkin_count", "geo_ok", "is_cover", "first_in"):
            self.assertIn(key, cell)
        self.assertIn("device_flags", attendance_service._DAY_ROW_KEYS)

    def test_cell_without_checkins_has_empty_flags(self):
        cell = attendance_service.build_cell(
            date_str="2026-10-14", shift_type=None, shift_location=None,
            scheduled_start=None, scheduled_end=None, group=None,
            day_off_row=None, is_holiday=False, is_cover=False, exempt=False,
            grace=10, today=datetime(2026, 10, 15).date(),
        )
        self.assertEqual(cell["device_flags"], [])
        self.assertIsNone(cell["device_flag_detail"])
