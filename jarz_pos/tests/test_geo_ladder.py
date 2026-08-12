"""The confidence ladder: full never-downgrade matrix.

A pin has one job — to be the best answer anyone has produced so far. That only
holds if a worse answer can never overwrite a better one, and the ways that rule
breaks are specific:

**Alphabetical ordering inverts it.** ``"courier_verified" < "customer_pin"``
and ``"pos_link" > "manual_override"`` as strings. A ``>=`` on the source *label*
would let a routine Woo re-sync (``customer_pin``) stamp over a pin two couriers
physically confirmed, and would block a manager's deliberate correction. Two of
the five sources invert — and both silently. The ranks exist for exactly this,
and :class:`TestStringComparisonWouldInvert` pins the failure mode down so the
comment can never become folklore.

**A rejected write must be a no-op, not an error.** WooCommerce re-syncs an
address on every order change. If a lower-confidence write raised, a customer
whose door a courier had already verified would start failing order sync — the
better the data, the more broken the system.

**Equal ranks are accepted.** A fresher pin of the same class is a better pin;
refusing it would freeze the first courier's GPS noise in place forever.

Pure ``unittest`` with mocks — no site.
"""

import itertools
import unittest
from unittest.mock import patch

from jarz_pos.utils import geo

ALL_SOURCES = [
    "territory_centroid",
    "pos_link",
    "customer_pin",
    "courier_web",
    "courier_verified",
    "manual_override",
]


class TestRanksAreTheContractedValues(unittest.TestCase):
    def test_exact_ranks(self):
        self.assertEqual(
            geo.CONFIDENCE_RANK,
            {
                "territory_centroid": 10,
                "pos_link": 20,
                "customer_pin": 30,
                "courier_web": 35,
                "courier_verified": 40,
                "manual_override": 50,
            },
        )

    def test_ranks_are_strictly_increasing_in_the_documented_order(self):
        ranks = [geo.CONFIDENCE_RANK[s] for s in ALL_SOURCES]
        self.assertEqual(ranks, sorted(ranks))
        self.assertEqual(len(set(ranks)), len(ranks), "ranks must be unique")

    def test_manual_override_is_the_ceiling(self):
        """An authorised human with context must be able to beat consensus."""
        self.assertEqual(
            max(geo.CONFIDENCE_RANK, key=geo.CONFIDENCE_RANK.get), "manual_override"
        )

    def test_territory_centroid_is_the_floor(self):
        self.assertEqual(
            min(geo.CONFIDENCE_RANK, key=geo.CONFIDENCE_RANK.get), "territory_centroid"
        )

    def test_unknown_and_empty_rank_zero(self):
        """A first write must always land, and an unrecognised label must not
        be able to abort a write path by raising."""
        for value in ("", None, "nonsense", "COURIER_VERIFIED_TYPO", 0, []):
            with self.subTest(value=value):
                self.assertEqual(geo.confidence_rank(value), geo.UNKNOWN_RANK)

    def test_case_and_whitespace_are_normalised(self):
        self.assertEqual(geo.confidence_rank("  Courier_Verified  "), 40)
        self.assertEqual(geo.normalize_source("POS_LINK"), "pos_link")


class TestStringComparisonWouldInvert(unittest.TestCase):
    """Documents, in executable form, why ranks are not optional."""

    def test_courier_verified_sorts_below_customer_pin_as_a_string(self):
        self.assertLess("courier_verified", "customer_pin")
        self.assertGreater(
            geo.CONFIDENCE_RANK["courier_verified"], geo.CONFIDENCE_RANK["customer_pin"]
        )

    def test_pos_link_sorts_above_manual_override_as_a_string(self):
        self.assertGreater("pos_link", "manual_override")
        self.assertLess(
            geo.CONFIDENCE_RANK["pos_link"], geo.CONFIDENCE_RANK["manual_override"]
        )

    def test_the_ladder_disagrees_with_alphabetical_order(self):
        by_rank = sorted(ALL_SOURCES, key=geo.CONFIDENCE_RANK.get)
        self.assertNotEqual(by_rank, sorted(ALL_SOURCES))



