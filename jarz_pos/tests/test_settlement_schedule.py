"""Settlement schedule rules (services/settlement_schedule.py) -- pure, no site.

The whole rule book for B2B settlement terms is pure arithmetic over dates, so
it is pinned here without a database:

* weekly on a day, fortnightly counted from an anchor (including an anchor that
  is not itself a due weekday);
* days of the month with ``last``, the 31st clamping onto 30-day months and
  February (leap and non-leap);
* every N days from an anchor (nothing before the anchor);
* the status buckets -- overdue / due_today / due_soon / ok / none /
  unscheduled -- including the load-bearing rule that an invoice is due at the
  first schedule date STRICTLY AFTER its posting date;
* Invoice after Invoice (every open invoice except the newest is due) and On
  Delivery (everything open is overdue);
* the reminder decision (never twice a day, overdue repeat cadence, due-soon
  exactly N days ahead) and the collections list order;
* storage validation (the strict normaliser the API and the DocType share).

Calendar anchors used throughout: 2026-09-24 is a THURSDAY, 2026-09-25 a Friday.

Deliberately plain ``unittest.TestCase`` (see test_task_board for why). The
module under test imports nothing from frappe; a stub is still registered when
frappe is absent so ``python -m pytest`` works outside a bench regardless of
what the package ``__init__`` pulls in.
"""

import datetime
import sys
import types
import unittest
from types import SimpleNamespace

try:  # pragma: no cover - depends on the environment
    import frappe  # noqa: F401
except ImportError:  # pragma: no cover
    _fake = types.ModuleType("frappe")
    _fake.whitelist = lambda *a, **k: (a[0] if a and callable(a[0]) else (lambda fn: fn))
    _fake._ = lambda message: message
    _fake.local = SimpleNamespace()
    _fake.session = SimpleNamespace(user="Guest")
    _fake.log_error = lambda *a, **k: None
    _fake.get_traceback = lambda *a, **k: ""
    sys.modules["frappe"] = _fake

from jarz_pos.services import settlement_schedule as ss

D = datetime.date
THU = D(2026, 9, 24)
FRI = D(2026, 9, 25)


def terms(**row):
    return ss.parse_terms(row)


def weekly(days="Thu", interval=1, anchor=None, remind=1, **extra):
    return terms(cycle="Weekly", weekdays=days, week_interval=interval, anchor_date=anchor,
                 remind_days_before=remind, **extra)


def monthly(days="15,last", remind=1, **extra):
    return terms(cycle="Days of Month", month_days=days, remind_days_before=remind, **extra)


def every(n=10, anchor="2026-09-01", remind=1, **extra):
    return terms(cycle="Every N Days", interval_days=n, anchor_date=anchor, remind_days_before=remind, **extra)


def inv(name, posting, amount):
    return {"name": name, "posting_date": posting, "outstanding_amount": amount}


# ─────────────────────────────────────────────────────────────────────────────
# Schedules
# ─────────────────────────────────────────────────────────────────────────────


