"""History of kanban state changes on Sales Invoices.

``custom_sales_invoice_state`` holds only the state an order is in right now,
so the time an order spent in each column could never be measured. Every
transition is therefore written to ``Invoice State Log`` with a timestamp.

Two kinds of writer exist, and each logs exactly once:

* Writers that SAVE the document (kanban drag, delivery handling, submit) are
  covered by the Sales Invoice ``on_submit`` / ``on_update_after_submit`` hooks
  (:func:`log_state_on_submit`, :func:`log_state_on_update_after_submit`). The
  hook reads ``doc.flags.jarz_state_source`` to say who moved the order.
* Writers that use ``frappe.db.set_value`` or raw SQL bypass doc hooks, so each
  calls :func:`record_state_change` explicitly.

Logging must never stop an order from moving, so :func:`record_state_change`
swallows every error -- except a deadlock or lock-wait timeout. InnoDB has
already rolled back the whole transaction by then, so swallowing it would let
the caller carry on and commit only the writes made after the rollback (half a
dispatch). Those two are re-raised so the request fails as a whole.

Rows are named by hash, not a naming series: a series takes a ``FOR UPDATE``
lock on its ``tabSeries`` row inside every order move and holds it to commit.
"""

from __future__ import annotations

import frappe
from frappe.exceptions import QueryDeadlockError, QueryTimeoutError

# Errors after which the transaction is already gone; never swallow these.
_TRANSACTION_LOST = (QueryDeadlockError, QueryTimeoutError)

STATE_LOG_DOCTYPE = "Invoice State Log"
STATE_FIELD = "custom_sales_invoice_state"
DEFAULT_SOURCE = "save"


def _clean(value) -> str:
	return str(value).strip() if value is not None else ""


def _safe_log_error(title: str) -> None:
	"""``frappe.log_error`` can itself raise (e.g. on a broken transaction)."""
	try:
		frappe.log_error(title=title, message=frappe.get_traceback())
	except Exception:  # noqa: BLE001 - logging about logging must not raise
		pass


def record_state_change(
	invoice_name,
	from_state,
	to_state,
	source,
	*,
	user=None,
	at=None,
	pos_profile=None,
):
	"""Append one row to ``Invoice State Log``. NEVER raises.

	Does nothing when the state did not change or there is no target state.
	Returns the new log row name, or ``None`` when nothing was written.
	"""
	try:
		from_state = _clean(from_state)
		to_state = _clean(to_state)
		if not invoice_name or not to_state or from_state == to_state:
			return None

		if pos_profile is None:
			pos_profile = frappe.db.get_value("Sales Invoice", invoice_name, "pos_profile")

		row = frappe.get_doc(
			{
				"doctype": STATE_LOG_DOCTYPE,
				"sales_invoice": invoice_name,
				"from_state": from_state or None,
				"to_state": to_state,
				"changed_at": at or frappe.utils.now_datetime(),
				"changed_by": user or frappe.session.user,
				"source": source or DEFAULT_SOURCE,
				"pos_profile": pos_profile or None,
			}
		)
		row.insert(ignore_permissions=True)
		return row.name
	except _TRANSACTION_LOST:
		raise
	except Exception:  # noqa: BLE001 - see module docstring
		_safe_log_error("Invoice State Log write failed")
		return None


def _state_of(doc) -> str:
	return _clean(doc.get(STATE_FIELD))


def _branch_of(doc):
	"""The branch whose board holds the order (follows a branch transfer)."""
	return doc.get("custom_kanban_profile") or doc.get("pos_profile")


def _is_credit_note(doc) -> bool:
	"""Credit notes carry a board state but are not orders on the board."""
	try:
		return bool(int(doc.get("is_return") or 0))
	except Exception:  # noqa: BLE001
		return False


def log_state_on_submit(doc, method=None):
	"""``on_submit``: the first entry of an invoice (from nothing)."""
	try:
		if _is_credit_note(doc):
			return
		source = doc.flags.get("jarz_state_source") or "submit"
		record_state_change(
			doc.name,
			None,
			_state_of(doc),
			source,
			pos_profile=_branch_of(doc),
		)
	except _TRANSACTION_LOST:
		raise
	except Exception:  # noqa: BLE001 - never block a submit
		_safe_log_error("Invoice State Log on_submit failed")


def log_state_on_update_after_submit(doc, method=None):
	"""``on_update_after_submit``: log a change made by saving the document.

	Frappe loads ``_doc_before_save`` for every save (``check_if_latest``), so
	it is available even for ``ignore_version=True`` saves.
	"""
	try:
		if _is_credit_note(doc):
			return
		before = doc.get_doc_before_save()
		old_state = _state_of(before) if before is not None else ""
		record_state_change(
			doc.name,
			old_state,
			_state_of(doc),
			doc.flags.get("jarz_state_source") or DEFAULT_SOURCE,
			pos_profile=_branch_of(doc),
		)
	except _TRANSACTION_LOST:
		raise
	except Exception:  # noqa: BLE001 - never block a save
		_safe_log_error("Invoice State Log on_update_after_submit failed")
