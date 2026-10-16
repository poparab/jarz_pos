"""Follow-up cycle rules (services/customer_followups.py) and the shared RFM
retail scope (services/rfm_segmentation.py) -- pure functions, no site needed.

Plain ``unittest.TestCase`` (no FrappeTestCase, see test_task_board.py); a
stub ``frappe`` is registered when frappe is not installed.
"""

import datetime
import sys
import types
import unittest
from types import SimpleNamespace

try:  # pragma: no cover - depends on the environment
	import frappe  # noqa: F401
except ImportError:  # pragma: no cover
	_fake = types.ModuleType("frappe")
	_fake.whitelist = lambda *a, **k: (a[0] if a and callable(a[0]) else (lambda fn: fn))
	_fake._ = lambda m: m
	_fake.local = SimpleNamespace()
	_fake.session = SimpleNamespace(user="Guest")
	_fake.db = SimpleNamespace()
	_fake.log_error = lambda *a, **k: None
	_fake.get_traceback = lambda *a, **k: ""
	sys.modules["frappe"] = _fake
	_utils = types.ModuleType("frappe.utils")
	_utils.today = lambda: "2026-10-12"
	_utils.add_days = lambda d, n: d
	_utils.getdate = lambda d=None: d
	_utils.get_datetime = lambda d=None: d
	_utils.now_datetime = lambda: datetime.datetime(2026, 10, 12, 12, 0)
	_utils.nowdate = lambda: "2026-10-12"
	sys.modules["frappe.utils"] = _utils
	_fake.utils = _utils

from jarz_pos.services import customer_followups as f  # noqa: E402
from jarz_pos.services import rfm_segmentation as rfm  # noqa: E402

D = datetime.datetime
T = datetime.time
SETTINGS = {
	"high_value_order_min": 1000,
	"quiet_start": T(22, 0),
	"quiet_end": T(11, 0),
	"call_window_start": T(12, 0),
	"call_window_end": T(16, 0),
}


def plan(delivered, value=500, repeat=False, settings=None):
	return f.plan_touches(delivered, value, repeat, settings or SETTINGS)


def keys(rows):
	return [(r["step"], r["channel"]) for r in rows]


def due(rows, step, channel):
	return [r for r in rows if (r["step"], r["channel"]) == (step, channel)][0]["due_at"]


