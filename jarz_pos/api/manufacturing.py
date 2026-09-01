from __future__ import annotations

import hashlib
import json
from datetime import date as _date_cls, datetime as _datetime_cls
from typing import Any, Dict, List, Optional

import frappe
from frappe import _
from jarz_pos.constants import DEFAULT_UOM, QUERY_LIMITS, ROLES
# The one implementation of the component-unit chain (stock_uom -> uom ->
# DEFAULT_UOM).  Module scope is safe and is the sanctioned direction:
# ``production_planning`` is the shared layer and reaches back here only through
# a deferred import inside a function body, so there is no cycle.
from jarz_pos.services.production_planning import attach_stock_elsewhere, component_uom

# Fallbacks for the Jarz POS Settings fields the floor flow reads.  The Single
# row reads empty until somebody saves it after a migrate, so every read has to
# survive a blank.
DEFAULT_MAX_BACKDATE_DAYS = 3
# Statuses a Work Order can hold while a batch is physically on the bench.
RUNNING_WORK_ORDER_STATUSES = ("Not Started", "In Process")
# Float slack shared with the material precheck so the two agree at the
# boundary instead of disagreeing by a float hair.
QTY_TOLERANCE = 1e-9
# The floor board shows what is open now, not a history; a cap keeps a
# misconfigured client from asking for the whole Work Order table.
MAX_RUNNING_WORK_ORDERS = 200
# Core Work Order columns the running-batch board needs.
RUNNING_WORK_ORDER_FIELDS = [
    "name",
    "production_item",
    "item_name",
    "qty",
    "produced_qty",
    "bom_no",
    "status",
    "wip_warehouse",
    "fg_warehouse",
    "stock_uom",
    "material_transferred_for_manufacturing",
]
# Custom fields that only exist once the fixture has migrated.
JARZ_WORK_ORDER_FIELDS = ["jarz_started_by", "jarz_started_at"]
try:
    from frappe import _dict as FrappeDict  # type: ignore
except Exception:  # pragma: no cover
    FrappeDict = dict  # type: ignore
try:
    from frappe.model.document import Document  # type: ignore
except Exception:  # pragma: no cover
    class Document:  # type: ignore
        pass
try:
    from frappe.utils import get_datetime  # type: ignore
except Exception:  # pragma: no cover
    def get_datetime(x):  # type: ignore
        return x

try:
    # ERPNext helper to build Stock Entry for a Work Order
    from erpnext.manufacturing.doctype.work_order.work_order import make_stock_entry  # type: ignore
except Exception:  # pragma: no cover
    make_stock_entry = None  # type: ignore
try:
    from erpnext.manufacturing.doctype.bom.bom import get_bom_items_as_dict  # type: ignore
except Exception:  # pragma: no cover
    get_bom_items_as_dict = None  # type: ignore
try:
    from erpnext.stock.utils import get_latest_stock_qty  # type: ignore
except Exception:  # pragma: no cover
    get_latest_stock_qty = None  # type: ignore


def _debug_log(message: str) -> None:
    """Floor-volume tracing.

    Deliberately NOT ``frappe.log_error``: these fired on *every* call and at
    production-floor volume (dozens of batches a shift) they would bury the real
    failures in the Error Log.  Note the site log level defaults to ERROR on
    staging/production, so this is silent there until somebody raises it — which
    is the point.
    """
    try:
        frappe.logger().debug(f"JARZ Manufacturing: {message}")
    except Exception:
        pass


def _get_default_company() -> str:
    try:
        return frappe.db.get_single_value("Global Defaults", "default_company") or ""
    except Exception:
        return ""


def _coerce_str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _warehouse_belongs_to_company(warehouse: str, company: str) -> bool:
    warehouse = _coerce_str(warehouse)
    company = _coerce_str(company)
    if not warehouse or not company:
        return False

    try:
        return _coerce_str(frappe.db.get_value("Warehouse", warehouse, "company")) == company
    except Exception:
        return False


def _get_item_default_warehouse(item_code: str, company: str) -> str | None:
    item_code = _coerce_str(item_code)
    company = _coerce_str(company)
    if not item_code or not company:
        return None

    try:
        warehouse = _coerce_str(
            frappe.db.get_value(
                "Item Default",
                {"parent": item_code, "parenttype": "Item", "company": company},
                "default_warehouse",
            )
        )
    except Exception:
        return None

    if warehouse and _warehouse_belongs_to_company(warehouse, company):
        return warehouse
    return None


def _get_mfg_defaults(company: str) -> Dict[str, str]:
    # Best-effort defaults for warehouses
    out: Dict[str, str] = {"company": company}
    try:
        ms = frappe.get_single("Manufacturing Settings")
        # Only include warehouses that belong to the given company to avoid cross-company errors
        if getattr(ms, "default_wip_warehouse", None):
            wh = ms.default_wip_warehouse
            wh_comp = frappe.db.get_value("Warehouse", wh, "company")
            if wh_comp == company:
                out["wip_warehouse"] = wh
        if getattr(ms, "default_fg_warehouse", None):
            wh = ms.default_fg_warehouse
            wh_comp = frappe.db.get_value("Warehouse", wh, "company")
            if wh_comp == company:
                out["fg_warehouse"] = wh
    except Exception:
        pass
    return out


def _ensure_manager_access() -> None:
    roles = set(frappe.get_roles())
    allowed = ROLES.MANUFACTURING
    if not roles.intersection(allowed):
        frappe.throw(_("Not permitted: Managers only"), frappe.PermissionError)


def _get_bom_company(bom_name: str) -> str:
    try:
        return frappe.db.get_value("BOM", bom_name, "company") or ""
    except Exception:
        return ""


def _resolve_scheduled_datetime(scheduled_at: Any):
    if scheduled_at:
        return get_datetime(scheduled_at)
    return get_datetime(_resolve_now_datetime())


# ── frappe / ERPNext resolvers ──────────────────────────────────────────
# Everything the Production Board flow reads from the database sits behind one
# of these so a test patches a single symbol instead of the world.  Same
# contract ``services/production_planning.py`` follows.


def _resolve_now_datetime():
    """The **server's** clock.

    Load-bearing for ``_assert_posting_date_allowed``: if "today" came from the
    client payload, changing a phone's date would walk straight through the
    backdating gate.
    """
    return frappe.utils.now_datetime()


def _resolve_user_roles() -> set:
    try:
        return set(frappe.get_roles())
    except Exception:
        return set()


def _resolve_work_order_doc(work_order: str, for_update: bool = False):
    """Load a Work Order, optionally taking a ``SELECT ... FOR UPDATE`` lock.

    The lock is not decoration: two operators finishing the same batch from two
    tablets is an ordinary Tuesday, and without it both read the same
    ``material_transferred_for_manufacturing`` and both post a Manufacture entry.
    """
    if for_update:
        return frappe.get_doc("Work Order", work_order, for_update=True)
    return frappe.get_doc("Work Order", work_order)


def _resolve_valuation_rate(item_code: str, warehouse: Any = None) -> float:
    """Valuation rate for a component, warehouse-specific where possible.

    Returns ``0.0`` rather than raising for anything unpriced — the caller is
    responsible for noticing that a whole batch valued at zero is suspicious.
    """
    if warehouse:
        try:
            rate = frappe.db.get_value(
                "Bin", {"item_code": item_code, "warehouse": warehouse}, "valuation_rate"
            )
            if rate:
                return float(rate)
        except Exception:
            pass
    try:
        return float(frappe.db.get_value("Item", item_code, "valuation_rate") or 0)
    except Exception:
        return 0.0


def _resolve_stock_entry_detail_rows(work_order: str, purpose: str) -> List[Dict[str, Any]]:
    """Submitted Stock Entry lines for one Work Order and one purpose."""
    try:
        rows = frappe.db.sql(
            """
            SELECT
                se.name          AS stock_entry,
                se.posting_date  AS posting_date,
                sed.item_code    AS item_code,
                sed.item_name    AS item_name,
                sed.qty          AS qty,
                sed.uom          AS uom,
                sed.amount       AS amount,
                sed.s_warehouse  AS s_warehouse,
                sed.t_warehouse  AS t_warehouse
            FROM `tabStock Entry Detail` sed
            INNER JOIN `tabStock Entry` se ON se.name = sed.parent
            WHERE se.work_order = %(work_order)s
              AND se.docstatus = 1
              AND se.purpose = %(purpose)s
            ORDER BY se.posting_date, se.posting_time, sed.idx
            """,
            {"work_order": work_order, "purpose": purpose},
            as_dict=True,
        )
        return [dict(r) for r in (rows or [])]
    except Exception:
        # Costing must never take the board down, but a silent zero would read
        # as "this batch cost nothing" — so it is logged loudly.
        frappe.log_error(
            title="JARZ – batch cost query failed",
            message=f"work_order={work_order} purpose={purpose}",
        )
        return []


def _resolve_bom_cost(bom_name: Any) -> Optional[Dict[str, float]]:
    """``{total_cost, quantity}`` for a BOM, or ``None`` when unreadable."""
    if not bom_name:
        return None
    try:
        row = frappe.db.get_value("BOM", bom_name, ["total_cost", "quantity"], as_dict=True)
        if not row:
            return None
        return {
            "total_cost": float(row.get("total_cost") or 0),
            "quantity": float(row.get("quantity") or 0),
        }
    except Exception:
        return None


def _resolve_settings():
    try:
        return frappe.get_cached_doc("Jarz POS Settings")
    except Exception:
        return None


def _resolve_update_work_order_status(work_order: str):
    """Recompute and reload the Work Order status after a Stock Entry lands."""
    doc = frappe.get_doc("Work Order", work_order)
    if hasattr(doc, "update_status"):
        doc.update_status()
    doc.reload()
    return doc


