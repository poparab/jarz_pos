"""Tests for the courier launch backend (F1 pin, F4 delivery code, F5 ETA).

What is locked down, and why each one is here:

1. **The code is a function of the token alone.** Four digits, stable for one
   token, different across tokens, never derived from the invoice number. The
   staff link and the customer page must show the SAME digits without storing
   anything, and a customer who cannot read the page must not be able to guess
   the code from a public identifier.
2. **Wrong codes cost attempts, and five lock the order.** The counter is bumped
   before the comparison, a correct code clears it, an Arabic-Indic or padded
   entry matches the same code, and a blank entry is "required", not "wrong".
3. **Everything is additive and off by default.** The six settings read their own
   default when nobody has saved them; ``mark_invoice_delivered`` only checks a
   code when the caller asks (``enforce_code=True``) AND the switch is on, so the
   Kanban and every older caller can always deliver.
4. **The ETA is garnish and never raises.** Live from OSRM while the order is out,
   the slot window as the fallback, nothing for a terminal order, and a bounded,
   cached OSRM call.
5. **The pin gate is a POS-only rule.** Off by default; a centroid is not a pin;
   Woo-origin, pickup and deliver-at-branch orders are exempt.

Pure ``unittest`` with mocks -- no site, no fixtures.
"""

import datetime as dt
import inspect
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe

from jarz_pos.services import courier_delivery, delivery_pin_requirement, tracking
from jarz_pos.tests.test_tracking_public_status import (
    EXPECTED_KEYS,
    TOKEN,
    _Harness,
    _row,
)

INVOICE = "ACC-SINV-2026-90001"
NOW = dt.datetime(2026, 10, 14, 12, 0, 0)


def _identity(text, *args, **kwargs):
    return text


def _throwing_frappe():
    """A ``frappe`` stand-in whose ``throw`` raises the class it is given."""
    fake = MagicMock()

    def _throw(msg, exc=None, *args, **kwargs):
        raise (exc or frappe.ValidationError)(msg)

    fake.throw.side_effect = _throw
    return fake


class _FakeCache:
    """The slice of ``RedisWrapper`` the attempt counter uses."""

    def __init__(self):
        self.store = {}
        self.expiry = {}

    def make_key(self, key):
        return f"site1|{key}"

    def get(self, key):
        return self.store.get(key)

    def incrby(self, key, amount):
        self.store[key] = int(self.store.get(key) or 0) + amount
        return self.store[key]

    def expire(self, key, seconds):
        self.expiry[key] = seconds

    def delete(self, key):
        self.store.pop(key, None)


# ─────────────────────────────────────────────────────────────────────────────
# F4 -- the code itself
# ─────────────────────────────────────────────────────────────────────────────

class DeliveryCodeDerivationTests(unittest.TestCase):
    def test_code_is_four_digits_and_stable_for_one_token(self):
        code = tracking.delivery_code_for_token(TOKEN)
        self.assertRegex(code, r"^[0-9]{4}$")
        self.assertEqual(code, tracking.delivery_code_for_token(TOKEN))
        self.assertEqual(code, tracking.delivery_code_for_token(f"  {TOKEN}  "))

    def test_code_is_the_documented_derivation(self):
        import hashlib

        digest = hashlib.sha256((TOKEN + ":delivery-code").encode()).hexdigest()
        self.assertEqual(tracking.delivery_code_for_token(TOKEN), "%04d" % (int(digest, 16) % 10000))

    def test_code_is_zero_padded(self):
        # Find a token whose code starts with 0 so the padding is really exercised.
        for i in range(2000):
            code = tracking.delivery_code_for_token(f"tok-{i}")
            if code.startswith("0"):
                self.assertEqual(len(code), 4)
                return
        self.fail("no zero-led code found in 2000 tokens")

    def test_codes_differ_across_tokens(self):
        codes = {tracking.delivery_code_for_token(f"token-{i}") for i in range(200)}
        self.assertGreater(len(codes), 150)

    def test_blank_token_has_no_code(self):
        for blank in (None, "", "   "):
            self.assertIsNone(tracking.delivery_code_for_token(blank))

    def test_invoice_code_follows_the_token_and_does_not_mint_unless_asked(self):
        with patch.object(tracking, "get_tracking_token", return_value=TOKEN) as getter:
            self.assertEqual(
                tracking.delivery_code_for_invoice(INVOICE, ensure=False),
                tracking.delivery_code_for_token(TOKEN),
            )
            getter.assert_called_once_with(INVOICE, ensure=False)
        with patch.object(tracking, "get_tracking_token", return_value=None):
            self.assertIsNone(tracking.delivery_code_for_invoice(INVOICE, ensure=False))


