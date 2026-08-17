"""B2B customer label stock tracking, reorder forecasting and alerts.

Most B2B customers sell JARZ product under their own branding, and JARZ prints
those jar labels for them. Printing is outsourced: a batch takes a couple of
*working* days (the print house is closed on the weekly rest day, Friday here),
so running out is never a same-day problem -- it stalls the customer's next order
for the better part of a week. This module is the ledger and the early warning.

Model
-----
``Jarz Customer Label``     one label design per (customer, title).
                            ``we_print = 0`` marks the customers who supply their
                            own labels: they are never counted and never alerted
                            on, which is the whole point of the flag.
``Jarz Label Movement``     the ledger. On hand is ALWAYS ``SUM(qty)`` over this
                            table -- never a stored counter, which would drift the
                            first time a movement was deleted.
``Jarz Label Print Order``  a print job in flight. An open order suppresses the
                            reorder alert so the same shortage is not announced
                            every morning until the batch lands.

Guarantees mirrored from ``services/consumable_deduction`` and ``crm/*``:

* Imports cleanly with NO top-level ``frappe`` calls.
* Never raises into a document hook or the scheduler -- every public entry point
  is wrapped, and failures are logged with ``log_error`` (not ``logger().info``,
  which is invisible on staging and production).
* Every write is a per-field ``frappe.db.set_value(..., update_modified=False)``
  or a fresh document insert; nothing does a full save of a Single.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

try:
    import frappe
except Exception:  # pragma: no cover - import-time safety, mirrors sibling services
    frappe = None  # type: ignore

LABEL_DOCTYPE = "Jarz Customer Label"
MOVEMENT_DOCTYPE = "Jarz Label Movement"
PRINT_ORDER_DOCTYPE = "Jarz Label Print Order"
SETTINGS_DOCTYPE = "Jarz POS Settings"

#: Movement types whose sign is fixed. ``Adjustment`` and ``Opening`` are
#: deliberately absent: a stock count corrects in either direction.
_POSITIVE_TYPES = {"Print Received"}
_NEGATIVE_TYPES = {"Consumed", "Scrapped"}

#: Print-order statuses that mean "a batch is already on its way".
OPEN_PRINT_STATUSES = ("Requested", "Printing", "Ready")

#: Status values, most urgent first. Kept in step with the Select options on
#: ``Jarz Customer Label.status`` and with ``LabelStatus`` in the Flutter app.
STATUS_OUT_OF_STOCK = "Out of Stock"
STATUS_REORDER_NOW = "Reorder Now"
STATUS_REORDER_SOON = "Reorder Soon"
STATUS_ON_ORDER = "On Order"
STATUS_OK = "OK"
STATUS_NOT_TRACKED = "Not Tracked"

#: The statuses that put a label on the alert list.
ALERT_STATUSES = (STATUS_OUT_OF_STOCK, STATUS_REORDER_NOW, STATUS_REORDER_SOON)

# Python's ``date.weekday()``: Mon=0 ... Fri=4, Sat=5, Sun=6.
_REST_DAY_SETS: Dict[str, Set[int]] = {
    "none": set(),
    "friday": {4},
    "friday & saturday": {4, 5},
    "saturday": {5},
    "sunday": {6},
}

#: Defaults applied when the settings field has never been written. ``Jarz POS
#: Settings`` is a Single -- its values live in ``tabSingles``, so a newly added
#: field has no row at all and its DocType default never lands on the existing
#: document. Reading through these fallbacks is the only thing that makes a new
#: field behave the way its form description claims.
DEFAULT_LEAD_DAYS_MIN = 2
DEFAULT_LEAD_DAYS_MAX = 3
DEFAULT_REST_DAY = "Friday"
DEFAULT_BUFFER_DAYS = 3
DEFAULT_USAGE_WINDOW_DAYS = 60
#: Never divide the usage window by fewer days than this -- a label created
#: yesterday that shipped 400 units would otherwise forecast 400/day.
_MIN_USAGE_DIVISOR_DAYS = 7


# ---------------------------------------------------------------------------
# Small guarded helpers
# ---------------------------------------------------------------------------
def _doctype_exists(name: str) -> bool:
    try:
        return bool(frappe.db.exists("DocType", name))
    except Exception:
        return False


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


def _single_value(fieldname: str) -> Any:
    try:
        return frappe.db.get_single_value(SETTINGS_DOCTYPE, fieldname)
    except Exception:
        return None


def _today():
    from frappe.utils import getdate, today

    return getdate(today())


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
def get_label_settings() -> Dict[str, Any]:
    """Effective label-printing settings, with defaults for never-written fields.

    Never raises: a site whose ``Jarz POS Settings`` does not yet carry these
    fields gets the documented defaults (2-3 working days, Friday closed, 3 days
    of buffer).
    """
    lead_min = _int(_single_value("label_print_lead_days_min"), DEFAULT_LEAD_DAYS_MIN)
    lead_max = _int(_single_value("label_print_lead_days_max"), DEFAULT_LEAD_DAYS_MAX)
    if lead_min <= 0:
        lead_min = DEFAULT_LEAD_DAYS_MIN
    if lead_max <= 0:
        lead_max = DEFAULT_LEAD_DAYS_MAX
    if lead_max < lead_min:
        lead_max = lead_min

    rest_raw = _single_value("label_print_rest_day")
    rest_label = str(rest_raw or DEFAULT_REST_DAY).strip() or DEFAULT_REST_DAY
    rest_days = _REST_DAY_SETS.get(rest_label.lower())
    if rest_days is None:
        rest_days = _REST_DAY_SETS[DEFAULT_REST_DAY.lower()]
        rest_label = DEFAULT_REST_DAY

    buffer_days = _int(_single_value("label_reorder_buffer_days"), DEFAULT_BUFFER_DAYS)
    if buffer_days < 0:
        buffer_days = DEFAULT_BUFFER_DAYS

    auto_raw = _single_value("label_auto_consume_on_invoice")
    alerts_raw = _single_value("label_alerts_enabled")

    return {
        "lead_days_min": lead_min,
        "lead_days_max": lead_max,
        "rest_day": rest_label,
        "rest_weekdays": set(rest_days),
        "buffer_days": buffer_days,
        # Both default ON when never written. That is safe because the feature is
        # inert until somebody creates a Jarz Customer Label: a site with no
        # labels consumes nothing and alerts on nothing, so there is no dark flag
        # anyone has to remember to switch on later.
        "auto_consume": True if auto_raw in (None, "") else bool(_int(auto_raw, 1)),
        "alerts_enabled": True if alerts_raw in (None, "") else bool(_int(alerts_raw, 1)),
        "usage_window_days": DEFAULT_USAGE_WINDOW_DAYS,
    }


# ---------------------------------------------------------------------------
# Working-day date maths
# ---------------------------------------------------------------------------
def add_working_days(start_date: Any, days: int, rest_weekdays: Optional[Iterable[int]] = None):
    """Return *start_date* advanced by *days*, skipping the weekly rest day(s).

    ``add_working_days("2026-08-19", 3, {4})`` -- Wednesday plus three printing
    days with Friday closed -- is Sunday 2026-08-23, not Saturday the 22nd.

    A rest-day set covering the whole week would loop forever, so the walk is
    bounded; in that (misconfigured) case it degrades to a plain calendar add.
    """
    from frappe.utils import add_days, getdate

    current = getdate(start_date)
    remaining = _int(days, 0)
    if remaining <= 0:
        return current

    rest = {int(d) for d in (rest_weekdays or set())}
    if not rest or len(rest) >= 7:
        return getdate(add_days(current, remaining))

    guard = remaining * 7 + 14  # generous, but bounded
    while remaining > 0 and guard > 0:
        guard -= 1
        current = getdate(add_days(current, 1))
        if current.weekday() not in rest:
            remaining -= 1
    return current


def expected_ready_date(requested_on: Any = None, *, settings: Optional[Dict[str, Any]] = None):
    """Date a batch requested on *requested_on* is expected to be ready.

    Uses the slower end of the lead time on purpose: the date a planner needs to
    promise against is the late one, not the optimistic one.
    """
    settings = settings or get_label_settings()
    from frappe.utils import getdate

    start = getdate(requested_on) if requested_on else _today()
    return add_working_days(start, settings["lead_days_max"], settings["rest_weekdays"])


def lead_time_calendar_days(from_date: Any = None, *, settings: Optional[Dict[str, Any]] = None) -> int:
    """Lead time expressed in *calendar* days from *from_date*.

    Days of cover are calendar days -- the customer keeps ordering through the
    rest day -- so the reorder threshold has to be calendar days too. Ordering on
    a Wednesday with Friday closed puts a three-working-day batch four calendar
    days out, and that extra day is exactly what this converts.
    """
    from frappe.utils import date_diff, getdate

    settings = settings or get_label_settings()
    start = getdate(from_date) if from_date else _today()
    ready = add_working_days(start, settings["lead_days_max"], settings["rest_weekdays"])
    try:
        return max(_int(date_diff(ready, start), settings["lead_days_max"]), 0)
    except Exception:
        return settings["lead_days_max"]


# ---------------------------------------------------------------------------
# Ledger reads
# ---------------------------------------------------------------------------
def get_on_hand(label_name: str) -> int:
    """Current on-hand count for one label: ``SUM(qty)`` over its movements."""
    if not label_name or not _doctype_exists(MOVEMENT_DOCTYPE):
        return 0
    try:
        rows = frappe.get_all(
            MOVEMENT_DOCTYPE,
            filters={"label": label_name},
            fields=["qty"],
            limit_page_length=0,
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), f"label_stock: on-hand read failed for {label_name}")
        return 0
    return sum(_int(r.get("qty")) for r in rows or [])


def get_usage_stats(label_name: str, *, settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Consumption rate for one label over the trailing usage window.

    The divisor is the *observed* span rather than the full window, floored at a
    week, so a label that started shipping three days ago does not forecast its
    first order as a daily rate.
    """
    settings = settings or get_label_settings()
    empty = {
        "consumed": 0,
        "window_days": 0,
        "avg_daily_usage": 0.0,
        "first_movement_on": None,
        "last_movement_on": None,
    }
    if not label_name or not _doctype_exists(MOVEMENT_DOCTYPE):
        return empty

    from frappe.utils import add_days, date_diff, getdate

    today = _today()
    window = _int(settings.get("usage_window_days"), DEFAULT_USAGE_WINDOW_DAYS)
    window_start = getdate(add_days(today, -window))

    try:
        rows = frappe.get_all(
            MOVEMENT_DOCTYPE,
            filters={
                "label": label_name,
                "movement_type": "Consumed",
                "posting_date": [">=", window_start],
            },
            fields=["qty", "posting_date"],
            limit_page_length=0,
        )
        latest = frappe.get_all(
            MOVEMENT_DOCTYPE,
            filters={"label": label_name},
            fields=["posting_date"],
            order_by="posting_date desc",
            limit_page_length=1,
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), f"label_stock: usage read failed for {label_name}")
        return empty

    consumed = sum(abs(_int(r.get("qty"))) for r in rows or [])
    dates = [getdate(r.get("posting_date")) for r in rows or [] if r.get("posting_date")]
    first = min(dates) if dates else None

    if first:
        try:
            span = _int(date_diff(today, first), 0) + 1
        except Exception:
            span = _MIN_USAGE_DIVISOR_DAYS
    else:
        span = 0

    divisor = max(span, _MIN_USAGE_DIVISOR_DAYS)
    avg = round(consumed / divisor, 4) if consumed else 0.0

    return {
        "consumed": consumed,
        "window_days": span,
        "avg_daily_usage": avg,
        "first_movement_on": str(first) if first else None,
        "last_movement_on": str(latest[0].get("posting_date")) if latest else None,
    }


