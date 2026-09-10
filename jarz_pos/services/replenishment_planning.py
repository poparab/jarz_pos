"""Branch replenishment maths — how many jars the factory sends each branch.

The Production Board answers *what should we make*.  This module answers the
question after it: **what should leave the factory store today, and for whom**.

Why it is per branch rather than per item.  Sales are recorded on
``Sales Invoice Item.warehouse``, and over the last 30 days the three selling
branches took 1,466 / 768 / 418 jars.  A branch that sells three times as much
needs three times the cover, so a company-wide velocity divided evenly is not a
plan — it is a way of starving the busiest shop while the quiet one fills up.
Every number below is therefore computed from **that branch's own** sales at
**that branch's own** warehouse.

Layering rule, same as ``production_planning`` and ``subassembly_planning``:
everything here is a pure function over plain numbers and plain mappings.  No
``frappe`` import, no database read, no write.  ``api/replenishment.py`` does
the resolving and calls in here for every number it reports.

Three conventions are load-bearing and each one is a bug this codebase has
already shipped somewhere else:

* **Negative stock never inflates a requirement.**  ERPNext permits negative
  ``Bin`` quantities and 26 branch bins are negative right now (Nasr city
  Chocolate Hazelnut Large is at -55) because the branches have been selling
  stock that was never booked in.  A negative on-hand is a *counting error*,
  never demand: subtracting it would add a phantom hole on top of real sales
  and ship 55 jars nobody asked for.  It is floored to zero for the arithmetic
  and reported separately as ``stock_is_negative`` so somebody counts the shelf.
* **The factory cannot send what it does not have.**  ``send_now`` is capped by
  what is actually in the factory store and the remainder is stated as
  ``short_by`` rather than quietly dropped — an unstated shortfall is how a
  branch waits all day for jars that were never coming.
* **A short supply is split proportionally, never first-come.**  See
  :func:`allocate_proportionally`.
"""

from __future__ import annotations

import math
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

#: Below this, a quantity is float noise rather than a jar.  Same value the
#: production planner uses so the two agree at the boundary.
QTY_EPSILON = 1e-9

#: Sales rates are reported to 3 decimals ("2.133 jars/day"); days of cover to
#: one ("4.7 days").  Enough to act on, without rendering 0.30000000000000004
#: at somebody deciding what to load into a van.
RATE_PRECISION = 3
COVER_PRECISION = 1

#: Warehouse types that are never a selling branch.  ``Transit`` is the
#: ERPNext type on ``Goods In Transit`` — stock in it is mid-journey, not on a
#: shelf somebody can sell from.  WIP and Rejected are excluded too, but via
#: ``production_planning.is_non_sellable_warehouse`` which is injected by the
#: caller rather than copied here (see :func:`select_selling_warehouses`).
NON_SELLING_WAREHOUSE_TYPES = ("Transit",)

#: The same idea for warehouses whose ``warehouse_type`` was never filled in —
#: on this site most of them are blank, so the type alone would let
#: ``Raw Material - J`` and ``Consumables - J`` in as "branches" and suggest
#: shipping finished jars into the flour store.  Lower-cased substrings,
#: matched against the type and the name together.
NON_SELLING_WAREHOUSE_NAME_HINTS = (
    "transit",
    "raw material",
    "raw materials",
    "consumable",
    "packaging",
    "stores",
    "quarantine",
    "sample",
    "scrap",
)


# ── Scalars ─────────────────────────────────────────────────────────────


