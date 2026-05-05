import unittest
import importlib
import sys
import types
from unittest.mock import MagicMock, patch


class TestCustomerAddressUtils(unittest.TestCase):
    def _load_utils_module(self):
        fake_frappe = types.ModuleType("frappe")
        fake_frappe.db = types.SimpleNamespace(
            has_column=MagicMock(return_value=True),
            exists=MagicMock(return_value=True),
            get_value=MagicMock(return_value=None),
            set_value=MagicMock(),
        )
        fake_frappe.get_all = MagicMock(return_value=[])
        fake_frappe.get_doc = MagicMock()

        with patch.dict(sys.modules, {"frappe": fake_frappe}):
            sys.modules.pop("jarz_pos.utils.customer_address_utils", None)
            module = importlib.import_module("jarz_pos.utils.customer_address_utils")
            return importlib.reload(module)

    def test_get_customer_shipping_addresses_prefers_shipping_rows(self):
        utils = self._load_utils_module()

        dynamic_link_rows = [
            {"parent": "ADDR-BILL"},
            {"parent": "ADDR-SHIP-1"},
            {"parent": "ADDR-SHIP-2"},
        ]
        address_rows = [
            {
                "name": "ADDR-BILL",
                "address_type": "Billing",
                "address_line1": "Billing 1",
                "address_line2": "",
                "city": "Cairo",
                "is_primary_address": 0,
                "is_shipping_address": 0,
                "modified": "2026-05-05 10:00:00",
                "mobile_no": "0100",
            },
            {
                "name": "ADDR-SHIP-2",
                "address_type": "Shipping",
                "address_line1": "Shipping 2",
                "address_line2": "",
                "city": "Giza",
                "is_primary_address": 0,
                "is_shipping_address": 1,
                "modified": "2026-05-05 12:00:00",
                "mobile_no": "0102",
            },
            {
                "name": "ADDR-SHIP-1",
                "address_type": "Shipping",
                "address_line1": "Shipping 1",
                "address_line2": "Apt 5",
                "city": "Cairo",
                "is_primary_address": 1,
                "is_shipping_address": 1,
                "modified": "2026-05-05 11:00:00",
                "mobile_no": "0101",
            },
        ]

        with patch.object(utils.frappe, "get_all", side_effect=[dynamic_link_rows, address_rows]), \
             patch.object(utils.frappe.db, "has_column", return_value=True):
            result = utils.get_customer_shipping_addresses("CUST-1")

        self.assertEqual([row["name"] for row in result], ["ADDR-SHIP-2", "ADDR-SHIP-1"])
        self.assertTrue(all(row["is_shipping_address"] for row in result))
        self.assertEqual(result[0]["full_address"], "Shipping 2, Giza")

    def test_resolve_customer_shipping_address_ignores_billing_preference_when_shipping_exists(self):
        utils = self._load_utils_module()

        candidates = [
            {"name": "ADDR-SHIP-1", "is_shipping_address": True, "is_primary_address": True},
            {"name": "ADDR-SHIP-2", "is_shipping_address": True, "is_primary_address": False},
        ]

        with patch.object(utils, "get_customer_shipping_addresses", return_value=candidates), \
             patch.object(utils.frappe.db, "get_value", return_value="ADDR-SHIP-1"):
            result = utils.resolve_customer_shipping_address(
                "CUST-1",
                preferred_address_name="ADDR-BILL",
            )

        self.assertEqual(result["name"], "ADDR-SHIP-1")

    def test_ensure_shipping_address_updates_type_and_flag(self):
        utils = self._load_utils_module()

        address_doc = MagicMock()
        address_doc.address_type = "Billing"
        address_doc.is_shipping_address = 0

        with patch.object(utils.frappe.db, "exists", return_value=True), \
             patch.object(utils.frappe, "get_doc", return_value=address_doc):
            result = utils.ensure_shipping_address("ADDR-1")

        self.assertIs(result, address_doc)
        self.assertEqual(address_doc.address_type, "Shipping")
        self.assertEqual(address_doc.is_shipping_address, 1)
        address_doc.save.assert_called_once_with(ignore_permissions=True)
