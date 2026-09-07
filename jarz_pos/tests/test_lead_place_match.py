"""Site-less checks for recognising a pasted link as a catalog place."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from jarz_pos.services import lead_place_match as match_mod


#: The real pair verified on 2026-09-07: opening the stored ?cid= URL landed on
#: a place URL whose FTID second word is that CID in hex.
REAL_CID = "8118497466869163611"
REAL_FTID_URL = (
    "https://www.google.com/maps/place/Cilantro/@30.0538921,31.2013923,17z/"
    "data=!3m1!4b1!4m6!3m5!1s0x1458413a6bccb49d:0x70aab26eb6a5265b"
    "!8m2!3d30.0538921!4d31.2013923!16s%2Fg%2F1tdtyp2w"
)


class TestIdentifiersInTheUrl(unittest.TestCase):
    def test_the_ftid_second_word_is_the_cid_the_sweep_stored(self):
        self.assertEqual(match_mod.cid_from_url(REAL_FTID_URL), REAL_CID)

    def test_the_stored_cid_url_form_is_read_directly(self):
        self.assertEqual(
            match_mod.cid_from_url("https://maps.google.com/?cid=" + REAL_CID),
            REAL_CID,
        )

    def test_a_percent_encoded_share_link_still_yields_its_cid(self):
        encoded = REAL_FTID_URL.replace("!1s", "%211s").replace(":", "%3A")
        self.assertEqual(match_mod.cid_from_url(encoded), REAL_CID)

    def test_a_link_with_no_place_identity_yields_nothing(self):
        for url in (
            "",
            None,
            "https://www.google.com/maps/@30.04,31.23,17z",
            "https://www.google.com/maps/place/Somewhere/data=!1s0x145:0x0",
        ):
            with self.subTest(url=url):
                self.assertEqual(match_mod.cid_from_url(url), "")

    def test_the_knowledge_graph_id_is_recovered_and_normalised(self):
        self.assertEqual(match_mod.mid_from_url(REAL_FTID_URL), "/g/1tdtyp2w")
        self.assertEqual(match_mod.mid_from_url("https://maps.google.com/?cid=1"), "")

    def test_names_fold_across_case_punctuation_and_arabic_diacritics(self):
        self.assertEqual(
            match_mod.normalise_name("  Costa   Coffee!  "),
            match_mod.normalise_name("costa coffee"),
        )
        self.assertEqual(
            match_mod.normalise_name("Café - Zamalek"),
            match_mod.normalise_name("cafe zamalek"),
        )
        self.assertEqual(match_mod.normalise_name(None), "")


def _index(leads=(), branches=(), points=()):
    return {
        "by_cid": {cid: value for cid, value in leads},
        "by_lead": {row["name"]: row for row in branches},
        "points": list(points),
    }


class TestMatching(unittest.TestCase):
    def setUp(self):
        self.lead = {
            "name": "LEAD-0007",
            "lead_name": "Cilantro",
            "phone": "+20 2 12345678",
            "website": "https://cilantro.com.eg",
            "custom_lead_category": "Coffee",
            "custom_fit_tier": "A",
            "custom_avg_rating": 4.3,
        }
        self.branch = {
            "parent": "LEAD-0007",
            "branch_name": "Cilantro Gameat El Dewal",
            "area": "Mohandessin",
            "region": "Cairo/Giza",
            "governorate": "Giza",
            "phone": "+20 2 87654321",
            "address": "Gameat El Dewal St, Giza",
            "hours": "07:00-01:00",
            "rating": 4.5,
            "reviews": 812,
        }

    def _match(self, index, **kwargs):
        with patch.object(match_mod, "index", return_value=index):
            return match_mod.match(**kwargs)

    def test_a_pasted_share_link_is_matched_to_the_swept_row_exactly(self):
        index = _index(leads=[(REAL_CID, {"lead": self.lead, "branch": self.branch})])

        result = self._match(index, url=REAL_FTID_URL)

        self.assertTrue(result["matched"])
        self.assertEqual(result["how"], "cid")
        self.assertEqual(result["confidence"], "exact")
        self.assertEqual(result["lead"], "LEAD-0007")
        self.assertEqual(result["branch_name"], "Cilantro Gameat El Dewal")

    def test_the_match_carries_everything_the_catalog_already_knows(self):
        index = _index(leads=[(REAL_CID, {"lead": self.lead, "branch": self.branch})])

        known = self._match(index, url=REAL_FTID_URL)["known"]

        # The branch is the more specific row and outranks the lead for the
        # fields both carry.
        self.assertEqual(known["phone"], "+20 2 87654321")
        self.assertEqual(known["primary_area"], "Mohandessin")
        self.assertEqual(known["opening_hours"], "07:00-01:00")
        self.assertEqual(known["rating"], 4.5)
        self.assertEqual(known["reviews"], 812)
        # And the lead supplies what only it has.
        self.assertEqual(known["website"], "https://cilantro.com.eg")
        self.assertEqual(known["category"], "Coffee")
        self.assertEqual(known["tier"], "A")
        self.assertNotIn("email", known, "blank fields are dropped, not sent as ''")

    def test_an_unknown_place_is_simply_not_a_match(self):
        index = _index(leads=[("999", {"lead": self.lead, "branch": None})])

        result = self._match(index, url=REAL_FTID_URL, latitude=30.05, longitude=31.20)

        self.assertFalse(result["matched"])
        self.assertEqual(result["known"], {})

    def test_the_same_name_at_the_same_door_matches_without_an_identifier(self):
        index = _index(
            branches=[self.lead],
            points=[(30.0538921, 31.2013923, "LEAD-0007",
                     match_mod.normalise_name("Cilantro"), self.branch)],
        )

        result = self._match(
            index,
            url="https://www.google.com/maps/place/Cilantro/@30.0539,31.2014,17z",
            latitude=30.0539,
            longitude=31.2014,
            place_name="Cilantro",
        )

        self.assertTrue(result["matched"])
        self.assertEqual(result["how"], "proximity")
        self.assertEqual(result["confidence"], "likely")
        self.assertLess(result["distance_m"], match_mod.SAME_PLACE_M)

    def test_the_same_brand_down_the_road_is_a_different_branch_not_a_duplicate(self):
        # 14 Costa Coffee doors must stay 14 branches. Name alone is not enough.
        index = _index(
            branches=[self.lead],
            points=[(30.0538921, 31.2013923, "LEAD-0007",
                     match_mod.normalise_name("Cilantro"), self.branch)],
        )

        result = self._match(
            index,
            url="https://www.google.com/maps/place/Cilantro/@30.07,31.22,17z",
            latitude=30.0700,
            longitude=31.2200,
            place_name="Cilantro",
        )

        self.assertFalse(result["matched"])

    def test_a_different_business_next_door_is_not_a_duplicate_either(self):
        # Distance alone is not enough: the cafe sharing the building is a lead
        # of its own.
        index = _index(
            branches=[self.lead],
            points=[(30.0538921, 31.2013923, "LEAD-0007",
                     match_mod.normalise_name("Cilantro"), self.branch)],
        )

        result = self._match(
            index,
            url="https://www.google.com/maps/place/Beans/@30.0539,31.2014,17z",
            latitude=30.0539,
            longitude=31.2014,
            place_name="Beans",
        )

        self.assertFalse(result["matched"])

    def test_coordinates_without_a_name_do_not_guess(self):
        index = _index(
            branches=[self.lead],
            points=[(30.0538921, 31.2013923, "LEAD-0007",
                     match_mod.normalise_name("Cilantro"), self.branch)],
        )

        result = self._match(index, latitude=30.0539, longitude=31.2014)

        self.assertFalse(result["matched"])

    def test_an_unreadable_catalog_reports_no_match_rather_than_raising(self):
        with patch.object(match_mod, "index", side_effect=RuntimeError("no site")):
            result = match_mod.match(url=REAL_FTID_URL)

        self.assertFalse(result["matched"])


class TestAMatchMustBeAbleToNameItsLead(unittest.TestCase):
    """A match nobody can name still prefills the form. That is the danger."""

    def test_an_orphaned_branch_row_is_not_reported_as_a_match(self):
        # The branch's parent Lead is gone, so `lead` would come back "". The
        # client suppresses the duplicate banner when it cannot name the lead,
        # while the caller still copies that row's phone and website into the
        # new form -- so the rep saves a NEW lead wearing another one's contacts.
        orphan = {"parent": "LEAD-GONE", "branch_name": "Ghost", "phone": "+20 1"}
        index = {
            "by_cid": {"42": {"lead": None, "branch": {"branch_name": "Ghost"}}},
            "by_lead": {},
            "points": [
                (30.05, 31.20, "", match_mod.normalise_name("Ghost"), orphan)
            ],
        }

        with patch.object(match_mod, "index", return_value=index):
            by_cid = match_mod.match(url="https://maps.google.com/?cid=42")
            by_name = match_mod.match(
                url="https://www.google.com/maps/place/Ghost/@30.05,31.20,17z",
                latitude=30.05,
                longitude=31.20,
                place_name="Ghost",
            )

        self.assertFalse(by_cid["matched"])
        self.assertEqual(by_cid["known"], {})
        self.assertFalse(by_name["matched"])
        self.assertEqual(by_name["known"], {})


class TestSchemaResilience(unittest.TestCase):
    def test_columns_are_filtered_against_the_live_schema_before_selecting(self):
        # Code deploys before `bench migrate` finishes. A SELECT naming a column
        # that does not exist yet fails wholesale, which would turn duplicate
        # detection off silently instead of degrading it.
        class _Meta:
            @staticmethod
            def get_field(name):
                return None if name.startswith("custom_") else object()

        with patch.object(match_mod.frappe, "get_meta", return_value=_Meta()):
            kept = match_mod._existing("Lead", match_mod._LEAD_FIELDS)

        self.assertIn("name", kept)
        self.assertIn("lead_name", kept)
        self.assertNotIn("custom_maps_url", kept)

    def test_unreadable_metadata_means_no_rows_rather_than_a_broken_query(self):
        # No schema, no column list, so nothing is selected at all -- reaching
        # the database with an empty column list would be a syntax error.
        with patch.object(match_mod.frappe, "get_meta", side_effect=RuntimeError):
            self.assertEqual(match_mod._existing("Lead", match_mod._LEAD_FIELDS), [])
            self.assertEqual(match_mod._lead_rows(), [])
            self.assertEqual(match_mod._branch_rows(), [])


if __name__ == "__main__":
    unittest.main()
