import frappe
from frappe.utils import add_days, getdate, today

# ---------------------------------------------------------------------------
# Retail scope
# ---------------------------------------------------------------------------
# The reorder plan is about retail (website / delivery) customers. B2B supply,
# staff orders, samples and waivers are not repeat purchases by a consumer, so
# they must neither feed the segments nor start a follow-up cycle. Both the
# nightly segmentation and the follow-up generator build their SQL from the
# helpers below so "retail" means one thing everywhere.

#: ``Sales Invoice.custom_order_purpose`` values that count as a normal retail
#: order. NULL / '' (older invoices) are retail too. Every other purpose
#: (B2B Supply, Employee, Sample - Courier, Sample - No Courier,
#: Free Shipping Waiver) is out of scope.
RETAIL_PURPOSES = ("", "Standard")

#: ``Customer.customer_group`` values that are business customers (see
#: ``jarz_pos.api.b2b_analytics``).
BUSINESS_CUSTOMER_GROUPS = ("B2B", "Distributor")


def _sql_list(values):
	return ", ".join("'%s'" % v for v in values)


def _col_exists(doctype, column):
	try:
		return bool(frappe.db.has_column(doctype, column))
	except Exception:
		return False


def retail_invoice_conditions(website_only=True, alias="si", has_column=None):
	"""SQL conditions on the Sales Invoice (alias ``alias``) for retail scope.

	Constants only, no user input, and no ``%`` so it is safe to splice into a
	query that also takes parameters. A column that does not exist on the site
	is simply not filtered on, instead of failing the whole nightly job.
	"""
	has_column = has_column or _col_exists
	conds = [
		"%s.docstatus = 1" % alias,
		"%s.is_return = 0" % alias,
		"%s.customer IS NOT NULL" % alias,
	]
	if has_column("Sales Invoice", "custom_order_purpose"):
		conds.append("IFNULL(%s.custom_order_purpose, '') IN (%s)" % (alias, _sql_list(RETAIL_PURPOSES)))
	if website_only and has_column("Sales Invoice", "woo_order_id"):
		conds.append("IFNULL(%s.woo_order_id, 0) > 0" % alias)
	return conds


def retail_customer_condition(alias="c"):
	"""SQL condition keeping business customers (B2B / Distributor) out."""
	return "IFNULL(%s.customer_group, '') NOT IN (%s)" % (alias, _sql_list(BUSINESS_CUSTOMER_GROUPS))


def retail_scope_sql(website_only=True, inv_alias="si", cust_alias="c", has_column=None):
	"""Full retail scope as one ``AND``-able string (needs the Customer joined)."""
	conds = retail_invoice_conditions(website_only, inv_alias, has_column)
	conds.append(retail_customer_condition(cust_alias))
	return " AND ".join(conds)


def is_retail_invoice(purpose, customer_group, woo_order_id, website_only=True):
	"""Python twin of :func:`retail_scope_sql`, for tests and one-off checks."""
	if str(purpose or "").strip() not in RETAIL_PURPOSES:
		return False
	if str(customer_group or "").strip() in BUSINESS_CUSTOMER_GROUPS:
		return False
	if website_only and int(woo_order_id or 0) <= 0:
		return False
	return True


def setting_value(settings, name, default):
	"""Read a setting tolerant of a Single that never stored the field."""
	try:
		value = settings.get(name) if hasattr(settings, "get") else getattr(settings, name, None)
	except Exception:
		value = None
	if value in (None, ""):
		return default
	return value


def get_settings():
	return frappe.get_single("Jarz Segmentation Settings")


def website_only_enabled(settings=None):
	settings = settings or get_settings()
	return bool(int(setting_value(settings, "website_only", 1)))


def high_value_threshold(settings=None):
	settings = settings or get_settings()
	return float(setting_value(settings, "high_value_order_min", 1000) or 0)


def classify_customer(recency_days, frequency_90d, avg_order_value, lifetime_orders, settings):
	"""Apply RFM rules and return a segment name. First match wins."""
	s = settings

	if recency_days <= s.new_customer_recency_max and lifetime_orders == 1:
		return "New Customer"

	if lifetime_orders == 1 and recency_days > s.new_customer_recency_max:
		return "One-Time"

	if recency_days <= s.champion_recency_max and frequency_90d >= s.champion_frequency_min:
		return "Champion"

	if recency_days <= s.loyal_recency_max and frequency_90d >= s.loyal_frequency_min:
		return "Loyal"

	if recency_days <= s.loyal_recency_max and frequency_90d >= 1:
		return "Potential Loyalist"

	if recency_days >= s.cant_lose_recency_min and lifetime_orders >= s.cant_lose_frequency_min:
		return "Can't Lose Them"

	if recency_days >= s.lost_recency_min:
		return "Lost"

	if s.at_risk_recency_min <= recency_days <= s.at_risk_recency_max:
		return "At Risk"

	return "Loyal"


