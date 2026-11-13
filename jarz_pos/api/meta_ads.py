"""Meta Ads report -- which ads/ad sets/campaigns got how many purchases, CTR and ROAS.

Read-only. Pulls insights from the Meta Marketing API (Graph) with the ads_read token
stored in Meta CAPI Settings, and sets Meta's own purchase count next to the ERP truth
(submitted Sales Invoices whose ``woo_utm_content`` is the ad name), because Meta credits
every placed web order, cancelled ones included.

Everything that does arithmetic is a plain function with no site or network dependency
(``parse_insight_row``, ``derive``, ``allocate_erp``, ``aggregate`` ...), so it is unit
tested in ``tests/test_meta_ads.py``. The token is sent in an ``Authorization`` header,
never in a URL, and is never put in a message or a log.

``woo_utm_content`` is read with plain SQL; nothing is imported from the Woo app.
"""

import json
import re
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import unquote_plus

import frappe
import requests
from frappe import _

from jarz_pos.constants import ROLES

ALLOWED_ROLES = frozenset({ROLES.JARZ_MANAGER, ROLES.SYSTEM_MANAGER, ROLES.ADMINISTRATOR, "Meta CAPI Manager"})
LEVELS = ("campaign", "adset", "ad")
DEFAULT_ACCOUNT = "act_1528131027872835"
DEFAULT_VERSION = "v23.0"
DEFAULT_DAYS = 7
MAX_DAYS = 400
CACHE_SECONDS = 15 * 60
HTTP_TIMEOUT = 30
REPORT_BUDGET_SECONDS = 60
ACCOUNT_RE = re.compile(r"^act_\d+$")
VERSION_RE = re.compile(r"^v\d+\.\d+$")
SLOW_MESSAGE = "Meta is responding slowly; try again in a minute."
MAX_PAGES = 40
IDS_CHUNK = 50
# The same preference the marketing dashboard uses: first type present wins, never a sum.
PURCHASE_TYPES = ("omni_purchase", "purchase", "offsite_conversion.fb_pixel_purchase")

METRIC_KEYS = (
	"spend",
	"impressions",
	"reach",
	"frequency",
	"link_clicks",
	"ctr",
	"cpc",
	"cpm",
	"purchases",
	"purchase_value",
	"roas",
	"cost_per_purchase",
	"erp_orders",
	"erp_revenue",
	"erp_roas",
	"blended_revenue",
	"blended_roas",
)
INSIGHT_FIELDS = ",".join(
	[
		"account_currency",
		"campaign_id",
		"campaign_name",
		"adset_id",
		"adset_name",
		"ad_id",
		"ad_name",
		"spend",
		"impressions",
		"reach",
		"inline_link_clicks",
		"actions",
		"action_values",
	]
)


# -- pure helpers ------------------------------------------------------------------


def num(value, default=0.0) -> float:
	try:
		return float(value)
	except (TypeError, ValueError):
		return default


def pick_action(items, types=PURCHASE_TYPES) -> float:
	"""Value of the first action type in ``types`` that is present (never a sum of types)."""
	if not items:
		return 0.0
	by_type = {}
	for item in items:
		if isinstance(item, dict) and item.get("action_type") not in by_type:
			by_type[item.get("action_type")] = num(item.get("value"))
	for action_type in types:
		if action_type in by_type:
			return by_type[action_type]
	return 0.0


def _ratio(numerator, denominator, scale=1.0) -> float:
	return (numerator / denominator * scale) if denominator else 0.0


