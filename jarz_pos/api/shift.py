from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from html import unescape
from types import SimpleNamespace
from typing import Any

import frappe
from frappe import _
from frappe.utils import flt, nowdate, now_datetime, get_datetime


from erpnext.accounts.doctype.pos_closing_entry.pos_closing_entry import (
    make_closing_entry_from_opening,
)
from erpnext.accounts.utils import get_balance_on
from jarz_pos.constants import ACCOUNTS, QUERY_LIMITS, WS_EVENTS


def _assert_user_has_profile_access(user: str, pos_profile: str):
    from jarz_pos.utils.validation_utils import assert_pos_profile_enabled
    assert_pos_profile_enabled(pos_profile)

    has_access = frappe.db.exists(
        "POS Profile User",
        {
            "parent": pos_profile,
            "user": user,
        },
    )
    if not has_access:
        frappe.throw(_("You are not allowed to use POS Profile {0}").format(pos_profile), frappe.PermissionError)


def _get_mode_of_payment_account(mode_of_payment: str, company: str) -> str | None:
    if not mode_of_payment or not company:
        return None

    account = frappe.db.get_value(
        "Mode of Payment Account",
        {
            "parent": mode_of_payment,
            "company": company,
        },
        "default_account",
    )
    if account:
        return account

    account = frappe.db.get_value(
        "Mode of Payment Account",
        {
            "parent": mode_of_payment,
        },
        "default_account",
    )
    return account


def _get_account_balance(account: str | None, company: str) -> float:
    if not account:
        return 0.0
    try:
        return flt(get_balance_on(account=account, company=company, date=nowdate()) or 0)
    except Exception:
        return 0.0


def _ensure_mode_of_payment_account(mode_of_payment: str, company: str, default_account: str | None):
    if not mode_of_payment or not company or not default_account:
        return

    existing = frappe.db.get_value(
        "Mode of Payment Account",
        {
            "parent": mode_of_payment,
            "company": company,
        },
        ["name", "default_account"],
        as_dict=True,
    )

    if existing:
        if existing.default_account != default_account:
            frappe.db.set_value(
                "Mode of Payment Account",
                existing.name,
                "default_account",
                default_account,
            )
        return

    row = frappe.get_doc(
        {
            "doctype": "Mode of Payment Account",
            "parent": mode_of_payment,
            "parenttype": "Mode of Payment",
            "parentfield": "accounts",
            "company": company,
            "default_account": default_account,
        }
    )
    row.insert(ignore_permissions=True)


def _get_profile_primary_mode_of_payment(profile) -> str | None:
    payments = profile.get("payments") or []
    if not payments:
        return None

    for row in payments:
        if row.get("default") and row.get("mode_of_payment"):
            return row.get("mode_of_payment")

    return (payments[0] or {}).get("mode_of_payment")


