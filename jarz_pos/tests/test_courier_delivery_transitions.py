"""Tests for the per-invoice courier transitions.

The invariants locked down here are the ones whose absence is *silent* — the
class of bug a courier app with an offline queue turns into lost work rather
than an error message:

* **Missing schema throws, never degrades.** ``update_submitted_sales_invoice_fields``
  filters out fields the site does not have and returns ``False`` with no error.
  An offline queue treating HTTP 200 as success would tick the stop off and
  discard it. So a missing field — or one without ``allow_on_submit`` — raises.
* **Dual idempotency.** The deterministic token (the delivery timestamp already
  on the invoice) survives a device reinstall; the ``request_id`` guard catches
  an offline retry of the same tap. Either alone leaves a hole.
* **A failure never moves the card.** The board state must be byte-identical
  before and after ``mark_invoice_failed``.
* **The access gate runs in the contracted order**, and a ``PermissionError``
  propagates as a real 403 rather than being flattened into a success envelope.

Pure ``unittest`` with mocks — no site, no fixtures.
"""

import datetime
import unittest
from unittest.mock import MagicMock, call, patch

#: Frozen clock for the harness. See _Harness for why this must be patched
#: rather than allowed to reach the real ``frappe.utils.now_datetime``.
FROZEN_NOW = datetime.datetime(2026, 8, 5, 12, 0, 0)

import frappe

from jarz_pos.services import courier_delivery as cd

STATE_FIELD = "custom_sales_invoice_state"

ALL_FIELDS = (
    STATE_FIELD,
    "custom_arrived_at",
    "custom_delivered_at",
    "custom_delivery_latitude",
    "custom_delivery_longitude",
    "custom_delivery_accuracy_m",
    "custom_delivery_attempt_no",
    "custom_delivery_failure_reason",
    "custom_delivery_sequence",
)


class _FakeMeta:
    """Minimal ``frappe.get_meta`` stand-in."""

    def __init__(self, fields=ALL_FIELDS, allow_on_submit=True):
        self._fields = set(fields)
        self._allow_on_submit = allow_on_submit

    def get_field(self, fieldname):
        if fieldname not in self._fields:
            return None
        return {
            "fieldname": fieldname,
            "allow_on_submit": 1 if self._allow_on_submit else 0,
        }


def _invoice(**overrides):
    data = {
        "name": "ACC-SINV-2026-00001",
        "is_return": 0,
        STATE_FIELD: "Out for Delivery",
        "custom_return_status": None,
        "custom_kanban_profile": "Branch A",
        "custom_courier_party_type": "Employee",
        "custom_courier_party": "HR-EMP-0001",
        "custom_arrived_at": None,
        "custom_delivered_at": None,
        "custom_delivery_attempt_no": 0,
        "custom_delivery_failure_reason": None,
    }
    data.update(overrides)
    doc = MagicMock()
    doc.name = data["name"]
    doc.docstatus = 1
    doc.get.side_effect = lambda key, default=None: data.get(key, default)
    doc._data = data
    return doc


REASON = {
    "name": "CUSTOMER_UNREACHABLE",
    "label_en": "Customer unreachable",
    "label_ar": "العميل لا يرد",
    "next_action": "Reschedule",
}


