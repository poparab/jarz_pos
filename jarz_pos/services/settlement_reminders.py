"""B2B settlement reminders (cron ``0 9 * * *``, site time Africa/Cairo) and the
"collect the previous invoice with this delivery" push on Sales Invoice submit.

Daily pass (:func:`run_settlement_reminders`), for every Jarz Settlement Terms
record:

* the customer's status is computed from their OPEN credit invoices (identified
  exactly as ``api/credit`` does, all branches) by the pure
  ``services.settlement_schedule``;
* ONE tagged ToDo per recipient is kept on the Customer (description starts
  ``[jarz:settlement]``), re-dated in place while something is due, and closed
  once nothing is. Only ToDos carrying that tag are ever touched -- never an
  Assignment Rule's, never another reminder kind's;
* if :func:`settlement_schedule.plan_reminder` says so, a ``due_soon`` /
  ``due_today`` / ``overdue`` push is queued. ``last_reminder_on`` /
  ``last_reminder_kind`` are written BEFORE the push is queued, so a re-run the
  same day (a retried job, a manual run) never sends a second copy.

On submit (:func:`on_sales_invoice_submit`): a credit invoice for a customer
whose cycle is ``Invoice after Invoice`` with OLDER open credit invoices pushes
"Collect <amount> for previous invoice(s) with this delivery".

Everything here never raises, and nothing leaves the process during a test run
(``api.notifications.outbound_alerts_suppressed``): CI runs against the live
staging site, and a rollback cannot recall a push already handed to Google.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

import frappe
from frappe.utils import getdate, nowdate

from jarz_pos.services import settlement_schedule as ss

TERMS_DOCTYPE = "Jarz Settlement Terms"
#: Tag identifying the ToDos this module owns (see crm.follow_ups for why tags).
TODO_MARKER = "[jarz:settlement]"
#: Push ``data.type`` (== api.notifications.SETTLEMENT_REMINDER_NOTIFICATION_TYPE).
NOTIFICATION_TYPE = "settlement_reminder"
#: The Flutter credit-account detail route. It takes the customer through
#: ``extra`` (``{customer, customer_name}``), which the payload carries as
#: separate keys -- the route itself has no path parameter.
CREDIT_DETAIL_ROUTE = "/credit-accounts/detail"
#: Records processed per pass. B2B terms number in the dozens; the cap only
#: stops a runaway table from crowding the rest of the scheduler slot.
REMINDER_BATCH_CAP = 500

_TERMS_FIELDS = [
    "name",
    "customer",
    "customer_name",
    "enabled",
    "cycle",
    "weekdays",
    "week_interval",
    "month_days",
    "interval_days",
    "anchor_date",
    "remind_days_before",
    "overdue_repeat_days",
    "responsible_user",
    "last_reminder_on",
    "last_reminder_kind",
    "creation",
]


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------


def _safe_log(title: str) -> None:
    """``frappe.log_error`` that can never become the failure (it can raise).

    ``defer_insert`` keeps the Error Log row out of the caller's transaction:
    the on-submit trigger runs inside the invoice's own.
    """
    try:
        frappe.log_error(frappe.get_traceback(), title[:140], defer_insert=True)
    except TypeError:
        try:
            frappe.log_error(frappe.get_traceback(), title[:140])
        except Exception:
            pass
    except Exception:
        pass


def _suppressed(context: str) -> bool:
    """True during a test run. A failed import counts as suppressed, loudly."""
    try:
        from jarz_pos.api.notifications import outbound_alerts_suppressed

        return bool(outbound_alerts_suppressed(context))
    except Exception:
        _safe_log("settlement_reminders: suppression check failed")
        return True


def _recipients(responsible_user: Optional[str]) -> List[str]:
    """The responsible user when set and enabled, else every JARZ Manager."""
    user = str(responsible_user or "").strip()
    if user:
        try:
            if frappe.db.get_value("User", user, "enabled"):
                return [user]
        except Exception:
            _safe_log("settlement_reminders: responsible user lookup failed")
    from jarz_pos.api.notifications import _get_users_with_roles
    from jarz_pos.constants import ROLES

    return list(_get_users_with_roles([ROLES.JARZ_MANAGER]) or [])


def _clean(recipients: Sequence[str]) -> List[str]:
    return sorted({str(u).strip() for u in recipients or [] if str(u or "").strip() and u != "Guest"})


# ---------------------------------------------------------------------------
# Push
# ---------------------------------------------------------------------------


def enqueue_settlement_push(
    customer: str,
    customer_name: str,
    kind: str,
    amount: float,
    currency: str,
    due_date: Optional[str],
    recipients: Sequence[str],
    description: Optional[str] = None,
    invoice: Optional[str] = None,
) -> bool:
    """Queue a settlement push for after the current transaction commits.

    Returns True when a job was queued. Never raises; queues nothing during a
    test run or with no recipients.
    """
    try:
        if _suppressed(f"settlement_reminder:{kind}"):
            return False
        cleaned = _clean(recipients)
        if not customer or not cleaned:
            return False
        frappe.enqueue(
            "jarz_pos.services.settlement_reminders.send_settlement_reminder",
            queue="short",
            enqueue_after_commit=True,
            customer=str(customer),
            customer_name=str(customer_name or customer),
            kind=str(kind),
            amount=float(amount or 0),
            currency=str(currency or ""),
            due_date=str(due_date or ""),
            recipients=cleaned,
            description=str(description or ""),
            invoice=str(invoice or ""),
        )
        return True
    except Exception:
        _safe_log("settlement_reminders: enqueue failed")
        return False


def build_push_data(
    customer: str,
    customer_name: str,
    kind: str,
    amount: float,
    currency: str,
    due_date: Optional[str],
    description: Optional[str] = None,
    invoice: Optional[str] = None,
    now: Any = None,
) -> Dict[str, str]:
    """The string-only FCM / web-push data map for a settlement push."""
    now = now or frappe.utils.now_datetime()
    title, body = ss.reminder_text(kind, customer_name, float(amount or 0), currency, due_date or None, description or None)
    day = str(now.date()) if hasattr(now, "date") else str(now)[:10]
    # One tray entry per customer, kind and day (or per invoice for the
    # on-delivery push), so a re-run replaces rather than stacks, and two
    # customers never collapse into one.
    suffix = invoice or day
    return {
        "type": NOTIFICATION_TYPE,
        "kind": str(kind),
        "customer": str(customer),
        "customer_name": str(customer_name or customer),
        "route": CREDIT_DETAIL_ROUTE,
        "amount": f"{float(amount or 0):.2f}",
        "currency": str(currency or ""),
        "due_date": str(due_date or ""),
        # NOT "invoice_id": that key drives the invoice-alert tag and client
        # routing for order pushes.
        "invoice": str(invoice or ""),
        "notification_id": f"settlement-{customer}-{kind}-{suffix}",
        "title": title,
        "body": body,
        "timestamp": now.isoformat() if hasattr(now, "isoformat") else str(now),
    }


def send_settlement_reminder(
    customer: str,
    customer_name: str,
    kind: str,
    amount: float,
    currency: str,
    due_date: Optional[str],
    recipients: Sequence[str],
    description: Optional[str] = None,
    invoice: Optional[str] = None,
) -> Dict[str, Any]:
    """Background-worker entry point: deliver one settlement push. Never raises.

    FCM + web push on the approvals Android channel (``jarz_approvals``, via
    ``APPROVAL_NOTIFICATION_TYPES``). A recipient set with no device at all is
    recorded as a notification gap in Error Log rather than passing silently.
    """
    result: Dict[str, Any] = {"ok": True, "status": "skipped", "recipients": 0}
    try:
        if _suppressed(f"settlement_reminder:{kind}"):
            result["status"] = "suppressed_test_run"
            return result
        cleaned = _clean(recipients)
        result["recipients"] = len(cleaned)
        if not cleaned:
            result["status"] = "skipped_no_recipients"
            return result

        from jarz_pos.api import notifications as n

        data = build_push_data(customer, customer_name, kind, amount, currency, due_date, description, invoice)
        tokens, token_platforms = n._get_token_targets_for_users(cleaned)
        vapid_subs = n._get_vapid_subscriptions_for_users(cleaned)
        if tokens:
            fcm_result = n._send_fcm_notifications(tokens, data, platforms=token_platforms)
        else:
            fcm_result = n._new_fcm_send_result(tokens, "skipped_no_tokens")
            fcm_result["ok"] = True
        if vapid_subs:
            vapid_result = n._send_vapid_notifications(vapid_subs, data)
        else:
            vapid_result = {"ok": True, "status": "skipped_no_subscriptions", "success_count": 0, "failure_count": 0}
        if not tokens and not vapid_subs:
            n._log_notification_gap(
                "Settlement reminder reached no device",
                (
                    f"Settlement reminder '{kind}' for {customer} resolved {len(cleaned)} "
                    "recipient(s) but none has an enabled push token or web-push "
                    f"subscription. Recipients: {', '.join(cleaned)}."
                ),
                throttle_key="settlement_reminder:notokens",
            )
        result.update(
            {
                "ok": bool(fcm_result.get("ok")) or bool(vapid_result.get("ok")),
                "status": fcm_result.get("status"),
                "success_count": fcm_result.get("success_count", 0) + vapid_result.get("success_count", 0),
                "failure_count": fcm_result.get("failure_count", 0) + vapid_result.get("failure_count", 0),
            }
        )
        return result
    except Exception:
        _safe_log("settlement_reminders: send failed")
        result["ok"] = False
        result["status"] = "failed_exception"
        return result


# ---------------------------------------------------------------------------
# ToDo (one per recipient, tagged, re-dated in place, closed when nothing is due)
# ---------------------------------------------------------------------------


def _open_settlement_todos(customer: str) -> List[Dict[str, Any]]:
    return (
        frappe.get_all(
            "ToDo",
            filters={
                "reference_type": "Customer",
                "reference_name": customer,
                "status": "Open",
                "description": ["like", f"%{TODO_MARKER}%"],
            },
            fields=["name", "allocated_to"],
            order_by="creation asc",
            limit_page_length=0,
        )
        or []
    )


def _close_todo(name: str) -> None:
    # Through the document, not db.set_value: ToDo.on_update is what removes
    # the assignee from the Customer's ``_assign`` list.
    todo = frappe.get_doc("ToDo", name)
    todo.status = "Closed"
    todo.save(ignore_permissions=True)


def sync_settlement_todo(
    customer: str,
    due_date: Optional[str],
    recipients: Sequence[str],
    text: str,
    summary: Optional[Dict[str, int]] = None,
) -> None:
    """Make the customer's tagged ToDos match the current state.

    ``due_date`` None closes every one of ours. Otherwise each recipient gets
    exactly one (re-dated / re-worded in place), and ours held by anyone who is
    no longer a recipient -- or duplicates -- are closed. Raises on DB failure;
    the caller counts it.
    """
    existing = _open_settlement_todos(customer)
    if not due_date:
        for row in existing:
            _close_todo(row["name"])
            if summary is not None:
                summary["todos_closed"] += 1
        return

    description = f"{TODO_MARKER} {text}".strip()
    wanted = set(_clean(recipients))
    kept = set()
    for row in existing:
        owner = row.get("allocated_to")
        if owner in wanted and owner not in kept:
            kept.add(owner)
            frappe.db.set_value(
                "ToDo",
                row["name"],
                {"date": due_date, "description": description},
                update_modified=False,
            )
        else:
            _close_todo(row["name"])
            if summary is not None:
                summary["todos_closed"] += 1
    for user in sorted(wanted - kept):
        frappe.get_doc(
            {
                "doctype": "ToDo",
                "description": description,
                "reference_type": "Customer",
                "reference_name": customer,
                "allocated_to": user,
                "date": due_date,
                "status": "Open",
                "priority": "Medium",
            }
        ).insert(ignore_permissions=True)
        if summary is not None:
            summary["todos_opened"] += 1


# ---------------------------------------------------------------------------
# Daily pass
# ---------------------------------------------------------------------------


def _process_terms_row(
    row: Dict[str, Any],
    invoices: List[Dict[str, Any]],
    today,
    currency: str,
    summary: Dict[str, int],
) -> None:
    customer = str(row.get("customer") or "")
    if not customer:
        return
    terms = ss.parse_terms(row)
    status = ss.compute_status(terms, invoices, today)
    summary["checked"] += 1
    customer_name = row.get("customer_name") or customer

    if not terms.get("enabled"):
        # Reminders switched off: take down anything we left open.
        sync_settlement_todo(customer, None, [], "", summary)
        return

    recipients = _recipients(terms.get("responsible_user"))
    description = ss.describe(terms)

    todo_on = ss.todo_date(status, today)
    todo_amount = status["due_now_amount"] if status["due_now_amount"] > ss.MONEY_EPSILON else status["next_due_amount"]
    sync_settlement_todo(
        customer,
        todo_on,
        recipients,
        f"Collect {todo_amount:,.2f} {currency} from {customer_name} ({description})",
        summary,
    )

    kind = ss.plan_reminder(terms, status, today)
    if not kind:
        return
    # Bookkeeping FIRST: a crash or retry after this line cannot double-send.
    frappe.db.set_value(
        TERMS_DOCTYPE,
        row.get("name"),
        {"last_reminder_on": today, "last_reminder_kind": kind},
        update_modified=False,
    )
    if enqueue_settlement_push(
        customer,
        customer_name,
        kind,
        ss.reminder_amount(kind, status),
        currency,
        ss.reminder_due_date(kind, status, today),
        recipients,
        description,
    ):
        summary["sent"] += 1


def run_settlement_reminders() -> Dict[str, int]:
    """Scheduler entry point. Returns counters. Never raises."""
    summary = {"checked": 0, "sent": 0, "todos_opened": 0, "todos_closed": 0, "errors": 0}
    try:
        if not frappe.db.exists("DocType", TERMS_DOCTYPE):
            return summary
        from jarz_pos.api.credit import _credit_currency, _open_credit_invoices

        today = getdate(nowdate())
        rows = frappe.get_all(
            TERMS_DOCTYPE,
            fields=_TERMS_FIELDS,
            order_by="name asc",
            limit_page_length=REMINDER_BATCH_CAP + 1,
        ) or []
        if len(rows) > REMINDER_BATCH_CAP:
            try:
                frappe.log_error(
                    f"More than {REMINDER_BATCH_CAP} Jarz Settlement Terms records; only the first "
                    f"{REMINDER_BATCH_CAP} (by name) were processed. Raise REMINDER_BATCH_CAP.",
                    "settlement_reminders: batch cap reached",
                )
            except Exception:
                pass
            rows = rows[:REMINDER_BATCH_CAP]

        customers = [str(r.get("customer")) for r in rows if r.get("customer")]
        by_customer: Dict[str, List[Dict[str, Any]]] = {}
        for inv in _open_credit_invoices(customers=customers) if customers else []:
            by_customer.setdefault(str(inv.get("customer") or ""), []).append(
                {
                    "name": inv.get("name"),
                    "posting_date": inv.get("posting_date"),
                    "outstanding_amount": inv.get("outstanding_amount"),
                }
            )
        currency = _credit_currency()

        for row in rows:
            try:
                _process_terms_row(row, by_customer.get(str(row.get("customer") or ""), []), today, currency, summary)
            except Exception:
                summary["errors"] += 1
                _safe_log(f"settlement_reminders: {row.get('customer')} failed")
    except Exception:
        summary["errors"] += 1
        _safe_log("settlement_reminders: pass failed")
    return summary


# ---------------------------------------------------------------------------
# Invoice after Invoice: the on-submit trigger
# ---------------------------------------------------------------------------


def _is_credit_invoice(doc: Any) -> bool:
    from jarz_pos.utils.credit_utils import CREDIT_TERMS_FIELD, is_credit_intent_doc

    if is_credit_intent_doc(doc):
        return True
    try:
        return float(getattr(doc, CREDIT_TERMS_FIELD, 0) or 0) > 0
    except (TypeError, ValueError):
        return False


def on_sales_invoice_submit(doc: Any, method: Optional[str] = None) -> None:
    """Sales Invoice ``on_submit``: "collect the previous invoice(s) with this delivery".

    Fires only for a CREDIT invoice (same OR as ``utils/credit_utils``: the
    payment method or the frozen terms stamp) whose customer's enabled terms
    say ``Invoice after Invoice`` and who has OTHER open credit invoices. The
    push is queued after commit. Never raises, never blocks the submit.
    """
    try:
        if not doc or not getattr(doc, "name", None):
            return
        if int(getattr(doc, "is_return", 0) or 0):
            return
        if not _is_credit_invoice(doc):
            return  # the overwhelmingly common case: no DB work at all
        if _suppressed("settlement_reminder:collect_on_delivery"):
            return
        customer = str(getattr(doc, "customer", "") or "").strip()
        if not customer or not frappe.db.exists("DocType", TERMS_DOCTYPE):
            return
        row = frappe.db.get_value(
            TERMS_DOCTYPE,
            {"customer": customer},
            ["name", "enabled", "cycle", "responsible_user", "customer_name"],
            as_dict=True,
        )
        if not row or not int(row.get("enabled") or 0):
            return
        if ss.canonical_cycle(row.get("cycle")) != ss.CYCLE_INVOICE_AFTER_INVOICE:
            return

        from jarz_pos.api.credit import _credit_currency, _open_credit_invoices

        previous = [
            r
            for r in _open_credit_invoices(customers=[customer])
            if r.get("name") != doc.name
        ]
        amount = round(sum(float(r.get("outstanding_amount") or 0) for r in previous), 2)
        if amount <= ss.MONEY_EPSILON:
            return
        enqueue_settlement_push(
            customer,
            row.get("customer_name") or getattr(doc, "customer_name", None) or customer,
            ss.KIND_COLLECT_ON_DELIVERY,
            amount,
            _credit_currency(),
            str(getdate(nowdate())),
            _recipients(row.get("responsible_user")),
            invoice=doc.name,
        )
    except Exception:
        _safe_log("settlement_reminders: on_submit trigger failed")
