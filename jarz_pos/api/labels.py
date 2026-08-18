"""Whitelisted API for B2B customer label stock (mobile app + Desk buttons).

v2: one label per (customer, flavour item); ordering in sheets (21 Medium / 18
Large per sheet); a home storage location per label; and real money -- print
batches are billed on supplier Purchase Invoices into a Labels Inventory asset,
and consumption drains that value into Label Cost (COGS) via Journal Entries
posted by ``services.label_stock``.

Every quantity returned here is computed by ``services.label_stock`` from the
movement ledger, so the app, the Desk list view and the daily alert can never
disagree about how many labels a customer has or whether they need printing.

Access: B2B Sales Reps and the manager tier for the day-to-day flow. Billing a
batch (creating the supplier Purchase Invoice) additionally requires the
manager tier -- reps receive boxes; managers commit money.
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
_MANAGER_ROLES = {"JARZ Manager", "System Manager", "Administrator"}

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


def _ensure_manager() -> None:
    roles = set(frappe.get_roles(frappe.session.user) or [])
    if not roles.intersection(_MANAGER_ROLES):
        frappe.throw("Not permitted: manager access required.")


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


def _coerce_list(value: Any) -> List[Any]:
    if isinstance(value, str):
        import json

        try:
            value = json.loads(value)
        except Exception:
            return []
    return list(value) if isinstance(value, (list, tuple)) else []


def _require_label(label: str) -> str:
    name = _clean(label)
    if not name or not frappe.db.exists(LABEL_DOCTYPE, name):
        frappe.throw(f"Label {label!r} not found.")
    return name


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
@frappe.whitelist()
def get_label_dashboard(customer=None, storage_location=None, only_attention=0, include_untracked=1):
    """Every tracked label with its stock, cover, value and reorder status.

    Shape: ``{"summary": {...}, "labels": [...], "settings": {...},
    "locations": [...]}``. One row per (customer, flavour), most urgent first.
    """
    _ensure_access()

    snapshots = label_stock.list_label_snapshots(
        customer=_clean(customer),
        storage_location=_clean(storage_location),
        include_untracked=_bool(include_untracked, True),
        only_attention=_bool(only_attention, False),
    )
    settings = label_stock.get_label_settings()

    locations = sorted({
        s["storage_location"] for s in snapshots if s.get("storage_location")
    })

    return {
        "summary": label_stock.summarise(snapshots),
        "labels": snapshots,
        "locations": locations,
        "settings": _settings_payload(settings),
    }


def _settings_payload(settings: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "lead_days_min": settings["lead_days_min"],
        "lead_days_max": settings["lead_days_max"],
        "rest_day": settings["rest_day"],
        "buffer_days": settings["buffer_days"],
        "auto_consume": settings["auto_consume"],
        "alerts_enabled": settings["alerts_enabled"],
        "sheet_medium": settings["sheet_medium"],
        "sheet_large": settings["sheet_large"],
        "default_print_sheets": settings["default_print_sheets"],
        "post_cogs": settings["post_cogs"],
        "accounting_ready": bool(settings["inventory_account"] and settings["cogs_account"]),
        "expected_ready_if_ordered_today": str(
            label_stock.expected_ready_date(settings=settings)
        ),
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
    """One label with its recent ledger (costed) and its print orders."""
    _ensure_access()
    name = _require_label(label)

    row = frappe.db.get_value(
        LABEL_DOCTYPE,
        name,
        [
            "name", "customer", "customer_name", "label_title", "item", "size",
            "storage_location", "enabled", "we_print", "labels_per_unit",
            "labels_per_sheet", "default_print_sheets", "min_stock_qty",
            "last_counted_on", "notes",
        ],
        as_dict=True,
    )
    snapshot = label_stock.build_snapshot(row)

    movements = frappe.get_all(
        MOVEMENT_DOCTYPE,
        filters={"label": name},
        fields=[
            "name", "movement_type", "qty", "unit_cost", "value", "posting_date",
            "remarks", "reference_doctype", "reference_name", "print_order",
            "journal_entry", "owner", "creation",
        ],
        order_by="posting_date desc, creation desc",
        limit_page_length=_int(movement_limit, 40) or 40,
    )

    print_orders = frappe.get_all(
        PRINT_ORDER_DOCTYPE,
        filters={"label": name},
        fields=[
            "name", "qty", "qty_sheets", "status", "requested_on", "expected_ready_date",
            "received_on", "received_qty", "printer_name", "supplier", "purchase_invoice",
            "total_cost", "cost_per_label", "billing_status", "notes", "requested_by",
        ],
        order_by="requested_on desc, creation desc",
        limit_page_length=25,
    )

    snapshot["movements"] = [
        {
            "name": m.get("name"),
            "movement_type": m.get("movement_type"),
            "qty": _int(m.get("qty")),
            "unit_cost": _float(m.get("unit_cost")),
            "value": _float(m.get("value")),
            "posting_date": str(m.get("posting_date") or "") or None,
            "remarks": m.get("remarks"),
            "reference_doctype": m.get("reference_doctype"),
            "reference_name": m.get("reference_name"),
            "print_order": m.get("print_order"),
            "journal_entry": m.get("journal_entry"),
            "owner": m.get("owner"),
        }
        for m in movements or []
    ]
    snapshot["print_orders"] = [_print_order_payload(p) for p in print_orders or []]
    return snapshot


def _print_order_payload(p: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "name": p.get("name"),
        "qty": _int(p.get("qty")),
        "qty_sheets": _int(p.get("qty_sheets")),
        "status": p.get("status"),
        "requested_on": str(p.get("requested_on") or "") or None,
        "expected_ready_date": str(p.get("expected_ready_date") or "") or None,
        "received_on": str(p.get("received_on") or "") or None,
        "received_qty": _int(p.get("received_qty")),
        "printer_name": p.get("printer_name"),
        "supplier": p.get("supplier"),
        "purchase_invoice": p.get("purchase_invoice"),
        "total_cost": _float(p.get("total_cost")),
        "cost_per_label": _float(p.get("cost_per_label")),
        "billing_status": p.get("billing_status") or "Unbilled",
        "notes": p.get("notes"),
        "requested_by": p.get("requested_by"),
    }


@frappe.whitelist()
def search_label_customers(query=""):
    """Company customers, flagged with whether they already have a label."""
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
            fields=["name", "customer_name", "customer_group", "default_price_list"],
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
                "default_price_list": (r.get("default_price_list") or "").strip() or None,
                "label_count": existing.get(r.get("name"), 0),
            }
            for r in rows
        ]
    }


@frappe.whitelist()
def get_flavour_options(customer):
    """The flavours a customer deals in, grouped by size, for the setup wizard.

    Sources, in order of authority:
      1. Item Price rows on the customer's effective price list -- their agreed
         per-flavour rates ARE the flavour list.
      2. Items they have actually bought (submitted Sales Invoices) -- catches
         customers priced by category rate, where no per-item rows exist.

    Each option is flagged with where it came from and whether a label already
    tracks it, so the wizard renders confirm-a-list, not type-from-memory.
    """
    _ensure_access()
    name = _clean(customer)
    if not name or not frappe.db.exists("Customer", name):
        frappe.throw(f"Customer {customer!r} not found.")

    price_list = (frappe.db.get_value("Customer", name, "default_price_list") or "").strip()

    options: Dict[str, Dict[str, Any]] = {}

    def add(item_code: str, source: str) -> None:
        if not item_code or item_code in options:
            if item_code in options and source not in options[item_code]["sources"]:
                options[item_code]["sources"].append(source)
            return
        meta = frappe.db.get_value(
            "Item", item_code, ["item_name", "item_group", "disabled"], as_dict=True
        )
        if not meta or _int(meta.get("disabled")):
            return
        group = (meta.get("item_group") or "").strip()
        if group not in label_stock.LABEL_BEARING_GROUPS:
            return  # merch/services never carry a customer label
        options[item_code] = {
            "item_code": item_code,
            "item_name": meta.get("item_name") or item_code,
            "size": group,
            "sources": [source],
            "has_label": False,
            "label": None,
        }

    if price_list:
        try:
            for row in frappe.get_all(
                "Item Price",
                filters={"price_list": price_list, "selling": 1},
                fields=["item_code"],
                limit_page_length=0,
            ):
                add(row["item_code"], "price_list")
        except Exception:
            frappe.log_error(frappe.get_traceback(), "labels.get_flavour_options price list")

    try:
        bought = frappe.db.sql(
            """
            select sii.item_code, count(*) as n
            from `tabSales Invoice Item` sii
            join `tabSales Invoice` si on si.name = sii.parent
            where si.customer = %(customer)s and si.docstatus = 1
            group by sii.item_code
            order by n desc
            limit 100
            """,
            {"customer": name},
            as_dict=True,
        )
        for row in bought or []:
            add(row["item_code"], "history")
    except Exception:
        frappe.log_error(frappe.get_traceback(), "labels.get_flavour_options history")

    try:
        for row in frappe.get_all(
            LABEL_DOCTYPE,
            filters={"customer": name},
            fields=["name", "item"],
            limit_page_length=0,
        ):
            code = (row.get("item") or "").strip()
            if code:
                add(code, "label")
                if code in options:
                    options[code]["has_label"] = True
                    options[code]["label"] = row["name"]
    except Exception:
        pass

    grouped = sorted(options.values(), key=lambda o: (o["size"], o["item_name"].lower()))
    return {
        "customer": name,
        "price_list": price_list or None,
        "flavours": grouped,
    }


@frappe.whitelist()
def get_storage_locations():
    """Leaf warehouses a label can live in (branches + the factory)."""
    _ensure_access()
    try:
        rows = frappe.get_all(
            "Warehouse",
            filters={"is_group": 0, "disabled": 0},
            fields=["name", "warehouse_name"],
            order_by="warehouse_name asc",
            limit_page_length=0,
        )
    except Exception:
        rows = []
    return {
        "locations": [
            {"name": r["name"], "label": r.get("warehouse_name") or r["name"]}
            for r in rows
        ]
    }


@frappe.whitelist()
def get_label_item_groups():
    """DEPRECATED v1 shim -- kept so already-shipped v1 clients don't 404.

    v1's "Track a label" sheet scoped labels by Item Group; v2 scopes by exact
    flavour Item and the app no longer calls this. Remove once no v1 build is
    in the field.
    """
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
    return {"item_groups": sorted({r.get("item_group") for r in rows or [] if r.get("item_group")})}


@frappe.whitelist()
def get_print_suppliers(query=""):
    """Suppliers for the print house picker."""
    _ensure_access()
    filters = {"disabled": 0}
    or_filters = None
    text = _clean(query)
    if text:
        or_filters = {"name": ["like", f"%{text}%"], "supplier_name": ["like", f"%{text}%"]}
    try:
        rows = frappe.get_all(
            "Supplier",
            filters=filters,
            or_filters=or_filters,
            fields=["name", "supplier_name"],
            order_by="supplier_name asc",
            limit_page_length=25,
        )
    except Exception:
        rows = []
    return {"suppliers": [
        {"name": r["name"], "supplier_name": r.get("supplier_name") or r["name"]}
        for r in rows
    ]}


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------
@frappe.whitelist()
def create_label(
    customer,
    item=None,
    label_title=None,
    we_print=1,
    storage_location=None,
    labels_per_unit=1,
    labels_per_sheet=0,
    default_print_sheets=0,
    min_stock_qty=0,
    opening_qty=0,
    notes=None,
):
    """Start tracking one flavour's label for a customer.

    ``opening_qty`` posts the first ledger row (at zero cost -- historic stock
    has no bill to price it), so a customer whose labels are already on the
    shelf does not start life firing an Out of Stock alert.
    """
    _ensure_access()

    name = _clean(customer)
    if not name or not frappe.db.exists("Customer", name):
        frappe.throw(f"Customer {customer!r} not found.")

    doc = frappe.new_doc(LABEL_DOCTYPE)
    doc.customer = name
    doc.item = _clean(item)
    doc.label_title = _clean(label_title)
    doc.enabled = 1
    doc.we_print = 1 if _bool(we_print, True) else 0
    doc.storage_location = _clean(storage_location)
    doc.labels_per_unit = _float(labels_per_unit, 1.0) or 1.0
    doc.labels_per_sheet = max(_int(labels_per_sheet), 0)
    doc.default_print_sheets = max(_int(default_print_sheets), 0)
    doc.min_stock_qty = max(_int(min_stock_qty), 0)
    doc.notes = _clean(notes)
    doc.insert(ignore_permissions=True)

    opening = _int(opening_qty)
    if opening:
        label_stock.post_movement(
            label=doc.name,
            movement_type="Opening",
            qty=opening,
            unit_cost=0,
            remarks="Opening label count",
        )
        frappe.db.set_value(
            LABEL_DOCTYPE, doc.name, "last_counted_on", frappe.utils.today(), update_modified=False
        )

    label_stock.refresh_label(doc.name)
    return get_label_detail(doc.name)


@frappe.whitelist()
def setup_customer_labels(
    customer,
    flavours,
    storage_location=None,
    price_list=None,
    default_print_sheets=0,
    we_print=1,
):
    """One-shot account setup: assign the price list, create every flavour label.

    *flavours*: list of ``{"item_code", "opening_qty"?, "min_stock_qty"?}``.
    Existing labels for the same flavour are left untouched (reported under
    ``skipped``), so re-running the wizard is additive, never destructive.

    This is the "new customer ilo" flow: pick his flavours off his price list,
    say where his labels live, and every design is tracked in one tap.
    """
    _ensure_access()

    name = _clean(customer)
    if not name or not frappe.db.exists("Customer", name):
        frappe.throw(f"Customer {customer!r} not found.")

    rows = _coerce_list(flavours)
    if not rows:
        frappe.throw("Pick at least one flavour.")

    assigned_price_list = None
    price_list_error = None
    wanted_list = _clean(price_list)
    if wanted_list:
        # Reuse the pricing module's write path: it owns the double-entry
        # bookkeeping between Customer.default_price_list and the list itself.
        # Its gate is TIGHTER than ours (full manager pricing access; B2B reps
        # are read-only on pricing by design), so a rep running the wizard must
        # not lose the label setup over the pricing half -- the assignment is
        # skipped and reported instead.
        try:
            from jarz_pos.api.price_lists import assign_customer_to_price_list

            assign_customer_to_price_list(customer=name, price_list=wanted_list)
            assigned_price_list = wanted_list
        except Exception as exc:
            price_list_error = str(exc)

    location = _clean(storage_location)
    sheets = max(_int(default_print_sheets), 0)

    created: List[str] = []
    skipped: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        item_code = _clean(row.get("item_code"))
        if not item_code:
            continue
        existing = frappe.db.get_value(
            LABEL_DOCTYPE, {"customer": name, "item": item_code}, "name"
        )
        if existing:
            skipped.append({"item_code": item_code, "label": existing})
            continue

        doc = frappe.new_doc(LABEL_DOCTYPE)
        doc.customer = name
        doc.item = item_code
        doc.enabled = 1
        doc.we_print = 1 if _bool(we_print, True) else 0
        doc.storage_location = location
        doc.labels_per_unit = 1
        doc.default_print_sheets = sheets
        doc.min_stock_qty = max(_int(row.get("min_stock_qty")), 0)
        doc.insert(ignore_permissions=True)

        opening = _int(row.get("opening_qty"))
        if opening:
            label_stock.post_movement(
                label=doc.name,
                movement_type="Opening",
                qty=opening,
                unit_cost=0,
                remarks="Opening label count (account setup)",
                refresh=False,
            )
            frappe.db.set_value(
                LABEL_DOCTYPE, doc.name, "last_counted_on",
                frappe.utils.today(), update_modified=False,
            )
        label_stock.refresh_label(doc.name)
        created.append(doc.name)

    snapshots = label_stock.list_label_snapshots(customer=name)
    return {
        "created": created,
        "skipped": skipped,
        "price_list": assigned_price_list,
        "price_list_error": price_list_error,
        "labels": snapshots,
        "summary": label_stock.summarise(snapshots),
    }


@frappe.whitelist()
def update_label(
    label,
    label_title=None,
    item=None,
    enabled=None,
    we_print=None,
    storage_location=None,
    labels_per_unit=None,
    labels_per_sheet=None,
    default_print_sheets=None,
    min_stock_qty=None,
    notes=None,
):
    """Change a label's policy. Only the arguments actually passed are written."""
    _ensure_access()
    name = _require_label(label)
    doc = frappe.get_doc(LABEL_DOCTYPE, name)

    if label_title is not None:
        doc.label_title = _clean(label_title) or doc.label_title
    if item is not None:
        doc.item = _clean(item)
    if enabled is not None:
        doc.enabled = 1 if _bool(enabled) else 0
    if we_print is not None:
        doc.we_print = 1 if _bool(we_print) else 0
    if storage_location is not None:
        # An explicit empty string clears the location.
        doc.storage_location = _clean(storage_location)
    if labels_per_unit is not None:
        doc.labels_per_unit = _float(labels_per_unit, 1.0) or 1.0
    if labels_per_sheet is not None:
        doc.labels_per_sheet = max(_int(labels_per_sheet), 0)
    if default_print_sheets is not None:
        doc.default_print_sheets = max(_int(default_print_sheets), 0)
    if min_stock_qty is not None:
        doc.min_stock_qty = max(_int(min_stock_qty), 0)
    if notes is not None:
        doc.notes = _clean(notes)

    doc.save(ignore_permissions=True)
    label_stock.refresh_label(name)
    return get_label_detail(name)


