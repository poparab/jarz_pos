"""B2B customer label stock tests (pure mock / unittest).

Everything asserted here is DB-free by construction: the working-day date maths,
the reorder-status decision, the invoice-to-label attribution and the settings
fallbacks are all pure functions, and the two that touch the database
(``get_on_hand``, ``consume_labels_on_invoice_submit``) are exercised with
``frappe.get_all`` mocked.

That matters for more than speed. The backend CI gate runs *before* ``bench
migrate``, so on the run that first sees this commit the three label DocTypes do
not exist yet. A test that inserted a ``Jarz Customer Label`` would fail on the
very commit that introduces it. Date helpers (``frappe.utils.getdate`` /
``add_days`` / ``date_diff``) are real -- they are pure functions and need no site.

Calendar facts the date tests lean on (2026):
    Mon 08-17, Tue 08-18, Wed 08-19, Thu 08-20, Fri 08-21, Sat 08-22, Sun 08-23.
"""

from __future__ import annotations

import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from jarz_pos.services import label_stock as ls

FRIDAY = {4}
FRI_SAT = {4, 5}


@contextmanager
def _settings(**overrides):
    """Patch the settings reader so no site is needed."""
    values = {
        "label_print_lead_days_min": 2,
        "label_print_lead_days_max": 3,
        "label_print_rest_day": "Friday",
        "label_reorder_buffer_days": 3,
        "label_auto_consume_on_invoice": 1,
        "label_alerts_enabled": 1,
    }
    values.update(overrides)
    with patch.object(ls, "_single_value", side_effect=lambda field: values.get(field)):
        yield


def _item(qty, item_group="Medium", is_bundle_parent=0):
    return SimpleNamespace(qty=qty, item_group=item_group, is_bundle_parent=is_bundle_parent)


def _label(name, group=None, per_unit=1.0):
    return {"name": name, "label_title": name, "applies_to_item_group": group, "labels_per_unit": per_unit}


# ---------------------------------------------------------------------------
# Working-day date maths
# ---------------------------------------------------------------------------
class TestWorkingDays(unittest.TestCase):
    def test_skips_friday(self):
        # Wed + 3 printing days, Friday closed: Thu, (Fri skipped), Sat, Sun.
        self.assertEqual(str(ls.add_working_days("2026-08-19", 3, FRIDAY)), "2026-08-23")

    def test_two_day_batch_crossing_friday(self):
        # Wed + 2: Thu, (Fri skipped), Sat.
        self.assertEqual(str(ls.add_working_days("2026-08-19", 2, FRIDAY)), "2026-08-22")

    def test_week_without_a_friday_is_plain_calendar(self):
        # Mon + 3 never reaches Friday, so it lands on Thursday.
        self.assertEqual(str(ls.add_working_days("2026-08-17", 3, FRIDAY)), "2026-08-20")

    def test_starting_on_thursday_pushes_past_the_rest_day(self):
        self.assertEqual(str(ls.add_working_days("2026-08-20", 1, FRIDAY)), "2026-08-22")

    def test_starting_on_the_rest_day_itself(self):
        # Fri + 1 printing day is Saturday: the rest day is never counted, but a
        # batch handed over on it still starts the clock the next working day.
        self.assertEqual(str(ls.add_working_days("2026-08-21", 1, FRIDAY)), "2026-08-22")

    def test_two_rest_days(self):
        # Wed + 3 with Fri+Sat closed: Thu, (Fri, Sat skipped), Sun, Mon.
        self.assertEqual(str(ls.add_working_days("2026-08-19", 3, FRI_SAT)), "2026-08-24")

    def test_no_rest_day_is_a_calendar_add(self):
        self.assertEqual(str(ls.add_working_days("2026-08-19", 3, set())), "2026-08-22")

    def test_zero_and_negative_days_are_identity(self):
        self.assertEqual(str(ls.add_working_days("2026-08-19", 0, FRIDAY)), "2026-08-19")
        self.assertEqual(str(ls.add_working_days("2026-08-19", -4, FRIDAY)), "2026-08-19")

    def test_a_full_week_of_rest_days_degrades_instead_of_hanging(self):
        # Misconfiguration must not spin the scheduler forever.
        self.assertEqual(str(ls.add_working_days("2026-08-19", 3, set(range(7)))), "2026-08-22")

    def test_long_batch_spans_multiple_rest_days(self):
        # Wed + 10 printing days crosses two Fridays -> 12 calendar days.
        self.assertEqual(str(ls.add_working_days("2026-08-19", 10, FRIDAY)), "2026-08-31")