class NormalizeDeliveryCodeTests(unittest.TestCase):
    def test_arabic_indic_and_persian_digits_become_ascii(self):
        self.assertEqual(courier_delivery.normalize_delivery_code("١٢٣٤"), "1234")
        self.assertEqual(courier_delivery.normalize_delivery_code("۱۲۳۴"), "1234")

    def test_whitespace_and_direction_marks_are_stripped(self):
        self.assertEqual(courier_delivery.normalize_delivery_code(" 12 34\n"), "1234")
        self.assertEqual(courier_delivery.normalize_delivery_code("‏1234‎"), "1234")

    def test_blank_and_none_are_empty(self):
        self.assertEqual(courier_delivery.normalize_delivery_code(None), "")
        self.assertEqual(courier_delivery.normalize_delivery_code("   "), "")

    def test_garbage_is_left_in_so_it_fails_to_match(self):
        self.assertEqual(courier_delivery.normalize_delivery_code("12ab"), "12ab")


class DeliveryCodeAttemptTests(unittest.TestCase):
    """``_assert_delivery_code`` against a fake cache, so the counter is observable."""

    def setUp(self):
        self.cache = _FakeCache()
        self.fake_frappe = _throwing_frappe()
        self.fake_frappe.cache.return_value = self.cache
        self.code = tracking.delivery_code_for_token(TOKEN)
        # A wrong code that is guaranteed different from the right one.
        self.wrong = "%04d" % ((int(self.code) + 1) % 10000)
        self._patches = [
            patch.object(courier_delivery, "frappe", self.fake_frappe),
            patch.object(courier_delivery, "_", _identity),
            patch.object(tracking, "delivery_code_for_invoice", return_value=self.code),
        ]
        for p in self._patches:
            p.start()
        self.key = self.cache.make_key(f"{courier_delivery._CODE_ATTEMPTS_KEY_PREFIX}{INVOICE}")

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()

    def _check(self, value):
        courier_delivery._assert_delivery_code(INVOICE, value)

    def test_the_right_code_passes_and_leaves_no_counter(self):
        self._check(self.code)
        self.assertNotIn(self.key, self.cache.store)

    def test_arabic_indic_entry_of_the_right_code_passes(self):
        arabic = "".join(chr(0x0660 + int(d)) for d in self.code)
        self._check(arabic)

    def test_blank_entry_is_required_not_wrong_and_costs_nothing(self):
        for blank in (None, "", "  "):
            with self.assertRaises(courier_delivery.DeliveryCodeRequired):
                self._check(blank)
        self.assertNotIn(self.key, self.cache.store)

    def test_wrong_code_raises_wrong_and_counts_the_attempt(self):
        with self.assertRaises(courier_delivery.DeliveryCodeWrong) as ctx:
            self._check(self.wrong)
        self.assertIn("4", str(ctx.exception))  # 4 attempts left
        self.assertEqual(self.cache.store[self.key], 1)
        self.assertEqual(self.cache.expiry[self.key], courier_delivery.DELIVERY_CODE_ATTEMPTS_TTL_SEC)

    def test_the_fifth_wrong_code_locks_and_the_sixth_attempt_is_refused_untried(self):
        for _ in range(courier_delivery.DELIVERY_CODE_MAX_ATTEMPTS - 1):
            with self.assertRaises(courier_delivery.DeliveryCodeWrong):
                self._check(self.wrong)
        with self.assertRaises(courier_delivery.DeliveryCodeLocked):
            self._check(self.wrong)
        # Locked: even the RIGHT code is refused now, and the counter stops growing.
        used = self.cache.store[self.key]
        with self.assertRaises(courier_delivery.DeliveryCodeLocked):
            self._check(self.code)
        self.assertEqual(self.cache.store[self.key], used)

    def test_a_correct_code_clears_the_counter(self):
        for _ in range(3):
            with self.assertRaises(courier_delivery.DeliveryCodeWrong):
                self._check(self.wrong)
        self._check(self.code)
        self.assertNotIn(self.key, self.cache.store)
        # And the budget is fresh again.
        with self.assertRaises(courier_delivery.DeliveryCodeWrong):
            self._check(self.wrong)
        self.assertEqual(self.cache.store[self.key], 1)

    def test_counter_key_is_site_namespaced_through_make_key(self):
        with self.assertRaises(courier_delivery.DeliveryCodeWrong):
            self._check(self.wrong)
        self.assertEqual([k for k in self.cache.store], [self.key])
        self.assertTrue(self.key.startswith("site1|jarz:delivery_code_attempts:"))

    def test_no_token_means_not_enforced_rather_than_a_stranded_courier(self):
        with patch.object(tracking, "delivery_code_for_invoice", return_value=None):
            self._check("0000")  # does not raise
        self.assertEqual(self.cache.store, {})

    def test_cache_down_degrades_to_no_counter_but_still_compares(self):
        self.fake_frappe.cache.side_effect = RuntimeError("redis down")
        self._check(self.code)
        with self.assertRaises(courier_delivery.DeliveryCodeWrong):
            self._check(self.wrong)

    def test_the_three_refusals_are_distinct_validation_errors(self):
        classes = (
            courier_delivery.DeliveryCodeRequired,
            courier_delivery.DeliveryCodeWrong,
            courier_delivery.DeliveryCodeLocked,
        )
        self.assertEqual(len(set(classes)), 3)
        for cls in classes:
            self.assertTrue(issubclass(cls, frappe.ValidationError))
        self.assertEqual(
            [c.__name__ for c in classes],
            ["DeliveryCodeRequired", "DeliveryCodeWrong", "DeliveryCodeLocked"],
        )


