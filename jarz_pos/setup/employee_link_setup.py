"""Schema + account prerequisites for the employee-advance flow.

WHY this is code and not a Custom Field made by hand on the server
-----------------------------------------------------------------
The advance flow spans two party spaces — an HRMS ``Employee Advance`` (party is
the ``Employee``) and an employee-purpose POS order (party is the ``Customer``) —
and it needs six columns HRMS does not ship: which branch account the cash left,
which POS Profile that branch is, who asked, who approved, when, and which
Payment Entry actually moved the money. Those columns are load-bearing:
``api/employee_advances.approve_employee_advance`` reads
``custom_jarz_paying_account`` to decide which drawer to pay out of, and refuses
to run without it.

A field created by hand on one server is the exact failure this app has already
paid for twice (see ``setup/accounts_setup.py``): the Cash Over/Short account
existed on staging and not on production, so shift close could not post its
journal entry there and — months later and apparently unrelated — every full save
of ``Jarz POS Settings`` failed link validation on production only. Deriving the
schema from a committed module means staging and production get the same columns
from the same commit, and they come back on the next migrate if anyone deletes
them.

WHY these live here and not in ``fixtures/custom_field.json``
------------------------------------------------------------
``sync_fixtures`` imports the whole file; a Custom Field whose ``dt`` is
``Employee Advance`` on a bench where HRMS is not installed makes that import
fail, and it takes every *other* fixture in the file down with it. ``hooks.py``
declares no ``required_apps``, and this app already probes HRMS tables behind a
guard (``api/recurring_expenses.py``), so HRMS absence has to be a normal
answer rather than a broken migrate. Everything below is therefore gated on
``hrms_available()`` and creates nothing when it is False.

CAUTION for whoever next runs ``bench export-fixtures``: ``Customer`` IS listed
in the ``fixtures`` filter in ``hooks.py``, so an export will happily scoop
``Customer.custom_employee`` into ``fixtures/custom_field.json`` — reintroducing
the "Link to Employee on a bench without HRMS" problem this module exists to
avoid. If that happens, delete the exported entry; this seeder already owns the
field.

Idempotent and safe on every ``bench migrate``: each unit is wrapped so one
failure logs and the rest continue, and the whole routine is wrapped so a seeder
can never abort the shared migrate for every app on the site.

This module must import cleanly with NO top-level frappe calls.
"""

from __future__ import annotations

from typing import Any, Dict, List

import frappe

from jarz_pos.utils.employee_link import (
    ADVANCE_DOCTYPE,
    CUSTOMER_EMPLOYEE_FIELD,
    hrms_available,
)

LOGGER_NAME = "employee_link_setup"

#: Company field HRMS itself falls back to in ``EmployeeAdvance.before_submit``.
#: It is an HRMS-owned Custom Field on Company, so every read of it is probed
#: first — on a bench without HRMS the column simply is not there.
COMPANY_ADVANCE_FIELD = "default_employee_advance_account"

#: The ledger name HRMS's own ``set_default_hr_accounts`` looks for when it
#: seeds ``Company.default_employee_advance_account``. Matching that name is
#: what makes this routine agree with HRMS instead of inventing a second
#: convention.
ADVANCE_ACCOUNT_NAME = "Employee Advances"

#: ``Employee Advance.advance_account`` is validated by
#: ``EmployeeAdvance.validate_advance_account_type``, which throws unless the
#: account is exactly this type.
ADVANCE_ACCOUNT_TYPE = "Receivable"

#: The explicit Employee <-> Customer join that ``utils/employee_link.py``
#: prefers over name matching. Only created when HRMS is present: a Link field
#: whose ``options`` name a DocType that does not exist is an invalid field.
CUSTOMER_FIELDS: List[Dict[str, Any]] = [
    {
        "fieldname": CUSTOMER_EMPLOYEE_FIELD,
        "label": "Employee",
        "fieldtype": "Link",
        "options": "Employee",
        # ``customer_group`` is a core Customer field, so this anchor exists on
        # every site. Anchoring on one of this app's own fixture fields would
        # silently drop the field to the bottom of the form on a site where the
        # fixture has not imported yet.
        "insert_after": "customer_group",
        "module": "jarz pos",
        "description": (
            "Staff member this customer account belongs to. Read by "
            "jarz_pos.utils.employee_link to join POS orders and Employee "
            "Advances into one per-person balance."
        ),
    },
]

