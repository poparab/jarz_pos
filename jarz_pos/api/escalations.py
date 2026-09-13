"""Jarz POS – Manager-facing order escalations.

The hourly job ``jarz_pos.tasks.escalate_unconfirmed_online_payments`` flags
unpaid InstaPay/Mobile Wallet orders that have sat Out for Delivery, still
awaiting the customer's bank/wallet transfer, past a configured "how long is
too long" threshold. Until now the only trace of that was a Notification Log
written per manager — a place nobody actually reads, so an aging unconfirmed
order was invisible unless someone happened to open the bell icon.

This module gives the mobile InstaPay Reconciliation screen a pull-based view
of the same thing: the orders that are escalated *right now*. It intentionally
re-derives the list from the Sales Invoice table rather than reading anything
the hourly job wrote, because the job's own idempotency flag
(``custom_payment_confirmation_alerted``) exists only to stop it re-notifying
every hour — an order stays escalated (and should stay visible here) long
after that flag is set, until a manager actually confirms the payment.

The threshold itself is never duplicated: both the job and this endpoint call
:func:`jarz_pos.tasks.get_unconfirmed_online_payment_alert_hours`, which is the
one place that reads ``Jarz POS Settings.instapay_unconfirmed_alert_hours``.
"""
from __future__ import annotations

import frappe
from frappe import _

from jarz_pos.constants import ROLES
from jarz_pos.tasks import get_unconfirmed_online_payment_alert_hours
from jarz_pos.utils.invoice_utils import normalize_woo_order_id


def _ensure_escalation_access() -> None:
    """Same manager tier the InstaPay Reconciliation screen's confirm action
    already requires (``jarz_pos.api.payment_receipts._has_payment_receipt_confirm_access``
    grants the admin/line-manager tier unconditionally, and everyone else only
    within their own branch). Viewing the escalation feed is read-only, so it is
    gated on the role alone; branch scoping is applied separately below.
    """
    roles = {str(role or "").strip() for role in (frappe.get_roles() or []) if str(role or "").strip()}
    allowed = ROLES.ADMIN | ROLES.LINE_MANAGER_TIER
    if not roles.intersection(allowed):
        frappe.throw(_("Not permitted: Manager access required"), frappe.PermissionError)


def _seconds_since(value) -> int | None:
    if not value:
        return None
    try:
        from frappe.utils import get_datetime, now_datetime

        dt = get_datetime(value)
        if not dt:
            return None
        return int((now_datetime() - dt).total_seconds())
    except Exception:
        return None


@frappe.whitelist(allow_guest=False)
def list_unconfirmed_online_payment_escalations(pos_profile: str | None = None) -> dict:
    """List the unpaid online-intent orders currently past the escalation threshold.

    Shares the exact filter shape
    ``jarz_pos.tasks.escalate_unconfirmed_online_payments`` uses to decide an
    order is aging, so this list is always the set the hourly job would (or
    already did) alert on.

    Branch scoping is the same rule
    ``jarz_pos.services.delivery_handling.list_unconfirmed_online_orders``
    enforces, on top of the manager-role gate:

    * An explicit ``pos_profile`` the caller is not assigned to is refused with
      ``BranchAccessError`` (a ``PermissionError``), before anything is queried.
      It used to be trusted as given, so any manager could read another
      branch's aging orders just by naming the branch.
    * With no ``pos_profile``, the list is limited to the caller's branches, and
      a caller assigned to no branch gets an empty ``orders`` list without a
      query. Empty used to mean "no filter" -- every escalated order of every
      branch.

    ``Administrator`` is unaffected: ``get_user_pos_profiles`` hands the
    unrestricted user every enabled profile, so an empty list genuinely means
    "assigned to no branch" and never "sees everything".

    Returns:
        {
          "success": True,
          "threshold_hours": int,
          "orders": [
            {
              "invoice": str,
              "woo_order_id": str | None,
              "customer": str,
              "customer_name": str,
              "branch": str | None,           # custom_kanban_profile, falling back to pos_profile
              "amount": float,
              "payment_method": str | None,
              "out_for_delivery_since": str | None,   # custom_ofd_unconfirmed_since
              "out_for_delivery_seconds": int | None,
              "threshold_hours": int,          # breached threshold, repeated per row for convenience
              "already_alerted": bool,         # the hourly job already wrote a Notification Log for this one
            },
            ...
          ]
        }
    """
    _ensure_escalation_access()

    from jarz_pos.api.manager import _current_user_allowed_profiles
    from jarz_pos.utils.access_control import BranchAccessError

    pos_profile = (pos_profile or "").strip()
    accessible_profiles = _current_user_allowed_profiles() or []

    # Refuse another branch before reading settings or querying anything.
    if pos_profile and pos_profile not in accessible_profiles:
        frappe.throw(
            _("This order belongs to a branch you are not assigned to."),
            BranchAccessError,
        )

    hours = get_unconfirmed_online_payment_alert_hours()

    if pos_profile:
        branch_filter: object = pos_profile
    elif accessible_profiles:
        branch_filter = ["in", accessible_profiles]
    else:
        # Assigned to no branch: nothing is theirs to see.
        return {"success": True, "threshold_hours": hours, "orders": []}

    cutoff = frappe.utils.add_to_date(frappe.utils.now_datetime(), hours=-hours)
    filters: dict[str, object] = {
        "docstatus": 1,
        "custom_payment_confirmation_status": "Awaiting Payment",
        "custom_ofd_unconfirmed_since": ["<=", cutoff],
        "custom_kanban_profile": branch_filter,
    }

    rows = frappe.get_all(
        "Sales Invoice",
        filters=filters,
        fields=[
            "name",
            "customer",
            "customer_name",
            "grand_total",
            "custom_payment_method",
            "custom_ofd_unconfirmed_since",
            "custom_kanban_profile",
            "pos_profile",
            "custom_payment_confirmation_alerted",
            "woo_order_id",
        ],
        order_by="custom_ofd_unconfirmed_since asc",
        limit_page_length=0,
    )

    orders = []
    for row in rows:
        unconfirmed_since = row.get("custom_ofd_unconfirmed_since")
        orders.append(
            {
                "invoice": row.get("name"),
                "woo_order_id": normalize_woo_order_id(row.get("woo_order_id")),
                "customer": row.get("customer"),
                "customer_name": row.get("customer_name"),
                "branch": row.get("custom_kanban_profile") or row.get("pos_profile"),
                "amount": float(row.get("grand_total") or 0),
                "payment_method": row.get("custom_payment_method"),
                "out_for_delivery_since": str(unconfirmed_since) if unconfirmed_since else None,
                "out_for_delivery_seconds": _seconds_since(unconfirmed_since),
                "threshold_hours": hours,
                "already_alerted": bool(row.get("custom_payment_confirmation_alerted")),
            }
        )

    return {"success": True, "threshold_hours": hours, "orders": orders}
