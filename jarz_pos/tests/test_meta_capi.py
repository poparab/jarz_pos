"""Meta CAPI chat orders (services/meta_capi.py) -- pure functions, no site needed.

Plain ``unittest.TestCase`` (no FrappeTestCase, see test_task_board.py); a
stub ``frappe`` is registered when frappe is not installed. The Graph API call
is exercised with ``requests.post`` patched, so nothing leaves the machine.
"""

import datetime
import hashlib
import json
import os
import sys
import types
import unittest
from types import SimpleNamespace
from unittest import mock

try:  # pragma: no cover - depends on the environment
    import frappe  # noqa: F401
except ImportError:  # pragma: no cover
    _fake = types.ModuleType("frappe")
    _fake.whitelist = lambda *a, **k: (a[0] if a and callable(a[0]) else (lambda fn: fn))
    _fake._ = lambda m: m
    _fake.local = SimpleNamespace()
    _fake.session = SimpleNamespace(user="Guest")
    _fake.db = SimpleNamespace()
    _fake.log_error = lambda *a, **k: None
    _fake.get_traceback = lambda *a, **k: ""
    sys.modules["frappe"] = _fake

import jarz_pos  # noqa: E402
from jarz_pos.services import meta_capi as m  # noqa: E402

APP_DIR = os.path.dirname(os.path.abspath(jarz_pos.__file__))
D = datetime.datetime


