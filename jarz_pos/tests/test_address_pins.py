"""Map-pin keys on order cards: the Maps link builder and the batch enrichment.

The order board's "Pinned / No map pin" badge said "No map pin" for website
orders that carried one, because ``get_kanban_invoices`` never projected the
Address geo columns. These tests pin the projection:

* :func:`geo.maps_link_for` - the pure link builder (valid, Null Island, out of
  range, ``None``, strings).
* :mod:`utils.address_pins` - one batched Address query, meta-guarded geo
  columns, legacy ``"Location: <url>"`` fallback, and failure degrading to the
  empty keys with a single (itself guarded) log call.

Pure ``unittest``: ``frappe`` is stubbed, no site, no network.
"""

import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from jarz_pos.utils import address_pins, geo

PIN_KEYS = {
	"address_latitude",
	"address_longitude",
	"geo_source",
	"geo_confidence",
	"has_location_pin",
	"location_link",
	"pin_source",
	"pin_flag",
}


class TestMapsLinkFor(unittest.TestCase):
	def test_valid_coordinates_format_to_six_decimals(self):
		self.assertEqual(
			geo.maps_link_for(30.0444, 31.2357),
			"https://www.google.com/maps/search/?api=1&query=30.044400,31.235700",
		)

	def test_negative_coordinates(self):
		self.assertEqual(
			geo.maps_link_for(-33.8688, 151.2093123456),
			"https://www.google.com/maps/search/?api=1&query=-33.868800,151.209312",
		)

	def test_null_island_rejected(self):
		self.assertEqual(geo.maps_link_for(0, 0), "")
		self.assertEqual(geo.maps_link_for(0.0, 0.0), "")

	def test_one_zero_axis_is_still_valid(self):
		self.assertTrue(geo.maps_link_for(0, 31.2).startswith("https://"))

	def test_out_of_range_rejected(self):
		for lat, lng in ((91, 31), (-90.5, 31), (30, 181), (30, -180.01)):
			with self.subTest(lat=lat, lng=lng):
				self.assertEqual(geo.maps_link_for(lat, lng), "")

	def test_bounds_inclusive(self):
		self.assertTrue(geo.maps_link_for(90, 180))
		self.assertTrue(geo.maps_link_for(-90, -180))

	def test_none_rejected(self):
		self.assertEqual(geo.maps_link_for(None, None), "")
		self.assertEqual(geo.maps_link_for(30.0, None), "")
		self.assertEqual(geo.maps_link_for(None, 31.0), "")

	def test_numeric_strings_accepted(self):
		self.assertEqual(
			geo.maps_link_for("30.05", " 31.24 "),
			"https://www.google.com/maps/search/?api=1&query=30.050000,31.240000",
		)

	def test_garbage_strings_rejected(self):
		for lat, lng in (("abc", "31"), ("", ""), ("nan", "31"), ("inf", "31"), ("30,1", "31")):
			with self.subTest(lat=lat, lng=lng):
				self.assertEqual(geo.maps_link_for(lat, lng), "")

	def test_no_spaces_in_link(self):
		self.assertNotIn(" ", geo.maps_link_for(30.1, 31.2))


