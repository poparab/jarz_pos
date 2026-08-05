"""Table-driven tests for the Maps-link parser.

The parser is the front door of the door-pin database: everything downstream —
the OFD pin gate, delivered-far-from-pin anomaly detection, the courier's
navigate button — inherits whatever it decides. Two properties matter more than
the rest:

**Parse order.** One Google Maps URL routinely carries two different coordinate
pairs. ``!3d/!4d`` is the place the user actually selected; ``@lat,lng`` is only
the viewport centre at the moment the link was produced and moves whenever the
map is panned. Reading ``@`` when ``!3d`` is present puts the pin wherever the
user happened to be looking — often hundreds of metres away, occasionally in a
different neighbourhood. The ordering test below is the guard.

**Nothing but numbers comes out.** The input is untrusted paste from WhatsApp.
The parser returns a ``(float, float, str)`` tuple built from regex captures
converted through ``float()``, so no attacker-controlled string can survive it —
the XSS cases assert that structurally rather than by blocklisting.

Pure ``unittest``, no site, no network.
"""

import math
import unittest

from jarz_pos.utils import geo


class TestParseOrder(unittest.TestCase):
    """``!3d/!4d`` beats ``@``, which beats ``q=``, which beats a Plus Code."""

    def test_pin_wins_over_viewport(self):
        url = (
            "https://www.google.com/maps/place/Jarz/@30.044400,31.235700,17z/"
            "data=!3m1!4b1!4m6!3m5!1s0x0:0x0!8m2!3d30.050000!4d31.240000"
        )
        lat, lng, precision = geo.parse_maps_link(url)
        self.assertAlmostEqual(lat, 30.05, places=6)
        self.assertAlmostEqual(lng, 31.24, places=6)
        self.assertEqual(precision, geo.PRECISION_PIN)

    def test_viewport_used_when_there_is_no_pin(self):
        url = "https://www.google.com/maps/@30.044400,31.235700,17z"
        lat, lng, precision = geo.parse_maps_link(url)
        self.assertAlmostEqual(lat, 30.0444, places=6)
        self.assertAlmostEqual(lng, 31.2357, places=6)
        self.assertEqual(precision, geo.PRECISION_VIEWPORT)

    def test_query_used_when_there_is_neither(self):
        url = "https://maps.google.com/?q=30.044400,31.235700"
        lat, lng, precision = geo.parse_maps_link(url)
        self.assertAlmostEqual(lat, 30.0444, places=6)
        self.assertEqual(precision, geo.PRECISION_QUERY)

    def test_pin_wins_over_query_too(self):
        url = "https://maps.google.com/?q=1.000000,1.000000&x=!3d30.050000!4d31.240000"
        lat, lng, precision = geo.parse_maps_link(url)
        self.assertAlmostEqual(lat, 30.05, places=6)
        self.assertEqual(precision, geo.PRECISION_PIN)


class TestCoordinateForms(unittest.TestCase):
    """One row per input shape the corpus actually contains."""

    CASES = [
        # (label, url, expected_lat, expected_lng, expected_precision)
        (
            "pin only",
            "https://www.google.com/maps/data=!4m2!3m1!1s0x0!8m2!3d-33.868800!4d151.209300",
            -33.8688,
            151.2093,
            geo.PRECISION_PIN,
        ),
        (
            "at with zoom and tilt",
            "https://www.google.com/maps/@40.712800,-74.006000,15z/data=!3m1!1e3",
            40.7128,
            -74.006,
            geo.PRECISION_VIEWPORT,
        ),
        (
            "ll parameter",
            "https://maps.google.com/maps?ll=51.507400,-0.127800&z=16",
            51.5074,
            -0.1278,
            geo.PRECISION_QUERY,
        ),
        (
            "query parameter (api v1)",
            "https://www.google.com/maps/search/?api=1&query=48.858400,2.294500",
            48.8584,
            2.2945,
            geo.PRECISION_QUERY,
        ),
        (
            "destination parameter (directions)",
            "https://www.google.com/maps/dir/?api=1&destination=30.100000,31.300000",
            30.1,
            31.3,
            geo.PRECISION_QUERY,
        ),
        (
            "url encoded comma",
            "https://maps.google.com/?q=30.044400%2C31.235700",
            30.0444,
            31.2357,
            geo.PRECISION_QUERY,
        ),
        (
            "southern and western hemisphere",
            "https://maps.google.com/?q=-22.906800,-43.172900",
            -22.9068,
            -43.1729,
            geo.PRECISION_QUERY,
        ),
        (
            "integer coordinates",
            "https://www.google.com/maps/@30,31,17z",
            30.0,
            31.0,
            geo.PRECISION_VIEWPORT,
        ),
    ]

    def test_table(self):
        for label, url, lat, lng, precision in self.CASES:
            with self.subTest(case=label):
                parsed = geo.parse_maps_link(url)
                self.assertIsNotNone(parsed, f"{label}: expected a coordinate")
                self.assertAlmostEqual(parsed[0], lat, places=6)
                self.assertAlmostEqual(parsed[1], lng, places=6)
                self.assertEqual(parsed[2], precision)


