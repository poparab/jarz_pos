"""Whitelisted API for B2B customer label stock (mobile app + Desk buttons).

Every quantity returned here is computed by ``services.label_stock`` from the
movement ledger, so the app, the Desk list view and the daily alert can never
disagree about how many labels a customer has or whether they need printing.

Access: B2B Sales Reps and the manager tier. The rep owns the customer
relationship and notices the shortage; the manager owns the print spend. Both
read and write through the same gate -- there is no money in this ledger, and a
feature only the manager can update is a feature nobody updates.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import frappe

from jarz_pos.services import label_stock

LABEL_DOCTYPE = label_stock.LABEL_DOCTYPE
MOVEMENT_DOCTYPE = label_stock.MOVEMENT_DOCTYPE
PRINT_ORDER_DOCTYPE = label_stock.PRINT_ORDER_DOCTYPE

_ACCESS_ROLES = {
    "B2B Sales Rep",
    "JARZ Manager",
    "System Manager",
    "Administrator",
}

MOVEMENT_TYPES = ("Print Received", "Consumed", "Adjustment", "Scrapped", "Opening")
PRINT_ORDER_STATUSES = ("Requested", "Printing", "Ready", "Received", "Cancelled")


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------
def _can_access() -> bool:
    roles = set(frappe.get_roles(frappe.session.user) or [])
    return bool(roles.intersection(_ACCESS_ROLES))


def _ensure_access() -> None:
    if not _can_access():
        frappe.throw("Not permitted: B2B sales or manager access required.")


def _int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _bool(value: Any, default: bool = False) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _clean(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


def _require_label(label: str) -> str:
    name = _clean(label)
    if not name or not frappe.db.exists(LABEL_DOCTYPE, name):
        frappe.throw(f"Label {label!r} not found.")
    return name


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
@frappe.whitelist()
def get_label_dashboard(customer=None, only_attention=0, include_untracked=1):
    """Every tracked label with its stock, cover and reorder status.

    Shape: ``{"summary": {...}, "labels": [...], "settings": {...}}``. The list is
    one row per (customer, label design) and is sorted most urgent first, so the
    app can render it without re-sorting.
    """
    _ensure_access()

    snapshots = label_stock.list_label_snapshots(
        customer=_clean(customer),
        include_untracked=_bool(include_untracked, True),
        only_attention=_bool(only_attention, False),
    )
    settings = label_stock.get_label_settings()

    return {
        "summary": label_stock.summarise(snapshots),
        "labels": snapshots,
        "settings": {
            "lead_days_min": settings["lead_days_min"],
            "lead_days_max": settings["lead_days_max"],
            "rest_day": settings["rest_day"],
            "buffer_days": settings["buffer_days"],
            "auto_consume": settings["auto_consume"],
            "alerts_enabled": settings["alerts_enabled"],
            "expected_ready_if_ordered_today": str(
                label_stock.expected_ready_date(settings=settings)
            ),
        },
    }


@frappe.whitelist()
def get_label_alert_count():
    """Cheap badge count: how many labels currently need printing attention."""
    _ensure_access()
    snapshots = label_stock.list_label_snapshots(include_untracked=False, only_attention=True)
    summary = label_stock.summarise(snapshots)
    return {
        "needs_attention": summary["needs_attention"],
        "out_of_stock": summary["out_of_stock"],
        "reorder_now": summary["reorder_now"],
        "reorder_soon": summary["reorder_soon"],
    }


@frappe.whitelist()
def get_label_detail(label, movement_limit=40):
    """One label with its recent ledger and its print orders."""
    _ensure_access()
    name = _require_label(label)

    row = frappe.db.get_value(
        LABEL_DOCTYPE,
        name,
        [
            "name", "customer", "customer_name", "label_title", "enabled", "we_print",
            "applies_to_item_group", "labels_per_unit", "min_stock_qty", "reorder_qty",
            "last_counted_on", "notes",
        ],
        as_dict=True,
    )
    snapshot = label_stock.build_snapshot(row)

    movements = frappe.get_all(
        MOVEMENT_DOCTYPE,
        filters={"label": name},
        fields=[
            "name", "movement_type", "qty", "posting_date", "remarks",
            "reference_doctype", "reference_name", "print_order", "owner", "creation",
        ],
        order_by="posting_date desc, creation desc",
        limit_page_length=_int(movement_limit, 40) or 40,
    )

    print_orders = frappe.get_all(
        PRINT_ORDER_DOCTYPE,
        filters={"label": name},
        fields=[
            "name", "qty", "status", "requested_on", "expected_ready_date",
            "received_on", "received_qty", "printer_name", "notes", "requested_by",
        ],
        order_by="requested_on desc, creation desc",
        limit_page_length=25,
    )

    snapshot["movements"] = [
        {
            "name": m.get("name"),
            "movement_type": m.get("movement_type"),
            "qty": _int(m.get("qty")),
            "posting_date": str(m.get("posting_date") or "") or None,
            "remarks": m.get("remarks"),
            "reference_doctype": m.get("reference_doctype"),
            "reference_name": m.get("reference_name"),
            "print_order": m.get("print_order"),
            "owner": m.get("owner"),
        }
        for m in movements or []
    ]
    snapshot["print_orders"] = [
        {
            "name": p.get("name"),
            "qty": _int(p.get("qty")),
            "status": p.get("status"),
            "requested_on": str(p.get("requested_on") or "") or None,
            "expected_ready_date": str(p.get("expected_ready_date") or "") or None,
            "received_on": str(p.get("received_on") or "") or None,
            "received_qty": _int(p.get("received_qty")),
            "printer_name": p.get("printer_name"),
            "notes": p.get("notes"),
            "requested_by": p.get("requested_by"),
        }
        for p in print_orders or []
    ]
    return snapshot


@frappe.whitelist()
def search_label_customers(query=""):
    """Company customers, flagged with whether they already have a label.

    Used by the "add a label" picker. Mirrors ``price_lists.search_b2b_customers``:
    ``customer_type = "Company"`` is what this deployment means by a B2B customer.
    """
    _ensure_access()

    filters: Dict[str, Any] = {}
    try:
        if frappe.get_meta("Customer").get_field("customer_type"):
            filters["customer_type"] = "Company"
    except Exception:
        pass

    or_filters = None
    text = _clean(query)
    if text:
        or_filters = {"name": ["like", f"%{text}%"], "customer_name": ["like", f"%{text}%"]}

    try:
        rows = frappe.get_all(
            "Customer",
            filters=filters,
            or_filters=or_filters,
            fields=["name", "customer_name", "customer_group"],
            order_by="customer_name asc",
            limit_page_length=50,
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), "labels.search_label_customers")
        rows = []

    existing: Dict[str, int] = {}
    if rows:
        try:
            for row in frappe.get_all(
                LABEL_DOCTYPE,
                filters={"customer": ["in", [r["name"] for r in rows]]},
                fields=["customer"],
                limit_page_length=0,
            ):
                existing[row["customer"]] = existing.get(row["customer"], 0) + 1
        except Exception:
            existing = {}

    return {
        "customers": [
            {
                "customer": r.get("name"),
                "customer_name": r.get("customer_name") or r.get("name"),
                "customer_group": r.get("customer_group"),
                "label_count": existing.get(r.get("name"), 0),
            }
            for r in rows
        ]
    }


@frappe.whitelist()
def get_label_item_groups():
    """Item Groups a label can be scoped to, for the label form's picker."""
    _ensure_access()
    try:
        rows = frappe.get_all(
            "Item",
            filters={"disabled": 0, "is_sales_item": 1},
            fields=["item_group"],
            limit_page_length=0,
        )
    except Exception:
        rows = []
    groups = sorted({r.get("item_group") for r in rows or [] if r.get("item_group")})
    return {"item_groups": groups}


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------
@frappe.whitelist()
def create_label(
    customer,
    label_title="Default",
    we_print=1,
    applies_to_item_group=None,
    labels_per_unit=1,
    min_stock_qty=0,
    reorder_qty=0,
    opening_qty=0,
    notes=None,
):
    """Start tracking a label design for a customer.

    ``opening_qty`` posts the first ledger row, so a customer whose labels are
    already sitting in the store does not start life reading zero and firing an
    Out of Stock alert on day one.
    """
    _ensure_access()

    name = _clean(customer)
    if not name or not frappe.db.exists("Customer", name):
        frappe.throw(f"Customer {customer!r} not found.")

    doc = frappe.new_doc(LABEL_DOCTYPE)
    doc.customer = name
    doc.label_title = _clean(label_title) or "Default"
    doc.enabled = 1
    doc.we_print = 1 if _bool(we_print, True) else 0
    doc.applies_to_item_group = _clean(applies_to_item_group)
    doc.labels_per_unit = _float(labels_per_unit, 1.0) or 1.0
    doc.min_stock_qty = max(_int(min_stock_qty), 0)
    doc.reorder_qty = max(_int(reorder_qty), 0)
    doc.notes = _clean(notes)
    doc.insert(ignore_permissions=True)

    opening = _int(opening_qty)
    if opening:
        label_stock.post_movement(
            label=doc.name,
            movement_type="Opening",
            qty=opening,
            remarks="Opening label count",
        )
        frappe.db.set_value(
            LABEL_DOCTYPE, doc.name, "last_counted_on", frappe.utils.today(), update_modified=False
        )

    label_stock.refresh_label(doc.name)
    return get_label_detail(doc.name)


