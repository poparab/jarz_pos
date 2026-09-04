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

    def test_get_customer_shipping_addresses_keeps_legacy_rows_visible(self):
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

        self.assertEqual([row["name"] for row in result], ["ADDR-SHIP-1", "ADDR-SHIP-2", "ADDR-BILL"])
        self.assertEqual(result[0]["full_address"], "Shipping 1, Apt 5, Cairo")
        self.assertEqual(result[2]["full_address"], "Billing 1, Cairo")

    def test_get_customer_shipping_addresses_dedupes_equivalent_rows(self):
        utils = self._load_utils_module()

        dynamic_link_rows = [
            {"parent": "ADDR-DUP-BILL"},
            {"parent": "ADDR-DUP-SHIP"},
            {"parent": "ADDR-UNIQUE"},
        ]
        address_rows = [
            {
                "name": "ADDR-DUP-BILL",
                "address_type": "Billing",
                "address_line1": "12 Road",
                "address_line2": "",
                "city": "Giza",
                "is_primary_address": 1,
                "is_shipping_address": 0,
                "modified": "2026-05-05 12:00:00",
                "mobile_no": "",
            },
            {
                "name": "ADDR-DUP-SHIP",
                "address_type": "Shipping",
                "address_line1": "12 Road",
                "address_line2": "",
                "city": "Giza",
                "is_primary_address": 0,
                "is_shipping_address": 1,
                "modified": "2026-05-05 11:00:00",
                "mobile_no": "0100",
            },
            {
                "name": "ADDR-UNIQUE",
                "address_type": "Shipping",
                "address_line1": "8 Nile St",
                "address_line2": "",
                "city": "Cairo",
                "is_primary_address": 0,
                "is_shipping_address": 1,
                "modified": "2026-05-05 10:00:00",
                "mobile_no": "0102",
            },
        ]

        with patch.object(utils.frappe, "get_all", side_effect=[dynamic_link_rows, address_rows]), \
             patch.object(utils.frappe.db, "has_column", return_value=True):
            result = utils.get_customer_shipping_addresses("CUST-2")

        self.assertEqual([row["name"] for row in result], ["ADDR-DUP-SHIP", "ADDR-UNIQUE"])
        self.assertTrue(result[0]["is_primary_address"])
        self.assertTrue(result[0]["is_shipping_address"])
        self.assertEqual(result[0]["phone"], "0100")

    def test_resolve_customer_shipping_address_ignores_billing_preference_when_shipping_exists(self):
        utils = self._load_utils_module()

        candidates = [
            {"name": "ADDR-SHIP-1", "address_line1": "Shipping 1", "address_line2": "", "city": "Cairo", "is_shipping_address": True, "is_primary_address": True},
            {"name": "ADDR-SHIP-2", "address_line1": "Shipping 2", "address_line2": "", "city": "Giza", "is_shipping_address": True, "is_primary_address": False},
        ]
        raw_rows = [
            {"name": "ADDR-BILL", "address_line1": "Billing 1", "address_line2": "", "city": "Cairo", "is_shipping_address": False, "is_primary_address": False},
            {"name": "ADDR-SHIP-1", "address_line1": "Shipping 1", "address_line2": "", "city": "Cairo", "is_shipping_address": True, "is_primary_address": True},
            {"name": "ADDR-SHIP-2", "address_line1": "Shipping 2", "address_line2": "", "city": "Giza", "is_shipping_address": True, "is_primary_address": False},
        ]

        with patch.object(utils, "get_customer_shipping_addresses", return_value=candidates), \
             patch.object(utils, "get_linked_customer_addresses", return_value=raw_rows), \
             patch.object(utils.frappe.db, "get_value", return_value="ADDR-SHIP-1"):
            result = utils.resolve_customer_shipping_address(
                "CUST-1",
                preferred_address_name="ADDR-BILL",
            )

        self.assertEqual(result["name"], "ADDR-SHIP-1")

    def test_resolve_customer_shipping_address_maps_duplicate_preference_to_canonical_candidate(self):
        utils = self._load_utils_module()

        candidates = [
            {"name": "ADDR-SHIP-1", "address_line1": "12 Road", "address_line2": "", "city": "Giza", "is_shipping_address": True, "is_primary_address": True},
        ]
        raw_rows = [
            {"name": "ADDR-LEGACY-1", "address_line1": "12 Road", "address_line2": "", "city": "Giza", "is_shipping_address": False, "is_primary_address": False},
            {"name": "ADDR-SHIP-1", "address_line1": "12 Road", "address_line2": "", "city": "Giza", "is_shipping_address": True, "is_primary_address": True},
        ]

        with patch.object(utils, "get_customer_shipping_addresses", return_value=candidates), \
             patch.object(utils, "get_linked_customer_addresses", return_value=raw_rows), \
             patch.object(utils.frappe.db, "get_value", return_value=None):
            result = utils.resolve_customer_shipping_address(
                "CUST-1",
                preferred_address_name="ADDR-LEGACY-1",
            )

        self.assertEqual(result["name"], "ADDR-SHIP-1")

    # ------------------------------------------------------------------
    # preferred_address_was_honoured
    #
    # The resolver above is DESIGNED to answer with a different name than the one
    # it was asked for (the deduped survivor of an equivalent address). The POS
    # invoice guard used to compare names and refuse the order when they differed,
    # which rejected the resolver's own correct answer: 5.8% of 90 days of
    # submitted invoices, every one of them permanently unamendable. These cases
    # pin the replacement rule -- equivalence, not identity -- and, more
    # importantly, pin that it still says NO to an address that is not this
    # customer's.
    # ------------------------------------------------------------------

    def _raw_rows(self):
        return [
            {
                "name": "ADDR-LEGACY",
                "address_type": "Billing",
                "address_line1": "12 Road",
                "address_line2": "",
                "city": "Giza",
            },
            {
                "name": "ADDR-SURVIVOR",
                "address_type": "Shipping",
                "address_line1": "12 Road",
                "address_line2": "",
                "city": "Giza",
            },
            {
                "name": "ADDR-OTHER-STREET",
                "address_type": "Shipping",
                "address_line1": "8 Nile St",
                "address_line2": "",
                "city": "Cairo",
            },
        ]

    def _survivor(self):
        return {
            "name": "ADDR-SURVIVOR",
            "address_line1": "12 Road",
            "address_line2": "",
            "city": "Giza",
        }

    def test_preferred_address_was_honoured_accepts_the_deduped_survivor(self):
        utils = self._load_utils_module()

        with patch.object(utils, "get_linked_customer_addresses", return_value=self._raw_rows()):
            honoured = utils.preferred_address_was_honoured(
                "CUST-1", "ADDR-LEGACY", self._survivor()
            )

        self.assertTrue(honoured)

    def test_preferred_address_was_honoured_short_circuits_on_an_exact_name_match(self):
        utils = self._load_utils_module()

        # The exact-name fast path must not need a database read at all -- the POS
        # calls this on every checkout.
        blow_up = MagicMock(side_effect=AssertionError("must not query addresses"))
        with patch.object(utils, "get_linked_customer_addresses", blow_up):
            honoured = utils.preferred_address_was_honoured(
                "CUST-1", "ADDR-SURVIVOR", self._survivor()
            )

        self.assertTrue(honoured)
        blow_up.assert_not_called()

    def test_preferred_address_was_honoured_rejects_another_customers_address(self):
        """LOAD-BEARING: a foreign address name must never be accepted.

        Not even when its text is identical to the resolved row. The resolver
        answers `candidates[0]` for any preference it cannot map, so without this
        the order would be silently stamped with an address the caller never
        chose, taken from a customer whose address book we just proved does not
        contain the requested name.
        """
        utils = self._load_utils_module()

        # ADDR-FOREIGN belongs to somebody else: it is absent from THIS customer's
        # linked rows, even though its address text collides exactly.
        with patch.object(utils, "get_linked_customer_addresses", return_value=self._raw_rows()):
            honoured = utils.preferred_address_was_honoured(
                "CUST-1", "ADDR-FOREIGN", self._survivor()
            )
        self.assertFalse(honoured)

        # And the same answer when the customer has no addresses at all.
        with patch.object(utils, "get_linked_customer_addresses", return_value=[]):
            honoured = utils.preferred_address_was_honoured(
                "CUST-1", "ADDR-FOREIGN", self._survivor()
            )
        self.assertFalse(honoured)

    def test_preferred_address_was_honoured_rejects_a_nonexistent_address(self):
        utils = self._load_utils_module()

        with patch.object(utils, "get_linked_customer_addresses", return_value=self._raw_rows()):
            honoured = utils.preferred_address_was_honoured(
                "CUST-1", "ADDR-DOES-NOT-EXIST", self._survivor()
            )

        self.assertFalse(honoured)

    def test_preferred_address_was_honoured_rejects_a_different_address_of_the_same_customer(self):
        utils = self._load_utils_module()

        # Both rows are this customer's, but they are genuinely different places:
        # substituting one for the other would ship to the wrong door.
        with patch.object(utils, "get_linked_customer_addresses", return_value=self._raw_rows()):
            honoured = utils.preferred_address_was_honoured(
                "CUST-1", "ADDR-OTHER-STREET", self._survivor()
            )

        self.assertFalse(honoured)

    def test_preferred_address_was_honoured_rejects_when_the_resolver_returned_nothing(self):
        utils = self._load_utils_module()

        with patch.object(utils, "get_linked_customer_addresses", return_value=self._raw_rows()):
            honoured = utils.preferred_address_was_honoured("CUST-1", "ADDR-LEGACY", None)

        self.assertFalse(honoured)

    def test_preferred_address_was_honoured_is_vacuously_true_without_a_preference(self):
        utils = self._load_utils_module()

        blow_up = MagicMock(side_effect=AssertionError("must not query addresses"))
        with patch.object(utils, "get_linked_customer_addresses", blow_up):
            self.assertTrue(utils.preferred_address_was_honoured("CUST-1", None, None))
            self.assertTrue(utils.preferred_address_was_honoured("CUST-1", "   ", self._survivor()))

    def test_preferred_address_was_honoured_matches_the_resolvers_own_substitution(self):
        """Whatever resolve_customer_shipping_address returns must be honoured.

        This runs the real resolver over a duplicate pair and feeds its answer
        straight back in, so the guard can never disagree with the code it guards.
        """
        utils = self._load_utils_module()

        candidates = [self._survivor()]
        raw_rows = self._raw_rows()

        with patch.object(utils, "get_customer_shipping_addresses", return_value=candidates), \
             patch.object(utils, "get_linked_customer_addresses", return_value=raw_rows), \
             patch.object(utils.frappe.db, "get_value", return_value=None):
            resolved = utils.resolve_customer_shipping_address(
                "CUST-1", preferred_address_name="ADDR-LEGACY"
            )
            honoured = utils.preferred_address_was_honoured("CUST-1", "ADDR-LEGACY", resolved)

        self.assertEqual(resolved["name"], "ADDR-SURVIVOR")
        self.assertTrue(honoured)

    def test_find_matching_customer_address_reuses_existing_line1_match(self):
        utils = self._load_utils_module()

        raw_rows = [
            {
                "name": "ADDR-SHIP-1",
                "address_type": "Shipping",
                "address_line1": "12 Road",
                "address_line2": "",
                "city": "Giza",
                "is_primary_address": 1,
                "is_shipping_address": 1,
                "modified": "2026-05-05 12:00:00",
                "mobile_no": "0101",
            },
            {
                "name": "ADDR-SHIP-2",
                "address_type": "Shipping",
                "address_line1": "8 Nile St",
                "address_line2": "",
                "city": "Cairo",
                "is_primary_address": 0,
                "is_shipping_address": 1,
                "modified": "2026-05-05 11:00:00",
                "mobile_no": "0102",
            },
        ]

        with patch.object(utils, "get_linked_customer_addresses", return_value=raw_rows):
            result = utils.find_matching_customer_address("CUST-1", "12 Road")

        self.assertIsNotNone(result)
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
