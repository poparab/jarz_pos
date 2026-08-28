"""Bases API — making the sub-assemblies the jars are built from.

The Production Board can only propose a batch for something that *sells*: its
whole suggestion is driven by ``jarz_velocity_60d``.  Fudge Cake, Sponge Cake,
Savoiardi, Butter Biscuit and Cheesecake Mix never leave the building on their
own, so their velocity is 0, their suggested batch count is 0, and the board
offers no way to start one — even though every jar eats them.

This module is that missing screen's read side.  Two endpoints, both read-only:

``get_base_items``
    The catalogue of bases with what is in the freezer, what the raw materials
    allow, and — the part the board could never supply — a **demand hint**
    derived from the jars somebody actually intends to fill today.

``preview_base_batch``
    What one specific run would consume, cost and produce, in the exact shape
    ``api/manufacturing.start_production_batch`` wants next.

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
    tidy-up.**  A phantom sub-assembly is expanded into its own components at
    Work Order time (``bom.py`` recurses through an ``is_phantom_item`` row
    whatever the exploded flag says), so a jar batch already relieves the raw
    cheese and cream directly.  Offering that same item here would let somebody
    *also* run it as a batch of its own -- consuming the identical raw materials
    a second time, and minting stock that no Work Order will ever relieve.  The
    freezer sub-assemblies are not phantom and stay listed, which is the whole
    point of this screen; the mix is a planning quantity, not a stocked thing.
    """
    rows = planning._resolve_producible_rows(company, search)
    phantom = _resolve_phantom_boms([row.get("default_bom") for row in rows])
    return [
        row
        for row in rows
        if row.get("item_group") not in FINISHED_GOODS_GROUPS
        and row.get("default_bom") not in phantom
    ]


def _resolve_phantom_boms(bom_names: Sequence[Any]) -> Set[str]:
    """Which of these BOMs are phantom, in one query.

    A read failure returns the empty set: degrading to "nothing is phantom"
    shows the operator one item too many, while degrading the other way would
    silently empty the screen and leave the floor unable to make anything.
    """
    names = sorted({_coerce_str(b) for b in (bom_names or []) if _coerce_str(b)})
    if not names:
        return set()

    try:
        rows = frappe.db.sql(
            """
            SELECT name FROM `tabBOM`
            WHERE name IN %(names)s AND is_phantom_bom = 1
            """,
            {"names": names},
            as_dict=True,
        )
    except Exception:
        _log_failure(
            "JARZ Bases – phantom BOM read failed",
            f"boms={names}\n{frappe.get_traceback()}",
        )
        return set()

    return {r["name"] for r in rows or []}


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


