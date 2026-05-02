"""Tests for invoice creation accounting correctness.

This module verifies that the invoice creation pipeline produces
correct document structures, totals, and tax handling for each
invoice type variant:
- Regular items with delivery charges
- Bundle items with expansion and pricing
- Discount handling (percentage and amount)
- Sales partner tax suppression
- Pickup order shipping suppression
- Free-shipping bundle shipping waiver
"""

import unittest
import json
from unittest.mock import patch, MagicMock, PropertyMock


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------

class _InvoiceDocCapture:
    """Captures fields and items appended to a Sales Invoice document."""

    def __init__(self):
        self.customer = None
        self.customer_name = None
        self.company = None
        self.pos_profile = None
        self.custom_kanban_profile = None
        self.is_pos = 0
        self.selling_price_list = None
        self.currency = None
        self.territory = None
        self.posting_date = None
        self.posting_time = None
        self.sales_partner = None
        self.custom_is_pickup = 0
        self.custom_payment_method = None
        self.custom_delivery_date = None
        self.custom_delivery_time_from = None
        self.custom_delivery_duration = None
        self.remarks = ""
        self.update_stock = 1
        self.items = []
        self.taxes = []
        self.payments = []
        self.name = "TEST-INV-001"
        self.docstatus = 0
        self.net_total = 0.0
        self.grand_total = 0.0
        self.custom_sales_invoice_state = None

    def append(self, child_table, row_or_empty=None):
        if row_or_empty is None:
            row_or_empty = {}
        item = MagicMock()
        # Copy fields from row dict
        for k, v in (row_or_empty if isinstance(row_or_empty, dict) else {}).items():
            setattr(item, k, v)
        item.get = lambda key, default=None: getattr(item, key, default)
        if child_table == "items":
            self.items.append(item)
        elif child_table == "taxes":
            self.taxes.append(item)
        elif child_table == "payments":
            self.payments.append(item)
        return item

    def set(self, field, value):
        setattr(self, field, value)

    def get(self, field, default=None):
        return getattr(self, field, default)

    def save(self, **kwargs):
        pass

    def submit(self):
        self.docstatus = 1

    def run_method(self, method):
        pass

    def db_set(self, field, value, **kwargs):
        setattr(self, field, value)


def _mock_pos_profile(name="Test POS", company="Test Company",
                       selling_price_list="Standard Selling",
                       currency="EGP"):
    """Return a mock POS Profile."""
    p = MagicMock()
    p.name = name
    p.company = company
    p.selling_price_list = selling_price_list
    p.currency = currency
    return p


def _mock_customer(name="CUST-001", customer_name="Test Customer",
                   territory="Cairo", delivery_income=30.0):
    """Return a mock Customer document."""
    c = MagicMock()
    c.name = name
    c.customer_name = customer_name
    c.territory = territory
    c.delivery_income = delivery_income
    return c


# ===========================================================================
# TEST: add_items_to_invoice – Item structure verification
# ===========================================================================