class TestWeekly(unittest.TestCase):
    def test_calendar_anchor_sanity(self):
        self.assertEqual(THU.weekday(), 3)
        self.assertEqual(FRI.weekday(), 4)

    def test_every_thursday(self):
        t = weekly("Thu")
        self.assertEqual(
            ss.due_dates_between(t, D(2026, 9, 1), D(2026, 9, 30)),
            [D(2026, 9, 3), D(2026, 9, 10), D(2026, 9, 17), D(2026, 9, 24)],
        )
        self.assertEqual(ss.next_due_date(t, FRI), D(2026, 10, 1))
        self.assertEqual(ss.next_due_date(t, THU), THU, "today counts")
        self.assertEqual(ss.previous_due_date(t, THU), D(2026, 9, 17), "strictly before today")
        self.assertEqual(ss.previous_due_date(t, FRI), THU)

    def test_two_weekdays(self):
        t = weekly("Mon, thursday")
        self.assertEqual(t["weekdays"], [0, 3])
        self.assertEqual(
            ss.due_dates_between(t, D(2026, 9, 21), D(2026, 9, 27)),
            [D(2026, 9, 21), D(2026, 9, 24)],
        )
        self.assertEqual(ss.describe(t), "Every Monday and Thursday")

    def test_fortnightly_from_anchor(self):
        t = weekly("Thu", interval=2, anchor="2026-09-03")
        self.assertEqual(
            ss.due_dates_between(t, D(2026, 9, 1), D(2026, 10, 31)),
            [D(2026, 9, 3), D(2026, 9, 17), D(2026, 10, 1), D(2026, 10, 15), D(2026, 10, 29)],
        )
        self.assertEqual(ss.next_due_date(t, D(2026, 9, 18)), D(2026, 10, 1))
        self.assertEqual(ss.previous_due_date(t, D(2026, 9, 30)), D(2026, 9, 17))
        # The grid extends backwards too (a debt older than the terms record).
        self.assertEqual(ss.previous_due_date(t, D(2026, 9, 3)), D(2026, 8, 20))

    def test_fortnight_counts_the_anchor_week_not_the_anchor_day(self):
        # Anchor on a Monday: that WEEK's Thursday is on the grid.
        t = weekly("Thu", interval=2, anchor="2026-09-07")
        self.assertEqual(
            ss.due_dates_between(t, D(2026, 9, 7), D(2026, 9, 30)),
            [D(2026, 9, 10), D(2026, 9, 24)],
        )

    def test_describe(self):
        self.assertEqual(ss.describe(weekly("Thu")), "Every Thursday")
        self.assertEqual(ss.describe(weekly("Thu", interval=2, anchor="2026-09-03")), "Every 2 weeks on Thursday")
        self.assertEqual(ss.describe(weekly("Mon,Wed,Fri")), "Every Monday, Wednesday and Friday")
        self.assertTrue(ss.describe(weekly("Thu"), "ar"))

    def test_no_weekdays_means_no_schedule(self):
        t = weekly("")
        self.assertFalse(ss.has_schedule(t))
        self.assertIsNone(ss.next_due_date(t, FRI))
        self.assertEqual(ss.due_dates_between(t, D(2026, 9, 1), D(2026, 9, 30)), [])


class TestDaysOfMonth(unittest.TestCase):
    def test_fifteenth_and_last_in_september(self):
        t = monthly("15,last")
        self.assertEqual(ss.due_dates_between(t, D(2026, 9, 1), D(2026, 9, 30)), [D(2026, 9, 15), D(2026, 9, 30)])
        self.assertEqual(ss.next_due_date(t, D(2026, 9, 16)), D(2026, 9, 30))
        self.assertEqual(ss.next_due_date(t, D(2026, 9, 30)), D(2026, 9, 30))
        self.assertEqual(ss.next_due_date(t, D(2026, 10, 1)), D(2026, 10, 15))
        self.assertEqual(ss.previous_due_date(t, D(2026, 10, 1)), D(2026, 9, 30))
        self.assertEqual(ss.previous_due_date(t, D(2026, 9, 15)), D(2026, 8, 31))

    def test_february_non_leap_and_leap(self):
        t = monthly("15,last")
        self.assertEqual(ss.due_dates_between(t, D(2026, 2, 1), D(2026, 2, 28)), [D(2026, 2, 15), D(2026, 2, 28)])
        self.assertEqual(ss.due_dates_between(t, D(2028, 2, 1), D(2028, 2, 29)), [D(2028, 2, 15), D(2028, 2, 29)])

    def test_day_past_month_end_clamps(self):
        t = monthly("31")
        self.assertEqual(ss.due_dates_between(t, D(2026, 4, 1), D(2026, 4, 30)), [D(2026, 4, 30)])
        self.assertEqual(ss.due_dates_between(t, D(2026, 2, 1), D(2026, 2, 28)), [D(2026, 2, 28)])
        self.assertEqual(ss.due_dates_between(t, D(2026, 5, 1), D(2026, 5, 31)), [D(2026, 5, 31)])
        t30 = monthly("30")
        self.assertEqual(ss.due_dates_between(t30, D(2028, 2, 1), D(2028, 2, 29)), [D(2028, 2, 29)])

    def test_31_and_last_do_not_double_count(self):
        t = monthly("31,last")
        self.assertEqual(ss.due_dates_between(t, D(2026, 4, 1), D(2026, 4, 30)), [D(2026, 4, 30)])

    def test_describe(self):
        self.assertEqual(ss.describe(monthly("15,last")), "On the 15th and the last day of each month")
        self.assertEqual(ss.describe(monthly("1,15")), "On the 1st and 15th of each month")
        self.assertEqual(ss.describe(monthly("last")), "On the last day of each month")
        self.assertEqual(ss.describe(monthly("2,3,22")), "On the 2nd, 3rd and 22nd of each month")
        self.assertEqual(ss.describe(monthly("11,12,13")), "On the 11th, 12th and 13th of each month")


