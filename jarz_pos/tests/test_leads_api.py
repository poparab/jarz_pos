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


def _has_lead_field(fieldname):
    """Whether the site's Lead meta carries ``fieldname``. Guarded -> False."""
    try:
        return bool(frappe.get_meta("Lead").get_field(fieldname))
    except Exception:
        return False


def _any_territory():
    """Return an existing Territory name (prefer the standard root) or None.

    Territory is a nested-set tree, so we never insert one in tests — we reuse
    whatever the site already seeds (ERPNext always seeds "All Territories").
    """
    if frappe.db.exists("Territory", "All Territories"):
        return "All Territories"
    rows = frappe.get_all("Territory", pluck="name", limit_page_length=1)
    return rows[0] if rows else None


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
        # Fit score writes to its OWN field now; custom_lead_score is reserved for
        # the nightly CRM job (compute_lead_scores) and is NOT written by save_lead.
        self.assertEqual(int(doc.custom_fit_score), 87)
        self.assertEqual(int(doc.custom_lead_score or 0), 0)
        self.assertEqual(doc.custom_notes, "hot lead")
        # JSON list fields stored as json.dumps.
        self.assertEqual(json.loads(doc.custom_regions), ["North Coast", "Cairo"])
        self.assertEqual(json.loads(doc.custom_areas), ["Zamalek"])
        self.assertEqual(json.loads(doc.custom_governorates), ["Cairo"])

    def test_create_requires_lead_name(self):
        with self.assertRaises(Exception):
            leads_api.save_lead({"category": _COFFEE})

    # --- 1b) standard-CRM parity fields (email_id / source / territory) -----
    def test_create_maps_crm_parity_fields(self):
        """save_lead now maps email_id + guarded source/territory (create_lead parity)."""
        territory = _any_territory()
        # A known-good Lead Source (only when the standard DocType is installed).
        source = None
        if frappe.db.exists("DocType", "Lead Source"):
            source = "_TEST Lead Source"
            if not frappe.db.exists("Lead Source", source):
                frappe.get_doc(
                    {"doctype": "Lead Source", "source_name": source}
                ).insert(ignore_permissions=True)

        payload = {
            "lead_name": "_TEST Parity",
            "category": _COFFEE,
            "email_id": "parity@example.com",
            # Unknown source is silently ignored (no such Lead Source record /
            # not a valid custom_lead_source option) — never raises.
            "source": source or "_TEST No Such Source",
        }
        if territory:
            payload["territory"] = territory

        doc = frappe.get_doc("Lead", leads_api.save_lead(payload)["name"])
        self.assertEqual(doc.email_id, "parity@example.com")
        if territory:
            self.assertEqual(doc.territory, territory)
        if source:
            self.assertEqual(doc.source, source)
        else:
            # Guard held: an unknown source left the Link field unset.
            self.assertFalse(doc.get("source"))

    def test_update_patches_crm_parity_fields(self):
        """email_id / territory are PATCH-applied on update too, like other scalars."""
        territory = _any_territory()

        name = leads_api.save_lead({"lead_name": "_TEST Parity Update"})["name"]
        patch = {"email_id": "later@example.com"}
        if territory:
            patch["territory"] = territory
        leads_api.save_lead(patch, name=name)

        doc = frappe.get_doc("Lead", name)
        self.assertEqual(doc.email_id, "later@example.com")
        if territory:
            self.assertEqual(doc.territory, territory)
        # Untouched rep-owned seeds preserved across the update.
        self.assertEqual(doc.custom_b2b_stage, "Lead")
        self.assertEqual(doc.status, "Open")

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
        self.assertEqual(int(doc.custom_fit_score), 40)      # intact (fit score field)
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
        # save_lead writes the fit score to custom_fit_score, never custom_lead_score.
        self.assertEqual(
            int(frappe.db.get_value("Lead", target, "custom_fit_score")), 55
        )
        self.assertEqual(
            int(frappe.db.get_value("Lead", target, "custom_lead_score") or 0), 0
        )
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

    def test_a_lead_past_qualify_still_appears_on_the_board(self):
        """Nothing converts a Lead to an Opportunity, so every stage must show.

        The board used to query only ("Lead", "Qualify") on the assumption that
        post-sample work continued on an Opportunity. It never does — so a rep
        who moved a lead to Sample watched the card disappear.
        """
        name = leads_api.save_lead(
            {"lead_name": "_TEST Sampled", "category": _COFFEE}
        )["name"]
        crm_api.advance_stage("Lead", name, "Sample")

        board = crm_api.get_b2b_pipeline()
        self.assertIn("Sample", board["columns"])
        card = next(
            (c for c in board["columns"]["Sample"] if c["name"] == name), None
        )
        self.assertIsNotNone(card, "lead at Sample is missing from the board")
        self.assertEqual(card["doctype"], "Lead")
        self.assertEqual(card["stage"], "Sample")

    def test_every_post_sample_stage_is_reachable_on_the_board(self):
        for stage in ("Approved", "Trial", "Check-up", "Active"):
            with self.subTest(stage=stage):
                name = leads_api.save_lead(
                    {"lead_name": f"_TEST At {stage}", "category": _COFFEE}
                )["name"]
                crm_api.advance_stage("Lead", name, stage)
                board = crm_api.get_b2b_pipeline()
                self.assertIn(
                    name, {c["name"] for c in board["columns"].get(stage, [])}
                )

    def test_lead_column_sorted_by_score_desc(self):
        """The Lead column orders higher fit score first (sorts by custom_fit_score).

        The card's output key stays ``lead_score`` but its source column is the
        catalog fit score (custom_fit_score), which is what save_lead writes.
        """
        low = leads_api.save_lead(
            {"lead_name": "_TEST Score Low", "category": _COFFEE, "score": 5}
        )["name"]
        high = leads_api.save_lead(
            {"lead_name": "_TEST Score High", "category": _COFFEE, "score": 95}
        )["name"]

        board = crm_api.get_b2b_pipeline()
        col = board["columns"]["Lead"]
        # Restrict to our two test cards so pre-existing leads don't interfere.
        order = [c["name"] for c in col if c["name"] in (low, high)]
        self.assertEqual(order, [high, low])


