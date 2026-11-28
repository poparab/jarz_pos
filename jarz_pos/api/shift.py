from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from html import unescape
from types import SimpleNamespace
from typing import Any

import frappe
from erpnext.accounts.doctype.pos_closing_entry.pos_closing_entry import (
	make_closing_entry_from_opening,
)
from erpnext.accounts.utils import get_balance_on
from frappe import _
from frappe.utils import cint, flt, get_datetime, getdate, now_datetime, nowdate

from jarz_pos.constants import ACCOUNTS, QUERY_LIMITS, ROLES, WS_EVENTS


def _assert_user_has_profile_access(user: str, pos_profile: str):
	from jarz_pos.utils.validation_utils import assert_pos_profile_enabled

	assert_pos_profile_enabled(pos_profile)

	has_access = frappe.db.exists(
		"POS Profile User",
		{
			"parent": pos_profile,
			"user": user,
		},
	)
	if not has_access:
		frappe.throw(
			_("You are not allowed to use POS Profile {0}").format(pos_profile), frappe.PermissionError
		)


def _get_mode_of_payment_account(mode_of_payment: str, company: str) -> str | None:
	if not mode_of_payment or not company:
		return None

	account = frappe.db.get_value(
		"Mode of Payment Account",
		{
			"parent": mode_of_payment,
			"company": company,
		},
		"default_account",
	)
	if account:
		return account

	account = frappe.db.get_value(
		"Mode of Payment Account",
		{
			"parent": mode_of_payment,
		},
		"default_account",
	)
	return account


def _get_account_balance(account: str | None, company: str) -> float:
	if not account:
		return 0.0
	try:
		return flt(get_balance_on(account=account, company=company, date=nowdate()) or 0)
	except Exception:
		return 0.0


def _ensure_mode_of_payment_account(mode_of_payment: str, company: str, default_account: str | None):
	"""Make sure *mode_of_payment* has SOME account for *company* (ERPNext's
	POS Opening validation 417s without one). Never overwrite an existing row.

	The Mode of Payment Account is one company-wide value, but every branch has
	its own till. This used to rewrite the row to the opening branch's till on
	every shift open, so the company-wide "Cash" account became whichever branch
	opened last — and anything reading it (the purchase screen's "Cash" option)
	paid out of that branch's drawer. Shift code always passes the till account
	explicitly and never needs this row to name it.
	"""
	if not mode_of_payment or not company or not default_account:
		return

	if frappe.db.exists(
		"Mode of Payment Account",
		{
			"parent": mode_of_payment,
			"company": company,
		},
	):
		return

	row = frappe.get_doc(
		{
			"doctype": "Mode of Payment Account",
			"parent": mode_of_payment,
			"parenttype": "Mode of Payment",
			"parentfield": "accounts",
			"company": company,
			"default_account": default_account,
		}
	)
	row.insert(ignore_permissions=True)


def _get_profile_primary_mode_of_payment(profile) -> str | None:
	payments = profile.get("payments") or []
	if not payments:
		return None

	for row in payments:
		if row.get("default") and row.get("mode_of_payment"):
			return row.get("mode_of_payment")

	return (payments[0] or {}).get("mode_of_payment")


def _normalize_shift_close_error_message(raw: Any) -> str:
	if raw is None:
		return _("Unknown error while closing the shift.")

	if isinstance(raw, dict):
		raw = raw.get("message") or raw.get("exc") or raw.get("exception") or json.dumps(raw)
	elif isinstance(raw, list):
		parts = [_normalize_shift_close_error_message(item) for item in raw]
		parts = [part for part in parts if part]
		return "; ".join(parts) if parts else _("Unknown error while closing the shift.")

	text = str(raw).strip()
	if not text:
		return _("Unknown error while closing the shift.")

	try:
		parsed = json.loads(text)
	except Exception:
		parsed = None

	if parsed is not None and parsed != raw:
		return _normalize_shift_close_error_message(parsed)

	text = unescape(text)
	text = re.sub(r"<[^>]+>", "", text)
	text = re.sub(r"\s+", " ", text).strip()
	return text or _("Unknown error while closing the shift.")


def _get_primary_traceback_error_message(traceback_text: str | None) -> str | None:
	if not traceback_text:
		return None

	for raw_line in traceback_text.splitlines():
		line = raw_line.strip()
		if not line or ": " not in line:
			continue
		if line.startswith("Traceback") or line.startswith("File ") or line.startswith("During handling"):
			continue
		if not re.match(r"^[A-Za-z0-9_.]+(?:Error|Exception): ", line):
			continue

		message = line.split(": ", 1)[1].strip()
		if message:
			return _normalize_shift_close_error_message(message)

	return None


def _get_shift_close_error_message(exc: Exception, closing=None, traceback_text: str | None = None) -> str:
	error_value: Any = None

	primary_traceback_message = _get_primary_traceback_error_message(traceback_text)
	if primary_traceback_message:
		return primary_traceback_message

	if closing is not None:
		error_value = getattr(closing, "error_message", None)
		if (
			not error_value
			and getattr(closing, "name", None)
			and frappe.db.exists("POS Closing Entry", closing.name)
		):
			error_value = frappe.db.get_value("POS Closing Entry", closing.name, "error_message")

	if not error_value and frappe.message_log:
		error_value = frappe.message_log[-1]

	if not error_value:
		error_value = str(exc)

	return _normalize_shift_close_error_message(error_value)


def _normalize_closing_balances_payload(
	closing_balances: list[dict[str, Any]] | dict[str, Any] | str | None,
) -> list[dict[str, Any]]:
	if closing_balances is None:
		return []

	payload = closing_balances
	if isinstance(payload, str):
		text = payload.strip()
		if not text:
			return []
		try:
			payload = json.loads(text)
		except Exception:
			frappe.throw(_("Invalid closing balances payload."))

	if isinstance(payload, dict):
		payload = [payload]

	if not isinstance(payload, list):
		frappe.throw(_("Invalid closing balances payload."))

	normalized: list[dict[str, Any]] = []
	for row in payload:
		if not isinstance(row, dict):
			frappe.throw(_("Invalid closing balances row."))
		normalized.append(row)

	return normalized


def _normalize_opening_balances_payload(
	opening_balances: list[dict[str, Any]] | dict[str, Any] | str | None,
) -> list[dict[str, Any]]:
	if opening_balances is None:
		return []

	payload = opening_balances
	if isinstance(payload, str):
		text = payload.strip()
		if not text:
			return []
		try:
			payload = json.loads(text)
		except Exception:
			frappe.throw(_("Invalid opening balances payload."))

	if isinstance(payload, dict):
		payload = [payload]

	if not isinstance(payload, list):
		frappe.throw(_("Invalid opening balances payload."))

	normalized: list[dict[str, Any]] = []
	for row in payload:
		if not isinstance(row, dict):
			frappe.throw(_("Invalid opening balances row."))
		normalized.append(row)

	return normalized


def _get_required_cash_count(row: dict[str, Any], fieldname: str, label: str) -> float:
	mode = (row.get("mode_of_payment") or _("this payment method")).strip()
	if fieldname not in row or row.get(fieldname) is None:
		frappe.throw(_("{0} is required for {1}.").format(label, mode))

	raw_value = row.get(fieldname)
	if isinstance(raw_value, str) and not raw_value.strip():
		frappe.throw(_("{0} is required for {1}.").format(label, mode))

	try:
		amount = Decimal(str(raw_value).strip())
	except (InvalidOperation, ValueError):
		frappe.throw(_("{0} must be a valid number for {1}.").format(label, mode))

	if not amount.is_finite():
		frappe.throw(_("{0} must be a valid number for {1}.").format(label, mode))

	if amount < 0:
		frappe.throw(_("{0} cannot be negative for {1}.").format(label, mode))

	return flt(str(amount))


def _resolve_pos_profile_account(
	company: str, pos_profile: str, branch: str | None, mode_of_payment: str | None
) -> str | None:
	# Required by business flow: use the account named after the POS Profile.
	if frappe.db.exists("Account", {"company": company, "is_group": 0, "account_name": pos_profile}):
		return frappe.db.get_value(
			"Account", {"company": company, "is_group": 0, "account_name": pos_profile}, "name"
		)

	if frappe.db.exists("Account", {"company": company, "is_group": 0, "name": pos_profile}):
		return pos_profile

	return None


def _get_employee_for_user(user: str) -> dict[str, Any] | None:
	employee = frappe.db.get_value(
		"Employee",
		{"user_id": user},
		["name", "employee_name", "branch"],
		as_dict=True,
	)
	if not employee:
		return None

	# Read shift requirement from User doctype, not Employee
	employee["require_pos_shift"] = bool(
		int(frappe.db.get_value("User", user, "custom_require_pos_shift") or 0)
	)
	return employee


def _serialize_opening_entry(opening) -> dict[str, Any]:
	user_fullname = frappe.db.get_value("User", opening.user, "full_name") or ""
	employee_name = frappe.db.get_value("Employee", {"user_id": opening.user}, "employee_name") or ""
	return {
		"name": opening.name,
		"status": opening.status,
		"user": opening.user,
		"user_full_name": user_fullname,
		"employee_name": employee_name,
		"company": opening.company,
		"pos_profile": opening.pos_profile,
		"period_start_date": opening.period_start_date,
		"period_end_date": opening.period_end_date,
		"balance_details": [
			{
				"mode_of_payment": row.mode_of_payment,
			}
			for row in (opening.balance_details or [])
		],
	}


def _get_latest_opening_entry(
	user: str | None = None, pos_profile: str | None = None
) -> dict[str, Any] | None:
	filters: dict[str, Any] = {
		"status": "Open",
		"docstatus": 1,
	}
	if user:
		filters["user"] = user
	if pos_profile:
		filters["pos_profile"] = pos_profile

	entries = frappe.get_all(
		"POS Opening Entry",
		filters=filters,
		fields=["name"],
		order_by="period_start_date desc, modified desc",
		limit=1,
	)

	if not entries:
		return None

	opening = frappe.get_doc("POS Opening Entry", entries[0]["name"])
	return _serialize_opening_entry(opening)


@frappe.whitelist(allow_guest=False)
def get_active_shift(pos_profile: str | None = None):
	"""Return the active (Open) shift for the given POS Profile.

	Shift logic is scoped entirely to the POS Profile – shifts on other
	profiles are irrelevant and never returned.  When *pos_profile* is not
	supplied the function returns ``None``.

	A user may have open shifts on multiple profiles simultaneously; only
	the shift matching *pos_profile* is considered.

	The returned dict includes an ``is_current_user`` flag:
	* ``1`` – the calling user owns the shift.
	* ``0`` – another user owns the shift.
	"""
	if not pos_profile:
		return None

	user = frappe.session.user

	# Find any open shift on this profile, regardless of owner.
	profile_shift = _get_latest_opening_entry(pos_profile=pos_profile)
	if not profile_shift:
		return None

	profile_shift["is_current_user"] = int(profile_shift.get("user") == user)
	return profile_shift


@frappe.whitelist(allow_guest=False)
def get_shift_payment_methods(pos_profile: str):
	if not pos_profile:
		frappe.throw(_("POS Profile is required"))

	user = frappe.session.user
	_assert_user_has_profile_access(user, pos_profile)

	profile = frappe.get_doc("POS Profile", pos_profile)
	company = profile.company
	branch = getattr(profile, "branch", None)
	mode = _get_profile_primary_mode_of_payment(profile)
	if not mode:
		frappe.throw(_("POS Profile {0} has no payment methods configured").format(pos_profile))

	account = _resolve_pos_profile_account(company, pos_profile, branch, mode)
	if not account:
		frappe.throw(
			_("No account named as POS Profile {0} was found in company {1}.").format(pos_profile, company)
		)

	try:
		_ensure_mode_of_payment_account(mode, company, account)
	except Exception:
		frappe.log_error(
			frappe.get_traceback(),
			"jarz_pos.shift.ensure_mode_of_payment_account",
		)

	return [
		{
			"mode_of_payment": mode,
			"account": account,
			"company": company,
			"branch": branch,
			"amounts_hidden": 1,
		}
	]


