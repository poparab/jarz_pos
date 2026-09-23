"""The Small jar (147 ml): its item group, packaging, finished items and BOMs.

WHAT A SMALL JAR IS
-------------------
A third size next to Medium (212 ml) and Large (330 ml), introduced 2026-09 for
B2B customers who hand it out as a free sample. Jarz sells it to them near cost
(30 EGP on "B2B Selling", seeded by ``setup/b2b_pricing``), and it is NOT a retail
product: ``services/invoice_creation`` refuses a Small line that no price list
backs, so a branch cashier cannot book one at 0.

Its recipe is the Medium recipe scaled to two thirds. Every filling line of the
flavour's default Medium BOM is multiplied by 2/3; the three packaging lines are
swapped for their 147 counterparts, one each:

    Glass Jar (the 212)         -> Glass Jar 147
    Jar Lid   (the 212)         -> Jar Lid 147
    <Flavour> Jar Label 212     -> <Flavour> Jar Label 147

So the standard mix line goes 79.333 g -> 52.889 g, i.e. exactly 180 Small jars
from one 9.52 kg batch of Cheesecake Mix (Medium 120, Large 77).

Deriving the BOM from the live Medium BOM, rather than hard-coding quantities
here, is deliberate: it carries Tiramisu's lighter mix line, Mango's sponge,
every ``do_not_explode`` flag and every sub-assembly ``bom_no`` across exactly as
the floor has them today.

WHAT IT CREATES (create-only, like every seeder here)
-----------------------------------------------------
1. Item Group ``Small`` under ``Finished Goods``.
2. Packaging: ``Glass Jar 147`` and ``Jar Lid 147`` (group Packaging), and
   ``B2B Carton`` if the site lacks it (the owner created it by hand on
   production on 2026-09-23; this keeps staging and local identical).
3. For every ENABLED Medium item with a default BOM: ``<Flavour> Small``, its
   ``<Flavour> Jar Label 147`` and a submitted default BOM.
4. Once, on the run that creates the item group: ``Small`` is added to every
   enabled POS Profile (B2B orders are placed through a branch profile, so the
   catalogue must carry it) and to every Warehouse Count Profile that already
   counts ``Medium`` (so branches can count it).

Nothing existing is ever changed. A Small item that already exists keeps its
fields; one that already has an active default BOM keeps it. Step 4 runs only
on the creating run so that a manager who later takes Small off a profile is
not overruled by the next migrate.

Materials start at zero stock and zero valuation. The BOMs cost on Valuation
Rate like their Medium parents, so the glass, lid and labels read 0 until the
first purchase; BOM costs correct themselves on the next BOM cost update.

This module must import cleanly with NO top-level frappe calls.
"""

import logging
import re

import frappe

LOGGER_NAME = "small_jar_setup"

SIZE_GROUP = "Small"
PARENT_GROUP = "Finished Goods"
SOURCE_GROUP = "Medium"

GLASS_ITEM = "Glass Jar 147"
LID_ITEM = "Jar Lid 147"
CARTON_ITEM = "B2B Carton"
PACKAGING_GROUP = "Packaging"
LABELS_GROUP = "Labels"

#: The Medium packaging and what replaces it in the Small BOM. The 212 items are
#: literally named "Glass Jar" and "Jar Lid"; labels are matched by item group.
PACKAGING_SWAPS = {"Glass Jar": GLASS_ITEM, "Jar Lid": LID_ITEM}

#: Filling scale from Medium to Small.
SCALE = 2.0 / 3.0

#: Stored precision of BOM Item.qty on this site (0.079333 is how the Medium mix
#: line is stored), so 6 places reproduces the floor's own rounding.
QTY_PRECISION = 6

_LABEL_SUFFIX = re.compile(r"\s*212\s*$")


def _logger():
	logger = frappe.logger(LOGGER_NAME, allow_site=True)
	# Off a dev server Frappe's default level is ERROR, which silently drops the
	# created/skipped report this seeder exists to leave behind.
	logger.setLevel(logging.INFO)
	return logger


