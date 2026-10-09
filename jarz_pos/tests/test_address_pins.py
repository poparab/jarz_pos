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
from unittest.mock import MagicMock, patch

from jarz_pos.utils import address_pins, geo


PIN_KEYS = {
    "address_latitude",
    "address_longitude",
    "geo_source",
    "geo_confidence",
    "has_location_pin",
    "location_link",
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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
