"""Site-less checks for the OpenStreetMap reverse geocoder.

Nominatim is never called here. What is under test is the policy the module
wraps it in -- one request per second, cached, off-switchable, and silent on
failure -- because that policy is what keeps the lookup available at all.
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from jarz_pos.services import osm_places


CAIRO = (30.0444, 31.2357)

#: Shaped like a real jsonv2 reverse for a mapped cafe.
NOMINATIM_CAFE = {
    "name": "Lucaffe",
    "category": "amenity",
    "type": "cafe",
    "display_name": "Lucaffe, Al Sadat Axis, Banafseg Districts, Cairo, 11865, Egypt",
    "address": {
        "road": "Al Sadat Axis",
        "house_number": "44",
        "suburb": "Heliopolis",
        "city": "Cairo",
        "state": "Cairo",
        "postcode": "11865",
        "country": "Egypt",
    },
    "extratags": {
        "phone": "01119660266",
        "website": "https://www.lucaffeegypt.com/",
        "opening_hours": "24/7",
        "cuisine": "coffee_shop",
    },
}


class _Cache:
    def __init__(self):
        self.values = {}

    def set_value(self, key, value, **_kwargs):
        self.values[key] = value

    def get_value(self, key, **_kwargs):
        return self.values.get(key)

    def make_key(self, key):
        return key


class _NoCache:
    """Stands in for a site whose Redis is unreachable."""

    def set_value(self, *_a, **_k):
        raise RuntimeError("redis down")

    def get_value(self, *_a, **_k):
        raise RuntimeError("redis down")

    def make_key(self, key):
        return key


class TestReverseMapping(unittest.TestCase):
    def _reverse(self, payload, cache=None):
        cache = cache or _Cache()
        with patch.object(osm_places, "enabled", return_value=True), patch.object(
            osm_places.frappe, "cache", return_value=cache
        ), patch.object(osm_places, "_request", return_value=payload):
            return osm_places.reverse(*CAIRO)

    def test_a_mapped_business_yields_an_address_and_its_contacts(self):
        result = self._reverse(NOMINATIM_CAFE)

        self.assertEqual(result["address_line1"], "44 Al Sadat Axis")
        self.assertEqual(result["address_line2"], "Heliopolis")
        self.assertEqual(result["city"], "Cairo")
        self.assertEqual(result["state"], "Cairo")
        self.assertEqual(result["pincode"], "11865")
        self.assertEqual(result["country"], "Egypt")
        self.assertEqual(result["phone"], "01119660266")
        self.assertEqual(result["website"], "https://www.lucaffeegypt.com/")
        self.assertEqual(result["opening_hours"], "24/7")
        self.assertEqual(result["osm_category"], "amenity")

    def test_a_plain_building_still_yields_the_address_it_has(self):
        # The common case: the pin lands on a house, not on a mapped business.
        payload = {
            "category": "place",
            "type": "house",
            "display_name": "12, Zakaria Khalil Street, Cairo, 11799, Egypt",
            "address": {
                "road": "Zakaria Khalil Street",
                "house_number": "12",
                "city": "Cairo",
                "postcode": "11799",
                "country": "Egypt",
            },
        }
        result = self._reverse(payload)

        self.assertEqual(result["address_line1"], "12 Zakaria Khalil Street")
        self.assertEqual(result["pincode"], "11799")
        self.assertNotIn("phone", result)
        self.assertNotIn("address_line2", result)

    def test_a_street_without_a_number_is_still_a_usable_line(self):
        result = self._reverse(
            {"address": {"road": "Kasr El Nil Street", "city": "Cairo"}}
        )

        self.assertEqual(result["address_line1"], "Kasr El Nil Street")

    def test_an_error_body_and_junk_shapes_produce_nothing(self):
        for payload in (
            {"error": "Unable to geocode"},
            {},
            {"address": "not a dict", "extratags": 7},
        ):
            with self.subTest(payload=payload):
                self.assertEqual(self._reverse(payload), {})


class TestUsagePolicy(unittest.TestCase):
    def test_the_second_call_is_served_from_cache_not_from_nominatim(self):
        cache = _Cache()
        with patch.object(osm_places, "enabled", return_value=True), patch.object(
            osm_places.frappe, "cache", return_value=cache
        ), patch.object(
            osm_places, "_request", return_value=NOMINATIM_CAFE
        ) as request:
            first = osm_places.reverse(*CAIRO)
            second = osm_places.reverse(*CAIRO)

        self.assertEqual(first, second)
        self.assertEqual(request.call_count, 1)

    def test_two_pins_at_the_same_doorway_share_one_lookup(self):
        cache = _Cache()
        with patch.object(osm_places, "enabled", return_value=True), patch.object(
            osm_places.frappe, "cache", return_value=cache
        ), patch.object(
            osm_places, "_request", return_value=NOMINATIM_CAFE
        ) as request:
            osm_places.reverse(*CAIRO)
            # ~2m away: the same door for addressing purposes.
            osm_places.reverse(CAIRO[0] + 0.00001, CAIRO[1])

        self.assertEqual(request.call_count, 1)

    def test_a_second_call_within_the_second_is_declined_not_queued(self):
        cache = _Cache()
        with patch.object(osm_places, "enabled", return_value=True), patch.object(
            osm_places.frappe, "cache", return_value=cache
        ), patch.object(
            osm_places, "_request", return_value=NOMINATIM_CAFE
        ) as request:
            osm_places.reverse(*CAIRO)
            far = osm_places.reverse(29.9600, 31.2600)

        self.assertEqual(request.call_count, 1)
        self.assertEqual(far, {})

    def test_without_redis_no_request_is_made_at_all(self):
        # Fail closed: we cannot prove we are inside one request per second, and
        # being wrong risks the block that removes the feature for everyone.
        with patch.object(osm_places, "enabled", return_value=True), patch.object(
            osm_places.frappe, "cache", return_value=_NoCache()
        ), patch.object(osm_places, "_request") as request:
            result = osm_places.reverse(*CAIRO)

        self.assertEqual(result, {})
        request.assert_not_called()

    def test_a_network_failure_is_silent_and_is_not_cached(self):
        cache = _Cache()
        with patch.object(osm_places, "enabled", return_value=True), patch.object(
            osm_places.frappe, "cache", return_value=cache
        ), patch.object(osm_places, "_request", side_effect=OSError("timeout")):
            self.assertEqual(osm_places.reverse(*CAIRO), {})

        stored = [k for k in cache.values if k.startswith("jarz_pos:osm:reverse:3")]
        self.assertEqual(stored, [], "a failure must not be cached as an answer")

    def test_a_cached_empty_answer_is_honoured_rather_than_retried(self):
        cache = _Cache()
        cache.values[osm_places._cache_key(*CAIRO)] = json.dumps({})
        with patch.object(osm_places, "enabled", return_value=True), patch.object(
            osm_places.frappe, "cache", return_value=cache
        ), patch.object(osm_places, "_request") as request:
            self.assertEqual(osm_places.reverse(*CAIRO), {})

        request.assert_not_called()

    def test_the_off_switch_stops_every_lookup(self):
        with patch.object(osm_places, "enabled", return_value=False), patch.object(
            osm_places, "_request"
        ) as request:
            self.assertEqual(osm_places.reverse(*CAIRO), {})

        request.assert_not_called()

    def test_unusable_coordinates_never_reach_the_network(self):
        with patch.object(osm_places, "enabled", return_value=True), patch.object(
            osm_places.frappe, "cache", return_value=_Cache()
        ), patch.object(osm_places, "_request") as request:
            for lat, lng in ((None, None), (0, 0), ("x", "y"), (91.0, 31.0)):
                with self.subTest(lat=lat, lng=lng):
                    self.assertEqual(osm_places.reverse(lat, lng), {})

        request.assert_not_called()

    def test_the_user_agent_identifies_the_application(self):
        # Nominatim's policy requires it; an anonymous agent gets blocked.
        with patch.object(osm_places.frappe, "conf", {}):
            self.assertIn("jarz", osm_places._user_agent().lower())


if __name__ == "__main__":
    unittest.main()
