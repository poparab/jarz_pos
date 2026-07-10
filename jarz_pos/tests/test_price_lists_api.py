"""Unit tests for the B2B Price List management API (pure mock / unittest).

Everything is mocked at the module's ``frappe`` boundary (no FrappeTestCase, no
``erpnext.tests.utils`` import) so the module is non-destructive under
``--skip-before-tests`` and runs against a populated site without touching real data.

Covers:
  - manager write-gate denies non-managers; read-gate allows a B2B Sales Rep
  - create_price_list idempotency
  - set_category_price upsert (insert / update / delete-on-null)
  - set_item_override upsert + delete-on-null
  - assign_customer_to_price_list set / clear
  - _customers_for_price_list union of direct + group-derived assignments
  - get_price_list_detail separates category rows from per-item overrides
  - get_customer_pricing reverse view source resolution (override/category/none)
"""

from __future__ import annotations

import unittest
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import frappe

from jarz_pos.api import price_lists as pl


@contextmanager
def _roles(role_list):
    """Pin the session role set for both the manager gate and the B2B gate.

    ``pl.frappe`` and ``crm.frappe`` / ``pos.frappe`` are the same module object, so a
    single patch of ``frappe.get_roles`` drives every gate the endpoints consult.
    """
    with patch.object(pl.frappe, "get_roles", return_value=list(role_list)):
        yield


class TestPricingGates(unittest.TestCase):
    """Writes require a FULL manager (JARZ Manager); reads allow managers,
    line managers OR B2B Sales Reps (read-only)."""

    def test_write_denies_non_manager(self):
        with _roles(["Sales User"]):
            with self.assertRaises(Exception):
                pl.create_price_list("Companies")

    def test_write_denies_b2b_rep(self):
        # A B2B rep can READ pricing but must never WRITE it.
        with _roles(["B2B Sales Rep"]):
            with self.assertRaises(Exception):
                pl.create_price_list("Companies")

    def test_write_denies_line_manager(self):
        # JARZ line manager has manager-pricing access but is walled off from B2B,
        # so must NOT be able to edit prices (read-only only).
        with _roles(["JARZ line manager"]):
            with self.assertRaises(Exception):
                pl.create_price_list("Companies")

    def test_read_allows_line_manager(self):
        # ...but a line manager CAN read pricing.
        with _roles(["JARZ line manager"]):
            with patch.object(pl, "_pricing_categories", return_value=[]):
                self.assertEqual(pl.list_pricing_categories(), {"categories": []})

    def test_read_allows_b2b_rep(self):
        with _roles(["B2B Sales Rep"]):
            with patch.object(
                pl, "_pricing_categories", return_value=[{"item_group": "Medium", "item_count": 3}]
            ):
                out = pl.list_pricing_categories()
        self.assertEqual(out["categories"][0]["item_group"], "Medium")

    def test_read_allows_manager(self):
        with _roles(["JARZ Manager"]):
            with patch.object(pl, "_pricing_categories", return_value=[]):
                self.assertEqual(pl.list_pricing_categories(), {"categories": []})

    def test_read_denies_plain_user(self):
        with _roles(["Sales User"]):
            with self.assertRaises(Exception):
                pl.list_pricing_categories()