class TestLeadTime(unittest.TestCase):
    def test_expected_ready_uses_the_slow_end(self):
        with _settings():
            self.assertEqual(str(ls.expected_ready_date("2026-08-19")), "2026-08-23")

    def test_calendar_days_include_the_skipped_rest_day(self):
        # Three printing days from Wednesday is four calendar days out, and it is
        # calendar days the days-of-cover threshold has to compare against.
        with _settings():
            self.assertEqual(ls.lead_time_calendar_days("2026-08-19"), 4)

    def test_calendar_days_match_working_days_when_no_rest_day_is_crossed(self):
        with _settings():
            self.assertEqual(ls.lead_time_calendar_days("2026-08-17"), 3)


# ---------------------------------------------------------------------------
# Settings fallbacks
# ---------------------------------------------------------------------------
class TestSettings(unittest.TestCase):
    def test_unwritten_fields_fall_back_to_documented_defaults(self):
        # A Single stores nothing for a newly added field, so every read is None.
        with patch.object(ls, "_single_value", return_value=None):
            settings = ls.get_label_settings()
        self.assertEqual(settings["lead_days_min"], 2)
        self.assertEqual(settings["lead_days_max"], 3)
        self.assertEqual(settings["rest_day"], "Friday")
        self.assertEqual(settings["rest_weekdays"], FRIDAY)
        self.assertEqual(settings["buffer_days"], 3)
        # Both flags default ON: the feature is inert until a label exists, so
        # there is no dark switch for anybody to forget.
        self.assertTrue(settings["auto_consume"])
        self.assertTrue(settings["alerts_enabled"])

    def test_operator_can_switch_the_flags_off(self):
        with _settings(label_auto_consume_on_invoice=0, label_alerts_enabled=0):
            settings = ls.get_label_settings()
        self.assertFalse(settings["auto_consume"])
        self.assertFalse(settings["alerts_enabled"])

    def test_zero_buffer_is_respected_but_zero_lead_is_not(self):
        # Zero buffer is a real choice; a zero lead time would mean printing is
        # instant, which it is not, so it falls back.
        with _settings(label_reorder_buffer_days=0, label_print_lead_days_max=0):
            settings = ls.get_label_settings()
        self.assertEqual(settings["buffer_days"], 0)
        self.assertEqual(settings["lead_days_max"], 3)

    def test_max_below_min_is_clamped_up(self):
        with _settings(label_print_lead_days_min=5, label_print_lead_days_max=2):
            settings = ls.get_label_settings()
        self.assertEqual(settings["lead_days_max"], 5)

    def test_unknown_rest_day_falls_back_to_friday(self):
        with _settings(label_print_rest_day="Blursday"):
            settings = ls.get_label_settings()
        self.assertEqual(settings["rest_day"], "Friday")
        self.assertEqual(settings["rest_weekdays"], FRIDAY)

    def test_none_rest_day_means_the_printer_never_closes(self):
        with _settings(label_print_rest_day="None"):
            settings = ls.get_label_settings()
        self.assertEqual(settings["rest_weekdays"], set())


# ---------------------------------------------------------------------------
# Status decision
# ---------------------------------------------------------------------------
def _status(**kwargs):
    args = {
        "tracked": True,
        "on_hand": 1000,
        "min_stock": 100,
        "avg_daily_usage": 10.0,
        "lead_days": 4,
        "buffer_days": 3,
        "has_open_print_order": False,
    }
    args.update(kwargs)
    return ls.compute_status(**args)


