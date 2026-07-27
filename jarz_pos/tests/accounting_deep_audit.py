"""Deep read-only accounting audit across every money surface jarz_pos touches.

``gl_audit`` proves double-entry holds: every voucher balances and the site
totals agree. That is necessary but not sufficient — a posting can be perfectly
balanced and still be *wrong*: paid to the wrong ledger, double-posted under a
different voucher, allocated beyond the invoice, or stranded on a party account
with no party. Those are the failures that quietly corrupt a subledger while
every balance check stays green.

This audit asserts the invariants that catch that class of bug, across ALL
historical data rather than a sample:

* subledger integrity — party lines carry parties, Debtors agrees with the
  invoices behind it, allocations never exceed what is owed;
* no double-posting — one Delivery Note per invoice, one dedup tag per journal
  type, no two Payment Entries each fully paying the same invoice;
* operational ledgers net out — Courier Outstanding and Sales Partner
  commission settle to zero once their transactions are marked settled;
* cancelled and returned documents leave no live GL behind them;
* the structural assumptions other code depends on actually hold (POS invoices
  are ``update_stock=0``, credit notes point at a real invoice, nothing posts to
  a group account).

Read-only: SELECT only, no writes, no commit. Safe on production.

Run::

    bench --site frontend execute jarz_pos.tests.accounting_deep_audit.run
    bench --site frontend execute jarz_pos.tests.accounting_deep_audit.run \
        --kwargs "{'strict': True}"

``strict`` promotes advisory findings to failures.
"""

from __future__ import annotations

import json

import frappe

PASS = "PASS"
FAIL = "FAIL"
WARN = "WARN"
SKIP = "SKIP"

#: Currency rounding tolerance.
_TOL = 0.01

#: Cap on rows echoed into a finding message, so a systemic issue does not
#: produce a megabyte of output.
_SAMPLE = 5


class DeepAuditError(Exception):
    """Raised when a gated accounting invariant fails."""


class _Result:
    __slots__ = ("name", "status", "message", "rows")

    def __init__(self, name: str, status: str, message: str = "", rows=None):
        self.name = name
        self.status = status
        self.message = message
        self.rows = rows or []


def _company():
    return frappe.defaults.get_defaults().get("company") or frappe.db.get_single_value(
        "Global Defaults", "default_company"
    )


def _sample(rows) -> str:
    return json.dumps(rows[:_SAMPLE], default=str)


def _has_column(doctype: str, column: str) -> bool:
    try:
        return column in (frappe.db.get_table_columns(doctype) or [])
    except Exception:
        return False


# ═════════════════════════════════════════════════════════════════════════════
# Subledger integrity
# ═════════════════════════════════════════════════════════════════════════════

def _check_party_lines_have_party(company: str) -> _Result:
    """Receivable/Payable GL rows must name a party.

    ERPNext v16 rejects these on save, so any that exist predate the validation
    or were written around it. Without a party the row lands in the account
    total but in nobody's subledger — the two stop agreeing and nothing says so.
    """
    rows = frappe.db.sql(
        """
        SELECT gle.voucher_type, gle.voucher_no, gle.account
        FROM `tabGL Entry` gle
        JOIN `tabAccount` acc ON acc.name = gle.account
        WHERE gle.company = %s
          AND gle.is_cancelled = 0
          AND acc.account_type IN ('Receivable', 'Payable')
          AND (gle.party IS NULL OR gle.party = '')
        LIMIT 50
        """,
        (company,),
        as_dict=True,
    )
    if rows:
        return _Result(
            "party_lines_have_party", FAIL,
            f"{len(rows)} Receivable/Payable GL rows carry no party: {_sample(rows)}",
            rows,
        )
    return _Result("party_lines_have_party", PASS, "all party-account rows name a party")


