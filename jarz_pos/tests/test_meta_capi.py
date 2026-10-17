"""Meta CAPI chat orders (services/meta_capi.py) -- pure functions, no site needed.

Plain ``unittest.TestCase`` (no FrappeTestCase, see test_task_board.py); a
stub ``frappe`` is registered when frappe is not installed. The Graph API call
is exercised with ``requests.post`` patched, so nothing leaves the machine.
"""

import datetime
import hashlib
import json
import os
import sys
import types
import unittest
from types import SimpleNamespace
from unittest import mock

try:  # pragma: no cover - depends on the environment
	import frappe
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

import jarz_pos
from jarz_pos.services import meta_capi as m

APP_DIR = os.path.dirname(os.path.abspath(jarz_pos.__file__))
D = datetime.datetime


def sha(value):
	return hashlib.sha256(value.encode("utf-8")).hexdigest()


class EventIdTest(unittest.TestCase):
	def test_amendment_suffix_is_dropped(self):
		self.assertEqual(
			m.event_id_for("ACC-SINV-2026-18128-1", "ACC-SINV-2026-18128"), "ACC-SINV-2026-18128"
		)
		self.assertEqual(
			m.event_id_for("ACC-SINV-2026-18128-12", "ACC-SINV-2026-18128-11"), "ACC-SINV-2026-18128"
		)

	def test_original_invoice_keeps_its_serial(self):
		self.assertEqual(m.event_id_for("ACC-SINV-2026-18128"), "ACC-SINV-2026-18128")
		self.assertEqual(m.event_id_for("ACC-SINV-2026-18128", None), "ACC-SINV-2026-18128")
		self.assertEqual(m.event_id_for("ACC-SINV-2026-18128", ""), "ACC-SINV-2026-18128")

	def test_original_and_amendment_share_the_id(self):
		self.assertEqual(
			m.event_id_for("ACC-SINV-2026-00007"),
			m.event_id_for("ACC-SINV-2026-00007-2", "ACC-SINV-2026-00007-1"),
		)

	def test_different_orders_never_share_an_id(self):
		self.assertNotEqual(m.event_id_for("ACC-SINV-2026-00007"), m.event_id_for("ACC-SINV-2026-00008"))


class PhoneTest(unittest.TestCase):
	def test_accepted_formats(self):
		for raw in (
			"01001234567",
			"1001234567",
			"201001234567",
			"+201001234567",
			"00201001234567",
			"+20 100 123 4567",
			"010-0123-4567",
		):
			with self.subTest(raw=raw):
				self.assertEqual(m.normalize_eg_mobile(raw), "201001234567")

	def test_every_mobile_network(self):
		for prefix in ("010", "011", "012", "015"):
			self.assertEqual(m.normalize_eg_mobile(prefix + "12345678"), "20" + prefix[1:] + "12345678")

	def test_rejected(self):
		for raw in (
			None,
			"",
			"0221234567",
			"01312345678",
			"0100123456",
			"010012345678",
			"abc",
			"+44 7700 900123",
		):
			with self.subTest(raw=raw):
				self.assertIsNone(m.normalize_eg_mobile(raw))


class NameTest(unittest.TestCase):
	def test_first_and_last(self):
		self.assertEqual(m.split_name("  Sara   Ahmed Hassan "), ("sara", "hassan"))

	def test_single_word(self):
		self.assertEqual(m.split_name("Sara"), ("sara", None))

	def test_arabic_is_kept(self):
		self.assertEqual(m.split_name("سارة أحمد"), ("سارة", "أحمد"))

	def test_punctuation_is_stripped(self):
		self.assertEqual(m.split_name("Dr. O'Neil"), ("dr", "oneil"))

	def test_parts_with_digits_are_dropped(self):
		self.assertEqual(m.split_name("01001234567"), (None, None))
		self.assertEqual(m.split_name("Sara 2nd order"), ("sara", "order"))

	def test_empty(self):
		self.assertEqual(m.split_name(None), (None, None))
		self.assertEqual(m.split_name("  "), (None, None))