class TestCreatePriceList(unittest.TestCase):
    """Idempotent get-or-create of a selling price list."""

    def test_idempotent_when_existing(self):
        with _roles(["JARZ Manager"]):
            with patch.object(pl.frappe.db, "exists", return_value=True), patch.object(
                pl.frappe, "get_doc"
            ) as mock_get_doc:
                out = pl.create_price_list("Companies")
        self.assertEqual(out, {"name": "Companies"})
        mock_get_doc.assert_not_called()  # never re-created / overwritten

    def test_creates_when_missing(self):
        with _roles(["JARZ Manager"]):
            with patch.object(pl.frappe.db, "exists", return_value=False), patch.object(
                pl.frappe, "get_doc"
            ) as mock_get_doc:
                mock_get_doc.return_value.name = "Companies"
                out = pl.create_price_list("Companies", currency="EGP")
        payload = mock_get_doc.call_args[0][0]
        self.assertEqual(payload["doctype"], "Price List")
        self.assertEqual(payload["selling"], 1)
        self.assertEqual(payload["buying"], 0)
        self.assertEqual(payload["enabled"], 1)
        self.assertEqual(payload["currency"], "EGP")
        mock_get_doc.return_value.insert.assert_called_once()
        self.assertEqual(out, {"name": "Companies"})

    def test_blank_name_rejected(self):
        with _roles(["JARZ Manager"]):
            with self.assertRaises(Exception):
                pl.create_price_list("   ")


class TestSetCategoryPrice(unittest.TestCase):
    """Upsert / delete of the ``Jarz Price List Category Rate`` row (NOT an Item Price)."""

    def test_insert_new(self):
        with _roles(["JARZ Manager"]):
            with patch.object(pl.frappe.db, "exists", return_value=True), patch.object(
                pl, "_find_category_price", return_value=None
            ), patch.object(pl, "_price_list_currency", return_value="EGP"), patch.object(
                pl.frappe, "get_doc"
            ) as mock_get_doc:
                out = pl.set_category_price("Companies", "Medium", 75)
        payload = mock_get_doc.call_args[0][0]
        self.assertEqual(payload["doctype"], pl._CATEGORY_RATE_DOCTYPE)
        self.assertEqual(payload["item_group"], "Medium")
        self.assertEqual(payload["price_list"], "Companies")
        self.assertEqual(payload["rate"], 75.0)
        self.assertNotIn("item_code", payload)  # no Item Price fan-out
        self.assertNotIn("customer", payload)
        mock_get_doc.return_value.insert.assert_called_once()
        self.assertEqual(out, {"ok": True})

    def test_update_existing(self):
        with _roles(["JARZ Manager"]):
            with patch.object(pl.frappe.db, "exists", return_value=True), patch.object(
                pl, "_find_category_price", return_value="CAT-RATE-1"
            ), patch.object(pl.frappe.db, "set_value") as mock_set, patch.object(
                pl.frappe, "get_doc"
            ) as mock_get_doc:
                out = pl.set_category_price("Companies", "Medium", 80)
        mock_set.assert_called_once_with(
            pl._CATEGORY_RATE_DOCTYPE, "CAT-RATE-1", "rate", 80.0, update_modified=True
        )
        mock_get_doc.assert_not_called()
        self.assertEqual(out, {"ok": True})

    def test_delete_on_null(self):
        with _roles(["JARZ Manager"]):
            with patch.object(pl.frappe.db, "exists", return_value=True), patch.object(
                pl, "_find_category_price", return_value="CAT-RATE-1"
            ), patch.object(pl.frappe, "delete_doc") as mock_del:
                out = pl.set_category_price("Companies", "Medium", None)
        mock_del.assert_called_once_with(
            pl._CATEGORY_RATE_DOCTYPE, "CAT-RATE-1", ignore_permissions=True
        )
        self.assertEqual(out, {"ok": True})


