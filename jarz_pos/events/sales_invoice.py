"""
Sales Invoice event handlers for Kanban profile seeding and realtime notifications.

These handlers are referenced in hooks.py. Implemented as safe no-ops with
lightweight behavior so integrations depending on these hooks don't fail.

You can extend these later for full real-time UI updates.
"""

from __future__ import annotations

from typing import Any, Optional

try:
	import frappe
except Exception:  # pragma: no cover - allows import-time safety in non-Frappe contexts
	frappe = None  # type: ignore


def _safe_publish(event: str, message: dict[str, Any], doc: Any = None) -> None:
	"""Publish a realtime message to the order's branch; ignore failures.

	A bare ``publish_realtime`` lands in the site-wide ``all`` room, which puts
	one branch's invoice on every branch's socket. Routing through the branch
	helper keeps these legacy fallback events scoped like everything else.
	"""
	try:
		if frappe:
			from jarz_pos.utils.realtime import publish_invoice_event

			publish_invoice_event(event, message, doc)
	except Exception:
		# Avoid breaking document lifecycle if realtime publish fails
		try:
			if frappe:
				frappe.log_error(frappe.get_traceback(), f"jarz_pos realtime publish failed: {event}")
		except Exception:
			pass


def sync_kanban_profile(doc: Any, method: Optional[str] = None) -> None:
	"""Seed Kanban profile from POS Profile on draft invoices only.

	- Draft invoices keep `custom_kanban_profile` aligned with `pos_profile`.
	- Submitted invoices preserve `custom_kanban_profile` so post-submit
	  branch reassignment remains intact.
	- Resolve territory-based shipping expense into `custom_shipping_expense`
	  so every consumer reads the same persisted value.
	- Safe no-op if fields are missing.
	"""
	try:
		pos_profile = getattr(doc, "pos_profile", None)
		docstatus = int(getattr(doc, "docstatus", 0) or 0)
		if docstatus == 0 and pos_profile and hasattr(doc, "custom_kanban_profile"):
			if getattr(doc, "custom_kanban_profile", None) != pos_profile:
				setattr(doc, "custom_kanban_profile", pos_profile)
	except Exception:
		if frappe:
			frappe.log_error(frappe.get_traceback(), "sync_kanban_profile failed")

	# Stamp territory-based shipping expense when not already set
	try:
		if hasattr(doc, "custom_shipping_expense"):
			current = float(getattr(doc, "custom_shipping_expense", 0) or 0)
			if current <= 0 and getattr(doc, "territory", None):
				from jarz_pos.services.delivery_handling import _get_delivery_expense_amount
				expense = _get_delivery_expense_amount(doc) or 0.0
				if expense > 0:
					doc.custom_shipping_expense = expense
	except Exception:
		if frappe:
			frappe.log_error(frappe.get_traceback(), "sync_shipping_expense failed")


def _check_flag(doc: Any, fieldname: str) -> int:
    """Read a 0/1 Check field off a Document as an int. Never raises.

    ``getattr(doc, name, 0)`` is NOT safe here and was the bug this replaced.
    When the instance dict has no entry for the field, attribute lookup falls
    through to the CLASS, where Frappe's generated property descriptor lives —
    so ``getattr`` hands back the descriptor OBJECT rather than a value, and
    ``int()`` on it raises ``TypeError: int() argument must be ... not 'object'``.
    The caller caught and logged that, which meant the stock suppression this
    hook exists to enforce was silently skipped on whichever path hit it.

    ``doc.get()`` reads the instance dict and returns None when absent, which is
    the behaviour the original code assumed ``getattr`` had.
    """
    value = None
    getter = getattr(doc, "get", None)
    if callable(getter):
        try:
            value = getter(fieldname)
        except Exception:
            value = None
    if value is None:
        # Plain objects (and test doubles) that carry the attribute directly.
        value = doc.__dict__.get(fieldname) if hasattr(doc, "__dict__") else None
    if value is None or value == "":
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        # Anything else truthy (a stray object, a "1" that is not numeric) is
        # treated as set — erring toward RUNNING the suppression, because
        # skipping it double-books stock while a needless 0 costs nothing.
        return 1 if value else 0


