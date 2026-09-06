"""B2B account linking and branch-selection contract tests (pure unittest)."""

from __future__ import annotations

import unittest
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from jarz_pos.api import crm
from jarz_pos.api import customer as customer_api
from jarz_pos.setup import b2b_master_data


@contextmanager
def _roles(values):
    with patch.object(crm.frappe, "get_roles", return_value=list(values)):
        yield


class TestStandardCustomerRelationshipResolution(unittest.TestCase):
    def test_lead_customer_direct_link(self):
        with patch.object(crm.frappe.db, "get_value", return_value="CUST-1"), patch.object(
            crm, "_customer_rows_for_lead", return_value=[]
        ):
            self.assertEqual(crm._resolve_lead_customer("LEAD-1"), "CUST-1")

    def test_legacy_converted_customer_lead_name_fallback(self):
        with patch.object(crm.frappe.db, "get_value", return_value=None), patch.object(
            crm,
            "_customer_rows_for_lead",
            return_value=[{"name": "CUST-CONVERTED", "disabled": 0}],
        ):
            self.assertEqual(
                crm._resolve_lead_customer("LEAD-1"), "CUST-CONVERTED"
            )

    def test_multiple_converted_customers_are_ambiguous(self):
        with patch.object(crm.frappe.db, "get_value", return_value=None), patch.object(
            crm,
            "_customer_rows_for_lead",
            return_value=[{"name": "CUST-1"}, {"name": "CUST-2"}],
        ):
            with self.assertRaises(Exception):
                crm._resolve_lead_customer("LEAD-1")

    def test_direct_and_converted_disagreement_is_ambiguous(self):
        with patch.object(crm.frappe.db, "get_value", return_value="CUST-DIRECT"), patch.object(
            crm,
            "_customer_rows_for_lead",
            return_value=[{"name": "CUST-CONVERTED"}],
        ):
            with self.assertRaises(Exception):
                crm._resolve_lead_customer("LEAD-1")

    def test_opportunity_from_lead_follows_lead_without_repointing(self):
        opportunity = {"opportunity_from": "Lead", "party_name": "LEAD-1"}
        with patch.object(crm.frappe.db, "exists", return_value=True), patch.object(
            crm, "_resolve_lead_customer", return_value="CUST-1"
        ) as resolver:
            self.assertEqual(
                crm._resolve_opportunity_customer(opportunity), "CUST-1"
            )
        resolver.assert_called_once_with("LEAD-1", strict=True)


