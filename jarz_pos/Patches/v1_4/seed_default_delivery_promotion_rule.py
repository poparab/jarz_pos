import frappe


RULE_NAME = "Free Delivery >= 999 EGP"


def execute():
    if not frappe.db.exists("DocType", "Jarz Promotion Rule"):
        return

    existing_name = frappe.db.get_value("Jarz Promotion Rule", {"rule_name": RULE_NAME}, "name")
    if existing_name:
        doc = frappe.get_doc("Jarz Promotion Rule", existing_name)
    else:
        doc = frappe.new_doc("Jarz Promotion Rule")

    default_currency = frappe.db.get_single_value("Global Defaults", "default_currency") or "EGP"
    doc.rule_name = RULE_NAME
    doc.enabled = 1
    doc.priority = 10
    doc.promotion_scope = "Delivery"
    doc.rule_type = "Free Delivery"
    doc.description = "Waive delivery charges when merchandise subtotal reaches 999 EGP or more."
    doc.currency = default_currency
    doc.threshold_basis = "Merchandise Subtotal"
    doc.minimum_threshold = 999
    doc.maximum_threshold = None
    doc.minimum_item_qty = None
    doc.is_pickup_allowed = 0
    doc.apply_to_shipping_income = 1
    doc.apply_to_legacy_delivery_charges = 1
    doc.set("channels", [])

    if existing_name:
        doc.save(ignore_permissions=True)
    else:
        doc.insert(ignore_permissions=True)

    frappe.clear_cache(doctype="Jarz Promotion Rule")