def small_item_code(medium_item_code):
	"""``Molten Medium`` -> ``Molten Small``; None when it does not end in Medium."""
	code = str(medium_item_code or "").strip()
	if not code.endswith(" " + SOURCE_GROUP):
		return None
	return code[: -len(SOURCE_GROUP)] + SIZE_GROUP


def small_label_code(medium_label_code):
	"""``Blueberry Jar label 212`` -> ``Blueberry Jar Label 147``.

	The 212 names are inconsistent ("Jar label" / "Jar Label", a stray double
	space in "Mango  Jar Label 330"); the new ones are written one way.
	"""
	base = _LABEL_SUFFIX.sub("", str(medium_label_code or "")).strip()
	if not base or base == str(medium_label_code or "").strip():
		return None
	base = re.sub(r"\s+", " ", base)
	base = re.sub(r"\bJar label\b", "Jar Label", base)
	return f"{base} 147"


def scale_qty(qty):
	return round(float(qty or 0) * SCALE, QTY_PRECISION)


def _company():
	return frappe.defaults.get_global_default("company") or frappe.db.get_value("Company", {}, "name")


def _ensure_item_group(log):
	"""Returns True when this run created the group."""
	if frappe.db.exists("Item Group", SIZE_GROUP):
		return False
	parent = PARENT_GROUP if frappe.db.exists("Item Group", PARENT_GROUP) else "All Item Groups"
	frappe.get_doc(
		{
			"doctype": "Item Group",
			"item_group_name": SIZE_GROUP,
			"parent_item_group": parent,
			"is_group": 0,
		}
	).insert(ignore_permissions=True)
	log["created"].append(f"Item Group: {SIZE_GROUP} (under {parent})")
	return True


def _ensure_material(item_code, item_group, description, company, warehouse, log):
	"""Create a purchased stock material (packaging or label) when missing."""
	if frappe.db.exists("Item", item_code):
		return
	doc = {
		"doctype": "Item",
		"item_code": item_code,
		"item_name": item_code,
		"item_group": item_group,
		"description": description,
		"stock_uom": "Nos",
		"is_stock_item": 1,
		"is_purchase_item": 1,
		"is_sales_item": 0,
		"include_item_in_manufacturing": 1,
		"valuation_method": "Moving Average",
		"default_material_request_type": "Purchase",
	}
	if company and warehouse:
		doc["item_defaults"] = [{"company": company, "default_warehouse": warehouse}]
	frappe.get_doc(doc).insert(ignore_permissions=True)
	log["created"].append(f"Item: {item_code} ({item_group})")


def _group_like(item_code, preferred):
	"""``preferred`` when that group exists, else the group ``item_code`` is in."""
	if frappe.db.exists("Item Group", preferred):
		return preferred
	return frappe.db.get_value("Item", item_code, "item_group") or preferred


def _default_warehouse(item_code, company):
	return frappe.db.get_value(
		"Item Default", {"parent": item_code, "company": company}, "default_warehouse"
	)


def _ensure_small_item(medium, company, log):
	"""Create ``<Flavour> Small`` from its Medium sibling when missing."""
	code = small_item_code(medium.name)
	if frappe.db.exists("Item", code):
		return code
	flavour = code[: -len(SIZE_GROUP)].strip()
	doc = {
		"doctype": "Item",
		"item_code": code,
		"item_name": code,
		"item_group": SIZE_GROUP,
		"description": f"{flavour} 147",
		"stock_uom": "Nos",
		"is_stock_item": 1,
		"is_sales_item": 1,
		"is_purchase_item": 0,
		"include_item_in_manufacturing": 1,
		"valuation_method": medium.valuation_method or "FIFO",
		"allow_negative_stock": medium.allow_negative_stock,
		"shelf_life_in_days": medium.shelf_life_in_days,
		"grant_commission": medium.grant_commission,
		"default_material_request_type": "Manufacture",
		# No retail price: the POS shows 0 and invoice_creation refuses the line
		# unless the order's price list prices it (B2B Selling does, at 30).
		"standard_rate": 0,
	}
	warehouse = _default_warehouse(medium.name, company)
	if company and warehouse:
		doc["item_defaults"] = [{"company": company, "default_warehouse": warehouse}]
	frappe.get_doc(doc).insert(ignore_permissions=True)
	log["created"].append(f"Item: {code}")
	return code