class TestPinFieldsFromAddress(unittest.TestCase):
	def test_pinned_address(self):
		out = address_pins.pin_fields_from_address(
			{
				"custom_latitude": 30.05,
				"custom_longitude": 31.24,
				"custom_geo_source": "customer_pin",
				"custom_geo_confidence": 30,
				"address_line2": "Location: https://maps.app.goo.gl/legacy",
			}
		)
		self.assertTrue(out["has_location_pin"])
		self.assertEqual(out["address_latitude"], 30.05)
		self.assertEqual(out["address_longitude"], 31.24)
		self.assertEqual(out["geo_source"], "customer_pin")
		self.assertEqual(out["geo_confidence"], 30)
		# The stored pin wins over the pasted link.
		self.assertEqual(
			out["location_link"],
			"https://www.google.com/maps/search/?api=1&query=30.050000,31.240000",
		)

	def test_unpinned_with_legacy_link(self):
		out = address_pins.pin_fields_from_address(
			{
				"custom_latitude": None,
				"custom_longitude": None,
				"address_line2": "Location: https://maps.app.goo.gl/AbC123",
			}
		)
		self.assertFalse(out["has_location_pin"])
		self.assertIsNone(out["address_latitude"])
		self.assertIsNone(out["address_longitude"])
		self.assertEqual(out["location_link"], "https://maps.app.goo.gl/AbC123")
		self.assertEqual(out["geo_source"], "")
		self.assertEqual(out["geo_confidence"], 0)

	def test_zero_zero_is_not_a_pin(self):
		out = address_pins.pin_fields_from_address(
			{"custom_latitude": 0, "custom_longitude": 0, "address_line2": "Flat 4"}
		)
		self.assertFalse(out["has_location_pin"])
		self.assertIsNone(out["address_latitude"])
		self.assertEqual(out["location_link"], "")

	def test_none_row(self):
		self.assertEqual(address_pins.pin_fields_from_address(None), address_pins.EMPTY_PIN_FIELDS)

	def test_bad_confidence_degrades_to_zero(self):
		out = address_pins.pin_fields_from_address(
			{"custom_latitude": "30.1", "custom_longitude": "31.2", "custom_geo_confidence": "high"}
		)
		self.assertTrue(out["has_location_pin"])
		self.assertEqual(out["geo_confidence"], 0)


def _frappe_stub(*, rows=None, geo_columns=True, get_all_raises=None, meta_raises=False):
	mf = MagicMock()
	meta = MagicMock()
	meta.has_field.side_effect = lambda f: bool(geo_columns) and f in address_pins.GEO_COLUMNS
	if meta_raises:
		mf.get_meta.side_effect = Exception("no meta")
	else:
		mf.get_meta.return_value = meta
	if get_all_raises:
		mf.get_all.side_effect = get_all_raises
	else:
		mf.get_all.return_value = rows or []
	mf.get_traceback.return_value = "tb"
	return mf