class UserDataTest(unittest.TestCase):
	def test_keys_are_hashed(self):
		data = m.build_user_data("201001234567", "CUST-00042", "Sara Ahmed")
		self.assertEqual(data["ph"], [sha("201001234567")])
		self.assertEqual(data["country"], [sha("eg")])
		self.assertEqual(data["external_id"], [sha("cust-00042")])
		self.assertEqual(data["fn"], [sha("sara")])
		self.assertEqual(data["ln"], [sha("ahmed")])

	def test_no_plain_text_and_no_email(self):
		data = m.build_user_data("201001234567", "CUST-00042", "Sara Ahmed")
		self.assertNotIn("em", data)
		blob = json.dumps(data)
		for plain in ("201001234567", "CUST-00042", "Sara", "sara"):
			self.assertNotIn(plain, blob)
		for values in data.values():
			for value in values:
				self.assertRegex(value, r"^[0-9a-f]{64}$")

	def test_missing_name_parts_are_left_out(self):
		data = m.build_user_data("201001234567", "CUST-1", "Sara")
		self.assertIn("fn", data)
		self.assertNotIn("ln", data)
		self.assertNotIn("fn", m.build_user_data("201001234567", "CUST-1", ""))


class EventTest(unittest.TestCase):
	def test_purchase_payload(self):
		event = m.build_event(
			event_id="ACC-SINV-2026-1", event_time=1760000000, value=480, user_data={"ph": ["x"]}
		)
		self.assertEqual(event["event_name"], "Purchase")
		self.assertEqual(event["action_source"], "chat")
		self.assertEqual(event["event_id"], "ACC-SINV-2026-1")
		self.assertEqual(event["event_time"], 1760000000)
		self.assertEqual(
			event["custom_data"], {"currency": "EGP", "value": 480.0, "order_id": "ACC-SINV-2026-1"}
		)
		self.assertNotIn("event_source_url", event)

	def test_value_is_rounded(self):
		event = m.build_event(event_id="X", event_time=1, value=159.996, user_data={})
		self.assertEqual(event["custom_data"]["value"], 160.0)

	def test_aware_time(self):
		aware = D(2026, 10, 19, 9, 0, tzinfo=datetime.timezone.utc)
		self.assertEqual(m.to_unix(aware, "Africa/Cairo"), int(aware.timestamp()))

	def test_naive_time_is_site_time(self):
		try:
			from zoneinfo import ZoneInfo

			ZoneInfo("Africa/Cairo")
		except Exception:  # pragma: no cover - no tz database on this machine
			self.skipTest("no tz database")
		naive = D(2026, 1, 15, 14, 0)  # Cairo is UTC+2 in January
		expected = int(D(2026, 1, 15, 12, 0, tzinfo=datetime.timezone.utc).timestamp())
		self.assertEqual(m.to_unix(naive, "Africa/Cairo"), expected)


class InitialStatusTest(unittest.TestCase):
	def test_every_order_waits(self):
		self.assertEqual(
			m.initial_status(480, "201001234567", m.APPROVAL_EVERY_ORDER, 2500), (m.STATUS_PENDING, "")
		)

	def test_held_only_approves_small_orders(self):
		self.assertEqual(
			m.initial_status(480, "201001234567", m.APPROVAL_HELD_ONLY, 2500), (m.STATUS_APPROVED, "")
		)

	def test_large_orders_are_held_in_both_modes(self):
		for mode in (m.APPROVAL_EVERY_ORDER, m.APPROVAL_HELD_ONLY):
			status, reason = m.initial_status(2600, "201001234567", mode, 2500)
			self.assertEqual(status, m.STATUS_HELD)
			self.assertEqual(reason, "Above 2,500 EGP")

	def test_exactly_the_limit_is_not_held(self):
		self.assertEqual(
			m.initial_status(2500, "201001234567", m.APPROVAL_HELD_ONLY, 2500)[0], m.STATUS_APPROVED
		)

	def test_no_phone_is_skipped(self):
		self.assertEqual(m.initial_status(480, None, m.APPROVAL_HELD_ONLY, 2500)[0], m.STATUS_SKIPPED)

	def test_zero_value_is_skipped(self):
		self.assertEqual(m.initial_status(0, "201001234567", m.APPROVAL_HELD_ONLY, 2500)[0], m.STATUS_SKIPPED)

	def test_zero_hold_disables_holding(self):
		self.assertEqual(
			m.initial_status(99999, "201001234567", m.APPROVAL_HELD_ONLY, 0)[0], m.STATUS_APPROVED
		)