class TestLinkExistingCustomer(unittest.TestCase):
    def test_unauthorized_caller_is_denied_before_db_access(self):
        with _roles(["Sales User"]), patch.object(
            crm.frappe.db,
            "exists",
            side_effect=AssertionError("DB must not be read before the role gate"),
        ):
            with self.assertRaises(Exception):
                crm.link_existing_customer("Lead", "LEAD-1", "CUST-1")

    def test_b2b_rep_can_link_ordinary_existing_customer(self):
        with _roles(["B2B Sales Rep"]), patch.object(
            crm.frappe.db, "exists", return_value=True
        ), patch.object(crm, "_require_doc_permission"), patch.object(
            crm, "_assert_enabled_customer", return_value="CUST-1"
        ), patch.object(
            crm, "_link_lead_customer", return_value=True
        ) as linker:
            result = crm.link_existing_customer("Lead", "LEAD-1", "CUST-1")

        self.assertTrue(result["changed"])
        self.assertEqual(result["customer"], "CUST-1")
        linker.assert_called_once_with(
            "LEAD-1", "CUST-1", expected_customer=None, allow_relink=False
        )

    def test_disabled_customer_is_blocked(self):
        with _roles(["B2B Sales Rep"]), patch.object(
            crm.frappe.db, "exists", return_value=True
        ), patch.object(crm, "_require_doc_permission"), patch.object(
            crm.frappe.db,
            "get_value",
            return_value={"name": "CUST-1", "disabled": 1},
        ), patch.object(crm, "_link_lead_customer") as linker:
            with self.assertRaises(Exception):
                crm.link_existing_customer("Lead", "LEAD-1", "CUST-1")
        linker.assert_not_called()

    def test_stale_expected_customer_cannot_overwrite_new_link(self):
        with patch.object(crm, "_lock_relationship_row"), patch.object(
            crm, "_customer_rows_for_lead", return_value=[]
        ), patch.object(crm.frappe.db, "get_value", return_value="CUST-CURRENT"), patch.object(
            crm.frappe.db, "set_value"
        ) as setter:
            with self.assertRaises(Exception):
                crm._link_lead_customer(
                    "LEAD-1",
                    "CUST-NEW",
                    expected_customer="CUST-STALE",
                    allow_relink=True,
                )
        setter.assert_not_called()

    def test_converted_customer_is_authoritative(self):
        with patch.object(crm, "_lock_relationship_row"), patch.object(
            crm,
            "_customer_rows_for_lead",
            return_value=[{"name": "CUST-CONVERTED"}],
        ), patch.object(crm.frappe.db, "get_value", return_value=None), patch.object(
            crm.frappe.db, "set_value"
        ) as setter:
            with self.assertRaises(Exception):
                crm._link_lead_customer(
                    "LEAD-1",
                    "CUST-OTHER",
                    expected_customer="CUST-CONVERTED",
                    allow_relink=True,
                )
        setter.assert_not_called()

    def test_customer_origin_opportunity_is_not_repointed(self):
        def get_value(doctype, name, fields, **kwargs):
            self.assertEqual(doctype, "Opportunity")
            return {"opportunity_from": "Customer", "party_name": "CUST-OLD"}

        with _roles(["JARZ Manager"]), patch.object(
            crm.frappe.db, "exists", return_value=True
        ), patch.object(crm, "_require_doc_permission"), patch.object(
            crm, "_assert_enabled_customer", return_value="CUST-NEW"
        ), patch.object(
            crm, "_lock_relationship_row"
        ), patch.object(crm.frappe.db, "get_value", side_effect=get_value), patch.object(
            crm.frappe.db, "set_value"
        ) as setter:
            with self.assertRaises(Exception):
                crm.link_existing_customer(
                    "Opportunity", "OPP-1", "CUST-NEW", allow_relink=1
                )
        setter.assert_not_called()


