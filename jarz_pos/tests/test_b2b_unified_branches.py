"""One branch list per B2B account: delivery Addresses + Google Maps branches.

The account screen used to show a shop's doors twice -- once as the Customer's
delivery Addresses (with invoices) and once as the Lead's Google Maps rows
(with rating and pin). ``b2b_branches.unified_branches`` pairs them so each
door appears once. These tests pin the pairing rules: a hand link wins; a
dismissed row is never auto-paired; pins pair within 150 m; names and areas
pair only when unique in BOTH directions (a wrong guess would show one door's
rating against another door's debt). Pure unittest, no site.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import frappe

from jarz_pos.api import crm
from jarz_pos.services import b2b_branches as bb

_DB = MagicMock()


def _db():
    _DB.reset_mock(return_value=True, side_effect=True)
    return patch.object(bb.frappe, "db", _DB)


def _address(name, title=None, members=None, city=None, territory=None, lat=None, lng=None, invoices=0):
    return {
        "address_name": name,
        "branch_name": title or name,
        "address_line1": f"{name} street",
        "address_line2": None,
        "city": city,
        "phone": None,
        "territory": territory,
        "territory_missing": False,
        "is_primary_address": False,
        "latitude": lat,
        "longitude": lng,
        "member_address_names": members or [name],
        "invoice_count": invoices,
        "total_billed": 100.0 * invoices,
        "outstanding": 0.0,
        "last_order_date": "2026-09-01" if invoices else None,
    }


def _row(name, idx, branch_name=None, area=None, lat=None, lng=None, linked=None, dismissed=0, **extra):
    row = {
        "name": name,
        "idx": idx,
        "branch_name": branch_name,
        "area": area,
        "region": None,
        "governorate": None,
        "rating": 4.5,
        "reviews": 120,
        "maps_url": f"https://maps.example/{name}",
        "phone": "0100",
        "address": f"{name} maps address",
        "latitude": lat,
        "longitude": lng,
        "on_talabat": 1,
        "linked_address": linked,
        "match_dismissed": dismissed,
    }
    row.update(extra)
    return row


_TERRITORIES = [
    {"name": "EGMASRJD", "territory_name": "Heliopolis", "custom_territory_name_ar": "مصر الجديدة"},
    {"name": "EGOBOUR", "territory_name": "Obour", "custom_territory_name_ar": "العبور"},
    {"name": "EGZAYED", "territory_name": "Sheikh Zayed", "custom_territory_name_ar": "الشيخ زايد"},
]


def _territory_get_all(doctype, filters=None, **kwargs):
    if doctype != "Territory":
        return []
    wanted = set((filters or {}).get("name", ["in", []])[1])
    return [dict(t) for t in _TERRITORIES if t["name"] in wanted]


class _Base(unittest.TestCase):
    def setUp(self):
        p = _db()
        p.start()
        self.addCleanup(p.stop)
        _DB.exists.return_value = True
        _DB.get_value.return_value = {"lead_name": "Orbt", "company_name": None}
        _DB.has_column.return_value = True
        g = patch.object(bb.frappe, "get_all", side_effect=_territory_get_all)
        g.start()
        self.addCleanup(g.stop)
        le = patch.object(bb.frappe, "log_error")
        le.start()
        self.addCleanup(le.stop)

    def _unified(self, addresses, rows, customer="C-1", lead="L-1", unassigned=None):
        book = {"branches": addresses, "unassigned": unassigned}
        with patch.object(bb, "account_branches", return_value=book) as ab, patch.object(
            bb, "_lead_maps_rows", return_value=rows
        ):
            out = bb.unified_branches(customer, lead)
        self.account_branches = ab
        return out

    @staticmethod
    def _by_address(out):
        return {b["address_name"]: b for b in out["branches"] if b["source"] == "address"}

    @staticmethod
    def _maps_only(out):
        return [b for b in out["branches"] if b["source"] == "maps"]


class TestLinkedMatch(_Base):
    def test_hand_link_to_any_member_address_pairs_the_door(self):
        out = self._unified(
            [_address("A", members=["A", "A-DUP"]), _address("B")],
            [_row("r1", 1, "Somewhere else", linked="A-DUP")],
        )
        a = self._by_address(out)["A"]
        self.assertEqual(a["maps_match"], "linked")
        self.assertEqual(a["maps"]["row"], "r1")
        self.assertTrue(a["maps"]["on_talabat"])
        self.assertIsNone(self._by_address(out)["B"]["maps"])
        self.assertEqual(self._maps_only(out), [])

    def test_contested_link_first_row_wins_other_falls_through(self):
        out = self._unified(
            [_address("A", title="Heliopolis door"), _address("B", title="Zamalek")],
            [
                _row("r1", 1, "x", linked="A"),
                _row("r2", 2, "Zamalek", linked="A"),
            ],
        )
        by = self._by_address(out)
        self.assertEqual(by["A"]["maps"]["row"], "r1")
        # r2 falls through to auto matching and pairs by name.
        self.assertEqual(by["B"]["maps"]["row"], "r2")
        self.assertEqual(by["B"]["maps_match"], "auto")

    def test_every_address_branch_is_marked_as_address(self):
        out = self._unified([_address("A")], [])
        self.assertEqual(out["branches"][0]["source"], "address")
        self.assertIsNone(out["branches"][0]["maps"])
        self.assertIsNone(out["branches"][0]["maps_match"])


class TestPinMatch(_Base):
    def test_pins_within_150m_pair(self):
        out = self._unified(
            [_address("A", lat=30.0, lng=31.0)],
            [_row("r1", 1, "Different name", lat=30.0009, lng=31.0)],  # ~100 m
        )
        a = self._by_address(out)["A"]
        self.assertEqual(a["maps_match"], "auto")
        self.assertEqual(a["maps"]["row"], "r1")

    def test_pins_beyond_150m_do_not_pair(self):
        out = self._unified(
            [_address("A", lat=30.0, lng=31.0)],
            [_row("r1", 1, "Different name", lat=30.0018, lng=31.0)],  # ~200 m
        )
        self.assertIsNone(self._by_address(out)["A"]["maps"])
        self.assertEqual(len(self._maps_only(out)), 1)

    def test_nearest_row_wins_the_door(self):
        out = self._unified(
            [_address("A", lat=30.0, lng=31.0)],
            [
                _row("far", 1, "x", lat=30.0010, lng=31.0),
                _row("near", 2, "y", lat=30.0002, lng=31.0),
            ],
        )
        self.assertEqual(self._by_address(out)["A"]["maps"]["row"], "near")
        self.assertEqual([m["maps"]["row"] for m in self._maps_only(out)], ["far"])


class TestNameMatch(_Base):
    def test_unique_name_pairs_ignoring_case_and_punctuation(self):
        out = self._unified(
            [_address("A", title="Costa Zamalek"), _address("B", title="Costa Maadi")],
            [_row("r1", 1, "costa-zamalek")],
        )
        self.assertEqual(self._by_address(out)["A"]["maps"]["row"], "r1")
        self.assertIsNone(self._by_address(out)["B"]["maps"])

    def test_duplicate_name_on_maps_side_does_not_pair(self):
        out = self._unified(
            [_address("A", title="Orbt")],
            [_row("r1", 1, "Orbt"), _row("r2", 2, "ORBT")],
        )
        self.assertIsNone(self._by_address(out)["A"]["maps"])
        self.assertEqual(len(self._maps_only(out)), 2)

    def test_duplicate_name_on_address_side_does_not_pair(self):
        out = self._unified(
            [_address("A", title="Orbt"), _address("B", title="Orbt")],
            [_row("r1", 1, "Orbt")],
        )
        self.assertTrue(all(b["maps"] is None for b in self._by_address(out).values()))

    def test_dismissed_row_is_never_auto_matched(self):
        out = self._unified(
            [_address("A", title="Orbt", lat=30.0, lng=31.0)],
            [_row("r1", 1, "Orbt", lat=30.0, lng=31.0, dismissed=1)],
        )
        self.assertIsNone(self._by_address(out)["A"]["maps"])
        only = self._maps_only(out)
        self.assertEqual(len(only), 1)
        self.assertIsNone(only[0]["maps_match"])


class TestAreaMatch(_Base):
    def test_unique_area_pairs_via_city_halves_and_territory_labels(self):
        out = self._unified(
            [
                _address("HELIO", title="مصر الجديدة فرع", city="Heliopolis - مصر الجديده", territory="EGMASRJD"),
                _address("OBOUR", title="فرع العبور", city="العبور", territory="EGOBOUR"),
                _address("MAADI", title="المعادي", city="Maadi - المعادي"),
            ],
            [
                _row("r-helio", 1, "Orbt Heliopolis", area="Heliopolis"),
                _row("r-obour", 2, "Orbt Obour", area="Obour"),
                _row("r-maadi", 3, "Orbt Maadi", area=" maadi "),
            ],
        )
        by = self._by_address(out)
        self.assertEqual(by["HELIO"]["maps"]["row"], "r-helio")
        self.assertEqual(by["OBOUR"]["maps"]["row"], "r-obour")  # via territory_name
        self.assertEqual(by["MAADI"]["maps"]["row"], "r-maadi")
        self.assertTrue(all(b["maps_match"] == "auto" for b in by.values()))
        self.assertEqual(self._maps_only(out), [])

    def test_two_rows_in_one_area_stay_unpaired(self):
        # Cloud Nine: one Sheikh Zayed address, two Sheikh Zayed Maps rows.
        out = self._unified(
            [_address("CLOUD9", title="كلاود ناين", city="الشيخ زايد", territory="EGZAYED")],
            [
                _row("r1", 1, "Cloud Nine Arkan", area="Sheikh Zayed"),
                _row("r2", 2, "Cloud Nine Galleria", area="Sheikh Zayed"),
                _row("r3", 3, "Cloud Nine Maadi", area="Maadi"),
            ],
        )
        self.assertIsNone(self._by_address(out)["CLOUD9"]["maps"])
        self.assertEqual(len(self._maps_only(out)), 3)

    def test_region_also_counts_as_an_area(self):
        out = self._unified(
            [_address("A", title="x", city="Obour")],
            [_row("r1", 1, "y", area="Industrial Zone", region="Obour")],
        )
        self.assertEqual(self._by_address(out)["A"]["maps"]["row"], "r1")


class TestListShape(_Base):
    def test_unmatched_maps_rows_follow_with_zero_stats(self):
        out = self._unified(
            [_address("A", title="Delivery door", invoices=2)],
            [_row("r1", 1, "Unrelated", area="Zamalek", lat=29.0, lng=31.0)],
            unassigned={"invoice_count": 1},
        )
        self.assertEqual(out["unassigned"], {"invoice_count": 1})
        self.assertEqual(out["branches"][0]["address_name"], "A")
        extra = out["branches"][1]
        self.assertEqual(extra["source"], "maps")
        self.assertIsNone(extra["address_name"])
        self.assertEqual(extra["branch_name"], "Unrelated")
        self.assertEqual(extra["address_line1"], "r1 maps address")
        self.assertEqual(extra["city"], "Zamalek")
        self.assertEqual(extra["member_address_names"], [])
        self.assertEqual(
            (extra["invoice_count"], extra["total_billed"], extra["outstanding"], extra["last_order_date"]),
            (0, 0.0, 0.0, None),
        )
        self.assertEqual(extra["latitude"], 29.0)
        self.assertEqual(extra["maps"]["row"], "r1")
        self.assertIsNone(extra["maps_match"])

    def test_unnamed_maps_row_takes_the_lead_title(self):
        out = self._unified([], [_row("r1", 1, None)], customer=None)
        self.assertEqual(out["branches"][0]["branch_name"], "Orbt")

    def test_matched_address_without_pin_takes_the_maps_pin(self):
        out = self._unified(
            [_address("A", title="Orbt")],
            [_row("r1", 1, "Orbt", lat=30.1, lng=31.2)],
        )
        a = self._by_address(out)["A"]
        self.assertEqual((a["latitude"], a["longitude"]), (30.1, 31.2))

    def test_no_customer_gives_a_maps_only_list(self):
        out = self._unified([], [_row("r1", 1, "One"), _row("r2", 2, "Two")], customer=None)
        self.account_branches.assert_not_called()
        self.assertIsNone(out["unassigned"])
        self.assertEqual([b["source"] for b in out["branches"]], ["maps", "maps"])
        self.assertEqual([b["maps"]["row"] for b in out["branches"]], ["r1", "r2"])

    def test_branchless_lead_offers_its_own_location_as_one_virtual_row(self):
        lead_doc = {
            "name": "L-1",
            "lead_name": "Orbt speciality coffee",
            "custom_primary_area": "Obour",
            "custom_maps_url": "https://maps.app.goo.gl/x",
            "custom_latitude": 30.21,
            "custom_longitude": 31.47,
            "mobile_no": "0100",
            "custom_branches": [],
        }
        with patch.object(bb.frappe, "get_doc", return_value=lead_doc):
            out = self._unified([], [], customer=None)
        self.assertEqual(len(out["branches"]), 1)
        entry = out["branches"][0]
        self.assertEqual(entry["maps"]["row"], bb.SELF_BRANCH_ROW)
        self.assertEqual(entry["branch_name"], "Orbt speciality coffee")
        self.assertEqual(entry["latitude"], 30.21)

    def test_missing_lead_degrades_to_address_branches(self):
        _DB.exists.return_value = False
        out = self._unified([_address("A")], [_row("r1", 1, "A")])
        self.assertEqual([b["source"] for b in out["branches"]], ["address"])

    def test_unmigrated_table_is_read_without_the_new_columns(self):
        _DB.has_column.side_effect = lambda dt, f: f not in ("linked_address", "match_dismissed")
        captured = {}

        def get_all(doctype, **kwargs):
            captured["fields"] = kwargs.get("fields")
            return []

        with patch.object(bb.frappe, "get_all", side_effect=get_all):
            bb._lead_maps_rows("L-1")
        self.assertNotIn("linked_address", captured["fields"])
        self.assertIn("on_talabat", captured["fields"])

    def test_failing_maps_lookup_never_raises(self):
        with patch.object(bb.frappe, "get_all", side_effect=RuntimeError("no table")):
            self.assertEqual(bb._lead_maps_rows("L-1"), [])


class TestAccountResolution(_Base):
    def test_customer_with_two_live_leads_has_no_branch_lead(self):
        with patch.object(bb, "_leads_for_customer", return_value=["L-1", "L-2"]):
            self.assertIsNone(crm._branch_lead("Customer", "C-1"))
        with patch.object(bb, "_leads_for_customer", return_value=["L-1"]):
            self.assertEqual(crm._branch_lead("Customer", "C-1"), "L-1")

    def test_opportunity_from_lead_uses_that_lead(self):
        opp = {"opportunity_from": "Lead", "party_name": "L-7"}
        self.assertEqual(crm._branch_lead("Opportunity", "OPP-1", opp, None), "L-7")

    def test_opportunity_from_customer_uses_its_customers_lead(self):
        opp = {"opportunity_from": "Customer", "party_name": "C-1"}
        with patch.object(bb, "_leads_for_customer", return_value=["L-3"]):
            self.assertEqual(crm._branch_lead("Opportunity", "OPP-1", opp, "C-1"), "L-3")

    def test_attach_branches_without_customer_lists_maps_doors(self):
        result = {"recent_invoices": []}
        with patch.object(bb, "unified_branches", return_value={
            "branches": [{"source": "maps", "member_address_names": []}], "unassigned": None,
        }) as unified:
            crm._attach_branches(result, None, "L-1")
        unified.assert_called_once_with(None, "L-1")
        self.assertEqual(result["branches"][0]["source"], "maps")
        self.assertIsNone(result["unassigned_invoices"])


class TestLinkBranch(_Base):
    def setUp(self):
        super().setUp()
        self.set_calls = []
        _DB.set_value.side_effect = lambda *a, **k: self.set_calls.append(a)
        for patcher in (
            patch.object(crm.frappe, "get_roles", return_value=["B2B Sales Rep"]),
            patch.object(crm, "_doctype_exists", return_value=True),
            patch.object(crm, "_require_doc_permission"),
            patch.object(crm, "_resolve_lead_customer", return_value="C-1"),
            patch.object(bb, "unified_branches", return_value={"branches": [], "unassigned": None}),
            patch.object(
                bb, "customer_branches",
                return_value=[_address("A", members=["A", "A-DUP"]), _address("B")],
            ),
            patch(
                "jarz_pos.utils.customer_address_utils.get_linked_customer_address_names",
                return_value=["A", "A-DUP", "B"],
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        _DB.get_value.return_value = {
            "parenttype": "Lead", "parent": "L-1", "parentfield": "custom_branches",
        }

    def test_foreign_row_is_refused(self):
        _DB.get_value.return_value = {
            "parenttype": "Lead", "parent": "L-OTHER", "parentfield": "custom_branches",
        }
        with self.assertRaises(frappe.ValidationError):
            crm.link_branch("Lead", "L-1", "r-foreign", "A")
        self.assertEqual(self.set_calls, [])

    def test_foreign_address_is_refused_before_any_write(self):
        with self.assertRaises(frappe.ValidationError):
            crm.link_branch("Lead", "L-1", "r1", "SOMEONE-ELSES")
        self.assertEqual(self.set_calls, [])

    def test_link_without_customer_is_refused(self):
        with patch.object(crm, "_resolve_lead_customer", return_value=None):
            with self.assertRaises(frappe.ValidationError):
                crm.link_branch("Lead", "L-1", "r1", "A")
        self.assertEqual(self.set_calls, [])

    def test_unmigrated_site_is_refused(self):
        _DB.has_column.return_value = False
        with self.assertRaises(frappe.ValidationError):
            crm.link_branch("Lead", "L-1", "r1", None)

    def test_link_sets_address_and_clears_other_rows_on_the_same_door(self):
        seen = {}

        def get_all(doctype, filters=None, **kwargs):
            seen["filters"] = filters
            return ["r-old"]

        with patch.object(crm.frappe, "get_all", side_effect=get_all):
            out = crm.link_branch("Lead", "L-1", "r1", "A")
        self.assertEqual(seen["filters"]["linked_address"], ["in", ["A", "A-DUP"]])
        self.assertEqual(seen["filters"]["parent"], "L-1")
        self.assertIn(("Jarz Lead Branch", "r-old", "linked_address", None), self.set_calls)
        self.assertIn(
            ("Jarz Lead Branch", "r1", {"linked_address": "A", "match_dismissed": 0}),
            self.set_calls,
        )
        bb.unified_branches.assert_called_once_with("C-1", "L-1")
        self.assertEqual(out, {"branches": [], "unassigned": None})

    def test_unlink_sets_match_dismissed(self):
        crm.link_branch("Lead", "L-1", "r1", "")
        self.assertEqual(
            self.set_calls,
            [("Jarz Lead Branch", "r1", {"linked_address": None, "match_dismissed": 1})],
        )

    def test_self_row_is_materialized_then_linked(self):
        lead_doc = MagicMock()
        lead_doc.get.side_effect = lambda f, d=None: [] if f == "custom_branches" else d
        new_row = MagicMock()
        new_row.name = "row-new"
        lead_doc.append.return_value = new_row
        with patch.object(crm.frappe, "get_doc", return_value=lead_doc), patch(
            "jarz_pos.api.leads._lead_self_branch", return_value={"branch_name": "Orbt", "latitude": 30.1}
        ), patch.object(crm.frappe, "get_all", return_value=[]):
            crm.link_branch("Lead", "L-1", bb.SELF_BRANCH_ROW, "B")
        lead_doc.append.assert_called_once_with("custom_branches", {"branch_name": "Orbt", "latitude": 30.1})
        lead_doc.save.assert_called_once()
        self.assertIn(
            ("Jarz Lead Branch", "row-new", {"linked_address": "B", "match_dismissed": 0}),
            self.set_calls,
        )

    def test_account_without_a_lead_is_refused(self):
        with patch.object(bb, "_leads_for_customer", return_value=[]):
            with self.assertRaises(frappe.ValidationError):
                crm.link_branch("Customer", "C-1", "r1", "A")


if __name__ == "__main__":
    unittest.main()