class ExpiryTest(unittest.TestCase):
	def test_window(self):
		now = D(2026, 10, 20, 12, 0)
		self.assertFalse(m.is_expired(D(2026, 10, 14, 12, 0), now))
		self.assertFalse(m.is_expired(now - datetime.timedelta(days=6, hours=23), now))
		self.assertTrue(m.is_expired(now - datetime.timedelta(days=6, hours=23, minutes=45), now))
		self.assertTrue(m.is_expired(D(2026, 10, 12, 12, 0), now))


def _inv(**over):
	inv = {
		"customer": "CUST-1",
		"customer_name": "Sara Ali",
		"contact_mobile": "01001234567",
		"docstatus": 1,
		"is_return": 0,
		"custom_sales_invoice_state": "Delivered",
	}
	inv.update(over)
	return SimpleNamespace(**inv)


class SendTimeEligibilityTest(unittest.TestCase):
	"""A row may wait days for approval; the order is rechecked when it is sent."""

	ROW = SimpleNamespace(
		invoice="ACC-SINV-1", event_id="ACC-SINV-1", event_time=D(2026, 10, 19, 12, 0), order_value=480
	)

	def _event(self, inv, has_return=False, customer=None, blocked=()):
		customer = customer or {"mobile_no": "", m.CUSTOMER_EXCLUDE_FIELD: 0}
		db = SimpleNamespace(
			get_value=mock.Mock(side_effect=[inv, customer]),
			exists=mock.Mock(return_value="ACC-SINV-RET-1" if has_return else None),
		)
		with mock.patch.object(m.frappe, "db", db, create=True):
			return m._event_for_row(self.ROW, "Africa/Cairo", blocked)

	def test_delivered_order_is_sent(self):
		event, reason = self._event(_inv())
		self.assertEqual(reason, "")
		self.assertEqual(event["event_id"], "ACC-SINV-1")

	def test_returned_state_is_skipped(self):
		event, reason = self._event(_inv(custom_sales_invoice_state="Returned"))
		self.assertIsNone(event)
		self.assertEqual(reason, "Order no longer delivered: Returned")

	def test_credit_note_against_the_order_is_skipped(self):
		event, reason = self._event(_inv(), has_return=True)
		self.assertIsNone(event)
		self.assertEqual(reason, "A return was booked against this order")

	def test_cancelled_return_and_missing_invoices_are_skipped(self):
		self.assertEqual(m.not_sendable_reason(_inv(docstatus=2), False), "Invoice is cancelled")
		self.assertEqual(m.not_sendable_reason(_inv(docstatus=0), False), "Invoice is not submitted")
		self.assertEqual(m.not_sendable_reason(_inv(is_return=1), False), "Invoice is a return")
		self.assertEqual(m.not_sendable_reason(None, False), "Invoice no longer exists")
		self.assertIsNone(m.not_sendable_reason(_inv(), False))

	def test_customer_excluded_while_waiting_is_skipped(self):
		event, reason = self._event(_inv(), customer={"mobile_no": "", m.CUSTOMER_EXCLUDE_FIELD: 1})
		self.assertIsNone(event)
		self.assertEqual(reason, "Customer excluded from Meta")

	def test_phone_added_to_do_not_send_while_waiting_is_skipped(self):
		event, reason = self._event(_inv(), blocked=m.parse_phones("+20 100 123 4567"))
		self.assertIsNone(event)
		self.assertEqual(reason, "Phone on the do-not-send list")

	def test_blocked_customer_mobile_wins_over_a_different_contact_number(self):
		customer = {"mobile_no": "01112223333", m.CUSTOMER_EXCLUDE_FIELD: 0}
		event, reason = self._event(_inv(), customer=customer, blocked=m.parse_phones("01112223333"))
		self.assertIsNone(event)
		self.assertEqual(reason, "Phone on the do-not-send list")

	def test_customer_mobile_is_the_fallback_phone(self):
		event, reason = self._event(_inv(contact_mobile=""), customer={"mobile_no": "01001234567"})
		self.assertEqual(reason, "")
		self.assertEqual(event["user_data"]["ph"], [sha(m.normalize_eg_mobile("01001234567"))])

	def test_no_phone_is_still_skipped(self):
		db = SimpleNamespace(
			get_value=mock.Mock(side_effect=[_inv(contact_mobile=""), {"mobile_no": ""}]),
			exists=mock.Mock(return_value=None),
		)
		with mock.patch.object(m.frappe, "db", db, create=True):
			event, reason = m._event_for_row(self.ROW, "Africa/Cairo")
		self.assertIsNone(event)
		self.assertEqual(reason, "No valid Egyptian mobile at send time")