def _has_active_default_bom(item_code):
	return bool(
		frappe.db.exists(
			"BOM", {"item": item_code, "is_active": 1, "is_default": 1, "docstatus": 1}
		)
	)


def build_small_bom_items(medium_bom_items, label_group_of):
	"""The Small BOM's item rows, from the Medium BOM's.

	``label_group_of(item_code)`` returns an item's group; labels are recognised by
	group because their names are not uniform. Returns ``(rows, labels)`` where
	``labels`` maps each Small label code to its Medium original, so the caller can
	create the label items. Raises ValueError when the Medium BOM has no glass,
	lid or label, since a Small BOM missing its packaging would under-cost the jar
	and under-issue stock silently.
	"""
	rows, labels = [], {}
	seen = {"glass": False, "lid": False, "label": False}
	for line in medium_bom_items:
		row = {
			"item_code": line.item_code,
			"qty": scale_qty(line.qty),
			"uom": line.uom,
			"conversion_factor": line.conversion_factor or 1,
			"bom_no": line.bom_no or "",
			"do_not_explode": line.do_not_explode,
			"source_warehouse": line.source_warehouse,
		}
		if line.item_code in PACKAGING_SWAPS:
			row["item_code"] = PACKAGING_SWAPS[line.item_code]
			row["qty"] = float(line.qty or 1)
			row["bom_no"] = ""
			seen["glass" if line.item_code == "Glass Jar" else "lid"] = True
		elif label_group_of(line.item_code) == LABELS_GROUP:
			small_label = small_label_code(line.item_code)
			if not small_label:
				raise ValueError(f"cannot derive a 147 label from '{line.item_code}'")
			labels[small_label] = line.item_code
			row["item_code"] = small_label
			row["qty"] = float(line.qty or 1)
			row["bom_no"] = ""
			seen["label"] = True
		rows.append(row)
	missing = [k for k, v in seen.items() if not v]
	if missing:
		raise ValueError(f"Medium BOM lacks its {', '.join(missing)} line(s)")
	return rows, labels


def _ensure_small_bom(medium, small_code, company, log):
	if _has_active_default_bom(small_code):
		return
	medium_bom = frappe.get_doc("BOM", medium.default_bom)
	rows, labels = build_small_bom_items(
		medium_bom.items, lambda code: frappe.db.get_value("Item", code, "item_group")
	)
	for small_label, medium_label in labels.items():
		_ensure_material(
			small_label,
			_group_like(medium_label, LABELS_GROUP),
			small_label,
			company,
			_default_warehouse(medium_label, company),
			log,
		)
	bom = frappe.get_doc(
		{
			"doctype": "BOM",
			"item": small_code,
			"company": medium_bom.company,
			"currency": medium_bom.currency,
			"quantity": 1,
			"uom": "Nos",
			"is_active": 1,
			"is_default": 1,
			"with_operations": 0,
			"rm_cost_as_per": medium_bom.rm_cost_as_per,
			"buying_price_list": medium_bom.buying_price_list,
			"set_rate_of_sub_assembly_item_based_on_bom": medium_bom.set_rate_of_sub_assembly_item_based_on_bom,
			"items": rows,
		}
	)
	bom.insert(ignore_permissions=True)
	bom.submit()
	log["created"].append(f"BOM: {bom.name} (2/3 of {medium_bom.name})")