def _normalize_shift_close_error_message(raw: Any) -> str:
    if raw is None:
        return _("Unknown error while closing the shift.")

    if isinstance(raw, dict):
        raw = raw.get("message") or raw.get("exc") or raw.get("exception") or json.dumps(raw)
    elif isinstance(raw, list):
        parts = [_normalize_shift_close_error_message(item) for item in raw]
        parts = [part for part in parts if part]
        return "; ".join(parts) if parts else _("Unknown error while closing the shift.")

    text = str(raw).strip()
    if not text:
        return _("Unknown error while closing the shift.")

    try:
        parsed = json.loads(text)
    except Exception:
        parsed = None

    if parsed is not None and parsed != raw:
        return _normalize_shift_close_error_message(parsed)

    text = unescape(text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or _("Unknown error while closing the shift.")


def _get_primary_traceback_error_message(traceback_text: str | None) -> str | None:
    if not traceback_text:
        return None

    for raw_line in traceback_text.splitlines():
        line = raw_line.strip()
        if not line or ": " not in line:
            continue
        if line.startswith("Traceback") or line.startswith("File ") or line.startswith("During handling"):
            continue
        if not re.match(r"^[A-Za-z0-9_.]+(?:Error|Exception): ", line):
            continue

        message = line.split(": ", 1)[1].strip()
        if message:
            return _normalize_shift_close_error_message(message)

    return None


def _get_shift_close_error_message(exc: Exception, closing=None, traceback_text: str | None = None) -> str:
    error_value: Any = None

    primary_traceback_message = _get_primary_traceback_error_message(traceback_text)
    if primary_traceback_message:
        return primary_traceback_message

    if closing is not None:
        error_value = getattr(closing, "error_message", None)
        if not error_value and getattr(closing, "name", None) and frappe.db.exists("POS Closing Entry", closing.name):
            error_value = frappe.db.get_value("POS Closing Entry", closing.name, "error_message")

    if not error_value and frappe.message_log:
        error_value = frappe.message_log[-1]

    if not error_value:
        error_value = str(exc)

    return _normalize_shift_close_error_message(error_value)


def _normalize_closing_balances_payload(
    closing_balances: list[dict[str, Any]] | dict[str, Any] | str | None,
) -> list[dict[str, Any]]:
    if closing_balances is None:
        return []

    payload = closing_balances
    if isinstance(payload, str):
        text = payload.strip()
        if not text:
            return []
        try:
            payload = json.loads(text)
        except Exception:
            frappe.throw(_("Invalid closing balances payload."))

    if isinstance(payload, dict):
        payload = [payload]

    if not isinstance(payload, list):
        frappe.throw(_("Invalid closing balances payload."))

    normalized: list[dict[str, Any]] = []
    for row in payload:
        if not isinstance(row, dict):
            frappe.throw(_("Invalid closing balances row."))
        normalized.append(row)

    return normalized


def _normalize_opening_balances_payload(
    opening_balances: list[dict[str, Any]] | dict[str, Any] | str | None,
) -> list[dict[str, Any]]:
    if opening_balances is None:
        return []

    payload = opening_balances
    if isinstance(payload, str):
        text = payload.strip()
        if not text:
            return []
        try:
            payload = json.loads(text)
        except Exception:
            frappe.throw(_("Invalid opening balances payload."))

    if isinstance(payload, dict):
        payload = [payload]

    if not isinstance(payload, list):
        frappe.throw(_("Invalid opening balances payload."))

    normalized: list[dict[str, Any]] = []
    for row in payload:
        if not isinstance(row, dict):
            frappe.throw(_("Invalid opening balances row."))
        normalized.append(row)

    return normalized


def _get_required_cash_count(row: dict[str, Any], fieldname: str, label: str) -> float:
    mode = (row.get("mode_of_payment") or _("this payment method")).strip()
    if fieldname not in row or row.get(fieldname) is None:
        frappe.throw(_("{0} is required for {1}.").format(label, mode))

    raw_value = row.get(fieldname)
    if isinstance(raw_value, str) and not raw_value.strip():
        frappe.throw(_("{0} is required for {1}.").format(label, mode))

    try:
        amount = Decimal(str(raw_value).strip())
    except (InvalidOperation, ValueError):
        frappe.throw(_("{0} must be a valid number for {1}.").format(label, mode))

    if not amount.is_finite():
        frappe.throw(_("{0} must be a valid number for {1}.").format(label, mode))

    if amount < 0:
        frappe.throw(_("{0} cannot be negative for {1}.").format(label, mode))

    return flt(str(amount))


def _resolve_pos_profile_account(company: str, pos_profile: str, branch: str | None, mode_of_payment: str | None) -> str | None:
    # Required by business flow: use the account named after the POS Profile.
    if frappe.db.exists("Account", {"company": company, "is_group": 0, "account_name": pos_profile}):
        return frappe.db.get_value("Account", {"company": company, "is_group": 0, "account_name": pos_profile}, "name")

    if frappe.db.exists("Account", {"company": company, "is_group": 0, "name": pos_profile}):
        return pos_profile

    return None


def _get_employee_for_user(user: str) -> dict[str, Any] | None:
    employee = frappe.db.get_value(
        "Employee",
        {"user_id": user},
        ["name", "employee_name", "branch"],
        as_dict=True,
    )
    if not employee:
        return None

    # Read shift requirement from User doctype, not Employee
    employee["require_pos_shift"] = bool(
        int(frappe.db.get_value("User", user, "custom_require_pos_shift") or 0)
    )
    return employee


def _serialize_opening_entry(opening) -> dict[str, Any]:
    user_fullname = frappe.db.get_value("User", opening.user, "full_name") or ""
    employee_name = frappe.db.get_value("Employee", {"user_id": opening.user}, "employee_name") or ""
    return {
        "name": opening.name,
        "status": opening.status,
        "user": opening.user,
        "user_full_name": user_fullname,
        "employee_name": employee_name,
        "company": opening.company,
        "pos_profile": opening.pos_profile,
        "period_start_date": opening.period_start_date,
        "period_end_date": opening.period_end_date,
        "balance_details": [
            {
                "mode_of_payment": row.mode_of_payment,
            }
            for row in (opening.balance_details or [])
        ],
    }


def _get_latest_opening_entry(user: str | None = None, pos_profile: str | None = None) -> dict[str, Any] | None:
    filters: dict[str, Any] = {
        "status": "Open",
        "docstatus": 1,
    }
    if user:
        filters["user"] = user
    if pos_profile:
        filters["pos_profile"] = pos_profile

    entries = frappe.get_all(
        "POS Opening Entry",
        filters=filters,
        fields=["name"],
        order_by="period_start_date desc, modified desc",
        limit=1,
    )

    if not entries:
        return None

    opening = frappe.get_doc("POS Opening Entry", entries[0]["name"])
    return _serialize_opening_entry(opening)


@frappe.whitelist(allow_guest=False)
def get_active_shift(pos_profile: str | None = None):
    """Return the active (Open) shift for the given POS Profile.

    Shift logic is scoped entirely to the POS Profile – shifts on other
    profiles are irrelevant and never returned.  When *pos_profile* is not
    supplied the function returns ``None``.

    A user may have open shifts on multiple profiles simultaneously; only
    the shift matching *pos_profile* is considered.

    The returned dict includes an ``is_current_user`` flag:
    * ``1`` – the calling user owns the shift.
    * ``0`` – another user owns the shift.
    """
    if not pos_profile:
        return None

    user = frappe.session.user

    # Find any open shift on this profile, regardless of owner.
    profile_shift = _get_latest_opening_entry(pos_profile=pos_profile)
    if not profile_shift:
        return None

    profile_shift["is_current_user"] = int(profile_shift.get("user") == user)
    return profile_shift


@frappe.whitelist(allow_guest=False)
def get_shift_payment_methods(pos_profile: str):
    if not pos_profile:
        frappe.throw(_("POS Profile is required"))

    user = frappe.session.user
    _assert_user_has_profile_access(user, pos_profile)

    profile = frappe.get_doc("POS Profile", pos_profile)
    company = profile.company
    branch = getattr(profile, "branch", None)
    mode = _get_profile_primary_mode_of_payment(profile)
    if not mode:
        frappe.throw(_("POS Profile {0} has no payment methods configured").format(pos_profile))

    account = _resolve_pos_profile_account(company, pos_profile, branch, mode)
    if not account:
        frappe.throw(
            _("No account named as POS Profile {0} was found in company {1}.").format(pos_profile, company)
        )

    try:
        _ensure_mode_of_payment_account(mode, company, account)
    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            "jarz_pos.shift.ensure_mode_of_payment_account",
        )

    return [
        {
            "mode_of_payment": mode,
            "account": account,
            "company": company,
            "branch": branch,
            "amounts_hidden": 1,
        }
    ]


