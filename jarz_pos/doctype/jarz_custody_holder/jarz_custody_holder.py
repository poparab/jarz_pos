from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint

from jarz_pos.services import cash_custody


class JarzCustodyHolder(Document):
    """An employee who may hold company cash (custody / عهدة).

    The holder owns exactly one Cash ledger under ``Staff Custody - <abbr>``,
    created on insert. The ledger is the custody: issuing, spending and
    returning are all ordinary vouchers against it, and
    ``services.cash_custody.guard_custody_balance`` keeps it from going
    negative whichever path posts them.
    """

    def _fill_from_employee(self) -> None:
        if not self.employee:
            frappe.throw(_("Employee is required."))
        emp = frappe.db.get_value(
            "Employee",
            self.employee,
            ["employee_name", "user_id", "company"],
            as_dict=True,
        )
        if not emp:
            frappe.throw(_("Employee {0} does not exist.").format(self.employee))
        # A NEW holder must have a user: the endpoints recognise the holder by
        # it. An existing one keeps the user it had if the employee's link was
        # removed since, so the holder can still be disabled or corrected.
        if emp.get("user_id"):
            self.user = emp.get("user_id")
        elif self.is_new() or not self.user:
            frappe.throw(
                _("Employee {0} has no linked user. Link a user to the employee before giving them custody.").format(
                    self.employee
                )
            )
        self.employee_name = emp.get("employee_name") or self.employee
        if not self.company:
            self.company = emp.get("company")
        if not self.company:
            frappe.throw(_("Company is required for a custody holder."))

    def before_insert(self) -> None:
        self._fill_from_employee()
        if not self.account:
            self.account = cash_custody.ensure_custody_account(
                self.employee,
                self.employee_name,
                self.company,
                holder_name=self.employee,
            )

    def validate(self) -> None:
        self._fill_from_employee()
        if not self.is_new():
            before = self.get_doc_before_save()
            if before and before.get("account") and before.get("account") != self.account:
                # Re-pointing would drop the old account out of the guard with
                # its money still in it.
                frappe.throw(_("The custody account of a holder cannot be changed."))
        if self.account:
            cash_custody.validate_custody_account(self.account, self.company)
            other = frappe.db.get_value(
                cash_custody.HOLDER_DOCTYPE,
                {"account": self.account, "name": ["!=", self.name or ""]},
                "name",
            )
            if other:
                frappe.throw(_("Account {0} is already the custody of {1}.").format(self.account, other))
        self._guard_disable()

    def _guard_disable(self) -> None:
        if cint(self.enabled) or not self.account:
            return
        before = self.get_doc_before_save()
        was_enabled = before is None or cint(before.get("enabled"))
        if not was_enabled:
            return
        balance = cash_custody.custody_balance(self.account, self.company, for_update=True)
        if abs(balance) > cash_custody.EPSILON:
            frappe.throw(
                _("Custody of {0} still holds {1}. Return it before disabling the holder.").format(
                    self.employee_name or self.employee, f"{balance:,.2f}"
                )
            )

    def on_update(self) -> None:
        cash_custody.clear_custody_cache()

    def on_trash(self) -> None:
        if self.account and cash_custody.account_has_gl_entries(self.account):
            frappe.throw(
                _("Custody of {0} has ledger entries on {1} and cannot be deleted. Disable it instead.").format(
                    self.employee_name or self.employee, self.account
                )
            )
        cash_custody.clear_custody_cache()