@frappe.whitelist(allow_guest=False)
def start_shift(pos_profile: str, opening_balances: list[dict[str, Any]] | None = None):
	user = frappe.session.user

	if not pos_profile:
		frappe.throw(_("POS Profile is required"))

	_assert_user_has_profile_access(user, pos_profile)

	# Only check THIS profile for an existing open shift.
	# Users may have open shifts on other profiles – that is allowed.
	profile_open = _get_latest_opening_entry(pos_profile=pos_profile)
	if profile_open:
		if profile_open.get("user") == user:
			frappe.throw(
				_("You already have an open shift on this profile: {0}").format(profile_open["name"]),
				title=_("Shift Already Open"),
			)
		else:
			opener = (
				profile_open.get("employee_name")
				or profile_open.get("user_full_name")
				or profile_open.get("user", "Unknown")
			)
			frappe.throw(
				_(
					"POS Profile {0} already has an open shift started by {1}. "
					"That shift must be closed first."
				).format(pos_profile, opener),
				title=_("Shift Blocked"),
			)

	company = frappe.db.get_value("POS Profile", pos_profile, "company")
	if not company:
		frappe.throw(_("POS Profile {0} was not found").format(pos_profile))

	profile_account = _resolve_pos_profile_account(company, pos_profile, None, None)
	if not profile_account:
		frappe.throw(
			_("No account named as POS Profile {0} was found in company {1}.").format(pos_profile, company)
		)

	opening_doc = frappe.new_doc("POS Opening Entry")
	opening_doc.user = user
	opening_doc.company = company
	opening_doc.pos_profile = pos_profile
	opening_doc.period_start_date = now_datetime()
	opening_doc.posting_date = nowdate()

	rows = _normalize_opening_balances_payload(opening_balances)
	if not rows:
		frappe.throw(_("At least one opening balance row is required"))

	opening_differences: list[dict[str, Any]] = []
	captured_one = False
	for row in rows:
		if captured_one:
			break
		mode = ((row or {}).get("mode_of_payment") or "").strip()
		if not mode:
			frappe.throw(_("Mode of Payment is required for opening balance row."))

		row_account = (row or {}).get("account") or profile_account
		if row_account:
			try:
				_ensure_mode_of_payment_account(mode, company, row_account)
			except Exception:
				frappe.log_error(
					frappe.get_traceback(),
					"jarz_pos.shift.ensure_mode_of_payment_account.start_shift",
				)

		system_balance = flt(_get_account_balance(row_account, company))
		confirmed_opening = _get_required_cash_count(
			row,
			"opening_amount",
			_("Opening cash count"),
		)
		difference = flt(confirmed_opening - system_balance, 2)

		opening_doc.append(
			"balance_details",
			{
				"mode_of_payment": mode,
				"opening_amount": confirmed_opening,
			},
		)

		opening_differences.append(
			{
				"mode_of_payment": mode,
				"account": row_account,
				"system_balance": system_balance,
				"confirmed_opening_amount": confirmed_opening,
				"difference": difference,
			}
		)
		captured_one = True

	if not opening_doc.balance_details:
		frappe.throw(_("At least one opening balance row is required"))

	opening_doc.insert(ignore_permissions=True)
	opening_doc.submit()

	# --- Create a Journal Entry if there is a discrepancy at opening ---
	opening_journal_entry = None
	if opening_differences:
		lines = [_("Opening confirmation differences:")]
		for diff in opening_differences:
			lines.append(
				_("{0} | Account: {1} | System: {2} | Confirmed: {3} | Difference: {4}").format(
					diff["mode_of_payment"],
					diff.get("account") or "-",
					flt(diff.get("system_balance") or 0),
					flt(diff.get("confirmed_opening_amount") or 0),
					flt(diff.get("difference") or 0),
				)
			)
		opening_doc.add_comment("Comment", "\n".join(lines))

		# Create JE for the discrepancy (surplus or shortage at opening)
		for diff in opening_differences:
			diff_amount = flt(diff.get("difference") or 0, 2)
			diff_account = diff.get("account")
			if diff_amount != 0 and diff_account:
				try:
					opening_journal_entry = _create_discrepancy_journal_entry(
						company=company,
						cash_account=diff_account,
						closing_amount=flt(diff.get("confirmed_opening_amount") or 0),
						expected_amount=flt(diff.get("system_balance") or 0),
						opening_entry=opening_doc.name,
						closing_entry="Opening",
					)
				except Exception:
					frappe.log_error(
						frappe.get_traceback(),
						"jarz_pos.shift.discrepancy_journal_entry.start_shift",
					)
				break  # single-account flow

	employee = _get_employee_for_user(user)

	# Notify all users on this POS profile about shift start
	_notify_shift_event(
		pos_profile=pos_profile,
		event_type="started",
		user=user,
		opening_entry=opening_doc.name,
	)

	return {
		"opening_entry": opening_doc.name,
		"employee": employee,
		"opening_differences": opening_differences,
		"journal_entry": opening_journal_entry,
	}


def _get_shift_account_movements(account: str, company: str, start_date, end_date) -> list[dict[str, Any]]:
	"""Return all GL movements affecting the shift cash account within the shift window."""
	if not account:
		return []

	start_dt = get_datetime(start_date)
	end_dt = get_datetime(end_date)

	entries = frappe.get_all(
		"GL Entry",
		filters={
			"account": account,
			"company": company,
			"is_cancelled": 0,
			"creation": ["between", [start_dt, end_dt]],
		},
		fields=[
			"name",
			"creation",
			"posting_date",
			"voucher_type",
			"voucher_no",
			"debit",
			"credit",
			"against",
			"remarks",
		],
		order_by="creation asc",
		limit=QUERY_LIMITS.GL_ENTRIES,
	)

	movements: list[dict[str, Any]] = []
	for entry in entries:
		debit = flt(entry.debit)
		credit = flt(entry.credit)
		movements.append(
			{
				"name": entry.name,
				"creation": str(entry.creation) if entry.creation else None,
				"posting_date": str(entry.posting_date) if entry.posting_date else None,
				"voucher_type": entry.voucher_type,
				"voucher_no": entry.voucher_no,
				"debit": debit,
				"credit": credit,
				"amount": flt(debit - credit),
				"against": entry.against,
				"remarks": entry.remarks,
			}
		)

	return movements


def _build_shift_payment_reconciliation_rows(rows: Any) -> list[dict[str, Any]]:
	normalized: list[dict[str, Any]] = []
	seen_modes: set[str] = set()

	for row in rows or []:
		if isinstance(row, dict):
			mode = str(row.get("mode_of_payment") or "").strip()
			opening_amount = flt(row.get("opening_amount"))
		else:
			mode = str(getattr(row, "mode_of_payment", "") or "").strip()
			opening_amount = flt(getattr(row, "opening_amount", 0))

		if not mode or mode in seen_modes:
			continue

		normalized.append(
			{
				"mode_of_payment": mode,
				"opening_amount": opening_amount,
				"expected_amount": opening_amount,
				"closing_amount": 0.0,
				"difference": 0.0,
			}
		)
		seen_modes.add(mode)

	return normalized


def _get_shift_payment_reconciliation_rows(opening, closing=None) -> list[dict[str, Any]]:
	closing_rows = _build_shift_payment_reconciliation_rows(
		getattr(closing, "payment_reconciliation", None) if closing else None
	)
	if closing_rows:
		return closing_rows

	opening_rows = _build_shift_payment_reconciliation_rows(getattr(opening, "balance_details", None))
	if opening_rows:
		return opening_rows

	opening_name = getattr(opening, "name", None) or _("this shift")
	frappe.throw(
		_("No payment method is available to close shift {0}. Restart the shift or contact support.").format(
			opening_name
		)
	)


def _sync_shift_payment_reconciliation_rows(closing, rows: list[dict[str, Any]]) -> None:
	if isinstance(getattr(closing, "doctype", None), str):
		closing.set("payment_reconciliation", [])
		for row in rows:
			closing.append("payment_reconciliation", row)
		return

	closing.payment_reconciliation = [SimpleNamespace(**row) for row in rows]


@frappe.whitelist(allow_guest=False)
def get_shift_summary(pos_opening_entry: str):
	if not pos_opening_entry:
		frappe.throw(_("POS Opening Entry is required"))

	opening = frappe.get_doc("POS Opening Entry", pos_opening_entry)
	if opening.user != frappe.session.user:
		frappe.throw(_("You are not allowed to access this shift"), frappe.PermissionError)

	closing_draft = make_closing_entry_from_opening(opening)
	payment_reconciliation_rows = _get_shift_payment_reconciliation_rows(opening, closing_draft)

	account = _resolve_pos_profile_account(opening.company, opening.pos_profile, None, None)

	account_movements = _get_shift_account_movements(
		account=account,
		company=opening.company,
		start_date=opening.period_start_date,
		end_date=now_datetime(),
	)
	courier_close_block = _get_shift_courier_close_block(opening.pos_profile)

	sales_vouchers = {
		row.get("voucher_no")
		for row in account_movements
		if row.get("voucher_type") in ("Sales Invoice", "POS Invoice") and row.get("voucher_no")
	}

	return {
		"opening_entry": opening.name,
		"status": opening.status,
		"user": opening.user,
		"company": opening.company,
		"pos_profile": opening.pos_profile,
		"period_start_date": opening.period_start_date,
		"period_end_date": opening.period_end_date,
		"invoice_count": len(sales_vouchers),
		"amounts_hidden": 1,
		"variance_visible": 0,
		"total_quantity": flt(closing_draft.total_quantity),
		"account": account,
		"courier_close_block": courier_close_block,
		"sales_invoices": [],
		"payment_reconciliation": [
			{
				"mode_of_payment": row["mode_of_payment"],
			}
			for row in payment_reconciliation_rows
		],
	}


def _get_shift_courier_profile_expr() -> str:
	"""Deprecated shim — the expression now lives with the carry service."""
	from jarz_pos.services.courier_carry import _courier_profile_expr

	return _courier_profile_expr()


def _get_shift_close_party_label(party_type: str, party: str) -> str:
	from jarz_pos.services.courier_carry import party_label

	return party_label(party_type, party)


def _get_shift_courier_close_block(pos_profile: str, detail_limit: int = 5) -> dict[str, Any]:
	"""Money still out with couriers on *pos_profile*, invoice by invoice.

	The closing screen renders this as a checklist: the operator confirms each
	line is genuinely still with its courier before the till can be counted out.
	"""
	from jarz_pos.services.courier_carry import build_close_block

	return build_close_block(pos_profile, detail_limit=detail_limit)


def _assert_courier_acknowledgement(pos_profile: str, acknowledged: list[str] | None) -> dict[str, Any]:
	"""Require an explicit per-transaction confirmation before closing.

	A courier who delivers the last order and drives home is a normal night, not
	an incident, so the close no longer refuses — but it does not wave the money
	through either.  Every unsettled transaction on the branch must be ticked;
	anything the operator leaves unticked is money they have NOT vouched for, and
	the close stops so they can settle it or look again.

	The check is a set comparison against what is unsettled *right now*, not
	against what the client was shown: a transaction settled by a colleague
	mid-close simply drops out, and one created mid-close is caught rather than
	silently carried.
	"""
	block = _get_shift_courier_close_block(pos_profile)
	rows = block.get("transactions") or []
	if not rows:
		return block

	acknowledged_set = set(acknowledged or [])
	missing = [row for row in rows if row.get("courier_transaction") not in acknowledged_set]
	if not missing:
		return block

	preview = ", ".join(
		str(row.get("reference_invoice") or row.get("courier_transaction")) for row in missing[:5]
	)
	if len(missing) > 5:
		preview = f"{preview}, +{len(missing) - 5}"

	frappe.throw(
		_(
			"{0} courier transaction(s) on {1} have not been confirmed: {2}. "
			"Settle them, or confirm on the closing screen that the money is "
			"still with the courier."
		).format(len(missing), pos_profile, preview),
		title=_("Unconfirmed Courier Money"),
	)
	return block


def _throw_if_shift_has_unsettled_courier_transactions(pos_profile: str) -> dict[str, Any]:
	"""Legacy hard block, kept for callers that cannot acknowledge.

	Retained so a client too old to send an acknowledgement list still gets the
	old refusal rather than an accidental silent carry.
	"""
	block = _get_shift_courier_close_block(pos_profile)
	if not block.get("blocked"):
		return block

	frappe.throw(
		_(
			"You still have {0} unsettled courier transaction(s) for {1} courier(s) across {2} invoice(s) on POS Profile {3}. Settle courier balances before closing the shift."
		).format(
			block.get("transaction_count", 0),
			block.get("party_count", 0),
			block.get("invoice_count", 0),
			pos_profile,
		),
		title=_("Unsettled Courier Transactions"),
	)
	return block


def _get_or_create_cash_over_short_account(company: str) -> str:
	"""Return (or create) a 'Cash Over/Short' expense account for shift discrepancies.

	Prefers the account set in Jarz POS Settings if available.
	"""
	# Try Jarz POS Settings first – but only trust the configured account when it
	# actually exists as a non-group ledger account in this company. A stale or
	# missing value (e.g. carried over from a clone/restore) would otherwise make
	# the discrepancy Journal Entry fail its link validation, silently skipping the
	# Cash Over/Short posting. When invalid, fall through to find-or-create below.
	try:
		from jarz_pos.doctype.jarz_pos_settings.jarz_pos_settings import get_jarz_settings

		s = get_jarz_settings()
		configured = (s.cash_over_short_account or "").strip() if s else ""
		if configured and frappe.db.get_value(
			"Account",
			{"name": configured, "company": company, "is_group": 0},
			"name",
		):
			return configured
	except Exception:
		pass

	account_name = ACCOUNTS.CASH_OVER_SHORT
	existing = frappe.db.get_value(
		"Account",
		{"company": company, "account_name": account_name, "is_group": 0},
		"name",
	)
	if existing:
		return existing

	# Find a suitable parent – Indirect Expenses or Expenses
	parent = frappe.db.get_value(
		"Account",
		{
			"company": company,
			"is_group": 1,
			"root_type": "Expense",
			"account_name": ACCOUNTS.INDIRECT_EXPENSES,
		},
		"name",
	)
	if not parent:
		parent = frappe.db.get_value(
			"Account",
			{"company": company, "is_group": 1, "root_type": "Expense"},
			"name",
		)
	if not parent:
		frappe.throw(_("Cannot find an Expense parent account in company {0}").format(company))

	acc = frappe.get_doc(
		{
			"doctype": "Account",
			"account_name": account_name,
			"parent_account": parent,
			"company": company,
			"account_type": "Expense Account",
			"root_type": "Expense",
			"is_group": 0,
		}
	)
	acc.insert(ignore_permissions=True)
	return acc.name