class TestCustomerSearchAndBinding(unittest.TestCase):
    def test_search_includes_any_type_or_group_and_counts_addresses(self):
        customer = {
            "name": "ILO",
            "customer_name": "ILO Specialty Coffee",
            "customer_type": "Individual",
            "customer_group": None,
            "mobile_no": "0100",
            "territory": "EGMADINATY",
            "disabled": 0,
            "customer_primary_address": None,
        }
        calls = []

        def get_all(doctype, **kwargs):
            calls.append((doctype, kwargs))
            if doctype == "Customer":
                return [customer]
            return [
                {"link_name": "ILO", "parent": "ADDR-1"},
                {"link_name": "ILO", "parent": "ADDR-2"},
            ]

        def get_list(doctype, **kwargs):
            self.assertEqual(doctype, "Customer")
            calls.append((doctype, kwargs))
            return [customer]

        with _roles(["B2B Sales Rep"]), patch.object(
            crm.frappe, "get_list", side_effect=get_list
        ), patch.object(
            crm.frappe, "get_all", side_effect=get_all
        ), patch.object(
            crm, "_require_doc_permission"
        ):
            result = crm.search_linkable_customers("ILO")

        self.assertEqual(result[0]["customer_type"], "Individual")
        self.assertIsNone(result[0]["customer_group"])
        self.assertEqual(result[0]["address_count"], 2)
        customer_filters = calls[0][1]["filters"]
        self.assertEqual(customer_filters, {"disabled": 0})
        self.assertNotIn("customer_type", customer_filters)
        self.assertNotIn("customer_group", customer_filters)

    def test_customer_account_entry_point_uses_customer_directly(self):
        customer_doc = MagicMock()
        customer_doc.lead_name = None
        customer_doc.customer_name = "ILO Specialty Coffee"
        customer_doc.party_name = None
        customer_doc.owner = "rep@example.com"
        with _roles(["B2B Sales Rep"]), patch.object(
            crm, "_doctype_exists", return_value=True
        ), patch.object(crm.frappe.db, "exists", return_value=True), patch.object(
            crm, "_require_doc_permission"
        ) as permission, patch.object(
            crm.frappe, "get_doc", return_value=customer_doc
        ), patch.object(crm, "_recent_b2b_invoices", return_value=[]), patch.object(
            crm, "_open_todos_for", return_value=[]
        ), patch.object(crm, "_journey_notes", return_value=[]), patch.object(
            crm, "_label_summary_for_customer", return_value=None
        ), patch.object(crm, "_has_field", return_value=False):
            result = crm.get_account("Customer", "ILO")

        self.assertEqual(result["customer"], "ILO")
        self.assertEqual(result["title"], "ILO Specialty Coffee")
        permission.assert_any_call("Customer", "ILO", "read")

    def test_binding_uses_converted_customer_instead_of_creating_duplicate(self):
        with patch.object(crm.frappe.db, "exists", return_value=True), patch.object(
            crm, "_resolve_lead_customer", return_value="CUST-CONVERTED"
        ), patch.object(crm, "_require_doc_permission"), patch.object(
            crm, "_lock_relationship_row"
        ), patch.object(crm, "_assert_enabled_customer"), patch.object(
            crm, "_policy_price_list", return_value="B2B Selling"
        ), patch.object(
            crm,
            "_order_address_selection",
            return_value={
                "address_book": {"branch_options": []},
                "requires_shipping_address_selection": True,
                "shipping_address_name": None,
                "effective_territory": None,
                "territory_pos_profile": None,
            },
        ), patch("jarz_pos.api.customer.create_customer") as create_customer:
            result = crm._resolve_order_binding(
                "Lead", "LEAD-1", crm._B2B_ORDER_PURPOSE
            )

        self.assertEqual(result["customer"], "CUST-CONVERTED")
        create_customer.assert_not_called()

    def test_new_customer_is_durably_bound_to_source_lead(self):
        with patch.object(crm.frappe.db, "exists", return_value=True), patch.object(
            crm, "_resolve_lead_customer", side_effect=[None, "CUST-NEW"]
        ), patch.object(crm, "_require_doc_permission"), patch.object(
            crm, "_lock_relationship_row"
        ), patch.object(crm, "_assert_enabled_customer"), patch.object(
            crm, "_policy_price_list", return_value=None
        ), patch.object(
            crm, "_order_address_selection", return_value={}
        ), patch(
            "jarz_pos.api.customer.create_customer",
            return_value={"name": "CUST-NEW"},
        ) as create_customer:
            result = crm._resolve_order_binding(
                "Lead",
                "LEAD-1",
                crm._B2B_ORDER_PURPOSE,
                customer_name="New Company",
                mobile_no="0100",
                customer_primary_address="1 Road",
                territory_id="TERR-1",
            )

        self.assertEqual(result["customer"], "CUST-NEW")
        self.assertEqual(create_customer.call_args.kwargs["source_lead"], "LEAD-1")

    def test_customer_origin_opportunity_with_broken_party_never_creates_customer(self):
        def exists(doctype, name):
            return doctype == "Opportunity"

        with patch.object(crm.frappe.db, "exists", side_effect=exists), patch.object(
            crm.frappe.db,
            "get_value",
            return_value={"opportunity_from": "Customer", "party_name": "MISSING"},
        ), patch.object(crm, "_require_doc_permission"), patch.object(
            crm, "_lock_relationship_row"
        ), patch("jarz_pos.api.customer.create_customer") as create_customer:
            with self.assertRaises(Exception):
                crm._resolve_order_binding(
                    "Opportunity",
                    "OPP-1",
                    crm._B2B_ORDER_PURPOSE,
                    customer_name="Wrong New Customer",
                    mobile_no="0100",
                    customer_primary_address="1 Road",
                    territory_id="TERR-1",
                )
        create_customer.assert_not_called()


