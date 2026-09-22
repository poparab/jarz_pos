"""B2B branches: per-branch invoice separation and merge-as-branch (pure unittest).

A B2B shop is one Customer with several doors. These tests pin the two things
the owner asked for:

* each branch shows ITS invoices and ITS outstanding, grouped through the same
  ``member_address_names`` the order flow uses (so a legacy duplicate Address
  row for one door still counts for that door), with anything unattributable
  reported as "unassigned" rather than guessed onto a branch;
* one account can be folded into another as a branch -- a customer merge that
  must move every invoice and payment without changing a single total, and a
  catalog merge that keeps each lead's own map pin as a branch.
"""

from __future__ import annotations

import unittest
from collections import defaultdict
from unittest.mock import MagicMock, patch

import frappe

from jarz_pos.api import crm, leads
from jarz_pos.services import b2b_branches as bb

# frappe.db is an unbound proxy in a site-less run; every test that touches it
# swaps the whole handle for this mock (attributes patched per test).
_DB = MagicMock()


def _db():
    _DB.reset_mock(return_value=True, side_effect=True)
    return patch.object(bb.frappe, "db", _DB)


def _branch(name, members=None, title=None):
    return {
        "address_name": name,
        "branch_name": title or name,
        "member_address_names": members or [name],
    }


def _inv(name, address, total, outstanding=0, date="2026-09-01", legacy=False):
    return {
        "name": name,
        "posting_date": date,
        "grand_total": total,
        "outstanding_amount": outstanding,
        "status": "Paid" if not outstanding else "Unpaid",
        "is_return": 0,
        "shipping_address_name": None if legacy else address,
        "customer_address": address if legacy else None,
    }


