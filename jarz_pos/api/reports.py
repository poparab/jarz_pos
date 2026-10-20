"""
Reports API for the Jarz POS mobile app.

Provides stock-level reports grouped by item group, with warehouse
breakdowns and totals.
"""

from __future__ import annotations

import json
from typing import Any, Optional

import frappe
from frappe import _
from frappe.utils import add_days, getdate, nowdate

from jarz_pos.constants import ROLES

# Pure maths shared with the branch replenishment plan, so "sells per day" and
# "days of cover" mean the same thing on the stock report as on the plan.
from jarz_pos.services.replenishment_planning import days_of_cover, sells_per_day

#: Days-of-cover on the Final Products report. The rate is net jars sold per
#: day over the last ``_COVER_SALES_DAYS`` whole days (ending yesterday); the
#: app colours a quantity cell red when it is <= 0 or covers fewer than
#: ``_COVER_RED_BELOW`` days, yellow from there up to ``_COVER_GREEN_ABOVE``
#: days, and green above it. ``None`` cover (no recent sales) is not coloured.
_COVER_SALES_DAYS = 14
_COVER_RED_BELOW = 3
_COVER_GREEN_ABOVE = 7


def _cover_meta() -> dict[str, int]:
	return {
		"sales_days": _COVER_SALES_DAYS,
		"red_below": _COVER_RED_BELOW,
		"green_above": _COVER_GREEN_ABOVE,
	}


def _ensure_jarz_manager():
	"""Raise if the current user is not a JARZ Manager."""
	roles = set(frappe.get_roles(frappe.session.user))
	if ROLES.JARZ_MANAGER not in roles and ROLES.ADMINISTRATOR not in roles:
		frappe.throw(_("Only JARZ Manager can access reports"), frappe.PermissionError)


def _ensure_materials_report_access():
	"""Materials & Consumables — the line-manager tier (line manager and above)."""
	roles = set(frappe.get_roles(frappe.session.user))
	if not roles.intersection(ROLES.LINE_MANAGER_TIER):
		frappe.throw(_("You are not permitted to access this report"), frappe.PermissionError)


#: Item groups each bucket of the Materials & Consumables report covers.
#:
#: Every name is expanded to its descendants before filtering, so moving these
#: under a parent group — or splitting packaging and labels out of Raw Material —
#: does not silently empty a bucket. A name that does not exist on a given site
#: is skipped rather than raising, which keeps the roots list safe to extend
#: ahead of the data.
_MATERIAL_ROOTS = ("Raw Material", "Packaging", "Labels")
_SUB_ASSEMBLY_ROOTS = ("Sub Assemblies",)
_CONSUMABLE_ROOTS = ("Consumable",)


def _finished_goods_warehouses() -> list[str]:
	"""The finished-goods store, so the Final Products tables always show it.

	The column set used to be "whatever warehouses happen to hold one of these
	items", which moves under the report. On production 2026-09-18 that meant
	the Large table had no ``Finished Goods`` column at all — no Large size was
	sitting in the store that day — while the Medium table grew a ``Raw
	Material`` column off a single stray Bin row. A stock report whose columns
	come and go cannot be read side by side, so the store is pinned here and
	everything else is added on top.

	Read off the **Company**, not Manufacturing Settings: v16's
	``set_company_wise_warehouses`` patch moved the setting, and
	``get_single_value`` *raises* on a field the meta no longer has. Production
	still answers the old address from a stale ``tabSingles`` row left by v15,
	so the old spelling looks fine there and throws on a site without it —
	``_get_mfg_defaults`` in ``api/manufacturing.py`` reads the Company the
	same way.
	"""
	try:
		rows = frappe.get_all("Company", fields=["default_fg_warehouse"])
	except Exception:
		return []

	names: list[str] = []
	for row in rows:
		warehouse = row.get("default_fg_warehouse")
		if not warehouse or warehouse in names:
			continue
		try:
			if frappe.db.get_value("Warehouse", warehouse, "is_group"):
				continue
		except Exception:
			pass
		names.append(warehouse)
	return names