def compute_flags(window_orders, max_order_value, regular_frequency_min, high_value_min):
	"""Regular / high-value flags for one customer (pure)."""
	regular_min = max(int(regular_frequency_min or 0), 1)
	return {
		"rfm_is_regular": 1 if int(window_orders or 0) >= regular_min else 0,
		"rfm_has_high_value_order": 1
		if (float(high_value_min or 0) > 0 and float(max_order_value or 0) >= float(high_value_min))
		else 0,
	}


#: Customer columns this module writes besides the original five. Written only
#: when the column exists, so the job still runs on a site mid-migration.
_NEW_CUSTOMER_FIELDS = (
	"rfm_lifetime_orders",
	"rfm_is_regular",
	"rfm_last_order_value",
	"rfm_has_high_value_order",
)


def _present_new_fields():
	return {f for f in _NEW_CUSTOMER_FIELDS if _col_exists("Customer", f)}


def run_segmentation(dry_run=False):
	"""Main entry point. Called by scheduler nightly."""
	settings = get_settings()
	lookback = int(settings.lookback_days or 90)
	regular_window = int(setting_value(settings, "regular_window_days", 60))
	regular_min = int(setting_value(settings, "regular_frequency_min", 4))
	high_min = high_value_threshold(settings)
	website_only = website_only_enabled(settings)
	today_date = getdate(today())
	lookback_start = add_days(today_date, -lookback)
	regular_start = add_days(today_date, -regular_window)

	scope = retail_scope_sql(website_only)

	# One pass: lifetime, recency, average, frequency inside the lookback and
	# regular windows, the biggest order, and the latest order's value. The
	# latest value comes from GROUP_CONCAT ordered newest first (only the first
	# element is read, so the connection's group_concat_max_len cannot matter).
	customers = frappe.db.sql(
		f"""
        SELECT
            si.customer                                   AS customer,
            MAX(si.posting_date)                          AS last_order_date,
            COUNT(*)                                      AS lifetime_orders,
            AVG(si.grand_total)                           AS avg_order_value,
            MAX(si.grand_total)                           AS max_order_value,
            SUM(si.posting_date >= %(lookback_start)s)    AS freq_lookback,
            SUM(si.posting_date >= %(regular_start)s)     AS freq_regular,
            SUBSTRING_INDEX(
                GROUP_CONCAT(si.grand_total ORDER BY si.posting_date DESC, si.creation DESC),
                ',', 1)                                   AS last_order_value
        FROM `tabSales Invoice` si
        INNER JOIN `tabCustomer` c ON c.name = si.customer
        WHERE {scope}
        GROUP BY si.customer
    """,
		{
			"lookback_start": lookback_start,
			"regular_start": regular_start,
		},
		as_dict=True,
	)

	# Overrides in one query instead of one get_value per customer.
	overridden = set(
		frappe.db.sql_list("SELECT name FROM `tabCustomer` WHERE IFNULL(segment_override, 0) = 1")
	)

	new_fields = _present_new_fields()
	updated = 0
	skipped_override = 0

	for c in customers:
		if c.customer in overridden:
			skipped_override += 1
			continue

		recency_days = (today_date - getdate(c.last_order_date)).days
		frequency_90d = int(c.freq_lookback or 0)
		lifetime = int(c.lifetime_orders or 0)
		segment = classify_customer(
			recency_days=recency_days,
			frequency_90d=frequency_90d,
			avg_order_value=float(c.avg_order_value or 0),
			lifetime_orders=lifetime,
			settings=settings,
		)

		if not dry_run:
			values = {
				"customer_segment": segment,
				"segment_updated_on": today(),
				"rfm_recency_days": recency_days,
				"rfm_frequency_count": frequency_90d,
				"rfm_avg_order_value": float(c.avg_order_value or 0),
			}
			flags = compute_flags(c.freq_regular, c.max_order_value, regular_min, high_min)
			extra = {
				"rfm_lifetime_orders": lifetime,
				"rfm_last_order_value": float(c.last_order_value or 0),
				**flags,
			}
			values.update({k: v for k, v in extra.items() if k in new_fields})
			frappe.db.set_value("Customer", c.customer, values, update_modified=False)
			updated += 1

	cleared = 0
	if not dry_run:
		cleared = _clear_out_of_scope(
			scope_invoice_conds=retail_invoice_conditions(website_only, "x"), new_fields=new_fields
		)
		frappe.db.commit()

	# logger().info is silent on the servers; the return value is what the
	# caller (and a manual run) sees.
	return {
		"updated": updated,
		"skipped_override": skipped_override,
		"cleared_out_of_scope": cleared,
		"total_customers": len(customers),
	}