class TestCourierWebSitsBetween(unittest.TestCase):
    """Rank 35 is the containment for a capture with no mock-GPS evidence.

    `geolocator_web` exposes no `isMocked` and iOS has no mock-provider signal at
    all, so a web capture is unverifiable by construction. It still beats a
    customer's remote pin — the courier is physically at the door — but a real
    Android capture must always be able to overwrite it.
    """

    def test_web_beats_customer_pin(self):
        self.assertTrue(geo.accepts_write("customer_pin", "courier_web"))

    def test_verified_android_overwrites_web(self):
        self.assertTrue(geo.accepts_write("courier_web", "courier_verified"))

    def test_web_cannot_overwrite_verified_android(self):
        self.assertFalse(geo.accepts_write("courier_verified", "courier_web"))

    def test_web_beats_a_pos_link_and_an_empty_address(self):
        self.assertTrue(geo.accepts_write("pos_link", "courier_web"))
        self.assertTrue(geo.accepts_write("", "courier_web"))

    def test_manual_override_still_wins(self):
        self.assertTrue(geo.accepts_write("courier_web", "manual_override"))
        self.assertFalse(geo.accepts_write("manual_override", "courier_web"))

class TestNeverDowngradeMatrix(unittest.TestCase):
    """Every (current, incoming) pair, including the empty state."""

    def test_full_matrix(self):
        states = [""] + ALL_SOURCES
        for current, incoming in itertools.product(states, states):
            with self.subTest(current=current or "<none>", incoming=incoming or "<none>"):
                expected = geo.confidence_rank(incoming) >= geo.confidence_rank(current)
                self.assertEqual(geo.accepts_write(current, incoming), expected)

    def test_every_source_beats_an_empty_address(self):
        for source in ALL_SOURCES:
            with self.subTest(source=source):
                self.assertTrue(geo.accepts_write("", source))

    def test_equal_ranks_are_accepted(self):
        """A fresher pin of the same class wins — GPS noise must not be frozen in."""
        for source in ALL_SOURCES:
            with self.subTest(source=source):
                self.assertTrue(geo.accepts_write(source, source))

    def test_strict_downgrades_are_rejected(self):
        for i, current in enumerate(ALL_SOURCES):
            for incoming in ALL_SOURCES[:i]:
                with self.subTest(current=current, incoming=incoming):
                    self.assertFalse(geo.accepts_write(current, incoming))

    def test_strict_upgrades_are_accepted(self):
        for i, current in enumerate(ALL_SOURCES):
            for incoming in ALL_SOURCES[i + 1 :]:
                with self.subTest(current=current, incoming=incoming):
                    self.assertTrue(geo.accepts_write(current, incoming))

    def test_woo_resync_cannot_overwrite_a_courier_verified_pin(self):
        """The concrete case: Woo passthrough writes customer_pin on every order."""
        self.assertFalse(geo.accepts_write("courier_verified", "customer_pin"))

    def test_manager_override_beats_courier_consensus(self):
        self.assertTrue(geo.accepts_write("courier_verified", "manual_override"))

    def test_unknown_incoming_never_wins(self):
        for source in ALL_SOURCES:
            with self.subTest(source=source):
                self.assertFalse(geo.accepts_write(source, "who_knows"))


class TestClampConfidence(unittest.TestCase):
    def test_returns_the_incoming_pair_when_accepted(self):
        self.assertEqual(
            geo.clamp_confidence("pos_link", "courier_verified"), ("courier_verified", 40)
        )

    def test_returns_the_current_pair_when_rejected(self):
        self.assertEqual(
            geo.clamp_confidence("courier_verified", "pos_link"), ("courier_verified", 40)
        )

    def test_equal_rank_returns_the_incoming_pair(self):
        self.assertEqual(geo.clamp_confidence("pos_link", "pos_link"), ("pos_link", 20))

    def test_empty_current_takes_the_incoming(self):
        self.assertEqual(geo.clamp_confidence("", "territory_centroid"), ("territory_centroid", 10))

    def test_unknown_incoming_leaves_the_current_pair_intact(self):
        self.assertEqual(geo.clamp_confidence("customer_pin", "junk"), ("customer_pin", 30))

    def test_source_and_rank_never_disagree(self):
        """The invariant Address.custom_geo_source / custom_geo_confidence rely on."""
        states = [""] + ALL_SOURCES + ["junk"]
        for current, incoming in itertools.product(states, states):
            source, rank = geo.clamp_confidence(current, incoming)
            with self.subTest(current=current, incoming=incoming):
                self.assertEqual(rank, geo.confidence_rank(source))


