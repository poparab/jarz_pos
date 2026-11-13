"""Meta Ads report (api/meta_ads.py) -- pure functions and the report builder, no site.

Plain ``unittest.TestCase``; a stub ``frappe`` is registered when frappe is not installed.
The Graph API is replaced by a fake ``session_get``, so nothing leaves the machine.
"""

import json
import sys
import types
import unittest
from datetime import date
from types import SimpleNamespace
from unittest import mock

try:  # pragma: no cover - depends on the environment
	import frappe  # noqa: F401
except ImportError:  # pragma: no cover
	_fake = types.ModuleType("frappe")
	_fake.whitelist = lambda *a, **k: (a[0] if a and callable(a[0]) else (lambda fn: fn))
	_fake._ = lambda m: m
	_fake.local = SimpleNamespace()
	_fake.session = SimpleNamespace(user="Guest")
	_fake.db = SimpleNamespace()
	_fake.PermissionError = type("PermissionError", (Exception,), {})
	_fake.get_roles = lambda user=None: []
	sys.modules["frappe"] = _fake

from jarz_pos.api import meta_ads as m


class PickActionTest(unittest.TestCase):
	def test_omni_purchase_wins_over_plain_purchase(self):
		items = [
			{"action_type": "purchase", "value": "9"},
			{"action_type": "omni_purchase", "value": "7"},
		]
		self.assertEqual(m.pick_action(items), 7.0)

	def test_fallback_order_and_never_a_sum(self):
		self.assertEqual(m.pick_action([{"action_type": "purchase", "value": "4"}]), 4.0)
		both = [
			{"action_type": "offsite_conversion.fb_pixel_purchase", "value": "3"},
			{"action_type": "purchase", "value": "5"},
		]
		self.assertEqual(m.pick_action(both), 5.0)
		pixel = [{"action_type": "offsite_conversion.fb_pixel_purchase", "value": "3"}]
		self.assertEqual(m.pick_action(pixel), 3.0)

	def test_missing_or_garbage_is_zero(self):
		self.assertEqual(m.pick_action(None), 0.0)
		self.assertEqual(m.pick_action([]), 0.0)
		self.assertEqual(m.pick_action([{"action_type": "link_click", "value": "12"}]), 0.0)
		self.assertEqual(m.pick_action([{"action_type": "purchase", "value": "x"}]), 0.0)


class ParseAndDeriveTest(unittest.TestCase):
	ROW = {
		"spend": "100.50",
		"impressions": "10000",
		"reach": "5000",
		"inline_link_clicks": "200",
		"actions": [{"action_type": "omni_purchase", "value": "4"}, {"action_type": "purchase", "value": "4"}],
		"action_values": [{"action_type": "omni_purchase", "value": "804"}],
	}

	def test_parse_row(self):
		base = m.parse_insight_row(self.ROW)
		self.assertEqual(base["spend"], 100.5)
		self.assertEqual(base["link_clicks"], 200)
		self.assertEqual(base["purchases"], 4)
		self.assertEqual(base["purchase_value"], 804)

	def test_ratios(self):
		base = m.parse_insight_row(self.ROW)
		base.update(erp_orders=3, erp_revenue=603)
		d = m.derive(base)
		self.assertEqual(d["ctr"], 2.0)
		self.assertEqual(d["cpc"], 0.5)
		self.assertEqual(d["cpm"], 10.05)
		self.assertEqual(d["frequency"], 2.0)
		self.assertEqual(d["roas"], 8.0)
		self.assertAlmostEqual(d["cost_per_purchase"], 25.13, places=1)
		self.assertEqual(d["erp_roas"], 6.0)
		self.assertEqual(d["erp_orders"], 3)

	def test_zero_denominators_give_zero_never_none(self):
		d = m.derive({})
		self.assertEqual(set(d), set(m.METRIC_KEYS))
		self.assertTrue(all(v == 0 for v in d.values()))
		d = m.derive({"spend": 50, "impressions": 0, "purchases": 0, "purchase_value": 10})
		self.assertEqual(d["roas"], 0.2)
		self.assertEqual(d["cost_per_purchase"], 0)
		self.assertEqual(d["ctr"], 0)


class DatesTest(unittest.TestCase):
	TODAY = date(2026, 11, 7)

	def test_default_is_last_seven_days(self):
		self.assertEqual(m.resolve_dates(None, None, self.TODAY), (date(2026, 11, 1), date(2026, 11, 7)))

	def test_reversed_is_swapped(self):
		self.assertEqual(
			m.resolve_dates("2026-11-05", "2026-11-01", self.TODAY), (date(2026, 11, 1), date(2026, 11, 5))
		)

	def test_capped_at_400_days(self):
		start, end = m.resolve_dates("2020-01-01", "2026-11-07", self.TODAY)
		self.assertEqual(end, date(2026, 11, 7))
		self.assertEqual((end - start).days + 1, 400)

	def test_bad_date_falls_back(self):
		self.assertEqual(m.resolve_dates("nope", "2026-11-07", self.TODAY), (date(2026, 11, 1), date(2026, 11, 7)))

	def test_level_defaults_to_ad(self):
		self.assertEqual(m.normalise_level("Adset"), "adset")
		self.assertEqual(m.normalise_level("weird"), "ad")
		self.assertEqual(m.normalise_level(None), "ad")