#: Everything the Jarz advance flow needs that HRMS does not carry. Every
#: fieldname is prefixed ``custom_jarz_`` so it can never collide with a field
#: HRMS adds later — the whole point of the prefix is that an HRMS upgrade
#: introducing its own ``paying_account`` cannot quietly take ours over.
#: ``more_info_section`` is a collapsible section on the standard form, so these
#: land together and out of the way instead of interleaving with the accounting
#: fields.
ADVANCE_FIELDS: List[Dict[str, Any]] = [
    {
        "fieldname": "custom_jarz_paying_account",
        "label": "Paying Account (Jarz)",
        "fieldtype": "Link",
        "options": "Account",
        "insert_after": "more_info_section",
        "module": "jarz pos",
        "description": (
            "Branch cash/bank account the advance is paid out of. "
            "approve_employee_advance refuses to pay without it."
        ),
    },
    {
        "fieldname": "custom_jarz_pos_profile",
        "label": "Branch (Jarz)",
        "fieldtype": "Link",
        "options": "POS Profile",
        "insert_after": "custom_jarz_paying_account",
        "module": "jarz pos",
        "description": "POS Profile (branch) the request was raised from.",
    },
    {
        "fieldname": "custom_jarz_requested_by",
        "label": "Requested By (Jarz)",
        "fieldtype": "Link",
        "options": "User",
        "insert_after": "custom_jarz_pos_profile",
        "read_only": 1,
        "module": "jarz pos",
        "description": "Line manager who raised the request.",
    },
    {
        "fieldname": "custom_jarz_approved_by",
        "label": "Approved By (Jarz)",
        "fieldtype": "Link",
        "options": "User",
        "insert_after": "custom_jarz_requested_by",
        "read_only": 1,
        "module": "jarz pos",
        "description": "Manager who approved and released the cash.",
    },
    {
        "fieldname": "custom_jarz_approved_on",
        "label": "Approved On (Jarz)",
        "fieldtype": "Datetime",
        "insert_after": "custom_jarz_approved_by",
        "read_only": 1,
        "module": "jarz pos",
    },
    {
        "fieldname": "custom_jarz_payment_entry",
        "label": "Payment Entry (Jarz)",
        "fieldtype": "Link",
        "options": "Payment Entry",
        "insert_after": "custom_jarz_approved_on",
        "read_only": 1,
        # The advance is already submitted by the time the Payment Entry exists,
        # so without allow_on_submit the write back in approve_employee_advance
        # would be silently filtered out and the advance would look unpaid to
        # every screen that reads this field.
        "allow_on_submit": 1,
        "module": "jarz pos",
        "description": "Payment Entry that actually moved the cash out of the branch account.",
    },
]


def _logger():
    return frappe.logger(LOGGER_NAME, allow_site=True)


def _custom_field_exists(doctype: str, fieldname: str) -> bool:
    """True when the Custom Field row is already there.

    ``create_custom_fields`` returns nothing, so created-vs-existing has to be
    observed before the call rather than read off its result.
    """
    try:
        return bool(frappe.db.exists("Custom Field", {"dt": doctype, "fieldname": fieldname}))
    except Exception:
        return False