class TestStatus(unittest.TestCase):
    def test_customers_who_print_their_own_are_never_tracked(self):
        self.assertEqual(_status(tracked=False, on_hand=0), ls.STATUS_NOT_TRACKED)

    def test_healthy_stock_is_ok(self):
        # 1000 on hand at 10/day = 100 days of cover.
        self.assertEqual(_status(), ls.STATUS_OK)

    def test_zero_on_hand_is_out_of_stock(self):
        self.assertEqual(_status(on_hand=0), ls.STATUS_OUT_OF_STOCK)

    def test_negative_on_hand_is_out_of_stock(self):
        # A ledger can go negative when consumption is recorded before a receipt.
        self.assertEqual(_status(on_hand=-20), ls.STATUS_OUT_OF_STOCK)

    def test_out_of_stock_outranks_an_open_print_order(self):
        # A batch arriving Sunday does not make an empty shelf on Thursday fine.
        self.assertEqual(
            _status(on_hand=0, has_open_print_order=True), ls.STATUS_OUT_OF_STOCK
        )

    def test_below_the_safety_floor_is_reorder_now_even_with_huge_cover(self):
        # 90 left at 0.1/day is 900 days of cover, but the floor is the floor.
        self.assertEqual(
            _status(on_hand=90, min_stock=100, avg_daily_usage=0.1), ls.STATUS_REORDER_NOW
        )

    def test_cover_shorter_than_the_lead_time_is_reorder_now(self):
        # 30 at 10/day = 3 days of cover against a 4-day print lead time: a batch
        # ordered today lands after the shelf is empty.
        self.assertEqual(_status(on_hand=30, min_stock=0), ls.STATUS_REORDER_NOW)

    def test_cover_exactly_at_the_lead_time_is_reorder_now(self):
        self.assertEqual(_status(on_hand=40, min_stock=0), ls.STATUS_REORDER_NOW)

    def test_cover_within_lead_plus_buffer_is_reorder_soon(self):
        # 60 at 10/day = 6 days, between the 4-day lead and the 7-day horizon.
        self.assertEqual(_status(on_hand=60, min_stock=0), ls.STATUS_REORDER_SOON)

    def test_cover_just_past_the_buffer_is_ok(self):
        # 71 at 10/day = 7.1 days, past lead+buffer = 7.
        self.assertEqual(_status(on_hand=71, min_stock=0), ls.STATUS_OK)

    def test_an_open_print_order_quiets_reorder_now(self):
        self.assertEqual(
            _status(on_hand=30, min_stock=0, has_open_print_order=True), ls.STATUS_ON_ORDER
        )

    def test_an_open_print_order_quiets_reorder_soon(self):
        self.assertEqual(
            _status(on_hand=60, min_stock=0, has_open_print_order=True), ls.STATUS_ON_ORDER
        )

    def test_unknown_usage_never_invents_urgency(self):
        # A brand-new label with stock and no consumption history yet has no
        # meaningful days-of-cover, so only the safety floor can flag it.
        self.assertEqual(_status(on_hand=500, min_stock=0, avg_daily_usage=0), ls.STATUS_OK)
        self.assertEqual(
            _status(on_hand=50, min_stock=100, avg_daily_usage=0), ls.STATUS_REORDER_NOW
        )


