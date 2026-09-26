from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

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


def _account_requires_period(account: Optional[str]) -> bool:
    from jarz_pos.utils.cleanup import ACCOUNT_REQUIRES_PERIOD_FIELD

    if not account or not frappe.db.has_column("Account", ACCOUNT_REQUIRES_PERIOD_FIELD):
        return False
    return bool(frappe.db.get_value("Account", account, ACCOUNT_REQUIRES_PERIOD_FIELD))


def _accrued_expenses_account(company: str) -> str:
    from jarz_pos.constants import ACCOUNTS

    account = frappe.db.get_value(
        "Account",
        {"company": company, "account_name": ACCOUNTS.ACCRUED_EXPENSES, "is_group": 0},
        "name",
    )
    if not account:
        frappe.throw(
            _("Account '{0}' is missing for {1}. Run bench migrate to create it.").format(
                ACCOUNTS.ACCRUED_EXPENSES, company
            )
        )
    return account


def _post_earlier_month_shares(doc, company: str) -> Tuple[float, Optional[str]]:
    """Book the earlier months' share of this payment at each month's end.

    Returns the total booked and the Accrued Expenses account. The payment's
    own Journal Entry debits that total to Accrued Expenses instead of to the
    expense, clearing the liability. ``(0.0, None)`` -- and nothing posted --
    when the whole period falls in the payment's month, or when no period was
    given. A module function rather than a method so ``on_submit`` still runs
    against the plain stand-ins the posting-time tests drive it with.
    """
    from jarz_pos.services.delivery_handling import _strip_je_tag_lookalikes
    from jarz_pos.services.expense_periods import earlier_month_shares

    period_from = getattr(doc, "period_from", None)
    period_to = getattr(doc, "period_to", None)
    if not (period_from and period_to):
        return 0.0, None

    shares = earlier_month_shares(
        flt(doc.amount), getdate(period_from), getdate(period_to), getdate(doc.expense_date or today())
    )
    if not shares:
        return 0.0, None

    accrued = _accrued_expenses_account(company)
    label = _strip_je_tag_lookalikes(doc.reason_label) or doc.reason_account
    names = []
    for share in shares:
        je = frappe.new_doc("Journal Entry")
        je.voucher_type = "Journal Entry"
        je.company = company
        je.posting_date = share.month_end
        je.user_remark = _("{0}: {1} day(s) of {2} to {3} belong to {4}; paid on {5} by expense {6}").format(
            label, share.days, period_from, period_to, share.month, doc.expense_date, doc.name
        )
        je.append(
            "accounts",
            {
                "account": doc.reason_account,
                "debit_in_account_currency": share.amount,
                "credit_in_account_currency": 0,
                "user_remark": _("Accrued, paid later"),
            },
        )
        je.append(
            "accounts",
            {
                "account": accrued,
                "credit_in_account_currency": share.amount,
                "debit_in_account_currency": 0,
                "user_remark": label,
            },
        )
        je.flags.ignore_permissions = True
        je.insert()
        je.submit()
        names.append(je.name)

    doc.db_set("period_journal_entries", "\n".join(names))
    return round(sum(s.amount for s in shares), 2), accrued