def _wire_profiles(log):
	"""Put Small on every enabled POS Profile and on count profiles that count Medium."""
	for profile in frappe.get_all("POS Profile", filters={"disabled": 0}, pluck="name"):
		doc = frappe.get_doc("POS Profile", profile)
		if any(r.item_group == SIZE_GROUP for r in doc.get("item_groups") or []):
			continue
		doc.append("item_groups", {"item_group": SIZE_GROUP})
		doc.save(ignore_permissions=True)
		log["created"].append(f"POS Profile {profile}: + {SIZE_GROUP}")

	if not frappe.db.exists("DocType", "Warehouse Count Profile"):
		return
	parents = frappe.get_all(
		"Warehouse Count Profile Item Group",
		filters={"item_group": SOURCE_GROUP, "parenttype": "Warehouse Count Profile"},
		pluck="parent",
		distinct=True,
	)
	for name in parents:
		doc = frappe.get_doc("Warehouse Count Profile", name)
		if any(r.item_group == SIZE_GROUP for r in doc.get("item_groups") or []):
			continue
		doc.append("item_groups", {"item_group": SIZE_GROUP, "enabled": 1})
		doc.save(ignore_permissions=True)
		log["created"].append(f"Warehouse Count Profile {name}: + {SIZE_GROUP}")


def ensure_small_jar_setup():
	"""after_migrate entry point. Never raises: a seeder must not break a migrate."""
	log = {"created": [], "failed": []}
	logger = _logger()
	try:
		company = _company()
		group_created = _ensure_item_group(log)

		# The new packaging lives wherever its 212 sibling lives: same group (so the
		# purchase warehouse route and the reports treat it alike), same store.
		raw_wh = _default_warehouse("Glass Jar", company)
		packaging_group = _group_like("Glass Jar", PACKAGING_GROUP)
		_ensure_material(GLASS_ITEM, packaging_group, "Glass Jar 147 ml", company, raw_wh, log)
		_ensure_material(
			LID_ITEM, _group_like("Jar Lid", packaging_group), "Jar Lid for the 147 ml jar", company, raw_wh, log
		)
		_ensure_material(CARTON_ITEM, packaging_group, CARTON_ITEM, company, raw_wh, log)

		mediums = frappe.get_all(
			"Item",
			filters={"item_group": SOURCE_GROUP, "disabled": 0, "default_bom": ["is", "set"]},
			fields=[
				"name",
				"default_bom",
				"valuation_method",
				"allow_negative_stock",
				"shelf_life_in_days",
				"grant_commission",
			],
			order_by="name asc",
		)
		for medium in mediums:
			if not small_item_code(medium.name):
				continue
			# One savepoint per flavour: a flavour whose BOM cannot be derived must
			# not leave a Small item without a BOM, nor take the others down.
			frappe.db.savepoint("small_jar_flavour")
			flavour_log = {"created": []}
			try:
				code = _ensure_small_item(medium, company, flavour_log)
				_ensure_small_bom(medium, code, company, flavour_log)
				log["created"].extend(flavour_log["created"])
			except Exception as exc:
				frappe.db.rollback(save_point="small_jar_flavour")
				log["failed"].append(f"{medium.name}: {exc}")
				logger.error(f"Small jar setup failed for {medium.name}", exc_info=True)

		if group_created:
			try:
				_wire_profiles(log)
			except Exception as exc:
				log["failed"].append(f"profiles: {exc}")
				logger.error("Small jar profile wiring failed", exc_info=True)

		if log["created"]:
			logger.info("Small jar setup created: " + "; ".join(log["created"]))
		if log["failed"]:
			frappe.log_error(
				title="Small jar setup: some flavours failed",
				message="\n".join(log["failed"]),
			)
	except Exception as exc:
		log["failed"].append(f"setup aborted: {exc}")
		logger.error("ensure_small_jar_setup failed unexpectedly", exc_info=True)
		try:
			frappe.log_error(title="Small jar setup aborted", message=frappe.get_traceback())
		except Exception:
			pass
	return log
