"""B2B base price list: every large jar 92, every medium jar 77 — seeded, not enforced.

WHY this exists
---------------
``setup/b2b_master_data._ensure_price_lists`` creates the "B2B Selling" list but
leaves it empty, and an empty price list prices nothing. Worse than nothing, in
fact: the "B2B Supply" policy deliberately carries NO price list (the tier is
meant to come from the customer or the customer group), so when a B2B customer
has no tier — which is every B2B customer on both servers today — the resolution
chain falls through to the POS Profile's list and the order books at RETAIL.
Proven on staging 2026-09-03: a B2B Supply order for a freshly created B2B
customer booked Molten Large at 160 and Molten Medium at 120.

This module seeds that list with the agreed B2B base rates so the fallback in
``services/invoice_creation._resolve_b2b_baseline_price_list`` has something real
to resolve to.

WHY IT IS CREATE-ONLY (the opposite of ``setup/employee_pricing``)
------------------------------------------------------------------
The staff price is a policy constant, so ``employee_pricing`` re-asserts it on
every migrate and reverts any edit. A B2B price is commercial: it is negotiated,
it moves, and the Pricing page exists precisely so a manager can move it. So this
module fills what is MISSING and never overwrites a rate that is already there.
A base price raised to 95 in the UI stays 95 through every later migrate.

The one-time corrective pass is opt-in: ``ensure_b2b_base_prices(realign=True)``
(run by hand, once per server, with the seeding) also drags existing generic rows
back to the base rate. That is how the two stale ``Large @ 100`` rows left behind
by an old UAT run get fixed without giving the migrate hook the power to undo a
manager's pricing decision every night.

The two layers
--------------
1. ``Jarz Price List Category Rate`` — one row per (price_list, item_group). This
   is what keeps the guarantee alive for a jar flavour created next month: it
   prices the GROUP, so a new item lands correctly priced with nobody re-running
   anything, and the coverage validator in ``invoice_creation`` accepts it.
2. ``Item Price`` — a generic per-item row for every large/medium item that exists
   today. Item Price outranks the category rate in ``_resolve_item_rate``, so
   writing both layers to the same number makes the precedence irrelevant.

Only the GENERIC row (no customer) is ever touched. A customer-scoped Item Price
is a negotiated rate for one account and outranks everything; realign must never
touch it.

Size is plain ``Item.item_group``. "Meduim" is a real item group on some sites
(a typo that predates this app and holds real items), so both spellings are
enumerated — see ``setup/employee_pricing`` for the same treatment.

This module must import cleanly with NO top-level frappe calls.
"""

import frappe

LOGGER_NAME = "b2b_pricing"

#: The B2B base price list, created by ``setup/b2b_master_data._ensure_price_lists``.
#: Imported by the resolver and by ``api/pos.resolve_customer_price_list`` — this is
#: the single place the name is spelled.
PRICE_LIST = "B2B Selling"

#: The order purpose whose orders fall back to ``PRICE_LIST``. Deliberately NOT every
#: policy without a price list: "Free Shipping Waiver" is a retail order with the
#: shipping income waived, and must keep pricing at retail.
B2B_SUPPLY_PURPOSE = "B2B Supply"

#: The two base rates.
LARGE_RATE = 92.0
MEDIUM_RATE = 77.0

#: Item Groups per size. Mirrors ``setup/employee_pricing``.
_LARGE_GROUPS = ("Large",)
_MEDIUM_GROUPS = ("Medium", "Meduim")

#: Float comparison tolerance — money here is whole piastres, so anything under
#: half a piastre is "the same value" and must not count as drift.
_EPSILON = 0.005

_CATEGORY_DOCTYPE = "Jarz Price List Category Rate"


def _logger():
	return frappe.logger(LOGGER_NAME, allow_site=True)


def _price_list_currency():
	"""Currency of the B2B price list, falling back to the default company's."""
	try:
		currency = frappe.db.get_value("Price List", PRICE_LIST, "currency")
		if currency:
			return currency
	except Exception:
		pass
	try:
		company = frappe.defaults.get_global_default("company")
		if company:
			currency = frappe.db.get_value("Company", company, "default_currency")
			if currency:
				return currency
	except Exception:
		pass
	return "EGP"


def _as_float(value):
	"""Coerce a DB numeric (often Decimal) to float, or None when unset."""
	if value in (None, ""):
		return None
	try:
		return float(value)
	except (TypeError, ValueError):
		return None


def _existing_item_groups(groups):
	"""The subset of ``groups`` that actually exists on this site.

	A site without the "Meduim" typo group must not get a category rate row
	pointing at a non-existent Item Group — the Link would fail validation on
	insert and poison later saves of anything referencing it.
	"""
	present = []
	for group in groups:
		try:
			if frappe.db.exists("Item Group", group):
				present.append(group)
		except Exception:
			_logger().error(f"Failed to check Item Group '{group}' existence", exc_info=True)
	return present