# ---------------------------------------------------------------------------
# 8) not-suitable verdict (manual inspection)
# ---------------------------------------------------------------------------
class TestLeadSuitability(unittest.TestCase):
    """set_lead_suitability marks/clears the verdict and hides the lead.

    Skipped whole on a site that has not migrated ``custom_not_suitable`` yet:
    the CI logic gate runs the branch's code against the live staging site
    WITHOUT migrating, so these would fail on a missing column rather than on a
    real defect. Every production code path is guarded on the same field, so a
    pre-migrate site is a supported state, not a broken one.
    """

    def setUp(self):
        if not _has_lead_field("custom_not_suitable"):
            self.skipTest(
                "Lead.custom_not_suitable not migrated on this site yet."
            )
        _ensure_category(_COFFEE)
        _ensure_b2b_role()
        self.name = leads_api.save_lead(
            {"lead_name": "_TEST Unsuitable", "category": _COFFEE, "score": 60}
        )["name"]

    def tearDown(self):
        frappe.db.rollback()

    def test_reasons_endpoint_returns_options(self):
        reasons = leads_api.get_not_suitable_reasons()["reasons"]
        self.assertTrue(reasons)
        self.assertIn("Out of Business", reasons)
        self.assertIn("Other", reasons)

    def test_mark_stamps_all_five_fields_and_parks_stage(self):
        out = leads_api.set_lead_suitability(
            self.name, 1, reason="Out of Business", notes="shutters down"
        )
        row = out["lead"]
        self.assertTrue(row["not_suitable"])
        self.assertEqual(row["not_suitable_reason"], "Out of Business")
        self.assertEqual(row["not_suitable_notes"], "shutters down")
        self.assertTrue(row["not_suitable_on"])
        self.assertEqual(row["not_suitable_by"], frappe.session.user)
        self.assertEqual(row["b2b_stage"], "Lost/On-hold")

        doc = frappe.get_doc("Lead", self.name)
        self.assertEqual(int(doc.custom_not_suitable), 1)
        self.assertEqual(doc.custom_not_suitable_reason, "Out of Business")
        self.assertEqual(doc.custom_b2b_stage, "Lost/On-hold")

    def test_mark_requires_a_valid_reason(self):
        with self.assertRaises(Exception):
            leads_api.set_lead_suitability(self.name, 1)
        with self.assertRaises(Exception):
            leads_api.set_lead_suitability(self.name, 1, reason="_TEST Nope")
        # Nothing was written by either rejected call.
        self.assertEqual(
            int(frappe.db.get_value("Lead", self.name, "custom_not_suitable") or 0), 0
        )

    def test_unknown_lead_throws(self):
        with self.assertRaises(Exception):
            leads_api.set_lead_suitability(
                "Lead-does-not-exist", 1, reason="Duplicate"
            )

    def test_clear_wipes_the_verdict_and_restores_the_stage(self):
        leads_api.set_lead_suitability(self.name, 1, reason="Too Small", notes="n")
        row = leads_api.set_lead_suitability(self.name, 0)["lead"]

        self.assertFalse(row["not_suitable"])
        self.assertEqual(row["not_suitable_reason"], "")
        self.assertEqual(row["not_suitable_notes"], "")
        self.assertIsNone(row["not_suitable_on"])
        self.assertEqual(row["not_suitable_by"], "")
        self.assertEqual(row["b2b_stage"], "Lead")

    def test_clear_keeps_a_stage_moved_on_since_marking(self):
        leads_api.set_lead_suitability(self.name, 1, reason="Unreachable")
        # Someone advanced the lead while it was parked.
        frappe.db.set_value("Lead", self.name, "custom_b2b_stage", "Qualify")
        row = leads_api.set_lead_suitability(self.name, 0)["lead"]
        self.assertEqual(row["b2b_stage"], "Qualify")

    def test_get_leads_tri_state_filter(self):
        keeper = leads_api.save_lead(
            {"lead_name": "_TEST Suitable", "category": _COFFEE}
        )["name"]
        leads_api.set_lead_suitability(self.name, 1, reason="Duplicate")

        all_names = {r["name"] for r in leads_api.get_leads()["leads"]}
        self.assertIn(self.name, all_names)   # default: everything
        self.assertIn(keeper, all_names)

        suitable = {r["name"] for r in leads_api.get_leads(not_suitable=0)["leads"]}
        self.assertNotIn(self.name, suitable)
        self.assertIn(keeper, suitable)

        rejected = {r["name"] for r in leads_api.get_leads(not_suitable=1)["leads"]}
        self.assertIn(self.name, rejected)
        self.assertNotIn(keeper, rejected)

    def test_marked_lead_drops_off_the_pipeline_board(self):
        board = crm_api.get_b2b_pipeline()
        self.assertIn(
            self.name, {c["name"] for c in board["columns"]["Lead"]}
        )

        leads_api.set_lead_suitability(self.name, 1, reason="Wrong Category")

        board = crm_api.get_b2b_pipeline()
        on_board = {
            card["name"]
            for column in board["columns"].values()
            for card in column
        }
        self.assertNotIn(self.name, on_board)

    def test_advancing_the_stage_clears_the_verdict(self):
        """Putting a lead back into a live stage overrides an earlier verdict."""
        leads_api.set_lead_suitability(self.name, 1, reason="Not Interested")
        crm_api.advance_stage("Lead", self.name, "Qualify")

        doc = frappe.get_doc("Lead", self.name)
        self.assertEqual(int(doc.custom_not_suitable or 0), 0)
        self.assertFalse(doc.custom_not_suitable_reason)
        self.assertEqual(doc.custom_b2b_stage, "Qualify")
        # And it is back on the board.
        board = crm_api.get_b2b_pipeline()
        self.assertIn(self.name, {c["name"] for c in board["columns"]["Qualify"]})

    def test_moving_to_lost_keeps_the_verdict(self):
        """Lost/On-hold is where marking parks the lead; re-setting it is a no-op."""
        leads_api.set_lead_suitability(self.name, 1, reason="Duplicate")
        crm_api.advance_stage("Lead", self.name, "Lost/On-hold")

        doc = frappe.get_doc("Lead", self.name)
        self.assertEqual(int(doc.custom_not_suitable), 1)
        self.assertEqual(doc.custom_not_suitable_reason, "Duplicate")

    def test_save_lead_cannot_write_the_verdict(self):
        """The verdict is owned by set_lead_suitability; save_lead ignores it."""
        leads_api.save_lead(
            {"not_suitable": True, "not_suitable_reason": "Too Small"},
            name=self.name,
        )
        self.assertEqual(
            int(frappe.db.get_value("Lead", self.name, "custom_not_suitable") or 0), 0
        )