@frappe.whitelist(allow_guest=False)
def start_shift(pos_profile: str, opening_balances: list[dict[str, Any]] | None = None):
    user = frappe.session.user

    if not pos_profile:
        frappe.throw(_("POS Profile is required"))

    _assert_user_has_profile_access(user, pos_profile)

    # Only check THIS profile for an existing open shift.
    # Users may have open shifts on other profiles – that is allowed.
    profile_open = _get_latest_opening_entry(pos_profile=pos_profile)
    if profile_open:
        if profile_open.get("user") == user:
            frappe.throw(
                _("You already have an open shift on this profile: {0}").format(profile_open["name"]),
                title=_("Shift Already Open"),
            )
        else:
            opener = (
                profile_open.get("employee_name")
                or profile_open.get("user_full_name")
                or profile_open.get("user", "Unknown")
            )
            frappe.throw(
                _("POS Profile {0} already has an open shift started by {1}. "
                  "That shift must be closed first.").format(pos_profile, opener),
                title=_("Shift Blocked"),
            )

    company = frappe.db.get_value("POS Profile", pos_profile, "company")
    if not company:
        frappe.throw(_("POS Profile {0} was not found").format(pos_profile))

    profile_account = _resolve_pos_profile_account(company, pos_profile, None, None)
    if not profile_account:
        frappe.throw(
            _("No account named as POS Profile {0} was found in company {1}.").format(pos_profile, company)
        )

    opening_doc = frappe.new_doc("POS Opening Entry")
    opening_doc.user = user
    opening_doc.company = company
    opening_doc.pos_profile = pos_profile
    opening_doc.period_start_date = now_datetime()
    opening_doc.posting_date = nowdate()

    rows = _normalize_opening_balances_payload(opening_balances)
    if not rows:
        frappe.throw(_("At least one opening balance row is required"))

    opening_differences: list[dict[str, Any]] = []
    captured_one = False
    for row in rows:
        if captured_one:
            break
        mode = ((row or {}).get("mode_of_payment") or "").strip()
        if not mode:
            frappe.throw(_("Mode of Payment is required for opening balance row."))

        row_account = (row or {}).get("account") or profile_account
        if row_account:
            try:
                _ensure_mode_of_payment_account(mode, company, row_account)
            except Exception:
                frappe.log_error(
                    frappe.get_traceback(),
                    "jarz_pos.shift.ensure_mode_of_payment_account.start_shift",
                )

        system_balance = flt(_get_account_balance(row_account, company))
        confirmed_opening = _get_required_cash_count(
            row,
            "opening_amount",
            _("Opening cash count"),
        )
        difference = flt(confirmed_opening - system_balance, 2)

        opening_doc.append(
            "balance_details",
            {
                "mode_of_payment": mode,
                "opening_amount": confirmed_opening,
            },
        )

        opening_differences.append(
            {
                "mode_of_payment": mode,
                "account": row_account,
                "system_balance": system_balance,
                "confirmed_opening_amount": confirmed_opening,
                "difference": difference,
            }
        )
        captured_one = True

    if not opening_doc.balance_details:
        frappe.throw(_("At least one opening balance row is required"))

    opening_doc.insert(ignore_permissions=True)
    opening_doc.submit()

    # --- Create a Journal Entry if there is a discrepancy at opening ---
    opening_journal_entry = None
    if opening_differences:
        lines = [
            _("Opening confirmation differences:")
        ]
        for diff in opening_differences:
            lines.append(
                _("{0} | Account: {1} | System: {2} | Confirmed: {3} | Difference: {4}").format(
                    diff["mode_of_payment"],
                    diff.get("account") or "-",
                    flt(diff.get("system_balance") or 0),
                    flt(diff.get("confirmed_opening_amount") or 0),
                    flt(diff.get("difference") or 0),
                )
            )
        opening_doc.add_comment("Comment", "\n".join(lines))

        # Create JE for the discrepancy (surplus or shortage at opening)
        for diff in opening_differences:
            diff_amount = flt(diff.get("difference") or 0, 2)
            diff_account = diff.get("account")
            if diff_amount != 0 and diff_account:
                try:
                    opening_journal_entry = _create_discrepancy_journal_entry(
                        company=company,
                        cash_account=diff_account,
                        closing_amount=flt(diff.get("confirmed_opening_amount") or 0),
                        expected_amount=flt(diff.get("system_balance") or 0),
                        opening_entry=opening_doc.name,
                        closing_entry="Opening",
                    )
                except Exception:
                    frappe.log_error(
                        frappe.get_traceback(),
                        "jarz_pos.shift.discrepancy_journal_entry.start_shift",
                    )
                break  # single-account flow

    employee = _get_employee_for_user(user)

    # Notify all users on this POS profile about shift start
    _notify_shift_event(
        pos_profile=pos_profile,
        event_type="started",
        user=user,
        opening_entry=opening_doc.name,
    )

    return {
        "opening_entry": opening_doc.name,
        "employee": employee,
        "opening_differences": opening_differences,
        "journal_entry": opening_journal_entry,
    }


def _get_shift_account_movements(account: str, company: str, start_date, end_date) -> list[dict[str, Any]]:
    """Return all GL movements affecting the shift cash account within the shift window."""
    if not account:
        return []

    start_dt = get_datetime(start_date)
    end_dt = get_datetime(end_date)

    entries = frappe.get_all(
        "GL Entry",
        filters={
            "account": account,
            "company": company,
            "is_cancelled": 0,
            "creation": ["between", [start_dt, end_dt]],
        },
        fields=[
            "name",
            "creation",
            "posting_date",
            "voucher_type",
            "voucher_no",
            "debit",
            "credit",
            "against",
            "remarks",
        ],
        order_by="creation asc",
        limit=QUERY_LIMITS.GL_ENTRIES,
    )

    movements: list[dict[str, Any]] = []
    for entry in entries:
        debit = flt(entry.debit)
        credit = flt(entry.credit)
        movements.append(
            {
                "name": entry.name,
                "creation": str(entry.creation) if entry.creation else None,
                "posting_date": str(entry.posting_date) if entry.posting_date else None,
                "voucher_type": entry.voucher_type,
                "voucher_no": entry.voucher_no,
                "debit": debit,
                "credit": credit,
                "amount": flt(debit - credit),
                "against": entry.against,
                "remarks": entry.remarks,
            }
        )

    return movements