class TestEveryNDays(unittest.TestCase):
    def test_from_anchor(self):
        t = every(10, "2026-09-01")
        self.assertEqual(
            ss.due_dates_between(t, D(2026, 8, 25), D(2026, 9, 30)),
            [D(2026, 9, 1), D(2026, 9, 11), D(2026, 9, 21)],
        )
        self.assertEqual(ss.next_due_date(t, D(2026, 9, 12)), D(2026, 9, 21))
        self.assertEqual(ss.next_due_date(t, D(2026, 9, 21)), D(2026, 9, 21))
        self.assertEqual(ss.next_due_date(t, D(2026, 8, 20)), D(2026, 9, 1), "nothing before the anchor")
        self.assertEqual(ss.previous_due_date(t, D(2026, 9, 21)), D(2026, 9, 11))
        self.assertEqual(ss.previous_due_date(t, D(2026, 9, 2)), D(2026, 9, 1))
        self.assertIsNone(ss.previous_due_date(t, D(2026, 9, 1)))

    def test_describe(self):
        self.assertEqual(ss.describe(every(10)), "Every 10 days")
        self.assertEqual(ss.describe(every(1)), "Every day")

    def test_anchor_defaults_to_creation(self):
        t = terms(cycle="Every N Days", interval_days=7, creation="2026-09-03 10:00:00")
        self.assertEqual(t["anchor_date"], D(2026, 9, 3))
        self.assertEqual(ss.next_due_date(t, D(2026, 9, 4)), D(2026, 9, 10))


class TestNonDateCycles(unittest.TestCase):
    def test_no_dates(self):
        for cycle in (ss.CYCLE_ON_DELIVERY, ss.CYCLE_INVOICE_AFTER_INVOICE):
            t = terms(cycle=cycle)
            self.assertIsNone(ss.next_due_date(t, FRI))
            self.assertIsNone(ss.previous_due_date(t, FRI))
            self.assertEqual(ss.due_dates_between(t, D(2026, 9, 1), D(2026, 9, 30)), [])

    def test_describe(self):
        self.assertEqual(ss.describe(terms(cycle="On Delivery")), "Pays on delivery")
        self.assertEqual(
            ss.describe(terms(cycle="Invoice after Invoice")),
            "Pays the previous invoice on each delivery",
        )


# ─────────────────────────────────────────────────────────────────────────────
# Status
# ─────────────────────────────────────────────────────────────────────────────