def derive(m: dict, blended_scale=0.0) -> dict:
	"""Fill the ratio metrics from the base counts and round. Never null, 0 on a zero denominator.

	``blended_scale`` (k = real retail sales / account-wide Meta purchase value) shares the
	real sales out by Meta purchase value: ``blended_revenue`` = purchase_value x k.
	"""
	spend = num(m.get("spend"))
	impressions = num(m.get("impressions"))
	reach = num(m.get("reach"))
	clicks = num(m.get("link_clicks"))
	purchases = num(m.get("purchases"))
	value = num(m.get("purchase_value"))
	erp_orders = num(m.get("erp_orders"))
	erp_revenue = num(m.get("erp_revenue"))
	blended_revenue = value * num(blended_scale)
	return {
		"spend": round(spend, 2),
		"impressions": int(impressions),
		"reach": int(reach),
		"frequency": round(_ratio(impressions, reach), 2),
		"link_clicks": int(clicks),
		"ctr": round(_ratio(clicks, impressions, 100), 2),
		"cpc": round(_ratio(spend, clicks), 2),
		"cpm": round(_ratio(spend, impressions, 1000), 2),
		"purchases": int(purchases) if purchases == int(purchases) else round(purchases, 2),
		"purchase_value": round(value, 2),
		"roas": round(_ratio(value, spend), 2),
		"cost_per_purchase": round(_ratio(spend, purchases), 2),
		"erp_orders": int(erp_orders),
		"erp_revenue": round(erp_revenue, 2),
		"erp_roas": round(_ratio(erp_revenue, spend), 2),
		"blended_revenue": round(blended_revenue, 2),
		"blended_roas": round(_ratio(blended_revenue, spend), 2),
	}


def parse_insight_row(row: dict) -> dict:
	"""Base counts of one Meta insights row (before ratios)."""
	return {
		"spend": num(row.get("spend")),
		"impressions": num(row.get("impressions")),
		"reach": num(row.get("reach")),
		"link_clicks": num(row.get("inline_link_clicks")),
		"purchases": pick_action(row.get("actions")),
		"purchase_value": pick_action(row.get("action_values")),
	}


def has_delivery(base: dict) -> bool:
	return num(base.get("spend")) > 0 or num(base.get("impressions")) > 0


def resolve_dates(date_from, date_to, today: date) -> tuple[date, date]:
	"""Default last 7 days ending ``today``; swap if reversed; cap at MAX_DAYS."""

	def parse(value):
		if not value:
			return None
		try:
			return datetime.strptime(str(value).strip()[:10], "%Y-%m-%d").date()
		except ValueError:
			return None

	end = parse(date_to) or today
	start = parse(date_from) or (end - timedelta(days=DEFAULT_DAYS - 1))
	if start > end:
		start, end = end, start
	if (end - start).days + 1 > MAX_DAYS:
		start = end - timedelta(days=MAX_DAYS - 1)
	return start, end


def normalise_level(level) -> str:
	level = (level or "ad").strip().lower()
	return level if level in LEVELS else "ad"


def name_key(value) -> str:
	"""Comparable form of an ad name: lower case, whitespace collapsed."""
	return " ".join(str(value or "").split()).lower()


def utm_key(value) -> str:
	"""Comparable form of a stored utm_content. The site keeps it URL-encoded
	(``Fixable+image``, ``Fixable%20image``), so decode before comparing."""
	return name_key(unquote_plus(str(value or "")))


def allocate_erp(ad_rows: list, erp_by_name: dict) -> dict:
	"""Give each ERP ad name to exactly one delivering ad, so no order is counted twice.

	``ad_rows`` need ``id``, ``name`` and ``spend``. Where several ads share a name the
	highest-spend one gets the ERP figures. Returns ``{ad_id: (orders, revenue)}``.
	"""
	lowered = {name_key(k): v for k, v in (erp_by_name or {}).items() if k}
	best = {}
	for ad in ad_rows:
		key = name_key(ad.get("name"))
		if key not in lowered:
			continue
		current = best.get(key)
		if current is None or (num(ad.get("spend")), str(ad.get("id"))) > (
			num(current.get("spend")),
			str(current.get("id")),
		):
			best[key] = ad
	return {best[key]["id"]: lowered[key] for key in best}


def duplicate_ad_names(ad_rows: list) -> bool:
	seen = set()
	for ad in ad_rows:
		key = str(ad.get("name") or "").strip().lower()
		if key in seen:
			return True
		seen.add(key)
	return False


