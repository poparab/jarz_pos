"""Seed realistic B2B DEMO data for the "B2B Sales & Clients" dashboard — STAGING ONLY.

Idempotent, get-or-create style seeding (mirrors
``jarz_pos/scripts/seed_example_b2b_prices.py`` and
``jarz_pos/setup/b2b_master_data.py``). Safe to run repeatedly: every mutation is
guarded (skip if already in the target state), inserts use ``ignore_permissions=True``,
and no existing record is ever deleted.

This populates the read-only ``jarz_pos.api.b2b_analytics.get_b2b_analytics`` payload
with real numbers by:
  1. Ensuring B2B master data exists (customer groups / price lists / commercial policy).
  2. Promoting ~15 already-active customers into the ``B2B`` group (and ~3 to ``Distributor``).
  3. Tagging their recent submitted invoices with ``custom_order_purpose='B2B Supply'`` and
     ``custom_commercial_policy='B2B Supply'`` so revenue/AOV/territory/policy charts fill in.
  4. Creating a spread of pipeline Opportunities across the real ``custom_b2b_stage`` values.
  5. Linking a few customers back to those Opportunities via ``custom_source_opportunity``
     so the conversion metric is non-zero.

PRODUCTION GUARD (critical): ``run`` refuses to execute unless the resolved site URL looks
like staging (contains ``stg``/``staging``). Production (``erp.orderjarz.com``) is hard-blocked
and cannot be overridden. Pass ``force=1`` only to override a non-staging, non-production URL
(e.g. a local dev site). Each unit of work is wrapped in its own try/except so a single bad
record logs and the rest continue.

Run with:
    bench --site <site> execute jarz_pos.scripts.seed_b2b_demo_data.run

This module is INERT: it is never auto-run by any hook and must import cleanly with NO
top-level frappe calls.
"""

import frappe
from frappe.utils import add_days, nowdate, getdate

from jarz_pos.setup.b2b_master_data import ensure_b2b_master_data

LOGGER_NAME = "seed_b2b_demo_data"

# ── Tunables ──────────────────────────────────────────────────────────────
WINDOW_DAYS = 120                 # lookback for "active" customers / invoices
MIN_INVOICES = 2                  # min submitted non-return invoices to qualify
TARGET_B2B_CUSTOMERS = 15         # customers promoted into a B2B group
TARGET_DISTRIBUTORS = 3           # of those, promoted to Distributor instead of B2B
MAX_INVOICES_TAGGED = 80          # cap on invoices flagged as B2B Supply
TARGET_OPPORTUNITIES = 9          # demo pipeline opportunities to maintain
TARGET_CONVERSIONS = 3            # customers linked back to a demo opportunity

# ── Verified master-data names (see custom_field.json / b2b_master_data.py) ─
B2B_GROUP = "B2B"
DISTRIBUTOR_GROUP = "Distributor"
B2B_GROUPS = (B2B_GROUP, DISTRIBUTOR_GROUP)
B2B_PURPOSE = "B2B Supply"                 # Sales Invoice.custom_order_purpose (Select)
B2B_POLICY = "B2B Supply"                  # Jarz Commercial Policy name (Link target)

# Non-terminal Opportunity.custom_b2b_stage options to cycle through.
PIPELINE_STAGES = ["Qualify", "Sample", "Approved", "Trial", "Check-up", "Active"]

# Idempotency marker for demo opportunities (stored in a stable free-text field).
DEMO_TITLE = "DEMO-B2B Pipeline"
DEMO_MARKER_EMAIL = "demo-b2b-seed@jarz.local"


def _logger():
	return frappe.logger(LOGGER_NAME, allow_site=True)


