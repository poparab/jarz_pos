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
    **overrides,
) -> bool:
    """Create a Custom Field if missing; return True if created.

    Additional Custom Field attributes can be passed via ``overrides`` and will
    be applied to the new document before insertion.
    """
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
        for key, value in overrides.items():
            try:
                doc.set(key, value)
            except Exception:
                setattr(doc, key, value)
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

        # Territory delivery fields must exist exactly once; keep the canonical
        # standard fields when available, otherwise fall back to Custom Fields.
        try:
            territory_meta = frappe.get_meta("Territory")
        except Exception:
            territory_meta = None

        def _sync(fieldname: str, label: str, insert_after: str) -> None:
            if not frappe:
                return
            field_meta = None
            if territory_meta:
                try:
                    field_meta = territory_meta.get_field(fieldname)
                except Exception:
                    field_meta = None
            is_standard = bool(field_meta and not field_meta.get("is_custom_field"))
            custom_exists = bool(
                frappe.db.exists("Custom Field", {"dt": "Territory", "fieldname": fieldname})
            )

            if is_standard:
                # Ensure no duplicate Custom Field lingers when the core field exists
                if custom_exists:
                    _safe_remove_custom_field("Territory", fieldname)
            else:
                # Standard field absent; make sure a Custom Field is present
                if not custom_exists:
                    _ensure_custom_field(
                        dt="Territory",
                        fieldname=fieldname,
                        label=label,
                        fieldtype="Currency",
                        insert_after=insert_after,
                        allow_in_quick_entry=1,
                        non_negative=1,
                    )

        _sync("delivery_income", "Delivery Income", "is_group")
        _sync("delivery_expense", "Delivery Expense", "delivery_income")

        # Remove legacy prefixed duplicates that should no longer exist.
        for stale in ["custom_delivery_income", "custom_delivery_expense"]:
            _safe_remove_custom_field("Territory", stale)
    except Exception as e:
        _log(f"remove_conflicting_territory_delivery_fields error: {e}")


def ensure_territory_delivery_fields() -> None:
    """Compat shim kept for older hooks/patches referencing this helper.

    Delegates to :func:`remove_conflicting_territory_delivery_fields`, which now
    ensures the Territory fields exist exactly once.
    """

    remove_conflicting_territory_delivery_fields()


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


