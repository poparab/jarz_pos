import frappe


def execute():
    """Backfill default receipt config in Jarz POS Settings when values are blank.

    This patch is idempotent and only writes missing values.
    """
    defaults = {
        "receipt_header_text": "ORDER RECEIPT",
        "receipt_footer_text": "Thank you for Your Order",
        "receipt_phone": "01061332266",
        "receipt_website": "www.orderjarz.com",
    }

    # Ensure singleton exists
    if not frappe.db.exists("DocType", "Jarz POS Settings"):
        return

    changed = False
    for fieldname, value in defaults.items():
        current = (frappe.db.get_single_value("Jarz POS Settings", fieldname) or "").strip()
        if not current:
            frappe.db.set_single_value("Jarz POS Settings", fieldname, value)
            changed = True

    if changed:
        frappe.clear_cache(doctype="Jarz POS Settings")
