"""Employee price list: every large jar 92, every medium jar 77.

WHY this exists
---------------
Staff buy at a fixed, publicly-known staff price. The "Employee" Price List is
created by ``setup/b2b_master_data._ensure_price_lists``, but an empty price list
prices nothing: ``services/invoice_creation._validate_policy_price_list_coverage``
rejects the order outright, or (worse, before that guard existed) the order books
at the wrong rate. Keeping the two staff rates correct by hand means remembering
to do it every time a jar flavour is added — and the last time that was the
process, it was not remembered.

So this module is a STANDING GUARANTEE, run on every ``bench migrate``: the
Employee list prices Large at 92 and Medium at 77, in both layers the pricing
resolver reads.

WHY IT DELIBERATELY OVERWRITES
------------------------------
``setup/b2b_master_data.py`` is strictly create-only, precisely so that an admin
edit is never clobbered. **This module is the opposite and that is intentional.**
The ask is a guarantee, not a default: if somebody edits the Employee rate for
Large jars to 85 in the pricing UI, the next migrate puts it back to 92. That is
the feature, not a bug.

It is never silent about it. Every correction records item, old value and new
value in ``log["updated"]``, is counted in the returned summary, and — because a
reverted edit that leaves no trace reads as "my change vanished" — also raises a
warning and an Error Log entry naming exactly what changed. Values that are
already right are never rewritten, so nothing churns ``modified`` on a no-op
migrate.

The two layers
--------------
1. ``Jarz Price List Category Rate`` — one row per (price_list, item_group). This
   is the app's PRIMARY pricing mechanism (``invoice_creation._resolve_item_rate``
   falls back to it, and the coverage validator accepts it). It is also what makes
   the guarantee durable: a jar item created next month lands in the Large group
   and is priced at 92 with nobody re-running anything.
2. ``Item Price`` — a per-item generic row for every large/medium item that exists
   today. Item Price wins over the category rate in the resolver, so leaving stale
   per-item rows behind would let them override the correct category price. Writing
   both layers to the same number makes precedence irrelevant.

Size is plain ``Item.item_group``. **"Meduim" is a real item group** holding real
items (the typo predates this app; see ``services/consumable_deduction.py``), so
enumerating only "Medium" silently misses them and their orders price wrong.

This module must import cleanly with NO top-level frappe calls.
"""

import frappe

LOGGER_NAME = "employee_pricing"

#: The staff price list, created by ``setup/b2b_master_data._ensure_price_lists``.
PRICE_LIST = "Employee"

#: The two staff rates. The whole feature is these two numbers.
LARGE_RATE = 92.0
MEDIUM_RATE = 77.0

#: Item Groups per size. Mirrors ``services/consumable_deduction.py`` — "Meduim"
#: is a known data typo that holds real items, so both spellings are enumerated.
_LARGE_GROUPS = ("Large",)
_MEDIUM_GROUPS = ("Medium", "Meduim")

#: Float comparison tolerance. Rates come back from the DB as Decimal and money
#: here is whole piastres, so anything under half a piastre is "the same value"
#: and must NOT trigger a write.
_EPSILON = 0.005


def _logger():
	return frappe.logger(LOGGER_NAME, allow_site=True)


def _price_list_currency():
	"""Currency of the Employee price list, falling back to the default company's."""
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
	insert and, worse, poison later saves of anything referencing it.
	"""
	present = []
	for group in groups:
		try:
			if frappe.db.exists("Item Group", group):
				present.append(group)
		except Exception:
			_logger().error(
				f"Failed to check Item Group '{group}' existence", exc_info=True
			)
	return present


def _items_for_groups(groups):
	"""Sellable, enabled, non-template items in the given Item Groups.

	``has_variants`` templates are excluded because they are never sold directly,
	and an Item Price on a template prices nothing.
	"""
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
		_logger().error(
			f"Failed to enumerate items for groups {list(groups)}", exc_info=True
		)
		return []


def _ensure_category_rate(item_group, rate, log, summary):
	"""Create or correct the (Employee, item_group) category rate row.

	This is the layer that keeps a jar item created *next month* correctly priced:
	it prices the group, not the item, so a new flavour needs no follow-up.
	"""
	doctype = "Jarz Price List Category Rate"
	label = f"Category rate: {PRICE_LIST} / {item_group}"
	try:
		existing = (
			frappe.get_all(
				doctype,
				filters={"price_list": PRICE_LIST, "item_group": item_group},
				fields=["name", "rate"],
				limit_page_length=0,
			)
			or []
		)

		if existing:
			# Duplicates are never deleted here (same rule as the Item Price
			# upsert): a second row is a data problem for a human to look at,
			# and silently removing it would destroy the evidence. Every row for
			# the pair is corrected so the resolver cannot read a stale one.
			for row in existing:
				current = _as_float(row.get("rate"))
				if current is not None and abs(current - rate) <= _EPSILON:
					log["existing"].append(f"{label} = {rate}")
					summary["unchanged"] += 1
					continue
				frappe.db.set_value(doctype, row["name"], "rate", rate, update_modified=True)
				log["updated"].append(f"{label}: {current} -> {rate}")
				summary["category_rates_updated"] += 1
			return

		doc = frappe.get_doc(
			{
				"doctype": doctype,
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


def _ensure_item_price(item_code, rate, currency, log, summary):
	"""Create or correct the generic Employee Item Price for one item.

	Follows the ``scripts/seed_example_b2b_prices._upsert_item_price`` idiom
	(look up the pair, ``set_value`` when found, insert otherwise, never delete a
	duplicate) with ONE deliberate narrowing: only the GENERIC row — the one with
	no customer — is touched. ``invoice_creation._resolve_item_rate`` reads a
	customer-scoped Item Price ahead of the generic one, so a per-person special
	rate on the Employee list is a deliberate override and is left alone.
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
					# Counted, not listed: an already-correct item is the normal
					# case for every one of ~20 jars, and naming them all would
					# bury the created/updated lines that actually need reading.
					summary["unchanged"] += 1
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