# ---------------------------------------------------------------------------
# 9) merging duplicate leads
# ---------------------------------------------------------------------------
class TestLeadMerge(unittest.TestCase):
    """merge_leads folds duplicates into one surviving Lead.

    Skipped on a site that has not migrated ``custom_merged_into`` yet, for the
    same reason as TestLeadSuitability — the CI logic gate does not migrate.
    """

    def setUp(self):
        if not _has_lead_field("custom_merged_into"):
            self.skipTest("Lead.custom_merged_into not migrated on this site yet.")
        _ensure_category(_COFFEE)
        _ensure_b2b_role()

        self.target = leads_api.save_lead(
            {
                "lead_name": "_TEST Brand",
                "category": _COFFEE,
                "phone": "0100000100",
                "areas": ["Maadi"],
                "regions": ["Cairo"],
                "avg_rating": 4.0,
                "total_reviews": 100,
                "notes": "target notes",
                "branches": [{"branch_name": "Maadi", "area": "Maadi",
                              "rating": 4.0, "reviews": 100}],
            }
        )["name"]
        self.duplicate = leads_api.save_lead(
            {
                "lead_name": "_TEST Brand",
                "category": _COFFEE,
                "phone": "0100000100",
                "instagram": "@brand",
                "areas": ["Zamalek"],
                "governorates": ["Cairo"],
                "avg_rating": 5.0,
                "total_reviews": 300,
                "notes": "duplicate notes",
                "branches": [{"branch_name": "Zamalek", "area": "Zamalek",
                              "rating": 5.0, "reviews": 300}],
            }
        )["name"]

    def tearDown(self):
        frappe.db.rollback()

    def test_merge_unions_branches_and_lists(self):
        leads_api.merge_leads(self.target, [self.duplicate])

        doc = frappe.get_doc("Lead", self.target)
        names = sorted(b.branch_name for b in doc.custom_branches)
        self.assertEqual(names, ["Maadi", "Zamalek"])
        self.assertEqual(int(doc.custom_branch_count), 2)
        self.assertEqual(int(doc.custom_total_reviews), 400)
        self.assertEqual(sorted(json.loads(doc.custom_areas)), ["Maadi", "Zamalek"])
        self.assertEqual(json.loads(doc.custom_regions), ["Cairo"])
        self.assertEqual(json.loads(doc.custom_governorates), ["Cairo"])

    def test_avg_rating_is_weighted_by_reviews_not_a_plain_mean(self):
        # 4.0 over 100 reviews + 5.0 over 300 = 4.75, NOT the naive 4.5.
        leads_api.merge_leads(self.target, [self.duplicate])
        self.assertEqual(
            float(frappe.db.get_value("Lead", self.target, "custom_avg_rating")),
            4.75,
        )

    def test_blank_target_fields_are_filled_but_set_ones_are_kept(self):
        leads_api.merge_leads(self.target, [self.duplicate])
        doc = frappe.get_doc("Lead", self.target)
        # Target had no instagram -> takes the duplicate's.
        self.assertEqual(doc.custom_instagram, "@brand")
        # Target already had a phone -> keeps its own.
        self.assertEqual(doc.phone, "0100000100")

    def test_source_notes_are_appended_with_attribution(self):
        leads_api.merge_leads(self.target, [self.duplicate])
        notes = frappe.db.get_value("Lead", self.target, "custom_notes")
        self.assertIn("target notes", notes)
        self.assertIn("duplicate notes", notes)
        self.assertIn("merged from", notes)

    def test_source_is_parked_and_stamped_but_never_deleted(self):
        leads_api.merge_leads(self.target, [self.duplicate])

        self.assertTrue(frappe.db.exists("Lead", self.duplicate))
        source = frappe.get_doc("Lead", self.duplicate)
        self.assertEqual(source.custom_merged_into, self.target)
        self.assertTrue(source.custom_merged_on)
        self.assertEqual(source.custom_merged_by, frappe.session.user)
        self.assertEqual(source.custom_b2b_stage, "Lost/On-hold")
        # Branches were COPIED, not moved, so the source stays restorable.
        self.assertEqual(len(source.custom_branches), 1)

    def test_merged_source_leaves_the_catalog_and_the_board(self):
        leads_api.merge_leads(self.target, [self.duplicate])

        catalog = {r["name"] for r in leads_api.get_leads()["leads"]}
        self.assertIn(self.target, catalog)
        self.assertNotIn(self.duplicate, catalog)

        # ...but is still reachable when explicitly asked for, and by name.
        with_merged = {
            r["name"] for r in leads_api.get_leads(include_merged=1)["leads"]
        }
        self.assertIn(self.duplicate, with_merged)
        self.assertEqual(
            leads_api.get_lead(self.duplicate)["merged_into"], self.target
        )

        board = crm_api.get_b2b_pipeline()
        on_board = {c["name"] for col in board["columns"].values() for c in col}
        self.assertNotIn(self.duplicate, on_board)

    def test_rejects_self_merge_and_unknown_and_empty_input(self):
        with self.assertRaises(Exception):
            leads_api.merge_leads(self.target, [self.target])
        with self.assertRaises(Exception):
            leads_api.merge_leads(self.target, [])
        with self.assertRaises(Exception):
            leads_api.merge_leads(self.target, ["Lead-does-not-exist"])
        with self.assertRaises(Exception):
            leads_api.merge_leads("Lead-does-not-exist", [self.duplicate])

    def test_a_source_cannot_be_merged_twice(self):
        leads_api.merge_leads(self.target, [self.duplicate])
        other = leads_api.save_lead({"lead_name": "_TEST Third"})["name"]
        with self.assertRaises(Exception):
            leads_api.merge_leads(other, [self.duplicate])

    def test_cannot_merge_into_a_lead_that_was_itself_merged_away(self):
        """Otherwise the result would be hidden along with its new target."""
        leads_api.merge_leads(self.target, [self.duplicate])
        other = leads_api.save_lead({"lead_name": "_TEST Fourth"})["name"]
        with self.assertRaises(Exception):
            leads_api.merge_leads(self.duplicate, [other])

    def test_merging_is_idempotent_on_branches(self):
        """Re-merging an identical branch does not duplicate the row."""
        same = leads_api.save_lead(
            {
                "lead_name": "_TEST Brand Copy",
                "branches": [{"branch_name": "Maadi", "area": "Maadi"}],
            }
        )["name"]
        leads_api.merge_leads(self.target, [same])
        doc = frappe.get_doc("Lead", self.target)
        self.assertEqual(len(doc.custom_branches), 1)

    def test_sources_accepts_a_json_string(self):
        """Frappe delivers list args as JSON strings over HTTP."""
        out = leads_api.merge_leads(self.target, json.dumps([self.duplicate]))
        self.assertEqual(out["merged"], [self.duplicate])

    def test_candidates_surface_the_duplicate_with_its_reasons(self):
        res = leads_api.get_merge_candidates(self.target)
        by_name = {c["name"]: c for c in res["candidates"]}
        self.assertIn(self.duplicate, by_name)
        # Matched on BOTH the brand name and the shared phone.
        self.assertGreaterEqual(by_name[self.duplicate]["score"], 2)
        self.assertIn("Same brand name", by_name[self.duplicate]["reasons"])

    def test_candidates_exclude_self_and_already_merged(self):
        res = leads_api.get_merge_candidates(self.target)
        self.assertNotIn(self.target, {c["name"] for c in res["candidates"]})

        leads_api.merge_leads(self.target, [self.duplicate])
        res = leads_api.get_merge_candidates(self.target)
        self.assertNotIn(self.duplicate, {c["name"] for c in res["candidates"]})

    def test_candidates_search_by_name(self):
        res = leads_api.get_merge_candidates(self.target, query="_TEST Brand")
        self.assertIn(self.duplicate, {c["name"] for c in res["candidates"]})
        res = leads_api.get_merge_candidates(self.target, query="_TEST No Such")
        self.assertEqual(res["candidates"], [])