def _expand_item_groups(roots: tuple) -> list[str]:
	"""Each named group plus every group beneath it in the Item Group tree."""
	names: list[str] = []
	for root in roots:
		bounds = frappe.db.get_value("Item Group", root, ["lft", "rgt"], as_dict=True)
		if not bounds:
			continue
		names.extend(
			row["name"]
			for row in frappe.get_all(
				"Item Group",
				filters={"lft": [">=", bounds["lft"]], "rgt": ["<=", bounds["rgt"]]},
				fields=["name"],
			)
		)
	return names


def branch_warehouse(branch: str | None) -> str | None:
	"""The stock warehouse of a branch (POS Profile), or None when no branch.

	Raises a clear ValidationError when the branch has no warehouse, rather than
	silently reporting company-wide stock under a branch's name.
	"""
	branch = (branch or "").strip()
	if not branch:
		return None
	warehouse = (frappe.db.get_value("POS Profile", branch, "warehouse") or "").strip()
	if not warehouse:
		frappe.throw(
			_("Branch {0} has no stock warehouse configured on its POS Profile.").format(branch),
			frappe.ValidationError,
		)
	return warehouse


def _bin_maps(item_codes: list[str], warehouse: str | None):
	"""``(qty_map, value_map)``: item → {warehouse: qty} and item → {warehouse: value}."""
	filters: dict[str, Any] = {"item_code": ["in", item_codes], "actual_qty": ["!=", 0]}
	if warehouse:
		filters["warehouse"] = warehouse
	bins = frappe.get_all(
		"Bin",
		filters=filters,
		fields=["item_code", "warehouse", "actual_qty", "stock_value"],
	)
	qty_map: dict[str, dict[str, float]] = {}
	value_map: dict[str, dict[str, float]] = {}
	for b in bins:
		qty_map.setdefault(b["item_code"], {})[b["warehouse"]] = float(b["actual_qty"])
		value_map.setdefault(b["item_code"], {})[b["warehouse"]] = float(b.get("stock_value") or 0)
	return qty_map, value_map


def _log_degraded_read(title: str, message: str) -> None:
	"""Log a degraded read without letting the logger itself break the report.

	``frappe.log_error`` reads System Settings outside its own try block, so it
	can raise from inside an ``except`` — same guard as
	``api/replenishment._log_failure``.
	"""
	title = (title or "JARZ Reports")[:140]
	try:
		frappe.log_error(title=title, message=message)
		return
	except Exception as logging_error:
		fallback = f"{title}: {message} (log_error itself failed: {logging_error})"
	try:
		frappe.logger().error(fallback)
	except Exception:
		return