class TestStatusDateCycles(unittest.TestCase):
    def test_overdue_with_next_due(self):
        t = weekly("Thu")
        status = ss.compute_status(
            t,
            [
                inv("A", "2026-09-10", 100),  # posted Thu -> due NEXT Thu 17th (not the same day)
                inv("B", "2026-09-20", 50),   # -> due Thu 24th
                inv("C", "2026-09-24", 70),   # posted Thu 24th -> due Thu Oct 1
            ],
            FRI,
        )
        self.assertEqual(status["state"], ss.STATE_OVERDUE)
        self.assertEqual(status["overdue_amount"], 150.0)
        self.assertEqual(status["due_now_amount"], 150.0)
        self.assertEqual(status["next_due_date"], "2026-10-01")
        self.assertEqual(status["next_due_amount"], 70.0)
        self.assertEqual(status["open_balance"], 220.0)
        self.assertEqual(status["oldest_overdue_date"], "2026-09-17")
        self.assertEqual(status["upcoming_dates"], ["2026-10-01", "2026-10-08", "2026-10-15"])
        by_name = {i["name"]: i for i in status["invoices"]}
        self.assertEqual(by_name["A"]["due_date"], "2026-09-17")
        self.assertTrue(by_name["A"]["overdue"])
        self.assertFalse(by_name["C"]["overdue"])

    def test_invoice_posted_on_a_due_day_is_due_next_time(self):
        status = ss.compute_status(weekly("Thu"), [inv("C", "2026-09-24", 70)], THU)
        self.assertEqual(status["state"], ss.STATE_OK)
        self.assertEqual(status["invoices"][0]["due_date"], "2026-10-01")
        # Today is a payment day but nothing is owed on it: the next COLLECTION
        # is next week's, with its amount -- not today with 0.
        self.assertEqual(status["next_due_date"], "2026-10-01")
        self.assertEqual(status["next_due_amount"], 70.0)
        self.assertEqual(status["upcoming_dates"][0], "2026-09-24", "the schedule itself still lists today")

    def test_due_today(self):
        status = ss.compute_status(
            weekly("Thu"), [inv("A", "2026-09-18", 80), inv("B", "2026-09-24", 30)], THU
        )
        self.assertEqual(status["state"], ss.STATE_DUE_TODAY)
        self.assertEqual(status["due_now_amount"], 80.0)
        self.assertEqual(status["overdue_amount"], 0.0)
        self.assertEqual(status["next_due_date"], "2026-09-24")
        self.assertEqual(status["next_due_amount"], 80.0)

    def test_due_soon_uses_remind_days_before(self):
        wed = D(2026, 9, 23)
        invoices = [inv("A", "2026-09-18", 80)]  # due Thu 24th
        self.assertEqual(ss.compute_status(weekly("Thu", remind=1), invoices, wed)["state"], ss.STATE_DUE_SOON)
        self.assertEqual(ss.compute_status(weekly("Thu", remind=0), invoices, wed)["state"], ss.STATE_OK)
        # The collections list may widen the window.
        self.assertEqual(
            ss.compute_status(weekly("Thu", remind=0), invoices, wed, soon_days=7)["state"],
            ss.STATE_DUE_SOON,
        )

    def test_ok_when_next_due_is_far(self):
        status = ss.compute_status(weekly("Thu", remind=1), [inv("A", "2026-09-18", 80)], D(2026, 9, 21))
        self.assertEqual(status["state"], ss.STATE_OK)
        self.assertEqual(status["next_due_amount"], 80.0)

    def test_none_without_balance(self):
        status = ss.compute_status(weekly("Thu"), [inv("A", "2026-09-10", 0), inv("B", "2026-09-10", 0.001)], FRI)
        self.assertEqual(status["state"], ss.STATE_NONE)
        self.assertEqual(status["open_balance"], 0.0)
        self.assertEqual(status["next_due_date"], "2026-10-01")
        self.assertEqual(status["invoice_count"], 0)

    def test_fifteenth_and_last(self):
        t = monthly("15,last")
        status = ss.compute_status(
            t, [inv("A", "2026-09-01", 100), inv("B", "2026-09-15", 40)], D(2026, 9, 20)
        )
        # A -> due 15th (overdue), B posted ON the 15th -> due the 30th.
        self.assertEqual(status["state"], ss.STATE_OVERDUE)
        self.assertEqual(status["overdue_amount"], 100.0)
        self.assertEqual(status["next_due_date"], "2026-09-30")
        self.assertEqual(status["next_due_amount"], 40.0)

    def test_every_n_days(self):
        status = ss.compute_status(every(10, "2026-09-01"), [inv("A", "2026-09-05", 60)], D(2026, 9, 11))
        self.assertEqual(status["state"], ss.STATE_DUE_TODAY)
        self.assertEqual(status["due_now_amount"], 60.0)