def get_open_print_orders(label_name: str) -> List[Dict[str, Any]]:
    """Print orders for *label_name* that have not landed or been cancelled."""
    if not label_name or not _doctype_exists(PRINT_ORDER_DOCTYPE):
        return []
    try:
        return frappe.get_all(
            PRINT_ORDER_DOCTYPE,
            filters={"label": label_name, "status": ["in", list(OPEN_PRINT_STATUSES)]},
            fields=["name", "qty", "status", "requested_on", "expected_ready_date", "printer_name"],
            order_by="expected_ready_date asc",
            limit_page_length=0,
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), f"label_stock: open print orders failed for {label_name}")
        return []


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------
def compute_status(
    *,
    tracked: bool,
    on_hand: int,
    min_stock: int,
    avg_daily_usage: float,
    lead_days: int,
    buffer_days: int,
    has_open_print_order: bool,
) -> str:
    """Pure status decision -- no DB access, so it is directly unit-testable.

    The order of the checks *is* the business rule:

    1. Untracked labels (the customer prints their own, or the design is retired)
       never get a status other than ``Not Tracked``.
    2. Zero or negative on hand is ``Out of Stock`` *even when a batch is on its
       way*. A print order arriving Sunday does not make Thursday less broken.
    3. Below the safety floor, or fewer days of cover than the print lead time,
       is ``Reorder Now``: by the time a batch ordered today lands, the shelf is
       already empty.
    4. Within the lead time plus the buffer is ``Reorder Soon``.
    5. An open print order downgrades 3 and 4 to ``On Order``, so the same
       shortage is not re-announced every morning until it lands.
    """
    if not tracked:
        return STATUS_NOT_TRACKED
    if on_hand <= 0:
        return STATUS_OUT_OF_STOCK

    cover = (on_hand / avg_daily_usage) if avg_daily_usage > 0 else None

    if on_hand <= min_stock or (cover is not None and cover <= lead_days):
        return STATUS_ON_ORDER if has_open_print_order else STATUS_REORDER_NOW
    if cover is not None and cover <= (lead_days + buffer_days):
        return STATUS_ON_ORDER if has_open_print_order else STATUS_REORDER_SOON
    return STATUS_OK