class TestSetItemOverride(unittest.TestCase):
    """Upsert / delete of the generic per-item override Item Price."""

    def test_insert_new(self):
        with _roles(["JARZ Manager"]):
            with patch.object(pl.frappe.db, "exists", return_value=True), patch.object(
                pl, "_find_item_override", return_value=None
            ), patch.object(pl, "_price_list_currency", return_value="EGP"), patch.object(
                pl.frappe, "get_doc"
            ) as mock_get_doc:
                out = pl.set_item_override("Companies", "COFFEE-M-01", 65)
        payload = mock_get_doc.call_args[0][0]
        self.assertEqual(payload["item_code"], "COFFEE-M-01")
        self.assertEqual(payload["price_list_rate"], 65.0)
        self.assertNotIn("customer", payload)
        mock_get_doc.return_value.insert.assert_called_once()
        self.assertEqual(out, {"ok": True})

    def test_update_existing(self):
        with _roles(["JARZ Manager"]):
            with patch.object(pl.frappe.db, "exists", return_value=True), patch.object(
                pl, "_find_item_override", return_value="IP-OV-1"
            ), patch.object(pl.frappe.db, "set_value") as mock_set:
                out = pl.set_item_override("Companies", "COFFEE-M-01", 70)
        mock_set.assert_called_once_with(
            "Item Price", "IP-OV-1", "price_list_rate", 70.0, update_modified=True
        )
        self.assertEqual(out, {"ok": True})

    def test_delete_on_null(self):
        with _roles(["JARZ Manager"]):
            with patch.object(pl.frappe.db, "exists", return_value=True), patch.object(
                pl, "_find_item_override", return_value="IP-OV-1"
            ), patch.object(pl.frappe, "delete_doc") as mock_del:
                out = pl.set_item_override("Companies", "COFFEE-M-01", None)
        mock_del.assert_called_once_with("Item Price", "IP-OV-1", ignore_permissions=True)
        self.assertEqual(out, {"ok": True})


class TestAssignCustomerToPriceList(unittest.TestCase):
    """Set / clear Customer.default_price_list."""

    def test_set(self):
        with _roles(["JARZ Manager"]):
            with patch.object(pl.frappe.db, "exists", return_value=True), patch.object(
                pl.frappe.db, "set_value"
            ) as mock_set:
                out = pl.assign_customer_to_price_list("CUST-A", "Companies")
        mock_set.assert_called_once_with(
            "Customer", "CUST-A", "default_price_list", "Companies", update_modified=True
        )
        self.assertEqual(out, {"ok": True})

    def test_clear_reverts_to_group(self):
        with _roles(["JARZ Manager"]):
            with patch.object(pl.frappe.db, "exists", return_value=True), patch.object(
                pl.frappe.db, "set_value"
            ) as mock_set:
                out = pl.assign_customer_to_price_list("CUST-A", None)
        # Cleared -> None so the customer inherits its Customer Group default.
        mock_set.assert_called_once_with(
            "Customer", "CUST-A", "default_price_list", None, update_modified=True
        )
        self.assertEqual(out, {"ok": True})


class TestCustomersForPriceList(unittest.TestCase):
    """The bidirectional customer<->list resolution (direct UNION group-derived)."""

    def _fake_get_all(self, doctype, filters=None, fields=None, pluck=None, **kw):
        filters = filters or {}
        if doctype == "Customer Group":
            # Groups whose default_price_list == the target list.
            return ["Companies"]
        if doctype == "Customer":
            if filters.get("default_price_list") == "Companies":
                # Direct assignment query.
                return [
                    {"name": "CUST-A", "customer_name": "A Co", "customer_group": "Retail"}
                ]
            # Group-derived query: customer_group in [...] AND no own default list.
            return [
                {"name": "CUST-B", "customer_name": "B Co", "customer_group": "Companies"}
            ]
        return []

    def test_direct_and_group_union(self):
        with patch.object(pl.frappe, "get_all", side_effect=self._fake_get_all):
            out = pl._customers_for_price_list("Companies")
        by_name = {c["customer"]: c for c in out}
        self.assertEqual(by_name["CUST-A"]["assignment"], "direct")
        self.assertEqual(by_name["CUST-B"]["assignment"], "group")
        self.assertEqual(len(out), 2)

    def test_direct_wins_no_double_count(self):
        # A customer returned by BOTH queries must appear once, as "direct".
        def fake(doctype, filters=None, fields=None, pluck=None, **kw):
            filters = filters or {}
            if doctype == "Customer Group":
                return ["Companies"]
            if doctype == "Customer":
                row = {"name": "CUST-X", "customer_name": "X", "customer_group": "Companies"}
                return [row]
            return []

        with patch.object(pl.frappe, "get_all", side_effect=fake):
            out = pl._customers_for_price_list("Companies")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["assignment"], "direct")