def _build_shift_payment_reconciliation_rows(rows: Any) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen_modes: set[str] = set()

    for row in rows or []:
        if isinstance(row, dict):
            mode = str(row.get("mode_of_payment") or "").strip()
            opening_amount = flt(row.get("opening_amount"))
        else:
            mode = str(getattr(row, "mode_of_payment", "") or "").strip()
            opening_amount = flt(getattr(row, "opening_amount", 0))

        if not mode or mode in seen_modes:
            continue

        normalized.append(
            {
                "mode_of_payment": mode,
                "opening_amount": opening_amount,
                "expected_amount": opening_amount,
                "closing_amount": 0.0,
                "difference": 0.0,
            }
        )
        seen_modes.add(mode)

    return normalized


def _get_shift_payment_reconciliation_rows(opening, closing=None) -> list[dict[str, Any]]:
    closing_rows = _build_shift_payment_reconciliation_rows(
        getattr(closing, "payment_reconciliation", None) if closing else None
    )
    if closing_rows:
        return closing_rows

    opening_rows = _build_shift_payment_reconciliation_rows(
        getattr(opening, "balance_details", None)
    )
    if opening_rows:
        return opening_rows

    opening_name = getattr(opening, "name", None) or _("this shift")
    frappe.throw(
        _(
            "No payment method is available to close shift {0}. Restart the shift or contact support."
        ).format(opening_name)
    )


def _sync_shift_payment_reconciliation_rows(closing, rows: list[dict[str, Any]]) -> None:
    if isinstance(getattr(closing, "doctype", None), str):
        closing.set("payment_reconciliation", [])
        for row in rows:
            closing.append("payment_reconciliation", row)
        return

    closing.payment_reconciliation = [SimpleNamespace(**row) for row in rows]


@frappe.whitelist(allow_guest=False)
def get_shift_summary(pos_opening_entry: str):
    if not pos_opening_entry:
        frappe.throw(_("POS Opening Entry is required"))

    opening = frappe.get_doc("POS Opening Entry", pos_opening_entry)
    if opening.user != frappe.session.user:
        frappe.throw(_("You are not allowed to access this shift"), frappe.PermissionError)

    closing_draft = make_closing_entry_from_opening(opening)
    payment_reconciliation_rows = _get_shift_payment_reconciliation_rows(opening, closing_draft)

    account = _resolve_pos_profile_account(
        opening.company, opening.pos_profile, None, None
    )

    account_movements = _get_shift_account_movements(
        account=account,
        company=opening.company,
        start_date=opening.period_start_date,
        end_date=now_datetime(),
    )
    courier_close_block = _get_shift_courier_close_block(opening.pos_profile)

    sales_vouchers = {
        row.get("voucher_no")
        for row in account_movements
        if row.get("voucher_type") in ("Sales Invoice", "POS Invoice") and row.get("voucher_no")
    }

    return {
        "opening_entry": opening.name,
        "status": opening.status,
        "user": opening.user,
        "company": opening.company,
        "pos_profile": opening.pos_profile,
        "period_start_date": opening.period_start_date,
        "period_end_date": opening.period_end_date,
        "invoice_count": len(sales_vouchers),
        "amounts_hidden": 1,
        "variance_visible": 0,
        "total_quantity": flt(closing_draft.total_quantity),
        "account": account,
        "courier_close_block": courier_close_block,
        "sales_invoices": [],
        "payment_reconciliation": [
            {
                "mode_of_payment": row["mode_of_payment"],
            }
            for row in payment_reconciliation_rows
        ],
    }


def _get_shift_courier_profile_expr() -> str:
    """Deprecated shim — the expression now lives with the carry service."""
    from jarz_pos.services.courier_carry import _courier_profile_expr

    return _courier_profile_expr()


def _get_shift_close_party_label(party_type: str, party: str) -> str:
    from jarz_pos.services.courier_carry import party_label

    return party_label(party_type, party)


def _get_shift_courier_close_block(pos_profile: str, detail_limit: int = 5) -> dict[str, Any]:
    """Money still out with couriers on *pos_profile*, invoice by invoice.

    The closing screen renders this as a checklist: the operator confirms each
    line is genuinely still with its courier before the till can be counted out.
    """
    from jarz_pos.services.courier_carry import build_close_block

    return build_close_block(pos_profile, detail_limit=detail_limit)


def _assert_courier_acknowledgement(
    pos_profile: str, acknowledged: list[str] | None
) -> dict[str, Any]:
    """Require an explicit per-transaction confirmation before closing.

    A courier who delivers the last order and drives home is a normal night, not
    an incident, so the close no longer refuses — but it does not wave the money
    through either.  Every unsettled transaction on the branch must be ticked;
    anything the operator leaves unticked is money they have NOT vouched for, and
    the close stops so they can settle it or look again.

    The check is a set comparison against what is unsettled *right now*, not
    against what the client was shown: a transaction settled by a colleague
    mid-close simply drops out, and one created mid-close is caught rather than
    silently carried.
    """
    block = _get_shift_courier_close_block(pos_profile)
    rows = block.get("transactions") or []
    if not rows:
        return block

    acknowledged_set = set(acknowledged or [])
    missing = [
        row for row in rows if row.get("courier_transaction") not in acknowledged_set
    ]
    if not missing:
        return block

    preview = ", ".join(
        str(row.get("reference_invoice") or row.get("courier_transaction"))
        for row in missing[:5]
    )
    if len(missing) > 5:
        preview = f"{preview}, +{len(missing) - 5}"

    frappe.throw(
        _(
            "{0} courier transaction(s) on {1} have not been confirmed: {2}. "
            "Settle them, or confirm on the closing screen that the money is "
            "still with the courier."
        ).format(len(missing), pos_profile, preview),
        title=_("Unconfirmed Courier Money"),
    )
    return block