def build_snapshot(label_row: Dict[str, Any], *, settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Full computed picture of one label -- the single source of truth for status.

    Used by the API, by ``refresh_label`` and by the daily alert, so the three can
    never disagree about what "Reorder Now" means.
    """
    settings = settings or get_label_settings()
    name = label_row.get("name")

    tracked = bool(_int(label_row.get("enabled"), 1)) and bool(_int(label_row.get("we_print"), 1))
    on_hand = get_on_hand(name)
    usage = get_usage_stats(name, settings=settings)
    open_orders = get_open_print_orders(name)
    lead_days = lead_time_calendar_days(settings=settings)
    buffer_days = _int(settings.get("buffer_days"), DEFAULT_BUFFER_DAYS)
    min_stock = _int(label_row.get("min_stock_qty"))
    avg = _float(usage.get("avg_daily_usage"))

    status = compute_status(
        tracked=tracked,
        on_hand=on_hand,
        min_stock=min_stock,
        avg_daily_usage=avg,
        lead_days=lead_days,
        buffer_days=buffer_days,
        has_open_print_order=bool(open_orders),
    )

    days_of_cover = round(on_hand / avg, 1) if avg > 0 else None
    # How many to print: the standard batch, or enough to cover twice the
    # lead-time-plus-buffer horizon, or at least back up to the safety floor --
    # whichever is largest. Never suggests less than what is missing.
    reorder_qty = _int(label_row.get("reorder_qty"))
    target_cover = avg * (lead_days + buffer_days) * 2
    suggested = int(max(reorder_qty, round(target_cover) - on_hand, min_stock - on_hand, 0))

    return {
        "name": name,
        "customer": label_row.get("customer"),
        "customer_name": label_row.get("customer_name") or label_row.get("customer"),
        "label_title": label_row.get("label_title") or "Default",
        "enabled": bool(_int(label_row.get("enabled"), 1)),
        "we_print": bool(_int(label_row.get("we_print"), 1)),
        "tracked": tracked,
        "applies_to_item_group": label_row.get("applies_to_item_group"),
        "labels_per_unit": _float(label_row.get("labels_per_unit"), 1.0) or 1.0,
        "on_hand_qty": on_hand,
        "min_stock_qty": min_stock,
        "reorder_qty": reorder_qty,
        "suggested_print_qty": suggested,
        "avg_daily_usage": avg,
        "days_of_cover": days_of_cover,
        "consumed_in_window": _int(usage.get("consumed")),
        "usage_window_days": _int(usage.get("window_days")),
        "status": status,
        "needs_attention": status in ALERT_STATUSES,
        "lead_days": lead_days,
        "lead_days_min": _int(settings.get("lead_days_min"), DEFAULT_LEAD_DAYS_MIN),
        "lead_days_max": _int(settings.get("lead_days_max"), DEFAULT_LEAD_DAYS_MAX),
        "rest_day": settings.get("rest_day"),
        "expected_ready_if_ordered_today": str(expected_ready_date(settings=settings)),
        "runs_out_on": _runs_out_on(on_hand, avg),
        "open_print_orders": [
            {
                "name": o.get("name"),
                "qty": _int(o.get("qty")),
                "status": o.get("status"),
                "requested_on": str(o.get("requested_on") or "") or None,
                "expected_ready_date": str(o.get("expected_ready_date") or "") or None,
                "printer_name": o.get("printer_name"),
            }
            for o in open_orders
        ],
        "last_movement_on": usage.get("last_movement_on"),
        "last_counted_on": str(label_row.get("last_counted_on") or "") or None,
        "notes": label_row.get("notes"),
    }


def _runs_out_on(on_hand: int, avg_daily_usage: float) -> Optional[str]:
    """Projected stock-out date, or None when usage is unknown."""
    if avg_daily_usage <= 0 or on_hand <= 0:
        return None
    from frappe.utils import add_days

    try:
        return str(add_days(_today(), int(on_hand / avg_daily_usage)))
    except Exception:
        return None


def write_snapshot(snapshot: Dict[str, Any]) -> None:
    """Persist an already-computed snapshot onto its label's cached columns.

    Split out from ``refresh_label`` so a caller that has just built a snapshot
    can write *that* one rather than compute a second. It is not only cheaper:
    recomputing would leave the status written to the row free to disagree with
    the status the caller acted on, which for the daily alert means announcing
    one thing and recording another.

    Writes are per-field with ``update_modified=False`` so a refresh never shows
    up as an edit in the version history.
    """
    name = snapshot.get("name")
    if not name:
        return
    updates = {
        "on_hand_qty": snapshot["on_hand_qty"],
        "avg_daily_usage": snapshot["avg_daily_usage"],
        "days_of_cover": snapshot["days_of_cover"] or 0,
        "status": snapshot["status"],
        "last_movement_on": snapshot["last_movement_on"],
    }
    for field, value in updates.items():
        frappe.db.set_value(LABEL_DOCTYPE, name, field, value, update_modified=False)


def refresh_label(label_name: str) -> Optional[Dict[str, Any]]:
    """Recompute the cached columns on one label and write them back.

    The cached values exist only so Desk list views and reports can sort and
    filter; every read path recomputes from the ledger anyway.
    """
    if not frappe or not label_name or not _doctype_exists(LABEL_DOCTYPE):
        return None
    try:
        row = frappe.db.get_value(
            LABEL_DOCTYPE,
            label_name,
            [
                "name", "customer", "customer_name", "label_title", "enabled", "we_print",
                "applies_to_item_group", "labels_per_unit", "min_stock_qty", "reorder_qty",
                "last_counted_on", "notes",
            ],
            as_dict=True,
        )
        if not row:
            return None

        snapshot = build_snapshot(row)
        write_snapshot(snapshot)
        return snapshot
    except Exception:
        frappe.log_error(frappe.get_traceback(), f"label_stock: refresh failed for {label_name}")
        return None


# ---------------------------------------------------------------------------
# Ledger writes
# ---------------------------------------------------------------------------
def post_movement(
    *,
    label: str,
    movement_type: str,
    qty: int,
    posting_date: Any = None,
    reference_doctype: Optional[str] = None,
    reference_name: Optional[str] = None,
    print_order: Optional[str] = None,
    remarks: Optional[str] = None,
    refresh: bool = True,
) -> Optional[str]:
    """Insert one ledger row and refresh the label. Returns the movement name.

    *qty* is signed by the caller for ``Adjustment`` and ``Opening``; for the
    fixed-sign types the sign is forced here, so a caller cannot post a positive
    ``Consumed``.
    """
    if not frappe or not label:
        return None
    qty = _int(qty)
    if qty == 0:
        return None

    if movement_type in _POSITIVE_TYPES:
        qty = abs(qty)
    elif movement_type in _NEGATIVE_TYPES:
        qty = -abs(qty)

    doc = frappe.new_doc(MOVEMENT_DOCTYPE)
    doc.label = label
    doc.movement_type = movement_type
    doc.qty = qty
    doc.posting_date = posting_date or _today()
    if reference_doctype:
        doc.reference_doctype = reference_doctype
    if reference_name:
        doc.reference_name = reference_name
    if print_order:
        doc.print_order = print_order
    if remarks:
        doc.remarks = remarks
    doc.flags.ignore_permissions = True
    doc.insert(ignore_permissions=True)

    if refresh:
        refresh_label(label)
    return doc.name


# ---------------------------------------------------------------------------
# Sales Invoice consumption
# ---------------------------------------------------------------------------
def labels_for_customer(customer: str) -> List[Dict[str, Any]]:
    """Enabled, we-print labels for *customer*, catch-all (no item group) last."""
    if not customer or not _doctype_exists(LABEL_DOCTYPE):
        return []
    try:
        rows = frappe.get_all(
            LABEL_DOCTYPE,
            filters={"customer": customer, "enabled": 1, "we_print": 1},
            fields=["name", "label_title", "applies_to_item_group", "labels_per_unit"],
            order_by="creation asc",
            limit_page_length=0,
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), f"label_stock: label lookup failed for {customer}")
        return []
    grouped = [r for r in rows if (r.get("applies_to_item_group") or "").strip()]
    catch_all = [r for r in rows if not (r.get("applies_to_item_group") or "").strip()]
    return grouped + catch_all


def invoice_label_usage(doc: Any, labels: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    """Labels consumed per label name for one invoice.

    Bundle *parent* rows are skipped: they carry the bundle SKU at a 100%
    discount while the physical jars are the child rows underneath, so counting
    both would double every bundled order. That is the same rule
    ``services/consumable_deduction`` gets for free by only counting real jar
    groups.

    Rows are claimed by the label whose ``applies_to_item_group`` matches; a
    single label with no item group is the catch-all for everything left over.
    ``JarzCustomerLabel.validate`` guarantees at most one catch-all per customer,
    so a row can never be billed to two labels.
    """
    by_group: Dict[str, Dict[str, Any]] = {}
    catch_all: Optional[Dict[str, Any]] = None
    for label in labels:
        group = (label.get("applies_to_item_group") or "").strip()
        if group:
            by_group.setdefault(group, label)
        elif catch_all is None:
            catch_all = label

    usage: Dict[str, int] = {}
    for item in getattr(doc, "items", None) or []:
        if _int(getattr(item, "is_bundle_parent", 0)):
            continue
        qty = _float(getattr(item, "qty", 0))
        if not qty:
            continue
        group = str(getattr(item, "item_group", "") or "").strip()
        label = by_group.get(group) or catch_all
        if not label:
            continue
        per_unit = _float(label.get("labels_per_unit"), 1.0) or 1.0
        # Return invoices carry negative qty, which flows straight through as a
        # positive movement -- the labels come back on the jars that came back.
        usage[label["name"]] = usage.get(label["name"], 0) + int(round(qty * per_unit))
    return {name: qty for name, qty in usage.items() if qty}


def _movements_for_invoice(invoice_name: str) -> List[Dict[str, Any]]:
    try:
        return frappe.get_all(
            MOVEMENT_DOCTYPE,
            filters={"reference_doctype": "Sales Invoice", "reference_name": invoice_name},
            fields=["name", "label", "qty", "movement_type"],
            limit_page_length=0,
        )
    except Exception:
        return []


def consume_labels_on_invoice_submit(doc: Any, method: Optional[str] = None) -> None:
    """Draw down label stock when a Sales Invoice for a label customer is submitted.

    Deliberately on submit rather than on the Out-for-Delivery transition (where
    ``consumable_deduction`` sits): a B2B supply order is not guaranteed to travel
    the dispatch kanban, and an invoice that never reached OFD would silently
    never consume a label -- the exact shape of the bug that hid a v16 rejection
    for a month. Submission is the one event every sale has.

    Fast-exits on the first query for every B2C invoice, is idempotent against a
    double fire, and never raises: a label ledger must not be able to block a sale.
    """
    if not frappe or not doc or not getattr(doc, "name", None):
        return
    try:
        if not get_label_settings()["auto_consume"]:
            return
        customer = str(getattr(doc, "customer", "") or "").strip()
        if not customer:
            return
        labels = labels_for_customer(customer)
        if not labels:
            return  # the overwhelmingly common case: not a printed-label customer
        if _movements_for_invoice(doc.name):
            return  # already consumed on an earlier fire

        usage = invoice_label_usage(doc, labels)
        if not usage:
            return

        is_return = bool(_int(getattr(doc, "is_return", 0)))
        verb = "returned on" if is_return else "consumed by"
        for label_name, qty in usage.items():
            post_movement(
                label=label_name,
                movement_type="Adjustment" if is_return else "Consumed",
                qty=qty if is_return else -abs(qty),
                posting_date=getattr(doc, "posting_date", None),
                reference_doctype="Sales Invoice",
                reference_name=doc.name,
                remarks=f"Auto: {abs(qty)} label(s) {verb} {doc.name}",
            )
    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            f"label_stock: consumption failed for {getattr(doc, 'name', '?')}",
        )


def reverse_labels_on_invoice_cancel(doc: Any, method: Optional[str] = None) -> None:
    """Put the labels back when a consumed invoice is cancelled.

    Posts a compensating movement rather than deleting the original: the ledger is
    an audit trail, and labels already applied to jars for an order that was later
    cancelled is a real thing that happened.

    Idempotent by construction -- it compensates the *net* of every movement that
    references the invoice, so a second cancel sees a net of zero and does nothing.
    """
    if not frappe or not doc or not getattr(doc, "name", None):
        return
    try:
        rows = _movements_for_invoice(doc.name)
        if not rows:
            return

        net_by_label: Dict[str, int] = {}
        for row in rows:
            label_name = row.get("label")
            if not label_name:
                continue
            net_by_label[label_name] = net_by_label.get(label_name, 0) + _int(row.get("qty"))

        for label_name, net in net_by_label.items():
            if net == 0:
                continue  # already compensated
            post_movement(
                label=label_name,
                movement_type="Adjustment",
                qty=-net,
                reference_doctype="Sales Invoice",
                reference_name=doc.name,
                remarks=f"Auto: reversed {abs(net)} label(s) -- {doc.name} cancelled",
            )
    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            f"label_stock: reversal failed for {getattr(doc, 'name', '?')}",
        )


# ---------------------------------------------------------------------------
# Dashboard / alerting
# ---------------------------------------------------------------------------
def list_label_snapshots(
    *,
    customer: Optional[str] = None,
    include_untracked: bool = True,
    only_attention: bool = False,
) -> List[Dict[str, Any]]:
    """Snapshots for every label, most urgent first.

    The list is small by nature -- one row per (B2B customer, label design) -- so
    it is built in Python rather than pushed into SQL, which keeps ``compute_status``
    the only place the thresholds live.
    """
    if not _doctype_exists(LABEL_DOCTYPE):
        return []

    filters: Dict[str, Any] = {}
    if customer:
        filters["customer"] = customer
    if not include_untracked:
        filters["enabled"] = 1
        filters["we_print"] = 1

    try:
        rows = frappe.get_all(
            LABEL_DOCTYPE,
            filters=filters,
            fields=[
                "name", "customer", "customer_name", "label_title", "enabled", "we_print",
                "applies_to_item_group", "labels_per_unit", "min_stock_qty", "reorder_qty",
                "last_counted_on", "notes",
            ],
            order_by="customer_name asc, label_title asc",
            limit_page_length=0,
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), "label_stock: label list failed")
        return []

    settings = get_label_settings()
    snapshots = [build_snapshot(row, settings=settings) for row in rows or []]
    if only_attention:
        snapshots = [s for s in snapshots if s["needs_attention"]]

    order = {
        STATUS_OUT_OF_STOCK: 0,
        STATUS_REORDER_NOW: 1,
        STATUS_REORDER_SOON: 2,
        STATUS_ON_ORDER: 3,
        STATUS_OK: 4,
        STATUS_NOT_TRACKED: 5,
    }
    snapshots.sort(
        key=lambda s: (
            order.get(s["status"], 9),
            s["days_of_cover"] if s["days_of_cover"] is not None else 10_000,
            (s["customer_name"] or "").lower(),
        )
    )
    return snapshots


def summarise(snapshots: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    """Counts per status, for the dashboard header and the alert badge."""
    summary = {
        "total": 0,
        "tracked": 0,
        "out_of_stock": 0,
        "reorder_now": 0,
        "reorder_soon": 0,
        "on_order": 0,
        "ok": 0,
        "not_tracked": 0,
        "needs_attention": 0,
    }
    key_by_status = {
        STATUS_OUT_OF_STOCK: "out_of_stock",
        STATUS_REORDER_NOW: "reorder_now",
        STATUS_REORDER_SOON: "reorder_soon",
        STATUS_ON_ORDER: "on_order",
        STATUS_OK: "ok",
        STATUS_NOT_TRACKED: "not_tracked",
    }
    for snap in snapshots:
        summary["total"] += 1
        if snap.get("tracked"):
            summary["tracked"] += 1
        key = key_by_status.get(snap.get("status"))
        if key:
            summary[key] += 1
        if snap.get("needs_attention"):
            summary["needs_attention"] += 1
    return summary


def _alert_recipients() -> List[str]:
    """Enabled users who should hear about a label shortage.

    The B2B rep owns the customer relationship and the manager owns the print
    budget, so both get the alert.
    """
    users: Set[str] = set()
    try:
        rows = frappe.get_all(
            "Has Role",
            filters={
                "role": ["in", ["JARZ Manager", "B2B Sales Rep", "System Manager"]],
                "parenttype": "User",
            },
            pluck="parent",
        )
    except Exception:
        return []
    # Administrator is deliberately NOT filtered out, matching
    # tasks._get_online_payment_alert_recipients: on a site where it holds the
    # only manager role, excluding it would mean the alert reached nobody at all
    # and did so silently.
    for user in rows or []:
        if user and user != "Guest":
            users.add(user)

    recipients: List[str] = []
    for user in sorted(users):
        try:
            if frappe.db.get_value("User", user, "enabled"):
                recipients.append(user)
        except Exception:
            continue
    return recipients


def _alert_message(snap: Dict[str, Any]) -> str:
    bits = [
        f"{snap['customer_name']} - {snap['label_title']}: {snap['on_hand_qty']} label(s) left.",
    ]
    if snap.get("days_of_cover") is not None:
        bits.append(f"About {snap['days_of_cover']} day(s) of cover at {snap['avg_daily_usage']}/day.")
    if snap.get("runs_out_on"):
        bits.append(f"Projected to run out on {snap['runs_out_on']}.")
    bits.append(
        f"Printing takes {snap['lead_days_min']}-{snap['lead_days_max']} working days "
        f"({snap['rest_day']} excluded); a batch ordered today is ready "
        f"{snap['expected_ready_if_ordered_today']}."
    )
    if snap.get("suggested_print_qty"):
        bits.append(f"Suggested print quantity: {snap['suggested_print_qty']}.")
    return " ".join(bits)


def run_label_stock_alerts() -> Dict[str, Any]:
    """Daily: refresh every label and alert on the ones that need printing.

    Returns a small summary so the job is inspectable from ``bench execute``.
    Never raises out of the scheduler.
    """
    result = {"checked": 0, "alerted": 0, "recipients": 0, "skipped": []}
    if not frappe:
        return result
    try:
        settings = get_label_settings()
        if not settings["alerts_enabled"]:
            result["skipped"].append("alerts disabled in Jarz POS Settings")
            return result
        if not _doctype_exists(LABEL_DOCTYPE):
            result["skipped"].append("label doctype not installed")
            return result

        snapshots = list_label_snapshots(include_untracked=False)
        result["checked"] = len(snapshots)

        # Keep the cached Desk columns honest even for labels that are fine.
        # Writing the snapshot already in hand, rather than recomputing, is what
        # guarantees the row ends up carrying the same status this pass alerts on.
        for snap in snapshots:
            try:
                write_snapshot(snap)
            except Exception:
                continue

        today = _today()
        due = []
        for snap in snapshots:
            if not snap["needs_attention"]:
                continue
            last_alert = frappe.db.get_value(LABEL_DOCTYPE, snap["name"], "last_alert_on")
            if last_alert:
                from frappe.utils import getdate

                try:
                    if getdate(last_alert) >= today:
                        continue  # already announced today
                except Exception:
                    pass
            due.append(snap)

        if not due:
            return result

        recipients = _alert_recipients()
        result["recipients"] = len(recipients)

        for snap in due:
            message = _alert_message(snap)
            subject = f"Labels running low: {snap['customer_name']} ({snap['status']})"
            for user in recipients:
                try:
                    note = frappe.new_doc("Notification Log")
                    note.subject = subject
                    note.email_content = message
                    note.for_user = user
                    note.type = "Alert"
                    note.document_type = LABEL_DOCTYPE
                    note.document_name = snap["name"]
                    note.insert(ignore_permissions=True)
                except Exception:
                    continue
            _publish_alert(snap, recipients)
            try:
                frappe.db.set_value(
                    LABEL_DOCTYPE, snap["name"], "last_alert_on", today, update_modified=False
                )
            except Exception:
                pass
            result["alerted"] += 1

        try:
            frappe.db.commit()
        except Exception:
            pass
        return result
    except Exception:
        frappe.log_error(frappe.get_traceback(), "label_stock: daily alert run failed")
        return result


def _publish_alert(snap: Dict[str, Any], recipients: Sequence[str]) -> None:
    """Push the alert to connected clients, one addressed room per user.

    A bare ``publish_realtime`` lands in the site-wide ``all`` room and a
    ``user="*"`` call addresses a room nobody joins, so each recipient is
    published to by name.
    """
    try:
        from jarz_pos.constants import WS_EVENTS

        event = getattr(WS_EVENTS, "LABEL_STOCK_ALERT", "jarz_pos_label_stock_alert")
    except Exception:
        event = "jarz_pos_label_stock_alert"

    payload = {
        "label": snap.get("name"),
        "customer": snap.get("customer"),
        "customer_name": snap.get("customer_name"),
        "label_title": snap.get("label_title"),
        "status": snap.get("status"),
        "on_hand_qty": snap.get("on_hand_qty"),
        "days_of_cover": snap.get("days_of_cover"),
        "suggested_print_qty": snap.get("suggested_print_qty"),
        "expected_ready_if_ordered_today": snap.get("expected_ready_if_ordered_today"),
    }
    for user in recipients:
        try:
            frappe.publish_realtime(event, payload, user=user)
        except Exception:
            continue