class TestServiceEnforcesTheLadder(unittest.TestCase):
    """``services.geo_resolution`` is the sole writer; it must apply the rule."""

    def _evaluate(self, current_source, incoming_source, **kwargs):
        from jarz_pos.services import geo_resolution

        current = {
            "custom_geo_source": current_source,
            "custom_latitude": 30.0,
            "custom_longitude": 31.0,
        }
        with patch.object(geo_resolution, "get_address_geo", return_value=current):
            return geo_resolution.evaluate_pin_write(
                "ADDR-0001",
                latitude=kwargs.get("latitude", 30.05),
                longitude=kwargs.get("longitude", 31.05),
                source=incoming_source,
            )

    def test_upgrade_is_accepted(self):
        result = self._evaluate("pos_link", "courier_verified")
        self.assertTrue(result["accepted"])
        self.assertEqual(result["resulting_source"], "courier_verified")
        self.assertEqual(result["resulting_rank"], 40)

    def test_downgrade_is_a_silent_no_op_not_an_error(self):
        result = self._evaluate("courier_verified", "pos_link")
        self.assertFalse(result["accepted"])
        self.assertEqual(result["reason"], "lower_confidence")
        self.assertEqual(result["resulting_source"], "courier_verified")

    def test_equal_rank_is_accepted(self):
        self.assertTrue(self._evaluate("customer_pin", "customer_pin")["accepted"])

    def test_invalid_coordinates_are_rejected_before_the_ladder(self):
        result = self._evaluate("", "courier_verified", latitude=0, longitude=0)
        self.assertFalse(result["accepted"])
        self.assertEqual(result["reason"], "invalid_coordinates")

    def test_unknown_source_is_rejected(self):
        result = self._evaluate("", "made_up_source")
        self.assertFalse(result["accepted"])
        self.assertEqual(result["reason"], "unknown_source")

    def test_missing_address_is_rejected(self):
        from jarz_pos.services import geo_resolution

        result = geo_resolution.evaluate_pin_write(
            "", latitude=30.0, longitude=31.0, source="pos_link"
        )
        self.assertFalse(result["accepted"])
        self.assertEqual(result["reason"], "missing_address")

    def test_movement_distance_is_reported(self):
        result = self._evaluate("pos_link", "courier_verified")
        self.assertIsNotNone(result["moved_m"])
        self.assertGreater(result["moved_m"], 0)


