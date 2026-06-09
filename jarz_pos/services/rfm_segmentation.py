import frappe
from frappe.utils import today, add_days, getdate


def get_settings():
    return frappe.get_single("Jarz Segmentation Settings")


def classify_customer(recency_days, frequency_90d, avg_order_value,
                      lifetime_orders, settings):
    """Apply RFM rules and return a segment name. First match wins."""
    s = settings

    if recency_days <= s.new_customer_recency_max and lifetime_orders == 1:
        return "New Customer"

    if lifetime_orders == 1 and recency_days > s.new_customer_recency_max:
        return "One-Time"

    if (recency_days <= s.champion_recency_max
            and frequency_90d >= s.champion_frequency_min):
        return "Champion"

    if (recency_days <= s.loyal_recency_max
            and frequency_90d >= s.loyal_frequency_min):
        return "Loyal"

    if recency_days <= s.loyal_recency_max and frequency_90d >= 1:
        return "Potential Loyalist"

    if (recency_days >= s.cant_lose_recency_min
            and lifetime_orders >= s.cant_lose_frequency_min):
        return "Can't Lose Them"

    if recency_days >= s.lost_recency_min:
        return "Lost"

    if s.at_risk_recency_min <= recency_days <= s.at_risk_recency_max:
        return "At Risk"

    return "Loyal"


def run_segmentation(dry_run=False):
    """Main entry point. Called by scheduler nightly."""
    settings = get_settings()
    lookback = int(settings.lookback_days or 90)
    today_date = getdate(today())
    lookback_start = add_days(today_date, -lookback)

    customers = frappe.db.sql("""
        SELECT
            customer,
            MAX(posting_date)   AS last_order_date,
            COUNT(*)            AS lifetime_orders,
            AVG(grand_total)    AS avg_order_value
        FROM `tabSales Invoice`
        WHERE docstatus = 1
          AND is_return = 0
          AND customer IS NOT NULL
        GROUP BY customer
    """, as_dict=True)

    freq_map = {}
    freq_rows = frappe.db.sql("""
        SELECT customer, COUNT(*) AS freq
        FROM `tabSales Invoice`
        WHERE docstatus = 1
          AND is_return = 0
          AND posting_date >= %s
        GROUP BY customer
    """, (lookback_start,), as_dict=True)
    for row in freq_rows:
        freq_map[row.customer] = row.freq

    updated = 0
    skipped_override = 0

    for c in customers:
        override = frappe.db.get_value("Customer", c.customer, "segment_override")
        if override:
            skipped_override += 1
            continue

        recency_days = (today_date - getdate(c.last_order_date)).days
        frequency_90d = freq_map.get(c.customer, 0)
        segment = classify_customer(
            recency_days=recency_days,
            frequency_90d=frequency_90d,
            avg_order_value=float(c.avg_order_value or 0),
            lifetime_orders=int(c.lifetime_orders or 0),
            settings=settings,
        )

        if not dry_run:
            frappe.db.set_value("Customer", c.customer, {
                "customer_segment":    segment,
                "segment_updated_on":  today(),
                "rfm_recency_days":    recency_days,
                "rfm_frequency_count": frequency_90d,
                "rfm_avg_order_value": float(c.avg_order_value or 0),
            }, update_modified=False)
            updated += 1

    if not dry_run:
        frappe.db.commit()

    frappe.logger().info(
        f"[RFM] Segmentation complete: {updated} updated, "
        f"{skipped_override} skipped (manual override)"
    )
    return {
        "updated": updated,
        "skipped_override": skipped_override,
        "total_customers": len(customers),
    }


def get_segment_summary():
    """Returns count of customers per segment for the management page."""
    return frappe.db.sql("""
        SELECT
            COALESCE(customer_segment, 'Unclassified') AS segment,
            COUNT(*) AS count
        FROM `tabCustomer`
        WHERE disabled = 0
        GROUP BY customer_segment
        ORDER BY count DESC
    """, as_dict=True)


def export_segment_csv(segment):
    """Returns list of customers in a given segment for CSV export."""
    return frappe.db.sql("""
        SELECT
            c.name            AS customer_id,
            c.customer_name,
            c.mobile_no,
            c.territory,
            c.customer_segment,
            c.rfm_recency_days,
            c.rfm_frequency_count,
            c.rfm_avg_order_value,
            c.segment_updated_on
        FROM `tabCustomer` c
        WHERE c.customer_segment = %s
          AND c.disabled = 0
        ORDER BY c.rfm_avg_order_value DESC
    """, (segment,), as_dict=True)