@frappe.whitelist()
def record_movement(label, movement_type, qty, posting_date=None, remarks=None):
    """Post one ledger row by hand (a receipt, a scrap, a correction).

    Values at the label's current average cost, and the value is mirrored into
    the COGS journal -- scrapped labels are real money leaving the shelf.
    """
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
    only way anybody later notices that a label keeps going missing. The
    difference is valued at average cost and mirrored to the GL: shrinkage is a
    real cost, not a free correction.
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
def create_print_order(
    label,
    qty_sheets,
    supplier=None,
    printer_name=None,
    total_cost=0,
    requested_on=None,
    notes=None,
):
    """Send a batch to the print house, in SHEETS.

    The label quantity and the expected-ready date are computed server-side, so
    the promise the app shows is the one alert suppression is based on.
    ``total_cost`` (net) may be given now if the printer quoted, or later when
    the bill arrives -- billing itself happens in :func:`bill_print_order`.
    """
    _ensure_access()
    name = _require_label(label)

    sheets = _int(qty_sheets)
    if sheets <= 0:
        frappe.throw("Order at least one sheet.")

    doc = frappe.new_doc(PRINT_ORDER_DOCTYPE)
    doc.label = name
    doc.qty_sheets = sheets
    doc.status = "Requested"
    doc.requested_on = _clean(requested_on) or frappe.utils.today()
    doc.supplier = _clean(supplier)
    doc.printer_name = _clean(printer_name)
    doc.total_cost = max(_float(total_cost), 0.0)
    doc.notes = _clean(notes)
    doc.insert(ignore_permissions=True)

    label_stock.refresh_label(name)
    return {"print_order": doc.name, "label": get_label_detail(name)}


