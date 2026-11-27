"""A rider created at a partner branch is linked to that branch's delivery partner.

On 2026-11-27 a Deliverk rider was created at 6th of October without the link; the
trip was booked as an ordinary courier and his fee was kept back from the drawer at
settlement. ``create_delivery_party`` now fills in the branch's
``custom_default_delivery_partner`` for a new Supplier rider unless the caller says
he is not a partner rider (``no_delivery_partner``).
"""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe

from jarz_pos.api import couriers


class TestCreateDeliveryPartyDefaultPartner(unittest.TestCase):
	def _create(self, branch_default, **kwargs):
		created = {"party_type": kwargs.get("party_type", "Supplier"), "party": "Rider 1"}
		doc = MagicMock()
		doc.meta.get_field.return_value = True
		patches = [
			patch.object(couriers, "_create_delivery_party", return_value=dict(created)),
			patch.object(couriers, "_branch_default_delivery_partner", return_value=branch_default),
			patch.object(frappe, "get_doc", return_value=doc),
			patch("jarz_pos.utils.validation_utils.assert_pos_profile_enabled", return_value=None),
			patch.object(frappe, "get_meta", return_value=SimpleNamespace(get_field=lambda f: True)),
			patch.object(frappe.db, "get_value", return_value=1),
		]
		for p in patches:
			p.start()
			self.addCleanup(p.stop)
		args = {
			"party_type": "Supplier",
			"first_name": "A",
			"last_name": "B",
			"pos_profile": "6th of october",
		}
		args.update(kwargs)
		return couriers.create_delivery_party(**args), doc

	def test_supplier_gets_branch_partner(self):
		result, doc = self._create("Deliverk")
		self.assertEqual(result["delivery_partner"], "Deliverk")
		self.assertEqual(doc.custom_delivery_partner, "Deliverk")
		doc.save.assert_called_once()

	def test_opt_out_keeps_rider_unlinked(self):
		result, doc = self._create("Deliverk", no_delivery_partner=1)
		self.assertNotIn("delivery_partner", result)
		doc.save.assert_not_called()

	def test_explicit_partner_wins(self):
		result, _doc = self._create("Deliverk", delivery_partner="Other Co")
		self.assertEqual(result["delivery_partner"], "Other Co")

	def test_employee_is_never_defaulted(self):
		result, doc = self._create("Deliverk", party_type="Employee")
		self.assertNotIn("delivery_partner", result)
		doc.save.assert_not_called()

	def test_branch_without_default(self):
		result, doc = self._create(None)
		self.assertNotIn("delivery_partner", result)
		doc.save.assert_not_called()


class TestBranchDefaultDeliveryPartner(unittest.TestCase):
	def _resolve(self, profile_row, partner_active=1, has_default_field=True):
		meta = SimpleNamespace(
			get_field=lambda f: has_default_field if f == "custom_default_delivery_partner" else True
		)

		def get_value(doctype, name, fields=None, as_dict=False):
			if doctype == "POS Profile":
				return profile_row
			if doctype == "Delivery Partner":
				return partner_active
			raise AssertionError(doctype)

		with (
			patch.object(frappe, "get_meta", return_value=meta),
			patch.object(frappe.db, "get_value", side_effect=get_value),
		):
			return couriers._branch_default_delivery_partner("6th of october")

	def test_returns_active_partner(self):
		row = {"custom_default_delivery_partner": "Deliverk", "custom_allow_delivery_partner": 1}
		self.assertEqual(self._resolve(row), "Deliverk")

	def test_branch_not_allowed_partners(self):
		row = {"custom_default_delivery_partner": "Deliverk", "custom_allow_delivery_partner": 0}
		self.assertIsNone(self._resolve(row))

	def test_inactive_partner(self):
		row = {"custom_default_delivery_partner": "Deliverk", "custom_allow_delivery_partner": 1}
		self.assertIsNone(self._resolve(row, partner_active=0))

	def test_field_missing_on_site(self):
		self.assertIsNone(self._resolve({}, has_default_field=False))

	def test_no_branch(self):
		self.assertIsNone(couriers._branch_default_delivery_partner(None))


if __name__ == "__main__":
	unittest.main()