def _create_discrepancy_journal_entry(
	company: str,
	cash_account: str,
	closing_amount: float,
	expected_amount: float,
	opening_entry: str,
	closing_entry: str,
):
	"""Create a Journal Entry for the difference between confirmed closing and expected amount.

	- If closing > expected: surplus – debit cash, credit over/short (income side)
	- If closing < expected: shortage – debit over/short (expense), credit cash
	"""
	diff = flt(closing_amount - expected_amount, 2)
	if diff == 0:
		return None

	over_short_account = _get_or_create_cash_over_short_account(company)

	remark = _("Shift cash discrepancy for {0} → {1}. Expected {2}, confirmed {3}, difference {4}").format(
		opening_entry, closing_entry, expected_amount, closing_amount, diff
	)

	je = frappe.new_doc("Journal Entry")
	je.voucher_type = "Journal Entry"
	je.company = company
	je.posting_date = nowdate()
	je.user_remark = remark

	if diff > 0:
		# Surplus: cash account has more than expected
		je.append(
			"accounts",
			{
				"account": cash_account,
				"debit_in_account_currency": abs(diff),
				"credit_in_account_currency": 0,
			},
		)
		je.append(
			"accounts",
			{
				"account": over_short_account,
				"debit_in_account_currency": 0,
				"credit_in_account_currency": abs(diff),
			},
		)
	else:
		# Shortage: cash account has less than expected
		je.append(
			"accounts",
			{
				"account": over_short_account,
				"debit_in_account_currency": abs(diff),
				"credit_in_account_currency": 0,
			},
		)
		je.append(
			"accounts",
			{
				"account": cash_account,
				"debit_in_account_currency": 0,
				"credit_in_account_currency": abs(diff),
			},
		)

	je.insert(ignore_permissions=True)
	je.submit()
	return je.name


def _close_shift(
	opening,
	closing_balances: list[dict[str, Any]] | None,
	*,
	forced_by: str | None = None,
	reason: str | None = None,
	acknowledge_unsettled: bool = False,
	acknowledged_courier_transactions: list[str] | None = None,
) -> dict[str, Any]:
	"""Close *opening* and post any Cash Over/Short difference.

	Shared by the normal close (the opener closes their own shift) and the
	manager force-close, so both take the identical accounting path: the counted
	amount is whatever the closer typed, and any variance books the same
	discrepancy Journal Entry. ``forced_by`` only adds an audit trail — it never
	changes the numbers.

	Money still out with couriers takes one of three paths:

	* ``acknowledged_courier_transactions`` — the closer ticked each line on the
	  closing screen. Every unsettled transaction must appear, and each one is
	  stamped as carried out of this shift.
	* ``forced_by`` + ``acknowledge_unsettled`` — a manager closing somebody
	  else's shift, who cannot vouch line by line for cash they never handled.
	  The balances are recorded and carried wholesale.
	* neither — the legacy hard refusal, so an old client cannot carry money by
	  omission.
	"""
	closing_balances = _normalize_closing_balances_payload(closing_balances)
	if not closing_balances:
		frappe.throw(_("At least one closing balance row is required"))

	if opening.status != "Open" or opening.docstatus != 1:
		frappe.throw(_("Selected POS Opening Entry should be open."), title=_("Invalid Opening Entry"))

	courier_block: dict[str, Any] = {}
	if forced_by and acknowledge_unsettled:
		# The manager has seen the outstanding courier balances and is closing
		# anyway — record exactly what was left open rather than dropping it.
		# Checked before the per-line path because a manager closing somebody
		# else's till cannot vouch for cash they never handled; requiring them
		# to tick each line would only teach them to tick without reading.
		courier_block = _get_shift_courier_close_block(opening.pos_profile)
	elif acknowledged_courier_transactions is not None:
		# The closer confirmed, line by line, that this money is still out.
		courier_block = _assert_courier_acknowledgement(
			opening.pos_profile, acknowledged_courier_transactions
		)
	else:
		_throw_if_shift_has_unsettled_courier_transactions(opening.pos_profile)

	closing = make_closing_entry_from_opening(opening)
	closing_payment_reconciliation = getattr(closing, "payment_reconciliation", None) or []
	normalized_closing_rows = _build_shift_payment_reconciliation_rows(closing_payment_reconciliation)
	payment_reconciliation_rows = _get_shift_payment_reconciliation_rows(opening, closing)
	if not normalized_closing_rows or len(normalized_closing_rows) != len(closing_payment_reconciliation):
		_sync_shift_payment_reconciliation_rows(closing, payment_reconciliation_rows)

	account = _resolve_pos_profile_account(opening.company, opening.pos_profile, None, None)
	system_expected = flt(_get_account_balance(account, opening.company)) if account else 0

	closing_map: dict[str, float] = {}
	for row in closing_balances or []:
		mode = ((row or {}).get("mode_of_payment") or "").strip()
		if not mode:
			frappe.throw(_("Mode of Payment is required for closing balance row."))
		closing_map[mode] = _get_required_cash_count(
			row,
			"closing_amount",
			_("Closing cash count"),
		)

	for row in closing.payment_reconciliation or []:
		if row.mode_of_payment not in closing_map:
			frappe.throw(_("Closing cash count is required for {0}.").format(row.mode_of_payment))
		row.expected_amount = system_expected
		row.closing_amount = flt(closing_map[row.mode_of_payment])
		# Stored as well, so Desk and any SQL reading the column see the
		# variance. Readers still compute it: older closings hold 0 here.
		row.difference = round(flt(row.closing_amount) - flt(row.expected_amount), 2)

	try:
		closing.insert(ignore_permissions=True)
		closing.submit()
	except Exception as exc:
		traceback_text = frappe.get_traceback()
		error_message = _get_shift_close_error_message(exc, closing, traceback_text)
		frappe.log_error(traceback_text, "jarz_pos.shift.end_shift")
		frappe.throw(
			_("Failed to close shift: {0}").format(error_message),
			title=_("Shift Close Failed"),
		)

	if forced_by:
		_record_force_close_audit(
			opening=opening,
			closing=closing,
			forced_by=forced_by,
			reason=reason,
			courier_block=courier_block,
		)

	# --- Carry the acknowledged courier money into the next shift ---
	# Stamped only once the closing entry is submitted: a close that fails
	# validation must not leave transactions marked as carried by a shift that
	# is in fact still open.
	carried_courier: dict[str, Any] = {"count": 0, "net_balance": 0.0, "transactions": []}
	if courier_block.get("transactions"):
		from jarz_pos.services.courier_carry import stamp_carried

		carried_courier = stamp_carried(
			courier_block["transactions"],
			opening_entry=opening.name,
			user=frappe.session.user,
		)
		_add_carry_audit_comment(
			opening=opening,
			closing=closing,
			courier_block=courier_block,
			forced_by=forced_by,
		)

	# --- Create discrepancy journal entry if closing differs from expected ---
	journal_entry = None
	if account:
		for row in closing.payment_reconciliation or []:
			diff = flt(row.closing_amount - row.expected_amount, 2)
			if diff != 0:
				try:
					journal_entry = _create_discrepancy_journal_entry(
						company=opening.company,
						cash_account=account,
						closing_amount=flt(row.closing_amount),
						expected_amount=flt(row.expected_amount),
						opening_entry=opening.name,
						closing_entry=closing.name,
					)
				except Exception:
					frappe.log_error(
						frappe.get_traceback(),
						"jarz_pos.shift.discrepancy_journal_entry",
					)
				break  # single-account flow

	# Fetch final account balance after closing
	account_balance = _get_account_balance(account, opening.company) if account else 0

	account_movements = _get_shift_account_movements(
		account=account,
		company=opening.company,
		start_date=opening.period_start_date,
		end_date=now_datetime(),
	)
	total_inflows = sum(flt(row.get("debit")) for row in account_movements)
	total_outflows = sum(flt(row.get("credit")) for row in account_movements)
	sales_vouchers = {
		row.get("voucher_no")
		for row in account_movements
		if row.get("voucher_type") in ("Sales Invoice", "POS Invoice") and row.get("voucher_no")
	}

	result = {
		"closing_entry": closing.name,
		"opening_entry": opening.name,
		"status": closing.status,
		"journal_entry": journal_entry,
		"account": account,
		"account_balance": flt(account_balance),
		"amounts_hidden": 0,
		"variance_visible": 1,
		"payment_reconciliation": [
			{
				"mode_of_payment": row.mode_of_payment,
				"opening_amount": flt(row.opening_amount),
				"expected_amount": flt(row.expected_amount),
				"closing_amount": flt(row.closing_amount),
				"difference": flt(row.closing_amount - row.expected_amount, 2),
			}
			for row in (closing.payment_reconciliation or [])
		],
		"invoice_count": len(sales_vouchers),
		"grand_total": flt(total_inflows),
		"net_total": flt(closing.net_total),
		"total_quantity": flt(closing.total_quantity),
		"total_sales": flt(total_inflows),
		"total_outflows": flt(total_outflows),
		"net_movement": flt(total_inflows - total_outflows),
		"account_movements": account_movements,
		"sales_invoices": [],
		"forced_by": forced_by,
		"shift_user": opening.user,
		# What this close handed forward: the closer sees it on the summary
		# screen, so "money is still out with a courier" is the last thing they
		# read before they log out.
		"carried_courier_count": int(carried_courier.get("count") or 0),
		"carried_courier_amount": flt(carried_courier.get("net_balance")),
		"carried_courier_parties": courier_block.get("parties") or [],
	}

	# Notify all users on this POS profile about shift end. The shift's owner is
	# the subject of the message even when a manager pressed the button, so the
	# branch sees "<opener>'s shift was closed", not the manager's name alone.
	_notify_shift_event(
		pos_profile=opening.pos_profile,
		event_type="ended",
		user=opening.user or frappe.session.user,
		opening_entry=opening.name,
	)

	return result


def _add_carry_audit_comment(
	*,
	opening,
	closing,
	courier_block: dict[str, Any],
	forced_by: str | None,
) -> None:
	"""Write who let which courier money walk, onto both shift entries.

	The stamps on the Courier Transactions are the queryable record; this is the
	human one, readable from the shift itself without knowing that Courier
	Transactions exist.
	"""
	try:
		rows = courier_block.get("transactions") or []
		if not rows:
			return
		actor = forced_by or frappe.session.user
		actor_name = frappe.db.get_value("User", actor, "full_name") or actor
		total = flt(sum(flt(row.get("net_balance")) for row in rows))
		lines = [
			# nosemgrep: string-concat-in-list -- intentional: one message string split over two source lines
			f"{actor_name} ({actor}) confirmed at close that {len(rows)} courier "
			f"transaction(s) worth {total:.2f} are still out with couriers."
		]
		for party in (courier_block.get("parties") or [])[:5]:
			lines.append(
				"- {}: {} transaction(s), {:.2f}".format(
					party.get("display_name") or party.get("party"),
					party.get("transaction_count"),
					flt(party.get("net_balance")),
				)
			)
		message = "<br>".join(lines)
		for doc in (opening, closing):
			try:
				doc.add_comment("Comment", message)
			except Exception:
				continue
	except Exception:
		frappe.log_error(frappe.get_traceback(), "jarz_pos.shift.carry_audit")


def _record_force_close_audit(
	*,
	opening,
	closing,
	forced_by: str,
	reason: str | None,
	courier_block: dict[str, Any] | None = None,
) -> None:
	"""Leave a permanent trail of who force-closed whose shift, and why."""
	try:
		forced_by_name = frappe.db.get_value("User", forced_by, "full_name") or forced_by
		owner_name = frappe.db.get_value("User", opening.user, "full_name") or opening.user
		lines = [
			# nosemgrep: string-concat-in-list -- intentional: one message string split over two source lines
			f"Shift force-closed by {forced_by_name} ({forced_by}) on behalf of "
			f"{owner_name} ({opening.user}).",
			f"Reason: {reason or 'not provided'}",
		]
		if courier_block and courier_block.get("blocked"):
			lines.append(
				"Closed with {} unsettled courier transaction(s) across {} invoice(s) "
				"still outstanding; these remain open and must still be settled.".format(
					courier_block.get("transaction_count", 0),
					courier_block.get("invoice_count", 0),
				)
			)
		message = " ".join(lines)
		for doc in (opening, closing):
			try:
				doc.add_comment("Comment", message)
			except Exception:
				continue
		frappe.logger().info(f"SHIFT: force close {opening.name} (owner={opening.user}) by {forced_by}")
	except Exception:
		# An audit-trail failure must not roll back a completed close, but it
		# must never pass unnoticed either.
		frappe.log_error(frappe.get_traceback(), "jarz_pos.shift.force_close_audit")


@frappe.whitelist(allow_guest=False)
def end_shift(
	pos_opening_entry: str,
	closing_balances: list[dict[str, Any]] | None = None,
	acknowledged_courier_transactions: Any = None,
):
	"""Close your own shift.

	``acknowledged_courier_transactions`` is the list of Courier Transaction
	names the closer ticked to confirm the money is still with the courier.
	Omitting it entirely keeps the old behaviour — the close refuses while
	anything is unsettled — so an un-upgraded client cannot carry money by
	accident. Sending an empty list is a valid answer meaning "nothing is out",
	and it is verified against the database rather than trusted.
	"""
	if not pos_opening_entry:
		frappe.throw(_("POS Opening Entry is required"))

	opening = frappe.get_doc("POS Opening Entry", pos_opening_entry)

	if opening.user != frappe.session.user:
		frappe.throw(_("You are not allowed to close this shift"), frappe.PermissionError)

	from jarz_pos.services.courier_carry import normalize_acknowledgement

	acknowledged = (
		normalize_acknowledgement(acknowledged_courier_transactions)
		if acknowledged_courier_transactions is not None
		else None
	)

	return _close_shift(
		opening,
		closing_balances,
		acknowledged_courier_transactions=acknowledged,
	)


