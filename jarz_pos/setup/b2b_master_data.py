"""B2B master-data setup (Phase 0).

Idempotent, create-only configuration seeding for the B2B commercial policy
feature. This is safe to run on every ``bench migrate`` via the ``after_migrate``
hook: every record is checked with ``frappe.db.exists`` before insert, inserts use
``ignore_permissions=True``, and no existing record is ever overwritten. Each item
is wrapped so that a single failure logs and the rest continue.

This module must import cleanly with NO top-level frappe calls.
"""

import frappe

LOGGER_NAME = "b2b_master_data"


def _logger():
	return frappe.logger(LOGGER_NAME, allow_site=True)


def _default_currency():
	"""Return the default company's currency, falling back to ``EGP``."""
	try:
		company = frappe.defaults.get_global_default("company")
		if company:
			currency = frappe.db.get_value("Company", company, "default_currency")
			if currency:
				return currency
	except Exception:
		_logger().warning("Could not resolve default company currency; using EGP")
	return "EGP"


def _ensure_b2b_roles(log):
	"""Ensure the B2B Sales Rep role and B2B Sales role profile exist.

	Create-only: never modifies an existing Role or Role Profile. v15/v16 safe —
	the Role/Role Profile doctypes always exist in core, but each unit is wrapped
	so a failure logs and the rest continue.
	"""
	role_name = "B2B Sales Rep"
	try:
		if frappe.db.exists("Role", role_name):
			log["existing"].append(f"Role: {role_name}")
		else:
			doc = frappe.get_doc(
				{
					"doctype": "Role",
					"role_name": role_name,
					"desk_access": 1,
					"disabled": 0,
				}
			)
			doc.insert(ignore_permissions=True)
			log["created"].append(f"Role: {role_name}")
	except Exception:
		_logger().error(f"Failed to ensure Role '{role_name}'", exc_info=True)

	# Bundle the role into a Role Profile (only if the role actually exists).
	profile_name = "B2B Sales"
	try:
		if not frappe.db.exists("Role", role_name):
			return
		if frappe.db.exists("Role Profile", profile_name):
			log["existing"].append(f"Role Profile: {profile_name}")
			return
		doc = frappe.get_doc(
			{
				"doctype": "Role Profile",
				"role_profile": profile_name,
				"roles": [{"role": role_name}],
			}
		)
		doc.insert(ignore_permissions=True)
		log["created"].append(f"Role Profile: {profile_name}")
	except Exception:
		_logger().error(
			f"Failed to ensure Role Profile '{profile_name}'", exc_info=True
		)


def _ensure_customer_groups(log):
	groups = ["B2B", "Distributor", "Employee", "Sample"]
	parent = "All Customer Groups"
	for name in groups:
		try:
			if frappe.db.exists("Customer Group", name):
				log["existing"].append(f"Customer Group: {name}")
				continue
			doc = frappe.get_doc(
				{
					"doctype": "Customer Group",
					"customer_group_name": name,
					"parent_customer_group": parent,
					"is_group": 0,
				}
			)
			doc.insert(ignore_permissions=True)
			log["created"].append(f"Customer Group: {name}")
		except Exception:
			_logger().error(f"Failed to ensure Customer Group '{name}'", exc_info=True)


def _ensure_price_lists(log, currency):
	price_lists = ["B2B Selling", "Employee", "Sample"]
	for name in price_lists:
		try:
			if frappe.db.exists("Price List", name):
				log["existing"].append(f"Price List: {name}")
				continue
			doc = frappe.get_doc(
				{
					"doctype": "Price List",
					"price_list_name": name,
					"selling": 1,
					"buying": 0,
					"enabled": 1,
					"currency": currency,
				}
			)
			doc.insert(ignore_permissions=True)
			log["created"].append(f"Price List: {name} ({currency})")
		except Exception:
			_logger().error(f"Failed to ensure Price List '{name}'", exc_info=True)


def _ensure_opportunity_lost_reasons(log):
	reasons = [
		"Price too high",
		"Chose competitor",
		"No budget",
		"No response",
		"Not a fit",
		"Out of service area",
	]
	for reason in reasons:
		try:
			if frappe.db.exists("Opportunity Lost Reason", reason):
				log["existing"].append(f"Opportunity Lost Reason: {reason}")
				continue
			doc = frappe.get_doc(
				{
					"doctype": "Opportunity Lost Reason",
					"lost_reason": reason,
				}
			)
			doc.insert(ignore_permissions=True)
			log["created"].append(f"Opportunity Lost Reason: {reason}")
		except Exception:
			_logger().error(
				f"Failed to ensure Opportunity Lost Reason '{reason}'", exc_info=True
			)


def _ensure_lead_sources(log):
	sources = [
		"Walk-in",
		"Referral",
		"Social Media",
		"Cold Call",
		"WhatsApp",
		"Existing Customer",
	]
	for source in sources:
		try:
			if frappe.db.exists("Lead Source", source):
				log["existing"].append(f"Lead Source: {source}")
				continue
			doc = frappe.get_doc(
				{
					"doctype": "Lead Source",
					"source_name": source,
				}
			)
			doc.insert(ignore_permissions=True)
			log["created"].append(f"Lead Source: {source}")
		except Exception:
			_logger().error(f"Failed to ensure Lead Source '{source}'", exc_info=True)