def _resolve_wip_available_materials(work_order: str) -> List[Dict[str, Any]]:
    """What is physically still sitting in WIP for this Work Order.

    ERPNext already nets transferred-minus-consumed per (item, warehouse); no
    reason to re-derive it and get the batch/serial handling subtly wrong.
    """
    getter = None
    try:
        getter = frappe.get_attr(
            "erpnext.stock.doctype.stock_entry.stock_entry.get_available_materials"
        )
    except Exception:
        getter = None
    if not getter:
        try:
            import importlib

            mod = importlib.import_module("erpnext.stock.doctype.stock_entry.stock_entry")
            getter = getattr(mod, "get_available_materials", None)
        except Exception:
            getter = None
    if not getter:
        frappe.throw(_("Could not resolve ERPNext get_available_materials helper"))

    rows: List[Dict[str, Any]] = []
    for key, data in (getter(work_order) or {}).items():
        try:
            item_code, warehouse = key
        except Exception:
            continue
        raw_qty = getattr(data, "qty", None)
        if raw_qty is None and isinstance(data, dict):
            raw_qty = data.get("qty")
        qty = _flt(raw_qty)
        if qty <= QTY_TOLERANCE:
            continue
        rows.append({"item_code": item_code, "warehouse": warehouse, "qty": qty})
    return rows


def _resolve_work_order_source_warehouses(work_order: str) -> Dict[str, str]:
    """``item_code -> source_warehouse`` from the Work Order Item table."""
    try:
        rows = frappe.get_all(
            "Work Order Item",
            filters={"parent": work_order, "parenttype": "Work Order"},
            fields=["item_code", "source_warehouse"],
        )
    except Exception:
        return {}
    out: Dict[str, str] = {}
    for row in rows or []:
        try:
            if row.get("source_warehouse"):
                out[row["item_code"]] = row["source_warehouse"]
        except Exception:
            continue
    return out


# ── Settings readers ────────────────────────────────────────────────────
# Blank/unsaved Single rows and mocked frappe objects both have to degrade to
# the documented default rather than blowing up mid-batch.


def _setting_raw(fieldname: str) -> Any:
    try:
        return getattr(_resolve_settings(), fieldname, None)
    except Exception:
        return None


def _setting_int(fieldname: str, default: int) -> int:
    try:
        return int(_setting_raw(fieldname))
    except (TypeError, ValueError):
        return default


def _setting_float(fieldname: str, default: float) -> float:
    try:
        return float(_setting_raw(fieldname))
    except (TypeError, ValueError):
        return default


def _setting_flag(fieldname: str, default: bool = False) -> bool:
    raw = _setting_raw(fieldname)
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    if isinstance(raw, str):
        return raw.strip().lower() in ("1", "true", "yes")
    return default


# ── Production Board access gates ───────────────────────────────────────


def _ensure_production_view_access() -> None:
    roles = _resolve_user_roles()
    if not roles.intersection(ROLES.PRODUCTION_VIEW):
        frappe.throw(_("Not permitted: production access required"), frappe.PermissionError)


def _ensure_production_execute_access() -> None:
    roles = _resolve_user_roles()
    if not roles.intersection(ROLES.PRODUCTION_EXECUTE):
        frappe.throw(_("Not permitted: production access required"), frappe.PermissionError)


def _as_date(value: Any):
    """Coerce anything date-ish to a ``datetime.date``, or ``None``.

    Does not lean on ``frappe.utils.get_datetime`` alone: that import has a
    fallback at the top of this module, and a posting-date guard that quietly
    stops recognising dates would be a guard that stops guarding.
    """
    if isinstance(value, _datetime_cls):
        return value.date()
    if isinstance(value, _date_cls):
        return value

    try:
        coerced = get_datetime(value)
    except Exception:
        coerced = None
    if isinstance(coerced, _datetime_cls):
        return coerced.date()
    if isinstance(coerced, _date_cls):
        return coerced

    text = value.strip() if isinstance(value, str) else ""
    if not text:
        return None
    try:
        return _datetime_cls.fromisoformat(text.replace(" ", "T")).date()
    except ValueError:
        return None


def _assert_posting_date_allowed(scheduled_dt: Any) -> None:
    """Gate *when* a batch may be posted.

    Three rules, in order:
      1. The future is never postable by anybody — stock that has not been made
         must not exist, and a wrong tablet clock is the usual cause.
      2. The past needs ``ROLES.PRODUCTION_BACKDATE``; operators are excluded.
      3. Beyond ``production_max_backdate_days`` only a System Manager may go,
         because that is the range that reaches a closed accounting period.

    "Today" comes from ``_resolve_now_datetime()`` — the server clock — and never
    from the caller's payload.
    """
    target = _as_date(scheduled_dt)
    today = _as_date(_resolve_now_datetime())
    if target is None or today is None:
        frappe.throw(_("Could not resolve the production posting date"))
        return

    if target > today:
        frappe.throw(
            _("Production cannot be posted in the future (requested {0}, today is {1})").format(
                target, today
            )
        )
        return

    if target == today:
        return

    roles = _resolve_user_roles()
    if not roles.intersection(ROLES.PRODUCTION_BACKDATE):
        frappe.throw(
            _("Not permitted: backdated production requires a manager"), frappe.PermissionError
        )
        return

    days_back = (today - target).days
    max_days = max(0, _setting_int("production_max_backdate_days", DEFAULT_MAX_BACKDATE_DAYS))
    if days_back > max_days and ROLES.SYSTEM_MANAGER not in roles:
        frappe.throw(
            _(
                "Backdating more than {0} day(s) requires a System Manager "
                "(requested {1} days back)"
            ).format(max_days, days_back)
        )


def _price_batch_components(line: Dict[str, Any], company: str) -> Dict[str, Any]:
    """Read the BOM once and value it at current valuation rates.

    ``fetch_exploded=0``: the operator batch-value cap bounds what one person
    can *issue from the store*, so it must price the rows the Work Order will
    actually consume.  A frozen sub-assembly already carries the cost of its own
    inputs in its valuation rate, so the one-level bill is also the complete
    figure — pricing the explosion instead priced items that never move.
    """
    rows = _get_required_material_rows(
        line["bom_name"], company, float(line["item_qty"]), fetch_exploded=0
    )
    priced: List[Dict[str, Any]] = []
    total = 0.0
    for row in rows:
        rate = _resolve_valuation_rate(row["item_code"], row.get("source_warehouse"))
        amount = float(row.get("required_qty") or 0) * rate
        total += amount
        priced.append(dict(row, valuation_rate=rate, estimated_amount=amount))
    return {"components": priced, "total_value": total}


