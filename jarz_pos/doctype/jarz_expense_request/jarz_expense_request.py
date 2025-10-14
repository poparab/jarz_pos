from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, get_datetime, getdate, now_datetime, today


@dataclass
class _AccountInfo:
    name: str
    company: Optional[str]
    currency: Optional[str]
    root_type: Optional[str]
    is_group: int
    parent_account: Optional[str]


def _get_account_info(account: str) -> _AccountInfo:
    row = frappe.db.get_value(
        "Account",
        account,
        ["name", "company", "account_currency", "root_type", "is_group", "parent_account"],
        as_dict=True,
    )
    if not row:
        frappe.throw(_("Account not found: {0}").format(account))
    return _AccountInfo(
        name=row.get("name") or account,
        company=row.get("company"),
        currency=row.get("account_currency"),
        root_type=row.get("root_type"),
        is_group=int(row.get("is_group") or 0),
        parent_account=row.get("parent_account"),
    )


def _validate_indirect_expense(account_info: _AccountInfo) -> None:
    if account_info.is_group:
        frappe.throw(_("Expense reason must be a ledger account (not a group)."))
    if (account_info.root_type or "").lower() != "expense":
        frappe.throw(_("Expense reason must be an Expense type account."))
    # Ensure the account sits under an Indirect Expenses parent somewhere in the tree
    parent = account_info.parent_account
    checked: set[str] = set()
    while parent and parent not in checked:
        checked.add(parent)
        if frappe.db.exists(
            "Account",
            {"name": parent, "account_name": ["in", ["Indirect Expenses", "Indirect Expense"]]},
        ):
            return
        parent = frappe.db.get_value("Account", parent, "parent_account")
    frappe.throw(_("Selected expense reason must belong under the Indirect Expenses group."))


class JarzExpenseRequest(Document):
    def before_insert(self):
        if not self.expense_date:
            self.expense_date = today()
        if not self.requested_by:
            self.requested_by = frappe.session.user
        if not self.currency:
            self.currency = frappe.defaults.get_global_default("currency")

    def validate(self):
        try:
            self.amount = flt(self.amount)
        except Exception as exc:
            frappe.throw(_("Invalid amount: {0}").format(exc))
        if self.amount <= 0:
            frappe.throw(_("Amount must be greater than zero."))

        if not self.reason_account:
            frappe.throw(_("Reason (expense account) is required."))
        if not self.paying_account:
            frappe.throw(_("Paying account is required."))

        reason_info = _get_account_info(self.reason_account)
        paying_info = _get_account_info(self.paying_account)
        _validate_indirect_expense(reason_info)

        if paying_info.is_group:
            frappe.throw(_("Paying account must be a ledger (not a group)."))

        if reason_info.company and paying_info.company and reason_info.company != paying_info.company:
            frappe.throw(_("Reason and paying accounts must belong to the same company."))

        company = paying_info.company or reason_info.company
        if company:
            self.company = company
        if not self.currency:
            self.currency = paying_info.currency or reason_info.currency or frappe.defaults.get_global_default("currency")

        if not self.reason_label:
            self.reason_label = frappe.db.get_value("Account", reason_info.name, "account_name") or reason_info.name
        if not self.payment_source_label:
            self.payment_source_label = frappe.db.get_value("Account", paying_info.name, "account_name") or paying_info.name

        if self.expense_date:
            month_key = getdate(self.expense_date).strftime("%Y-%m")
            self.expense_month = month_key

        if self.approved_on:
            try:
                self.approved_on = get_datetime(self.approved_on)
            except Exception:
                self.approved_on = now_datetime()

        if self.requires_approval is None:
            self.requires_approval = 0

        # Provide a simple textual status hint for list views
        if self.docstatus == 0:
            self.status = "Pending Approval" if flt(self.requires_approval) else "Draft"
        elif self.docstatus == 1:
            self.status = "Approved"
        elif self.docstatus == 2:
            self.status = "Cancelled"

    def before_submit(self):
        if not self.approved_by:
            self.approved_by = frappe.session.user
        if not self.approved_on:
            self.approved_on = now_datetime()
        self.requires_approval = 0

    def on_submit(self):
        if self.journal_entry:
            return

        company = self.company or frappe.defaults.get_user_default("Company")
        if not company:
            frappe.throw(_("Company is required to create the journal entry."))

        je = frappe.new_doc("Journal Entry")
        je.voucher_type = "Journal Entry"
        je.company = company
        je.posting_date = self.expense_date or today()
        je.user_remark = self.remarks or _("Expense {0}").format(self.name)
        je.set_posting_time = 1

        amount = flt(self.amount)
        je.append(
            "accounts",
            {
                "account": self.reason_account,
                "debit_in_account_currency": amount,
                "credit_in_account_currency": 0,
                "user_remark": self.payment_source_label,
            },
        )
        je.append(
            "accounts",
            {
                "account": self.paying_account,
                "credit_in_account_currency": amount,
                "debit_in_account_currency": 0,
                "user_remark": self.reason_label,
            },
        )

        je.flags.ignore_permissions = True
        je.insert()
        je.submit()
        self.db_set("journal_entry", je.name)

    def on_cancel(self):
        if self.journal_entry and frappe.db.exists("Journal Entry", self.journal_entry):
            try:
                je = frappe.get_doc("Journal Entry", self.journal_entry)
                je.flags.ignore_permissions = True
                if je.docstatus == 1:
                    je.cancel()
            except Exception:
                pass


def on_doctype_update():
    frappe.db.add_index("Jarz Expense Request", ["expense_month"])
