"""Money-level audit of the post-dispatch return workflow.

``return_flow_validation`` proves the return graph is *structurally* sound: the
documents exist, the source invoice survives, every voucher balances. That is a
weaker statement than it looks — "GL balanced" only says debits equal credits
within a voucher, which is true of any entry ERPNext will accept, including a
completely wrong one.

This harness asks the question the accountant actually cares about: **after the
dust settles, does every account come back to where it should be?** It sums the
GL across *every* voucher a scenario produced — invoice, payment, delivery note,
credit note, return delivery note, journal entries, settlement — and compares the
per-account net against a hand-derived expectation.

The expectations are written out longhand in each case rather than computed, so a
bug in the code under test cannot quietly rewrite the thing it is being measured
against.

Fixtures only, ``_RETMONEY_`` prefixed, cleaned up at the end. Never run against
production — :func:`_guard_environment` is a hard stop.

Usage::

    bench --site frontend execute jarz_pos.scripts.return_money_audit.run
    bench --site frontend execute jarz_pos.scripts.return_money_audit.run \
        --kwargs "{'cleanup': False}"
"""

from __future__ import annotations

import json
from typing import Any, Optional

import frappe
from frappe.utils import flt

from jarz_pos.scripts.b2b_accounting_validation import (
	RunContext,
	_cleanup,
	_create_invoice,
	_ensure_customer,
	_ensure_customer_group,
	_ensure_item_prices,
	_ensure_stock,
	_ensure_territory,
	_pay_invoice_full_cash,
	_pick_courier_party,
	_pick_pos_profile,
	_pick_sellable_items,
)
from jarz_pos.scripts.return_flow_validation import (
	_dispatch_invoice,
	_enable_returns,
	_full_lines,
	_guard_environment,
	_normalize_courier,
	_restore_returns,
	_teardown_dispatched_fixtures,
)
from jarz_pos.services import invoice_return as ir
from jarz_pos.services.delivery_handling import _get_courier_outstanding_account
from jarz_pos.utils.account_utils import (
	get_company_receivable_account,
	get_creditors_account,
	get_freight_expense_account,
	get_pos_cash_account,
)

REPORT_MARKER_START = "RETURN_MONEY_AUDIT_JSON_START"
REPORT_MARKER_END = "RETURN_MONEY_AUDIT_JSON_END"

CUSTOMER_PREFIX = "_RETMONEY_"
_TOL = 0.01
# What the partner charges for one fixture trip, as typed off the partner's app.
PARTNER_FEE = 45.0


# ─────────────────────────────────────────────────────────────────────────────
# Ledger measurement
# ─────────────────────────────────────────────────────────────────────────────


def _write_off_entries(invoice: str) -> list[str]:
	"""Submitted Material Issues that wrote this order's returned goods off as waste.

	Found by the invoice name in ``remarks`` — by discovery, like everything else
	here — and never by reading the name back out of the return's own result.
	"""
	# Anchored on the write-off's own prefix: the automatic consumable deduction is a
	# Material Issue too, and it must stay out of every other case's ledger sum.
	prefix = getattr(ir, "WASTE_REMARKS_PREFIX", "Damaged return write-off")
	return [
		r["name"]
		for r in (
			frappe.db.sql(
				"""SELECT name FROM `tabStock Entry`
	               WHERE docstatus = 1 AND purpose = 'Material Issue' AND remarks LIKE %s""",
				(prefix + "%" + invoice + "%",),
				as_dict=True,
			)
			or []
		)
	]


def _vouchers_for(invoice: str, extra: list[tuple] | None = None) -> list[tuple]:
	"""Every voucher whose GL belongs to this order's lifecycle.

	Collected by *discovery* rather than from the return's return-value, so a
	posting the code makes but does not report still shows up in the audit. A
	silent extra journal entry is precisely the kind of thing this is for.
	"""
	found: list[tuple] = [("Sales Invoice", invoice)]

	credit_notes = (
		frappe.get_all(
			"Sales Invoice",
			filters={"return_against": invoice, "docstatus": 1},
			pluck="name",
			limit_page_length=0,
		)
		or []
	)
	found += [("Sales Invoice", cn) for cn in credit_notes]

	for dn in (
		frappe.get_all(
			"Delivery Note Item",
			filters={"against_sales_invoice": invoice, "docstatus": 1},
			pluck="parent",
			limit_page_length=0,
		)
		or []
	):
		if ("Delivery Note", dn) not in found:
			found.append(("Delivery Note", dn))

	# Journal Entries tag themselves with the invoice or credit-note name in
	# user_remark (title is unusable — v16 overwrites it on validate).
	like_keys = [invoice] + credit_notes
	for key in like_keys:
		for je in (
			frappe.db.sql(
				"SELECT name FROM `tabJournal Entry` WHERE docstatus = 1 AND user_remark LIKE %s",
				("%" + key + "%",),
				as_dict=True,
			)
			or []
		):
			if ("Journal Entry", je["name"]) not in found:
				found.append(("Journal Entry", je["name"]))

	# A damaged return's write-off names the invoice and credit note in remarks.
	for se in _write_off_entries(invoice):
		if ("Stock Entry", se) not in found:
			found.append(("Stock Entry", se))

	# Payment Entries reach the order either through their reference table or
	# through the RET- reference_no the refund stamps.
	for key in like_keys:
		for pe in (
			frappe.db.sql(
				"""SELECT DISTINCT per.parent AS name FROM `tabPayment Entry Reference` per
               JOIN `tabPayment Entry` pe ON pe.name = per.parent
               WHERE pe.docstatus = 1 AND per.reference_name = %s""",
				(key,),
				as_dict=True,
			)
			or []
		):
			if ("Payment Entry", pe["name"]) not in found:
				found.append(("Payment Entry", pe["name"]))
		for pe in (
			frappe.db.sql(
				"SELECT name FROM `tabPayment Entry` WHERE docstatus = 1 AND reference_no LIKE %s",
				("%" + key + "%",),
				as_dict=True,
			)
			or []
		):
			if ("Payment Entry", pe["name"]) not in found:
				found.append(("Payment Entry", pe["name"]))

	return found + list(extra or [])


def _net_by_account(vouchers: list[tuple]) -> dict[str, float]:
	"""Signed net (debit positive) per account across *vouchers*."""
	net: dict[str, float] = {}
	for voucher_type, voucher_no in vouchers:
		for row in (
			frappe.get_all(
				"GL Entry",
				filters={"voucher_type": voucher_type, "voucher_no": voucher_no, "is_cancelled": 0},
				fields=["account", "debit", "credit"],
				limit_page_length=0,
			)
			or []
		):
			key = row["account"]
			net[key] = round(net.get(key, 0.0) + flt(row["debit"]) - flt(row["credit"]), 2)
	return {k: v for k, v in net.items() if abs(v) > _TOL}