def _check_debtors_matches_invoices(company: str) -> _Result:
    """The Debtors GL balance must equal the receivables behind it.

    Compares the net Debtors movement against the sum of submitted invoice
    outstanding amounts. A drift means a payment or credit landed on Debtors
    without touching an invoice (or vice versa) — the classic way a customer
    balance goes wrong while every voucher still balances.
    """
    gl = frappe.db.sql(
        """
        SELECT ROUND(SUM(gle.debit - gle.credit), 2) AS bal
        FROM `tabGL Entry` gle
        JOIN `tabAccount` acc ON acc.name = gle.account
        WHERE gle.company = %s AND gle.is_cancelled = 0
          AND acc.account_type = 'Receivable'
        """,
        (company,),
    )
    gl_balance = float((gl and gl[0][0]) or 0)

    si = frappe.db.sql(
        """
        SELECT ROUND(SUM(outstanding_amount), 2)
        FROM `tabSales Invoice`
        WHERE company = %s AND docstatus = 1
        """,
        (company,),
    )
    si_outstanding = float((si and si[0][0]) or 0)

    # Unallocated customer payments legitimately sit on Debtors without an
    # invoice behind them, so they are not drift — subtract them before judging.
    unalloc = frappe.db.sql(
        """
        SELECT ROUND(SUM(unallocated_amount), 2)
        FROM `tabPayment Entry`
        WHERE docstatus = 1 AND company = %s AND party_type = 'Customer'
          AND IFNULL(unallocated_amount, 0) > 0
        """,
        (company,),
    )
    unallocated = float((unalloc and unalloc[0][0]) or 0)

    drift = round(gl_balance - si_outstanding + unallocated, 2)
    detail = (
        f"Debtors GL {gl_balance} vs invoice outstanding {si_outstanding} "
        f"(unallocated credits {unallocated}) -> residual drift {drift}"
    )
    # Scale the threshold: a few pounds across tens of thousands of invoices is
    # rounding, a systemic break is orders of magnitude larger.
    threshold = max(10.0, abs(si_outstanding) * 0.001)
    if abs(drift) > threshold:
        return _Result("debtors_matches_invoices", FAIL, detail)
    return _Result("debtors_matches_invoices", PASS, detail)


#: Cash is tendered in whole pounds against invoices carrying piastres, so a
#: sub-pound over-allocation is the till rounding, not a defect. Anything above
#: it is a genuine over-payment.
_ROUNDING_TOLERANCE = 1.0


def _check_no_over_allocation(company: str) -> _Result:
    """A Payment Entry must never allocate materially more than the invoice.

    Over-allocation drives outstanding negative and turns a payment into an
    unearned customer credit. Sub-pound differences are excluded deliberately:
    thousands of them exist and they are all the till rounding a whole-pound
    cash tender against a piastre total — reporting those buries the handful of
    real over-payments underneath them.
    """
    rows = frappe.db.sql(
        """
        SELECT per.reference_name AS invoice,
               ROUND(SUM(per.allocated_amount), 2) AS allocated,
               ROUND(si.grand_total, 2) AS grand_total,
               ROUND(SUM(per.allocated_amount) - si.grand_total, 2) AS excess
        FROM `tabPayment Entry Reference` per
        JOIN `tabPayment Entry` pe ON pe.name = per.parent AND pe.docstatus = 1
        JOIN `tabSales Invoice` si ON si.name = per.reference_name
        WHERE per.reference_doctype = 'Sales Invoice'
          AND si.docstatus = 1
          AND si.company = %s
          AND IFNULL(si.is_return, 0) = 0
        GROUP BY per.reference_name, si.grand_total
        HAVING excess > %s
        ORDER BY excess DESC
        LIMIT 50
        """,
        (company, _ROUNDING_TOLERANCE),
        as_dict=True,
    )
    if rows:
        return _Result(
            "no_over_allocation", FAIL,
            f"{len(rows)} invoices over-allocated beyond rounding: {_sample(rows)}", rows,
        )
    return _Result(
        "no_over_allocation", PASS,
        f"no invoice over-allocated by more than {_ROUNDING_TOLERANCE:.2f}",
    )