class MarkDeliveredCodeGateTests(unittest.TestCase):
    """The additive kwargs on ``mark_invoice_delivered``."""

    def test_signature_is_additive_and_defaults_to_not_enforcing(self):
        params = inspect.signature(courier_delivery.mark_invoice_delivered).parameters
        self.assertIsNone(params["delivery_code"].default)
        self.assertIs(params["enforce_code"].default, False)
        self.assertEqual(params["enforce_code"].kind, inspect.Parameter.KEYWORD_ONLY)

    def _run(self, *, required, enforce, delivered_at=None, code="1234"):
        inv = MagicMock()
        inv.docstatus = 1
        inv.get.side_effect = lambda field, default=None: {
            "custom_delivered_at": delivered_at,
            "custom_arrived_at": NOW,
            "is_return": 0,
        }.get(field, default)
        check = MagicMock()
        update = MagicMock(return_value=True)
        with patch.object(courier_delivery, "courier_delivery_enabled", return_value=True), patch.object(
            courier_delivery, "_replay_lookup", return_value=None
        ), patch.object(courier_delivery, "_assert_invoice_fields"), patch.object(
            courier_delivery, "_load_and_gate", return_value=inv
        ), patch.object(courier_delivery, "_assert_actionable", return_value=None), patch.object(
            courier_delivery, "delivery_code_required", return_value=required
        ), patch.object(courier_delivery, "_assert_delivery_code", check), patch.object(
            courier_delivery, "_board_state_fields", return_value=[]
        ), patch.object(courier_delivery, "_log"), patch.object(
            courier_delivery.delivery_leg, "leg_fields_present", return_value=False
        ), patch.object(courier_delivery, "update_submitted_sales_invoice_fields", update), patch.object(
            courier_delivery, "_record_delivery_note_comment"
        ), patch.object(courier_delivery, "_envelope", return_value={"success": True}), patch.object(
            courier_delivery, "_replay_store"
        ), patch.object(courier_delivery, "_publish"), patch.object(
            courier_delivery, "_", _identity
        ):
            result = courier_delivery.mark_invoice_delivered(
                INVOICE, delivery_code=code, enforce_code=enforce
            )
        return result, check, update

    def test_enforced_and_required_checks_the_code_before_writing(self):
        result, check, update = self._run(required=True, enforce=True, code="4321")
        check.assert_called_once_with(INVOICE, "4321")
        self.assertTrue(result["success"])
        update.assert_called_once()

    def test_a_refused_code_propagates_and_nothing_is_written(self):
        check = MagicMock(side_effect=courier_delivery.DeliveryCodeWrong("nope"))
        inv = MagicMock(docstatus=1)
        inv.get.return_value = None
        with patch.object(courier_delivery, "_assert_delivery_code", check):
            with patch.object(courier_delivery, "courier_delivery_enabled", return_value=True), patch.object(
                courier_delivery, "_replay_lookup", return_value=None
            ), patch.object(courier_delivery, "_assert_invoice_fields"), patch.object(
                courier_delivery, "_load_and_gate", return_value=inv
            ), patch.object(courier_delivery, "_assert_actionable", return_value=None), patch.object(
                courier_delivery, "delivery_code_required", return_value=True
            ), patch.object(courier_delivery, "update_submitted_sales_invoice_fields") as update:
                with self.assertRaises(courier_delivery.DeliveryCodeWrong):
                    courier_delivery.mark_invoice_delivered(INVOICE, delivery_code="0", enforce_code=True)
        update.assert_not_called()

    def test_not_enforced_by_the_caller_never_checks(self):
        _, check, _ = self._run(required=True, enforce=False)
        check.assert_not_called()

    def test_switch_off_never_checks_even_when_the_caller_enforces(self):
        _, check, _ = self._run(required=False, enforce=True)
        check.assert_not_called()

    def test_an_already_delivered_order_is_idempotent_and_costs_no_attempt(self):
        result, check, update = self._run(required=True, enforce=True, delivered_at=NOW)
        check.assert_not_called()
        update.assert_not_called()