def _report_drift(log):
	"""Make a corrected rate impossible to miss.

	``frappe.logger().info`` is effectively silent on the servers (the default log
	level there is ERROR), and a rate that was edited in the UI and then reverted
	by a migrate is exactly the change somebody will come asking about. So drift —
	and only drift, never a normal seed — is also written to the Error Log, where
	it survives and is searchable.

	``frappe.log_error`` writes a document and can itself raise, so it is guarded:
	a failure to record the correction must not undo it.
	"""
	if not log["updated"]:
		return
	message = (
		f"Employee price list rates corrected on migrate "
		f"(Large={LARGE_RATE}, Medium={MEDIUM_RATE}): " + "; ".join(log["updated"])
	)
	_logger().warning(message)
	try:
		frappe.log_error(message, "Employee price list drift corrected")
	except Exception:
		pass


def ensure_employee_price_list_rates():
	"""Guarantee the Employee price list prices Large at 92 and Medium at 77.

	Idempotent and safe to run on every migrate: values that are already correct
	are not rewritten, so a steady-state migrate performs no writes at all.
	Corrections ARE made (see the module docstring) and are always reported.

	Returns the log dict, with per-group counts under ``summary``.
	"""
	log = {"created": [], "updated": [], "existing": [], "summary": {}}
	summary = {
		"large_items": 0,
		"medium_items": 0,
		"category_rates_created": 0,
		"category_rates_updated": 0,
		"item_prices_created": 0,
		"item_prices_updated": 0,
		"unchanged": 0,
		"skipped": [],
		"failed": [],
	}
	log["summary"] = summary
	logger = _logger()

	try:
		# The price list is seeded by b2b_master_data on the same after_migrate
		# run; this module must be registered AFTER it. If it is somehow absent,
		# stop cleanly rather than creating Item Prices pointing nowhere.
		if not frappe.db.exists("Price List", PRICE_LIST):
			summary["skipped"].append(f"Price List '{PRICE_LIST}' does not exist")
			logger.warning(
				f"Employee pricing skipped: Price List '{PRICE_LIST}' does not exist "
				f"(is setup.b2b_master_data.ensure_b2b_master_data registered before this?)"
			)
			return log

		currency = _price_list_currency()

		large_groups = _existing_item_groups(_LARGE_GROUPS)
		medium_groups = _existing_item_groups(_MEDIUM_GROUPS)
		for missing in set(_LARGE_GROUPS + _MEDIUM_GROUPS) - set(large_groups + medium_groups):
			summary["skipped"].append(f"Item Group '{missing}' does not exist")

		# Layer 1: the category rates. Written first because they are the layer
		# that survives new items being created later.
		for group in large_groups:
			_ensure_category_rate(group, LARGE_RATE, log, summary)
		for group in medium_groups:
			_ensure_category_rate(group, MEDIUM_RATE, log, summary)

		# Layer 2: per-item prices for everything that exists today.
		large_items = _items_for_groups(large_groups)
		medium_items = _items_for_groups(medium_groups)
		summary["large_items"] = len(large_items)
		summary["medium_items"] = len(medium_items)

		for code in large_items:
			_ensure_item_price(code, LARGE_RATE, currency, log, summary)
		for code in medium_items:
			_ensure_item_price(code, MEDIUM_RATE, currency, log, summary)

		# Resolved counts are logged even when nothing changed: the site has ~11
		# large items while the mix migration script knows 9 flavours, and that
		# gap is only ever visible if the number is stated out loud every migrate.
		logger.info(
			f"Employee pricing resolved {summary['large_items']} large item(s) in "
			f"{large_groups} @ {LARGE_RATE} and {summary['medium_items']} medium item(s) in "
			f"{medium_groups} @ {MEDIUM_RATE}"
		)

		if log["created"]:
			logger.info("Employee pricing created: " + "; ".join(log["created"]))
		_report_drift(log)
		if not log["created"] and not log["updated"]:
			logger.info(
				f"Employee pricing already correct: {summary['unchanged']} rate(s) verified"
			)
		if summary["skipped"]:
			logger.warning("Employee pricing skipped: " + "; ".join(summary["skipped"]))

		logger.info(f"ensure_employee_price_list_rates summary: {summary}")
	except Exception:
		# Never let pricing enforcement break a migrate. A wrong staff price is a
		# bad day; a bench that will not migrate is a worse one.
		logger.error("ensure_employee_price_list_rates failed unexpectedly", exc_info=True)

	return log