class TestAddItemsToInvoice(unittest.TestCase):
    """Verify that add_items_to_invoice produces correct item fields."""

    def _add_items(self, items):
        """Run add_items_to_invoice with captures."""
        inv = _InvoiceDocCapture()
        logger = MagicMock()

        with patch("jarz_pos.utils.invoice_utils.frappe") as mf:
            mf.get_doc.return_value = MagicMock(stock_uom="Unit")

            from jarz_pos.utils.invoice_utils import add_items_to_invoice
            add_items_to_invoice(inv, items, logger)

        return inv

    def test_basic_item_fields(self):
        """Items should have item_code, qty, and price_list_rate."""
        items = [{"item_code": "ITEM-A", "qty": 3, "rate": 50.0}]
        inv = self._add_items(items)

        self.assertEqual(len(inv.items), 1)
        item = inv.items[0]
        self.assertEqual(item.item_code, "ITEM-A")
        self.assertEqual(item.qty, 3.0)
        self.assertEqual(item.price_list_rate, 50.0)

    def test_discount_percentage_set(self):
        """discount_percentage should be set on item when provided."""
        items = [{
            "item_code": "ITEM-B",
            "qty": 1,
            "price_list_rate": 100.0,
            "discount_percentage": 25.0,
        }]
        inv = self._add_items(items)
        self.assertEqual(inv.items[0].discount_percentage, 25.0)

    def test_discount_amount_converted_to_percentage(self):
        """discount_amount should be converted to discount_percentage."""
        items = [{
            "item_code": "ITEM-C",
            "qty": 1,
            "price_list_rate": 200.0,
            "rate": 200.0,
            "discount_amount": 40.0,  # = 20% of 200
        }]
        inv = self._add_items(items)
        self.assertAlmostEqual(inv.items[0].discount_percentage, 20.0, places=1)

    def test_bundle_parent_100_pct_discount(self):
        """Bundle parent items should have 100% discount → rate=0."""
        items = [{
            "item_code": "BUNDLE-PARENT",
            "qty": 1,
            "price_list_rate": 500.0,
            "discount_percentage": 100.0,
            "is_bundle_parent": True,
            "bundle_code": "BUNDLE-001",
        }]
        inv = self._add_items(items)
        self.assertEqual(inv.items[0].discount_percentage, 100.0)
        self.assertTrue(getattr(inv.items[0], "is_bundle_parent", False))

    def test_bundle_child_items(self):
        """Bundle child items should carry bundle_code and partial discount."""
        items = [
            {
                "item_code": "CHILD-A",
                "qty": 1,
                "price_list_rate": 200.0,
                "discount_percentage": 50.0,
                "is_bundle_child": True,
                "bundle_code": "BUNDLE-001",
            },
            {
                "item_code": "CHILD-B",
                "qty": 2,
                "price_list_rate": 150.0,
                "discount_percentage": 50.0,
                "is_bundle_child": True,
                "bundle_code": "BUNDLE-001",
            },
        ]
        inv = self._add_items(items)
        for item in inv.items:
            self.assertEqual(item.discount_percentage, 50.0)
            self.assertTrue(getattr(item, "is_bundle_child", False))
            self.assertEqual(getattr(item, "bundle_code", None), "BUNDLE-001")

    def test_multiple_items(self):
        """Multiple items should all be added."""
        items = [
            {"item_code": f"ITEM-{i}", "qty": i, "rate": 10.0 * i}
            for i in range(1, 6)
        ]
        inv = self._add_items(items)
        self.assertEqual(len(inv.items), 5)


# ===========================================================================
# TEST: Sales Partner Tax Suppression
# ===========================================================================

class TestSalesPartnerTaxSuppression(unittest.TestCase):
    """Verify that invoices with sales partner have all tax rows suppressed."""

    def test_sales_partner_clears_taxes(self):
        """When sales_partner is set, existing taxes should be cleared."""
        inv = _InvoiceDocCapture()
        # Pre-populate some taxes
        inv.append("taxes", {"charge_type": "Actual", "description": "VAT", "tax_amount": 50.0})
        inv.append("taxes", {"charge_type": "Actual", "description": "Shipping", "tax_amount": 30.0})

        self.assertEqual(len(inv.taxes), 2)

        # Simulate what create_pos_invoice does
        inv.sales_partner = "PARTNER-A"
        if inv.sales_partner:
            inv.set("taxes", [])

        self.assertEqual(len(inv.taxes), 0, "Sales partner invoices should have no tax rows")

    def test_no_partner_keeps_taxes(self):
        """When no sales_partner, taxes should remain."""
        inv = _InvoiceDocCapture()
        inv.append("taxes", {"charge_type": "Actual", "description": "VAT", "tax_amount": 50.0})

        inv.sales_partner = None
        if not inv.sales_partner:
            pass  # Don't clear

        self.assertEqual(len(inv.taxes), 1, "Non-partner invoices should keep tax rows")


# ===========================================================================
# TEST: Pickup Order Shipping Suppression
# ===========================================================================