def sum_bases(rows: list) -> dict:
	"""Add up base counts (``erp_orders`` / ``erp_revenue`` included when present)."""
	total = {k: 0.0 for k in ("spend", "impressions", "reach", "link_clicks", "purchases", "purchase_value", "erp_orders", "erp_revenue")}
	for row in rows:
		for key in total:
			total[key] += num(row.get(key))
	return total


def aggregate_erp(level: str, ad_rows: list, allocation: dict) -> dict:
	"""ERP orders / revenue per entity id at ``level``, summed over its delivering ads."""
	field = {"campaign": "campaign_id", "adset": "adset_id", "ad": "id"}[level]
	out = {}
	for ad in ad_rows:
		orders, revenue = allocation.get(ad["id"], (0, 0.0))
		entity = ad.get(field)
		bucket = out.setdefault(entity, [0, 0.0])
		bucket[0] += orders
		bucket[1] += revenue
	return {k: (v[0], v[1]) for k, v in out.items()}


def build_rows(level: str, insight_rows: list, erp_by_entity: dict, meta: dict, blended_scale=0.0) -> list:
	"""Final response rows: only entities that delivered, metrics derived, sorted by spend."""
	out = []
	for raw in insight_rows:
		base = parse_insight_row(raw)
		if not has_delivery(base):
			continue
		entity_id = raw.get(f"{level}_id")
		if not entity_id:
			continue
		orders, revenue = erp_by_entity.get(entity_id, (0, 0.0))
		base["erp_orders"], base["erp_revenue"] = orders, revenue
		info = (meta or {}).get(entity_id, {})
		row = {
			"id": entity_id,
			"name": raw.get(f"{level}_name") or entity_id,
			"status": info.get("status") or "UNKNOWN",
			"campaign_id": raw.get("campaign_id"),
			"campaign_name": raw.get("campaign_name"),
			"adset_id": raw.get("adset_id") if level != "campaign" else None,
			"adset_name": raw.get("adset_name") if level != "campaign" else None,
			"thumbnail_url": info.get("thumbnail_url") if level == "ad" else None,
		}
		row.update(derive(base, blended_scale))
		out.append(row)
	out.sort(key=lambda r: (-r["spend"], str(r["id"])))
	return out


def build_filters(ad_rows: list, status_by_id: dict, campaign_id: str | None) -> dict:
	"""Campaigns and ad sets that delivered, taken from the unfiltered ad-level rows."""
	campaigns, adsets = {}, {}
	for ad in ad_rows:
		if ad.get("campaign_id") and ad["campaign_id"] not in campaigns:
			campaigns[ad["campaign_id"]] = {
				"id": ad["campaign_id"],
				"name": ad.get("campaign_name") or ad["campaign_id"],
				"status": status_by_id.get(ad["campaign_id"]) or "UNKNOWN",
				"_spend": 0.0,
			}
		if ad.get("campaign_id"):
			campaigns[ad["campaign_id"]]["_spend"] += num(ad.get("spend"))
		if ad.get("adset_id") and (not campaign_id or ad.get("campaign_id") == campaign_id):
			if ad["adset_id"] not in adsets:
				adsets[ad["adset_id"]] = {
					"id": ad["adset_id"],
					"name": ad.get("adset_name") or ad["adset_id"],
					"campaign_id": ad.get("campaign_id"),
					"status": status_by_id.get(ad["adset_id"]) or "UNKNOWN",
					"_spend": 0.0,
				}
			adsets[ad["adset_id"]]["_spend"] += num(ad.get("spend"))

	def finish(items):
		ordered = sorted(items.values(), key=lambda x: (-x["_spend"], str(x["id"])))
		for item in ordered:
			item.pop("_spend", None)
		return ordered

	return {"campaigns": finish(campaigns), "adsets": finish(adsets)}


def matches_filters(ad: dict, campaign_id: str | None, adset_id: str | None) -> bool:
	if campaign_id and ad.get("campaign_id") != campaign_id:
		return False
	if adset_id and ad.get("adset_id") != adset_id:
		return False
	return True