ADS = [
	{"id": "a1", "name": "UGC-Sarah", "adset_id": "s1", "campaign_id": "c1", "spend": 100},
	{"id": "a2", "name": "Static-1", "adset_id": "s1", "campaign_id": "c1", "spend": 50},
	{"id": "a3", "name": "UGC-Sarah", "adset_id": "s2", "campaign_id": "c1", "spend": 30},
	{"id": "a4", "name": "Reel-9", "adset_id": "s3", "campaign_id": "c2", "spend": 10},
]


class ErpAllocationTest(unittest.TestCase):
	ERP = {"ugc-sarah": (6, 1800.0), "static-1": (2, 500.0)}

	def test_shared_name_goes_to_highest_spend_ad_once(self):
		alloc = m.allocate_erp(ADS, self.ERP)
		self.assertEqual(alloc, {"a1": (6, 1800.0), "a2": (2, 500.0)})

	def test_match_ignores_case_and_spaces(self):
		alloc = m.allocate_erp(ADS[:1], {"ugc-sarah": (1, 10.0)})
		self.assertEqual(alloc, {"a1": (1, 10.0)})
		alloc = m.allocate_erp([{"id": "x", "name": " UGC-SARAH ", "spend": 1}], self.ERP)
		self.assertEqual(alloc, {"x": (6, 1800.0)})

	def test_levels_sum_without_double_counting(self):
		alloc = m.allocate_erp(ADS, self.ERP)
		self.assertEqual(m.aggregate_erp("adset", ADS, alloc), {"s1": (8, 2300.0), "s2": (0, 0.0), "s3": (0, 0.0)})
		self.assertEqual(m.aggregate_erp("campaign", ADS, alloc), {"c1": (8, 2300.0), "c2": (0, 0.0)})
		self.assertEqual(sum(v[0] for v in m.aggregate_erp("campaign", ADS, alloc).values()), 8)

	def test_duplicate_names_detected(self):
		self.assertTrue(m.duplicate_ad_names(ADS))
		self.assertFalse(m.duplicate_ad_names(ADS[:2]))

	def test_utm_key_decodes_stored_utm(self):
		# Production stores utm_content URL-encoded ("Fixable+image"); found 2026-11-07.
		self.assertEqual(m.utm_key("Fixable+image"), "fixable image")
		self.assertEqual(m.utm_key("Fixable%20Video"), "fixable video")
		self.assertEqual(m.utm_key("E08_first_spoon_changed_it"), "e08_first_spoon_changed_it")
		self.assertEqual(m.name_key("  Fixable   Image "), "fixable image")

	def test_fetch_erp_matches_encoded_utm(self):
		rows = [
			{"woo_utm_content": "Fixable+image", "orders": 9, "revenue": 4500},
			{"woo_utm_content": "Fixable%20image", "orders": 1, "revenue": 500},
			{"woo_utm_content": "E08_first_spoon_changed_it", "orders": 19, "revenue": 10195},
			{"woo_utm_content": "{{ad.name}}", "orders": 1, "revenue": 300},
		]
		db = SimpleNamespace(has_column=lambda *a: True, sql=lambda *a, **k: rows)
		with mock.patch.object(m.frappe, "db", db, create=True):
			out = m.fetch_erp_by_name(["Fixable image", "E08_first_spoon_changed_it"], date(2026, 11, 1), date(2026, 11, 7))
		self.assertEqual(out, {"fixable image": (10, 5000.0), "e08_first_spoon_changed_it": (19, 10195.0)})
		alloc = m.allocate_erp([{"id": "f", "name": "Fixable image", "spend": 5}], out)
		self.assertEqual(alloc, {"f": (10, 5000.0)})


