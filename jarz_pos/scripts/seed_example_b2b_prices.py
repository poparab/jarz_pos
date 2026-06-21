"""Seed EXAMPLE B2B pricing data for STAGING UAT.

Idempotent, get-or-create style seeding (mirrors
``jarz_pos/setup/b2b_master_data.py``). Safe to run repeatedly: every record is
guarded with ``frappe.db.exists`` / ``frappe.get_all`` before insert, and Item
Prices are upserted (update if the (item_code, price_list) pair already exists,
else insert). No existing record is ever deleted, and Item Prices on price lists
other than the two seeded here are never touched.

This is EXAMPLE / UAT data intended for STAGING only. Each unit of work is
wrapped in its own try/except so a single failure logs and the rest continue.

Run with:
    bench --site <site> execute jarz_pos.scripts.seed_example_b2b_prices.run

This module must import cleanly with NO top-level frappe calls.
"""

import frappe

LOGGER_NAME = "seed_example_b2b_prices"

CURRENCY = "EGP"

# (item_group, price_list) -> rate
SIZE_PRICE_LISTS = {
	"Medium": {"Companies": 75, "Cafes": 70},
	"Large": {"Companies": 95, "Cafes": 90},
}

# Customer Group name -> default price list
CUSTOMER_GROUP_PRICE_LISTS = {
	"Companies": "Companies",
	"Cafes": "Cafes",
}

LEAD_SOURCES = [
	"Walk In",
	"Reference",
	"Campaign",
	"Existing Customer",
	"Cold Call",
	"Social Media",
]


def _logger():
	return frappe.logger(LOGGER_NAME, allow_site=True)


def _ensure_price_lists(summary):
	"""Create the two selling price lists if missing (EGP, enabled)."""
	log = _logger()
	for name in ("Companies", "Cafes"):
		try:
			if frappe.db.exists("Price List", name):
				log.info(f"Price List already present: {name}")
				continue
			doc = frappe.get_doc(
				{
					"doctype": "Price List",
					"price_list_name": name,
					"selling": 1,
					"enabled": 1,
					"currency": CURRENCY,
				}
			)
			doc.insert(ignore_permissions=True)
			summary["price_lists"] += 1
			log.info(f"Price List created: {name} ({CURRENCY})")
		except Exception:
			log.error(f"Failed to ensure Price List '{name}'", exc_info=True)


def _items_for_group(group):
	"""Return non-disabled item codes for an Item Group (size)."""
	return frappe.get_all(
		"Item",
		filters={"item_group": group, "disabled": 0},
		pluck="name",
	)


def _upsert_item_price(item_code, price_list, rate):
	"""Insert or update the Item Price for (item_code, price_list).

	Returns True if a row was inserted or updated, False on failure.
	"""
	existing = frappe.get_all(
		"Item Price",
		filters={"item_code": item_code, "price_list": price_list},
		pluck="name",
	)
	if existing:
		# Update the (first) existing Item Price rate; never delete duplicates.
		frappe.db.set_value(
			"Item Price", existing[0], "price_list_rate", rate, update_modified=True
		)
		return True

	doc = frappe.get_doc(
		{
			"doctype": "Item Price",
			"item_code": item_code,
			"price_list": price_list,
			"price_list_rate": rate,
			"selling": 1,
			"currency": CURRENCY,
		}
	)
	doc.insert(ignore_permissions=True)
	return True


def _ensure_item_prices(summary):
	"""Set Item Prices for Medium/Large items on the Companies/Cafes lists."""
	log = _logger()

	for group, price_map in SIZE_PRICE_LISTS.items():
		try:
			item_codes = _items_for_group(group)
		except Exception:
			log.error(f"Failed to fetch items for group '{group}'", exc_info=True)
			item_codes = []

		if group == "Medium":
			summary["medium_items"] = len(item_codes)
		elif group == "Large":
			summary["large_items"] = len(item_codes)

		for code in item_codes:
			for price_list, rate in price_map.items():
				try:
					if _upsert_item_price(code, price_list, rate):
						summary["item_prices_set"] += 1
				except Exception:
					log.error(
						f"Failed to set Item Price for item '{code}' on "
						f"price list '{price_list}'",
						exc_info=True,
					)