# ---------------------------------------------------------------------------
# Production guard
# ---------------------------------------------------------------------------
def _guard_environment(force):
	"""Return (ok, message). Refuse anything that is not clearly staging.

	Production (erp.orderjarz.com) is a HARD block that ``force`` cannot bypass.
	A non-staging, non-production URL (e.g. local dev) can be overridden with force.
	"""
	try:
		url = (frappe.utils.get_url() or "").lower()
	except Exception:
		url = ""

	is_prod = "erp.orderjarz.com" in url
	is_staging = ("stg" in url) or ("staging" in url)

	if is_prod:
		return False, (
			f"REFUSING to seed B2B demo data: resolved site URL '{url}' is PRODUCTION. "
			f"This seeder is staging-only and cannot be forced on production."
		)
	if not is_staging and not force:
		return False, (
			f"REFUSING to seed B2B demo data: resolved site URL '{url}' does not look like "
			f"staging (expected 'stg'/'staging'). Re-run with force=1 to override on a "
			f"non-production dev site."
		)
	return True, f"Environment OK to seed (url='{url}', staging={is_staging}, forced={bool(force)})."


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _default_company():
	company = frappe.defaults.get_global_default("company")
	if company:
		return company
	names = frappe.get_all("Company", pluck="name", limit=1)
	return names[0] if names else None


def _active_customers(cutoff):
	"""Customers with >= MIN_INVOICES submitted, non-return invoices since cutoff."""
	return frappe.db.sql(
		"""
		SELECT customer, COUNT(*) AS cnt
		FROM `tabSales Invoice`
		WHERE docstatus = 1
		  AND is_return = 0
		  AND customer IS NOT NULL AND customer != ''
		  AND posting_date >= %(cutoff)s
		GROUP BY customer
		HAVING cnt >= %(mininv)s
		ORDER BY cnt DESC, customer ASC
		""",
		{"cutoff": cutoff, "mininv": MIN_INVOICES},
		as_dict=True,
	)


# ---------------------------------------------------------------------------
# Step 3 — promote customers into B2B / Distributor groups
# ---------------------------------------------------------------------------
def _promote_customers(summary, cutoff):
	log = _logger()
	promoted = []          # customers we actually changed this run
	b2b_customers = []      # all customers now in a B2B group (changed + pre-existing)

	try:
		candidates = _active_customers(cutoff)
	except Exception:
		log.error("Failed to query active customers", exc_info=True)
		candidates = []

	selected = candidates[:TARGET_B2B_CUSTOMERS]
	for idx, row in enumerate(selected):
		cust = row["customer"]
		try:
			current_group = frappe.db.get_value("Customer", cust, "customer_group")
			# Deterministic: first TARGET_DISTRIBUTORS become Distributor, rest B2B.
			target_group = DISTRIBUTOR_GROUP if idx < TARGET_DISTRIBUTORS else B2B_GROUP

			if current_group in B2B_GROUPS:
				# Already B2B — idempotent skip (still eligible for later steps).
				b2b_customers.append(cust)
				continue

			frappe.db.set_value("Customer", cust, "customer_group", target_group)
			promoted.append((cust, target_group))
			b2b_customers.append(cust)
			log.info(f"Promoted customer {cust} -> {target_group}")
		except Exception:
			log.error(f"Failed to promote customer '{cust}'", exc_info=True)

	summary["customers_promoted"] = len(promoted)
	summary["customers_promoted_detail"] = [
		f"{c} -> {g}" for c, g in promoted
	]
	return b2b_customers


# ---------------------------------------------------------------------------
# Step 4 — tag recent invoices as B2B Supply
# ---------------------------------------------------------------------------
def _tag_invoices(summary, b2b_customers, cutoff):
	log = _logger()
	if not b2b_customers:
		summary["invoices_tagged"] = 0
		return

	# Ensure the referenced policy exists before we point invoices at it.
	policy = B2B_POLICY if frappe.db.exists("Jarz Commercial Policy", B2B_POLICY) else None
	if not policy:
		log.warning(
			f"Commercial policy '{B2B_POLICY}' missing; invoices will be tagged "
			f"with purpose only"
		)

	try:
		invoices = frappe.db.sql(
			"""
			SELECT name
			FROM `tabSales Invoice`
			WHERE docstatus = 1
			  AND is_return = 0
			  AND customer IN %(custs)s
			  AND posting_date >= %(cutoff)s
			  AND (custom_order_purpose IS NULL OR custom_order_purpose != %(purpose)s)
			ORDER BY posting_date DESC, name DESC
			LIMIT %(cap)s
			""",
			{
				"custs": tuple(b2b_customers),
				"cutoff": cutoff,
				"purpose": B2B_PURPOSE,
				"cap": MAX_INVOICES_TAGGED,
			},
			as_dict=True,
		)
	except Exception:
		log.error("Failed to query invoices to tag", exc_info=True)
		invoices = []

	tagged = 0
	for row in invoices:
		name = row["name"]
		try:
			# set_value bypasses submit locks — acceptable for a seeder.
			frappe.db.set_value(
				"Sales Invoice", name, "custom_order_purpose", B2B_PURPOSE,
				update_modified=False,
			)
			if policy:
				frappe.db.set_value(
					"Sales Invoice", name, "custom_commercial_policy", policy,
					update_modified=False,
				)
			tagged += 1
		except Exception:
			log.error(f"Failed to tag invoice '{name}'", exc_info=True)

	summary["invoices_tagged"] = tagged
	log.info(f"Tagged {tagged} invoice(s) as {B2B_PURPOSE}")