class PlanTouchesTest(unittest.TestCase):
	def test_new_low_value_order(self):
		rows = plan(D(2026, 10, 15, 14, 0))
		self.assertEqual(
			sorted(keys(rows)),
			sorted(
				[
					("3h", "Message"),
					("day2", "Call"),
					("day7", "Message"),
					("day32", "Message"),
					("day50", "Message"),
				]
			),
		)

	def test_new_high_value_adds_3h_call(self):
		rows = plan(D(2026, 10, 15, 14, 0), value=1200)
		self.assertIn(("3h", "Call"), keys(rows))
		self.assertNotIn(("day30", "Call"), keys(rows))

	def test_repeat_low_value_adds_day30_call(self):
		rows = plan(D(2026, 10, 15, 14, 0), repeat=True)
		self.assertIn(("day30", "Call"), keys(rows))
		self.assertNotIn(("day45", "Call"), keys(rows))

	def test_repeat_high_value_gets_everything(self):
		rows = plan(D(2026, 10, 15, 14, 0), value=1000, repeat=True)
		self.assertEqual(len(rows), 8)
		self.assertIn(("day45", "Call"), keys(rows))

	def test_high_value_boundary(self):
		self.assertNotIn(("3h", "Call"), keys(plan(D(2026, 10, 15, 14, 0), value=999.99)))
		self.assertIn(("3h", "Call"), keys(plan(D(2026, 10, 15, 14, 0), value=1000)))

	def test_three_hours_inside_awake_hours_is_exact(self):
		self.assertEqual(due(plan(D(2026, 10, 15, 13, 0)), "3h", "Message"), D(2026, 10, 15, 16, 0))

	def test_late_delivery_message_moves_to_next_morning(self):
		# +3h = 00:30 next day, inside quiet hours
		self.assertEqual(due(plan(D(2026, 10, 15, 21, 30)), "3h", "Message"), D(2026, 10, 16, 11, 0))

	def test_message_due_just_after_quiet_start_same_evening(self):
		# +3h = 22:30
		self.assertEqual(due(plan(D(2026, 10, 15, 19, 30)), "3h", "Message"), D(2026, 10, 16, 11, 0))

	def test_early_morning_message_waits_until_eleven_same_day(self):
		# +3h = 06:00
		self.assertEqual(due(plan(D(2026, 10, 15, 3, 0)), "3h", "Message"), D(2026, 10, 15, 11, 0))

	def test_call_window_shift(self):
		self.assertEqual(due(plan(D(2026, 10, 15, 9, 0), value=2000), "3h", "Call"), D(2026, 10, 15, 12, 0))
		self.assertEqual(due(plan(D(2026, 10, 15, 14, 30), value=2000), "3h", "Call"), D(2026, 10, 16, 12, 0))
		self.assertEqual(due(plan(D(2026, 10, 15, 6, 0), value=2000), "3h", "Call"), D(2026, 10, 15, 12, 0))

	def test_day_offsets_use_calendar_days(self):
		rows = plan(D(2026, 10, 15, 14, 0), value=2000, repeat=True)
		self.assertEqual(due(rows, "day2", "Call"), D(2026, 10, 17, 12, 0))
		self.assertEqual(due(rows, "day7", "Message"), D(2026, 10, 22, 11, 0))
		self.assertEqual(due(rows, "day30", "Call"), D(2026, 11, 14, 12, 0))
		self.assertEqual(due(rows, "day50", "Message"), D(2026, 12, 4, 11, 0))

	def test_skip_links_and_order(self):
		rows = plan(D(2026, 10, 15, 14, 0), value=2000, repeat=True)
		link = {(r["step"], r["channel"]): r["skip_if_reached"] for r in rows}
		self.assertEqual(link[("day2", "Call")], "3h")
		self.assertEqual(link[("day32", "Message")], "day30")
		self.assertEqual(link[("day50", "Message")], "day45")
		self.assertIsNone(link[("3h", "Message")])
		dues = [r["due_at"] for r in rows]
		self.assertEqual(dues, sorted(dues))

	def test_settings_accept_timedelta_and_strings(self):
		s = dict(SETTINGS, quiet_start=datetime.timedelta(hours=23), quiet_end="10:00:00")
		# +3h = 00:30, inside 23:00-10:00
		self.assertEqual(
			due(plan(D(2026, 10, 15, 21, 30), settings=s), "3h", "Message"), D(2026, 10, 16, 10, 0)
		)

	def test_string_delivered_at(self):
		self.assertEqual(due(plan("2026-10-15 14:00:00"), "3h", "Message"), D(2026, 10, 15, 17, 0))


class LogEffectsTest(unittest.TestCase):
	def test_call_needs_reached(self):
		with self.assertRaises(ValueError):
			f.decide_log_effects("Call", "3h", None, "Happy")

	def test_message_cannot_have_reached(self):
		with self.assertRaises(ValueError):
			f.decide_log_effects("Message", "3h", "Reached", None)

	def test_not_reached_only_no_reply(self):
		with self.assertRaises(ValueError):
			f.decide_log_effects("Call", "3h", "Not Reached", "Happy")
		self.assertIsNone(f.decide_log_effects("Call", "3h", "Not Reached", "No Reply")["skip_followers_of"])

	def test_reached_skips_followers(self):
		self.assertEqual(f.decide_log_effects("Call", "3h", "Reached", "Happy")["skip_followers_of"], "3h")

	def test_quality_cancels_cycle_and_creates_task(self):
		e = f.decide_log_effects("Call", "day2", "Reached", "Quality")
		self.assertTrue(e["create_task"])
		self.assertEqual(e["cancel_all_reason"], f.REASON_QUALITY)

	def test_delivery_code_only_when_percent_configured(self):
		self.assertTrue(f.decide_log_effects("Call", "3h", "Reached", "Delivery", 15)["create_apology_code"])
		self.assertFalse(f.decide_log_effects("Call", "3h", "Reached", "Delivery", 0)["create_apology_code"])

	def test_opt_out(self):
		e = f.decide_log_effects("Call", "3h", "Reached", "Opted Out")
		self.assertTrue(e["opt_out"])
		self.assertEqual(e["cancel_all_reason"], f.REASON_OPT_OUT)

	def test_unknown_outcome(self):
		with self.assertRaises(ValueError):
			f.decide_log_effects("Message", "3h", None, "Banana")

	def test_message_outcome_without_reached_is_fine(self):
		e = f.decide_log_effects("Message", "day7", None, "Happy")
		self.assertIsNone(e["skip_followers_of"])


