"""Recurring (time-based) fees owed to a Delivery Partner.

Some partners charge a fixed fee per period on top of — and independent of — the
per-trip fee: Deliverk charges 50 EGP every day, whether or not a single order
went out with them that day. Other partners may bill weekly or monthly. That fee
is a real debt from the day it is incurred, so it is ACCRUED per period rather
than typed in as a "fixed charge" at payment time, where it depended on someone
remembering it and the payable under-stated what we owed all week.

    DR  Freight & Forwarding Charges   (fee)
    CR  Partner settlement_account     (fee)   [Supplier party]

— the same two lines as a per-trip fee accrual, so the partner payable keeps its
one-directional shape: credited as debts arise, debited by the weekly transfer.

Each period becomes one ``Delivery Partner Fee Accrual`` row. Those rows are
listed next to the unbilled trips on the weekly settlement screen (Desk and the
app) and are ticked and cleared the same way; see ``api/delivery_partners.py``.

Periods are anchored on ``recurring_fee_start_date``:

  * Daily   — every calendar day.
  * Weekly  — 7-day blocks counted from the start date.
  * Monthly — same day-of-month as the start date (a start on the 31st falls
    back to the month's last day, computed from the start each time so it never
    drifts to the 28th).

A period is accrued on its first day, dated that day. The job is idempotent and
catches up: a start date in the past, or a missed run, is filled on the next run.
The database refuses a second row for one partner/period, so two workers cannot
both book a day.
"""
from __future__ import annotations

import datetime

import frappe
from frappe.utils import add_days, add_months, flt, getdate, nowdate

from jarz_pos.services.delivery_handling import (
    _find_je_by_tag,
    _get_partner_settlement_account,
    _je_dedup_tag,
    _tag_journal_entry,
    get_delivery_partner_supplier,
)
from jarz_pos.utils.account_utils import get_freight_expense_account, validate_account_exists

ACCRUAL_DOCTYPE = "Delivery Partner Fee Accrual"

#: Tag type on the accrual Journal Entry; the key is ``{partner}:{period_start}``.
PARTNER_RECURRING_FEE_JE_TAG_TYPE = "PARTNER_RECURRING_FEE"

FREQUENCIES = ("Daily", "Weekly", "Monthly")

#: Hard ceiling on periods posted for one partner in one run. A start date typed
#: years in the past (or a typo in the year) must not post thousands of entries
#: in one go; the remainder is picked up on following runs.
MAX_PERIODS_PER_RUN = 120


# ---------------------------------------------------------------------------
# Period arithmetic (pure — unit-tested without a site)
# ---------------------------------------------------------------------------

def period_bounds(start_date, frequency: str, index: int) -> tuple[datetime.date, datetime.date]:
    """Return ``(period_start, period_end)`` of the *index*-th period (0-based).

    Monthly periods are computed from the ANCHOR each time rather than by
    chaining ``add_months`` from the previous period, so a contract starting on
    the 31st bills the 31st of every long month instead of drifting to the 28th
    after February.
    """
    anchor = getdate(start_date)
    if frequency == "Daily":
        ps = add_days(anchor, index)
        return getdate(ps), getdate(ps)
    if frequency == "Weekly":
        ps = add_days(anchor, 7 * index)
        return getdate(ps), getdate(add_days(ps, 6))
    if frequency == "Monthly":
        ps = getdate(add_months(anchor, index))
        nxt = getdate(add_months(anchor, index + 1))
        return ps, getdate(add_days(nxt, -1))
    frappe.throw(f"Unknown recurring fee frequency: {frequency!r}")


def due_periods(
    start_date,
    frequency: str,
    up_to,
    end_date=None,
    existing: list[tuple] | None = None,
    limit: int = MAX_PERIODS_PER_RUN,
) -> list[tuple[datetime.date, datetime.date]]:
    """Periods that have started by *up_to* and are not yet accrued.

    A period is due on its first day. It is skipped when it OVERLAPS any
    already-accrued ``(period_start, period_end)`` in *existing* rather than when
    its start matches one exactly, because an edit to the start date or the
    frequency re-anchors every later period: exact matching would then bill the
    same days twice under a new grid. The price of that guarantee: a new-grid
    period that overlaps ANY accrued day is skipped whole, so when changing the
    frequency set the new start date to the first day not yet accrued (the
    Delivery Partner form says so) — otherwise the straddling period is lost.

    *end_date* (inclusive) stops the contract: no period starting after it is due.
    """
    if not start_date or frequency not in FREQUENCIES:
        return []
    up_to = getdate(up_to)
    last_day = min(up_to, getdate(end_date)) if end_date else up_to
    booked = [(getdate(s), getdate(e)) for s, e in (existing or [])]

    out: list[tuple[datetime.date, datetime.date]] = []
    index = 0
    while len(out) < limit:
        ps, pe = period_bounds(start_date, frequency, index)
        if ps > last_day:
            break
        index += 1
        if any(ps <= be and bs <= pe for bs, be in booked):
            continue
        out.append((ps, pe))
    return out


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------