def ad_rows_from_insights(raw_rows: list) -> list:
	"""Normalise level=ad insight rows into light dicts used for ERP allocation and filters."""
	out = []
	for raw in raw_rows:
		base = parse_insight_row(raw)
		if not has_delivery(base) or not raw.get("ad_id"):
			continue
		out.append(
			{
				"id": raw["ad_id"],
				"name": raw.get("ad_name") or raw["ad_id"],
				"adset_id": raw.get("adset_id"),
				"adset_name": raw.get("adset_name"),
				"campaign_id": raw.get("campaign_id"),
				"campaign_name": raw.get("campaign_name"),
				"spend": base["spend"],
				"purchase_value": base["purchase_value"],
			}
		)
	return out


# -- Meta Graph --------------------------------------------------------------------


class MetaError(Exception):
	"""A Meta/HTTP failure with a message that is safe to show (no token)."""


def _scrub(text, token) -> str:
	text = str(text or "")
	return text.replace(token, "***") if token else text


def _graph_get(session_get, url: str, params: dict, token: str) -> dict:
	try:
		resp = session_get(
			url,
			params=params,
			headers={"Authorization": f"Bearer {token}"},
			timeout=HTTP_TIMEOUT,
		)
	except requests.Timeout:
		raise MetaError("Meta did not answer in time. Try again in a minute.") from None
	except requests.RequestException as exc:
		raise MetaError(f"Could not reach Meta ({type(exc).__name__}).") from None
	try:
		body = resp.json()
	except ValueError:
		body = {}
	if resp.status_code < 400 and not isinstance(body, dict):
		raise MetaError("Unexpected reply from Meta.")
	if resp.status_code >= 400 or (isinstance(body, dict) and body.get("error")):
		err = body.get("error", {}) if isinstance(body, dict) else {}
		message = _scrub(err.get("error_user_msg") or err.get("message") or f"HTTP {resp.status_code}", token)
		raise MetaError(message)
	return body


def with_deadline(session_get, budget=REPORT_BUDGET_SECONDS, clock=time.monotonic):
	"""Wrap ``session_get`` so all calls made through it share one time budget.

	The deadline lives in this closure (one per report build), so nothing leaks between
	requests. Each call's timeout is capped to the remaining budget; once it is spent the
	next call raises ``MetaError`` without touching the network.
	"""
	deadline = clock() + budget

	def limited(url, params=None, headers=None, timeout=None):
		remaining = deadline - clock()
		if remaining <= 0:
			raise MetaError(SLOW_MESSAGE)
		try:
			return session_get(
				url, params=params, headers=headers, timeout=min(timeout or HTTP_TIMEOUT, remaining)
			)
		except requests.Timeout:
			if deadline - clock() <= 0:
				raise MetaError(SLOW_MESSAGE) from None
			raise

	return limited


def _graph_all(session_get, url: str, params: dict, token: str) -> list:
	"""Follow cursors (not ``paging.next``, which embeds the token in its URL)."""
	params = dict(params)
	params.setdefault("limit", 500)
	out = []
	for _page in range(MAX_PAGES):
		body = _graph_get(session_get, url, params, token)
		out.extend(body.get("data", []))
		paging = body.get("paging") or {}
		after = (paging.get("cursors") or {}).get("after")
		if not paging.get("next") or not after:
			break
		params["after"] = after
	return out


def fetch_insights(session_get, base_url, account, token, level, start, end, filtering=None) -> list:
	params = {
		"level": level,
		"fields": INSIGHT_FIELDS,
		"time_range": json.dumps({"since": str(start), "until": str(end)}),
	}
	if filtering:
		params["filtering"] = json.dumps(filtering)
	return _graph_all(session_get, f"{base_url}/{account}/insights", params, token)


ALL_STATUSES = [
	"ACTIVE",
	"PAUSED",
	"ARCHIVED",
	"CAMPAIGN_PAUSED",
	"ADSET_PAUSED",
	"IN_PROCESS",
	"WITH_ISSUES",
	"PENDING_REVIEW",
	"DISAPPROVED",
	"PREAPPROVED",
	"PENDING_BILLING_INFO",
]
EDGE_BY_LEVEL = {"campaign": "campaigns", "adset": "adsets", "ad": "ads"}