def _resolve_suggestion_targets(company: str) -> List[Dict[str, Any]]:
    """Jar targets from the Plan tab's own suggestion maths.

    Deliberately calls the existing endpoint rather than re-deriving velocity,
    season and cover here: the two screens disagreeing about what today needs
    would be worse than either being wrong.  Capacity is skipped — this call
    only wants the quantities, and the BOM explosion it would trigger is paid
    for again below by ``build_capacity_map``.
    """
    from jarz_pos.api import production

    try:
        payload = production.get_production_suggestions(company=company, include_capacity=0)
    except Exception:
        # A suggestion failure degrades the demand hint to "none"; it must not
        # take the whole Bases screen down.
        _log_failure(
            "JARZ Bases – suggestion fallback failed",
            f"company={company}\n{frappe.get_traceback()}",
        )
        return []

    targets: List[Dict[str, Any]] = []
    for item in (payload or {}).get("items") or []:
        if item.get("item_group") not in FINISHED_GOODS_GROUPS:
            continue
        if bases.to_float(item.get("suggested_batches"), 0.0) <= 0:
            continue
        bom_name = _coerce_str(item.get("default_bom"))
        if not bom_name:
            continue
        targets.append(
            {
                "item_code": item.get("item_code"),
                "qty": bases.countable(item.get("suggested_units")),
                "bom_name": bom_name,
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


def _resolve_required_material_rows(bom_name: str, company: str, qty: float) -> List[Dict[str, Any]]:
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

    return _get_required_material_rows(bom_name, company, qty, fetch_exploded=0)


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
    """The board's capacity row, reduced to the five fields the card renders."""
    if not row:
        return None
    item_code = str(row.get("item_code") or "")
    return {
        "item_code": item_code,
        "item_name": row.get("item_name") or item_code,
        "available_qty": bases.to_float(row.get("available_qty"), 0.0),
        "required_qty": bases.to_float(row.get("required_qty"), 0.0),
        "is_missing_warehouse": row.get("reason") == "missing_source_warehouse",
    }


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


# ── Endpoints ───────────────────────────────────────────────────────────


@frappe.whitelist()
def get_base_items(
    company: Optional[str] = None,
    search: Optional[str] = None,
    include_demand: Any = 1,
    plan_date: Optional[str] = None,
) -> Dict[str, Any]:
    """The bases the floor can make, with stock, capacity and a demand hint.

    A "base item" is any item with a submitted default BOM that is not disabled
    and does not sit in ``FINISHED_GOODS_GROUPS`` — i.e. everything the jars are
    built from rather than the jars themselves.

    ``include_demand=0`` skips the plan/suggestion derivation entirely and
    returns ``demand_source: "none"`` with every ``demand`` blank; the stock and
    capacity figures are unaffected.
    """
    _ensure_production_view_access()

    company = _resolve_company(company)
    search = _coerce_str(search)
    want_demand = _coerce_flag(include_demand, default=True)
    plan_date = _coerce_str(plan_date) or _resolve_today()

    rows = _resolve_base_rows(company, search)
    item_codes = [row["item_code"] for row in rows]

    on_hand_map = planning._resolve_on_hand_map(item_codes)
    # ``build_capacity_map`` already degrades a single unexplodable BOM to an
    # empty component set, which ``can_make_now`` reports as ``None`` — exactly
    # the "capacity was skipped" the contract asks for, per item rather than for
    # the whole screen.
    capacity_map = planning.build_capacity_map(rows, company)
    sop_index = _resolve_sop_index(item_codes)

    mix_item = _resolve_mix_item()
    mix_run_sizes = _resolve_mix_run_sizes()

    demand_map: Dict[str, float] = {}
    demand_source = DEMAND_SOURCE_NONE
    driver: Optional[str] = None
    if want_demand:
        demand_map, demand_source, driver = _resolve_demand(company, plan_date, set(item_codes))

    items: List[Dict[str, Any]] = []
    for row in rows:
        item_code = row["item_code"]
        batch_yield = bases.to_float(row.get("bom_qty"), 0.0)
        on_hand = bases.to_float(on_hand_map.get(item_code), 0.0)
        capacity = capacity_map.get(item_code) or {}
        sop = sop_index.get(item_code)

        demand_block = None
        if driver is not None and item_code in demand_map:
            demand_block = bases.build_demand_block(
                qty_required=demand_map[item_code],
                on_hand=on_hand,
                batch_yield=batch_yield,
                driver=driver,
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
                "has_sop": sop is not None,
                "sop_total_duration_mins": _sop_duration_for_batch(sop, batch_yield),
                "demand": demand_block,
            }
        )

    return {
        "company": company,
        "generated_on": _resolve_now(),
        "demand_source": demand_source,
        "items": items,
        "summary": bases.summarise_bases(items),
    }


@frappe.whitelist()
def preview_base_batch(
    item_code: str,
    bom_name: Optional[str] = None,
    batches: Any = 1,
    company: Optional[str] = None,
) -> Dict[str, Any]:
    """What one run of a base would consume, cost and produce.

    ``item_qty`` is the number the client hands straight to
    ``api/manufacturing.start_production_batch`` — batches are this screen's
    unit, units are ERPNext's, and the conversion happens exactly once, here,
    off the BOM's own yield.

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

    batch_count = bases.to_float(batches, 0.0)
    if batch_count <= 0:
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
    item_qty = batch_count * batch_yield

    rows = _resolve_required_material_rows(bom_name, company, item_qty)

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
                "item_name": row.get("item_name") or component_code,
                "uom": _component_uom(row),
                "required_qty": required_qty,
                "available_qty": available_qty,
                "shortfall": shortfall,
                "source_warehouse": row.get("source_warehouse") or None,
            }
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