class TestSourceLeadCreateBoundary(unittest.TestCase):
    def test_direct_source_lead_create_requires_b2b_role(self):
        with _roles(["Sales User"]), patch.object(
            customer_api.frappe.db, "exists", return_value=True
        ), patch.object(customer_api.frappe, "get_doc") as get_doc:
            with self.assertRaises(Exception):
                customer_api.create_customer(
                    customer_name="Unauthorised",
                    mobile_no="0100",
                    customer_primary_address="LEAD-ADDR",
                    territory_id="TERR-1",
                    source_lead="LEAD-1",
                )
        self.assertFalse(
            any(
                call.args and isinstance(call.args[0], dict)
                and call.args[0].get("doctype") == "Customer"
                for call in get_doc.call_args_list
            )
        )

    def test_source_lead_cannot_claim_foreign_address(self):
        def exists(doctype, value):
            if doctype in ("Lead", "Address"):
                return True
            if doctype == "Dynamic Link":
                return False
            return False

        with _roles(["B2B Sales Rep"]), patch.object(
            customer_api.frappe.db, "exists", side_effect=exists
        ), patch.object(crm, "_require_doc_permission"), patch.object(
            crm, "_lock_relationship_row"
        ), patch.object(crm, "_resolve_lead_customer", return_value=None), patch.object(
            customer_api.frappe, "get_doc"
        ) as get_doc:
            with self.assertRaises(Exception):
                customer_api.create_customer(
                    customer_name="New Company",
                    mobile_no="0100",
                    customer_primary_address="FOREIGN-ADDR",
                    territory_id="TERR-1",
                    source_lead="LEAD-1",
                )
        self.assertFalse(
            any(
                call.args and isinstance(call.args[0], dict)
                and call.args[0].get("doctype") == "Customer"
                for call in get_doc.call_args_list
            )
        )


class TestB2BDocPermissions(unittest.TestCase):
    def test_unrestricted_rows_are_created_without_customer_desk_create(self):
        inserted = []

        def exists(doctype, filters=None):
            if doctype in ("Role", "DocType"):
                return True
            if doctype == "Custom DocPerm":
                self.assertEqual(filters["if_owner"], 0)
                return None
            return False

        def get_doc(payload, name=None):
            self.assertIsNone(name)
            doc = MagicMock()
            doc.insert.side_effect = lambda **_kwargs: inserted.append(dict(payload))
            return doc

        log = {"created": [], "existing": []}
        with patch.object(
            b2b_master_data.frappe.db, "exists", side_effect=exists
        ), patch.object(
            b2b_master_data.frappe, "get_doc", side_effect=get_doc
        ), patch.object(
            b2b_master_data.frappe, "clear_cache"
        ), patch("frappe.permissions.setup_custom_perms"):
            b2b_master_data._ensure_b2b_docperms(log)

        by_doctype = {row["parent"]: row for row in inserted}
        self.assertEqual(
            set(by_doctype), {"Customer", "Address", "Lead", "Opportunity"}
        )
        self.assertEqual(by_doctype["Customer"]["read"], 1)
        self.assertEqual(by_doctype["Customer"]["create"], 0)
        self.assertEqual(by_doctype["Customer"]["write"], 0)
        self.assertEqual(by_doctype["Address"]["read"], 1)
        self.assertEqual(by_doctype["Address"]["write"], 0)
        self.assertEqual(by_doctype["Address"]["delete"], 0)
        self.assertEqual(by_doctype["Lead"]["write"], 1)
        self.assertEqual(by_doctype["Opportunity"]["write"], 1)

    def test_existing_customer_row_has_broad_permissions_removed(self):
        stored = {"read": 1, "write": 1, "create": 1, "delete": 1}
        doc = MagicMock()
        doc.get.side_effect = lambda fieldname: stored.get(fieldname, 0)
        doc.set.side_effect = lambda fieldname, value: stored.__setitem__(fieldname, value)

        def exists(doctype, filters=None):
            if doctype in ("Role", "DocType"):
                return True
            if doctype == "Custom DocPerm" and filters["parent"] == "Customer":
                self.assertEqual(filters["if_owner"], 0)
                return "PERM-CUSTOMER"
            return None

        log = {"created": [], "existing": []}
        with patch.object(
            b2b_master_data.frappe.db, "exists", side_effect=exists
        ), patch.object(
            b2b_master_data.frappe, "get_doc", return_value=doc
        ), patch.object(
            b2b_master_data.frappe, "clear_cache"
        ), patch("frappe.permissions.setup_custom_perms"):
            b2b_master_data._ensure_b2b_docperms(log)

        self.assertEqual(stored["read"], 1)
        self.assertEqual(stored["write"], 0)
        self.assertEqual(stored["create"], 0)
        self.assertEqual(stored["delete"], 0)
        doc.save.assert_called_once_with(ignore_permissions=True)