class TestGetPriceListDetail(unittest.TestCase):
    """Detail view separates category rows from per-item overrides + lists customers."""

    def test_separates_categories_and_overrides(self):
        categories = [{"item_group": "Medium", "rate": 75.0, "item_count": 4}]
        overrides = [
            {"item_code": "COFFEE-M-01", "item_name": "Latte M", "item_group": "Medium", "rate": 65.0}
        ]
        customers = [
            {"customer": "CUST-A", "customer_name": "A", "assignment": "direct", "customer_group": "Retail"},
            {"customer": "CUST-B", "customer_name": "B", "assignment": "group", "customer_group": "Companies"},
        ]
        with _roles(["JARZ Manager"]):
            with patch.object(pl.frappe.db, "exists", return_value=True), patch.object(
                pl.frappe.db, "get_value", return_value={"currency": "EGP", "enabled": 1}
            ), patch.object(pl, "_default_selling_price_list", return_value="Standard Selling"), patch.object(
                pl, "_category_rows_for_list", return_value=categories
            ), patch.object(
                pl, "_item_overrides_for_list", return_value=overrides
            ), patch.object(
                pl, "_customers_for_price_list", return_value=customers
            ):
                out = pl.get_price_list_detail("Companies")
        # Category rows carry no item_code; override rows do -> the two are disjoint.
        self.assertEqual(out["categories"], categories)
        self.assertEqual(out["item_overrides"], overrides)
        self.assertTrue(all("item_code" not in c for c in out["categories"]))
        self.assertTrue(all(o["item_code"] for o in out["item_overrides"]))
        self.assertEqual({c["assignment"] for c in out["customers"]}, {"direct", "group"})
        self.assertFalse(out["is_default"])  # Companies != Standard Selling


class TestGetCustomerPricing(unittest.TestCase):
    """Reverse ('double entry') view resolves per-source rows for a customer."""

    def _run(self, effective, assignment, category_rate):
        cats = [{"item_group": "Medium", "item_count": 2}]
        overrides = [
            {"item_code": "COFFEE-M-01", "item_name": "Latte M", "item_group": "Medium", "rate": 65.0}
        ]
        with _roles(["B2B Sales Rep"]):
            with patch.object(pl.frappe.db, "exists", return_value=True), patch.object(
                pl.frappe.db, "get_value", return_value={"customer_name": "A Co", "customer_group": "Companies"}
            ), patch.object(
                pl, "_customer_effective_list", return_value=(effective, assignment)
            ), patch.object(
                pl, "_pricing_categories", return_value=cats
            ), patch.object(
                pl, "_category_rate", return_value=category_rate
            ), patch.object(
                pl, "_item_overrides_for_list", return_value=overrides
            ):
                return pl.get_customer_pricing("CUST-A")

    def test_sources_category_and_override(self):
        out = self._run("Companies", "direct", 75.0)
        self.assertEqual(out["effective_price_list"], "Companies")
        self.assertEqual(out["assignment"], "direct")
        sources = {(p["item_code"], p["source"]): p["rate"] for p in out["prices"]}
        self.assertEqual(sources[(None, "category")], 75.0)
        self.assertEqual(sources[("COFFEE-M-01", "override")], 65.0)

    def test_source_none_when_category_unpriced(self):
        # Category exists but has no configured rate in the effective list -> source none.
        out = self._run("Companies", "group", None)
        cat_row = next(p for p in out["prices"] if p["item_code"] is None)
        self.assertEqual(cat_row["source"], "none")
        self.assertEqual(cat_row["rate"], 0.0)

    def test_no_effective_list_returns_empty_prices(self):
        out = self._run(None, "none", 75.0)
        self.assertEqual(out["assignment"], "none")
        self.assertEqual(out["prices"], [])


if __name__ == "__main__":
    unittest.main()