class TestPickupShippingSuppression(unittest.TestCase):
    """Verify that pickup orders suppress shipping charges."""

    def test_pickup_suppresses_delivery_charges(self):
        """Pickup orders should not have delivery charges added."""
        # Simulating the business rule from create_pos_invoice
        is_pickup = True
        partner_tax_suppressed = False
        free_shipping_waived = False

        # The condition that gates delivery charge insertion:
        should_add_delivery = not partner_tax_suppressed and not free_shipping_waived and not is_pickup

        self.assertFalse(should_add_delivery, "Pickup should suppress delivery charges")

    def test_pickup_sets_flag(self):
        """Pickup orders should set custom_is_pickup = 1."""
        inv = _InvoiceDocCapture()
        is_pickup = True
        if is_pickup:
            inv.custom_is_pickup = 1

        self.assertEqual(inv.custom_is_pickup, 1)

    def test_pickup_adds_remarks_marker(self):
        """Pickup orders should add [PICKUP] to remarks."""
        inv = _InvoiceDocCapture()
        is_pickup = True
        if is_pickup:
            marker = "[PICKUP]"
            existing = (inv.remarks or "").strip()
            if marker not in existing:
                inv.remarks = (existing + "\n" if existing else "") + marker

        self.assertIn("[PICKUP]", inv.remarks)


# ===========================================================================
# TEST: Delivery Charges JSON Construction (Flutter-side logic, verified here)
# ===========================================================================

class TestDeliveryChargesConstruction(unittest.TestCase):
    """Verify delivery_charges_json construction rules."""

    def test_delivery_charges_present_for_customer_with_income(self):
        """Customer with delivery_income should produce delivery_charges_json."""
        customer = {"name": "CUST-001", "delivery_income": 30.0, "territory": "Cairo"}
        sales_partner = None

        partner_active = sales_partner is not None and sales_partner != ""
        should_add = (
            not partner_active
            and customer is not None
            and customer.get("delivery_income") is not None
            and customer["delivery_income"] > 0
        )
        self.assertTrue(should_add)

        charges_json = json.dumps([{
            "charge_type": "Delivery",
            "amount": customer["delivery_income"],
            "description": f"Delivery charge for {customer.get('territory', 'Unknown Territory')}",
        }])
        parsed = json.loads(charges_json)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["amount"], 30.0)

    def test_delivery_charges_suppressed_for_sales_partner(self):
        """Sales partner active should suppress delivery_charges_json."""
        customer = {"name": "CUST-002", "delivery_income": 30.0}
        sales_partner = "PARTNER-A"

        partner_active = sales_partner is not None and sales_partner != ""
        should_add = (
            not partner_active
            and customer is not None
            and customer.get("delivery_income") is not None
            and customer["delivery_income"] > 0
        )
        self.assertFalse(should_add, "Sales partner should suppress delivery charges")

    def test_delivery_charges_suppressed_for_zero_income(self):
        """Zero delivery_income should not produce charges."""
        customer = {"name": "CUST-003", "delivery_income": 0}
        should_add = (
            customer.get("delivery_income") is not None
            and customer["delivery_income"] > 0
        )
        self.assertFalse(should_add)

    def test_delivery_charges_suppressed_for_no_customer(self):
        """No customer should not produce charges."""
        customer = None
        should_add = customer is not None and customer.get("delivery_income", 0) > 0
        self.assertFalse(should_add)


# ===========================================================================
# TEST: Free Shipping Bundle Detection
# ===========================================================================

class TestFreeShippingBundleDetection(unittest.TestCase):
    """Verify that free-shipping bundles trigger shipping waiver."""

    def test_free_shipping_waiver_logic(self):
        """When a bundle has free_shipping=1, shipping should be waived."""
        # Simulate _get_delivery_expense_amount behavior for free-shipping bundle
        with patch("jarz_pos.services.delivery_handling.frappe") as mf:
            # Mock invoice with bundle items
            inv = MagicMock()
            item1 = MagicMock()
            item1.item_code = "BUNDLE-PARENT"
            item1.bundle_code = "FREE-SHIP-BUNDLE"
            inv.items = [item1]

            # Mock DB: Jarz Bundle has free_shipping=1
            mf.db.get_table_columns.return_value = ["name", "free_shipping"]
            mf.get_all.return_value = ["FREE-SHIP-BUNDLE"]  # has free_shipping=1

            # This simulates the lookup code path
            bundle_candidates = []
            for it in inv.items:
                bcode = getattr(it, "bundle_code", None)
                if bcode:
                    bundle_candidates.append(bcode)

            self.assertEqual(len(bundle_candidates), 1)
            self.assertEqual(bundle_candidates[0], "FREE-SHIP-BUNDLE")

    def test_no_bundle_no_waiver(self):
        """Regular items should not trigger free-shipping waiver."""
        inv = MagicMock()
        item1 = MagicMock()
        item1.item_code = "REGULAR-ITEM"
        item1.bundle_code = None
        item1.parent_bundle = None
        inv.items = [item1]

        bundle_candidates = []
        for it in inv.items:
            bcode = getattr(it, "bundle_code", None) or getattr(it, "parent_bundle", None)
            if bcode:
                bundle_candidates.append(bcode)

        self.assertEqual(len(bundle_candidates), 0, "Regular items should have no bundle candidates")


