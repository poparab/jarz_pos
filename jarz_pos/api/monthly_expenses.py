"""Monthly Expenses API — the manager screen that PAYS the monthly bills.

`jarz_pos.api.recurring_expenses` answers "what should have been posted?" for the
ERPNext Desk page. It is read-only and is deliberately left untouched here: this
module imports its helpers rather than copying them, so the cadence rules, the
month arithmetic and the access gate can only ever have one definition.

What this module adds is the *money* half:

* what is DUE this month (registry + HRMS payroll),
* what has actually been PAID for that period,
* therefore what REMAINS,
* and the two endpoints that pay it.

A payment is a ``Jarz Expense Request``. Submitting one posts a two-line Journal
Entry (DEBIT the expense account / CREDIT the paying account) via the existing
``JarzExpenseRequest.on_submit``. There is no second posting path, and cancelling
a payment reverses that JE through ``on_cancel``.

── Why ``period_month`` exists ────────────────────────────────────────────────
``expense_month`` is when the money moved. ``period_month`` is the month being
paid FOR. August rent paid on 3 September is ``expense_month=2026-09`` and
``period_month=2026-08``. Every "paid" figure on this screen is keyed on
``period_month``; every GL comparison is keyed on ``expense_month``, because that
is when the Journal Entry actually hit the ledger. Confusing the two is how a
screen ends up reporting September's rent as paid when it was August's.
"""

from __future__ import annotations

import calendar
import json
import re
from datetime import date
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import frappe
from frappe import _
from frappe.utils import flt, getdate, now_datetime, today

from jarz_pos.api.employee_advances import _advance_has_field
from jarz_pos.api.expenses import (
    _account_label_map,
    _cashlike_accounts,
    _default_company,
    _indirect_expense_accounts,
    _is_manager,
    _month_label,
    _parse_filters,
    _serialize_expense,
    _serialize_payment_sources,
    cancel_expense,
)
from jarz_pos.api.recurring_expenses import (
    FREQUENCY_MONTHS,
    _ensure_manager,
    _gl_posted_by_account,
    _is_due_in_month,
    _load_payroll,
    _month_bounds,
    _payroll_expense_accounts,
)
from jarz_pos.utils.employee_link import (
    ADVANCE_DOCTYPE,
    EMPLOYEE_ORDER_PURPOSE,
    customers_for_employees,
    employee_display_names,
    hrms_available,
)
from jarz_pos.utils.posting_datetime import (
    apply_ledger_posting_datetime,
    join_posting_datetime,
    split_posting_datetime,
)

# The two jarz-owned settlement columns on ``Employee Advance``, seeded by
# ``jarz_pos.setup.employee_link_setup``. Imported, never re-spelled: the
# settlement writer here and the balance reader must agree on the column name,
# and a typo in one of them would silently stop recovering advances.
#
# Hard imports, deliberately. A deploy moves this whole app to one commit, so a
# bench carrying this module but not `utils.employee_link` or the penalty
# controller cannot exist — and a fallback that restates the fieldnames or the
# conversion here is a SECOND definition waiting to drift from the first.
from jarz_pos.utils.employee_link import F_SETTLED_AMOUNT, F_SETTLED_VIA
from jarz_pos.doctype.jarz_employee_penalty.jarz_employee_penalty import (
    DAYS_PER_MONTH,
    PENALTY_UNITS,
    convert_penalty,
)

# ── constants ─────────────────────────────────────────────────────────────

#: Two amounts closer than this are the same money. Every comparison in this
#: module goes through it — ``==`` on a float that survived a Currency column,
#: a division by a frequency and a JSON round-trip is a coin toss.
MONEY_TOLERANCE = 0.5

#: `available_months` = this many months back, plus the current one.
AVAILABLE_MONTHS_BACK = 12

#: A `day_of_month` above this does not exist in February. See
#: `_normalized_day_of_month` for why the API refuses rather than clamps.
MAX_DAY_OF_MONTH = 28

#: Fallback when the DocType meta cannot be read (kept in step with the JSON).
DEFAULT_CATEGORIES = [
    "Rent",
    "Utilities",
    "Telecom & Internet",
    "Software & Subscriptions",
    "Professional Services",
    "Marketing",
    "Maintenance",
    "Insurance",
    "Government & Licenses",
    "Contract Labor",
    "Other",
]

REGISTRY_STATUSES = ("Active", "Paused", "Ended")

PAYROLL_CATEGORY = "Salaries (HRMS)"

#: Penalties are recorded against this submittable jarz DocType.
PENALTY_DOCTYPE = "Jarz Employee Penalty"

#: ``PENALTY_UNITS`` and ``DAYS_PER_MONTH`` are imported from the DocType
#: controller above rather than restated here. The units mirror the ``unit``
#: Select options and the Flutter segmented control is built from the list this
#: endpoint publishes, so a third spelling of "Half Days" would put the app,
#: the API and the schema out of step with nothing to catch it.
#:
#: ``DAYS_PER_MONTH = 30`` is the owner's decision (2026-09-12): a day of salary
#: is a THIRTIETH of the monthly salary — a fixed calendar basis, not the days
#: in the month and not the days actually worked. A February penalty and an
#: August one must cost the same, or the same offence is priced differently by
#: the calendar.

#: The dedup/provenance tag on the settlement Journal Entry. Read back by
#: ``_load_settlements`` to credit a month with what it discharged in kind.
SETTLEMENT_JE_TAG = "SALARY_SETTLEMENT"

_SETTLEMENT_TAG_RE = re.compile(
    r"\[JARZ-JE:" + SETTLEMENT_JE_TAG + r":([^:\]]+):(\d{4}-\d{2})\]"
)

_PENALTY_FIELDS = [
    "name",
    "employee",
    "employee_name",
    "company",
    "penalty_date",
    "period_month",
    "unit",
    "quantity",
    "amount",
    "day_rate",
    "equivalent_days",
    "currency",
    "reason",
    "settled",
    "settled_via",
]

#: Columns read off ``Employee Advance``. Deliberately the same set
#: ``manager._employee_ledger_advance_rows`` reads, so one person's balance is
#: the same number on both screens.
_ADVANCE_FIELDS = [
    "name",
    "employee",
    "employee_name",
    "posting_date",
    "advance_amount",
    "paid_amount",
    "claimed_amount",
    "return_amount",
    "status",
    "purpose",
    "advance_account",
    "currency",
    "company",
]

_ORDER_FIELDS = [
    "name",
    "customer",
    "customer_name",
    "posting_date",
    "grand_total",
    "outstanding_amount",
    "status",
]

#: An ``advance_account`` whose ``account_name`` contains none of these is not
#: an employee-advance ledger. Production's three open advances all sit on
#: ``Debtors - J`` (customer AR), which is reported as a gap and never repointed
#: from here — moving a submitted advance's account rewrites posted GL.
_ADVANCE_ACCOUNT_HINTS = ("advance", "staff", "employee")

_REQUEST_FIELDS = [
    "name",
    "amount",
    "currency",
    "expense_date",
    "expense_month",
    "period_month",
    "expense_kind",
    "recurring_expense",
    "employee",
    "reason_account",
    "reason_label",
    "paying_account",
    "payment_source_label",
    "journal_entry",
    "remarks",
    "requested_by",
    "owner",
    "company",
]

_REGISTRY_FIELDS = [
    "name",
    "expense_name",
    "category",
    "status",
    "supplier",
    "amount",
    "currency",
    "frequency",
    "monthly_equivalent",
    "day_of_month",
    "expense_account",
    "cost_center",
    "default_paying_account",
    "start_date",
    "end_date",
    "auto_repeat",
    "notes",
]


def _log(title: str) -> None:
    """Best-effort error log. ``frappe.log_error`` can itself raise."""
    try:
        frappe.log_error(frappe.get_traceback(), title[:130])
    except Exception:
        pass


# ── schema readiness ──────────────────────────────────────────────────────

_PERIOD_COLUMNS = ("expense_kind", "recurring_expense", "employee", "period_month")


def _period_fields_ready() -> bool:
    cached = getattr(frappe.flags, "jarz_monthly_expense_fields", None)
    if cached is not None:
        return bool(cached)
    ready = True
    try:
        for column in _PERIOD_COLUMNS:
            if not frappe.db.has_column("Jarz Expense Request", column):
                ready = False
                break
    except Exception:
        _log("monthly_expenses: period field probe")
        ready = False
    frappe.flags.jarz_monthly_expense_fields = ready
    return ready


def _require_period_fields() -> None:
    """Fail loudly rather than reporting every bill as unpaid.

    If the DocType has not been migrated, ``period_month`` does not exist and
    every "paid" query would return nothing. Returning zeros there is worse than
    an error: the screen would confidently tell a manager to pay the rent twice.
    """
    if not _period_fields_ready():
        frappe.throw(
            _(
                "Monthly Expenses needs the Jarz Expense Request period fields "
                "(expense_kind, recurring_expense, employee, period_month). "
                "Run `bench migrate` on this site."
            )
        )


# ── small pure helpers ────────────────────────────────────────────────────