class TestPlusCodes(unittest.TestCase):
    def test_full_plus_code_decodes_to_the_cell_centre(self):
        lat, lng = geo.decode_plus_code("8FVC2222+22")
        self.assertAlmostEqual(lat, 47.0000625, places=6)
        self.assertAlmostEqual(lng, 8.0000625, places=6)

    def test_full_plus_code_inside_a_url(self):
        parsed = geo.parse_maps_link("https://plus.codes/8FVC2222+22")
        self.assertIsNotNone(parsed)
        self.assertAlmostEqual(parsed[0], 47.0000625, places=5)
        self.assertEqual(parsed[2], geo.PRECISION_PLUS_CODE)

    def test_ten_digit_code_decodes(self):
        decoded = geo.decode_plus_code("8FVC9G8F+6X")
        self.assertIsNotNone(decoded)
        self.assertTrue(46.5 < decoded[0] < 48.0, decoded)
        self.assertTrue(7.5 < decoded[1] < 9.0, decoded)

    def test_eleven_digit_code_refines_into_the_grid_section(self):
        """Past ten digits the code switches to a 4x5 grid — a separate code path."""
        coarse = geo.decode_plus_code("8FVC9G8F+6X")
        fine = geo.decode_plus_code("8FVC9G8F+6XQ")
        self.assertIsNotNone(fine)
        # The refinement stays inside the cell the coarse code named (~14 m).
        self.assertLess(geo.haversine_m(coarse[0], coarse[1], fine[0], fine[1]), 30.0)

    def test_short_plus_code_is_rejected(self):
        """A short code needs a reference location we would have to geocode."""
        self.assertIsNone(geo.decode_plus_code("4CX2+M7"))
        self.assertIsNone(geo.parse_maps_link("https://maps.google.com/?q=4CX2%2BM7+Cairo"))

    def test_padded_plus_code_is_rejected(self):
        """'8FVC0000+' names an area kilometres across — useless as a door pin."""
        self.assertIsNone(geo.decode_plus_code("8FVC0000+"))

    def test_letters_outside_the_alphabet_are_rejected(self):
        self.assertIsNone(geo.decode_plus_code("8FVCAAAA+22"))


class TestRejections(unittest.TestCase):
    """Everything that must NOT produce a coordinate."""

    CASES = [
        ("empty", ""),
        ("none", None),
        ("whitespace", "   "),
        ("plain text", "come to the blue building next to the mosque"),
        ("bare place url", "https://www.google.com/maps/place/Cairo+Tower"),
        ("place name query", "https://maps.google.com/?q=Cairo+Tower"),
        ("latitude out of range", "https://www.google.com/maps/@95.000000,10.000000,17z"),
        ("longitude out of range", "https://maps.google.com/?q=10.000000,200.000000"),
        ("null island", "https://maps.google.com/?q=0.000000,0.000000"),
        ("only one number", "https://maps.google.com/?q=30.044400"),
        ("garbage", "!!!!3d!!!!4d"),
        ("html", "<script>alert('xss')</script>"),
        ("script in query", "https://maps.google.com/?q=<script>alert(1)</script>"),
        ("javascript scheme, no coords", "javascript:alert(document.cookie)"),
        ("sql-ish", "'; DROP TABLE tabAddress; --"),
        ("arabic place name", "https://maps.google.com/?q=القاهرة"),
        ("arabic-indic digits", "https://www.google.com/maps/@٣٠.٠٤٤,٣١.٢٣٥,17z"),
        ("emoji", "📍 https://example.com"),
        ("short link (needs expansion)", "https://maps.app.goo.gl/aBcDeF123"),
    ]

    def test_table(self):
        for label, url in self.CASES:
            with self.subTest(case=label):
                self.assertIsNone(geo.parse_maps_link(url), f"{label} must not parse")

    def test_a_parsed_result_is_always_plain_floats(self):
        """Structural XSS guard: nothing attacker-controlled can survive."""
        parsed = geo.parse_maps_link(
            "https://www.google.com/maps/place/<script>/@30.044400,31.235700,17z"
        )
        self.assertIsNotNone(parsed)
        self.assertIsInstance(parsed[0], float)
        self.assertIsInstance(parsed[1], float)
        self.assertIn(parsed[2], geo.PRECISION_ACCURACY_M)

    def test_very_long_input_does_not_hang(self):
        self.assertIsNone(geo.parse_maps_link("https://x/" + ("a" * 50000)))