class TestStatusInvoiceAfterInvoice(unittest.TestCase):
    def setUp(self):
        self.t = terms(cycle="Invoice after Invoice")

    def test_everything_but_the_newest_is_due(self):
        status = ss.compute_status(
            self.t,
            [inv("A", "2026-09-20", 100), inv("B", "2026-09-22", 60), inv("C", "2026-09-24", 40)],
            FRI,
        )
        self.assertEqual(status["state"], ss.STATE_OVERDUE)
        self.assertEqual(status["due_now_amount"], 160.0)
        self.assertEqual(status["overdue_amount"], 160.0)
        self.assertEqual(status["collect_on_next_delivery"], 40.0)
        self.assertIsNone(status["next_due_date"])
        self.assertEqual(status["oldest_overdue_date"], "2026-09-24")
        self.assertEqual(status["upcoming_dates"], [])

    def test_same_day_invoices_are_due_not_overdue(self):
        status = ss.compute_status(self.t, [inv("A", "2026-09-24", 100), inv("B", "2026-09-24", 40)], FRI)
        self.assertEqual(status["state"], ss.STATE_DUE_TODAY)
        self.assertEqual(status["due_now_amount"], 100.0)
        self.assertEqual(status["overdue_amount"], 0.0)
        self.assertEqual(status["collect_on_next_delivery"], 40.0)

    def test_single_open_invoice_is_ok(self):
        status = ss.compute_status(self.t, [inv("A", "2026-09-24", 40)], FRI)
        self.assertEqual(status["state"], ss.STATE_OK)
        self.assertEqual(status["due_now_amount"], 0.0)
        self.assertEqual(status["collect_on_next_delivery"], 40.0)

    def test_input_order_does_not_matter(self):
        status = ss.compute_status(self.t, [inv("C", "2026-09-24", 40), inv("A", "2026-09-20", 100)], FRI)
        self.assertEqual(status["collect_on_next_delivery"], 40.0)
        self.assertEqual(status["overdue_amount"], 100.0)


class TestStatusOnDeliveryAndUnscheduled(unittest.TestCase):
    def test_on_delivery_everything_open_is_overdue(self):
        status = ss.compute_status(terms(cycle="On Delivery"), [inv("A", "2026-09-20", 50), inv("B", "2026-09-22", 25)], FRI)
        self.assertEqual(status["state"], ss.STATE_OVERDUE)
        self.assertEqual(status["overdue_amount"], 75.0)
        self.assertEqual(status["due_now_amount"], 75.0)
        self.assertEqual(status["oldest_overdue_date"], "2026-09-20")

    def test_on_delivery_nothing_open(self):
        self.assertEqual(ss.compute_status(terms(cycle="On Delivery"), [], FRI)["state"], ss.STATE_NONE)

    def test_no_terms(self):
        self.assertEqual(ss.compute_status(None, [inv("A", "2026-09-20", 50)], FRI)["state"], ss.STATE_UNSCHEDULED)
        self.assertEqual(ss.compute_status(None, [], FRI)["state"], ss.STATE_NONE)

    def test_weekly_without_days_is_unscheduled(self):
        self.assertEqual(
            ss.compute_status(weekly(""), [inv("A", "2026-09-20", 50)], FRI)["state"], ss.STATE_UNSCHEDULED
        )


