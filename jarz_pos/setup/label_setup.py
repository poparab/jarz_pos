"""Label accounting masters — the accounts and Item the label COGS flow rides on.

Printed customer labels are tracked in an app-owned ledger (``Jarz Label
Movement``), not as ERPNext stock Items — one Item per customer-flavour would
bury the catalogue. But the *money* must live in the real GL:

* ``Labels Inventory - <abbr>``  (Balance Sheet, under Current Assets)
    Debited by the printer's Purchase Invoice, so labels on the shelf are an
    asset at their actual printed cost.
* ``Label Cost - <abbr>``        (P&L, under Cost of Goods Sold)
    Debited as labels are consumed by invoices, via the Journal Entries
    ``services.label_stock`` posts — label spend lands in COGS in the period
    the labels are used, not the period they were printed.
* ``Customer Label Printing``    (non-stock Item, expense account = the
    inventory account) — the ONE Item every label Purchase Invoice bills
    against, whatever customer the batch belongs to.

Why this seeder is NOT gated on the settings fields the way
``accounts_setup.ensure_pos_accounts`` is: that seeder only creates an account
once a settings field names it, and ``settings_defaults`` only seeds a Link
field once the record it names exists. For a brand-new account those two rules
deadlock — the field stays empty because the account is missing, and the
account is never created because the field is empty. So this runs first,
creates the masters unconditionally (create-only, idempotent), and
``settings_defaults._dynamic_defaults`` then finds them and fills the links.

Never raises: an exception here would abort the shared bench migrate.
"""

from __future__ import annotations

from typing import Optional

import frappe

LOGGER_NAME = "label_setup"

INVENTORY_ACCOUNT_NAME = "Labels Inventory"
COGS_ACCOUNT_NAME = "Label Cost"
PRINTING_ITEM_CODE = "Customer Label Printing"
PRINTING_ITEM_GROUP = "Label Printing"

#: Parent account names tried in order, per company. The ERPNext default chart
#: has "Current Assets" and "Cost of Goods Sold"; the fallbacks cover charts
#: that renamed the middle tier.
_INVENTORY_PARENTS = ("Stock Assets", "Current Assets")
_COGS_PARENTS = ("Cost of Goods Sold", "Direct Expenses", "Expenses")


def _logger():
    return frappe.logger(LOGGER_NAME, allow_site=True)


def _resolve_company() -> Optional[str]:
    """Global default company, else the only company. None when ambiguous."""
    try:
        company = frappe.db.get_single_value("Global Defaults", "default_company")
        if company:
            return company
        rows = frappe.get_all("Company", fields=["name"], limit=2)
        if len(rows) == 1:
            return rows[0]["name"]
    except Exception:
        frappe.log_error(frappe.get_traceback(), "label_setup: company resolution failed")
    return None


def resolve_label_account_names(company: Optional[str] = None) -> dict:
    """The fully-qualified account names for *company* (existence not implied)."""
    company = company or _resolve_company()
    if not company:
        return {}
    abbr = frappe.db.get_value("Company", company, "abbr") or ""
    if not abbr:
        return {}
    return {
        "company": company,
        "inventory": f"{INVENTORY_ACCOUNT_NAME} - {abbr}",
        "cogs": f"{COGS_ACCOUNT_NAME} - {abbr}",
    }


def _find_parent(company: str, candidates) -> Optional[str]:
    for name in candidates:
        parent = frappe.db.get_value(
            "Account", {"account_name": name, "company": company, "is_group": 1}, "name"
        )
        if parent:
            return parent
    return None


def _ensure_account(
    *, company: str, account_name: str, parents, root_type: str, report_type: str,
    account_type: str, log: dict,
) -> Optional[str]:
    abbr = frappe.db.get_value("Company", company, "abbr") or ""
    full_name = f"{account_name} - {abbr}"
    if frappe.db.exists("Account", full_name):
        log["existing"].append(full_name)
        return full_name

    parent = _find_parent(company, parents)
    if not parent:
        log["skipped"].append(f"{full_name}: no parent among {parents}")
        return None

    account = frappe.new_doc("Account")
    account.account_name = account_name
    account.company = company
    account.parent_account = parent
    account.root_type = root_type
    account.report_type = report_type
    if account_type:
        account.account_type = account_type
    account.flags.ignore_permissions = True
    account.insert(ignore_permissions=True)
    log["created"].append(full_name)
    return full_name