class Accounts:
	"""The accounts every expectation is written in terms of."""

	def __init__(self, company: str, pos_profile: str):
		self.company = company
		self.receivable = get_company_receivable_account(company)
		self.cash = get_pos_cash_account(pos_profile, company)
		self.courier_outstanding = _get_courier_outstanding_account(company)
		self.creditors = get_creditors_account(company)
		self.freight = get_freight_expense_account(company)


def _record_expectation(
	ctx: RunContext, case: str, actual: dict[str, float], expected: dict[str, float]
) -> None:
	"""Compare a measured per-account net against the hand-derived expectation."""
	accounts = set(actual) | {a for a, v in expected.items() if abs(v) > _TOL}
	diffs = []
	for account in sorted(accounts):
		want = flt(expected.get(account, 0.0))
		got = flt(actual.get(account, 0.0))
		if abs(want - got) > _TOL:
			diffs.append(f"{account}: expected {want:+.2f}, got {got:+.2f}")
	ctx.record(
		f"{case}.ledger_nets_as_expected",
		not diffs,
		"; ".join(diffs) if diffs else f"all {len(accounts)} accounts as expected",
	)


# ─────────────────────────────────────────────────────────────────────────────
# Environment
# ─────────────────────────────────────────────────────────────────────────────


def _build_env() -> dict[str, Any]:
	pos_profile = _pick_pos_profile()
	company = frappe.db.get_value("POS Profile", pos_profile, "company")
	return {
		"pos_profile": pos_profile,
		"company": company,
		"territory": _ensure_territory(company),
		"customer_group": _ensure_customer_group(),
		"items": _pick_sellable_items(pos_profile, count=2),
		"accounts": Accounts(company, pos_profile),
	}


def _shipping_income(invoice: str) -> float:
	"""What the customer was charged for delivery — the tax row, nothing else.

	Never ``custom_delivery_income`` (NOT NULL DEFAULT 0) and never the Territory
	rate: no row means the customer was charged nothing.
	"""
	total = 0.0
	for row in (
		frappe.get_all(
			"Sales Taxes and Charges",
			filters={"parent": invoice, "parenttype": "Sales Invoice"},
			fields=["tax_amount"],
			limit_page_length=0,
		)
		or []
	):
		total += flt(row["tax_amount"])
	return round(total, 2)


def _courier_shipping_expense(invoice: str) -> float:
	row = (
		frappe.get_all(
			"Courier Transaction",
			filters={"reference_invoice": invoice},
			fields=["shipping_amount"],
			order_by="creation asc",
			limit_page_length=1,
		)
		or []
	)
	return round(flt(row[0]["shipping_amount"]), 2) if row else 0.0


def _dispatch_as_partner(invoice: str, profile: str, courier: dict[str, Any]) -> None:
	"""Dispatch through the delivery-partner path.

	``handle_out_for_delivery_transition`` sends every unpaid order to
	``mark_courier_outstanding``, which is the generic courier flow — it never
	stamps ``custom_is_partner_order`` however partner-linked the courier is. The
	partner strategies hang off ``dispatch_settlement``, which detects the link
	itself, so the partner case has to go in that way or it silently tests the
	ordinary path instead.
	"""
	from jarz_pos.services.settlement_strategies import dispatch_settlement

	dispatch_settlement(
		invoice,
		mode="later",
		pos_profile=profile,
		party_type=courier.get("party_type"),
		party=courier.get("party"),
		# A partner order is refused without the partner's own price for the trip
		# (``_require_partner_fee``); the operator types it off the partner's app.
		partner_fee=PARTNER_FEE,
	)


def _setup_order(
	ctx: RunContext,
	env: dict[str, Any],
	case: str,
	*,
	prepay: bool,
	courier: dict[str, Any] | None,
	pos_profile: str | None = None,
	dispatch_fn: Any | None = None,
) -> str:
	"""Create -> (pay) -> dispatch, on *pos_profile* or the environment default.

	The profile override exists for the partner case: couriers are branch-scoped,
	and dispatching through a profile the courier does not belong to is refused by
	design (``Courier X belongs to POS Profile A, not B``).
	"""
	profile = pos_profile or env["pos_profile"]
	customer = _ensure_customer(ctx, case, env["territory"], env["customer_group"])
	invoice = _create_invoice(
		ctx,
		customer=customer,
		pos_profile=profile,
		items=env["items"],
		order_purpose=None,
		commercial_policy=None,
		policy_reason=None,
		pickup=False,
	)
	if prepay:
		_pay_invoice_full_cash(ctx, invoice, profile)
	(dispatch_fn or _dispatch_invoice)(invoice, profile, courier)
	frappe.db.commit()
	return invoice


# ─────────────────────────────────────────────────────────────────────────────
# Cases
# ─────────────────────────────────────────────────────────────────────────────


def _case_cod_full_return(
	ctx: RunContext, env: dict[str, Any], *, case: str, pay_courier: bool, courier: dict[str, Any] | None
) -> None:
	"""COD order the courier still holds, returned in full.

	Expectation: revenue, receivable and Courier Outstanding all return to zero.
	The only residue is the trip fee — expensed and owed to the courier when we
	pay for the trip, and nothing at all when we do not.
	"""
	acc: Accounts = env["accounts"]
	invoice = _setup_order(ctx, env, case, prepay=False, courier=courier)
	ship_expense = _courier_shipping_expense(invoice)

	result = ir.run_invoice_return(
		invoice_id=invoice,
		lines=_full_lines(invoice),
		reason=f"{case} money audit",
		refund_mode=ir.REFUND_CUSTOMER_CREDIT,
		pay_courier_for_trip=pay_courier,
	)
	ctx.record(f"{case}.succeeded", bool(result.get("success")), json.dumps(result, default=str)[:300])
	if not result.get("success"):
		return

	expected = {
		acc.receivable: 0.0,
		acc.courier_outstanding: 0.0,
		acc.freight: ship_expense if pay_courier else 0.0,
		acc.creditors: -ship_expense if pay_courier else 0.0,
	}
	_record_expectation(ctx, case, _net_by_account(_vouchers_for(invoice)), expected)