# ---------------------------------------------------------------------------
# 10) importer idempotency + rep-owned field preservation
# ---------------------------------------------------------------------------
class TestLeadContacts(unittest.TestCase):
    """Multiple people per lead: save/read, primary rules, merge, phone backfill.

    Skipped on a site that has not migrated ``custom_contacts`` yet, for the
    same reason as TestLeadSuitability — the CI logic gate does not migrate.
    """

    def setUp(self):
        if not _has_lead_field("custom_contacts"):
            self.skipTest("Lead.custom_contacts not migrated on this site yet.")
        _ensure_category(_COFFEE)
        _ensure_b2b_role()

    def tearDown(self):
        frappe.db.rollback()

    def _lead(self, **extra):
        payload = {"lead_name": "_TEST Contacts", "category": _COFFEE}
        payload.update(extra)
        return leads_api.save_lead(payload)["name"]

    # --- save through save_lead -------------------------------------------
    def test_save_lead_stores_multiple_people(self):
        name = self._lead(
            phone="0100000900",
            contacts=[
                {"contact_name": "Omar", "role": "Owner",
                 "phone": "0100000001", "email": "omar@example.com",
                 "is_primary": 1, "notes": "decides"},
                {"contact_name": "Sara", "role": "Shift Manager",
                 "phone": "0100000002"},
                {"contact_name": "Ali", "role": "Barista"},
            ],
        )

        detail = leads_api.get_lead(name)
        contacts = detail["contacts"]
        self.assertEqual(len(contacts), 3)
        self.assertEqual(
            [c["contact_name"] for c in contacts], ["Omar", "Sara", "Ali"]
        )
        self.assertEqual(
            [c["role"] for c in contacts], ["Owner", "Shift Manager", "Barista"]
        )
        self.assertTrue(contacts[0]["is_primary"])
        self.assertFalse(contacts[1]["is_primary"])
        self.assertEqual(contacts[0]["email"], "omar@example.com")
        self.assertEqual(contacts[0]["notes"], "decides")
        # A contact with no phone is still a valid person to record.
        self.assertEqual(contacts[2]["phone"], "")

    def test_omitting_contacts_key_leaves_people_untouched(self):
        name = self._lead(
            contacts=[{"contact_name": "Omar", "role": "Owner",
                       "phone": "0100000001"}]
        )
        leads_api.save_lead({"notes": "unrelated edit"}, name=name)
        self.assertEqual(len(leads_api.get_lead(name)["contacts"]), 1)

    # --- dedicated endpoint -----------------------------------------------
    def test_save_lead_contacts_replaces_wholesale(self):
        name = self._lead(
            contacts=[{"contact_name": "Omar", "role": "Owner",
                       "phone": "0100000001"}]
        )
        out = leads_api.save_lead_contacts(
            name,
            [
                {"contact_name": "Omar", "role": "Owner",
                 "phone": "0100000001"},
                {"contact_name": "Ali", "role": "Barista",
                 "phone": "0100000003", "is_primary": 1},
            ],
        )
        self.assertEqual(len(out["contacts"]), 2)
        # Explicit primary wins; the first row is NOT auto-promoted.
        self.assertFalse(out["contacts"][0]["is_primary"])
        self.assertTrue(out["contacts"][1]["is_primary"])
        self.assertEqual(len(leads_api.get_lead(name)["contacts"]), 2)

    def test_save_lead_contacts_accepts_a_json_string(self):
        """Frappe delivers list args as strings; the endpoint must parse them."""
        name = self._lead()
        out = leads_api.save_lead_contacts(
            name,
            json.dumps([{"contact_name": "Sara", "role": "Manager",
                         "phone": "0100000002"}]),
        )
        self.assertEqual(len(out["contacts"]), 1)
        self.assertEqual(out["contacts"][0]["contact_name"], "Sara")

    def test_save_lead_contacts_clears_the_list(self):
        name = self._lead(
            contacts=[{"contact_name": "Omar", "phone": "0100000001"}]
        )
        out = leads_api.save_lead_contacts(name, [])
        self.assertEqual(out["contacts"], [])
        self.assertEqual(leads_api.get_lead(name)["contacts"], [])

    def test_save_lead_contacts_rejects_an_unknown_lead(self):
        with self.assertRaises(Exception):
            leads_api.save_lead_contacts("_TEST No Such Lead", [])

    # --- normalisation rules ----------------------------------------------
    def test_blank_rows_are_dropped(self):
        name = self._lead()
        out = leads_api.save_lead_contacts(
            name,
            [
                {"contact_name": "", "role": "Barista", "phone": ""},
                {"contact_name": "  ", "phone": "  "},
                {"contact_name": "Real Person"},
                "not a dict",
            ],
        )
        self.assertEqual(len(out["contacts"]), 1)
        self.assertEqual(out["contacts"][0]["contact_name"], "Real Person")

    def test_first_row_is_promoted_when_none_is_flagged(self):
        name = self._lead()
        out = leads_api.save_lead_contacts(
            name,
            [
                {"contact_name": "Sara", "phone": "0100000002"},
                {"contact_name": "Ali", "phone": "0100000003"},
            ],
        )
        self.assertTrue(out["contacts"][0]["is_primary"])
        self.assertFalse(out["contacts"][1]["is_primary"])

    def test_only_one_row_stays_primary(self):
        name = self._lead()
        out = leads_api.save_lead_contacts(
            name,
            [
                {"contact_name": "Sara", "phone": "0100000002", "is_primary": 1},
                {"contact_name": "Ali", "phone": "0100000003", "is_primary": 1},
            ],
        )
        self.assertEqual([c["is_primary"] for c in out["contacts"]], [True, False])

    # --- lead phone backfill ----------------------------------------------
    def test_primary_contact_backfills_a_missing_lead_phone(self):
        name = self._lead()
        self.assertFalse(frappe.db.get_value("Lead", name, "phone"))
        out = leads_api.save_lead_contacts(
            name,
            [{"contact_name": "Omar", "role": "Owner", "phone": "0100000001"}],
        )
        self.assertEqual(out["phone"], "0100000001")
        self.assertEqual(
            frappe.db.get_value("Lead", name, "phone"), "0100000001"
        )

    def test_an_existing_lead_phone_is_never_overwritten(self):
        name = self._lead(phone="0100000900")
        leads_api.save_lead_contacts(
            name,
            [{"contact_name": "Omar", "phone": "0100000001", "is_primary": 1}],
        )
        self.assertEqual(
            frappe.db.get_value("Lead", name, "phone"), "0100000900"
        )

    # --- catalog list ------------------------------------------------------
    def test_get_leads_carries_contacts(self):
        name = self._lead(
            contacts=[
                {"contact_name": "Omar", "role": "Owner",
                 "phone": "0100000001"},
                {"contact_name": "Ali", "role": "Barista"},
            ]
        )
        rows = leads_api.get_leads(category=_COFFEE)["leads"]
        row = next((r for r in rows if r["name"] == name), None)
        self.assertIsNotNone(row)
        self.assertEqual(len(row["contacts"]), 2)
        self.assertEqual(row["contacts"][0]["role"], "Owner")
        # Every row carries the key, even leads with nobody recorded.
        self.assertTrue(all("contacts" in r for r in rows))

    # --- merge -------------------------------------------------------------
    def test_merge_carries_people_and_dedups_on_phone(self):
        if not _has_lead_field("custom_merged_into"):
            self.skipTest("Lead.custom_merged_into not migrated on this site yet.")

        target = leads_api.save_lead(
            {
                "lead_name": "_TEST Contacts Brand",
                "category": _COFFEE,
                "contacts": [
                    {"contact_name": "Omar", "role": "Owner",
                     "phone": "0100000001", "is_primary": 1}
                ],
            }
        )["name"]
        duplicate = leads_api.save_lead(
            {
                "lead_name": "_TEST Contacts Brand",
                "category": _COFFEE,
                "contacts": [
                    # Same human, same number -> deduped away.
                    {"contact_name": "Omar Owner", "role": "Owner",
                     "phone": "0100000001", "is_primary": 1},
                    # New person -> carried over, but never as the primary.
                    {"contact_name": "Sara", "role": "Manager",
                     "phone": "0100000002", "is_primary": 1},
                ],
            }
        )["name"]

        merged = leads_api.merge_leads(target, [duplicate])["lead"]
        contacts = merged["contacts"]
        self.assertEqual(len(contacts), 2)
        self.assertEqual(
            [c["contact_name"] for c in contacts], ["Omar", "Sara"]
        )
        self.assertEqual([c["is_primary"] for c in contacts], [True, False])


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

        # Catalog metrics refreshed on the 2nd run. The fit score now lives on
        # custom_fit_score; the importer NEVER writes custom_lead_score (that field
        # is owned by the nightly CRM job compute_lead_scores).
        self.assertEqual(int(frappe.db.get_value("Lead", lead_a, "custom_fit_score")), 99)
        self.assertEqual(
            int(frappe.db.get_value("Lead", lead_a, "custom_lead_score") or 0), 0
        )
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