def _items_for_groups(groups):
	"""Sellable, enabled, non-template items in the given Item Groups."""
	if not groups:
		return []
	try:
		return (
			frappe.get_all(
				"Item",
				filters={
					"item_group": ["in", list(groups)],
					"disabled": 0,
					"has_variants": 0,
					"is_sales_item": 1,
				},
				pluck="name",
			)
			or []
		)
	except Exception:
		_logger().error(f"Failed to enumerate items for groups {list(groups)}", exc_info=True)
		return []


def _ensure_category_rate(item_group, rate, realign, log, summary):
	"""Create the (B2B Selling, item_group) category rate row when it is missing.

	Corrects an existing row ONLY under ``realign`` — outside that, a rate already
	on the row is a manager's number and is left exactly as it is.
	"""
	label = f"Category rate: {PRICE_LIST} / {item_group}"
	try:
		existing = (
			frappe.get_all(
				_CATEGORY_DOCTYPE,
				filters={"price_list": PRICE_LIST, "item_group": item_group},
				fields=["name", "rate"],
				limit_page_length=0,
			)
			or []
		)

		if existing:
			for row in existing:
				current = _as_float(row.get("rate"))
				if current is not None and abs(current - rate) <= _EPSILON:
					summary["unchanged"] += 1
					continue
				if not realign:
					# Duplicates are never deleted here: a second row for the pair is a
					# data problem for a human to look at, and removing it silently
					# would destroy the evidence.
					log["kept"].append(f"{label} = {current} (left alone; base is {rate})")
					summary["kept"] += 1
					continue
				frappe.db.set_value(_CATEGORY_DOCTYPE, row["name"], "rate", rate, update_modified=True)
				log["updated"].append(f"{label}: {current} -> {rate}")
				summary["category_rates_updated"] += 1
			return

		doc = frappe.get_doc(
			{
				"doctype": _CATEGORY_DOCTYPE,
				"price_list": PRICE_LIST,
				"item_group": item_group,
				"rate": rate,
				"currency": _price_list_currency(),
			}
		)
		doc.insert(ignore_permissions=True)
		log["created"].append(f"{label} = {rate}")
		summary["category_rates_created"] += 1
	except Exception:
		_logger().error(f"Failed to ensure {label}", exc_info=True)
		summary["failed"].append(label)


def _ensure_item_price(item_code, rate, currency, realign, log, summary):
	"""Create the generic B2B Item Price for one item when it is missing.

	Only the GENERIC row — the one with no customer — is considered.
	``invoice_creation._resolve_item_rate`` reads a customer-scoped Item Price
	ahead of the generic one, so a negotiated per-account rate on this list is a
	deliberate override and is never read, written or realigned here.
	"""
	label = f"Item Price: {item_code} @ {PRICE_LIST}"
	try:
		existing = (
			frappe.get_all(
				"Item Price",
				filters={
					"item_code": item_code,
					"price_list": PRICE_LIST,
					"customer": ["in", [None, ""]],
				},
				fields=["name", "price_list_rate"],
				limit_page_length=0,
			)
			or []
		)

		if existing:
			for row in existing:
				current = _as_float(row.get("price_list_rate"))
				if current is not None and abs(current - rate) <= _EPSILON:
					# Counted, not listed: an already-correct item is the normal case
					# for every one of ~20 jars, and naming them all would bury the
					# lines that actually need reading.
					summary["unchanged"] += 1
					continue
				if not realign:
					log["kept"].append(f"{label} = {current} (left alone; base is {rate})")
					summary["kept"] += 1
					continue
				frappe.db.set_value(
					"Item Price", row["name"], "price_list_rate", rate, update_modified=True
				)
				log["updated"].append(f"{label}: {current} -> {rate}")
				summary["item_prices_updated"] += 1
			return

		doc = frappe.get_doc(
			{
				"doctype": "Item Price",
				"item_code": item_code,
				"price_list": PRICE_LIST,
				"price_list_rate": rate,
				"selling": 1,
				"currency": currency,
			}
		)
		doc.insert(ignore_permissions=True)
		log["created"].append(f"{label} = {rate}")
		summary["item_prices_created"] += 1
	except Exception:
		_logger().error(f"Failed to ensure {label}", exc_info=True)
		summary["failed"].append(label)


