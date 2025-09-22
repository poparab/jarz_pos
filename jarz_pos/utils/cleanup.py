"""Install/migrate-time cleanup utilities.

These functions are called via hooks.before_migrate. They must be safe to run multiple
times and should never raise unhandled exceptions. If a field doesn't exist, they should
quietly skip.
"""
from __future__ import annotations
from typing import Optional

import frappe


def _safe_remove_custom_field(dt: str, fieldname: str) -> None:
    """Remove a Custom Field record if it exists, ignoring errors."""
    try:
        name = frappe.db.get_value("Custom Field", {"dt": dt, "fieldname": fieldname}, "name")
        if name:
            try:
                frappe.delete_doc("Custom Field", name, ignore_permissions=True, force=True)
            except Exception:
                # As fallback, mark as deleted
                try:
                    frappe.db.sql("UPDATE `tabCustom Field` SET docstatus=2 WHERE name=%s", name)
                except Exception:
                    pass
    except Exception:
        pass


def remove_conflicting_territory_delivery_fields() -> None:
    """Remove legacy or conflicting delivery-related fields if present.

    This prevents duplicate fieldname collisions when fixtures are applied.
    """
    # Example legacy names that may conflict; adjust as needed
    legacy_fields = [
        ("Sales Invoice", "required_delivery_datetime"),
        ("Sales Invoice", "delivery_datetime"),
        ("Sales Invoice", "delivery_time"),
        ("Sales Invoice", "delivery_duration"),
        ("Sales Invoice", "state"),
    ]
    for dt, fn in legacy_fields:
        _safe_remove_custom_field(dt, fn)


def ensure_delivery_slot_fields() -> None:
    """Ensure new split delivery fields exist prior to fixtures import.

    This function is defensive: it will create fields if missing with minimal settings,
    so that downstream code relying on them doesn't fail.
    """
    try:
        # Minimal field specs matching fixtures intent
        needed = [
            ("Sales Invoice", "custom_delivery_date", "Date", "posting_time"),
            ("Sales Invoice", "custom_delivery_time_from", "Time", "custom_delivery_date"),
            ("Sales Invoice", "custom_delivery_duration", "Duration", "custom_delivery_time_from"),
            ("Sales Invoice", "custom_sales_invoice_state", "Select", "custom_delivery_duration"),
            ("Sales Invoice", "custom_kanban_profile", "Link", "pos_profile", "POS Profile"),
        ]
        for dt, fieldname, fieldtype, insert_after, *rest in needed:
            exists = frappe.db.exists("Custom Field", {"dt": dt, "fieldname": fieldname})
            if exists:
                continue
            doc = frappe.get_doc({
                "doctype": "Custom Field",
                "dt": dt,
                "fieldname": fieldname,
                "fieldtype": fieldtype,
                "label": fieldname.replace("_", " ").title(),
                "insert_after": insert_after,
                "allow_on_submit": 1 if fieldname.startswith("custom_") else 0,
            })
            if rest:
                doc.options = rest[0]
            try:
                doc.insert(ignore_permissions=True)
            except Exception:
                pass
    except Exception:
        pass


def remove_required_delivery_datetime_field() -> None:
    """Drop the legacy required_delivery_datetime field if present."""
    _safe_remove_custom_field("Sales Invoice", "required_delivery_datetime")
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