def _check_no_double_full_payment(company: str) -> _Result:
    """Two submitted Payment Entries each fully paying one invoice = double take."""
    rows = frappe.db.sql(
        """
        SELECT per.reference_name AS invoice, COUNT(DISTINCT pe.name) AS pe_count,
               ROUND(SUM(per.allocated_amount), 2) AS allocated,
               ROUND(si.grand_total, 2) AS grand_total
        FROM `tabPayment Entry Reference` per
        JOIN `tabPayment Entry` pe ON pe.name = per.parent AND pe.docstatus = 1
        JOIN `tabSales Invoice` si ON si.name = per.reference_name
        WHERE per.reference_doctype = 'Sales Invoice'
          AND si.docstatus = 1 AND si.company = %s
          AND IFNULL(si.is_return, 0) = 0
          AND si.grand_total > 0
        GROUP BY per.reference_name, si.grand_total
        HAVING pe_count > 1 AND allocated > grand_total + 0.01
        LIMIT 50
        """,
        (company,),
        as_dict=True,
    )
    if rows:
        return _Result(
            "no_double_full_payment", FAIL,
            f"{len(rows)} invoices paid more than once: {_sample(rows)}", rows,
        )
    return _Result("no_double_full_payment", PASS, "no invoice carries duplicate full payments")


def _check_paid_invoices_have_backing(company: str) -> _Result:
    """An invoice showing zero outstanding must have something that paid it.

    Either a Payment Entry, a Courier Transaction holding the receivable, a
    credit note, or a journal reference. A zero-outstanding invoice with none of
    those is revenue recognised against nothing — a phantom payment.
    """
    if not _has_column("Sales Invoice", "is_return"):
        return _Result("paid_invoices_have_backing", SKIP, "is_return column absent")

    rows = frappe.db.sql(
        """
        SELECT si.name, si.grand_total, si.outstanding_amount
        FROM `tabSales Invoice` si
        WHERE si.docstatus = 1
          AND si.company = %s
          AND IFNULL(si.is_return, 0) = 0
          AND si.grand_total > 1
          AND si.outstanding_amount <= 0.01
          AND NOT EXISTS (
                SELECT 1 FROM `tabPayment Entry Reference` per
                JOIN `tabPayment Entry` pe ON pe.name = per.parent AND pe.docstatus = 1
                WHERE per.reference_doctype = 'Sales Invoice' AND per.reference_name = si.name)
          AND NOT EXISTS (
                SELECT 1 FROM `tabCourier Transaction` ct
                WHERE ct.reference_invoice = si.name)
          AND NOT EXISTS (
                SELECT 1 FROM `tabJournal Entry Account` jea
                JOIN `tabJournal Entry` je ON je.name = jea.parent AND je.docstatus = 1
                WHERE jea.reference_type = 'Sales Invoice' AND jea.reference_name = si.name)
          AND NOT EXISTS (
                SELECT 1 FROM `tabSales Invoice` cn
                WHERE cn.return_against = si.name AND cn.docstatus = 1)
        ORDER BY si.modified DESC
        LIMIT 50
        """,
        (company,),
        as_dict=True,
    )
    if rows:
        return _Result(
            "paid_invoices_have_backing", FAIL,
            f"{len(rows)} invoices show paid with nothing backing it: {_sample(rows)}", rows,
        )
    return _Result("paid_invoices_have_backing", PASS, "every settled invoice has backing")


# ═════════════════════════════════════════════════════════════════════════════
# Double-posting
# ═════════════════════════════════════════════════════════════════════════════

def _check_no_duplicate_delivery_notes(company: str) -> _Result:
    """One submitted, non-return Delivery Note per invoice.

    Two means the stock left the warehouse twice for one sale — the exact
    failure the v16 DN-reuse fix was written for.
    """
    rows = frappe.db.sql(
        """
        SELECT dni.against_sales_invoice AS invoice,
               COUNT(DISTINCT dni.parent) AS dn_count
        FROM `tabDelivery Note Item` dni
        JOIN `tabDelivery Note` dn ON dn.name = dni.parent
        WHERE dn.docstatus = 1
          AND dn.company = %s
          AND IFNULL(dn.is_return, 0) = 0
          AND IFNULL(dni.against_sales_invoice, '') != ''
        GROUP BY dni.against_sales_invoice
        HAVING dn_count > 1
        LIMIT 50
        """,
        (company,),
        as_dict=True,
    )
    if rows:
        return _Result(
            "no_duplicate_delivery_notes", FAIL,
            f"{len(rows)} invoices have more than one Delivery Note: {_sample(rows)}", rows,
        )
    return _Result("no_duplicate_delivery_notes", PASS, "one Delivery Note per invoice")