def _ensure_customer_groups(summary):
	"""Create Companies/Cafes customer groups with the matching default list.

	If the group already exists, only (idempotently) set its default price list
	via frappe.db.set_value.
	"""
	log = _logger()
	parent = "All Customer Groups"
	for name, price_list in CUSTOMER_GROUP_PRICE_LISTS.items():
		try:
			if frappe.db.exists("Customer Group", name):
				frappe.db.set_value(
					"Customer Group", name, "default_price_list", price_list
				)
				log.info(
					f"Customer Group already present: {name} "
					f"(default_price_list set -> {price_list})"
				)
				continue
			doc = frappe.get_doc(
				{
					"doctype": "Customer Group",
					"customer_group_name": name,
					"parent_customer_group": parent,
					"is_group": 0,
					"default_price_list": price_list,
				}
			)
			doc.insert(ignore_permissions=True)
			summary["customer_groups"] += 1
			log.info(
				f"Customer Group created: {name} (default_price_list={price_list})"
			)
		except Exception:
			log.error(f"Failed to ensure Customer Group '{name}'", exc_info=True)


def _lead_source_title_field():
	"""Resolve the title/name field of the Lead Source doctype.

	Defaults to ``source_name`` (ERPNext standard) but falls back to whatever the
	doctype's meta declares as its title field, to stay robust across versions.
	"""
	try:
		meta = frappe.get_meta("Lead Source")
		if meta.get_field("source_name"):
			return "source_name"
		if meta.title_field and meta.get_field(meta.title_field):
			return meta.title_field
	except Exception:
		pass
	return "source_name"


def _ensure_lead_sources(summary):
	"""Diagnose the Lead.source field, then seed Lead Source records if possible."""
	log = _logger()

	# DIAGNOSE: log the Lead.source field fieldtype/options so we know what it
	# links to (helps verify the Lead Source doctype assumption).
	try:
		source_field = frappe.get_meta("Lead").get_field("source")
		if source_field:
			log.info(
				"Lead.source field -> fieldtype=%s options=%s"
				% (source_field.fieldtype, source_field.options)
			)
		else:
			log.warning("Lead doctype has no 'source' field")
	except Exception:
		log.error("Failed to read Lead.source field meta", exc_info=True)

	if not frappe.db.exists("DocType", "Lead Source"):
		log.warning(
			"Lead Source DocType does not exist; skipping lead source seeding"
		)
		summary["lead_sources_skipped"] = True
		return

	title_field = _lead_source_title_field()
	for name in LEAD_SOURCES:
		try:
			if frappe.db.exists("Lead Source", name):
				log.info(f"Lead Source already present: {name}")
				continue
			doc = frappe.get_doc(
				{
					"doctype": "Lead Source",
					title_field: name,
				}
			)
			doc.insert(ignore_permissions=True)
			summary["lead_sources_seeded"] += 1
			log.info(f"Lead Source created: {name}")
		except Exception:
			log.error(f"Failed to ensure Lead Source '{name}'", exc_info=True)


def run():
	"""Idempotently seed example B2B pricing data for staging UAT.

	Safe to run repeatedly. Commits at the end. Returns a summary dict of counts.
	"""
	log = _logger()
	summary = {
		"price_lists": 0,
		"medium_items": 0,
		"large_items": 0,
		"item_prices_set": 0,
		"customer_groups": 0,
		"lead_sources_seeded": 0,
		"lead_sources_skipped": False,
	}

	try:
		_ensure_price_lists(summary)
	except Exception:
		log.error("_ensure_price_lists failed unexpectedly", exc_info=True)

	try:
		_ensure_item_prices(summary)
	except Exception:
		log.error("_ensure_item_prices failed unexpectedly", exc_info=True)

	try:
		_ensure_customer_groups(summary)
	except Exception:
		log.error("_ensure_customer_groups failed unexpectedly", exc_info=True)

	try:
		_ensure_lead_sources(summary)
	except Exception:
		log.error("_ensure_lead_sources failed unexpectedly", exc_info=True)

	try:
		frappe.db.commit()
	except Exception:
		log.error("Failed to commit seed_example_b2b_prices changes", exc_info=True)

	log.info(f"seed_example_b2b_prices summary: {summary}")
	return summary
