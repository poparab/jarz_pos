# ... existing code ...

@frappe.whitelist()
def get_invoice_item_counts(invoice_names):
    """Return a dict of {invoice_name: item_count} for the given invoices.

    The count excludes: 
    1. Bundle parent items (Jarz Bundle.erpnext_item)
    2. Delivery / freight rows (identified by keywords in item_name)
    """
    import json as _json  # Local alias to avoid shadowing

    # Accept both JSON string and list
    if isinstance(invoice_names, str):
        try:
            invoice_names = _json.loads(invoice_names)
        except Exception:
            frappe.throw("Parameter invoice_names must be JSON list or Python list")

    if not isinstance(invoice_names, (list, tuple)):
        frappe.throw("invoice_names must be a list of Sales Invoice names")

    if not invoice_names:
        return {}

    # Fetch bundle parent item codes to exclude
    parent_codes = frappe.get_all("Jarz Bundle", pluck="erpnext_item") or []

    # Initialise counts dict
    counts = {name: 0 for name in invoice_names}

    # Fetch all items for the invoices in a single query
    items = frappe.get_all(
        "Sales Invoice Item",
        filters={"parent": ["in", invoice_names]},
        fields=["parent", "item_code", "item_name", "qty"],
        limit=None,
    )

    for it in items:
        # Skip bundle parent item rows
        if it.item_code in parent_codes:
            continue

        # Skip obvious delivery / freight rows based on name heuristics
        itm_name = (it.item_name or "").lower()
        if "delivery" in itm_name or "freight" in itm_name:
            continue

        try:
            qty = float(it.qty or 0)
        except Exception:
            qty = 0
        counts[it.parent] += qty

    return counts

# ... existing code ...