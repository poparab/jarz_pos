"""Leads catalog API tests (light-DB / unittest).

Exercises the six whitelisted endpoints in ``jarz_pos.api.leads`` plus the
bench-run importer ``jarz_pos.scripts.import_leads_catalog`` against a real (but
uncommitted) DB, mirroring the ``test_commercial_policy`` pattern.

Why plain ``unittest.TestCase`` (not FrappeTestCase): on ERPNext v16 FrappeTestCase
imports ``erpnext.tests.utils`` whose module-level BootStrapTestData() collides with
the populated CI ``frontend`` clone. We instead insert docs on the live connection
(uncommitted, visible on the same connection) and ``frappe.db.rollback()`` them in
tearDown so the module is non-destructive and CI-safe under ``--skip-before-tests``.

Fixtures the site is expected to provide (installed via app fixtures + after_migrate
seeding): the Lead ``custom_*`` catalog fields, the ``Jarz Lead Branch`` child table,
the ``Jarz Lead Category`` master (seeded "Coffee"), and the ``B2B Sales Rep`` role.
setUp is defensive and ensures the "Coffee" category and the B2B role exist so the
suite is self-sufficient. Tests run as Administrator, who carries every role, so the
``_ensure_b2b_access()`` gate passes on every endpoint.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

import frappe

from jarz_pos.api import crm as crm_api
from jarz_pos.api import leads as leads_api
from jarz_pos.scripts import import_leads_catalog as importer

_COFFEE = "Coffee"
_B2B_ROLE = "B2B Sales Rep"


def _ensure_category(name):
    """Create-only guard for a Jarz Lead Category master (idempotent)."""
    if not frappe.db.exists("Jarz Lead Category", name):
        frappe.get_doc(
            {"doctype": "Jarz Lead Category", "category_name": name}
        ).insert(ignore_permissions=True)


def _ensure_b2b_role():
    """Create-only guard for the B2B Sales Rep role (idempotent)."""
    if not frappe.db.exists("Role", _B2B_ROLE):
        frappe.get_doc(
            {"doctype": "Role", "role_name": _B2B_ROLE, "desk_access": 1, "disabled": 0}
        ).insert(ignore_permissions=True)


# ---------------------------------------------------------------------------
# save_lead / get_lead / get_leads
# ---------------------------------------------------------------------------
class TestSaveAndGetLead(unittest.TestCase):
    """Create/update a catalog Lead and read it back via the flat mappers."""

    def setUp(self):
        _ensure_category(_COFFEE)
        _ensure_b2b_role()

    def tearDown(self):
        frappe.db.rollback()

    # --- 1) create ---------------------------------------------------------
    def test_create_maps_fields_and_seeds_stage(self):
        out = leads_api.save_lead(
            {
                "lead_name": "_TEST Roastery",
                "category": _COFFEE,
                "tier": "A",
                "is_specialty": True,
                "mobile_no": "0100000001",
                "instagram": "@roastery",
                "facebook": "fb/roastery",
                "maps_url": "https://maps/roastery",
                "primary_area": "Zamalek",
                "price_band": "$$$",
                "score": 87,
                "notes": "hot lead",
                "regions": ["North Coast", "Cairo"],
                "areas": ["Zamalek"],
                "governorates": ["Cairo"],
            }
        )
        name = out["name"]
        self.assertTrue(name)

        doc = frappe.get_doc("Lead", name)
        # Create-only seeds.
        self.assertEqual(doc.custom_b2b_stage, "Lead")
        self.assertEqual(doc.status, "Open")
        # Scalar field mapping.
        self.assertEqual(doc.custom_lead_category, _COFFEE)
        self.assertEqual(doc.custom_fit_tier, "A")
        self.assertEqual(int(doc.custom_is_specialty), 1)
        self.assertEqual(doc.mobile_no, "0100000001")
        self.assertEqual(doc.custom_instagram, "@roastery")
        self.assertEqual(doc.custom_facebook, "fb/roastery")
        self.assertEqual(doc.custom_maps_url, "https://maps/roastery")
        self.assertEqual(doc.custom_primary_area, "Zamalek")
        self.assertEqual(doc.custom_price_band, "$$$")
        self.assertEqual(int(doc.custom_lead_score), 87)
        self.assertEqual(doc.custom_notes, "hot lead")
        # JSON list fields stored as json.dumps.
        self.assertEqual(json.loads(doc.custom_regions), ["North Coast", "Cairo"])
        self.assertEqual(json.loads(doc.custom_areas), ["Zamalek"])
        self.assertEqual(json.loads(doc.custom_governorates), ["Cairo"])

    def test_create_requires_lead_name(self):
        with self.assertRaises(Exception):
            leads_api.save_lead({"category": _COFFEE})

    # --- 2) update PATCHes only provided keys ------------------------------
    def test_update_patches_only_provided_keys(self):
        name = leads_api.save_lead(
            {
                "lead_name": "_TEST Patchable",
                "tier": "B",
                "instagram": "@before",
                "notes": "keep me",
                "score": 40,
            }
        )["name"]

        # Update only tier + instagram; omit notes/score/lead_name.
        leads_api.save_lead({"tier": "A", "instagram": "@after"}, name=name)

        doc = frappe.get_doc("Lead", name)
        self.assertEqual(doc.custom_fit_tier, "A")           # patched
        self.assertEqual(doc.custom_instagram, "@after")     # patched
        self.assertEqual(doc.lead_name, "_TEST Patchable")   # intact
        self.assertEqual(doc.custom_notes, "keep me")        # intact
        self.assertEqual(int(doc.custom_lead_score), 40)     # intact
        # Rep-owned seeds preserved across an update.
        self.assertEqual(doc.custom_b2b_stage, "Lead")
        self.assertEqual(doc.status, "Open")

    def test_update_unknown_lead_throws(self):
        with self.assertRaises(Exception):
            leads_api.save_lead({"tier": "A"}, name="Lead-does-not-exist")

    # --- 3) get_lead flat fields + branches + addresses + notes -----------
    def test_get_lead_detail_shape(self):
        name = leads_api.save_lead(
            {
                "lead_name": "_TEST Detail",
                "category": _COFFEE,
                "notes": "detail notes",
                "branches": [
                    {
                        "branch_name": "Main",
                        "area": "Maadi",
                        "region": "Cairo",
                        "governorate": "Cairo",
                        "rating": 4.5,
                        "reviews": 120,
                        "price": "$$",
                        "status": "Open",
                    },
                    {"branch_name": "Second", "area": "Sahel"},
                ],
            }
        )["name"]

        detail = leads_api.get_lead(name)
        # Flat fields present.
        self.assertEqual(detail["name"], name)
        self.assertEqual(detail["lead_name"], "_TEST Detail")
        self.assertEqual(detail["category"], _COFFEE)
        self.assertEqual(detail["b2b_stage"], "Lead")
        self.assertEqual(detail["status"], "Open")
        # Branches mapped from the child table.
        self.assertEqual(len(detail["branches"]), 2)
        self.assertEqual(detail["branches"][0]["branch_name"], "Main")
        self.assertEqual(detail["branches"][0]["rating"], 4.5)
        self.assertEqual(detail["branches"][0]["reviews"], 120)
        self.assertEqual(detail["branches"][1]["branch_name"], "Second")
        # Notes.
        self.assertEqual(detail["notes"], "detail notes")
        # No addresses yet -> null.
        self.assertIsNone(detail["primary_address"])
        self.assertIsNone(detail["shipping_address"])

    def test_get_lead_unknown_throws(self):
        with self.assertRaises(Exception):
            leads_api.get_lead("Lead-does-not-exist")

    # --- 4) get_leads list shape + parsed lists + coarse filter -----------
    def test_get_leads_shape_and_category_filter(self):
        target = leads_api.save_lead(
            {
                "lead_name": "_TEST Listable",
                "category": _COFFEE,
                "regions": ["Cairo", "Giza"],
                "areas": ["Maadi"],
                "governorates": ["Cairo"],
                "score": 55,
            }
        )["name"]
        # A second lead in a different (throwaway) category to prove filtering.
        other_cat = "_TEST Category X"
        _ensure_category(other_cat)
        leads_api.save_lead({"lead_name": "_TEST Other Cat", "category": other_cat})

        res = leads_api.get_leads(category=_COFFEE)
        self.assertIn("leads", res)
        self.assertIn("count", res)
        self.assertEqual(res["count"], len(res["leads"]))

        by_name = {row["name"]: row for row in res["leads"]}
        self.assertIn(target, by_name)
        row = by_name[target]
        # Output keys / parsed list fields.
        self.assertEqual(row["category"], _COFFEE)
        self.assertEqual(row["regions"], ["Cairo", "Giza"])
        self.assertEqual(row["areas"], ["Maadi"])
        self.assertEqual(row["governorates"], ["Cairo"])
        self.assertEqual(row["score"], 55)
        self.assertEqual(row["b2b_stage"], "Lead")
        # Coarse filter excludes the other-category lead.
        self.assertTrue(all(r["category"] == _COFFEE for r in res["leads"]))


# ---------------------------------------------------------------------------
# 5) set_lead_address (primary / shipping, update-in-place)
# ---------------------------------------------------------------------------
class TestLeadAddress(unittest.TestCase):
    def setUp(self):
        _ensure_category(_COFFEE)
        _ensure_b2b_role()
        self.name = leads_api.save_lead(
            {"lead_name": "_TEST Addressable", "category": _COFFEE}
        )["name"]

    def tearDown(self):
        frappe.db.rollback()

    def test_primary_then_shipping_then_update_in_place(self):
        # Primary address created + resolvable back via get_lead.
        res_p = leads_api.set_lead_address(
            self.name,
            "primary",
            {
                "address_line1": "1 Nile St",
                "city": "Cairo",
                "state": "Cairo",
                "country": "Egypt",
                "pincode": "11511",
                "phone": "0100000009",
            },
        )
        primary_addr = res_p["address"]
        self.assertTrue(primary_addr)
        self.assertEqual(
            int(frappe.db.get_value("Address", primary_addr, "is_primary_address")), 1
        )

        detail = leads_api.get_lead(self.name)
        self.assertIsNotNone(detail["primary_address"])
        self.assertEqual(detail["primary_address"]["name"], primary_addr)
        self.assertEqual(detail["primary_address"]["address_line1"], "1 Nile St")
        self.assertIsNone(detail["shipping_address"])  # none yet

        # Shipping is a SEPARATE record.
        res_s = leads_api.set_lead_address(
            self.name,
            "shipping",
            {"address_line1": "9 Delivery Rd", "city": "Giza"},
        )
        shipping_addr = res_s["address"]
        self.assertNotEqual(shipping_addr, primary_addr)
        self.assertEqual(
            int(frappe.db.get_value("Address", shipping_addr, "is_shipping_address")), 1
        )

        detail = leads_api.get_lead(self.name)
        self.assertEqual(detail["shipping_address"]["name"], shipping_addr)

        # Updating primary again edits the SAME record (no duplicate).
        res_p2 = leads_api.set_lead_address(
            self.name, "primary", {"address_line1": "2 Nile St", "city": "Cairo"}
        )
        self.assertEqual(res_p2["address"], primary_addr)
        self.assertEqual(
            frappe.db.get_value("Address", primary_addr, "address_line1"), "2 Nile St"
        )
        # Still exactly one primary Address linked to this lead.
        primary_names = leads_api._linked_lead_address_names(self.name)
        primary_count = sum(
            1
            for a in primary_names
            if frappe.db.get_value("Address", a, "is_primary_address")
        )
        self.assertEqual(primary_count, 1)

    def test_invalid_kind_throws(self):
        with self.assertRaises(Exception):
            leads_api.set_lead_address(self.name, "billing", {"address_line1": "x"})

    def test_unknown_lead_throws(self):
        with self.assertRaises(Exception):
            leads_api.set_lead_address(
                "Lead-does-not-exist", "primary", {"address_line1": "x"}
            )


# ---------------------------------------------------------------------------
# 6) categories
# ---------------------------------------------------------------------------
class TestLeadCategories(unittest.TestCase):
    def setUp(self):
        _ensure_category(_COFFEE)
        _ensure_b2b_role()

    def tearDown(self):
        frappe.db.rollback()

    def test_get_categories_includes_coffee(self):
        res = leads_api.get_lead_categories()
        names = {c["name"] for c in res["categories"]}
        self.assertIn(_COFFEE, names)

    def test_save_category_is_idempotent(self):
        cat = "_TEST Bakery"
        out1 = leads_api.save_lead_category(cat, color="#ff0000")
        self.assertEqual(out1["category_name"], cat)
        self.assertTrue(frappe.db.exists("Jarz Lead Category", cat))

        # Second call must not error or duplicate; color update applies.
        out2 = leads_api.save_lead_category(cat, color="#00ff00")
        self.assertEqual(out2["name"], out1["name"])
        count = frappe.db.count("Jarz Lead Category", {"category_name": cat})
        self.assertEqual(count, 1)
        self.assertEqual(
            frappe.db.get_value("Jarz Lead Category", cat, "color"), "#00ff00"
        )

    def test_save_category_requires_name(self):
        with self.assertRaises(Exception):
            leads_api.save_lead_category("   ")


# ---------------------------------------------------------------------------
# 7) a saved lead shows up in the B2B pipeline "Lead" column
# ---------------------------------------------------------------------------
class TestLeadInPipeline(unittest.TestCase):
    def setUp(self):
        _ensure_category(_COFFEE)
        _ensure_b2b_role()

    def tearDown(self):
        frappe.db.rollback()

    def test_new_lead_appears_in_lead_stage(self):
        name = leads_api.save_lead(
            {"lead_name": "_TEST Pipeline Lead", "category": _COFFEE}
        )["name"]

        board = crm_api.get_b2b_pipeline()
        self.assertIn("Lead", board["columns"])
        lead_names = {card["name"] for card in board["columns"]["Lead"]}
        self.assertIn(name, lead_names)
        # And the card is typed as a Lead at the Lead stage.
        card = next(c for c in board["columns"]["Lead"] if c["name"] == name)
        self.assertEqual(card["doctype"], "Lead")
        self.assertEqual(card["stage"], "Lead")


# ---------------------------------------------------------------------------
# 8) importer idempotency + rep-owned field preservation
# ---------------------------------------------------------------------------
class TestImportIdempotency(unittest.TestCase):
    """import_leads_catalog.run is idempotent on custom_source_brand_id.

    NOTE: ``importer.run`` calls ``frappe.db.commit()``, so rollback in tearDown
    cannot undo its writes. This test tracks the created Leads (and their linked
    Addresses/Contacts) by source id and hard-deletes them in tearDown.
    """

    SRC_A = "_TEST_SRC_A"
    SRC_B = "_TEST_SRC_B"

    def setUp(self):
        _ensure_category(_COFFEE)
        _ensure_b2b_role()
        self._tmp_files = []

    def tearDown(self):
        # Roll back any uncommitted work first.
        frappe.db.rollback()
        # Hard-delete the committed Leads (and their linked Addresses) by source id.
        for src in (self.SRC_A, self.SRC_B):
            for lead in frappe.get_all(
                "Lead", filters={"custom_source_brand_id": src}, pluck="name"
            ):
                self._delete_lead_and_links(lead)
        frappe.db.commit()
        for path in self._tmp_files:
            try:
                os.remove(path)
            except OSError:
                pass

    def _delete_lead_and_links(self, lead):
        # Remove Addresses linked to the Lead via Dynamic Link, then the Lead.
        for addr in leads_api._linked_lead_address_names(lead):
            try:
                frappe.delete_doc("Address", addr, force=True, ignore_permissions=True)
            except Exception:
                pass
        # Frappe auto-creates a Contact for a Lead; drop any that reference it.
        for contact in frappe.get_all(
            "Dynamic Link",
            filters={"link_doctype": "Lead", "link_name": lead, "parenttype": "Contact"},
            pluck="parent",
        ):
            try:
                frappe.delete_doc("Contact", contact, force=True, ignore_permissions=True)
            except Exception:
                pass
        try:
            frappe.delete_doc("Lead", lead, force=True, ignore_permissions=True)
        except Exception:
            pass

    def _write_catalog(self, leads):
        fd, path = tempfile.mkstemp(suffix=".json", prefix="_test_leads_")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"generated": "test", "count": len(leads), "leads": leads}, fh)
        self._tmp_files.append(path)
        return path

    def _count(self, src):
        return frappe.db.count("Lead", {"custom_source_brand_id": src})

    def test_run_twice_is_idempotent_and_preserves_rep_fields(self):
        catalog_v1 = [
            {
                "id": self.SRC_A,
                "name": "_TEST Import A",
                "score": 10,
                "tier": "B",
                "rating": 4.0,
                "reviews": 50,
                "regions": ["Cairo"],
                "areas": ["Maadi"],
                "governorates": ["Cairo"],
                "phone": "0111",
                "notes": "seed notes A",
                "branches": [
                    {"name": "A-Main", "area": "Maadi", "address": "1 Import St"}
                ],
            },
            {
                "id": self.SRC_B,
                "name": "_TEST Import B",
                "score": 20,
                "tier": "C",
                "branches": [],
            },
        ]

        # --- First run: two creates -----------------------------------------
        path1 = self._write_catalog(catalog_v1)
        summary1 = importer.run(path1)
        self.assertEqual(summary1["created"], 2)
        self.assertEqual(summary1["updated"], 0)
        self.assertEqual(self._count(self.SRC_A), 1)
        self.assertEqual(self._count(self.SRC_B), 1)

        lead_a = frappe.db.get_value(
            "Lead", {"custom_source_brand_id": self.SRC_A}, "name"
        )
        # Create-only rep-owned seeds.
        self.assertEqual(
            frappe.db.get_value("Lead", lead_a, "custom_b2b_stage"), "Lead"
        )
        self.assertEqual(frappe.db.get_value("Lead", lead_a, "status"), "Open")
        self.assertEqual(
            frappe.db.get_value("Lead", lead_a, "custom_lead_category"), _COFFEE
        )
        self.assertEqual(
            frappe.db.get_value("Lead", lead_a, "custom_notes"), "seed notes A"
        )

        # --- Simulate rep edits between runs (rep-owned fields) -------------
        frappe.db.set_value("Lead", lead_a, "status", "Replied")
        frappe.db.set_value("Lead", lead_a, "custom_b2b_stage", "Qualify")
        frappe.db.set_value("Lead", lead_a, "custom_notes", "REP EDITED")
        frappe.db.set_value("Lead", lead_a, "custom_lead_category", _COFFEE)
        frappe.db.commit()

        # --- Second run: same ids, bumped metrics -> two updates ------------
        catalog_v2 = [dict(catalog_v1[0]), dict(catalog_v1[1])]
        catalog_v2[0]["score"] = 99          # metric bump
        catalog_v2[0]["reviews"] = 500       # metric bump
        catalog_v2[0]["notes"] = "IGNORED ON UPDATE"
        path2 = self._write_catalog(catalog_v2)
        summary2 = importer.run(path2)
        self.assertEqual(summary2["created"], 0)
        self.assertEqual(summary2["updated"], 2)

        # Count stable (no duplicates) across runs.
        self.assertEqual(self._count(self.SRC_A), 1)
        self.assertEqual(self._count(self.SRC_B), 1)

        # Catalog metrics refreshed on the 2nd run.
        self.assertEqual(int(frappe.db.get_value("Lead", lead_a, "custom_lead_score")), 99)
        self.assertEqual(
            int(frappe.db.get_value("Lead", lead_a, "custom_total_reviews")), 500
        )

        # Rep-owned fields PRESERVED (never clobbered by the update).
        self.assertEqual(frappe.db.get_value("Lead", lead_a, "status"), "Replied")
        self.assertEqual(
            frappe.db.get_value("Lead", lead_a, "custom_b2b_stage"), "Qualify"
        )
        self.assertEqual(
            frappe.db.get_value("Lead", lead_a, "custom_notes"), "REP EDITED"
        )


if __name__ == "__main__":
    unittest.main()
