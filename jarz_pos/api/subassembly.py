"""Bases API — making the sub-assemblies the jars are built from.

The Production Board can only propose a batch for something that *sells*: its
whole suggestion is driven by ``jarz_velocity_60d``.  Fudge Cake, Sponge Cake,
Savoiardi, Butter Biscuit and Cheesecake Mix never leave the building on their
own, so their velocity is 0, their suggested batch count is 0, and the board
offers no way to start one — even though every jar eats them.

This module is that missing screen's read side.  Two endpoints, both read-only:

``get_base_items``
    The catalogue of bases with what is in the freezer, what the raw materials
    allow, and — the part the board could never supply — both a **demand hint**
    derived from the jars somebody actually intends to fill today, and a
    **make-to-cover** figure derived from what the jars sell.

    The cover half is what gives a base the same model a jar has.  A base has no
    velocity of its own because it is never sold, so the board computes nothing
    for it and the fortnight the freezer is supposed to hold has been an
    owner's guess.  Its consumption is derivable all the same: one BOM level
    down, ``sum over jars (jar's effective daily velocity x base qty per jar)``.
    That rate feeds the identical cover/target/status maths the jar board runs,
    against the identical Jarz Forecast Settings row.

    It also says **how each base is entered**.  Not every base has a batch:
    Fudge Cake is counted by its 30 eggs, but Blueberry mix is 1 Kg of fruit and
    1 Kg of jelly with nothing countable in it, and it is made by the kilo.
    ``entry_mode``/``batch_unit`` carry that distinction, and ``jar_consumers``
    carries the per-jar rates so a screen can take jar counts for a mix and work
    the Kg out — which is the number the floor actually has in mind.

``preview_base_batch``
    What one specific run would consume, cost and produce, in the exact shape
    ``api/manufacturing.start_production_batch`` wants next.  Sized either in
    batches or in a quantity, because half the catalogue has no batch to count.

Nothing here writes.  Starting and finishing a batch stays entirely with
``api/manufacturing``, which is already item-generic — the client converts
batches to units using ``item_qty`` from the preview and calls it directly.

Layering matches ``api/production.py``: the arithmetic lives in
``services/subassembly_planning.py`` (pure, frappe-free, unit tested) and every
database touch sits behind a ``_resolve_x()`` accessor so a test patches one
symbol instead of the world.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

import frappe
from frappe import _

from jarz_pos.constants import DEFAULT_UOM, ROLES
# The single source of truth for "which item groups are a sellable jar".  A base
# is defined as the complement of this, so re-declaring the tuple here would
# make a base silently appear on both screens the day somebody adds a group.
from jarz_pos.api.daily_plan import FINISHED_GOODS_GROUPS
from jarz_pos.services import production_planning as planning
from jarz_pos.services import subassembly_planning as bases

PLAN_DOCTYPE = "Jarz Production Plan"
PLAN_LINE_DOCTYPE = "Jarz Production Plan Line"
SOP_DOCTYPE = "Jarz SOP"
SOP_STEP_DOCTYPE = "Jarz SOP Step"

# Plan states that still describe an intention.  A Closed plan is history and
# must not drive today's freezer decision.
OPEN_PLAN_STATUSES = ("Draft", "Planned")

# Short human strings the card renders under the demand figure.  Kept as
# constants so the two producers cannot drift from each other.
DEMAND_SOURCE_PLAN = "plan"
DEMAND_SOURCE_SUGGESTIONS = "suggestions"
DEMAND_SOURCE_NONE = "none"
DRIVER_PLAN = "today's plan"
DRIVER_SUGGESTIONS = "board suggestions"

# How a base is entered on the screen.  ``batch`` means the item has a countable
# component to step by — 30 eggs is one batch of Fudge Cake — and ``quantity``
# means it does not, so the only honest unit is its stock UOM: Blueberry mix is
# made by the kilo and a half-batch stepper for it is a fiction.
ENTRY_MODE_BATCH = "batch"
ENTRY_MODE_QUANTITY = "quantity"


# ── Access ──────────────────────────────────────────────────────────────


def _ensure_production_view_access() -> None:
    """Same gate as the rest of the Production Board.

    ``ROLES.PRODUCTION_VIEW`` is deliberately wider than ``ROLES.MANUFACTURING``
    — see the comment on the constant.  Both endpoints here are reads; starting
    a batch keeps the narrower ``PRODUCTION_EXECUTE`` gate over in
    ``api/manufacturing``.
    """
    roles = set(frappe.get_roles())
    if not roles.intersection(ROLES.PRODUCTION_VIEW):
        frappe.throw(_("Not permitted: production access required"), frappe.PermissionError)


# ── Logging ─────────────────────────────────────────────────────────────


def _log_failure(title: str, message: str) -> None:
    """Log a degraded path without ever masking the failure that caused it.

    ``frappe.log_error`` reads System Settings *outside* its own try block, so
    it can raise from inside an ``except`` and replace the caller's real
    traceback with its own.  Every call here is therefore itself guarded, and
    the last resort is the request logger.
    """
    title = (title or "JARZ Bases")[:140]
    try:
        frappe.log_error(title=title, message=message)
        return
    except Exception as logging_error:  # noqa: BLE001 — see docstring
        fallback = f"{title}: {message} (log_error itself failed: {logging_error})"

    try:
        frappe.logger().error(fallback)
    except Exception:  # noqa: BLE001
        # Nothing left to log with.  Raising here would substitute a logging
        # error for the real one, which is the exact trap this helper exists to
        # avoid, so the fallback ends silently and only here.
        return


# ── Coercion ────────────────────────────────────────────────────────────


def _coerce_flag(value: Any, *, default: bool) -> bool:
    """Whitelisted args arrive as strings over HTTP, so ``"0"`` must be False."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("0", "false", "no", ""):
        return False
    if text in ("1", "true", "yes"):
        return True
    return default