def _throw_if_shift_has_unsettled_courier_transactions(pos_profile: str) -> dict[str, Any]:
    """Legacy hard block, kept for callers that cannot acknowledge.

    Retained so a client too old to send an acknowledgement list still gets the
    old refusal rather than an accidental silent carry.
    """
    block = _get_shift_courier_close_block(pos_profile)
    if not block.get("blocked"):
        return block

    frappe.throw(
        _(
            "You still have {0} unsettled courier transaction(s) for {1} courier(s) across {2} invoice(s) on POS Profile {3}. Settle courier balances before closing the shift."
        ).format(
            block.get("transaction_count", 0),
            block.get("party_count", 0),
            block.get("invoice_count", 0),
            pos_profile,
        ),
        title=_("Unsettled Courier Transactions"),
    )
    return block


def _get_or_create_cash_over_short_account(company: str) -> str:
    """Return (or create) a 'Cash Over/Short' expense account for shift discrepancies.

    Prefers the account set in Jarz POS Settings if available.
    """
    # Try Jarz POS Settings first – but only trust the configured account when it
    # actually exists as a non-group ledger account in this company. A stale or
    # missing value (e.g. carried over from a clone/restore) would otherwise make
    # the discrepancy Journal Entry fail its link validation, silently skipping the
    # Cash Over/Short posting. When invalid, fall through to find-or-create below.
    try:
        from jarz_pos.doctype.jarz_pos_settings.jarz_pos_settings import get_jarz_settings
        s = get_jarz_settings()
        configured = (s.cash_over_short_account or "").strip() if s else ""
        if configured and frappe.db.get_value(
            "Account",
            {"name": configured, "company": company, "is_group": 0},
            "name",
        ):
            return configured
    except Exception:
        pass

    account_name = ACCOUNTS.CASH_OVER_SHORT
    existing = frappe.db.get_value(
        "Account",
        {"company": company, "account_name": account_name, "is_group": 0},
        "name",
    )
    if existing:
        return existing

    # Find a suitable parent – Indirect Expenses or Expenses
    parent = frappe.db.get_value(
        "Account",
        {"company": company, "is_group": 1, "root_type": "Expense", "account_name": ACCOUNTS.INDIRECT_EXPENSES},
        "name",
    )
    if not parent:
        parent = frappe.db.get_value(
            "Account",
            {"company": company, "is_group": 1, "root_type": "Expense"},
            "name",
        )
    if not parent:
        frappe.throw(_("Cannot find an Expense parent account in company {0}").format(company))

    acc = frappe.get_doc({
        "doctype": "Account",
        "account_name": account_name,
        "parent_account": parent,
        "company": company,
        "account_type": "Expense Account",
        "root_type": "Expense",
        "is_group": 0,
    })
    acc.insert(ignore_permissions=True)
    return acc.name


def _create_discrepancy_journal_entry(
    company: str,
    cash_account: str,
    closing_amount: float,
    expected_amount: float,
    opening_entry: str,
    closing_entry: str,
):
    """Create a Journal Entry for the difference between confirmed closing and expected amount.

    - If closing > expected: surplus – debit cash, credit over/short (income side)
    - If closing < expected: shortage – debit over/short (expense), credit cash
    """
    diff = flt(closing_amount - expected_amount, 2)
    if diff == 0:
        return None

    over_short_account = _get_or_create_cash_over_short_account(company)

    remark = _(
        "Shift cash discrepancy for {0} → {1}. Expected {2}, confirmed {3}, difference {4}"
    ).format(opening_entry, closing_entry, expected_amount, closing_amount, diff)

    je = frappe.new_doc("Journal Entry")
    je.voucher_type = "Journal Entry"
    je.company = company
    je.posting_date = nowdate()
    je.user_remark = remark

    if diff > 0:
        # Surplus: cash account has more than expected
        je.append("accounts", {
            "account": cash_account,
            "debit_in_account_currency": abs(diff),
            "credit_in_account_currency": 0,
        })
        je.append("accounts", {
            "account": over_short_account,
            "debit_in_account_currency": 0,
            "credit_in_account_currency": abs(diff),
        })
    else:
        # Shortage: cash account has less than expected
        je.append("accounts", {
            "account": over_short_account,
            "debit_in_account_currency": abs(diff),
            "credit_in_account_currency": 0,
        })
        je.append("accounts", {
            "account": cash_account,
            "debit_in_account_currency": 0,
            "credit_in_account_currency": abs(diff),
        })

    je.insert(ignore_permissions=True)
    je.submit()
    return je.name