@frappe.whitelist()
def update_label(
    label,
    label_title=None,
    enabled=None,
    we_print=None,
    applies_to_item_group=None,
    labels_per_unit=None,
    min_stock_qty=None,
    reorder_qty=None,
    notes=None,
):
    """Change a label's policy. Only the arguments actually passed are written."""
    _ensure_access()
    name = _require_label(label)
    doc = frappe.get_doc(LABEL_DOCTYPE, name)

    if label_title is not None:
        doc.label_title = _clean(label_title) or doc.label_title
    if enabled is not None:
        doc.enabled = 1 if _bool(enabled) else 0
    if we_print is not None:
        doc.we_print = 1 if _bool(we_print) else 0
    if applies_to_item_group is not None:
        # An explicit empty string clears the scope back to catch-all.
        doc.applies_to_item_group = _clean(applies_to_item_group)
    if labels_per_unit is not None:
        doc.labels_per_unit = _float(labels_per_unit, 1.0) or 1.0
    if min_stock_qty is not None:
        doc.min_stock_qty = max(_int(min_stock_qty), 0)
    if reorder_qty is not None:
        doc.reorder_qty = max(_int(reorder_qty), 0)
    if notes is not None:
        doc.notes = _clean(notes)

    doc.save(ignore_permissions=True)
    label_stock.refresh_label(name)
    return get_label_detail(name)