class TestLeadsTalabat(unittest.TestCase):
    """Talabat presence: a two-state, catalog-owned flag.

    Distinct from the Google service signals (takeout / dine_in / serves_dessert),
    which are one-way because the Places API omits them when false. This one is
    read off Talabat's own per-area listings, so 0 genuinely means "not listed in
    any area we swept" and filtering on 0 is meaningful.
    """

    SRC = "_TEST-TALABAT-SRC"

    def setUp(self):
        _ensure_category(_COFFEE)
        _ensure_b2b_role()
        if not frappe.db.has_column("Lead", "custom_on_talabat"):
            self.skipTest("site has not migrated the Talabat fields yet")

    def tearDown(self):
        frappe.db.rollback()

    def test_save_lead_round_trips_the_flag_and_zones(self):
        out = leads_api.save_lead(
            {
                "lead_name": "_TEST Talabat Cafe",
                "category": _COFFEE,
                "on_talabat": True,
                "talabat_areas": ["6th of October", "Sheikh Zayed"],
            }
        )
        doc = frappe.get_doc("Lead", out["name"])
        self.assertEqual(int(doc.custom_on_talabat), 1)
        self.assertEqual(
            json.loads(doc.custom_talabat_areas), ["6th of October", "Sheikh Zayed"]
        )

        got = leads_api.get_lead(out["name"])
        self.assertIs(got["on_talabat"], True)
        self.assertEqual(got["talabat_areas"], ["6th of October", "Sheikh Zayed"])

    def test_flag_defaults_off_and_survives_a_clearing_update(self):
        out = leads_api.save_lead(
            {"lead_name": "_TEST Talabat Off", "category": _COFFEE}
        )
        got = leads_api.get_lead(out["name"])
        self.assertIs(got["on_talabat"], False)
        self.assertEqual(got["talabat_areas"], [])

        leads_api.save_lead({"on_talabat": True, "talabat_areas": ["Sheikh Zayed"]},
                            name=out["name"])
        self.assertIs(leads_api.get_lead(out["name"])["on_talabat"], True)
        # ...and can be turned back off, unlike the one-way Google signals.
        leads_api.save_lead({"on_talabat": False, "talabat_areas": []},
                            name=out["name"])
        got = leads_api.get_lead(out["name"])
        self.assertIs(got["on_talabat"], False)
        self.assertEqual(got["talabat_areas"], [])

    def test_get_leads_filters_both_ways(self):
        on = leads_api.save_lead({
            "lead_name": "_TEST Talabat Yes", "category": _COFFEE,
            "on_talabat": True, "talabat_areas": ["Sheikh Zayed"],
        })["name"]
        off = leads_api.save_lead({
            "lead_name": "_TEST Talabat No", "category": _COFFEE,
        })["name"]

        listed = {l["name"] for l in leads_api.get_leads(on_talabat=1)["leads"]}
        self.assertIn(on, listed)
        self.assertNotIn(off, listed)

        unlisted = {l["name"] for l in leads_api.get_leads(on_talabat=0)["leads"]}
        self.assertIn(off, unlisted)
        self.assertNotIn(on, unlisted)

        # Omitting the filter returns both -- the client caches the whole catalog.
        every = {l["name"] for l in leads_api.get_leads()["leads"]}
        self.assertTrue({on, off} <= every)

    def test_importer_refreshes_the_flag_on_re_run(self):
        """Talabat presence is a catalog metric, so a later sweep must update it."""
        def _run(on, areas):
            doc = {"generated": "2026-08-25", "count": 1, "leads": [{
                "id": self.SRC, "name": "_TEST Talabat Import", "score": 50,
                "tier": "B", "category": _COFFEE, "onTalabat": on,
                "talabatAreas": areas,
                "branches": [{"name": "Zayed", "area": "Sheikh Zayed",
                              "onTalabat": on, "lat": 30.0, "lng": 31.0}],
            }]}
            fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                             encoding="utf-8")
            json.dump(doc, fh); fh.close()
            try:
                importer.run(fh.name)
            finally:
                os.unlink(fh.name)

        _run(False, [])
        name = frappe.db.get_value("Lead", {"custom_source_brand_id": self.SRC}, "name")
        self.assertTrue(name)
        self.assertEqual(int(frappe.db.get_value("Lead", name, "custom_on_talabat")), 0)

        _run(True, ["Sheikh Zayed"])
        self.assertEqual(int(frappe.db.get_value("Lead", name, "custom_on_talabat")), 1)
        self.assertEqual(
            json.loads(frappe.db.get_value("Lead", name, "custom_talabat_areas")),
            ["Sheikh Zayed"],
        )
        # The per-branch flag rides along, and the API decodes it as a real bool.
        branch = leads_api.get_lead(name)["branches"][0]
        self.assertIs(branch["on_talabat"], True)

    def test_a_merged_duplicate_hands_its_flag_to_the_survivor(self):
        """Google splits one business across branch names; a rep merges them.

        Without promotion the flag sits on the hidden duplicate and the badge
        never shows on the lead anyone actually works.
        """
        if not frappe.db.has_column("Lead", "custom_merged_into"):
            self.skipTest("site has not migrated the merge fields yet")

        survivor = leads_api.save_lead({
            "lead_name": "_TEST PAO Survivor", "category": _COFFEE,
            "on_talabat": True, "talabat_areas": ["Sheikh Zayed"],
        })["name"]
        dup = leads_api.save_lead({
            "lead_name": "_TEST PAO Duplicate", "category": _COFFEE,
        })["name"]
        frappe.db.set_value("Lead", dup, {
            "custom_merged_into": survivor,
            "custom_on_talabat": 1,
            "custom_talabat_areas": json.dumps(["6th of October"]),
        }, update_modified=False)

        promoted = importer._propagate_talabat_to_merge_targets()
        self.assertGreaterEqual(promoted, 1)

        # Union, not overwrite -- the survivor keeps the zone it already had.
        self.assertEqual(int(frappe.db.get_value("Lead", survivor, "custom_on_talabat")), 1)
        self.assertEqual(
            json.loads(frappe.db.get_value("Lead", survivor, "custom_talabat_areas")),
            ["6th of October", "Sheikh Zayed"],
        )
        # Idempotent: a second pass promotes nothing new for this pair.
        before = frappe.db.get_value("Lead", survivor, "custom_talabat_areas")
        importer._propagate_talabat_to_merge_targets()
        self.assertEqual(frappe.db.get_value("Lead", survivor, "custom_talabat_areas"), before)


if __name__ == "__main__":
    unittest.main()
