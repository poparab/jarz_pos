"""The pasted Maps link survives a save that yields no pin.

``create_customer`` used to keep the link as ``address_line2 = "Location: <url>"``.
That text was pushed verbatim into WooCommerce ``address_2``, so it stopped. But
a shared place (or a short link whose background resolve fails) yields no
coordinates, and the board/courier fell back to that line for exactly this case:
with nothing else storing the link, the courier got no pin AND no link.
``_apply_address_geo`` now keeps the raw link in ``custom_location_link``, a
column in no Woo field list.

Pure ``unittest``: ``frappe`` is stubbed, no site, no network.
"""

import unittest
from unittest.mock import MagicMock, patch

from jarz_pos.api import customer
from jarz_pos.services import geo_resolution

PLACE = "https://maps.app.goo.gl/tdjgBvin5QKk8LKz7?g_st=ac"


def _frappe(has_field=True):
	mf = MagicMock()
	mf.PermissionError = type("PermissionError", (Exception,), {})
	mf.get_meta.return_value.has_field.side_effect = lambda f: has_field and f == "custom_location_link"
	return mf


class TestApplyAddressGeoKeepsTheLink(unittest.TestCase):
	def _run(self, mf, **kwargs):
		with (
			patch.object(customer, "frappe", mf),
			patch.object(
				geo_resolution,
				"resolve_address_from_link",
				return_value={"accepted": False, "reason": "place_link_no_pin"},
			) as resolve,
			patch.object(geo_resolution, "set_address_pin", return_value={"accepted": True}) as set_pin,
		):
			return customer._apply_address_geo("ADDR-1", **kwargs), resolve, set_pin

	def test_place_link_without_a_pin_is_still_stored(self):
		mf = _frappe()
		out, resolve, _ = self._run(mf, location_link=PLACE)
		self.assertFalse(out["applied"])
		self.assertEqual(out["reason"], "place_link_no_pin")
		self.assertTrue(out["link_stored"])
		mf.db.set_value.assert_called_once_with(
			"Address", "ADDR-1", "custom_location_link", PLACE, update_modified=False
		)
		resolve.assert_called_once()

	def test_never_writes_address_line2(self):
		mf = _frappe()
		self._run(mf, location_link=PLACE)
		for call in mf.db.set_value.call_args_list:
			self.assertNotIn("address_line2", call.args)
		mf.get_doc.assert_not_called()

	def test_link_is_kept_even_when_coordinates_win(self):
		mf = _frappe()
		out, resolve, set_pin = self._run(mf, location_link=PLACE, latitude=30.0, longitude=31.0)
		self.assertTrue(out["applied"])
		self.assertTrue(out["link_stored"])
		set_pin.assert_called_once()
		resolve.assert_not_called()

	def test_unmigrated_site_skips_the_store_and_still_resolves(self):
		mf = _frappe(has_field=False)
		out, resolve, _ = self._run(mf, location_link=PLACE)
		self.assertFalse(out["link_stored"])
		mf.db.set_value.assert_not_called()
		resolve.assert_called_once()

	def test_store_failure_never_fails_the_save(self):
		mf = _frappe()
		mf.db.set_value.side_effect = Exception("db down")
		out, resolve, _ = self._run(mf, location_link=PLACE)
		self.assertFalse(out["link_stored"])
		resolve.assert_called_once()

	def test_no_link_no_store(self):
		mf = _frappe()
		out, _, _ = self._run(mf, latitude=30.0, longitude=31.0)
		self.assertFalse(out["link_stored"])
		mf.db.set_value.assert_not_called()


if __name__ == "__main__":
	unittest.main()