def fetch_entity_meta(session_get, base_url, account, token, level, ids, with_creative=False) -> dict:
	"""Status (and thumbnail) for entities by id, via the account edge filtered on ``id`` in chunks of 50.

	The root ``?ids=`` form is refused by Graph on this account, hence the edge.
	"""
	fields = "id,effective_status,status"
	if with_creative:
		fields += ",creative.thumbnail_width(400).thumbnail_height(400){thumbnail_url,image_url}"
	out = {}
	ids = [i for i in dict.fromkeys(ids) if i]
	for i in range(0, len(ids), IDS_CHUNK):
		chunk = ids[i : i + IDS_CHUNK]
		filtering = [
			{"field": "id", "operator": "IN", "value": chunk},
			{"field": "effective_status", "operator": "IN", "value": ALL_STATUSES},
		]
		params = {"fields": fields, "limit": IDS_CHUNK, "filtering": json.dumps(filtering)}
		for info in _graph_all(session_get, f"{base_url}/{account}/{EDGE_BY_LEVEL[level]}", params, token):
			creative = info.get("creative") or {}
			out[info.get("id")] = {
				"status": info.get("effective_status") or info.get("status"),
				"thumbnail_url": creative.get("thumbnail_url") or creative.get("image_url"),
			}
	return out


# -- ERP truth ---------------------------------------------------------------------


def fetch_erp_by_name(names: list, start: date, end: date) -> dict | None:
	"""{ad name key: (orders, revenue)}; None when the Woo UTM column is absent.

	utm_content is stored URL-encoded (``Fixable+image``), so every UTM in the range is
	grouped in SQL and decoded here before matching the ad names."""
	if not frappe.db.has_column("Sales Invoice", "woo_utm_content"):
		return None
	wanted = {name_key(n) for n in names if n}
	if not wanted:
		return {}
	rows = frappe.db.sql(
		"""
		SELECT woo_utm_content, COUNT(*) AS orders, COALESCE(SUM(grand_total), 0) AS revenue
		FROM `tabSales Invoice`
		WHERE docstatus = 1 AND is_return = 0
			AND posting_date BETWEEN %(start)s AND %(end)s
			AND IFNULL(woo_utm_content, '') != ''
		GROUP BY woo_utm_content
		""",
		{"start": start, "end": end},
		as_dict=True,
	)
	out = {}
	for r in rows:
		key = utm_key(r["woo_utm_content"])
		if key not in wanted:
			continue
		prev = out.get(key, (0, 0.0))
		out[key] = (prev[0] + int(r["orders"]), prev[1] + float(r["revenue"]))
	return out


# -- blended ROAS (every retail sale vs all Meta spend) -----------------------------

#: Included in blended ROAS, in response order. ``woo`` channel -> ``website``.
BLENDED_INCLUDED = ("website", "pos")
#: Left out of blended ROAS, in response order.
BLENDED_EXCLUDED = ("whatsapp_followup", "b2b", "staff", "samples", "talabat", "partner")
_CHANNEL_TO_GROUP = {"woo": "website", "pos": "pos"}
FOLLOWUP_TOUCH_DOCTYPE = "Jarz Follow-up Touch"
FALLBACK_FOLLOWUP_WINDOW_DAYS = 30
BLENDED_NOTE = (
	"Blended ROAS counts every website and in-shop/chat sale in the period against all Meta spend; "
	"B2B, staff, samples, Talabat and orders after a WhatsApp follow-up are left out."
)
BLENDED_ROW_NOTE = (
	"Per-row Blended ROAS shares the real retail sales out in proportion to Meta's purchase value; "
	"it is an estimate, not a per-ad count."
)


def blended_scale(revenue, meta_purchase_value) -> float:
	"""k = real retail sales / account-wide Meta purchase value; 0 when Meta's value is 0."""
	return _ratio(num(revenue), num(meta_purchase_value))