class BuildRowsTest(unittest.TestCase):
	RAW = [
		{"campaign_id": "c1", "campaign_name": "Camp", "adset_id": "s1", "adset_name": "Set", "ad_id": "a1",
		 "ad_name": "Small", "spend": "10", "impressions": "100"},
		{"campaign_id": "c1", "campaign_name": "Camp", "adset_id": "s1", "adset_name": "Set", "ad_id": "a2",
		 "ad_name": "Big", "spend": "90", "impressions": "900", "inline_link_clicks": "30",
		 "actions": [{"action_type": "omni_purchase", "value": "3"}],
		 "action_values": [{"action_type": "omni_purchase", "value": "450"}]},
		{"campaign_id": "c1", "adset_id": "s1", "ad_id": "a3", "ad_name": "Idle", "spend": "0", "impressions": "0"},
	]

	def test_rows_sorted_by_spend_and_idle_dropped(self):
		rows = m.build_rows("ad", self.RAW, {"a2": (2, 400.0)}, {"a2": {"status": "ACTIVE", "thumbnail_url": "http://t"}})
		self.assertEqual([r["id"] for r in rows], ["a2", "a1"])
		big = rows[0]
		self.assertEqual((big["purchases"], big["roas"], big["ctr"]), (3, 5.0, 3.33))
		self.assertEqual((big["erp_orders"], big["erp_roas"]), (2, 4.44))
		self.assertEqual((big["status"], big["thumbnail_url"]), ("ACTIVE", "http://t"))
		self.assertEqual(rows[1]["status"], "UNKNOWN")
		self.assertTrue(all(set(m.METRIC_KEYS) <= set(r) for r in rows))

	def test_campaign_rows_have_no_adset_or_thumbnail(self):
		raw = [{"campaign_id": "c1", "campaign_name": "Camp", "adset_id": "x", "spend": "5", "impressions": "50"}]
		row = m.build_rows("campaign", raw, {}, {"c1": {"status": "PAUSED", "thumbnail_url": "http://t"}})[0]
		self.assertIsNone(row["adset_id"])
		self.assertIsNone(row["adset_name"])
		self.assertIsNone(row["thumbnail_url"])
		self.assertEqual(row["name"], "Camp")


class FiltersTest(unittest.TestCase):
	def test_campaign_filter_limits_adsets_only(self):
		ads = [
			{"id": "a1", "adset_id": "s1", "adset_name": "S1", "campaign_id": "c1", "campaign_name": "C1", "spend": 5},
			{"id": "a2", "adset_id": "s2", "adset_name": "S2", "campaign_id": "c2", "campaign_name": "C2", "spend": 9},
		]
		f = m.build_filters(ads, {"c1": "ACTIVE", "s2": "PAUSED"}, "c1")
		self.assertEqual([c["id"] for c in f["campaigns"]], ["c2", "c1"])
		self.assertEqual([a["id"] for a in f["adsets"]], ["s1"])
		self.assertEqual(f["campaigns"][1]["status"], "ACTIVE")
		self.assertEqual(f["campaigns"][0]["status"], "UNKNOWN")
		self.assertEqual(f["adsets"][0]["campaign_id"], "c1")
		self.assertEqual(len(m.build_filters(ads, {}, None)["adsets"]), 2)


class FakeResp:
	def __init__(self, body, status=200):
		self._body, self.status_code = body, status

	def json(self):
		return self._body


class GraphTest(unittest.TestCase):
	def test_token_goes_in_header_never_in_params(self):
		seen = {}

		def get(url, params=None, headers=None, timeout=None):
			seen.update(url=url, params=params, headers=headers, timeout=timeout)
			return FakeResp({"data": []})

		m._graph_all(get, "https://g/x", {"fields": "a"}, "SECRET")
		self.assertNotIn("SECRET", str(seen["params"]) + seen["url"])
		self.assertEqual(seen["headers"], {"Authorization": "Bearer SECRET"})
		self.assertEqual(seen["timeout"], 30)

	def test_paging_follows_cursor(self):
		pages = [
			{"data": [1], "paging": {"cursors": {"after": "C1"}, "next": "https://g/x?access_token=SECRET"}},
			{"data": [2], "paging": {"cursors": {"after": "C2"}}},
		]
		calls = []

		def get(url, params=None, headers=None, timeout=None):
			calls.append(dict(params))
			return FakeResp(pages[len(calls) - 1])

		self.assertEqual(m._graph_all(get, "u", {}, "T"), [1, 2])
		self.assertNotIn("after", calls[0])
		self.assertEqual(calls[1]["after"], "C1")

	def test_meta_error_becomes_clean_message_without_token(self):
		def get(url, params=None, headers=None, timeout=None):
			return FakeResp({"error": {"message": "Invalid token SECRET123 expired"}}, 400)

		with self.assertRaises(m.MetaError) as ctx:
			m._graph_get(get, "u", {}, "SECRET123")
		self.assertIn("Invalid token", str(ctx.exception))
		self.assertNotIn("SECRET123", str(ctx.exception))


