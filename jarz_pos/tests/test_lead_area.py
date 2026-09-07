"""Site-less checks for naming an area from the lead corpus.

The gazetteer is injected rather than read from a site: these tests are about
the vote and its honesty, not about the two SELECTs that fill it.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from jarz_pos.services import lead_area


#: Roughly Zamalek. Offsets below are in degrees; 0.001 is about 100m.
ZAMALEK = (30.0600, 31.2200)
MAADI = (29.9600, 31.2600)


def _point(lat, lng, area, region="Cairo/Giza", governorate="Cairo"):
    return (lat, lng, area, region, governorate)


def _cluster(centre, area, count=6, spread=0.001, **kwargs):
    lat, lng = centre
    return [
        _point(lat + i * spread, lng + i * spread, area, **kwargs)
        for i in range(count)
    ]


class TestResolveArea(unittest.TestCase):
    def _resolve(self, points, lat, lng):
        with patch.object(lead_area, "gazetteer", return_value=points):
            return lead_area.resolve_area(lat, lng)

    def test_a_pin_inside_a_known_cluster_is_named_with_high_confidence(self):
        # Maadi is sized past MIN_COARSE_SUPPORT so the shared region and
        # governorate are offered at all; the area vote is decided by Zamalek.
        points = _cluster(ZAMALEK, "Zamalek") + _cluster(
            MAADI, "Maadi", count=lead_area.MIN_COARSE_SUPPORT
        )

        result = self._resolve(points, ZAMALEK[0] + 0.0005, ZAMALEK[1])

        self.assertEqual(result["area"], "Zamalek")
        self.assertEqual(result["confidence"], "high")
        self.assertEqual(result["region"], "Cairo/Giza")
        self.assertEqual(result["governorate"], "Cairo")
        self.assertEqual(result["source"], "nearby_leads")
        self.assertLess(result["nearest_m"], 200)

    def test_a_contested_border_returns_the_winner_and_the_alternative(self):
        # Two areas meeting at the same street: the vote still answers, but it
        # must not claim high confidence and must surface the runner-up so the
        # rep can correct it in one tap.
        points = (
            _cluster(ZAMALEK, "Zamalek", count=3, spread=0.004)
            + _cluster((ZAMALEK[0] + 0.012, ZAMALEK[1]), "Dokki", count=3, spread=0.004)
        )

        result = self._resolve(points, ZAMALEK[0] + 0.006, ZAMALEK[1])

        self.assertIn(result["area"], {"Zamalek", "Dokki"})
        self.assertNotEqual(result["confidence"], "high")
        self.assertEqual(set(result["candidates"]), {"Zamalek", "Dokki"})

    def test_a_place_outside_the_swept_corpus_is_not_given_a_cairo_area(self):
        # Alexandria against a Cairo-only corpus. A confident wrong label is
        # worse than a blank the rep will fill.
        points = _cluster(ZAMALEK, "Zamalek")

        result = self._resolve(points, 31.2001, 29.9187)

        self.assertEqual(result["area"], "")
        self.assertEqual(result["confidence"], "none")
        self.assertGreater(result["nearest_m"], lead_area.MAX_USEFUL_M)

    def test_unset_and_placeholder_coordinates_resolve_to_nothing(self):
        points = _cluster(ZAMALEK, "Zamalek")

        for lat, lng in ((None, None), (0, 0), ("", ""), (95.0, 31.2), ("x", "y")):
            with self.subTest(lat=lat, lng=lng):
                result = self._resolve(points, lat, lng)
                self.assertEqual(result["area"], "")
                self.assertEqual(result["confidence"], "none")
                self.assertIsNone(result["nearest_m"])

    def test_an_empty_corpus_suggests_nothing_rather_than_raising(self):
        result = self._resolve([], ZAMALEK[0], ZAMALEK[1])

        self.assertEqual(result["area"], "")
        self.assertEqual(result["governorate"], "")

    def test_a_database_failure_degrades_to_no_suggestion(self):
        with patch.object(lead_area, "gazetteer", side_effect=RuntimeError("no site")):
            result = lead_area.resolve_area(ZAMALEK[0], ZAMALEK[1])

        self.assertEqual(result["area"], "")
        self.assertEqual(result["confidence"], "none")


class TestCoarseLabelNoise(unittest.TestCase):
    """The sweep left raw geocoder strings in ``governorate``; they must lose."""

    def _resolve(self, points, lat, lng):
        with patch.object(lead_area, "gazetteer", return_value=points):
            return lead_area.resolve_area(lat, lng)

    def test_a_locally_dominant_geocoder_string_never_wins_a_governorate(self):
        noisy = "qism awwal El Nozha, Cairo Governorate"
        # The mangled label out-numbers the clean one *locally* -- which is
        # exactly the case an isolated-point argument does not cover.
        points = _cluster(ZAMALEK, "Zamalek", count=5, governorate=noisy)
        points += [
            _point(MAADI[0] + i * 0.001, MAADI[1], "Maadi", governorate="Cairo")
            for i in range(lead_area.MIN_COARSE_SUPPORT)
        ]

        result = self._resolve(points, ZAMALEK[0], ZAMALEK[1])

        self.assertEqual(result["area"], "Zamalek")
        self.assertNotEqual(result["governorate"], noisy)
        self.assertEqual(result["governorate"], "")

    def test_a_thinly_supported_but_clean_label_is_still_withheld(self):
        # Support is what separates a real governorate from a one-off typo, and
        # a typo can be perfectly comma-free.
        points = _cluster(ZAMALEK, "Zamalek", count=5, governorate="Cairoo")

        result = self._resolve(points, ZAMALEK[0], ZAMALEK[1])

        self.assertEqual(result["area"], "Zamalek")
        self.assertEqual(result["governorate"], "")

    def test_area_labels_are_not_support_gated_so_a_new_area_still_answers(self):
        # A newly swept neighbourhood has few points by definition. Areas are
        # protected by the vote, not by a support floor.
        points = _cluster(ZAMALEK, "New Heliopolis", count=5)

        result = self._resolve(points, ZAMALEK[0], ZAMALEK[1])

        self.assertEqual(result["area"], "New Heliopolis")


class TestKnownAreas(unittest.TestCase):
    def test_vocabulary_is_ordered_by_how_much_of_the_corpus_uses_it(self):
        points = _cluster(ZAMALEK, "Zamalek", count=2) + _cluster(MAADI, "Maadi", count=5)

        with patch.object(lead_area, "gazetteer", return_value=points):
            self.assertEqual(lead_area.known_areas(), ["Maadi", "Zamalek"])


class TestLabelHygiene(unittest.TestCase):
    def test_place_labels_reject_address_lines_and_keep_names(self):
        self.assertTrue(lead_area._is_place_label("Cairo"))
        self.assertTrue(lead_area._is_place_label("6th of October"))
        self.assertFalse(lead_area._is_place_label(""))
        self.assertFalse(lead_area._is_place_label("Nasr City, Cairo Governorate"))
        self.assertFalse(lead_area._is_place_label("x" * 61))


class TestMapsPreviewFillsTheArea(unittest.TestCase):
    """The whole point: a pasted link must arrive with the catalog's filter set."""

    def test_a_pin_without_a_places_key_still_suggests_an_area(self):
        points = _cluster((30.0444, 31.2357), "Downtown") + _cluster(
            MAADI, "Maadi", count=lead_area.MIN_COARSE_SUPPORT
        )

        with patch.object(lead_area, "gazetteer", return_value=points):
            from jarz_pos.services import lead_maps

            with patch.object(lead_maps, "_places_key", return_value=""):
                result = lead_maps.preview(
                    "https://www.google.com/maps/place/Coffee+Lab/"
                    "data=!4m2!3d30.0444!4d31.2357"
                )

        self.assertTrue(result["resolved"])
        self.assertEqual(result["place_name"], "Coffee Lab")
        self.assertEqual(result["primary_area"], "Downtown")
        self.assertEqual(result["primary_area_source"], "nearby_leads")
        self.assertEqual(result["primary_area_confidence"], "high")
        self.assertEqual(result["city"], "Cairo")
        # The form reads suggestions, so the area has to reach that map too.
        self.assertEqual(result["suggestions"]["primary_area"], "Downtown")
        self.assertEqual(result["suggestions"]["city"], "Cairo")

    def test_a_link_without_a_pin_gets_no_invented_area(self):
        points = _cluster((30.0444, 31.2357), "Downtown")

        with patch.object(lead_area, "gazetteer", return_value=points):
            from jarz_pos.services import lead_maps

            result = lead_maps.preview(
                "https://www.google.com/maps/place/Coffee+Lab/@30.0444,31.2357,17z"
            )

        self.assertFalse(result["resolved"])
        self.assertNotIn("primary_area", result)
        self.assertNotIn("primary_area", result["suggestions"])


if __name__ == "__main__":
    unittest.main()
