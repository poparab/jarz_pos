"""CRM reorder forecasting (Phase 3, no ML).

Scheduled (daily) task that, for each Company customer with submitted Sales
Invoices, computes a simple reorder forecast from invoice cadence:

  - ``custom_avg_order_cycle_days`` = mean gap (days) between consecutive invoice
    dates (0 when only one invoice exists).
  - ``custom_last_order_date``      = most recent invoice posting date.
  - ``custom_predicted_next_order`` = last_order_date + avg_cycle (omitted when the
    cycle is unknown).
  - ``custom_avg_basket_value``     = mean grand_total across the customer's invoices.

Hard requirements (mirrors lead_scoring / follow_ups):
  - ERPNext v15 AND v16 safe: every DocType/field access is guarded; a missing
    doctype/field never raises.
  - Batch-safe: each customer is processed inside its own try/except.
  - Never raises; imports cleanly with NO top-level frappe calls.
  - Writes use ``frappe.db.set_value(..., update_modified=False)``.
"""

import frappe

LOGGER_NAME = "crm_reorder_forecast"

# Customer fields we may write (only those that actually exist are written).
_TARGET_FIELDS = (
    "custom_avg_order_cycle_days",
    "custom_last_order_date",
    "custom_predicted_next_order",
    "custom_avg_basket_value",
)


def _logger():
    return frappe.logger(LOGGER_NAME, allow_site=True)


def _doctype_exists(name):
    try:
        return bool(frappe.db.exists("DocType", name))
    except Exception:
        return False


def _has_field(doctype, fieldname):
    try:
        return bool(frappe.get_meta(doctype).get_field(fieldname))
    except Exception:
        return False


def _date_diff(later, earlier):
    try:
        from frappe.utils import date_diff

        return date_diff(later, earlier)
    except Exception:
        return None


def _add_days(date, days):
    try:
        from frappe.utils import add_days

        return add_days(date, days)
    except Exception:
        return None


def _compute_for_customer(customer_name, present_fields):
    """Return a dict of field->value to write for one customer (or None to skip).

    Never raises.
    """
    try:
        invoices = frappe.get_all(
            "Sales Invoice",
            filters={"customer": customer_name, "docstatus": 1},
            fields=["posting_date", "grand_total"],
            order_by="posting_date asc",
            limit_page_length=0,
        )
    except Exception:
        return None

    if not invoices:
        return None

    dates = [r.get("posting_date") for r in invoices if r.get("posting_date")]
    totals = [
        r.get("grand_total")
        for r in invoices
        if r.get("grand_total") not in (None, "")
    ]

    if not dates:
        return None

    updates = {}

    # Average basket value.
    if present_fields.get("custom_avg_basket_value") and totals:
        try:
            updates["custom_avg_basket_value"] = round(
                sum(float(t) for t in totals) / len(totals), 2
            )
        except Exception:
            pass

    # Last order date.
    last_date = dates[-1]
    if present_fields.get("custom_last_order_date"):
        updates["custom_last_order_date"] = last_date

    # Average cycle (days) between consecutive invoices.
    avg_cycle = 0.0
    if len(dates) >= 2:
        gaps = []
        for i in range(1, len(dates)):
            diff = _date_diff(dates[i], dates[i - 1])
            if diff is not None and diff >= 0:
                gaps.append(diff)
        if gaps:
            avg_cycle = round(sum(gaps) / len(gaps), 1)

    if present_fields.get("custom_avg_order_cycle_days"):
        updates["custom_avg_order_cycle_days"] = avg_cycle

    # Predicted next order = last + avg_cycle (only when cycle is known/positive).
    if present_fields.get("custom_predicted_next_order") and avg_cycle > 0:
        predicted = _add_days(last_date, int(round(avg_cycle)))
        if predicted:
            updates["custom_predicted_next_order"] = predicted

    return updates or None


def compute_reorder_forecast():
    """Scheduled daily task: recompute reorder forecast for Company customers.

    Never raises. Returns a summary dict.
    """
    summary = {"processed": 0, "updated": 0, "errors": 0}
    logger = _logger()

    try:
        if not _doctype_exists("Customer") or not _doctype_exists("Sales Invoice"):
            logger.info(
                "compute_reorder_forecast: required DocTypes missing; nothing to do"
            )
            return summary

        present_fields = {f: _has_field("Customer", f) for f in _TARGET_FIELDS}
        if not any(present_fields.values()):
            logger.info(
                "compute_reorder_forecast: no target custom fields present; nothing to do"
            )
            return summary

        # Restrict to Company customers when the field exists; else process all.
        cust_filters = {}
        if _has_field("Customer", "customer_type"):
            cust_filters["customer_type"] = "Company"

        try:
            customers = frappe.get_all(
                "Customer",
                filters=cust_filters or None,
                fields=["name"],
                limit_page_length=0,
            )
        except Exception:
            logger.error(
                "compute_reorder_forecast: failed to query Customers", exc_info=True
            )
            return summary

        for cust in customers:
            name = cust.get("name")
            if not name:
                continue
            summary["processed"] += 1
            try:
                updates = _compute_for_customer(name, present_fields)
                if not updates:
                    continue
                frappe.db.set_value(
                    "Customer", name, updates, update_modified=False
                )
                summary["updated"] += 1
            except Exception:
                summary["errors"] += 1
                logger.error(
                    f"compute_reorder_forecast: failed for Customer '{name}'",
                    exc_info=True,
                )

        try:
            frappe.db.commit()
        except Exception:
            pass

        logger.info(
            "compute_reorder_forecast summary: "
            f"processed={summary['processed']} updated={summary['updated']} "
            f"errors={summary['errors']}"
        )
    except Exception:
        logger.error("compute_reorder_forecast failed unexpectedly", exc_info=True)

    return summary