class _Harness:
    """Patch set every happy-path test needs. Used as a context manager."""

    def __init__(self, inv, *, meta=None, update_ok=True, reason=REASON):
        self.inv = inv
        self.meta = meta or _FakeMeta()
        self.update_ok = update_ok
        self.reason = reason
        self.updates = []
        self._patches = []

    def __enter__(self):
        def _capture(inv, values):
            self.updates.append(values)
            for key, value in (values or {}).items():
                inv._data[key] = value
            return self.update_ok

        spec = {
            "enabled": patch.object(cd, "courier_delivery_enabled", return_value=True),
            "replay_lookup": patch.object(cd, "_replay_lookup", return_value=None),
            "replay_store": patch.object(cd, "_replay_store"),
            "update": patch.object(
                cd, "update_submitted_sales_invoice_fields", side_effect=_capture
            ),
            "publisher": patch.object(cd, "publish_invoice_event"),
            "branch_gate": patch.object(cd, "ensure_profile_scoped_invoice_access"),
            "shift_gate": patch.object(cd, "ensure_open_shift_for_invoice"),
            "reason_lookup": patch.object(
                cd, "_resolve_failure_reason", return_value=self.reason
            ),
            "comment": patch.object(cd, "_add_comment"),
            "courier_gate": patch.object(
                cd.courier_identity, "assert_invoice_assigned_to_courier"
            ),
            "get_meta": patch.object(frappe, "get_meta", return_value=self.meta),
            "get_doc": patch.object(frappe, "get_doc", return_value=self.inv),
            "has_permission": patch.object(frappe, "has_permission", return_value=True),
            # Freezing the clock is load-bearing, not cosmetic.
            #
            # `get_doc` above is patched globally, and frappe.utils.now_datetime()
            # reaches get_system_timezone() -> frappe.client_cache.get_doc(
            # "System Settings"). On a cache miss that calls the *patched*
            # frappe.get_doc, gets this harness's MagicMock invoice back, and then
            # Redis tries to pickle it to populate the cache:
            #
            #   _pickle.PicklingError: Can't pickle <class 'MagicMock'>
            #
            # That error surfaced from inside the service's try block, so it was
            # caught and returned as the envelope's message — displacing the real
            # reason and making an unrelated assertion fail. It only reproduces
            # against a real bench, where the Redis client is live.
            "now": patch.object(cd, "now_datetime", return_value=FROZEN_NOW),
        }
        self._patches = list(spec.values())
        self.mocks = {name: patcher.start() for name, patcher in spec.items()}
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Schema assertions
# ─────────────────────────────────────────────────────────────────────────────

class TestSchemaAssertion(unittest.TestCase):
    """A pre-migration deployment must fail loudly, never lose the write."""

    def test_missing_field_throws(self):
        meta = _FakeMeta(fields=(STATE_FIELD,))  # every outcome field absent
        with patch.object(cd, "courier_delivery_enabled", return_value=True), patch.object(
            cd, "_replay_lookup", return_value=None
        ), patch.object(frappe, "get_meta", return_value=meta):
            with self.assertRaises(cd.CourierSchemaError):
                cd.mark_invoice_delivered("ACC-SINV-2026-00001")

    def test_field_without_allow_on_submit_throws(self):
        """The field exists but the write would be rejected by every other path."""
        meta = _FakeMeta(allow_on_submit=False)
        with patch.object(cd, "courier_delivery_enabled", return_value=True), patch.object(
            cd, "_replay_lookup", return_value=None
        ), patch.object(frappe, "get_meta", return_value=meta):
            with self.assertRaises(cd.CourierSchemaError):
                cd.mark_invoice_delivered("ACC-SINV-2026-00001")

    def test_schema_error_propagates_and_is_not_flattened(self):
        """It must not come back as {'success': False} — that reads as retryable."""
        meta = _FakeMeta(fields=(STATE_FIELD,))
        for fn, kwargs in (
            (cd.mark_invoice_arrived, {}),
            (cd.mark_invoice_delivered, {}),
            (cd.mark_invoice_failed, {"failure_reason": "CUSTOMER_UNREACHABLE"}),
        ):
            with self.subTest(fn=fn.__name__):
                with patch.object(
                    cd, "courier_delivery_enabled", return_value=True
                ), patch.object(cd, "_replay_lookup", return_value=None), patch.object(
                    frappe, "get_meta", return_value=meta
                ):
                    with self.assertRaises(cd.CourierSchemaError):
                        fn("ACC-SINV-2026-00001", **kwargs)

    def test_every_action_declares_the_fields_it_writes(self):
        for action, fields in cd._REQUIRED_FIELDS.items():
            with self.subTest(action=action):
                self.assertTrue(fields)
                self.assertTrue(all(f.startswith("custom_") for f in fields))


# ─────────────────────────────────────────────────────────────────────────────
# Feature flag
# ─────────────────────────────────────────────────────────────────────────────

class TestFeatureFlag(unittest.TestCase):
    def test_all_three_refuse_when_the_flag_is_off(self):
        with patch.object(cd, "courier_delivery_enabled", return_value=False):
            for fn, kwargs in (
                (cd.mark_invoice_arrived, {}),
                (cd.mark_invoice_delivered, {}),
                (cd.mark_invoice_failed, {"failure_reason": "CUSTOMER_UNREACHABLE"}),
            ):
                with self.subTest(fn=fn.__name__):
                    result = fn("ACC-SINV-2026-00001", **kwargs)
                    self.assertFalse(result["success"])
                    self.assertIn("not enabled", result["error"])

    def test_flag_is_checked_before_any_document_is_loaded(self):
        with patch.object(cd, "courier_delivery_enabled", return_value=False), patch.object(
            frappe, "get_doc"
        ) as get_doc:
            cd.mark_invoice_delivered("ACC-SINV-2026-00001")
            self.assertFalse(get_doc.called)

    def test_flag_defaults_to_off_when_the_setting_is_unreadable(self):
        with patch.object(frappe.db, "get_single_value", side_effect=Exception("boom")):
            self.assertFalse(cd.courier_delivery_enabled())


