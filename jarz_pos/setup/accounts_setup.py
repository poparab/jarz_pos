"""Accounts setup — the POS ledger accounts Jarz POS Settings points at.

Idempotent, create-only. Safe on every ``bench migrate``: an account that
already exists is never modified, and a missing parent is skipped rather than
throwing.

Why this is code and not a hand-created account on the server: the Cash
Over/Short account existed on staging and was never created on production,
while ``Jarz POS Settings.cash_over_short_account`` pointed at it on *both*.
The consequences compounded quietly.

* Shift close could not post its Cash-Over/Short journal entry on production
  (2026-07-11), because the account named in settings was not there.
* Every *full* save of that Single on production then failed link validation —
  which is what silently stopped the purchasing warehouse-routing seeder from
  writing anything on production while it succeeded on staging (2026-08-03).

One absent master record, two unrelated features broken months apart, both
diagnosed only after they misbehaved. Seeding it from git means staging and
production derive the same account from the same commit, and it comes back on
the next migrate if it is ever removed.
"""

import frappe

LOGGER_NAME = "accounts_setup"

SETTINGS_DOCTYPE = "Jarz POS Settings"

#: The POS accounts this app relies on, keyed by the Jarz POS Settings field
#: that names them. ``parent`` is matched per company by account name.
REQUIRED_ACCOUNTS = (
    {
        "settings_field": "cash_over_short_account",
        "account_name": "Cash Over Short",
        "parent_account_name": "Indirect Expenses",
        "root_type": "Expense",
        "report_type": "Profit and Loss",
        "account_type": "Expense Account",
    },
)


def _logger():
    return frappe.logger(LOGGER_NAME, allow_site=True)


def _company_of(account_name: str) -> str:
    """Company for an account named ``<something> - <abbr>``.

    Settings store the full account name including the company abbreviation, so
    the abbreviation is the reliable way back to the company without guessing a
    default.
    """
    abbr = (account_name or "").rsplit(" - ", 1)[-1].strip()
    if not abbr:
        return ""
    return frappe.db.get_value("Company", {"abbr": abbr}, "name") or ""


def ensure_pos_accounts():
    """Create any missing POS ledger account. Never raises — returns a summary."""
    log = {"created": [], "existing": [], "skipped": []}
    try:
        for spec in REQUIRED_ACCOUNTS:
            _ensure_one(spec, log)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "accounts_setup failed")
        return log

    if log["created"]:
        frappe.db.commit()
    # .info() is invisible at the default server log level, so what matters on a
    # real migrate goes out at warning.
    _logger().warning(
        "accounts_setup: created=%s existing=%s skipped=%s"
        % (log["created"], log["existing"], log["skipped"])
    )
    return log


def _ensure_one(spec, log):
    field = spec["settings_field"]
    configured = (frappe.db.get_single_value(SETTINGS_DOCTYPE, field) or "").strip()
    if not configured:
        # Nothing points at it; creating an unreferenced account would be noise.
        log["skipped"].append(f"{field} not configured")
        return

    if frappe.db.exists("Account", configured):
        log["existing"].append(configured)
        return

    company = _company_of(configured)
    if not company:
        log["skipped"].append(f"{configured}: no company matches its abbreviation")
        return

    parent = frappe.db.get_value(
        "Account",
        {
            "account_name": spec["parent_account_name"],
            "company": company,
            "is_group": 1,
        },
        "name",
    )
    if not parent:
        log["skipped"].append(
            f"{configured}: parent {spec['parent_account_name']!r} missing for {company}"
        )
        return

    try:
        doc = frappe.get_doc({
            "doctype": "Account",
            "account_name": spec["account_name"],
            "parent_account": parent,
            "company": company,
            "root_type": spec["root_type"],
            "report_type": spec["report_type"],
            "account_type": spec["account_type"],
            "is_group": 0,
            "account_currency": frappe.db.get_value("Company", company, "default_currency"),
        })
        doc.flags.ignore_permissions = True
        doc.insert()
    except Exception:
        frappe.log_error(frappe.get_traceback(), f"accounts_setup: could not create {configured}")
        log["skipped"].append(f"{configured}: insert failed")
        return

    if doc.name != configured:
        # ERPNext derives the account name itself; if it landed somewhere other
        # than what settings expect, say so rather than leaving a silent
        # mismatch that looks fixed.
        log["skipped"].append(f"created {doc.name} but settings expect {configured}")
        return

    log["created"].append(doc.name)
