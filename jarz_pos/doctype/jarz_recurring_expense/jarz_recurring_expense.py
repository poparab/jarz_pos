"""Jarz Recurring Expense — registry of non-payroll recurring costs.

This DocType is deliberately **not** a place to record employee salaries.
Payroll is owned by HRMS (`Salary Structure Assignment`) and is read live by
`jarz_pos.api.recurring_expenses`, so there is no second salary store to
reconcile or migrate later.

What belongs here: rent, utilities, telecom, SaaS subscriptions, professional
retainers, insurance, licences and other costs that repeat on a fixed cadence.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, getdate, today

from jarz_pos.utils.recurring_amount import plan_history_change

# Number of months each occurrence covers, used to normalise to a monthly run-rate.
FREQUENCY_MONTHS = {
	"Monthly": 1,
	"Quarterly": 3,
	"Semi-Annual": 6,
	"Annual": 12,
}

# Kept in step with the `naming_series` field's options in the DocType JSON.
DEFAULT_NAMING_SERIES = "JREXP-.#####"


class JarzRecurringExpense(Document):
	def before_insert(self):
		# The form fills `naming_series` from its default client-side, but API
		# inserts and Data Import do not — leaving those paths to fail autoname
		# with "Naming Series mandatory". Fill it here so every path names alike.
		if not self.naming_series:
			self.naming_series = DEFAULT_NAMING_SERIES

	def validate(self):
		self._validate_amount()
		self._validate_period()
		self._record_amount_history()
		self._validate_day_of_month()
		self._validate_expense_account()
		self._set_defaults()
		self._compute_monthly_equivalent()

	# ── validation ────────────────────────────────────────────────────────

	def _validate_amount(self) -> None:
		self.amount = flt(self.amount)
		if self.amount <= 0:
			frappe.throw(_("Amount per occurrence must be greater than zero."))

	def _record_amount_history(self) -> None:
		"""Keep the dated amount list in step with ``amount``.

		A raise is recorded from ``flags.amount_effective_from`` (the API sets it)
		or, failing that, from the current month, so past months keep the amount
		that was due then — including for an old app build that only edits
		``amount``. ``amount`` is then set to the rate in force today.
		"""
		before = None if self.is_new() else self.get_doc_before_save()
		old_amount = (
			before.amount
			if before
			else (None if self.is_new() else frappe.db.get_value(self.doctype, self.name, "amount"))
		)
		rows = [
			{"effective_from": r.effective_from, "amount": r.amount, "note": r.note}
			for r in self.get("amount_history") or []
		]
		notes = {str(getdate(r["effective_from"])): r.get("note") for r in rows if r.get("effective_from")}
		try:
			history, amount_today = plan_history_change(
				rows,
				is_new=self.is_new(),
				old_amount=old_amount,
				new_amount=self.amount,
				start_date=self.start_date,
				effective_from=self.flags.get("amount_effective_from"),
				today_date=getdate(today()),
			)
		except ValueError as exc:
			frappe.throw(_(str(exc)))
		self.set("amount_history", [])
		for entry in history:
			self.append(
				"amount_history",
				{
					"effective_from": entry["effective_from"],
					"amount": entry["amount"],
					"note": notes.get(str(entry["effective_from"])),
				},
			)
		self.amount = amount_today

	def _validate_period(self) -> None:
		if not self.start_date:
			frappe.throw(_("Start date is required."))
		if self.end_date and getdate(self.end_date) < getdate(self.start_date):
			frappe.throw(_("End date cannot be before start date."))

	def _validate_day_of_month(self) -> None:
		if self.day_of_month in (None, ""):
			return
		day = int(self.day_of_month)
		if day < 1 or day > 31:
			frappe.throw(_("Due day of month must be between 1 and 31."))

	def _validate_expense_account(self) -> None:
		if not self.expense_account:
			frappe.throw(_("Expense account is required."))

		account = frappe.db.get_value(
			"Account",
			self.expense_account,
			["root_type", "is_group", "company", "account_currency"],
			as_dict=True,
		)
		if not account:
			frappe.throw(_("Account not found: {0}").format(self.expense_account))
		if int(account.get("is_group") or 0):
			frappe.throw(_("Expense account must be a ledger account, not a group."))
		if (account.get("root_type") or "").lower() != "expense":
			frappe.throw(_("Expense account must be an account of root type Expense."))
		if self.company and account.get("company") and account["company"] != self.company:
			frappe.throw(
				_("Expense account {0} belongs to company {1}, not {2}.").format(
					self.expense_account, account["company"], self.company
				)
			)

	# ── derived values ────────────────────────────────────────────────────

	def _set_defaults(self) -> None:
		if not self.company:
			self.company = frappe.defaults.get_user_default("Company")
		if not self.currency:
			self.currency = (
				frappe.db.get_value("Company", self.company, "default_currency") if self.company else None
			) or frappe.defaults.get_global_default("currency")

	def _compute_monthly_equivalent(self) -> None:
		months = FREQUENCY_MONTHS.get(self.frequency or "Monthly", 1)
		self.monthly_equivalent = flt(self.amount) / months