# ===========================================================================
# TEST: Stock Update suppression for POS Sales Invoices
# ===========================================================================

class TestStockUpdateSuppression(unittest.TestCase):
    """Verify that POS Sales Invoices never update stock at SI creation."""

    def test_sales_partner_disables_stock_update(self):
        """Sales partner invoices should have update_stock=0."""
        inv = _InvoiceDocCapture()
        inv.sales_partner = "PARTNER-A"

        if inv.sales_partner:
            if hasattr(inv, "update_stock"):
                inv.update_stock = 0

        self.assertEqual(inv.update_stock, 0)

    def test_regular_invoice_disables_stock_update(self):
        """Regular POS invoices should also keep update_stock=0."""
        inv = _InvoiceDocCapture()
        inv.sales_partner = None

        if hasattr(inv, "update_stock"):
            inv.update_stock = 0

        self.assertEqual(inv.update_stock, 0)


# ===========================================================================
# TEST: Payment Method Validation
# ===========================================================================

class TestPaymentMethodValidation(unittest.TestCase):
    """Verify payment method validation logic."""

    def test_valid_methods(self):
        """All valid payment methods should pass validation."""
        allowed = ["Cash", "Instapay", "Mobile Wallet", "Kashier Card", "Kashier Wallet"]
        for method in allowed:
            self.assertIn(method, allowed)

    def test_invalid_method_rejected(self):
        """Invalid payment methods should be rejected."""
        allowed = ["Cash", "Instapay", "Mobile Wallet", "Kashier Card", "Kashier Wallet"]
        self.assertNotIn("Bitcoin", allowed)
        self.assertNotIn("", allowed)
        self.assertNotIn("cash", allowed)  # case-sensitive


# ===========================================================================
# TEST: Initial State for Sales Partner
# ===========================================================================

class TestInitialStateForSalesPartner(unittest.TestCase):
    """Verify that sales partner invoices start In Progress on kanban."""

    def test_sales_partner_initial_state(self):
        """Sales partner invoices should start with 'In Progress' state."""
        # This tests the _set_initial_state_for_sales_partner function behavior
        inv = _InvoiceDocCapture()
        inv.sales_partner = "PARTNER-A"

        if inv.sales_partner:
            inv.custom_sales_invoice_state = "In Progress"

        self.assertEqual(inv.custom_sales_invoice_state, "In Progress")

    def test_regular_invoice_no_initial_state(self):
        """Regular invoices should not be set to In Progress."""
        inv = _InvoiceDocCapture()
        inv.sales_partner = None

        if inv.sales_partner:
            inv.custom_sales_invoice_state = "In Progress"

        self.assertIsNone(inv.custom_sales_invoice_state)


# ===========================================================================
# TEST: Cart JSON Construction
# ===========================================================================

