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


def _item(qty, item_code="MANGO-M", item_group="Medium", item_name=None, is_bundle_parent=0):
    return SimpleNamespace(
        qty=qty,
        item_code=item_code,
        item_name=item_name or item_code,
        item_group=item_group,
        is_bundle_parent=is_bundle_parent,
    )


def _label(name, item="MANGO-M", per_unit=1.0, location=None):
    return {
        "name": name,
        "label_title": name,
        "item": item,
        "size": "Medium",
        "labels_per_unit": per_unit,
        "storage_location": location,
    }


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
    """v2 attribution: every flavour has its own label, matched by item_code."""

    def test_each_flavour_draws_its_own_label(self):
        doc = SimpleNamespace(items=[_item(10, "MANGO-M"), _item(4, "BERRY-M")])
        labels = [_label("L-MANGO", "MANGO-M"), _label("L-BERRY", "BERRY-M")]
        usage, unmatched = ls.invoice_label_usage(doc, labels)
        self.assertEqual(usage, {"L-MANGO": 10, "L-BERRY": 4})
        self.assertEqual(unmatched, [])

    def test_bundle_parent_rows_are_skipped(self):
        # The parent carries the bundle SKU at 100% discount; the jars are the
        # children. Counting both would double every bundled order.
        doc = SimpleNamespace(
            items=[
                _item(2, "BUNDLE-X", item_group="Medium", is_bundle_parent=1),
                _item(4, "MANGO-M"),
                _item(2, "BERRY-M"),
            ]
        )
        labels = [_label("L-MANGO", "MANGO-M"), _label("L-BERRY", "BERRY-M")]
        usage, unmatched = ls.invoice_label_usage(doc, labels)
        self.assertEqual(usage, {"L-MANGO": 4, "L-BERRY": 2})
        self.assertEqual(unmatched, [])

    def test_a_flavour_without_a_label_is_reported_unmatched(self):
        # Customers add flavours at will; an untracked one must surface, not
        # silently consume nothing.
        doc = SimpleNamespace(items=[_item(10, "MANGO-M"), _item(3, "NEWFLAV-M")])
        usage, unmatched = ls.invoice_label_usage(doc, [_label("L-MANGO", "MANGO-M")])
        self.assertEqual(usage, {"L-MANGO": 10})
        self.assertEqual(len(unmatched), 1)
        self.assertEqual(unmatched[0]["item_code"], "NEWFLAV-M")
        self.assertEqual(unmatched[0]["labels"], 3)

    def test_non_jar_lines_are_ignored_not_unmatched(self):
        # Merch and services carry no customer label; reporting them unmatched
        # would auto-create nonsense labels.
        doc = SimpleNamespace(items=[_item(10, "MANGO-M"), _item(1, "TOTE", item_group="Merch")])
        usage, unmatched = ls.invoice_label_usage(doc, [_label("L-MANGO", "MANGO-M")])
        self.assertEqual(usage, {"L-MANGO": 10})
        self.assertEqual(unmatched, [])

    def test_the_meduim_typo_group_still_counts_as_a_jar(self):
        doc = SimpleNamespace(items=[_item(5, "MANGO-M", item_group="Meduim")])
        usage, unmatched = ls.invoice_label_usage(doc, [_label("L-MANGO", "MANGO-M")])
        self.assertEqual(usage, {"L-MANGO": 5})

    def test_labels_per_unit_multiplies(self):
        # A jar that carries a body label and a lid label.
        doc = SimpleNamespace(items=[_item(10, "MANGO-M")])
        usage, _ = ls.invoice_label_usage(doc, [_label("L1", "MANGO-M", per_unit=2)])
        self.assertEqual(usage, {"L1": 20})

    def test_return_invoice_yields_a_positive_credit(self):
        # Return lines carry negative qty; the labels come back on the jars.
        doc = SimpleNamespace(items=[_item(-6, "MANGO-M")])
        usage, _ = ls.invoice_label_usage(doc, [_label("L1", "MANGO-M")])
        self.assertEqual(usage, {"L1": -6})

    def test_zero_qty_and_empty_invoices_produce_nothing(self):
        usage, unmatched = ls.invoice_label_usage(SimpleNamespace(items=[]), [_label("L1")])
        self.assertEqual((usage, unmatched), ({}, []))
        usage, unmatched = ls.invoice_label_usage(
            SimpleNamespace(items=[_item(0)]), [_label("L1")]
        )
        self.assertEqual((usage, unmatched), ({}, []))

    def test_no_labels_still_reports_jar_lines_unmatched(self):
        doc = SimpleNamespace(items=[_item(10, "MANGO-M")])
        usage, unmatched = ls.invoice_label_usage(doc, [])
        self.assertEqual(usage, {})
        self.assertEqual(len(unmatched), 1)