class KillSwitchTest(unittest.TestCase):
	def test_backfill_queues_nothing_while_off(self):
		with (
			mock.patch.object(m, "load_settings", return_value={"enabled": 0}),
			mock.patch.object(m, "_candidate_orders") as candidates,
		):
			result = m.queue_delivered_orders(backfill_days=7)
		candidates.assert_not_called()
		self.assertEqual(result["queued"], 0)

	def test_queue_button_says_it_is_off(self):
		with (
			mock.patch.object(m, "_require_manager"),
			mock.patch.object(m, "load_settings", return_value={"enabled": 0}),
			mock.patch.object(m, "queue_delivered_orders") as queue,
		):
			result = m.run_queue_now(backfill_days=7)
		queue.assert_not_called()
		self.assertFalse(result["ok"])


class SettingsHelpersTest(unittest.TestCase):
	def test_groups_from_lines_or_commas(self):
		self.assertEqual(m.parse_groups("B2B\nEmployee, Sample\n\nB2B"), ("B2B", "Employee", "Sample"))

	def test_blank_groups_fall_back_to_defaults(self):
		self.assertEqual(m.parse_groups(""), m.DEFAULT_EXCLUDED_GROUPS)
		self.assertIn("B2B", m.DEFAULT_EXCLUDED_GROUPS)

	def test_chunks(self):
		self.assertEqual(list(m.chunks([1, 2, 3, 4, 5], 2)), [[1, 2], [3, 4], [5]])

	def test_get_reads_dicts_and_falls_back(self):
		self.assertEqual(m._get({"a": 1}, "a"), 1)
		self.assertEqual(m._get({"a": ""}, "a", 7), 7)
		self.assertEqual(m._get({}, "a", 7), 7)


def _settings(test_mode=0):
	return {
		"api_version": "v23.0",
		"dataset_id": "1296894931563065",
		"test_mode": test_mode,
		"test_event_code": "TEST123",
	}


def _response(status, body):
	return SimpleNamespace(status_code=status, json=lambda: body)


class PostEventsTest(unittest.TestCase):
	TOKEN = "EAAtoken-secret"

	def test_success(self):
		with mock.patch(
			"requests.post", return_value=_response(200, {"events_received": 2, "fbtrace_id": "abc"})
		) as post:
			ok, body = m._post_events(_settings(), self.TOKEN, [{"a": 1}, {"b": 2}])
		self.assertTrue(ok)
		self.assertEqual(body["fbtrace_id"], "abc")
		url = post.call_args[0][0]
		form = post.call_args[1]["data"]
		self.assertEqual(url, "https://graph.facebook.com/v23.0/1296894931563065/events")
		self.assertNotIn(self.TOKEN, url)
		self.assertEqual(json.loads(form["data"]), [{"a": 1}, {"b": 2}])
		self.assertNotIn("test_event_code", form)

	def test_test_mode_adds_the_code(self):
		with mock.patch("requests.post", return_value=_response(200, {"events_received": 1})) as post:
			m._post_events(_settings(test_mode=1), self.TOKEN, [{"a": 1}])
		self.assertEqual(post.call_args[1]["data"]["test_event_code"], "TEST123")

	def test_partial_receipt_is_a_failure(self):
		with mock.patch("requests.post", return_value=_response(200, {"events_received": 1})):
			ok, _body = m._post_events(_settings(), self.TOKEN, [{"a": 1}, {"b": 2}])
		self.assertFalse(ok)

	def test_error_message_never_holds_the_token(self):
		body = {
			"error": {"message": "Invalid parameter", "error_user_msg": "event_time too old", "code": 100}
		}
		with mock.patch("requests.post", return_value=_response(400, body)):
			ok, text = m._post_events(_settings(), self.TOKEN, [{"a": 1}])
		self.assertFalse(ok)
		self.assertEqual(text, "Invalid parameter | event_time too old | code 100")
		self.assertNotIn(self.TOKEN, text)

	def test_network_error_names_the_type_only(self):
		with mock.patch("requests.post", side_effect=ConnectionError("https://x?access_token=" + self.TOKEN)):
			ok, text = m._post_events(_settings(), self.TOKEN, [{"a": 1}])
		self.assertFalse(ok)
		self.assertEqual(text, "Network error (ConnectionError)")

	def test_error_text_fallbacks(self):
		self.assertEqual(m.error_text({}, 502), "HTTP 502")
		self.assertEqual(m.error_text(None), "Unknown error")