class TestGetAddressPinMap(unittest.TestCase):
	def test_single_batched_query_for_the_board(self):
		mf = _frappe_stub(
			rows=[
				{
					"name": "ADDR-1",
					"address_line2": "",
					"custom_latitude": 30.05,
					"custom_longitude": 31.24,
					"custom_geo_source": "customer_pin",
					"custom_geo_confidence": 30,
				},
				{
					"name": "ADDR-2",
					"address_line2": "Location: https://maps.app.goo.gl/X1",
					"custom_latitude": None,
					"custom_longitude": None,
					"custom_geo_source": None,
					"custom_geo_confidence": None,
				},
			]
		)
		with patch.object(address_pins, "frappe", mf):
			out = address_pins.get_address_pin_map(["ADDR-1", "ADDR-2", "ADDR-1", None, ""])

		self.assertEqual(mf.get_all.call_count, 1)
		args, kwargs = mf.get_all.call_args
		self.assertEqual(args[0], "Address")
		self.assertEqual(kwargs["filters"], {"name": ["in", ["ADDR-1", "ADDR-2"]]})
		self.assertEqual(
			kwargs["fields"],
			["name", "address_line2", *address_pins.GEO_COLUMNS],
		)
		mf.get_doc.assert_not_called()

		self.assertTrue(out["ADDR-1"]["has_location_pin"])
		self.assertFalse(out["ADDR-2"]["has_location_pin"])
		self.assertEqual(out["ADDR-2"]["location_link"], "https://maps.app.goo.gl/X1")

	def test_no_names_means_no_query(self):
		mf = _frappe_stub()
		with patch.object(address_pins, "frappe", mf):
			self.assertEqual(address_pins.get_address_pin_map([None, "", "  "]), {})
		mf.get_all.assert_not_called()

	def test_missing_address_gets_empty_keys(self):
		mf = _frappe_stub(rows=[])
		with patch.object(address_pins, "frappe", mf):
			pin_map = address_pins.get_address_pin_map(["ADDR-GONE"])
		out = address_pins.pin_fields_for("ADDR-GONE", pin_map)
		self.assertEqual(out, address_pins.EMPTY_PIN_FIELDS)
		self.assertEqual(address_pins.pin_fields_for(None, pin_map), address_pins.EMPTY_PIN_FIELDS)

	def test_missing_geo_columns_selects_legacy_only(self):
		mf = _frappe_stub(
			geo_columns=False,
			rows=[{"name": "ADDR-1", "address_line2": "Location: https://goo.gl/maps/abc"}],
		)
		with patch.object(address_pins, "frappe", mf):
			out = address_pins.get_address_pin_map(["ADDR-1"])
		self.assertEqual(mf.get_all.call_args.kwargs["fields"], ["name", "address_line2"])
		self.assertFalse(out["ADDR-1"]["has_location_pin"])
		self.assertEqual(out["ADDR-1"]["location_link"], "https://goo.gl/maps/abc")
		self.assertEqual(out["ADDR-1"]["geo_confidence"], 0)

	def test_meta_failure_selects_legacy_only(self):
		mf = _frappe_stub(meta_raises=True, rows=[])
		with patch.object(address_pins, "frappe", mf):
			address_pins.get_address_pin_map(["ADDR-1"])
		self.assertEqual(mf.get_all.call_args.kwargs["fields"], ["name", "address_line2"])

	def test_query_failure_degrades_and_logs_once(self):
		mf = _frappe_stub(get_all_raises=Exception("db down"))
		with patch.object(address_pins, "frappe", mf):
			out = address_pins.get_address_pin_map(["A", "B", "C"])
		self.assertEqual(out, {})
		self.assertEqual(mf.log_error.call_count, 1)

	def test_log_error_raising_is_swallowed(self):
		mf = _frappe_stub(get_all_raises=Exception("db down"))
		mf.log_error.side_effect = Exception("log table locked")
		with patch.object(address_pins, "frappe", mf):
			self.assertEqual(address_pins.get_address_pin_map(["A"]), {})

	def test_pin_fields_for_returns_a_copy(self):
		pin_map = {"A": {"has_location_pin": True}}
		out = address_pins.pin_fields_for("A", pin_map)
		out["has_location_pin"] = False
		self.assertTrue(pin_map["A"]["has_location_pin"])
		empty = address_pins.pin_fields_for("missing", pin_map)
		empty["geo_source"] = "x"
		self.assertEqual(address_pins.EMPTY_PIN_FIELDS["geo_source"], "")

	def test_every_result_carries_the_full_key_set(self):
		mf = _frappe_stub(rows=[{"name": "A", "address_line2": None}])
		with patch.object(address_pins, "frappe", mf):
			pin_map = address_pins.get_address_pin_map(["A"])
		self.assertEqual(set(address_pins.pin_fields_for("A", pin_map)), PIN_KEYS)
		self.assertEqual(set(address_pins.pin_fields_for("Z", pin_map)), PIN_KEYS)


# ─────────────────────────────────────────────────────────────────────────────
# Per-invoice pin snapshot (custom_delivery_pin_*) over the shared Address pin
# ─────────────────────────────────────────────────────────────────────────────

#: The snapshot this order was placed with.
SNAP_LAT, SNAP_LNG = 30.0500, 31.2400
#: ~2.2 km north: where a LATER order re-pinned the shared Address.
FAR_LAT, FAR_LNG = 30.0700, 31.2400
#: ~55 m north of the snapshot: a courier's refinement of the same door.
NEAR_LAT, NEAR_LNG = 30.0505, 31.2400


def _address(lat, lng, source="customer_pin", confidence=30):
	return address_pins.pin_fields_from_address(
		{
			"custom_latitude": lat,
			"custom_longitude": lng,
			"custom_geo_source": source,
			"custom_geo_confidence": confidence,
		}
	)


def _invoice(lat=SNAP_LAT, lng=SNAP_LNG, source="customer_checkout", flag=""):
	return {
		"name": "ACC-SINV-1",
		"custom_delivery_pin_latitude": lat,
		"custom_delivery_pin_longitude": lng,
		"custom_delivery_pin_source": source,
		"custom_delivery_pin_flag": flag,
	}