class BuildReportTest(unittest.TestCase):
	AD_ROWS = [
		{"account_currency": "EGP", "campaign_id": "c1", "campaign_name": "Camp", "adset_id": "s1",
		 "adset_name": "Set", "ad_id": "a1", "ad_name": "UGC", "spend": "100", "impressions": "1000",
		 "reach": "800", "inline_link_clicks": "20",
		 "actions": [{"action_type": "omni_purchase", "value": "5"}],
		 "action_values": [{"action_type": "omni_purchase", "value": "900"}]},
		{"campaign_id": "c1", "campaign_name": "Camp", "adset_id": "s1", "adset_name": "Set",
		 "ad_id": "a2", "ad_name": "Static", "spend": "50", "impressions": "500", "reach": "400"},
	]
	SETTINGS = {"token": "T", "account": "act_1", "version": "v23.0"}

	def _graph(self, ad_rows, level_rows=None):
		calls = []

		def get(url, params=None, headers=None, timeout=None):
			params = params or {}
			calls.append((url, params))
			if url.endswith("/insights"):
				rows = ad_rows if params["level"] == "ad" else level_rows
				return FakeResp({"data": rows})
			ids = json.loads(params["filtering"])[0]["value"]
			return FakeResp(
				{"data": [{"id": i, "effective_status": "ACTIVE", "creative": {"thumbnail_url": "http://t/" + i}} for i in ids]}
			)

		return get, calls

	def test_ad_level_uses_one_insights_call_and_erp_totals(self):
		get, calls = self._graph(self.AD_ROWS)

		def erp(names, s, e):
			return {"ugc": (3, 600.0)}

		rep = m.build_report(get, self.SETTINGS, erp, "ad", date(2026, 11, 1), date(2026, 11, 7))
		self.assertEqual(sum(1 for u, _p in calls if u.endswith("/insights")), 1)
		self.assertEqual([r["id"] for r in rep["rows"]], ["a1", "a2"])
		self.assertEqual(rep["rows"][0]["thumbnail_url"], "http://t/a1")
		self.assertEqual(rep["totals"]["spend"], 150)
		self.assertEqual(rep["totals"]["purchases"], 5)
		self.assertEqual(rep["totals"]["erp_orders"], 3)
		self.assertEqual(rep["totals"]["erp_roas"], 4.0)
		self.assertEqual(rep["currency"], "EGP")
		self.assertEqual([c["id"] for c in rep["filters"]["campaigns"]], ["c1"])
		self.assertTrue(rep["configured"])

	def test_campaign_level_filters_with_insights_filtering_and_sums_erp(self):
		camp = [{"campaign_id": "c1", "campaign_name": "Camp", "spend": "150", "impressions": "1500", "reach": "1000"}]
		get, calls = self._graph(self.AD_ROWS, camp)

		def erp(names, s, e):
			return {"ugc": (3, 600.0), "static": (1, 100.0)}

		rep = m.build_report(
			get, self.SETTINGS, erp, "campaign", date(2026, 11, 1), date(2026, 11, 7), campaign_id="c1"
		)
		level_call = [p for u, p in calls if u.endswith("/insights") and p["level"] == "campaign"][0]
		self.assertIn("campaign.id", level_call["filtering"])
		row = rep["rows"][0]
		self.assertEqual((row["erp_orders"], row["erp_revenue"]), (4, 700.0))
		self.assertEqual(rep["totals"]["erp_orders"], 4)
		self.assertIsNone(row["thumbnail_url"])

	def test_missing_utm_column_zeroes_erp_and_notes_it(self):
		get, _calls = self._graph(self.AD_ROWS)
		rep = m.build_report(get, self.SETTINGS, lambda n, s, e: None, "ad", date(2026, 11, 1), date(2026, 11, 7))
		self.assertEqual(rep["totals"]["erp_orders"], 0)
		self.assertTrue(any("UTM" in n for n in rep["notes"]))