# ---------------------------------------------------------------------------
# Step 5 — create pipeline opportunities (idempotent via marker)
# ---------------------------------------------------------------------------
def _existing_demo_opportunities():
	"""Return demo opportunity names previously created by this seeder."""
	try:
		return frappe.get_all(
			"Opportunity",
			filters={"contact_email": DEMO_MARKER_EMAIL},
			pluck="name",
			order_by="creation asc",
		)
	except Exception:
		_logger().error("Failed to query existing demo opportunities", exc_info=True)
		return []


def _create_opportunities(summary, b2b_customers, company):
	log = _logger()
	existing = _existing_demo_opportunities()
	summary["opportunities_existing"] = len(existing)

	if not b2b_customers:
		log.warning("No B2B customers available; skipping opportunity creation")
		summary["opportunities_created"] = 0
		return existing

	if not company:
		log.warning("No default company resolved; skipping opportunity creation")
		summary["opportunities_created"] = 0
		return existing

	created = list(existing)
	to_create = max(0, TARGET_OPPORTUNITIES - len(existing))
	base_date = getdate(nowdate())

	for i in range(to_create):
		idx = len(existing) + i
		party = b2b_customers[idx % len(b2b_customers)]
		stage = PIPELINE_STAGES[idx % len(PIPELINE_STAGES)]
		# Deterministic amount spread (no wall-clock in computed values).
		amount = 5000 + (idx * 2500)
		# Deterministic transaction date inside the window.
		txn_date = add_days(base_date, -(5 + (idx * 7)))
		try:
			doc = frappe.get_doc(
				{
					"doctype": "Opportunity",
					"opportunity_from": "Customer",
					"party_name": party,
					"custom_b2b_stage": stage,
					"opportunity_amount": amount,
					"transaction_date": txn_date,
					"company": company,
					"title": DEMO_TITLE,
					"contact_email": DEMO_MARKER_EMAIL,
				}
			)
			doc.insert(ignore_permissions=True)
			created.append(doc.name)
			log.info(
				f"Created demo Opportunity {doc.name} "
				f"(party={party}, stage={stage}, amount={amount})"
			)
		except Exception:
			log.error(
				f"Failed to create demo opportunity (idx={idx}, party={party})",
				exc_info=True,
			)

	summary["opportunities_created"] = len(created) - len(existing)
	summary["opportunities_total"] = len(created)
	return created