class TestBranchSelectionMetadata(unittest.TestCase):
    def test_foreign_address_is_rejected(self):
        with patch(
            "jarz_pos.api.customer._build_customer_shipping_address_book",
            return_value={"addresses": [], "branch_options": []},
        ), patch(
            "jarz_pos.utils.customer_address_utils.resolve_customer_shipping_address",
            return_value={"name": "ADDR-FALLBACK"},
        ), patch(
            "jarz_pos.utils.customer_address_utils.preferred_address_was_honoured",
            return_value=False,
        ):
            with self.assertRaises(Exception):
                crm._order_address_selection("CUST-1", "ADDR-FOREIGN")

    def test_unresolved_branch_does_not_inherit_customer_default_territory(self):
        unresolved = {
            "name": "ADDR-UNKNOWN",
            "address_line1": "Unknown branch",
            "address_line2": "",
            "city": "Unknown",
            "state": "",
        }
        address_book = {
            "addresses": [unresolved],
            "branch_options": [
                {
                    "address_name": "ADDR-UNKNOWN",
                    "address_territory": None,
                    "effective_territory": None,
                    "territory_missing": True,
                }
            ],
        }
        with patch(
            "jarz_pos.api.customer._build_customer_shipping_address_book",
            return_value=address_book,
        ), patch(
            "jarz_pos.utils.customer_address_utils.resolve_customer_shipping_address",
            return_value=unresolved,
        ), patch(
            "jarz_pos.utils.customer_address_utils.preferred_address_was_honoured",
            return_value=True,
        ), patch(
            "jarz_pos.utils.invoice_utils._territory_from_address_row",
            return_value=None,
        ), patch(
            "jarz_pos.utils.invoice_utils.resolve_pos_profile_for_territory",
            side_effect=AssertionError("must not resolve a profile without address territory"),
        ):
            with self.assertRaises(Exception):
                crm._order_address_selection("CUST-1", "ADDR-UNKNOWN")

    def test_multiple_valid_branches_require_explicit_selection(self):
        address_book = {
            "addresses": [],
            "branch_options": [
                {"address_name": "ADDR-1", "effective_territory": "TERR-1"},
                {"address_name": "ADDR-2", "effective_territory": "TERR-2"},
            ],
        }
        with patch(
            "jarz_pos.api.customer._build_customer_shipping_address_book",
            return_value=address_book,
        ):
            result = crm._order_address_selection("CUST-1")

        self.assertTrue(result["requires_shipping_address_selection"])
        self.assertEqual(result["address_selection_error"], "selection_required")

    def test_no_branch_returns_actionable_empty_state(self):
        with patch(
            "jarz_pos.api.customer._build_customer_shipping_address_book",
            return_value={"addresses": [], "branch_options": []},
        ):
            result = crm._order_address_selection("CUST-1")

        self.assertTrue(result["requires_shipping_address_selection"])
        self.assertEqual(result["address_selection_error"], "no_shipping_address")

    def test_ilo_legacy_rows_group_into_two_territory_safe_options(self):
        customer = MagicMock(name="customer")
        customer.name = "ilo specialty coffee"
        customer.customer_name = "ilo specialty coffee"

        def row(name, line, city, primary=0, shipping=1):
            return {
                "name": name,
                "address_title": "ilo specialty coffee-16815",
                "address_line1": line,
                "address_line2": "",
                "full_address": f"{line}, {city}",
                "city": city,
                "state": "",
                "country": "Egypt",
                "phone": "",
                "is_primary_address": bool(primary),
                "is_shipping_address": bool(shipping),
                "modified": "2026-01-01",
            }

        heliopolis = "104 عمر بن الخطاب مصر الجديدة"
        madinaty_a = "مدينتي allseasonpark"
        madinaty_b = "مدينتي All season Park"
        rows = [
            row("HEL-1", heliopolis, "EGMASRJD", primary=1),
            row("HEL-2", heliopolis, "Heliopolis - مصر الجديده"),
            row("HEL-3", heliopolis, "EGMASRJD", shipping=0),
            row("MAD-RESOLVED", madinaty_a, "EGMADINATY"),
            row("MAD-UNKNOWN-1", madinaty_a, "Unknown", shipping=0),
            row("MAD-UNKNOWN-2", madinaty_b, "Unknown", shipping=0),
        ]

        def address_territory(address):
            city = address.get("city")
            if city in ("EGMASRJD", "Heliopolis - مصر الجديده"):
                return "EGMASRJD"
            if city == "EGMADINATY":
                return "EGMADINATY"
            return None

        with patch(
            "jarz_pos.utils.invoice_utils._territory_from_address_row",
            side_effect=address_territory,
        ), patch(
            "jarz_pos.utils.invoice_utils.resolve_order_territory",
            side_effect=lambda _customer, resolved_shipping_address=None, **_kwargs: (
                address_territory(resolved_shipping_address) or "EGMADINATY"
            ),
        ), patch(
            "jarz_pos.utils.invoice_utils.resolve_pos_profile_for_territory",
            side_effect=lambda territory: f"PROFILE-{territory}",
        ):
            options = customer_api._build_customer_branch_options(customer, rows)

        self.assertEqual(len(options), 2)
        by_territory = {option["address_territory"]: option for option in options}
        self.assertEqual(by_territory["EGMASRJD"]["duplicate_count"], 2)
        self.assertEqual(by_territory["EGMADINATY"]["duplicate_count"], 2)
        self.assertEqual(by_territory["EGMADINATY"]["address_name"], "MAD-RESOLVED")
        self.assertEqual(
            set(by_territory["EGMADINATY"]["member_address_names"]),
            {"MAD-RESOLVED", "MAD-UNKNOWN-1", "MAD-UNKNOWN-2"},
        )

    def test_same_line_in_two_known_territories_stays_two_choices(self):
        customer = MagicMock()
        customer.name = "CUST-1"
        customer.customer_name = "Customer One"
        rows = [
            {
                "name": "ADDR-A", "address_title": "A", "address_line1": "12 Road",
                "address_line2": "", "city": "CITY-A", "state": "", "country": "Egypt",
                "full_address": "12 Road, CITY-A", "is_shipping_address": True,
                "is_primary_address": False, "modified": "2026-01-01",
            },
            {
                "name": "ADDR-B", "address_title": "B", "address_line1": "12 Road",
                "address_line2": "", "city": "CITY-B", "state": "", "country": "Egypt",
                "full_address": "12 Road, CITY-B", "is_shipping_address": True,
                "is_primary_address": False, "modified": "2026-01-01",
            },
        ]
        with patch(
            "jarz_pos.utils.invoice_utils._territory_from_address_row",
            side_effect=lambda row: {"CITY-A": "TERR-A", "CITY-B": "TERR-B"}[row["city"]],
        ), patch(
            "jarz_pos.utils.invoice_utils.resolve_order_territory",
            side_effect=lambda _customer, resolved_shipping_address=None, **_kwargs: (
                {"CITY-A": "TERR-A", "CITY-B": "TERR-B"}[resolved_shipping_address["city"]]
            ),
        ), patch(
            "jarz_pos.utils.invoice_utils.resolve_pos_profile_for_territory",
            return_value="PROFILE",
        ):
            options = customer_api._build_customer_branch_options(customer, rows)

        self.assertEqual(len(options), 2)
        self.assertEqual({o["address_name"] for o in options}, {"ADDR-A", "ADDR-B"})


if __name__ == "__main__":
    unittest.main()