class BlendedTest(unittest.TestCase):
	ROWS = [
		{"channel": "woo", "followup": 0, "orders": 112, "revenue": 56150},
		{"channel": "woo", "followup": 1, "orders": 18, "revenue": 8587},
		{"channel": "pos", "followup": 0, "orders": 80, "revenue": 41206},
		{"channel": "pos", "followup": 1, "orders": 19, "revenue": 12131},
		{"channel": "b2b", "followup": 1, "orders": 5, "revenue": 9000},
		{"channel": "b2b", "followup": 0, "orders": 20, "revenue": 20393},
		{"channel": "staff", "followup": 0, "orders": 4, "revenue": 1106},
		{"channel": "samples", "followup": 0, "orders": 1, "revenue": 0},
		{"channel": "talabat", "followup": 1, "orders": 10, "revenue": 3080},
	]

	def test_groups_and_followup_precedence(self):
		b = m.summarise_blended(self.ROWS, 23456.78)
		self.assertEqual([g["group"] for g in b["included"]], ["website", "pos"])
		self.assertEqual(
			[g["group"] for g in b["excluded"]],
			["whatsapp_followup", "b2b", "staff", "samples", "talabat", "partner"],
		)
		inc = {g["group"]: (g["orders"], g["revenue"]) for g in b["included"]}
		exc = {g["group"]: (g["orders"], g["revenue"]) for g in b["excluded"]}
		self.assertEqual(inc, {"website": (112, 56150.0), "pos": (80, 41206.0)})
		# Only website/pos move to whatsapp_followup; b2b and talabat keep their own group.
		self.assertEqual(exc["whatsapp_followup"], (37, 20718.0))
		self.assertEqual(exc["b2b"], (25, 29393.0))
		self.assertEqual(exc["talabat"], (10, 3080.0))
		self.assertEqual(exc["partner"], (0, 0.0))
		self.assertEqual((b["orders"], b["revenue"], b["spend"]), (192, 97356.0, 23456.78))
		self.assertEqual(b["roas"], 4.15)
		self.assertEqual(b["avg_order_value"], 507.06)

	def test_empty_is_all_zero_keys(self):
		b = m.summarise_blended([], 0)
		self.assertEqual(
			set(b),
			{"spend", "orders", "revenue", "roas", "avg_order_value", "included", "excluded", "scale", "meta_purchase_value"},
		)
		self.assertEqual((b["spend"], b["orders"], b["revenue"], b["roas"], b["avg_order_value"]), (0, 0, 0, 0, 0))
		self.assertEqual((b["scale"], b["meta_purchase_value"]), (0, 0))
		self.assertEqual(len(b["included"]), 2)
		self.assertEqual(len(b["excluded"]), 6)
		self.assertTrue(all(g["orders"] == 0 and g["revenue"] == 0 for g in b["included"] + b["excluded"]))
		self.assertIsInstance(b["orders"], int)

	def test_zero_denominators(self):
		b = m.summarise_blended([{"channel": "pos", "followup": 0, "orders": 2, "revenue": 300}], 0)
		self.assertEqual((b["roas"], b["avg_order_value"]), (0, 150.0))
		b = m.summarise_blended([{"channel": "b2b", "followup": 0, "orders": 2, "revenue": 300}], 100)
		self.assertEqual((b["orders"], b["roas"], b["avg_order_value"]), (0, 0, 0))

	def test_sql_has_followup_exists_with_30_day_window(self):
		sql = m.blended_sql("'pos'", True, 30)
		self.assertIn("EXISTS", sql)
		self.assertIn("`tabJarz Follow-up Touch`", sql)
		self.assertIn("INTERVAL 30 DAY", sql)
		self.assertIn("t.status = 'Done'", sql)
		self.assertIn("wa_message_id", sql)
		self.assertIn("t.done_at < si.creation", sql)
		self.assertIn("si.docstatus = 1 AND si.is_return = 0", sql)
		self.assertIn("GROUP BY 1, 2", sql)
		self.assertNotIn("EXISTS", m.blended_sql("'pos'", False, 30))

	def test_fetch_blended_uses_window_and_skips_missing_doctype(self):
		seen = []
		db = SimpleNamespace(
			exists=lambda *a: True,
			has_column=lambda *a: True,
			sql=lambda q, params=None, as_dict=False: seen.append((q, params)) or [],
		)
		with mock.patch.object(m.frappe, "db", db, create=True), \
				mock.patch.object(m, "_channel_expr", lambda: "'pos'"), \
				mock.patch.object(m, "_followup_window_days", lambda: 30):
			m.fetch_blended(date(2026, 11, 1), date(2026, 11, 12))
			db.exists = lambda *a: False
			m.fetch_blended(date(2026, 11, 1), date(2026, 11, 12))
		self.assertIn("`tabJarz Follow-up Touch`", seen[0][0])
		self.assertIn("INTERVAL 30 DAY", seen[0][0])
		self.assertEqual(seen[0][1], {"start": date(2026, 11, 1), "end": date(2026, 11, 12)})
		self.assertNotIn("Follow-up Touch", seen[1][0])

	def test_window_constant_falls_back_to_30(self):
		self.assertEqual(m._followup_window_days(), 30)

	def test_report_spend_ignores_campaign_and_adset_filters(self):
		rows = [dict(r) for r in BuildReportTest.AD_ROWS] + [
			{"campaign_id": "c2", "campaign_name": "WA", "adset_id": "s9", "adset_name": "Other",
			 "ad_id": "a9", "ad_name": "Wa", "spend": "250", "impressions": "2500"},
		]
		get, _calls = BuildReportTest()._graph(rows)
		seen = []

		def blended(s, e):
			seen.append((s, e))
			return [{"channel": "pos", "followup": 0, "orders": 4, "revenue": 800}]

		rep = m.build_report(
			get, BuildReportTest.SETTINGS, lambda n, s, e: {}, "ad", date(2026, 11, 1), date(2026, 11, 7),
			campaign_id="c1", adset_id="s1", blended_fetch=blended,
		)
		self.assertEqual(rep["totals"]["spend"], 150)
		self.assertEqual(rep["blended"]["spend"], 400)
		self.assertEqual(rep["blended"]["roas"], 2.0)
		self.assertEqual(rep["blended"]["avg_order_value"], 200.0)
		self.assertEqual(seen, [(date(2026, 11, 1), date(2026, 11, 7))])
		self.assertIn(m.BLENDED_NOTE, rep["notes"])

	def test_report_without_blended_fetch_is_zero_shape(self):
		get, _calls = BuildReportTest()._graph(BuildReportTest.AD_ROWS)
		rep = m.build_report(get, BuildReportTest.SETTINGS, lambda n, s, e: {}, "ad", date(2026, 11, 1), date(2026, 11, 7))
		self.assertEqual(rep["blended"]["spend"], 150)
		self.assertEqual(rep["blended"]["orders"], 0)
		self.assertEqual(len(rep["blended"]["excluded"]), 6)

	def test_failed_blended_lookup_does_not_break_report(self):
		get, _calls = BuildReportTest()._graph(BuildReportTest.AD_ROWS)

		def boom(s, e):
			raise RuntimeError("db down")

		with mock.patch.object(m.frappe, "log_error", create=True), \
				mock.patch.object(m.frappe, "get_traceback", create=True, return_value="tb"):
			rep = m.build_report(
				get, BuildReportTest.SETTINGS, lambda n, s, e: {}, "ad", date(2026, 11, 1), date(2026, 11, 7),
				blended_fetch=boom,
			)
		self.assertEqual(rep["blended"]["orders"], 0)
		self.assertTrue(any("Blended ROAS is 0" in n for n in rep["notes"]))