# ─────────────────────────────────────────────────────────────────────────────
# Idempotency
# ─────────────────────────────────────────────────────────────────────────────

class TestDeterministicIdempotency(unittest.TestCase):
    def test_token_is_composed_of_invoice_and_action(self):
        self.assertEqual(
            cd.deterministic_token("ACC-SINV-2026-00001", cd.ACTION_DELIVERED),
            "ACC-SINV-2026-00001::delivered",
        )

    def test_second_delivered_tap_is_a_no_op(self):
        """Survives a device reinstall, which is when request_id loses its memory."""
        inv = _invoice(custom_delivered_at="2026-08-05 10:00:00")
        with _Harness(inv) as h:
            result = cd.mark_invoice_delivered("ACC-SINV-2026-00001")

        self.assertTrue(result["success"])
        self.assertTrue(result["idempotent"])
        self.assertEqual(h.updates, [], "an idempotent replay must write nothing")

    def test_second_arrived_tap_is_a_no_op(self):
        inv = _invoice(custom_arrived_at="2026-08-05 09:55:00")
        with _Harness(inv) as h:
            result = cd.mark_invoice_arrived("ACC-SINV-2026-00001")

        self.assertTrue(result["idempotent"])
        self.assertEqual(h.updates, [])

    def test_idempotent_replay_still_reports_the_token(self):
        inv = _invoice(custom_delivered_at="2026-08-05 10:00:00")
        with _Harness(inv):
            result = cd.mark_invoice_delivered("ACC-SINV-2026-00001")
        self.assertEqual(result["idempotency_token"], "ACC-SINV-2026-00001::delivered")

    def test_first_delivered_tap_writes(self):
        inv = _invoice()
        with _Harness(inv) as h:
            result = cd.mark_invoice_delivered("ACC-SINV-2026-00001")

        self.assertTrue(result["success"])
        self.assertFalse(result["idempotent"])
        self.assertEqual(len(h.updates), 1)
        self.assertIn("custom_delivered_at", h.updates[0])


class TestRequestIdReplayGuard(unittest.TestCase):
    def test_a_cached_result_short_circuits_everything(self):
        cached = {"success": True, "action": cd.ACTION_FAILED, "attempt_no": 1}
        with patch.object(cd, "courier_delivery_enabled", return_value=True), patch.object(
            cd, "_replay_lookup", return_value=cached
        ), patch.object(frappe, "get_doc") as get_doc:
            result = cd.mark_invoice_failed(
                "ACC-SINV-2026-00001",
                failure_reason="CUSTOMER_UNREACHABLE",
                request_id="req-1",
            )

        self.assertTrue(result["replayed"])
        self.assertEqual(result["attempt_no"], 1)
        self.assertFalse(get_doc.called, "a replay must not reload the invoice")

    def test_replay_guard_is_the_only_protection_a_failure_has(self):
        """Repeat failures are legitimate, so there is no deterministic token —
        without request_id an offline retry double-counts the attempt."""
        inv = _invoice(custom_delivery_attempt_no=2)
        with _Harness(inv) as h:
            cd.mark_invoice_failed(
                "ACC-SINV-2026-00001", failure_reason="CUSTOMER_UNREACHABLE"
            )
        self.assertEqual(h.updates[0]["custom_delivery_attempt_no"], 3)

    def test_replay_key_is_namespaced_per_action(self):
        keys = {
            cd._replay_key("INV-1", action, "req-1")
            for action in (cd.ACTION_ARRIVED, cd.ACTION_DELIVERED, cd.ACTION_FAILED)
        }
        self.assertEqual(len(keys), 3)

    def test_replay_store_survives_a_cache_outage(self):
        """A dead Redis must not undo a completed delivery."""
        with patch.object(frappe, "cache", side_effect=Exception("redis down")), patch.object(
            frappe, "log_error"
        ):
            cd._replay_store("INV-1", cd.ACTION_DELIVERED, "req-1", {"success": True})

    def test_replay_lookup_returns_none_on_a_cache_outage(self):
        with patch.object(frappe, "cache", side_effect=Exception("redis down")):
            self.assertIsNone(cd._replay_lookup("INV-1", cd.ACTION_DELIVERED, "req-1"))

    def test_no_request_id_means_no_replay_lookup(self):
        self.assertIsNone(cd._replay_lookup("INV-1", cd.ACTION_DELIVERED, ""))


