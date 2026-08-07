"""Daily Production Plan API — the morning target and the evening count.

Sits above ``services.daily_production_plan`` (the maths) and the
``Jarz Production Plan`` DocType (the record).  Nothing here writes stock or
GL: a plan is an intention, and turning it into Work Orders stays the job of
``api.manufacturing``.

``preview_plan`` deliberately computes without saving so the screen can update
the batch split on every keystroke without leaving a trail of draft documents.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate, nowdate

from jarz_pos.constants import ROLES
from jarz_pos.services import daily_production_plan as planning
from jarz_pos.services import production_planning as board

PLAN_DOCTYPE = "Jarz Production Plan"

# Item groups that represent a sellable finished jar.  Used to default the
# morning screen to the things somebody actually fills, rather than every item
# in the catalogue that happens to own a BOM.
FINISHED_GOODS_GROUPS = ("Medium", "Large")


def _ensure_view_access() -> None:
    roles = set(frappe.get_roles())
    if not roles.intersection(ROLES.PRODUCTION_VIEW):
        frappe.throw(_("Not permitted: production access required"), frappe.PermissionError)


def _ensure_execute_access() -> None:
    roles = set(frappe.get_roles())
    if not roles.intersection(ROLES.PRODUCTION_EXECUTE):
        frappe.throw(_("Not permitted: production access required"), frappe.PermissionError)


def _resolve_company(company: Optional[str]) -> str:
    company = (company or "").strip() or planning._resolve_default_company()
    if not company:
        frappe.throw(_("Company is not configured and no Default Company is set"))
    return company


def _coerce_lines(lines: Any) -> List[Dict[str, Any]]:
    if isinstance(lines, str):
        try:
            lines = json.loads(lines)
        except Exception:
            frappe.throw(_("Invalid JSON payload for lines"))
    if lines is None:
        return []
    if not isinstance(lines, list):
        frappe.throw(_("lines must be a list"))

    out: List[Dict[str, Any]] = []
    for i, row in enumerate(lines):
        if not isinstance(row, dict):
            frappe.throw(_("lines[{0}] must be an object").format(i))
        item_code = str(row.get("item_code") or "").strip()
        if not item_code:
            frappe.throw(_("lines[{0}] is missing item_code").format(i))

        actual = row.get("actual_qty")
        out.append(
            {
                "item_code": item_code,
                "planned_qty": cint(row.get("planned_qty") or 0),
                # Preserved as None rather than coerced to 0 — blank means
                # "not counted yet" all the way through the stack.
                "actual_qty": None if actual in (None, "") else cint(actual),
                "notes": row.get("notes") or None,
            }
        )
    return out


# ── Reads ───────────────────────────────────────────────────────────────


@frappe.whitelist()
def get_plan_template(company: Optional[str] = None, plan_date: Optional[str] = None) -> Dict[str, Any]:
    """Everything the morning screen needs to render an empty plan.

    Returns the fillable items with the mix each one consumes and the jars a
    single batch yields, plus today's existing plan if somebody already started
    one.
    """
    _ensure_view_access()
    company = _resolve_company(company)
    plan_date = (plan_date or "").strip() or nowdate()

    mix_item = planning._resolve_mix_item()
    batch_qty, mix_uom, mix_bom = planning._resolve_mix_batch_qty(mix_item)
    rows = planning._resolve_planable_rows(company, mix_item)

    items = []
    for row in rows:
        if row.get("item_group") not in FINISHED_GOODS_GROUPS:
            continue
        per_unit = flt(row.get("mix_qty_per_unit") or 0)
        items.append(
            {
                "item_code": row["item_code"],
                "item_name": row.get("item_name") or row["item_code"],
                "item_group": row.get("item_group"),
                "stock_uom": row.get("stock_uom"),
                "default_bom": row.get("default_bom"),
                "mix_qty_per_unit": per_unit,
                "jars_per_batch": planning.jars_per_batch(
                    batch_qty=batch_qty, mix_qty_per_unit=per_unit
                ),
                "uses_mix": per_unit > planning.QTY_EPSILON,
            }
        )

    return {
        "company": company,
        "plan_date": plan_date,
        "mix": {
            "item_code": mix_item,
            "default_bom": mix_bom,
            "batch_qty": batch_qty,
            "uom": mix_uom,
            "run_quality": [
                {"size": size, "quality": quality}
                for size, quality in sorted(planning._resolve_run_quality().items())
            ],
        },
        "items": items,
        "existing_plan": _find_open_plan(company, plan_date),
    }


def _find_open_plan(company: str, plan_date: str) -> Optional[str]:
    return frappe.db.get_value(
        PLAN_DOCTYPE,
        {"company": company, "plan_date": plan_date, "status": ["in", ("Draft", "Planned")]},
        "name",
    )


@frappe.whitelist()
def get_plan(name: str) -> Dict[str, Any]:
    """One saved plan, in the shape the screen renders."""
    _ensure_view_access()
    name = (name or "").strip()
    if not name:
        frappe.throw(_("name is required"))
    if not frappe.db.exists(PLAN_DOCTYPE, name):
        frappe.throw(_("Production plan {0} not found").format(name))

    return _serialise(frappe.get_doc(PLAN_DOCTYPE, name))


@frappe.whitelist()
def list_plans(
    company: Optional[str] = None,
    limit: Any = 30,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Recent plans, newest first — the history view."""
    _ensure_view_access()
    company = _resolve_company(company)

    filters: Dict[str, Any] = {"company": company}
    if from_date and to_date:
        filters["plan_date"] = ["between", [getdate(from_date), getdate(to_date)]]
    elif from_date:
        filters["plan_date"] = [">=", getdate(from_date)]
    elif to_date:
        filters["plan_date"] = ["<=", getdate(to_date)]

    return frappe.get_all(
        PLAN_DOCTYPE,
        filters=filters,
        fields=[
            "name", "plan_date", "status", "mixer_runs", "run_count",
            "required_batches", "planned_batches", "total_mix_qty",
            "total_planned_units", "total_actual_units",
            "realised_units_per_batch",
        ],
        order_by="plan_date desc, creation desc",
        limit_page_length=cint(limit) or 30,
    )


