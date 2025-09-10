import frappe


def _ensure_custom_field(dt: str, fieldname: str, label: str, fieldtype: str, **kwargs):
    exists = frappe.db.exists("Custom Field", {"dt": dt, "fieldname": fieldname})
    if exists:
        return exists
    df = frappe.new_doc("Custom Field")
    df.dt = dt
    df.fieldname = fieldname
    df.label = label
    df.fieldtype = fieldtype
    # Reasonable defaults
    if "insert_after" in kwargs:
        df.insert_after = kwargs.get("insert_after")
    if "reqd" in kwargs:
        df.reqd = kwargs.get("reqd")
    if "read_only" in kwargs:
        df.read_only = kwargs.get("read_only")
    if "depends_on" in kwargs:
        df.depends_on = kwargs.get("depends_on")
    if "default" in kwargs:
        df.default = kwargs.get("default")
    df.save(ignore_permissions=True)
    frappe.db.commit()
    return df.name


def ensure_delivery_slot_fields():
    """Create delivery slot fields on Sales Invoice if missing.

        Fields:
            - custom_delivery_date (Date)
            - custom_delivery_time_from (Time)
            - custom_delivery_duration (Duration)
    """
    # Place after posting_time if exists; else after posting_date
    insert_after = None
    try:
        meta = frappe.get_meta("Sales Invoice")
        insert_after = "posting_time" if meta.get_field("posting_time") else "posting_date"
    except Exception:
        insert_after = "posting_date"

    _ensure_custom_field(
        "Sales Invoice",
        "custom_delivery_date",
        "Delivery Date",
        "Date",
        insert_after=insert_after,
    )
    _ensure_custom_field(
        "Sales Invoice",
        "custom_delivery_time_from",
        "Delivery Time From",
        "Time",
        insert_after="custom_delivery_date",
    )
    _ensure_custom_field(
        "Sales Invoice",
        "custom_delivery_duration",
        "Delivery Duration",
        "Duration",
        insert_after="custom_delivery_time_from",
    )


def remove_required_delivery_datetime_field():
    """Remove legacy required_delivery_datetime field from Sales Invoice if exists."""
    try:
        cf = frappe.db.get_value(
            "Custom Field",
            {"dt": "Sales Invoice", "fieldname": "required_delivery_datetime"},
            ["name"],
        )
        if cf:
            frappe.delete_doc("Custom Field", cf, ignore_permissions=True)
            frappe.db.commit()
    except Exception:
        # Safe to ignore; we don't want migration to fail due to absence
        pass


def remove_conflicting_territory_delivery_fields():
    # Kept for backward compatibility (referenced in hooks.before_migrate)
    pass
import frappe


def remove_conflicting_territory_delivery_fields():
    """
    Remove existing Custom Fields in Territory that conflict with fixtures:
    - delivery_income
    - delivery_expense
    Returns a dict with counts of deleted docs.
    """

    to_delete = frappe.get_all(
        "Custom Field",
        filters={
            "dt": "Territory",
            "fieldname": ["in", ["delivery_income", "delivery_expense"]],
        },
        pluck="name",
    )

    deleted = []
    for name in to_delete:
        try:
            frappe.delete_doc("Custom Field", name, force=1, ignore_permissions=True)
            deleted.append(name)
        except Exception:
            frappe.db.rollback()

    if deleted:
        frappe.db.commit()
    return {"deleted": deleted, "count": len(deleted)}


def before_migrate():
    """Hook: called via hooks.before_migrate to ensure fixtures import succeeds."""
    try:
        remove_conflicting_territory_delivery_fields()
    except Exception:
        # Best-effort cleanup; ignore errors so migrate proceeds
        pass