class TestInvoicePinOverride(unittest.TestCase):
	def test_snapshot_wins_over_a_different_address_pin(self):
		"""The bug: a later order re-pinned the Address and moved this order."""
		out = address_pins.invoice_pin_override(_invoice(), _address(FAR_LAT, FAR_LNG))
		self.assertTrue(out["has_location_pin"])
		self.assertEqual(out["address_latitude"], SNAP_LAT)
		self.assertEqual(out["address_longitude"], SNAP_LNG)
		self.assertEqual(out["geo_source"], "customer_pin")
		self.assertEqual(out["geo_confidence"], 30)
		self.assertEqual(out["location_link"], geo.maps_link_for(SNAP_LAT, SNAP_LNG))
		self.assertEqual(out["pin_source"], "customer_checkout")
		self.assertEqual(out["pin_flag"], "")
		self.assertEqual(set(out), PIN_KEYS)

	def test_snapshot_wins_when_address_has_no_pin(self):
		out = address_pins.invoice_pin_override(_invoice(), address_pins.empty_pin_fields())
		self.assertTrue(out["has_location_pin"])
		self.assertEqual(out["address_latitude"], SNAP_LAT)

	def test_verified_address_pin_near_the_snapshot_wins(self):
		for source, rank in (("courier_verified", 40), ("manual_override", 50)):
			with self.subTest(source=source):
				addr = _address(NEAR_LAT, NEAR_LNG, source, rank)
				out = address_pins.invoice_pin_override(_invoice(), addr)
				self.assertEqual(out["address_latitude"], NEAR_LAT)
				self.assertEqual(out["geo_source"], source)
				self.assertEqual(out["geo_confidence"], rank)
				self.assertEqual(out["location_link"], geo.maps_link_for(NEAR_LAT, NEAR_LNG))
				# Snapshot metadata still rides along.
				self.assertEqual(out["pin_source"], "customer_checkout")

	def test_far_staff_confirmed_address_pin_beats_the_snapshot(self):
		"""A far verified/override pin is staff correcting a wrong customer pin."""
		for source, rank in (("courier_verified", 40), ("manual_override", 50)):
			with self.subTest(source=source):
				addr = _address(FAR_LAT, FAR_LNG, source, rank)
				out = address_pins.invoice_pin_override(_invoice(flag="area_mismatch"), addr)
				self.assertEqual(out["address_latitude"], FAR_LAT)
				self.assertEqual(out["geo_source"], source)
				self.assertEqual(out["pin_flag"], "area_mismatch")

	def test_far_courier_web_pin_loses_to_the_snapshot(self):
		"""courier_web is unverified: only the 150 m refinement rule applies."""
		out = address_pins.invoice_pin_override(_invoice(), _address(FAR_LAT, FAR_LNG, "courier_web", 35))
		self.assertEqual(out["address_latitude"], SNAP_LAT)
		self.assertEqual(out["geo_source"], "customer_pin")
		self.assertEqual(out["geo_confidence"], 30)

	def test_flagged_order_without_a_pin_hides_another_orders_pin(self):
		"""outside_egypt + no snapshot: the shared Address pin is not this order's."""
		out = address_pins.invoice_pin_override(
			_invoice(lat=None, lng=None, source="", flag="outside_egypt"), _address(FAR_LAT, FAR_LNG)
		)
		self.assertFalse(out["has_location_pin"])
		self.assertIsNone(out["address_latitude"])
		self.assertEqual(out["location_link"], "")
		self.assertEqual(out["pin_flag"], "outside_egypt")
		self.assertEqual(set(out), PIN_KEYS)

	def test_flagged_order_without_a_pin_keeps_a_staff_confirmed_pin(self):
		addr = _address(FAR_LAT, FAR_LNG, "manual_override", 50)
		out = address_pins.invoice_pin_override(
			_invoice(lat=None, lng=None, source="", flag="outside_egypt"), addr
		)
		self.assertTrue(out["has_location_pin"])
		self.assertEqual(out["address_latitude"], FAR_LAT)
		self.assertEqual(out["pin_flag"], "outside_egypt")

	def test_near_pin_of_equal_confidence_does_not_beat_the_snapshot(self):
		"""Only a HIGHER-confidence pin is a refinement; a peer customer pin is not."""
		out = address_pins.invoice_pin_override(_invoice(), _address(NEAR_LAT, NEAR_LNG))
		self.assertEqual(out["address_latitude"], SNAP_LAT)

	def test_no_snapshot_falls_back_to_the_address_pin_unchanged(self):
		addr = _address(FAR_LAT, FAR_LNG, "courier_verified", 40)
		for row in (
			_invoice(lat=None, lng=None, source=""),
			_invoice(lat=0, lng=0, source=""),
			_invoice(lat=95, lng=31, source=""),
			_invoice(lat="", lng="", source=""),
		):
			with self.subTest(row=row):
				out = address_pins.invoice_pin_override(row, addr)
				self.assertEqual(out, addr)

	def test_flag_passthrough_with_a_pin(self):
		out = address_pins.invoice_pin_override(
			_invoice(flag=" outside_area , area_mismatch ,"), _address(FAR_LAT, FAR_LNG)
		)
		self.assertTrue(out["has_location_pin"])
		self.assertEqual(out["pin_flag"], "outside_area,area_mismatch")

	def test_flag_passthrough_without_a_usable_pin(self):
		"""outside_egypt can arrive with no stored pin at all."""
		out = address_pins.invoice_pin_override(
			_invoice(lat=None, lng=None, source="maps_link", flag="outside_egypt"),
			address_pins.empty_pin_fields(),
		)
		self.assertFalse(out["has_location_pin"])
		self.assertIsNone(out["address_latitude"])
		self.assertEqual(out["pin_flag"], "outside_egypt")
		self.assertEqual(out["pin_source"], "maps_link")

	def test_columns_missing_keeps_old_behaviour(self):
		"""An unmigrated site selects none of the columns: the row lacks the keys."""
		addr = _address(FAR_LAT, FAR_LNG)
		row = {"name": "ACC-SINV-1", "customer_address": "ADDR-1"}
		out = address_pins.invoice_pin_override(row, addr)
		self.assertEqual(out, addr)
		self.assertEqual(out["pin_source"], "")
		self.assertEqual(out["pin_flag"], "")

	def test_none_row_and_none_address(self):
		self.assertEqual(address_pins.invoice_pin_override(None, None), address_pins.EMPTY_PIN_FIELDS)

	def test_does_not_mutate_the_address_fields(self):
		addr = _address(FAR_LAT, FAR_LNG)
		snapshot = dict(addr)
		address_pins.invoice_pin_override(_invoice(), addr)
		self.assertEqual(addr, snapshot)

	def test_row_whose_get_raises_degrades_to_the_address_pin(self):
		class Boom:
			def get(self, key):
				raise RuntimeError("boom")

		addr = _address(FAR_LAT, FAR_LNG)
		self.assertEqual(address_pins.invoice_pin_override(Boom(), addr), addr)