def _partner_company(settlement_account: str) -> str:
    company = frappe.db.get_value("Account", settlement_account, "company")
    if not company:
        frappe.throw(f"Cannot determine the company of account {settlement_account}.")
    return company


def _fee_label(frequency: str, period_start, period_end) -> str:
    if frequency == "Daily":
        return f"Daily fee {period_start}"
    return f"{frequency} fee {period_start} to {period_end}"


def _post_accrual_je(*, delivery_partner, company, amount, frequency, period_start, period_end) -> str:
    """DR Freight / CR partner payable for one period. Idempotent on its tag."""
    key = f"{delivery_partner}:{period_start}"
    tag = _je_dedup_tag(key, PARTNER_RECURRING_FEE_JE_TAG_TYPE)
    existing = _find_je_by_tag(company, tag)
    if existing:
        return existing

    partner_acc = _get_partner_settlement_account(delivery_partner)
    supplier = get_delivery_partner_supplier(delivery_partner)
    freight_acc = get_freight_expense_account(company)
    for acc in (partner_acc, freight_acc):
        validate_account_exists(acc)

    label = _fee_label(frequency, period_start, period_end)
    je = frappe.new_doc("Journal Entry")
    je.voucher_type = "Journal Entry"
    je.posting_date = period_start
    je.company = company
    je.title = f"Partner {label} – {delivery_partner}"
    _tag_journal_entry(
        je, key, PARTNER_RECURRING_FEE_JE_TAG_TYPE, f"{label} owed to {delivery_partner}"
    )
    je.append("accounts", {
        "account": freight_acc,
        "debit_in_account_currency": amount,
        "credit_in_account_currency": 0,
        "user_remark": label,
    })
    je.append("accounts", {
        "account": partner_acc,
        "party_type": "Supplier",
        "party": supplier,
        "debit_in_account_currency": 0,
        "credit_in_account_currency": amount,
        "user_remark": label,
    })
    je.save(ignore_permissions=True)
    je.submit()
    return je.name


def accrue_partner(delivery_partner: str, up_to=None) -> list[str]:
    """Accrue every due period for one partner. Returns the new accrual names.

    Each period is its own savepoint: one bad day (a closed accounting period, a
    duplicate lost to a concurrent worker) is rolled back and logged on its own and
    does not take the rest of the catch-up with it — a start date typed inside a
    closed period must not stop today's fee from being booked. The failed day is
    retried on the next run. The caller commits.
    """
    dp = frappe.db.get_value(
        "Delivery Partner",
        delivery_partner,
        [
            "name", "is_active", "settlement_account", "recurring_fee_amount",
            "recurring_fee_frequency", "recurring_fee_start_date", "recurring_fee_end_date",
        ],
        as_dict=True,
    )
    if not dp or not dp.is_active:
        return []
    amount = round(flt(dp.recurring_fee_amount), 2)
    if amount <= 0 or not dp.recurring_fee_frequency or not dp.recurring_fee_start_date:
        return []
    if not dp.settlement_account:
        frappe.throw(f"Delivery Partner {delivery_partner} has no settlement_account.")

    company = _partner_company(dp.settlement_account)
    existing = frappe.get_all(
        ACCRUAL_DOCTYPE,
        filters={"delivery_partner": delivery_partner},
        fields=["period_start", "period_end"],
    )
    periods = due_periods(
        dp.recurring_fee_start_date,
        dp.recurring_fee_frequency,
        up_to or nowdate(),
        end_date=dp.recurring_fee_end_date,
        existing=[(r.period_start, r.period_end) for r in existing],
    )

    created: list[str] = []
    for ps, pe in periods:
        sp = f"dpfee_{frappe.generate_hash(length=8)}"
        frappe.db.savepoint(sp)
        try:
            # Claim the period FIRST: the unique (partner, period_start) index makes
            # a concurrent worker fail here, before it has posted any money.
            row = frappe.get_doc({
                "doctype": ACCRUAL_DOCTYPE,
                "delivery_partner": delivery_partner,
                "frequency": dp.recurring_fee_frequency,
                "amount": amount,
                "company": company,
                "period_start": ps,
                "period_end": pe,
            })
            row.insert(ignore_permissions=True)
            je = _post_accrual_je(
                delivery_partner=delivery_partner,
                company=company,
                amount=amount,
                frequency=dp.recurring_fee_frequency,
                period_start=ps,
                period_end=pe,
            )
            frappe.db.set_value(ACCRUAL_DOCTYPE, row.name, "journal_entry", je, update_modified=False)
            created.append(row.name)
        except (frappe.DuplicateEntryError, frappe.UniqueValidationError):
            # Another worker claimed this period between our read and insert.
            frappe.db.rollback(save_point=sp)
        except Exception:
            frappe.db.rollback(save_point=sp)
            try:
                frappe.log_error(
                    title=f"Partner recurring fee not accrued: {delivery_partner} {ps}",
                    message=frappe.get_traceback(),
                )
            except Exception:
                pass
    return created