def _close_shift(
    opening,
    closing_balances: list[dict[str, Any]] | None,
    *,
    forced_by: str | None = None,
    reason: str | None = None,
    acknowledge_unsettled: bool = False,
    acknowledged_courier_transactions: list[str] | None = None,
) -> dict[str, Any]:
    """Close *opening* and post any Cash Over/Short difference.

    Shared by the normal close (the opener closes their own shift) and the
    manager force-close, so both take the identical accounting path: the counted
    amount is whatever the closer typed, and any variance books the same
    discrepancy Journal Entry. ``forced_by`` only adds an audit trail — it never
    changes the numbers.

    Money still out with couriers takes one of three paths:

    * ``acknowledged_courier_transactions`` — the closer ticked each line on the
      closing screen. Every unsettled transaction must appear, and each one is
      stamped as carried out of this shift.
    * ``forced_by`` + ``acknowledge_unsettled`` — a manager closing somebody
      else's shift, who cannot vouch line by line for cash they never handled.
      The balances are recorded and carried wholesale.
    * neither — the legacy hard refusal, so an old client cannot carry money by
      omission.
    """
    closing_balances = _normalize_closing_balances_payload(closing_balances)
    if not closing_balances:
        frappe.throw(_("At least one closing balance row is required"))

    if opening.status != "Open" or opening.docstatus != 1:
        frappe.throw(_("Selected POS Opening Entry should be open."), title=_("Invalid Opening Entry"))

    courier_block: dict[str, Any] = {}
    if forced_by and acknowledge_unsettled:
        # The manager has seen the outstanding courier balances and is closing
        # anyway — record exactly what was left open rather than dropping it.
        # Checked before the per-line path because a manager closing somebody
        # else's till cannot vouch for cash they never handled; requiring them
        # to tick each line would only teach them to tick without reading.
        courier_block = _get_shift_courier_close_block(opening.pos_profile)
    elif acknowledged_courier_transactions is not None:
        # The closer confirmed, line by line, that this money is still out.
        courier_block = _assert_courier_acknowledgement(
            opening.pos_profile, acknowledged_courier_transactions
        )
    else:
        _throw_if_shift_has_unsettled_courier_transactions(opening.pos_profile)

    closing = make_closing_entry_from_opening(opening)
    closing_payment_reconciliation = getattr(closing, "payment_reconciliation", None) or []
    normalized_closing_rows = _build_shift_payment_reconciliation_rows(closing_payment_reconciliation)
    payment_reconciliation_rows = _get_shift_payment_reconciliation_rows(opening, closing)
    if not normalized_closing_rows or len(normalized_closing_rows) != len(closing_payment_reconciliation):
        _sync_shift_payment_reconciliation_rows(closing, payment_reconciliation_rows)

    account = _resolve_pos_profile_account(
        opening.company, opening.pos_profile, None, None
    )
    system_expected = flt(_get_account_balance(account, opening.company)) if account else 0

    closing_map: dict[str, float] = {}
    for row in (closing_balances or []):
        mode = ((row or {}).get("mode_of_payment") or "").strip()
        if not mode:
            frappe.throw(_("Mode of Payment is required for closing balance row."))
        closing_map[mode] = _get_required_cash_count(
            row,
            "closing_amount",
            _("Closing cash count"),
        )

    for row in (closing.payment_reconciliation or []):
        if row.mode_of_payment not in closing_map:
            frappe.throw(_("Closing cash count is required for {0}.").format(row.mode_of_payment))
        row.expected_amount = system_expected
        row.closing_amount = flt(closing_map[row.mode_of_payment])

    try:
        closing.insert(ignore_permissions=True)
        closing.submit()
    except Exception as exc:
        traceback_text = frappe.get_traceback()
        error_message = _get_shift_close_error_message(exc, closing, traceback_text)
        frappe.log_error(traceback_text, "jarz_pos.shift.end_shift")
        frappe.throw(
            _("Failed to close shift: {0}").format(error_message),
            title=_("Shift Close Failed"),
        )

    if forced_by:
        _record_force_close_audit(
            opening=opening,
            closing=closing,
            forced_by=forced_by,
            reason=reason,
            courier_block=courier_block,
        )

    # --- Carry the acknowledged courier money into the next shift ---
    # Stamped only once the closing entry is submitted: a close that fails
    # validation must not leave transactions marked as carried by a shift that
    # is in fact still open.
    carried_courier: dict[str, Any] = {"count": 0, "net_balance": 0.0, "transactions": []}
    if courier_block.get("transactions"):
        from jarz_pos.services.courier_carry import stamp_carried

        carried_courier = stamp_carried(
            courier_block["transactions"],
            opening_entry=opening.name,
            user=frappe.session.user,
        )
        _add_carry_audit_comment(
            opening=opening,
            closing=closing,
            courier_block=courier_block,
            forced_by=forced_by,
        )

    # --- Create discrepancy journal entry if closing differs from expected ---
    journal_entry = None
    if account:
        for row in (closing.payment_reconciliation or []):
            diff = flt(row.closing_amount - row.expected_amount, 2)
            if diff != 0:
                try:
                    journal_entry = _create_discrepancy_journal_entry(
                        company=opening.company,
                        cash_account=account,
                        closing_amount=flt(row.closing_amount),
                        expected_amount=flt(row.expected_amount),
                        opening_entry=opening.name,
                        closing_entry=closing.name,
                    )
                except Exception:
                    frappe.log_error(
                        frappe.get_traceback(),
                        "jarz_pos.shift.discrepancy_journal_entry",
                    )
                break  # single-account flow

    # Fetch final account balance after closing
    account_balance = _get_account_balance(account, opening.company) if account else 0

    account_movements = _get_shift_account_movements(
        account=account,
        company=opening.company,
        start_date=opening.period_start_date,
        end_date=now_datetime(),
    )
    total_inflows = sum(flt(row.get("debit")) for row in account_movements)
    total_outflows = sum(flt(row.get("credit")) for row in account_movements)
    sales_vouchers = {
        row.get("voucher_no")
        for row in account_movements
        if row.get("voucher_type") in ("Sales Invoice", "POS Invoice") and row.get("voucher_no")
    }

    result = {
        "closing_entry": closing.name,
        "opening_entry": opening.name,
        "status": closing.status,
        "journal_entry": journal_entry,
        "account": account,
        "account_balance": flt(account_balance),
        "amounts_hidden": 0,
        "variance_visible": 1,
        "payment_reconciliation": [
            {
                "mode_of_payment": row.mode_of_payment,
                "opening_amount": flt(row.opening_amount),
                "expected_amount": flt(row.expected_amount),
                "closing_amount": flt(row.closing_amount),
                "difference": flt(row.closing_amount - row.expected_amount, 2),
            }
            for row in (closing.payment_reconciliation or [])
        ],
        "invoice_count": len(sales_vouchers),
        "grand_total": flt(total_inflows),
        "net_total": flt(closing.net_total),
        "total_quantity": flt(closing.total_quantity),
        "total_sales": flt(total_inflows),
        "total_outflows": flt(total_outflows),
        "net_movement": flt(total_inflows - total_outflows),
        "account_movements": account_movements,
        "sales_invoices": [],
        "forced_by": forced_by,
        "shift_user": opening.user,
        # What this close handed forward: the closer sees it on the summary
        # screen, so "money is still out with a courier" is the last thing they
        # read before they log out.
        "carried_courier_count": int(carried_courier.get("count") or 0),
        "carried_courier_amount": flt(carried_courier.get("net_balance")),
        "carried_courier_parties": courier_block.get("parties") or [],
    }

    # Notify all users on this POS profile about shift end. The shift's owner is
    # the subject of the message even when a manager pressed the button, so the
    # branch sees "<opener>'s shift was closed", not the manager's name alone.
    _notify_shift_event(
        pos_profile=opening.pos_profile,
        event_type="ended",
        user=opening.user or frappe.session.user,
        opening_entry=opening.name,
    )

    return result