class JarzExpenseRequest(Document):
    def before_insert(self):
        if not self.expense_date:
            self.expense_date = today()
        if not self.requested_by:
            self.requested_by = frappe.session.user
        if not self.currency:
            self.currency = frappe.defaults.get_global_default("currency")
        # Every row that existed before the monthly-expenses feature, and every
        # caller that predates it (jarz_pos.api.expenses.create_expense), is an
        # ad-hoc payment. Defaulting here keeps the field non-null without
        # forcing a single existing caller to pass it.
        if not self.expense_kind:
            self.expense_kind = "Ad-hoc"

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

        # `expense_month` is when the money MOVED and is force-derived above on
        # every save. `period_month` is the month being paid FOR, and the two
        # differ whenever a bill is settled late: August rent paid on 3 Sept is
        # expense_month=2026-09, period_month=2026-08. So this must only ever
        # FILL IN a blank — overwriting it would silently re-file every late
        # payment under the month it was made and make "what remains for August"
        # unanswerable.
        if not self.period_month:
            self.period_month = self.expense_month

        self._validate_service_period()

        if self.approved_on:
            try:
                self.approved_on = get_datetime(self.approved_on)
            except Exception:
                self.approved_on = now_datetime()

        if self.requires_approval is None:
            self.requires_approval = 0

        # Provide a simple textual status hint for list views.
        #
        # "Rejected" is checked before the docstatus ladder because a rejected
        # request stays a DRAFT: there is no docstatus for "refused", and
        # submitting one just to cancel it would post the very Journal Entry the
        # manager said no to. The rejection stamp is therefore the only thing
        # that distinguishes it from a request still waiting for an answer.
        if self.docstatus == 0 and (self.rejection_reason or self.rejected_on):
            self.status = "Rejected"
        elif self.docstatus == 0:
            self.status = "Pending Approval" if flt(self.requires_approval) else "Draft"
        elif self.docstatus == 1:
            self.status = "Approved"
        elif self.docstatus == 2:
            self.status = "Cancelled"

    def _validate_service_period(self) -> None:
        """The days a bill covers, when it covers days rather than a moment.

        Required when the reason account is flagged ``custom_jarz_requires_period``
        (paid ads), optional otherwise. ``getattr`` because the fields only exist
        once the DocType has synced; a site mid-migrate must still save expenses.
        """
        from jarz_pos.services.expense_periods import PeriodError, validate_period

        period_from = getattr(self, "period_from", None)
        period_to = getattr(self, "period_to", None)
        if not period_from and not period_to:
            if _account_requires_period(self.reason_account):
                frappe.throw(
                    _("{0} is billed for a period. Enter the first and last day this payment covers.").format(
                        self.reason_label or self.reason_account
                    )
                )
            return
        if not (period_from and period_to):
            frappe.throw(_("Enter both the first and the last day of the period."))
        try:
            validate_period(getdate(period_from), getdate(period_to), getdate(self.expense_date or today()))
        except PeriodError as exc:
            frappe.throw(_(str(exc)))

    def after_insert(self):
        """Tell the managers who can answer this that it is waiting.

        Here rather than in api.expenses.create_expense so EVERY way a
        pending request comes into being is covered by one rule -- the mobile
        endpoint, a Desk entry, and anything added later. The gate is the
        document's own state, not the caller's.

        Fires on insert only, because requires_approval is decided at
        insert on every path that creates one (0 if is_manager else 1), and
        a request that already exists is either answered or still pending --
        neither is a new thing to be told about. Notifying on every save would
        re-alert the whole management team each time a field was corrected.

        Queued, not sent inline. The alert is one blocking HTTP request per
        manager device (nine of them on production today), and after_insert
        runs inside the insert's transaction -- so sending here would make the
        cashier wait through the fan-out with the row still locked, and would
        put an FCM timeout, the VAPID key bootstrap's frappe.db.commit() and
        an Error Log insert all inside the transaction that owns their expense.
        enqueue_after_commit moves every one of those past the commit: the
        expense is durable before the first packet leaves, so no failure in the
        notification can roll back the document it is merely ABOUT.

        Still swallows: a queue that refuses the job must not fail the expense
        either.
        """
        if self.docstatus != 0 or not flt(self.requires_approval):
            return
        # A rejected request is a closed decision, not a pending one. It cannot
        # arrive rejected today, but before_submit already guards the same case
        # and the two must not disagree.
        if self.rejection_reason or self.rejected_on:
            return

        try:
            from jarz_pos.api.notifications import outbound_alerts_suppressed

            # Checked before enqueuing as well as inside the job: CI runs
            # against the live staging site, and a queued job is not rolled
            # back by the tearDown that undoes this row.
            if outbound_alerts_suppressed("expense_approval_required"):
                return

            frappe.enqueue(
                "jarz_pos.api.notifications.send_expense_approval_alert",
                queue="short",
                enqueue_after_commit=True,
                expense=self.name,
            )
        except Exception:
            # defer_insert: if what failed was a DB error, the transaction is
            # already unusable and a direct Error Log insert would raise out of
            # this handler and take the expense down with it.
            frappe.log_error(
                frappe.get_traceback(),
                "expense_approval_notification_failed",
                defer_insert=True,
            )

    def before_submit(self):
        # A rejected request must never become an approved one by a later
        # submit: `on_submit` posts the Journal Entry, so letting this through
        # would spend money a manager explicitly refused. Clearing the rejection
        # first is the documented way back — an un-reject, not a silent submit.
        if self.rejection_reason or self.rejected_on:
            frappe.throw(
                _("Expense {0} was rejected by {1} and cannot be approved. File a new request.").format(
                    self.name, self.rejected_by or _("a manager")
                )
            )
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
        # Sanitised, not passed through. Every `[JARZ-JE:<type>:<key>]` lookup
        # in this app is a company-wide `user_remark LIKE '%<tag>%'`, so ANY
        # submitted Journal Entry in the company whose remark contains that
        # literal satisfies them — including this one, whose remark is free
        # text the requester typed. An expense remark reading
        # `[JARZ-JE:COURIER_SETTLEMENT_REVERSAL:<je>]` makes a real settlement
        # permanently un-reversable; one reading `[JARZ-JE:OFD:<invoice>]` gets
        # this entry cancelled and hard-deleted by the dispatch path.
        from jarz_pos.services.delivery_handling import _strip_je_tag_lookalikes

        je.user_remark = (
            _strip_je_tag_lookalikes(self.remarks)
            or _("Expense {0}").format(self.name)
        )
        # `je.set_posting_time = 1` used to sit here and did nothing at all.
        # That flag only unlocks a `posting_time` field, and ERPNext's Journal
        # Entry has none — neither do Payment Entry and GL Entry. The general
        # ledger is DATE-granular: two entries on the same day have no defined
        # order, whatever time the requester picked, so the flag was a no-op that
        # read as though the choice had been honoured.
        #
        # The chosen time is recorded on `custom_jarz_posting_time` instead
        # (seeded by utils.cleanup.ensure_posting_time_fields in before_migrate).
        # It is provenance for the app's own expense-history screens — "who spent
        # what, when" — NOT ledger ordering, and nothing in accounting reads it.
        # `expense_date` above remains the only thing deciding where this lands
        # in the books, and via `expense_month` which month it is filed under.
        #
        # Routed through utils.posting_datetime rather than assigned by hand, so
        # this doctype and every other ledger writer share ONE definition of the
        # field and ONE existence guard (`frappe.get_meta(dt).get_field(...)`, in
        # `_ledger_time_field_exists`). Two things that would otherwise be
        # re-implemented here and get silently wrong: a `Time` column comes back
        # from the DB as a `datetime.timedelta`, not a string; and a site that has
        # not migrated since this release must still post the expense, with the
        # dropped time LOGGED rather than swallowed.
        #
        # `getattr` on the read side because a Document only carries attributes
        # for fields in its meta: plain `self.expense_time` would raise
        # AttributeError before the DocType has synced, turning a missing display
        # field into a failed expense.
        from jarz_pos.utils.posting_datetime import (
            apply_ledger_posting_datetime,
            join_posting_datetime,
        )

        apply_ledger_posting_datetime(
            je,
            join_posting_datetime(self.expense_date, getattr(self, "expense_time", None)),
        )

        amount = flt(self.amount)
        # A payment for days in earlier months: those months were just booked
        # against Accrued Expenses, and this payment clears that liability. Only
        # the payment month's own days reach the expense account here.
        accrued_total, accrued_account = _post_earlier_month_shares(self, company)
        own_share = round(amount - accrued_total, 2)
        if own_share > 0:
            je.append(
                "accounts",
                {
                    "account": self.reason_account,
                    "debit_in_account_currency": own_share,
                    "credit_in_account_currency": 0,
                    "user_remark": self.payment_source_label,
                },
            )
        if accrued_total > 0:
            je.append(
                "accounts",
                {
                    "account": accrued_account,
                    "debit_in_account_currency": accrued_total,
                    "credit_in_account_currency": 0,
                    "user_remark": _("Clears the accrual for {0} to {1}").format(
                        getattr(self, "period_from", None), getattr(self, "period_to", None)
                    ),
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
        """Reverse the Journal Entry this expense posted.

        Deliberately NOT wrapped in a swallowing ``except``. Cancelling the
        request while its Journal Entry stays submitted is the worst possible
        outcome: the money is still out of the account, the ledger still carries
        the expense, and the only document that explains either now reads
        "Cancelled". If the reversal cannot be posted the whole cancel must fail
        so the caller is told, and Frappe rolls the transaction back.
        """
        # The payment first, then the month-end accruals it cleared. Cancelling
        # only the payment would leave earlier months carrying an expense and
        # Accrued Expenses a liability for a bill that was never paid.
        names = [self.journal_entry] if self.journal_entry else []
        names += [n.strip() for n in (getattr(self, "period_journal_entries", None) or "").splitlines() if n.strip()]
        for name in names:
            if not frappe.db.exists("Journal Entry", name):
                continue
            je = frappe.get_doc("Journal Entry", name)
            if je.docstatus != 1:
                continue
            je.flags.ignore_permissions = True
            je.cancel()

    def before_cancel(self):
        """Move the textual status to Cancelled.

        ``validate`` is the only place that derives ``status``, and Frappe skips
        it on a cancel (``_save`` guards ``_validate`` with
        ``if self._action != "cancel"``). Without this the row keeps reading
        "Approved" after it has been cancelled — every list view and the mobile
        expense card included.
        """
        self.status = "Cancelled"


def on_doctype_update():
    frappe.db.add_index("Jarz Expense Request", ["expense_month"])
    # The monthly-expenses screen queries by the period being paid for, and by
    # the registry item that was paid, on every load.
    frappe.db.add_index("Jarz Expense Request", ["period_month"])
    frappe.db.add_index("Jarz Expense Request", ["recurring_expense"])