@frappe.whitelist()
def record_movement(label, movement_type, qty, posting_date=None, remarks=None):
    """Post one ledger row by hand (a receipt, a scrap, a correction)."""
    _ensure_access()
    name = _require_label(label)

    kind = _clean(movement_type)
    if kind not in MOVEMENT_TYPES:
        frappe.throw(f"Unknown movement type {movement_type!r}.")

    amount = _int(qty)
    if amount == 0:
        frappe.throw("Quantity cannot be zero.")

    movement = label_stock.post_movement(
        label=name,
        movement_type=kind,
        qty=amount,
        posting_date=_clean(posting_date),
        remarks=_clean(remarks),
    )
    return {"movement": movement, "label": get_label_detail(name)}


@frappe.whitelist()
def record_count(label, counted_qty, remarks=None):
    """Reconcile the ledger to a physical count.

    Posts the *difference* as an Adjustment rather than overwriting the balance,
    so the count is visible in the ledger as an event with a size -- which is the
    only way anybody later notices that a label keeps going missing.
    """
    _ensure_access()
    name = _require_label(label)

    counted = _int(counted_qty, -1)
    if counted < 0:
        frappe.throw("Counted quantity must be zero or more.")

    on_hand = label_stock.get_on_hand(name)
    delta = counted - on_hand
    movement = None
    if delta:
        movement = label_stock.post_movement(
            label=name,
            movement_type="Adjustment",
            qty=delta,
            remarks=_clean(remarks) or f"Physical count: {counted} (was {on_hand})",
        )

    frappe.db.set_value(
        LABEL_DOCTYPE, name, "last_counted_on", frappe.utils.today(), update_modified=False
    )
    label_stock.refresh_label(name)
    return {"movement": movement, "delta": delta, "label": get_label_detail(name)}


@frappe.whitelist()
def create_print_order(label, qty, printer_name=None, requested_on=None, notes=None):
    """Send a batch to the print house.

    The expected-ready date is computed server-side over working days, so the
    promise the app shows is the one the alert suppression is based on.
    """
    _ensure_access()
    name = _require_label(label)

    amount = _int(qty)
    if amount <= 0:
        frappe.throw("Print quantity must be greater than zero.")

    doc = frappe.new_doc(PRINT_ORDER_DOCTYPE)
    doc.label = name
    doc.qty = amount
    doc.status = "Requested"
    doc.requested_on = _clean(requested_on) or frappe.utils.today()
    doc.printer_name = _clean(printer_name)
    doc.notes = _clean(notes)
    doc.insert(ignore_permissions=True)

    label_stock.refresh_label(name)
    return {"print_order": doc.name, "label": get_label_detail(name)}


@frappe.whitelist()
def update_print_order(print_order, status=None, received_qty=None, received_on=None, notes=None, printer_name=None):
    """Advance a print order. Setting ``Received`` credits the stock exactly once."""
    _ensure_access()

    name = _clean(print_order)
    if not name or not frappe.db.exists(PRINT_ORDER_DOCTYPE, name):
        frappe.throw(f"Print order {print_order!r} not found.")

    doc = frappe.get_doc(PRINT_ORDER_DOCTYPE, name)

    if status is not None:
        new_status = _clean(status)
        if new_status not in PRINT_ORDER_STATUSES:
            frappe.throw(f"Unknown print order status {status!r}.")
        if doc.status == "Received" and new_status != "Received":
            frappe.throw(
                "A received batch cannot be reopened. Post a Scrapped or Adjustment "
                "movement on the label instead."
            )
        doc.status = new_status
    if received_qty is not None:
        doc.received_qty = max(_int(received_qty), 0)
    if received_on is not None:
        doc.received_on = _clean(received_on)
    if notes is not None:
        doc.notes = _clean(notes)
    if printer_name is not None:
        doc.printer_name = _clean(printer_name)

    doc.save(ignore_permissions=True)
    return {"print_order": doc.name, "label": get_label_detail(doc.label)}


@frappe.whitelist()
def run_alerts_now():
    """Run the daily alert pass on demand (manager tooling / smoke test)."""
    _ensure_access()
    roles = set(frappe.get_roles(frappe.session.user) or [])
    if not roles.intersection({"JARZ Manager", "System Manager", "Administrator"}):
        frappe.throw("Not permitted: manager access required.")
    return label_stock.run_label_stock_alerts()
