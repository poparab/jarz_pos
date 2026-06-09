import frappe
from frappe.utils import today, add_days, getdate, now_datetime
from datetime import date


def get_settings():
    return frappe.get_single("Jarz Forecast Settings")


def get_active_seasonal_multiplier(settings, for_date=None):
    """
    Check if today falls within a seasonal period.
    Returns the multiplier (default 1.0 if no active season).
    """
    check_date = getdate(for_date or today())
    for season in (settings.seasonal_multipliers or []):
        if getdate(season.date_from) <= check_date <= getdate(season.date_to):
            return float(season.multiplier or 1.0), season.season_name
    return 1.0, None


def calculate_velocity(item_code, warehouse=None):
    """
    Calculate 30-day and 60-day sales velocity for a single item.
    Reads from submitted, non-return Sales Invoice Items.
    Returns dict with velocity_30d, velocity_60d, trend.
    """
    today_date = getdate(today())
    day30 = add_days(today_date, -30)
    day60 = add_days(today_date, -60)

    row = frappe.db.sql("""
        SELECT
            SUM(CASE WHEN si.posting_date >= %(day30)s THEN sii.qty ELSE 0 END) AS qty_30d,
            SUM(CASE WHEN si.posting_date >= %(day60)s THEN sii.qty ELSE 0 END) AS qty_60d
        FROM `tabSales Invoice Item` sii
        JOIN `tabSales Invoice` si ON si.name = sii.parent
        WHERE si.docstatus = 1
          AND si.is_return = 0
          AND sii.item_code = %(item_code)s
          AND si.posting_date >= %(day60)s
    """, {
        "item_code": item_code,
        "day30": day30,
        "day60": day60,
    }, as_dict=True)

    if not row or not row[0]:
        return {"velocity_30d": 0.0, "velocity_60d": 0.0, "trend": "No Sales"}

    qty_30d = float(row[0].qty_30d or 0)
    qty_60d = float(row[0].qty_60d or 0)

    v30 = round(qty_30d / 30.0, 3)
    v60 = round(qty_60d / 60.0, 3)

    if v60 == 0 and v30 == 0:
        trend = "No Sales"
    elif v60 == 0:
        trend = "New Item"
    elif v30 > v60 * 1.15:
        trend = "Accelerating"
    elif v30 < v60 * 0.85:
        trend = "Declining"
    else:
        trend = "Stable"

    return {"velocity_30d": v30, "velocity_60d": v60, "trend": trend}


def get_current_stock(item_code):
    """Returns actual_qty summed across all warehouses from tabBin."""
    result = frappe.db.sql("""
        SELECT COALESCE(SUM(actual_qty), 0) AS stock
        FROM `tabBin`
        WHERE item_code = %s
    """, (item_code,), as_dict=True)
    return float(result[0].stock) if result else 0.0


def run_velocity_update():
    """
    Weekly job: recalculate velocity for all active stock items.
    Updates jarz_velocity_30d, jarz_velocity_60d, jarz_velocity_trend,
    jarz_days_of_stock on each Item record.
    """
    settings = get_settings()
    seasonal_multiplier, season_name = get_active_seasonal_multiplier(settings)

    items = frappe.db.sql("""
        SELECT name, item_name, default_material_request_type
        FROM `tabItem`
        WHERE is_stock_item = 1
          AND disabled = 0
    """, as_dict=True)

    updated = 0
    for item in items:
        vel = calculate_velocity(item.name)
        stock = get_current_stock(item.name)

        effective_velocity = vel["velocity_60d"] * seasonal_multiplier
        days_of_stock = (
            round(stock / effective_velocity)
            if effective_velocity > 0 else 999
        )

        frappe.db.set_value("Item", item.name, {
            "jarz_velocity_30d":      vel["velocity_30d"],
            "jarz_velocity_60d":      vel["velocity_60d"],
            "jarz_velocity_trend":    vel["trend"],
            "jarz_days_of_stock":     days_of_stock,
            "jarz_velocity_updated_on": now_datetime(),
        }, update_modified=False)
        updated += 1

    frappe.db.commit()
    frappe.logger().info(
        f"[Forecast] Velocity update complete: {updated} items. "
        f"Active season: {season_name or 'None'} (multiplier: {seasonal_multiplier})"
    )
    return updated