def _ensure_lead_categories(log):
	"""Seed default Jarz Lead Category master records.

	Create-only: each category is checked with ``frappe.db.exists`` before insert
	and never overwritten. The Jarz Lead Category doctype is provided by this app;
	the whole unit is guarded so a failure logs and the rest of the seeding continues.
	"""
	categories = ["Coffee"]
	for name in categories:
		try:
			if frappe.db.exists("Jarz Lead Category", name):
				log["existing"].append(f"Jarz Lead Category: {name}")
				continue
			doc = frappe.get_doc(
				{
					"doctype": "Jarz Lead Category",
					"category_name": name,
				}
			)
			doc.insert(ignore_permissions=True)
			log["created"].append(f"Jarz Lead Category: {name}")
		except Exception:
			_logger().error(
				f"Failed to ensure Jarz Lead Category '{name}'", exc_info=True
			)


def _ensure_commercial_policies(log):
	"""Seed default Jarz Commercial Policy records.

	Policies that reference a price list are only created after confirming the
	referenced price list exists (price lists are seeded earlier in the flow).
	"""
	policies = [
		{
			"policy_name": "B2B Supply",
			"order_purpose": "B2B Supply",
			# No fixed price list: the B2B tier is resolved per-customer from
			# Customer.default_price_list → Customer Group.default_price_list, so each
			# B2B category (Customer Group) can carry its own pricing.
			"price_list": None,
			"shipping_income_behavior": "Zero",
			"shipping_expense_behavior": "Normal",
			"courier_behavior": "Courier",
			"priority": 100,
		},
		{
			"policy_name": "Employee Order",
			"order_purpose": "Employee",
			"price_list": "Employee",
			"shipping_income_behavior": "Zero",
			"shipping_expense_behavior": "Zero",
			"courier_behavior": "No Courier",
		},
		{
			"policy_name": "Sample (Courier)",
			"order_purpose": "Sample - Courier",
			"price_list": "Sample",
			"discount_percentage": 100,
			"shipping_income_behavior": "Zero",
			"shipping_expense_behavior": "Normal",
			"courier_behavior": "Courier",
		},
		{
			"policy_name": "Sample (No Courier)",
			"order_purpose": "Sample - No Courier",
			"price_list": "Sample",
			"discount_percentage": 100,
			"shipping_income_behavior": "Zero",
			"shipping_expense_behavior": "Zero",
			"courier_behavior": "No Courier",
		},
		{
			"policy_name": "Free Shipping Waiver",
			"order_purpose": "Free Shipping Waiver",
			"price_list": None,
			"shipping_income_behavior": "Zero",
			"shipping_expense_behavior": "Normal",
			"courier_behavior": "Courier",
		},
	]

	for spec in policies:
		name = spec["policy_name"]
		try:
			if frappe.db.exists("Jarz Commercial Policy", name):
				# One-time normalization: earlier seeds pinned "B2B Supply" to the
				# "B2B Selling" list. B2B pricing is now customer-group driven, so clear
				# that fixed list (only if still the old seed default — never clobber a
				# deliberate admin choice of a different list).
				if (
					name == "B2B Supply"
					and frappe.db.get_value("Jarz Commercial Policy", name, "price_list")
					== "B2B Selling"
				):
					frappe.db.set_value(
						"Jarz Commercial Policy", name, "price_list", None,
						update_modified=False,
					)
					log["created"].append(
						"Jarz Commercial Policy: B2B Supply (cleared fixed price list → customer-group driven)"
					)
				log["existing"].append(f"Jarz Commercial Policy: {name}")
				continue

			price_list = spec.get("price_list")
			if price_list and not frappe.db.exists("Price List", price_list):
				_logger().warning(
					f"Skipping policy '{name}': referenced Price List "
					f"'{price_list}' does not exist"
				)
				continue

			doc_data = {"doctype": "Jarz Commercial Policy", "enabled": 1}
			doc_data.update(spec)
			# Drop None price_list so the Link field stays empty.
			if doc_data.get("price_list") is None:
				doc_data.pop("price_list", None)

			doc = frappe.get_doc(doc_data)
			doc.insert(ignore_permissions=True)
			log["created"].append(f"Jarz Commercial Policy: {name}")
		except Exception:
			_logger().error(
				f"Failed to ensure Jarz Commercial Policy '{name}'", exc_info=True
			)


def ensure_b2b_master_data():
	"""Idempotently seed B2B master data. Safe to run on every migrate."""
	log = {"created": [], "existing": []}
	logger = _logger()

	try:
		currency = _default_currency()

		# Roles/role profile first (no dependency ordering required).
		_ensure_b2b_roles(log)
		# Order matters: price lists before the policies that reference them.
		_ensure_customer_groups(log)
		_ensure_price_lists(log, currency)
		_ensure_opportunity_lost_reasons(log)
		_ensure_lead_sources(log)
		_ensure_lead_categories(log)
		_ensure_commercial_policies(log)

		if log["created"]:
			logger.info(
				"B2B master data created: " + "; ".join(log["created"])
			)
		else:
			logger.info("B2B master data: nothing new to create")

		if log["existing"]:
			logger.info(
				"B2B master data already present: " + "; ".join(log["existing"])
			)
	except Exception:
		# Never let master-data seeding break a migrate.
		logger.error("ensure_b2b_master_data failed unexpectedly", exc_info=True)

	return log
