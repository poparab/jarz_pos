import frappe


def execute():
    """Ensure Territory delivery income/expense fields exist exactly once.

    Older sites may rely on Custom Fields for these columns. This patch aligns the
    schema by delegating to the shared cleanup helper, which now creates the
    Custom Fields when the core DocType is missing them.
    """
    try:
        from jarz_pos.utils.cleanup import remove_conflicting_territory_delivery_fields

        remove_conflicting_territory_delivery_fields()
        try:
            frappe.clear_cache(doctype="Territory")
        except Exception:
            pass
    except Exception as exc:
        try:
            frappe.log_error(
                message=f"ensure_territory_delivery_fields failed: {exc}",
                title="jarz_pos patches",
            )
        except Exception:
            pass
