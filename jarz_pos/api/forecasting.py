import frappe


@frappe.whitelist()
def run_velocity_update_now():
    """Manually trigger velocity recalculation. Manager-only."""
    frappe.only_for("JARZ Manager")
    from jarz_pos.services.demand_forecasting import run_velocity_update
    count = run_velocity_update()
    return {"updated": count}


@frappe.whitelist()
def get_alert_summary():
    """Get current alert data for dashboard display."""
    frappe.only_for("JARZ Manager")
    from jarz_pos.services.demand_forecasting import build_alert_data, get_settings
    return build_alert_data(get_settings())


@frappe.whitelist()
def get_item_velocity(item_code):
    """Get velocity details for a single item."""
    frappe.only_for("JARZ Manager")
    from jarz_pos.services.demand_forecasting import calculate_velocity, get_current_stock
    vel = calculate_velocity(item_code)
    stock = get_current_stock(item_code)
    return {**vel, "stock_on_hand": stock}