class TemplateTest(unittest.TestCase):
	def test_keys(self):
		self.assertEqual(f.template_key("3h", "Message"), "tpl_msg_3h")
		self.assertEqual(f.template_key("day30", "Call"), "tpl_call_day30")

	def test_render(self):
		out = f.render_template("hi {first_name} order {order_no} {offer}", "Mona", 17900, "")
		self.assertEqual(out, "hi Mona order 17900")

	def test_render_leaves_stray_braces(self):
		self.assertEqual(f.render_template("a {x} b", "n", 1, "o"), "a {x} b")

	def test_first_name(self):
		self.assertEqual(f.first_name_of("Mona Ahmed"), "Mona")
		self.assertEqual(f.first_name_of(None), "")


class RetailScopeTest(unittest.TestCase):
	def test_python_twin(self):
		self.assertTrue(rfm.is_retail_invoice("", "Individual", 55))
		self.assertTrue(rfm.is_retail_invoice(None, None, 55))
		self.assertTrue(rfm.is_retail_invoice("Standard", "Individual", 55))
		self.assertFalse(rfm.is_retail_invoice("B2B Supply", "Individual", 55))
		self.assertFalse(rfm.is_retail_invoice("Employee", "Individual", 55))
		self.assertFalse(rfm.is_retail_invoice("Sample - Courier", "Individual", 55))
		self.assertFalse(rfm.is_retail_invoice("Standard", "B2B", 55))
		self.assertFalse(rfm.is_retail_invoice("Standard", "Distributor", 55))
		self.assertFalse(rfm.is_retail_invoice("Standard", "Individual", 0))
		self.assertTrue(rfm.is_retail_invoice("Standard", "Individual", 0, website_only=False))

	def test_sql_is_guarded_by_has_column(self):
		none = rfm.retail_scope_sql(True, has_column=lambda d, c: False)
		self.assertNotIn("custom_order_purpose", none)
		self.assertNotIn("woo_order_id", none)
		self.assertIn("is_return = 0", none)
		full = rfm.retail_scope_sql(True, has_column=lambda d, c: True)
		self.assertIn("custom_order_purpose", full)
		self.assertIn("woo_order_id", full)
		self.assertNotIn("woo_order_id", rfm.retail_scope_sql(False, has_column=lambda d, c: True))

	def test_sql_has_no_percent_and_honours_alias(self):
		sql = rfm.retail_scope_sql(True, "n", "cu", has_column=lambda d, c: True)
		self.assertNotIn("%", sql)
		self.assertIn("n.docstatus = 1", sql)
		self.assertIn("cu.customer_group", sql)

	def test_flags(self):
		self.assertEqual(
			rfm.compute_flags(4, 500, 4, 1000), {"rfm_is_regular": 1, "rfm_has_high_value_order": 0}
		)
		self.assertEqual(
			rfm.compute_flags(3, 1000, 4, 1000), {"rfm_is_regular": 0, "rfm_has_high_value_order": 1}
		)
		self.assertEqual(rfm.compute_flags(0, 0, 0, 0), {"rfm_is_regular": 0, "rfm_has_high_value_order": 0})


if __name__ == "__main__":
	unittest.main()