# ---------------------------------------------------------------------------
# Invoice -> label attribution
# ---------------------------------------------------------------------------
class TestInvoiceUsage(unittest.TestCase):
    def test_single_catch_all_label_counts_every_line(self):
        doc = SimpleNamespace(items=[_item(10, "Medium"), _item(5, "Large")])
        self.assertEqual(ls.invoice_label_usage(doc, [_label("L1")]), {"L1": 15})

    def test_bundle_parent_rows_are_skipped(self):
        # The parent carries the bundle SKU at 100% discount; the jars are the
        # children. Counting both would double every bundled order.
        doc = SimpleNamespace(
            items=[
                _item(2, "Bundles", is_bundle_parent=1),
                _item(4, "Medium"),
                _item(2, "Large"),
            ]
        )
        self.assertEqual(ls.invoice_label_usage(doc, [_label("L1")]), {"L1": 6})

    def test_item_group_scoping_routes_each_line_to_its_own_label(self):
        doc = SimpleNamespace(items=[_item(10, "Medium"), _item(4, "Large")])
        labels = [_label("MED", "Medium"), _label("LRG", "Large")]
        self.assertEqual(ls.invoice_label_usage(doc, labels), {"MED": 10, "LRG": 4})

    def test_catch_all_absorbs_only_the_unclaimed_groups(self):
        doc = SimpleNamespace(
            items=[_item(10, "Medium"), _item(4, "Large"), _item(3, "Small")]
        )
        labels = [_label("MED", "Medium"), _label("REST")]
        self.assertEqual(ls.invoice_label_usage(doc, labels), {"MED": 10, "REST": 7})

    def test_lines_with_no_matching_label_are_ignored(self):
        doc = SimpleNamespace(items=[_item(10, "Medium"), _item(4, "Merch")])
        self.assertEqual(ls.invoice_label_usage(doc, [_label("MED", "Medium")]), {"MED": 10})

    def test_labels_per_unit_multiplies(self):
        # A jar that carries a body label and a lid label.
        doc = SimpleNamespace(items=[_item(10, "Medium")])
        self.assertEqual(ls.invoice_label_usage(doc, [_label("L1", per_unit=2)]), {"L1": 20})

    def test_return_invoice_yields_a_positive_credit(self):
        # Return lines carry negative qty; the labels come back on the jars.
        doc = SimpleNamespace(items=[_item(-6, "Medium")])
        self.assertEqual(ls.invoice_label_usage(doc, [_label("L1")]), {"L1": -6})

    def test_zero_qty_and_empty_invoices_produce_nothing(self):
        self.assertEqual(ls.invoice_label_usage(SimpleNamespace(items=[]), [_label("L1")]), {})
        self.assertEqual(
            ls.invoice_label_usage(SimpleNamespace(items=[_item(0)]), [_label("L1")]), {}
        )

    def test_no_labels_configured_consumes_nothing(self):
        doc = SimpleNamespace(items=[_item(10, "Medium")])
        self.assertEqual(ls.invoice_label_usage(doc, []), {})


# ---------------------------------------------------------------------------
# Ledger reads
# ---------------------------------------------------------------------------
class TestOnHand(unittest.TestCase):
    def test_on_hand_is_the_signed_sum_of_the_ledger(self):
        rows = [{"qty": 1000}, {"qty": -120}, {"qty": -80}, {"qty": 25}]
        with patch.object(ls, "_doctype_exists", return_value=True), patch.object(
            ls.frappe, "get_all", return_value=rows
        ):
            self.assertEqual(ls.get_on_hand("LBL-1"), 825)

    def test_empty_ledger_is_zero_not_an_error(self):
        with patch.object(ls, "_doctype_exists", return_value=True), patch.object(
            ls.frappe, "get_all", return_value=[]
        ):
            self.assertEqual(ls.get_on_hand("LBL-1"), 0)

    def test_missing_doctype_is_zero_not_an_error(self):
        # CI runs before migrate, so this is the state on the very first run.
        with patch.object(ls, "_doctype_exists", return_value=False):
            self.assertEqual(ls.get_on_hand("LBL-1"), 0)


class TestRunsOut(unittest.TestCase):
    def test_no_usage_means_no_projection(self):
        self.assertIsNone(ls._runs_out_on(500, 0))

    def test_no_stock_means_no_projection(self):
        self.assertIsNone(ls._runs_out_on(0, 10))

    def test_projection_is_stock_over_rate(self):
        from frappe.utils import add_days, today

        self.assertEqual(ls._runs_out_on(100, 10), str(add_days(today(), 10)))