def _money(value: Any) -> float:
    return flt(flt(value), 2)


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _available_months(anchor_key: str, back: int = AVAILABLE_MONTHS_BACK) -> List[str]:
    """``back`` months before ``anchor_key`` plus ``anchor_key``, newest last."""
    year = int(str(anchor_key)[0:4])
    month = int(str(anchor_key)[5:7])
    base = year * 12 + (month - 1)
    keys: List[str] = []
    for offset in range(back, -1, -1):
        total = base - offset
        keys.append("{0:04d}-{1:02d}".format(total // 12, (total % 12) + 1))
    return keys


def _due_date_for_month(day_of_month: Any, anchor: date) -> Optional[str]:
    """The item's due date inside ``anchor``'s month, or ``None``.

    Clamped to the last day of the month for DISPLAY only: rows created before
    the 1–28 rule below may legitimately carry 29–31, and a stored 31 must still
    render something in February rather than crashing the screen.
    """
    if day_of_month in (None, ""):
        return None
    try:
        day = int(day_of_month)
    except (TypeError, ValueError):
        return None
    if day < 1:
        return None
    last = calendar.monthrange(anchor.year, anchor.month)[1]
    return date(anchor.year, anchor.month, min(day, last)).isoformat()


def _normalized_day_of_month(value: Any) -> Optional[int]:
    """Validate a caller-supplied due day, or raise ``ValueError``.

    **Rejects** 29–31 rather than clamping them. Clamping would store a day the
    registrar never typed, and the registry is a document accountants read back
    later — silently turning "the 31st" into "the 28th" is a change of fact made
    behind their back. Refusing forces them to state a day that exists in all
    twelve months, which is the only kind of answer this field can honour.
    """
    if value in (None, ""):
        return None
    try:
        day = int(value)
    except (TypeError, ValueError):
        raise ValueError(
            "Due day of month must be a whole number between 1 and {0}.".format(
                MAX_DAY_OF_MONTH
            )
        )
    if day < 1 or day > MAX_DAY_OF_MONTH:
        raise ValueError(
            "Due day of month must be between 1 and {0}. "
            "Days 29-31 do not exist in every month, so the due date they imply "
            "would move on its own in February.".format(MAX_DAY_OF_MONTH)
        )
    return day


def _monthly_equivalent(amount: Any, frequency: Optional[str]) -> float:
    """Normalise an occurrence amount to a monthly run-rate.

    Deliberately identical to ``JarzRecurringExpense._compute_monthly_equivalent``
    and driven by the same ``FREQUENCY_MONTHS`` map, so the API can never write a
    run-rate the DocType would disagree with.
    """
    months = FREQUENCY_MONTHS.get(frequency or "Monthly", 1)
    return flt(amount) / months


def _payment_status(
    due_amount: Any,
    paid_amount: Any,
    due_this_month: bool = True,
    tolerance: float = MONEY_TOLERANCE,
) -> str:
    """One of Not Due / Unpaid / Partial / Paid / Overpaid.

    Order matters: Overpaid and Paid are tested before Unpaid so that a rounding
    wobble around zero cannot present a fully paid line as unpaid.
    """
    due = flt(due_amount)
    paid = flt(paid_amount)
    if not due_this_month or due <= tolerance:
        return "Not Due"
    if paid > due + tolerance:
        return "Overpaid"
    if paid >= due - tolerance:
        return "Paid"
    if paid <= tolerance:
        return "Unpaid"
    return "Partial"


def _overpay_excess(
    due_amount: Any,
    already_paid: Any,
    requested: Any,
    tolerance: float = MONEY_TOLERANCE,
) -> float:
    """How far past ``due_amount`` this payment would push the period. 0 if none."""
    total = flt(already_paid) + flt(requested)
    excess = total - flt(due_amount)
    return excess if excess > tolerance else 0.0


def _guard_overpay(
    label: str,
    due_amount: Any,
    already_paid: Any,
    requested: Any,
    allow_overpay: bool,
    month_key: str,
    tolerance: float = MONEY_TOLERANCE,
) -> float:
    """Refuse an overpayment unless the caller explicitly asked for it.

    The message spells out due / already paid / requested because "overpayment"
    on its own gives the manager nothing to act on — the usual cause is an Auto
    Repeat Journal Entry submitted in Desk on top of an app payment, and they can
    only see that if the three numbers are in front of them.
    """
    excess = _overpay_excess(due_amount, already_paid, requested, tolerance)
    if not excess or allow_overpay:
        return excess
    frappe.throw(
        _(
            "{0} for {1} is already covered. Due {2}, already paid {3}, "
            "this payment adds {4} — that is {5} more than due. "
            "Re-send with allow_overpay=1 if this extra payment is intended."
        ).format(
            label,
            month_key,
            _money(due_amount),
            _money(already_paid),
            _money(requested),
            _money(excess),
        )
    )
    return excess  # pragma: no cover - frappe.throw does not return


# ── deductions: penalties, advances, employee orders ──────────────────────


def _day_rate(gross_due: Any, days_per_month: int = DAYS_PER_MONTH) -> float:
    """One day of this salary. 0 when there is no salary to divide.

    Off-payroll people (no Salary Structure Assignment) have a gross of 0, so a
    day-based penalty cannot be priced for them at all — ``add_employee_penalty``
    refuses and tells the caller to enter the money amount instead, rather than
    booking a 0 EGP penalty that reads as recorded and deducts nothing.
    """
    gross = flt(gross_due)
    if gross <= 0:
        return 0.0
    return flt(gross) / float(days_per_month or DAYS_PER_MONTH)


def _penalty_amounts(
    unit: Optional[str], quantity: Any, amount: Any, day_rate: Any
) -> Tuple[float, float]:
    """``(amount, equivalent_days)`` for one penalty. Pure.

    Delegates to the DocType controller's ``convert_penalty`` rather than
    repeating the arithmetic. The API needs the money value BEFORE the document
    exists — the month's penalty total is guarded against ``gross_due`` first —
    and the DocType needs it because a penalty entered in Desk must come out
    identical. Two callers, ONE formula: a second copy here would agree on the
    day it was written and drift the first time either rounding changed.

    Both directions are always produced: ``Money`` still reports how many days
    it cost, so an employee shown "500 EGP" can be told "that is 1.67 days".
    """
    return convert_penalty(unit, quantity, amount, day_rate)


def _serialize_penalty(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "name": row.get("name"),
        "penalty_date": row.get("penalty_date"),
        "period_month": row.get("period_month"),
        "unit": row.get("unit"),
        "quantity": flt(row.get("quantity")),
        "amount": _money(row.get("amount")),
        "equivalent_days": flt(row.get("equivalent_days")),
        "day_rate": _money(row.get("day_rate")),
        "reason": row.get("reason"),
        "settled": _as_bool(row.get("settled")),
        "settled_via": row.get("settled_via"),
    }


def _advance_open_amount(row: Dict[str, Any]) -> float:
    """What is still owed on one Employee Advance, floored at zero.

    ``paid_amount - claimed_amount - return_amount - custom_jarz_settled_amount``.

    ``claimed_amount`` is HRMS's own recovery (an Expense Claim consumed the
    advance) and ``return_amount`` is cash handed back; the jarz column is the
    third route, added because HRMS drives ``claimed_amount`` ONLY from Expense
    Claims — ``update_claimed_amount`` recomputes it from
    ``Expense Claim Advance``, so writing it from here is overwritten on the
    advance's next touch.

    Starts from ``paid_amount``, not ``advance_amount``: an approved-but-unpaid
    advance is a promise, not a debt, and deducting it from a salary would
    recover money that never left the company.
    """
    open_amount = (
        flt(row.get("paid_amount"))
        - flt(row.get("claimed_amount"))
        - flt(row.get("return_amount"))
        - flt(row.get(F_SETTLED_AMOUNT))
    )
    return open_amount if open_amount > 0 else 0.0


def _serialize_advance(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "name": row.get("name"),
        "posting_date": row.get("posting_date"),
        "amount": _money(row.get("advance_amount")),
        "paid_amount": _money(row.get("paid_amount")),
        "claimed_amount": _money(row.get("claimed_amount")),
        "return_amount": _money(row.get("return_amount")),
        "settled_amount": _money(row.get(F_SETTLED_AMOUNT)),
        "outstanding": _money(_advance_open_amount(row)),
        "purpose": row.get("purpose"),
        "status": row.get("status"),
        "advance_account": row.get("advance_account"),
        "currency": row.get("currency"),
    }


def _serialize_order(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "invoice": row.get("name"),
        "posting_date": row.get("posting_date"),
        "customer": row.get("customer"),
        "customer_name": row.get("customer_name"),
        "grand_total": _money(row.get("grand_total")),
        "outstanding": _money(row.get("outstanding_amount")),
        "status": row.get("status"),
    }


def _is_advance_ledger(account_name: Optional[str]) -> bool:
    """Does this account's name read as an employee-advance ledger?

    Name-based on purpose: ``root_type`` cannot tell the two apart (both are
    Receivable), and the thing that goes wrong in practice is an advance booked
    to plain customer AR, where it sits inside the same balance as real
    customers' debt and nobody ever notices.
    """
    text = str(account_name or "").strip().lower()
    if not text:
        return False
    return any(hint in text for hint in _ADVANCE_ACCOUNT_HINTS)


# ── attribution: turning GL money into per-item "paid" ────────────────────


def _unlinked_by_account(
    gl_by_account: Dict[str, Any],
    linked_posted_by_account: Optional[Dict[str, Any]] = None,
) -> Dict[str, float]:
    """Money on each account this month that no app payment explains.

    ── Both sides of this subtraction must be in the SAME scope ──────────────
    ``gl_by_account`` is the month's net debit per account: it is keyed on the
    POSTING month, because that is when the Journal Entry hit the ledger.
    Therefore the only thing that may be subtracted from it is the set of app
    payments whose Journal Entry ALSO landed in this month — that is
    ``linked_posted_by_account``, keyed on ``expense_month``, whatever period
    each payment settles.

    Subtracting period-scoped payments here is not a rounding difference, it is
    a category error, and it hides real money. August rent is posted from Desk
    by Auto Repeat (GL(Aug)=47,000) and then paid again through the app on
    3 September for period 2026-08. Viewing August, a period-scoped subtrahend
    cancels the 47,000 of Desk money that is genuinely sitting in August's
    ledger, so the item reads "Paid, remaining 0" while 94,000 has actually left
    the company for one month's rent. Keyed on the posting month instead, that
    September payment is simply not part of August's subtraction: August keeps
    its 47,000 of unexplained GL, the item is inferred up to 94,000 paid, and
    the screen says Overpaid — which is the truth.

    Only LINKED payments (Recurring with a ``recurring_expense``, Salary with an
    ``employee``) belong in the subtrahend. An ad-hoc request booked straight to
    a rent account is deliberately left in the unexplained pot so the single due
    item on that account still picks it up by inference.

    Clamped at zero: a month can hold app payments for other periods that exceed
    its own GL, and a negative "unexplained" amount is not a thing.
    """
    linked_posted_by_account = linked_posted_by_account or {}
    accounts = set(gl_by_account) | set(linked_posted_by_account)
    unlinked: Dict[str, float] = {}
    for account in accounts:
        residual = flt(gl_by_account.get(account)) - flt(
            linked_posted_by_account.get(account)
        )
        unlinked[account] = residual if residual > 0 else 0.0
    return unlinked


def _attribute_unlinked(
    rows: Sequence[Dict[str, Any]],
    unlinked_by_account: Dict[str, Any],
    tolerance: float = MONEY_TOLERANCE,
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, float]]:
    """Attach unexplained account money to an item only when it can only be that item.

    ``rows`` need ``name``, ``expense_account`` and ``due_this_month``.

    * Exactly one item due on the account → it gets the money, flagged
      ``inferred`` so the UI can say "matched by account, not by payment".
    * Several items due on the same account → nobody gets it. The amount is
      returned as an account-level leftover instead. Splitting it would put a
      number next to each item that no document supports, and a manager acting on
      that number pays the wrong landlord.
    * No item due → leftover as well.
    """
    due_by_account: Dict[str, List[str]] = {}
    attribution: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        name = row.get("name")
        attribution[name] = {
            "paid_unlinked": 0.0,
            "shared_account": False,
            "inferred": False,
        }
        account = row.get("expense_account")
        if account and row.get("due_this_month"):
            due_by_account.setdefault(account, []).append(name)

    leftovers: Dict[str, float] = {}
    accounts = set(unlinked_by_account) | set(due_by_account)
    for account in accounts:
        unlinked = flt(unlinked_by_account.get(account))
        due_names = due_by_account.get(account, [])
        if len(due_names) == 1:
            target = attribution[due_names[0]]
            target["paid_unlinked"] = unlinked
            target["inferred"] = unlinked > tolerance
            continue
        for name in due_names:
            attribution[name]["shared_account"] = True
        if unlinked > tolerance:
            leftovers[account] = unlinked
    return attribution, leftovers


def _payment_row(request: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "name": request.get("name"),
        "amount": _money(request.get("amount")),
        "date": request.get("expense_date"),
        "period_month": request.get("period_month"),
        "expense_month": request.get("expense_month"),
        "paying_account": request.get("paying_account"),
        "paying_label": request.get("payment_source_label") or request.get("paying_account"),
        "journal_entry": request.get("journal_entry"),
        "remarks": request.get("remarks"),
        "by": request.get("requested_by") or request.get("owner"),
    }


def _split_period_requests(
    requests: Sequence[Dict[str, Any]], month_key: str
) -> Dict[str, Any]:
    """Index submitted expense requests for one month.

    Two INDEPENDENT scopes come out of this, and keeping them independent is the
    whole point. A single request can land in both, in one, or in neither:

    * **Period scope** (``period_month == month_key``) — "what has this period
      been paid?". Feeds ``paid_linked_by_item`` / ``paid_linked_by_employee``
      and the payment lists the UI shows under each row. Wherever the Journal
      Entry landed is irrelevant here: the money settles THIS period.
    * **Posting scope** (``expense_month == month_key``) — "which of this
      month's GL movements did this app produce?". Feeds
      ``linked_posted_by_account`` / ``salary_linked_by_account``, which are the
      ONLY things ever subtracted from this month's GL (see
      ``_unlinked_by_account``). Which period they pay for is irrelevant here:
      the money moved THIS month.

    An August payment posted in September is in August's period scope and in
    September's posting scope — never in August's posting scope. Filling the
    account buckets from the period branch is what let a genuine double payment
    read as "Paid": the GL it cancelled was in a different month entirely.

    Only LINKED rows feed the account buckets — Recurring with a
    ``recurring_expense``, Salary with an ``employee``. An ad-hoc request booked
    straight to a rent account is intentionally left in the unexplained pot, so
    the single due item on that account still picks it up by inference.
    """
    index: Dict[str, Any] = {
        "paid_linked_by_item": {},
        "payments_by_item": {},
        "paid_linked_by_employee": {},
        "payments_by_employee": {},
        "linked_posted_by_account": {},
        "salary_linked_by_account": {},
    }

    def _add(bucket: Dict[str, float], key: Optional[str], amount: float) -> None:
        if key:
            bucket[key] = flt(bucket.get(key)) + amount

    for request in requests:
        amount = flt(request.get("amount"))
        account = request.get("reason_account")
        period = (request.get("period_month") or "").strip()
        posted = (request.get("expense_month") or "").strip()
        kind = (request.get("expense_kind") or "Ad-hoc").strip() or "Ad-hoc"

        is_recurring = kind == "Recurring" and bool(request.get("recurring_expense"))
        is_salary = kind == "Salary" and bool(request.get("employee"))

        # ── period scope: what this period has been paid ──────────────────
        if period == month_key:
            if is_recurring:
                item = request["recurring_expense"]
                _add(index["paid_linked_by_item"], item, amount)
                index["payments_by_item"].setdefault(item, []).append(_payment_row(request))
            elif is_salary:
                employee = request["employee"]
                _add(index["paid_linked_by_employee"], employee, amount)
                index["payments_by_employee"].setdefault(employee, []).append(
                    _payment_row(request)
                )

        # ── posting scope: which of this month's GL this app produced ─────
        if posted == month_key:
            if is_recurring:
                _add(index["linked_posted_by_account"], account, amount)
            elif is_salary:
                _add(index["salary_linked_by_account"], account, amount)
            # Ad-hoc requests are deliberately NOT credited to an account here.

    return index


def _build_registry_rows(
    registry_rows: Sequence[Dict[str, Any]],
    month_start: date,
    month_end: date,
    paid_linked_by_item: Optional[Dict[str, Any]] = None,
    payments_by_item: Optional[Dict[str, Any]] = None,
    gl_by_account: Optional[Dict[str, Any]] = None,
    linked_posted_by_account: Optional[Dict[str, Any]] = None,
    payable_accounts: Optional[Iterable[str]] = None,
    account_labels: Optional[Dict[str, Dict[str, str]]] = None,
    tolerance: float = MONEY_TOLERANCE,
) -> Tuple[List[Dict[str, Any]], Dict[str, float]]:
    """Compute the per-item money picture for one month. Pure — no DB access.

    ``paid_linked_by_item`` is PERIOD-scoped (what this period has been paid);
    ``linked_posted_by_account`` is POSTING-scoped (what this app put in this
    month's ledger). They are not interchangeable — see ``_unlinked_by_account``.
    """
    paid_linked_by_item = paid_linked_by_item or {}
    payments_by_item = payments_by_item or {}
    gl_by_account = gl_by_account or {}
    linked_posted_by_account = linked_posted_by_account or {}
    account_labels = account_labels or {}
    payable = set(payable_accounts) if payable_accounts is not None else None

    prepared = [
        {
            "name": raw.get("name"),
            "expense_account": raw.get("expense_account"),
            "due_this_month": _is_due_in_month(raw, month_start, month_end),
            "raw": raw,
        }
        for raw in registry_rows
    ]

    unlinked = _unlinked_by_account(gl_by_account, linked_posted_by_account)
    attribution, leftovers = _attribute_unlinked(prepared, unlinked, tolerance=tolerance)

    rows: List[Dict[str, Any]] = []
    for entry in prepared:
        raw = entry["raw"]
        name = entry["name"]
        due_this_month = entry["due_this_month"]
        account = entry["expense_account"]
        attr = attribution.get(name) or {}

        due_amount = flt(raw.get("amount")) if due_this_month else 0.0
        paid_linked = flt(paid_linked_by_item.get(name))
        paid_unlinked = flt(attr.get("paid_unlinked"))
        paid_amount = paid_linked + paid_unlinked
        remaining = due_amount - paid_amount
        if remaining < 0:
            remaining = 0.0

        labels = account_labels.get(account) or {}
        payable_here = True if payable is None else bool(account and account in payable)

        rows.append(
            {
                "name": name,
                "expense_name": raw.get("expense_name"),
                "category": raw.get("category"),
                "status": raw.get("status"),
                "amount": _money(raw.get("amount")),
                "currency": raw.get("currency"),
                "frequency": raw.get("frequency"),
                "monthly_equivalent": _money(raw.get("monthly_equivalent")),
                "day_of_month": raw.get("day_of_month"),
                "due_date": _due_date_for_month(raw.get("day_of_month"), month_start),
                "expense_account": account,
                "expense_account_label": labels.get("label") or account,
                "expense_account_label_en": labels.get("label_en") or labels.get("label") or account,
                "expense_account_label_ar": labels.get("label_ar") or labels.get("label") or account,
                "cost_center": raw.get("cost_center"),
                "supplier": raw.get("supplier"),
                "default_paying_account": raw.get("default_paying_account"),
                "start_date": raw.get("start_date"),
                "end_date": raw.get("end_date"),
                "auto_repeat": raw.get("auto_repeat"),
                "notes": raw.get("notes"),
                "due_this_month": due_this_month,
                "due_amount": _money(due_amount),
                "paid_amount": _money(paid_amount),
                "paid_linked": _money(paid_linked),
                "paid_unlinked": _money(paid_unlinked),
                "remaining": _money(remaining),
                "payment_status": _payment_status(
                    due_amount, paid_amount, due_this_month, tolerance
                ),
                "shared_account": bool(attr.get("shared_account")),
                "inferred": bool(attr.get("inferred")),
                "account_payable": payable_here,
                "can_pay": bool(due_this_month and payable_here),
                "payments": list(payments_by_item.get(name) or []),
            }
        )

    rows.sort(
        key=lambda r: (r["payment_status"] == "Not Due", -flt(r["remaining"]), r["expense_name"] or "")
    )
    return rows, leftovers


def _build_payroll_rows(
    payroll_rows: Sequence[Dict[str, Any]],
    paid_linked_by_employee: Optional[Dict[str, Any]] = None,
    payments_by_employee: Optional[Dict[str, Any]] = None,
    slips_by_employee: Optional[Dict[str, Any]] = None,
    salary_account: Optional[str] = None,
    penalties_by_employee: Optional[Dict[str, Any]] = None,
    advances_by_employee: Optional[Dict[str, Any]] = None,
    orders_by_employee: Optional[Dict[str, Any]] = None,
    settled_by_employee: Optional[Dict[str, Any]] = None,
    off_payroll_rows: Optional[Sequence[Dict[str, Any]]] = None,
    days_per_month: int = DAYS_PER_MONTH,
    tolerance: float = MONEY_TOLERANCE,
) -> List[Dict[str, Any]]:
    """Per-employee salary money for one month. Pure — no DB access.

    Salary GL is a single account for the whole company, so it can never be split
    per employee. Nothing is inferred here: an employee's ``paid_amount`` is only
    ever the sum of app payments explicitly linked to them, plus what a
    settlement Journal Entry discharged in kind.

    Every deduction arrives as a pre-loaded MAP, exactly like
    ``paid_linked_by_employee``. Keeping this function free of DB access is not
    style: it is what lets the money model be tested at all, and what stops the
    payroll table issuing a query per employee.

    ── One asymmetry, deliberate ────────────────────────────────────────────
    ``penalty_total`` is MONTH-scoped (a penalty is deducted from the salary
    month it was recorded against). ``advance_total`` and ``order_total`` are
    ALL-TIME open balances, mirroring ``manager.get_employee_ledger``. That is
    not an oversight to be tidied up later: windowing a balance to the month
    hides exactly the stale debt worth chasing — a 5,000 EGP advance drawn in
    July would vanish from August's screen while still being owed.

    ``off_payroll_rows`` are people with NO Salary Structure Assignment who
    nonetheless carry an advance, an order or a penalty. They get a row with
    ``gross_due`` 0 so their debt is visible; production has two (Kareem Mamdouh
    and the CEO), and an advance to either must not be invisible.
    """
    paid_linked_by_employee = paid_linked_by_employee or {}
    payments_by_employee = payments_by_employee or {}
    slips_by_employee = slips_by_employee or {}
    penalties_by_employee = penalties_by_employee or {}
    advances_by_employee = advances_by_employee or {}
    orders_by_employee = orders_by_employee or {}
    settled_by_employee = settled_by_employee or {}

    prepared = [(raw, False) for raw in payroll_rows]
    prepared += [(raw, True) for raw in (off_payroll_rows or [])]

    rows: List[Dict[str, Any]] = []
    for raw, off_payroll in prepared:
        employee = raw.get("employee")

        if off_payroll:
            gross_due = 0.0
        else:
            gross_due = flt(raw.get("monthly"))
            if not gross_due:
                gross_due = flt(raw.get("base")) + flt(raw.get("variable"))

        penalties = [_serialize_penalty(p) for p in (penalties_by_employee.get(employee) or [])]
        penalty_total = sum(flt(p["amount"]) for p in penalties)
        penalty_days = sum(flt(p["equivalent_days"]) for p in penalties)

        # THE definition of what the company owes for the month. A penalty
        # lowers it here and nowhere else, so `_summarize`, `_build_categories`
        # and the overpay guard all see the reduced obligation without knowing
        # penalties exist. Floored at zero: a penalty larger than the salary
        # (possible only via `allow_overpay=1`) means nothing is owed — carrying
        # a negative due into the roll-up would silently cancel out somebody
        # else's unpaid salary.
        due_amount = gross_due - penalty_total
        if due_amount < 0:
            due_amount = 0.0

        paid_linked = flt(paid_linked_by_employee.get(employee))
        settled_amount = flt(settled_by_employee.get(employee))
        paid_amount = paid_linked + settled_amount
        remaining = due_amount - paid_amount
        if remaining < 0:
            remaining = 0.0

        advances = [_serialize_advance(a) for a in (advances_by_employee.get(employee) or [])]
        advance_total = sum(flt(a["outstanding"]) for a in advances)
        orders = [_serialize_order(o) for o in (orders_by_employee.get(employee) or [])]
        order_total = sum(flt(o["outstanding"]) for o in orders)

        # Cash to hand over NOW: what is still owed for the month, less the
        # balances this payment can clear. Floored at zero — an employee who owes
        # the company more than this month's salary gets nothing, they do not get
        # a negative payslip, and the remainder stays on the advance.
        net_payable = remaining - advance_total - order_total
        if net_payable < 0:
            net_payable = 0.0

        has_slip = bool(slips_by_employee.get(employee))
        rows.append(
            {
                "employee": employee,
                "employee_name": raw.get("employee_name"),
                "designation": raw.get("designation"),
                "department": raw.get("department"),
                "salary_structure": raw.get("salary_structure"),
                "base": _money(raw.get("base")),
                "variable": _money(raw.get("variable")),
                "gross_due": _money(gross_due),
                "day_rate": _money(_day_rate(gross_due, days_per_month)),
                "penalty_total": _money(penalty_total),
                "penalty_days": flt(penalty_days, 2),
                "penalties": penalties,
                "due_amount": _money(due_amount),
                "paid_amount": _money(paid_amount),
                "paid_linked": _money(paid_linked),
                "settled_amount": _money(settled_amount),
                "remaining": _money(remaining),
                "advance_total": _money(advance_total),
                "advances": advances,
                "order_total": _money(order_total),
                "orders": orders,
                "deductions_total": _money(penalty_total + advance_total + order_total),
                "net_payable": _money(net_payable),
                "off_payroll": off_payroll,
                "payment_status": _payment_status(
                    due_amount, paid_amount, due_amount > 0, tolerance
                ),
                "has_salary_slip": has_slip,
                "salary_slip": (slips_by_employee.get(employee) or {}).get("name"),
                "can_pay": bool(not has_slip and salary_account),
                "payments": list(payments_by_employee.get(employee) or []),
            }
        )
    # Off-payroll rows tie-break AFTER payroll rows: they always have
    # `remaining` 0, so without the flag they would interleave with paid staff
    # by name alone and read as part of the payroll.
    rows.sort(
        key=lambda r: (
            -flt(r["remaining"]),
            r["off_payroll"],
            -flt(r["deductions_total"]),
            r["employee_name"] or "",
        )
    )
    return rows


def _build_deductions(
    payroll_rows: Sequence[Dict[str, Any]],
    advances_readable: bool = True,
    employee_orders_present: bool = False,
    advances_by_employee: Optional[Dict[str, Any]] = None,
    unattributed_orders: Optional[Sequence[Dict[str, Any]]] = None,
    tolerance: float = MONEY_TOLERANCE,
) -> Dict[str, Any]:
    """The month's deduction totals, rolled up from the rows. Pure.

    Summed from the SAME row fields the screen renders, never recomputed from
    the source documents: a total that disagrees with the rows under it is worse
    than no total, because the manager cannot tell which one to act on.
    """
    advances_by_employee = advances_by_employee or {}

    penalty_total = sum(flt(r.get("penalty_total")) for r in payroll_rows)
    penalty_days = sum(flt(r.get("penalty_days")) for r in payroll_rows)
    advance_total = sum(flt(r.get("advance_total")) for r in payroll_rows)
    order_total = sum(flt(r.get("order_total")) for r in payroll_rows)
    net_payable = sum(flt(r.get("net_payable")) for r in payroll_rows)

    # Open advances booked somewhere that is not an employee-advance ledger.
    # Reported, never repointed: `advance_account` is on a SUBMITTED document
    # and is the account the original payout credited, so changing it here would
    # leave the posted GL and the document disagreeing.
    suspect: Dict[str, Dict[str, Any]] = {}
    for rows in advances_by_employee.values():
        for row in rows or []:
            account = row.get("advance_account")
            if not account or _is_advance_ledger(account):
                continue
            bucket = suspect.setdefault(account, {"account": account, "count": 0, "total": 0.0})
            bucket["count"] += 1
            bucket["total"] += _advance_open_amount(row)

    # Open staff orders that belong to nobody on this board. Kept OUT of
    # `order_total` and out of every row — attributing them would be a guess —
    # but listed, and raised as a gap, so the money is visible and somebody can
    # link the Customer to the Employee. Dropping them is what made the board
    # report zero jar debt on staging while a 92 EGP order sat unpaid.
    orphan_rows = [_serialize_order(r) for r in (unattributed_orders or [])]
    orphan_total = sum(flt(r.get("outstanding")) for r in orphan_rows)

    return {
        "penalty_total": _money(penalty_total),
        "penalty_days": flt(penalty_days, 2),
        "advance_total": _money(advance_total),
        "order_total": _money(order_total),
        "total": _money(penalty_total + advance_total + order_total),
        "net_payable": _money(net_payable),
        "unattributed_order_total": _money(orphan_total),
        "unattributed_orders": orphan_rows,
        "advances_readable": bool(advances_readable),
        "employee_orders_present": bool(employee_orders_present),
        "penalty_units": list(PENALTY_UNITS),
        "days_per_month": DAYS_PER_MONTH,
        "advance_accounts_suspect": [
            {
                "account": b["account"],
                "count": b["count"],
                "total": _money(b["total"]),
            }
            for b in sorted(suspect.values(), key=lambda b: -flt(b["total"]))
        ],
    }


def _summarize(
    registry_rows: Sequence[Dict[str, Any]],
    payroll_rows: Sequence[Dict[str, Any]],
    registry_run_rate: float,
    payroll_run_rate: float,
    tolerance: float = MONEY_TOLERANCE,
) -> Dict[str, Any]:
    """Roll the rows up.

    ``remaining`` is the SUM of per-row remainders, never ``due - paid``:
    overpaying one landlord must not cancel out an unpaid one. ``overpaid`` is
    the mirror-image sum, so the two together explain the difference.
    """
    due = paid = remaining = overpaid = 0.0
    counted = counted_paid = counted_partial = counted_unpaid = 0

    for row in list(registry_rows) + list(payroll_rows):
        row_due = flt(row.get("due_amount"))
        row_paid = flt(row.get("paid_amount"))
        status = row.get("payment_status")
        if status == "Not Due":
            # Money can still have moved against a not-due row (an explicit
            # overpay); it belongs in `paid` so the totals reconcile with the GL.
            paid += row_paid
            overpaid += row_paid
            continue
        counted += 1
        due += row_due
        paid += row_paid
        remaining += max(row_due - row_paid, 0.0)
        overpaid += max(row_paid - row_due, 0.0)
        if status in ("Paid", "Overpaid"):
            counted_paid += 1
        elif status == "Partial":
            counted_partial += 1
        else:
            counted_unpaid += 1

    return {
        "run_rate": _money(registry_run_rate + payroll_run_rate),
        "registry_run_rate": _money(registry_run_rate),
        "payroll_run_rate": _money(payroll_run_rate),
        "due": _money(due),
        "paid": _money(paid),
        "remaining": _money(remaining),
        "overpaid": _money(overpaid),
        "items_total": counted,
        "items_paid": counted_paid,
        "items_partial": counted_partial,
        "items_unpaid": counted_unpaid,
    }


def _build_categories(
    registry_rows: Sequence[Dict[str, Any]],
    payroll: Dict[str, Any],
    tolerance: float = MONEY_TOLERANCE,
) -> List[Dict[str, Any]]:
    buckets: Dict[str, Dict[str, Any]] = {}
    for row in registry_rows:
        due = flt(row.get("due_amount"))
        paid = flt(row.get("paid_amount"))
        if due <= tolerance and paid <= tolerance:
            continue
        key = row.get("category") or "Other"
        bucket = buckets.setdefault(
            key,
            {"category": key, "due": 0.0, "paid": 0.0, "remaining": 0.0, "count": 0, "source": "Registry"},
        )
        bucket["due"] += due
        bucket["paid"] += paid
        bucket["remaining"] += max(due - paid, 0.0)
        bucket["count"] += 1

    payroll_due = flt(payroll.get("due"))
    payroll_paid = flt(payroll.get("paid"))
    if payroll_due > tolerance or payroll_paid > tolerance:
        buckets[PAYROLL_CATEGORY] = {
            "category": PAYROLL_CATEGORY,
            "due": payroll_due,
            "paid": payroll_paid,
            "remaining": flt(payroll.get("remaining")),
            "count": len(payroll.get("rows") or []),
            "source": "HRMS",
        }

    result = []
    for bucket in buckets.values():
        result.append(
            {
                "category": bucket["category"],
                "due": _money(bucket["due"]),
                "paid": _money(bucket["paid"]),
                "remaining": _money(bucket["remaining"]),
                "count": bucket["count"],
                "source": bucket["source"],
            }
        )
    result.sort(key=lambda b: (-flt(b["due"]), b["category"]))
    return result


def _build_gaps(
    registry_rows: Sequence[Dict[str, Any]],
    registry_present: bool,
    leftovers: Dict[str, float],
    payroll: Dict[str, Any],
    deductions: Optional[Dict[str, Any]] = None,
    tolerance: float = MONEY_TOLERANCE,
) -> List[Dict[str, str]]:
    # `deductions` is optional and trailing so the four-argument call every
    # existing caller (and test) makes keeps working unchanged.
    deductions = deductions or {}
    gaps: List[Dict[str, str]] = []

    if not registry_present:
        gaps.append(
            {
                "severity": "critical",
                "message": _(
                    "No recurring expenses registered yet. Rent, utilities, telecom and services are invisible until they are added."
                ),
            }
        )

    # `readable` is absent on hand-built payroll blocks in older callers; only an
    # explicit False means HRMS actually failed.
    if payroll.get("readable") is False:
        gaps.append(
            {
                "severity": "critical",
                "message": _(
                    "Salaries could not be read from HRMS, so no payroll is shown or counted as due this month. Everything below covers the recurring expense registry only."
                ),
            }
        )

    missing_structures = int(payroll.get("employees_without_structure") or 0)
    if missing_structures:
        gaps.append(
            {
                "severity": "critical" if not payroll.get("configured") else "warning",
                "message": _(
                    "{0} of {1} active employees have no Salary Structure Assignment, so their salary is not counted as due."
                ).format(missing_structures, payroll.get("employees_total") or 0),
            }
        )

    overpaid = [r for r in registry_rows if r.get("payment_status") == "Overpaid"]
    if overpaid:
        gaps.append(
            {
                "severity": "warning",
                "message": _(
                    "{0} expense(s) show more posted than due this month ({1}). Usually an Auto Repeat journal entry submitted in Desk on top of an app payment."
                ).format(len(overpaid), ", ".join(r.get("expense_name") or r["name"] for r in overpaid[:5])),
            }
        )

    if leftovers:
        total = sum(flt(v) for v in leftovers.values())
        gaps.append(
            {
                "severity": "warning",
                "message": _(
                    "{0} posted to account(s) {1} could not be matched to a single expense — several due expenses share the account, so it is not counted as paid for any of them."
                ).format(_money(total), ", ".join(sorted(leftovers)[:5])),
            }
        )

    unattributed = flt(payroll.get("unattributed_gl"))
    if unattributed > tolerance:
        gaps.append(
            {
                "severity": "warning",
                "message": _(
                    "{0} posted to the salary account(s) this month did not come from this screen, so it is not credited to any employee."
                ).format(_money(unattributed)),
            }
        )

    unpayable = [
        r
        for r in registry_rows
        if r.get("due_this_month") and not r.get("account_payable")
    ]
    if unpayable:
        gaps.append(
            {
                "severity": "warning",
                "message": _(
                    "{0} expense(s) due this month post to an account outside Indirect Expenses and cannot be paid from this screen: {1}."
                ).format(len(unpayable), ", ".join(r.get("expense_name") or r["name"] for r in unpayable[:5])),
            }
        )

    # ── deductions ────────────────────────────────────────────────────────
    # Only when the block was actually computed: an older caller passing four
    # arguments gets exactly the gaps it got before, rather than three new ones
    # asserting things this call never looked at.
    if deductions:
        if deductions.get("advances_readable") is False:
            gaps.append(
                {
                    "code": "advances_unreadable",
                    "severity": "warning",
                    "message": _(
                        "Employee advances could not be read from HRMS, so no advance is "
                        "deducted from any salary below. An employee may be paid in full "
                        "while still owing the company an advance."
                    ),
                }
            )

        if not deductions.get("employee_orders_present"):
            gaps.append(
                {
                    "code": "employee_orders_unused",
                    "severity": "info",
                    "message": _(
                        "Staff purchases only appear here when the order is rung up with "
                        "the Employee Order policy. No order has ever been, so jar debt "
                        "reads as zero for everyone."
                    ),
                }
            )

        orphans = deductions.get("unattributed_orders") or []
        if orphans:
            gaps.append(
                {
                    "code": "employee_orders_unattributed",
                    "severity": "warning",
                    "message": _(
                        "{0} staff order(s) worth {1} are not linked to anyone, so they "
                        "are not deducted from any salary. Set the Employee field on "
                        "each of those customers ({2}) and they will land on that "
                        "person's row."
                    ).format(
                        len(orphans),
                        _money(deductions.get("unattributed_order_total")),
                        ", ".join(
                            sorted(
                                {
                                    str(o.get("customer_name") or o.get("customer"))
                                    for o in orphans
                                }
                            )[:5]
                        ),
                    ),
                }
            )

        suspect = deductions.get("advance_accounts_suspect") or []
        if suspect:
            gaps.append(
                {
                    "code": "advance_account_is_debtors",
                    "severity": "warning",
                    "message": _(
                        "{0} open advance(s) worth {1} sit on {2}, which is not an "
                        "employee-advance ledger — that balance is mixed in with real "
                        "customers' debt. Settling from this screen credits the same "
                        "account it was booked to, so nothing is repointed here."
                    ).format(
                        sum(int(s.get("count") or 0) for s in suspect),
                        _money(sum(flt(s.get("total")) for s in suspect)),
                        ", ".join(str(s.get("account")) for s in suspect[:5]),
                    ),
                }
            )

    return gaps


# ── data loading ──────────────────────────────────────────────────────────


def _load_registry(company: Optional[str]) -> List[Dict[str, Any]]:
    filters: Dict[str, Any] = {}
    if company:
        filters["company"] = company
    return frappe.get_all(
        "Jarz Recurring Expense",
        filters=filters,
        fields=_REGISTRY_FIELDS,
        order_by="category asc, expense_name asc",
        limit_page_length=0,
    )


def _load_period_requests(month_key: str, company: Optional[str]) -> List[Dict[str, Any]]:
    """Submitted requests that either pay this period or posted in this month."""
    filters: Dict[str, Any] = {"docstatus": 1}
    if company:
        filters["company"] = company
    return frappe.get_all(
        "Jarz Expense Request",
        filters=filters,
        or_filters=[
            ["Jarz Expense Request", "period_month", "=", month_key],
            ["Jarz Expense Request", "expense_month", "=", month_key],
        ],
        fields=_REQUEST_FIELDS,
        order_by="expense_date asc, creation asc",
        limit_page_length=0,
    )


def _penalty_doctype_ready() -> bool:
    """True when ``Jarz Employee Penalty`` has actually been migrated in.

    Same shape as ``_period_fields_ready``: the DocType ships with this release,
    so between deploying the code and running ``bench migrate`` the table does
    not exist and querying it raises. Penalties simply do not exist yet on such
    a bench — an empty list is the truth, not a degraded answer.
    """
    try:
        return bool(frappe.db.table_exists(PENALTY_DOCTYPE))
    except Exception:
        _log("monthly_expenses: penalty table probe")
        return False


def _load_penalties(
    month_key: str, company: Optional[str]
) -> Dict[str, List[Dict[str, Any]]]:
    """ACTIVE penalties for one salary month, keyed by employee.

    MONTH-scoped, unlike advances and orders: ``period_month`` says which
    salary a penalty comes out of, and a September penalty must not keep
    deducting from October.

    ``docstatus=1`` only — cancelling a penalty (docstatus 2) is how it is
    undone, so a cancelled one must stop reducing the salary immediately.
    """
    if not _penalty_doctype_ready():
        return {}
    filters: Dict[str, Any] = {"docstatus": 1, "period_month": month_key}
    if company:
        filters["company"] = company
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    try:
        rows = frappe.get_all(
            PENALTY_DOCTYPE,
            filters=filters,
            fields=_PENALTY_FIELDS,
            order_by="penalty_date asc, creation asc",
            limit_page_length=0,
        ) or []
        # Grouped INSIDE the try: the read and the shaping of its result fail
        # the same way, and a screen that dies half-way through building a map
        # is no more use than one that never read it.
        for row in rows:
            grouped.setdefault(row.get("employee"), []).append(row)
    except Exception:
        _log("monthly_expenses: penalty load")
        return {}
    return grouped


def _load_advances(
    company: Optional[str],
) -> Tuple[Dict[str, List[Dict[str, Any]]], bool]:
    """Open Employee Advance balances, ALL-TIME, keyed by employee.

    Returns ``(grouped, readable)``. ``readable`` is False when HRMS is absent
    or the query failed — the screen then renders every other number and
    ``_build_gaps`` says advances are missing, rather than showing a confident
    zero next to a salary that is about to be overpaid.

    Not month-scoped, deliberately: see ``_build_payroll_rows``. Fully settled
    advances are dropped here rather than shown with a zero balance; they are
    history, and the row is a list of what is still owed.
    """
    if not hrms_available():
        return {}, False

    fields = list(_ADVANCE_FIELDS)
    has_settled = _advance_has_field(F_SETTLED_AMOUNT)
    if has_settled:
        fields.append(F_SETTLED_AMOUNT)
    if _advance_has_field(F_SETTLED_VIA):
        fields.append(F_SETTLED_VIA)

    filters: Dict[str, Any] = {"docstatus": 1}
    if company:
        filters["company"] = company
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    try:
        rows = frappe.get_all(
            ADVANCE_DOCTYPE,
            filters=filters,
            fields=fields,
            order_by="posting_date desc, modified desc",
            limit_page_length=0,
        ) or []
        for row in rows:
            if _advance_open_amount(row) <= MONEY_TOLERANCE:
                continue
            grouped.setdefault(row.get("employee"), []).append(row)
    except Exception:
        _log("monthly_expenses: employee advance load")
        return {}, False
    return grouped, True


def _employee_order_field_ready() -> bool:
    """True when ``Sales Invoice.custom_order_purpose`` exists.

    A pre-fixture bench has no such column, and filtering on it raises. Empty is
    the correct answer there: with no purpose field, no order can be flagged as
    staff.
    """
    try:
        return bool(frappe.get_meta("Sales Invoice").get_field("custom_order_purpose"))
    except Exception:
        return False


def _employee_orders_exist(company: Optional[str]) -> bool:
    """Has ANY order ever been rung up with the Employee purpose?

    Separate from the balance query and deliberately unfiltered by outstanding:
    production has zero such invoices, which is why jar debt legitimately reads
    zero, and the gap that says so must distinguish "nobody owes anything" from
    "the flow has never been used".
    """
    if not _employee_order_field_ready():
        return False
    filters: Dict[str, Any] = {
        "docstatus": 1,
        "custom_order_purpose": EMPLOYEE_ORDER_PURPOSE,
    }
    if company:
        filters["company"] = company
    try:
        return bool(frappe.db.count("Sales Invoice", filters))
    except Exception:
        _log("monthly_expenses: employee order probe")
        return False


def _load_employee_orders(
    employees: Sequence[str], company: Optional[str]
) -> Tuple[Dict[str, List[Dict[str, Any]]], List[Dict[str, Any]]]:
    """Unpaid Employee-purpose invoices, ALL-TIME, keyed by employee.

    The join is ``employee_link.customers_for_employees`` and the filter is
    ``custom_order_purpose = "Employee"``. BOTH are required, and the owner ruled
    out the shortcut of trusting ``Customer.custom_employee`` alone: production
    carries 48 such links and they are misused — one employee is linked to 25
    customers who are not her, so counting those customers' invoices would
    invent tens of thousands of pounds of staff debt.

    Returns ``(grouped, unattributed)``. EVERY open staff order is in one or the
    other — an invoice whose customer maps to no employee goes into
    ``unattributed`` rather than being dropped. Staging proved why: its one staff
    order (92 EGP, still outstanding) sits on a Customer whose
    ``custom_employee`` is empty, so a join-and-forget implementation showed it
    on nobody's row and the board reported zero jar debt while a real unpaid
    order existed. Silence is the one answer this screen must never give about
    money — the same rule ``unattributed_gl`` already follows for the registry.
    """
    if not _employee_order_field_ready():
        return {}, []
    try:
        by_employee = customers_for_employees(employees) if employees else {}
    except Exception:
        _log("monthly_expenses: employee customer join")
        by_employee = {}
    # Inverted from the same map, so an order can only ever be attributed to the
    # employee that map already points at.
    employee_of_customer = {cust: emp for emp, cust in (by_employee or {}).items()}

    # Unfiltered by customer on purpose: the attribution happens below, so an
    # order for an unlinked customer is still SEEN.
    filters: Dict[str, Any] = {
        "docstatus": 1,
        "custom_order_purpose": EMPLOYEE_ORDER_PURPOSE,
        "outstanding_amount": [">", 0],
    }
    if company:
        filters["company"] = company
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    unattributed: List[Dict[str, Any]] = []
    try:
        rows = frappe.get_all(
            "Sales Invoice",
            filters=filters,
            fields=_ORDER_FIELDS,
            order_by="posting_date desc, modified desc",
            limit_page_length=0,
        ) or []
        for row in rows:
            employee = employee_of_customer.get(row.get("customer"))
            if employee:
                grouped.setdefault(employee, []).append(row)
            else:
                unattributed.append(row)
    except Exception:
        _log("monthly_expenses: employee order load")
        return {}, []
    return grouped, unattributed


def _load_settlements(month_key: str, company: Optional[str]) -> Dict[str, float]:
    """What each employee's salary month was discharged IN KIND, by employee.

    A settlement is a Journal Entry, not a ``Jarz Expense Request``, so
    ``paid_linked_by_employee`` cannot see it. It is found by its tag —
    ``[JARZ-JE:SALARY_SETTLEMENT:<employee>:<month>]`` — the same provenance
    mechanism every other jarz-posted Journal Entry uses.

    ``docstatus=1`` only, which is what makes this self-healing: cancelling the
    settlement Journal Entry in Desk immediately puts the salary back to unpaid,
    with no second document to remember to undo.
    """
    like = "%[JARZ-JE:{0}:%:{1}]%".format(SETTLEMENT_JE_TAG, month_key)
    filters: Dict[str, Any] = {"docstatus": 1, "user_remark": ["like", like]}
    if company:
        filters["company"] = company
    settled: Dict[str, float] = {}
    try:
        rows = frappe.get_all(
            "Journal Entry",
            filters=filters,
            fields=["name", "user_remark", "total_debit"],
            limit_page_length=0,
        ) or []
        for row in rows:
            # The LIKE is a prefilter; the regex is the decision. `%` in the
            # pattern would happily match a remark whose employee segment
            # contains a colon or a second tag, and the employee is read out of
            # the tag, so it has to be matched exactly.
            match = _SETTLEMENT_TAG_RE.search(str(row.get("user_remark") or ""))
            if not match or match.group(2) != month_key:
                continue
            employee = match.group(1)
            settled[employee] = flt(settled.get(employee)) + flt(row.get("total_debit"))
    except Exception:
        _log("monthly_expenses: settlement lookup")
        return {}
    return settled


def _off_payroll_stubs(
    employees: Sequence[str],
    penalties_by_employee: Optional[Dict[str, Any]] = None,
    advances_by_employee: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Minimal payroll-row seeds for people with debt but no salary structure.

    The display name is taken from a document that already names them —
    ``employee_name`` is denormalised onto both Employee Advance and the penalty
    — and only the leftovers cost a query. On a bench with no HRMS the lookup
    returns nothing and the row falls back to the employee ID, which is still a
    visible row: the whole point is that an advance to the CEO cannot be
    invisible just because he draws no salary.
    """
    penalties_by_employee = penalties_by_employee or {}
    advances_by_employee = advances_by_employee or {}
    if not employees:
        return []

    names: Dict[str, str] = {}
    for source in (advances_by_employee, penalties_by_employee):
        for employee, rows in source.items():
            for row in rows or []:
                label = str(row.get("employee_name") or "").strip()
                if label and employee not in names:
                    names[employee] = label

    unknown = [e for e in employees if e not in names]
    if unknown:
        try:
            names.update(employee_display_names(unknown) or {})
        except Exception:
            _log("monthly_expenses: off-payroll employee names")

    return [
        {
            "employee": employee,
            "employee_name": names.get(employee) or employee,
            "designation": None,
            "department": None,
            "salary_structure": None,
            "base": 0.0,
            "variable": 0.0,
            "monthly": 0.0,
        }
        for employee in employees
    ]


def _empty_payroll() -> Dict[str, Any]:
    """The payroll block a site without readable HRMS gets."""
    return {
        "configured": False,
        "employees_total": 0,
        "employees_with_structure": 0,
        "employees_without_structure": 0,
        "monthly_total": 0.0,
        "rows": [],
        "missing": [],
    }


def _safe_load_payroll(
    company: Optional[str], month_end: date
) -> Tuple[Dict[str, Any], bool]:
    """``recurring_expenses._load_payroll``, but never fatal to the screen.

    ``Employee`` and ``Salary Structure Assignment`` are HRMS doctypes. On a site
    where HRMS is not installed — or is installed and broken — querying them
    raises, and before this wrapper that took the ENTIRE endpoint down: the
    manager saw nothing at all, not even the registry half, which needs no HRMS.

    Half a screen is strictly better than none here, provided the missing half
    announces itself. So the failure degrades to an empty, explicitly
    ``configured: False`` payroll block and a ``gaps`` entry (see
    ``_build_gaps``) rather than silently reporting "0 salaries due" as fact.

    The wrap lives at the call site on purpose: ``recurring_expenses`` backs the
    Desk page and is deliberately left untouched by this module.
    """
    try:
        return _load_payroll(company, month_end), True
    except Exception:
        # `_log` and not `frappe.log_error`: logging can itself raise (it writes
        # an Error Log document), and losing the screen to the error handler is
        # exactly the failure being fixed here.
        _log("monthly_expenses: payroll load")
        return _empty_payroll(), False


def _salary_slip_doctype_exists() -> bool:
    try:
        return bool(frappe.db.table_exists("Salary Slip"))
    except Exception:
        _log("monthly_expenses: salary slip table probe")
        return False


def _submitted_salary_slips(
    employees: Sequence[str], month_start: date, month_end: date, strict: bool = False
) -> Dict[str, Dict[str, Any]]:
    """Submitted Salary Slips overlapping the month, keyed by employee.

    ``strict=True`` (the payment path) lets a query failure propagate: if HRMS is
    installed but unreadable, assuming "no slips" would let this module post a
    second salary Journal Entry on top of a real payroll run.
    """
    if not employees:
        return {}
    if not _salary_slip_doctype_exists():
        return {}
    try:
        rows = frappe.get_all(
            "Salary Slip",
            filters={
                "docstatus": 1,
                "employee": ["in", list(employees)],
                "start_date": ["<=", month_end],
                "end_date": [">=", month_start],
            },
            fields=["name", "employee", "start_date", "end_date", "net_pay"],
            limit_page_length=0,
        )
    except Exception:
        _log("monthly_expenses: salary slip lookup")
        if strict:
            raise
        return {}
    slips: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        slips.setdefault(row["employee"], row)
    return slips


def _resolve_salary_account(
    payroll_accounts: Sequence[str], payable_accounts: Iterable[str]
) -> Optional[str]:
    """Pick the ONE account a salary payment should debit.

    ``_payroll_expense_accounts`` can return several (one per Salary Component).
    A payment needs exactly one, so prefer an account that is actually payable
    from this screen (under Indirect Expenses) and whose name is the aggregate
    salary ledger rather than a component sub-ledger.
    """
    payable = set(payable_accounts or [])
    candidates = [a for a in payroll_accounts if a in payable] or list(payroll_accounts)
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    preferred = {"salary", "salaries", "payroll", "wages"}
    for account in sorted(candidates):
        account_name = str(account).split(" - ")[0].strip().lower()
        if account_name in preferred:
            return account
    return sorted(candidates)[0]


def _registry_categories() -> List[str]:
    try:
        field = frappe.get_meta("Jarz Recurring Expense").get_field("category")
        options = [o.strip() for o in str(getattr(field, "options", "") or "").split("\n")]
        options = [o for o in options if o]
        if options:
            return options
    except Exception:
        _log("monthly_expenses: category options")
    return list(DEFAULT_CATEGORIES)


def _cost_centers(company: Optional[str]) -> List[Dict[str, Any]]:
    filters: Dict[str, Any] = {"is_group": 0}
    if company:
        filters["company"] = company
    try:
        rows = frappe.get_all(
            "Cost Center",
            filters=filters,
            fields=["name", "cost_center_name"],
            order_by="name asc",
            limit_page_length=0,
        )
    except Exception:
        _log("monthly_expenses: cost centers")
        return []
    return [
        {"name": r["name"], "label": r.get("cost_center_name") or r["name"]} for r in rows
    ]


def _month_context(
    month: Optional[str], company: Optional[str]
) -> Tuple[date, date, str, str, str]:
    month_start, month_end, month_key = _month_bounds(month)
    company = company or _default_company()
    currency = (
        frappe.db.get_value("Company", company, "default_currency") if company else None
    ) or frappe.defaults.get_global_default("currency")
    return month_start, month_end, month_key, company, currency


def _compute_month(
    month: Optional[str], company: Optional[str]
) -> Dict[str, Any]:
    """Everything the screen needs for one month. Used by the read AND pay paths.

    The pay endpoints recompute through here rather than re-deriving "already
    paid" their own way — one definition of paid, or the guard and the screen
    will eventually disagree about the same money.
    """
    month_start, month_end, month_key, company, currency = _month_context(month, company)

    registry_raw = _load_registry(company)
    requests = _load_period_requests(month_key, company)
    index = _split_period_requests(requests, month_key)

    expense_accounts = _indirect_expense_accounts(company)
    payable_accounts = [a["account"] for a in expense_accounts]
    payable_set = set(payable_accounts)

    registry_accounts = sorted({r["expense_account"] for r in registry_raw if r.get("expense_account")})
    payroll_accounts = _payroll_expense_accounts(company)
    # An account cannot be reconciled twice. If a registry item is booked to a
    # payroll account, the registry owns it and payroll must not also claim it.
    payroll_gl_accounts = [a for a in payroll_accounts if a not in set(registry_accounts)]

    gl_map = _gl_posted_by_account(
        sorted(set(registry_accounts) | set(payroll_gl_accounts)),
        month_start,
        month_end,
        company,
    )
    gl_by_account = {account: flt(b.get("amount")) for account, b in gl_map.items()}

    label_seed = {a: a for a in set(registry_accounts) | set(payroll_accounts)}
    account_labels = _account_label_map(list(label_seed), label_seed) if label_seed else {}

    registry_rows, leftovers = _build_registry_rows(
        registry_raw,
        month_start,
        month_end,
        paid_linked_by_item=index["paid_linked_by_item"],
        payments_by_item=index["payments_by_item"],
        gl_by_account={a: gl_by_account.get(a, 0.0) for a in registry_accounts},
        linked_posted_by_account=index["linked_posted_by_account"],
        payable_accounts=payable_set,
        account_labels=account_labels,
    )

    # ── payroll ───────────────────────────────────────────────────────────
    payroll_raw, payroll_readable = _safe_load_payroll(company, month_end)
    employees = [r["employee"] for r in payroll_raw.get("rows") or []]
    slips = _submitted_salary_slips(employees, month_start, month_end)
    salary_account = _resolve_salary_account(payroll_accounts, payable_set)

    # ── deductions ────────────────────────────────────────────────────────
    # Loaded HERE and handed to `_build_payroll_rows` as maps, the same way the
    # payment index is. The builder stays pure and issues no query per row.
    penalties_by_employee = _load_penalties(month_key, company)
    advances_by_employee, advances_readable = _load_advances(company)
    settled_by_employee = _load_settlements(month_key, company)

    # Anyone the month has anything to say about, whether or not payroll knows
    # them. Orders can only be joined for a known employee (see
    # `_load_employee_orders`), so the candidate set is built first.
    on_payroll = set(employees)
    candidates = sorted(
        on_payroll
        | set(penalties_by_employee)
        | set(advances_by_employee)
        # Somebody settled off-payroll this month: their advance may now be
        # closed and carry no balance, but the month still discharged money in
        # their name and has to be able to show it.
        | set(settled_by_employee)
    )
    orders_by_employee, unattributed_orders = _load_employee_orders(candidates, company)

    off_payroll_ids = sorted(
        (
            set(penalties_by_employee)
            | set(advances_by_employee)
            | set(orders_by_employee)
            | set(settled_by_employee)
        )
        - on_payroll
    )
    off_payroll_rows = _off_payroll_stubs(
        off_payroll_ids, penalties_by_employee, advances_by_employee
    )

    payroll_rows = _build_payroll_rows(
        payroll_raw.get("rows") or [],
        paid_linked_by_employee=index["paid_linked_by_employee"],
        payments_by_employee=index["payments_by_employee"],
        slips_by_employee=slips,
        salary_account=salary_account,
        penalties_by_employee=penalties_by_employee,
        advances_by_employee=advances_by_employee,
        orders_by_employee=orders_by_employee,
        settled_by_employee=settled_by_employee,
        off_payroll_rows=off_payroll_rows,
    )

    payroll_unlinked = _unlinked_by_account(
        {a: gl_by_account.get(a, 0.0) for a in payroll_gl_accounts},
        index["salary_linked_by_account"],
    )
    payroll_due = sum(flt(r["due_amount"]) for r in payroll_rows)
    payroll_paid = sum(flt(r["paid_amount"]) for r in payroll_rows)
    payroll_remaining = sum(flt(r["remaining"]) for r in payroll_rows)

    payroll = {
        "configured": bool(payroll_raw.get("configured")),
        # False only when HRMS could not be read at all — the screen still
        # renders the registry half, and `_build_gaps` says why payroll is blank.
        "readable": payroll_readable,
        "employees_total": payroll_raw.get("employees_total") or 0,
        "employees_with_structure": payroll_raw.get("employees_with_structure") or 0,
        "employees_without_structure": payroll_raw.get("employees_without_structure") or 0,
        "salary_account": salary_account,
        "salary_accounts": payroll_accounts,
        "salary_account_label": (account_labels.get(salary_account) or {}).get("label")
        or salary_account,
        "due": _money(payroll_due),
        "paid": _money(payroll_paid),
        "remaining": _money(payroll_remaining),
        "unattributed_gl": _money(sum(flt(v) for v in payroll_unlinked.values())),
        "rows": payroll_rows,
        "missing": payroll_raw.get("missing") or [],
    }

    deductions = _build_deductions(
        payroll_rows,
        advances_readable=advances_readable,
        employee_orders_present=_employee_orders_exist(company),
        advances_by_employee=advances_by_employee,
        unattributed_orders=unattributed_orders,
    )

    registry_run_rate = sum(
        flt(r.get("monthly_equivalent"))
        for r in registry_rows
        if (r.get("status") or "") == "Active"
    )
    summary = _summarize(
        registry_rows,
        payroll_rows,
        registry_run_rate,
        flt(payroll_raw.get("monthly_total")),
    )

    return {
        "month": month_key,
        "month_start": month_start,
        "month_end": month_end,
        "company": company,
        "currency": currency,
        "registry": registry_rows,
        "leftovers": leftovers,
        "payroll": payroll,
        "deductions": deductions,
        "summary": summary,
        "account_labels": account_labels,
        "payable_accounts": payable_accounts,
        "expense_accounts": expense_accounts,
        "registry_present": bool(registry_raw),
    }


# ── endpoints ─────────────────────────────────────────────────────────────


@frappe.whitelist()
def get_monthly_expenses(
    month: Optional[str] = None, company: Optional[str] = None
) -> Dict[str, Any]:
    """Everything the Monthly Expenses screen shows for one month."""
    _ensure_manager()
    _require_period_fields()

    context = _compute_month(month, company)
    month_key = context["month"]
    month_start: date = context["month_start"]
    month_end: date = context["month_end"]
    company = context["company"]

    months = _available_months(getdate().strftime("%Y-%m"))
    if month_key not in months:
        months = sorted(set(months) | {month_key})

    # `_serialize_payment_sources` names the bucket `category`; the contract's
    # picker reads `type`. Aliased rather than forked so the ad-hoc expense
    # screen and this one keep listing exactly the same accounts and balances.
    payment_sources = _serialize_payment_sources(_cashlike_accounts(company))
    for source in payment_sources:
        source["type"] = source.get("category")

    return {
        "success": True,
        "month": month_key,
        "month_label": _month_label(month_key),
        "month_start": month_start.isoformat(),
        "month_end": month_end.isoformat(),
        "company": company,
        "currency": context["currency"],
        "available_months": [{"month": m, "label": _month_label(m)} for m in months],
        "summary": context["summary"],
        "by_category": _build_categories(context["registry"], context["payroll"]),
        "recurring": context["registry"],
        "payroll": context["payroll"],
        # What REDUCES the payroll: penalties for this month, plus the all-time
        # advance and staff-order balances a payment can clear.
        "deductions": context["deductions"],
        "payment_sources": payment_sources,
        "expense_accounts": context["expense_accounts"],
        "cost_centers": _cost_centers(company),
        "categories": _registry_categories(),
        "frequencies": list(FREQUENCY_MONTHS.keys()),
        "statuses": list(REGISTRY_STATUSES),
        # Registry money this month that no single due item can claim. Mirrors
        # `payroll.unattributed_gl`; kept out of every item's `paid_amount` on
        # purpose (see `_attribute_unlinked`).
        "unattributed_gl": _money(sum(flt(v) for v in context["leftovers"].values())),
        "unattributed_by_account": [
            {"account": account, "amount": _money(amount)}
            for account, amount in sorted(context["leftovers"].items())
        ],
        "gaps": _build_gaps(
            context["registry"],
            context["registry_present"],
            context["leftovers"],
            context["payroll"],
            context["deductions"],
        ),
        "can_manage": True,
        # Cancelling a payment reverses a posted Journal Entry, and
        # `expenses.cancel_expense` gates that on `_is_manager()` — JARZ Manager
        # and the admin tier only. That is NARROWER than the gate on this
        # endpoint, which also admits Accounts Manager. Without this flag the
        # client would show an Accounts Manager a Cancel button that always
        # answers "Only managers can cancel expenses" — the visible-tile-that-
        # is-refused bug this codebase keeps hitting. Ask the same function the
        # cancel path will ask, rather than restating its role set here.
        "can_cancel_payments": _is_manager(),
    }


def _resolve_paying_account(paying_account: Optional[str], company: str) -> str:
    """The account the money leaves. Must be one the picker actually offers.

    The read path builds ``payment_sources`` from ``_cashlike_accounts``; the
    write path has to accept exactly that set and nothing else. Anything looser
    is not a validation gap, it is a hole in the chart of accounts: a direct
    call with ``paying_account="Sales - J"`` posts DEBIT Rent / CREDIT Sales,
    which books an expense by inventing revenue and quietly corrupts the P&L
    with a Journal Entry that looks entirely ordinary in the ledger.

    Refusing when the list comes back empty is deliberate. An empty picker means
    the company has no cash or bank ledger configured, and in that state there
    is no correct account to credit — failing closed leaves nothing posted,
    while failing open posts against whatever the caller named.
    """
    account = (paying_account or "").strip()
    if not account:
        frappe.throw(_("Paying account is required."))
    row = frappe.db.get_value(
        "Account", account, ["name", "is_group", "company", "account_name"], as_dict=True
    )
    if not row:
        frappe.throw(_("Paying account not found: {0}").format(account))
    if int(row.get("is_group") or 0):
        frappe.throw(_("Paying account must be a ledger account, not a group."))
    if company and row.get("company") and row["company"] != company:
        frappe.throw(
            _("Paying account {0} belongs to company {1}, not {2}.").format(
                account, row["company"], company
            )
        )

    try:
        allowed = {source.account for source in (_cashlike_accounts(company) or [])}
    except Exception:
        _log("monthly_expenses: paying account whitelist")
        allowed = set()
    if row["name"] not in allowed:
        frappe.throw(
            _(
                "{0} is not a valid source of money. A payment must be credited to "
                "one of the cash, bank or wallet accounts offered by the payment "
                "source picker; paying from any other ledger would post money out "
                "of an account that never held it."
            ).format(row["name"])
        )
    return row["name"]


def _lock_row(doctype: str, name: Optional[str]) -> None:
    """Serialize the read → guard → write window for one payable row.

    ``pay_recurring_expense`` and ``pay_salary`` read the month's state, decide
    against the overpay guard, then insert and submit. Nothing about that is
    atomic on its own: two taps of Pay, or a client retry after a lost response,
    both read the pre-payment state, both pass the guard, and both post a
    Journal Entry. The guard is not wrong — it never saw the other payment.

    A ``SELECT ... FOR UPDATE`` on the row being paid makes the second request
    block at this line until the first commits, so it recomputes against the
    state the first one created and the guard refuses it. The lock has to be
    taken BEFORE the state read; taken after, it protects nothing, because the
    numbers the guard uses were already fetched.

    Held to the end of the request and released by Frappe's commit/rollback.

    ``ignore=True`` covers the one doctype here that may not exist at all:
    ``Employee`` belongs to HRMS. There is nothing to serialize on a site with
    no Employee table, and raising a bare ``OperationalError`` from the lock
    would replace the friendly "no salary account could be resolved" refusal
    that path ends in with a database error.
    """
    if not name:
        return
    frappe.db.get_value(doctype, name, "name", ignore=True, for_update=True)


def _create_payment(
    *,
    company: str,
    amount: float,
    reason_account: str,
    paying_account: str,
    payment_date: Optional[str],
    remarks: Optional[str],
    expense_kind: str,
    period_month: str,
    recurring_expense: Optional[str] = None,
    employee: Optional[str] = None,
):
    """Insert and submit the Jarz Expense Request that posts the Journal Entry.

    ``requires_approval=0`` and an immediate ``submit()`` because the caller has
    already passed the manager gate — asking a manager to approve their own
    payment in a second round-trip would leave the money unposted if they never
    made it.
    """
    payment_label = (
        frappe.db.get_value("Account", paying_account, "account_name") or paying_account
    )
    # ``payment_date`` may carry a time ("YYYY-MM-DD HH:MM:SS"). Split it: the
    # date half decides ``expense_month`` (and so which month this payment is
    # reported in), which must never depend on the hour; the time half is
    # provenance, carried by ``on_submit`` onto the Journal Entry.
    expense_date, expense_time = split_posting_datetime(payment_date or today())
    doc = frappe.get_doc(
        {
            "doctype": "Jarz Expense Request",
            "company": company,
            "expense_date": expense_date,
            "expense_time": expense_time,
            "amount": flt(amount),
            "reason_account": reason_account,
            "paying_account": paying_account,
            "payment_source_type": "Account",
            "payment_source_label": payment_label,
            "requires_approval": 0,
            "remarks": remarks,
            "requested_by": frappe.session.user,
            "expense_kind": expense_kind,
            "recurring_expense": recurring_expense,
            "employee": employee,
            "period_month": period_month,
        }
    )
    doc.flags.ignore_permissions = True
    doc.insert(ignore_permissions=True)
    doc.approved_by = frappe.session.user
    doc.approved_on = now_datetime()
    doc.flags.ignore_permissions = True
    doc.submit()
    doc.reload()
    return doc


@frappe.whitelist()
def pay_recurring_expense(
    recurring_expense: str,
    month: Optional[str] = None,
    amount: Optional[float] = None,
    paying_account: Optional[str] = None,
    payment_date: Optional[str] = None,
    remarks: Optional[str] = None,
    allow_overpay: Any = 0,
) -> Dict[str, Any]:
    """Pay a registry item for a billing period."""
    _ensure_manager()
    _require_period_fields()

    name = (recurring_expense or "").strip()
    if not name:
        frappe.throw(_("Recurring expense is required."))

    amount = flt(amount)
    if amount <= 0:
        frappe.throw(_("Amount must be greater than zero."))

    allow = _as_bool(allow_overpay)

    # Before the state read, not after: see `_lock_row`. A second concurrent
    # payment for this item queues here and recomputes against the first.
    _lock_row("Jarz Recurring Expense", name)

    context = _compute_month(month, None)
    month_key = context["month"]
    company = context["company"]

    row = next((r for r in context["registry"] if r["name"] == name), None)
    if not row:
        frappe.throw(_("Recurring expense {0} not found for company {1}.").format(name, company))

    label = row.get("expense_name") or name

    if not row.get("expense_account"):
        frappe.throw(_("{0} has no expense account configured.").format(label))

    # This module computed `account_payable` for the very row the screen drew;
    # ignoring it here means the screen greys out Pay while the endpoint accepts
    # the same payment, and it fails deep inside DocType validation instead of
    # saying why. Not overridable — an account outside Indirect Expenses is a
    # configuration error, not a judgement call about this month.
    if not row.get("account_payable"):
        frappe.throw(
            _(
                "{0} posts to {1}, which is outside Indirect Expenses, so it cannot "
                "be paid from this screen. Move the expense to an Indirect Expenses "
                "ledger, or post the payment in Desk."
            ).format(label, row.get("expense_account"))
        )

    # Paused / Ended / off-cadence: the overpay guard alone would refuse this
    # (due is 0, so every pound is excess) but `allow_overpay=1` then posts money
    # against a retired expense with nothing else asked. Name the real reason.
    if not row.get("due_this_month") or flt(row.get("due_amount")) <= 0:
        if not allow:
            frappe.throw(
                _(
                    "{0} is not due in {1} (status {2}). Nothing is owed for this "
                    "period, so this payment would not settle anything. "
                    "Re-send with allow_overpay=1 if you are deliberately paying "
                    "an expense that is not due."
                ).format(label, month_key, row.get("status") or _("unknown"))
            )

    account = _resolve_paying_account(paying_account, company)

    _guard_overpay(
        label,
        row.get("due_amount"),
        row.get("paid_amount"),
        amount,
        allow,
        month_key,
    )

    doc = _create_payment(
        company=company,
        amount=amount,
        reason_account=row["expense_account"],
        paying_account=account,
        payment_date=payment_date,
        remarks=remarks or "{0} — {1}".format(label, month_key),
        expense_kind="Recurring",
        period_month=month_key,
        recurring_expense=name,
    )

    refreshed = _compute_month(month_key, company)
    updated = next((r for r in refreshed["registry"] if r["name"] == name), row)
    return {
        "success": True,
        "payment": _serialize_expense(doc.as_dict()),
        "item": updated,
        "summary": refreshed["summary"],
    }


def _parse_settlement_list(value: Any, key: str) -> List[Dict[str, Any]]:
    """Normalise ``settle_advances`` / ``settle_orders`` to ``[{key, amount}]``.

    Three wire shapes are accepted because three callers exist: a JSON string
    (Frappe hands every HTTP argument over as text), a list of dicts (the
    Flutter sheet, which knows the amount each checkbox settles), and a bare
    list of names — "settle the full open balance of each", which is what a
    curl-driven fix-up or a bench console call will reach for.

    ``amount`` is left as ``None`` when unstated. It is NOT defaulted to zero:
    zero is a refusal ("settle nothing"), while unstated means "all of it", and
    collapsing the two would silently post an empty settlement.
    """
    if value in (None, "", []):
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            frappe.throw(_("{0} must be a JSON list.").format(key))
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, (list, tuple)):
        frappe.throw(_("{0} must be a list.").format(key))

    parsed: List[Dict[str, Any]] = []
    for entry in value:
        if isinstance(entry, str):
            name, requested = entry.strip(), None
        elif isinstance(entry, dict):
            name = str(entry.get(key) or entry.get("name") or "").strip()
            requested = entry.get("amount")
            if requested in (None, ""):
                requested = None
            else:
                requested = flt(requested)
        else:
            frappe.throw(_("{0} entries must be a name or an object.").format(key))
            continue  # pragma: no cover - frappe.throw does not return
        if not name:
            frappe.throw(_("{0} entries must name a document.").format(key))
        parsed.append({key: name, "amount": requested})
    return parsed


def _plan_advance_settlements(
    employee: str, company: str, requests: Sequence[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Validate each requested advance and cap it at its OPEN balance.

    Every row is locked and re-read from the database rather than trusted from
    the payload or from the month context: the open balance is the only thing
    standing between "recover an advance" and "recover the same advance every
    month forever", and it must be read inside the same lock the write happens
    under.
    """
    if not requests:
        # Nothing asked for, nothing read: the ordinary cash-only payslip must
        # not touch HRMS at all, so a bench without it keeps working.
        return []

    plan: List[Dict[str, Any]] = []
    seen: set = set()
    fields = [
        "name",
        "employee",
        "company",
        "advance_account",
        "currency",
        "docstatus",
        "advance_amount",
        "paid_amount",
        "claimed_amount",
        "return_amount",
    ]
    if _advance_has_field(F_SETTLED_AMOUNT):
        fields.append(F_SETTLED_AMOUNT)

    for request in requests:
        name = request.get("name")
        if name in seen:
            frappe.throw(_("Advance {0} is listed twice in this settlement.").format(name))
        seen.add(name)

        # Locked BEFORE the balance is read, so a second settlement of the same
        # advance queues here and then sees the balance this one leaves behind.
        _lock_row(ADVANCE_DOCTYPE, name)
        row = frappe.db.get_value(ADVANCE_DOCTYPE, name, fields, as_dict=True)
        if not row:
            frappe.throw(_("Employee Advance {0} not found.").format(name))
        if int(row.get("docstatus") or 0) != 1:
            frappe.throw(
                _("Employee Advance {0} is not submitted and cannot be settled.").format(name)
            )
        if str(row.get("employee") or "") != employee:
            frappe.throw(
                _("Employee Advance {0} belongs to {1}, not {2}.").format(
                    name, row.get("employee"), employee
                )
            )
        if company and row.get("company") and str(row["company"]) != company:
            frappe.throw(
                _("Employee Advance {0} belongs to company {1}.").format(name, row["company"])
            )
        if not row.get("advance_account"):
            frappe.throw(
                _(
                    "Employee Advance {0} has no advance account, so there is nothing "
                    "to credit. Set it on the advance first."
                ).format(name)
            )

        open_amount = _advance_open_amount(row)
        if open_amount <= MONEY_TOLERANCE:
            frappe.throw(
                _(
                    "Employee Advance {0} has nothing left to recover — it has already "
                    "been claimed, returned or settled."
                ).format(name)
            )

        requested = request.get("amount")
        settled = open_amount if requested is None else flt(requested)
        if settled <= 0:
            frappe.throw(_("Settlement amount for {0} must be greater than zero.").format(name))
        if settled > open_amount + MONEY_TOLERANCE:
            frappe.throw(
                _(
                    "Cannot settle {0} against advance {1}: only {2} is still open. "
                    "Recovering more than the open balance would take the same money "
                    "from the employee twice."
                ).format(_money(settled), name, _money(open_amount))
            )
        # Inside tolerance, the open balance wins: a client that rounded 499.996
        # up to 500 must not leave a fraction of a pound open forever.
        settled = min(settled, open_amount)

        plan.append(
            {
                "name": name,
                "amount": _money(settled),
                "account": row["advance_account"],
                "open_amount": _money(open_amount),
                "settled_before": flt(row.get(F_SETTLED_AMOUNT)),
            }
        )
    return plan


def _plan_order_settlements(
    employee: str, company: str, requests: Sequence[Dict[str, Any]], allowed_invoices: Iterable[str]
) -> List[Dict[str, Any]]:
    """Validate each requested staff invoice and cap it at its outstanding.

    ``allowed_invoices`` is the set the month context already attributed to this
    employee through ``employee_link``. Anything outside it is refused rather
    than joined a second, looser way here — the whole reason the Employee-order
    policy exists is that ``Customer.custom_employee`` alone attributes orders to
    the wrong people.
    """
    if not requests:
        return []

    allowed = set(allowed_invoices or [])
    plan: List[Dict[str, Any]] = []
    seen: set = set()

    for request in requests:
        name = request.get("invoice")
        if name in seen:
            frappe.throw(_("Invoice {0} is listed twice in this settlement.").format(name))
        seen.add(name)
        if name not in allowed:
            frappe.throw(
                _(
                    "Invoice {0} is not an open Employee-purpose order for {1}, so it "
                    "cannot be settled from this salary."
                ).format(name, employee)
            )

        _lock_row("Sales Invoice", name)
        row = frappe.db.get_value(
            "Sales Invoice",
            name,
            ["name", "customer", "company", "debit_to", "outstanding_amount", "docstatus"],
            as_dict=True,
        )
        if not row:
            frappe.throw(_("Sales Invoice {0} not found.").format(name))
        if int(row.get("docstatus") or 0) != 1:
            frappe.throw(_("Sales Invoice {0} is not submitted.").format(name))
        if company and row.get("company") and str(row["company"]) != company:
            frappe.throw(
                _("Sales Invoice {0} belongs to company {1}.").format(name, row["company"])
            )
        if not row.get("debit_to"):
            frappe.throw(
                _("Sales Invoice {0} has no receivable account to credit.").format(name)
            )

        outstanding = flt(row.get("outstanding_amount"))
        if outstanding <= MONEY_TOLERANCE:
            frappe.throw(_("Invoice {0} is already paid.").format(name))

        requested = request.get("amount")
        settled = outstanding if requested is None else flt(requested)
        if settled <= 0:
            frappe.throw(_("Settlement amount for {0} must be greater than zero.").format(name))
        if settled > outstanding + MONEY_TOLERANCE:
            frappe.throw(
                _(
                    "Cannot settle {0} against invoice {1}: only {2} is outstanding."
                ).format(_money(settled), name, _money(outstanding))
            )
        settled = min(settled, outstanding)

        plan.append(
            {
                "invoice": name,
                "amount": _money(settled),
                "account": row["debit_to"],
                "customer": row.get("customer"),
                "outstanding": _money(outstanding),
            }
        )
    return plan


def _post_settlement_journal_entry(
    *,
    company: str,
    employee: str,
    month_key: str,
    salary_account: str,
    advance_plan: Sequence[Dict[str, Any]],
    order_plan: Sequence[Dict[str, Any]],
    posting_date: Optional[str],
    posting_time: Optional[str],
    remarks: Optional[str],
) -> str:
    """The ONE Journal Entry that discharges a salary in kind.

    ``Dr Salary`` for the whole settled amount, then one credit per balance it
    clears. Two details carry all the weight:

    * ``reference_type`` / ``reference_name`` on the credit rows is what makes
      ERPNext reduce a Sales Invoice's ``outstanding_amount`` and reconcile an
      advance. Without them the money still lands on the right account, but the
      invoice stays "Unpaid" forever and the employee is asked for it again.
    * ``party_type`` / ``party`` keep the Employee's and the Customer's
      sub-ledgers right. An advance account carries a per-Employee balance and a
      receivable a per-Customer one; a partyless credit to either leaves the
      account total correct and every party statement wrong.

    Built here rather than by teaching ``JarzExpenseRequest.on_submit`` a second
    shape: that DocType's two-line Journal Entry is what its ``on_cancel``
    reverses, and anything else posted through it would be reversed by a cancel
    that was only ever written for cash.
    """
    from jarz_pos.services.delivery_handling import _strip_je_tag_lookalikes

    total = sum(flt(p["amount"]) for p in advance_plan) + sum(
        flt(p["amount"]) for p in order_plan
    )

    je = frappe.new_doc("Journal Entry")
    je.voucher_type = "Journal Entry"
    je.company = company
    je.posting_date = posting_date or today()

    # The free text is sanitised before it is concatenated with the tag. Every
    # `[JARZ-JE:<type>:<key>]` lookup in this app is a company-wide
    # `user_remark LIKE '%<tag>%'`, so a remark carrying a forged tag satisfies
    # somebody else's idempotency guard — see the long note in
    # `JarzExpenseRequest.on_submit`. Here it would also let one employee's
    # remark be counted as another employee's settlement by `_load_settlements`.
    tag = "[JARZ-JE:{0}:{1}:{2}]".format(SETTLEMENT_JE_TAG, employee, month_key)
    free_text = _strip_je_tag_lookalikes(remarks) or _(
        "Salary settlement {0} {1}"
    ).format(employee, month_key)
    je.user_remark = "{0} {1}".format(tag, free_text)

    apply_ledger_posting_datetime(
        je, join_posting_datetime(je.posting_date, posting_time)
    )

    je.append(
        "accounts",
        {
            "account": salary_account,
            "debit_in_account_currency": flt(total),
            "credit_in_account_currency": 0,
            "user_remark": _("Salary settled against balances"),
        },
    )
    for plan in advance_plan:
        je.append(
            "accounts",
            {
                "account": plan["account"],
                "credit_in_account_currency": flt(plan["amount"]),
                "debit_in_account_currency": 0,
                "party_type": "Employee",
                "party": employee,
                "reference_type": ADVANCE_DOCTYPE,
                "reference_name": plan["name"],
                "is_advance": "Yes",
                "user_remark": _("Advance {0}").format(plan["name"]),
            },
        )
    for plan in order_plan:
        je.append(
            "accounts",
            {
                "account": plan["account"],
                "credit_in_account_currency": flt(plan["amount"]),
                "debit_in_account_currency": 0,
                "party_type": "Customer",
                "party": plan.get("customer"),
                "reference_type": "Sales Invoice",
                "reference_name": plan["invoice"],
                "user_remark": _("Employee order {0}").format(plan["invoice"]),
            },
        )

    je.flags.ignore_permissions = True
    je.insert(ignore_permissions=True)
    je.submit()

    # Stamped AFTER the submit, so a Journal Entry that failed validation never
    # leaves an advance marked as recovered by a document that does not exist.
    if _advance_has_field(F_SETTLED_AMOUNT):
        for plan in advance_plan:
            values = {
                F_SETTLED_AMOUNT: _money(flt(plan.get("settled_before")) + flt(plan["amount"]))
            }
            if _advance_has_field(F_SETTLED_VIA):
                values[F_SETTLED_VIA] = je.name
            frappe.db.set_value(
                ADVANCE_DOCTYPE, plan["name"], values, update_modified=False
            )
    return je.name


@frappe.whitelist()
def pay_salary(
    employee: str,
    month: Optional[str] = None,
    amount: Optional[float] = None,
    paying_account: Optional[str] = None,
    payment_date: Optional[str] = None,
    remarks: Optional[str] = None,
    allow_overpay: Any = 0,
    settle_advances: Any = None,
    settle_orders: Any = None,
) -> Dict[str, Any]:
    """Pay one employee's salary for a month, collapsing the two HRMS entries.

    DEBIT the salary expense account / CREDIT cash-or-bank, in one Journal Entry.
    That is only correct while HRMS payroll is not being run — so this refuses
    outright if a SUBMITTED Salary Slip overlaps the month, because that slip has
    already booked the expense and paying again would post it twice.

    ``amount`` is the CASH handed over and keeps exactly that meaning.
    ``settle_advances`` / ``settle_orders`` are ADDITIONAL discharge: the same
    salary, paid in kind by clearing what the employee already owes. The two
    together are what the month is credited with, which is why the overpay guard
    measures their sum.

    ``amount=0`` with settlements is a legitimate payslip — the whole salary went
    on advances and no cash moves — so the old "amount must be greater than
    zero" applies to the TOTAL discharge, not to the cash half.
    """
    _ensure_manager()
    _require_period_fields()

    employee = (employee or "").strip()
    if not employee:
        frappe.throw(_("Employee is required."))

    amount = flt(amount)
    if amount < 0:
        frappe.throw(_("Amount cannot be negative."))

    advance_requests = _parse_settlement_list(settle_advances, "name")
    order_requests = _parse_settlement_list(settle_orders, "invoice")

    if amount <= 0 and not (advance_requests or order_requests):
        frappe.throw(
            _(
                "Amount must be greater than zero, or an advance or staff order must "
                "be settled."
            )
        )

    allow = _as_bool(allow_overpay)

    # Before every read this payment is decided on — the slip check, the payroll
    # state and the guard. See `_lock_row`.
    _lock_row("Employee", employee)

    month_start, month_end, month_key, company, _currency = _month_context(month, None)

    slips = _submitted_salary_slips([employee], month_start, month_end, strict=True)
    slip = slips.get(employee)
    if slip:
        frappe.throw(
            _(
                "Salary Slip {0} is already submitted for this employee covering {1}. "
                "It has posted the salary expense; paying again here would double it. "
                "Settle it through the HRMS payroll payment instead."
            ).format(slip.get("name"), month_key)
        )

    context = _compute_month(month_key, company)
    payroll = context["payroll"]
    row = next((r for r in payroll["rows"] if r["employee"] == employee), None)

    salary_account = payroll.get("salary_account")
    if not salary_account:
        frappe.throw(
            _(
                "No salary expense account could be resolved. Configure the account on the "
                "Salary Components, or create an Expense ledger named Salary under Indirect Expenses."
            )
        )

    # Only the cash half needs a source of money. Resolving an account for a
    # settlement-only payslip would refuse it for the wrong reason — no cash is
    # leaving any drawer.
    account = _resolve_paying_account(paying_account, company) if amount > 0 else None

    # Planned (and capped) BEFORE the guard, because the guard measures the
    # TOTAL discharge and the plan is what decides that total: asking to settle
    # 5,000 against an advance with 500 open discharges 500, not 5,000.
    advance_plan = _plan_advance_settlements(employee, company, advance_requests)
    order_plan = _plan_order_settlements(
        employee,
        company,
        order_requests,
        [o.get("invoice") for o in ((row or {}).get("orders") or [])],
    )
    settle_total = _money(
        sum(flt(p["amount"]) for p in advance_plan)
        + sum(flt(p["amount"]) for p in order_plan)
    )

    due_amount = flt(row.get("due_amount")) if row else 0.0
    already_paid = flt(row.get("paid_amount")) if row else 0.0
    label = (row.get("employee_name") if row else None) or employee
    if not row:
        label = _("{0} (no Salary Structure Assignment)").format(employee)

    # ── the guard the per-employee figure cannot be ───────────────────────
    # `already_paid` counts app payments linked to THIS employee, and that is by
    # design: salary GL is one company-wide account, so nothing about a posting
    # says whose salary it was. The consequence is that a lump payroll Journal
    # Entry booked in Desk is invisible to the per-row guard — it sees zero for
    # all sixteen employees and would happily pay every one of them a second
    # time. The registry path already folds inferred GL into the number its
    # guard reads (a lone due item absorbs its account's unexplained money);
    # this is the salary equivalent, applied at account level because that is
    # the only level at which the money can honestly be described.
    # Applied to the month's TOTAL rather than fired on any unattributed money
    # at all. A blanket refusal is the wrong shape here: production already
    # carries unattributed salary GL most months (staff pay salaries ad-hoc,
    # naming the person only in a free-text remark), so refusing every payment
    # would make `allow_overpay=1` the normal way to use the screen — and a
    # guard everyone routinely overrides stops guarding anything, including the
    # case it exists for. The real hazard is the company paying MORE salary in a
    # month than it owes, so that is what is measured.
    unattributed = flt(payroll.get("unattributed_gl"))
    payroll_due = flt(payroll.get("due"))
    payroll_linked = flt(payroll.get("paid"))
    # The settlement Journal Entry debits the SAME salary account the cash half
    # does, so it is salary expense for the month exactly like cash is and
    # belongs in this total. Leaving it out would let the month's salary expense
    # be run past the payroll by settling advances instead of paying cash.
    salary_total_after = payroll_linked + unattributed + amount + settle_total
    if (
        unattributed > MONEY_TOLERANCE
        and payroll_due > 0
        and salary_total_after > payroll_due + MONEY_TOLERANCE
        and not allow
    ):
        frappe.throw(
            _(
                "{0} has already been posted to the salary account {1} in {2} without "
                "being credited to any employee — usually a payroll Journal Entry "
                "booked in Desk. Paying {3} now would take the month's salary total to "
                "{4} against a payroll of {5}. Check that entry first; re-send with "
                "allow_overpay=1 if this payment is genuinely additional to it."
            ).format(
                _money(unattributed),
                payroll.get("salary_account_label") or salary_account,
                month_key,
                _money(amount + settle_total),
                _money(salary_total_after),
                _money(payroll_due),
            )
        )

    # The guard measures the TOTAL discharge against what is still owed. Cash and
    # settlement both reduce the same obligation, so counting only the cash half
    # would let a full salary be paid in cash and then paid again in kind.
    _guard_overpay(label, due_amount, already_paid, amount + settle_total, allow, month_key)

    # ── the two halves, one transaction ───────────────────────────────────
    # Nothing commits between them. A cash payment that posts while the
    # settlement fails is the worst outcome available: the advance stays open,
    # the employee has the money, and the company chases them for it again.
    # Frappe rolls the whole request back on any exception raised below, which
    # is why neither half swallows one.
    doc = None
    if amount > 0:
        doc = _create_payment(
            company=company,
            amount=amount,
            reason_account=salary_account,
            paying_account=account,
            payment_date=payment_date,
            remarks=remarks or "{0} — {1} {2}".format(label, _("salary"), month_key),
            expense_kind="Salary",
            period_month=month_key,
            employee=employee,
        )

    settlement = None
    if advance_plan or order_plan:
        posting_date, posting_time = split_posting_datetime(payment_date or today())
        journal_entry = _post_settlement_journal_entry(
            company=company,
            employee=employee,
            month_key=month_key,
            salary_account=salary_account,
            advance_plan=advance_plan,
            order_plan=order_plan,
            posting_date=posting_date,
            posting_time=posting_time,
            remarks=remarks,
        )
        settlement = {
            "journal_entry": journal_entry,
            "total": settle_total,
            "advances": [
                {"name": p["name"], "amount": p["amount"]} for p in advance_plan
            ],
            "orders": [
                {"invoice": p["invoice"], "amount": p["amount"]} for p in order_plan
            ],
        }

    refreshed = _compute_month(month_key, company)
    updated = next(
        (r for r in refreshed["payroll"]["rows"] if r["employee"] == employee), row
    )
    return {
        "success": True,
        "payment": _serialize_expense(doc.as_dict()) if doc else None,
        "settlement": settlement,
        "row": updated,
        "payroll": {
            "due": refreshed["payroll"]["due"],
            "paid": refreshed["payroll"]["paid"],
            "remaining": refreshed["payroll"]["remaining"],
        },
        # `.get`, not `[...]`: every caller that stubs `_compute_month` (and the
        # pay path's own older tests) hands back a context without this key.
        "deductions": refreshed.get("deductions"),
        "summary": refreshed["summary"],
    }


# ── penalties ─────────────────────────────────────────────────────────────


@frappe.whitelist()
def add_employee_penalty(
    employee: str,
    month: Optional[str] = None,
    unit: str = "Days",
    quantity: Any = None,
    amount: Any = None,
    reason: Optional[str] = None,
    penalty_date: Optional[str] = None,
    company: Optional[str] = None,
    allow_overpay: Any = 0,
) -> Dict[str, Any]:
    """Record a penalty against one employee's salary month.

    Posts NO Journal Entry, deliberately. Salary is expensed when it is PAID
    (``Jarz Expense Request`` debits the salary account), so paying less already
    books less expense; a penalty that also posted somewhere would count the
    same reduction twice, and inventing a "penalty income" account would turn a
    deduction into revenue the company never earned.

    The ``day_rate`` is SNAPSHOT onto the document. A raise six months later
    must not silently re-price a penalty already agreed with the employee.
    """
    _ensure_manager()
    _require_period_fields()

    employee = (employee or "").strip()
    if not employee:
        frappe.throw(_("Employee is required."))

    unit = (unit or "Days").strip()
    if unit not in PENALTY_UNITS:
        frappe.throw(_("Penalty unit must be one of {0}.").format(", ".join(PENALTY_UNITS)))

    reason = (reason or "").strip()
    if not reason:
        frappe.throw(
            _(
                "A reason is required. A deduction from someone's pay with no stated "
                "reason cannot be defended to them."
            )
        )

    if not _penalty_doctype_ready():
        frappe.throw(
            _(
                "Employee penalties need the {0} DocType. Run `bench migrate` on this site."
            ).format(PENALTY_DOCTYPE)
        )

    allow = _as_bool(allow_overpay)

    # Before the state the guard below reads, same as `pay_salary`.
    _lock_row("Employee", employee)

    _month_start, _month_end, month_key, company, currency = _month_context(month, company)
    context = _compute_month(month_key, company)
    row = next(
        (r for r in context["payroll"]["rows"] if r["employee"] == employee), None
    )

    gross_due = flt(row.get("gross_due")) if row else 0.0
    day_rate = flt(row.get("day_rate")) if row else 0.0
    already = flt(row.get("penalty_total")) if row else 0.0

    if unit in ("Days", "Half Days"):
        if flt(quantity) <= 0:
            frappe.throw(_("Number of days must be greater than zero."))
        if day_rate <= 0:
            frappe.throw(
                _(
                    "{0} has no Salary Structure Assignment, so a day of their salary "
                    "has no value and a day-based penalty cannot be priced. Enter the "
                    "penalty as an amount of money instead."
                ).format((row or {}).get("employee_name") or employee)
            )
    elif flt(amount) <= 0:
        frappe.throw(_("Penalty amount must be greater than zero."))

    penalty_amount, equivalent_days = _penalty_amounts(unit, quantity, amount, day_rate)

    if gross_due > 0 and already + penalty_amount > gross_due + MONEY_TOLERANCE and not allow:
        frappe.throw(
            _(
                "Penalties for {0} would total {1} against a salary of {2}. "
                "Re-send with allow_overpay=1 if the whole month is genuinely forfeit."
            ).format(month_key, _money(already + penalty_amount), _money(gross_due))
        )

    doc = frappe.get_doc(
        {
            "doctype": PENALTY_DOCTYPE,
            "employee": employee,
            "company": company,
            "penalty_date": penalty_date or today(),
            "period_month": month_key,
            "unit": unit,
            "quantity": flt(quantity),
            "amount": _money(penalty_amount),
            "day_rate": _money(day_rate),
            "equivalent_days": flt(equivalent_days, 2),
            "currency": currency,
            "reason": reason,
        }
    )
    doc.flags.ignore_permissions = True
    doc.insert(ignore_permissions=True)
    doc.submit()
    doc.reload()

    refreshed = _compute_month(month_key, company)
    updated = next(
        (r for r in refreshed["payroll"]["rows"] if r["employee"] == employee), row
    )
    return {
        "success": True,
        "penalty": _serialize_penalty(doc.as_dict()),
        "row": updated,
        "deductions": refreshed.get("deductions"),
        "summary": refreshed["summary"],
    }


@frappe.whitelist()
def cancel_employee_penalty(name: str, reason: str) -> Dict[str, Any]:
    """Undo a penalty. Cancels the document; the salary goes back up.

    Refused once the penalty is ``settled``: at that point the reduced salary
    has already been paid, and un-deducting it here would silently re-open a
    month that was settled with the employee. The way back from there is to pay
    the difference, which leaves a document saying so.
    """
    _ensure_manager()
    _require_period_fields()  # reaches `_compute_month`, which needs the columns

    name = (name or "").strip()
    if not name:
        frappe.throw(_("Penalty is required."))
    reason = (reason or "").strip()
    if not reason:
        frappe.throw(_("A reason is required to cancel a penalty."))

    doc = frappe.get_doc(PENALTY_DOCTYPE, name)
    if int(doc.docstatus or 0) == 2:
        frappe.throw(_("Penalty {0} is already cancelled.").format(name))
    if _as_bool(getattr(doc, "settled", 0)):
        frappe.throw(
            _(
                "Penalty {0} has already been settled with {1} and cannot be cancelled. "
                "Pay the difference instead, so the correction is a document of its own."
            ).format(name, getattr(doc, "settled_via", None) or _("a payment"))
        )

    employee = doc.employee
    month_key = doc.period_month
    company = doc.company

    doc.flags.ignore_permissions = True
    doc.cancel()
    try:
        doc.add_comment("Comment", _("Cancelled: {0}").format(reason))
    except Exception:
        # A missing audit comment must not undo the cancellation the caller
        # asked for; the docstatus change is the outcome that matters.
        _log("monthly_expenses: penalty cancel comment")

    context = _compute_month(month_key, company)
    updated = next(
        (r for r in context["payroll"]["rows"] if r["employee"] == employee), None
    )
    return {
        "success": True,
        "penalty": name,
        "row": updated,
        "deductions": context.get("deductions"),
        "summary": context["summary"],
    }


def _validate_expense_ledger(account: str, company: Optional[str]) -> str:
    """The registry's expense account must be a bookable Expense ledger."""
    account = (account or "").strip()
    if not account:
        frappe.throw(_("Expense account is required."))
    row = frappe.db.get_value(
        "Account", account, ["name", "root_type", "is_group", "company"], as_dict=True
    )
    if not row:
        frappe.throw(_("Account not found: {0}").format(account))
    if int(row.get("is_group") or 0):
        frappe.throw(_("Expense account must be a ledger account, not a group."))
    if (row.get("root_type") or "").lower() != "expense":
        frappe.throw(_("Expense account must be an account of root type Expense."))
    if company and row.get("company") and row["company"] != company:
        frappe.throw(
            _("Expense account {0} belongs to company {1}, not {2}.").format(
                account, row["company"], company
            )
        )
    return row["name"]


_EDITABLE_REGISTRY_FIELDS = (
    "expense_name",
    "category",
    "status",
    "supplier",
    "amount",
    "currency",
    "frequency",
    "day_of_month",
    "expense_account",
    "cost_center",
    "default_paying_account",
    "start_date",
    "end_date",
    "notes",
)


@frappe.whitelist()
def save_recurring_expense(payload: Optional[str] = None, **kwargs) -> Dict[str, Any]:
    """Create a registry item when ``name`` is absent, otherwise update it."""
    _ensure_manager()
    # This endpoint SAVES before it recomputes, and `_compute_month` selects the
    # period columns. Without this check an unmigrated site takes the write,
    # then dies on `Unknown column 'period_month'` — the user sees a raw
    # OperationalError and their save rolled back with it. Refuse up front.
    _require_period_fields()

    data = _parse_filters(payload)
    data.update(kwargs)

    name = str(data.get("name") or "").strip()
    company = str(data.get("company") or "").strip() or _default_company()

    if data.get("status") and data["status"] not in REGISTRY_STATUSES:
        frappe.throw(
            _("Status must be one of {0}.").format(", ".join(REGISTRY_STATUSES))
        )

    frequency = str(data.get("frequency") or "").strip()
    if frequency and frequency not in FREQUENCY_MONTHS:
        frappe.throw(
            _("Frequency must be one of {0}.").format(", ".join(FREQUENCY_MONTHS))
        )

    if "day_of_month" in data:
        try:
            data["day_of_month"] = _normalized_day_of_month(data.get("day_of_month"))
        except ValueError as exc:
            frappe.throw(_(str(exc)))

    if name:
        doc = frappe.get_doc("Jarz Recurring Expense", name)
    else:
        doc = frappe.new_doc("Jarz Recurring Expense")
        doc.company = company
        doc.status = data.get("status") or "Active"
        doc.frequency = frequency or "Monthly"

    if data.get("company"):
        doc.company = company

    for field in _EDITABLE_REGISTRY_FIELDS:
        if field in data:
            setattr(doc, field, data.get(field))

    if not doc.expense_name:
        frappe.throw(_("Expense name is required."))
    if flt(doc.amount) <= 0:
        frappe.throw(_("Amount per occurrence must be greater than zero."))
    if not doc.start_date:
        frappe.throw(_("Start date is required."))

    doc.expense_account = _validate_expense_ledger(doc.expense_account, doc.company)

    # Recomputed here as well as in the DocType so the value returned to the
    # caller is never a stale read-only field from before this save.
    doc.monthly_equivalent = _monthly_equivalent(doc.amount, doc.frequency)

    doc.flags.ignore_permissions = True
    if name:
        doc.save(ignore_permissions=True)
    else:
        doc.insert(ignore_permissions=True)
    doc.reload()

    context = _compute_month(data.get("month"), doc.company)
    item = next((r for r in context["registry"] if r["name"] == doc.name), None)
    return {"success": True, "name": doc.name, "item": item, "summary": context["summary"]}


@frappe.whitelist()
def set_recurring_expense_status(name: str, status: str) -> Dict[str, Any]:
    """Flip a registry item between Active / Paused / Ended."""
    _ensure_manager()
    _require_period_fields()  # reaches `_compute_month`, which needs the columns

    name = (name or "").strip()
    if not name:
        frappe.throw(_("Recurring expense is required."))
    status = (status or "").strip()
    if status not in REGISTRY_STATUSES:
        frappe.throw(_("Status must be one of {0}.").format(", ".join(REGISTRY_STATUSES)))

    doc = frappe.get_doc("Jarz Recurring Expense", name)
    doc.status = status
    doc.flags.ignore_permissions = True
    doc.save(ignore_permissions=True)
    doc.reload()

    context = _compute_month(None, doc.company)
    item = next((r for r in context["registry"] if r["name"] == doc.name), None)
    return {"success": True, "name": doc.name, "status": doc.status, "item": item}


@frappe.whitelist()
def cancel_expense_payment(name: str, reason: str) -> Dict[str, Any]:
    """Reverse a payment made from this screen.

    Delegates to ``expenses.cancel_expense`` — that is the only code path that
    knows how to unwind the Journal Entry, and duplicating it would eventually
    produce a "Cancelled" request sitting next to a live expense entry.
    """
    _ensure_manager()
    _require_period_fields()  # reads period columns, then recomputes the month

    name = (name or "").strip()
    if not name:
        frappe.throw(_("Payment is required."))

    row = frappe.db.get_value(
        "Jarz Expense Request",
        name,
        ["name", "expense_kind", "recurring_expense", "employee", "period_month", "company"],
        as_dict=True,
    )
    if not row:
        frappe.throw(_("Payment {0} not found.").format(name))

    result = cancel_expense(name, reason)

    context = _compute_month(row.get("period_month"), row.get("company"))
    item = None
    payroll_row = None
    if row.get("recurring_expense"):
        item = next(
            (r for r in context["registry"] if r["name"] == row["recurring_expense"]), None
        )
    if row.get("employee"):
        payroll_row = next(
            (r for r in context["payroll"]["rows"] if r["employee"] == row["employee"]),
            None,
        )

    return {
        "success": True,
        "payment": (result or {}).get("expense"),
        "item": item,
        "row": payroll_row,
        "summary": context["summary"],
    }
