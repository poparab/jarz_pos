"""Jarz Employee Penalty — a deduction agreed with an employee, in days or money.

WHAT THIS IS
------------
A penalty lowers what the company owes an employee for one salary month. It is
entered from the Monthly Expenses board either as *time* ("two days") or as
*money* ("300 EGP"), and this controller is the ONE place that converts between
the two. Both directions are always stored: a Money penalty still records
``equivalent_days`` and a Days penalty still records ``amount``, so no screen —
and no later report — has to re-derive one from the other and get a different
answer.

WHY IT POSTS NO JOURNAL ENTRY
-----------------------------
Salary is expensed when it is PAID (``Jarz Expense Request`` debits ``Salary -
J``), so paying less already books less expense. Posting a penalty to some
"penalty income" account as well would double-count it — once as a smaller
salary expense and once as income — and would leave two numbers to reconcile
for the same event. The penalty is therefore a *measurement*, not a posting:
``api/monthly_expenses`` subtracts it from ``due_amount`` and the GL follows
from the smaller payment.

WHY ``day_rate`` IS A SNAPSHOT
------------------------------
It is passed in by the caller (monthly salary / ``DAYS_PER_MONTH``) and is never
re-derived here. A raise three months later must not silently re-price a
penalty already agreed with the employee — the number they accepted has to stay
the number on the record. The flip side is that a Days penalty with no rate
cannot be priced at all, and that is refused rather than quietly stored as zero:
a penalty worth 0 EGP looks settled and deducts nothing.
"""

from __future__ import annotations

import re
from typing import Any, Optional, Tuple

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, getdate, today

# Kept in step with the ``naming_series`` field's options in the DocType JSON.
DEFAULT_NAMING_SERIES = "JPEN-.#####"

#: The fixed calendar basis for a day of salary (owner's decision, 2026-09-12):
#: monthly salary / 30, regardless of how many days the month actually has, so
#: the same absence costs the same in February and in August. Callers that
#: compute ``day_rate`` must use this constant rather than a literal.
DAYS_PER_MONTH = 30

UNIT_DAYS = "Days"
UNIT_HALF_DAYS = "Half Days"
UNIT_MONEY = "Money"

#: Same order as the ``unit`` Select options, and what the API publishes as
#: ``penalty_units`` so the app's segmented control cannot drift from the schema.
PENALTY_UNITS = (UNIT_DAYS, UNIT_HALF_DAYS, UNIT_MONEY)

#: Units whose money value is DERIVED from a day rate, so they cannot be
#: entered for an employee with no salary on file.
TIME_UNITS = (UNIT_DAYS, UNIT_HALF_DAYS)

#: ``period_month`` is a plain Data column, queried with ``=`` and indexed, so
#: the format is the contract. Anything else — "2026-9", "Sep 2026", a stray
#: space — silently matches no month and the penalty vanishes from the board it
#: was entered on.
PERIOD_MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

#: Money is rounded like a Currency column; days keep enough precision that a
#: half day (0.5) and a third of a day still survive a round trip.
AMOUNT_PRECISION = 2
DAYS_PRECISION = 4


# ── pure helpers (no document, no database) ───────────────────────────────


def is_valid_period_month(value: Any) -> bool:
    """True when ``value`` is a ``YYYY-MM`` key this app can query by."""
    return bool(PERIOD_MONTH_PATTERN.match(str(value or "").strip()))


def period_month_for_date(value: Any) -> str:
    """The ``YYYY-MM`` key a date falls in."""
    return getdate(value).strftime("%Y-%m")


def convert_penalty(
    unit: Optional[str],
    quantity: Any,
    amount: Any,
    day_rate: Any,
) -> Tuple[float, float]:
    """``(amount, equivalent_days)`` for one penalty — the single definition.

    Deliberately pure arithmetic: it never throws and never reads the database,
    so the API, the controller and the app's live preview can all agree by
    calling it rather than by each re-implementing it.

    A zero ``day_rate`` on a Money penalty yields ``equivalent_days = 0`` rather
    than a ZeroDivisionError. Refusing the *time* units without a rate is a
    separate decision and lives in :meth:`JarzEmployeePenalty.validate`; here,
    "we cannot express this in days" is honestly reported as 0.
    """
    qty = flt(quantity)
    rate = flt(day_rate)
    unit = str(unit or "").strip() or UNIT_DAYS

    if unit == UNIT_DAYS:
        return flt(qty * rate, AMOUNT_PRECISION), flt(qty, DAYS_PRECISION)

    if unit == UNIT_HALF_DAYS:
        return (
            flt(qty * rate / 2.0, AMOUNT_PRECISION),
            flt(qty / 2.0, DAYS_PRECISION),
        )

    money = flt(amount, AMOUNT_PRECISION)
    days = flt(money / rate, DAYS_PRECISION) if rate else 0.0
    return money, days