def suppress_pos_invoice_stock_update(doc: Any, method: Optional[str] = None) -> None:
    """Force ``update_stock = 0`` on every POS Sales Invoice.

    Stock leaves the warehouse through the Delivery Note and nothing else. The
    entire delivery, settlement and return design depends on that: a return
    reverses stock with a Sales Return Delivery Note, so an invoice that also
    moved stock would be reversed once and deducted twice.

    ``services.invoice_creation`` already sets this before saving, but that only
    protects the POS API. ERPNext's ``set_pos_fields()`` re-applies the POS
    Profile's own ``update_stock`` during ``validate`` for *any* document that
    carries ``is_pos``, and every POS Profile on this site has it enabled. So a
    document that acquires ``is_pos`` and is then saved through a normal
    validate — a Woo-initiated amendment is the path that actually did this —
    silently picks the flag back up and books the stock a second time.

    Enforcing it in ``validate`` closes the hole for every path at once: POS,
    Woo, amendments, Desk. Non-POS invoices are untouched.
    """
    if not doc:
        return
    try:
        if not _check_flag(doc, "is_pos"):
            return
        if _check_flag(doc, "update_stock"):
            doc.update_stock = 0
            if frappe:
                frappe.logger().info(
                    f"jarz_pos: suppressed update_stock on POS invoice {getattr(doc, 'name', '?')}"
                )
    except Exception:
        if frappe:
            frappe.log_error(frappe.get_traceback(), "suppress_pos_invoice_stock_update failed")


def publish_new_invoice(doc: Any, method: Optional[str] = None) -> None:
	"""Notify listeners a Sales Invoice has been submitted."""
	try:
		from jarz_pos.api import notifications as _notifications  # local import to avoid circulars

		_notifications.handle_invoice_submission(doc)
	except Exception:
		# Fall back to legacy event payload if enhanced notification fails
		_safe_publish(
			"jarz_pos:new_invoice",
			{"name": getattr(doc, "name", None), "status": getattr(doc, "status", None)},
			doc,
		)
		if frappe:
			frappe.log_error(frappe.get_traceback(), "handle_invoice_submission failed")


def publish_state_change_if_needed(doc: Any, method: Optional[str] = None) -> None:
	"""Emit a generic state-change notification for already-submitted invoices.

	Intentionally lightweight. Frontend can refetch details by name.
	"""
	_safe_publish(
		"jarz_pos:invoice_state",
		{"name": getattr(doc, "name", None), "status": getattr(doc, "status", None)},
		doc,
	)


def mark_cancelled_invoice_workflow_fields(doc: Any, method: Optional[str] = None) -> None:
	"""Keep Jarz workflow fields aligned whenever a Sales Invoice is cancelled."""
	if not frappe or not doc or not getattr(doc, "name", None):
		return

	try:
		meta = frappe.get_meta("Sales Invoice")
		updates: dict[str, Any] = {}

		for fieldname in ("custom_sales_invoice_state", "sales_invoice_state", "custom_state", "state"):
			if not meta.get_field(fieldname):
				continue
			if str(getattr(doc, fieldname, None) or "").strip() != "Cancelled":
				updates[fieldname] = "Cancelled"

		if meta.get_field("custom_acceptance_status"):
			current_acceptance = str(getattr(doc, "custom_acceptance_status", None) or "").strip()
			if current_acceptance != "Accepted":
				updates["custom_acceptance_status"] = "Accepted"

			accepted_by = getattr(getattr(frappe, "session", None), "user", None)
			if meta.get_field("custom_accepted_by") and accepted_by and not getattr(doc, "custom_accepted_by", None):
				updates["custom_accepted_by"] = accepted_by

			if meta.get_field("custom_accepted_on") and not getattr(doc, "custom_accepted_on", None):
				updates["custom_accepted_on"] = frappe.utils.now_datetime()

		if not updates:
			return

		frappe.db.set_value("Sales Invoice", doc.name, updates, update_modified=False)
		for fieldname, value in updates.items():
			setattr(doc, fieldname, value)
	except Exception:
		if frappe:
			frappe.log_error(frappe.get_traceback(), "mark_cancelled_invoice_workflow_fields failed")