@frappe.whitelist()
def preview_plan(
    lines: Any,
    company: Optional[str] = None,
    include_materials: Any = 1,
) -> Dict[str, Any]:
    """Compute the batch split for a set of jar quantities without saving.

    This is what the morning screen calls while somebody is still typing.  It
    also rolls up the raw-material demand so a shortage shows up before the
    mixer is switched on rather than halfway through the day.
    """
    _ensure_view_access()
    company = _resolve_company(company)
    parsed = _coerce_lines(lines)

    mix_item = planning._resolve_mix_item()
    batch_qty, mix_uom, mix_bom = planning._resolve_mix_batch_qty(mix_item)
    rows = {r["item_code"]: r for r in planning._resolve_planable_rows(company, mix_item)}

    demand_lines = []
    for row in parsed:
        source = rows.get(row["item_code"]) or {}
        demand_lines.append(
            {
                "item_code": row["item_code"],
                "item_name": source.get("item_name") or row["item_code"],
                "planned_qty": row["planned_qty"],
                "mix_qty_per_unit": flt(source.get("mix_qty_per_unit") or 0),
            }
        )

    demand = planning.aggregate_mix_demand(demand_lines)
    required = planning.batches_needed(
        total_mix_qty=demand["total_mix_qty"], batch_qty=batch_qty
    )
    split = planning.plan_mixer_runs(required, run_quality=planning._resolve_run_quality())

    payload: Dict[str, Any] = {
        "company": company,
        "mix": {
            "item_code": mix_item,
            "default_bom": mix_bom,
            "batch_qty": batch_qty,
            "uom": mix_uom,
        },
        "total_mix_qty": demand["total_mix_qty"],
        "breakdown": demand["breakdown"],
        "required_batches": split["required_batches"],
        "planned_batches": split["planned_batches"],
        "runs": split["runs"],
        "run_detail": split["run_detail"],
        "run_count": split["run_count"],
        "overproduction_batches": split["overproduction_batches"],
        "capped": split["capped"],
    }

    if cint(include_materials):
        payload["materials"] = _material_rollup(parsed, rows, company)

    return payload