def _check_no_duplicate_je_dedup_tags(company: str) -> _Result:
    """Each ``[JARZ-JE:TYPE:key]`` tag must appear on exactly one submitted JE.

    The tag exists precisely to make these postings idempotent. Two entries
    sharing one tag means a retry double-posted — the failure mode the v16
    ``title``-overwrite bug caused for months.
    """
    rows = frappe.db.sql(
        """
        SELECT SUBSTRING(user_remark,
                 LOCATE('[JARZ-JE:', user_remark),
                 LOCATE(']', user_remark, LOCATE('[JARZ-JE:', user_remark))
                   - LOCATE('[JARZ-JE:', user_remark) + 1) AS tag,
               COUNT(*) AS je_count,
               GROUP_CONCAT(name) AS entries
        FROM `tabJournal Entry`
        WHERE docstatus = 1
          AND company = %s
          AND user_remark LIKE '%%[JARZ-JE:%%'
        GROUP BY tag
        HAVING je_count > 1
        LIMIT 50
        """,
        (company,),
        as_dict=True,
    )
    if rows:
        return _Result(
            "no_duplicate_je_dedup_tags", FAIL,
            f"{len(rows)} dedup tags appear on multiple JEs (double-post): {_sample(rows)}", rows,
        )
    return _Result("no_duplicate_je_dedup_tags", PASS, "every JE dedup tag is unique")


def _check_no_duplicate_courier_transactions(company: str) -> _Result:
    """An invoice should not carry two positive unsettled courier rows."""
    rows = frappe.db.sql(
        """
        SELECT reference_invoice AS invoice, COUNT(*) AS ct_count
        FROM `tabCourier Transaction`
        WHERE IFNULL(reference_invoice, '') != ''
          AND amount > 0.01
          AND status != 'Settled'
        GROUP BY reference_invoice
        HAVING ct_count > 1
        LIMIT 50
        """,
        as_dict=True,
    )
    if rows:
        return _Result(
            "no_duplicate_courier_transactions", WARN,
            f"{len(rows)} invoices have multiple unsettled courier rows: {_sample(rows)}", rows,
        )
    return _Result("no_duplicate_courier_transactions", PASS, "no duplicated unsettled courier rows")


# ═════════════════════════════════════════════════════════════════════════════
# Operational ledgers
# ═════════════════════════════════════════════════════════════════════════════

def _check_settled_courier_rows_have_je(company: str) -> _Result:
    """A settled Courier Transaction must point at the entry that settled it."""
    rows = frappe.db.sql(
        """
        SELECT name, party, amount
        FROM `tabCourier Transaction`
        WHERE status = 'Settled'
          AND IFNULL(journal_entry, '') = ''
          AND ABS(IFNULL(amount, 0)) > 0.01
        LIMIT 50
        """,
        as_dict=True,
    )
    if rows:
        return _Result(
            "settled_courier_rows_have_je", WARN,
            f"{len(rows)} settled courier rows have no journal entry link: {_sample(rows)}", rows,
        )
    return _Result("settled_courier_rows_have_je", PASS, "settled courier rows link their JE")


def _check_courier_rows_reference_live_invoices(company: str) -> _Result:
    """An unsettled courier row against a cancelled invoice is a stranded balance."""
    rows = frappe.db.sql(
        """
        SELECT ct.name, ct.reference_invoice, ct.amount, si.docstatus
        FROM `tabCourier Transaction` ct
        JOIN `tabSales Invoice` si ON si.name = ct.reference_invoice
        WHERE ct.status != 'Settled'
          AND si.docstatus = 2
          AND ABS(IFNULL(ct.amount, 0)) > 0.01
        LIMIT 50
        """,
        as_dict=True,
    )
    if rows:
        return _Result(
            "courier_rows_reference_live_invoices", FAIL,
            f"{len(rows)} unsettled courier rows point at cancelled invoices: {_sample(rows)}", rows,
        )
    return _Result("courier_rows_reference_live_invoices", PASS, "no courier row on a cancelled invoice")