class TestBeforeSaveClamp(unittest.TestCase):
    """``events.address.clamp_geo_confidence`` — zero queries, never raises."""

    class _Doc:
        def __init__(self, **data):
            self._data = data
            for key, value in data.items():
                setattr(self, key, value)

        def get(self, key, default=None):
            return getattr(self, key, default)

    def _clamp(self, **data):
        from jarz_pos.events import address as address_events

        doc = self._Doc(**data)
        address_events.clamp_geo_confidence(doc)
        return doc

    def test_confidence_is_forced_to_the_rank_of_the_source(self):
        doc = self._clamp(
            custom_geo_source="courier_verified",
            custom_geo_confidence=1,
            custom_latitude=30.0,
            custom_longitude=31.0,
        )
        self.assertEqual(doc.custom_geo_confidence, 40)

    def test_a_hand_typed_rank_cannot_defeat_the_ladder(self):
        """Typing 99 into the form must not outrank a courier-verified pin."""
        doc = self._clamp(
            custom_geo_source="pos_link",
            custom_geo_confidence=99,
            custom_latitude=30.0,
            custom_longitude=31.0,
        )
        self.assertEqual(doc.custom_geo_confidence, 20)

    def test_unknown_source_is_cleared(self):
        doc = self._clamp(
            custom_geo_source="something_else",
            custom_geo_confidence=50,
            custom_latitude=30.0,
            custom_longitude=31.0,
        )
        self.assertIsNone(doc.custom_geo_source)
        self.assertEqual(doc.custom_geo_confidence, 0)

    def test_a_blank_pin_clears_the_source_and_the_rank(self):
        doc = self._clamp(
            custom_geo_source="courier_verified",
            custom_geo_confidence=40,
            custom_latitude=None,
            custom_longitude=None,
        )
        self.assertIsNone(doc.custom_geo_source)
        self.assertEqual(doc.custom_geo_confidence, 0)

    def test_null_island_counts_as_no_pin(self):
        doc = self._clamp(
            custom_geo_source="pos_link",
            custom_geo_confidence=20,
            custom_latitude=0.0,
            custom_longitude=0.0,
        )
        self.assertEqual(doc.custom_geo_confidence, 0)

    def test_an_address_with_no_geo_fields_is_untouched(self):
        """The hook fires on every Address save site-wide, including Woo's bulk
        sync — it must fast-exit on documents that carry no geo data at all."""
        doc = self._clamp(address_line1="12 Somewhere St")
        self.assertFalse(hasattr(doc, "custom_geo_confidence"))

    def test_it_never_touches_the_woo_trigger_fields(self):
        doc = self._clamp(
            custom_geo_source="pos_link",
            custom_geo_confidence=0,
            custom_latitude=30.0,
            custom_longitude=31.0,
            address_line1="12 Somewhere St",
            address_line2="Location: https://maps.app.goo.gl/xyz",
            city="Nasr City",
            phone="0100",
            email_id="a@b.com",
            address_type="Shipping",
            is_shipping_address=1,
        )
        self.assertEqual(doc.address_line1, "12 Somewhere St")
        self.assertEqual(doc.address_line2, "Location: https://maps.app.goo.gl/xyz")
        self.assertEqual(doc.city, "Nasr City")
        self.assertEqual(doc.phone, "0100")
        self.assertEqual(doc.email_id, "a@b.com")
        self.assertEqual(doc.address_type, "Shipping")
        self.assertEqual(doc.is_shipping_address, 1)

    def test_it_never_raises(self):
        """A raise here would abort the save of a document this app does not own."""
        from jarz_pos.events import address as address_events

        for bad in (None, object(), "a string", 42):
            with self.subTest(doc=type(bad).__name__):
                address_events.clamp_geo_confidence(bad)  # must not raise


class TestGeoResolutionRefusesWooTriggerFields(unittest.TestCase):
    def test_guard_rejects_address_line2(self):
        from jarz_pos.services import geo_resolution

        with self.assertRaises(Exception):
            geo_resolution._assert_no_woo_trigger_fields(
                {"custom_latitude": 1.0, "address_line2": "anything"}
            )

    def test_guard_allows_a_pure_geo_update(self):
        from jarz_pos.services import geo_resolution

        geo_resolution._assert_no_woo_trigger_fields(
            {field: None for field in geo_resolution.GEO_FIELDS}
        )

    def test_the_owned_field_set_is_exactly_the_contracted_six(self):
        from jarz_pos.services import geo_resolution

        self.assertEqual(
            set(geo_resolution.GEO_FIELDS),
            {
                "custom_latitude",
                "custom_longitude",
                "custom_geo_source",
                "custom_geo_confidence",
                "custom_geo_accuracy_m",
                "custom_geo_verified_on",
            },
        )

    def test_no_geo_field_is_in_the_woo_trigger_set(self):
        from jarz_pos.services import geo_resolution

        self.assertEqual(
            set(geo_resolution.GEO_FIELDS) & geo_resolution.WOO_TRIGGER_FIELDS, set()
        )


if __name__ == "__main__":
    unittest.main()