def _case_prepaid_full_return(ctx: RunContext, env: dict[str, Any], *, case: str, refund_mode: str) -> None:
	"""Prepaid pickup-style order returned in full.

	``refund_now``  -> the cash goes back out; every account returns to zero.
	``customer_credit`` -> the cash stays in the till and the customer is left
	holding a credit, i.e. a negative receivable of exactly the order total.
	"""
	acc: Accounts = env["accounts"]
	invoice = _setup_order(ctx, env, case, prepay=True, courier=None)
	grand_total = flt(frappe.db.get_value("Sales Invoice", invoice, "grand_total"))
	ship_expense = _courier_shipping_expense(invoice)

	result = ir.run_invoice_return(
		invoice_id=invoice,
		lines=_full_lines(invoice),
		reason=f"{case} money audit",
		refund_mode=refund_mode,
		pay_courier_for_trip=True,
	)
	ctx.record(f"{case}.succeeded", bool(result.get("success")), json.dumps(result, default=str)[:300])
	if not result.get("success"):
		return

	if refund_mode == ir.REFUND_NOW:
		expected = {acc.receivable: 0.0, acc.cash: 0.0}
	else:
		expected = {acc.receivable: -grand_total, acc.cash: grand_total}
	expected[acc.freight] = ship_expense
	expected[acc.creditors] = -ship_expense
	_record_expectation(ctx, case, _net_by_account(_vouchers_for(invoice)), expected)


def _stock_moved(voucher_type: str, voucher_no: str) -> float:
	"""Net quantity a voucher moved, straight off the stock ledger (in = positive)."""
	rows = frappe.get_all(
		"Stock Ledger Entry",
		filters={"voucher_type": voucher_type, "voucher_no": voucher_no, "is_cancelled": 0},
		fields=["actual_qty"],
		limit_page_length=0,
	)
	return round(sum(flt(r["actual_qty"]) for r in rows), 3)


def _case_returned_goods(
	ctx: RunContext, env: dict[str, Any], *, case: str, damaged: bool, courier: dict[str, Any] | None
) -> None:
	"""Where the GOODS end up after a full return — sellable stock, or waste.

	Every return used to put the goods back as sellable; "Damaged" was only a
	label. Measured on the stock ledger across the order's own vouchers:

	* sellable -> dispatch out, return in: the order's net stock movement is zero.
	* damaged  -> dispatch out, return in, write-off out: the net is minus what was
	  sold, i.e. the jars are NOT back in the count.

	The money side must be the ordinary cash-on-delivery result either way, plus —
	damaged only — the loss on the Stock Adjustment Account, taken out of stock.
	"""
	acc: Accounts = env["accounts"]
	invoice = _setup_order(ctx, env, case, prepay=False, courier=courier)
	ship_expense = _courier_shipping_expense(invoice)

	original_dn = ir._original_delivery_note(invoice)
	sold_qty = -_stock_moved("Delivery Note", original_dn) if original_dn else 0.0
	ctx.record(f"{case}.dispatch_moved_stock", sold_qty > 0, f"original DN={original_dn}, qty out={sold_qty}")
	if sold_qty <= 0:
		return

	result = ir.run_invoice_return(
		invoice_id=invoice,
		lines=_full_lines(invoice),
		reason=f"{case} money audit",
		# The dialog has no goods question yet: the return TYPE is how the floor
		# says "these are not sellable", so that is what the case drives.
		return_type="Damaged" if damaged else "Customer Return",
		refund_mode=ir.REFUND_CUSTOMER_CREDIT,
		pay_courier_for_trip=True,
	)
	ctx.record(f"{case}.succeeded", bool(result.get("success")), json.dumps(result, default=str)[:300])
	if not result.get("success"):
		return

	write_offs = _write_off_entries(invoice)
	try:
		ctx.record(
			f"{case}.write_off_entry_count",
			len(write_offs) == (1 if damaged else 0),
			f"write-off Stock Entries for this order={write_offs}",
		)

		net_qty = round(
			sum(
				_stock_moved("Delivery Note", dn)
				for dn in set(
					frappe.get_all(
						"Delivery Note Item",
						filters={"against_sales_invoice": invoice, "docstatus": 1},
						pluck="parent",
						limit_page_length=0,
					)
				)
			)
			+ sum(_stock_moved("Stock Entry", se) for se in write_offs),
			3,
		)
		want_qty = -sold_qty if damaged else 0.0
		ctx.record(
			f"{case}.goods_end_up_where_they_should",
			abs(net_qty - want_qty) <= 0.001,
			f"net stock movement for the order={net_qty}, expected {want_qty} (sold {sold_qty})",
		)

		actual = _net_by_account(_vouchers_for(invoice))
		expected = {
			acc.receivable: 0.0,
			acc.courier_outstanding: 0.0,
			acc.freight: ship_expense,
			acc.creditors: -ship_expense,
		}
		if damaged:
			adjustment = frappe.db.get_value("Company", env["company"], "stock_adjustment_account")
			loss = flt(actual.get(adjustment, 0.0))
			ctx.record(
				f"{case}.loss_is_booked_as_waste",
				loss > _TOL,
				f"{adjustment} net={loss} (must be a debit: the cost of the jars thrown away)",
			)
			# The loss comes out of stock and out of nothing else. Stated per stock
			# ledger account rather than by name so a warehouse with its own account
			# is covered too; cost of goods sold must be back to zero, so it is
			# deliberately absent from the expectation.
			stock_accounts = [
				a for a in actual if frappe.db.get_value("Account", a, "account_type") == "Stock"
			]
			stock_net = round(sum(flt(actual[a]) for a in stock_accounts), 2)
			ctx.record(
				f"{case}.loss_comes_out_of_stock",
				abs(stock_net + loss) <= _TOL,
				f"stock accounts {stock_accounts} net={stock_net}, expected {-loss}",
			)
			expected[adjustment] = loss
			for a in stock_accounts:
				expected[a] = flt(actual[a])
		_record_expectation(ctx, case, actual, expected)
	finally:
		# The shared teardown cancels the Delivery Notes; a write-off left standing
		# would keep the fixture's jars out of staging stock for good.
		for se in write_offs:
			try:
				doc = frappe.get_doc("Stock Entry", se)
				doc.flags.ignore_permissions = True
				doc.cancel()
			except Exception as exc:
				ctx.record(f"{case}.write_off_cleaned_up", False, f"{se}: {type(exc).__name__}: {exc}")


def _dispatch_paid_with_courier(invoice: str, profile: str, courier: dict[str, Any]) -> None:
	"""Dispatch an already-paid order with our own courier, fee settled later.

	``_dispatch_invoice`` sends a paid order down the legacy kanban transition,
	which writes no Courier Transaction and no freight entry at all ("paid_noops")
	— so the prepaid cases above never had a courier to pay or not pay. The floor
	dispatches through ``dispatch_settlement``; that is the path that leaves a
	courier row carrying a FEE and no CASH, which is the whole point of the case.
	"""
	from jarz_pos.services.settlement_strategies import dispatch_settlement

	dispatch_settlement(
		invoice,
		mode="later",
		pos_profile=profile,
		party_type=courier.get("party_type"),
		party=courier.get("party"),
	)