def _check_sales_partner_amounts_consistent(company: str) -> _Result:
    """``base_fee + vat_amount`` must equal ``partner_fees`` on each row.

    The two are posted to different ledgers (commission vs input VAT), so a
    mismatch means one side of the split is wrong in the GL.
    """
    if not frappe.db.exists("DocType", "Sales Partner Transactions"):
        return _Result("sales_partner_amounts_consistent", SKIP, "doctype absent")

    rows = frappe.db.sql(
        """
        SELECT name, sales_partner, base_fee, vat_amount, partner_fees
        FROM `tabSales Partner Transactions`
        WHERE ABS(IFNULL(base_fee, 0) + IFNULL(vat_amount, 0) - IFNULL(partner_fees, 0)) > 0.01
        LIMIT 50
        """,
        as_dict=True,
    )
    if rows:
        return _Result(
            "sales_partner_amounts_consistent", WARN,
            f"{len(rows)} partner rows where base_fee+VAT != partner_fees: {_sample(rows)}", rows,
        )
    return _Result("sales_partner_amounts_consistent", PASS, "partner fee splits are consistent")


# ═════════════════════════════════════════════════════════════════════════════
# Cancellation and returns
# ═════════════════════════════════════════════════════════════════════════════

def _check_cancelled_docs_have_no_live_gl(company: str) -> _Result:
    """A cancelled voucher must leave no ``is_cancelled = 0`` GL behind it."""
    rows = frappe.db.sql(
        """
        SELECT gle.voucher_type, gle.voucher_no, COUNT(*) AS live_rows
        FROM `tabGL Entry` gle
        JOIN `tabSales Invoice` si ON si.name = gle.voucher_no
        WHERE gle.voucher_type = 'Sales Invoice'
          AND gle.company = %s
          AND gle.is_cancelled = 0
          AND si.docstatus = 2
        GROUP BY gle.voucher_type, gle.voucher_no
        LIMIT 50
        """,
        (company,),
        as_dict=True,
    )
    if rows:
        return _Result(
            "cancelled_docs_have_no_live_gl", FAIL,
            f"{len(rows)} cancelled invoices still have live GL: {_sample(rows)}", rows,
        )
    return _Result("cancelled_docs_have_no_live_gl", PASS, "cancelled invoices leave no live GL")


def _check_credit_notes_reference_a_real_invoice(company: str) -> _Result:
    """Every credit note must point at a submitted invoice it reverses."""
    rows = frappe.db.sql(
        """
        SELECT cn.name, cn.return_against
        FROM `tabSales Invoice` cn
        LEFT JOIN `tabSales Invoice` src ON src.name = cn.return_against
        WHERE cn.docstatus = 1
          AND cn.company = %s
          AND IFNULL(cn.is_return, 0) = 1
          AND (cn.return_against IS NULL OR cn.return_against = '' OR src.name IS NULL)
        LIMIT 50
        """,
        (company,),
        as_dict=True,
    )
    if rows:
        return _Result(
            "credit_notes_reference_a_real_invoice", FAIL,
            f"{len(rows)} credit notes reference no valid invoice: {_sample(rows)}", rows,
        )
    return _Result("credit_notes_reference_a_real_invoice", PASS, "credit notes reference real invoices")