class TestBranchSeparation(unittest.TestCase):
    BRANCHES = [
        _branch("ILO-HELIO", ["ILO-HELIO", "ILO-HELIO-DUP"], "Heliopolis"),
        _branch("ILO-MADINATY", title="Madinaty"),
    ]
    INVOICES = [
        _inv("SI-1", "ILO-HELIO", 736, 736, "2026-09-20"),
        _inv("SI-2", "ILO-MADINATY", 920, 920, "2026-09-19"),
        _inv("SI-3", "ILO-HELIO-DUP", 2560, 0, "2026-08-14"),  # legacy duplicate row
        _inv("SI-4", "ILO-MADINATY", 920, 0, "2026-09-08", legacy=True),  # customer_address only
        _inv("SI-5", None, 100, 0, "2026-07-01"),  # no address at all
        _inv("SI-6", "SOMEONE-ELSES", 50, 50, "2026-07-02"),  # address not on this customer
    ]

    @staticmethod
    def _groups(invoices):
        """What the GROUP BY query returns for these invoices."""
        groups = defaultdict(lambda: {"invoice_count": 0, "total_billed": 0, "outstanding": 0, "last_order_date": None})
        for inv in invoices:
            key = inv.get("shipping_address_name") or inv.get("customer_address")
            g = groups[key]
            g["address"] = key
            g["invoice_count"] += 1
            g["total_billed"] += inv["grand_total"]
            g["outstanding"] += inv["outstanding_amount"]
            g["last_order_date"] = max(filter(None, [g["last_order_date"], inv["posting_date"]]))
        return list(groups.values())

    def _patched(self, invoices=None):
        invoices = list(self.INVOICES if invoices is None else invoices)
        return (
            patch.object(bb, "customer_branches", return_value=[dict(b) for b in self.BRANCHES]),
            patch.object(bb, "_submitted_invoices", return_value=invoices),
            patch.object(bb, "_invoice_totals_by_address", return_value=self._groups(invoices)),
        )

    def test_each_branch_carries_its_own_invoices_and_balance(self):
        p1, p2, p3 = self._patched()
        with p1, p2, p3:
            book = bb.account_branches("ilo")
        by_name = {b["address_name"]: b for b in book["branches"]}
        helio, madinaty = by_name["ILO-HELIO"], by_name["ILO-MADINATY"]
        self.assertEqual(helio["invoice_count"], 2)
        self.assertEqual(helio["total_billed"], 3296.0)
        self.assertEqual(helio["outstanding"], 736.0)
        self.assertEqual(helio["last_order_date"], "2026-09-20")
        self.assertEqual(madinaty["invoice_count"], 2)
        self.assertEqual(madinaty["outstanding"], 920.0)

    def test_unattributable_invoices_are_reported_not_guessed(self):
        p1, p2, p3 = self._patched()
        with p1, p2, p3:
            book = bb.account_branches("ilo")
        self.assertEqual(book["unassigned"]["invoice_count"], 2)
        self.assertEqual(book["unassigned"]["outstanding"], 50.0)
        total = sum(b["invoice_count"] for b in book["branches"]) + book["unassigned"]["invoice_count"]
        self.assertEqual(total, len(self.INVOICES))

    def test_no_unassigned_key_when_everything_is_attributed(self):
        p1, p2, p3 = self._patched(self.INVOICES[:4])
        with p1, p2, p3:
            self.assertIsNone(bb.account_branches("ilo")["unassigned"])

    def test_branch_filter_lists_only_that_door(self):
        p1, p2, p3 = self._patched()
        with p1, p2, p3, patch("jarz_pos.utils.invoice_utils.normalize_woo_order_id", side_effect=lambda v: v):
            out = bb.account_invoices("ilo", branch="ILO-MADINATY")
        self.assertEqual([i["name"] for i in out["invoices"]], ["SI-2", "SI-4"])
        self.assertTrue(all(i["branch_name"] == "Madinaty" for i in out["invoices"]))
        self.assertEqual(out["summary"]["outstanding"], 920.0)

    def test_unassigned_filter(self):
        p1, p2, p3 = self._patched()
        with p1, p2, p3, patch("jarz_pos.utils.invoice_utils.normalize_woo_order_id", side_effect=lambda v: v):
            out = bb.account_invoices("ilo", branch=bb.UNASSIGNED_BRANCH)
        self.assertEqual([i["name"] for i in out["invoices"]], ["SI-5", "SI-6"])
        self.assertIsNone(out["invoices"][0]["branch_address"])

    def test_summary_is_the_whole_branch_not_the_page(self):
        p1, p2, p3 = self._patched()
        with p1, p2, p3, patch("jarz_pos.utils.invoice_utils.normalize_woo_order_id", side_effect=lambda v: v):
            out = bb.account_invoices("ilo", limit=2)
        self.assertEqual(len(out["invoices"]), 2)
        self.assertTrue(out["truncated"])
        self.assertEqual(out["summary"]["invoice_count"], 6)

    def test_foreign_branch_is_refused(self):
        p1, p2, p3 = self._patched()
        with p1, p2, p3:
            with self.assertRaises(frappe.ValidationError):
                bb.account_invoices("ilo", branch="NOT-THIS-CUSTOMERS")