class BlendedRowsTest(unittest.TestCase):
	"""Per-row Blended ROAS: real retail sales shared out by Meta purchase value."""

	@staticmethod
	def _ad_rows():
		# a1: pv 900 (c1/s1), a2: pv 0 (c1/s1), a9: pv 600 (c2/s9). Account-wide Meta pv = 1500.
		return [dict(r) for r in BuildReportTest.AD_ROWS] + [
			{"campaign_id": "c2", "campaign_name": "WA", "adset_id": "s9", "adset_name": "Other",
			 "ad_id": "a9", "ad_name": "Wa", "spend": "250", "impressions": "2500",
			 "action_values": [{"action_type": "omni_purchase", "value": "600"}]},
		]

	CAMPAIGN_ROWS = [
		{"campaign_id": "c1", "campaign_name": "Camp", "spend": "150", "impressions": "1500",
		 "action_values": [{"action_type": "omni_purchase", "value": "900"}]},
		{"campaign_id": "c2", "campaign_name": "WA", "spend": "250", "impressions": "2500",
		 "action_values": [{"action_type": "omni_purchase", "value": "600"}]},
	]

	@staticmethod
	def _sales(revenue):
		return lambda s, e: [{"channel": "pos", "followup": 0, "orders": 4, "revenue": revenue}]

	def _report(self, level="ad", level_rows=None, ad_rows=None, blended_fetch=None, **filters):
		get, _calls = BuildReportTest()._graph(ad_rows or self._ad_rows(), level_rows)
		return m.build_report(
			get, BuildReportTest.SETTINGS, lambda n, s, e: {}, level, date(2026, 11, 1), date(2026, 11, 7),
			blended_fetch=blended_fetch, **filters,
		)

	def test_derive_applies_scale(self):
		d = m.derive({"spend": 100, "purchase_value": 900}, 0.5)
		self.assertEqual((d["blended_revenue"], d["blended_roas"]), (450.0, 4.5))
		d = m.derive({"spend": 0, "purchase_value": 900}, 0.5)
		self.assertEqual((d["blended_revenue"], d["blended_roas"]), (450.0, 0))
		d = m.derive({"spend": 100, "purchase_value": 900})
		self.assertEqual((d["blended_revenue"], d["blended_roas"]), (0, 0))

	def test_scale_uses_unfiltered_purchase_value_even_when_filtered(self):
		rep = self._report(campaign_id="c1", adset_id="s1", blended_fetch=self._sales(800))
		b = rep["blended"]
		# k = 800 / 1500 (all ads), not 800 / 900 (the filtered ads).
		self.assertEqual(b["meta_purchase_value"], 1500.0)
		self.assertEqual(b["scale"], 0.5333)
		rows = {r["id"]: r for r in rep["rows"]}
		self.assertEqual(set(rows), {"a1", "a2"})
		self.assertEqual((rows["a1"]["blended_revenue"], rows["a1"]["blended_roas"]), (480.0, 4.8))
		self.assertEqual((rows["a2"]["blended_revenue"], rows["a2"]["blended_roas"]), (0, 0))
		self.assertEqual((rep["totals"]["blended_revenue"], rep["totals"]["blended_roas"]), (480.0, 3.2))
		self.assertIn(m.BLENDED_ROW_NOTE, rep["notes"])

	def test_campaign_level_rows_carry_blended(self):
		rep = self._report("campaign", self.CAMPAIGN_ROWS, blended_fetch=self._sales(800))
		rows = {r["id"]: r for r in rep["rows"]}
		self.assertEqual((rows["c1"]["blended_revenue"], rows["c1"]["blended_roas"]), (480.0, 3.2))
		self.assertEqual((rows["c2"]["blended_revenue"], rows["c2"]["blended_roas"]), (320.0, 1.28))

	def test_unfiltered_rows_sum_to_blended_revenue(self):
		for level, level_rows in (("ad", None), ("campaign", self.CAMPAIGN_ROWS)):
			rep = self._report(level, level_rows, blended_fetch=self._sales(97356.37))
			total = sum(r["blended_revenue"] for r in rep["rows"])
			self.assertAlmostEqual(total, rep["blended"]["revenue"], delta=0.02, msg=level)
			self.assertAlmostEqual(rep["totals"]["blended_revenue"], rep["blended"]["revenue"], delta=0.01, msg=level)
			self.assertTrue(all(set(m.METRIC_KEYS) <= set(r) for r in rep["rows"]))

	def test_zero_purchase_value_gives_zero_scale(self):
		rows = [dict(r) for r in BuildReportTest.AD_ROWS]
		rows[0].pop("action_values")
		rep = self._report(ad_rows=rows, blended_fetch=self._sales(800))
		self.assertEqual((rep["blended"]["scale"], rep["blended"]["meta_purchase_value"]), (0, 0))
		self.assertEqual(rep["blended"]["revenue"], 800.0)
		self.assertTrue(all(r["blended_revenue"] == 0 and r["blended_roas"] == 0 for r in rep["rows"]))
		self.assertEqual(rep["totals"]["blended_revenue"], 0)

	def test_failed_lookup_gives_zero_scale(self):
		def boom(s, e):
			raise RuntimeError("db down")

		with mock.patch.object(m.frappe, "log_error", create=True), \
				mock.patch.object(m.frappe, "get_traceback", create=True, return_value="tb"):
			rep = self._report(blended_fetch=boom)
		self.assertEqual(rep["blended"]["scale"], 0)
		self.assertEqual(rep["blended"]["meta_purchase_value"], 1500.0)
		self.assertTrue(all(r["blended_revenue"] == 0 and r["blended_roas"] == 0 for r in rep["rows"]))
		self.assertNotIn(m.BLENDED_ROW_NOTE, rep["notes"])

	def test_empty_response_has_blended_keys(self):
		rep = m._empty_response(False, "ad", date(2026, 11, 1), date(2026, 11, 7), [])
		self.assertEqual((rep["totals"]["blended_revenue"], rep["totals"]["blended_roas"]), (0, 0))
		self.assertEqual((rep["blended"]["scale"], rep["blended"]["meta_purchase_value"]), (0, 0))


