"""SOP / work-instruction API for the Production Board.

Read path for the phone at the bench: given an item (or a Work Order), return
the procedure with every quantity already scaled to the run actually being
made, plus the capture points the operator has to fill in.

Layering matches ``api/production.py``: the maths lives in
``services/sop_rendering.py`` (pure, frappe-free) and everything that touches
the database sits behind a ``_resolve_x()`` accessor so tests patch one symbol
instead of the world.

Two behaviours here are load-bearing and easy to break later:

1. ``get_sop_for_item`` **never raises** for a missing or broken SOP.  The
   board calls it per item and most items have no SOP; a throw would blank the
   whole screen instead of one card.  Failures degrade to ``has_sop: False``
   and are logged, never swallowed silently.
2. ``get_sop_for_work_order`` returns the SOP version **stamped on the Work
   Order**, not today's active one.  Without that, opening a batch run from
   last month would show instructions that were written after it finished.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Optional, Tuple

import frappe
from frappe import _

from jarz_pos.constants import ROLES
from jarz_pos.services import sop_rendering as rendering

SOP_DOCTYPE = "Jarz SOP"
EXECUTION_LOG_DOCTYPE = "Jarz SOP Execution Log"

# The Data field another part of the app stamps onto a Work Order when it is
# submitted.  Read defensively: this API ships alongside the field, and a site
# that has the code but not yet the migration must degrade, not 500.
WORK_ORDER_STAMP_FIELD = "jarz_sop_version"
_WORK_ORDER_FIELDS = ["name", "production_item", "qty", "bom_no", "company", WORK_ORDER_STAMP_FIELD]

_NAIVE_TAG_RE = re.compile(r"<[^>]+>")


# ── Access ──────────────────────────────────────────────────────────────


def _ensure_production_view_access() -> None:
    """Same gate as the rest of the Production Board.

    ``ROLES.PRODUCTION_VIEW`` is deliberately wider than ``ROLES.MANUFACTURING``
    — see the comment on the constant.
    """
    roles = set(frappe.get_roles())
    if not roles.intersection(ROLES.PRODUCTION_VIEW):
        frappe.throw(_("Not permitted: production access required"), frappe.PermissionError)


# ── Coercion helpers ────────────────────────────────────────────────────


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _field(row: Any, name: str, default: Any = None) -> Any:
    """Read a field from either a Document/child row or a plain dict."""
    if isinstance(row, dict):
        return row.get(name, default)
    return getattr(row, name, default)


# ── frappe / ERPNext resolvers ──────────────────────────────────────────


def _resolve_active_sop(item_code: str) -> Optional[str]:
    """Name of the live SOP for an item, or ``None``.

    Highest ``version`` wins; ``modified`` breaks a tie.  ``on_update`` on the
    DocType keeps at most one row active per item, so this is normally a
    single-row read — the ordering is belt-and-braces for a site where two rows
    were activated by a direct database edit.
    """
    rows = frappe.get_all(
        SOP_DOCTYPE,
        filters={"item_code": item_code, "is_active": 1},
        fields=["name", "version"],
        order_by="version desc, modified desc",
        limit=1,
    )
    return rows[0]["name"] if rows else None


def _resolve_sop_doc(sop_name: str):
    return frappe.get_doc(SOP_DOCTYPE, sop_name)


def _resolve_required_material_rows():
    """Deferred import: keeps this module off ``api/manufacturing`` at import."""
    from jarz_pos.api.manufacturing import _get_required_material_rows

    return _get_required_material_rows


def _resolve_strip_html():
    return frappe.utils.strip_html


def _resolve_now_datetime():
    return frappe.utils.now_datetime()


def _resolve_session_user() -> Optional[str]:
    try:
        return frappe.session.user
    except Exception:
        return None


def _resolve_work_order_row(work_order: str) -> Optional[Dict[str, Any]]:
    """Read the Work Order, tolerating a site without the stamp field yet."""
    try:
        return frappe.db.get_value("Work Order", work_order, _WORK_ORDER_FIELDS, as_dict=True)
    except Exception:
        frappe.log_error(
            title="JARZ SOP – Work Order read fell back (stamp field missing?)",
            message=f"work_order={work_order}\n{frappe.get_traceback()}",
        )
        return frappe.db.get_value("Work Order", work_order, _WORK_ORDER_FIELDS[:-1], as_dict=True)


def _resolve_bom_row(bom: str) -> Optional[Dict[str, Any]]:
    if not bom:
        return None
    try:
        return frappe.db.get_value("BOM", bom, ["name", "quantity", "company"], as_dict=True)
    except Exception:
        frappe.log_error(title="JARZ SOP – BOM read failed", message=f"bom={bom}")
        return None


def _resolve_default_bom(item_code: str) -> Optional[str]:
    try:
        return frappe.db.get_value(
            "BOM", {"item": item_code, "is_default": 1, "docstatus": 1}, "name"
        )
    except Exception:
        return None


# ── Rendering plumbing ──────────────────────────────────────────────────


def _make_plain_text_fn():
    """Server-side HTML stripper, with a regex fallback.

    The app renders ``instruction_text`` and must never need an HTML package to
    read a procedure.  A failure here degrades to a crude tag strip rather than
    to an empty instruction, and always logs — an unreadable step is a safety
    problem, a silently blank one is worse.
    """
    try:
        strip = _resolve_strip_html()
    except Exception:
        frappe.log_error(
            title="JARZ SOP – strip_html unavailable",
            message=frappe.get_traceback(),
        )
        strip = None

    def to_text(html: Any) -> str:
        raw = html or ""
        if strip is not None:
            try:
                return (strip(raw) or "").strip()
            except Exception:
                frappe.log_error(
                    title="JARZ SOP – strip_html failed, using naive fallback",
                    message=frappe.get_traceback(),
                )
        return _NAIVE_TAG_RE.sub("", str(raw)).strip()

    return to_text


def _step_rows(doc: Any) -> List[Dict[str, Any]]:
    """Flatten the ``steps`` child table into plain dicts.

    Uses ``getattr`` rather than ``as_dict()`` so the same code path works for
    a real ``Document`` and for a lightweight stub in a unit test.
    """
    rows: List[Dict[str, Any]] = []
    for row in (_field(doc, "steps") or []):
        rows.append(
            {
                "step_no": _field(row, "step_no"),
                "idx": _field(row, "idx"),
                "title": _field(row, "title"),
                "instruction": _field(row, "instruction"),
                "image": _field(row, "image"),
                "duration_mins": _field(row, "duration_mins"),
                "scaling_mode": _field(row, "scaling_mode"),
                "requires_confirmation": _field(row, "requires_confirmation"),
                "capture_type": _field(row, "capture_type"),
                "capture_label": _field(row, "capture_label"),
                "capture_min": _field(row, "capture_min"),
                "capture_max": _field(row, "capture_max"),
            }
        )
    return rows


def _sop_to_dict(doc: Any) -> Dict[str, Any]:
    return {
        "name": _field(doc, "name"),
        "item_code": _field(doc, "item_code"),
        "item_name": _field(doc, "item_name"),
        "bom": _field(doc, "bom"),
        "version": _field(doc, "version"),
        "yield_percent": _field(doc, "yield_percent"),
        "prep_time_mins": _field(doc, "prep_time_mins"),
        "equipment": _field(doc, "equipment"),
        "steps": _step_rows(doc),
    }


def _build_component_maps(
    bom: Optional[str], company: Optional[str], bom_qty: float
) -> Tuple[Dict[str, float], Dict[str, str], Dict[str, str]]:
    """Per-**batch** component quantities, keyed by item code.

    ``render_instruction`` multiplies by the batch count, so these rows are
    always exploded for exactly one BOM quantity.  A BOM that will not explode
    degrades to empty maps: the tokens then come back verbatim and land in
    ``unresolved_tokens``, which is visibly broken rather than quietly wrong.
    """
    qty_map: Dict[str, float] = {}
    uom_map: Dict[str, str] = {}
    name_map: Dict[str, str] = {}

    if not bom:
        return qty_map, uom_map, name_map

    try:
        rows = _resolve_required_material_rows()(bom, company, bom_qty) or []
    except Exception:
        frappe.log_error(
            title="JARZ SOP – BOM explosion failed",
            message=f"bom={bom} company={company}\n{frappe.get_traceback()}",
        )
        return qty_map, uom_map, name_map

    for row in rows:
        code = str(_field(row, "item_code") or "").strip()
        if not code:
            continue
        # An exploded BOM can list the same raw material twice; the instruction
        # must quote the total, not whichever line happened to come last.
        qty_map[code] = qty_map.get(code, 0.0) + rendering.to_float(_field(row, "required_qty"), 0.0)
        uom_map[code] = _field(row, "uom") or ""
        name_map[code] = _field(row, "item_name") or code

    return qty_map, uom_map, name_map


def _build_payload(
    doc: Any,
    *,
    item_code: str,
    bom: Optional[str] = None,
    batches: Any = 1,
    units: Any = None,
    work_order: Optional[str] = None,
    stamp: Optional[str] = None,
    is_stamped: bool = False,
) -> Dict[str, Any]:
    sop = _sop_to_dict(doc)

    resolved_bom = (bom or "").strip() or sop.get("bom") or _resolve_default_bom(item_code)
    bom_row = _resolve_bom_row(resolved_bom) or {}
    company = bom_row.get("company")
    bom_qty = rendering.to_float(bom_row.get("quantity"), 1.0) or 1.0

    batch_count = rendering.to_float(batches, 1.0)
    # Unstated ``units`` means "whatever this many batches yields".  ``to_float``
    # keeps an explicit 0 as 0 rather than folding it into the default.
    unit_count = rendering.to_float(units, batch_count * bom_qty)

    qty_map, uom_map, name_map = _build_component_maps(resolved_bom, company, bom_qty)

    rendered = rendering.render_sop(
        sop,
        batches=batch_count,
        units=unit_count,
        component_qty_map=qty_map,
        uom_map=uom_map,
        name_map=name_map,
    )

    to_text = _make_plain_text_fn()
    for step in rendered["steps"]:
        step["instruction_text"] = to_text(step.get("instruction_html"))

    version = _coerce_int(sop.get("version"), 1)
    payload = {
        "has_sop": True,
        "sop": sop.get("name"),
        "version": version,
        "item_code": sop.get("item_code") or item_code,
        "item_name": sop.get("item_name") or sop.get("item_code") or item_code,
        "bom": resolved_bom,
        "yield_percent": rendering.to_float(sop.get("yield_percent"), 100.0),
        "prep_time_mins": _coerce_int(sop.get("prep_time_mins"), 0),
        "equipment": sop.get("equipment") or None,
        "batches": rendered["batches"],
        "units": rendered["units"],
        "total_duration_mins": rendered["total_duration_mins"],
        "steps": rendered["steps"],
        "unresolved_tokens": rendered["unresolved_tokens"],
    }

    if work_order:
        payload["work_order"] = work_order
        payload["is_stamped"] = bool(is_stamped)
        payload["sop_version_stamp"] = stamp or rendering.format_version_stamp(sop.get("name"), version)

    return payload


def _resolve_sop_for_work_order(wo: Mapping[str, Any]) -> Tuple[Any, Optional[str], bool]:
    """``(doc, stamp, is_stamped)`` for a Work Order.

    The stamped version wins over the active one.  This is the whole point of
    stamping: editing an SOP must not rewrite the history of batches already
    run.  A stamp that no longer resolves (the row was deleted) falls back to
    the active SOP and is logged, because losing the procedure entirely is
    worse than showing a newer one.
    """
    stamp = str(wo.get(WORK_ORDER_STAMP_FIELD) or "").strip()
    stamped_name, _stamped_version = rendering.parse_version_stamp(stamp)

    if stamped_name:
        try:
            return _resolve_sop_doc(stamped_name), stamp, True
        except Exception:
            frappe.log_error(
                title="JARZ SOP – stamped version no longer resolves",
                message=f"work_order={wo.get('name')} stamp={stamp}",
            )

    item_code = wo.get("production_item")
    active = _resolve_active_sop(item_code) if item_code else None
    if not active:
        return None, None, False
    return _resolve_sop_doc(active), None, False


# ── Whitelisted endpoints ───────────────────────────────────────────────


@frappe.whitelist()
def get_sop_for_item(
    item_code: str,
    bom: Optional[str] = None,
    batches: Any = 1,
    units: Any = None,
) -> Dict[str, Any]:
    """The active SOP for an item, scaled to ``batches``.

    **Never raises for a missing or unrenderable SOP.**  The board calls this
    for every card and most items have no procedure; the caller gets
    ``{"has_sop": False, "item_code": ...}`` and renders a plain card.
    Permission failures still raise — those are not a per-item condition.
    """
    _ensure_production_view_access()

    item_code = (item_code or "").strip()
    if not item_code:
        return {"has_sop": False, "item_code": item_code}

    try:
        sop_name = _resolve_active_sop(item_code)
    except Exception:
        frappe.log_error(
            title="JARZ SOP – active SOP lookup failed",
            message=f"item_code={item_code}\n{frappe.get_traceback()}",
        )
        return {"has_sop": False, "item_code": item_code}

    if not sop_name:
        return {"has_sop": False, "item_code": item_code}

    try:
        doc = _resolve_sop_doc(sop_name)
        return _build_payload(doc, item_code=item_code, bom=bom, batches=batches, units=units)
    except Exception:
        # Degrade to "no SOP" rather than to a broken screen — but never
        # silently: a procedure that cannot render is a real defect.
        frappe.log_error(
            title="JARZ SOP – render failed",
            message=f"item_code={item_code} sop={sop_name}\n{frappe.get_traceback()}",
        )
        return {"has_sop": False, "item_code": item_code}


@frappe.whitelist()
def get_sop_for_work_order(work_order: str) -> Dict[str, Any]:
    """The SOP as stamped on a Work Order, scaled to that order's quantity.

    ``batches`` is ``WO.qty / BOM.quantity`` and ``units`` is ``WO.qty``, so a
    Work Order for 30 units off a BOM that yields 10 renders every quantity at
    three times the authored figure.
    """
    _ensure_production_view_access()

    work_order = (work_order or "").strip()
    if not work_order:
        frappe.throw(_("work_order is required"))

    wo = _resolve_work_order_row(work_order)
    if not wo:
        frappe.throw(_("Work Order {0} not found").format(work_order))

    doc, stamp, is_stamped = _resolve_sop_for_work_order(wo)
    item_code = wo.get("production_item") or ""
    if doc is None:
        return {"has_sop": False, "item_code": item_code, "work_order": work_order}

    bom = wo.get("bom_no")
    bom_row = _resolve_bom_row(bom) or {}
    bom_qty = rendering.to_float(bom_row.get("quantity"), 1.0) or 1.0
    order_qty = rendering.to_float(wo.get("qty"), 0.0)

    return _build_payload(
        doc,
        item_code=item_code,
        bom=bom,
        batches=order_qty / bom_qty,
        units=order_qty,
        work_order=work_order,
        stamp=stamp,
        is_stamped=is_stamped,
    )


@frappe.whitelist()
def record_sop_step_capture(
    work_order: str,
    step_no: Any,
    value: Any = None,
    file_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Log an operator's reading, photo, or confirmation for one step.

    Writes a ``Jarz SOP Execution Log`` row.  Without the log, capture points
    are write-only theatre: the operator types a temperature, the screen says
    thank you, and nothing on earth can tell you later what it was.
    """
    _ensure_production_view_access()

    work_order = (work_order or "").strip()
    if not work_order:
        frappe.throw(_("work_order is required"))

    step_number = _coerce_int(step_no, 0)
    if step_number <= 0:
        frappe.throw(_("A valid step_no is required"))

    wo = _resolve_work_order_row(work_order)
    if not wo:
        frappe.throw(_("Work Order {0} not found").format(work_order))

    doc, stamp, _is_stamped = _resolve_sop_for_work_order(wo)
    if doc is None:
        frappe.throw(_("No SOP is linked to Work Order {0}").format(work_order))

    step = next((s for s in _step_rows(doc) if _coerce_int(s.get("step_no"), 0) == step_number), None)
    if step is None:
        frappe.throw(_("Step {0} does not exist on this SOP").format(step_number))

    capture_type = str(step.get("capture_type") or rendering.CAPTURE_NONE).strip() or rendering.CAPTURE_NONE
    value_float: Optional[float] = None
    value_text: Optional[str] = None
    attachment: Optional[str] = (file_url or "").strip() or None

    if capture_type in rendering.NUMERIC_CAPTURE_TYPES:
        if value is None or str(value).strip() == "":
            frappe.throw(_("Step {0} requires a reading").format(step_number))
        try:
            value_float = float(str(value).strip())
        except (TypeError, ValueError):
            frappe.throw(_("Step {0} expects a number, got {1}").format(step_number, value))

        low = rendering.to_float(step.get("capture_min"), 0.0)
        high = rendering.to_float(step.get("capture_max"), 0.0)
        # A range is only enforced when max > min.  Both left at zero is the
        # DocType default and means "no bounds"; guessing otherwise would
        # reject every reading on every step nobody configured.
        if high > low and not (low <= value_float <= high):
            frappe.throw(
                _("Step {0}: {1} is outside the accepted range {2} – {3}").format(
                    step_number, value_float, low, high
                )
            )
    elif capture_type == rendering.CAPTURE_PHOTO:
        if not attachment:
            frappe.throw(_("Step {0} requires a photo").format(step_number))
    else:
        # ``None`` capture type: this is a plain confirmation, but keep any
        # free-text the client sent rather than discarding it.
        if value is not None and str(value).strip() != "":
            value_text = str(value).strip()

    log = frappe.get_doc(
        {
            "doctype": EXECUTION_LOG_DOCTYPE,
            "work_order": work_order,
            "sop": _field(doc, "name"),
            "sop_version": stamp
            or rendering.format_version_stamp(_field(doc, "name"), _field(doc, "version")),
            "step_no": step_number,
            "title": step.get("title"),
            "captured_by": _resolve_session_user(),
            "captured_at": _resolve_now_datetime(),
            "capture_type": capture_type,
            "value_float": value_float,
            "value_text": value_text,
            "attachment": attachment,
        }
    )
    log.insert(ignore_permissions=True)

    return {
        "ok": True,
        "name": _field(log, "name"),
        "work_order": work_order,
        "sop": _field(doc, "name"),
        "step_no": step_number,
        "capture_type": capture_type,
        "value_float": value_float,
        "value_text": value_text,
        "attachment": attachment,
    }


@frappe.whitelist()
def list_sops(item_code: Optional[str] = None, active_only: Any = 1) -> Dict[str, Any]:
    """Catalogue of SOPs, newest version first, for a picker or an admin list."""
    _ensure_production_view_access()

    filters: Dict[str, Any] = {}
    item_code = (item_code or "").strip()
    if item_code:
        filters["item_code"] = item_code
    if _coerce_int(active_only, 1):
        filters["is_active"] = 1

    rows = frappe.get_all(
        SOP_DOCTYPE,
        filters=filters,
        fields=[
            "name",
            "item_code",
            "item_name",
            "bom",
            "version",
            "is_active",
            "yield_percent",
            "prep_time_mins",
            "modified",
        ],
        order_by="item_name asc, version desc",
        limit_page_length=0,
    )

    return {"sops": [dict(r) for r in rows], "count": len(rows)}