class TestSheets(unittest.TestCase):
    """Sheet geometry: 21 labels per Medium sheet, 18 per Large."""

    def _settings(self):
        return {"sheet_medium": 21, "sheet_large": 18, "default_print_sheets": 2}

    def test_medium_uses_21(self):
        row = {"size": "Medium", "labels_per_sheet": 0}
        self.assertEqual(ls.labels_per_sheet_for(row, settings=self._settings()), 21)

    def test_large_uses_18(self):
        row = {"size": "Large", "labels_per_sheet": 0}
        self.assertEqual(ls.labels_per_sheet_for(row, settings=self._settings()), 18)

    def test_the_meduim_typo_size_falls_back_to_medium(self):
        row = {"size": "Meduim", "labels_per_sheet": 0}
        self.assertEqual(ls.labels_per_sheet_for(row, settings=self._settings()), 21)

    def test_per_label_override_wins(self):
        row = {"size": "Large", "labels_per_sheet": 30}
        self.assertEqual(ls.labels_per_sheet_for(row, settings=self._settings()), 30)

    def test_missing_size_is_treated_as_medium(self):
        row = {"size": None, "labels_per_sheet": 0}
        self.assertEqual(ls.labels_per_sheet_for(row, settings=self._settings()), 21)


# ---------------------------------------------------------------------------
# Ledger reads
# ---------------------------------------------------------------------------
class TestOnHand(unittest.TestCase):
    def test_on_hand_is_the_signed_sum_of_the_ledger(self):
        rows = [
            {"qty": 1000, "value": 500.0},
            {"qty": -120, "value": -60.0},
            {"qty": -80, "value": -40.0},
            {"qty": 25, "value": 12.5},
        ]
        with patch.object(ls, "_doctype_exists", return_value=True), patch.object(
            ls.frappe, "get_all", return_value=rows
        ):
            self.assertEqual(ls.get_on_hand("LBL-1"), 825)

    def test_position_carries_value_and_weighted_average(self):
        rows = [
            {"qty": 1000, "value": 500.0},   # printed at 0.50
            {"qty": -200, "value": -100.0},  # consumed at 0.50
        ]
        with patch.object(ls, "_doctype_exists", return_value=True), patch.object(
            ls.frappe, "get_all", return_value=rows
        ):
            position = ls.get_position("LBL-1")
        self.assertEqual(position["on_hand"], 800)
        self.assertEqual(position["value"], 400.0)
        self.assertEqual(position["avg_cost"], 0.5)

    def test_two_batches_at_different_costs_weight_the_average(self):
        # 500 at 0.40 plus 500 at 0.60 = 1000 worth 500.00 -> 0.50 each.
        rows = [
            {"qty": 500, "value": 200.0},
            {"qty": 500, "value": 300.0},
        ]
        with patch.object(ls, "_doctype_exists", return_value=True), patch.object(
            ls.frappe, "get_all", return_value=rows
        ):
            self.assertEqual(ls.get_position("LBL-1")["avg_cost"], 0.5)

    def test_a_billed_later_revaluation_lifts_the_average(self):
        # Received unbilled (value 0), then a zero-qty revaluation carries the
        # bill's value: the average must absorb it.
        rows = [
            {"qty": 420, "value": 0.0},    # unbilled receipt
            {"qty": 0, "value": 210.0},    # revaluation when the PI landed
        ]
        with patch.object(ls, "_doctype_exists", return_value=True), patch.object(
            ls.frappe, "get_all", return_value=rows
        ):
            self.assertEqual(ls.get_position("LBL-1")["avg_cost"], 0.5)

    def test_zero_stock_has_zero_average_not_a_division_error(self):
        rows = [{"qty": 100, "value": 50.0}, {"qty": -100, "value": -50.0}]
        with patch.object(ls, "_doctype_exists", return_value=True), patch.object(
            ls.frappe, "get_all", return_value=rows
        ):
            position = ls.get_position("LBL-1")
        self.assertEqual(position["on_hand"], 0)
        self.assertEqual(position["avg_cost"], 0.0)

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
            items=[_item(10, "MANGO-M")],
        )

    def test_b2c_invoice_fast_exits_without_posting(self):
        with _settings(), patch.object(ls, "labels_for_customer", return_value=[]), patch.object(
            ls, "post_movement"
        ) as post:
            ls.consume_labels_on_invoice_submit(self._invoice())
        post.assert_not_called()

    def test_label_customer_is_consumed_negatively(self):
        with _settings(), patch.object(
            ls, "labels_for_customer", return_value=[_label("L1", "MANGO-M")]
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
            ls, "labels_for_customer", return_value=[_label("L1", "MANGO-M")]
        ), patch.object(
            ls, "_movements_for_invoice", return_value=[{"name": "M1", "label": "L1", "qty": -10}]
        ), patch.object(ls, "post_movement") as post:
            ls.consume_labels_on_invoice_submit(self._invoice())
        post.assert_not_called()

    def test_auto_consume_off_posts_nothing(self):
        with _settings(label_auto_consume_on_invoice=0), patch.object(
            ls, "labels_for_customer", return_value=[_label("L1", "MANGO-M")]
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

    def test_a_new_flavour_is_auto_created_and_consumed_dark(self):
        # ilo adds a flavour: the first invoice carrying it must create its
        # label at zero stock and draw it negative, so the board shows Out of
        # Stock instead of the flavour silently not existing.
        invoice = SimpleNamespace(
            name="SI-2",
            customer="CUST-A",
            posting_date="2026-08-17",
            is_return=0,
            items=[_item(10, "MANGO-M"), _item(3, "NEWFLAV-M")],
        )
        with _settings(), patch.object(
            ls, "labels_for_customer", return_value=[_label("L1", "MANGO-M")]
        ), patch.object(ls, "_movements_for_invoice", return_value=[]), patch.object(
            ls, "_auto_create_label", return_value="L-NEW"
        ) as created, patch.object(ls, "post_movement") as post:
            ls.consume_labels_on_invoice_submit(invoice)
        created.assert_called_once()
        posted = {c.kwargs["label"]: c.kwargs["qty"] for c in post.call_args_list}
        self.assertEqual(posted, {"L1": -10, "L-NEW": -3})

    def test_auto_create_failure_still_consumes_the_matched_flavours(self):
        invoice = SimpleNamespace(
            name="SI-3",
            customer="CUST-A",
            posting_date="2026-08-17",
            is_return=0,
            items=[_item(10, "MANGO-M"), _item(3, "NEWFLAV-M")],
        )
        with _settings(), patch.object(
            ls, "labels_for_customer", return_value=[_label("L1", "MANGO-M")]
        ), patch.object(ls, "_movements_for_invoice", return_value=[]), patch.object(
            ls, "_auto_create_label", return_value=None
        ), patch.object(ls, "post_movement") as post:
            ls.consume_labels_on_invoice_submit(invoice)
        posted = {c.kwargs["label"]: c.kwargs["qty"] for c in post.call_args_list}
        self.assertEqual(posted, {"L1": -10})

    def test_cancel_credits_the_net_back(self):
        rows = [{"name": "M1", "label": "L1", "qty": -10, "value": -5.0, "movement_type": "Consumed"}]
        with patch.object(ls, "_movements_for_invoice", return_value=rows), patch.object(
            ls, "post_movement"
        ) as post:
            ls.reverse_labels_on_invoice_cancel(SimpleNamespace(name="SI-1"))
        post.assert_called_once()
        self.assertEqual(post.call_args.kwargs["qty"], 10)

    def test_cancel_reverses_at_the_original_cost_not_todays_average(self):
        # Consumed at 0.50, then a newer batch moved the average to 0.80. The
        # reversal must credit back exactly the 5.00 the consumption took out,
        # or the inventory account keeps a 3.00 residue forever.
        rows = [{"name": "M1", "label": "L1", "qty": -10, "value": -5.0, "movement_type": "Consumed"}]
        with patch.object(ls, "_movements_for_invoice", return_value=rows), patch.object(
            ls, "post_movement"
        ) as post:
            ls.reverse_labels_on_invoice_cancel(SimpleNamespace(name="SI-1"))
        self.assertEqual(post.call_args.kwargs["unit_cost"], 0.5)

    def test_cancelling_an_already_reversed_invoice_does_not_double_credit(self):
        rows = [
            {"name": "M1", "label": "L1", "qty": -10, "value": -5.0, "movement_type": "Consumed"},
            {"name": "M2", "label": "L1", "qty": 10, "value": 5.0, "movement_type": "Adjustment"},
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
# Movement valuation + GL decision
# ---------------------------------------------------------------------------
class TestMovementValuation(unittest.TestCase):
    """post_movement prices rows and decides when the GL hears about them."""

    def _run(self, *, movement_type, qty, unit_cost=None, post_gl=True, avg_cost=0.5):
        from unittest.mock import MagicMock

        doc = MagicMock()
        with patch.object(ls.frappe, "new_doc", return_value=doc), patch.object(
            ls, "get_position", return_value={"on_hand": 100, "value": 50.0, "avg_cost": avg_cost}
        ), patch.object(ls, "_post_value_journal") as je, patch.object(
            ls, "refresh_label"
        ):
            ls.post_movement(
                label="L1",
                movement_type=movement_type,
                qty=qty,
                unit_cost=unit_cost,
                post_gl=post_gl,
            )
        return doc, je

    def test_consumption_prices_at_the_running_average(self):
        doc, je = self._run(movement_type="Consumed", qty=10, avg_cost=0.5)
        self.assertEqual(doc.qty, -10)
        self.assertEqual(doc.unit_cost, 0.5)
        self.assertEqual(doc.value, -5.0)
        je.assert_called_once()

    def test_receipts_price_at_the_batch_cost_and_skip_the_je(self):
        # The PI already debited the inventory account; a JE too would double it.
        doc, je = self._run(
            movement_type="Print Received", qty=420, unit_cost=0.4762, post_gl=False
        )
        self.assertEqual(doc.qty, 420)
        self.assertEqual(doc.unit_cost, 0.4762)
        self.assertAlmostEqual(doc.value, 200.0, places=1)
        je.assert_not_called()

    def test_a_return_credit_carries_positive_value_and_posts(self):
        doc, je = self._run(movement_type="Adjustment", qty=6, avg_cost=0.5)
        self.assertEqual(doc.value, 3.0)
        je.assert_called_once()

    def test_zero_value_movements_skip_the_je(self):
        # An unbilled label has no cost yet: the count moves, the GL does not.
        doc, je = self._run(movement_type="Consumed", qty=10, avg_cost=0.0)
        self.assertEqual(doc.value, 0.0)
        je.assert_not_called()

    def test_negative_unit_cost_is_clamped_to_zero(self):
        doc, je = self._run(movement_type="Consumed", qty=10, unit_cost=-3)
        self.assertEqual(doc.unit_cost, 0.0)
        self.assertEqual(doc.value, 0.0)


class TestValueJournalGuards(unittest.TestCase):
    """_post_value_journal must fail into a log line, never into the sale."""

    def _movement(self, value=-5.0):
        from unittest.mock import MagicMock

        movement = MagicMock()
        movement.value = value
        movement.qty = -10
        movement.label = "L1"
        movement.name = "JLMV-1"
        movement.posting_date = "2026-08-17"
        return movement

    def test_unconfigured_accounts_mean_count_only_mode(self):
        with patch.object(ls, "get_label_settings", return_value={
            "post_cogs": True, "inventory_account": "", "cogs_account": "",
        }), patch.object(ls.frappe, "new_doc") as new_doc:
            ls._post_value_journal(self._movement())
        new_doc.assert_not_called()

    def test_the_kill_switch_stops_posting(self):
        with patch.object(ls, "get_label_settings", return_value={
            "post_cogs": False, "inventory_account": "A", "cogs_account": "B",
        }), patch.object(ls.frappe, "new_doc") as new_doc:
            ls._post_value_journal(self._movement())
        new_doc.assert_not_called()

    def test_a_je_crash_is_logged_not_raised(self):
        with patch.object(ls, "get_label_settings", side_effect=RuntimeError("boom")), patch.object(
            ls.frappe, "log_error"
        ) as log:
            ls._post_value_journal(self._movement())  # must not raise
        log.assert_called()


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
            "label_title": "Mango",
            "size": "Medium",
            "on_hand_qty": 40,
            "days_of_cover": 4.0,
            "avg_daily_usage": 10.0,
            "runs_out_on": "2026-08-21",
            "lead_days_min": 2,
            "lead_days_max": 3,
            "rest_day": "Friday",
            "expected_ready_if_ordered_today": "2026-08-20",
            "suggested_print_sheets": 2,
            "labels_per_sheet": 21,
        }
        message = ls._alert_message(snap)
        self.assertIn("Cafe X", message)
        self.assertIn("Mango", message)
        self.assertIn("(Medium)", message)
        self.assertIn("40 label(s) left", message)
        self.assertIn("2-3 working days", message)
        self.assertIn("Friday excluded", message)
        self.assertIn("2026-08-20", message)
        # The printer sells sheets: the ask is 2 sheets, with the 42-label
        # equivalence spelled out.
        self.assertIn("2 sheet(s) (42 labels)", message)


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


# ---------------------------------------------------------------------------
# Settings reader (regression: the feature shipped dark once)
# ---------------------------------------------------------------------------
class TestSingleValueReader(unittest.TestCase):
    """The unset-vs-zero distinction every default in this module rests on.

    ``frappe.db.get_single_value`` casts Int/Check through ``cint()``, so a field
    with no row in ``tabSingles`` reads back as ``0`` — identical to an operator
    who deliberately set 0. Reading through it made ``auto_consume`` and
    ``alerts_enabled`` evaluate False on a site nobody had configured, and the
    whole feature went live dark while every endpoint answered 200.

    These tests exercise the REAL reader against a mocked DB rather than
    patching ``_single_value`` out, which is exactly how the original suite
    missed this.
    """

    def test_no_row_means_never_written(self):
        with patch.object(ls.frappe.db, "sql", return_value=[]):
            self.assertIsNone(ls._single_value("label_alerts_enabled"))

    def test_a_stored_zero_is_returned_not_swallowed(self):
        with patch.object(ls.frappe.db, "sql", return_value=[("0",)]):
            self.assertEqual(ls._single_value("label_alerts_enabled"), "0")

    def test_a_stored_value_is_returned_verbatim(self):
        with patch.object(ls.frappe.db, "sql", return_value=[("7",)]):
            self.assertEqual(ls._single_value("label_reorder_buffer_days"), "7")

    def test_a_null_column_reads_as_never_written(self):
        with patch.object(ls.frappe.db, "sql", return_value=[(None,)]):
            self.assertIsNone(ls._single_value("label_print_rest_day"))

    def test_a_db_failure_degrades_to_the_defaults(self):
        with patch.object(ls.frappe.db, "sql", side_effect=RuntimeError("no table")):
            self.assertIsNone(ls._single_value("label_alerts_enabled"))

    def test_an_unconfigured_site_gets_the_feature_switched_ON(self):
        # The exact regression: empty tabSingles must not read as "all zeroes".
        with patch.object(ls.frappe.db, "sql", return_value=[]):
            settings = ls.get_label_settings()
        self.assertTrue(settings["auto_consume"])
        self.assertTrue(settings["alerts_enabled"])
        self.assertEqual(settings["buffer_days"], 3)
        self.assertEqual(settings["lead_days_min"], 2)
        self.assertEqual(settings["lead_days_max"], 3)
        self.assertEqual(settings["rest_day"], "Friday")

    def test_an_operator_zero_still_switches_it_off(self):
        # The other half: a real stored 0 must survive, or the "0 is a decision"
        # doctrine is broken in the opposite direction.
        def stored(query, params):
            return [("0",)] if params[1] == "label_alerts_enabled" else []

        with patch.object(ls.frappe.db, "sql", side_effect=stored):
            settings = ls.get_label_settings()
        self.assertFalse(settings["alerts_enabled"])
        self.assertTrue(settings["auto_consume"])  # untouched, still defaults on

    def test_the_casting_reader_is_never_used(self):
        # Guards against a future "simplification" back to get_single_value,
        # which would silently reintroduce the dark-launch bug.
        with patch.object(ls.frappe.db, "sql", return_value=[]), patch.object(
            ls.frappe.db, "get_single_value"
        ) as casting_reader:
            ls.get_label_settings()
        casting_reader.assert_not_called()


if __name__ == "__main__":
    unittest.main()