class TestInvoicePinColumns(unittest.TestCase):
	def _mf(self, present):
		mf = MagicMock()
		mf.get_meta.return_value.has_field.side_effect = lambda f: f in present
		return mf

	def test_all_columns_present(self):
		mf = self._mf(set(address_pins.INVOICE_PIN_COLUMNS))
		with patch.object(address_pins, "frappe", mf):
			self.assertEqual(address_pins.invoice_pin_columns(), list(address_pins.INVOICE_PIN_COLUMNS))
		mf.get_meta.assert_called_once_with("Sales Invoice")

	def test_unmigrated_site_selects_nothing(self):
		mf = self._mf(set())
		with patch.object(address_pins, "frappe", mf):
			self.assertEqual(address_pins.invoice_pin_columns(), [])

	def test_partial_migration_selects_only_existing(self):
		mf = self._mf({"custom_delivery_pin_flag"})
		with patch.object(address_pins, "frappe", mf):
			self.assertEqual(address_pins.invoice_pin_columns(), ["custom_delivery_pin_flag"])

	def test_meta_failure_selects_nothing(self):
		mf = MagicMock()
		mf.get_meta.side_effect = Exception("no meta")
		with patch.object(address_pins, "frappe", mf):
			self.assertEqual(address_pins.invoice_pin_columns(), [])

	def test_never_reuses_the_courier_proof_of_delivery_columns(self):
		"""custom_delivery_latitude/longitude are the courier's GPS at delivery."""
		self.assertNotIn("custom_delivery_latitude", address_pins.INVOICE_PIN_COLUMNS)
		self.assertNotIn("custom_delivery_longitude", address_pins.INVOICE_PIN_COLUMNS)