class FakeClock:
	def __init__(self):
		self.now = 1000.0

	def __call__(self):
		return self.now


class DeadlineTest(unittest.TestCase):
	def test_timeout_is_capped_to_remaining_budget(self):
		clock, seen = FakeClock(), []

		def get(url, params=None, headers=None, timeout=None):
			seen.append(timeout)
			clock.now += 50
			return FakeResp({"data": []})

		limited = m.with_deadline(get, budget=60, clock=clock)
		limited("u", params={}, headers={}, timeout=30)
		limited("u", params={}, headers={}, timeout=30)
		self.assertEqual(seen, [30, 10])

	def test_exhausted_budget_raises_without_calling_graph(self):
		clock, calls = FakeClock(), []

		def get(url, params=None, headers=None, timeout=None):
			calls.append(1)
			clock.now += 61
			return FakeResp({"data": []})

		limited = m.with_deadline(get, budget=60, clock=clock)
		limited("u", params={}, headers={})
		with self.assertRaises(m.MetaError) as ctx:
			limited("u", params={}, headers={})
		self.assertEqual(str(ctx.exception), "Meta is responding slowly; try again in a minute.")
		self.assertEqual(len(calls), 1)

	def test_timeout_after_deadline_reports_slow(self):
		clock = FakeClock()

		def get(url, params=None, headers=None, timeout=None):
			clock.now += 100
			raise m.requests.Timeout()

		with self.assertRaises(m.MetaError) as ctx:
			m._graph_get(m.with_deadline(get, 60, clock), "u", {}, "T")
		self.assertIn("responding slowly", str(ctx.exception))

	def test_build_report_stops_when_budget_spent(self):
		clock = FakeClock()
		inner, _calls = BuildReportTest()._graph(BuildReportTest.AD_ROWS)

		def get(url, params=None, headers=None, timeout=None):
			clock.now += 40
			return inner(url, params=params, headers=headers, timeout=timeout)

		with self.assertRaises(m.MetaError) as ctx:
			m.build_report(
				get, BuildReportTest.SETTINGS, lambda n, s, e: {}, "ad", date(2026, 11, 1), date(2026, 11, 7),
				clock=clock,
			)
		self.assertIn("responding slowly", str(ctx.exception))

	def test_budget_is_per_build_not_global(self):
		clock = FakeClock()
		inner, _calls = BuildReportTest()._graph(BuildReportTest.AD_ROWS)
		for _ in range(3):
			clock.now += 1000
			m.build_report(
				inner, BuildReportTest.SETTINGS, lambda n, s, e: {}, "ad", date(2026, 11, 1), date(2026, 11, 7),
				clock=clock,
			)


