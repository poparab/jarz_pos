import frappe
from frappe import _

from jarz_pos.constants import ROLES


def _ensure_jarz_manager() -> None:
    """JARZ Manager or Administrator — the same gate as the analytics reports.

    Replaces ``frappe.only_for("JARZ Manager")``, which did not admit the
    Administrator role by name and so disagreed with every other report gate.
    """
    roles = set(frappe.get_roles(frappe.session.user))
    if ROLES.JARZ_MANAGER not in roles and ROLES.ADMINISTRATOR not in roles:
        frappe.throw(_("Only JARZ Manager can access stock forecasting"), frappe.PermissionError)


@frappe.whitelist()
def run_velocity_update_now():
    """Manually trigger velocity recalculation. Manager-only."""
    _ensure_jarz_manager()
    from jarz_pos.services.demand_forecasting import run_velocity_update
    count = run_velocity_update()
    return {"updated": count}


@frappe.whitelist()
def get_alert_summary():
    """Get current alert data for dashboard display."""
    _ensure_jarz_manager()
    from jarz_pos.services.demand_forecasting import build_alert_data, get_settings
    return build_alert_data(get_settings())


@frappe.whitelist()
def get_item_velocity(item_code):
    """Get velocity details for a single item."""
    _ensure_jarz_manager()
    from jarz_pos.services.demand_forecasting import calculate_velocity, get_current_stock
    vel = calculate_velocity(item_code)
    stock = get_current_stock(item_code)
    return {**vel, "stock_on_hand": stock}