# ─────────────────────────────────────────────────────────────────────────────
# Reminder decision
# ─────────────────────────────────────────────────────────────────────────────


class TestPlanReminder(unittest.TestCase):
    def overdue_status(self):
        return ss.compute_status(weekly("Thu"), [inv("A", "2026-09-10", 100)], FRI)

    def test_first_overdue_goes_out(self):
        self.assertEqual(ss.plan_reminder(weekly("Thu"), self.overdue_status(), FRI), ss.KIND_OVERDUE)

    def test_never_twice_a_day(self):
        t = weekly("Thu", last_reminder_on="2026-09-25", last_reminder_kind="due_soon")
        self.assertIsNone(ss.plan_reminder(t, self.overdue_status(), FRI))

    def test_overdue_repeat_cadence(self):
        yesterday = weekly("Thu", overdue_repeat_days=2, last_reminder_on="2026-09-24", last_reminder_kind="overdue")
        self.assertIsNone(ss.plan_reminder(yesterday, self.overdue_status(), FRI))
        two_days = weekly("Thu", overdue_repeat_days=2, last_reminder_on="2026-09-23", last_reminder_kind="overdue")
        self.assertEqual(ss.plan_reminder(two_days, self.overdue_status(), FRI), ss.KIND_OVERDUE)
        after_other_kind = weekly("Thu", overdue_repeat_days=5, last_reminder_on="2026-09-24", last_reminder_kind="due_today")
        self.assertEqual(ss.plan_reminder(after_other_kind, self.overdue_status(), FRI), ss.KIND_OVERDUE)

    def test_due_today(self):
        t = weekly("Thu")
        status = ss.compute_status(t, [inv("A", "2026-09-18", 80)], THU)
        self.assertEqual(ss.plan_reminder(t, status, THU), ss.KIND_DUE_TODAY)

    def test_due_soon_exactly_n_days_ahead(self):
        invoices = [inv("A", "2026-09-18", 80)]  # due Thu 24th
        t1 = weekly("Thu", remind=1)
        self.assertEqual(ss.plan_reminder(t1, ss.compute_status(t1, invoices, D(2026, 9, 23)), D(2026, 9, 23)), ss.KIND_DUE_SOON)
        t2 = weekly("Thu", remind=2)
        self.assertIsNone(ss.plan_reminder(t2, ss.compute_status(t2, invoices, D(2026, 9, 23)), D(2026, 9, 23)))
        self.assertEqual(ss.plan_reminder(t2, ss.compute_status(t2, invoices, D(2026, 9, 22)), D(2026, 9, 22)), ss.KIND_DUE_SOON)
        t0 = weekly("Thu", remind=0)
        self.assertIsNone(ss.plan_reminder(t0, ss.compute_status(t0, invoices, D(2026, 9, 23)), D(2026, 9, 23)))

    def test_disabled_or_nothing_owed(self):
        off = weekly("Thu", enabled=0)
        self.assertIsNone(ss.plan_reminder(off, self.overdue_status(), FRI))
        t = weekly("Thu")
        self.assertIsNone(ss.plan_reminder(t, ss.compute_status(t, [], FRI), FRI))

    def test_invoice_after_invoice_due_today_repeats_on_cadence(self):
        t = terms(cycle="Invoice after Invoice", overdue_repeat_days=3,
                  last_reminder_on="2026-09-24", last_reminder_kind="due_today")
        status = ss.compute_status(t, [inv("A", "2026-09-24", 100), inv("B", "2026-09-24", 40)], FRI)
        self.assertIsNone(ss.plan_reminder(t, status, FRI))

    def test_amounts_and_dates(self):
        status = self.overdue_status()
        self.assertEqual(ss.reminder_amount(ss.KIND_OVERDUE, status), 100.0)
        self.assertEqual(ss.reminder_due_date(ss.KIND_OVERDUE, status, FRI), "2026-09-17")
        self.assertEqual(ss.todo_date(status, FRI), "2026-09-17")
        ok = ss.compute_status(weekly("Thu"), [inv("A", "2026-09-24", 1)], FRI)
        self.assertIsNone(ss.todo_date(ok, FRI))
        title, body = ss.reminder_text(ss.KIND_OVERDUE, "Cafe X", 100.0, "EGP", "2026-09-17", "Every Thursday")
        self.assertIn("Cafe X", title)
        self.assertIn("100.00 EGP", title)
        self.assertIn("2026-09-17", body)