def _ensure_fields(doctype: str, specs: List[Dict[str, Any]], log: Dict[str, List[str]]) -> None:
    """Create (or bring in line) the given Custom Fields on ``doctype``.

    Uses ``frappe.custom.doctype.custom_field.custom_field.create_custom_fields``
    — the same helper ``api/test_kanban_setup.py`` reaches for — rather than
    hand-inserting Custom Field docs, because it also clears the DocType cache
    and runs ``frappe.db.updatedb`` so the new column is actually queryable in
    the same migrate rather than one migrate later.
    """
    if not frappe.db.exists("DocType", doctype):
        # Migrate ordering put this seeder ahead of the DocType, or the owning
        # app is not installed. Not an error — the next migrate picks it up.
        log.setdefault("skipped", []).append(f"{doctype}: DocType not present")
        return

    try:
        from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
    except Exception:
        _logger().error("Could not import create_custom_fields", exc_info=True)
        return

    for spec in specs:
        fieldname = spec["fieldname"]
        try:
            already = _custom_field_exists(doctype, fieldname)
            # One call per field on purpose: a batched call means one bad spec
            # takes the whole group down, and these six fields are what the
            # approve path reads.
            create_custom_fields({doctype: [dict(spec)]})
            if already:
                log["existing"].append(f"{doctype}.{fieldname}")
            else:
                log["created"].append(f"{doctype}.{fieldname}")
        except Exception:
            _logger().error(
                f"Failed to ensure Custom Field {doctype}.{fieldname}", exc_info=True
            )


def _ensure_customer_employee_field(log: Dict[str, List[str]]) -> None:
    """Seed ``Customer.custom_employee`` — but only where ``Employee`` exists.

    Guarded on HRMS as a whole rather than on the ``Employee`` DocType alone so
    this module has exactly one "is HRMS here" answer, the one
    ``utils/employee_link.py`` publishes.
    """
    if not hrms_available():
        log.setdefault("skipped", []).append(
            f"Customer.{CUSTOMER_EMPLOYEE_FIELD}: HRMS not installed"
        )
        return
    _ensure_fields("Customer", CUSTOMER_FIELDS, log)


def _ensure_advance_jarz_fields(log: Dict[str, List[str]]) -> None:
    """Seed the six ``custom_jarz_*`` fields on ``Employee Advance``."""
    if not hrms_available():
        log.setdefault("skipped", []).append(f"{ADVANCE_DOCTYPE}: HRMS not installed")
        return
    _ensure_fields(ADVANCE_DOCTYPE, ADVANCE_FIELDS, log)


def _find_advance_account(company: str) -> str:
    """An existing Receivable ledger usable as ``Employee Advance.advance_account``.

    Deliberately a *lookup*, never a create: a chart-of-accounts node invented by
    a migrate is a node nobody chose, in a tree an accountant owns. If nothing is
    found the caller warns by name and the operator creates it.
    """
    try:
        account = frappe.db.get_value(
            "Account",
            {
                "company": company,
                "is_group": 0,
                "account_name": ADVANCE_ACCOUNT_NAME,
                "account_type": ADVANCE_ACCOUNT_TYPE,
            },
            "name",
        )
        if account:
            return str(account)

        # Fall back to any Receivable ledger whose name reads like an employee
        # advance account — sites that translated or renamed the node still have
        # one, and HRMS only cares about the account_type.
        rows = (
            frappe.get_all(
                "Account",
                filters={
                    "company": company,
                    "is_group": 0,
                    "account_type": ADVANCE_ACCOUNT_TYPE,
                },
                or_filters=[
                    ["Account", "account_name", "like", "%Employee Advance%"],
                    ["Account", "name", "like", "%Employee Advance%"],
                ],
                fields=["name"],
                order_by="name asc",
                limit=1,
            )
            or []
        )
        if rows:
            return str(rows[0]["name"])
    except Exception:
        _logger().error(
            f"Failed looking up an employee advance account for {company}", exc_info=True
        )
    return ""