def summarise_blended(rows: list, spend, meta_purchase_value=0.0) -> dict:
	"""The ``blended`` object from rows of ``{channel, followup, orders, revenue}``.

	Each invoice lands in exactly one group: b2b/staff/samples/talabat/partner keep their
	own group even after a follow-up; only website/pos sales move to ``whatsapp_followup``.
	``meta_purchase_value`` is Meta's account-wide purchase value for the range (unfiltered),
	from which ``scale`` is derived. All keys always present, zeros allowed, never null.
	"""
	acc = {g: [0, 0.0] for g in BLENDED_INCLUDED + BLENDED_EXCLUDED}
	for r in rows or []:
		channel = str(r.get("channel") or "").strip().lower()
		if channel in _CHANNEL_TO_GROUP:
			group = "whatsapp_followup" if num(r.get("followup")) else _CHANNEL_TO_GROUP[channel]
		elif channel in acc:
			group = channel
		else:
			continue
		acc[group][0] += int(num(r.get("orders")))
		acc[group][1] += num(r.get("revenue"))

	spend = num(spend)
	meta_value = num(meta_purchase_value)
	orders = sum(acc[g][0] for g in BLENDED_INCLUDED)
	revenue = sum(acc[g][1] for g in BLENDED_INCLUDED)

	def items(groups):
		return [{"group": g, "orders": int(acc[g][0]), "revenue": round(acc[g][1], 2)} for g in groups]

	return {
		"spend": round(spend, 2),
		"orders": int(orders),
		"revenue": round(revenue, 2),
		"roas": round(_ratio(revenue, spend), 2),
		"avg_order_value": round(_ratio(revenue, orders), 2),
		"included": items(BLENDED_INCLUDED),
		"excluded": items(BLENDED_EXCLUDED),
		"scale": round(blended_scale(revenue, meta_value), 4),
		"meta_purchase_value": round(meta_value, 2),
	}


def blended_sql(channel_expr: str, with_followup: bool, window_days: int) -> str:
	"""One query: submitted, non-return invoices in the range grouped by (channel, follow-up flag).

	The follow-up flag is 1 when the same customer had a WhatsApp follow-up that was sent
	(``Done`` with a message id) in the ``window_days`` before the invoice was created --
	the same window the Follow-up Results page credits orders with.
	"""
	if with_followup:
		followup = f"""EXISTS (
			SELECT 1 FROM `tab{FOLLOWUP_TOUCH_DOCTYPE}` t
			WHERE t.customer = si.customer
				AND t.status = 'Done'
				AND IFNULL(t.wa_message_id, '') != ''
				AND t.done_at >= DATE_SUB(si.creation, INTERVAL {int(window_days)} DAY)
				AND t.done_at < si.creation
		)"""
	else:
		followup = "0"
	return f"""
		SELECT {channel_expr} AS channel, ({followup}) AS followup,
			COUNT(*) AS orders, COALESCE(SUM(si.grand_total), 0) AS revenue
		FROM `tabSales Invoice` si
		WHERE si.docstatus = 1 AND si.is_return = 0
			AND si.posting_date BETWEEN %(start)s AND %(end)s
		GROUP BY 1, 2
	"""


def _channel_expr() -> str:
	from jarz_pos.api.ops_reports import _channel_sql

	return _channel_sql("si")


def _followup_window_days() -> int:
	try:
		from jarz_pos.services.customer_followups import ORDER_WINDOW_DAYS

		return int(ORDER_WINDOW_DAYS)
	except Exception:
		return FALLBACK_FOLLOWUP_WINDOW_DAYS


def _followup_available() -> bool:
	if not frappe.db.exists("DocType", FOLLOWUP_TOUCH_DOCTYPE):
		return False
	return all(
		frappe.db.has_column(FOLLOWUP_TOUCH_DOCTYPE, col) for col in ("customer", "status", "wa_message_id", "done_at")
	)


def fetch_blended(start: date, end: date) -> list:
	"""Rows of ``{channel, followup, orders, revenue}`` for the range (one SQL query)."""
	sql = blended_sql(_channel_expr(), _followup_available(), _followup_window_days())
	return frappe.db.sql(sql, {"start": start, "end": end}, as_dict=True) or []


# -- endpoint ----------------------------------------------------------------------