class WiringTest(unittest.TestCase):
	"""The doctype JSON, hooks, patch list and fixture must agree with the service."""

	def _doctype(self, folder):
		with open(os.path.join(APP_DIR, "doctype", folder, folder + ".json"), encoding="utf-8") as fh:
			return json.load(fh)

	def test_event_fields_used_by_the_service_exist(self):
		fields = {f["fieldname"] for f in self._doctype("meta_capi_event")["fields"]}
		for name in (
			"invoice",
			"event_id",
			"customer",
			"order_value",
			"event_time",
			"status",
			"status_reason",
			"decided_by",
			"decided_at",
			"test_sent_at",
			"sent_at",
			"attempts",
			"fbtrace_id",
			"last_error",
		):
			self.assertIn(name, fields)

	def test_event_statuses_match(self):
		field = next(f for f in self._doctype("meta_capi_event")["fields"] if f["fieldname"] == "status")
		options = set(field["options"].split("\n"))
		self.assertEqual(
			options,
			{
				m.STATUS_PENDING,
				m.STATUS_HELD,
				m.STATUS_APPROVED,
				m.STATUS_REJECTED,
				m.STATUS_SENT,
				m.STATUS_FAILED,
				m.STATUS_EXPIRED,
				m.STATUS_SKIPPED,
			},
		)

	def test_event_invoice_is_unique(self):
		field = next(f for f in self._doctype("meta_capi_event")["fields"] if f["fieldname"] == "invoice")
		self.assertEqual(field.get("unique"), 1)

	def test_settings_fields_and_token_protection(self):
		doc = self._doctype("meta_capi_settings")
		fields = {f["fieldname"]: f for f in doc["fields"]}
		for name in (
			"enabled",
			"test_mode",
			"test_event_code",
			"dataset_id",
			"api_version",
			"access_token",
			"start_date",
			"approval_mode",
			"hold_above",
			"excluded_customer_groups",
			"do_not_send_phones",
		):
			self.assertIn(name, fields)
		self.assertEqual(fields["access_token"]["fieldtype"], "Password")
		self.assertEqual(fields["access_token"]["permlevel"], 1)
		self.assertEqual(fields["enabled"]["default"], "0")
		self.assertEqual(fields["test_mode"]["default"], "1")
		self.assertEqual(
			set(fields["approval_mode"]["options"].split("\n")),
			{m.APPROVAL_EVERY_ORDER, m.APPROVAL_HELD_ONLY},
		)
		level_one = {p["role"] for p in doc["permissions"] if p.get("permlevel") == 1}
		self.assertEqual(level_one, {"System Manager"})

	def test_scheduler_and_role_hooks(self):
		from jarz_pos import hooks

		hourly = hooks.scheduler_events["hourly"]
		self.assertIn("jarz_pos.services.meta_capi.queue_delivered_orders", hourly)
		self.assertIn("jarz_pos.services.meta_capi.send_approved_events", hourly)
		self.assertLess(
			hourly.index("jarz_pos.services.meta_capi.queue_delivered_orders"),
			hourly.index("jarz_pos.services.meta_capi.send_approved_events"),
		)
		self.assertIn("jarz_pos.setup.meta_capi_role.ensure_meta_capi_role", hooks.before_migrate)

	def test_seed_patch_is_registered(self):
		with open(os.path.join(APP_DIR, "patches.txt"), encoding="utf-8") as fh:
			self.assertIn("jarz_pos.Patches.v1_9.seed_meta_capi_settings", fh.read().split())

	def test_customer_exclusion_field_is_a_fixture(self):
		with open(os.path.join(APP_DIR, "fixtures", "custom_field.json"), encoding="utf-8") as fh:
			rows = json.load(fh)
		row = next(r for r in rows if r.get("name") == "Customer-custom_exclude_from_meta")
		self.assertEqual(row["fieldname"], m.CUSTOMER_EXCLUDE_FIELD)
		self.assertEqual(row["fieldtype"], "Check")
		self.assertEqual(row["dt"], "Customer")