def _type_unused_advance_account(company: str, log: Dict[str, List[str]]) -> None:
    """Set ``account_type = Receivable`` on an advance account that has none.

    ERPNext creates "Employee Advances" under Loans and Advances with NO
    ``account_type`` on some charts — which is what staging turned out to have on
    2026-08-29: the node existed, correctly parented, right company, right
    currency, and HRMS still refused every approval, because
    ``validate_advance_account_type`` compares against ``"Receivable"`` exactly
    and an empty type fails it.

    Repointing the Company default at some other Receivable ledger is NOT an
    option: on this chart the only one is ``Debtors``, and putting staff advances
    there merges them into customer AR.

    So the account is typed in place, but ONLY when every one of these holds:
      * it is a leaf (``is_group = 0``) under the right company,
      * its ``root_type`` is already ``Asset``,
      * its ``account_type`` is genuinely empty -- never overwritten,
      * it has NO GL entries.

    That last guard is the important one. Typing an empty account is a
    classification, not a reclassification; doing the same to an account that has
    already posted would silently change how existing entries are reported. If it
    has entries, this warns and leaves it alone for an accountant.
    """
    try:
        rows = (
            frappe.get_all(
                "Account",
                filters={
                    "company": company,
                    "is_group": 0,
                    "account_name": ADVANCE_ACCOUNT_NAME,
                },
                fields=["name", "account_type", "root_type"],
                limit_page_length=0,
            )
            or []
        )
    except Exception:
        _logger().error(
            f"Failed looking for an untyped advance account in {company}", exc_info=True
        )
        return

    for row in rows:
        account = str(row["name"])
        if str(row.get("account_type") or "").strip():
            continue  # already typed, whatever the type — not ours to change
        if str(row.get("root_type") or "").strip() != "Asset":
            log.setdefault("warnings", []).append(
                f"{company}: {account} is untyped but its root_type is "
                f"{row.get('root_type')!r}, not 'Asset' — left alone"
            )
            continue
        try:
            posted = frappe.db.count("GL Entry", {"account": account, "is_cancelled": 0})
        except Exception:
            _logger().error(f"Failed counting GL entries for {account}", exc_info=True)
            continue
        if posted:
            log.setdefault("warnings", []).append(
                f"{company}: {account} is untyped and has {posted} GL entries — "
                f"NOT typed automatically; an accountant must set it to "
                f"{ADVANCE_ACCOUNT_TYPE!r}"
            )
            continue
        try:
            frappe.db.set_value(
                "Account", account, "account_type", ADVANCE_ACCOUNT_TYPE,
                update_modified=False,
            )
            log["created"].append(
                f"{company}: {account} account_type -> {ADVANCE_ACCOUNT_TYPE} "
                f"(was unset, no GL entries)"
            )
        except Exception:
            _logger().error(f"Failed typing {account} as {ADVANCE_ACCOUNT_TYPE}", exc_info=True)