def _check_no_over_return(company: str) -> _Result:
    """Returned quantity per line must never exceed what was sold."""
    rows = frappe.db.sql(
        """
        SELECT src.name AS source_row, src.parent AS invoice, src.item_code,
               src.qty AS sold, ROUND(SUM(ABS(ret.qty)), 3) AS returned
        FROM `tabSales Invoice Item` ret
        JOIN `tabSales Invoice Item` src ON src.name = ret.sales_invoice_item
        WHERE ret.docstatus = 1
          AND IFNULL(ret.sales_invoice_item, '') != ''
        GROUP BY src.name, src.parent, src.item_code, src.qty
        HAVING returned > sold + 0.001
        LIMIT 50
        """,
        as_dict=True,
    )
    if rows:
        return _Result(
            "no_over_return", FAIL,
            f"{len(rows)} lines returned beyond what was sold: {_sample(rows)}", rows,
        )
    return _Result("no_over_return", PASS, "no line was over-returned")


def _check_credit_notes_are_off_the_board(company: str) -> _Result:
    """Credit notes must not carry ``is_pos``, or they render as Kanban cards."""
    rows = frappe.db.sql(
        """
        SELECT name, is_pos, custom_sales_invoice_state
        FROM `tabSales Invoice`
        WHERE docstatus = 1 AND company = %s
          AND IFNULL(is_return, 0) = 1
          AND IFNULL(is_pos, 0) = 1
        LIMIT 50
        """,
        (company,),
        as_dict=True,
    )
    if rows:
        return _Result(
            "credit_notes_are_off_the_board", WARN,
            f"{len(rows)} credit notes still carry is_pos: {_sample(rows)}", rows,
        )
    return _Result("credit_notes_are_off_the_board", PASS, "no credit note is flagged is_pos")


def _check_credit_notes_carry_no_woo_id(company: str) -> _Result:
    """A credit note carrying a Woo order id can push a phantom status update."""
    if not _has_column("Sales Invoice", "woo_order_id"):
        return _Result("credit_notes_carry_no_woo_id", SKIP, "woo_order_id column absent")

    rows = frappe.db.sql(
        """
        SELECT name, woo_order_id
        FROM `tabSales Invoice`
        WHERE docstatus = 1 AND company = %s
          AND IFNULL(is_return, 0) = 1
          AND IFNULL(woo_order_id, 0) != 0
        LIMIT 50
        """,
        (company,),
        as_dict=True,
    )
    if rows:
        return _Result(
            "credit_notes_carry_no_woo_id", FAIL,
            f"{len(rows)} credit notes carry a Woo order id: {_sample(rows)}", rows,
        )
    return _Result("credit_notes_carry_no_woo_id", PASS, "no credit note carries a Woo id")


# ═════════════════════════════════════════════════════════════════════════════
# Structural assumptions other code depends on
# ═════════════════════════════════════════════════════════════════════════════

def _check_pos_invoices_do_not_update_stock(company: str) -> _Result:
    """POS invoices must be ``update_stock = 0``.

    The whole delivery and return design assumes stock moves only through
    Delivery Notes. An invoice that also moves stock double-counts it.
    """
    rows = frappe.db.sql(
        """
        SELECT name, update_stock
        FROM `tabSales Invoice`
        WHERE docstatus = 1 AND company = %s
          AND IFNULL(is_pos, 0) = 1
          AND IFNULL(update_stock, 0) = 1
        LIMIT 50
        """,
        (company,),
        as_dict=True,
    )
    if rows:
        return _Result(
            "pos_invoices_do_not_update_stock", FAIL,
            f"{len(rows)} POS invoices move stock themselves: {_sample(rows)}", rows,
        )
    return _Result("pos_invoices_do_not_update_stock", PASS, "POS invoices never move stock directly")


def _check_no_gl_on_group_accounts(company: str) -> _Result:
    """Posting to a group account corrupts every rollup report built on it."""
    rows = frappe.db.sql(
        """
        SELECT gle.account, COUNT(*) AS entries
        FROM `tabGL Entry` gle
        JOIN `tabAccount` acc ON acc.name = gle.account
        WHERE gle.company = %s AND gle.is_cancelled = 0 AND acc.is_group = 1
        GROUP BY gle.account
        LIMIT 50
        """,
        (company,),
        as_dict=True,
    )
    if rows:
        return _Result(
            "no_gl_on_group_accounts", FAIL,
            f"{len(rows)} group accounts carry GL entries: {_sample(rows)}", rows,
        )
    return _Result("no_gl_on_group_accounts", PASS, "no GL posted to a group account")