def _cairo_now() -> datetime:
	try:
		from zoneinfo import ZoneInfo

		return datetime.now(ZoneInfo("Africa/Cairo"))
	except Exception:
		return datetime.now(timezone(timedelta(hours=2)))


def _ensure_allowed() -> None:
	roles = set(frappe.get_roles(frappe.session.user))
	if not roles & ALLOWED_ROLES:
		frappe.throw(_("You are not allowed to see the Meta Ads report."), frappe.PermissionError)


def clean_account(value) -> str:
	"""``act_<digits>``; a bare number gets the prefix; anything else is the default."""
	value = str(value or "").strip()
	if value.isascii() and value.isdigit():
		value = f"act_{value}"
	return value if ACCOUNT_RE.match(value) else DEFAULT_ACCOUNT


def clean_version(value) -> str:
	value = str(value or "").strip()
	return value if VERSION_RE.match(value) else DEFAULT_VERSION


def _read_settings() -> dict:
	doc = frappe.get_doc("Meta CAPI Settings")
	token = doc.get_password("ads_access_token", raise_exception=False)
	return {
		"token": (token or "").strip(),
		"account": clean_account(doc.get("ads_account_id")),
		"version": clean_version(doc.get("ads_api_version")),
	}


def _empty_response(configured, level, start, end, notes):
	return {
		"configured": configured,
		"level": level,
		"date_from": str(start),
		"date_to": str(end),
		"currency": "EGP",
		"fetched_at": _cairo_now().strftime("%Y-%m-%d %H:%M:%S"),
		"totals": derive({}),
		"rows": [],
		"filters": {"campaigns": [], "adsets": []},
		"blended": summarise_blended([], 0),
		"notes": notes,
	}