def _order(**over):
	order = {
		"name": "ACC-SINV-9",
		"amended_from": None,
		"customer": "CUST-1",
		"contact_mobile": "01001234567",
		"customer_mobile": "",
		"grand_total": 480,
		"creation": D(2026, 10, 18, 14, 0),
		"custom_order_purpose": "Standard",
		"sales_partner": None,
		"is_partner_order": 0,
		"customer_type": "Individual",
		"customer_group": "Individual",
		"exclude_from_meta": 0,
	}
	order.update(over)
	return SimpleNamespace(**order)


class RuleSkipReasonTest(unittest.TestCase):
	"""Every delivered chat order gets a row; these rules decide which are Skipped and why."""

	def reason(self, blocked=(), **over):
		phone = m.normalize_eg_mobile("01001234567")
		return m.rule_skip_reason(_order(**over), phone, ("B2B", "Employee"), blocked)

	def test_a_plain_customer_order_passes(self):
		self.assertIsNone(self.reason())

	def test_each_rule_names_its_reason(self):
		self.assertEqual(self.reason(custom_order_purpose="Sample"), "Not a standard order: Sample")
		self.assertEqual(self.reason(custom_order_purpose=None), "Not a standard order: blank")
		self.assertEqual(self.reason(sales_partner="Talabat"), "Partner order: Talabat")
		self.assertEqual(self.reason(is_partner_order=1), "Partner order: partner order")
		self.assertEqual(self.reason(customer_type="Company"), "Not an individual customer: Company")
		self.assertEqual(self.reason(customer_group="B2B"), "Excluded customer group: B2B")
		self.assertEqual(self.reason(exclude_from_meta=1), "Customer excluded from Meta")
		self.assertEqual(self.reason(blocked=m.parse_phones("01001234567")), "Phone on the do-not-send list")

	def test_any_of_the_order_phones_can_block(self):
		blocked = m.parse_phones("01112223333")
		others = m.order_phones("01001234567", "+20 111 222 3333", "junk")
		self.assertEqual(len(others), 2)
		phone = m.normalize_eg_mobile("01001234567")
		reason = m.rule_skip_reason(_order(), phone, ("B2B",), blocked, others)
		self.assertEqual(reason, "Phone on the do-not-send list")
		self.assertIsNone(m.rule_skip_reason(_order(), phone, ("B2B",), blocked))

	def test_purpose_is_checked_before_the_group(self):
		self.assertEqual(
			self.reason(custom_order_purpose="Employee", customer_group="Employee"),
			"Not a standard order: Employee",
		)

	def test_every_reason_has_a_category(self):
		self.assertEqual(m.reason_category("Partner order: Talabat"), "Partner order")
		self.assertEqual(m.reason_category("Customer excluded from Meta"), "Customer excluded from Meta")
		self.assertEqual(m.reason_category(""), "No reason recorded")
		self.assertEqual(m.reason_category(None), "No reason recorded")


class DoNotSendListTest(unittest.TestCase):
	def test_formats_are_normalised_and_junk_ignored(self):
		phones = m.parse_phones("01001234567\n+20 111 222 3333, 01223334444; hello\n\n123")
		expected = {m.normalize_eg_mobile(p) for p in ("01001234567", "01112223333", "01223334444")}
		self.assertEqual(phones, frozenset(expected))

	def test_blank_list_blocks_nobody(self):
		self.assertEqual(m.parse_phones(""), frozenset())
		self.assertEqual(m.parse_phones(None), frozenset())

	def test_settings_carry_the_list(self):
		doc = {"enabled": 1, "do_not_send_phones": "01001234567"}
		with mock.patch.object(m.frappe, "get_single", return_value=doc, create=True):
			settings = m.load_settings()
		self.assertIn(m.normalize_eg_mobile("01001234567"), settings["blocked_phones"])


class EarlierRowVerdictTest(unittest.TestCase):
	def test_sent_or_rejected_blocks_any_copy(self):
		for status in (m.STATUS_SENT, m.STATUS_REJECTED):
			self.assertEqual(m.earlier_row_verdict(status, True), "block")
			self.assertEqual(m.earlier_row_verdict(status, False), "block")

	def test_in_flight_on_a_cancelled_original_is_superseded(self):
		for status in (m.STATUS_PENDING, m.STATUS_HELD, m.STATUS_APPROVED, m.STATUS_FAILED):
			self.assertEqual(m.earlier_row_verdict(status, False), "supersede")
			self.assertEqual(m.earlier_row_verdict(status, True), "block")

	def test_skipped_and_expired_say_nothing(self):
		self.assertEqual(m.earlier_row_verdict(m.STATUS_SKIPPED, False), "ignore")
		self.assertEqual(m.earlier_row_verdict(m.STATUS_EXPIRED, True), "ignore")