# ── controller ───────────────────────────────────────────────────────────


class JarzEmployeePenalty(Document):
    def before_insert(self):
        # The Desk form fills `naming_series` from its default client-side, but
        # API inserts and Data Import do not — leaving those paths to fail
        # autoname with "Naming Series mandatory". That exact shape cost this
        # workspace a debugging session on `Jarz Recurring Expense`, which is
        # also why the field is a Select with a default and NOT a read-only
        # Data field: a read-only Data naming_series is only ever populated by
        # the form.
        if not self.naming_series:
            self.naming_series = DEFAULT_NAMING_SERIES
        if not self.penalty_date:
            self.penalty_date = today()

    def validate(self):
        self._apply_defaults()
        self._validate_period_month()
        self._validate_reason()
        self._validate_unit_inputs()
        self._compute_amounts()
        self._validate_amount()

    def on_cancel(self):
        # `settled` means a salary payment has already been reduced by this
        # penalty and the money has moved. Cancelling here would leave the
        # employee short with nothing on record saying why, and the settling
        # payment untouched — so the reversal has to start from the payment.
        if int(self.settled or 0):
            frappe.throw(
                _(
                    "Penalty {0} has already been settled against a salary payment"
                    "{1}. Reverse that payment first."
                ).format(self.name, f" ({self.settled_via})" if self.settled_via else "")
            )

    # ── defaults ─────────────────────────────────────────────────────────

    def _apply_defaults(self) -> None:
        if not self.penalty_date:
            self.penalty_date = today()
        if not self.company:
            self.company = frappe.defaults.get_user_default("Company")
        if not self.currency:
            self.currency = (
                frappe.db.get_value("Company", self.company, "default_currency")
                if self.company
                else None
            ) or frappe.defaults.get_global_default("currency")
        # Only ever FILLS IN a blank. Forcing it from `penalty_date` would
        # re-file every carried-over incident under the month it happened in
        # and make "what was deducted from September" unanswerable.
        if not self.period_month and self.penalty_date:
            self.period_month = period_month_for_date(self.penalty_date)

    # ── validation ───────────────────────────────────────────────────────

    def _validate_period_month(self) -> None:
        self.period_month = str(self.period_month or "").strip()
        if not is_valid_period_month(self.period_month):
            frappe.throw(
                _(
                    "Period must be a month in YYYY-MM form (for example 2026-09); "
                    "got {0}."
                ).format(self.period_month or _("nothing"))
            )

    def _validate_reason(self) -> None:
        self.reason = str(self.reason or "").strip()
        if not self.reason:
            frappe.throw(_("A reason is required for every penalty."))

    def _validate_unit_inputs(self) -> None:
        unit = str(self.unit or "").strip() or UNIT_DAYS
        if unit not in PENALTY_UNITS:
            frappe.throw(
                _("Unit must be one of {0}.").format(", ".join(PENALTY_UNITS))
            )
        self.unit = unit

        if unit in TIME_UNITS:
            if flt(self.quantity) <= 0:
                frappe.throw(
                    _("Enter how many {0} the penalty is.").format(unit.lower())
                )
            # Refused, never defaulted to zero: `day_rate` is a snapshot the
            # caller must supply (monthly salary / 30). Pricing a penalty at 0
            # because nobody knew the salary produces a record that looks
            # applied and deducts nothing.
            if flt(self.day_rate) <= 0:
                frappe.throw(
                    _(
                        "No day rate is available for {0}, so a penalty in {1} "
                        "cannot be priced. Record it as an amount of money "
                        "instead, or set a salary for the employee first."
                    ).format(self.employee_name or self.employee or _("this employee"), unit.lower())
                )
        else:
            if flt(self.amount) <= 0:
                frappe.throw(_("Enter the penalty amount."))
            # A money penalty has no day count of its own; `equivalent_days` is
            # derived below. Leaving a stale quantity behind would contradict it.
            self.quantity = 0.0

    def _compute_amounts(self) -> None:
        amount, equivalent_days = convert_penalty(
            self.unit, self.quantity, self.amount, self.day_rate
        )
        self.amount = amount
        self.equivalent_days = equivalent_days
        self.day_rate = flt(self.day_rate, AMOUNT_PRECISION)

    def _validate_amount(self) -> None:
        if flt(self.amount) <= 0:
            frappe.throw(_("Penalty amount must be greater than zero."))


def on_doctype_update():
    # The payroll board reads every penalty for one month on every load, and the
    # per-employee row reads every penalty for one person. Both are plain
    # equality filters on unindexed Data/Link columns without these.
    frappe.db.add_index("Jarz Employee Penalty", ["period_month"])
    frappe.db.add_index("Jarz Employee Penalty", ["employee"])
