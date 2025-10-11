import frappe


def execute():
    """Delegate to cleanup utility to remove conflicting fields.

    Kept as a patch wrapper so patches.txt can import
    `jarz_pos.patches.v0_0_4.remove_conflicting_territory_delivery_fields`.
    """
    try:
        from jarz_pos.utils.cleanup import remove_conflicting_territory_delivery_fields

        remove_conflicting_territory_delivery_fields()
    except Exception as e:
        try:
            frappe.log_error(f"v0_0_4 remove_conflicting_territory_delivery_fields failed: {e}", "jarz_pos patches")
        except Exception:
            pass