class TestMergePlan(unittest.TestCase):
    @staticmethod
    def _party(doctype, name, lead=None, customer=None):
        return {"doctype": doctype, "name": name, "title": name, "lead": lead, "customer": customer}

    def test_two_customers_merge_and_need_a_manager(self):
        plan = bb.build_plan(
            self._party("Customer", "ORBT", customer="ORBT"),
            self._party("Lead", "L-1311", lead="L-1311", customer="ORBT-1"),
        )
        self.assertEqual(plan["customer_action"], "merge_customers")
        self.assertIsNone(plan["lead_action"])
        self.assertTrue(plan["requires_manager"])
        self.assertEqual(plan["final_customer"], "ORBT-1")

    def test_two_catalog_leads_fold_without_a_manager(self):
        plan = bb.build_plan(
            self._party("Lead", "L-1", lead="L-1"),
            self._party("Lead", "L-2", lead="L-2"),
        )
        self.assertIsNone(plan["customer_action"])
        self.assertEqual(plan["lead_action"], "merge_leads")
        self.assertFalse(plan["requires_manager"])

    def test_target_without_customer_adopts_the_sources(self):
        plan = bb.build_plan(
            self._party("Lead", "L-1", lead="L-1", customer="C-1"),
            self._party("Lead", "L-2", lead="L-2"),
        )
        self.assertEqual(plan["customer_action"], "adopt_source_customer")
        self.assertEqual(plan["final_customer"], "C-1")
        self.assertFalse(plan["requires_manager"])

    def test_lead_into_bare_customer_links_its_addresses_and_follows(self):
        plan = bb.build_plan(
            self._party("Lead", "L-1", lead="L-1"),
            self._party("Customer", "C-9", customer="C-9"),
        )
        self.assertEqual(plan["customer_action"], "link_lead_addresses")
        self.assertEqual(plan["lead_action"], "relink_source_lead")

    def test_same_account_is_refused(self):
        with self.assertRaises(frappe.ValidationError):
            bb.build_plan(
                self._party("Lead", "L-1", lead="L-1", customer="C-1"),
                self._party("Customer", "C-1", lead="L-1", customer="C-1"),
            )
        with self.assertRaises(frappe.ValidationError):
            bb.build_plan(
                self._party("Customer", "C-1", customer="C-1"),
                self._party("Customer", "C-1", customer="C-1"),
            )

    def test_two_customers_sharing_one_lead_can_still_merge(self):
        plan = bb.build_plan(
            self._party("Customer", "ORBT", lead="L-1", customer="ORBT"),
            self._party("Customer", "ORBT-1", lead="L-1", customer="ORBT-1"),
        )
        self.assertEqual(plan["customer_action"], "merge_customers")
        self.assertIsNone(plan["lead_action"])

    def test_two_leads_on_one_customer_only_fold_the_leads(self):
        plan = bb.build_plan(
            self._party("Lead", "L-1", lead="L-1", customer="C-1"),
            self._party("Lead", "L-2", lead="L-2", customer="C-1"),
        )
        self.assertIsNone(plan["customer_action"])
        self.assertEqual(plan["lead_action"], "merge_leads")