class SettingsDefaultTests(unittest.TestCase):
    """Singles ignore JSON defaults, so each reader must pass its own."""

    def test_unset_settings_read_the_documented_defaults(self):
        with patch("jarz_pos.utils.settings_utils.raw_single_value", return_value=None):
            self.assertFalse(tracking.delivery_code_required())
            self.assertFalse(courier_delivery.delivery_code_required())
            self.assertFalse(delivery_pin_requirement.pin_required_enabled())
            self.assertEqual(tracking.eta_dwell_minutes(), 4)

    def test_saved_values_are_honoured_including_zero_dwell(self):
        with patch("jarz_pos.utils.settings_utils.raw_single_value", return_value="1"):
            self.assertTrue(tracking.delivery_code_required())
            self.assertTrue(delivery_pin_requirement.pin_required_enabled())
        with patch("jarz_pos.utils.settings_utils.raw_single_value", return_value="0"):
            self.assertFalse(tracking.delivery_code_required())
            self.assertEqual(tracking.eta_dwell_minutes(), 0)

    def test_negative_dwell_is_clamped_to_zero(self):
        with patch("jarz_pos.utils.settings_utils.raw_single_value", return_value="-3"):
            self.assertEqual(tracking.eta_dwell_minutes(), 0)

    def test_the_settings_doctype_declares_the_six_fields_with_safe_defaults(self):
        import json
        import os

        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "doctype",
            "jarz_pos_settings",
            "jarz_pos_settings.json",
        )
        with open(path, encoding="utf-8") as handle:
            fields = {f["fieldname"]: f for f in json.load(handle)["fields"]}
        expected = {
            "require_address_pin_for_delivery": ("Check", "0"),
            "require_delivery_code": ("Check", "0"),
            "enforce_courier_action_gate": ("Check", "0"),
            "courier_action_radius_m": ("Int", "300"),
            "courier_action_max_fix_age_sec": ("Int", "300"),
            "courier_eta_dwell_minutes": ("Int", "4"),
        }
        for name, (fieldtype, default) in expected.items():
            with self.subTest(field=name):
                self.assertIn(name, fields)
                self.assertEqual(fields[name]["fieldtype"], fieldtype)
                self.assertEqual(str(fields[name].get("default")), default)


# ─────────────────────────────────────────────────────────────────────────────
# F5 -- the ETA
# ─────────────────────────────────────────────────────────────────────────────

COURIER_FIX = {"latitude": 30.05, "longitude": 31.24, "updated_at": None}
DESTINATION = (30.0444, 31.2357)


def _slot_row(**overrides):
    base = {
        "name": INVOICE,
        "custom_kanban_profile": "Branch A",
        "custom_courier_party": "HR-EMP-0001",
        "custom_delivery_date": "2026-10-14",
        "custom_delivery_time_from": dt.timedelta(hours=18),
        "custom_delivery_duration": 3600,
    }
    base.update(overrides)
    return base