# ---------------------------------------------------------------------------
# Step 5b — link customers back to opportunities (conversion metric)
# ---------------------------------------------------------------------------
def _link_conversions(summary, b2b_customers, opportunities):
	log = _logger()
	if not b2b_customers or not opportunities:
		summary["conversions_linked"] = 0
		return

	linked = 0
	for i in range(min(TARGET_CONVERSIONS, len(b2b_customers), len(opportunities))):
		cust = b2b_customers[i]
		opp = opportunities[i]
		try:
			current = frappe.db.get_value("Customer", cust, "custom_source_opportunity")
			if current:
				# Idempotent: already linked.
				continue
			frappe.db.set_value("Customer", cust, "custom_source_opportunity", opp)
			linked += 1
			log.info(f"Linked customer {cust} -> source opportunity {opp}")
		except Exception:
			log.error(
				f"Failed to link conversion (customer={cust}, opp={opp})",
				exc_info=True,
			)

	summary["conversions_linked"] = linked


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def run(force=0):
	"""Idempotently seed realistic B2B demo data for the staging dashboard.

	Refuses to run on production (and any non-staging URL unless force=1). Commits
	at the end and prints a summary plus a live analytics snapshot. Returns the
	summary dict.
	"""
	log = _logger()

	# 1) Production guard — do NOTHING if this is not clearly staging.
	try:
		force = int(force)
	except Exception:
		force = 1 if force else 0

	ok, msg = _guard_environment(force)
	print(msg)
	if not ok:
		log.warning(msg)
		return {"aborted": True, "reason": msg}

	summary = {
		"aborted": False,
		"customers_promoted": 0,
		"invoices_tagged": 0,
		"opportunities_created": 0,
		"opportunities_total": 0,
		"conversions_linked": 0,
	}

	cutoff = add_days(nowdate(), -WINDOW_DAYS)
	company = _default_company()

	# 2) Ensure master data (groups / price lists / B2B Supply policy).
	try:
		ensure_b2b_master_data()
	except Exception:
		log.error("ensure_b2b_master_data failed", exc_info=True)

	# 3) Promote active customers into B2B / Distributor.
	try:
		b2b_customers = _promote_customers(summary, cutoff)
	except Exception:
		log.error("_promote_customers failed unexpectedly", exc_info=True)
		b2b_customers = []

	# If promotion found none new but B2B customers already exist, reuse them so
	# steps 4/5 still populate on re-runs.
	if not b2b_customers:
		try:
			b2b_customers = frappe.get_all(
				"Customer",
				filters={"customer_group": ["in", list(B2B_GROUPS)], "disabled": 0},
				pluck="name",
				limit_page_length=TARGET_B2B_CUSTOMERS,
			)
		except Exception:
			log.error("Failed to load existing B2B customers", exc_info=True)
			b2b_customers = []

	# 4) Tag recent invoices as B2B Supply.
	try:
		_tag_invoices(summary, b2b_customers, cutoff)
	except Exception:
		log.error("_tag_invoices failed unexpectedly", exc_info=True)

	# 5) Create pipeline opportunities + link conversions.
	try:
		opportunities = _create_opportunities(summary, b2b_customers, company)
	except Exception:
		log.error("_create_opportunities failed unexpectedly", exc_info=True)
		opportunities = _existing_demo_opportunities()

	try:
		_link_conversions(summary, b2b_customers, opportunities)
	except Exception:
		log.error("_link_conversions failed unexpectedly", exc_info=True)

	# 6) Commit.
	try:
		frappe.db.commit()
	except Exception:
		log.error("Failed to commit seed_b2b_demo_data changes", exc_info=True)

	# Summary output.
	print("\n=== seed_b2b_demo_data summary ===")
	print(f"  customers promoted : {summary.get('customers_promoted')}")
	for detail in summary.get("customers_promoted_detail", []):
		print(f"      - {detail}")
	print(f"  invoices tagged    : {summary.get('invoices_tagged')}")
	print(f"  opportunities new  : {summary.get('opportunities_created')} "
		  f"(total demo: {summary.get('opportunities_total')})")
	print(f"  conversions linked : {summary.get('conversions_linked')}")

	# Convenience: show the live analytics result for the last 90 days.
	try:
		from jarz_pos.api.b2b_analytics import get_b2b_analytics

		date_to = nowdate()
		date_from = add_days(date_to, -90)
		result = get_b2b_analytics(date_from=date_from, date_to=date_to)
		print(f"\n=== get_b2b_analytics ({date_from} -> {date_to}) ===")
		print(f"  summary          : {result.get('summary')}")
		print(f"  pipeline_by_stage: {result.get('pipeline_by_stage')}")
		summary["analytics_snapshot"] = {
			"summary": result.get("summary"),
			"pipeline_by_stage": result.get("pipeline_by_stage"),
		}
	except Exception:
		log.error("Failed to render analytics snapshot", exc_info=True)

	log.info(f"seed_b2b_demo_data summary: {summary}")
	return summary
