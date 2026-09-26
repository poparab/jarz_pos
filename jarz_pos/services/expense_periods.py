"""Split a bill that covers a stretch of days across the months it covers.

Meta charges paid ads in arrears: the charge on 5 October pays for 25 September
to 5 October. Booked whole on the 5th, six days of September's advertising land
in October's profit. The accounting fix is an accrual, and this module is the
arithmetic behind it -- pure, so it is tested without a site.

The rule, per payment:

* the amount is spread evenly over every day of ``period_from..period_to``;
* each month BEFORE the payment's month gets its days' share, booked on that
  month's last day against Accrued Expenses;
* the payment's own month keeps the rest, posted with the payment, which also
  clears the accrual.

Rounding never loses a piastre: every share is rounded, and the last month
takes whatever the rounding left over, so the shares always sum to the amount.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from typing import List

#: A typo in the year (2025 for 2026) would otherwise book a year of ads into
#: the wrong accounting year without anyone noticing. A Meta billing cycle is
#: days to weeks; a year is already generous.
MAX_PERIOD_DAYS = 366


@dataclass(frozen=True)
class MonthShare:
    month: str  # "YYYY-MM"
    days: int
    amount: float
    month_end: date


class PeriodError(ValueError):
    """The period cannot be booked. The message is safe to show the user."""


def validate_period(period_from: date, period_to: date, expense_date: date) -> None:
    if period_from > period_to:
        raise PeriodError("The period must start on or before the day it ends.")
    if period_to > expense_date:
        raise PeriodError(
            "The period cannot end after the expense date: a bill is paid after the days it covers."
        )
    if (period_to - period_from).days + 1 > MAX_PERIOD_DAYS:
        raise PeriodError(f"The period cannot be longer than {MAX_PERIOD_DAYS} days.")


def _month_end(day: date) -> date:
    return day.replace(day=calendar.monthrange(day.year, day.month)[1])


def split_by_month(amount: float, period_from: date, period_to: date, precision: int = 2) -> List[MonthShare]:
    """Each calendar month's share of *amount*, oldest first."""
    total_days = (period_to - period_from).days + 1
    if total_days <= 0:
        raise PeriodError("The period must start on or before the day it ends.")

    months: List[tuple] = []
    cursor = period_from
    while cursor <= period_to:
        end = min(_month_end(cursor), period_to)
        months.append((cursor.strftime("%Y-%m"), (end - cursor).days + 1, _month_end(cursor)))
        cursor = end + timedelta(days=1)

    shares: List[MonthShare] = []
    allocated = 0.0
    for index, (month, days, month_end) in enumerate(months):
        if index == len(months) - 1:
            share = round(amount - allocated, precision)
        else:
            share = round(amount * days / total_days, precision)
            allocated = round(allocated + share, precision)
        shares.append(MonthShare(month=month, days=days, amount=share, month_end=month_end))
    return shares


def earlier_month_shares(
    amount: float, period_from: date, period_to: date, expense_date: date, precision: int = 2
) -> List[MonthShare]:
    """The shares that belong to months before the payment's month.

    Empty when the whole period falls in the payment's month -- the ordinary
    case, which then posts exactly as an expense without a period does.
    """
    validate_period(period_from, period_to, expense_date)
    payment_month = expense_date.strftime("%Y-%m")
    return [s for s in split_by_month(amount, period_from, period_to, precision) if s.month < payment_month]