def _verify_advance_accounts(log: Dict[str, List[str]]) -> None:
    """Report — and only where it is unambiguous, repair — the advance account.

    ``EmployeeAdvance.before_submit`` falls back to
    ``Company.default_employee_advance_account`` and throws when it is still
    empty, and ``validate_advance_account_type`` throws unless that account is
    ``Receivable``. Both throws land on a manager pressing "approve" with an
    employee standing in front of them, so the gap is worth naming at migrate
    time instead of at approval time.

    Three outcomes, all of them loud:
      * already configured and Receivable -> recorded as existing;
      * unset but an unambiguous "Employee Advances" Receivable ledger exists ->
        the Company default is pointed at it;
      * neither -> a warning naming the company. Nothing is created.
    """
    try:
        if not hrms_available():
            log.setdefault("skipped", []).append("advance account check: HRMS not installed")
            return

        # The Company field is an HRMS Custom Field, so it is absent on a bench
        # where HRMS was installed after this app — probing beats assuming.
        try:
            has_field = bool(frappe.db.has_column("Company", COMPANY_ADVANCE_FIELD))
        except Exception:
            has_field = False
        if not has_field:
            log.setdefault("warnings", []).append(
                f"Company.{COMPANY_ADVANCE_FIELD} does not exist; run bench migrate for hrms"
            )
            return

        companies = [row["name"] for row in (frappe.get_all("Company", fields=["name"]) or [])]
    except Exception:
        _logger().error("Failed to enumerate companies for the advance-account check", exc_info=True)
        return

    for company in companies:
        try:
            # Runs FIRST, and before the `configured` read below, because the
            # common broken shape is a Company default already pointing at an
            # untyped "Employee Advances" node. Typing it here turns the warning
            # branch below into the success branch on the same migrate, instead
            # of reporting a gap that nothing ever closes.
            _type_unused_advance_account(company, log)

            configured = str(
                frappe.db.get_value("Company", company, COMPANY_ADVANCE_FIELD) or ""
            ).strip()

            if configured:
                account_type = str(
                    frappe.db.get_value("Account", configured, "account_type") or ""
                ).strip()
                if account_type == ADVANCE_ACCOUNT_TYPE:
                    log["existing"].append(f"{company}: advance account {configured}")
                else:
                    # Do not "fix" this by repointing: an account someone chose
                    # deliberately is not ours to replace, and the wrong type is
                    # a decision to revisit, not a typo to patch.
                    log.setdefault("warnings", []).append(
                        f"{company}: {COMPANY_ADVANCE_FIELD} is {configured!r} whose "
                        f"account_type is {account_type or 'unset'!r}, not "
                        f"{ADVANCE_ACCOUNT_TYPE!r} — Employee Advance submit will be refused"
                    )
                continue

            candidate = _find_advance_account(company)
            if not candidate:
                log.setdefault("warnings", []).append(
                    f"{company}: no {ADVANCE_ACCOUNT_TYPE} account named "
                    f"{ADVANCE_ACCOUNT_NAME!r} exists and "
                    f"{COMPANY_ADVANCE_FIELD} is unset — create it, then set the "
                    f"Company default, or every advance approval will be refused"
                )
                continue

            # db_set, not a full Company save. A full save of Company revalidates
            # every Link on it, and this app has already been bitten by exactly
            # that: one dangling Link on Jarz POS Settings silently broke every
            # later full save of that Single. A migrate-time seeder must not be
            # able to fail on an unrelated field.
            frappe.db.set_value(
                "Company", company, COMPANY_ADVANCE_FIELD, candidate, update_modified=False
            )
            log["created"].append(f"{company}: {COMPANY_ADVANCE_FIELD} -> {candidate}")
        except Exception:
            _logger().error(
                f"Failed to verify the employee advance account for {company}", exc_info=True
            )


def ensure_employee_link_fields() -> Dict[str, List[str]]:
    """Idempotently seed the employee-advance schema. Safe on every migrate.

    Never raises. This runs inside the shared ``bench migrate`` that the whole
    bench depends on; a raising seeder does not fail its own feature, it aborts
    the migrate for every app on the site.
    """
    log: Dict[str, List[str]] = {
        "created": [],
        "existing": [],
        "skipped": [],
        "warnings": [],
    }
    logger = _logger()

    try:
        _ensure_customer_employee_field(log)
        _ensure_advance_jarz_fields(log)
        # Last: the account check reads Company rows and is pure diagnostics for
        # the two field seeders above, which is the order an operator reads the
        # summary in.
        _verify_advance_accounts(log)

        if log["created"]:
            frappe.db.commit()

        # Logged at ERROR level on purpose. Frappe's default log level off a dev
        # server is ERROR, so .info() and .warning() are discarded entirely on
        # staging and production — a seeder summary that exists only at INFO is a
        # summary nobody will ever read where it matters.
        logger.error(
            "employee_link_setup: created=%s existing=%s skipped=%s"
            % (log["created"], log["existing"], log["skipped"])
        )
        for warning in log["warnings"]:
            logger.error("employee_link_setup WARNING: " + warning)
    except Exception:
        # Never let schema seeding break a migrate for the whole bench.
        logger.error("ensure_employee_link_fields failed unexpectedly", exc_info=True)

    return log