def _coerce_str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _resolve_company(company: Optional[str]) -> str:
    company = _coerce_str(company) or planning._resolve_default_company()
    if not company:
        frappe.throw(_("Company is not configured and no Default Company is set"))
    return company


# ── frappe / ERPNext resolvers ──────────────────────────────────────────


def _resolve_now() -> str:
    return str(frappe.utils.now_datetime())


def _resolve_today() -> str:
    return frappe.utils.nowdate()


def _resolve_base_rows(company: str, search: Optional[str]) -> List[Dict[str, Any]]:
    """Every producible item that is **not** a finished jar.

    Reuses the board's own producible query rather than writing a third variant
    of "items with a submitted default BOM": it already filters
    ``is_default = 1 AND docstatus = 1 AND Item.disabled = 0``, already honours
    the search term, and is not capped at an arbitrary 100 rows.

    **Phantom BOMs are excluded, and that exclusion is a safety guard, not a
    tidy-up** -- see ``planning.exclude_phantom_rows``, which now owns it for
    every screen that offers something to produce.  The freezer sub-assemblies
    are not phantom and stay listed, which is the whole point of this screen;
    the mix is a planning quantity, not a stocked thing.
    """
    rows = planning.exclude_phantom_rows(
        planning._resolve_producible_rows(company, search)
    )
    return [row for row in rows if row.get("item_group") not in FINISHED_GOODS_GROUPS]


def _resolve_mix_item() -> str:
    from jarz_pos.services.daily_production_plan import _resolve_mix_item as resolver

    return resolver()


def _resolve_mix_run_sizes() -> Optional[List[float]]:
    """Configured mixer run sizes, or ``None`` when nobody has set any.

    Read as a ``Data`` field, which casts through ``cstr`` rather than
    ``cint`` — so unlike an Int or Check on a Single, blank really does read
    blank here and ``None`` genuinely means "not configured".
    """
    try:
        raw = frappe.db.get_single_value("Jarz POS Settings", "production_mixer_run_sizes")
    except Exception:
        _log_failure("JARZ Bases – run size read failed", frappe.get_traceback())
        return None
    return bases.parse_run_sizes(raw)


def _resolve_open_plan(company: str, plan_date: str) -> Optional[str]:
    """The open plan for that day, newest first if somebody made two."""
    try:
        return frappe.db.get_value(
            PLAN_DOCTYPE,
            {
                "company": company,
                "plan_date": plan_date,
                "status": ["in", OPEN_PLAN_STATUSES],
            },
            "name",
            order_by="modified desc",
        )
    except Exception:
        _log_failure(
            "JARZ Bases – open plan lookup failed",
            f"company={company} plan_date={plan_date}\n{frappe.get_traceback()}",
        )
        return None


def _resolve_plan_targets(company: str, plan_date: str) -> List[Dict[str, Any]]:
    """Jar targets from the day's plan, as ``{item_code, qty, bom_name}``.

    A line whose ``default_bom`` was never stamped (the item was not on the
    plan template when it was saved) is repaired from the BOM table rather than
    dropped — a jar missing from the demand roll-up understates the freezer.
    """
    plan = _resolve_open_plan(company, plan_date)
    if not plan:
        return []

    try:
        rows = frappe.get_all(
            PLAN_LINE_DOCTYPE,
            filters={"parent": plan, "parenttype": PLAN_DOCTYPE},
            fields=["item_code", "planned_qty", "default_bom"],
            limit_page_length=0,
        )
    except Exception:
        _log_failure(
            "JARZ Bases – plan line read failed",
            f"plan={plan}\n{frappe.get_traceback()}",
        )
        return []

    targets: List[Dict[str, Any]] = []
    missing_bom: List[str] = []
    for row in rows or []:
        qty = bases.countable(row.get("planned_qty"))
        if qty <= 0:
            continue
        item_code = _coerce_str(row.get("item_code"))
        if not item_code:
            continue
        bom_name = _coerce_str(row.get("default_bom"))
        if not bom_name:
            missing_bom.append(item_code)
        targets.append({"item_code": item_code, "qty": qty, "bom_name": bom_name})

    if missing_bom:
        repaired = _resolve_default_bom_map(missing_bom)
        for target in targets:
            if not target["bom_name"]:
                target["bom_name"] = repaired.get(target["item_code"], "")

    return [t for t in targets if t["bom_name"]]


def _resolve_default_bom_map(item_codes: Sequence[str]) -> Dict[str, str]:
    """``item_code -> submitted default BOM`` for a batch of items, in one query."""
    codes = sorted({c for c in (item_codes or []) if c})
    if not codes:
        return {}

    try:
        rows = frappe.db.sql(
            """
            SELECT b.item AS item_code, b.name AS bom_name
            FROM `tabBOM` b
            WHERE b.item IN %(codes)s AND b.is_default = 1 AND b.docstatus = 1
            """,
            {"codes": codes},
            as_dict=True,
        )
    except Exception:
        _log_failure("JARZ Bases – default BOM lookup failed", frappe.get_traceback())
        return {}

    return {r["item_code"]: r["bom_name"] for r in rows or []}