def _clear_out_of_scope(scope_invoice_conds, new_fields):
	"""Blank the segment of every customer with no retail order (one UPDATE).

	Without this a customer who is now out of scope (a B2B account, or someone
	whose only orders were samples) keeps the label from the last run when they
	were counted, and the dashboard keeps counting them. Manual overrides stay.
	"""
	sets = ["c.customer_segment = NULL"]
	if "rfm_is_regular" in new_fields:
		sets.append("c.rfm_is_regular = 0")
	if "rfm_has_high_value_order" in new_fields:
		sets.append("c.rfm_has_high_value_order = 0")
	if "rfm_lifetime_orders" in new_fields:
		sets.append("c.rfm_lifetime_orders = 0")
	conds = " AND ".join(scope_invoice_conds)
	frappe.db.sql(
		"""
        UPDATE `tabCustomer` c
        SET {sets}
        WHERE IFNULL(c.customer_segment, '') != ''
          AND IFNULL(c.segment_override, 0) = 0
          AND (
                IFNULL(c.customer_group, '') IN ({groups})
                OR NOT EXISTS (
                    SELECT 1 FROM `tabSales Invoice` x
                    WHERE x.customer = c.name AND {conds}
                )
          )
    """.format(sets=", ".join(sets), groups=_sql_list(BUSINESS_CUSTOMER_GROUPS), conds=conds)
	)
	return int((frappe.db.sql("SELECT ROW_COUNT()") or [[0]])[0][0] or 0)


REGULAR_PSEUDO_SEGMENT = "Regular"


def get_segment_summary():
	"""Count of customers per segment for the management page.

	Customers without a segment (out of retail scope, or never ordered) are not
	listed: the dashboard is about the retail base. A final pseudo-row
	``{"segment": "Regular", "count": n, "pseudo": 1}`` counts the customers
	flagged ``rfm_is_regular``; it overlaps the real segments by design.
	"""
	rows = frappe.db.sql(
		"""
        SELECT
            customer_segment AS segment,
            COUNT(*) AS count
        FROM `tabCustomer`
        WHERE disabled = 0
          AND IFNULL(customer_segment, '') != ''
        GROUP BY customer_segment
        ORDER BY count DESC
    """,
		as_dict=True,
	)

	regular = 0
	if _col_exists("Customer", "rfm_is_regular"):
		regular = int(
			frappe.db.sql(
				"SELECT COUNT(*) FROM `tabCustomer` WHERE disabled = 0 AND IFNULL(rfm_is_regular, 0) = 1"
			)[0][0]
			or 0
		)
	rows = list(rows)
	rows.append({"segment": REGULAR_PSEUDO_SEGMENT, "count": regular, "pseudo": 1})
	return rows


def export_segment_csv(segment):
	"""Returns list of customers in a given segment for CSV export.

	``segment == "Regular"`` is a pseudo-segment: customers flagged
	``rfm_is_regular`` whatever their RFM segment.
	"""
	present = _present_new_fields()
	extra_cols = ", ".join(
		("c.%s" % f) if f in present else ("NULL AS %s" % f)
		for f in ("rfm_lifetime_orders", "rfm_is_regular", "rfm_last_order_value")
	)
	if segment == REGULAR_PSEUDO_SEGMENT:
		if "rfm_is_regular" not in present:
			return []
		where = "c.rfm_is_regular = 1"
		params = ()
	else:
		where = "c.customer_segment = %s"
		params = (segment,)
	return frappe.db.sql(
		f"""
        SELECT
            c.name            AS customer_id,
            c.customer_name,
            c.mobile_no,
            c.territory,
            c.customer_segment,
            c.rfm_recency_days,
            c.rfm_frequency_count,
            c.rfm_avg_order_value,
            {extra_cols},
            c.segment_updated_on
        FROM `tabCustomer` c
        WHERE {where}
          AND c.disabled = 0
        ORDER BY c.rfm_avg_order_value DESC
    """,
		params,
		as_dict=True,
	)
