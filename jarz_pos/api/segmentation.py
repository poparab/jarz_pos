import frappe
from frappe import _


@frappe.whitelist()
def get_segment_summary():
    """Returns count per segment for management dashboard."""
    frappe.only_for("JARZ Manager")
    from jarz_pos.services.rfm_segmentation import get_segment_summary as _get
    return _get()


@frappe.whitelist()
def run_segmentation_now():
    """Manually trigger RFM recalculation. Manager-only."""
    frappe.only_for("JARZ Manager")
    from jarz_pos.services.rfm_segmentation import run_segmentation
    return run_segmentation()


@frappe.whitelist()
def export_segment(segment):
    """Export a segment to a list for CSV download."""
    frappe.only_for("JARZ Manager")
    from jarz_pos.services.rfm_segmentation import export_segment_csv
    return export_segment_csv(segment)


@frappe.whitelist()
def set_segment_override(customer, override, manual_segment=None):
    """Pin or unpin a customer's segment from automatic recalculation."""
    frappe.only_for("JARZ Manager")
    values = {"segment_override": int(override)}
    if override and manual_segment:
        values["customer_segment"] = manual_segment
    frappe.db.set_value("Customer", customer, values)
    frappe.db.commit()
    return {"status": "ok", "customer": customer}