QUEUE_SETTINGS = {
	"excluded_groups": ("B2B",),
	"blocked_phones": frozenset(),
	"approval_mode": m.APPROVAL_EVERY_ORDER,
	"hold_above": 2500,
}


class QueueOneTest(unittest.TestCase):
	def _queue(self, order, earlier=()):
		inserted, marks = {}, []

		def get_doc(values):
			inserted.update(values)
			return SimpleNamespace(insert=lambda **k: None)

		# The earlier invoice was cancelled when it was amended.
		db = SimpleNamespace(get_value=mock.Mock(return_value=2))
		with (
			mock.patch.object(m.frappe, "get_doc", get_doc, create=True),
			mock.patch.object(m.frappe, "get_all", mock.Mock(return_value=list(earlier)), create=True),
			mock.patch.object(m.frappe, "db", db, create=True),
			mock.patch.object(m, "_mark", lambda name, values: marks.append((name, values))),
		):
			status = m._queue_one(order, QUEUE_SETTINGS)
		return status, inserted, marks

	def test_rule_skip_still_writes_a_row(self):
		status, row, _ = self._queue(_order(customer_group="B2B"))
		self.assertEqual(status, m.STATUS_SKIPPED)
		self.assertEqual(row["status_reason"], "Excluded customer group: B2B")
		self.assertEqual(row["invoice"], "ACC-SINV-9")

	def test_eligible_order_waits_for_approval(self):
		status, row, _ = self._queue(_order())
		self.assertEqual(status, m.STATUS_PENDING)
		self.assertEqual(row["status_reason"], "")

	def test_amendment_retires_the_cancelled_original(self):
		earlier = [SimpleNamespace(name="EV-1", invoice="ACC-SINV-9", status=m.STATUS_PENDING)]
		status, _, marks = self._queue(_order(name="ACC-SINV-9-1", amended_from="ACC-SINV-9"), earlier)
		self.assertEqual(status, m.STATUS_PENDING)
		retired = {"status": m.STATUS_SKIPPED, "status_reason": "Replaced by amendment: ACC-SINV-9-1"}
		self.assertEqual(marks, [("EV-1", retired)])

	def test_already_sent_order_is_not_sent_twice(self):
		earlier = [SimpleNamespace(name="EV-1", invoice="ACC-SINV-9", status=m.STATUS_SENT)]
		status, row, _ = self._queue(_order(name="ACC-SINV-9-1", amended_from="ACC-SINV-9"), earlier)
		self.assertEqual(status, m.STATUS_SKIPPED)
		self.assertEqual(row["status_reason"], "Already queued: same order as ACC-SINV-9 (Sent)")

	def test_rule_skipped_amendment_still_retires_the_original(self):
		earlier = [SimpleNamespace(name="EV-1", invoice="ACC-SINV-9", status=m.STATUS_PENDING)]
		order = _order(name="ACC-SINV-9-1", amended_from="ACC-SINV-9", exclude_from_meta=1)
		status, row, marks = self._queue(order, earlier)
		self.assertEqual(status, m.STATUS_SKIPPED)
		self.assertEqual(row["status_reason"], "Customer excluded from Meta")
		self.assertEqual([name for name, _ in marks], ["EV-1"])

	def test_blocked_customer_mobile_is_skipped_behind_another_contact_number(self):
		with mock.patch.dict(QUEUE_SETTINGS, {"blocked_phones": m.parse_phones("01112223333")}):
			status, row, _ = self._queue(_order(customer_mobile="01112223333"))
		self.assertEqual(status, m.STATUS_SKIPPED)
		self.assertEqual(row["status_reason"], "Phone on the do-not-send list")