def _report_realignment(log):
	"""Make a realigned rate impossible to miss.

	``frappe.logger().info`` is effectively silent on the servers (the default log
	level there is ERROR), and a rate that was edited in the UI and then moved by a
	realign run is exactly the change somebody will come asking about. So a
	correction — which only ever happens under ``realign=True`` — is also written to
	the Error Log, where it survives and is searchable.

	``frappe.log_error`` writes a document and can itself raise, so it is guarded: a
	failure to record the correction must not undo it.
	"""
	if not log["updated"]:
		return
	message = (
		f"B2B base price list realigned (Large={LARGE_RATE}, Medium={MEDIUM_RATE}): "
		+ "; ".join(log["updated"])
	)
	_logger().warning(message)
	try:
		frappe.log_error(message, "B2B base price list realigned")
	except Exception:
		pass


def ensure_b2b_base_prices(realign=False):
	"""Seed the B2B base rates (Large 92 / Medium 77) on the "B2B Selling" list.

	Registered on ``after_migrate`` with the default ``realign=False``: it creates
	what is missing and NEVER overwrites an existing rate, so a manager's pricing
	decision survives every migrate. Values that are already correct are not
	rewritten either, so a steady-state migrate performs no writes at all.

	Pass ``realign=True`` (by hand, once per server) to additionally drag existing
	generic rows back to the base rate; every such correction is listed and written
	to the Error Log. Customer-scoped rows are untouched in both modes.

	Returns the log dict, with per-layer counts under ``summary``.
	"""
	log = {"created": [], "updated": [], "kept": [], "summary": {}}
	summary = {
		"realign": bool(realign),
		"large_items": 0,
		"medium_items": 0,
		"category_rates_created": 0,
		"category_rates_updated": 0,
		"item_prices_created": 0,
		"item_prices_updated": 0,
		"unchanged": 0,
		"kept": 0,
		"skipped": [],
		"failed": [],
	}
	log["summary"] = summary
	logger = _logger()

	try:
		# The price list is seeded by b2b_master_data on the same after_migrate run;
		# this module must be registered AFTER it. If it is somehow absent, stop
		# cleanly rather than creating Item Prices pointing nowhere.
		if not frappe.db.exists("Price List", PRICE_LIST):
			summary["skipped"].append(f"Price List '{PRICE_LIST}' does not exist")
			logger.warning(
				f"B2B pricing skipped: Price List '{PRICE_LIST}' does not exist "
				f"(is setup.b2b_master_data.ensure_b2b_master_data registered before this?)"
			)
			return log

		currency = _price_list_currency()

		large_groups = _existing_item_groups(_LARGE_GROUPS)
		medium_groups = _existing_item_groups(_MEDIUM_GROUPS)
		for missing in set(_LARGE_GROUPS + _MEDIUM_GROUPS) - set(large_groups + medium_groups):
			summary["skipped"].append(f"Item Group '{missing}' does not exist")

		# Layer 1: the category rates. Written first because they are the layer that
		# survives new items being created later.
		for group in large_groups:
			_ensure_category_rate(group, LARGE_RATE, realign, log, summary)
		for group in medium_groups:
			_ensure_category_rate(group, MEDIUM_RATE, realign, log, summary)

		# Layer 2: per-item prices for everything that exists today.
		large_items = _items_for_groups(large_groups)
		medium_items = _items_for_groups(medium_groups)
		summary["large_items"] = len(large_items)
		summary["medium_items"] = len(medium_items)

		for code in large_items:
			_ensure_item_price(code, LARGE_RATE, currency, realign, log, summary)
		for code in medium_items:
			_ensure_item_price(code, MEDIUM_RATE, currency, realign, log, summary)

		logger.info(
			f"B2B pricing resolved {summary['large_items']} large item(s) in {large_groups} "
			f"@ {LARGE_RATE} and {summary['medium_items']} medium item(s) in {medium_groups} "
			f"@ {MEDIUM_RATE} (realign={bool(realign)})"
		)

		if log["created"]:
			logger.info("B2B pricing created: " + "; ".join(log["created"]))
		if log["kept"]:
			# Not a warning: leaving a manager's number alone is the designed
			# behaviour, not a problem. It is still stated, because "the seeder ran
			# and the price is not 92" needs an explanation somewhere.
			logger.info("B2B pricing left existing rates alone: " + "; ".join(log["kept"]))
		_report_realignment(log)
		if not log["created"] and not log["updated"]:
			logger.info(
				f"B2B pricing already seeded: {summary['unchanged']} rate(s) verified, "
				f"{summary['kept']} left as configured"
			)
		if summary["skipped"]:
			logger.warning("B2B pricing skipped: " + "; ".join(summary["skipped"]))

		logger.info(f"ensure_b2b_base_prices summary: {summary}")
	except Exception:
		# Never let pricing seeding break a migrate. A missing B2B price is a bad day;
		# a bench that will not migrate is a worse one.
		logger.error("ensure_b2b_base_prices failed unexpectedly", exc_info=True)

	return log