class TestMergeExecution(unittest.TestCase):
    def setUp(self):
        bb.frappe.flags.ignore_woo_outbound = False
        self._db_patch = _db()
        self._db_patch.start()
        self.addCleanup(self._db_patch.stop)

    def test_rep_cannot_merge_two_customers(self):
        source = {"doctype": "Customer", "name": "A", "lead": None, "customer": "A"}
        target = {"doctype": "Customer", "name": "B", "lead": None, "customer": "B"}
        with patch.object(bb.frappe, "get_roles", return_value=["B2B Sales Rep"]), patch.object(
            bb, "merge_customers"
        ) as merge:
            with self.assertRaises(frappe.ValidationError):
                bb.execute(source, target)
        merge.assert_not_called()

    def test_manager_customer_merge_suppresses_woo_and_restores_flag(self):
        source = {"doctype": "Customer", "name": "A", "lead": None, "customer": "A"}
        target = {"doctype": "Customer", "name": "B", "lead": None, "customer": "B"}
        seen = {}

        def fake_merge(s, t, branch_name):
            seen["flag"] = bb.frappe.flags.ignore_woo_outbound
            seen["args"] = (s, t, branch_name)
            return {"stamped_invoices": 0, "credit_terms_carried": False, "totals": {}}

        with patch.object(bb.frappe, "get_roles", return_value=["JARZ Manager"]), patch.object(
            bb, "merge_customers", side_effect=fake_merge
        ), patch.object(_DB, "count", return_value=3):
            result = bb.execute(source, target, branch_name="Hayat Town Mall")
        self.assertTrue(seen["flag"])
        self.assertEqual(seen["args"], ("A", "B", "Hayat Town Mall"))
        self.assertFalse(bb.frappe.flags.ignore_woo_outbound)
        self.assertEqual(result["merged_customer"], "A")
        self.assertEqual(result["customer"], "B")

    def test_flag_restored_when_merge_fails(self):
        source = {"doctype": "Customer", "name": "A", "lead": None, "customer": "A"}
        target = {"doctype": "Customer", "name": "B", "lead": None, "customer": "B"}
        with patch.object(bb.frappe, "get_roles", return_value=["JARZ Manager"]), patch.object(
            bb, "merge_customers", side_effect=RuntimeError("boom")
        ), patch.object(_DB, "count", return_value=0):
            with self.assertRaises(RuntimeError):
                bb.execute(source, target)
        self.assertFalse(bb.frappe.flags.ignore_woo_outbound)

    def test_lead_fold_points_survivor_at_the_surviving_account(self):
        source = {"doctype": "Lead", "name": "L-1", "lead": "L-1", "customer": "C-1"}
        target = {"doctype": "Lead", "name": "L-2", "lead": "L-2", "customer": "C-2"}
        set_calls = []
        with patch.object(bb.frappe, "get_roles", return_value=["JARZ Manager"]), patch.object(
            bb, "merge_customers",
            return_value={"stamped_invoices": 0, "credit_terms_carried": False, "totals": {}},
        ), patch.object(leads, "merge_leads") as merge_leads, patch.object(
            _DB, "set_value", side_effect=lambda *a, **k: set_calls.append(a)
        ), patch.object(_DB, "get_value", return_value=None), patch.object(
            _DB, "count", return_value=1
        ), patch.object(crm, "_resolve_lead_customer", return_value=None):
            bb.execute(source, target)
        merge_leads.assert_called_once_with("L-2", ["L-1"])
        self.assertIn(("Lead", "L-2", "customer", "C-2"), set_calls)

    def test_adopting_the_sources_customer_links_the_target_lead(self):
        source = {"doctype": "Lead", "name": "L-1", "lead": "L-1", "customer": "C-1"}
        target = {"doctype": "Lead", "name": "L-2", "lead": "L-2", "customer": None}
        set_calls = []
        with patch.object(bb.frappe, "get_roles", return_value=["B2B Sales Rep"]), patch.object(
            leads, "merge_leads"
        ), patch.object(
            _DB, "set_value", side_effect=lambda *a, **k: set_calls.append(a)
        ), patch.object(_DB, "get_value", return_value="C-1"):
            bb.execute(source, target)
        self.assertEqual(set_calls[0], ("Lead", "L-2", "customer", "C-1"))

    def test_customer_merge_rolls_back_on_total_drift(self):
        snapshots = iter([
            {"invoices": 10, "billed": 100.0, "outstanding": 0.0, "gl_rows": 4,
             "gl_debit": 100.0, "gl_credit": 100.0, "payments": 1, "paid": 100.0},
            {"invoices": 10, "billed": 90.0, "outstanding": 0.0, "gl_rows": 4,
             "gl_debit": 100.0, "gl_credit": 100.0, "payments": 1, "paid": 100.0},
        ])
        with patch.object(bb, "_snapshot", side_effect=lambda names: next(snapshots)), patch.object(
            bb, "_stamp_unaddressed_invoices", return_value=0
        ), patch.object(bb, "_title_source_addresses", return_value=0), patch.object(
            bb, "_carry_credit_terms", return_value=False
        ), patch.object(bb, "_leads_for_customer", return_value=[]), patch.object(
            bb.frappe, "get_all", return_value=[]
        ), patch.object(_DB, "get_value", return_value="Orbt"), patch.object(
            _DB, "set_value"
        ), patch.object(_DB, "exists", return_value=False), patch.object(
            bb, "customer_merge_blockers", return_value=[]
        ), patch.object(bb, "_woo_id", return_value=None), patch(
            "frappe.model.rename_doc.rename_doc"
        ):
            with self.assertRaises(frappe.ValidationError) as ctx:
                bb.merge_customers("ORBT", "ORBT-1")
        self.assertIn("billed", str(ctx.exception))
        _DB.rollback.assert_called_once_with(save_point="jarz_b2b_merge_as_branch")

    def test_customer_merge_restores_display_name_and_lead_status(self):
        snap = {"invoices": 2, "billed": 5.0, "outstanding": 0.0, "gl_rows": 2,
                "gl_debit": 5.0, "gl_credit": 5.0, "payments": 0, "paid": 0.0}
        set_calls = []

        def get_value(doctype, name, field, *a, **k):
            if doctype == "Customer" and field == "customer_name":
                return "Orbt speciality Coffee"
            if doctype == "Lead" and field == "status":
                return "Converted"
            return None

        with patch.object(crm, "_resolve_lead_customer", return_value="ORBT-1"), patch.object(
            bb, "_snapshot", return_value=dict(snap)
        ), patch.object(
            bb, "_stamp_unaddressed_invoices", return_value=1
        ), patch.object(bb, "_title_source_addresses", return_value=0), patch.object(
            bb, "_carry_credit_terms", return_value=True
        ), patch.object(bb, "_leads_for_customer", return_value=["L-9"]), patch.object(
            bb.frappe, "get_all", return_value=[]
        ), patch.object(_DB, "get_value", side_effect=get_value), patch.object(
            _DB, "set_value", side_effect=lambda *a, **k: set_calls.append(a)
        ), patch.object(_DB, "exists", return_value=False), patch.object(
            bb, "customer_merge_blockers", return_value=[]
        ), patch.object(bb, "_woo_id", return_value=None), patch(
            "frappe.model.rename_doc.rename_doc"
        ) as rename:
            out = bb.merge_customers("ORBT", "ORBT-1")
        rename.assert_called_once()
        self.assertEqual(rename.call_args.args, ("Customer", "ORBT", "ORBT-1"))
        self.assertTrue(rename.call_args.kwargs["merge"])
        self.assertIn(("Customer", "ORBT-1", "customer_name", "Orbt speciality Coffee"), set_calls)
        self.assertIn(("Lead", "L-9", "status", "Converted"), set_calls)
        self.assertEqual(out["stamped_invoices"], 1)
        self.assertTrue(out["credit_terms_carried"])