def block_cancel_if_dispatched(doc: Any, method: Optional[str] = None) -> None:
	"""FIX 5 (2026-07-20): document-level guard blocking cancel of a dispatched invoice.

	The OFD hard-mutation guard previously lived ONLY inside
	``api/kanban.cancel_invoice``. A raw ``SI.cancel()`` from Desk, a server script or
	any other path bypassed it and corrupted the ledger (dangling Courier Outstanding /
	Creditors balances, reversed stock against a still-owed courier, etc.). Wiring the
	SAME blocker into the Sales Invoice ``before_cancel`` lifecycle closes that hole for
	EVERY cancel path.

	The cancel is refused when the invoice shows ANY dispatch signal:
	  * ``custom_was_out_for_delivery`` — the permanent dispatch lock, or
	  * a current operational state of Out for Delivery / Delivered / Completed, or
	  * a hard downstream artifact reported by ``get_invoice_hard_mutation_blocker``
	    (submitted Delivery Note, active Delivery Trip, Courier Transaction, sales-partner
	    settlement, settlement Journal Entries, active custom shipping request).

	Legitimate pre-dispatch cancels (Sc20) are unaffected: no dispatch flag, a prep-state
	value and a ``None`` blocker mean every signal is false and the cancel proceeds.
	Returns are their own corrective artifact and are never blocked here.
	"""
	if not frappe or not doc or not getattr(doc, "name", None):
		return

	try:
		if int(getattr(doc, "is_return", 0) or 0):
			return
	except Exception:
		pass

	# Gather dispatch signals defensively — a detection error must never silently
	# let a corrupting cancel through, but must also never raise on its own.
	was_ofd = False
	try:
		was_ofd = bool(int(getattr(doc, "custom_was_out_for_delivery", 0) or 0))
	except Exception:
		was_ofd = False

	current_state = ""
	try:
		current_state = str(
			doc.get("custom_sales_invoice_state")
			or doc.get("sales_invoice_state")
			or doc.get("custom_state")
			or doc.get("state")
			or ""
		).strip().lower().replace(" ", "_").replace("-", "_")
	except Exception:
		current_state = ""
	state_dispatched = current_state in {"out_for_delivery", "delivered", "completed"}

	blocker = None
	try:
		from jarz_pos.api.manager import get_invoice_hard_mutation_blocker

		# An UNSETTLED sales-partner transaction is a pending charge with no ledger
		# behind it, not a settlement artifact; release_unsettled_partner_transactions
		# (on_cancel) drops it. A settled one still blocks: its fee is in the books.
		blocker = get_invoice_hard_mutation_blocker(
			doc, ignore_unsettled_partner_transactions=True
		)
	except Exception:
		blocker = None

	if was_ofd or state_dispatched or blocker:
		reason = None
		if isinstance(blocker, dict):
			reason = blocker.get("mutation_block_reason")
		if not reason:
			reason = frappe._(
				"This invoice was already dispatched (Out for Delivery) and cannot be "
				"cancelled directly. Use the corrective / return workflow."
			)
		frappe.throw(reason, title=frappe._("Cancellation blocked"))