class _EtaHarness:
    """Patches ``tracking.frappe`` / the clock / OSRM for ``resolve_eta``."""

    def __init__(self, *, matrix=None, osrm_enabled=True, others=(), dwell=4, osrm_raises=None):
        self.cache = MagicMock()
        self.cache_values = {}
        self.cache.get_value.side_effect = lambda key, *a, **k: self.cache_values.get(key)
        self.cache.set_value.side_effect = lambda key, value, **k: self.cache_values.__setitem__(key, value)
        self.fake_frappe = MagicMock()
        self.fake_frappe.cache.return_value = self.cache
        self.table = MagicMock()
        if osrm_raises:
            self.table.side_effect = osrm_raises
        else:
            self.table.return_value = SimpleNamespace(durations_s=matrix) if matrix is not None else None
        self.osrm_enabled = osrm_enabled
        self.others = list(others)
        self.dwell = dwell

    def __enter__(self):
        from jarz_pos.services import osrm_client

        self._patches = [
            patch.object(tracking, "frappe", self.fake_frappe),
            patch.object(tracking, "now_datetime", return_value=NOW),
            patch.object(tracking, "eta_dwell_minutes", return_value=self.dwell),
            patch.object(tracking, "_other_open_stops", return_value=self.others),
            patch.object(osrm_client, "is_enabled", return_value=self.osrm_enabled),
            patch.object(osrm_client, "table", self.table),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()
        return False


class EtaTests(unittest.TestCase):
    def test_live_eta_is_the_osrm_drive_time_rounded_to_minutes(self):
        # courier -> destination: 10 minutes.
        with _EtaHarness(matrix=[[0, 600], [600, 0]]) as h:
            result = tracking.resolve_eta(_slot_row(), tracking.STATUS_ON_THE_WAY, DESTINATION, COURIER_FIX)
        self.assertEqual(result["eta_basis"], "live")
        self.assertEqual(result["eta_minutes"], 10)
        self.assertEqual(result["eta_at"], (NOW + dt.timedelta(minutes=10)).isoformat())
        self.assertIsNone(result["eta_window"])
        h.table.assert_called_once()
        self.assertEqual(len(h.table.call_args.args[0]), 2)

    def test_a_nearer_open_stop_is_visited_first_and_charged_dwell(self):
        # points: courier, destination, stop0.  courier->dest 15 min; courier->stop0 5 min;
        # stop0->dest 5 min.  5 + 4 dwell + 5 = 14.
        matrix = [[0, 900, 300], [900, 0, 300], [300, 300, 0]]
        with _EtaHarness(matrix=matrix, others=[(30.06, 31.25)], dwell=4):
            result = tracking.resolve_eta(_slot_row(), tracking.STATUS_ON_THE_WAY, DESTINATION, COURIER_FIX)
        self.assertEqual(result["eta_minutes"], 14)

    def test_a_farther_open_stop_is_not_detoured_through(self):
        # stop0 is farther from the courier (20 min) than the destination (10 min).
        matrix = [[0, 600, 1200], [600, 0, 300], [1200, 300, 0]]
        with _EtaHarness(matrix=matrix, others=[(30.2, 31.5)]):
            result = tracking.resolve_eta(_slot_row(), tracking.STATUS_ON_THE_WAY, DESTINATION, COURIER_FIX)
        self.assertEqual(result["eta_minutes"], 10)

    def test_eta_is_at_least_one_minute(self):
        with _EtaHarness(matrix=[[0, 5], [5, 0]]):
            result = tracking.resolve_eta(_slot_row(), tracking.STATUS_ON_THE_WAY, DESTINATION, COURIER_FIX)
        self.assertEqual(result["eta_minutes"], 1)

    def test_live_result_is_cached_and_the_second_call_does_not_hit_osrm(self):
        with _EtaHarness(matrix=[[0, 600], [600, 0]]) as h:
            first = tracking.resolve_eta(_slot_row(), tracking.STATUS_ON_THE_WAY, DESTINATION, COURIER_FIX)
            second = tracking.resolve_eta(_slot_row(), tracking.STATUS_ON_THE_WAY, DESTINATION, COURIER_FIX)
        self.assertEqual(first, second)
        h.table.assert_called_once()
        (key, value), kwargs = h.cache.set_value.call_args.args, h.cache.set_value.call_args.kwargs
        self.assertEqual(key, f"{tracking._ETA_KEY_PREFIX}{INVOICE}")
        self.assertEqual(kwargs["expires_in_sec"], tracking.ETA_CACHE_TTL_SEC)

    def test_osrm_disabled_falls_back_to_the_slot_window(self):
        with _EtaHarness(matrix=[[0, 600], [600, 0]], osrm_enabled=False):
            result = tracking.resolve_eta(_slot_row(), tracking.STATUS_ON_THE_WAY, DESTINATION, COURIER_FIX)
        self.assertEqual(result["eta_basis"], "slot")
        self.assertIsNone(result["eta_minutes"])
        self.assertIsNone(result["eta_at"])

    def test_osrm_returning_nothing_falls_back_to_the_slot_window(self):
        with _EtaHarness(matrix=None):
            result = tracking.resolve_eta(_slot_row(), tracking.STATUS_ON_THE_WAY, DESTINATION, COURIER_FIX)
        self.assertEqual(result["eta_basis"], "slot")

    def test_a_stale_courier_fix_is_not_used(self):
        stale = dict(COURIER_FIX, updated_at=NOW - dt.timedelta(seconds=tracking.ETA_MAX_FIX_AGE_SEC + 60))
        with _EtaHarness(matrix=[[0, 600], [600, 0]]) as h:
            result = tracking.resolve_eta(_slot_row(), tracking.STATUS_ON_THE_WAY, DESTINATION, stale)
        h.table.assert_not_called()
        self.assertEqual(result["eta_basis"], "slot")

    def test_a_fix_with_an_unreadable_timestamp_is_accepted(self):
        odd = dict(COURIER_FIX, updated_at="not a date")
        with _EtaHarness(matrix=[[0, 600], [600, 0]]):
            result = tracking.resolve_eta(_slot_row(), tracking.STATUS_ON_THE_WAY, DESTINATION, odd)
        self.assertEqual(result["eta_basis"], "live")

    def test_slot_window_is_start_to_start_plus_duration(self):
        with _EtaHarness():
            result = tracking.resolve_eta(_slot_row(), tracking.STATUS_PREPARING, DESTINATION)
        self.assertEqual(result["eta_basis"], "slot")
        self.assertEqual(
            result["eta_window"],
            {"from": "2026-10-14T18:00:00", "to": "2026-10-14T19:00:00"},
        )
        self.assertIsNone(result["eta_minutes"])

    def test_a_blank_duration_reads_as_one_hour(self):
        with _EtaHarness():
            result = tracking.resolve_eta(
                _slot_row(custom_delivery_duration=0), tracking.STATUS_READY, DESTINATION
            )
        self.assertEqual(result["eta_window"]["to"], "2026-10-14T19:00:00")

    def test_a_slot_that_already_ended_gives_nothing(self):
        row = _slot_row(custom_delivery_time_from=dt.timedelta(hours=9))  # 09:00-10:00, now 12:00
        with _EtaHarness():
            result = tracking.resolve_eta(row, tracking.STATUS_ON_THE_WAY, DESTINATION)
        self.assertEqual(result, tracking._empty_eta())

    def test_no_slot_and_no_live_data_is_all_none(self):
        with _EtaHarness():
            result = tracking.resolve_eta(
                {"name": INVOICE}, tracking.STATUS_ON_THE_WAY, (None, None)
            )
        self.assertEqual(
            result, {"eta_minutes": None, "eta_at": None, "eta_window": None, "eta_basis": None}
        )

    def test_a_terminal_order_has_no_eta_even_with_a_live_courier(self):
        for status in sorted(tracking.TERMINAL_STATUSES):
            with self.subTest(status=status), _EtaHarness(matrix=[[0, 600], [600, 0]]) as h:
                result = tracking.resolve_eta(_slot_row(), status, DESTINATION, COURIER_FIX)
                h.table.assert_not_called()
                self.assertEqual(result, tracking._empty_eta())

    def test_a_pre_dispatch_order_never_goes_live(self):
        with _EtaHarness(matrix=[[0, 600], [600, 0]]) as h:
            result = tracking.resolve_eta(_slot_row(), tracking.STATUS_PREPARING, DESTINATION, COURIER_FIX)
        h.table.assert_not_called()
        self.assertEqual(result["eta_basis"], "slot")

    def test_it_never_raises_whatever_osrm_does(self):
        with _EtaHarness(osrm_raises=RuntimeError("boom")):
            result = tracking.resolve_eta(_slot_row(), tracking.STATUS_ON_THE_WAY, DESTINATION, COURIER_FIX)
        self.assertEqual(set(result), {"eta_minutes", "eta_at", "eta_window", "eta_basis"})

    def test_an_unreadable_slot_is_ignored_not_raised(self):
        with _EtaHarness():
            result = tracking.resolve_eta(
                _slot_row(custom_delivery_date="garbage"), tracking.STATUS_PREPARING, DESTINATION
            )
        self.assertEqual(result, tracking._empty_eta())


# ─────────────────────────────────────────────────────────────────────────────
# F5 -- what the public page returns
# ─────────────────────────────────────────────────────────────────────────────

NEW_PUBLIC_KEYS = {"eta_minutes", "eta_at", "eta_window", "eta_basis", "delivery_code"}


class PublicStatusLaunchKeysTests(unittest.TestCase):
    def test_the_new_keys_are_on_the_allow_list(self):
        self.assertTrue(NEW_PUBLIC_KEYS <= set(tracking.PUBLIC_STATUS_KEYS))
        self.assertTrue(NEW_PUBLIC_KEYS <= EXPECTED_KEYS)

    def test_the_response_has_exactly_the_allow_list_keys(self):
        with _Harness(settings={"require_delivery_code": 1}):
            result = tracking.resolve_public_status(TOKEN)
        self.assertTrue(result["success"])
        self.assertEqual(set(result), EXPECTED_KEYS)

    def test_the_code_is_withheld_while_the_switch_is_off(self):
        with _Harness(settings={"require_delivery_code": 0}):
            result = tracking.resolve_public_status(TOKEN)
        self.assertIsNone(result["delivery_code"])

    def test_the_code_is_shown_for_an_open_order_when_the_switch_is_on(self):
        with _Harness(settings={"require_delivery_code": 1}):
            result = tracking.resolve_public_status(TOKEN)
        self.assertEqual(result["delivery_code"], tracking.delivery_code_for_token(TOKEN))

    def test_the_code_is_not_shown_once_the_order_is_finished(self):
        delivered = _row(
            custom_sales_invoice_state="Delivered",
            custom_delivered_at=str(dt.datetime.now()),
        )
        with _Harness(invoice=delivered, settings={"require_delivery_code": 1}):
            result = tracking.resolve_public_status(TOKEN)
        self.assertIsNone(result["delivery_code"])
        self.assertIsNone(result["eta_basis"])

    def test_the_invoice_name_never_appears_in_the_new_keys(self):
        with _Harness(settings={"require_delivery_code": 1}):
            result = tracking.resolve_public_status(TOKEN)
        blob = " ".join(str(result[key]) for key in NEW_PUBLIC_KEYS)
        self.assertNotIn(_row()["name"], blob)


class StaffLinkTests(unittest.TestCase):
    """``api.tracking.get_tracking_link`` carries the code and the customer phone."""

    def test_the_phone_helper_prefers_the_invoice_contact_and_never_raises(self):
        from jarz_pos.api import tracking as api_tracking

        fake = MagicMock()
        fake.db.get_value.return_value = {
            "customer": "CUST-1",
            "contact_mobile": "01012345678",
            "contact_phone": None,
        }
        with patch.object(api_tracking, "frappe", fake):
            self.assertEqual(api_tracking._tracking_customer_phone(INVOICE), "01012345678")
        fake.db.get_value.side_effect = RuntimeError("db gone")
        with patch.object(api_tracking, "frappe", fake):
            self.assertIsNone(api_tracking._tracking_customer_phone(INVOICE))

    def test_the_staff_payload_keys_are_declared_in_source(self):
        from jarz_pos.api import tracking as api_tracking

        source = inspect.getsource(api_tracking.get_tracking_link)
        for key in ('"delivery_code"', '"code_required"', '"customer_phone"'):
            self.assertIn(key, source)


# ─────────────────────────────────────────────────────────────────────────────
# F1 -- the required pin
# ─────────────────────────────────────────────────────────────────────────────

PIN = {"custom_latitude": 30.0444, "custom_longitude": 31.2357}


def _address(**fields):
    return {**PIN, **fields}


class _PinHarness:
    def __init__(self, *, enabled=True, address=None):
        self.fake_frappe = _throwing_frappe()
        self.fake_frappe.db.get_value.return_value = address
        self.enabled = enabled

    def __enter__(self):
        self._patches = [
            patch.object(delivery_pin_requirement, "frappe", self.fake_frappe),
            patch.object(delivery_pin_requirement, "_", _identity),
            patch.object(delivery_pin_requirement, "pin_required_enabled", return_value=self.enabled),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()
        return False


class PinGradeTests(unittest.TestCase):
    grade = staticmethod(delivery_pin_requirement.pin_fields_are_pin_grade)

    def test_a_territory_centroid_is_not_a_pin(self):
        self.assertFalse(self.grade(30.04, 31.23, "territory_centroid"))

    def test_pos_link_and_better_sources_are_pins(self):
        for source in ("pos_link", "customer_pin", "courier_web", "courier_verified", "manual_override"):
            with self.subTest(source=source):
                self.assertTrue(self.grade(30.04, 31.23, source))

    def test_source_is_case_and_space_tolerant(self):
        self.assertTrue(self.grade(30.04, 31.23, "  POS_Link "))
        self.assertFalse(self.grade(30.04, 31.23, " Territory_Centroid "))

    def test_a_legacy_pin_with_no_recorded_source_passes(self):
        for source in (None, "", "something_old"):
            with self.subTest(source=source):
                self.assertTrue(self.grade(30.04, 31.23, source))

    def test_unpinned_and_null_island_coordinates_fail_whatever_the_source(self):
        for lat, lng in ((0.0, 0.0), (None, None), ("", ""), (95, 31), (30, 200)):
            with self.subTest(lat=lat, lng=lng):
                self.assertFalse(self.grade(lat, lng, "pos_link"))

    def test_the_address_lookup_reads_the_three_fields(self):
        with _PinHarness(address=_address(custom_geo_source="pos_link")) as h:
            self.assertTrue(delivery_pin_requirement.address_is_pin_grade("ADDR-1"))
            self.assertEqual(
                h.fake_frappe.db.get_value.call_args.args[2],
                ["custom_latitude", "custom_longitude", "custom_geo_source"],
            )

    def test_a_missing_address_or_blank_name_is_not_pinned(self):
        with _PinHarness(address=None):
            self.assertFalse(delivery_pin_requirement.address_is_pin_grade("ADDR-GONE"))
            self.assertFalse(delivery_pin_requirement.address_is_pin_grade(""))
            self.assertFalse(delivery_pin_requirement.address_is_pin_grade(None))


class AssertDeliveryPinTests(unittest.TestCase):
    assert_pin = staticmethod(delivery_pin_requirement.assert_delivery_pin)

    def test_the_error_class_is_a_structured_validation_error(self):
        self.assertTrue(issubclass(delivery_pin_requirement.AddressPinRequired, frappe.ValidationError))
        self.assertEqual(delivery_pin_requirement.AddressPinRequired.__name__, "AddressPinRequired")

    def test_off_by_default_nothing_is_refused_and_nothing_is_read(self):
        with _PinHarness(enabled=False, address=_address(custom_geo_source="territory_centroid")) as h:
            self.assert_pin("ADDR-1")
            h.fake_frappe.db.get_value.assert_not_called()

    def test_on_a_centroid_address_is_refused(self):
        with _PinHarness(address=_address(custom_geo_source="territory_centroid")):
            with self.assertRaises(delivery_pin_requirement.AddressPinRequired) as ctx:
                self.assert_pin("ADDR-1")
        self.assertIn("pin", str(ctx.exception).lower())

    def test_on_an_unpinned_address_is_refused(self):
        with _PinHarness(address=_address(custom_latitude=0, custom_longitude=0)):
            with self.assertRaises(delivery_pin_requirement.AddressPinRequired):
                self.assert_pin("ADDR-1")

    def test_on_no_address_at_all_is_refused(self):
        with _PinHarness(address=None):
            with self.assertRaises(delivery_pin_requirement.AddressPinRequired):
                self.assert_pin(None)

    def test_on_a_pinned_address_passes(self):
        with _PinHarness(address=_address(custom_geo_source="pos_link")):
            self.assert_pin("ADDR-1")

    def test_a_woo_origin_order_is_exempt_even_with_no_pin(self):
        with _PinHarness(address=None) as h:
            self.assert_pin("ADDR-1", woo_order_id="12345")
            h.fake_frappe.db.get_value.assert_not_called()

    def test_pickup_and_deliver_at_branch_are_exempt(self):
        with _PinHarness(address=None):
            self.assert_pin("ADDR-1", pickup=True)
            self.assert_pin("ADDR-1", deliver_at_branch=True)

    def test_a_lookup_failure_lets_the_order_through_and_logs(self):
        with _PinHarness(address=None) as h:
            h.fake_frappe.db.get_value.side_effect = RuntimeError("column missing")
            self.assert_pin("ADDR-1")
            h.fake_frappe.log_error.assert_called_once()


class AmendmentPinPrecheckTests(unittest.TestCase):
    precheck = staticmethod(delivery_pin_requirement.assert_amendment_pin)

    def test_off_is_a_noop(self):
        with _PinHarness(enabled=False, address=None) as h:
            self.precheck({"shipping_address_name": "ADDR-1"})
            h.fake_frappe.db.get_value.assert_not_called()

    def test_an_unpinned_standard_order_is_refused_synchronously(self):
        with _PinHarness(address=_address(custom_geo_source="territory_centroid")):
            with self.assertRaises(delivery_pin_requirement.AddressPinRequired):
                self.precheck({"shipping_address_name": "ADDR-1"})

    def test_the_new_address_argument_wins_over_the_source_address(self):
        with _PinHarness(address=_address(custom_geo_source="pos_link")) as h:
            self.precheck({"shipping_address_name": "OLD"}, shipping_address_name="NEW")
            self.assertEqual(h.fake_frappe.db.get_value.call_args.args[1], "NEW")

    def test_pickup_woo_and_policy_orders_are_exempt(self):
        with _PinHarness(address=None):
            self.precheck({"shipping_address_name": "A"}, pickup="1")
            self.precheck({"shipping_address_name": "A", "custom_is_pickup": 1})
            self.precheck({"shipping_address_name": "A", "woo_order_id": "991"})
            self.precheck({"shipping_address_name": "A", "custom_commercial_policy": "Employee Cash"})

    def test_the_precheck_is_wired_into_the_amendment_endpoint(self):
        from jarz_pos.api import manager

        source = inspect.getsource(manager.submit_invoice_amendment)
        self.assertIn("assert_amendment_pin", source)
        self.assertLess(source.index("assert_amendment_pin"), source.index("frappe.enqueue"))


class PinWiredIntoCreateTests(unittest.TestCase):
    def test_creation_calls_the_gate_before_territory_is_resolved(self):
        from jarz_pos.services import invoice_creation

        source = inspect.getsource(invoice_creation.create_pos_invoice)
        self.assertIn("_delivery_pin.assert_delivery_pin(", source)
        self.assertLess(
            source.index("_delivery_pin.assert_delivery_pin("),
            source.index("effective_order_territory = resolve_order_territory("),
        )
        # The Woo discriminator and the exemptions are passed, not re-derived.
        gate_call = source[source.index("_delivery_pin.assert_delivery_pin(") :][:400]
        for needle in ("pickup=", "woo_order_id=", "deliver_at_branch="):
            self.assertIn(needle, gate_call)

    def test_the_gate_is_not_a_sales_invoice_doc_event(self):
        """A doc_event would also fire for the Woo sync, which must never be refused."""
        import os

        hooks = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hooks.py"
        )
        with open(hooks, encoding="utf-8") as handle:
            self.assertNotIn("delivery_pin_requirement", handle.read())


if __name__ == "__main__":
    unittest.main()