def run_partner_recurring_fees(up_to=None) -> dict:
    """Scheduler entry point. Never raises; one partner's failure is logged alone."""
    summary: dict[str, object] = {}
    try:
        partners = frappe.get_all(
            "Delivery Partner",
            filters={"is_active": 1, "recurring_fee_amount": [">", 0]},
            pluck="name",
        )
    except Exception:
        # Code deployed ahead of its migrate: the column is not there yet.
        return summary
    for name in partners:
        try:
            created = accrue_partner(name, up_to=up_to)
            frappe.db.commit()
            summary[name] = len(created)
        except Exception:
            frappe.db.rollback()
            summary[name] = "error"
            try:
                frappe.log_error(
                    title=f"Partner recurring fee accrual failed: {name}",
                    message=frappe.get_traceback(),
                )
            except Exception:
                pass
    return summary


# ---------------------------------------------------------------------------
# Reads used by the settlement endpoints
# ---------------------------------------------------------------------------

def _accruals_available() -> bool:
    """False until ``bench migrate`` has created the table.

    The settlement endpoints call into here on every request, and code routinely
    runs ahead of its migrate (a deploy mid-flight, the CI gate that runs before
    migrate). A missing table means "no recurring fees yet", not an error.
    """
    try:
        return bool(frappe.db.table_exists(ACCRUAL_DOCTYPE))
    except Exception:
        return False


def unsettled_accruals(delivery_partner: str | None = None, names: list[str] | None = None) -> list[dict]:
    """Unpaid periods whose accrual Journal Entry is still SUBMITTED.

    Cancelling a period's accrual entry is how a day is waived: the payable was
    never credited, so paying it would debit the partner account with nothing
    behind it. Such a row stays (it still blocks re-accruing that period) but is
    no longer owed.
    """
    if not _accruals_available():
        return []
    if names is not None and not names:
        return []
    conds = ["a.settled = 0", "je.docstatus = 1"]
    params: dict = {}
    if delivery_partner:
        conds.append("a.delivery_partner = %(dp)s")
        params["dp"] = delivery_partner
    if names is not None:
        conds.append("a.name IN %(names)s")
        params["names"] = tuple(names)
    return frappe.db.sql(
        f"""SELECT a.name, a.delivery_partner, a.frequency, a.amount,
                   a.period_start, a.period_end
            FROM `tabDelivery Partner Fee Accrual` a
            JOIN `tabJournal Entry` je ON je.name = a.journal_entry
            WHERE {' AND '.join(conds)}
            ORDER BY a.period_start ASC""",
        params,
        as_dict=True,
    )


def accrual_label(row) -> str:
    return _fee_label(row.get("frequency"), row.get("period_start"), row.get("period_end"))


def split_accrual_names(names: list[str]) -> tuple[list[str], list[str]]:
    """Split a settlement's ticked names into ``(trip names, accrual names)``.

    The screens send both kinds in one list; which table a name lives in is what
    tells them apart.
    """
    if not names or not _accruals_available():
        return list(names or []), []
    fee_names = set(frappe.get_all(ACCRUAL_DOCTYPE, filters={"name": ["in", names]}, pluck="name"))
    return [n for n in names if n not in fee_names], [n for n in names if n in fee_names]


def lock_accruals_for_settlement(delivery_partner: str, names: list[str] | None) -> list[dict]:
    """Lock and return the unsettled accruals about to be paid.

    ``names=None`` means every unsettled period of the partner. The rows are read
    UNDER the lock: a read taken before ``FOR UPDATE`` is a stale snapshot, and two
    managers settling at once must not both clear the same day's fee.
    """
    candidates = unsettled_accruals(delivery_partner, names=names)
    if not candidates:
        return []
    return frappe.db.sql(
        """SELECT a.name, a.amount, a.period_start
           FROM `tabDelivery Partner Fee Accrual` a
           JOIN `tabJournal Entry` je ON je.name = a.journal_entry AND je.docstatus = 1
           WHERE a.name IN %(n)s AND a.settled = 0 AND a.delivery_partner = %(dp)s
           ORDER BY a.period_start
           FOR UPDATE""",
        {"n": tuple(r["name"] for r in candidates), "dp": delivery_partner},
        as_dict=True,
    )


def mark_accruals_settled(names: list[str], settlement_je: str, settled_on) -> None:
    for name in names:
        frappe.db.set_value(
            ACCRUAL_DOCTYPE,
            name,
            {"settled": 1, "settlement_je": settlement_je, "settled_on": settled_on},
            update_modified=False,
        )
