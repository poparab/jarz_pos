"""Site-less checks for safe, partial Google Maps lead suggestions."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from jarz_pos.services import lead_maps


class _Cache:
    def __init__(self):
        self.values = {}
        self.counters = {}

    def set_value(self, key, value, **_kwargs):
        self.values[key] = value

    def get_value(self, key, **_kwargs):
        return self.values.get(key)

    def make_key(self, key):
        return key

    def incrby(self, key, amount):
        self.counters[key] = self.counters.get(key, 0) + amount
        return self.counters[key]

    def expire(self, *_args):
        return True


class TestLeadMapsPreview(unittest.TestCase):
    def test_selected_pin_supplies_name_coordinates_and_save_payload(self):
        result = lead_maps.preview(
            "https://www.google.com/maps/place/Coffee+Lab/"
            "data=!4m2!3d30.0444!4d31.2357"
        )

        self.assertTrue(result["success"])
        self.assertTrue(result["resolved"])
        self.assertEqual(result["place_name"], "Coffee Lab")
        self.assertEqual(result["precision"], "pin")
        self.assertAlmostEqual(result["latitude"], 30.0444, places=4)
        self.assertAlmostEqual(result["longitude"], 31.2357, places=4)
        self.assertEqual(result["suggestions"]["lead_name"], "Coffee Lab")
        self.assertEqual(result["suggestions"]["maps_url"], result["url"])

    def test_viewport_is_never_suggested_as_the_business_pin(self):
        result = lead_maps.preview(
            "https://www.google.com/maps/place/Coffee+Lab/@30.0444,31.2357,17z"
        )

        self.assertTrue(result["success"])
        self.assertFalse(result["resolved"])
        self.assertEqual(result["reason"], "viewport_only")
        self.assertNotIn("latitude", result["suggestions"])
        self.assertNotIn("longitude", result["suggestions"])

    def test_named_query_is_an_honest_partial_result(self):
        result = lead_maps.preview(
            "https://www.google.com/maps/search/?api=1&query=Coffee+Lab+Cairo"
        )

        self.assertTrue(result["success"])
        self.assertFalse(result["resolved"])
        self.assertEqual(result["reason"], "no_coordinates_in_link")
        self.assertEqual(result["place_name"], "Coffee Lab Cairo")
        self.assertEqual(result["metadata_source"], "url")

    def test_maps_subdomain_root_query_and_plus_codes_remain_supported(self):
        query = lead_maps.preview("https://maps.google.com/?q=30.0444,31.2357")
        plus = lead_maps.preview("https://plus.codes/8G2H2M2G+Q2")
        self.assertTrue(query["resolved"])
        self.assertEqual(query["precision"], "query")
        self.assertTrue(plus["success"])
        self.assertTrue(plus["resolved"])
        self.assertEqual(plus["precision"], "plus_code")

    def test_non_maps_hosts_protocols_ports_and_paths_are_rejected(self):
        urls = (
            "http://maps.google.com/maps?q=30,31",
            "https://127.0.0.1/maps?q=30,31",
            "https://169.254.169.254/latest/meta-data",
            "https://google.example.com/maps?q=30,31",
            "https://www.google.com.evil.example/maps?q=30,31",
            "https://google.zip/maps?q=30,31",
            "https://user:pass@www.google.com/maps?q=30,31",
            "https://www.google.com:444/maps?q=30,31",
            "https://www.google.com/url?q=https://example.com",
            "https://www.google.com/search?q=coffee",
            "https://goo.gl/not-maps",
        )
        for url in urls:
            with self.subTest(url=url):
                result = lead_maps.preview(url)
                self.assertFalse(result["success"])
                self.assertEqual(result["reason"], "invalid_maps_url")

    def test_redirect_destination_is_validated_before_the_next_request(self):
        short = "https://maps.app.goo.gl/AbCdEf12345"
        with patch.object(
            lead_maps,
            "_request_once",
            return_value=(302, {"Location": "https://169.254.169.254/latest"}),
        ) as request:
            self.assertEqual(lead_maps._expand_short_link(short), "")
        request.assert_called_once()

    def test_expansion_has_a_single_safe_redirect_chain(self):
        short = "https://maps.app.goo.gl/AbCdEf12345"
        long_url = "https://www.google.com/maps/place/Coffee"
        with patch.object(
            lead_maps,
            "_request_once",
            side_effect=[
                (302, {"Location": long_url}),
                (200, {}),
            ],
        ):
            self.assertEqual(lead_maps._expand_short_link(short), long_url)

    @patch("jarz_pos.services.lead_maps._places_key", return_value="configured")
    @patch("jarz_pos.services.lead_maps._fetch_place_details")
    def test_places_location_wins_over_a_viewport_and_fills_address(self, fetch, _key):
        fetch.return_value = {
            "id": "ChIJ1234567890",
            "displayName": {"text": "Coffee Lab"},
            "formattedAddress": "12 Nile Street, Zamalek, Cairo, Egypt",
            "internationalPhoneNumber": "+20 2 1234 5678",
            "websiteUri": "https://coffee.example",
            "location": {"latitude": 30.061, "longitude": 31.219},
            "addressComponents": [
                {"longText": "Zamalek", "types": ["sublocality_level_1"]},
                {"longText": "Cairo", "types": ["locality"]},
                {
                    "longText": "Cairo Governorate",
                    "types": ["administrative_area_level_1"],
                },
                {"longText": "Egypt", "shortText": "EG", "types": ["country"]},
                {"longText": "11211", "types": ["postal_code"]},
            ],
        }
        url = (
            "https://www.google.com/maps/place/Coffee+Lab/@1.0,2.0,17z"
            "/data=!4m2!1sChIJ1234567890!8m2"
        )

        result = lead_maps._preview_canonical(
            url, url, include_place_details=True
        )

        fetch.assert_called_once_with("ChIJ1234567890", "configured")
        self.assertEqual(result["metadata_source"], "google_places")
        self.assertTrue(result["resolved"])
        self.assertEqual(result["precision"], "place")
        self.assertEqual(result["latitude"], 30.061)
        self.assertEqual(result["longitude"], 31.219)
        self.assertEqual(result["address_line1"], "12 Nile Street, Zamalek, Cairo, Egypt")
        self.assertEqual(result["primary_area"], "Zamalek")
        self.assertEqual(result["city"], "Cairo")
        self.assertEqual(result["state"], "Cairo Governorate")
        self.assertEqual(result["country"], "Egypt")
        self.assertEqual(result["pincode"], "11211")
        self.assertEqual(result["suggestions"]["phone"], "+20 2 1234 5678")

    @patch("jarz_pos.services.lead_maps._places_key", return_value="configured")
    @patch("jarz_pos.services.lead_maps._fetch_place_details")
    def test_malformed_places_nested_values_degrade_to_url_hints(self, fetch, _key):
        fetch.return_value = {
            "id": "ChIJ1234567890",
            "displayName": "unexpected",
            "location": ["unexpected"],
            "addressComponents": "unexpected",
        }
        url = (
            "https://www.google.com/maps/place/Coffee+Lab/"
            "data=!4m2!1sChIJ1234567890!3d30.0444!4d31.2357"
        )
        result = lead_maps._preview_canonical(
            url, url, include_place_details=True
        )
        self.assertTrue(result["success"])
        self.assertTrue(result["resolved"])
        self.assertEqual(result["place_name"], "Coffee Lab")


class TestLeadMapsAsyncPreview(unittest.TestCase):
    def setUp(self):
        self.cache = _Cache()

    def _request(self, link, user="rep@example.com"):
        with (
            patch.object(lead_maps.frappe, "cache", return_value=self.cache),
            patch.object(
                lead_maps.frappe, "generate_hash", return_value="A" * 32
            ),
            patch.object(lead_maps.frappe, "enqueue") as enqueue,
        ):
            result = lead_maps.request_preview(link, user=user)
        return result, enqueue

    def test_short_link_queues_and_returns_a_user_scoped_poll_ticket(self):
        short = "https://maps.app.goo.gl/AbCdEf12345"
        result, enqueue = self._request(short)

        self.assertTrue(result["success"])
        self.assertTrue(result["pending"])
        self.assertEqual(result["request_id"], "A" * 32)
        self.assertEqual(result["retry_after_ms"], 500)
        self.assertEqual(result["suggestions"], {"maps_url": short})
        enqueue.assert_called_once()

        with patch.object(lead_maps.frappe, "cache", return_value=self.cache):
            denied = lead_maps.request_preview(
                request_id="A" * 32, user="other@example.com"
            )
        self.assertFalse(denied["success"])
        self.assertEqual(denied["reason"], "request_expired")

    def test_same_user_and_link_reuses_ticket_without_a_second_job(self):
        short = "https://maps.app.goo.gl/AbCdEf12345"
        first, enqueue = self._request(short)
        self.assertEqual(enqueue.call_count, 1)

        with (
            patch.object(lead_maps.frappe, "cache", return_value=self.cache),
            patch.object(lead_maps.frappe, "enqueue") as second_enqueue,
        ):
            second = lead_maps.request_preview(short, user="rep@example.com")
        self.assertEqual(second["request_id"], first["request_id"])
        second_enqueue.assert_not_called()

    def test_queue_failure_is_terminal_and_manual_save_remains_available(self):
        short = "https://maps.app.goo.gl/AbCdEf12345"
        with (
            patch.object(lead_maps.frappe, "cache", return_value=self.cache),
            patch.object(
                lead_maps.frappe, "generate_hash", return_value="B" * 32
            ),
            patch.object(
                lead_maps.frappe, "enqueue", side_effect=RuntimeError("down")
            ),
        ):
            result = lead_maps.request_preview(short, user="rep@example.com")
        self.assertFalse(result["success"])
        self.assertFalse(result["pending"])
        self.assertEqual(result["reason"], "queue_unavailable")
        self.assertEqual(result["suggestions"], {"maps_url": short})

    def test_worker_stores_terminal_result_for_polling(self):
        short = "https://maps.app.goo.gl/AbCdEf12345"
        self._request(short)
        expanded = (
            "https://www.google.com/maps/place/Coffee/"
            "data=!4m2!3d30.0444!4d31.2357"
        )
        with (
            patch.object(lead_maps.frappe, "cache", return_value=self.cache),
            patch.object(lead_maps, "_expand_short_link", return_value=expanded),
            patch.object(lead_maps, "_places_key", return_value=""),
        ):
            lead_maps.resolve_preview_job(
                "A" * 32, short, "rep@example.com"
            )
            polled = lead_maps.request_preview(
                request_id="A" * 32, user="rep@example.com"
            )
        self.assertFalse(polled["pending"])
        self.assertTrue(polled["resolved"])
        self.assertEqual(polled["canonical_url"], expanded)
        self.assertEqual(polled["suggestions"]["maps_url"], short)


if __name__ == "__main__":
    unittest.main()


class TestCatalogAndOsmEnrichment(unittest.TestCase):
    """What a pasted link yields when there is no Google Places key at all."""

    CAFE_LINK = (
        "https://www.google.com/maps/place/Cilantro/"
        "data=!4m2!3d30.0538921!4d31.2013923"
        "!1s0x1458413a6bccb49d:0x70aab26eb6a5265b"
    )

    def _preview(self, *, match=None, area=None):
        from jarz_pos.services import lead_area, lead_place_match

        with patch.object(lead_maps, "_places_key", return_value=""), patch.object(
            lead_place_match, "match", return_value=match or {"matched": False}
        ), patch.object(
            lead_area, "resolve_area", return_value=area or {"area": ""}
        ):
            return lead_maps.preview(self.CAFE_LINK)

    def test_a_known_place_is_flagged_and_lends_what_the_catalog_holds(self):
        result = self._preview(
            match={
                "matched": True,
                "how": "cid",
                "confidence": "exact",
                "lead": "LEAD-0007",
                "branch_name": "Cilantro Gameat El Dewal",
                "distance_m": 0,
                "known": {
                    "phone": "+20 2 87654321",
                    "primary_area": "Mohandessin",
                    "opening_hours": "07:00-01:00",
                    "rating": 4.5,
                    "category": "Coffee",
                },
            }
        )

        self.assertEqual(result["duplicate"]["lead"], "LEAD-0007")
        self.assertEqual(result["duplicate"]["how"], "cid")
        self.assertEqual(result["metadata_source"], "lead_catalog")
        self.assertEqual(result["phone"], "+20 2 87654321")
        self.assertEqual(result["primary_area"], "Mohandessin")
        # And the form has to be able to see them.
        self.assertEqual(result["suggestions"]["opening_hours"], "07:00-01:00")
        self.assertEqual(result["suggestions"]["rating"], 4.5)
        self.assertEqual(result["suggestions"]["category"], "Coffee")

    def test_an_unknown_place_carries_no_duplicate_block(self):
        result = self._preview()

        self.assertNotIn("duplicate", result)

    def test_the_web_request_never_reaches_openstreetmap(self):
        # The invariant this module exists to keep: no network in a web worker.
        from jarz_pos.services import osm_places

        with patch.object(osm_places, "reverse") as reverse:
            self._preview()

        reverse.assert_not_called()

    def test_the_background_pass_fills_only_what_is_still_blank(self):
        result = lead_maps._base("x", resolved=True, latitude=30.05, longitude=31.20)
        result["city"] = "Cairo"          # already known, must survive
        result["phone"] = "+20 100 000"   # already known, must survive

        from jarz_pos.services import osm_places

        with patch.object(
            osm_places,
            "reverse",
            return_value={
                "address_line1": "44 Al Sadat Axis",
                "city": "Alexandria",
                "phone": "01119660266",
                "pincode": "11865",
            },
        ):
            lead_maps._enrich_with_osm(result)

        self.assertEqual(result["address_line1"], "44 Al Sadat Axis")
        self.assertEqual(result["pincode"], "11865")
        self.assertEqual(result["city"], "Cairo")
        self.assertEqual(result["phone"], "+20 100 000")
        self.assertEqual(sorted(result["osm_filled"]), ["address_line1", "pincode"])
        self.assertEqual(result["suggestions"]["pincode"], "11865")

    def test_a_link_with_no_pin_is_never_reverse_geocoded(self):
        result = lead_maps._base("x", resolved=False)

        from jarz_pos.services import osm_places

        with patch.object(osm_places, "reverse") as reverse:
            lead_maps._enrich_with_osm(result)

        reverse.assert_not_called()

    def test_a_complete_address_does_not_spend_the_rate_limited_slot(self):
        complete = lead_maps._base(
            "x", resolved=True, address_line1="1 Nile St", city="Cairo", pincode="11511"
        )
        self.assertFalse(lead_maps._needs_lookup(complete))

        missing = lead_maps._base("x", resolved=True, city="Cairo")
        with patch("jarz_pos.services.osm_places.enabled", return_value=True):
            self.assertTrue(lead_maps._needs_lookup(missing))

    def test_nothing_is_queued_for_a_link_that_never_resolved_a_pin(self):
        self.assertFalse(lead_maps._needs_lookup(lead_maps._base("x", resolved=False)))


class TestBackgroundPassNeverCostsTheForeground(unittest.TestCase):
    """Regressions found in review: the optional extra must stay optional."""

    LINK = (
        "https://www.google.com/maps/place/Cilantro/"
        "data=!4m2!3d30.0538921!4d31.2013923"
    )

    def _initial(self):
        return lead_maps._base(
            self.LINK,
            resolved=True,
            latitude=30.0538921,
            longitude=31.2013923,
            primary_area="Mohandessin",
            suggestions={"maps_url": self.LINK, "latitude": 30.0538921},
        )

    def test_a_rate_limited_lookup_keeps_the_pin_it_already_had(self):
        # The 9th paste in a minute used to come back success=False, and the
        # client treats that as terminal -- so the lead saved with NO location
        # even though the backend had the coordinates in hand.
        initial = self._initial()
        with patch.object(lead_maps, "_cache_get", return_value={}), patch.object(
            lead_maps, "_consume_enqueue_budget", return_value=False
        ):
            result = lead_maps._enqueue_preview(self.LINK, "rep@jarz", initial=initial)

        self.assertTrue(result["success"])
        self.assertTrue(result["resolved"])
        self.assertEqual(result["latitude"], 30.0538921)
        self.assertEqual(result["primary_area"], "Mohandessin")
        self.assertFalse(result["pending"])
        self.assertEqual(result["reason"], "preview_rate_limited")
        self.assertTrue(result["warnings"])

    def test_a_dead_queue_keeps_the_pin_too(self):
        initial = self._initial()
        with patch.object(lead_maps, "_cache_get", return_value={}), patch.object(
            lead_maps, "_consume_enqueue_budget", return_value=True
        ), patch.object(lead_maps, "_cache_set", return_value=True), patch.object(
            lead_maps.frappe, "generate_hash", return_value="a" * 32
        ), patch.object(
            lead_maps.frappe, "enqueue", side_effect=RuntimeError("no redis")
        ):
            result = lead_maps._enqueue_preview(self.LINK, "rep@jarz", initial=initial)

        self.assertTrue(result["success"])
        self.assertEqual(result["latitude"], 30.0538921)
        self.assertEqual(result["reason"], "queue_unavailable")

    def test_a_short_link_with_nothing_computed_still_reports_failure(self):
        # No `initial` means the URL was never expanded: there is genuinely
        # nothing to hand back, and pretending otherwise would be worse.
        with patch.object(lead_maps, "_cache_get", return_value={}), patch.object(
            lead_maps, "_consume_enqueue_budget", return_value=False
        ):
            result = lead_maps._enqueue_preview(
                "https://maps.app.goo.gl/abc", "rep@jarz"
            )

        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "preview_rate_limited")

    def test_the_ticket_is_published_before_the_address_lookup_runs(self):
        # The client polls for 7.5s; expansion can take 5s and Nominatim 6 more.
        # Waiting for the optional extra pushed a ready preview past the window.
        published = []

        def record(key, value, ttl=None):
            result = (value or {}).get("result")
            published.append(dict(result) if isinstance(result, dict) else None)
            return True

        canonical = lead_maps._base(
            self.LINK, resolved=True, latitude=30.05, longitude=31.20
        )
        with patch.object(
            lead_maps,
            "_cache_get",
            return_value={"user": "rep@jarz", "canonical_url": self.LINK},
        ), patch.object(lead_maps, "_cache_set", side_effect=record), patch.object(
            lead_maps, "_preview_canonical", return_value=canonical
        ), patch.object(
            lead_maps, "_enrich_with_osm"
        ) as enrich:
            lead_maps.resolve_preview_job("b" * 32, self.LINK, "rep@jarz")

        self.assertTrue(
            published and published[0] is not None,
            "a usable result must be published before the address lookup",
        )
        self.assertTrue(published[0]["resolved"])
        enrich.assert_called_once()