def _jar_sales_by_warehouse(
	item_codes: list[str],
	warehouse: str | None,
	sales_days: int,
) -> dict[tuple[str, str], float]:
	"""Net quantity sold per ``(warehouse, item_code)`` over the cover window.

	Same shape as ``api/replenishment._resolve_sales``: submitted Sales
	Invoices only, keyed on ``Sales Invoice Item.warehouse``, ``SUM(stock_qty)``
	so returns (negative qty) net themselves out, and the window is whole
	completed days ending yesterday so the rate does not sag through the
	morning.

	With ``warehouse`` it reads that warehouse only. Without it, it reads EVERY
	warehouse — not just the report's columns — because the totals and the
	finished-goods column need company-wide demand. Rows with no warehouse are
	skipped (they cannot be keyed).

	Fail-soft: any failure is logged and ``{}`` is returned, which leaves every
	cover ``None``. The stock report must never break because the sales read did.
	"""
	codes = sorted({str(c) for c in item_codes or [] if c})
	if not codes or sales_days <= 0:
		return {}

	try:
		today = getdate(nowdate())
		params: dict[str, Any] = {
			"from_date": add_days(today, -sales_days),
			"to_date": add_days(today, -1),
			"codes": codes,
		}
		warehouse_clause = "AND IFNULL(sii.warehouse, '') != ''"
		if warehouse:
			warehouse_clause = "AND sii.warehouse = %(warehouse)s"
			params["warehouse"] = warehouse
		rows = frappe.db.sql(
			f"""
			SELECT sii.warehouse AS warehouse,
			       sii.item_code AS item_code,
			       SUM(sii.stock_qty) AS qty
			FROM `tabSales Invoice Item` sii
			INNER JOIN `tabSales Invoice` si ON si.name = sii.parent
			WHERE si.docstatus = 1
			  AND si.posting_date BETWEEN %(from_date)s AND %(to_date)s
			  AND sii.item_code IN %(codes)s
			  {warehouse_clause}
			GROUP BY sii.warehouse, sii.item_code
			""",
			params,
			as_dict=True,
		)
		return {
			(str(row["warehouse"]), str(row["item_code"])): float(row.get("qty") or 0)
			for row in rows or []
		}
	except Exception:
		try:
			tb = frappe.get_traceback()
		except Exception:
			tb = ""
		_log_degraded_read(
			"JARZ Reports - final products sales read failed",
			f"warehouse={warehouse} days={sales_days}\n{tb}",
		)
		return {}


def _sales_rates(
	sales: dict[tuple[str, str], float], sales_days: int
) -> tuple[dict[str, dict[str, float]], dict[str, float]]:
	"""``(rate_map, company_rate)``: item → {warehouse: rate}, item → summed rate.

	Each warehouse's rate goes through ``sells_per_day`` on its own (so a
	warehouse that took back more than it sold contributes 0, not a negative);
	the item's company-wide rate is the sum of those per-warehouse rates.
	"""
	rate_map: dict[str, dict[str, float]] = {}
	company_rate: dict[str, float] = {}
	for (wh, item_code), qty in sales.items():
		rate = sells_per_day(qty_sold=qty, sales_days=sales_days)
		rate_map.setdefault(item_code, {})[wh] = rate
		company_rate[item_code] = company_rate.get(item_code, 0.0) + rate
	return rate_map, {code: round(rate, 3) for code, rate in company_rate.items()}


def _totals(rows: list[dict[str, Any]]) -> dict[str, float]:
	return {
		"qty": round(sum(float(r.get("total_qty") or 0) for r in rows), 3),
		"stock_value": round(sum(float(r.get("stock_value") or 0) for r in rows), 2),
	}