def _assert_batch_value_within_threshold(
    line: Dict[str, Any],
    company: str,
    priced: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Cap the material value one operator may commit in a single batch.

    A limit of ``0`` means no limit.  Anybody in ``ROLES.MANUFACTURING`` bypasses
    it outright — the cap exists to bound an operator mistake, not to slow a
    manager down.

    Returns the priced explosion so the caller does not pay for a second one.
    """
    if priced is None:
        priced = _price_batch_components(line, company)

    roles = _resolve_user_roles()
    if roles.intersection(ROLES.MANUFACTURING):
        return priced

    limit = _setting_float("production_operator_batch_value_limit", 0.0)
    if limit <= 0:
        return priced

    value = float(priced.get("total_value") or 0)
    if value <= 0 and priced.get("components"):
        # An item that has never been purchased carries valuation_rate 0, so a
        # whole batch of them values at nothing and would sail under any
        # threshold.  The check still passes (blocking production over missing
        # master data would be worse) but it must not pass *quietly*.
        frappe.log_error(
            title="JARZ – batch value computed as zero",
            message=(
                f"item={line.get('item_code')} bom={line.get('bom_name')} "
                f"qty={line.get('item_qty')} company={company}; "
                f"{len(priced.get('components') or [])} component(s) all valued 0 — "
                "the operator batch-value limit cannot bind on this batch."
            ),
        )
        return priced

    if value > limit:
        frappe.throw(
            _(
                "This batch is worth {0} in materials, above the {1} limit for "
                "operators. Ask a manager to start it."
            ).format(f"{value:,.2f}", f"{limit:,.2f}")
        )
    return priced


def _apply_posting_datetime(stock_entry: Any, scheduled_dt: Any) -> None:
    posting_date = scheduled_dt.strftime("%Y-%m-%d")
    posting_time = scheduled_dt.strftime("%H:%M:%S")

    if isinstance(stock_entry, Document):
        stock_entry.posting_date = posting_date
        stock_entry.posting_time = posting_time
        stock_entry.set_posting_time = 1
        return

    if isinstance(stock_entry, (dict, FrappeDict)):
        stock_entry["posting_date"] = posting_date
        stock_entry["posting_time"] = posting_time
        stock_entry["set_posting_time"] = 1


def _set_work_order_actual_dates(work_order: str, scheduled_dt: Any) -> None:
    wo_doc = frappe.get_doc("Work Order", work_order)
    wo_doc.db_set("actual_start_date", scheduled_dt, update_modified=False)
    wo_doc.db_set("actual_end_date", scheduled_dt, update_modified=False)
    wo_doc.actual_start_date = scheduled_dt
    wo_doc.actual_end_date = scheduled_dt
    if hasattr(wo_doc, "set_lead_time"):
        wo_doc.set_lead_time()
        if getattr(wo_doc, "lead_time", None) is not None:
            wo_doc.db_set("lead_time", wo_doc.lead_time, update_modified=False)


def _find_company_warehouse(company: str, warehouse_type: str | None, name_hints: list[str]) -> str | None:
    """Pick a reasonable warehouse for the company.
    Priority: exact warehouse_type match -> name contains any hint -> any leaf warehouse for company.
    """
    try:
        if warehouse_type:
            wh = frappe.db.get_value(
                "Warehouse",
                {"company": company, "warehouse_type": warehouse_type, "is_group": 0},
                "name",
            )
            if wh:
                return wh
        # Try name hints (case-insensitive contains)
        for hint in name_hints:
            rows = frappe.get_all(
                "Warehouse",
                filters={"company": company, "is_group": 0},
                fields=["name"],
                or_filters=[["Warehouse", "warehouse_name", "like", f"%{hint}%"], ["Warehouse", "name", "like", f"%{hint}%"]],
                limit=1,
            )
            if rows:
                return rows[0]["name"]
        # Fallback to any leaf warehouse of the company
        any_wh = frappe.db.get_value("Warehouse", {"company": company, "is_group": 0}, "name")
        return any_wh
    except Exception:
        return None


def _resolve_work_order_warehouses(line: Dict[str, Any], company: str, defaults: Dict[str, str]) -> Dict[str, str]:
    resolved: Dict[str, str] = {"company": company}

    requested_wip = _coerce_str(line.get("wip_warehouse"))
    requested_fg = _coerce_str(line.get("fg_warehouse") or line.get("target_warehouse"))
    default_wip = _coerce_str(defaults.get("wip_warehouse"))
    default_fg = _coerce_str(defaults.get("fg_warehouse"))

    if _warehouse_belongs_to_company(requested_wip, company):
        resolved["wip_warehouse"] = requested_wip
    elif default_wip:
        resolved["wip_warehouse"] = default_wip

    if _warehouse_belongs_to_company(requested_fg, company):
        resolved["fg_warehouse"] = requested_fg
    else:
        item_fg = _get_item_default_warehouse(line.get("item_code"), company)
        if item_fg:
            resolved["fg_warehouse"] = item_fg
        elif default_fg:
            resolved["fg_warehouse"] = default_fg

    if not resolved.get("wip_warehouse"):
        resolved["wip_warehouse"] = _find_company_warehouse(company, "WIP", ["WIP", "Work In Progress"]) or ""
    if not resolved.get("fg_warehouse"):
        resolved["fg_warehouse"] = _find_company_warehouse(company, "Finished Goods", ["FG", "Finished Goods"]) or ""

    return resolved


def _resolve_get_bom_items_as_dict():
    if get_bom_items_as_dict:
        return get_bom_items_as_dict
    try:
        fn = frappe.get_attr("erpnext.manufacturing.doctype.bom.bom.get_bom_items_as_dict")
        if fn:
            return fn
    except Exception:
        pass
    return None


def _resolve_get_latest_stock_qty():
    if get_latest_stock_qty:
        return get_latest_stock_qty
    try:
        fn = frappe.get_attr("erpnext.stock.utils.get_latest_stock_qty")
        if fn:
            return fn
    except Exception:
        pass
    return None


def _get_live_stock_qty(item_code: str, warehouse: str) -> float:
    getter = _resolve_get_latest_stock_qty()
    if getter:
        try:
            return float(getter(item_code, warehouse) or 0)
        except Exception:
            pass
    try:
        return float(
            frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": warehouse}, "actual_qty") or 0
        )
    except Exception:
        return 0.0


def _get_required_material_rows(
    bom_name: str,
    company: str,
    qty: float,
    fetch_exploded: int = 0,
) -> List[Dict[str, Any]]:
    """Required-material rows for a BOM, at either level of the bill.

    ``fetch_exploded`` decides **which bill of materials this answers about**,
    and the two values are genuinely different questions — do not collapse them:

    * ``0`` (**the default**) reads ``tabBOM Item``: the **one-level** bill,
      where a sub-assembly appears as itself.  This is what a Work Order on this
      site actually consumes.  ``_ensure_work_order`` states
      ``use_multi_level_bom = 0`` outright, so ERPNext's ``set_required_items``
      explodes nothing and requires exactly these rows.  That pairing is
      load-bearing: flip one without the other and every capacity, shortage and
      cost figure in the app describes a batch nobody is going to run.
    * ``1`` reads ``tabBOM Explosion Item``: the bill flattened all the way down
      to raw materials, so a sub-assembly appears as flour and eggs.  Correct
      only for a question about ultimate raw-material consumption, which is
      **not** what any current caller asks.

    Correctness is the default and the explosion is the thing you opt into,
    because the old default was the defect: the board checked Tiramisu Large
    against eggs and flour while the Work Order went on to demand Cheesecake Mix
    from a bin holding -14.64.  Neither list contained the other's items, so a
    batch could pass the check and still fail — after ``submit_work_orders`` had
    already committed earlier lines and consumed real stock.

    ``uom`` is the unit the ``required_qty`` is actually **in**.  ERPNext
    defaults ``get_bom_items_as_dict`` to ``fetch_qty_in_stock_uom=True``, so
    both branches return a stock-UOM quantity; ``component_uom`` therefore
    prefers ``stock_uom`` and keeps ``bom_item.uom`` (the unit somebody typed
    the line in) only as a fallback for the exploded branch, which does not
    select it at all.  ``stock_uom`` is carried through raw and undefaulted so a
    caller can still tell "no unit was stated" from "the unit really is Nos".
    """
    getter = _resolve_get_bom_items_as_dict()
    if not getter:
        frappe.throw(_("Could not resolve ERPNext BOM items helper"))

    item_dict = getter(bom_name, company, qty=qty, fetch_exploded=1 if fetch_exploded else 0)
    rows: List[Dict[str, Any]] = []
    for item in sorted(item_dict.values(), key=lambda row: row.get("idx") or float("inf")):
        if item.get("include_item_in_manufacturing") in (0, "0", False):
            continue

        item_code = str(item.get("item_code") or "")
        source_warehouse = item.get("source_warehouse") or item.get("default_warehouse")
        rows.append(
            {
                "item_code": item_code,
                "item_name": item.get("item_name") or item_code,
                "uom": component_uom(item),
                # Additive: never substituted, never defaulted, so a caller can
                # tell "no unit was stated" from "the unit really is Nos".
                "stock_uom": item.get("stock_uom") or None,
                "required_qty": float(item.get("qty") or 0),
                "source_warehouse": source_warehouse,
                "available_qty": _get_live_stock_qty(item_code, source_warehouse) if source_warehouse else 0.0,
            }
        )
    return rows


def _get_material_precheck_issues(line: Dict[str, Any], company: str) -> List[Dict[str, Any]]:
    """Per-line "will this batch's material transfer succeed" check.

    ``fetch_exploded=0``: this check exists to predict the Stock Entry the
    Work Order is about to post, and that entry moves the one-level bill.
    Checking raw materials was checking a different document entirely.

    An ``insufficient_stock`` issue additionally carries ``available_elsewhere``
    and ``alternatives``: short is measured in the recipe line's **source
    warehouse only**, so an operator can be told they cannot start while the
    company holds plenty of the item one branch away.  The block is unchanged —
    the fix is a stock transfer, and the message now says so.
    """
    issues: List[Dict[str, Any]] = []
    required_rows = _get_required_material_rows(
        line["bom_name"], company, float(line["item_qty"]), fetch_exploded=0
    )
    for row in required_rows:
        source_warehouse = row.get("source_warehouse")
        if not source_warehouse:
            issues.append(
                {
                    "type": "missing_source_warehouse",
                    "item_code": row["item_code"],
                    "item_name": row["item_name"],
                }
            )
            continue

        try:
            is_group = int(frappe.db.get_value("Warehouse", source_warehouse, "is_group") or 0)
        except Exception:
            is_group = 0

        if is_group:
            issues.append(
                {
                    "type": "group_source_warehouse",
                    "item_code": row["item_code"],
                    "item_name": row["item_name"],
                    "source_warehouse": source_warehouse,
                }
            )
            continue

        required_qty = float(row.get("required_qty") or 0)
        available_qty = float(row.get("available_qty") or 0)
        if available_qty + 1e-9 < required_qty:
            issues.append(
                {
                    "type": "insufficient_stock",
                    "item_code": row["item_code"],
                    "item_name": row["item_name"],
                    "uom": row.get("uom") or DEFAULT_UOM,
                    "required_qty": required_qty,
                    "available_qty": available_qty,
                    "missing_qty": required_qty - available_qty,
                    "source_warehouse": source_warehouse,
                }
            )

    # One batched lookup for every short item on the line, after the loop —
    # never inside it.
    attach_stock_elsewhere(
        [issue for issue in issues if issue.get("type") == "insufficient_stock"], company
    )

    return issues


def _format_precheck_issue(item_name: str, item_code: str) -> str:
    if item_name and item_name != item_code:
        return f"{item_name} ({item_code})"
    return item_code


def _format_qty(value: Any) -> str:
    """Trimmed quantity for prose: ``40.5``, not ``40.500``.

    The existing shortage numbers stay at three fixed decimals — they are being
    compared against each other and the alignment matters.  This one is read as
    a sentence.
    """
    try:
        text = f"{float(value or 0):.3f}"
    except (TypeError, ValueError):
        return "0"
    text = text.rstrip("0").rstrip(".")
    return text or "0"


def _format_stock_elsewhere(issue: Dict[str, Any]) -> str:
    """The "; 40.5 Kg is available in Nasr City - J" tail, or "" when there is none.

    At most two warehouses are named and the rest collapse into "and N more":
    this lands in a ``frappe.throw`` that already lists every short item, and a
    message nobody finishes reading blocks just as hard while helping less.

    ``alternatives`` absent means the lookup never ran; an empty list means it
    ran and found nothing.  Both add nothing to the sentence, which is why the
    distinction lives in the payload rather than here.
    """
    alternatives = issue.get("alternatives") or []
    if not alternatives:
        return ""

    named = [str(alt.get("warehouse") or "") for alt in alternatives[:2]]
    warehouses = ", ".join(name for name in named if name)
    if not warehouses:
        return ""

    remaining = len(alternatives) - len(named)
    if remaining > 0:
        warehouses = _("{0} and {1} more").format(warehouses, remaining)

    return _("; {0} {1} is available in {2}").format(
        _format_qty(issue.get("available_elsewhere")),
        issue.get("uom") or DEFAULT_UOM,
        warehouses,
    )


def _assert_material_availability(line: Dict[str, Any], company: str) -> None:
    issues = _get_material_precheck_issues(line, company)
    if not issues:
        return

    detail_parts: List[str] = []
    for issue in issues:
        item_label = _format_precheck_issue(issue.get("item_name") or issue["item_code"], issue["item_code"])
        issue_type = issue.get("type")
        if issue_type == "missing_source_warehouse":
            detail_parts.append(_("{0} has no source warehouse configured").format(item_label))
        elif issue_type == "group_source_warehouse":
            detail_parts.append(
                _("{0} uses group warehouse {1} as source warehouse").format(
                    item_label, issue.get("source_warehouse") or ""
                )
            )
        else:
            detail_parts.append(
                _("{0} in Warehouse {1} is short by {2} {3} (required {4}, available {5})").format(
                    item_label,
                    issue.get("source_warehouse") or "",
                    f"{float(issue.get('missing_qty') or 0):.3f}",
                    issue.get("uom") or DEFAULT_UOM,
                    f"{float(issue.get('required_qty') or 0):.3f}",
                    f"{float(issue.get('available_qty') or 0):.3f}",
                )
                # Appended, not substituted: the operator still needs the
                # numbers, and now also needs to know a transfer — not a
                # purchase — is what unblocks them.
                + _format_stock_elsewhere(issue)
            )

    frappe.throw(
        _("Manufacturing pre-check failed for {0} on BOM {1}: {2}").format(
            line["item_code"],
            line["bom_name"],
            "; ".join(detail_parts),
        )
    )


def _resolve_build_basket_rollup():
    """Deferred import keeps ``production_planning`` free to reach back here."""
    from jarz_pos.services.production_planning import build_basket_rollup

    return build_basket_rollup


def _get_basket_shortages(lines: List[Dict[str, Any]], company: str) -> List[Dict[str, Any]]:
    """Raw-material shortages across the WHOLE basket, not line by line.

    ``_assert_material_availability`` measures each line against the same
    available quantity, so two lines drawing on one pile of flour could both
    pass and the pair still bust the warehouse.  Worse, the submit loop commits
    per line — line 1 succeeds and consumes the stock, then line 2 fails with a
    raw ERPNext error and the operator finds out from a "3 of 5" dialog.
    """
    try:
        return _resolve_build_basket_rollup()(lines, company).get("shortages") or []
    except Exception:
        # Never let the aggregate check block a submit the per-line checks
        # would have allowed — it is an extra guard, not a new gate.
        frappe.log_error(
            title="JARZ – basket precheck failed",
            message=f"lines={lines}\ncompany={company}\n{frappe.get_traceback()}",
        )
        return []


def _format_basket_shortage_message(shortages: List[Dict[str, Any]]) -> str:
    parts: List[str] = []
    for row in shortages:
        label = _format_precheck_issue(row.get("item_name") or row["item_code"], row["item_code"])
        if row.get("reason") == "missing_source_warehouse":
            parts.append(_("{0} has no source warehouse configured").format(label))
            continue
        parts.append(
            _("{0} in Warehouse {1} is short by {2} {3} (required {4}, available {5})").format(
                label,
                row.get("source_warehouse") or "",
                f"{float(row.get('missing_qty') or 0):.3f}",
                row.get("uom") or DEFAULT_UOM,
                f"{float(row.get('required_qty') or 0):.3f}",
                f"{float(row.get('available_qty') or 0):.3f}",
            )
            # The roll-up stamps the same two fields onto its shortage rows, so
            # the basket message can point at the other store exactly the way the
            # per-line one does.  Two shortage messages that answer "where is it"
            # differently is how an operator learns to distrust both.
            + _format_stock_elsewhere(row)
        )
    return _("Combined material shortage across the batch: {0}").format("; ".join(parts))


def _assert_basket_material_availability(lines: List[Dict[str, Any]], company: str) -> None:
    shortages = _get_basket_shortages(lines, company)
    if shortages:
        frappe.throw(_format_basket_shortage_message(shortages))


@frappe.whitelist()
def list_default_bom_items(search: str | None = None) -> List[Dict[str, Any]]:
    """List Items that have a default BOM, with basic info.

    Returns: [{ item_code, item_name, stock_uom, default_bom, bom_qty }]
    """
    _ensure_manager_access()
    term = (search or "").strip()
    cond = ""
    vals: Dict[str, Any] = {}
    if term:
        cond = "AND (i.name LIKE %(q)s OR i.item_name LIKE %(q)s)"
        vals["q"] = f"%{term}%"

    sql = f"""
        SELECT
            i.name AS item_code,
            COALESCE(i.item_name, i.name) AS item_name,
            i.stock_uom,
            b.name AS default_bom,
            b.quantity AS bom_qty
        FROM `tabBOM` b
        INNER JOIN `tabItem` i ON i.name = b.item
        WHERE b.is_default = 1
          AND b.docstatus = 1
          {cond}
        ORDER BY i.modified DESC
        LIMIT {QUERY_LIMITS.DEFAULT_LIST}
    """
    rows = frappe.db.sql(sql, vals, as_dict=True)  # type: ignore
    return [dict(r) for r in rows]


@frappe.whitelist()
def list_bom_items(limit: int = 100) -> List[Dict[str, Any]]:
    """Alias for list_default_bom_items for consistency with mobile app expectations"""
    return list_default_bom_items(search=None)


@frappe.whitelist()
def search_bom_items(search: str | None = None) -> List[Dict[str, Any]]:
    """Search BOM items - alias for list_default_bom_items with search"""
    return list_default_bom_items(search=search)


@frappe.whitelist()
def get_bom_details(item_code: str) -> Dict[str, Any]:
    """Return default BOM details for an Item, including components.

    Structure:
      {
        item_code, item_name, stock_uom, default_bom, bom_qty,
        components: [{ item_code, item_name, uom, qty_per_bom }]
      }
    """
    _ensure_manager_access()
    item_code = (item_code or "").strip()
    if not item_code:
        frappe.throw(_("item_code is required"))

    bom = frappe.db.get_value(
        "BOM",
        {"item": item_code, "is_default": 1, "docstatus": 1},
        ["name", "quantity", "company"],
        as_dict=True,
    )
    if not bom:
        frappe.throw(_(f"No submitted default BOM found for Item {item_code}"))

    item = frappe.db.get_value("Item", item_code, ["item_name", "stock_uom"], as_dict=True)
    company = (bom.get("company") if isinstance(bom, dict) else None) or _get_default_company()
    # fetch_exploded=0: the screen shows the components of THIS BOM, and the
    # operator picks them off a shelf.  A "Tiramisu Large needs eggs and flour"
    # list is not something anybody can act on when the recipe says Savoiardi.
    comps = _get_required_material_rows(
        bom["name"], company, float(bom.get("quantity") or 1), fetch_exploded=0
    )
    return {
        "item_code": item_code,
        "item_name": item.get("item_name") if item else item_code,
        "stock_uom": item.get("stock_uom") if item else DEFAULT_UOM,
        "default_bom": bom["name"],
        "bom_qty": float(bom.get("quantity") or 1),
        "components": [
            {
                "item_code": c["item_code"],
                "item_name": c.get("item_name") or c["item_code"],
                "uom": c.get("uom") or DEFAULT_UOM,
                "qty_per_bom": float(c.get("required_qty") or 0),
                "available_qty": float(c.get("available_qty") or 0),
                "source_warehouse": c.get("source_warehouse"),
            }
            for c in comps
        ],
    }


def _ensure_work_order(line: Dict[str, Any], company: str, defaults: Dict[str, str], scheduled_dt: Any) -> str:
    # Create and submit a Work Order for the given line dict
    resolved_defaults = _resolve_work_order_warehouses(line, company, defaults)
    wip_wh = resolved_defaults.get("wip_warehouse")
    fg_wh = resolved_defaults.get("fg_warehouse")
    if not wip_wh:
        wip_wh = _find_company_warehouse(company, "WIP", ["WIP", "Work In Progress"]) or None
    if not fg_wh:
        fg_wh = _find_company_warehouse(company, "Finished Goods", ["FG", "Finished Goods"]) or None
    # Strict requirement: both warehouses must be resolvable for the BOM company
    if not wip_wh or not fg_wh:
        frappe.throw(_(f"Missing WIP/FG warehouse for company {company}. Configure Manufacturing Settings or create WIP/FG warehouses."))

    wo = frappe.get_doc({
        "doctype": "Work Order",
        "company": company,
        "production_item": line["item_code"],
        "qty": float(line["item_qty"]),
        "bom_no": line["bom_name"],
        "planned_start_date": scheduled_dt,
        "transfer_material_against": "Work Order",
        # STATED, not inherited.  ERPNext's ``set_required_items`` branches on
        # this flag: 0 requires the one-level bill (``tabBOM Item``, so a
        # sub-assembly is drawn from the freezer as itself), 1 requires the
        # explosion (flour, eggs, raw cheese).  Production carries a Property
        # Setter defaulting it to "0", which is what we want — but inheriting it
        # meant one edit in Desk could invert every capacity, shortage and cost
        # number the app reports, with no code change and no signal.
        #
        # LOAD-BEARING PAIRING: this must stay in step with
        # ``_get_required_material_rows``'s ``fetch_exploded`` default (also 0).
        # The app's numbers are only true because the two agree; change one and
        # you must change the other, or the floor is checked against a bill of
        # materials no Work Order will ever consume.
        "use_multi_level_bom": 0,
        # Set defaults if present
        "wip_warehouse": wip_wh,
        "fg_warehouse": fg_wh,
    })
    # Elevate to avoid role-based blocks from mobile user
    wo.flags.ignore_permissions = True
    wo.insert()
    wo.flags.ignore_permissions = True
    wo.submit()
    return wo.name


def _resolve_make_stock_entry():
    # Resolve helper dynamically to be resilient to import timing
    if make_stock_entry:
        return make_stock_entry
    try:
        fn = frappe.get_attr("erpnext.manufacturing.doctype.work_order.work_order.make_stock_entry")
        if fn:
            return fn
    except Exception:
        pass
    # Importlib fallback
    try:
        import importlib
        mod = importlib.import_module('erpnext.manufacturing.doctype.work_order.work_order')
        fn = getattr(mod, 'make_stock_entry', None)
        return fn
    except Exception:
        return None


def _make_and_submit_se(work_order: str, purpose: str, qty: float, scheduled_dt: Any) -> str:
    creator = _resolve_make_stock_entry()
    if not creator:
        frappe.throw(_("Could not resolve ERPNext make_stock_entry helper"))
    _debug_log(
        f"About to call make_stock_entry; creator={creator!r}; "
        f"callable={callable(creator)}; purpose={purpose}; qty={qty}"
    )
    try:
        se = creator(work_order, purpose, qty=qty)  # type: ignore
    except Exception as e:
        try:
            tb = frappe.get_traceback()
        except Exception:
            tb = None
        try:
            frappe.log_error(
                title="JARZ – make_stock_entry call failed",
                message=f"creator={creator!r}\ncallable={callable(creator)}\nwork_order={work_order}\npurpose={purpose}\nqty={qty}\nerror={e}\ntraceback=\n{tb}",
            )
        except Exception:
            pass
        raise
    _debug_log(
        f"make_stock_entry() returned type={type(se)}; hasattr doctype={hasattr(se, 'doctype')}"
    )
    # Coerce return into a dict/document pair. frappe._dict has attribute access but is NOT a Document.
    is_document = isinstance(se, Document)
    is_mapping = isinstance(se, (dict, FrappeDict))
    if not is_document and not is_mapping:
        frappe.throw(_("make_stock_entry did not return a Document or dict-like mapping"))
    _apply_posting_datetime(se, scheduled_dt)
    # Ensure finished qty is set for Manufacture
    # Ensure finished qty is set for Manufacture
    try:
        if purpose == "Manufacture":
            if is_document and not getattr(se, "fg_completed_qty", None):
                se.fg_completed_qty = qty
            elif is_mapping and not se.get("fg_completed_qty"):
                se["fg_completed_qty"] = qty
    except Exception:
        pass
    try:
        if is_document:
            se.flags.ignore_permissions = True
            se.set_posting_time = 1
            se.insert()
            se.flags.ignore_permissions = True
            se.submit()
            name = se.name
        else:
            # dict path via get_doc
            # Ensure posting flag present on mapping too
            if is_mapping:
                se["set_posting_time"] = 1
            doc = frappe.get_doc(se)  # type: ignore
            doc.flags.ignore_permissions = True
            doc.set_posting_time = 1
            doc.insert()
            doc.flags.ignore_permissions = True
            doc.submit()
            name = doc.name
    except Exception as e1:
        try:
            tb = frappe.get_traceback()
        except Exception:
            tb = None
        try:
            frappe.log_error(
                title="JARZ – SE insert/submit failed",
                message=f"WO: {work_order}\nPurpose: {purpose}\nError: {e1}\nTraceback:\n{tb}",
            )
        except Exception:
            pass
        raise
    _debug_log(f"Submitted SE {name} for WO {work_order} ({purpose})")
    return name


def _build_submit_savepoint_name(index: int, line: Dict[str, Any]) -> str:
    payload = json.dumps(
        {
            "index": index,
            "item_code": line.get("item_code"),
            "bom_name": line.get("bom_name"),
            "item_qty": line.get("item_qty"),
            "scheduled_at": line.get("scheduled_at"),
        },
        sort_keys=True,
        default=str,
    )
    token = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"mfg_submit_{token}"


def _coerce_lines(lines: Any) -> List[Dict[str, Any]]:
    if isinstance(lines, str):
        try:
            lines = json.loads(lines)
        except Exception:
            frappe.throw(_("Invalid JSON payload for lines"))
    if not isinstance(lines, list):
        frappe.throw(_("lines must be a list"))
    out: List[Dict[str, Any]] = []
    for i, it in enumerate(lines):
        if not isinstance(it, dict):
            frappe.throw(_(f"lines[{i}] must be an object"))
        for req in ("item_code", "bom_name", "item_qty"):
            if not it.get(req):
                frappe.throw(_(f"Missing required field {req} in lines[{i}]"))
        out.append(it)
    return out


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


@frappe.whitelist()
def submit_work_orders(lines: Any, strict_basket: Any = True) -> Dict[str, Any]:
    """Create and submit Work Orders, then create Stock Entries for:
    - Material Transfer for Manufacture
    - Manufacture (to finish with same quantity)

    Args:
      lines: JSON/list of objects with keys: item_code, bom_name, item_qty, scheduled_at (optional ISO)
      strict_basket: when true (default), reject the whole batch up front if the
        lines are collectively short on a raw material, even where each line
        passes on its own.
    Returns per-line results with created names or error.
    """
    _ensure_manager_access()
    lines = _coerce_lines(lines)

    # Aggregate check first.  The per-line prechecks inside the loop below each
    # measure against the same available stock, so a basket can pass line by
    # line and still be short overall — and because the loop commits per line,
    # discovering that halfway through leaves real stock consumed.
    if _coerce_flag(strict_basket, default=True):
        basket_company = _get_default_company()
        for ln in lines:
            company_for_line = _get_bom_company(ln.get("bom_name"))
            if company_for_line:
                basket_company = company_for_line
                break

        basket_shortages = _get_basket_shortages(lines, basket_company)
        if basket_shortages:
            message = _format_basket_shortage_message(basket_shortages)
            return {
                "results": [{"ok": False, "error": message, "line": ln} for ln in lines],
                "basket_shortages": basket_shortages,
            }

    results: List[Dict[str, Any]] = []
    release_savepoint = getattr(frappe.db, "release_savepoint", None)
    for index, ln in enumerate(lines):
        save_point = _build_submit_savepoint_name(index, ln)
        frappe.db.savepoint(save_point)
        try:
            _debug_log(f"start line: {ln}")
            scheduled_dt = _resolve_scheduled_datetime(ln.get("scheduled_at"))
            # Always respect the BOM's company; fallback to default if missing
            company = _get_bom_company(ln["bom_name"]) or _get_default_company()
            if not company:
                frappe.throw(_("Company is not configured on BOM and no Default Company set"))
            _assert_material_availability(ln, company)
            defaults = _get_mfg_defaults(company)
            resolved_defaults = _resolve_work_order_warehouses(ln, company, defaults)

            wo_name = _ensure_work_order(ln, company, defaults, scheduled_dt)
            _debug_log(
                f"WO created: {wo_name}; company={company}; "
                f"wip={resolved_defaults.get('wip_warehouse')}; fg={resolved_defaults.get('fg_warehouse')}"
            )
            qty = float(ln["item_qty"])
            se1 = _make_and_submit_se(wo_name, "Material Transfer for Manufacture", qty, scheduled_dt)
            _debug_log(f"SE1 done for {wo_name}: {se1}")
            se2 = _make_and_submit_se(wo_name, "Manufacture", qty, scheduled_dt)
            _debug_log(f"SE2 done for {wo_name}: {se2}")
            # Refresh WO status; after Manufacture entry, it should be Completed when produced qty >= planned qty
            try:
                _set_work_order_actual_dates(wo_name, scheduled_dt)
                wo_doc = frappe.get_doc("Work Order", wo_name)
                if hasattr(wo_doc, "update_status"):
                    wo_doc.update_status()
                wo_doc.reload()
                _debug_log(f"WO status for {wo_name}: {wo_doc.status}")
            except Exception:
                pass
            results.append({
                "ok": True,
                "status": "success",
                "work_order": wo_name,
                "material_transfer": se1,
                "manufacture_entry": se2,
                "line": ln,
                "company": company,
                "wip_warehouse": getattr(wo_doc, "wip_warehouse", None) or resolved_defaults.get("wip_warehouse"),
                "fg_warehouse": getattr(wo_doc, "fg_warehouse", None) or resolved_defaults.get("fg_warehouse"),
                "wo_status": (wo_doc.status if 'wo_doc' in locals() else None),
            })
            if callable(release_savepoint):
                release_savepoint(save_point)
            frappe.db.commit()
        except Exception as e:
            try:
                frappe.db.rollback(save_point=save_point)
            except Exception:
                frappe.db.rollback()
            # Avoid exceeding title length by using a short title and detailed message
            try:
                frappe.log_error(
                    title="JARZ – Manufacturing submit failed",
                    message=f"Line: {ln}\nError: {e}",
                )
            except Exception:
                # Swallow logging issues to not mask original error in response
                pass
            results.append({"ok": False, "error": str(e), "line": ln})

    return {"results": results}

@frappe.whitelist()
def submit_single_work_order(item_code: str, bom_name: str, item_qty: float, scheduled_at: str | None = None) -> Dict[str, Any]:
    _ensure_manager_access()
    line = {
        "item_code": item_code,
        "bom_name": bom_name,
        "item_qty": float(item_qty),
        "scheduled_at": scheduled_at,
    }
    # One line cannot conflict with itself, so the aggregate pass would only
    # re-explode the same BOM the per-line precheck already handles.
    out = submit_work_orders([line], strict_basket=False)
    try:
        if isinstance(out, dict) and isinstance(out.get("results"), list) and out["results"]:
            first = out["results"][0]
            if isinstance(first, dict):
                return first  # Flatten for single-wo convenience
    except Exception:
        pass
    return out


@frappe.whitelist()
def list_recent_work_orders(limit: int = 50) -> List[Dict[str, Any]]:
    """Return recent Work Orders sorted by creation (last added first)."""
    _ensure_manager_access()

    rows = frappe.get_all(
        "Work Order",
        filters={},
        fields=[
            "name",
            "production_item",
            "qty",
            "bom_no",
            "status",
            "company",
            "planned_start_date",
            "wip_warehouse",
            "fg_warehouse",
            "creation",
        ],
        order_by="creation desc",
        limit=limit,
    )
    # Cast/normalize types
    out: List[Dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "name": r.get("name"),
                "production_item": r.get("production_item"),
                "qty": float(r.get("qty") or 0),
                "bom_no": r.get("bom_no"),
                "status": r.get("status"),
                "company": r.get("company"),
                "planned_start_date": r.get("planned_start_date"),
                "wip_warehouse": r.get("wip_warehouse"),
                "fg_warehouse": r.get("fg_warehouse"),
                "creation": r.get("creation"),
            }
        )
    return out


# ══════════════════════════════════════════════════════════════════════════
# Production Board — Stage 2: start / finish a batch on the floor
# ══════════════════════════════════════════════════════════════════════════
#
# ``submit_work_orders`` / ``submit_single_work_order`` above stay exactly as
# they are: they post the transfer AND the manufacture in one shot, which is
# the "Quick produce" path for a manager recording something already made.
#
# The pair below splits that in two, because on a real floor the batch exists
# for hours between those two events: the material is out of the store and the
# cake is not made yet.  ``start`` posts the transfer only; ``finish`` posts
# the manufacture, and only ``finish`` knows the quantity that actually came
# out the other end.


def _flt(value: Any, default: float = 0.0) -> float:
    """``float()`` that degrades instead of raising on ``None``/junk/mocks."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _resolve_current_user() -> Any:
    try:
        return frappe.session.user
    except Exception:
        return None


def _get_item_stock_uom(item_code: str) -> str:
    try:
        return _coerce_str(frappe.db.get_value("Item", item_code, "stock_uom")) or DEFAULT_UOM
    except Exception:
        return DEFAULT_UOM


def _resolve_active_sop_stamp(item_code: str) -> str:
    """``"SOP-0001#3"`` for the item's active SOP, or ``""`` when it has none.

    Deferred import keeps this module independent of the SOP feature, and the
    broad catch is deliberate: a batch must never fail to start because the SOP
    lookup had a bad day.  The format comes from ``sop_rendering`` rather than
    an f-string here so the writer and both readers cannot drift apart.
    """
    try:
        from jarz_pos.services.sop_rendering import format_version_stamp

        row = frappe.db.get_value(
            "Jarz SOP",
            {"item_code": item_code, "is_active": 1},
            ["name", "version"],
            as_dict=True,
        )
        if not row:
            return ""
        return format_version_stamp(row.get("name"), row.get("version"))
    except Exception:
        _debug_log(f"SOP stamp lookup failed for {item_code}")
        return ""


def _stamp_work_order(work_order: str, values: Dict[str, Any]) -> None:
    """Write the Jarz floor-tracking fields onto a *submitted* Work Order.

    ``db.set_value`` rather than a save: these are plain custom fields on a
    submitted document, and ``update_modified=False`` keeps the write from
    tripping a concurrent reader's timestamp check.

    Failures are logged and swallowed on purpose.  By the time this runs the
    Stock Entry is already submitted — raising here would hand the caller an
    error for a batch that really did start, and the retry would transfer the
    material twice.
    """
    try:
        frappe.db.set_value("Work Order", work_order, values, update_modified=False)
    except Exception:
        frappe.log_error(
            title="JARZ – work order stamp failed",
            message=f"work_order={work_order} values={values}",
        )


def _elapsed_minutes(started_at: Any, now: Any = None) -> Optional[float]:
    if not started_at:
        return None
    try:
        start = get_datetime(started_at)
        end = get_datetime(now if now is not None else _resolve_now_datetime())
        return round((end - start).total_seconds() / 60.0, 1)
    except Exception:
        return None


def _compute_batch_cost(
    work_order: str,
    produced_qty: Any = None,
    bom_no: Any = None,
) -> Dict[str, Any]:
    """What the batch actually consumed, against what the BOM said it would.

    ``material_cost`` is the sum of the submitted *transfer* Stock Entry lines —
    everything that left the store for this Work Order.  Not the Manufacture
    entry: that one is valued off the transfer anyway, and reading it would
    double-count on a batch finished in two goes.

    Every division is guarded.  ``produced_qty == 0`` (started, not yet
    finished) and a BOM with no cost both return ``None`` rather than raising or
    reporting a fake zero.
    """
    rows = _resolve_stock_entry_detail_rows(work_order, "Material Transfer for Manufacture")
    material_cost = 0.0
    for row in rows:
        material_cost += _flt(row.get("amount"))

    produced = _flt(produced_qty)
    cost_per_unit = (material_cost / produced) if produced > 0 else None

    standard_per_unit = None
    bom = _resolve_bom_cost(bom_no)
    if bom:
        bom_qty = _flt(bom.get("quantity"))
        bom_total = _flt(bom.get("total_cost"))
        # A costed BOM is the whole point of the comparison; a BOM whose total
        # cost is 0 has never been costed, and dividing it out would render as
        # "100% over standard" on every batch.
        if bom_qty > 0 and bom_total > 0:
            standard_per_unit = bom_total / bom_qty

    standard_cost = None
    variance_amount = None
    variance_pct = None
    if standard_per_unit is not None and produced > 0:
        standard_cost = standard_per_unit * produced
        variance_amount = material_cost - standard_cost
        if standard_cost > 0:
            variance_pct = (variance_amount / standard_cost) * 100.0

    return {
        "work_order": work_order,
        "bom_no": bom_no,
        "produced_qty": produced,
        "material_cost": material_cost,
        "cost_per_unit": cost_per_unit,
        "standard_per_unit": standard_per_unit,
        "standard_cost": standard_cost,
        "variance_amount": variance_amount,
        "variance_pct": variance_pct,
        "transfer_entries": sorted({r["stock_entry"] for r in rows if r.get("stock_entry")}),
    }


def _get_wip_leftover_rows(work_order: str) -> List[Dict[str, Any]]:
    """Material still physically in WIP for this Work Order.

    ERPNext's own reconciliation only counts the three manufacturing purposes,
    so a plain ``Material Transfer`` back out of WIP stays invisible to it.  Our
    own returns are therefore netted off here — otherwise a second call would
    cheerfully try to move stock that already went back to the store.
    """
    returned: Dict[Any, float] = {}
    for row in _resolve_stock_entry_detail_rows(work_order, "Material Transfer"):
        key = (row.get("item_code"), row.get("s_warehouse"))
        returned[key] = returned.get(key, 0.0) + _flt(row.get("qty"))

    out: List[Dict[str, Any]] = []
    for row in _resolve_wip_available_materials(work_order):
        remaining = _flt(row.get("qty")) - returned.get((row.get("item_code"), row.get("warehouse")), 0.0)
        if remaining > QTY_TOLERANCE:
            out.append(dict(row, qty=remaining))
    return out


@frappe.whitelist()
def start_production_batch(
    item_code: str,
    bom_name: str,
    item_qty: Any,
    scheduled_at: str | None = None,
    wip_warehouse: str | None = None,
    fg_warehouse: str | None = None,
) -> Dict[str, Any]:
    """Open a batch: create the Work Order and move its materials into WIP.

    Posts **exactly one** Stock Entry — ``Material Transfer for Manufacture``.
    The ``Manufacture`` entry belongs to :func:`finish_production_batch` and
    posting it here would just re-create the quick-produce path this endpoint
    exists to replace.
    """
    _ensure_production_execute_access()

    item_code = _coerce_str(item_code)
    bom_name = _coerce_str(bom_name)
    if not item_code:
        frappe.throw(_("item_code is required"))
    if not bom_name:
        frappe.throw(_("bom_name is required"))

    qty = _flt(item_qty)
    if qty <= 0:
        frappe.throw(_("Quantity to produce must be greater than zero"))

    line: Dict[str, Any] = {
        "item_code": item_code,
        "bom_name": bom_name,
        "item_qty": qty,
        "wip_warehouse": _coerce_str(wip_warehouse) or None,
        "fg_warehouse": _coerce_str(fg_warehouse) or None,
    }

    company = _get_bom_company(bom_name) or _get_default_company()
    if not company:
        frappe.throw(_("Company is not configured on BOM and no Default Company set"))

    scheduled_dt = _resolve_scheduled_datetime(scheduled_at)
    _assert_posting_date_allowed(scheduled_dt)
    _assert_material_availability(line, company)
    # Priced once and reused for the response — a second BOM explosion here
    # would double the cost of every start.
    priced = _assert_batch_value_within_threshold(line, company)

    defaults = _get_mfg_defaults(company)
    resolved = _resolve_work_order_warehouses(line, company, defaults)
    wo_name = _ensure_work_order(line, company, defaults, scheduled_dt)
    _debug_log(f"batch start: WO {wo_name} for {item_code} x{qty}")

    material_transfer = _make_and_submit_se(
        wo_name, "Material Transfer for Manufacture", qty, scheduled_dt
    )

    stamp_values: Dict[str, Any] = {
        "jarz_started_by": _resolve_current_user(),
        "jarz_started_at": scheduled_dt,
    }

    # Pin the SOP version this batch was actually made by.
    #
    # Without it, `get_sop_for_work_order` falls back to whatever SOP is active
    # *now*, so editing an SOP silently rewrites the method every past batch was
    # made by — and `Jarz SOP.validate`'s "already used in production" guard,
    # which matches on `jarz_sop_version LIKE '<name>#%'`, never fires either.
    # Never fail a batch over this: an unstamped Work Order degrades to the
    # active SOP, which is exactly the old behaviour.
    sop_stamp = _resolve_active_sop_stamp(item_code)
    if sop_stamp:
        stamp_values["jarz_sop_version"] = sop_stamp

    _stamp_work_order(wo_name, stamp_values)

    try:
        wo_doc = _resolve_work_order_doc(wo_name)
    except Exception:
        wo_doc = None

    return {
        "work_order": wo_name,
        "material_transfer": material_transfer,
        "status": getattr(wo_doc, "status", None),
        "planned_qty": qty,
        "uom": _get_item_stock_uom(item_code),
        "wip_warehouse": getattr(wo_doc, "wip_warehouse", None) or resolved.get("wip_warehouse"),
        "fg_warehouse": getattr(wo_doc, "fg_warehouse", None) or resolved.get("fg_warehouse"),
        "estimated_material_cost": _flt(priced.get("total_value")),
        "company": company,
        "scheduled_at": str(scheduled_dt),
        "components": [
            {
                "item_code": c["item_code"],
                "item_name": c.get("item_name") or c["item_code"],
                "uom": c.get("uom") or DEFAULT_UOM,
                "required_qty": _flt(c.get("required_qty")),
                "source_warehouse": c.get("source_warehouse"),
                "valuation_rate": _flt(c.get("valuation_rate")),
                "estimated_amount": _flt(c.get("estimated_amount")),
            }
            for c in (priced.get("components") or [])
        ],
    }


@frappe.whitelist()
def finish_production_batch(
    work_order: str,
    actual_qty: Any,
    scrap_qty: Any = 0,
    scheduled_at: str | None = None,
    notes: str | None = None,
) -> Dict[str, Any]:
    """Close a batch: post the Manufacture entry for what was ACTUALLY made.

    Scrap model, decided and implemented exactly as follows:

    * ``actual_qty`` is the good output and is what drives the Manufacture
      entry.  Not the planned quantity — a batch that yields 47 of a planned 50
      must book 47, or finished-goods stock is a lie from the first shift.
    * ``scrap_qty`` is recorded on the Work Order as a reported figure only.  It
      posts no stock: routing it to a scrap warehouse needs per-item scrap items
      that do not exist, and inventing them silently would be worse than a
      number an operator can be asked about.
    * Material transferred but not consumed stays in WIP and is surfaced as
      ``wip_leftover_qty``.  :func:`return_wip_to_store` is how it gets home.
    """
    _ensure_production_execute_access()

    work_order = _coerce_str(work_order)
    if not work_order:
        frappe.throw(_("work_order is required"))

    # Row lock FIRST, before anything is read for a decision.  Two operators
    # finishing the same batch from two tablets is an ordinary Tuesday; without
    # the lock both read the same transferred quantity and both post.
    wo = _resolve_work_order_doc(work_order, for_update=True)

    if int(_flt(getattr(wo, "docstatus", 0))) != 1:
        frappe.throw(_("Work Order {0} is not submitted").format(work_order))
        return {}

    transferred = _flt(getattr(wo, "material_transferred_for_manufacturing", 0))
    if transferred <= 0:
        frappe.throw(
            _("Work Order {0} has no material in WIP — this batch was never started").format(
                work_order
            )
        )
        return {}

    actual = _flt(actual_qty)
    if actual <= 0:
        frappe.throw(_("Produced quantity must be greater than zero"))
        return {}

    planned = _flt(getattr(wo, "qty", 0))
    if (
        planned > 0
        and actual > planned + QTY_TOLERANCE
        and not _setting_flag("production_allow_over_production", False)
    ):
        frappe.throw(
            _(
                "Produced quantity {0} is more than the planned {1}. "
                "Over-production is switched off in Jarz POS Settings."
            ).format(f"{actual:g}", f"{planned:g}")
        )
        return {}

    produced_before = _flt(getattr(wo, "produced_qty", 0))
    remaining = transferred - produced_before
    if actual > remaining + QTY_TOLERANCE:
        # Pre-validated here on purpose: ERPNext checks this deep inside the
        # Stock Entry and throws a message about fg_completed_qty and
        # over-production percentages that means nothing to somebody holding a
        # tray. The operator gets ours instead.
        frappe.throw(
            _(
                "Only {0} of this batch is still in WIP, but {1} was entered. "
                "Transfer more material or reduce the quantity."
            ).format(f"{remaining:g}", f"{actual:g}")
        )
        return {}

    scrap = max(0.0, _flt(scrap_qty))
    notes_text = notes if isinstance(notes, str) else None

    scheduled_dt = _resolve_scheduled_datetime(scheduled_at)
    _assert_posting_date_allowed(scheduled_dt)

    # The ACTUAL quantity, never wo.qty.
    manufacture_entry = _make_and_submit_se(work_order, "Manufacture", actual, scheduled_dt)
    _debug_log(f"batch finish: WO {work_order} actual={actual} scrap={scrap}")

    _stamp_work_order(
        work_order,
        {
            "jarz_finished_by": _resolve_current_user(),
            "jarz_finished_at": scheduled_dt,
            "jarz_scrap_qty": scrap,
            "jarz_batch_notes": notes_text,
        },
    )

    try:
        _set_work_order_actual_dates(work_order, scheduled_dt)
    except Exception:
        frappe.log_error(
            title="JARZ – work order actual dates failed",
            message=f"work_order={work_order}",
        )

    refreshed = None
    try:
        refreshed = _resolve_update_work_order_status(work_order)
    except Exception:
        frappe.log_error(
            title="JARZ – work order status refresh failed",
            message=f"work_order={work_order}",
        )

    produced_after = _flt(getattr(refreshed, "produced_qty", None)) or (produced_before + actual)
    bom_no = getattr(refreshed, "bom_no", None) or getattr(wo, "bom_no", None)

    return {
        "work_order": work_order,
        "manufacture_entry": manufacture_entry,
        "actual_qty": actual,
        "scrap_qty": scrap,
        "status": getattr(refreshed, "status", None) or getattr(wo, "status", None),
        "planned_qty": planned,
        "produced_qty": produced_after,
        "wip_leftover_qty": max(0.0, transferred - produced_after),
        "cost": _compute_batch_cost(work_order, produced_qty=produced_after, bom_no=bom_no),
    }


@frappe.whitelist()
def list_running_work_orders(limit: Any = 50) -> List[Dict[str, Any]]:
    """Batches that are open on the floor right now.

    "Running" means submitted, not yet complete, **and** with material actually
    transferred into WIP.  That last clause is what separates a batch somebody
    is standing over from a Work Order that was merely created.
    """
    _ensure_production_view_access()

    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 50
    limit = max(1, min(limit, MAX_RUNNING_WORK_ORDERS))

    filters = {
        "docstatus": 1,
        "status": ["in", list(RUNNING_WORK_ORDER_STATUSES)],
        "material_transferred_for_manufacturing": [">", 0],
    }
    rows = _fetch_running_work_orders(filters, limit)

    now = _resolve_now_datetime()
    out: List[Dict[str, Any]] = []
    for r in rows or []:
        transferred = _flt(r.get("material_transferred_for_manufacturing"))
        produced = _flt(r.get("produced_qty"))
        started_at = r.get("jarz_started_at")
        out.append(
            {
                "name": r.get("name"),
                "production_item": r.get("production_item"),
                "item_name": r.get("item_name") or r.get("production_item"),
                "qty": _flt(r.get("qty")),
                "produced_qty": produced,
                "bom_no": r.get("bom_no"),
                "status": r.get("status"),
                "jarz_started_by": r.get("jarz_started_by"),
                "jarz_started_at": started_at,
                "elapsed_minutes": _elapsed_minutes(started_at, now),
                "wip_warehouse": r.get("wip_warehouse"),
                "fg_warehouse": r.get("fg_warehouse"),
                "stock_uom": r.get("stock_uom") or DEFAULT_UOM,
                "material_transferred_qty": transferred,
                # What is still sitting in WIP against this batch — the number
                # that turns stranded material into something visible.
                "wip_leftover_qty": max(0.0, transferred - produced),
            }
        )
    return out


def _fetch_running_work_orders(filters: Dict[str, Any], limit: int) -> List[Dict[str, Any]]:
    try:
        return frappe.get_all(
            "Work Order",
            filters=filters,
            fields=RUNNING_WORK_ORDER_FIELDS + JARZ_WORK_ORDER_FIELDS,
            order_by="jarz_started_at desc, modified desc",
            limit=limit,
        )
    except Exception:
        # The jarz_* custom fields land with a migrate.  A board that 500s in
        # the window between deploy and migrate is worse than one missing two
        # columns — but degrading silently is how a v16 rejection hid for a
        # month last time, so this is logged loudly.
        frappe.log_error(
            title="JARZ – running work orders fell back",
            message=f"jarz_* Work Order fields unavailable; filters={filters}",
        )
        return frappe.get_all(
            "Work Order",
            filters=filters,
            fields=RUNNING_WORK_ORDER_FIELDS,
            order_by="modified desc",
            limit=limit,
        )


@frappe.whitelist()
def get_batch_cost(work_order: str) -> Dict[str, Any]:
    """Material cost of one batch against its BOM standard."""
    _ensure_production_view_access()

    work_order = _coerce_str(work_order)
    if not work_order:
        frappe.throw(_("work_order is required"))

    wo = _resolve_work_order_doc(work_order)
    return _compute_batch_cost(
        work_order,
        produced_qty=getattr(wo, "produced_qty", 0),
        bom_no=getattr(wo, "bom_no", None),
    )


@frappe.whitelist()
def return_wip_to_store(work_order: str) -> Dict[str, Any]:
    """Send material that never got consumed back out of WIP.

    Stranded WIP is the single biggest risk in splitting start from finish:
    every short batch leaves the difference sitting in a warehouse nobody
    counts, and without this it accumulates until someone does a stock take.
    Manager-gated, because it is a stock correction rather than a floor action.

    Posts a plain ``Material Transfer`` (WIP → each component's source
    warehouse).  Deliberately NOT ``Material Consumption for Manufacture``: the
    material is going back on the shelf, not into the product.
    """
    _ensure_manager_access()

    work_order = _coerce_str(work_order)
    if not work_order:
        frappe.throw(_("work_order is required"))

    wo = _resolve_work_order_doc(work_order, for_update=True)
    if int(_flt(getattr(wo, "docstatus", 0))) != 1:
        frappe.throw(_("Work Order {0} is not submitted").format(work_order))
        return {}

    return _post_wip_return(
        wo,
        work_order,
        remarks=f"Jarz: WIP returned to store for Work Order {work_order}",
    )


def _post_wip_return(wo: Any, work_order: str, remarks: str) -> Dict[str, Any]:
    """Post the WIP -> source-warehouse Material Transfer for one Work Order.

    Shared by :func:`return_wip_to_store` (a manager clearing stranded material
    off a short batch) and :func:`cancel_production_batch` (the floor aborting a
    batch outright). One implementation on purpose: the two differ only in who
    may call them and what the remark says, and a second copy would be the place
    the netting-off of previous returns silently drifted.
    """
    leftover = _get_wip_leftover_rows(work_order)
    if not leftover:
        frappe.throw(_("Nothing is left in WIP for Work Order {0}").format(work_order))
        return {}

    sources = _resolve_work_order_source_warehouses(work_order)
    fallback_source = _coerce_str(getattr(wo, "source_warehouse", "") or "")

    items: List[Dict[str, Any]] = []
    unplaced: List[str] = []
    for row in leftover:
        target = sources.get(row["item_code"]) or fallback_source
        if not target:
            unplaced.append(row["item_code"])
            continue
        if target == row["warehouse"]:
            continue
        items.append(
            {
                "item_code": row["item_code"],
                "qty": row["qty"],
                "s_warehouse": row["warehouse"],
                "t_warehouse": target,
            }
        )

    if unplaced:
        frappe.throw(
            _("No source warehouse is recorded for {0}, so it cannot be returned").format(
                ", ".join(sorted(set(unplaced)))
            )
        )
        return {}
    if not items:
        frappe.throw(_("Nothing is left in WIP for Work Order {0}").format(work_order))
        return {}

    now_dt = get_datetime(_resolve_now_datetime())
    payload = {
        "doctype": "Stock Entry",
        "stock_entry_type": "Material Transfer",
        "purpose": "Material Transfer",
        "company": getattr(wo, "company", None),
        # Linked for traceability. ERPNext explicitly preserves work_order on a
        # plain Material Transfer, and leaves fg_completed_qty out of it, so the
        # link changes no quantity on the Work Order.
        "work_order": work_order,
        "remarks": remarks,
        "items": items,
    }
    _apply_posting_datetime(payload, now_dt)

    doc = frappe.get_doc(payload)
    doc.flags.ignore_permissions = True
    doc.insert()
    doc.flags.ignore_permissions = True
    doc.submit()
    _debug_log(f"WIP return for {work_order}: {doc.name} ({len(items)} line(s))")

    return {
        "work_order": work_order,
        "stock_entry": doc.name,
        "returned_items": items,
    }


@frappe.whitelist()
def cancel_production_batch(work_order: str, reason: str) -> Dict[str, Any]:
    """Abort a batch that was started but must not be finished.

    Splitting production into ``start`` and ``finish`` created a state the old
    one-shot flow never had: a batch that is open on the floor. Until now the
    only way out of it was forward — post a Manufacture entry for goods that
    were never made — because there was no way back. Operators started a batch
    on the wrong item, or for the wrong quantity, and then had to choose between
    faking an output and leaving the Work Order running for ever.

    What this does, in order:

    1. Refuses if anything has already been produced. Once a Manufacture entry
       exists, finished goods are in stock and unwinding them is an accounting
       reversal, not a cancel — the batch must be finished for what it actually
       made. The message names the alternative rather than just saying no.
    2. Returns everything still in WIP to the warehouse each component came
       from, via the same Material Transfer :func:`return_wip_to_store` posts.
       A cancelled batch that left its material in WIP would be worse than no
       cancel at all: the stranded stock is invisible until a stock take.
    3. Stops the Work Order. ``Stopped`` is ERPNext's own terminal state for
       "this will not be produced", and it is deliberately not in
       ``RUNNING_WORK_ORDER_STATUSES``, so the batch leaves the floor board.

    Full cancellation (docstatus 2) is not used on purpose. It would require
    cancelling the original Material Transfer for Manufacture, which rewrites
    the stock ledger at the original posting date and fails outright once that
    period is closed. Reversing with a new entry keeps the audit trail — the
    material went out, then came back, and the reason is on the Work Order.

    Gated at execute level rather than manager: a mis-started batch is a floor
    mistake made minutes ago, and routing every one through a manager is how
    operators learn to fake the finish instead.
    """
    _ensure_production_execute_access()

    work_order = _coerce_str(work_order)
    if not work_order:
        frappe.throw(_("work_order is required"))

    reason = _coerce_str(reason)
    if not reason:
        frappe.throw(_("A reason is required to cancel a batch"))

    # Row lock FIRST, exactly as `finish_production_batch` does: two tablets
    # racing a cancel against a finish must not both read produced_qty = 0.
    wo = _resolve_work_order_doc(work_order, for_update=True)

    if int(_flt(getattr(wo, "docstatus", 0))) != 1:
        frappe.throw(_("Work Order {0} is not submitted").format(work_order))
        return {}

    status = _coerce_str(getattr(wo, "status", "") or "")
    if status in ("Stopped", "Closed", "Completed"):
        frappe.throw(
            _("Work Order {0} is already {1} and cannot be cancelled").format(work_order, status)
        )
        return {}

    produced = _flt(getattr(wo, "produced_qty", 0))
    if produced > QTY_TOLERANCE:
        frappe.throw(
            _(
                "{0} has already been produced against Work Order {1}, so it cannot "
                "be cancelled. Finish the batch for what was actually made instead."
            ).format(produced, work_order)
        )
        return {}

    # Return whatever is still in WIP. A batch can legitimately have nothing
    # there — someone already returned it by hand — and that must not block the
    # cancel, so the "nothing left" throw is caught here rather than in the
    # shared helper, which is right to refuse for its own caller.
    stock_entry: Optional[str] = None
    returned_items: List[Dict[str, Any]] = []
    if _get_wip_leftover_rows(work_order):
        result = _post_wip_return(
            wo,
            work_order,
            remarks=f"Jarz: batch cancelled - {reason}",
        )
        stock_entry = result.get("stock_entry")
        returned_items = result.get("returned_items") or []

    _stop_work_order(work_order)

    _stamp_work_order(
        work_order,
        {
            "jarz_cancelled_by": _resolve_current_user(),
            "jarz_cancelled_at": get_datetime(_resolve_now_datetime()),
            "jarz_cancel_reason": reason,
        },
    )
    _debug_log(f"batch cancelled: {work_order} ({reason})")

    return {
        "work_order": work_order,
        "status": "Stopped",
        "reason": reason,
        "stock_entry": stock_entry,
        "returned_items": returned_items,
    }


def _stop_work_order(work_order: str) -> None:
    """Move a Work Order to ``Stopped``.

    ERPNext exposes this as ``WorkOrder.stop_unstop(status)``, which does more
    than write a field: it also cancels the reserved-quantity entries and
    updates the linked production plan. Calling the method is therefore the
    correct path and a bare ``db_set('status', 'Stopped')`` is not.

    The fallback exists because ``stop_unstop`` has moved between ERPNext
    versions, and a cancel whose material has ALREADY been returned must not
    fail on the last step — that would leave the batch on the board with an
    empty WIP, which reads as "nothing was transferred" and invites a second
    start. Writing the status directly is a worse outcome than the method call,
    and a better one than stopping half way.
    """
    try:
        doc = frappe.get_doc("Work Order", work_order)
        doc.flags.ignore_permissions = True
        doc.stop_unstop("Stopped")
        return
    except Exception:
        frappe.log_error(
            title="JARZ - work order stop fell back",
            message=f"stop_unstop failed for {work_order}; writing status directly",
        )

    frappe.db.set_value("Work Order", work_order, "status", "Stopped", update_modified=False)