def _add_carry_audit_comment(
    *,
    opening,
    closing,
    courier_block: dict[str, Any],
    forced_by: str | None,
) -> None:
    """Write who let which courier money walk, onto both shift entries.

    The stamps on the Courier Transactions are the queryable record; this is the
    human one, readable from the shift itself without knowing that Courier
    Transactions exist.
    """
    try:
        rows = courier_block.get("transactions") or []
        if not rows:
            return
        actor = forced_by or frappe.session.user
        actor_name = frappe.db.get_value("User", actor, "full_name") or actor
        total = flt(sum(flt(row.get("net_balance")) for row in rows))
        lines = [
            f"{actor_name} ({actor}) confirmed at close that {len(rows)} courier "
            f"transaction(s) worth {total:.2f} are still out with couriers."
        ]
        for party in (courier_block.get("parties") or [])[:5]:
            lines.append(
                "- {0}: {1} transaction(s), {2:.2f}".format(
                    party.get("display_name") or party.get("party"),
                    party.get("transaction_count"),
                    flt(party.get("net_balance")),
                )
            )
        message = "<br>".join(lines)
        for doc in (opening, closing):
            try:
                doc.add_comment("Comment", message)
            except Exception:
                continue
    except Exception:
        frappe.log_error(frappe.get_traceback(), "jarz_pos.shift.carry_audit")


def _record_force_close_audit(
    *,
    opening,
    closing,
    forced_by: str,
    reason: str | None,
    courier_block: dict[str, Any] | None = None,
) -> None:
    """Leave a permanent trail of who force-closed whose shift, and why."""
    try:
        forced_by_name = frappe.db.get_value("User", forced_by, "full_name") or forced_by
        owner_name = frappe.db.get_value("User", opening.user, "full_name") or opening.user
        lines = [
            f"Shift force-closed by {forced_by_name} ({forced_by}) on behalf of "
            f"{owner_name} ({opening.user}).",
            f"Reason: {reason or 'not provided'}",
        ]
        if courier_block and courier_block.get("blocked"):
            lines.append(
                "Closed with {0} unsettled courier transaction(s) across {1} invoice(s) "
                "still outstanding; these remain open and must still be settled.".format(
                    courier_block.get("transaction_count", 0),
                    courier_block.get("invoice_count", 0),
                )
            )
        message = " ".join(lines)
        for doc in (opening, closing):
            try:
                doc.add_comment("Comment", message)
            except Exception:
                continue
        frappe.logger().info(
            f"SHIFT: force close {opening.name} (owner={opening.user}) by {forced_by}"
        )
    except Exception:
        # An audit-trail failure must not roll back a completed close, but it
        # must never pass unnoticed either.
        frappe.log_error(frappe.get_traceback(), "jarz_pos.shift.force_close_audit")


@frappe.whitelist(allow_guest=False)
def end_shift(
    pos_opening_entry: str,
    closing_balances: list[dict[str, Any]] | None = None,
    acknowledged_courier_transactions: Any = None,
):
    """Close your own shift.

    ``acknowledged_courier_transactions`` is the list of Courier Transaction
    names the closer ticked to confirm the money is still with the courier.
    Omitting it entirely keeps the old behaviour — the close refuses while
    anything is unsettled — so an un-upgraded client cannot carry money by
    accident. Sending an empty list is a valid answer meaning "nothing is out",
    and it is verified against the database rather than trusted.
    """
    if not pos_opening_entry:
        frappe.throw(_("POS Opening Entry is required"))

    opening = frappe.get_doc("POS Opening Entry", pos_opening_entry)

    if opening.user != frappe.session.user:
        frappe.throw(_("You are not allowed to close this shift"), frappe.PermissionError)

    from jarz_pos.services.courier_carry import normalize_acknowledgement

    acknowledged = (
        normalize_acknowledgement(acknowledged_courier_transactions)
        if acknowledged_courier_transactions is not None
        else None
    )

    return _close_shift(
        opening,
        closing_balances,
        acknowledged_courier_transactions=acknowledged,
    )