# ─────────────────────────────────────────────────────────────────────────────
# The frozen state string
# ─────────────────────────────────────────────────────────────────────────────

class TestFailureNeverChangesState(unittest.TestCase):
    """COURIER_CONTRACTS §5.4 — no new state is introduced by this project."""

    def test_no_state_alias_appears_in_the_update(self):
        inv = _invoice()
        with _Harness(inv) as h:
            result = cd.mark_invoice_failed(
                "ACC-SINV-2026-00001", failure_reason="CUSTOMER_UNREACHABLE"
            )

        self.assertTrue(result["success"])
        written = set(h.updates[0])
        for alias in cd._STATE_FIELD_ALIASES:
            self.assertNotIn(alias, written, f"a failure must not write {alias}")

    def test_the_state_value_is_unchanged_after_a_failure(self):
        inv = _invoice()
        with _Harness(inv):
            cd.mark_invoice_failed(
                "ACC-SINV-2026-00001", failure_reason="CUSTOMER_UNREACHABLE"
            )
        self.assertEqual(inv._data[STATE_FIELD], "Out for Delivery")

    def test_failure_sets_the_reason_and_increments_the_attempt(self):
        inv = _invoice(custom_delivery_attempt_no=1)
        with _Harness(inv) as h:
            result = cd.mark_invoice_failed(
                "ACC-SINV-2026-00001",
                failure_reason="CUSTOMER_UNREACHABLE",
                notes="rang three times",
            )

        self.assertEqual(h.updates[0]["custom_delivery_failure_reason"], "CUSTOMER_UNREACHABLE")
        self.assertEqual(h.updates[0]["custom_delivery_attempt_no"], 2)
        self.assertEqual(result["attempt_no"], 2)
        self.assertEqual(result["next_action"], "Reschedule")

    def test_unknown_reason_is_refused(self):
        inv = _invoice()
        with _Harness(inv, reason=None):
            result = cd.mark_invoice_failed("ACC-SINV-2026-00001", failure_reason="NOPE")
        self.assertFalse(result["success"])
        self.assertIn("Unknown or inactive", result["error"])

    def test_a_reason_is_mandatory(self):
        with patch.object(cd, "courier_delivery_enabled", return_value=True):
            result = cd.mark_invoice_failed("ACC-SINV-2026-00001", failure_reason="")
        self.assertFalse(result["success"])

    def test_delivered_does_move_the_state(self):
        """The contrast case: only delivery advances the board."""
        inv = _invoice()
        with _Harness(inv) as h:
            cd.mark_invoice_delivered("ACC-SINV-2026-00001")
        self.assertEqual(h.updates[0][STATE_FIELD], "Delivered")

    def test_delivered_state_is_one_of_the_frozen_options(self):
        self.assertEqual(cd.DELIVERED_STATE, "Delivered")

    def test_arrived_does_not_move_the_state(self):
        """Arrival is a sub-step of Out for Delivery, not a column."""
        inv = _invoice()
        with _Harness(inv) as h:
            cd.mark_invoice_arrived("ACC-SINV-2026-00001")
        for alias in cd._STATE_FIELD_ALIASES:
            self.assertNotIn(alias, h.updates[0])


# ─────────────────────────────────────────────────────────────────────────────
# Delivery specifics
# ─────────────────────────────────────────────────────────────────────────────