class TestInvoicePinSchema(unittest.TestCase):
	"""Fixture + before_migrate seeder for the snapshot fields."""

	@classmethod
	def setUpClass(cls):
		import json
		import os

		import jarz_pos

		path = os.path.join(
			os.path.dirname(os.path.abspath(jarz_pos.__file__)), "fixtures", "custom_field.json"
		)
		with open(path, encoding="utf-8") as handle:
			data = json.load(handle)
		cls.fields = {e.get("fieldname"): e for e in data if e.get("dt") == "Sales Invoice"}
		cls.data = data

	def test_every_field_present_once_with_safe_types(self):
		"""No varchar: tabSales Invoice is at the 65,535-byte row limit."""
		expected = {
			"custom_delivery_pin_section": "Section Break",
			"custom_delivery_pin_latitude": "Float",
			"custom_delivery_pin_longitude": "Float",
			"custom_delivery_pin_source": "Small Text",
			"custom_delivery_pin_flag": "Small Text",
		}
		for fieldname, fieldtype in expected.items():
			with self.subTest(field=fieldname):
				matches = [
					e for e in self.data if e.get("dt") == "Sales Invoice" and e.get("fieldname") == fieldname
				]
				self.assertEqual(len(matches), 1)
				entry = matches[0]
				self.assertEqual(entry["fieldtype"], fieldtype)
				self.assertEqual(entry["name"], f"Sales Invoice-{fieldname}")
				self.assertEqual(entry["module"], "jarz pos")
				self.assertEqual(entry["allow_on_submit"], 1)
				self.assertEqual(entry["no_copy"], 1)
				self.assertEqual(entry["read_only"], 1)
				self.assertEqual(entry["print_hide"], 1)

	def test_section_is_collapsible_and_anchored_in_the_address_block(self):
		section = self.fields["custom_delivery_pin_section"]
		self.assertEqual(section["collapsible"], 1)
		self.assertEqual(section["insert_after"], "dispatch_address")

	def test_coordinates_signed_six_places(self):
		for fieldname in ("custom_delivery_pin_latitude", "custom_delivery_pin_longitude"):
			with self.subTest(field=fieldname):
				self.assertEqual(self.fields[fieldname]["non_negative"], 0)
				self.assertEqual(str(self.fields[fieldname]["precision"]), "6")

	def test_seeder_mirrors_the_fixture(self):
		from jarz_pos.utils import cleanup

		created = []
		fake = MagicMock()
		fake.db.exists.return_value = False

		def _capture(payload):
			doc = MagicMock()
			created.append((payload, doc))
			return doc

		fake.get_doc.side_effect = _capture
		with patch.object(cleanup, "frappe", fake):
			cleanup.ensure_invoice_delivery_pin_fields()

		seeded = {}
		for payload, doc in created:
			attrs = dict(payload)
			for call in doc.set.call_args_list:
				if len(call.args) == 2:
					attrs[call.args[0]] = call.args[1]
			seeded[payload["fieldname"]] = attrs

		self.assertEqual(set(seeded), {f for f in self.fields if f.startswith("custom_delivery_pin_")})
		for fieldname, attrs in seeded.items():
			with self.subTest(field=fieldname):
				fixture = self.fields[fieldname]
				self.assertEqual(attrs["dt"], "Sales Invoice")
				self.assertEqual(attrs["fieldtype"], fixture["fieldtype"])
				self.assertEqual(attrs["insert_after"], fixture["insert_after"])
				for flag in ("allow_on_submit", "no_copy", "read_only", "print_hide"):
					self.assertEqual(attrs.get(flag), 1)

	def test_seeder_registered_after_the_collision_sweep(self):
		from jarz_pos import hooks

		before = hooks.before_migrate
		seeder = "jarz_pos.utils.cleanup.ensure_invoice_delivery_pin_fields"
		sweep = "jarz_pos.utils.cleanup.remove_colliding_custom_fields_for_fixtures"
		self.assertIn(seeder, before)
		self.assertGreater(before.index(seeder), before.index(sweep))