@frappe.whitelist(allow_guest=False)
def force_close_shift(
    pos_opening_entry: str,
    closing_balances: list[dict[str, Any]] | None = None,
    reason: str | None = None,
    acknowledge_unsettled: bool | int | str = False,
    acknowledged_courier_transactions: Any = None,
):
    """Close a shift opened by somebody else.

    Only the opener could close a shift, and nobody else could start one on that
    branch while it stayed open — so a staff member who left with an open shift
    took the whole branch offline until an administrator intervened in Desk.
    This is that escape hatch, restricted to shift-monitor managers and to the
    branches they are assigned to.

    The manager supplies the counted amounts exactly as the opener would, so a
    real drawer difference still books a Cash Over/Short entry instead of being
    papered over.
    """
    from jarz_pos.utils.access_control import (
        ensure_profile_scoped_invoice_access,
    )

    if not pos_opening_entry:
        frappe.throw(_("POS Opening Entry is required"))

    reason = (reason or "").strip()
    if not reason:
        frappe.throw(_("A reason is required when closing someone else's shift."))

    opening = frappe.get_doc("POS Opening Entry", pos_opening_entry)

    # Role gate (may this user force-close at all?) …
    from jarz_pos.api.manager import _ensure_shift_monitor_access

    _ensure_shift_monitor_access()
    # … then branch gate (is this branch one of theirs?).
    ensure_profile_scoped_invoice_access(
        frappe._dict({"custom_kanban_profile": opening.pos_profile}),
        action_label="closing a shift on this branch",
    )

    from jarz_pos.services.courier_carry import normalize_acknowledgement

    acknowledged = (
        normalize_acknowledgement(acknowledged_courier_transactions)
        if acknowledged_courier_transactions is not None
        else None
    )

    if opening.user == frappe.session.user:
        # Nothing forced about closing your own shift — take the normal path so
        # the audit trail stays meaningful.
        return _close_shift(
            opening,
            closing_balances,
            acknowledged_courier_transactions=acknowledged,
        )

    acknowledge = str(acknowledge_unsettled or "").strip().lower() in {
        "1", "true", "yes", "y", "on",
    } or acknowledge_unsettled is True

    return _close_shift(
        opening,
        closing_balances,
        forced_by=frappe.session.user,
        reason=reason,
        acknowledge_unsettled=acknowledge,
        acknowledged_courier_transactions=acknowledged,
    )


@frappe.whitelist(allow_guest=False)
def get_force_close_preview(pos_opening_entry: str) -> dict[str, Any]:
    """What a manager needs to see before force-closing someone else's shift.

    Returns the shift's owner, its payment modes (so the UI can ask for a count
    per mode), and any courier balances that would block a normal close.
    """
    from jarz_pos.api.manager import _ensure_shift_monitor_access
    from jarz_pos.utils.access_control import ensure_profile_scoped_invoice_access

    if not pos_opening_entry:
        frappe.throw(_("POS Opening Entry is required"))

    opening = frappe.get_doc("POS Opening Entry", pos_opening_entry)
    _ensure_shift_monitor_access()
    ensure_profile_scoped_invoice_access(
        frappe._dict({"custom_kanban_profile": opening.pos_profile}),
        action_label="closing a shift on this branch",
    )

    account = _resolve_pos_profile_account(opening.company, opening.pos_profile, None, None)
    courier_block = _get_shift_courier_close_block(opening.pos_profile)

    return {
        "success": True,
        "opening_entry": opening.name,
        "pos_profile": opening.pos_profile,
        "status": opening.status,
        "user": opening.user,
        "user_full_name": frappe.db.get_value("User", opening.user, "full_name") or opening.user,
        "period_start_date": str(opening.period_start_date or ""),
        "is_current_user": opening.user == frappe.session.user,
        "modes_of_payment": [
            row.mode_of_payment for row in (opening.balance_details or []) if row.mode_of_payment
        ],
        "expected_amount": flt(_get_account_balance(account, opening.company)) if account else 0.0,
        "account": account,
        "courier_block": courier_block,
    }


# ---------------------------------------------------------------------------
# Shift notification helper
# ---------------------------------------------------------------------------

def _notify_shift_event(*, pos_profile: str, event_type: str, user: str, opening_entry: str):
    """Send realtime + FCM notifications for shift start/end to all users on the POS profile.

    Args:
        pos_profile: POS Profile name
        event_type: "started" or "ended"
        user: The user who started/ended the shift
        opening_entry: The POS Opening Entry name
    """
    try:
        user_full_name = frappe.db.get_value("User", user, "full_name") or user

        ws_event = WS_EVENTS.SHIFT_STARTED if event_type == "started" else WS_EVENTS.SHIFT_ENDED
        payload = {
            "pos_profile": pos_profile,
            "event_type": event_type,
            "user": user,
            "user_full_name": user_full_name,
            "opening_entry": opening_entry,
            "timestamp": now_datetime().isoformat(),
        }

        # Get all users assigned to this POS profile
        profile_users = frappe.get_all(
            "POS Profile User",
            filters={"parent": pos_profile, "parenttype": "POS Profile"},
            pluck="user",
        )

        # Send realtime to each user individually
        for u in profile_users:
            frappe.publish_realtime(ws_event, payload, user=u)

        # Send FCM push notifications to registered devices
        try:
            from jarz_pos.api.notifications import _get_tokens_for_users, _send_fcm_notifications

            tokens = _get_tokens_for_users(profile_users)
            if tokens:
                title = f"Shift {'Started' if event_type == 'started' else 'Ended'}"
                body = f"{user_full_name} {'opened' if event_type == 'started' else 'closed'} a shift on {pos_profile}"
                data_payload = {
                    "type": f"shift_{event_type}",
                    "pos_profile": pos_profile,
                    "user": user,
                    "user_full_name": user_full_name,
                    "opening_entry": opening_entry,
                    "title": title,
                    "body": body,
                }
                _send_fcm_notifications(tokens, data_payload)
        except Exception:
            frappe.log_error(frappe.get_traceback(), "shift_notification.fcm_failed")

    except Exception:
        # Never let notification errors block the shift operation
        frappe.log_error(frappe.get_traceback(), "shift_notification_failed")