def _ensure_item_group(log: dict) -> Optional[str]:
    if frappe.db.exists("Item Group", PRINTING_ITEM_GROUP):
        log["existing"].append(f"item group {PRINTING_ITEM_GROUP}")
        return PRINTING_ITEM_GROUP
    root = frappe.db.get_value("Item Group", {"is_group": 1, "parent_item_group": ""}, "name")
    if not root:
        root = "All Item Groups" if frappe.db.exists("Item Group", "All Item Groups") else None
    if not root:
        log["skipped"].append("no root item group found")
        return None
    group = frappe.new_doc("Item Group")
    group.item_group_name = PRINTING_ITEM_GROUP
    group.parent_item_group = root
    group.is_group = 0
    group.flags.ignore_permissions = True
    group.insert(ignore_permissions=True)
    log["created"].append(f"item group {PRINTING_ITEM_GROUP}")
    return PRINTING_ITEM_GROUP


def _ensure_printing_item(company: str, inventory_account: Optional[str], log: dict) -> None:
    """The single non-stock Item label Purchase Invoices bill against.

    ``is_stock_item = 0`` is the whole trick: a PI line for a non-stock item
    debits the line's expense account instead of a warehouse — and this item's
    default expense account is the Labels Inventory asset, so the printer's
    bill lands on the balance sheet, not straight into expenses. Consumption
    later moves it to Label Cost via JE.
    """
    if frappe.db.exists("Item", PRINTING_ITEM_CODE):
        log["existing"].append(f"item {PRINTING_ITEM_CODE}")
        _ensure_item_expense_default(company, inventory_account, log)
        return

    group = _ensure_item_group(log)
    if not group:
        return

    item = frappe.new_doc("Item")
    item.item_code = PRINTING_ITEM_CODE
    item.item_name = PRINTING_ITEM_CODE
    item.item_group = group
    item.stock_uom = "Nos"
    item.is_stock_item = 0
    item.is_sales_item = 0
    item.is_purchase_item = 1
    item.description = (
        "Outsourced printing of customer jar labels. One sheet = one unit. "
        "Billed through Jarz Label Print Orders; do not sell."
    )
    if inventory_account:
        item.append("item_defaults", {"company": company, "expense_account": inventory_account})
    item.flags.ignore_permissions = True
    item.insert(ignore_permissions=True)
    log["created"].append(f"item {PRINTING_ITEM_CODE}")


def _ensure_item_expense_default(company: str, inventory_account: Optional[str], log: dict) -> None:
    """Point an already-existing printing item's expense default at the inventory account.

    Fill-only: a row that already names an expense account is someone's choice.
    """
    if not inventory_account:
        return
    try:
        item = frappe.get_doc("Item", PRINTING_ITEM_CODE)
        row = next((d for d in item.get("item_defaults") or [] if d.company == company), None)
        if row is None:
            item.append("item_defaults", {"company": company, "expense_account": inventory_account})
        elif not (row.expense_account or "").strip():
            row.expense_account = inventory_account
        else:
            return
        item.flags.ignore_permissions = True
        item.save(ignore_permissions=True)
        log["created"].append(f"item {PRINTING_ITEM_CODE} expense default")
    except Exception:
        frappe.log_error(frappe.get_traceback(), "label_setup: item expense default failed")


def ensure_label_accounting():
    """Create the label accounts + printing item. Create-only, never raises."""
    log = {"created": [], "existing": [], "skipped": []}
    try:
        company = _resolve_company()
        if not company:
            log["skipped"].append("no unambiguous company")
            return log

        inventory = _ensure_account(
            company=company,
            account_name=INVENTORY_ACCOUNT_NAME,
            parents=_INVENTORY_PARENTS,
            root_type="Asset",
            report_type="Balance Sheet",
            account_type="",
            log=log,
        )
        _ensure_account(
            company=company,
            account_name=COGS_ACCOUNT_NAME,
            parents=_COGS_PARENTS,
            root_type="Expense",
            report_type="Profit and Loss",
            account_type="Expense Account",
            log=log,
        )
        _ensure_printing_item(company, inventory, log)

        if log["created"]:
            frappe.db.commit()
    except Exception:
        frappe.log_error(frappe.get_traceback(), "label_setup failed")

    # .info() is invisible at the servers' default log level.
    _logger().warning(
        "label_setup: created=%s existing=%s skipped=%s"
        % (log["created"], log["existing"], log["skipped"])
    )
    return log