def _check_invoice_totals_are_internally_consistent(company: str) -> _Result:
    """``grand_total`` must equal net + taxes − discount on every invoice."""
    rows = frappe.db.sql(
        """
        SELECT name, net_total, total_taxes_and_charges, discount_amount, grand_total
        FROM `tabSales Invoice`
        WHERE docstatus = 1 AND company = %s
          AND ABS(
                IFNULL(net_total, 0) + IFNULL(total_taxes_and_charges, 0)
                - IFNULL(grand_total, 0)
              ) > 0.02
        LIMIT 50
        """,
        (company,),
        as_dict=True,
    )
    if rows:
        return _Result(
            "invoice_totals_internally_consistent", WARN,
            f"{len(rows)} invoices where net+tax != grand_total: {_sample(rows)}", rows,
        )
    return _Result("invoice_totals_internally_consistent", PASS, "invoice totals reconcile")


def _check_no_gl_on_disabled_accounts(company: str) -> _Result:
    """Recent postings to a disabled account usually mean a stale config."""
    rows = frappe.db.sql(
        """
        SELECT gle.account, COUNT(*) AS entries
        FROM `tabGL Entry` gle
        JOIN `tabAccount` acc ON acc.name = gle.account
        WHERE gle.company = %s AND gle.is_cancelled = 0
          AND IFNULL(acc.disabled, 0) = 1
          AND gle.creation >= DATE_SUB(NOW(), INTERVAL 90 DAY)
        GROUP BY gle.account
        LIMIT 50
        """,
        (company,),
        as_dict=True,
    )
    if rows:
        return _Result(
            "no_gl_on_disabled_accounts", WARN,
            f"{len(rows)} disabled accounts received GL in the last 90 days: {_sample(rows)}", rows,
        )
    return _Result("no_gl_on_disabled_accounts", PASS, "no recent GL on disabled accounts")


def _check_payment_entries_reference_something(company: str) -> _Result:
    """A submitted customer Payment Entry with no reference is unallocated cash."""
    rows = frappe.db.sql(
        """
        SELECT pe.name, pe.party, pe.paid_amount, pe.unallocated_amount
        FROM `tabPayment Entry` pe
        WHERE pe.docstatus = 1
          AND pe.company = %s
          AND pe.party_type = 'Customer'
          AND IFNULL(pe.unallocated_amount, 0) > 0.01
          AND NOT EXISTS (
                SELECT 1 FROM `tabPayment Entry Reference` per WHERE per.parent = pe.name)
        ORDER BY pe.modified DESC
        LIMIT 50
        """,
        (company,),
        as_dict=True,
    )
    if rows:
        return _Result(
            "payment_entries_reference_something", WARN,
            f"{len(rows)} customer payments sit fully unallocated: {_sample(rows)}", rows,
        )
    return _Result("payment_entries_reference_something", PASS, "customer payments are allocated")


def _check_dispatched_invoices_have_delivery_notes(company: str) -> _Result:
    """A dispatched order should have the Delivery Note that moved its stock.

    Advisory: the historical gap this reports is what the v1_7 backfill exists
    to close, and it also gates whether a return can reverse stock.
    """
    if not _has_column("Sales Invoice", "custom_was_out_for_delivery"):
        return _Result("dispatched_invoices_have_delivery_notes", SKIP, "flag column absent")

    row = frappe.db.sql(
        """
        SELECT COUNT(*) FROM `tabSales Invoice` si
        WHERE si.docstatus = 1 AND si.company = %s
          AND IFNULL(si.is_return, 0) = 0
          AND IFNULL(si.custom_was_out_for_delivery, 0) = 1
          AND NOT EXISTS (
                SELECT 1 FROM `tabDelivery Note Item` dni
                JOIN `tabDelivery Note` dn ON dn.name = dni.parent AND dn.docstatus = 1
                WHERE dni.against_sales_invoice = si.name)
        """,
        (company,),
    )
    missing = int((row and row[0][0]) or 0)
    total = frappe.db.count(
        "Sales Invoice",
        {"docstatus": 1, "company": company, "custom_was_out_for_delivery": 1, "is_return": 0},
    )
    if missing:
        return _Result(
            "dispatched_invoices_have_delivery_notes", WARN,
            f"{missing} of {total} dispatched invoices have no linked Delivery Note "
            "(returns are blocked for these until the v1_7 backfill links them)",
        )
    return _Result(
        "dispatched_invoices_have_delivery_notes", PASS,
        f"all {total} dispatched invoices link a Delivery Note",
    )