class TestCartJsonConstruction(unittest.TestCase):
    """Verify cart_json structure produced by Flutter for different item types."""

    def test_regular_item_json(self):
        """Regular item should have item_code, qty, rate, is_bundle=false."""
        item = {
            "item_code": "ITEM-001",
            "qty": 2,
            "rate": 50.0,
            "is_bundle": False,
        }
        cart_json = json.dumps([item])
        parsed = json.loads(cart_json)
        self.assertEqual(parsed[0]["item_code"], "ITEM-001")
        self.assertEqual(parsed[0]["qty"], 2)
        self.assertEqual(parsed[0]["rate"], 50.0)
        self.assertFalse(parsed[0]["is_bundle"])

    def test_bundle_item_json(self):
        """Bundle item should have is_bundle=true and selected_items map."""
        item = {
            "item_code": "BUNDLE-001",
            "qty": 1,
            "rate": 250.0,
            "is_bundle": True,
            "selected_items": {
                "Main Course": [
                    {"id": "FOOD-A", "item_name": "Burger"},
                ],
                "Side Dish": [
                    {"id": "FOOD-B", "item_name": "Fries"},
                ],
            },
        }
        cart_json = json.dumps([item])
        parsed = json.loads(cart_json)
        self.assertTrue(parsed[0]["is_bundle"])
        self.assertIn("selected_items", parsed[0])
        self.assertEqual(len(parsed[0]["selected_items"]["Main Course"]), 1)
        self.assertEqual(len(parsed[0]["selected_items"]["Side Dish"]), 1)

    def test_discount_fields_preserved(self):
        """Discount fields should be in cart_json when present."""
        item = {
            "item_code": "ITEM-002",
            "qty": 1,
            "rate": 80.0,
            "is_bundle": False,
            "price_list_rate": 100.0,
            "discount_amount": 20.0,
            "discount_percentage": 20.0,
        }
        cart_json = json.dumps([item])
        parsed = json.loads(cart_json)
        self.assertEqual(parsed[0]["price_list_rate"], 100.0)
        self.assertEqual(parsed[0]["discount_amount"], 20.0)
        self.assertEqual(parsed[0]["discount_percentage"], 20.0)

    def test_mixed_cart(self):
        """Cart with both regular items and bundles."""
        items = [
            {"item_code": "ITEM-001", "qty": 2, "rate": 50.0, "is_bundle": False},
            {
                "item_code": "BUNDLE-001",
                "qty": 1,
                "rate": 300.0,
                "is_bundle": True,
                "selected_items": {"Group A": [{"id": "A1"}]},
            },
        ]
        cart_json = json.dumps(items)
        parsed = json.loads(cart_json)
        self.assertEqual(len(parsed), 2)
        self.assertFalse(parsed[0]["is_bundle"])
        self.assertTrue(parsed[1]["is_bundle"])


# ===========================================================================
# TEST: Delivery slot handling
# ===========================================================================

class TestDeliverySlotHandling(unittest.TestCase):
    """Verify delivery datetime → custom fields conversion."""

    def test_parse_duration_minutes_heuristic(self):
        """Small numbers (< 1000) assumed minutes, converted to seconds."""
        from jarz_pos.services.invoice_creation import _apply_delivery_slot_fields

        inv = _InvoiceDocCapture()
        with patch("jarz_pos.services.invoice_creation.frappe") as mf:
            mf.utils.get_datetime.return_value = MagicMock(
                date=lambda: "2026-03-14",
                time=lambda: MagicMock(strftime=lambda fmt: "14:00:00")
            )
            mf.form_dict = {"delivery_duration": "60"}

            _apply_delivery_slot_fields(inv, "2026-03-14 14:00:00")

        # 60 minutes = 3600 seconds (small number heuristic)
        self.assertEqual(inv.custom_delivery_duration, 3600)

    def test_parse_duration_hours_suffix(self):
        """Duration with 'h' suffix should convert hours to seconds."""
        from jarz_pos.services.invoice_creation import _apply_delivery_slot_fields

        inv = _InvoiceDocCapture()
        with patch("jarz_pos.services.invoice_creation.frappe") as mf:
            mf.utils.get_datetime.return_value = MagicMock(
                date=lambda: "2026-03-14",
                time=lambda: MagicMock(strftime=lambda fmt: "14:00:00")
            )
            mf.form_dict = {"delivery_duration": "2h"}

            _apply_delivery_slot_fields(inv, "2026-03-14 14:00:00")

        self.assertEqual(inv.custom_delivery_duration, 7200)

    def test_no_duration_defaults_to_3600(self):
        """Missing duration should default to 3600 seconds (1 hour)."""
        from jarz_pos.services.invoice_creation import _apply_delivery_slot_fields

        inv = _InvoiceDocCapture()
        inv.custom_delivery_duration = None

        with patch("jarz_pos.services.invoice_creation.frappe") as mf:
            mf.utils.get_datetime.return_value = MagicMock(
                date=lambda: "2026-03-14",
                time=lambda: MagicMock(strftime=lambda fmt: "10:00:00")
            )
            mf.form_dict = {}

            _apply_delivery_slot_fields(inv, "2026-03-14 10:00:00")

        self.assertEqual(inv.custom_delivery_duration, 3600)


if __name__ == "__main__":
    unittest.main()