def _open_courier_fee(invoice: str) -> float:
	"""What the courier is still owed for this order on the rows he is settled from."""
	return round(
		sum(
			flt(r["shipping_amount"])
			for r in frappe.get_all(
				"Courier Transaction",
				filters={"reference_invoice": invoice},
				fields=["shipping_amount"],
				limit_page_length=0,
			)
		),
		2,
	)


def _case_online_courier_fee(
	ctx: RunContext, env: dict[str, Any], *, case: str, pay_courier: bool, courier: dict[str, Any] | None
) -> None:
	"""An order with NO cash on the courier, returned in full.

	Paid before dispatch, so the courier row carries a fee and an amount of zero —
	the same shape an InstaPay or Kashier order has. The fee used to be looked up
	on the row that carries cash, found nothing, and the "pay the courier for the
	trip" switch was hidden: the courier was paid for an order that never left the
	shop (production order 21983, 2026-11-17, 80 EGP).

	Expectation: the money the customer paid stays in the till as their credit
	either way. The trip fee stands when we pay for the trip, and is gone from
	BOTH the ledger and the courier's own rows when we do not — the second half is
	what makes the reversal real, because couriers are settled from the rows.
	"""
	acc: Accounts = env["accounts"]
	if not courier:
		ctx.skip(f"{case}.skipped", "no courier on this site; the case needs one")
		return
	invoice = _setup_order(
		ctx, env, case, prepay=True, courier=courier, dispatch_fn=_dispatch_paid_with_courier
	)
	grand_total = flt(frappe.db.get_value("Sales Invoice", invoice, "grand_total"))
	ship_expense = _courier_shipping_expense(invoice)
	cash_rows = frappe.get_all(
		"Courier Transaction", filters={"reference_invoice": invoice, "amount": [">", 0]}, pluck="name"
	)
	ctx.record(
		f"{case}.courier_carries_a_fee_and_no_cash",
		ship_expense > _TOL and not cash_rows,
		f"shipping_amount={ship_expense}, rows with amount>0={cash_rows}",
	)
	if ship_expense <= _TOL or cash_rows:
		return

	preview = ir.build_return_preview(invoice)
	ctx.record(
		f"{case}.fee_switch_is_offered",
		bool((preview.get("toggles") or {}).get("can_choose_courier_fee")),
		f"toggles={preview.get('toggles')} courier={preview.get('courier')}",
	)

	result = ir.run_invoice_return(
		invoice_id=invoice,
		lines=_full_lines(invoice),
		reason=f"{case} money audit",
		refund_mode=ir.REFUND_CUSTOMER_CREDIT,
		pay_courier_for_trip=pay_courier,
	)
	ctx.record(f"{case}.succeeded", bool(result.get("success")), json.dumps(result, default=str)[:300])
	if not result.get("success"):
		return

	expected = {
		acc.receivable: -grand_total,
		acc.cash: grand_total,
		acc.freight: ship_expense if pay_courier else 0.0,
		acc.creditors: -ship_expense if pay_courier else 0.0,
	}
	_record_expectation(ctx, case, _net_by_account(_vouchers_for(invoice)), expected)

	still_owed = _open_courier_fee(invoice)
	want = ship_expense if pay_courier else 0.0
	ctx.record(
		f"{case}.courier_rows_agree_with_the_ledger",
		abs(still_owed - want) <= _TOL,
		f"fee still on the courier's rows={still_owed}, expected {want}",
	)


def _case_online_fee_already_paid(ctx: RunContext, env: dict[str, Any], courier: dict[str, Any] | None) -> None:
	"""Online-paid order whose courier was ALREADY PAID his fee, then returned "no fee".

	A return cannot take paid cash back from a courier: no settlement screen can
	collect from one. A lone negative fee row would be closed by the per-invoice
	button with no posting, leaving the reversal entry as a permanent debit on
	Creditors while the courier keeps the money.

	Expectation: the fee question is not offered; asked anyway, the return goes
	through, posts NO fee reversal, writes NO fee row, and says so — in its
	result and on the credit note, which must not claim the courier went unpaid.
	"""
	case = "MONEY-ONLINE-FEE-ALREADY-PAID"
	acc: Accounts = env["accounts"]
	if not courier:
		ctx.skip(f"{case}.skipped", "no courier on this site; the case needs one")
		return
	invoice = _setup_order(
		ctx, env, case, prepay=True, courier=courier, dispatch_fn=_dispatch_paid_with_courier
	)
	grand_total = flt(frappe.db.get_value("Sales Invoice", invoice, "grand_total"))
	ship_expense = _courier_shipping_expense(invoice)
	if ship_expense <= _TOL:
		ctx.record(f"{case}.courier_carries_a_fee", False, f"shipping_amount={ship_expense}")
		return

	from jarz_pos.services.delivery_handling import settle_single_invoice_paid

	extra: list[tuple] = []
	try:
		settled = (
			settle_single_invoice_paid(
				invoice, env["pos_profile"], courier.get("party_type"), courier.get("party")
			)
			or {}
		)
	except frappe.ValidationError as exc:
		ctx.skip(f"{case}.skipped", f"could not pay the fee first: {type(exc).__name__}: {exc}")
		return
	name = settled.get("journal_entry") if isinstance(settled, dict) else None
	if name and frappe.db.exists("Journal Entry", name):
		extra.append(("Journal Entry", name))
	open_rows = frappe.get_all(
		"Courier Transaction",
		filters={"reference_invoice": invoice, "status": ["!=", "Settled"]},
		pluck="name",
	)
	ctx.record(f"{case}.fee_was_paid_first", bool(name) and not open_rows, f"je={name} open={open_rows}")
	if not name or open_rows:
		return

	preview = ir.build_return_preview(invoice)
	toggles = preview.get("toggles") or {}
	ctx.record(
		f"{case}.fee_switch_is_not_offered",
		not toggles.get("can_choose_courier_fee")
		and abs(flt(toggles.get("courier_fee_already_paid")) - ship_expense) <= _TOL,
		f"toggles={toggles}",
	)

	result = ir.run_invoice_return(
		invoice_id=invoice,
		lines=_full_lines(invoice),
		reason=f"{case} money audit",
		refund_mode=ir.REFUND_CUSTOMER_CREDIT,
		pay_courier_for_trip=False,
	)
	ctx.record(f"{case}.succeeded", bool(result.get("success")), json.dumps(result, default=str)[:300])
	if not result.get("success"):
		return
	ctx.record(
		f"{case}.says_the_fee_was_not_removed",
		bool(result.get("courier_fee_not_reversible"))
		and abs(flt(result.get("courier_fee_reversed"))) <= _TOL
		and not result.get("compensating_courier_transaction"),
		f"not_reversible={result.get('courier_fee_not_reversible')} "
		f"reversed={result.get('courier_fee_reversed')} row={result.get('compensating_courier_transaction')}",
	)
	if frappe.get_meta("Sales Invoice").get_field("custom_return_courier_paid"):
		stamped = frappe.db.get_value("Sales Invoice", result.get("credit_note"), "custom_return_courier_paid")
		ctx.record(f"{case}.credit_note_says_courier_was_paid", int(stamped or 0) == 1, f"stamp={stamped}")

	# The customer's money stays as credit; the courier's fee was paid out of the
	# till and stays a freight cost. Creditors is square: accrued, then paid.
	expected = {
		acc.receivable: -grand_total,
		acc.cash: grand_total - ship_expense,
		acc.freight: ship_expense,
		acc.creditors: 0.0,
	}
	vouchers = _vouchers_for(invoice, extra)
	_record_expectation(ctx, case, _net_by_account(list(dict.fromkeys(vouchers))), expected)

	open_after = frappe.get_all(
		"Courier Transaction",
		filters={"reference_invoice": invoice, "status": ["!=", "Settled"]},
		pluck="name",
	)
	ctx.record(
		f"{case}.no_dangling_courier_row",
		not open_after and abs(_open_courier_fee(invoice) - ship_expense) <= _TOL,
		f"open rows={open_after}, fee on rows={_open_courier_fee(invoice)}, expected {ship_expense}",
	)