class TestDelivered(unittest.TestCase):
    def test_delivery_clears_any_outstanding_failure_reason(self):
        inv = _invoice(custom_delivery_failure_reason="WRONG_ADDRESS")
        with _Harness(inv) as h:
            cd.mark_invoice_delivered("ACC-SINV-2026-00001")
        self.assertIsNone(h.updates[0]["custom_delivery_failure_reason"])

    def test_delivery_backfills_a_missing_arrival_time(self):
        """A Delivered tap whose Arrived is stuck in the offline queue must not
        silently drop that stop out of every duration report."""
        inv = _invoice()
        with _Harness(inv) as h:
            cd.mark_invoice_delivered("ACC-SINV-2026-00001")
        self.assertEqual(
            h.updates[0]["custom_arrived_at"], h.updates[0]["custom_delivered_at"]
        )

    def test_an_existing_arrival_time_is_preserved(self):
        inv = _invoice(custom_arrived_at="2026-08-05 09:50:00")
        with _Harness(inv) as h:
            cd.mark_invoice_delivered("ACC-SINV-2026-00001")
        self.assertNotIn("custom_arrived_at", h.updates[0])

    def test_coordinates_are_recorded_when_supplied(self):
        inv = _invoice()
        with _Harness(inv) as h:
            cd.mark_invoice_delivered(
                "ACC-SINV-2026-00001", latitude=30.0444, longitude=31.2357, accuracy_m=8.5
            )
        self.assertAlmostEqual(h.updates[0]["custom_delivery_latitude"], 30.0444)
        self.assertAlmostEqual(h.updates[0]["custom_delivery_longitude"], 31.2357)
        self.assertAlmostEqual(h.updates[0]["custom_delivery_accuracy_m"], 8.5)

    def test_invalid_coordinates_are_dropped_not_stored(self):
        inv = _invoice()
        with _Harness(inv) as h:
            cd.mark_invoice_delivered("ACC-SINV-2026-00001", latitude=0, longitude=0)
        self.assertNotIn("custom_delivery_latitude", h.updates[0])

    def test_it_posts_no_accounting(self):
        """Cash settles through the existing settlement path, never here."""
        inv = _invoice()
        with _Harness(inv) as h:
            result = cd.mark_invoice_delivered(
                "ACC-SINV-2026-00001", collected_amount=500.0
            )
        self.assertFalse(result["posted_accounting"])
        self.assertEqual(result["collected_amount"], 500.0)
        # Nothing money-shaped is written onto the invoice.
        for field in h.updates[0]:
            self.assertFalse(field.startswith("custom_collected"))

    def test_negative_collected_amount_is_refused(self):
        with patch.object(cd, "courier_delivery_enabled", return_value=True):
            result = cd.mark_invoice_delivered(
                "ACC-SINV-2026-00001", collected_amount=-1
            )
        self.assertFalse(result["success"])

    def test_a_mocked_location_is_escalated(self):
        inv = _invoice()
        with _Harness(inv), patch.object(frappe, "log_error") as log_error:
            result = cd.mark_invoice_delivered("ACC-SINV-2026-00001", is_mocked=True)
        self.assertTrue(result["is_mocked"])
        self.assertTrue(log_error.called, "a spoofed GPS at POD must never be silent")

    def test_a_write_failure_is_reported_not_swallowed(self):
        inv = _invoice()
        with _Harness(inv, update_ok=False):
            result = cd.mark_invoice_delivered("ACC-SINV-2026-00001")
        self.assertFalse(result["success"])


class TestGuards(unittest.TestCase):
    def test_a_draft_invoice_is_refused(self):
        inv = _invoice()
        inv.docstatus = 0
        with _Harness(inv):
            result = cd.mark_invoice_delivered("ACC-SINV-2026-00001")
        self.assertFalse(result["success"])

    def test_a_credit_note_is_refused(self):
        inv = _invoice(is_return=1)
        with _Harness(inv):
            result = cd.mark_invoice_delivered("ACC-SINV-2026-00001")
        self.assertFalse(result["success"])

    def test_a_fully_returned_order_is_refused(self):
        inv = _invoice(custom_return_status="Fully Returned")
        with _Harness(inv):
            result = cd.mark_invoice_delivered("ACC-SINV-2026-00001")
        self.assertFalse(result["success"])

    def test_a_blank_invoice_id_is_refused(self):
        with patch.object(cd, "courier_delivery_enabled", return_value=True):
            self.assertFalse(cd.mark_invoice_delivered("")["success"])
            self.assertFalse(cd.mark_invoice_arrived("   ")["success"])


# ─────────────────────────────────────────────────────────────────────────────
# Access gate
# ─────────────────────────────────────────────────────────────────────────────