# ---------------------------------------------------------------------------
# Consumption hook
# ---------------------------------------------------------------------------
class TestConsumptionHook(unittest.TestCase):
    def _invoice(self, name="SI-1", is_return=0):
        return SimpleNamespace(
            name=name,
            customer="CUST-A",
            posting_date="2026-08-17",
            is_return=is_return,
            items=[_item(10, "Medium")],
        )

    def test_b2c_invoice_fast_exits_without_posting(self):
        with _settings(), patch.object(ls, "labels_for_customer", return_value=[]), patch.object(
            ls, "post_movement"
        ) as post:
            ls.consume_labels_on_invoice_submit(self._invoice())
        post.assert_not_called()

    def test_label_customer_is_consumed_negatively(self):
        with _settings(), patch.object(
            ls, "labels_for_customer", return_value=[_label("L1")]
        ), patch.object(ls, "_movements_for_invoice", return_value=[]), patch.object(
            ls, "post_movement"
        ) as post:
            ls.consume_labels_on_invoice_submit(self._invoice())
        post.assert_called_once()
        kwargs = post.call_args.kwargs
        self.assertEqual(kwargs["label"], "L1")
        self.assertEqual(kwargs["movement_type"], "Consumed")
        self.assertEqual(kwargs["qty"], -10)
        self.assertEqual(kwargs["reference_name"], "SI-1")

    def test_second_fire_on_the_same_invoice_is_a_no_op(self):
        # Idempotency is checked against the ledger, not a flag on the invoice --
        # Sales Invoice is at the MariaDB column limit and cannot take another field.
        with _settings(), patch.object(
            ls, "labels_for_customer", return_value=[_label("L1")]
        ), patch.object(
            ls, "_movements_for_invoice", return_value=[{"name": "M1", "label": "L1", "qty": -10}]
        ), patch.object(ls, "post_movement") as post:
            ls.consume_labels_on_invoice_submit(self._invoice())
        post.assert_not_called()

    def test_auto_consume_off_posts_nothing(self):
        with _settings(label_auto_consume_on_invoice=0), patch.object(
            ls, "labels_for_customer", return_value=[_label("L1")]
        ), patch.object(ls, "post_movement") as post:
            ls.consume_labels_on_invoice_submit(self._invoice())
        post.assert_not_called()

    def test_a_failure_never_escapes_into_the_submit(self):
        # A label ledger must not be able to block a sale.
        with _settings(), patch.object(
            ls, "labels_for_customer", side_effect=RuntimeError("boom")
        ), patch.object(ls.frappe, "log_error") as log:
            ls.consume_labels_on_invoice_submit(self._invoice())
        log.assert_called()

    def test_cancel_credits_the_net_back(self):
        rows = [{"name": "M1", "label": "L1", "qty": -10, "movement_type": "Consumed"}]
        with patch.object(ls, "_movements_for_invoice", return_value=rows), patch.object(
            ls, "post_movement"
        ) as post:
            ls.reverse_labels_on_invoice_cancel(SimpleNamespace(name="SI-1"))
        post.assert_called_once()
        self.assertEqual(post.call_args.kwargs["qty"], 10)

    def test_cancelling_an_already_reversed_invoice_does_not_double_credit(self):
        rows = [
            {"name": "M1", "label": "L1", "qty": -10, "movement_type": "Consumed"},
            {"name": "M2", "label": "L1", "qty": 10, "movement_type": "Adjustment"},
        ]
        with patch.object(ls, "_movements_for_invoice", return_value=rows), patch.object(
            ls, "post_movement"
        ) as post:
            ls.reverse_labels_on_invoice_cancel(SimpleNamespace(name="SI-1"))
        post.assert_not_called()

    def test_cancelling_an_invoice_that_never_consumed_is_a_no_op(self):
        with patch.object(ls, "_movements_for_invoice", return_value=[]), patch.object(
            ls, "post_movement"
        ) as post:
            ls.reverse_labels_on_invoice_cancel(SimpleNamespace(name="SI-1"))
        post.assert_not_called()


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
class TestSummarise(unittest.TestCase):
    def test_counts_by_status(self):
        snaps = [
            {"status": ls.STATUS_OUT_OF_STOCK, "tracked": True, "needs_attention": True},
            {"status": ls.STATUS_REORDER_NOW, "tracked": True, "needs_attention": True},
            {"status": ls.STATUS_REORDER_SOON, "tracked": True, "needs_attention": True},
            {"status": ls.STATUS_ON_ORDER, "tracked": True, "needs_attention": False},
            {"status": ls.STATUS_OK, "tracked": True, "needs_attention": False},
            {"status": ls.STATUS_NOT_TRACKED, "tracked": False, "needs_attention": False},
        ]
        summary = ls.summarise(snaps)
        self.assertEqual(summary["total"], 6)
        self.assertEqual(summary["tracked"], 5)
        self.assertEqual(summary["needs_attention"], 3)
        self.assertEqual(summary["out_of_stock"], 1)
        self.assertEqual(summary["on_order"], 1)
        self.assertEqual(summary["not_tracked"], 1)

    def test_empty_input_is_all_zeroes(self):
        self.assertEqual(ls.summarise([])["total"], 0)