class TestNewerPosLinkBeatsTheSnapshot(unittest.TestCase):
	"""L2 / L4: a pos_link Address pin written AFTER the invoice snapshot wins.

	``Address.modified`` cannot say when a pin was written (the geo writer uses
	``update_modified=False``), so the Address carries ``custom_geo_updated_on`` and
	the invoice's ``creation`` stands in for the snapshot time.
	"""

	CREATED = datetime(2026, 10, 10, 12, 0, 0)

	def _addr(self, lat, lng, source="pos_link", rank=20, updated=None):
		return address_pins.pin_fields_from_address(
			{
				"custom_latitude": lat,
				"custom_longitude": lng,
				"custom_geo_source": source,
				"custom_geo_confidence": rank,
				"custom_geo_updated_on": updated,
			}
		)

	def _inv(self, created=None, **kwargs):
		row = _invoice(**kwargs)
		row["creation"] = created if created is not None else self.CREATED
		return row

	def test_pos_link_written_after_the_invoice_wins(self):
		addr = self._addr(FAR_LAT, FAR_LNG, updated=self.CREATED + timedelta(hours=1))
		out = address_pins.invoice_pin_override(self._inv(), addr)
		self.assertEqual(out["address_latitude"], FAR_LAT)
		self.assertEqual(out["address_longitude"], FAR_LNG)
		self.assertEqual(out["geo_source"], "pos_link")
		self.assertEqual(out["geo_confidence"], 20)
		self.assertEqual(out["location_link"], geo.maps_link_for(FAR_LAT, FAR_LNG))
		# The snapshot's metadata still rides along.
		self.assertEqual(out["pin_source"], "customer_checkout")

	def test_older_pos_link_still_loses_to_the_snapshot(self):
		addr = self._addr(FAR_LAT, FAR_LNG, updated=self.CREATED - timedelta(hours=1))
		out = address_pins.invoice_pin_override(self._inv(), addr)
		self.assertEqual(out["address_latitude"], SNAP_LAT)
		self.assertEqual(out["geo_source"], "customer_pin")

	def test_pos_link_written_at_the_same_moment_loses(self):
		addr = self._addr(FAR_LAT, FAR_LNG, updated=self.CREATED)
		out = address_pins.invoice_pin_override(self._inv(), addr)
		self.assertEqual(out["address_latitude"], SNAP_LAT)

	def test_unstamped_pos_link_keeps_the_old_behaviour(self):
		"""A site that has not migrated the column, or a pin written before it existed."""
		for updated in (None, "", "not a date"):
			with self.subTest(updated=updated):
				addr = self._addr(FAR_LAT, FAR_LNG, updated=updated)
				out = address_pins.invoice_pin_override(self._inv(), addr)
				self.assertEqual(out["address_latitude"], SNAP_LAT)

	def test_invoice_without_creation_keeps_the_old_behaviour(self):
		addr = self._addr(FAR_LAT, FAR_LNG, updated=self.CREATED + timedelta(hours=1))
		row = _invoice()
		out = address_pins.invoice_pin_override(row, addr)
		self.assertEqual(out["address_latitude"], SNAP_LAT)

	def test_newer_customer_pin_does_not_count(self):
		"""Only an explicit staff pin (pos_link) outranks the snapshot by recency."""
		for source, rank in (("customer_pin", 30), ("courier_web", 35), ("territory_centroid", 10)):
			with self.subTest(source=source):
				addr = self._addr(FAR_LAT, FAR_LNG, source, rank, updated=self.CREATED + timedelta(hours=1))
				out = address_pins.invoice_pin_override(self._inv(), addr)
				self.assertEqual(out["address_latitude"], SNAP_LAT)

	def test_timestamps_may_be_strings_or_datetimes(self):
		later = (self.CREATED + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S.%f")
		for created, updated in (
			(self.CREATED, later),
			(self.CREATED.isoformat(sep=" "), self.CREATED + timedelta(minutes=5)),
			(self.CREATED.isoformat(sep=" "), later),
		):
			with self.subTest(created=created, updated=updated):
				out = address_pins.invoice_pin_override(
					self._inv(created=created), self._addr(FAR_LAT, FAR_LNG, updated=updated)
				)
				self.assertEqual(out["address_latitude"], FAR_LAT)

	def test_card_payload_shape_does_not_change(self):
		addr = self._addr(FAR_LAT, FAR_LNG, updated=self.CREATED + timedelta(hours=1))
		self.assertIn("_geo_updated_on", addr)
		for row in (self._inv(), _invoice(), None):
			with self.subTest(row=bool(row)):
				out = address_pins.invoice_pin_override(row, addr)
				self.assertEqual(set(out), PIN_KEYS)
		self.assertEqual(set(address_pins.invoice_pin_override(self._inv(), None)), PIN_KEYS)

	# ── L4: the Woo app's pin_removed flag ──────────────────────────────

	def test_pin_removed_flag_is_accepted_and_hides_the_shared_pin(self):
		"""Removed at checkout: the shared Address pin belongs to some other order."""
		out = address_pins.invoice_pin_override(
			self._inv(lat=None, lng=None, source="", flag="pin_removed"),
			self._addr(FAR_LAT, FAR_LNG, "customer_pin", 30, updated=self.CREATED - timedelta(days=1)),
		)
		self.assertEqual(out["pin_flag"], "pin_removed")
		self.assertFalse(out["has_location_pin"])
		self.assertIsNone(out["address_latitude"])
		self.assertEqual(out["location_link"], "")
		self.assertEqual(set(out), PIN_KEYS)

	def test_pin_removed_with_blank_string_coordinates(self):
		out = address_pins.invoice_pin_override(
			self._inv(lat="", lng="", source="", flag=" pin_removed "), address_pins.empty_pin_fields()
		)
		self.assertEqual(out["pin_flag"], "pin_removed")
		self.assertFalse(out["has_location_pin"])

	def test_pin_removed_combines_with_other_flags(self):
		out = address_pins.invoice_pin_override(
			self._inv(lat=None, lng=None, source="", flag="outside_area, pin_removed"),
			address_pins.empty_pin_fields(),
		)
		self.assertEqual(out["pin_flag"], "outside_area,pin_removed")

	def test_staff_correction_after_the_removal_still_shows(self):
		"""The customer removed the pin; staff then pasted one in the POS app."""
		addr = self._addr(FAR_LAT, FAR_LNG, "pos_link", 20, updated=self.CREATED + timedelta(minutes=30))
		out = address_pins.invoice_pin_override(self._inv(lat=None, lng=None, source="", flag="pin_removed"), addr)
		self.assertTrue(out["has_location_pin"])
		self.assertEqual(out["address_latitude"], FAR_LAT)
		self.assertEqual(out["geo_source"], "pos_link")
		self.assertEqual(out["pin_flag"], "pin_removed")

	def test_pos_link_older_than_the_removal_stays_hidden(self):
		addr = self._addr(FAR_LAT, FAR_LNG, "pos_link", 20, updated=self.CREATED - timedelta(minutes=30))
		out = address_pins.invoice_pin_override(self._inv(lat=None, lng=None, source="", flag="pin_removed"), addr)
		self.assertFalse(out["has_location_pin"])

	def test_staff_verified_pin_survives_the_removal(self):
		addr = self._addr(FAR_LAT, FAR_LNG, "manual_override", 50)
		out = address_pins.invoice_pin_override(self._inv(lat=None, lng=None, source="", flag="pin_removed"), addr)
		self.assertTrue(out["has_location_pin"])
		self.assertEqual(out["geo_source"], "manual_override")

	def test_geo_updated_column_is_selected_when_present_and_skipped_when_not(self):
		self.assertIn("custom_geo_updated_on", address_pins.GEO_COLUMNS)
		mf = _frappe_stub(
			rows=[
				{
					"name": "ADDR-1",
					"address_line2": "",
					"custom_latitude": 30.05,
					"custom_longitude": 31.24,
					"custom_geo_source": "pos_link",
					"custom_geo_confidence": 20,
					"custom_geo_updated_on": self.CREATED,
				}
			]
		)
		with patch.object(address_pins, "frappe", mf):
			out = address_pins.get_address_pin_map(["ADDR-1"])
		self.assertIn("custom_geo_updated_on", mf.get_all.call_args.kwargs["fields"])
		self.assertEqual(out["ADDR-1"]["_geo_updated_on"], self.CREATED)

		# A site that has not migrated the column: the query must not ask for it.
		mf = _frappe_stub(rows=[])
		mf.get_meta.return_value.has_field.side_effect = (
			lambda f: f != "custom_geo_updated_on" and f in address_pins.GEO_COLUMNS
		)
		with patch.object(address_pins, "frappe", mf):
			address_pins.get_address_pin_map(["ADDR-1"])
		self.assertNotIn("custom_geo_updated_on", mf.get_all.call_args.kwargs["fields"])


if __name__ == "__main__":  # pragma: no cover
	unittest.main()
