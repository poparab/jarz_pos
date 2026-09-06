"""Read-only WooCommerce backlog review queue, for the manager dashboard.

Serves ``Jarz Woo Backlog Exception`` rows written by the daily
``jarz_pos.services.woo_backlog_watch.run_woo_backlog_sweep`` job. Everything
in this module is a read: it never writes to a Sales Invoice, a Payment Entry,
a Delivery Note, a Courier Transaction, or the exception rows themselves.
Resolving/dismissing an entry is not exposed here -- see
``jarz_pos.services.woo_backlog_watch.close_resolved_backlog_exceptions``,
which is the only path that changes a row's status, and it only does so when
the underlying order has actually left its non-terminal state.

Gated at the manager-dashboard tier, mirroring
``jarz_pos.api.manager._ensure_manager_dashboard_access`` -- duplicated rather
than imported, because ``api/manager.py`` carries other work in this change
and stays untouched; the gate here must fail exactly the same way.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import frappe
from frappe import _
from frappe.utils import flt, get_datetime, now_datetime

from jarz_pos.constants import ROLES
from jarz_pos.services.woo_backlog_watch import EXCEPTION_DOCTYPE, STATUS_OPEN


def _has_manager_dashboard_access() -> bool:
    roles = {str(role or "").strip() for role in (frappe.get_roles() or []) if str(role or "").strip()}
    allowed = ROLES.ADMIN | ROLES.LINE_MANAGER_TIER
    return bool(roles.intersection(allowed))


def _ensure_manager_dashboard_access() -> None:
    """Same tier, same failure, as ``api.manager._ensure_manager_dashboard_access``."""
    if not _has_manager_dashboard_access():
        frappe.throw(_("Not permitted: Manager Dashboard access required"), frappe.PermissionError)


def _age_hours(expected_finish_by: Any, now: Any) -> float:
    if not expected_finish_by:
        return 0.0
    try:
        delta = now - get_datetime(expected_finish_by)
        return max(round(delta.total_seconds() / 3600.0, 1), 0.0)
    except Exception:
        return 0.0


@frappe.whitelist()
def get_woo_backlog_queue(branch: Optional[str] = None) -> Dict[str, Any]:
    """Return the open WooCommerce backlog review queue for the mobile dashboard.

    Read-only: reads ``Jarz Woo Backlog Exception`` rows only, and never
    touches a Sales Invoice or any WooCommerce record.

    Args:
        branch: optional POS Profile name to narrow the queue to one branch.

    Returns:
        {
          "success": True,
          "rows": [
            {
              "exception": "...", "invoice": "...", "woo_order_id": 123,
              "customer": "...", "pos_profile": "...", "ops_state": "...",
              "posting_date": "2026-07-01", "age_hours": 812.4,
              "outstanding_amount": 350.0, "grand_total": 350.0,
              "currency": "EGP", "first_detected_on": "...", "last_seen_on": "...",
            }, ...
          ],
          "by_day": [{"date": "2026-07-01", "count": 13, "amount": 12700.0}, ...],
          "by_branch": [{"pos_profile": "Nasr city", "count": 40, "amount": 38000.0}, ...],
          "totals": {"count": N, "amount": F},
        }
    """
    _ensure_manager_dashboard_access()

    filters: Dict[str, Any] = {"status": STATUS_OPEN}
    branch_name = str(branch or "").strip()
    if branch_name:
        filters["pos_profile"] = branch_name

    rows = frappe.get_all(
        EXCEPTION_DOCTYPE,
        filters=filters,
        fields=[
            "name",
            "sales_invoice",
            "woo_order_id",
            "customer",
            "pos_profile",
            "ops_state",
            "posting_date",
            "expected_finish_by",
            "outstanding_amount",
            "grand_total",
            "currency",
            "first_detected_on",
            "last_seen_on",
        ],
        order_by="posting_date asc",
        limit_page_length=0,
    ) or []

    now = now_datetime()
    out_rows: List[Dict[str, Any]] = []
    by_day: Dict[str, Dict[str, float]] = {}
    by_branch: Dict[str, Dict[str, float]] = {}
    total_count = 0
    total_amount = 0.0

    for row in rows:
        amount = flt(row.get("outstanding_amount"))
        age_hours = _age_hours(row.get("expected_finish_by"), now)
        day_key = str(row.get("posting_date") or "unknown")
        branch_key = str(row.get("pos_profile") or "(no branch)")

        out_rows.append(
            {
                "exception": row.get("name"),
                "invoice": row.get("sales_invoice"),
                "woo_order_id": row.get("woo_order_id"),
                "customer": row.get("customer"),
                "pos_profile": row.get("pos_profile"),
                "ops_state": row.get("ops_state"),
                "posting_date": day_key,
                "age_hours": age_hours,
                "outstanding_amount": amount,
                "grand_total": flt(row.get("grand_total")),
                "currency": row.get("currency"),
                "first_detected_on": str(row.get("first_detected_on") or ""),
                "last_seen_on": str(row.get("last_seen_on") or ""),
            }
        )

        day_bucket = by_day.setdefault(day_key, {"count": 0, "amount": 0.0})
        day_bucket["count"] += 1
        day_bucket["amount"] += amount

        branch_bucket = by_branch.setdefault(branch_key, {"count": 0, "amount": 0.0})
        branch_bucket["count"] += 1
        branch_bucket["amount"] += amount

        total_count += 1
        total_amount += amount

    return {
        "success": True,
        "rows": out_rows,
        "by_day": [
            {"date": day, "count": bucket["count"], "amount": round(bucket["amount"], 2)}
            for day, bucket in sorted(by_day.items())
        ],
        "by_branch": [
            {"pos_profile": branch, "count": bucket["count"], "amount": round(bucket["amount"], 2)}
            for branch, bucket in sorted(
                by_branch.items(), key=lambda item: item[1]["amount"], reverse=True
            )
        ],
        "totals": {"count": total_count, "amount": round(total_amount, 2)},
    }
