import frappe


def publish_new_invoice(doc, method):
    """Publish a realtime event whenever a POS Sales Invoice is created."""
    try:
        # Only push POS invoices (avoid noise from back-office invoices)
        if not getattr(doc, "is_pos", 0):
            return

        payload = {
            "name": doc.name,
            "customer_name": doc.get("customer_name") or doc.customer,
            "total": float(doc.total or 0),
            "grand_total": float(doc.grand_total or 0),
            "status": doc.status,
            "sales_invoice_state": doc.get("sales_invoice_state"),
            "posting_date": str(doc.posting_date),
            "posting_time": str(doc.posting_time),
            "pos_profile": doc.pos_profile or ""
        }

        frappe.publish_realtime("jarz_pos_new_invoice", payload, user="*")  # type: ignore[attr-defined]
    except Exception as e:
        frappe.log_error(f"Realtime publish failed: {e}")  # type: ignore[attr-defined] 