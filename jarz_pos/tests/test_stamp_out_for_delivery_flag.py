"""stamp_out_for_delivery_flag: the permanent "was out for delivery" lock.

These cases lived in the Woo app's test_submitted_invoice_amendment.py, which
imported jarz_pos — an app the Woo CI never installs — so they errored on every
Woo CI run from 2026-09-09. The hook belongs to jarz_pos, so its tests do too.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import jarz_pos.events.sales_invoice as si_events


class _FakeMeta:
	def __init__(self, has_field=True):
		self.has_field = has_field

	def get_field(self, fieldname):
		return self.has_field if fieldname == "custom_was_out_for_delivery" else None


def _run(doc, has_field=True):
	set_value_calls = []
	fake_frappe = SimpleNamespace(
		get_meta=lambda dt: _FakeMeta(has_field),
		db=SimpleNamespace(set_value=lambda *a, **kw: set_value_calls.append((a, kw))),
		log_error=lambda *a, **kw: None,
		get_traceback=lambda: "",
	)
	with patch.object(si_events, "frappe", fake_frappe):
		si_events.stamp_out_for_delivery_flag(doc)
	return set_value_calls


def _doc(state, flagged=0):
	return SimpleNamespace(
		name="ACC-SINV-00042",
		custom_was_out_for_delivery=flagged,
		custom_sales_invoice_state=state,
		sales_invoice_state=None,
	)


class TestStampOutForDeliveryFlag(unittest.TestCase):
	def test_stamps_flag_on_first_ofd_transition(self):
		doc = _doc("Out for Delivery")
		calls = _run(doc)
		self.assertEqual(len(calls), 1)
		self.assertEqual(calls[0][0], ("Sales Invoice", "ACC-SINV-00042", "custom_was_out_for_delivery", 1))
		self.assertEqual(calls[0][1], {"update_modified": False})
		self.assertEqual(doc.custom_was_out_for_delivery, 1)

	def test_does_not_stamp_when_already_flagged(self):
		self.assertEqual(_run(_doc("Out for Delivery", flagged=1)), [])

	def test_does_not_stamp_for_non_ofd_state(self):
		doc = _doc("Delivered")
		self.assertEqual(_run(doc), [])
		self.assertEqual(doc.custom_was_out_for_delivery, 0)

	def test_skips_when_field_is_missing(self):
		self.assertEqual(_run(_doc("Out for Delivery"), has_field=False), [])


if __name__ == "__main__":
	unittest.main()
