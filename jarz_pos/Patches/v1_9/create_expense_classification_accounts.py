"""Give each kind of delivery and vehicle money its own ledger.

Until 2026-09 one account, ``Freight and Forwarding Charges``, carried four
different things: the courier cost of delivering orders (its real job), the
delivery charge billed to the customer (credited to it, so 2026 showed a
*negative* expense of -108k and ~165k of revenue never reached income), the
delivery paid on purchases, and the owner's car's fuel and upkeep.

This patch only creates the ledgers the code now posts to; it moves no
history. Idempotent: an account that already exists is left as it is, apart
from filling in a bilingual label that is still blank.

    Income  > Direct Income     > Shipping Income
    Expense > Indirect Expenses > Purchase Delivery Charges
    Expense > Indirect Expenses > Vehicle Expenses (group)
                                    > Vehicle Fuel
                                    > Vehicle Maintenance and Repairs
                                    > Vehicle Other Expenses

The vehicle is the owner's, lent to the business: its running costs are
business expenses, but it is not a company asset, so there is no fixed-asset
or depreciation ledger. It does not deliver orders, so its costs sit with
running costs rather than with Freight.
"""

import frappe

from jarz_pos.constants import ACCOUNTS

#: account_name -> (English label, Arabic label)
LABELS = {
    ACCOUNTS.SHIPPING_INCOME: ("Shipping Income (delivery charged to customers)", "إيراد التوصيل (المحصل من العملاء)"),
    ACCOUNTS.PURCHASE_DELIVERY: ("Purchase Delivery Charges", "مصاريف نقل المشتريات"),
    ACCOUNTS.VEHICLE_EXPENSES: ("Vehicle Expenses", "مصاريف العربية"),
    ACCOUNTS.VEHICLE_FUEL: ("Vehicle Fuel", "بنزين العربية"),
    ACCOUNTS.VEHICLE_MAINTENANCE: ("Vehicle Maintenance and Repairs", "صيانة وإصلاح العربية"),
    ACCOUNTS.VEHICLE_OTHER: ("Vehicle Other Expenses (licence, parking, washing)", "مصاريف أخرى للعربية (ترخيص، جراج، غسيل)"),
    ACCOUNTS.FREIGHT_AND_FORWARDING: ("Order Delivery Expense (couriers)", "مصاريف توصيل الطلبات (المناديب)"),
}


def execute():
    for company in frappe.get_all("Company", pluck="name"):
        _for_company(company)


def _for_company(company: str) -> None:
    abbr = frappe.db.get_value("Company", company, "abbr")
    if not abbr:
        return

    income_parent = _group(company, abbr, ACCOUNTS.DIRECT_INCOME) or _root_group(company, "Income")
    expense_parent = _group(company, abbr, ACCOUNTS.INDIRECT_EXPENSES)
    if not income_parent or not expense_parent:
        print(f"create_expense_classification_accounts: {company} has no Direct Income / Indirect Expenses group, skipped")
        return

    freight = f"{ACCOUNTS.FREIGHT_AND_FORWARDING} - {abbr}"
    freight_type = frappe.db.get_value("Account", freight, "account_type") if frappe.db.exists("Account", freight) else None

    # Sequential on purpose: parallel Account inserts deadlock on the nested set.
    _ensure(company, abbr, ACCOUNTS.SHIPPING_INCOME, income_parent, "Income Account")
    _ensure(company, abbr, ACCOUNTS.PURCHASE_DELIVERY, expense_parent, freight_type or "Chargeable")
    vehicle_group = _ensure(company, abbr, ACCOUNTS.VEHICLE_EXPENSES, expense_parent, None, is_group=1)
    for leaf in (ACCOUNTS.VEHICLE_FUEL, ACCOUNTS.VEHICLE_MAINTENANCE, ACCOUNTS.VEHICLE_OTHER):
        _ensure(company, abbr, leaf, vehicle_group, "Expense Account")

    # Freight keeps its name (code resolves it by name) but its label now says
    # what it is for, so nobody picks it for fuel again.
    if frappe.db.exists("Account", freight):
        _fill_labels(freight, ACCOUNTS.FREIGHT_AND_FORWARDING, overwrite=True)


def _group(company: str, abbr: str, account_name: str):
    name = f"{account_name} - {abbr}"
    if frappe.db.get_value("Account", {"name": name, "company": company, "is_group": 1}):
        return name
    return frappe.db.get_value(
        "Account", {"company": company, "account_name": account_name, "is_group": 1}, "name"
    )


def _root_group(company: str, root_type: str):
    rows = frappe.get_all(
        "Account",
        filters={"company": company, "root_type": root_type, "is_group": 1},
        fields=["name"],
        order_by="lft asc",
        limit=1,
    )
    return rows[0].name if rows else None


def _ensure(company, abbr, account_name, parent, account_type, is_group=0):
    name = f"{account_name} - {abbr}"
    if not frappe.db.exists("Account", name):
        doc = frappe.get_doc({
            "doctype": "Account",
            "account_name": account_name,
            "company": company,
            "parent_account": parent,
            "is_group": is_group,
            "account_type": account_type or "",
        })
        doc.flags.ignore_permissions = True
        doc.insert()
        name = doc.name
        print(f"create_expense_classification_accounts: created {name} under {parent}")
    _fill_labels(name, account_name)
    return name


def _fill_labels(name: str, account_name: str, overwrite: bool = False) -> None:
    en, ar = LABELS.get(account_name, (None, None))
    for column, value in (("custom_account_name_en", en), ("custom_account_name_ar", ar)):
        if not value or not frappe.db.has_column("Account", column):
            continue
        if overwrite or not frappe.db.get_value("Account", name, column):
            frappe.db.set_value("Account", name, column, value, update_modified=False)