# ─────────────────────────────────────────────────────────────────────────────
# Collections list
# ─────────────────────────────────────────────────────────────────────────────


def collection_entries():
    return [
        {"customer": "ZED", "customer_name": "Zed", "terms": None, "invoices": [inv("z", "2026-09-01", 500)]},
        {"customer": "ALPHA", "customer_name": "Alpha", "terms": weekly("Thu", remind=1),
         "invoices": [inv("a", "2026-09-24", 90)]},  # due Oct 1 -> ok
        {"customer": "BETA", "customer_name": "Beta", "terms": weekly("Thu"),
         "invoices": [inv("b", "2026-09-10", 10)]},  # overdue since Sep 17
        {"customer": "GAMMA", "customer_name": "Gamma", "terms": monthly("20"),
         "invoices": [inv("g", "2026-09-01", 999)]},  # overdue since Sep 20
        {"customer": "DELTA", "customer_name": "Delta", "terms": weekly("Thu"),
         "invoices": [inv("d", "2026-09-18", 30)]},  # due today
        {"customer": "EPS", "customer_name": "Eps", "terms": weekly("Fri", remind=1),
         "invoices": [inv("e", "2026-09-20", 20)]},  # due Fri Sep 25 -> soon
        {"customer": "NIL", "customer_name": "Nil", "terms": weekly("Thu"), "invoices": []},
    ]


class TestCollectionRows(unittest.TestCase):
    def test_order_and_counts(self):
        rows, counts = ss.build_collection_rows(collection_entries(), THU)
        self.assertEqual([r["customer"] for r in rows], ["BETA", "GAMMA", "DELTA", "EPS", "ZED", "ALPHA"])
        self.assertEqual(
            [r["state"] for r in rows],
            ["overdue", "overdue", "due_today", "due_soon", "unscheduled", "ok"],
        )
        self.assertEqual(counts, {"overdue": 2, "due_today": 1, "due_soon": 1, "unscheduled": 1})
        self.assertNotIn("NIL", [r["customer"] for r in rows], "state none is never listed")

    def test_row_keys(self):
        rows, _ = ss.build_collection_rows(collection_entries(), THU)
        for key in ("customer", "customer_name", "cycle", "description", "state", "next_due_date",
                    "next_due_amount", "due_now_amount", "overdue_amount", "open_balance", "responsible_user"):
            self.assertIn(key, rows[0])
        zed = [r for r in rows if r["customer"] == "ZED"][0]
        self.assertIsNone(zed["cycle"])
        self.assertIsNone(zed["description"])

    def test_soon_days_widens_due_soon(self):
        rows, counts = ss.build_collection_rows(collection_entries(), THU, soon_days=7)
        self.assertEqual([r["customer"] for r in rows], ["BETA", "GAMMA", "DELTA", "EPS", "ALPHA", "ZED"])
        self.assertEqual(counts["due_soon"], 2)

    def test_needs_attention(self):
        self.assertTrue(ss.needs_attention("overdue"))
        self.assertTrue(ss.needs_attention("due_today"))
        for state in ("due_soon", "ok", "unscheduled", "none"):
            self.assertFalse(ss.needs_attention(state))


# ─────────────────────────────────────────────────────────────────────────────
# Storage validation (shared by the API and the DocType)
# ─────────────────────────────────────────────────────────────────────────────