def _resolve_jar_board(company: str) -> Dict[str, Any]:
    """The Plan tab's own payload, read once and shared by both derivations.

    Deliberately calls the existing endpoint rather than re-deriving velocity,
    season and cover here: the two screens disagreeing about what today needs
    would be worse than either being wrong.  Capacity is skipped — these callers
    only want quantities, and the BOM explosion it would trigger is paid for
    again by ``build_capacity_map``.

    A failure degrades to ``{}``: the demand hint falls back to "none" and the
    cover figures to "unknown", but the Bases screen still renders.  Both
    callers below therefore treat an empty payload as *no signal*, never as a
    signal of zero.
    """
    from jarz_pos.api import production

    try:
        return production.get_production_suggestions(company=company, include_capacity=0) or {}
    except Exception:
        _log_failure(
            "JARZ Bases – jar board read failed",
            f"company={company}\n{frappe.get_traceback()}",
        )
        return {}


def _jar_rows(payload: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Finished-jar rows of a board payload, with a usable BOM."""
    rows: List[Dict[str, Any]] = []
    for item in (payload or {}).get("items") or []:
        if item.get("item_group") not in FINISHED_GOODS_GROUPS:
            continue
        if not _coerce_str(item.get("default_bom")):
            continue
        rows.append(item)
    return rows


def _effective_velocity(item: Mapping[str, Any]) -> float:
    """A jar's season-adjusted daily velocity, off the board's own figure.

    ``effective_velocity`` is what the board publishes and what its own
    suggestion is built from, so reading it keeps the base's consumption tied to
    the jar's plan.  The product is recomputed only as a fallback for a payload
    served from cache by an older build mid-deploy, where the key may be absent
    — degrading to 0 there would blank the whole Bases screen for two minutes.
    """
    if item.get("effective_velocity") is not None:
        return bases.countable(item.get("effective_velocity"))
    return bases.countable(item.get("velocity_60d")) * bases.to_float(
        item.get("season_multiplier"), 1.0
    )


def _resolve_suggestion_targets(company: str) -> List[Dict[str, Any]]:
    """Jar targets from the Plan tab's own suggestion maths.

    What the board says somebody should fill *today* — the fallback demand
    driver when no plan has been saved.  A jar the board is not asking for
    contributes nothing.
    """
    targets: List[Dict[str, Any]] = []
    for item in _jar_rows(_resolve_jar_board(company)):
        if bases.to_float(item.get("suggested_batches"), 0.0) <= 0:
            continue
        targets.append(
            {
                "item_code": item.get("item_code"),
                "qty": bases.countable(item.get("suggested_units")),
                "bom_name": _coerce_str(item.get("default_bom")),
            }
        )
    return targets


def _resolve_velocity_targets(company: str) -> List[Dict[str, Any]]:
    """Jar **rates** — one day's worth of each jar — as demand targets.

    The same ``{item_code, qty, bom_name}`` shape ``_resolve_suggestion_targets``
    produces, but ``qty`` is a per-day velocity rather than a quantity to fill.
    Pushed through the identical BOM walk, that returns each base's per-day
    consumption (see ``bases.derive_base_consumption_per_day``).

    Every jar that moves is included, not just the ones the board wants a batch
    of: a jar that is well stocked today still eats its base tomorrow, and
    dropping it would understate the freezer's burn rate.
    """
    targets: List[Dict[str, Any]] = []
    for item in _jar_rows(_resolve_jar_board(company)):
        velocity = _effective_velocity(item)
        if velocity <= bases.QTY_EPSILON:
            continue
        targets.append(
            {
                "item_code": item.get("item_code"),
                "qty": velocity,
                "bom_name": _coerce_str(item.get("default_bom")),
            }
        )
    return targets


def _resolve_jar_bom_rows(bom_names: Iterable[str]) -> List[Dict[str, Any]]:
    """One-level ``BOM Item`` rows for a set of jar BOMs, with each BOM's yield.

    One level on purpose.  The explosion would report flour and cream, which is
    precisely the information this feature is trying not to give: the floor
    makes Sponge Cake, not its ingredients.
    """
    names = sorted({_coerce_str(b) for b in (bom_names or []) if _coerce_str(b)})
    if not names:
        return []

    try:
        rows = frappe.db.sql(
            """
            SELECT
                bi.parent     AS bom_name,
                bi.item_code  AS item_code,
                -- stock_qty, never qty: a BOM line may be entered in a display
                -- unit that is not the item's stock UOM, and the demand figure
                -- is compared against Bin quantities, which are stock UOM.
                -- Mango Large lists Mango mix in "Nos" while Mango Medium lists
                -- the same item in "Kg" -- reading qty there would silently mix
                -- two scales in one total.  daily_production_plan does the same.
                bi.stock_qty  AS qty,
                b.quantity    AS bom_quantity
            FROM `tabBOM Item` bi
            INNER JOIN `tabBOM` b ON b.name = bi.parent
            WHERE bi.parent IN %(names)s
              AND bi.parenttype = 'BOM'
            """,
            {"names": names},
            as_dict=True,
        )
    except Exception:
        _log_failure(
            "JARZ Bases – jar BOM read failed",
            f"boms={names}\n{frappe.get_traceback()}",
        )
        return []

    return [dict(r) for r in rows or []]


def _resolve_base_component_rows(bom_names: Iterable[str]) -> Dict[str, List[Dict[str, Any]]]:
    """One-level component rows of the **bases' own** BOMs, keyed by BOM name.

    One query for the whole screen, then grouped in Python — the alternative is
    a read per card, and this list is every producible non-jar item on site.

    ``stock_qty`` and ``stock_uom``, never ``qty``/``uom``, for the reason
    documented on ``_resolve_jar_bom_rows``: a BOM line may be typed in a display
    unit that is not the item's stock unit, and reading the two halves from
    different scales is how "30 eggs" becomes 30 of something else.

    The quantities are deliberately **not** divided by ``bom.quantity``.  A BOM
    is one batch by definition, so ``stock_qty`` is already the per-batch figure;
    dividing would report 3.24 eggs per Kg of Fudge Cake, which is not a unit
    anybody on the floor counts.
    """
    names = sorted({_coerce_str(b) for b in (bom_names or []) if _coerce_str(b)})
    if not names:
        return {}

    try:
        rows = frappe.db.sql(
            """
            SELECT
                bi.parent     AS bom_name,
                bi.item_code  AS item_code,
                bi.item_name  AS item_name,
                bi.stock_qty  AS qty,
                bi.stock_uom  AS uom
            FROM `tabBOM Item` bi
            WHERE bi.parent IN %(names)s
              AND bi.parenttype = 'BOM'
            """,
            {"names": names},
            as_dict=True,
        )
    except Exception:
        # Degrades to "no batch unit" for every base, which the client renders as
        # quantity entry — the honest fallback, since typing Kg is always valid
        # and offering a batch stepper we cannot size is not.
        _log_failure(
            "JARZ Bases – base component read failed",
            f"boms={names}\n{frappe.get_traceback()}",
        )
        return {}

    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows or []:
        bom_name = _coerce_str(row.get("bom_name"))
        if not bom_name:
            continue
        grouped.setdefault(bom_name, []).append(dict(row))
    return grouped


def _resolve_jar_consumers(base_item_codes: Set[str]) -> Dict[str, List[Dict[str, Any]]]:
    """``{base_item_code: [{item_code, item_name, qty_per_jar}, ...]}``.

    Everything whose **default, active, submitted** one-level BOM lists the base,
    with how much of it one unit eats — the map that lets the floor type jar
    counts for a mix and have the Kg worked out.

    ``qty_per_jar = stock_qty / bom.quantity``, and a BOM whose ``quantity`` is
    missing or zero is skipped rather than divided by: substituting 1 would
    multiply the rate by the real batch size, the same trap ``derive_base_demand``
    documents.

    Two rows for one consumer are summed, not listed twice, for the same reason
    that walk accumulates them: a BOM listing a base on two lines really does eat
    both, and publishing the jar twice would make it appear twice on a screen
    somebody is typing counts into.

    No item-group filter.  Restricting this to ``FINISHED_GOODS_GROUPS`` would
    silently drop a sub-assembly that eats another sub-assembly, and a count
    typed against that is exactly as valid as one typed against a jar.
    """
    codes = sorted({c for c in (base_item_codes or set()) if c})
    if not codes:
        return {}

    try:
        rows = frappe.db.sql(
            """
            SELECT
                bi.item_code  AS base_item_code,
                b.item        AS item_code,
                b.item_name   AS item_name,
                bi.stock_qty  AS qty,
                b.quantity    AS bom_quantity
            FROM `tabBOM Item` bi
            INNER JOIN `tabBOM` b ON b.name = bi.parent
            WHERE bi.item_code IN %(codes)s
              AND bi.parenttype = 'BOM'
              AND b.is_default = 1
              AND b.docstatus = 1
              AND b.is_active = 1
            """,
            {"codes": codes},
            as_dict=True,
        )
    except Exception:
        _log_failure(
            "JARZ Bases – jar consumer read failed",
            f"bases={codes}\n{frappe.get_traceback()}",
        )
        return {}

    accumulated: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for row in rows or []:
        base_code = _coerce_str(row.get("base_item_code"))
        consumer_code = _coerce_str(row.get("item_code"))
        if not base_code or not consumer_code:
            continue

        bom_quantity = bases.to_float(row.get("bom_quantity"), 0.0)
        if bom_quantity <= 0:
            continue

        per_jar = bases.countable(row.get("qty")) / bom_quantity
        if per_jar <= bases.QTY_EPSILON:
            continue

        bucket = accumulated.setdefault(base_code, {})
        existing = bucket.get(consumer_code)
        if existing is None:
            bucket[consumer_code] = {
                "item_code": consumer_code,
                "item_name": _coerce_str(row.get("item_name")) or consumer_code,
                "qty_per_jar": per_jar,
            }
        else:
            existing["qty_per_jar"] += per_jar

    consumers: Dict[str, List[Dict[str, Any]]] = {}
    for base_code, bucket in accumulated.items():
        shaped = [
            {
                "item_code": entry["item_code"],
                "item_name": entry["item_name"],
                "qty_per_jar": round(entry["qty_per_jar"], bases.QTY_PRECISION),
            }
            for entry in bucket.values()
        ]
        # Ascending rate, then code: Medium lands before Large on the card, and
        # two requests cannot order the same jars differently.
        shaped.sort(key=lambda entry: (entry["qty_per_jar"], entry["item_code"]))
        consumers[base_code] = shaped

    return consumers


def _resolve_sop_index(item_codes: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    """Active SOP and its per-batch step durations for a batch of items.

    One query for the whole screen.  Calling ``api/sop.get_sop_for_item`` per
    card would re-explode every BOM a second time purely to learn whether a
    procedure exists.

    Highest ``version`` wins, ``modified`` breaks a tie — the same rule
    ``api/sop._resolve_active_sop`` applies, so the list and the detail screen
    can never name different SOPs.
    """
    codes = sorted({c for c in (item_codes or []) if c})
    if not codes:
        return {}

    try:
        rows = frappe.db.sql(
            """
            SELECT
                s.name            AS sop,
                s.item_code       AS item_code,
                s.version         AS version,
                s.modified        AS modified,
                st.duration_mins  AS duration_mins,
                st.scaling_mode   AS scaling_mode
            FROM `tabJarz SOP` s
            LEFT JOIN `tabJarz SOP Step` st
                   ON st.parent = s.name AND st.parenttype = %(step_parent)s
            WHERE s.item_code IN %(codes)s AND s.is_active = 1
            """,
            {"codes": codes, "step_parent": SOP_DOCTYPE},
            as_dict=True,
        )
    except Exception:
        # A site that has the code but not yet the SOP tables must render the
        # board without procedures rather than 500.
        _log_failure("JARZ Bases – SOP index read failed", frappe.get_traceback())
        return {}

    per_sop: Dict[str, Dict[str, Any]] = {}
    for row in rows or []:
        sop = row.get("sop")
        if not sop:
            continue
        bucket = per_sop.get(sop)
        if bucket is None:
            bucket = {
                "sop": sop,
                "item_code": row.get("item_code"),
                "version": bases.to_float(row.get("version"), 0.0),
                "modified": str(row.get("modified") or ""),
                "steps": [],
            }
            per_sop[sop] = bucket
        # A LEFT JOIN yields one all-NULL step row for an SOP with no steps.
        if row.get("duration_mins") is not None or row.get("scaling_mode") is not None:
            bucket["steps"].append((row.get("duration_mins"), row.get("scaling_mode")))

    winners: Dict[str, Dict[str, Any]] = {}
    for bucket in per_sop.values():
        item_code = bucket["item_code"]
        current = winners.get(item_code)
        if current is None or (bucket["version"], bucket["modified"]) > (
            current["version"],
            current["modified"],
        ):
            winners[item_code] = bucket

    return winners


def _sop_duration_for_batch(bucket: Optional[Mapping[str, Any]], batch_yield: float) -> Optional[float]:
    """Total minutes one batch of this item takes, per its SOP.

    ``None`` when the item has no SOP at all.  An SOP whose steps carry no
    durations sums to ``0.0`` — known to be untimed, which is different from
    having no procedure.

    Durations are scaled with ``sop_rendering.scale_duration`` rather than
    summed raw, so a ``Per Unit`` step is multiplied by the batch yield exactly
    the way the detail screen will show it.
    """
    if not bucket:
        return None

    try:
        from jarz_pos.services.sop_rendering import scale_duration
    except Exception:
        _log_failure("JARZ Bases – SOP scaler unavailable", frappe.get_traceback())
        return None

    total = 0.0
    for duration, mode in bucket.get("steps") or []:
        total += bases.to_float(
            scale_duration(duration, mode, 1.0, batch_yield), 0.0
        )
    return round(total, 3)


def _resolve_item_row(item_code: str) -> Dict[str, Any]:
    row = frappe.db.get_value(
        "Item", item_code, ["name", "item_name", "item_group", "stock_uom", "disabled"], as_dict=True
    )
    return dict(row) if row else {}


def _resolve_bom_row(bom_name: str) -> Dict[str, Any]:
    row = frappe.db.get_value(
        "BOM", bom_name, ["name", "item", "quantity", "company", "docstatus"], as_dict=True
    )
    return dict(row) if row else {}


def _resolve_required_material_rows(
    bom_name: str,
    company: str,
    qty: float,
    material_selections: Any = None,
) -> List[Dict[str, Any]]:
    """Components of one batch, read from ``api/manufacturing``.

    Deliberately not a second implementation of the bill read: the transfer that
    ``start_production_batch`` posts is built from these rows, so a preview
    computed any other way would be a preview of a different batch.

    ``fetch_exploded=0`` — the **one-level** bill — is the load-bearing part.
    ``manufacturing._ensure_work_order`` states ``use_multi_level_bom = 0`` on
    every Work Order the app creates, so the Work Order consumes ``tabBOM
    Item``: a sub-assembly is drawn from stock as itself, not exploded into
    flour and eggs.  A screen whose entire job is "here is what this batch will
    consume" has to show that, not the explosion.  Passed explicitly even though
    it is now also the reader's default, because this module's answer would be
    wrong rather than merely different if the default ever moved.
    """
    from jarz_pos.api.manufacturing import _get_required_material_rows

    kwargs: Dict[str, Any] = {"fetch_exploded": 0}
    if material_selections:
        kwargs["material_selections"] = material_selections
    return _get_required_material_rows(bom_name, company, qty, **kwargs)


def _component_uom(row: Mapping[str, Any]) -> str:
    """``stock_uom`` -> ``uom`` -> ``DEFAULT_UOM``, in that order.

    Behaviour is unchanged; the chain now lives in ``production_planning`` as
    the single implementation shared with ``api/manufacturing``'s row reader.
    Two copies of it was how Mango Large came to list Mango mix in "Nos"
    against a Kg stock figure on one screen and correctly in Kg on another.
    """
    return planning.component_uom(row)


def _resolve_valuation_rate(item_code: str, warehouse: Any) -> float:
    from jarz_pos.api.manufacturing import _resolve_valuation_rate as resolver

    return resolver(item_code, warehouse)


def _resolve_has_sop(item_code: str) -> bool:
    try:
        return bool(
            frappe.db.get_value(SOP_DOCTYPE, {"item_code": item_code, "is_active": 1}, "name")
        )
    except Exception:
        _log_failure(
            "JARZ Bases – SOP existence check failed",
            f"item_code={item_code}\n{frappe.get_traceback()}",
        )
        return False


# ── Shaping ─────────────────────────────────────────────────────────────


def _shape_limiting_component(row: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    """The board's capacity row, reduced to the fields the card renders."""
    if not row:
        return None
    item_code = str(row.get("item_code") or "")
    shaped = {
        "item_code": item_code,
        "item_name": row.get("item_name") or item_code,
        "available_qty": bases.to_float(row.get("available_qty"), 0.0),
        "required_qty": bases.to_float(row.get("required_qty"), 0.0),
        "is_missing_warehouse": row.get("reason") == "missing_source_warehouse",
    }

    # Carried through only when ``build_capacity_map`` actually looked — which
    # it does for a component that blocks the batch outright.  Defaulting them
    # in would turn "nobody looked" into "there is none anywhere", which is a
    # different and far more discouraging answer.
    if "alternatives" in row:
        shaped["available_elsewhere"] = bases.to_float(row.get("available_elsewhere"), 0.0)
        shaped["alternatives"] = list(row.get("alternatives") or [])

    return shaped


def _resolve_demand(
    company: str,
    plan_date: str,
    base_codes: Set[str],
) -> Tuple[Dict[str, float], str, Optional[str]]:
    """``(demand_map, demand_source, driver)`` for the whole screen.

    Order is the contract's: today's plan first because it is somebody's stated
    intention, then the board's suggestion as a standing-in guess, then nothing
    at all.  "Nothing at all" is reported honestly as ``none`` with every
    ``demand`` blank rather than as a screen full of zeroes.
    """
    if not base_codes:
        return {}, DEMAND_SOURCE_NONE, None

    targets = _resolve_plan_targets(company, plan_date)
    source = DEMAND_SOURCE_PLAN
    driver = DRIVER_PLAN

    if not targets:
        targets = _resolve_suggestion_targets(company)
        source = DEMAND_SOURCE_SUGGESTIONS
        driver = DRIVER_SUGGESTIONS

    if not targets:
        return {}, DEMAND_SOURCE_NONE, None

    bom_rows = _resolve_jar_bom_rows({t.get("bom_name") for t in targets})
    demand = bases.derive_base_demand(targets, bom_rows, base_codes)
    if not demand:
        # Targets existed but no jar BOM lists a base one level down — usually a
        # catalogue that predates the sub-assembly migration.  Reporting "none"
        # is honest; reporting zeroes would read as "nothing is needed".
        return {}, DEMAND_SOURCE_NONE, None

    return demand, source, driver


def _resolve_consumption(company: str, base_codes: Set[str]) -> Dict[str, float]:
    """``{base_item_code: qty consumed per day}`` for the whole screen.

    Bases are never sold, so ``jarz_velocity_60d`` is 0 for every one of them
    and the board can say nothing about their cover — which is why the freezer
    target has been a guess.  Their consumption is nonetheless derivable: one
    BOM level down, each jar eats a known quantity of a base per unit, and the
    jar's velocity is already resolved.

    A base absent from the returned map has **no signal** (no jar's one-level
    BOM lists it — usually a catalogue that predates the sub-assembly BOM
    migration, or a base nothing currently sells uses).  The caller reports that
    as ``null``, never as a consumption of zero.
    """
    if not base_codes:
        return {}

    targets = _resolve_velocity_targets(company)
    if not targets:
        return {}

    bom_rows = _resolve_jar_bom_rows({t.get("bom_name") for t in targets})
    return bases.derive_base_consumption_per_day(targets, bom_rows, base_codes)


# ── Endpoints ───────────────────────────────────────────────────────────


@frappe.whitelist()
def get_base_items(
    company: Optional[str] = None,
    search: Optional[str] = None,
    include_demand: Any = 1,
    plan_date: Optional[str] = None,
    include_cover: Any = 1,
) -> Dict[str, Any]:
    """The bases the floor can make, with stock, capacity, demand and cover.

    A "base item" is any item with a submitted default BOM that is not disabled
    and does not sit in ``FINISHED_GOODS_GROUPS`` — i.e. everything the jars are
    built from rather than the jars themselves.

    Two different questions are answered per item and **both** are reported:

    ``demand``
        what the plan somebody is looking at needs today.  Blank when there is
        no plan and the board is proposing nothing.

    ``consumption_per_day`` / ``days_of_cover`` / ``suggested_qty``
        make-to-cover, the same model the jar board runs, against the same
        resolved ``target_days`` and the same status vocabulary.  A base can be
        fully covered for today's plan and still be four days from empty; that
        is the gap this half closes, and it is why neither number replaces the
        other.

    ``include_demand=0`` skips the plan/suggestion derivation and returns
    ``demand_source: "none"`` with every ``demand`` blank.  ``include_cover=0``
    skips the consumption derivation and returns ``cover_included: false`` with
    every cover figure blank.  Stock and capacity are unaffected by either.
    """
    _ensure_production_view_access()

    company = _resolve_company(company)
    search = _coerce_str(search)
    want_demand = _coerce_flag(include_demand, default=True)
    want_cover = _coerce_flag(include_cover, default=True)
    plan_date = _coerce_str(plan_date) or _resolve_today()

    rows = _resolve_base_rows(company, search)
    item_codes = [row["item_code"] for row in rows]

    # Thresholds, season and the default cover target, read once for the whole
    # screen from the same accessor the jar board uses — so one Settings row
    # drives both boards and a base cannot be planned to a different fortnight
    # than the jar that eats it.
    context = planning.get_planning_context(company)
    thresholds = context["thresholds"]

    on_hand_map = planning._resolve_on_hand_map(item_codes)
    # ``build_capacity_map`` already degrades a single unexplodable BOM to an
    # empty component set, which ``can_make_now`` reports as ``None`` — exactly
    # the "capacity was skipped" the contract asks for, per item rather than for
    # the whole screen.
    capacity_map = planning.build_capacity_map(rows, company)
    sop_index = _resolve_sop_index(item_codes)

    mix_item = _resolve_mix_item()
    mix_run_sizes = _resolve_mix_run_sizes()

    # How each base is entered, resolved for the whole screen in two queries.
    # Per-card reads here would be one BOM read per producible non-jar item.
    component_rows = _resolve_base_component_rows([row.get("default_bom") for row in rows])
    jar_consumers = _resolve_jar_consumers(set(item_codes))

    demand_map: Dict[str, float] = {}
    demand_source = DEMAND_SOURCE_NONE
    driver: Optional[str] = None
    if want_demand:
        demand_map, demand_source, driver = _resolve_demand(company, plan_date, set(item_codes))

    consumption_map: Dict[str, float] = {}
    if want_cover:
        consumption_map = _resolve_consumption(company, set(item_codes))

    items: List[Dict[str, Any]] = []
    for row in rows:
        item_code = row["item_code"]
        batch_yield = bases.to_float(row.get("bom_qty"), 0.0)
        on_hand = bases.to_float(on_hand_map.get(item_code), 0.0)
        capacity = capacity_map.get(item_code) or {}
        sop = sop_index.get(item_code)

        # ``None`` here is a real answer — every component is divisible, so this
        # base has no batch and is made by the kilo — and must reach the client as
        # ``null``, never as an empty object.
        batch_unit = bases.pick_batch_unit(
            component_rows.get(_coerce_str(row.get("default_bom"))) or []
        )

        demand_block = None
        if driver is not None and item_code in demand_map:
            demand_block = bases.build_demand_block(
                qty_required=demand_map[item_code],
                on_hand=on_hand,
                batch_yield=batch_yield,
                driver=driver,
            )

        # Same resolution the jar board applies, so an Item-level override set
        # on a base behaves exactly as it does on a jar.
        target_days, target_source = planning.resolve_target_days(
            row.get("target_days_override"), context["default_target_days"]
        )
        cover = bases.build_cover_block(
            # ``None``, not ``0.0``: absent from the map means no jar BOM lists
            # this base one level down, which is "we have no signal", not "it is
            # not consumed".
            consumption_per_day=consumption_map.get(item_code),
            on_hand=on_hand,
            target_days=target_days,
            batch_yield=batch_yield,
        )

        items.append(
            {
                "item_code": item_code,
                "item_name": row.get("item_name") or item_code,
                "item_group": row.get("item_group"),
                "stock_uom": row.get("stock_uom") or DEFAULT_UOM,
                "default_bom": row.get("default_bom"),
                "batch_yield": batch_yield,
                # Raw, negative and all: the hole is the thing somebody has to
                # act on.  Every calculation above floored it away already.
                "on_hand": on_hand,
                "stock_is_negative": on_hand < 0,
                "batches_on_hand": bases.batches_on_hand(
                    on_hand=on_hand, batch_yield=batch_yield
                ),
                "can_make_now_batches": capacity.get("can_make_now_batches"),
                "limiting_component": _shape_limiting_component(capacity.get("limiting_component")),
                "run_sizes": bases.run_sizes_for_item(
                    item_code, mix_item=mix_item, mix_run_sizes=mix_run_sizes
                ),
                # ── How this base is entered ──
                # Published explicitly rather than left for the client to
                # re-derive from ``batch_unit``: the rule for which items have a
                # batch belongs in one place, and a second implementation of it
                # in Dart would disagree the first time the UOM list grows.
                "entry_mode": ENTRY_MODE_BATCH if batch_unit else ENTRY_MODE_QUANTITY,
                "batch_unit": batch_unit,
                # ``[]``, never ``null``: an empty list says "nothing's BOM lists
                # this base", which is a fact, and a client iterating it needs no
                # special case for the mix nobody consumes yet.
                "jar_consumers": jar_consumers.get(item_code, []),
                "has_sop": sop is not None,
                "sop_total_duration_mins": _sop_duration_for_batch(sop, batch_yield),
                "demand": demand_block,
                # ── Make-to-cover, derived one BOM level down from the jars ──
                # ``None`` throughout when nothing consumes this base, paired
                # with ``no_velocity`` — exactly how the jar board reports an
                # item that never sells.
                "consumption_per_day": cover["consumption_per_day"],
                "days_of_cover": cover["days_of_cover"],
                "target_days": cover["target_days"],
                "target_days_source": target_source,
                "status": planning.status_for_days_of_cover(
                    cover["days_of_cover"], **thresholds
                ),
                "suggested_qty": cover["suggested_qty"],
                "suggested_batches": cover["suggested_batches"],
            }
        )

    return {
        "company": company,
        "generated_on": _resolve_now(),
        "demand_source": demand_source,
        # Mirrors the jar board's ``capacity_included``: a client must be able
        # to tell "nothing consumes this" from "we did not look this time".
        "cover_included": want_cover,
        "season": context["season"],
        "default_target_days": context["default_target_days"],
        "thresholds": thresholds,
        "items": items,
        "summary": bases.summarise_bases(items),
    }


@frappe.whitelist()
def preview_base_batch(
    item_code: str,
    bom_name: Optional[str] = None,
    batches: Any = 1,
    company: Optional[str] = None,
    material_selections: Any = None,
    qty: Any = None,
) -> Dict[str, Any]:
    """What one run of a base would consume, cost and produce.

    ``item_qty`` is the number the client hands straight to
    ``api/manufacturing.start_production_batch`` — and it stays the single place
    either unit is converted, off the BOM's own yield.

    The run is sized **one** of two ways:

    * ``batches`` — this screen's original unit, for a base the floor counts in
      whole mixer loads.
    * ``qty`` — the item's stock UOM directly, for a base that has no batch to
      count.  Blueberry mix is 2 Kg of nothing countable; asking for it in
      batches would make somebody divide Kg by 2 in their head.

    ``qty`` wins when it is given and positive, and ``batches`` is then reported
    back as the fraction it works out to.  A ``qty`` that is given but zero or
    negative **throws**, exactly as ``batches`` does: falling back to the batch
    path there would quietly return a preview of a run nobody asked for, which
    the client would then start.

    Read-only.  A shortage is reported, never enforced: the material precheck
    inside ``start_production_batch`` is the gate, and duplicating it here would
    give two places to disagree about whether a batch may run.
    """
    _ensure_production_view_access()

    item_code = _coerce_str(item_code)
    if not item_code:
        frappe.throw(_("item_code is required"))

    item = _resolve_item_row(item_code)
    if not item:
        frappe.throw(_("Item {0} not found").format(item_code))

    # "Given" is not-None and not blank: a client that leaves the field empty
    # sends ``""`` over HTTP and means "size this in batches", while a client
    # that sends a 0 is asking for a run of nothing and gets told so.
    requested_qty: Optional[float] = None
    if qty is not None and str(qty).strip() != "":
        requested_qty = bases.to_float(qty, 0.0)
        if requested_qty <= 0:
            frappe.throw(_("Quantity to produce must be greater than zero"))

    batch_count = bases.to_float(batches, 0.0)
    if requested_qty is None and batch_count <= 0:
        frappe.throw(_("Batches must be greater than zero"))

    bom_name = _coerce_str(bom_name)
    if not bom_name:
        bom_name = _resolve_default_bom_map([item_code]).get(item_code, "")
        if not bom_name:
            frappe.throw(_("No submitted default BOM found for Item {0}").format(item_code))

    bom = _resolve_bom_row(bom_name)
    if not bom:
        frappe.throw(_("BOM {0} not found").format(bom_name))
    if str(bom.get("item") or "") != item_code:
        frappe.throw(
            _("BOM {0} produces {1}, not {2}").format(bom_name, bom.get("item"), item_code)
        )
    if int(bom.get("docstatus") or 0) != 1:
        frappe.throw(_("BOM {0} is not submitted").format(bom_name))

    batch_yield = bases.to_float(bom.get("quantity"), 0.0)
    if batch_yield <= 0:
        # Not a degrade: every quantity below divides or multiplies by this, and
        # a preview of "0 units" would send the client to an endpoint that
        # rejects it with a far less useful message.
        frappe.throw(_("BOM {0} has no yield quantity to produce against").format(bom_name))

    company = _coerce_str(company) or _coerce_str(bom.get("company")) or _resolve_company(None)

    if requested_qty is not None:
        item_qty = requested_qty
        # Reported back through the module's own conversion rather than a bare
        # division, so the batch figure on a quantity-sized run reads exactly like
        # the one on every other screen.
        batch_count = bases.batches_from_qty(qty=item_qty, batch_yield=batch_yield)
    else:
        item_qty = batch_count * batch_yield

    rows = _resolve_required_material_rows(
        bom_name, company, item_qty, material_selections=material_selections
    )

    components: List[Dict[str, Any]] = []
    estimated_cost = 0.0
    priced_any = False
    has_shortage = False

    for row in rows or []:
        component_code = str(row.get("item_code") or "")
        required_qty = bases.to_float(row.get("required_qty"), 0.0)
        available_qty = bases.to_float(row.get("available_qty"), 0.0)
        # The raw availability is reported, but a negative Bin must never make a
        # shortfall look larger than the requirement itself.
        shortfall = max(0.0, required_qty - bases.countable(available_qty))
        if shortfall > bases.QTY_EPSILON:
            has_shortage = True

        rate = _resolve_valuation_rate(component_code, row.get("source_warehouse"))
        if rate:
            priced_any = True
        estimated_cost += rate * required_qty

        components.append(
            {
                "item_code": component_code,
                "original_item_code": row.get("original_item_code") or component_code,
                "item_name": row.get("item_name") or component_code,
                "uom": _component_uom(row),
                "required_qty": required_qty,
                "available_qty": available_qty,
                "shortfall": shortfall,
                "source_warehouse": row.get("source_warehouse") or None,
                "valuation_rate": rate,
                "estimated_amount": rate * required_qty,
            }
        )

    # "It's in another store", for the short components only, in one query after
    # the loop.  Short is measured in the recipe line's source warehouse, so a
    # component can read short here while the company holds plenty of it one
    # branch away — and the fix for that is a transfer, not a purchase.  Rows
    # with no shortfall keep both fields absent: nobody looked for them.
    planning.attach_stock_elsewhere(
        [row for row in components if row["shortfall"] > bases.QTY_EPSILON], company
    )

    run_sizes = bases.run_sizes_for_item(
        item_code, mix_item=_resolve_mix_item(), mix_run_sizes=_resolve_mix_run_sizes()
    )

    return {
        "item_code": item_code,
        "bom_name": bom_name,
        "company": company,
        "batches": batch_count,
        "batch_yield": batch_yield,
        "item_qty": item_qty,
        "stock_uom": item.get("stock_uom") or DEFAULT_UOM,
        "components": components,
        "has_shortage": has_shortage,
        # ``None``, not 0.0, when nothing could be valued: a whole batch of
        # never-purchased components really does total zero, and reporting that
        # as a cost would read as "this batch is free".
        "estimated_cost": round(estimated_cost, 2) if priced_any else None,
        "run_size_ok": bases.matches_run_size(batch_count, run_sizes),
        "run_sizes": run_sizes,
        "has_sop": _resolve_has_sop(item_code),
    }