@frappe.whitelist()
def get_final_products_report(branch: str | None = None) -> dict[str, Any]:
	"""
	Return stock balances for items in the "Small", "Medium" and "Large" item
	groups, pivoted by warehouse.

	``branch`` (optional POS Profile): only that branch's warehouse is read and
	it is the only column. Each item row carries ``stock_value`` (sum of
	``Bin.stock_value`` over the columns shown) and ``warehouse_value``; each
	group carries ``totals: {"qty", "stock_value"}`` and so does the response.

	Every table carries the *same* warehouse columns — see
	``_finished_goods_warehouses`` — and every active size gets a row whether or
	not it has stock, because "we are out of Carrot cake" is the line a stock
	count exists to show and the old "skip items with no Bin row" rule hid it.

	Days of cover (so the app can colour each quantity cell). The rate is net
	jars sold per day over the last ``cover.sales_days`` (14) whole days ending
	yesterday, read off submitted Sales Invoices by ``Sales Invoice Item.warehouse``.
	Per item row:

	* ``sells_per_day``: ``{wh: rate}`` — one entry for EVERY warehouse column,
	  that warehouse's own rate; ``0.0`` when it sold none (never omitted).
	* ``days_cover``: ``{wh: float | None}`` — one entry for EVERY column. Own
	  rate > 0 → ``qty / own rate``. Otherwise, a finished-goods store (it feeds
	  every branch) → ``qty / company-wide rate``. Otherwise ``None``. Stock at
	  or below zero with a rate covers ``0.0``, never a negative number.
	* ``total_sells_per_day``: the item's rate summed over every warehouse it
	  sold from (company-wide, not only the columns). Branch view: that
	  branch's own rate.
	* ``total_days_cover``: ``total_qty / total_sells_per_day``, same rules.

	Colour: qty <= 0 or cover < ``red_below`` (3) = red; 3..7 = yellow;
	> ``green_above`` (7) = green; ``None`` = no recent sales, no colour. The
	thresholds travel in the top-level ``cover`` block. If the sales read fails
	every cover is ``None`` and every rate ``0.0``; the stock figures are untouched.

	Response shape:
	{
	    "groups": [
	        {
	            "group_name": "Medium",
	            "warehouses": ["WH-A", "WH-B", ...],
	            "items": [
	                {
	                    "item_code": "ITEM-001",
	                    "item_name": "...",
	                    "item_group": "Medium",
	                    "stock_uom": "Nos",
	                    "warehouse_qty": {"WH-A": 10.0, "WH-B": 5.0},
	                    "total_qty": 15.0,
	                    "warehouse_value": {"WH-A": 300.0, "WH-B": 150.0},
	                    "stock_value": 450.0,
	                    "sells_per_day": {"WH-A": 2.0, "WH-B": 0.0},
	                    "days_cover": {"WH-A": 5.0, "WH-B": None},
	                    "total_sells_per_day": 2.0,
	                    "total_days_cover": 7.5
	                },
	                ...
	            ],
	            "totals": {"qty": 15.0, "stock_value": 450.0}
	        },
	        ...
	    ],
	    "totals": {"qty": ..., "stock_value": ...},
	    "filters": {"branch": None, "warehouse": None},
	    "cover": {"sales_days": 14, "red_below": 3, "green_above": 7}
	}
	"""
	_ensure_jarz_manager()
	branch = (branch or "").strip() or None
	warehouse = branch_warehouse(branch)
	filters_echo = {"branch": branch, "warehouse": warehouse}

	group_aliases = {
		"Small": {"Small"},
		"Medium": {"Medium", "Meduim"},
		"Large": {"Large"},
	}
	target_groups = sorted({group for aliases in group_aliases.values() for group in aliases})

	# Get all non-disabled items in the target groups
	items = frappe.get_all(
		"Item",
		filters={"item_group": ["in", target_groups], "disabled": 0, "has_variants": 0},
		fields=["name as item_code", "item_name", "item_group", "stock_uom"],
		order_by="item_group asc, item_name asc",
	)

	if not items:
		return {
			"groups": [],
			"totals": {"qty": 0.0, "stock_value": 0.0},
			"filters": filters_echo,
			"cover": _cover_meta(),
		}

	item_codes = [it["item_code"] for it in items]

	# Every non-zero balance, negatives included. The filter used to be
	# ``> 0``, which dropped a branch's oversold bin and so read the total
	# HIGH: on production Chocolate Hazelnut Medium showed 74 while the ledger
	# said 70, because Dokki's -4 was simply not fetched.
	item_wh_map, item_value_map = _bin_maps(item_codes, warehouse)

	# Demand for days-of-cover. Branch view: that warehouse only. Otherwise
	# every warehouse, so totals and the finished-goods column see company-wide
	# demand. Fail-soft: {} leaves every cover None.
	rate_map, company_rate = _sales_rates(
		_jar_sales_by_warehouse(item_codes, warehouse, _COVER_SALES_DAYS),
		_COVER_SALES_DAYS,
	)

	# One column set for the whole report. What this guarantees is that the
	# Company's FG warehouse is ALWAYS a column, and that Small, Medium and
	# Large always carry identical columns so the tables line up.
	#
	# It does NOT freeze the column list: warehouse_set is the union of the
	# pinned FG warehouse and every warehouse holding a non-zero bin, so a
	# stray Bin row elsewhere still grows a column (and since the filter above
	# counts negatives, an oversold bin now grows one too). The report that
	# sprouted a "Raw Material" column was cured by repairing the phantom bins,
	# not by this line.
	if warehouse:
		# A branch view shows that branch's store and nothing else. Its sales
		# read is that warehouse only, so the company-wide fallback for a
		# finished-goods column would equal its own rate anyway.
		warehouses = [warehouse]
		fg_warehouses: set = set()
	else:
		fg_warehouses = set(_finished_goods_warehouses())
		warehouse_set: set = set(fg_warehouses)
		for wh_qty in item_wh_map.values():
			warehouse_set.update(wh_qty.keys())
		warehouses = sorted(warehouse_set)

	# Build separate tables per group: Medium, Large, then Small at the bottom
	# (the owner asked for the small jars last, 2026-10-20).
	groups_order = ["Medium", "Large", "Small"]
	result_groups = []

	for group_name in groups_order:
		group_items = [it for it in items if it["item_group"] in group_aliases[group_name]]
		if not group_items:
			continue

		group_result_items = []
		for it in group_items:
			wh_qty = item_wh_map.get(it["item_code"], {})
			wh_value = item_value_map.get(it["item_code"], {})
			total = sum(wh_qty.values())

			own_rates = rate_map.get(it["item_code"], {})
			item_company_rate = company_rate.get(it["item_code"], 0.0)
			wh_rates: dict[str, float] = {}
			wh_cover: dict[str, float | None] = {}
			for wh in warehouses:
				own_rate = own_rates.get(wh, 0.0)
				wh_rates[wh] = own_rate
				if own_rate > 0:
					wh_cover[wh] = days_of_cover(on_hand=wh_qty.get(wh, 0.0), rate=own_rate)
				elif wh in fg_warehouses:
					# The store feeds every branch: measure it against all demand.
					wh_cover[wh] = days_of_cover(on_hand=wh_qty.get(wh, 0.0), rate=item_company_rate)
				else:
					wh_cover[wh] = None

			group_result_items.append(
				{
					"item_code": it["item_code"],
					"item_name": it["item_name"],
					"item_group": group_name,
					"stock_uom": it["stock_uom"],
					"warehouse_qty": wh_qty,
					"total_qty": total,
					"warehouse_value": {wh: round(v, 2) for wh, v in wh_value.items()},
					"stock_value": round(sum(wh_value.values()), 2),
					"sells_per_day": wh_rates,
					"days_cover": wh_cover,
					"total_sells_per_day": item_company_rate,
					"total_days_cover": days_of_cover(on_hand=total, rate=item_company_rate),
				}
			)

		result_groups.append(
			{
				"group_name": group_name,
				"warehouses": warehouses,
				"items": group_result_items,
				"totals": _totals(group_result_items),
			}
		)

	all_rows = [row for group in result_groups for row in group["items"]]
	return {
		"groups": result_groups,
		"totals": _totals(all_rows),
		"filters": filters_echo,
		"cover": _cover_meta(),
	}