def to_float(value: Any, default: float = 0.0) -> float:
    """``float()`` that degrades instead of raising on ``None``/junk."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def countable(value: Any) -> float:
    """The part of an on-hand quantity that may be counted against demand.

    A negative ``Bin`` quantity contributes nothing.  It does not, and must
    not, add to what has to be sent — see the module docstring.
    """
    return max(0.0, to_float(value, 0.0))


def coerce_days(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    """A day count from an HTTP query string, clamped to something sane.

    Blank, junk and zero all mean "not specified" and fall back to ``default``:
    a ``cover_days`` of 0 would suppress every suggestion, which is never what
    an empty field is trying to say.
    """
    try:
        days = int(float(value))
    except (TypeError, ValueError):
        return default
    if days <= 0:
        return default
    return max(minimum, min(days, maximum))


def whole_jars(value: Any) -> int:
    """Round a quantity down to countable units.

    Jars are sold in ``Nos``; half a jar cannot be put in a van.  Used for
    supply (what the factory has), never for demand — demand rounds *up*, see
    :func:`suggested_qty`.
    """
    return max(0, int(math.floor(countable(value) + QTY_EPSILON)))


# ── Per-item, per-branch arithmetic ─────────────────────────────────────


def sells_per_day(*, qty_sold: Any, sales_days: Any) -> float:
    """This branch's own daily sales rate for one jar.

    ``qty_sold`` is the **net** quantity that left this warehouse over the
    window (returns carry a negative qty on a submitted Sales Invoice and so
    net themselves out).  A net-negative item — more returned than sold — is
    reported as a rate of zero rather than a negative one: "sells less than
    nothing per day" is not a demand signal anybody can act on.
    """
    days = to_float(sales_days, 0.0)
    if days <= 0:
        return 0.0
    return round(max(0.0, to_float(qty_sold, 0.0)) / days, RATE_PRECISION)


def days_of_cover(*, on_hand: Any, rate: Any) -> Optional[float]:
    """Days this branch's shelf covers at its own selling rate.

    ``None`` — not a sentinel — when the branch never sells the item.  The
    stored ``jarz_days_of_stock`` field writes ``999`` for that case, which
    makes "never sold here" and "huge pile" indistinguishable downstream.

    Stock at or below zero covers ``0.0`` days, never a negative number: "-17
    days of cover" means nothing to somebody loading a van.  The raw ``on_hand``
    travels beside this figure so the hole itself stays visible.
    """
    rate = to_float(rate, 0.0)
    if rate <= 0:
        return None
    on_hand = to_float(on_hand, 0.0)
    if on_hand <= 0:
        return 0.0
    return round(on_hand / rate, COVER_PRECISION)


def suggested_qty(*, rate: Any, cover_days: Any, on_hand: Any) -> int:
    """Whole jars to send so this branch holds ``cover_days`` of its own sales.

    ``max(0, rate * cover_days - max(on_hand, 0))``, rounded **up** to a whole
    jar.  Rounding up because a shelf holding 5.6 days' worth of demand needs 6
    jars, and rounding down here is a shop that runs out on the last afternoon.

    The ``max(on_hand, 0)`` is the negative-stock floor and it is the whole
    reason this function exists as its own testable unit: with 26 negative bins
    on site, subtracting the raw figure would turn a counting error into a van
    load.  The epsilon keeps ``0.4 * 35`` (which floats to 14.000000000000002)
    from asking for a 15th jar nobody needs.
    """
    target = to_float(rate, 0.0) * to_float(cover_days, 0.0)
    deficit = target - countable(on_hand)
    if deficit <= QTY_EPSILON:
        return 0
    return max(0, int(math.ceil(deficit - QTY_EPSILON)))


# ── Allocation ──────────────────────────────────────────────────────────


def allocate_proportionally(
    demands: Mapping[str, Any],
    available: Any,
) -> Dict[str, int]:
    """Split a short supply across branches in proportion to what each needs.

    ``demands`` is ``{warehouse: suggested_qty}``; the return is
    ``{warehouse: whole jars to send}``, summing to at most ``available``.

    **Why proportional and not first-come.**  The obvious implementation walks
    the branches in order and gives each one everything it asks for until the
    pile runs out.  With a fixed branch order that is not a tie-break, it is a
    policy: Nasr city sells 1,466 jars a month and 6th of october 418, so on
    every single short day the first branch in the list would be filled and the
    last one would get nothing — not less, *nothing* — and it would be the same
    branch tomorrow and the day after.  A shop that is never restocked stops
    selling, its velocity falls, and next week the arithmetic "proves" it needed
    even less.  Proportional splitting gives every branch the same fraction of
    what it asked for, so a shortage is shared instead of being paid entirely by
    the smallest shop.

    Whole jars only, by largest remainder: each branch takes ``floor`` of its
    exact share and the leftover units go to the largest fractional remainders
    (ties broken by the bigger demand, then by warehouse name so two calls on
    the same data agree).  No branch is ever allocated more than it asked for.
    """
    wanted: Dict[str, int] = {
        str(key): max(0, int(math.floor(to_float(value, 0.0) + QTY_EPSILON)))
        for key, value in (demands or {}).items()
    }
    if not wanted:
        return {}

    pool = whole_jars(available)
    total = sum(wanted.values())
    if pool <= 0 or total <= 0:
        return {key: 0 for key in wanted}
    if pool >= total:
        # Everybody gets exactly what they asked for; nothing to ration.
        return dict(wanted)

    exact = {key: (qty * pool) / total for key, qty in wanted.items()}
    out = {key: int(math.floor(share)) for key, share in exact.items()}

    remaining = pool - sum(out.values())
    # Largest-remainder guarantees ``remaining < len(wanted)``, so one pass
    # distributes every leftover unit.
    order = sorted(
        wanted,
        key=lambda key: (-(exact[key] - math.floor(exact[key])), -wanted[key], key),
    )
    for key in order:
        if remaining <= 0:
            break
        if out[key] < wanted[key]:
            out[key] += 1
            remaining -= 1

    return out


# ── Warehouse selection ─────────────────────────────────────────────────


def is_selling_warehouse(
    warehouse: Any,
    warehouse_type: Any = None,
    *,
    is_group: Any = 0,
    disabled: Any = 0,
) -> bool:
    """Whether this warehouse is a shop that sells jars to customers.

    A selling warehouse is non-group, not disabled, and not one of the
    factory-side stores.  The rule is stated by exclusion rather than by a list
    of branch names on purpose: hard-coding ``Nasr city - J`` means the next
    branch to open is invisible to this screen until somebody edits Python.

    WIP and Rejected are **not** decided here — the caller injects
    ``production_planning.is_non_sellable_warehouse`` for those (see
    :func:`select_selling_warehouses`) so that rule keeps exactly one
    definition in the codebase.
    """
    if int(to_float(is_group, 0.0)) or int(to_float(disabled, 0.0)):
        return False

    name = str(warehouse or "").strip()
    if not name:
        return False

    wtype = str(warehouse_type or "").strip()
    if wtype in NON_SELLING_WAREHOUSE_TYPES:
        return False

    haystack = f"{wtype} {name}".lower()
    return not any(hint in haystack for hint in NON_SELLING_WAREHOUSE_NAME_HINTS)


def select_selling_warehouses(
    rows: Sequence[Mapping[str, Any]],
    *,
    source_warehouse: Any,
    also_excluded: Optional[Callable[[Any, Any], bool]] = None,
    branch_labels: Optional[Mapping[str, str]] = None,
) -> List[Dict[str, Any]]:
    """Turn raw ``Warehouse`` rows into the branches this plan ships to.

    ``rows`` are ``{name, warehouse_type, is_group, disabled}``.  The factory
    store itself is dropped — it is the source, and a plan telling the factory
    to ship to the factory is noise.

    ``also_excluded`` is the WIP/Rejected predicate, injected rather than
    reimplemented: ``production_planning.is_non_sellable_warehouse`` is the one
    definition of that rule in this codebase and it lives in a module that
    imports ``frappe``, which this one must not.  Injection keeps both true.

    ``branch_labels`` maps a warehouse to the operational branch name (from the
    POS Profile).  When a warehouse has no profile the label falls back to the
    warehouse name with its company abbreviation stripped, which is exactly the
    ``Branch`` record name on this site ("Nasr city - J" -> "Nasr city").
    """
    source = str(source_warehouse or "").strip()
    labels = {str(k): str(v) for k, v in (branch_labels or {}).items()}

    out: List[Dict[str, Any]] = []
    for row in rows or []:
        name = str(row.get("name") or row.get("warehouse") or "").strip()
        if not name or name == source:
            continue
        # A warehouse an enabled POS Profile sells from IS a branch, and that
        # is a far better answer than any rule about names.  It is also
        # self-maintaining: opening a branch means creating its profile, and
        # the plan picks the new shop up with no code change.  The name-based
        # exclusion below is only the fallback for a site that has no profiles
        # yet -- keeping it as the primary rule would have quietly hidden a
        # future branch called something like "Maadi Stores - J", because
        # "stores" is one of the factory-side hints.
        if labels:
            if name not in labels:
                continue
            if int(to_float(row.get("is_group"), 0.0)) or int(
                to_float(row.get("disabled"), 0.0)
            ):
                continue
        elif not is_selling_warehouse(
            name,
            row.get("warehouse_type"),
            is_group=row.get("is_group"),
            disabled=row.get("disabled"),
        ):
            continue
        if also_excluded is not None and also_excluded(name, row.get("warehouse_type")):
            continue
        out.append({"warehouse": name, "branch": labels.get(name) or strip_abbr(name)})

    out.sort(key=lambda entry: (entry["branch"].lower(), entry["warehouse"].lower()))
    return out


def strip_abbr(warehouse: Any) -> str:
    """``"Nasr city - J"`` -> ``"Nasr city"``.

    ERPNext suffixes every warehouse with " - <company abbr>".  Only the last
    segment is removed, and only when something is left over, so a warehouse
    genuinely named "- J" is returned unchanged rather than blanked.
    """
    name = str(warehouse or "").strip()
    head, sep, _tail = name.rpartition(" - ")
    return head.strip() if sep and head.strip() else name


# ── Payload composition ─────────────────────────────────────────────────


def build_branch_item_row(
    item: Mapping[str, Any],
    *,
    on_hand: Any,
    qty_sold: Any,
    sales_days: int,
    cover_days: int,
) -> Dict[str, Any]:
    """One jar, at one branch: what is there, what sells, what to send.

    ``send_now`` / ``short_by`` / ``available_at_source`` are filled in later by
    :func:`apply_allocation`, because what this branch can actually receive is
    not knowable until every branch's demand for the same jar is known.
    """
    on_hand_value = to_float(on_hand, 0.0)
    rate = sells_per_day(qty_sold=qty_sold, sales_days=sales_days)
    return {
        "item_code": str(item.get("item_code") or ""),
        "item_name": str(item.get("item_name") or item.get("item_code") or ""),
        "stock_uom": str(item.get("stock_uom") or ""),
        "on_hand": on_hand_value,
        # Reported, never subtracted.  This is the flag that says "go count
        # this shelf", and it is the only place the raw negative does any work.
        "stock_is_negative": on_hand_value < 0,
        "sells_per_day": rate,
        "days_of_cover": days_of_cover(on_hand=on_hand_value, rate=rate),
        "target_days": int(cover_days),
        "suggested_qty": suggested_qty(rate=rate, cover_days=cover_days, on_hand=on_hand_value),
        "available_at_source": 0.0,
        "send_now": 0,
        "short_by": 0,
    }


def is_worth_showing(row: Mapping[str, Any]) -> bool:
    """Whether a row carries any signal at all.

    Every jar crossed with every branch is mostly zeroes — an item a branch has
    never stocked and never sold says nothing and would bury the rows that do.
    A row survives when it needs stock, or has stock, or sells: anything an
    operator could act on or query.
    """
    return bool(
        int(row.get("suggested_qty") or 0) > 0
        or to_float(row.get("on_hand"), 0.0) != 0.0
        or to_float(row.get("sells_per_day"), 0.0) > 0
    )


def apply_allocation(
    rows_by_warehouse: Mapping[str, Mapping[str, Dict[str, Any]]],
    source_available: Mapping[str, Any],
) -> None:
    """Fill ``available_at_source`` / ``send_now`` / ``short_by``, in place.

    ``rows_by_warehouse`` is ``{warehouse: {item_code: row}}``.  Allocation runs
    once **per item across all branches**, which is the only level at which the
    competition for a short pile is visible.
    """
    item_codes = {
        code
        for rows in rows_by_warehouse.values()
        for code in rows.keys()
    }

    for code in item_codes:
        available = countable(source_available.get(code))
        demands = {
            warehouse: rows[code]["suggested_qty"]
            for warehouse, rows in rows_by_warehouse.items()
            if code in rows
        }
        granted = allocate_proportionally(demands, available)
        for warehouse, rows in rows_by_warehouse.items():
            row = rows.get(code)
            if row is None:
                continue
            send_now = int(granted.get(warehouse, 0))
            row["available_at_source"] = available
            row["send_now"] = send_now
            # Stated, not hidden: the branch is told what is still owed to it.
            row["short_by"] = max(0, int(row["suggested_qty"]) - send_now)


def summarise(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """The four numbers a branch card shows above its item list."""
    return {
        "items_below_cover": sum(1 for row in rows if int(row.get("suggested_qty") or 0) > 0),
        "total_suggested": float(sum(int(row.get("suggested_qty") or 0) for row in rows)),
        "total_send_now": float(sum(int(row.get("send_now") or 0) for row in rows)),
        "negative_bins": sum(1 for row in rows if row.get("stock_is_negative")),
    }


def build_plan(
    *,
    generated_on: str,
    company: Optional[str],
    source_warehouse: Optional[str],
    cover_days: int,
    sales_days: int,
    items: Sequence[Mapping[str, Any]],
    branches: Sequence[Mapping[str, Any]],
    source_available: Mapping[str, Any],
    on_hand: Mapping[Tuple[str, str], Any],
    sold: Mapping[Tuple[str, str], Any],
    notice: Optional[str] = None,
) -> Dict[str, Any]:
    """The whole endpoint payload, from plain data.  No database, no frappe.

    ``on_hand`` and ``sold`` are keyed ``(warehouse, item_code)`` — the two
    batched reads in ``api/replenishment.py`` produce exactly that shape.

    Every ``send_now`` line is directly the shape
    ``jarz_pos.api.transfer.submit_transfer`` expects
    (``{"item_code": ..., "qty": ...}``), so the screen hands this payload
    straight to the existing write path rather than growing a second one.
    """
    catalogue = [
        item for item in (items or []) if str(item.get("item_code") or "").strip()
    ]

    rows_by_warehouse: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for branch in branches or []:
        warehouse = str(branch.get("warehouse") or "").strip()
        if not warehouse:
            continue
        rows_by_warehouse[warehouse] = {
            str(item["item_code"]): build_branch_item_row(
                item,
                on_hand=on_hand.get((warehouse, str(item["item_code"]))),
                qty_sold=sold.get((warehouse, str(item["item_code"]))),
                sales_days=sales_days,
                cover_days=cover_days,
            )
            for item in catalogue
        }

    apply_allocation(rows_by_warehouse, source_available or {})

    branch_payloads: List[Dict[str, Any]] = []
    for branch in branches or []:
        warehouse = str(branch.get("warehouse") or "").strip()
        if not warehouse:
            continue
        rows = [
            row
            for row in rows_by_warehouse.get(warehouse, {}).values()
            if is_worth_showing(row)
        ]
        rows.sort(
            key=lambda row: (
                -int(row.get("suggested_qty") or 0),
                -to_float(row.get("sells_per_day"), 0.0),
                str(row.get("item_name") or ""),
            )
        )
        branch_payloads.append(
            {
                "warehouse": warehouse,
                "branch": str(branch.get("branch") or strip_abbr(warehouse)),
                "items": rows,
                "summary": summarise(rows),
            }
        )

    every_row = [row for payload in branch_payloads for row in payload["items"]]
    summary = summarise(every_row)
    summary["branches"] = len(branch_payloads)
    summary["total_short_by"] = float(
        sum(int(row.get("short_by") or 0) for row in every_row)
    )

    return {
        "generated_on": generated_on,
        "company": company,
        "cover_days": int(cover_days),
        "sales_days": int(sales_days),
        "source": {
            "warehouse": source_warehouse,
            "available": {
                str(code): countable(qty) for code, qty in (source_available or {}).items()
            },
        },
        "branches": branch_payloads,
        "summary": summary,
        "notice": notice,
    }