def build_report(
	session_get, settings, erp_fetch, level, start, end, campaign_id=None, adset_id=None,
	budget=REPORT_BUDGET_SECONDS, clock=time.monotonic, blended_fetch=None,
) -> dict:
	"""The whole report from injected collaborators (Graph GET, ERP lookup, blended lookup, clock).

	All Graph calls share one ``budget`` (seconds); see ``with_deadline``.
	``blended_fetch(start, end)`` returns ``{channel, followup, orders, revenue}`` rows; None
	means no blended figures (all zeros).
	"""
	session_get = with_deadline(session_get, budget, clock)
	base_url = f"https://graph.facebook.com/{settings['version']}"
	account, token = settings["account"], settings["token"]

	# One unfiltered ad-level pull serves the ERP mapping, the filter lists and (at ad level) the rows.
	ad_raw = fetch_insights(session_get, base_url, account, token, "ad", start, end)
	ad_rows = ad_rows_from_insights(ad_raw)
	scoped_ads = [a for a in ad_rows if matches_filters(a, campaign_id, adset_id)]

	notes = [
		"Purchases and purchase value are Meta's own count (web orders are credited when placed, cancelled ones included). "
		"ERP orders are submitted invoices matched on the ad name."
	]
	erp_by_name = erp_fetch([a["name"] for a in scoped_ads], start, end)
	if erp_by_name is None:
		notes.append("ERP columns are 0: the website UTM column is not available on this site.")
		erp_by_name = {}
	elif duplicate_ad_names(scoped_ads):
		notes.append("Some ads share a name; ERP orders for a shared name are shown on the highest-spend ad only.")
	allocation = allocate_erp(scoped_ads, erp_by_name)
	erp_by_entity = aggregate_erp(level, scoped_ads, allocation)

	# Blended ROAS is account-wide: spend and Meta purchase value come from the UNFILTERED
	# ad rows, whatever the filters, so a filtered view gets its true share of real sales.
	try:
		blended_rows = blended_fetch(start, end) if blended_fetch else []
		blended_notes = [BLENDED_NOTE, BLENDED_ROW_NOTE]
	except Exception:
		blended_rows = []
		blended_notes = ["Blended ROAS is 0: the sales lookup failed (see Error Log)."]
		try:
			frappe.log_error(title="Meta Ads blended ROAS lookup failed", message=frappe.get_traceback())
		except Exception:
			pass
	blended = summarise_blended(
		blended_rows,
		sum(num(a.get("spend")) for a in ad_rows),
		sum(num(a.get("purchase_value")) for a in ad_rows),
	)
	# Full precision for the rows (blended["scale"] is rounded for display); 0 on a failed lookup.
	scale = blended_scale(blended["revenue"], blended["meta_purchase_value"])

	if level == "ad":
		level_raw = [r for r in ad_raw if matches_filters(r, campaign_id, adset_id)]
	else:
		filtering = []
		if campaign_id:
			filtering.append({"field": "campaign.id", "operator": "IN", "value": [campaign_id]})
		if adset_id:
			filtering.append({"field": "adset.id", "operator": "IN", "value": [adset_id]})
		level_raw = fetch_insights(session_get, base_url, account, token, level, start, end, filtering or None)

	level_ids = [r.get(f"{level}_id") for r in level_raw if has_delivery(parse_insight_row(r))]
	meta = fetch_entity_meta(session_get, base_url, account, token, level, level_ids, with_creative=(level == "ad"))
	rows = build_rows(level, level_raw, erp_by_entity, meta, scale)

	# Filters: campaigns/ad sets with delivery, independent of the campaign filter.
	filter_status = {}
	for kind, key in (("campaign", "campaign_id"), ("adset", "adset_id")):
		found = fetch_entity_meta(session_get, base_url, account, token, kind, [a.get(key) for a in ad_rows])
		filter_status.update({k: v.get("status") for k, v in found.items()})
	filters = build_filters(ad_rows, filter_status, campaign_id)

	totals_base = sum_bases([parse_insight_row(r) for r in level_raw if has_delivery(parse_insight_row(r))])
	totals_base["erp_orders"] = sum(allocation.get(a["id"], (0, 0.0))[0] for a in scoped_ads)
	totals_base["erp_revenue"] = sum(allocation.get(a["id"], (0, 0.0))[1] for a in scoped_ads)
	if level != "ad" and len(rows) > 1:
		notes.append("Total reach adds up each row's reach, so people seen in several rows are counted more than once.")

	notes.extend(blended_notes)

	currency = next((r.get("account_currency") for r in ad_raw if r.get("account_currency")), "EGP")
	return {
		"configured": True,
		"level": level,
		"date_from": str(start),
		"date_to": str(end),
		"currency": currency,
		"fetched_at": _cairo_now().strftime("%Y-%m-%d %H:%M:%S"),
		"totals": derive(totals_base, scale),
		"rows": rows,
		"filters": filters,
		"blended": blended,
		"notes": notes,
	}


def _cache_key(level, start, end, campaign_id, adset_id) -> str:
	return f"jarz_meta_ads_report:{level}:{start}:{end}:{campaign_id or ''}:{adset_id or ''}"


@frappe.whitelist()
def get_ads_report(level=None, date_from=None, date_to=None, campaign_id=None, adset_id=None, refresh=None):
	"""Meta Ads performance by campaign, ad set or ad, with ERP order truth beside it."""
	_ensure_allowed()
	level = normalise_level(level)
	start, end = resolve_dates(date_from, date_to, _cairo_now().date())
	campaign_id = (campaign_id or "").strip() or None
	adset_id = (adset_id or "").strip() or None

	settings = _read_settings()
	if not settings["token"]:
		return _empty_response(False, level, start, end, ["Meta Ads reporting is not configured: add the Ads Access Token in Meta CAPI Settings."])

	key = _cache_key(level, start, end, campaign_id, adset_id)
	force = str(refresh or "").strip().lower() in ("1", "true", "yes")
	if not force:
		cached = frappe.cache().get_value(key)
		if cached:
			return cached

	try:
		report = build_report(
			requests.get, settings, fetch_erp_by_name, level, start, end, campaign_id, adset_id,
			blended_fetch=fetch_blended,
		)
	except MetaError as exc:
		frappe.throw(_("Meta Ads: {0}").format(_scrub(exc, settings["token"])))
	frappe.cache().set_value(key, report, expires_in_sec=CACHE_SECONDS)
	return report