def _materials_payload(raw_materials, sub_assemblies, consumables, filters_echo) -> dict[str, Any]:
	bucket_totals = {
		"raw_materials": _totals(raw_materials),
		"sub_assemblies": _totals(sub_assemblies),
		"consumables": _totals(consumables),
	}
	return {
		"raw_materials": raw_materials,
		"sub_assemblies": sub_assemblies,
		"consumables": consumables,
		"bucket_totals": bucket_totals,
		"totals": _totals(raw_materials + sub_assemblies + consumables),
		"filters": filters_echo,
	}


@frappe.whitelist()
def get_materials_report(branch: str | None = None) -> dict[str, Any]:
	"""
	Return stock balances for the material, sub-assembly and consumable item
	groups — each expanded to its descendants, so the report follows the Item
	Group tree rather than a fixed list of leaf names.

	``branch`` (optional POS Profile): only that branch's warehouse is read.
	Each row carries ``stock_value`` (sum of ``Bin.stock_value``) and
	``warehouse_value``; ``bucket_totals`` gives ``{"qty", "stock_value"}`` per
	bucket and ``totals`` the grand total (qty mixes UOMs — use the value).

	Response shape:
	{
	    "raw_materials": [
	        {
	            "item_code": "RM-001",
	            "item_name": "...",
	            "item_group": "Raw Material",
	            "stock_uom": "Kg",
	            "warehouse_qty": {"WH-A": 100.0},
	            "total_qty": 100.0,
	            "warehouse_count": 1
	        },
	        ...
	    ],
	    "sub_assemblies": [ ... ],
	    "consumables": [ ... ]
	}
	"""
	_ensure_materials_report_access()
	branch = (branch or "").strip() or None
	warehouse = branch_warehouse(branch)
	filters_echo = {"branch": branch, "warehouse": warehouse}

	# Previously a literal ["Raw Material", "Sub Assembly", "Consumable"]. The
	# middle name never matched anything — the group is "Sub Assemblies", plural
	# — so the sub-assemblies bucket was empty on every site since this shipped.
	material_groups = _expand_item_groups(_MATERIAL_ROOTS)
	sub_assembly_groups = _expand_item_groups(_SUB_ASSEMBLY_ROOTS)
	consumable_groups = _expand_item_groups(_CONSUMABLE_ROOTS)
	target_groups = material_groups + sub_assembly_groups + consumable_groups
	if not target_groups:
		return _materials_payload([], [], [], filters_echo)

	items = frappe.get_all(
		"Item",
		filters={"item_group": ["in", target_groups], "disabled": 0, "has_variants": 0},
		fields=["name as item_code", "item_name", "item_group", "stock_uom"],
		order_by="item_group asc, item_name asc",
	)

	if not items:
		return _materials_payload([], [], [], filters_echo)

	item_codes = [it["item_code"] for it in items]

	# Every non-zero balance, negatives included -- the same rule as
	# get_final_products_report, and for the same reason. > 0 dropped a
	# branch's oversold bin, so the total read HIGH: a raw material at 70 in
	# the factory store and -4 at Dokki reported 70, not 66. The two halves of
	# this screen disagreeing was worse than either rule on its own, because
	# the same item in the same condition read differently depending on which
	# tab you opened.
	item_wh_map, item_value_map = _bin_maps(item_codes, warehouse)

	raw_materials = []
	sub_assemblies = []
	consumables = []
	sub_assembly_set = set(sub_assembly_groups)
	consumable_set = set(consumable_groups)

	for it in items:
		wh_qty = item_wh_map.get(it["item_code"], {})
		if not wh_qty:
			continue
		total = sum(wh_qty.values())
		wh_value = item_value_map.get(it["item_code"], {})
		entry = {
			"item_code": it["item_code"],
			"item_name": it["item_name"],
			"item_group": it["item_group"],
			"stock_uom": it["stock_uom"],
			"warehouse_qty": wh_qty,
			"total_qty": total,
			"warehouse_count": len(wh_qty),
			"warehouse_value": {wh: round(v, 2) for wh, v in wh_value.items()},
			"stock_value": round(sum(wh_value.values()), 2),
		}
		if it["item_group"] in sub_assembly_set:
			sub_assemblies.append(entry)
		elif it["item_group"] in consumable_set:
			consumables.append(entry)
		else:
			raw_materials.append(entry)

	# Sort consumables: items in more warehouses first
	consumables.sort(key=lambda x: (-x["warehouse_count"], x["item_name"]))

	return _materials_payload(raw_materials, sub_assemblies, consumables, filters_echo)