class TestNormalizeTermsInput(unittest.TestCase):
    def n(self, **values):
        return ss.normalize_terms_input(values)

    def bad(self, **values):
        with self.assertRaises(ss.SettlementTermsError):
            ss.normalize_terms_input(values)

    def test_weekly(self):
        out = self.n(cycle="weekly", weekdays="thu, Mon", month_days="15")
        self.assertEqual(out["cycle"], "Weekly")
        self.assertEqual(out["weekdays"], "Mon,Thu")
        self.assertEqual(out["week_interval"], 1)
        self.assertIsNone(out["month_days"], "fields of other cycles are cleared")
        self.assertEqual(self.n(cycle="Weekly", weekdays='["Thu"]')["weekdays"], "Thu")
        self.assertEqual(self.n(cycle="Weekly", weekdays=["Thursday"], week_interval="2")["week_interval"], 2)

    def test_weekly_refusals(self):
        self.bad(cycle="Weekly", weekdays="Thx")
        self.bad(cycle="Weekly", weekdays="")
        self.bad(cycle="Weekly", weekdays="Thu", week_interval=0)
        self.bad(cycle="Weekly", weekdays="Thu", week_interval="1.5")

    def test_days_of_month(self):
        self.assertEqual(self.n(cycle="Days of Month", month_days="15,last")["month_days"], "15,last")
        self.assertEqual(self.n(cycle="Days of Month", month_days=["last", 15, "1"])["month_days"], "1,15,last")
        self.assertEqual(self.n(cycle="Days of Month", month_days='["last"]')["month_days"], "last")
        self.bad(cycle="Days of Month", month_days="32")
        self.bad(cycle="Days of Month", month_days="0")
        self.bad(cycle="Days of Month", month_days="abc")
        self.bad(cycle="Days of Month", month_days="")

    def test_every_n_days(self):
        out = self.n(cycle="Every N Days", interval_days="10", anchor_date="2026-09-01")
        self.assertEqual(out["interval_days"], 10)
        self.assertEqual(out["anchor_date"], D(2026, 9, 1))
        self.assertTrue(ss.needs_anchor(out))
        self.bad(cycle="Every N Days")
        self.bad(cycle="Every N Days", interval_days=0)
        self.bad(cycle="Every N Days", interval_days=5, anchor_date="2026-13-01")

    def test_cycle_and_common_fields(self):
        self.bad(cycle="Monthly")
        self.bad(cycle="")
        out = self.n(cycle="On Delivery", weekdays="Thu", enabled="0", notes="  returns ok  ")
        self.assertEqual(out["enabled"], 0)
        self.assertIsNone(out["weekdays"])
        self.assertEqual(out["notes"], "returns ok")
        self.assertEqual(out["remind_days_before"], 1)
        self.assertEqual(out["overdue_repeat_days"], 2)
        self.assertFalse(ss.needs_anchor(out))
        self.assertEqual(self.n(cycle="Invoice after Invoice", remind_days_before=0)["remind_days_before"], 0)
        self.bad(cycle="On Delivery", remind_days_before=-1)
        self.bad(cycle="On Delivery", overdue_repeat_days=0)

    def test_needs_anchor_only_for_multi_week(self):
        self.assertFalse(ss.needs_anchor(self.n(cycle="Weekly", weekdays="Thu")))
        self.assertTrue(ss.needs_anchor(self.n(cycle="Weekly", weekdays="Thu", week_interval=2)))

    def test_parse_terms_is_lenient(self):
        t = ss.parse_terms({"cycle": "Weekly", "weekdays": "Thu,Nope", "week_interval": "x",
                            "remind_days_before": None, "overdue_repeat_days": 0, "anchor_date": "bad"})
        self.assertEqual(t["weekdays"], [3])
        self.assertEqual(t["week_interval"], 1)
        self.assertEqual(t["remind_days_before"], 1)
        self.assertEqual(t["overdue_repeat_days"], 1)
        self.assertIsNone(t["anchor_date"])
        self.assertIsNone(ss.parse_terms(None))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