def _material_rollup(
    parsed: List[Dict[str, Any]],
    rows: Dict[str, Dict[str, Any]],
    company: str,
) -> Dict[str, Any]:
    """Raw-material demand for the whole plan, measured against one stock snapshot.

    Reuses the Production Board's basket roll-up so the two screens can never
    disagree about whether the warehouse can cover a day.  A BOM that fails to
    explode degrades to "no material check" rather than blanking the plan.
    """
    basket = [
        {
            "item_code": row["item_code"],
            "bom_name": (rows.get(row["item_code"]) or {}).get("default_bom"),
            "item_qty": row["planned_qty"],
        }
        for row in parsed
        if row["planned_qty"] > 0 and (rows.get(row["item_code"]) or {}).get("default_bom")
    ]
    if not basket:
        return {"ok": True, "components": [], "shortages": [], "max_feasible_scale": None}

    try:
        return board.build_basket_rollup(basket, company)
    except Exception:
        frappe.log_error(
            title="JARZ Daily Plan – material rollup failed",
            message=frappe.get_traceback(),
        )
        return {
            "ok": True,
            "components": [],
            "shortages": [],
            "max_feasible_scale": None,
            "unavailable": True,
        }


# ── Writes ──────────────────────────────────────────────────────────────


@frappe.whitelist()
def save_plan(
    lines: Any,
    company: Optional[str] = None,
    plan_date: Optional[str] = None,
    name: Optional[str] = None,
    status: Optional[str] = None,
    notes: Optional[str] = None,
    actual_batches_run: Any = None,
) -> Dict[str, Any]:
    """Create or update a day's plan.

    Same endpoint for the morning save and the evening count — the only
    difference is which columns carry values, and the DocType recomputes every
    derived figure either way.
    """
    _ensure_execute_access()
    company = _resolve_company(company)
    parsed = _coerce_lines(lines)

    name = (name or "").strip()
    if name:
        if not frappe.db.exists(PLAN_DOCTYPE, name):
            frappe.throw(_("Production plan {0} not found").format(name))
        doc = frappe.get_doc(PLAN_DOCTYPE, name)
    else:
        plan_date = (plan_date or "").strip() or nowdate()
        existing = _find_open_plan(company, plan_date)
        if existing:
            doc = frappe.get_doc(PLAN_DOCTYPE, existing)
        else:
            doc = frappe.new_doc(PLAN_DOCTYPE)
            doc.company = company
            doc.plan_date = getdate(plan_date)

    if status:
        if status not in ("Draft", "Planned", "Closed"):
            frappe.throw(_("Unknown status {0}").format(status))
        doc.status = status
    if notes is not None:
        doc.notes = notes
    if actual_batches_run not in (None, ""):
        doc.actual_batches_run = flt(actual_batches_run)

    doc.set("lines", [])
    for row in parsed:
        doc.append(
            "lines",
            {
                "item_code": row["item_code"],
                "planned_qty": row["planned_qty"],
                "actual_qty": row["actual_qty"],
                "notes": row["notes"],
            },
        )

    doc.save()
    frappe.db.commit()
    return _serialise(doc)


@frappe.whitelist()
def close_plan(
    name: str,
    lines: Any = None,
    actual_batches_run: Any = None,
    notes: Optional[str] = None,
) -> Dict[str, Any]:
    """End-of-day: record what actually came out and close the day.

    Closing with uncounted lines is allowed but reported, because "we never
    counted pistachio" is a normal end to a shift and blocking on it would
    just teach people to type zero.
    """
    _ensure_execute_access()
    name = (name or "").strip()
    if not name:
        frappe.throw(_("name is required"))
    if not frappe.db.exists(PLAN_DOCTYPE, name):
        frappe.throw(_("Production plan {0} not found").format(name))

    doc = frappe.get_doc(PLAN_DOCTYPE, name)
    if doc.status == "Closed":
        frappe.throw(_("Production plan {0} is already closed").format(name))

    if lines is not None:
        parsed = {row["item_code"]: row for row in _coerce_lines(lines)}
        for row in doc.lines or []:
            update = parsed.get(row.item_code)
            if update is None:
                continue
            row.actual_qty = update["actual_qty"]
            if update["notes"] is not None:
                row.notes = update["notes"]

    if actual_batches_run not in (None, ""):
        doc.actual_batches_run = flt(actual_batches_run)
    if notes is not None:
        doc.notes = notes

    doc.status = "Closed"
    doc.save()
    frappe.db.commit()

    payload = _serialise(doc)
    payload["uncounted_items"] = [
        row.item_code
        for row in doc.lines or []
        if row.actual_qty in (None, "") and cint(row.planned_qty) > 0
    ]
    return payload


# ── BOM health ──────────────────────────────────────────────────────────