def release_unsettled_partner_transactions(doc: Any, method: Optional[str] = None) -> None:
	"""Drop the pending partner fee rows of a cancelled invoice (on_cancel).

	A paid-online sales-partner order carries a Sales Partner Transaction from the
	moment it is created. Once the order is cancelled there is nothing to charge a
	commission on, and leaving the row would make ``settle_sales_partner`` post a
	fee for a cancelled order. Only Unsettled rows go; a settled row has a fee
	journal behind it and ``block_cancel_if_dispatched`` refuses the cancel first.
	Registered as a doc event so every cancel path (board, Desk, script) agrees.
	"""
	if not frappe or not doc or not getattr(doc, "name", None):
		return
	try:
		rows = frappe.get_all(
			"Sales Partner Transactions",
			filters={"reference_invoice": doc.name, "status": "Unsettled"},
			pluck="name",
		) or []
		for row_name in rows:
			frappe.delete_doc(
				"Sales Partner Transactions", row_name, ignore_permissions=True, force=True
			)
		if rows:
			frappe.logger().info(
				f"jarz_pos: released {len(rows)} unsettled partner transaction(s) on cancel of {doc.name}"
			)
	except Exception:
		if frappe:
			frappe.log_error(frappe.get_traceback(), "release_unsettled_partner_transactions failed")


def validate_invoice_before_submit(doc: Any, method: Optional[str] = None) -> None:
	"""Placeholder for pre-submit validations (e.g., bundle checks).

	Currently a no-op to avoid interrupting flows. Add validations here later.
	"""
	return None


def stamp_out_for_delivery_flag(doc: Any, method: Optional[str] = None) -> None:
	"""Permanently set custom_was_out_for_delivery=1 the first time the invoice
	enters the 'Out for Delivery' state.  Once set, this flag is never cleared —
	it acts as a hard lock preventing automated amendments even if the state
	is later changed back (e.g., mis-click correction).
	"""
	if not frappe or not doc or not getattr(doc, "name", None):
		return
	try:
		meta = frappe.get_meta("Sales Invoice")
		if not meta.get_field("custom_was_out_for_delivery"):
			return
		# Already permanently flagged — nothing to do.
		if int(getattr(doc, "custom_was_out_for_delivery", 0) or 0):
			return
		current_state = str(
			getattr(doc, "custom_sales_invoice_state", None)
			or getattr(doc, "sales_invoice_state", None)
			or ""
		).strip()
		if current_state == "Out for Delivery":
			frappe.db.set_value(
				"Sales Invoice",
				doc.name,
				"custom_was_out_for_delivery",
				1,
				update_modified=False,
			)
			doc.custom_was_out_for_delivery = 1
	except Exception:
		if frappe:
			frappe.log_error(frappe.get_traceback(), "stamp_out_for_delivery_flag failed")


def mint_tracking_token_on_ofd(doc: Any, method: Optional[str] = None) -> None:
	"""Mint the customer tracking token the first time an order goes out.

	Registered on ``on_update_after_submit`` because that is the one point every
	dispatch path converges on: the Kanban drag saves the submitted invoice, the
	Delivery Trip bulk send goes through
	``delivery_handling.update_submitted_sales_invoice_fields`` (which saves), and
	``mark_courier_outstanding`` does the same. Minting in either API module
	instead would mean the other path produced orders with no tracking link —
	the identical trap A6's pin gate has with its two ``_build_ofd_preview_errors``
	copies.

	Idempotent in the service: an invoice that already has a token keeps it, so a
	card dragged out, back and out again does not invalidate a link the customer
	already has.

	Never raises. A tracking link is a courtesy; it must not be able to fail a
	dispatch, and this hook shares a transaction with the accounting that the
	dispatch just posted.
	"""
	if not frappe or not doc or not getattr(doc, "name", None):
		return
	try:
		current_state = str(
			getattr(doc, "custom_sales_invoice_state", None)
			or getattr(doc, "sales_invoice_state", None)
			or ""
		).strip()
		if current_state != "Out for Delivery":
			return

		from jarz_pos.services import tracking

		tracking.ensure_tracking_token(doc)
	except Exception:
		if frappe:
			frappe.log_error(frappe.get_traceback(), "mint_tracking_token_on_ofd failed")