class TestShortLinks(unittest.TestCase):
    def test_short_link_is_detected(self):
        for url in (
            "https://maps.app.goo.gl/aBcDeF123",
            "https://goo.gl/maps/xyz",
            "http://g.co/maps/abc",
        ):
            with self.subTest(url=url):
                self.assertTrue(geo.is_short_maps_link(url))

    def test_full_link_is_not_a_short_link(self):
        self.assertFalse(
            geo.is_short_maps_link("https://www.google.com/maps/@30.0,31.0,17z")
        )
        self.assertFalse(geo.is_short_maps_link(""))
        self.assertFalse(geo.is_short_maps_link("not a url"))

    def test_parser_never_expands_a_short_link_itself(self):
        """An inline HTTP call in a request path is a worker-blocking bug."""
        self.assertIsNone(geo.parse_maps_link("https://maps.app.goo.gl/aBcDeF123"))


class TestCoordinateValidation(unittest.TestCase):
    def test_valid(self):
        self.assertTrue(geo.is_valid_coordinate(30.0444, 31.2357))
        self.assertTrue(geo.is_valid_coordinate(-90, 180))

    def test_invalid(self):
        for lat, lng in (
            (91, 0),
            (-91, 0),
            (0, 181),
            (0, -181),
            (0, 0),
            ("abc", 1),
            (None, None),
            (float("nan"), 1),
            (float("inf"), 1),
        ):
            with self.subTest(lat=lat, lng=lng):
                self.assertFalse(geo.is_valid_coordinate(lat, lng))


class TestLocationLinkExtraction(unittest.TestCase):
    """``address_line2`` is READ here and never written — see the module docstring."""

    def test_extracts_the_url_from_the_legacy_format(self):
        self.assertEqual(
            geo.extract_location_link("Location: https://maps.app.goo.gl/xyz"),
            "https://maps.app.goo.gl/xyz",
        )

    def test_extracts_a_url_embedded_in_free_text(self):
        self.assertEqual(
            geo.extract_location_link("flat 3, Location: https://maps.google.com/?q=1,2 ."),
            "https://maps.google.com/?q=1,2",
        )

    def test_no_url_gives_empty_string(self):
        self.assertEqual(geo.extract_location_link("Apartment 4, second floor"), "")
        self.assertEqual(geo.extract_location_link(None), "")


class TestHaversine(unittest.TestCase):
    def test_zero_distance(self):
        self.assertAlmostEqual(geo.haversine_m(30.0, 31.0, 30.0, 31.0), 0.0, places=6)

    def test_one_degree_of_latitude_is_about_111km(self):
        distance = geo.haversine_m(30.0, 31.0, 31.0, 31.0)
        self.assertTrue(110_000 < distance < 112_000, distance)

    def test_known_pair_cairo_to_alexandria(self):
        distance = geo.haversine_m(30.0444, 31.2357, 31.2001, 29.9187)
        self.assertTrue(175_000 < distance < 185_000, distance)

    def test_symmetry(self):
        forward = geo.haversine_m(30.0, 31.0, 30.1, 31.1)
        backward = geo.haversine_m(30.1, 31.1, 30.0, 31.0)
        self.assertAlmostEqual(forward, backward, places=6)

    def test_short_distance_is_metre_scale(self):
        """~0.0001 degree of latitude is about 11 m — the door-pin scale."""
        distance = geo.haversine_m(30.0000, 31.0000, 30.0001, 31.0000)
        self.assertTrue(10.0 < distance < 12.0, distance)

    def test_invalid_input_raises_rather_than_returning_zero(self):
        """0.0 would read as 'delivered exactly on the pin'."""
        with self.assertRaises(ValueError):
            geo.haversine_m(999, 0, 1, 1)

    def test_soft_variant_returns_none(self):
        self.assertIsNone(geo.distance_m_or_none(None, None, 1, 1))
        self.assertIsNotNone(geo.distance_m_or_none(30.0, 31.0, 30.1, 31.1))

    def test_antipodal_is_half_the_circumference(self):
        distance = geo.haversine_m(0.0, 0.0000001, 0.0, 180.0)
        self.assertTrue(
            abs(distance - math.pi * geo.EARTH_RADIUS_M) < 1000, distance
        )


class TestPrecisionAccuracy(unittest.TestCase):
    def test_every_precision_label_has_an_accuracy(self):
        for label in (
            geo.PRECISION_PIN,
            geo.PRECISION_VIEWPORT,
            geo.PRECISION_QUERY,
            geo.PRECISION_PLUS_CODE,
        ):
            with self.subTest(label=label):
                self.assertIsNotNone(geo.accuracy_for_precision(label))

    def test_viewport_is_the_least_trusted(self):
        """A panned map centre must never read as tight as a real pin."""
        self.assertGreater(
            geo.PRECISION_ACCURACY_M[geo.PRECISION_VIEWPORT],
            geo.PRECISION_ACCURACY_M[geo.PRECISION_PIN],
        )

    def test_unknown_precision_falls_back(self):
        self.assertIsNone(geo.accuracy_for_precision("nonsense"))
        self.assertEqual(geo.accuracy_for_precision("nonsense", 42.0), 42.0)


if __name__ == "__main__":
    unittest.main()