def _case_partial_shipping(ctx: RunContext, env: dict[str, Any], courier: dict[str, Any] | None) -> None:
	"""Return ONE line of a two-line order.

	The customer keeps the rest and the delivery happened, so the delivery charge
	is not refundable: the credit note must equal the returned goods and nothing
	more.
	"""
	case = "MONEY-PARTIAL-SHIPPING"
	invoice = _setup_order(ctx, env, case, prepay=False, courier=courier)
	inv = frappe.get_doc("Sales Invoice", invoice)
	if len(inv.items) < 2:
		# WHY: recorded passed=True. A single-line fixture means the partial
		# return — the whole point of this case — never happened.
		ctx.skip(f"{case}.skipped", "fixture invoice has a single line; no partial return was run")
		return

	first = inv.items[0]
	goods_only = round(flt(first.amount), 2)
	shipping = _shipping_income(invoice)
	ctx.record(f"{case}.fixture_has_shipping_income", shipping > _TOL, f"shipping income={shipping}")

	result = ir.run_invoice_return(
		invoice_id=invoice,
		lines=[{"si_detail": first.name, "qty": flt(first.qty)}],
		reason=f"{case} money audit",
		refund_mode=ir.REFUND_CUSTOMER_CREDIT,
		pay_courier_for_trip=True,
	)
	ctx.record(f"{case}.succeeded", bool(result.get("success")), json.dumps(result, default=str)[:300])
	if not result.get("success"):
		return

	credited = round(abs(flt(result.get("return_total"))), 2)
	ctx.record(
		f"{case}.credits_goods_only",
		abs(credited - goods_only) <= _TOL,
		f"returned goods={goods_only}, credit note={credited}, "
		f"shipping income on the order={shipping} (a partial return must not refund delivery)",
	)


def _case_partial_twice_never_exceeds_invoice(
	ctx: RunContext, env: dict[str, Any], courier: dict[str, Any] | None
) -> None:
	"""Return each line separately; the two credit notes must sum to the invoice.

	Anything more means the order was credited for money the customer never paid.
	"""
	case = "MONEY-PARTIAL-TWICE"
	invoice = _setup_order(ctx, env, case, prepay=False, courier=courier)
	inv = frappe.get_doc("Sales Invoice", invoice)
	if len(inv.items) < 2:
		# WHY: recorded passed=True — the two-leg over-credit check never ran.
		ctx.skip(f"{case}.skipped", "fixture invoice has a single line; no two-leg return was run")
		return
	grand_total = round(flt(inv.grand_total), 2)

	credited = 0.0
	for index, row in enumerate(inv.items):
		result = ir.run_invoice_return(
			invoice_id=invoice,
			lines=[{"si_detail": row.name, "qty": flt(row.qty)}],
			reason=f"{case} leg {index} money audit",
			refund_mode=ir.REFUND_CUSTOMER_CREDIT,
			pay_courier_for_trip=True,
		)
		ctx.record(
			f"{case}.leg{index}_succeeded", bool(result.get("success")), json.dumps(result, default=str)[:300]
		)
		if not result.get("success"):
			return
		credited += abs(flt(result.get("return_total")))

	credited = round(credited, 2)
	ctx.record(
		f"{case}.total_credited_equals_invoice",
		abs(credited - grand_total) <= _TOL,
		f"invoice grand total={grand_total}, total credited across both partial "
		f"returns={credited}, over-credit={round(credited - grand_total, 2)}",
	)


def _case_settle_after_return(ctx: RunContext, env: dict[str, Any], courier: dict[str, Any] | None) -> None:
	"""Fully return a COD order, then run the one-by-one courier settlement.

	The return already released the courier's liability. Settling afterwards must
	not release it a second time — Courier Outstanding has to end at zero, and the
	branch must not bank cash for goods that came back.
	"""
	case = "MONEY-SETTLE-AFTER-RETURN"
	if not courier:
		# WHY: recorded passed=True. The double-release defect this case hunts —
		# settlement releasing Courier Outstanding a second time after a return —
		# was never exercised, and the audit still reported green.
		ctx.skip(
			f"{case}.skipped",
			"no courier bound to this branch — the settle-after-return path did NOT run",
		)
		return

	acc: Accounts = env["accounts"]
	invoice = _setup_order(ctx, env, case, prepay=False, courier=courier)
	ship_expense = _courier_shipping_expense(invoice)

	result = ir.run_invoice_return(
		invoice_id=invoice,
		lines=_full_lines(invoice),
		reason=f"{case} money audit",
		refund_mode=ir.REFUND_CUSTOMER_CREDIT,
		pay_courier_for_trip=True,
	)
	ctx.record(f"{case}.return_succeeded", bool(result.get("success")), json.dumps(result, default=str)[:300])
	if not result.get("success"):
		return

	from jarz_pos.services.delivery_handling import settle_single_invoice_paid

	settled: dict[str, Any] = {}
	settlement_ran = False
	try:
		settled = (
			settle_single_invoice_paid(
				invoice, env["pos_profile"], courier.get("party_type"), courier.get("party")
			)
			or {}
		)
		settlement_ran = True
		ctx.record(f"{case}.settlement_ran", True, json.dumps(settled, default=str)[:300])
	except Exception as exc:
		# Refusing to settle a returned order is a perfectly good outcome — better
		# than posting a wrong entry.
		# WHY this is now conditional: both branches used to record True, so a
		# crash halfway through the settlement (partially posted, then blew up)
		# read exactly like a deliberate refusal. Only an explicit guard —
		# frappe.throw, i.e. a ValidationError — counts as a refusal; any other
		# exception type is a defect in the code under test.
		expected_refusal = isinstance(exc, frappe.ValidationError)
		ctx.record(
			f"{case}.settlement_refused",
			expected_refusal,
			f"settlement raised {type(exc).__name__}: {exc} — "
			f"{'explicit guard refusal (acceptable)' if expected_refusal else 'NOT a guard refusal; this is a crash mid-settlement'}",
		)

	# The courier was paid for the trip, so once the settlement runs the fee has
	# left the till: freight is expensed and cash is down by the same amount. What
	# must NOT happen is the settlement releasing Courier Outstanding a second
	# time, or the branch banking the order value on returned goods — both are
	# pinned at zero here.
	#
	# When the settlement was REFUSED the fee never left the till: it is still
	# owed to the courier, so it sits in Creditors instead of coming out of cash.
	# The old code asserted the settled shape either way, which meant the refusal
	# branch was graded against a ledger it could not produce.
	if settlement_ran:
		expected = {
			acc.receivable: 0.0,
			acc.courier_outstanding: 0.0,
			acc.freight: ship_expense,
			acc.cash: -ship_expense,
		}
	else:
		expected = {
			acc.receivable: 0.0,
			acc.courier_outstanding: 0.0,
			acc.freight: ship_expense,
			acc.creditors: -ship_expense,
			acc.cash: 0.0,
		}
	_record_expectation(ctx, case, _net_by_account(_vouchers_for(invoice)), expected)