@frappe.whitelist()
def update_print_order(
    print_order,
    status=None,
    received_qty=None,
    received_on=None,
    total_cost=None,
    supplier=None,
    notes=None,
    printer_name=None,
):
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
    if total_cost is not None:
        doc.total_cost = max(_float(total_cost), 0.0)
    if supplier is not None:
        doc.supplier = _clean(supplier)
    if notes is not None:
        doc.notes = _clean(notes)
    if printer_name is not None:
        doc.printer_name = _clean(printer_name)

    doc.save(ignore_permissions=True)
    return {"print_order": doc.name, "label": get_label_detail(doc.label)}


@frappe.whitelist()
def bill_print_order(
    print_order,
    supplier=None,
    total_cost=None,
    bill_no=None,
    bill_date=None,
    is_paid=0,
    payment_option=None,
):
    """Book the printer's bill: a real supplier Purchase Invoice into Labels Inventory.

    Manager-gated -- this commits money. The PI bills the ``Customer Label
    Printing`` non-stock item (sheets x per-sheet rate), whose expense account
    is the Labels Inventory asset, so the batch value lands on the balance
    sheet; consumption later drains it into Label Cost via the movement JEs.

    Works before OR after the batch is received:
      * before -- the receipt that follows prices at this bill's cost;
      * after  -- the receipt was booked at its recorded cost, so a zero-qty
        revaluation movement posts the difference (``post_gl=False``: the PI is
        the GL side), keeping ledger value == account balance.
    """
    _ensure_manager()

    name = _clean(print_order)
    if not name or not frappe.db.exists(PRINT_ORDER_DOCTYPE, name):
        frappe.throw(f"Print order {print_order!r} not found.")

    doc = frappe.get_doc(PRINT_ORDER_DOCTYPE, name)
    if doc.purchase_invoice:
        frappe.throw(f"{name} is already billed on {doc.purchase_invoice}.")
    if doc.status == "Cancelled":
        frappe.throw("A cancelled batch cannot be billed.")

    supplier_name = _clean(supplier) or _clean(doc.supplier)
    if not supplier_name:
        frappe.throw("Pick the print supplier.")

    cost = _float(total_cost) if total_cost is not None else _float(doc.total_cost)
    if cost <= 0:
        frappe.throw("Enter what the printer charged (net of VAT).")

    settings = label_stock.get_label_settings()
    printing_item = settings["printing_item"]
    if not printing_item or not frappe.db.exists("Item", printing_item):
        frappe.throw(
            "The label printing item is not configured (Jarz POS Settings > "
            "Label Printing Item). Run bench migrate or create it, then retry."
        )

    sheets = _int(doc.qty_sheets) or 1
    from jarz_pos.api.purchase import create_purchase_invoice

    result = create_purchase_invoice(
        supplier=supplier_name,
        is_paid=1 if _bool(is_paid) else 0,
        payment_option=_clean(payment_option),
        items=[{
            "item_code": printing_item,
            "qty": sheets,
            "uom": "Nos",
            "rate": round(cost / sheets, 4),
        }],
        bill_no=_clean(bill_no),
        bill_date=_clean(bill_date),
        remarks=f"Label print batch {name} ({doc.label})",
        idempotency_key=f"label-po-{name}",
    )
    pi_name = result.get("purchase_invoice")

    doc.supplier = supplier_name
    doc.total_cost = cost
    doc.purchase_invoice = pi_name
    doc.save(ignore_permissions=True)

    # Receipt already posted at a different cost? True the ledger up so its
    # summed value matches what the PI just put on the balance sheet.
    receipt = frappe.db.get_value(
        MOVEMENT_DOCTYPE,
        {"print_order": name, "movement_type": "Print Received"},
        ["name", "qty", "value"],
        as_dict=True,
    )
    if receipt:
        booked = _float(receipt.get("value"))
        difference = round(cost - booked, 2)
        if abs(difference) >= 0.01:
            # Inserted directly, NOT via post_movement: a revaluation is a
            # zero-quantity, value-only row, and post_movement (correctly)
            # refuses zero-qty movements. No JE either -- the PI itself is the
            # GL side of this value; the row only keeps ledger value in step.
            reval = frappe.new_doc(MOVEMENT_DOCTYPE)
            reval.label = doc.label
            reval.movement_type = "Adjustment"
            reval.qty = 0
            reval.unit_cost = 0
            reval.value = difference
            reval.posting_date = frappe.utils.today()
            reval.reference_doctype = "Purchase Invoice"
            reval.reference_name = pi_name
            reval.print_order = name
            reval.remarks = (
                f"Revaluation: batch {name} billed at {cost:.2f} "
                f"(was booked at {booked:.2f})"
            )
            reval.flags.ignore_permissions = True
            reval.flags.allow_zero_qty = True
            reval.insert(ignore_permissions=True)
            label_stock.refresh_label(doc.label)

    return {
        "print_order": name,
        "purchase_invoice": pi_name,
        "label": get_label_detail(doc.label),
    }


@frappe.whitelist()
def run_alerts_now():
    """Run the daily alert pass on demand (manager tooling / smoke test)."""
    _ensure_manager()
    return label_stock.run_label_stock_alerts()