def sha(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class EventIdTest(unittest.TestCase):
    def test_amendment_suffix_is_dropped(self):
        self.assertEqual(m.event_id_for("ACC-SINV-2026-18128-1", "ACC-SINV-2026-18128"), "ACC-SINV-2026-18128")
        self.assertEqual(m.event_id_for("ACC-SINV-2026-18128-12", "ACC-SINV-2026-18128-11"), "ACC-SINV-2026-18128")

    def test_original_invoice_keeps_its_serial(self):
        self.assertEqual(m.event_id_for("ACC-SINV-2026-18128"), "ACC-SINV-2026-18128")
        self.assertEqual(m.event_id_for("ACC-SINV-2026-18128", None), "ACC-SINV-2026-18128")
        self.assertEqual(m.event_id_for("ACC-SINV-2026-18128", ""), "ACC-SINV-2026-18128")

    def test_original_and_amendment_share_the_id(self):
        self.assertEqual(m.event_id_for("ACC-SINV-2026-00007"),
                         m.event_id_for("ACC-SINV-2026-00007-2", "ACC-SINV-2026-00007-1"))

    def test_different_orders_never_share_an_id(self):
        self.assertNotEqual(m.event_id_for("ACC-SINV-2026-00007"), m.event_id_for("ACC-SINV-2026-00008"))


class PhoneTest(unittest.TestCase):
    def test_accepted_formats(self):
        for raw in (
            "01001234567",
            "1001234567",
            "201001234567",
            "+201001234567",
            "00201001234567",
            "+20 100 123 4567",
            "010-0123-4567",
        ):
            with self.subTest(raw=raw):
                self.assertEqual(m.normalize_eg_mobile(raw), "201001234567")

    def test_every_mobile_network(self):
        for prefix in ("010", "011", "012", "015"):
            self.assertEqual(m.normalize_eg_mobile(prefix + "12345678"), "20" + prefix[1:] + "12345678")

    def test_rejected(self):
        for raw in (None, "", "0221234567", "01312345678", "0100123456", "010012345678", "abc", "+44 7700 900123"):
            with self.subTest(raw=raw):
                self.assertIsNone(m.normalize_eg_mobile(raw))


class NameTest(unittest.TestCase):
    def test_first_and_last(self):
        self.assertEqual(m.split_name("  Sara   Ahmed Hassan "), ("sara", "hassan"))

    def test_single_word(self):
        self.assertEqual(m.split_name("Sara"), ("sara", None))

    def test_arabic_is_kept(self):
        self.assertEqual(m.split_name("سارة أحمد"), ("سارة", "أحمد"))

    def test_punctuation_is_stripped(self):
        self.assertEqual(m.split_name("Dr. O'Neil"), ("dr", "oneil"))

    def test_parts_with_digits_are_dropped(self):
        self.assertEqual(m.split_name("01001234567"), (None, None))
        self.assertEqual(m.split_name("Sara 2nd order"), ("sara", "order"))

    def test_empty(self):
        self.assertEqual(m.split_name(None), (None, None))
        self.assertEqual(m.split_name("  "), (None, None))


class UserDataTest(unittest.TestCase):
    def test_keys_are_hashed(self):
        data = m.build_user_data("201001234567", "CUST-00042", "Sara Ahmed")
        self.assertEqual(data["ph"], [sha("201001234567")])
        self.assertEqual(data["country"], [sha("eg")])
        self.assertEqual(data["external_id"], [sha("cust-00042")])
        self.assertEqual(data["fn"], [sha("sara")])
        self.assertEqual(data["ln"], [sha("ahmed")])

    def test_no_plain_text_and_no_email(self):
        data = m.build_user_data("201001234567", "CUST-00042", "Sara Ahmed")
        self.assertNotIn("em", data)
        blob = json.dumps(data)
        for plain in ("201001234567", "CUST-00042", "Sara", "sara"):
            self.assertNotIn(plain, blob)
        for values in data.values():
            for value in values:
                self.assertRegex(value, r"^[0-9a-f]{64}$")

    def test_missing_name_parts_are_left_out(self):
        data = m.build_user_data("201001234567", "CUST-1", "Sara")
        self.assertIn("fn", data)
        self.assertNotIn("ln", data)
        self.assertNotIn("fn", m.build_user_data("201001234567", "CUST-1", ""))


class EventTest(unittest.TestCase):
    def test_purchase_payload(self):
        event = m.build_event(event_id="ACC-SINV-2026-1", event_time=1760000000, value=480, user_data={"ph": ["x"]})
        self.assertEqual(event["event_name"], "Purchase")
        self.assertEqual(event["action_source"], "chat")
        self.assertEqual(event["event_id"], "ACC-SINV-2026-1")
        self.assertEqual(event["event_time"], 1760000000)
        self.assertEqual(event["custom_data"], {"currency": "EGP", "value": 480.0, "order_id": "ACC-SINV-2026-1"})
        self.assertNotIn("event_source_url", event)

    def test_value_is_rounded(self):
        event = m.build_event(event_id="X", event_time=1, value=159.996, user_data={})
        self.assertEqual(event["custom_data"]["value"], 160.0)

    def test_aware_time(self):
        aware = D(2026, 10, 19, 9, 0, tzinfo=datetime.timezone.utc)
        self.assertEqual(m.to_unix(aware, "Africa/Cairo"), int(aware.timestamp()))

    def test_naive_time_is_site_time(self):
        try:
            from zoneinfo import ZoneInfo

            ZoneInfo("Africa/Cairo")
        except Exception:  # pragma: no cover - no tz database on this machine
            self.skipTest("no tz database")
        naive = D(2026, 1, 15, 14, 0)  # Cairo is UTC+2 in January
        expected = int(D(2026, 1, 15, 12, 0, tzinfo=datetime.timezone.utc).timestamp())
        self.assertEqual(m.to_unix(naive, "Africa/Cairo"), expected)


class InitialStatusTest(unittest.TestCase):
    def test_every_order_waits(self):
        self.assertEqual(m.initial_status(480, "201001234567", m.APPROVAL_EVERY_ORDER, 2500), (m.STATUS_PENDING, ""))

    def test_held_only_approves_small_orders(self):
        self.assertEqual(m.initial_status(480, "201001234567", m.APPROVAL_HELD_ONLY, 2500), (m.STATUS_APPROVED, ""))

    def test_large_orders_are_held_in_both_modes(self):
        for mode in (m.APPROVAL_EVERY_ORDER, m.APPROVAL_HELD_ONLY):
            status, reason = m.initial_status(2600, "201001234567", mode, 2500)
            self.assertEqual(status, m.STATUS_HELD)
            self.assertEqual(reason, "Above 2,500 EGP")

    def test_exactly_the_limit_is_not_held(self):
        self.assertEqual(m.initial_status(2500, "201001234567", m.APPROVAL_HELD_ONLY, 2500)[0], m.STATUS_APPROVED)

    def test_no_phone_is_skipped(self):
        self.assertEqual(m.initial_status(480, None, m.APPROVAL_HELD_ONLY, 2500)[0], m.STATUS_SKIPPED)

    def test_zero_value_is_skipped(self):
        self.assertEqual(m.initial_status(0, "201001234567", m.APPROVAL_HELD_ONLY, 2500)[0], m.STATUS_SKIPPED)

    def test_zero_hold_disables_holding(self):
        self.assertEqual(m.initial_status(99999, "201001234567", m.APPROVAL_HELD_ONLY, 0)[0], m.STATUS_APPROVED)


class ExpiryTest(unittest.TestCase):
    def test_window(self):
        now = D(2026, 10, 20, 12, 0)
        self.assertFalse(m.is_expired(D(2026, 10, 14, 12, 0), now))
        self.assertFalse(m.is_expired(now - datetime.timedelta(days=6, hours=23), now))
        self.assertTrue(m.is_expired(now - datetime.timedelta(days=6, hours=23, minutes=45), now))
        self.assertTrue(m.is_expired(D(2026, 10, 12, 12, 0), now))


class SettingsHelpersTest(unittest.TestCase):
    def test_groups_from_lines_or_commas(self):
        self.assertEqual(m.parse_groups("B2B\nEmployee, Sample\n\nB2B"), ("B2B", "Employee", "Sample"))

    def test_blank_groups_fall_back_to_defaults(self):
        self.assertEqual(m.parse_groups(""), m.DEFAULT_EXCLUDED_GROUPS)
        self.assertIn("B2B", m.DEFAULT_EXCLUDED_GROUPS)

    def test_chunks(self):
        self.assertEqual(list(m.chunks([1, 2, 3, 4, 5], 2)), [[1, 2], [3, 4], [5]])

    def test_get_reads_dicts_and_falls_back(self):
        self.assertEqual(m._get({"a": 1}, "a"), 1)
        self.assertEqual(m._get({"a": ""}, "a", 7), 7)
        self.assertEqual(m._get({}, "a", 7), 7)


def _settings(test_mode=0):
    return {
        "api_version": "v23.0",
        "dataset_id": "1296894931563065",
        "test_mode": test_mode,
        "test_event_code": "TEST123",
    }


def _response(status, body):
    return SimpleNamespace(status_code=status, json=lambda: body)


class PostEventsTest(unittest.TestCase):
    TOKEN = "EAAtoken-secret"

    def test_success(self):
        with mock.patch("requests.post", return_value=_response(200, {"events_received": 2, "fbtrace_id": "abc"})) as post:
            ok, body = m._post_events(_settings(), self.TOKEN, [{"a": 1}, {"b": 2}])
        self.assertTrue(ok)
        self.assertEqual(body["fbtrace_id"], "abc")
        url = post.call_args[0][0]
        form = post.call_args[1]["data"]
        self.assertEqual(url, "https://graph.facebook.com/v23.0/1296894931563065/events")
        self.assertNotIn(self.TOKEN, url)
        self.assertEqual(json.loads(form["data"]), [{"a": 1}, {"b": 2}])
        self.assertNotIn("test_event_code", form)

    def test_test_mode_adds_the_code(self):
        with mock.patch("requests.post", return_value=_response(200, {"events_received": 1})) as post:
            m._post_events(_settings(test_mode=1), self.TOKEN, [{"a": 1}])
        self.assertEqual(post.call_args[1]["data"]["test_event_code"], "TEST123")

    def test_partial_receipt_is_a_failure(self):
        with mock.patch("requests.post", return_value=_response(200, {"events_received": 1})):
            ok, _body = m._post_events(_settings(), self.TOKEN, [{"a": 1}, {"b": 2}])
        self.assertFalse(ok)

    def test_error_message_never_holds_the_token(self):
        body = {"error": {"message": "Invalid parameter", "error_user_msg": "event_time too old", "code": 100}}
        with mock.patch("requests.post", return_value=_response(400, body)):
            ok, text = m._post_events(_settings(), self.TOKEN, [{"a": 1}])
        self.assertFalse(ok)
        self.assertEqual(text, "Invalid parameter | event_time too old | code 100")
        self.assertNotIn(self.TOKEN, text)

    def test_network_error_names_the_type_only(self):
        with mock.patch("requests.post", side_effect=ConnectionError("https://x?access_token=" + self.TOKEN)):
            ok, text = m._post_events(_settings(), self.TOKEN, [{"a": 1}])
        self.assertFalse(ok)
        self.assertEqual(text, "Network error (ConnectionError)")

    def test_error_text_fallbacks(self):
        self.assertEqual(m.error_text({}, 502), "HTTP 502")
        self.assertEqual(m.error_text(None), "Unknown error")


class WiringTest(unittest.TestCase):
    """The doctype JSON, hooks, patch list and fixture must agree with the service."""

    def _doctype(self, folder):
        with open(os.path.join(APP_DIR, "doctype", folder, folder + ".json"), encoding="utf-8") as fh:
            return json.load(fh)

    def test_event_fields_used_by_the_service_exist(self):
        fields = {f["fieldname"] for f in self._doctype("meta_capi_event")["fields"]}
        for name in (
            "invoice", "event_id", "customer", "order_value", "event_time", "status", "status_reason",
            "decided_by", "decided_at", "test_sent_at", "sent_at", "attempts", "fbtrace_id", "last_error",
        ):
            self.assertIn(name, fields)

    def test_event_statuses_match(self):
        field = next(f for f in self._doctype("meta_capi_event")["fields"] if f["fieldname"] == "status")
        options = set(field["options"].split("\n"))
        self.assertEqual(
            options,
            {m.STATUS_PENDING, m.STATUS_HELD, m.STATUS_APPROVED, m.STATUS_REJECTED,
             m.STATUS_SENT, m.STATUS_FAILED, m.STATUS_EXPIRED, m.STATUS_SKIPPED},
        )

    def test_event_invoice_is_unique(self):
        field = next(f for f in self._doctype("meta_capi_event")["fields"] if f["fieldname"] == "invoice")
        self.assertEqual(field.get("unique"), 1)

    def test_settings_fields_and_token_protection(self):
        doc = self._doctype("meta_capi_settings")
        fields = {f["fieldname"]: f for f in doc["fields"]}
        for name in ("enabled", "test_mode", "test_event_code", "dataset_id", "api_version", "access_token",
                     "start_date", "approval_mode", "hold_above", "excluded_customer_groups"):
            self.assertIn(name, fields)
        self.assertEqual(fields["access_token"]["fieldtype"], "Password")
        self.assertEqual(fields["access_token"]["permlevel"], 1)
        self.assertEqual(fields["enabled"]["default"], "0")
        self.assertEqual(fields["test_mode"]["default"], "1")
        self.assertEqual(set(fields["approval_mode"]["options"].split("\n")),
                         {m.APPROVAL_EVERY_ORDER, m.APPROVAL_HELD_ONLY})
        level_one = {p["role"] for p in doc["permissions"] if p.get("permlevel") == 1}
        self.assertEqual(level_one, {"System Manager"})

    def test_scheduler_and_role_hooks(self):
        from jarz_pos import hooks

        hourly = hooks.scheduler_events["hourly"]
        self.assertIn("jarz_pos.services.meta_capi.queue_delivered_orders", hourly)
        self.assertIn("jarz_pos.services.meta_capi.send_approved_events", hourly)
        self.assertLess(hourly.index("jarz_pos.services.meta_capi.queue_delivered_orders"),
                        hourly.index("jarz_pos.services.meta_capi.send_approved_events"))
        self.assertIn("jarz_pos.setup.meta_capi_role.ensure_meta_capi_role", hooks.before_migrate)

    def test_seed_patch_is_registered(self):
        with open(os.path.join(APP_DIR, "patches.txt"), encoding="utf-8") as fh:
            self.assertIn("jarz_pos.Patches.v1_9.seed_meta_capi_settings", fh.read().split())

    def test_customer_exclusion_field_is_a_fixture(self):
        with open(os.path.join(APP_DIR, "fixtures", "custom_field.json"), encoding="utf-8") as fh:
            rows = json.load(fh)
        row = next(r for r in rows if r.get("name") == "Customer-custom_exclude_from_meta")
        self.assertEqual(row["fieldname"], m.CUSTOMER_EXCLUDE_FIELD)
        self.assertEqual(row["fieldtype"], "Check")
        self.assertEqual(row["dt"], "Customer")


if __name__ == "__main__":
    unittest.main()