def _pay_deposit_cash(ctx: RunContext, invoice: str, pos_profile: str, amount: float) -> str:
	"""Take part of the order in cash at the branch before dispatch."""
	inv = frappe.get_doc("Sales Invoice", invoice)
	receivable = inv.debit_to or get_company_receivable_account(inv.company)
	pe = frappe.new_doc("Payment Entry")
	pe.payment_type = "Receive"
	pe.company = inv.company
	pe.posting_date = frappe.utils.nowdate()
	pe.mode_of_payment = "Cash"
	pe.party_type = "Customer"
	pe.party = inv.customer
	pe.paid_from = receivable
	pe.paid_to = get_pos_cash_account(pos_profile, inv.company)
	pe.paid_amount = amount
	pe.received_amount = amount
	pe.append(
		"references",
		{"reference_doctype": "Sales Invoice", "reference_name": invoice, "allocated_amount": amount},
	)
	pe.flags.ignore_permissions = True
	pe.insert(ignore_permissions=True)
	pe.submit()
	ctx.payment_entries.append(pe.name)
	return pe.name


def _case_cod_deposit_return(
	ctx: RunContext, env: dict[str, Any], *, case: str, refund_mode: str, courier: dict[str, Any] | None
) -> None:
	"""Cash-on-delivery order with a deposit taken at the branch, returned in full.

	Dispatch puts only the remainder (grand total - deposit) on the courier. The
	return used to release the WHOLE credit note from Courier Outstanding and add
	a negative courier row for the whole total, so the courier's rows netted to
	minus the deposit, the settlement then paid that deposit out of the till, and
	the customer was left with no credit at all.

	``customer_credit`` -> the deposit stays in the till and the customer holds a
	credit of exactly the deposit; settling the courier afterwards moves only the
	trip fee.  ``refund_now`` -> the deposit goes back out of the till; every
	account but the trip fee returns to zero.
	"""
	if not courier:
		ctx.skip(f"{case}.skipped", "no courier bound to this branch — the deposit return did NOT run")
		return

	acc: Accounts = env["accounts"]
	profile = env["pos_profile"]
	customer = _ensure_customer(ctx, case, env["territory"], env["customer_group"])
	invoice = _create_invoice(
		ctx,
		customer=customer,
		pos_profile=profile,
		items=env["items"],
		order_purpose=None,
		commercial_policy=None,
		policy_reason=None,
		pickup=False,
	)
	grand_total = flt(frappe.db.get_value("Sales Invoice", invoice, "grand_total"))
	deposit = round(min(100.0, grand_total / 2), 2)
	_pay_deposit_cash(ctx, invoice, profile, deposit)
	_dispatch_invoice(invoice, profile, courier)
	frappe.db.commit()
	ship_expense = _courier_shipping_expense(invoice)

	carried = round(
		sum(
			flt(r["amount"])
			for r in frappe.get_all(
				"Courier Transaction", filters={"reference_invoice": invoice}, fields=["amount"]
			)
		),
		2,
	)
	ctx.record(
		f"{case}.courier_carries_remainder_only",
		abs(carried - (grand_total - deposit)) <= _TOL,
		f"grand total={grand_total}, deposit={deposit}, courier carries={carried}",
	)

	result = ir.run_invoice_return(
		invoice_id=invoice,
		lines=_full_lines(invoice),
		reason=f"{case} money audit",
		refund_mode=refund_mode,
		pay_courier_for_trip=True,
	)
	ctx.record(f"{case}.succeeded", bool(result.get("success")), json.dumps(result, default=str)[:300])
	if not result.get("success"):
		return

	net_ct = round(
		sum(
			flt(r["amount"])
			for r in frappe.get_all(
				"Courier Transaction", filters={"reference_invoice": invoice}, fields=["amount"]
			)
		),
		2,
	)
	ctx.record(
		f"{case}.courier_rows_net_to_zero",
		abs(net_ct) <= _TOL,
		f"courier transaction net after return={net_ct} (negative = the courier would be "
		f"paid the deposit out of the till at settlement)",
	)

	if refund_mode == ir.REFUND_NOW:
		ctx.record(
			f"{case}.refund_is_the_deposit",
			abs(flt(result.get("refund_amount")) - deposit) <= _TOL,
			f"refund_amount={result.get('refund_amount')}, deposit={deposit}",
		)
		expected = {
			acc.receivable: 0.0,
			acc.cash: 0.0,
			acc.courier_outstanding: 0.0,
			acc.freight: ship_expense,
			acc.creditors: -ship_expense,
		}
		_record_expectation(ctx, case, _net_by_account(_vouchers_for(invoice)), expected)
		return

	from jarz_pos.services.delivery_handling import settle_single_invoice_paid

	extra: list[tuple] = []
	try:
		settled = (
			settle_single_invoice_paid(invoice, profile, courier.get("party_type"), courier.get("party"))
			or {}
		)
		ctx.record(f"{case}.settlement_ran", True, json.dumps(settled, default=str)[:300])
		for key in ("journal_entry", "settlement_journal_entry", "payment_entry"):
			name = settled.get(key) if isinstance(settled, dict) else None
			if name and frappe.db.exists("Journal Entry", name):
				extra.append(("Journal Entry", name))
		settlement_ran = True
	except frappe.ValidationError as exc:
		ctx.record(f"{case}.settlement_refused", True, f"{type(exc).__name__}: {exc}")
		settlement_ran = False

	# The deposit stays in the till as the customer's credit. Settling only pays
	# the courier his trip fee out of the till; it must not touch the deposit.
	expected = {
		acc.receivable: -deposit,
		acc.courier_outstanding: 0.0,
		acc.freight: ship_expense,
	}
	if settlement_ran:
		expected[acc.cash] = deposit - ship_expense
	else:
		expected[acc.cash] = deposit
		expected[acc.creditors] = -ship_expense
	vouchers = _vouchers_for(invoice, extra)
	_record_expectation(ctx, case, _net_by_account(list(dict.fromkeys(vouchers))), expected)