def ensure_courier_delivery_fields() -> None:
    """Ensure the Sales Invoice delivery-outcome fields exist (COURIER_CONTRACTS §2).

    Belt and braces with ``fixtures/custom_field.json``: the fixture is the
    canonical definition, but fixtures are synced at the *end* of ``bench
    migrate`` while code deployed in the same release may already be running.
    Creating the fields here (``before_migrate``) closes that window.

    Every field carries ``allow_on_submit`` and ``no_copy``:

    * without ``allow_on_submit`` Frappe rejects the write on a submitted
      invoice, ``update_submitted_sales_invoice_fields`` filters the field out
      and returns ``False`` **with no error** — a courier's "Delivered" tap
      would vanish silently; and
    * without ``no_copy`` a delivery outcome would carry over onto an amended
      invoice.

    Create-only: an existing field is never rewritten, so a hand edit in Desk
    survives. Anchored on ``custom_courier_party`` so the block stays contiguous.
    """
    try:
        if not frappe:
            return

        _ensure_custom_field(
            dt="Sales Invoice",
            fieldname="custom_delivery_sequence",
            label="Delivery Sequence",
            fieldtype="Int",
            insert_after="custom_courier_party",
            description="Stop order within the courier's run. 0 means unsequenced.",
            allow_on_submit=1,
            no_copy=1,
            non_negative=1,
        )
        _ensure_custom_field(
            dt="Sales Invoice",
            fieldname="custom_arrived_at",
            label="Arrived At",
            fieldtype="Datetime",
            insert_after="custom_delivery_sequence",
            description="When the courier reported arriving at the customer's door.",
            allow_on_submit=1,
            no_copy=1,
        )
        _ensure_custom_field(
            dt="Sales Invoice",
            fieldname="custom_delivered_at",
            label="Delivered At",
            fieldtype="Datetime",
            insert_after="custom_arrived_at",
            description=(
                "When the courier reported the order handed over. Doubles as the "
                "durable idempotency marker for the Delivered transition."
            ),
            allow_on_submit=1,
            no_copy=1,
        )
        _ensure_custom_field(
            dt="Sales Invoice",
            fieldname="custom_delivery_latitude",
            label="Delivery Latitude",
            fieldtype="Float",
            insert_after="custom_delivered_at",
            description="Latitude the courier's device reported at proof of delivery.",
            allow_on_submit=1,
            no_copy=1,
            # Deliberately NOT non_negative: the southern hemisphere exists.
            non_negative=0,
            precision="6",
        )
        _ensure_custom_field(
            dt="Sales Invoice",
            fieldname="custom_delivery_longitude",
            label="Delivery Longitude",
            fieldtype="Float",
            insert_after="custom_delivery_latitude",
            description="Longitude the courier's device reported at proof of delivery.",
            allow_on_submit=1,
            no_copy=1,
            non_negative=0,
            precision="6",
        )
        _ensure_custom_field(
            dt="Sales Invoice",
            fieldname="custom_delivery_accuracy_m",
            label="Delivery Accuracy (m)",
            fieldtype="Float",
            insert_after="custom_delivery_longitude",
            description="GPS accuracy radius in metres reported with the delivery coordinates.",
            allow_on_submit=1,
            no_copy=1,
            non_negative=1,
            precision="2",
        )
        _ensure_custom_field(
            dt="Sales Invoice",
            fieldname="custom_delivery_attempt_no",
            label="Delivery Attempt No",
            fieldtype="Int",
            insert_after="custom_delivery_accuracy_m",
            default="0",
            description=(
                "Incremented on every failed delivery attempt. A failure never "
                "changes the board state."
            ),
            allow_on_submit=1,
            no_copy=1,
            non_negative=1,
        )
        # Small Text, NOT Link — deliberately, and it cannot be changed back.
        #
        # `tabSales Invoice` carries 247 columns and sits at MariaDB's hard
        # 65,535-byte row limit. A Link is varchar(140), which in utf8mb4 is
        # ~560 bytes inline, and the ALTER is rejected outright:
        #
        #   (1118) Row size too large ... You have to change some columns to
        #   TEXT or BLOBs
        #
        # Every other field in this block is int/datetime/decimal — small and
        # fixed-width — so they migrate cleanly. This was the only varchar, and
        # the only one that failed. Small Text maps to a TEXT column, which
        # stores an ~20-byte pointer inline and leaves the row budget alone.
        #
        # The referential integrity a Link would give is not lost: the value is
        # a Delivery Failure Reason document name and
        # services.courier_delivery._resolve_failure_reason resolves it against
        # that DocType and rejects anything missing or inactive. What is lost is
        # Desk click-through, which is a fair trade for a field that cannot
        # otherwise exist at all.
        _ensure_custom_field(
            dt="Sales Invoice",
            fieldname="custom_delivery_failure_reason",
            label="Delivery Failure Reason",
            fieldtype="Small Text",
            insert_after="custom_delivery_attempt_no",
            description="Why the last delivery attempt failed. Cleared on successful delivery.",
            allow_on_submit=1,
            no_copy=1,
            in_standard_filter=1,
        )
    except Exception as e:
        _log(f"ensure_courier_delivery_fields error: {e}")


def ensure_tracking_fields() -> None:
    """Ensure the customer-tracking token exists on Sales Invoice.

    A **separate** seeder from :func:`ensure_courier_delivery_fields` on purpose.
    COURIER_CONTRACTS §2 freezes that block at exactly eight fields and
    ``tests/test_courier_custom_fields`` asserts the set, so bundling a ninth
    field of a different feature in there would either break that guard or force
    it to be loosened — and loosening a frozen-contract test to fit new work is
    how a contract stops meaning anything.

    Anchored after ``custom_delivery_failure_reason`` so the delivery block stays
    contiguous and this sits immediately behind it.
    """
    try:
        if not frappe:
            return

        # Small Text for the SAME reason as custom_delivery_failure_reason, and
        # equally non-negotiable: `tabSales Invoice` carries 247 columns and is at
        # MariaDB's 65,535-byte row limit, so no app can add a varchar to this
        # table. A Data field here is rejected outright with error 1118.
        #
        # allow_on_submit: minted at the Out for Delivery transition, i.e. always
        # on a submitted document. Without it the write is filtered out and
        # reported as success.
        #
        # no_copy: an amended invoice must mint its own token. Copying it would
        # give two different orders the same public URL and the resolver would
        # return whichever row it found first — a cross-customer leak created by a
        # single field flag.
        #
        # read_only + print_hide: this is a URL credential, not data. Typing over
        # it in Desk silently breaks a link the customer already has, and it has
        # no business on a printed invoice.
        _ensure_custom_field(
            dt="Sales Invoice",
            fieldname="custom_tracking_token",
            label="Customer Tracking Token",
            fieldtype="Small Text",
            insert_after="custom_delivery_failure_reason",
            description=(
                "Opaque token addressing this order's public /track page. Minted "
                "once, at the first Out for Delivery transition, by "
                "jarz_pos.services.tracking. Never derived from the invoice name."
            ),
            allow_on_submit=1,
            no_copy=1,
            read_only=1,
            print_hide=1,
        )
    except Exception as e:
        _log(f"ensure_tracking_fields error: {e}")