class TestAccessGate(unittest.TestCase):
    def test_gate_runs_in_the_contracted_order(self):
        """has_permission -> branch scoping -> open shift.

        Order is not cosmetic: the doctype permission is the cheapest and must
        never be bypassed, and asking for a shift last means a user who fails the
        first two never sees a confusing 'start a shift' prompt for an order they
        cannot touch at all.
        """
        inv = _invoice()
        recorder = MagicMock()
        with _Harness(inv):
            with patch.object(
                frappe, "has_permission", side_effect=lambda *a, **k: recorder("perm")
            ), patch.object(
                cd, "ensure_profile_scoped_invoice_access", side_effect=lambda *a, **k: recorder("branch")
            ), patch.object(
                cd, "ensure_open_shift_for_invoice", side_effect=lambda *a, **k: recorder("shift")
            ), patch.object(
                cd.courier_identity,
                "assert_invoice_assigned_to_courier",
                side_effect=lambda *a, **k: recorder("courier"),
            ):
                cd.mark_invoice_delivered("ACC-SINV-2026-00001")

        self.assertEqual(
            recorder.call_args_list,
            [call("perm"), call("branch"), call("shift"), call("courier")],
        )

    def test_permission_error_propagates_as_a_real_403(self):
        """Flattened into an envelope, an offline queue would retry it forever."""
        inv = _invoice()
        with _Harness(inv):
            with patch.object(
                cd,
                "ensure_profile_scoped_invoice_access",
                side_effect=frappe.PermissionError("wrong branch"),
            ):
                with self.assertRaises(frappe.PermissionError):
                    cd.mark_invoice_delivered("ACC-SINV-2026-00001")

    def test_shift_required_propagates_so_the_client_can_route_to_start_shift(self):
        from jarz_pos.utils.access_control import ShiftRequiredError

        inv = _invoice()
        with _Harness(inv):
            with patch.object(
                cd,
                "ensure_open_shift_for_invoice",
                side_effect=ShiftRequiredError("no open shift"),
            ):
                with self.assertRaises(ShiftRequiredError):
                    cd.mark_invoice_delivered("ACC-SINV-2026-00001")

    def test_a_courier_cannot_stamp_another_couriers_stop(self):
        inv = _invoice()
        with _Harness(inv):
            with patch.object(
                cd.courier_identity,
                "assert_invoice_assigned_to_courier",
                side_effect=frappe.PermissionError("wrong courier"),
            ):
                with self.assertRaises(frappe.PermissionError):
                    cd.mark_invoice_delivered("ACC-SINV-2026-00001")

    def test_an_unexpected_error_becomes_an_envelope_not_a_500(self):
        """An unexpected failure returns an envelope carrying the real reason.

        Both halves matter. The envelope stops an exception escaping as a 500,
        and the *reason* is what a courier's offline queue records when a stop
        will not sync — a generic string there is a support ticket nobody can
        action.

        Substituted with a plain function rather than a MagicMock with
        side_effect, so no mock object enters the traceback path.
        """
        def _boom(*_args, **_kwargs):
            raise RuntimeError("boom")

        inv = _invoice()
        with _Harness(inv):
            with patch.object(
                cd, "update_submitted_sales_invoice_fields", new=_boom
            ), patch.object(frappe, "log_error"):
                # Escaping instead of returning is itself the failure.
                result = cd.mark_invoice_delivered("ACC-SINV-2026-00001")

        self.assertIsInstance(result, dict)
        self.assertFalse(result["success"])
        self.assertIn("boom", result["error"])


# ─────────────────────────────────────────────────────────────────────────────
# Realtime
# ─────────────────────────────────────────────────────────────────────────────