def _case_partner_return(
	ctx: RunContext, env: dict[str, Any], *, case: str = "MONEY-PARTNER-RETURN", pay_courier: bool = True
) -> None:
	"""Return a delivery-partner order.

	A partner rider carries the branch's money like our own courier: dispatch
	moves the receivable to Courier Outstanding and accrues the partner's fee to
	the partner's settlement account. The return must release Courier Outstanding
	in full and leave the fee owed — the partner is paid for a trip whose goods
	came back. (This case used to assert the older model, where partner dispatch
	bypassed Courier Outstanding; it also failed at dispatch once the partner's own
	delivery cost became mandatory.)

	``pay_courier=False`` is the order that was pulled back before the rider took
	it: the trip never happened, so the fee must leave the partner's payable AND
	the weekly bill. There was no way to do that at all before 2026-11-25.
	"""
	partner = frappe.db.get_value("Delivery Partner", {}, ["name", "settlement_account"], as_dict=True)
	if not partner or not partner.get("settlement_account"):
		# WHY: recorded passed=True. Without a Delivery Partner the partner
		# return path — the one that must never touch Courier Outstanding —
		# was never run, yet the audit claimed it green.
		ctx.skip(f"{case}.skipped", "no Delivery Partner with a settlement account on this site")
		return

	party = frappe.db.get_value(
		"Supplier", {"custom_delivery_partner": partner["name"]}, "name"
	) or frappe.db.get_value("Employee", {"custom_delivery_partner": partner["name"]}, "name")
	if not party:
		# WHY: recorded passed=True.
		ctx.skip(f"{case}.skipped", f"no courier bound to partner {partner['name']}")
		return
	party_type = "Supplier" if frappe.db.exists("Supplier", party) else "Employee"

	# Couriers are branch-scoped, so the order has to be raised on the profile the
	# partner's courier actually belongs to, not whichever profile came first.
	from jarz_pos.utils.courier_visibility import resolve_courier_branch

	profile = resolve_courier_branch(party_type, party) or env["pos_profile"]
	if not frappe.db.exists("POS Profile", profile):
		# WHY: recorded passed=True.
		ctx.skip(f"{case}.skipped", f"courier branch {profile!r} is not a POS Profile")
		return

	company = frappe.db.get_value("POS Profile", profile, "company")
	acc = Accounts(company, profile)
	invoice = _setup_order(
		ctx,
		env,
		case,
		prepay=False,
		courier={"party_type": party_type, "party": party},
		pos_profile=profile,
		dispatch_fn=_dispatch_as_partner,
	)
	is_partner = int(frappe.db.get_value("Sales Invoice", invoice, "custom_is_partner_order") or 0)
	ctx.record(
		f"{case}.dispatched_as_partner_order", is_partner == 1, f"custom_is_partner_order={is_partner}"
	)
	if not is_partner:
		return

	if not pay_courier:
		preview = ir.build_return_preview(invoice)
		ctx.record(
			f"{case}.fee_switch_is_offered",
			bool((preview.get("toggles") or {}).get("can_choose_courier_fee")),
			f"toggles={preview.get('toggles')} courier={preview.get('courier')}",
		)

	result = ir.run_invoice_return(
		invoice_id=invoice,
		lines=_full_lines(invoice),
		reason=f"{case} money audit",
		refund_mode=ir.REFUND_CUSTOMER_CREDIT,
		pay_courier_for_trip=pay_courier,
	)
	ctx.record(f"{case}.succeeded", bool(result.get("success")), json.dumps(result, default=str)[:300])
	if not result.get("success"):
		return

	# The partner's fee lives on ``partner_fee`` (and ``custom_shipping_expense``);
	# a partner Courier Transaction carries ``shipping_amount`` 0 on purpose, so
	# ``_courier_shipping_expense`` would read 0 here.
	partner_rows = frappe.get_all(
		"Courier Transaction",
		filters={"reference_invoice": invoice, "is_partner_order": 1},
		fields=["name", "partner_fee", "partner_settled"],
		limit_page_length=0,
	)
	# What dispatch entered, read off the dispatch row alone.
	partner_fee = round(sum(flt(r["partner_fee"]) for r in partner_rows if flt(r["partner_fee"]) > 0), 2)
	ctx.record(
		f"{case}.partner_fee_recorded",
		abs(partner_fee - PARTNER_FEE) <= _TOL,
		f"partner_fee on the dispatch row={partner_fee}, entered at dispatch={PARTNER_FEE}",
	)
	# What the weekly bill would charge for this order: the unbilled rows, summed —
	# exactly the query ``settle_delivery_partner`` runs.
	billable = round(
		sum(flt(r["partner_fee"]) for r in partner_rows if not int(r.get("partner_settled") or 0)), 2
	)
	fee_owed = partner_fee if pay_courier else 0.0
	ctx.record(
		f"{case}.weekly_bill_agrees_with_the_ledger",
		abs(billable - fee_owed) <= _TOL,
		f"unbilled partner_fee for this order={billable}, expected {fee_owed}; rows={partner_rows}",
	)
	actual = _net_by_account(_vouchers_for(invoice))
	# Dispatch moves the receivable to Courier Outstanding against the partner's
	# rider, exactly like our own courier, and accrues the partner's fee
	# (DR Freight / CR partner ledger). A full return releases Courier Outstanding
	# in full. The fee is NOT reversed when the trip happened — the partner charges
	# for a trip whose goods came back — so the partner ends up owed exactly the
	# fee (a payable, hence negative) and freight stands. When the trip never
	# happened both go back to zero. Every other account nets to zero either way.
	expected = {
		acc.receivable: 0.0,
		acc.courier_outstanding: 0.0,
		partner["settlement_account"]: -fee_owed,
		acc.freight: fee_owed,
	}
	_record_expectation(ctx, case, actual, expected)
	ctx.record(
		f"{case}.courier_outstanding_nets_to_zero",
		abs(flt(actual.get(acc.courier_outstanding, 0.0))) <= _TOL,
		f"Courier Outstanding net={actual.get(acc.courier_outstanding, 0.0)} "
		f"(dispatch puts the order there; the return must release all of it)",
	)

	reversal = (
		frappe.get_all(
			"Courier Transaction",
			filters={"reference_invoice": invoice, "idempotency_token": ["like", "RET::%"]},
			fields=["name", "is_partner_order"],
			limit_page_length=5,
		)
		or []
	)
	ctx.record(
		f"{case}.reversal_row_flagged_as_partner",
		bool(reversal) and all(int(r.get("is_partner_order") or 0) == 1 for r in reversal),
		f"compensating rows={reversal} (must carry is_partner_order so the partner "
		f"settlement sweep sees the reversal and the generic courier sweep does not)",
	)