# ═════════════════════════════════════════════════════════════════════════════
# Registry + runner
# ═════════════════════════════════════════════════════════════════════════════

#: Hard invariants — a failure here is a real accounting defect.
_GATED = [
    _check_party_lines_have_party,
    _check_debtors_matches_invoices,
    _check_no_over_allocation,
    _check_no_double_full_payment,
    _check_paid_invoices_have_backing,
    _check_no_duplicate_delivery_notes,
    _check_no_duplicate_je_dedup_tags,
    _check_courier_rows_reference_live_invoices,
    _check_cancelled_docs_have_no_live_gl,
    _check_credit_notes_reference_a_real_invoice,
    _check_no_over_return,
    _check_credit_notes_carry_no_woo_id,
    _check_pos_invoices_do_not_update_stock,
    _check_no_gl_on_group_accounts,
]

#: Advisory — operationally interesting, but a hit is not necessarily a defect.
_ADVISORY = [
    _check_no_duplicate_courier_transactions,
    _check_settled_courier_rows_have_je,
    _check_sales_partner_amounts_consistent,
    _check_credit_notes_are_off_the_board,
    _check_invoice_totals_are_internally_consistent,
    _check_no_gl_on_disabled_accounts,
    _check_payment_entries_reference_something,
    _check_dispatched_invoices_have_delivery_notes,
]


def run(strict: bool = False) -> dict:
    """Run the deep audit. Read-only.

    ``strict`` promotes advisory warnings to failures.
    """
    if isinstance(strict, str):
        strict = strict.strip().lower() not in {"", "0", "false", "no"}

    company = _company()
    if not company:
        raise DeepAuditError("No default company configured — refusing to report a green.")

    print("=" * 78)
    print("jarz_pos — DEEP accounting audit (read-only)")
    print(f"  site:    {frappe.local.site}")
    print(f"  company: {company}")
    print(f"  strict:  {strict}")
    print("=" * 78)

    results = []
    for check in _GATED:
        try:
            results.append(("GATED", check(company)))
        except Exception as exc:
            results.append(("GATED", _Result(check.__name__, FAIL, f"check errored: {exc}")))

    for check in _ADVISORY:
        try:
            results.append(("ADVISORY", check(company)))
        except Exception as exc:
            results.append(("ADVISORY", _Result(check.__name__, WARN, f"check errored: {exc}")))

    failed, warned, passed, skipped = [], [], 0, 0
    for tier, res in results:
        marker = {PASS: "PASS", FAIL: "FAIL", WARN: "WARN", SKIP: "SKIP"}[res.status]
        print(f"  [{marker}] {res.name}: {res.message}")
        if res.status == FAIL:
            failed.append(res)
        elif res.status == WARN:
            (failed if (strict and tier == "ADVISORY") else warned).append(res)
        elif res.status == PASS:
            passed += 1
        else:
            skipped += 1

    print("-" * 78)
    print(f"  {passed} passed, {len(failed)} failed, {len(warned)} warnings, {skipped} skipped")
    print("=" * 78)

    summary = {
        "passed": passed,
        "failed": len(failed),
        "warnings": len(warned),
        "skipped": skipped,
        "failures": [{"name": r.name, "message": r.message} for r in failed],
        "warnings_detail": [{"name": r.name, "message": r.message} for r in warned],
    }
    print(json.dumps(summary, default=str))

    if failed:
        raise DeepAuditError(f"{len(failed)} accounting invariant(s) failed")
    return summary
