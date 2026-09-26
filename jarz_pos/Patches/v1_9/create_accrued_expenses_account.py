"""Create the ledger that carries an earlier month's share of a bill paid later.

Meta bills paid ads in arrears: the charge on 5 October covers 25 September to
5 October. Booked whole on the 5th, September's ads land in October's profit.
``JarzExpenseRequest.on_submit`` now splits such a payment by the days it
covers, booking each earlier month's share at that month's end against this
liability, which the payment itself then clears.

Idempotent and create-only:

    Liability > Current Liabilities > Accrued Expenses

It also switches on ``custom_jarz_requires_period`` for the paid-ads ledger
(created by hand on 2026-09-26), so the Expenses screen asks which days a Meta
charge covers. Any other ledger billed in arrears can be flagged in Desk.
"""

import frappe

from jarz_pos.constants import ACCOUNTS
from jarz_pos.utils.cleanup import ACCOUNT_REQUIRES_PERIOD_FIELD

LABELS = (
    "Accrued Expenses (bills for past days not yet paid)",
    "مصروفات مستحقة (فواتير عن أيام سابقة لم تُدفع بعد)",
)


def execute():
    for company in frappe.get_all("Company", pluck="name"):
        _for_company(company)


def _for_company(company: str) -> None:
    abbr = frappe.db.get_value("Company", company, "abbr")
    if not abbr:
        return

    parent = frappe.db.get_value(
        "Account",
        {"company": company, "account_name": ACCOUNTS.CURRENT_LIABILITIES, "is_group": 1},
        "name",
    )
    if not parent:
        print(f"create_accrued_expenses_account: {company} has no Current Liabilities group, skipped")
    else:
        name = f"{ACCOUNTS.ACCRUED_EXPENSES} - {abbr}"
        if not frappe.db.exists("Account", name):
            doc = frappe.get_doc({
                "doctype": "Account",
                "account_name": ACCOUNTS.ACCRUED_EXPENSES,
                "company": company,
                "parent_account": parent,
                "is_group": 0,
                "account_type": "",
            })
            doc.flags.ignore_permissions = True
            doc.insert()
            name = doc.name
            print(f"create_accrued_expenses_account: created {name} under {parent}")
        for column, value in zip(("custom_account_name_en", "custom_account_name_ar"), LABELS):
            if frappe.db.has_column("Account", column) and not frappe.db.get_value("Account", name, column):
                frappe.db.set_value("Account", name, column, value, update_modified=False)

    if frappe.db.has_column("Account", ACCOUNT_REQUIRES_PERIOD_FIELD):
        for ads in frappe.get_all(
            "Account",
            filters={"company": company, "account_name": ACCOUNTS.PAID_ADS, "is_group": 0},
            pluck="name",
        ):
            frappe.db.set_value("Account", ads, ACCOUNT_REQUIRES_PERIOD_FIELD, 1, update_modified=False)