@frappe.whitelist(allow_guest=False)
def force_close_shift(
	pos_opening_entry: str,
	closing_balances: list[dict[str, Any]] | None = None,
	reason: str | None = None,
	acknowledge_unsettled: bool | int | str = False,
	acknowledged_courier_transactions: Any = None,
):
	"""Close a shift opened by somebody else.

	Only the opener could close a shift, and nobody else could start one on that
	branch while it stayed open — so a staff member who left with an open shift
	took the whole branch offline until an administrator intervened in Desk.
	This is that escape hatch, restricted to shift-monitor managers and to the
	branches they are assigned to.

	The manager supplies the counted amounts exactly as the opener would, so a
	real drawer difference still books a Cash Over/Short entry instead of being
	papered over.
	"""
	from jarz_pos.utils.access_control import (
		ensure_profile_scoped_invoice_access,
	)

	if not pos_opening_entry:
		frappe.throw(_("POS Opening Entry is required"))

	reason = (reason or "").strip()
	if not reason:
		frappe.throw(_("A reason is required when closing someone else's shift."))

	opening = frappe.get_doc("POS Opening Entry", pos_opening_entry)

	# Role gate (may this user force-close at all?) …
	from jarz_pos.api.manager import _ensure_shift_monitor_access

	_ensure_shift_monitor_access()
	# … then branch gate (is this branch one of theirs?).
	ensure_profile_scoped_invoice_access(
		frappe._dict({"custom_kanban_profile": opening.pos_profile}),
		action_label="closing a shift on this branch",
	)

	from jarz_pos.services.courier_carry import normalize_acknowledgement

	acknowledged = (
		normalize_acknowledgement(acknowledged_courier_transactions)
		if acknowledged_courier_transactions is not None
		else None
	)

	if opening.user == frappe.session.user:
		# Nothing forced about closing your own shift — take the normal path so
		# the audit trail stays meaningful.
		return _close_shift(
			opening,
			closing_balances,
			acknowledged_courier_transactions=acknowledged,
		)

	acknowledge = (
		str(acknowledge_unsettled or "").strip().lower()
		in {
			"1",
			"true",
			"yes",
			"y",
			"on",
		}
		or acknowledge_unsettled is True
	)

	return _close_shift(
		opening,
		closing_balances,
		forced_by=frappe.session.user,
		reason=reason,
		acknowledge_unsettled=acknowledge,
		acknowledged_courier_transactions=acknowledged,
	)


@frappe.whitelist(allow_guest=False)
def get_force_close_preview(pos_opening_entry: str) -> dict[str, Any]:
	"""What a manager needs to see before force-closing someone else's shift.

	Returns the shift's owner, its payment modes (so the UI can ask for a count
	per mode), and any courier balances that would block a normal close.
	"""
	from jarz_pos.api.manager import _ensure_shift_monitor_access
	from jarz_pos.utils.access_control import ensure_profile_scoped_invoice_access

	if not pos_opening_entry:
		frappe.throw(_("POS Opening Entry is required"))

	opening = frappe.get_doc("POS Opening Entry", pos_opening_entry)
	_ensure_shift_monitor_access()
	ensure_profile_scoped_invoice_access(
		frappe._dict({"custom_kanban_profile": opening.pos_profile}),
		action_label="closing a shift on this branch",
	)

	account = _resolve_pos_profile_account(opening.company, opening.pos_profile, None, None)
	courier_block = _get_shift_courier_close_block(opening.pos_profile)

	return {
		"success": True,
		"opening_entry": opening.name,
		"pos_profile": opening.pos_profile,
		"status": opening.status,
		"user": opening.user,
		"user_full_name": frappe.db.get_value("User", opening.user, "full_name") or opening.user,
		"period_start_date": str(opening.period_start_date or ""),
		"is_current_user": opening.user == frappe.session.user,
		"modes_of_payment": [
			row.mode_of_payment for row in (opening.balance_details or []) if row.mode_of_payment
		],
		"expected_amount": flt(_get_account_balance(account, opening.company)) if account else 0.0,
		"account": account,
		"courier_block": courier_block,
	}


# ---------------------------------------------------------------------------
# Shift notification helper
# ---------------------------------------------------------------------------


def _notify_shift_event(*, pos_profile: str, event_type: str, user: str, opening_entry: str):
	"""Send realtime + FCM notifications for shift start/end to all users on the POS profile.

	Args:
	    pos_profile: POS Profile name
	    event_type: "started" or "ended"
	    user: The user who started/ended the shift
	    opening_entry: The POS Opening Entry name
	"""
	try:
		user_full_name = frappe.db.get_value("User", user, "full_name") or user

		ws_event = WS_EVENTS.SHIFT_STARTED if event_type == "started" else WS_EVENTS.SHIFT_ENDED
		payload = {
			"pos_profile": pos_profile,
			"event_type": event_type,
			"user": user,
			"user_full_name": user_full_name,
			"opening_entry": opening_entry,
			"timestamp": now_datetime().isoformat(),
		}

		# Get all users assigned to this POS profile
		profile_users = frappe.get_all(
			"POS Profile User",
			filters={"parent": pos_profile, "parenttype": "POS Profile"},
			pluck="user",
		)

		# Send realtime to each user individually
		for u in profile_users:
			frappe.publish_realtime(ws_event, payload, user=u)

		# Send FCM push notifications to registered devices
		try:
			from jarz_pos.api.notifications import (
				_get_tokens_for_users,
				_send_fcm_notifications,
				outbound_alerts_suppressed,
			)

			if outbound_alerts_suppressed(f"shift_{event_type}"):
				return

			tokens = _get_tokens_for_users(profile_users)
			if tokens:
				title = f"Shift {'Started' if event_type == 'started' else 'Ended'}"
				body = f"{user_full_name} {'opened' if event_type == 'started' else 'closed'} a shift on {pos_profile}"
				data_payload = {
					"type": f"shift_{event_type}",
					"pos_profile": pos_profile,
					"user": user,
					"user_full_name": user_full_name,
					"opening_entry": opening_entry,
					"title": title,
					"body": body,
				}
				_send_fcm_notifications(tokens, data_payload)
		except Exception:
			frappe.log_error(frappe.get_traceback(), "shift_notification.fcm_failed")

	except Exception:
		# Never let notification errors block the shift operation
		frappe.log_error(frappe.get_traceback(), "shift_notification_failed")


def _user_can_see_all_shifts() -> bool:
	"""Managers see every shift; everyone else only their own.

	Same tier that already guards the shift monitor — a cashier browsing the
	branch's takings is a different permission from reviewing their own day.
	"""
	return bool(set(frappe.get_roles()).intersection(ROLES.MANAGER))


def _row_difference(row: dict[str, Any]) -> float:
	"""Counted minus expected for one reconciliation row (over > 0, short < 0).

	`round`, not `flt(..., 2)` — see the note in list_shifts.
	"""
	return round(flt(row.get("closing_amount")) - flt(row.get("expected_amount")), 2)


@frappe.whitelist(allow_guest=False)
def list_shifts(
	limit: Any = 30,
	page: Any = 0,
	pos_profile: str | None = None,
	status: str | None = None,
	from_date: str | None = None,
	to_date: str | None = None,
	mine_only: Any = 0,
) -> dict[str, Any]:
	"""Return past shifts, newest first — the shift history view.

	Once a shift closed it left the app entirely: ``get_active_shift`` answers
	only about the open one and the monitor lists only what is still open, so
	yesterday's close was unreachable from any screen.

	Amounts stay manager-only.  ``get_shift_summary`` deliberately withholds
	every figure from the closing cashier so the count is blind, and a history
	that handed the same person those figures afterwards would be a way around
	that control — so a non-manager gets their own shifts with dates, profile
	and status, and no money.
	"""
	limit = max(1, min(cint(limit) or 30, 200))
	start = max(0, cint(page)) * limit
	is_manager = _user_can_see_all_shifts()

	filters: dict[str, Any] = {"docstatus": ["<", 2]}
	if not is_manager or cint(mine_only):
		filters["user"] = frappe.session.user
	if pos_profile:
		filters["pos_profile"] = pos_profile
	if status:
		filters["status"] = status
	if from_date and to_date:
		filters["period_start_date"] = [
			"between",
			[f"{getdate(from_date)} 00:00:00", f"{getdate(to_date)} 23:59:59"],
		]
	elif from_date:
		filters["period_start_date"] = [">=", f"{getdate(from_date)} 00:00:00"]
	elif to_date:
		filters["period_start_date"] = ["<=", f"{getdate(to_date)} 23:59:59"]

	total = frappe.db.count("POS Opening Entry", filters=filters)
	openings = frappe.get_all(
		"POS Opening Entry",
		filters=filters,
		fields=[
			"name",
			"status",
			"user",
			"company",
			"pos_profile",
			"period_start_date",
			"period_end_date",
			"docstatus",
			"creation",
		],
		order_by="period_start_date desc, creation desc",
		limit_page_length=limit,
		limit_start=start,
	)
	if not openings:
		return {"shifts": [], "total": total, "amounts_hidden": 0 if is_manager else 1}

	opening_names = [o["name"] for o in openings]
	closings = {
		row["pos_opening_entry"]: row
		for row in frappe.get_all(
			"POS Closing Entry",
			filters={"pos_opening_entry": ["in", opening_names], "docstatus": 1},
			fields=[
				"name",
				"pos_opening_entry",
				"posting_date",
				"period_end_date",
				"grand_total",
				"net_total",
				"total_quantity",
				"status",
			],
			limit_page_length=0,
		)
	}

	# Per-mode figures only exist on a submitted closing entry, and only a
	# manager may read them.
	recon_by_closing: dict[str, list[dict[str, Any]]] = {}
	if is_manager and closings:
		for row in frappe.get_all(
			"POS Closing Entry Detail",
			filters={"parent": ["in", [c["name"] for c in closings.values()]]},
			fields=[
				"parent",
				"mode_of_payment",
				"opening_amount",
				"expected_amount",
				"closing_amount",
				"difference",
			],
			order_by="parent asc, idx asc",
			limit_page_length=0,
		):
			recon_by_closing.setdefault(row["parent"], []).append(row)

	users = {o["user"] for o in openings if o.get("user")}
	full_names = (
		{
			row["name"]: row.get("full_name") or row["name"]
			for row in frappe.get_all(
				"User", filters={"name": ["in", list(users)]}, fields=["name", "full_name"]
			)
		}
		if users
		else {}
	)

	out: list[dict[str, Any]] = []
	for opening in openings:
		closing = closings.get(opening["name"])
		shift: dict[str, Any] = {
			"opening_entry": opening["name"],
			"closing_entry": (closing or {}).get("name"),
			"status": opening.get("status"),
			"user": opening.get("user"),
			"user_full_name": full_names.get(opening.get("user"), opening.get("user")),
			"company": opening.get("company"),
			"pos_profile": opening.get("pos_profile"),
			"period_start_date": opening.get("period_start_date"),
			"period_end_date": (closing or {}).get("period_end_date") or opening.get("period_end_date"),
			"is_open": (opening.get("status") or "") == "Open",
		}
		if is_manager and closing:
			rows = recon_by_closing.get(closing["name"], [])
			shift.update(
				{
					"grand_total": flt(closing.get("grand_total")),
					"net_total": flt(closing.get("net_total")),
					"total_quantity": flt(closing.get("total_quantity")),
					# The signed sum across modes — what the drawer was over or
					# short by, which is the one number a manager scans a list for.
					#
					# `round`, not `flt(..., 2)`: flt reads the rounding method
					# from System Settings when given a precision, and with no
					# site bound that lookup raises inside flt and is swallowed,
					# so flt(-50.0, 2) answers 0.0. Every unit test of this
					# endpoint would then be asserting against a silent zero.
					#
					# Computed from counted - expected rather than read from the
					# stored `difference` column: closings before 2026-10-15 never
					# had it written, so it reads 0 on every one of them.
					"difference": round(sum(_row_difference(r) for r in rows), 2),
					"payment_reconciliation": [
						{
							"mode_of_payment": r.get("mode_of_payment"),
							"opening_amount": flt(r.get("opening_amount")),
							"expected_amount": flt(r.get("expected_amount")),
							"closing_amount": flt(r.get("closing_amount")),
							"difference": _row_difference(r),
						}
						for r in rows
					],
				}
			)
		out.append(shift)

	return {
		"shifts": out,
		"total": total,
		"amounts_hidden": 0 if is_manager else 1,
	}


# ---------------------------------------------------------------------------
# Shift sales sheet (replaces the paper sheet branches fill per shift)
# ---------------------------------------------------------------------------

_SHEET_SIZE_BY_GROUP = {"small": "small", "medium": "medium", "meduim": "medium", "large": "large"}
_SHEET_SIZE_WORDS = re.compile(r"\b(large|medium|meduim|small)\b", re.IGNORECASE)


def _sheet_log(title: str) -> None:
	try:
		frappe.log_error(frappe.get_traceback(), f"jarz_pos.shift_sheet.{title}")
	except Exception:
		pass