class TestAlertMessage(unittest.TestCase):
    def test_message_states_the_lead_time_and_the_ready_date(self):
        snap = {
            "customer_name": "Cafe X",
            "label_title": "250g",
            "on_hand_qty": 40,
            "days_of_cover": 4.0,
            "avg_daily_usage": 10.0,
            "runs_out_on": "2026-08-21",
            "lead_days_min": 2,
            "lead_days_max": 3,
            "rest_day": "Friday",
            "expected_ready_if_ordered_today": "2026-08-20",
            "suggested_print_qty": 500,
        }
        message = ls._alert_message(snap)
        self.assertIn("Cafe X", message)
        self.assertIn("40 label(s) left", message)
        self.assertIn("2-3 working days", message)
        self.assertIn("Friday excluded", message)
        self.assertIn("2026-08-20", message)
        self.assertIn("500", message)


# ---------------------------------------------------------------------------
# Snapshot write-back
# ---------------------------------------------------------------------------
class TestWriteSnapshot(unittest.TestCase):
    def _snapshot(self, **overrides):
        snap = {
            "name": "JLBL-00001",
            "on_hand_qty": 40,
            "avg_daily_usage": 10.0,
            "days_of_cover": 4.0,
            "status": ls.STATUS_REORDER_NOW,
            "last_movement_on": "2026-08-16",
        }
        snap.update(overrides)
        return snap

    def test_writes_the_status_it_was_handed(self):
        # The daily pass alerts on one snapshot and writes the same one, so the
        # row can never end up recording a status different from the one that
        # was announced.
        with patch.object(ls.frappe.db, "set_value") as write:
            ls.write_snapshot(self._snapshot())
        written = {call.args[2]: call.args[3] for call in write.call_args_list}
        self.assertEqual(written["status"], ls.STATUS_REORDER_NOW)
        self.assertEqual(written["on_hand_qty"], 40)
        self.assertEqual(written["days_of_cover"], 4.0)

    def test_unknown_cover_is_stored_as_zero_not_null(self):
        # The column is a Float; None would fail the write on some backends.
        with patch.object(ls.frappe.db, "set_value") as write:
            ls.write_snapshot(self._snapshot(days_of_cover=None))
        written = {call.args[2]: call.args[3] for call in write.call_args_list}
        self.assertEqual(written["days_of_cover"], 0)

    def test_a_snapshot_with_no_name_writes_nothing(self):
        with patch.object(ls.frappe.db, "set_value") as write:
            ls.write_snapshot({"name": None})
        write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