class TestCustomerMergeGuards(unittest.TestCase):
    def setUp(self):
        self._db_patch = _db()
        self._db_patch.start()
        self.addCleanup(self._db_patch.stop)

    def test_walk_in_pos_customer_is_never_merged(self):
        with patch.object(bb.frappe, "get_all", side_effect=lambda dt, **k: [{"name": "POS-1"}] if dt == "POS Profile" else []), patch.object(
            bb, "_label_clash", return_value=[]
        ), patch.object(bb, "_woo_id", return_value=None), patch("frappe.model.rename_doc.rename_doc") as rename:
            with self.assertRaises(frappe.ValidationError):
                bb.merge_customers("Walk-in", "ORBT-1")
        rename.assert_not_called()

    def test_shared_label_flavour_blocks(self):
        with patch.object(bb, "_structural_use", return_value=None), patch.object(
            bb, "_label_clash", return_value=["Strawberry Large"]
        ), patch.object(bb, "_woo_id", return_value=None):
            self.assertTrue(bb.customer_merge_blockers("A", "B"))

    def test_two_different_woo_accounts_block(self):
        with patch.object(bb, "_structural_use", return_value=None), patch.object(
            bb, "_label_clash", return_value=[]
        ), patch.object(bb, "_woo_id", side_effect=lambda c: {"A": "11", "B": "22"}[c]):
            self.assertTrue(bb.customer_merge_blockers("A", "B"))

    def test_source_woo_binding_is_carried_to_target(self):
        snap = {"invoices": 1, "billed": 5.0}
        set_calls = []
        woo = {"ORBT": "3357", "ORBT-1": None}
        with patch.object(bb, "customer_merge_blockers", return_value=[]), patch.object(
            bb, "_snapshot", return_value=dict(snap)
        ), patch.object(bb, "_stamp_unaddressed_invoices", return_value=0), patch.object(
            bb, "_title_source_addresses", return_value=0
        ), patch.object(bb, "_carry_credit_terms", return_value=False), patch.object(
            bb, "_leads_for_customer", return_value=[]
        ), patch.object(bb.frappe, "get_all", return_value=[]), patch.object(
            _DB, "set_value", side_effect=lambda *a, **k: (set_calls.append(a), woo.__setitem__("ORBT-1", a[3]) if a[2] == "woo_customer_id" else None)
        ), patch.object(_DB, "get_value", return_value=None), patch.object(
            _DB, "exists", return_value=False
        ), patch.object(bb, "_woo_id", side_effect=lambda c: woo.get(c)), patch(
            "frappe.model.rename_doc.rename_doc"
        ):
            out = bb.merge_customers("ORBT", "ORBT-1")
        self.assertIn(("Customer", "ORBT-1", "woo_customer_id", "3357"), set_calls)
        self.assertEqual(out["woo_customer_id_carried"], "3357")