class TestRealtime(unittest.TestCase):
    def test_events_go_through_the_branch_scoped_publisher(self):
        """A bare frappe.publish_realtime lands in the site-wide 'all' room."""
        inv = _invoice()
        with _Harness(inv) as h:
            cd.mark_invoice_delivered("ACC-SINV-2026-00001")
        publisher = h.mocks["publisher"]
        self.assertTrue(publisher.called)
        event, payload, invoice = publisher.call_args.args
        self.assertEqual(event, "jarz_pos_courier_stop_delivered")
        self.assertIs(invoice, inv)
        self.assertEqual(payload["invoice_id"], "ACC-SINV-2026-00001")

    def test_each_action_uses_its_own_frozen_event_name(self):
        from jarz_pos.constants import WS_EVENTS

        self.assertEqual(WS_EVENTS.COURIER_STOP_ARRIVED, "jarz_pos_courier_stop_arrived")
        self.assertEqual(WS_EVENTS.COURIER_STOP_DELIVERED, "jarz_pos_courier_stop_delivered")
        self.assertEqual(WS_EVENTS.COURIER_STOP_FAILED, "jarz_pos_courier_stop_failed")

    def test_a_realtime_failure_does_not_undo_the_delivery(self):
        inv = _invoice()
        with _Harness(inv) as h:
            with patch.object(
                cd, "publish_invoice_event", side_effect=Exception("socket down")
            ), patch.object(frappe, "log_error"):
                result = cd.mark_invoice_delivered("ACC-SINV-2026-00001")
        self.assertTrue(result["success"])
        self.assertEqual(len(h.updates), 1)

    def test_no_realtime_is_published_for_an_idempotent_replay(self):
        inv = _invoice(custom_delivered_at="2026-08-05 10:00:00")
        with _Harness(inv) as h:
            cd.mark_invoice_delivered("ACC-SINV-2026-00001")
        self.assertFalse(h.mocks["publisher"].called)


class TestCourierIdentity(unittest.TestCase):
    def test_a_non_courier_user_is_not_policed(self):
        """Dispatchers and managers legitimately stamp outcomes from the board."""
        from jarz_pos.services import courier_identity

        with patch.object(courier_identity, "resolve_delivery_courier", return_value=None):
            self.assertIsNone(courier_identity.user_is_assigned_courier(_invoice()))
            courier_identity.assert_invoice_assigned_to_courier(
                _invoice(), action_label="testing"
            )  # must not raise

    def test_an_employee_outside_the_delivery_group_is_not_a_courier(self):
        """The trap this guards: in any real site most staff — cashiers, line
        managers, accountants — are Employees with user_id set. Treating that as
        'is a courier' would block a dispatcher from stamping a delivery."""
        from jarz_pos.services import courier_identity

        staff = {"party_type": "Employee", "party": "HR-EMP-0500", "is_delivery_courier": False}
        with patch.object(courier_identity, "resolve_courier_party", return_value=staff):
            self.assertIsNone(courier_identity.resolve_delivery_courier())
            self.assertIsNone(courier_identity.user_is_assigned_courier(_invoice()))

    def test_the_assigned_courier_passes(self):
        from jarz_pos.services import courier_identity

        courier = {
            "party_type": "Employee",
            "party": "HR-EMP-0001",
            "is_delivery_courier": True,
        }
        with patch.object(courier_identity, "resolve_courier_party", return_value=courier):
            self.assertTrue(courier_identity.user_is_assigned_courier(_invoice()))

    def test_another_courier_is_refused(self):
        from jarz_pos.services import courier_identity

        courier = {
            "party_type": "Employee",
            "party": "HR-EMP-0099",
            "is_delivery_courier": True,
        }
        with patch.object(courier_identity, "resolve_courier_party", return_value=courier):
            self.assertFalse(courier_identity.user_is_assigned_courier(_invoice()))
            with self.assertRaises(frappe.PermissionError):
                courier_identity.assert_invoice_assigned_to_courier(
                    _invoice(), action_label="testing"
                )

    def test_a_supplier_courier_never_resolves_to_a_login(self):
        """Third-party delivery companies are billed, not handed an app."""
        from jarz_pos.services import courier_identity

        with patch.object(frappe, "get_all", return_value=[]):
            self.assertIsNone(courier_identity.resolve_courier_party("nobody@example.com"))

    def test_guest_is_never_a_courier(self):
        from jarz_pos.services import courier_identity

        self.assertIsNone(courier_identity.resolve_courier_party("Guest"))

    def test_group_membership_lookup_fails_open(self):
        """A broken Employee Group must not lock every courier out of delivering."""
        from jarz_pos.services import courier_identity

        try:
            delattr(frappe.local, "jarz_delivery_group_employees")
        except Exception:
            pass
        with patch.object(frappe.db, "get_value", side_effect=Exception("db down")), patch.object(
            frappe, "log_error"
        ):
            self.assertEqual(courier_identity.delivery_group_employees(), set())
            self.assertFalse(courier_identity.is_delivery_courier("HR-EMP-0001"))
        try:
            delattr(frappe.local, "jarz_delivery_group_employees")
        except Exception:
            pass


if __name__ == "__main__":
    unittest.main()
