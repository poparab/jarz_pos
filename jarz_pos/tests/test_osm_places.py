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
    """Redis stand-in with real SET NX semantics -- the gate depends on them."""

    def __init__(self):
        self.values = {}
        self.atomic = {}

    def set_value(self, key, value, **_kwargs):
        self.values[key] = value

    def get_value(self, key, **_kwargs):
        return self.values.get(key)

    def make_key(self, key):
        return key

    def set(self, key, value, nx=False, **_kwargs):
        if nx and key in self.atomic:
            return None
        self.atomic[key] = value
        return True


class _NoCache:
    """Stands in for a site whose Redis is unreachable."""

    def set_value(self, *_a, **_k):
        raise RuntimeError("redis down")

    def get_value(self, *_a, **_k):
        raise RuntimeError("redis down")

    def make_key(self, key):
        return key

    def set(self, *_a, **_k):
        raise RuntimeError("redis down")


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
        ), patch.object(
            osm_places, "_request", side_effect=OSError("timeout")
        ), patch.object(osm_places, "_note_failure"):
            self.assertEqual(osm_places.reverse(*CAIRO), {})

        stored = [k for k in cache.values if k.startswith("jarz_pos:osm:reverse:3")]
        self.assertEqual(stored, [], "a failure must not be cached as an answer")

    def test_a_failing_lookup_is_logged_once_an_hour_not_once_per_lead(self):
        # Every path here swallows its exception, so without a log a Nominatim
        # block would present as "addresses quietly stopped filling". Throttled,
        # because a busy day must not fill the error log with one outage.
        cache = _Cache()
        with patch.object(osm_places.frappe, "cache", return_value=cache), patch.object(
            osm_places.frappe, "log_error"
        ) as log_error, patch.object(
            osm_places.frappe, "get_traceback", return_value="boom"
        ):
            osm_places._note_failure()
            osm_places._note_failure()
            osm_places._note_failure()

        self.assertEqual(log_error.call_count, 1)

    def test_a_logger_that_itself_raises_cannot_break_the_lookup(self):
        with patch.object(osm_places.frappe, "cache", return_value=_Cache()), patch.object(
            osm_places.frappe, "log_error", side_effect=RuntimeError("db gone")
        ), patch.object(osm_places.frappe, "get_traceback", return_value="boom"):
            osm_places._note_failure()  # must not raise

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

class TestRateSlotIsAtomic(unittest.TestCase):
    """Two workers entering in the same millisecond must not both get through."""

    def test_only_one_of_two_simultaneous_workers_claims_the_slot(self):
        # A read-compare-write gate passes both; SET NX passes exactly one.
        cache = _Cache()
        with patch.object(osm_places.frappe, "cache", return_value=cache):
            first = osm_places._claim_rate_slot()
            second = osm_places._claim_rate_slot()

        self.assertTrue(first)
        self.assertFalse(second)

    def test_the_claim_is_bounded_so_the_slot_frees_itself(self):
        recorded = {}

        class _Recording(_Cache):
            def set(self, key, value, nx=False, **kwargs):
                recorded.update(kwargs)
                return super().set(key, value, nx=nx)

        with patch.object(osm_places.frappe, "cache", return_value=_Recording()):
            osm_places._claim_rate_slot()

        self.assertEqual(recorded.get("px"), osm_places.MIN_INTERVAL_MS)


class TestEnvironmentIdentity(unittest.TestCase):
    def test_staging_and_production_do_not_present_as_one_application(self):
        # Both sites are named `frontend` (staging is an AMI clone), and each
        # has its own Redis -- so the 1/sec gate is per environment. If the UA
        # were one constant, Nominatim would see 2 req/s from one application.
        def agent_for(host):
            with patch.object(osm_places.frappe, "conf", {}), patch(
                "frappe.utils.get_url", return_value="https://" + host
            ):
                return osm_places._user_agent()

        production = agent_for("erp.orderjarz.com")
        staging = agent_for("erpstg.orderjarz.com")

        self.assertNotEqual(production, staging)
        self.assertIn("erp.orderjarz.com", production)
        self.assertIn("erpstg.orderjarz.com", staging)
        self.assertIn("jarz", staging.lower())

    def test_an_explicit_agent_in_site_config_always_wins(self):
        with patch.object(
            osm_places.frappe, "conf", {"osm_user_agent": "custom-agent/2.0"}
        ):
            self.assertEqual(osm_places._user_agent(), "custom-agent/2.0")


class TestEmptyAnswersAreNotCachedForAMonth(unittest.TestCase):
    def test_an_empty_result_gets_the_short_ttl(self):
        # Nominatim reports "unable to geocode" as a 200 with an error body,
        # which is indistinguishable from a transient upstream problem.
        seen = {}

        class _Recording(_Cache):
            def set_value(self, key, value, **kwargs):
                seen[key] = kwargs.get("expires_in_sec")
                super().set_value(key, value)

        cache = _Recording()
        with patch.object(osm_places, "enabled", return_value=True), patch.object(
            osm_places.frappe, "cache", return_value=cache
        ), patch.object(osm_places, "_request", return_value={"error": "nope"}):
            osm_places.reverse(*CAIRO)

        ttl = seen.get(osm_places._cache_key(*CAIRO))
        self.assertEqual(ttl, osm_places.EMPTY_CACHE_TTL_SEC)
        self.assertLess(ttl, osm_places.CACHE_TTL_SEC)

    def test_a_real_answer_still_gets_the_long_ttl(self):
        seen = {}

        class _Recording(_Cache):
            def set_value(self, key, value, **kwargs):
                seen[key] = kwargs.get("expires_in_sec")
                super().set_value(key, value)

        with patch.object(osm_places, "enabled", return_value=True), patch.object(
            osm_places.frappe, "cache", return_value=_Recording()
        ), patch.object(osm_places, "_request", return_value=NOMINATIM_CAFE):
            osm_places.reverse(*CAIRO)

        self.assertEqual(
            seen.get(osm_places._cache_key(*CAIRO)), osm_places.CACHE_TTL_SEC
        )


if __name__ == "__main__":
    unittest.main()