@frappe.whitelist()
def check_bom_readiness(company: Optional[str] = None) -> Dict[str, Any]:
    """Whether the BOMs can answer the batch question, and what blocks them.

    The plan reads the mix from a one-level BOM line, so a jar whose BOM still
    carries the mix flattened into cheese and cream contributes zero and would
    silently understate the day.  This is the check that makes that visible —
    and doubles as the migration's own progress report.
    """
    _ensure_view_access()
    company = _resolve_company(company)

    mix_item = planning._resolve_mix_item()
    batch_qty, _uom, mix_bom = planning._resolve_mix_batch_qty(mix_item)
    mix_components = set(planning._resolve_flattened_mix_components(mix_item))
    rows = planning._resolve_planable_rows(company, mix_item)

    issues: List[Dict[str, Any]] = []
    ready = 0

    for row in rows:
        if row.get("item_group") not in FINISHED_GOODS_GROUPS:
            continue

        uses_sub_assembly = flt(row.get("mix_qty_per_unit") or 0) > planning.QTY_EPSILON
        flattened = _flattened_mix_components_on(row["default_bom"], mix_components)

        if uses_sub_assembly and flattened:
            issues.append(
                {
                    "item_code": row["item_code"],
                    "default_bom": row["default_bom"],
                    "severity": "error",
                    "reason": "double_counted",
                    "detail": _(
                        "Uses the {0} sub-assembly and also lists {1} directly — "
                        "the mix is counted twice."
                    ).format(mix_item, ", ".join(sorted(flattened))),
                }
            )
        elif flattened:
            issues.append(
                {
                    "item_code": row["item_code"],
                    "default_bom": row["default_bom"],
                    "severity": "warning",
                    "reason": "not_migrated",
                    "detail": _(
                        "Still lists {0} directly instead of the {1} sub-assembly, "
                        "so it contributes nothing to the batch calculation."
                    ).format(", ".join(sorted(flattened)), mix_item),
                }
            )
        else:
            ready += 1

    return {
        "company": company,
        "mix_item": mix_item,
        "mix_bom": mix_bom,
        "batch_qty": batch_qty,
        "ready_items": ready,
        "issue_count": len(issues),
        "issues": issues,
        "ok": not issues and bool(mix_bom),
    }


def _flattened_mix_components_on(bom_name: str, mix_components: set) -> set:
    """Mix raw materials that a jar BOM lists directly.

    Only counts a component that the mix BOM also uses; a jar legitimately
    using cream somewhere else (Molten's ganache) sits behind its own
    sub-assembly and never appears as a direct line, so this stays specific.
    """
    if not bom_name or not mix_components:
        return set()
    direct = set(frappe.db.get_all("BOM Item", filters={"parent": bom_name}, pluck="item_code"))
    return direct.intersection(mix_components)


def _serialise(doc) -> Dict[str, Any]:
    return {
        "name": doc.name,
        "plan_date": str(doc.plan_date),
        "company": doc.company,
        "status": doc.status,
        "mix_item": doc.mix_item,
        "mix_batch_qty": flt(doc.mix_batch_qty),
        "mix_uom": doc.mix_uom,
        "total_mix_qty": flt(doc.total_mix_qty),
        "required_batches": flt(doc.required_batches),
        "planned_batches": flt(doc.planned_batches),
        "mixer_runs": doc.mixer_runs,
        "run_count": cint(doc.run_count),
        "overproduction_batches": flt(doc.overproduction_batches),
        "overproduction_note": doc.overproduction_note,
        "actual_batches_run": flt(doc.actual_batches_run),
        "total_planned_units": cint(doc.total_planned_units),
        "total_actual_units": cint(doc.total_actual_units),
        "realised_units_per_batch": flt(doc.realised_units_per_batch),
        "closed_on": str(doc.closed_on) if doc.closed_on else None,
        "closed_by": doc.closed_by,
        "notes": doc.notes,
        "lines": [
            {
                "item_code": row.item_code,
                "item_name": row.item_name,
                "item_group": row.item_group,
                "default_bom": row.default_bom,
                "planned_qty": cint(row.planned_qty),
                "actual_qty": None if row.actual_qty in (None, "") else cint(row.actual_qty),
                "variance_qty": cint(row.variance_qty),
                "mix_qty_per_unit": flt(row.mix_qty_per_unit),
                "mix_qty": flt(row.mix_qty),
                "jars_per_batch": flt(row.jars_per_batch),
                "notes": row.notes,
            }
            for row in doc.lines or []
        ],
    }