def build_alert_data(settings):
    """
    Compile alert lists from Item velocity data + current stock.
    Returns dict with four lists: critical, watch_list, slow_movers, overstocked.
    NO documents are created. Data is for alerting only.
    """
    critical_days    = int(settings.critical_days_threshold or 5)
    watch_days       = int(settings.watch_days_threshold or 14)
    overstock_days   = int(settings.overstock_days_threshold or 90)
    slow_mover_days  = int(settings.slow_mover_days or 30)

    critical = frappe.db.sql("""
        SELECT
            i.name                        AS item_code,
            i.item_name,
            i.item_group,
            i.default_material_request_type AS replenishment_type,
            i.jarz_velocity_60d           AS daily_velocity,
            COALESCE(b.stock, 0)          AS stock_on_hand,
            i.jarz_days_of_stock          AS days_remaining
        FROM `tabItem` i
        LEFT JOIN (
            SELECT item_code, SUM(actual_qty) AS stock
            FROM `tabBin` GROUP BY item_code
        ) b ON b.item_code = i.name
        WHERE i.is_stock_item = 1
          AND i.disabled = 0
          AND i.jarz_velocity_60d > 0
          AND i.jarz_days_of_stock <= %(critical_days)s
        ORDER BY i.jarz_days_of_stock ASC
    """, {"critical_days": critical_days}, as_dict=True)

    watch_list = frappe.db.sql("""
        SELECT
            i.name                        AS item_code,
            i.item_name,
            i.item_group,
            i.default_material_request_type AS replenishment_type,
            i.jarz_velocity_60d           AS daily_velocity,
            COALESCE(b.stock, 0)          AS stock_on_hand,
            i.jarz_days_of_stock          AS days_remaining
        FROM `tabItem` i
        LEFT JOIN (
            SELECT item_code, SUM(actual_qty) AS stock
            FROM `tabBin` GROUP BY item_code
        ) b ON b.item_code = i.name
        WHERE i.is_stock_item = 1
          AND i.disabled = 0
          AND i.jarz_velocity_60d > 0
          AND i.jarz_days_of_stock > %(critical_days)s
          AND i.jarz_days_of_stock <= %(watch_days)s
        ORDER BY i.jarz_days_of_stock ASC
    """, {"critical_days": critical_days, "watch_days": watch_days}, as_dict=True)

    slow_mover_cutoff = add_days(today(), -slow_mover_days)
    slow_movers = frappe.db.sql("""
        SELECT
            i.name        AS item_code,
            i.item_name,
            i.item_group,
            i.default_material_request_type AS replenishment_type,
            COALESCE(b.stock, 0) AS stock_on_hand,
            i.jarz_velocity_trend AS trend
        FROM `tabItem` i
        LEFT JOIN (
            SELECT item_code, SUM(actual_qty) AS stock
            FROM `tabBin` GROUP BY item_code
        ) b ON b.item_code = i.name
        LEFT JOIN (
            SELECT sii.item_code, MAX(si.posting_date) AS last_sale
            FROM `tabSales Invoice Item` sii
            JOIN `tabSales Invoice` si ON si.name = sii.parent
            WHERE si.docstatus = 1 AND si.is_return = 0
            GROUP BY sii.item_code
        ) sales ON sales.item_code = i.name
        WHERE i.is_stock_item = 1
          AND i.disabled = 0
          AND COALESCE(b.stock, 0) > 0
          AND (sales.last_sale IS NULL OR sales.last_sale < %(cutoff)s)
        ORDER BY COALESCE(b.stock, 0) * i.standard_rate DESC
    """, {"cutoff": slow_mover_cutoff}, as_dict=True)

    overstocked = frappe.db.sql("""
        SELECT
            i.name        AS item_code,
            i.item_name,
            i.item_group,
            i.default_material_request_type AS replenishment_type,
            i.jarz_velocity_60d AS daily_velocity,
            COALESCE(b.stock, 0) AS stock_on_hand,
            i.jarz_days_of_stock AS days_remaining,
            ROUND(COALESCE(b.stock, 0) * i.valuation_rate, 0) AS stock_value
        FROM `tabItem` i
        LEFT JOIN (
            SELECT item_code, SUM(actual_qty) AS stock
            FROM `tabBin` GROUP BY item_code
        ) b ON b.item_code = i.name
        WHERE i.is_stock_item = 1
          AND i.disabled = 0
          AND i.jarz_velocity_60d > 0
          AND i.jarz_days_of_stock > %(overstock_days)s
        ORDER BY stock_value DESC
        LIMIT 20
    """, {"overstock_days": overstock_days}, as_dict=True)

    return {
        "critical": critical,
        "watch_list": watch_list,
        "slow_movers": slow_movers,
        "overstocked": overstocked,
    }


def send_daily_digest():
    """
    Daily job: send inventory alert email to configured recipients.
    Reads velocity data already stored on Item records (updated weekly).
    """
    settings = get_settings()
    recipients = [r.strip() for r in (settings.digest_recipients or "").split(",") if r.strip()]

    if not recipients:
        frappe.logger().warning("[Forecast] No recipients configured — skipping digest")
        return

    data = build_alert_data(settings)

    total_alerts = (
        len(data["critical"])
        + len(data["watch_list"])
        + len(data["slow_movers"])
        + len(data["overstocked"])
    )

    if total_alerts == 0:
        frappe.logger().info("[Forecast] No alerts today — digest skipped")
        return

    subject = f"Jarz Inventory Alert — {today()} ({len(data['critical'])} Critical)"

    frappe.sendmail(
        recipients=recipients,
        subject=subject,
        template="inventory_digest",
        args={
            "critical":    data["critical"],
            "watch_list":  data["watch_list"],
            "slow_movers": data["slow_movers"],
            "overstocked": data["overstocked"],
            "today":       today(),
        },
        delayed=False,
    )
    frappe.logger().info(
        f"[Forecast] Digest sent to {recipients}. "
        f"Critical: {len(data['critical'])}, Watch: {len(data['watch_list'])}, "
        f"Slow: {len(data['slow_movers'])}, Overstock: {len(data['overstocked'])}"
    )
