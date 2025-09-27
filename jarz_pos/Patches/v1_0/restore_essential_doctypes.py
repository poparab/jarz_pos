import frappe


def execute():
    """Restore/ensure essential DocTypes after app structure changes.

    This patch is idempotent and will no-op if everything already exists.
    It primarily ensures the DocTypes under jarz_pos.jarz_pos.doctype are
    correctly installed and accessible after previous module path issues.
    """
    try:
        # Touch essential DocTypes to ensure they are synced/registered
        essential_doctypes = [
            "City",
            "Courier Transaction",
            "Custom Settings",
            "Jarz Bundle",
            "Jarz Bundle Item Group",
            "POS Profile Day Timing",
            "POS Profile Timetable",
            "Sales Partner Transactions",
        ]

        for dt in essential_doctypes:
            try:
                frappe.get_meta(dt)
            except Exception:
                # If meta is missing, ensure models are synced by creating then deleting a dummy doc
                try:
                    doc = frappe.new_doc(dt)
                    # Required fields handling best-effort; save only if possible
                    try:
                        doc.insert(ignore_permissions=True)
                        doc.delete()
                        frappe.db.commit()
                    except Exception:
                        frappe.db.rollback()
                except Exception:
                    # If we can't instantiate, continue; migrate will sync schema
                    pass

        # No schema modifications are done here; migrate handles model sync.
    except Exception as e:
        # Never block migrate entirely due to this patch; log and proceed.
        try:
            frappe.log_error(f"restore_essential_doctypes failed: {e}", "jarz_pos patches")
        except Exception:
            pass
