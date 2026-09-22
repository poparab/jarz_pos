"""Everything waiting on the caller's approval, counted, for the side menu.

One call answers "does anything need me?" across every approval queue in the
app, so the drawer (and the badge on its menu button) can say so on every
screen without the manager opening five of them to find out.

Each queue reports only when the caller may actually act on it, using the SAME
role predicate its own endpoint enforces. That is the rule the drawer keeps
relearning: a count for a queue whose Approve button answers 403 is a promise
the screen cannot keep. The client therefore does no role logic of its own for
this indicator — a queue missing from the answer is one the caller cannot act on.

Counts only, no rows. The client polls this roughly once a minute while the app
is open, so every queue has to stay a ``COUNT(*)`` (plus, for the month-paged
screens, a single ``LIMIT 1`` read).

A failure in one queue never hides the others: it is logged and that queue is
left out, because a manager seeing four of five counts beats seeing none.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import frappe

from jarz_pos.constants import STATUS

#: Queue keys — the wire contract with the Flutter client
#: (``PendingApprovalQueue.key``). Renaming one silently drops that row there.
Q_EXPENSES = "expenses"
Q_EMPLOYEE_ADVANCES = "employee_advances"
Q_ITEM_REQUESTS = "item_requests"
Q_PAYMENT_RECEIPTS = "payment_receipts"
Q_CUSTOM_SHIPPING = "custom_shipping"


#: One Error Log row per broken queue per this many seconds. The endpoint is
#: polled every minute by every manager, so an unthrottled persistent fault
#: would write thousands of identical rows a day.
_LOG_THROTTLE_SECONDS = 900


def _log(key: str) -> None:
    """Throttled ``frappe.log_error`` that cannot itself raise (it can; see employee_advances._log)."""
    try:
        cache_key = f"jarz_pos:approvals_error:{key}"
        cache = frappe.cache()
        if cache.get_value(cache_key):
            return
        cache.set_value(cache_key, 1, expires_in_sec=_LOG_THROTTLE_SECONDS)
        frappe.log_error(
            title=f"get_pending_approvals: {key} queue failed",
            message=frappe.get_traceback(),
        )
    except Exception:
        pass


def _month_of(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text[:7] if len(text) >= 7 else None


# ── Queues ───────────────────────────────────────────────────────────────────
# Each returns None when the caller may not act on the queue, else a dict with
# at least ``count``. ``oldest_month`` is carried by the month-paged screens
# (Expenses, Advances): they open on the current month, so without it a request
# filed last month is counted here and invisible there.


def _expenses_queue() -> Optional[Dict[str, Any]]:
    from jarz_pos.api.expenses import _is_manager

    if not _is_manager():
        return None
    # ``status`` rather than docstatus alone: a rejected request stays a draft
    # (docstatus 0, status "Rejected"), and counting it would badge a request
    # there is nothing left to do about.
    filters = {"docstatus": 0, "status": STATUS.PENDING_APPROVAL}
    count = int(frappe.db.count("Jarz Expense Request", filters=filters) or 0)
    oldest = None
    if count:
        rows = frappe.get_all(
            "Jarz Expense Request",
            filters=filters,
            fields=["expense_month"],
            order_by="expense_month asc",
            limit=1,
        )
        oldest = _month_of(rows[0].get("expense_month")) if rows else None
    return {"count": count, "oldest_month": oldest}


def _employee_advances_queue() -> Optional[Dict[str, Any]]:
    from jarz_pos.api.employee_advances import _can_approve, hrms_available

    if not _can_approve() or not hrms_available():
        return None
    # A rejection deletes the draft, so every remaining draft is awaiting a
    # decision — the same reading the Advances screen's pending_count uses.
    filters = {"docstatus": 0}
    count = int(frappe.db.count("Employee Advance", filters=filters) or 0)
    oldest = None
    if count:
        rows = frappe.get_all(
            "Employee Advance",
            filters=filters,
            fields=["posting_date"],
            order_by="posting_date asc",
            limit=1,
        )
        oldest = _month_of(rows[0].get("posting_date")) if rows else None
    return {"count": count, "oldest_month": oldest}


def _item_requests_queue() -> Optional[Dict[str, Any]]:
    from jarz_pos.api import purchase_request

    # Reviewers only: "Accept" is the review action, and a requester's own open
    # requests are waiting on somebody else, not on them.
    if not purchase_request._can_review():
        return None
    filters: Dict[str, Any] = {
        "material_request_type": "Purchase",
        "docstatus": 1,
        "status": ["in", list(purchase_request.OPEN_STATUSES)],
    }
    if purchase_request._has_ack_fields():
        filters["custom_jarz_acknowledged_at"] = ["is", "not set"]
    return {"count": int(frappe.db.count("Material Request", filters=filters) or 0)}


def _payment_receipts_queue() -> Optional[Dict[str, Any]]:
    from jarz_pos.api import payment_receipts

    # The confirm gate with no branch named answers "may this user confirm
    # anywhere"; the count is then limited to the caller's own branches exactly
    # as the receipts list is (an empty scope means none, never "all").
    if not payment_receipts._has_payment_receipt_confirm_access(None):
        return None
    scope = payment_receipts._receipt_branch_scope()
    if not scope:
        return {"count": 0}
    count = frappe.db.count(
        "POS Payment Receipt",
        filters={
            "status": payment_receipts.RECEIPT_STATUS_UNCONFIRMED,
            "pos_profile": ["in", scope],
        },
    )
    return {"count": int(count or 0)}


def _custom_shipping_queue() -> Optional[Dict[str, Any]]:
    from jarz_pos.api.custom_shipping import _can_approve_shipping

    if not _can_approve_shipping():
        return None
    # Mirrors get_pending_custom_shipping_requests, which the Manager Dashboard
    # lists from: the badge must not count fewer than the list it leads to.
    count = frappe.db.count(
        "Custom Shipping Request", filters={"docstatus": 0, "status": "Pending"}
    )
    return {"count": int(count or 0)}


_QUEUES: List[tuple] = [
    (Q_EXPENSES, _expenses_queue),
    (Q_EMPLOYEE_ADVANCES, _employee_advances_queue),
    (Q_ITEM_REQUESTS, _item_requests_queue),
    (Q_PAYMENT_RECEIPTS, _payment_receipts_queue),
    (Q_CUSTOM_SHIPPING, _custom_shipping_queue),
]


@frappe.whitelist()
def get_pending_approvals() -> Dict[str, Any]:
    """Counts of everything awaiting the caller's decision.

    Returns ``{"eligible", "total", "queues": [{"key", "count", ...}]}``.

    ``eligible`` is False when the caller may act on no queue at all — the
    client stops polling for such a user instead of asking every minute for an
    answer that cannot change. ``queues`` lists every queue the caller may act
    on, including ones at zero, in a fixed order.
    """
    queues: List[Dict[str, Any]] = []
    eligible = False
    for key, build in _QUEUES:
        try:
            result = build()
        except Exception:
            _log(key)
            # The caller may well be entitled to this queue; staying eligible
            # keeps the client polling so the count returns once it recovers.
            eligible = True
            continue
        if result is None:
            continue
        eligible = True
        queues.append({"key": key, **result})
    return {
        "eligible": eligible,
        "total": sum(int(q.get("count") or 0) for q in queues),
        "queues": queues,
    }