def _sheet_iso(value: Any) -> str | None:
	if not value:
		return None
	try:
		return get_datetime(value).isoformat()
	except Exception:
		return None


def _sheet_has_column(doctype: str, column: str) -> bool:
	try:
		return bool(frappe.db.has_column(doctype, column))
	except Exception:
		return False


def _sheet_caller_access() -> tuple[bool, list[str]]:
	"""(has shift-monitor access, branches the caller may monitor)."""
	from jarz_pos.api.manager import _current_user_allowed_profiles, _has_shift_monitor_access

	has_access = bool(_has_shift_monitor_access())
	return has_access, (list(_current_user_allowed_profiles() or []) if has_access else [])


def _resolve_sheet_opening(opening_entry: str) -> dict[str, Any] | None:
	return frappe.db.get_value(
		"POS Opening Entry",
		opening_entry,
		["name", "user", "pos_profile", "period_start_date", "company"],
		as_dict=True,
	)


def _resolve_sheet_closing(opening_entry: str) -> dict[str, Any] | None:
	"""The submitted POS Closing Entry for this shift, or None while it is open."""
	try:
		return frappe.db.get_value(
			"POS Closing Entry",
			{"pos_opening_entry": opening_entry, "docstatus": 1},
			["name", "period_end_date"],
			as_dict=True,
		)
	except Exception:
		_sheet_log("closing")
		return None


def _resolve_sheet_invoices(
	branch: str, start: Any, end: Any, extra_names: list[str] | None = None
) -> list[dict[str, Any]]:
	"""Submitted, non-return Sales Invoices that belong on the sheet.

	Those of the branch created inside the shift, plus ``extra_names`` (orders that
	went out for delivery or were settled inside the shift; the caller already
	matched their branch / shift link), whenever they are submitted non-returns.
	"""
	try:
		optional = [
			c
			for c in (
				"woo_order_number",
				"custom_order_channel",
				"custom_sub_territory",
				"custom_is_pickup",
				"custom_kanban_profile",
				"custom_shipping_expense",
			)
			if _sheet_has_column("Sales Invoice", c)
		]
		cols = [
			"name",
			"creation",
			"grand_total",
			"outstanding_amount",
			"territory",
			"customer",
			"sales_partner",
			"woo_order_id",
			"custom_courier_party_type",
			"custom_courier_party",
			"custom_sales_invoice_state",
			"custom_payment_method",
			"pos_profile",
			"status",
		]
		cols = [c for c in cols if _sheet_has_column("Sales Invoice", c)] + optional
		if "custom_kanban_profile" in optional:
			branch_cond = "IFNULL(NULLIF(custom_kanban_profile, ''), pos_profile) = %(branch)s"
		else:
			branch_cond = "pos_profile = %(branch)s"
		select = ", ".join(f"`{c}`" for c in cols)
		extra_cond = " OR name IN %(extra)s" if extra_names else ""
		params: dict[str, Any] = {"branch": branch, "start": start, "end": end}
		if extra_names:
			params["extra"] = tuple(extra_names)
		return (
			frappe.db.sql(
				f"""
				SELECT {select}
				FROM `tabSales Invoice`
				WHERE docstatus = 1 AND is_return = 0
				  AND (({branch_cond} AND creation BETWEEN %(start)s AND %(end)s){extra_cond})
				ORDER BY creation ASC, name ASC
				""",
				params,
				as_dict=True,
			)
			or []
		)
	except Exception:
		_sheet_log("invoices")
		return []


def _sheet_branch_expr(alias: str = "si") -> str:
	if _sheet_has_column("Sales Invoice", "custom_kanban_profile"):
		return f"IFNULL(NULLIF({alias}.custom_kanban_profile, ''), {alias}.pos_profile)"
	return f"{alias}.pos_profile"


def _resolve_sheet_dispatches(branch: str, start: Any, end: Any) -> dict[str, Any]:
	"""invoice -> time it went Out for Delivery inside the window (branch orders).

	Mirrors ``ops_reports.get_fulfilment_sla``: the first non-migration
	``Invoice State Log`` move to "Out for Delivery"; an order with no such log
	row at all (it predates the log, 2026-10-13) falls back to the creation of
	its first submitted Delivery Note. Only times inside [start, end] count.
	"""
	out: dict[str, Any] = {}
	try:
		branch_expr = _sheet_branch_expr("si")
		params = {"branch": branch, "start": start, "end": end, "ofd": "Out for Delivery"}
		has_log = bool(frappe.db.table_exists("Invoice State Log"))
		if has_log:
			for r in (
				frappe.db.sql(
					f"""
					SELECT l.sales_invoice AS name, MIN(l.changed_at) AS at
					FROM `tabInvoice State Log` l
					JOIN `tabSales Invoice` si ON si.name = l.sales_invoice
					WHERE si.docstatus = 1 AND si.is_return = 0 AND {branch_expr} = %(branch)s
					  AND l.to_state = %(ofd)s AND IFNULL(l.source, '') NOT LIKE 'migration%%'
					  AND l.changed_at BETWEEN %(start)s AND %(end)s
					GROUP BY l.sales_invoice
					""",
					params,
					as_dict=True,
				)
				or []
			):
				out[r["name"]] = r.get("at")
		no_log = (
			"""AND NOT EXISTS (
				SELECT 1 FROM `tabInvoice State Log` l2
				WHERE l2.sales_invoice = si.name AND l2.to_state = %(ofd)s
				  AND IFNULL(l2.source, '') NOT LIKE 'migration%%')"""
			if has_log
			else ""
		)
		for r in (
			frappe.db.sql(
				f"""
				SELECT dni.against_sales_invoice AS name, MIN(dn.creation) AS at
				FROM `tabDelivery Note Item` dni
				JOIN `tabDelivery Note` dn ON dn.name = dni.parent
				JOIN `tabSales Invoice` si ON si.name = dni.against_sales_invoice
				WHERE dn.docstatus = 1 AND dn.is_return = 0
				  AND si.docstatus = 1 AND si.is_return = 0 AND {branch_expr} = %(branch)s
				  {no_log}
				GROUP BY dni.against_sales_invoice
				HAVING MIN(dn.creation) BETWEEN %(start)s AND %(end)s
				""",
				params,
				as_dict=True,
			)
			or []
		):
			out.setdefault(r["name"], r.get("at"))
	except Exception:
		_sheet_log("dispatches")
	return out


def _resolve_sheet_settlements(
	opening_entry: str, branch: str, start: Any, end: Any
) -> dict[str, dict[str, Any]]:
	"""invoice -> {"at", "amount", "fee"} for Courier Transactions settled in the shift.

	``amount`` is the order money the courier carried (Courier Transaction.amount)
	and ``fee`` the courier's delivery fee deducted at settlement
	(``shipping_amount``), so ``amount - fee`` is the cash the branch received
	(negative for a prepaid order: the branch paid the courier). A Courier
	Transaction counts when ``settled_in_shift`` is this opening entry, or when it
	has no shift link, was settled inside the window and the invoice belongs to
	the branch.
	"""
	out: dict[str, dict[str, Any]] = {}
	try:
		if not (
			_sheet_has_column("Courier Transaction", "settled_in_shift")
			and _sheet_has_column("Courier Transaction", "settled_at")
		):
			return out
		rows = (
			frappe.db.sql(
				f"""
				SELECT ct.reference_invoice AS name, ct.amount, ct.shipping_amount, ct.settled_at
				FROM `tabCourier Transaction` ct
				JOIN `tabSales Invoice` si ON si.name = ct.reference_invoice
				WHERE si.docstatus = 1 AND si.is_return = 0
				  AND (
				    ct.settled_in_shift = %(oe)s
				    OR (IFNULL(ct.settled_in_shift, '') = ''
				        AND ct.settled_at BETWEEN %(start)s AND %(end)s
				        AND {_sheet_branch_expr("si")} = %(branch)s)
				  )
				""",
				{"oe": opening_entry, "branch": branch, "start": start, "end": end},
				as_dict=True,
			)
			or []
		)
		for r in rows:
			entry = out.setdefault(r["name"], {"at": None, "amount": 0.0, "fee": 0.0})
			entry["amount"] += flt(r.get("amount"))
			entry["fee"] += flt(r.get("shipping_amount"))
			at = r.get("settled_at")
			if at and (entry["at"] is None or get_datetime(at) < get_datetime(entry["at"])):
				entry["at"] = at
	except Exception:
		_sheet_log("settlements")
	return out


def _resolve_sheet_drawer_payments(
	branch: str, company: str | None, start: Any, end: Any
) -> dict[str, dict[str, Any]]:
	"""invoice -> {"at", "amount"} for order money that reached the branch till in the window.

	Read from the till's own GL: a POS-paid Sales Invoice posting cash to the
	till, or a Receive Payment Entry into the till allocated to the invoice.
	Money through Courier Outstanding is not here; it reaches the till at the
	courier settlement, which ``_resolve_sheet_settlements`` covers.
	"""
	out: dict[str, dict[str, Any]] = {}
	try:
		from jarz_pos.utils.account_utils import get_pos_cash_account

		if not company:
			return out
		till = get_pos_cash_account(branch, company)
		if not till:
			return out
		params = {"till": till, "start": start, "end": end}

		def add(name: str, amount: Any, at: Any) -> None:
			amount = flt(amount, 2)
			if not name or amount <= 0:
				return
			entry = out.setdefault(name, {"at": None, "amount": 0.0})
			entry["amount"] = flt(entry["amount"] + amount, 2)
			if at and (entry["at"] is None or get_datetime(at) < get_datetime(entry["at"])):
				entry["at"] = at

		for r in (
			frappe.db.sql(
				"""
				SELECT gle.voucher_no AS name, SUM(gle.debit - gle.credit) AS amount, MIN(gle.creation) AS at
				FROM `tabGL Entry` gle
				WHERE gle.account = %(till)s AND gle.is_cancelled = 0
				  AND gle.voucher_type = 'Sales Invoice'
				  AND gle.creation BETWEEN %(start)s AND %(end)s
				GROUP BY gle.voucher_no
				""",
				params,
				as_dict=True,
			)
			or []
		):
			add(r.get("name"), r.get("amount"), r.get("at"))
		for r in (
			frappe.db.sql(
				"""
				SELECT per.reference_name AS name, SUM(per.allocated_amount) AS amount, MIN(g.at) AS at
				FROM (
				  -- When the money hit the till (submit), as the cash-flow card dates it.
				  SELECT voucher_no, MIN(creation) AS at
				  FROM `tabGL Entry`
				  WHERE account = %(till)s AND is_cancelled = 0 AND voucher_type = 'Payment Entry'
				    AND creation BETWEEN %(start)s AND %(end)s
				  GROUP BY voucher_no
				) g
				JOIN `tabPayment Entry` pe ON pe.name = g.voucher_no
				JOIN `tabPayment Entry Reference` per
				  ON per.parent = pe.name AND per.parenttype = 'Payment Entry'
				WHERE pe.docstatus = 1 AND pe.payment_type = 'Receive' AND pe.paid_to = %(till)s
				  AND per.reference_doctype = 'Sales Invoice'
				GROUP BY per.reference_name
				""",
				params,
				as_dict=True,
			)
			or []
		):
			add(r.get("name"), r.get("amount"), r.get("at"))
	except Exception:
		_sheet_log("drawer_payments")
	return out


SHEET_OVERDUE_LOOKBACK_DAYS = 14
# An order is on the sheet's list only when one of these happened in the shift:
# each one moves money in or out of the drawer (or hands it to a courier).
SHEET_DRAWER_REASONS = frozenset({"out_for_delivery", "settled", "paid"})