def ensure_address_geo_fields() -> None:
    """Ensure the Address geo fields exist (COURIER_CONTRACTS §3).

    Mirrors ``fixtures/custom_field.json`` for the same reason as
    :func:`ensure_courier_delivery_fields`.

    ``jarz_pos.services.geo_resolution`` is the sole writer of all six.
    ``custom_geo_confidence`` and ``custom_geo_verified_on`` are read-only in the
    Desk form: the rank is derived from the source and must never be typed in by
    hand, or the never-downgrade ladder can be defeated with a form edit.

    None of these fields is in WooCommerce's Address outbound trigger set (which
    lists no ``custom_*`` field at all), so writing them never fans out a sync.
    That property is what makes the geo lane safe — and it evaporates the moment
    anybody bundles an ``address_line1/2`` edit into the same save.
    """
    try:
        if not frappe:
            return

        # ``gps_location`` is a core Frappe field on Address (last in field_order).
        # Resolve defensively anyway: a site missing it would otherwise get an
        # unanchored field, and ``ensure_delivery_slot_fields`` sets the precedent.
        insert_after = "gps_location"
        try:
            meta = frappe.get_meta("Address")
            if not meta.get_field("gps_location"):
                insert_after = "city" if meta.get_field("city") else None
        except Exception:
            pass

        _ensure_custom_field(
            dt="Address",
            fieldname="custom_latitude",
            label="Latitude",
            fieldtype="Float",
            insert_after=insert_after,
            description="Door pin latitude. Written only by jarz_pos.services.geo_resolution.",
            non_negative=0,
            precision="6",
        )
        _ensure_custom_field(
            dt="Address",
            fieldname="custom_longitude",
            label="Longitude",
            fieldtype="Float",
            insert_after="custom_latitude",
            description="Door pin longitude. Written only by jarz_pos.services.geo_resolution.",
            non_negative=0,
            precision="6",
        )
        _ensure_custom_field(
            dt="Address",
            fieldname="custom_geo_source",
            label="Geo Source",
            fieldtype="Select",
            insert_after="custom_longitude",
            # Leading blank is intentional: Frappe's convention for an optional
            # Select. Order matches CONFIDENCE_RANK but is NOT what enforces the
            # ladder — jarz_pos.utils.geo.CONFIDENCE_RANK is.
            options="\nterritory_centroid\npos_link\ncustomer_pin\ncourier_web\ncourier_verified\nmanual_override",
            description="Where the pin came from; the label half of the confidence ladder.",
            in_standard_filter=1,
        )
        _ensure_custom_field(
            dt="Address",
            fieldname="custom_geo_confidence",
            label="Geo Confidence",
            fieldtype="Int",
            insert_after="custom_geo_source",
            description="Integer rank of custom_geo_source. Derived, never written independently.",
            read_only=1,
            non_negative=1,
        )
        _ensure_custom_field(
            dt="Address",
            fieldname="custom_geo_accuracy_m",
            label="Geo Accuracy (m)",
            fieldtype="Float",
            insert_after="custom_geo_confidence",
            description="Accuracy radius in metres for the stored pin.",
            non_negative=1,
            precision="2",
        )
        _ensure_custom_field(
            dt="Address",
            fieldname="custom_geo_verified_on",
            label="Geo Verified On",
            fieldtype="Datetime",
            insert_after="custom_geo_accuracy_m",
            description="Set when the pin reaches courier_verified confidence.",
            read_only=1,
        )
    except Exception as e:
        _log(f"ensure_address_geo_fields error: {e}")


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
