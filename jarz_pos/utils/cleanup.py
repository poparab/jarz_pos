"""Install/migrate-time cleanup utilities for Jarz POS.

All functions are designed to be import-safe and idempotent so migrations never break.
"""
from __future__ import annotations

from typing import Iterable, Optional
import json
import os

try:
    import frappe
except Exception:  # pragma: no cover - during static analysis or docs build
    frappe = None  # type: ignore


def _log(msg: str, title: str = "Jarz POS – Install Cleanup") -> None:
    try:
        if frappe and getattr(frappe, "log_error", None):
            frappe.log_error(msg, title)
    except Exception:
        # Never fail on logging
        pass


def _safe_remove_custom_field(dt: str, fieldname: str) -> bool:
    """Remove a Custom Field if it exists; return True if removed.

    - No exceptions escape this function.
    """
    try:
        if not frappe:
            return False
        exists = frappe.db.exists("Custom Field", {"dt": dt, "fieldname": fieldname})
        if not exists:
            return False
        doc = frappe.get_doc("Custom Field", exists)
        doc.delete(ignore_permissions=True)
        _log(f"Removed Custom Field {dt}.{fieldname}")
        return True
    except Exception as e:
        _log(f"Failed to remove Custom Field {dt}.{fieldname}: {e}")
        return False


def _ensure_custom_field(
    dt: str,
    fieldname: str,
    label: str,
    fieldtype: str,
    insert_after: Optional[str] = None,
    options: Optional[str] = None,
    default: Optional[str] = None,
    reqd: int = 0,
    hidden: int = 0,
) -> bool:
    """Create a Custom Field if missing; return True if created."""
    try:
        if not frappe:
            return False
        if frappe.db.exists("Custom Field", {"dt": dt, "fieldname": fieldname}):
            return False
        doc = frappe.get_doc({
            "doctype": "Custom Field",
            "dt": dt,
            "label": label,
            "fieldname": fieldname,
            "fieldtype": fieldtype,
            "insert_after": insert_after,
            "options": options,
            "default": default,
            "reqd": reqd,
            "hidden": hidden,
        })
        doc.insert(ignore_permissions=True)
        _log(f"Created Custom Field {dt}.{fieldname}")
        return True
    except Exception as e:
        _log(f"Failed to ensure Custom Field {dt}.{fieldname}: {e}")
        return False


# Public API used from hooks.before_migrate

def remove_conflicting_territory_delivery_fields() -> None:
    """Remove legacy/duplicate fields that could conflict with fixtures.

    - Sales Invoice: legacy delivery fields, stray state/duration
    - Territory: delivery_income, delivery_expense
    Safe no-op when fields are absent.
    """
    try:
        if not frappe:
            return
        # Sales Invoice legacy fields
        for fname in [
            "required_delivery_datetime",
            "delivery_datetime",
            "delivery_time",
            "delivery_duration",
            "state",
        ]:
            _safe_remove_custom_field("Sales Invoice", fname)

        # Territory conflicting fields (if any prior experiments created them)
        q = frappe.get_all(
            "Custom Field",
            filters={"dt": "Territory", "fieldname": ["in", ["delivery_income", "delivery_expense"]]},
            pluck="name",
        )
        for name in q:
            try:
                frappe.delete_doc("Custom Field", name, ignore_permissions=True)
            except Exception:
                pass
    except Exception as e:
        _log(f"remove_conflicting_territory_delivery_fields error: {e}")


def ensure_delivery_slot_fields() -> None:
    """Ensure the split delivery slot fields exist on Sales Invoice.

    Fields:
    - custom_delivery_date (Date)
    - custom_delivery_time_from (Time)
    - custom_delivery_duration (Int, seconds)
    - custom_delivery_slot_label (Data, hidden)
    """
    try:
        if not frappe:
            return
        # Place after posting_time if present, otherwise posting_date
        insert_after = "posting_date"
        try:
            meta = frappe.get_meta("Sales Invoice")
            insert_after = "posting_time" if meta.get_field("posting_time") else "posting_date"
        except Exception:
            pass

        _ensure_custom_field(
            dt="Sales Invoice",
            fieldname="custom_delivery_date",
            label="Delivery Date",
            fieldtype="Date",
            insert_after=insert_after,
        )
        _ensure_custom_field(
            dt="Sales Invoice",
            fieldname="custom_delivery_time_from",
            label="Delivery Start Time",
            fieldtype="Time",
            insert_after="custom_delivery_date",
        )
        _ensure_custom_field(
            dt="Sales Invoice",
            fieldname="custom_delivery_duration",
            label="Delivery Duration (seconds)",
            fieldtype="Int",
            insert_after="custom_delivery_time_from",
            default="3600",
        )
        _ensure_custom_field(
            dt="Sales Invoice",
            fieldname="custom_delivery_slot_label",
            label="Delivery Slot Label",
            fieldtype="Data",
            insert_after="custom_delivery_duration",
            hidden=1,
        )
    except Exception as e:
        _log(f"ensure_delivery_slot_fields error: {e}")


def remove_required_delivery_datetime_field() -> None:
    """Remove legacy single datetime field if still present (safe no-op)."""
    try:
        _safe_remove_custom_field("Sales Invoice", "required_delivery_datetime")
    except Exception as e:
        _log(f"remove_required_delivery_datetime_field error: {e}")


def remove_colliding_custom_fields_for_fixtures() -> None:
    """Ensure fixture Custom Fields can be inserted by removing conflicting existing ones.

    For each Custom Field in our fixtures (dt+fieldname), if a Custom Field already exists
    with the SAME (dt, fieldname) but a DIFFERENT name, delete the existing one so the
    fixture import can proceed without DuplicateEntry.
    """
    try:
        if not frappe:
            return
        # Locate fixtures/custom_field.json within this app
        app_path = None
        try:
            app_path = frappe.get_app_path("jarz_pos")
        except Exception:
            # Fallback: try relative from this file
            app_path = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
        fx_path = os.path.join(app_path, "fixtures", "custom_field.json")
        if not os.path.exists(fx_path):
            return
        with open(fx_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            return
        for doc in data:
            try:
                dt = doc.get("dt")
                fieldname = doc.get("fieldname")
                fx_name = doc.get("name")
                if not dt or not fieldname or not fx_name:
                    continue
                existing = frappe.db.get_value("Custom Field", {"dt": dt, "fieldname": fieldname}, "name")
                if existing and existing != fx_name:
                    try:
                        frappe.delete_doc("Custom Field", existing, ignore_permissions=True)
                        _log(f"Removed colliding Custom Field {dt}.{fieldname} (existing: {existing}) to allow fixture {fx_name}")
                    except Exception as de:
                        _log(f"Failed removing colliding Custom Field {dt}.{fieldname}: {de}")
            except Exception as inner:
                _log(f"Fixture collision scan error: {inner}")
    except Exception as e:
        _log(f"remove_colliding_custom_fields_for_fixtures error: {e}")