class RetryTest(unittest.TestCase):
	def test_failed_rows_go_back_to_approved(self):
		rows = {
			"EV-1": SimpleNamespace(status=m.STATUS_FAILED, event_time=D(2026, 10, 18, 12, 0)),
			"EV-2": SimpleNamespace(status=m.STATUS_FAILED, event_time=D(2026, 10, 10, 12, 0)),
			"EV-3": SimpleNamespace(status=m.STATUS_SENT, event_time=D(2026, 10, 18, 12, 0)),
		}
		marks = {}
		db = SimpleNamespace(get_value=mock.Mock(side_effect=lambda dt, name, *a, **k: rows.get(name)))
		with (
			mock.patch.object(m, "_require_manager"),
			mock.patch.object(m.frappe, "db", db, create=True),
			mock.patch.object(m, "_mark", lambda name, values: marks.__setitem__(name, values)),
			mock.patch("frappe.utils.now_datetime", return_value=D(2026, 10, 19, 12, 0)),
		):
			result = m.retry_events(json.dumps([*rows, "EV-missing"]))
		self.assertEqual(result, {"retried": 1, "expired": 1, "skipped": 2})
		self.assertEqual(marks["EV-1"], {"status": m.STATUS_APPROVED, "attempts": 0, "last_error": ""})
		self.assertEqual(marks["EV-2"]["status"], m.STATUS_EXPIRED)
		self.assertNotIn("EV-3", marks)


class SkippedReportTest(unittest.TestCase):
	def test_reasons_group_by_category(self):
		day = datetime.date
		rows = [
			SimpleNamespace(
				status_reason="Partner order: A", orders=3, order_value=900, last_day=day(2026, 10, 18)
			),
			SimpleNamespace(
				status_reason="Partner order: B", orders=2, order_value=600, last_day=day(2026, 10, 19)
			),
			SimpleNamespace(
				status_reason="Order value is zero", orders=1, order_value=0, last_day=day(2026, 10, 17)
			),
		]
		db = SimpleNamespace(sql=mock.Mock(return_value=rows))
		with mock.patch.object(m.frappe, "db", db, create=True):
			out = m.skipped_by_reason("2026-10-06", "2026-10-19")
		self.assertEqual([r["reason"] for r in out], ["Partner order", "Order value is zero"])
		self.assertEqual(out[0]["orders"], 5)
		self.assertEqual(out[0]["order_value"], 1500.0)
		self.assertEqual(out[0]["last_day"], day(2026, 10, 19))

	def test_report_switches_view(self):
		from jarz_pos.report.meta_capi_daily import meta_capi_daily as report

		with mock.patch.object(report, "skipped_by_reason", return_value=[{"reason": "x"}]) as skipped:
			columns, data = report.execute({"view": report.VIEW_SKIPPED})
		skipped.assert_called_once()
		self.assertEqual(columns[0]["fieldname"], "reason")
		self.assertEqual(data, [{"reason": "x"}])
		with mock.patch.object(report, "daily_rows", return_value=[]) as daily:
			columns, _ = report.execute({})
		daily.assert_called_once()
		self.assertEqual(columns[0]["fieldname"], "day")


class WorkspaceCountsTest(unittest.TestCase):
	def test_meta_tiles_count_event_rows(self):
		from jarz_pos.utils import setup_workspace as sw

		labels = [s["label"] for s in sw.META_SHORTCUTS]
		self.assertEqual(
			labels, ["Meta: Waiting Approval", "Meta: Held", "Meta: Failed", "Meta: Sent This Week"]
		)
		statuses = set()
		for sc in sw.META_SHORTCUTS:
			self.assertEqual((sc["type"], sc["link_to"]), ("DocType", m.EVENT_DOCTYPE))
			self.assertIn("{}", sc["format"])
			for flt in sc["stats_filter"]:
				# Four elements: with a fifth, Frappe drops the last filter.
				self.assertEqual(len(flt), 4)
				self.assertEqual(flt[0], m.EVENT_DOCTYPE)
				if flt[1] == "status":
					statuses.add(flt[3])
		self.assertEqual(statuses, {m.STATUS_PENDING, m.STATUS_HELD, m.STATUS_FAILED, m.STATUS_SENT})

	def test_meta_tiles_get_their_own_header(self):
		from jarz_pos.utils import setup_workspace as sw

		blocks = sw._content_blocks([], [], sw.META_SHORTCUTS)
		self.assertIn("Meta Chat Orders", blocks[1]["data"]["text"])
		names = [b["data"]["shortcut_name"] for b in blocks[2:]]
		self.assertEqual(names, [s["label"] for s in sw.META_SHORTCUTS])


if __name__ == "__main__":
	unittest.main()