# ─────────────────────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────────────────────


def run(cleanup: bool = True) -> dict[str, Any]:
	_guard_environment()
	previous_flag = _enable_returns()
	ctx = RunContext()

	try:
		env = _build_env()
		_ensure_item_prices(ctx, env["items"])
		_ensure_stock(ctx, env["items"], env["pos_profile"])
		courier = _normalize_courier(_pick_courier_party(env["pos_profile"]))
		print(
			f"\nEnvironment: profile={env['pos_profile']} company={env['company']} "
			f"items={env['items']} courier={courier}"
		)

		# Each case is isolated: one that blows up records a failure and the rest
		# still run. A single unhandled error used to abort the whole run before
		# the report was printed, which threw away every result already gathered.
		cases = [
			(
				"MONEY-COD-PAY-COURIER",
				lambda: _case_cod_full_return(
					ctx, env, case="MONEY-COD-PAY-COURIER", pay_courier=True, courier=courier
				),
			),
			(
				"MONEY-COD-NO-COURIER-FEE",
				lambda: _case_cod_full_return(
					ctx, env, case="MONEY-COD-NO-COURIER-FEE", pay_courier=False, courier=courier
				),
			),
			(
				"MONEY-PREPAID-REFUND",
				lambda: _case_prepaid_full_return(
					ctx, env, case="MONEY-PREPAID-REFUND", refund_mode=ir.REFUND_NOW
				),
			),
			(
				"MONEY-PREPAID-CREDIT",
				lambda: _case_prepaid_full_return(
					ctx, env, case="MONEY-PREPAID-CREDIT", refund_mode=ir.REFUND_CUSTOMER_CREDIT
				),
			),
			("MONEY-PARTIAL-SHIPPING", lambda: _case_partial_shipping(ctx, env, courier)),
			("MONEY-PARTIAL-TWICE", lambda: _case_partial_twice_never_exceeds_invoice(ctx, env, courier)),
			("MONEY-SETTLE-AFTER-RETURN", lambda: _case_settle_after_return(ctx, env, courier)),
			(
				"MONEY-COD-DEPOSIT-CREDIT",
				lambda: _case_cod_deposit_return(
					ctx,
					env,
					case="MONEY-COD-DEPOSIT-CREDIT",
					refund_mode=ir.REFUND_CUSTOMER_CREDIT,
					courier=courier,
				),
			),
			(
				"MONEY-COD-DEPOSIT-REFUND",
				lambda: _case_cod_deposit_return(
					ctx, env, case="MONEY-COD-DEPOSIT-REFUND", refund_mode=ir.REFUND_NOW, courier=courier
				),
			),
			("MONEY-PARTNER-RETURN", lambda: _case_partner_return(ctx, env)),
			(
				"MONEY-PARTNER-NO-FEE",
				lambda: _case_partner_return(ctx, env, case="MONEY-PARTNER-NO-FEE", pay_courier=False),
			),
			(
				"MONEY-GOODS-RESTOCK",
				lambda: _case_returned_goods(
					ctx, env, case="MONEY-GOODS-RESTOCK", damaged=False, courier=courier
				),
			),
			(
				"MONEY-GOODS-WASTE",
				lambda: _case_returned_goods(ctx, env, case="MONEY-GOODS-WASTE", damaged=True, courier=courier),
			),
			(
				"MONEY-ONLINE-PAY-COURIER",
				lambda: _case_online_courier_fee(
					ctx, env, case="MONEY-ONLINE-PAY-COURIER", pay_courier=True, courier=courier
				),
			),
			(
				"MONEY-ONLINE-NO-COURIER-FEE",
				lambda: _case_online_courier_fee(
					ctx, env, case="MONEY-ONLINE-NO-COURIER-FEE", pay_courier=False, courier=courier
				),
			),
			("MONEY-ONLINE-FEE-ALREADY-PAID", lambda: _case_online_fee_already_paid(ctx, env, courier)),
		]
		for name, run_case in cases:
			try:
				run_case()
			except Exception as exc:
				ctx.record(f"{name}.case_raised", False, f"{type(exc).__name__}: {exc}")
				frappe.log_error(frappe.get_traceback(), f"return_money_audit case {name}")

	finally:
		if cleanup:
			try:
				_teardown_dispatched_fixtures(ctx)
				_cleanup(ctx)
			except Exception as exc:
				frappe.log_error(frappe.get_traceback(), "return_money_audit cleanup failed")
				# WHY: swallowed. A failed teardown leaves dispatched fixtures,
				# credit notes and JEs live in the ledger while the audit prints
				# its counts as if the run were clean. The report is built below,
				# so this failure lands in it.
				ctx.record("cleanup.teardown_completed", False, f"{type(exc).__name__}: {exc}")
		_restore_returns(previous_flag)
		frappe.db.commit()

	# Skipped checks are not failures — but they are not passes either, so they
	# get their own line rather than disappearing into the pass count.
	failures = [c for c in ctx.checks if not c.passed and not c.skipped]
	print("\n" + "=" * 78)
	print(f"RETURN MONEY AUDIT — {ctx.passed} passed, {ctx.failed} failed, {ctx.skipped} skipped")
	print("=" * 78)
	for check in failures:
		print(f"  FAIL  {check.name}\n        {check.detail}")
	for check in ctx.skipped_checks:
		print(f"  SKIP  {check.name}\n        {check.detail}")

	report = {
		"passed": ctx.passed,
		"failed": ctx.failed,
		"skipped": ctx.skipped,
		"skipped_checks": ctx.summary_skipped(),
		"checks": ctx.summary_checks(),
	}
	print(REPORT_MARKER_START)
	print(json.dumps(report, default=str))
	print(REPORT_MARKER_END)
	return report