def _resolve_sheet_overdue(branch: str, start: Any, ref_end: Any, is_open: bool) -> list[dict[str, Any]]:
	"""Branch orders whose delivery slot passed by ``ref_end`` and had not gone out by then.

	``ref_end`` is the shift's end (now while it is open). An order counts when
	it is submitted, not in the Cancelled state, has a delivery slot whose end
	(``ops_reports.slot_window``; a slot with no duration ends at its start, a
	date-only slot at midnight) is at most ``ref_end`` and at most
	``SHEET_OVERDUE_LOOKBACK_DAYS`` old, and its first Out for Delivery /
	Delivered move (Invoice State Log, else the first Delivery Note, else
	``custom_delivered_at``) is missing or after ``ref_end``. A rescheduled
	order has a later slot and drops out by itself. Orders with no slot are
	left out: there is no promised time to have missed.
	"""
	try:
		from datetime import timedelta

		from jarz_pos.api.ops_reports import slot_window

		ref = get_datetime(ref_end)
		shift_start = get_datetime(start)
		floor = ref - timedelta(days=SHEET_OVERDUE_LOOKBACK_DAYS)

		def col(c: str, default: str = "NULL") -> str:
			return f"si.`{c}`" if _sheet_has_column("Sales Invoice", c) else default

		if not _sheet_has_column("Sales Invoice", "custom_delivery_date"):
			return []
		rows = (
			frappe.db.sql(
				f"""
				SELECT si.name, si.creation, si.customer, si.customer_name, si.territory,
				       si.grand_total, si.outstanding_amount, {col("woo_order_id", "0")} AS woo_order_id,
				       {col("woo_order_number")} AS woo_order_number,
				       {col("custom_sales_invoice_state")} AS state,
				       {col("custom_payment_method")} AS payment_method,
				       {col("custom_is_pickup", "0")} AS is_pickup,
				       {col("custom_sub_territory")} AS sub_territory,
				       {col("custom_delivery_date")} AS delivery_date,
				       {col("custom_delivery_time_from")} AS time_from,
				       {col("custom_delivery_duration")} AS duration,
				       {col("custom_delivery_slot_label")} AS slot_label,
				       {col("custom_delivered_at")} AS delivered_at
				FROM `tabSales Invoice` si
				WHERE si.docstatus = 1 AND si.is_return = 0
				  AND {_sheet_branch_expr("si")} = %(branch)s
				  AND si.creation <= %(ref)s
				  AND si.custom_delivery_date BETWEEN %(floor_day)s AND %(ref_day)s
				  AND IFNULL({col("custom_sales_invoice_state", "''")}, '') != 'Cancelled'
				ORDER BY si.creation ASC
				""",
				{
					"branch": branch,
					"ref": ref,
					"floor_day": floor.date(),
					"ref_day": ref.date(),
				},
				as_dict=True,
			)
			or []
		)
		due: list[tuple[dict[str, Any], Any, Any, Any]] = []
		for r in rows:
			slot_start, slot_end = slot_window(r.get("delivery_date"), r.get("time_from"), r.get("duration"))
			deadline = slot_end or slot_start
			if not deadline or deadline > ref or deadline < floor:
				continue
			due.append((r, slot_start, slot_end, deadline))
		if not due:
			return []

		names = tuple(r["name"] for r, *_ in due)
		went_out: dict[str, Any] = {}
		if frappe.db.table_exists("Invoice State Log"):
			for r in (
				frappe.db.sql(
					"""
					SELECT sales_invoice AS name, MIN(changed_at) AS at
					FROM `tabInvoice State Log`
					WHERE sales_invoice IN %(names)s
					  AND to_state IN ('Out for Delivery', 'Delivered')
					  AND IFNULL(source, '') NOT LIKE 'migration%%'
					GROUP BY sales_invoice
					""",
					{"names": names},
					as_dict=True,
				)
				or []
			):
				went_out[r["name"]] = r.get("at")
		missing = tuple(n for n in names if n not in went_out)
		if missing:
			for r in (
				frappe.db.sql(
					"""
					SELECT dni.against_sales_invoice AS name, MIN(dn.creation) AS at
					FROM `tabDelivery Note Item` dni
					JOIN `tabDelivery Note` dn ON dn.name = dni.parent
					WHERE dn.docstatus = 1 AND dn.is_return = 0 AND dni.against_sales_invoice IN %(names)s
					GROUP BY dni.against_sales_invoice
					""",
					{"names": missing},
					as_dict=True,
				)
				or []
			):
				went_out[r["name"]] = r.get("at")

		areas = _resolve_sheet_area_labels({r["territory"] for r, *_ in due if r.get("territory")})
		out: list[dict[str, Any]] = []
		for r, slot_start, slot_end, deadline in due:
			left = went_out.get(r["name"]) or r.get("delivered_at")
			if left and get_datetime(left) <= ref:
				continue
			if is_open and r.get("state") in ("Out for Delivery", "Delivered", "Returned"):
				# Moved by a writer that left no log row: it is out, just undated.
				continue
			area = areas.get(r.get("territory")) if r.get("territory") else None
			if area and r.get("sub_territory"):
				area = f"{area} - {r['sub_territory']}"
			out.append(
				{
					"name": r["name"],
					"order_number": _sheet_order_number(r),
					"customer_name": r.get("customer_name") or r.get("customer") or None,
					"created_at": _sheet_iso(r.get("creation")),
					"slot_start": _sheet_iso(slot_start),
					"slot_end": _sheet_iso(slot_end),
					"slot_label": (str(r.get("slot_label") or "").strip() or None),
					"deadline": _sheet_iso(deadline),
					"minutes_late": max(0, int((ref - deadline).total_seconds() // 60)),
					"state": r.get("state") or None,
					"is_pickup": bool(cint(r.get("is_pickup"))),
					"area": area or None,
					"payment_method": r.get("payment_method") or None,
					"grand_total": flt(r.get("grand_total"), 2),
					"outstanding_amount": flt(r.get("outstanding_amount"), 2),
					"due_in_this_shift": deadline >= shift_start,
					# Only a closed shift can learn that the order left later.
					"went_out_at": None if is_open else _sheet_iso(left),
				}
			)
		out.sort(key=lambda o: (o["deadline"] or "", o["name"]))
		return out
	except Exception:
		_sheet_log("overdue")
		return []


def _sheet_order_number(row: dict[str, Any]) -> str:
	woo_number = str(row.get("woo_order_number") or "").strip()
	woo_id = cint(row.get("woo_order_id"))
	return woo_number or (str(woo_id) if woo_id else str(row.get("name") or ""))


def _resolve_sheet_returns(branch: str, company: str | None, start: Any, end: Any) -> list[dict[str, Any]]:
	"""Credit notes of the branch's orders created in the window, plus any credit
	note of the branch refunded (Payment Entry ``RET-<credit note>``) in the window.
	"""
	try:
		number_col = (
			"orig.woo_order_number" if _sheet_has_column("Sales Invoice", "woo_order_number") else "NULL"
		)
		branch_expr = _sheet_branch_expr("orig")
		select = (
			"cn.name AS credit_note, cn.return_against AS orig_name, cn.grand_total, cn.creation, "
			f"{number_col} AS woo_order_number, orig.woo_order_id"
		)
		base = "FROM `tabSales Invoice` cn JOIN `tabSales Invoice` orig ON orig.name = cn.return_against"
		params = {"branch": branch, "start": start, "end": end}
		notes: dict[str, dict[str, Any]] = {}
		for r in (
			frappe.db.sql(
				f"""SELECT {select} {base}
				WHERE cn.docstatus = 1 AND cn.is_return = 1 AND {branch_expr} = %(branch)s
				  AND cn.creation BETWEEN %(start)s AND %(end)s""",
				params,
				as_dict=True,
			)
			or []
		):
			notes[r["credit_note"]] = r
		refunds: dict[str, dict[str, Any]] = {}
		for r in (
			frappe.db.sql(
				f"""SELECT {select}, pe.paid_amount, pe.paid_from
				{base}
				JOIN `tabPayment Entry Reference` per
				  ON per.reference_doctype = 'Sales Invoice' AND per.reference_name = cn.name
				JOIN `tabPayment Entry` pe ON pe.name = per.parent
				WHERE pe.docstatus = 1 AND pe.payment_type = 'Pay'
				  AND pe.reference_no LIKE 'RET-%%'
				  AND cn.docstatus = 1 AND cn.is_return = 1 AND {branch_expr} = %(branch)s
				  AND pe.creation BETWEEN %(start)s AND %(end)s""",
				params,
				as_dict=True,
			)
			or []
		):
			notes.setdefault(r["credit_note"], r)
			slot = refunds.setdefault(r["credit_note"], {"amount": 0.0, "account": None})
			slot["amount"] += abs(flt(r.get("paid_amount")))
			slot["account"] = slot["account"] or r.get("paid_from")
		cash_account = None
		try:
			from jarz_pos.utils.account_utils import get_pos_cash_account

			if company:
				cash_account = get_pos_cash_account(branch, company)
		except Exception:
			cash_account = None
		out = []
		for name, r in notes.items():
			refund = refunds.get(name)
			account = refund["account"] if refund else None
			kind = "credit" if not refund else ("cash" if account and account == cash_account else "bank")
			out.append(
				{
					"credit_note": name,
					"order": r["orig_name"],
					"order_number": _sheet_order_number(
						{
							"name": r["orig_name"],
							"woo_order_number": r.get("woo_order_number"),
							"woo_order_id": r.get("woo_order_id"),
						}
					),
					"amount": abs(flt(r.get("grand_total"))),
					"refund_amount": flt(refund["amount"], 2) if refund else 0.0,
					"refund_account": account,
					"refund_kind": kind,
					"at": _sheet_iso(r.get("creation")),
				}
			)
		out.sort(key=lambda x: (x.get("at") or "", x["credit_note"]))
		return out
	except Exception:
		_sheet_log("returns")
		return []


def _resolve_sheet_jar_rows(invoice_names: list[str]) -> list[dict[str, Any]]:
	if not invoice_names:
		return []
	try:
		return (
			frappe.get_all(
				"Sales Invoice Item",
				filters={"parent": ["in", invoice_names], "parenttype": "Sales Invoice"},
				fields=["parent", "item_code", "item_name", "item_group", "qty"],
				limit=0,
			)
			or []
		)
	except Exception:
		_sheet_log("jar_rows")
		return []


def _resolve_sheet_dispatched_jar_rows(branch: str, start: Any, end: Any) -> list[dict[str, Any]]:
	"""Item rows of the stock that left the branch inside the window.

	Read from the Delivery Notes themselves (submitted, non-return, created in
	[start, end]) of the branch's orders: a Delivery Note is made the moment an
	order goes Out for Delivery or is handed over, whoever does it. ``parent`` is
	the Sales Invoice, so the rows feed ``_aggregate_sheet_jars`` like invoice rows.
	A cancelled Delivery Note (the stock came back) is not counted.
	"""
	if not branch:
		return []
	try:
		branch_expr = _sheet_branch_expr("si")
		return (
			frappe.db.sql(
				f"""
				SELECT dni.against_sales_invoice AS parent, dni.item_code, dni.item_name,
				       COALESCE(NULLIF(dni.item_group, ''), i.item_group) AS item_group, dni.qty
				FROM `tabDelivery Note Item` dni
				JOIN `tabDelivery Note` dn ON dn.name = dni.parent
				JOIN `tabSales Invoice` si ON si.name = dni.against_sales_invoice
				LEFT JOIN `tabItem` i ON i.name = dni.item_code
				WHERE dn.docstatus = 1 AND dn.is_return = 0
				  AND dn.creation BETWEEN %(start)s AND %(end)s
				  AND si.is_return = 0 AND {branch_expr} = %(branch)s
				""",
				{"branch": branch, "start": start, "end": end},
				as_dict=True,
			)
			or []
		)
	except Exception:
		_sheet_log("dispatched_jar_rows")
		return []


def _resolve_sheet_bundle_codes() -> set[str]:
	try:
		from jarz_pos.api.product_analytics import _get_bundle_item_codes

		return set(_get_bundle_item_codes())
	except Exception:
		_sheet_log("bundle_codes")
		return set()


def _resolve_sheet_shipping(invoice_names: list[str]) -> dict[str, float]:
	"""Shipping Income tax row per invoice (the only honest delivery fee)."""
	out: dict[str, float] = {}
	if not invoice_names:
		return out
	try:
		rows = frappe.get_all(
			"Sales Taxes and Charges",
			filters={
				"parent": ["in", invoice_names],
				"parenttype": "Sales Invoice",
				"description": ["like", "Shipping Income%"],
			},
			fields=["parent", "tax_amount"],
			limit=0,
		)
		for row in rows or []:
			out[row["parent"]] = out.get(row["parent"], 0.0) + flt(row.get("tax_amount"))
	except Exception:
		_sheet_log("shipping")
	return out


def _resolve_sheet_courier_labels(invoices: list[dict[str, Any]]) -> dict[tuple[str, str], str]:
	"""(party_type, party) -> label, one query per party type."""
	from jarz_pos.services.courier_carry import party_label

	wanted: dict[str, set[str]] = {}
	for inv in invoices:
		party = inv.get("custom_courier_party")
		if party:
			wanted.setdefault(str(inv.get("custom_courier_party_type") or ""), set()).add(party)
	labels: dict[tuple[str, str], str] = {}
	spec = {"Employee": "employee_name", "Supplier": "supplier_name"}
	for ptype, parties in wanted.items():
		found: dict[str, str] = {}
		if ptype in spec:
			try:
				rows = frappe.get_all(
					ptype, filters={"name": ["in", list(parties)]}, fields=["name", spec[ptype]], limit=0
				)
				found = {r["name"]: r.get(spec[ptype]) for r in rows or []}
			except Exception:
				_sheet_log("courier_labels")
		for party in parties:
			labels[(ptype, party)] = found.get(party) or party_label(None, party)
	return labels


def _resolve_sheet_payment_methods(invoices: list[dict[str, Any]]) -> dict[str, str]:
	try:
		from jarz_pos.api.kanban import _get_actual_payment_method_map

		return _get_actual_payment_method_map(
			[
				{
					"name": inv.get("name"),
					"outstanding_amount": inv.get("outstanding_amount"),
					"status": inv.get("status"),
					"custom_payment_method": inv.get("custom_payment_method"),
					"sales_partner": inv.get("sales_partner"),
				}
				for inv in invoices
			]
		)
	except Exception:
		_sheet_log("payment_methods")
		return {}


def _resolve_sheet_area_labels(territories: set[str]) -> dict[str, str]:
	out: dict[str, str] = {}
	try:
		from jarz_pos.utils.invoice_utils import get_area_label

		for territory in territories:
			out[territory] = get_area_label(territory)
	except Exception:
		_sheet_log("areas")
	return out


def _resolve_sheet_payouts(branch: str, start: Any, end: Any) -> list[dict[str, Any]]:
	"""Expense requests and employee advances the branch paid out in the window."""
	payouts: list[dict[str, Any]] = []
	try:
		rows = frappe.get_all(
			"Jarz Expense Request",
			filters={
				"pos_profile": branch,
				"docstatus": ["!=", 2],
				"status": ["not in", ["Cancelled", "Rejected"]],
				"creation": ["between", [start, end]],
			},
			fields=["name", "reason_label", "amount", "status", "creation"],
			order_by="creation asc",
			limit=0,
		)
		for r in rows or []:
			payouts.append(
				{
					"kind": "expense",
					"name": r["name"],
					"label": r.get("reason_label") or r["name"],
					"amount": flt(r.get("amount")),
					"status": r.get("status") or "",
					"at": _sheet_iso(r.get("creation")),
				}
			)
	except Exception:
		_sheet_log("payouts_expense")
	try:
		if _sheet_has_column("Employee Advance", "custom_jarz_pos_profile"):
			rows = frappe.get_all(
				"Employee Advance",
				filters={
					"custom_jarz_pos_profile": branch,
					"docstatus": ["!=", 2],
					"creation": ["between", [start, end]],
				},
				fields=["name", "employee_name", "purpose", "advance_amount", "status", "creation"],
				order_by="creation asc",
				limit=0,
			)
			for r in rows or []:
				label = " - ".join(p for p in (r.get("employee_name"), r.get("purpose")) if p)
				payouts.append(
					{
						"kind": "advance",
						"name": r["name"],
						"label": label or r["name"],
						"amount": flt(r.get("advance_amount")),
						"status": r.get("status") or "",
						"at": _sheet_iso(r.get("creation")),
					}
				)
	except Exception:
		_sheet_log("payouts_advance")
	payouts.sort(key=lambda p: p.get("at") or "")
	return payouts


SHEET_CASH_KINDS = (
	"sales_cash",
	"courier_orders",
	"courier_fees",
	"expenses",
	"refunds",
	"handovers",
	"other",
)


def _classify_sheet_cash(
	vouchers: list[dict[str, Any]],
	je_legs: dict[str, list[dict[str, Any]]],
	pe_info: dict[str, dict[str, Any]],
	account_types: dict[str, dict[str, Any]],
	till: str,
	courier_outstanding: str | None,
	over_short: str | None,
	courier_parties: set[tuple[str, str]] | None = None,
) -> tuple[dict[str, float], list[dict[str, Any]]]:
	"""Sort the till's GL movements of a shift into the drawer's kinds of money.

	``vouchers`` are rows of {voucher_type, voucher_no, amount (till debit - credit),
	at}. Returns per-kind sums (money out negative) and one line per entry.

	A courier settlement Journal Entry is split in two: the order money it
	releases from Courier Outstanding (``courier_orders``) and the courier fees
	kept back (``courier_fees`` = what reached the till minus that order money).
	A Cash Over/Short entry is a shift close adjusting the previous count, which
	the counted opening already includes, so it is left out.
	"""
	sums = {k: 0.0 for k in SHEET_CASH_KINDS}
	lines: list[dict[str, Any]] = []

	def add(kind: str, amount: float, row: dict[str, Any], label: str) -> None:
		amount = flt(amount, 2)
		if abs(amount) < 0.005:
			return
		sums[kind] += amount
		lines.append(
			{
				"kind": kind,
				"voucher_type": row.get("voucher_type"),
				"voucher_no": row.get("voucher_no"),
				"label": label or row.get("voucher_no") or "",
				"amount": amount,
				"at": _sheet_iso(row.get("at")),
			}
		)

	for row in vouchers:
		vtype = row.get("voucher_type")
		amount = flt(row.get("amount"))
		name = row.get("voucher_no")
		if vtype == "Journal Entry":
			legs = [leg for leg in je_legs.get(name, []) if leg.get("account") != till]
			accounts = {leg.get("account") for leg in legs}
			title = str(row.get("title") or "")
			if courier_outstanding and courier_outstanding in accounts:
				gross = sum(
					flt(leg.get("credit")) - flt(leg.get("debit"))
					for leg in legs
					if leg.get("account") == courier_outstanding
				)
				add("courier_orders", gross, row, title)
				add("courier_fees", amount - gross, row, title)
				continue
			if over_short and over_short in accounts:
				continue
			if courier_parties and any(
				(leg.get("party_type"), leg.get("party")) in courier_parties for leg in legs
			):
				# Paying a courier (e.g. a ride-hailing driver supplier) its trip fees.
				add("courier_fees", amount, row, title)
				continue
			meta = [account_types.get(a) or {} for a in accounts]
			if any(m.get("account_type") in ("Cash", "Bank") for m in meta):
				other = next(
					(
						a
						for a in accounts
						if (account_types.get(a) or {}).get("account_type") in ("Cash", "Bank")
					),
					"",
				)
				add("handovers", amount, row, other)
			elif any(m.get("root_type") == "Expense" for m in meta) or any(
				leg.get("party_type") in ("Employee", "Supplier") for leg in legs
			):
				add("expenses", amount, row, title)
			else:
				add("other", amount, row, title)
		elif vtype == "Payment Entry":
			pe = pe_info.get(name) or {}
			label = str(pe.get("party_name") or pe.get("party") or "")
			ptype = pe.get("payment_type")
			if ptype == "Receive":
				add("sales_cash", amount, row, label)
			elif ptype == "Pay" and str(pe.get("reference_no") or "").startswith("RET-"):
				add("refunds", amount, row, label)
			elif ptype == "Pay":
				add("expenses", amount, row, label)
			else:
				add("handovers", amount, row, label)
		elif vtype == "Sales Invoice":
			add("sales_cash" if amount >= 0 else "refunds", amount, row, name)
		else:
			add("expenses" if amount < 0 else "other", amount, row, name)
	return {k: flt(v, 2) for k, v in sums.items()}, lines


def _resolve_sheet_cash_flow(
	branch: str,
	company: str | None,
	start: Any,
	end: Any,
	drawer: dict[str, float | None],
	is_open: bool,
) -> dict[str, Any] | None:
	"""How the drawer went from the opening count to the expected closing.

	Built from the branch till's own GL entries in the shift window, so every
	figure is money that actually moved. ``other`` absorbs whatever the
	classified movements do not explain, so opening + kinds == expected always.
	"""
	try:
		from jarz_pos.utils.account_utils import get_pos_cash_account

		if not company:
			return None
		till = get_pos_cash_account(branch, company)
		if not till:
			return None
		vouchers = (
			frappe.db.sql(
				"""
				SELECT voucher_type, voucher_no, SUM(debit - credit) AS amount, MIN(creation) AS at
				FROM `tabGL Entry`
				WHERE account = %(till)s AND is_cancelled = 0 AND creation BETWEEN %(start)s AND %(end)s
				GROUP BY voucher_type, voucher_no
				ORDER BY at ASC, voucher_no ASC
				""",
				{"till": till, "start": start, "end": end},
				as_dict=True,
			)
			or []
		)
		je_names = [v["voucher_no"] for v in vouchers if v.get("voucher_type") == "Journal Entry"]
		pe_names = [v["voucher_no"] for v in vouchers if v.get("voucher_type") == "Payment Entry"]
		je_legs: dict[str, list[dict[str, Any]]] = {}
		titles: dict[str, str] = {}
		if je_names:
			for leg in frappe.get_all(
				"Journal Entry Account",
				filters={"parent": ["in", je_names], "parenttype": "Journal Entry"},
				fields=["parent", "account", "debit", "credit", "party_type", "party"],
				limit=0,
			):
				je_legs.setdefault(leg["parent"], []).append(leg)
			for je in frappe.get_all(
				"Journal Entry", filters={"name": ["in", je_names]}, fields=["name", "title"], limit=0
			):
				titles[je["name"]] = je.get("title") or ""
		employee_names: dict[str, str] = {}
		employees = sorted(
			{
				leg["party"]
				for legs in je_legs.values()
				for leg in legs
				if leg.get("party_type") == "Employee" and leg.get("party")
			}
		)
		if employees:
			for e in frappe.get_all(
				"Employee", filters={"name": ["in", employees]}, fields=["name", "employee_name"], limit=0
			):
				employee_names[e["name"]] = e.get("employee_name") or e["name"]
		for v in vouchers:
			party_leg = next(
				(leg for leg in je_legs.get(v["voucher_no"], []) if leg.get("party")),
				None,
			)
			if party_leg:
				party = party_leg["party"]
				v["title"] = (
					employee_names.get(party, party) if party_leg.get("party_type") == "Employee" else party
				)
			else:
				v["title"] = titles.get(v["voucher_no"], "")
		pe_info: dict[str, dict[str, Any]] = {}
		if pe_names:
			for pe in frappe.get_all(
				"Payment Entry",
				filters={"name": ["in", pe_names]},
				fields=["name", "payment_type", "party", "party_name", "reference_no"],
				limit=0,
			):
				pe_info[pe["name"]] = pe
		leg_accounts = sorted(
			{leg["account"] for legs in je_legs.values() for leg in legs if leg.get("account")}
		)
		account_types: dict[str, dict[str, Any]] = {}
		if leg_accounts:
			for acc in frappe.get_all(
				"Account",
				filters={"name": ["in", leg_accounts]},
				fields=["name", "account_type", "root_type", "account_name"],
				limit=0,
			):
				account_types[acc["name"]] = acc
		courier_outstanding = next(
			(
				a
				for a, m in account_types.items()
				if str(m.get("account_name") or "").startswith("Courier Outstanding")
			),
			None,
		)
		over_short = next(
			(
				a
				for a, m in account_types.items()
				if "over" in str(m.get("account_name") or "").lower()
				and "short" in str(m.get("account_name") or "").lower()
			),
			None,
		)
		leg_parties = {
			(leg.get("party_type"), leg.get("party"))
			for legs in je_legs.values()
			for leg in legs
			if leg.get("party_type") in ("Supplier", "Employee") and leg.get("party")
		}
		courier_parties: set[tuple[str, str]] = set()
		if leg_parties:
			for r in (
				frappe.db.sql(
					"""SELECT DISTINCT party_type, party FROM `tabCourier Transaction`
				WHERE party IN %(parties)s""",
					{"parties": tuple(p for _t, p in leg_parties)},
					as_dict=True,
				)
				or []
			):
				key = (r.get("party_type"), r.get("party"))
				if key in leg_parties:
					courier_parties.add(key)
		sums, lines = _classify_sheet_cash(
			vouchers, je_legs, pe_info, account_types, till, courier_outstanding, over_short, courier_parties
		)
		opening = flt(drawer.get("opening_amount"))
		moved = flt(sum(sums.values()), 2)
		if not is_open and drawer.get("expected_closing") is not None:
			expected = flt(drawer.get("expected_closing"), 2)
			remainder = flt(expected - opening - moved, 2)
			if abs(remainder) >= 0.005:
				sums["other"] = flt(sums["other"] + remainder, 2)
		else:
			expected = flt(opening + moved, 2)
		return {
			"opening": opening,
			**sums,
			"expected": expected,
			"counted": None if is_open else drawer.get("actual_closing"),
			"difference": None if is_open else drawer.get("difference"),
			"is_open": bool(is_open),
			"till_account": till,
			"lines": lines,
		}
	except Exception:
		_sheet_log("cash_flow")
		return None


def _sheet_shipping_expense(inv: dict[str, Any], dispatched: bool) -> float | None:
	"""Courier cost of one order: the order's ``custom_shipping_expense``.

	0 for a pickup and for a delivery-partner order with no courier of ours
	(the partner delivers, we pay no trip); None while the order has not gone
	out yet, because the stored figure is then only the territory estimate.
	"""
	if cint(inv.get("custom_is_pickup")):
		return 0.0
	has_courier = bool(inv.get("custom_courier_party"))
	if inv.get("sales_partner") and not has_courier:
		return 0.0
	if not (dispatched or has_courier):
		return None
	return flt(inv.get("custom_shipping_expense"), 2)


def _resolve_sheet_drawer(opening_entry: str, closing_entry: str | None) -> dict[str, float | None]:
	"""Drawer figures for the manager totals; every missing part stays None."""
	out: dict[str, float | None] = {
		"opening_amount": None,
		"expected_closing": None,
		"actual_closing": None,
		"difference": None,
	}
	try:
		rows = frappe.get_all(
			"POS Opening Entry Detail",
			filters={"parent": opening_entry, "parenttype": "POS Opening Entry"},
			fields=["opening_amount"],
			limit=0,
		)
		if rows:
			out["opening_amount"] = flt(sum(flt(r.get("opening_amount")) for r in rows), 2)
	except Exception:
		_sheet_log("drawer_opening")
	if closing_entry:
		try:
			rows = frappe.get_all(
				"POS Closing Entry Detail",
				filters={"parent": closing_entry, "parenttype": "POS Closing Entry"},
				fields=["expected_amount", "closing_amount"],
				limit=0,
			)
			if rows:
				expected = flt(sum(flt(r.get("expected_amount")) for r in rows), 2)
				actual = flt(sum(flt(r.get("closing_amount")) for r in rows), 2)
				# Same definition the shift monitor row uses: counted minus expected.
				diff = flt(actual - expected, 2)
				out.update(expected_closing=expected, actual_closing=actual, difference=diff)
		except Exception:
			_sheet_log("drawer_closing")
	return out


def _sheet_flavour_label(item_name: str | None, item_code: str | None) -> str:
	"""Item name without its size word, original casing kept for display."""
	name = " ".join(_SHEET_SIZE_WORDS.sub(" ", str(item_name or item_code or "")).split())
	return name or str(item_code or "")


def _aggregate_sheet_jars(
	rows: list[dict[str, Any]], bundle_codes: set[str]
) -> tuple[dict[str, int], list[dict[str, Any]]]:
	"""Jar totals per invoice and the flavour x size matrix.

	A jar is an item row in a Small / Medium (or the legacy "Meduim") / Large item
	group. A bundle header row is the bundle's own item, not a jar, so it is
	skipped; the jars inside the bundle are their own rows and do count.
	"""
	per_invoice: dict[str, int] = {}
	matrix: dict[str, dict[str, Any]] = {}
	for row in rows or []:
		size = _SHEET_SIZE_BY_GROUP.get(str(row.get("item_group") or "").strip().lower())
		if not size or row.get("item_code") in bundle_codes:
			continue
		qty = int(flt(row.get("qty")))
		if qty <= 0:
			continue
		per_invoice[row["parent"]] = per_invoice.get(row["parent"], 0) + qty
		label = _sheet_flavour_label(row.get("item_name"), row.get("item_code"))
		entry = matrix.setdefault(label.casefold(), {"flavour": label, "small": 0, "medium": 0, "large": 0})
		entry[size] += qty
	return per_invoice, sorted(matrix.values(), key=lambda e: (e["flavour"].casefold(), e["flavour"]))


@frappe.whitelist()
def get_shift_sheet(opening_entry: str) -> dict[str, Any]:
	"""Read-only sales sheet of one shift (the digital replacement of the paper sheet).

	The opener and shift-monitor managers of the branch may read it. The staff view
	carries per-order amounts but no totals and no drawer figures (blind count).

	``orders`` lists only the orders that touched the drawer inside the shift
	window: went Out for Delivery, were courier-settled, or had cash paid into
	the branch till. An order only created in the shift (due out tomorrow) is not
	listed. Each row carries ``reasons`` (subset of created / out_for_delivery /
	settled / paid, in that order; "created" is never alone),
	``out_for_delivery_at``, ``settled_at`` and ``paid_at`` (in-window times or
	null), ``settled_amount`` (Courier Transaction amounts settled in this shift)
	and ``paid_amount`` (cash into the till for it). Rows are sorted by the
	earliest in-window moment. ``totals.orders / jars / grand_total /
	delivery_fees`` count every order created in the shift, listed or not, so
	nothing is counted twice across shifts. ``jars_by_flavour`` (and
	``totals.jars_out``) is stock, not sales: the jars on the Delivery Notes made
	inside the shift, i.e. what physically left the branch, whoever sent it out.

	``overdue`` (always a list) holds the branch's orders whose delivery slot
	passed by the shift end (now while open), at most 14 days back, that had not
	gone out by then: see ``_resolve_sheet_overdue``.

	``returns`` (always a list) holds credit notes of the branch's orders created
	in the window plus those refunded in the window: credit_note, order,
	order_number, amount, refund_amount, refund_account, refund_kind
	(cash / bank / credit) and at. The manager ``totals`` add new_orders,
	out_for_delivery_orders, settled_orders, settled_amount, returns,
	returns_amount, cash_refunds and bank_refunds.

	Delivery money per order: ``delivery_fee`` is what the customer was charged
	(the Shipping Income row, so a custom delivery charge is included),
	``shipping_expense`` the courier cost (``custom_shipping_expense``; 0 for
	pickup / partner-delivered, null before dispatch), ``courier_cash_in`` the cash
	the courier handed in for it in this shift and ``courier_fee_paid`` the fee
	kept back. Totals add delivery_orders / delivery_income / delivery_cost /
	delivery_net over orders sent out in this shift, plus courier_cash_in and
	courier_fees_paid.

	``cash_flow`` (managers only) walks the drawer from the opening count to the
	expected closing using the till's GL: sales_cash, courier_orders,
	courier_fees, expenses, refunds, handovers, other (money out negative), with
	the entries in ``lines``.
	"""
	opening = _resolve_sheet_opening(opening_entry)
	if not opening:
		frappe.throw(_("POS Opening Entry {0} not found").format(opening_entry), frappe.DoesNotExistError)

	user = frappe.session.user
	branch = opening.get("pos_profile")
	is_opener = bool(user) and opening.get("user") == user
	is_manager_view, allowed_profiles = _sheet_caller_access()
	if not is_opener and not (is_manager_view and branch in allowed_profiles):
		frappe.throw(_("Not permitted to view this shift sheet"), frappe.PermissionError)

	closing = _resolve_sheet_closing(opening_entry)
	start = opening.get("period_start_date")
	end = (closing or {}).get("period_end_date") or now_datetime()

	dispatches = _resolve_sheet_dispatches(branch, start, end)
	settlements = _resolve_sheet_settlements(opening_entry, branch, start, end)
	drawer_paid = _resolve_sheet_drawer_payments(branch, opening.get("company"), start, end)
	invoices = _resolve_sheet_invoices(
		branch, start, end, sorted(set(dispatches) | set(settlements) | set(drawer_paid))
	)
	names = [inv["name"] for inv in invoices]

	start_dt, end_dt = get_datetime(start), get_datetime(end)

	def _created_in_window(inv: dict[str, Any]) -> bool:
		# Only this branch's orders are its sales: an order of another branch
		# can be on the list (settled or paid here) without being a sale here.
		owner = inv.get("custom_kanban_profile") or inv.get("pos_profile")
		if owner and owner != branch:
			return False
		try:
			return bool(inv.get("creation")) and start_dt <= get_datetime(inv["creation"]) <= end_dt
		except Exception:
			return False

	created_names = {inv["name"] for inv in invoices if _created_in_window(inv)}

	jar_rows = _resolve_sheet_jar_rows(names)
	bundle_codes = _resolve_sheet_bundle_codes()
	jars_by_invoice, _all_flavours = _aggregate_sheet_jars(jar_rows, bundle_codes)
	# The flavour matrix is stock, not sales: the jars on the Delivery Notes made
	# inside this shift (what physically left the branch), so it matches the
	# branch's count. A jar leaves once, so it is never counted in two shifts.
	jars_out_by_invoice, jars_by_flavour = _aggregate_sheet_jars(
		_resolve_sheet_dispatched_jar_rows(branch, start, end), bundle_codes
	)
	shipping = _resolve_sheet_shipping(names)
	courier_labels = _resolve_sheet_courier_labels(invoices)
	methods = _resolve_sheet_payment_methods(invoices)
	areas = _resolve_sheet_area_labels({inv["territory"] for inv in invoices if inv.get("territory")})

	orders = []
	sort_keys: dict[str, Any] = {}
	for inv in invoices:
		reasons = []
		moments = []
		if inv["name"] in created_names:
			reasons.append("created")
			moments.append(get_datetime(inv["creation"]))
		ofd_at = dispatches.get(inv["name"])
		if ofd_at:
			reasons.append("out_for_delivery")
			moments.append(get_datetime(ofd_at))
		settled = settlements.get(inv["name"])
		if settled:
			reasons.append("settled")
			if settled.get("at"):
				moments.append(get_datetime(settled["at"]))
		paid = drawer_paid.get(inv["name"])
		if paid:
			reasons.append("paid")
			if paid.get("at"):
				moments.append(get_datetime(paid["at"]))
		if not (set(reasons) & SHEET_DRAWER_REASONS):
			# Created here but nothing reached the drawer yet (e.g. due out
			# tomorrow): it belongs to the shift that sends it out.
			continue
		sort_keys[inv["name"]] = (min(moments) if moments else start_dt, inv["name"])
		is_pickup = bool(cint(inv.get("custom_is_pickup")))
		party = inv.get("custom_courier_party")
		area = areas.get(inv.get("territory")) if inv.get("territory") else None
		sub = inv.get("custom_sub_territory")
		if area and sub:
			area = f"{area} - {sub}"
		woo_number = str(inv.get("woo_order_number") or "").strip()
		woo_id = cint(inv.get("woo_order_id"))
		orders.append(
			{
				"name": inv["name"],
				"order_number": woo_number or (str(woo_id) if woo_id else inv["name"]),
				"created_at": _sheet_iso(inv.get("creation")),
				"jars": jars_by_invoice.get(inv["name"], 0),
				"grand_total": flt(inv.get("grand_total")),
				"outstanding_amount": flt(inv.get("outstanding_amount")),
				"delivery_fee": flt(shipping.get(inv["name"], 0.0)),
				"courier": None
				if is_pickup or not party
				else courier_labels.get((str(inv.get("custom_courier_party_type") or ""), party), party),
				"is_pickup": is_pickup,
				"area": area or None,
				"payment_method": methods.get(inv["name"]) or inv.get("custom_payment_method") or None,
				"state": inv.get("custom_sales_invoice_state") or None,
				"channel": inv.get("custom_order_channel") or None,
				"reasons": reasons,
				"out_for_delivery_at": _sheet_iso(ofd_at),
				"settled_at": _sheet_iso((settled or {}).get("at")),
				"settled_amount": flt((settled or {}).get("amount"), 2) if settled else 0.0,
				"shipping_expense": _sheet_shipping_expense(inv, bool(ofd_at)),
				"courier_fee_paid": flt((settled or {}).get("fee"), 2) if settled else 0.0,
				"courier_cash_in": flt(
					flt((settled or {}).get("amount")) - flt((settled or {}).get("fee")), 2
				)
				if settled
				else 0.0,
				"paid_at": _sheet_iso((paid or {}).get("at")),
				"paid_amount": flt((paid or {}).get("amount"), 2) if paid else 0.0,
			}
		)
	orders.sort(key=lambda o: sort_keys[o["name"]])
	if not is_manager_view:
		# Blind count: the staff view never carries the money that reached the
		# drawer, even per order (the app hides it too).
		for o in orders:
			o.update(paid_amount=0.0, courier_cash_in=0.0, courier_fee_paid=0.0, settled_amount=0.0)

	returns = _resolve_sheet_returns(branch, opening.get("company"), start, end)
	overdue = _resolve_sheet_overdue(branch, start, end, is_open=not closing)

	payouts = _resolve_sheet_payouts(branch, start, end)

	totals = None
	cash_flow = None
	if is_manager_view:
		drawer = _resolve_sheet_drawer(opening_entry, (closing or {}).get("name"))
		cash_flow = _resolve_sheet_cash_flow(
			branch, opening.get("company"), start, end, drawer, is_open=not closing
		)
		# Sales count every order created in the shift, listed or not (an order
		# due out tomorrow is a sale of today but not on today's list).
		created_invoices = [inv for inv in invoices if inv["name"] in created_names]
		sent_out = [o for o in orders if "out_for_delivery" in o["reasons"] and not o["is_pickup"]]
		delivery_income = flt(sum(o["delivery_fee"] for o in sent_out), 2)
		delivery_cost = flt(sum(flt(o["shipping_expense"]) for o in sent_out), 2)
		totals = {
			"orders": len(created_invoices),
			"jars": sum(jars_by_invoice.get(inv["name"], 0) for inv in created_invoices),
			"jars_out": sum(jars_out_by_invoice.values()),
			"grand_total": flt(sum(flt(inv.get("grand_total")) for inv in created_invoices), 2),
			"delivery_fees": flt(sum(flt(shipping.get(inv["name"], 0.0)) for inv in created_invoices), 2),
			"new_orders": len(created_invoices),
			"listed_orders": len(orders),
			"paid_orders": sum(1 for o in orders if "paid" in o["reasons"]),
			"paid_amount": flt(sum(o["paid_amount"] for o in orders), 2),
			"overdue_orders": len(overdue),
			"out_for_delivery_orders": sum(1 for o in orders if "out_for_delivery" in o["reasons"]),
			"settled_orders": sum(1 for o in orders if "settled" in o["reasons"]),
			"settled_amount": flt(sum(o["settled_amount"] for o in orders), 2),
			"returns": len(returns),
			"returns_amount": flt(sum(r["amount"] for r in returns), 2),
			"cash_refunds": flt(sum(r["refund_amount"] for r in returns if r["refund_kind"] == "cash"), 2),
			"bank_refunds": flt(sum(r["refund_amount"] for r in returns if r["refund_kind"] == "bank"), 2),
			"payouts": flt(sum(p["amount"] for p in payouts), 2),
			"delivery_orders": len(sent_out),
			"delivery_income": delivery_income,
			"delivery_cost": delivery_cost,
			"delivery_net": flt(delivery_income - delivery_cost, 2),
			"courier_cash_in": flt(sum(o["courier_cash_in"] for o in orders), 2),
			"courier_fees_paid": flt(sum(o["courier_fee_paid"] for o in orders), 2),
			**drawer,
		}

	user_full_name = None
	try:
		user_full_name = frappe.db.get_value("User", opening.get("user"), "full_name")
	except Exception:
		_sheet_log("user")

	return {
		"opening_entry": opening_entry,
		"closing_entry": (closing or {}).get("name"),
		"pos_profile": branch,
		"branch_label": branch,
		"user": opening.get("user"),
		"user_full_name": user_full_name or opening.get("user"),
		"opened_at": _sheet_iso(start),
		"closed_at": _sheet_iso((closing or {}).get("period_end_date")),
		"is_open": not closing,
		"is_manager_view": is_manager_view,
		"amounts_hidden": 0 if is_manager_view else 1,
		"orders": orders,
		"overdue": overdue,
		"returns": returns,
		"jars_by_flavour": jars_by_flavour,
		"payouts": payouts,
		"totals": totals,
		"cash_flow": cash_flow,
	}