class GraphBodyTest(unittest.TestCase):
	def test_non_dict_2xx_body_is_rejected(self):
		for body in ([1, 2], "x", None, 5):
			with self.assertRaises(m.MetaError) as ctx:
				m._graph_get(lambda *a, **k: FakeResp(body), "u", {}, "T")
			self.assertEqual(str(ctx.exception), "Unexpected reply from Meta.")


class SettingsTest(unittest.TestCase):
	def _read(self, account, version, token="tok"):
		values = {"ads_account_id": account, "ads_api_version": version}
		doc = SimpleNamespace(get=lambda k: values.get(k), get_password=lambda *a, **k: token)
		with mock.patch.object(m.frappe, "get_doc", create=True, return_value=doc):
			return m._read_settings()

	def test_valid_values_kept_and_bare_number_prefixed(self):
		self.assertEqual(self._read("act_42", "v20.1")["account"], "act_42")
		s = self._read(" 12345 ", " v19.0 ")
		self.assertEqual((s["account"], s["version"]), ("act_12345", "v19.0"))

	def test_garbage_falls_back_to_defaults(self):
		for bad in ("act_", "act_12x", "../x", "abc", "act_1/insights", "12 34"):
			self.assertEqual(self._read(bad, "x")["account"], m.DEFAULT_ACCOUNT, bad)
		for bad in ("23.0", "v23", "v23.0/../x", "latest", "v1.2.3"):
			self.assertEqual(self._read("", bad)["version"], m.DEFAULT_VERSION, bad)

	def test_empty_uses_defaults_and_token_stripped(self):
		s = self._read(None, None, token="  tok  ")
		self.assertEqual((s["account"], s["version"], s["token"]), (m.DEFAULT_ACCOUNT, m.DEFAULT_VERSION, "tok"))


class EndpointTest(unittest.TestCase):
	def setUp(self):
		self.cache = SimpleNamespace(store={}, sets=[])
		self.cache.get_value = lambda key: self.cache.store.get(key)
		self.cache.set_value = lambda key, val, **k: self.cache.sets.append(key)
		self.roles = []
		self.token = "tok"
		self.build_calls = []

		def throw(msg, exc=None, *a, **k):
			raise (exc or Exception)(msg)

		def fake_build(*a, **k):
			self.build_calls.append(a)
			return {"configured": True, "rows": ["fresh"]}

		patches = [
			mock.patch.object(m.frappe, "get_roles", create=True, side_effect=lambda user=None: self.roles),
			mock.patch.object(m.frappe, "session", SimpleNamespace(user="u@x"), create=True),
			mock.patch.object(m.frappe, "throw", throw, create=True),
			mock.patch.object(m.frappe, "PermissionError", PermissionError, create=True),
			mock.patch.object(m.frappe, "cache", lambda: self.cache, create=True),
			mock.patch.object(m, "_read_settings", lambda: {"token": self.token, "account": "act_1", "version": "v23.0"}),
			mock.patch.object(m, "build_report", fake_build),
		]
		for p in patches:
			p.start()
			self.addCleanup(p.stop)

	def test_no_role_is_permission_error(self):
		self.roles = []
		with self.assertRaises(PermissionError):
			m.get_ads_report()
		self.assertEqual(self.build_calls, [])

	def test_meta_capi_manager_passes_gate(self):
		self.roles = ["Meta CAPI Manager"]
		self.assertEqual(m.get_ads_report()["rows"], ["fresh"])

	def test_empty_token_is_unconfigured_without_graph_call(self):
		self.roles, self.token = ["Meta CAPI Manager"], ""
		rep = m.get_ads_report()
		self.assertFalse(rep["configured"])
		self.assertEqual(rep["rows"], [])
		self.assertEqual(self.build_calls, [])
		self.assertEqual(rep["blended"], m.summarise_blended([], 0))
		self.assertEqual(rep["blended"]["roas"], 0)
		self.assertEqual([g["group"] for g in rep["blended"]["included"]], ["website", "pos"])
		self.assertEqual(len(rep["blended"]["excluded"]), 6)

	def test_cached_value_skips_graph_unless_refresh(self):
		self.roles = ["Meta CAPI Manager"]
		start, end = m.resolve_dates(None, None, m._cairo_now().date())
		self.cache.store[m._cache_key("ad", start, end, None, None)] = {"configured": True, "rows": ["cached"]}
		self.assertEqual(m.get_ads_report()["rows"], ["cached"])
		self.assertEqual(self.build_calls, [])
		self.assertEqual(m.get_ads_report(refresh="1")["rows"], ["fresh"])
		self.assertEqual(len(self.build_calls), 1)


if __name__ == "__main__":
	unittest.main()
