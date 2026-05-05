from __future__ import annotations

from typing import Any, Dict, List, Optional

import frappe


ADDRESS_PHONE_FIELDS = ("mobile_no", "phone", "phone_no", "phone_number")


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    normalized = str(value or "").strip().lower()
    return normalized in {"1", "true", "yes", "y", "on"}


def _address_fields() -> List[str]:
    fields = [
        "name",
        "address_title",
        "address_type",
        "address_line1",
        "address_line2",
        "city",
        "is_primary_address",
        "is_shipping_address",
        "modified",
    ]
    for fieldname in ADDRESS_PHONE_FIELDS:
        if frappe.db.has_column("Address", fieldname):
            fields.append(fieldname)
    return fields


def _address_phone(address_row: Dict[str, Any], fallback: str = "") -> str:
    for fieldname in ADDRESS_PHONE_FIELDS:
        value = str(address_row.get(fieldname) or "").strip()
        if value:
            return value
    return fallback


def format_address_text(address_row: Dict[str, Any]) -> str:
    parts = []
    for fieldname in ("address_line1", "address_line2", "city"):
        value = str(address_row.get(fieldname) or "").strip()
        if value:
            parts.append(value)
    return ", ".join(parts)


def get_linked_customer_address_names(customer: str) -> List[str]:
    rows = frappe.get_all(
        "Dynamic Link",
        filters={
            "link_doctype": "Customer",
            "link_name": customer,
            "parenttype": "Address",
        },
        fields=["parent"],
        limit_page_length=500,
    ) or []

    names: List[str] = []
    seen = set()
    for row in rows:
        parent_name = str(row.get("parent") or "").strip()
        if parent_name and parent_name not in seen:
            seen.add(parent_name)
            names.append(parent_name)
    return names


def get_linked_customer_addresses(customer: str) -> List[Dict[str, Any]]:
    address_names = get_linked_customer_address_names(customer)
    if not address_names:
        return []

    rows = frappe.get_all(
        "Address",
        filters={"name": ["in", address_names]},
        fields=_address_fields(),
        order_by="is_primary_address desc, modified desc",
        limit_page_length=max(len(address_names), 50),
    ) or []

    normalized: List[Dict[str, Any]] = []
    for row in rows:
        record = dict(row)
        record["is_primary_address"] = _as_bool(record.get("is_primary_address"))
        record["is_shipping_address"] = _as_bool(record.get("is_shipping_address"))
        record["full_address"] = format_address_text(record)
        record["phone"] = _address_phone(record)
        normalized.append(record)
    return normalized


def get_customer_shipping_addresses(customer: str) -> List[Dict[str, Any]]:
    rows = get_linked_customer_addresses(customer)
    shipping_rows = [
        row for row in rows
        if row.get("is_shipping_address") or str(row.get("address_type") or "").strip().lower() == "shipping"
    ]
    return shipping_rows or rows


def resolve_customer_shipping_address(
    customer: str,
    preferred_address_name: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    candidates = get_customer_shipping_addresses(customer)
    if not candidates:
        return None

    preferred = str(preferred_address_name or "").strip()
    if preferred:
        for candidate in candidates:
            if str(candidate.get("name") or "").strip() == preferred:
                return candidate

    primary_address = str(
        frappe.db.get_value("Customer", customer, "customer_primary_address") or ""
    ).strip()
    if primary_address:
        for candidate in candidates:
            if str(candidate.get("name") or "").strip() == primary_address:
                return candidate

    return candidates[0]


def ensure_shipping_address(address_name: str) -> Optional[Any]:
    address_name = str(address_name or "").strip()
    if not address_name or not frappe.db.exists("Address", address_name):
        return None

    address_doc = frappe.get_doc("Address", address_name)
    changed = False
    if str(getattr(address_doc, "address_type", "") or "").strip().lower() != "shipping":
        address_doc.address_type = "Shipping"
        changed = True
    if not _as_bool(getattr(address_doc, "is_shipping_address", 0)):
        address_doc.is_shipping_address = 1
        changed = True

    if changed:
        address_doc.save(ignore_permissions=True)
    return address_doc


def set_customer_primary_shipping_address(customer: str, address_name: str) -> None:
    address_name = str(address_name or "").strip()
    if not customer or not address_name:
        return

    ensure_shipping_address(address_name)
    for linked_address in get_linked_customer_address_names(customer):
        frappe.db.set_value(
            "Address",
            linked_address,
            "is_primary_address",
            1 if linked_address == address_name else 0,
            update_modified=False,
        )

    frappe.db.set_value(
        "Customer",
        customer,
        "customer_primary_address",
        address_name,
        update_modified=False,
    )


def link_shipping_address_to_invoice(invoice_name: str, address_name: str) -> None:
    invoice_name = str(invoice_name or "").strip()
    address_name = str(address_name or "").strip()
    if not invoice_name or not address_name or not frappe.db.exists("Sales Invoice", invoice_name):
        return

    invoice_doc = frappe.get_doc("Sales Invoice", invoice_name)
    changed = False
    if getattr(invoice_doc, "shipping_address_name", None) != address_name:
        invoice_doc.shipping_address_name = address_name
        changed = True
    if getattr(invoice_doc, "customer_address", None) != address_name:
        invoice_doc.customer_address = address_name
        changed = True

    if changed:
        invoice_doc.flags.ignore_validate_update_after_submit = True
        invoice_doc.save(ignore_permissions=True)