class TestLeadMergeKeepsEachDoor(unittest.TestCase):
    def test_lead_without_branch_rows_becomes_its_own_branch(self):
        doc = {
            "name": "L-1",
            "lead_name": "Orbt speciality coffee",
            "custom_primary_area": "Obour",
            "custom_maps_url": "https://maps.app.goo.gl/x",
            "custom_latitude": 30.21,
            "custom_longitude": 31.47,
            "mobile_no": "01009853333",
            "custom_branches": [],
        }
        rows = leads._branch_rows_or_self(doc)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["branch_name"], "Orbt speciality coffee")
        self.assertEqual(rows[0]["phone"], "01009853333")

    def test_lead_without_any_location_adds_nothing(self):
        self.assertEqual(leads._branch_rows_or_self({"name": "L-1", "lead_name": "X"}), [])

    def test_two_doors_in_one_area_stay_two_branches(self):
        a = {"branch_name": "Orbt", "area": "Obour", "latitude": 30.2101, "longitude": 31.4702}
        b = {"branch_name": "Orbt", "area": "Obour", "latitude": 30.1650, "longitude": 31.4901}
        self.assertFalse(leads._same_branch(a, b))

    def test_same_door_dedups_even_when_pins_differ_slightly_or_one_is_missing(self):
        a = {"branch_name": "Costa", "area": "Zamalek", "latitude": 30.06000, "longitude": 31.22000}
        near = {"branch_name": " costa ", "area": "ZAMALEK", "latitude": 30.06015, "longitude": 31.22010}
        unpinned = {"branch_name": "Costa", "area": "Zamalek"}
        self.assertTrue(leads._same_branch(a, near))
        self.assertTrue(leads._same_branch(a, unpinned))
        self.assertFalse(leads._same_branch(a, {"branch_name": "Costa", "area": "Maadi"}))


class TestEndpointsGate(unittest.TestCase):
    def setUp(self):
        self._db_patch = _db()
        self._db_patch.start()
        self.addCleanup(self._db_patch.stop)

    def test_merge_endpoint_requires_b2b_access_before_any_read(self):
        with patch.object(crm.frappe, "get_roles", return_value=["Sales User"]), patch.object(
            bb, "resolve_party"
        ) as resolve:
            with self.assertRaises(frappe.ValidationError):
                crm.merge_as_branch("Customer", "A", "Customer", "B")
        resolve.assert_not_called()

    def test_account_invoices_requires_a_customer(self):
        with patch.object(crm.frappe, "get_roles", return_value=["B2B Sales Rep"]), patch.object(
            crm, "_doctype_exists", return_value=True
        ), patch.object(_DB, "exists", return_value=True), patch.object(
            crm, "_require_doc_permission"
        ), patch.object(crm, "_resolve_lead_customer", return_value=None):
            with self.assertRaises(frappe.ValidationError):
                crm.get_account_invoices("Lead", "L-1")


if __name__ == "__main__":
    unittest.main()
