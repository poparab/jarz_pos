"""Access-control gate for the Delivery Partner settlement API.

Companion to ``test_line_manager_tier.py``'s pattern, but for ``api/delivery_partners.py``
specifically: a review found none of its whitelisted endpoints carried any permission
guard beyond ``@frappe.whitelist()``, so a plain POS cashier could call
``settle_delivery_partner`` and post the weekly bank transfer, complete with
caller-supplied ``extra_charges`` expensed at payment time.

The fix mirrors ``ROLES.MANAGER`` — the same tier ``api/cash_transfer.py`` uses —
rather than the wider ``LINE_MANAGER_TIER``: a Delivery Partner is a courier COMPANY
whose payable is company-level, with no branch dimension to hand down to a floor
supervisor the way ``STOCK_TRANSFER`` does for stock moves.

These tests assert three things, for every one of the three whitelisted endpoints:

  1. A plain POS user is refused.
  2. A JARZ Manager is allowed through the gate.
  3. The gate is genuinely the FIRST statement — an unprivileged caller never reaches
     a single database call, let alone a posting. This is asserted by making every
     ``frappe`` DB accessor blow up if touched, and checking the exception that
     actually propagates is the permission error, not that self-destruct.
"""

import unittest
from unittest.mock import MagicMock, patch

from jarz_pos.constants import ROLES

MANAGER_ROLE = ROLES.JARZ_MANAGER
POS_USER_ROLES = ["POS User", "Sales User"]


def _boom(*_args, **_kwargs):
    raise AssertionError("database was touched before the permission gate ran")


class TestGuardHelperDirectly(unittest.TestCase):
    """``_ensure_delivery_partner_access`` in isolation."""

    def _run(self, roles):
        from jarz_pos.api import delivery_partners

        with patch.object(delivery_partners, "frappe") as mock_frappe:
            mock_frappe.PermissionError = PermissionError
            mock_frappe.throw.side_effect = PermissionError
            mock_frappe.get_roles.return_value = roles
            delivery_partners._ensure_delivery_partner_access()

    def test_manager_passes(self):
        self._run([MANAGER_ROLE])

    def test_every_manager_tier_member_passes(self):
        for role in sorted(ROLES.MANAGER):
            with self.subTest(role=role):
                self._run([role])

    def test_plain_pos_user_is_refused(self):
        with self.assertRaises(PermissionError):
            self._run(POS_USER_ROLES)

    def test_line_manager_alone_is_refused(self):
        """Deliberately NOT widened to the line-manager tier: no branch owns a partner."""
        with self.assertRaises(PermissionError):
            self._run([ROLES.JARZ_LINE_MANAGER, ROLES.JARZ_LINE_MANAGER_ALT])


class _EndpointGuardOrderingMixin:
    """Shared machinery: run an endpoint under a fully mocked ``frappe``."""

    def _mocked_module(self, roles, *, block_db=False):
        from jarz_pos.api import delivery_partners

        patcher = patch.object(delivery_partners, "frappe")
        mock_frappe = patcher.start()
        self.addCleanup(patcher.stop)
        mock_frappe.PermissionError = PermissionError
        mock_frappe.throw.side_effect = PermissionError
        mock_frappe.get_roles.return_value = roles
        if block_db:
            mock_frappe.db.sql.side_effect = _boom
            mock_frappe.get_all.side_effect = _boom
            mock_frappe.get_doc.side_effect = _boom
            mock_frappe.db.get_value.side_effect = _boom
            mock_frappe.db.set_value.side_effect = _boom
            mock_frappe.db.commit.side_effect = _boom
        return delivery_partners, mock_frappe


class TestGetDeliveryPartnerBalancesGate(_EndpointGuardOrderingMixin, unittest.TestCase):
    def test_pos_user_refused_before_any_db_call(self):
        module, _mock_frappe = self._mocked_module(POS_USER_ROLES, block_db=True)
        with self.assertRaises(PermissionError):
            module.get_delivery_partner_balances()

    def test_manager_reaches_the_query(self):
        module, mock_frappe = self._mocked_module([MANAGER_ROLE])
        mock_frappe.db.sql.return_value = []
        result = module.get_delivery_partner_balances()
        self.assertEqual(result, [])
        mock_frappe.db.sql.assert_called_once()


class TestGetDeliveryPartnerUnsettledDetailsGate(_EndpointGuardOrderingMixin, unittest.TestCase):
    def test_pos_user_refused_before_any_db_call(self):
        module, _mock_frappe = self._mocked_module(POS_USER_ROLES, block_db=True)
        with self.assertRaises(PermissionError):
            module.get_delivery_partner_unsettled_details("Partner A")

    def test_manager_reaches_the_query(self):
        module, mock_frappe = self._mocked_module([MANAGER_ROLE])
        mock_frappe.get_all.return_value = []
        result = module.get_delivery_partner_unsettled_details("Partner A")
        self.assertEqual(result, [])
        mock_frappe.get_all.assert_called_once()


class TestSettleDeliveryPartnerGate(_EndpointGuardOrderingMixin, unittest.TestCase):
    def test_pos_user_refused_before_touching_the_partner_doc(self):
        """The gate fires even before the ``delivery_partner is required`` check."""
        module, mock_frappe = self._mocked_module(POS_USER_ROLES, block_db=True)
        with self.assertRaises(PermissionError):
            module.settle_delivery_partner("Partner A")
        # get_doc is the very next line after the required-field check; proving it
        # was never reached proves the guard ran first.
        mock_frappe.get_doc.assert_not_called()

    def test_pos_user_refused_even_with_extra_charges_supplied(self):
        """The exact abuse the review flagged: caller-supplied extra_charges."""
        module, mock_frappe = self._mocked_module(POS_USER_ROLES, block_db=True)
        with self.assertRaises(PermissionError):
            module.settle_delivery_partner(
                "Partner A",
                extra_charges=[{"description": "Monthly subscription", "amount": 500.0}],
            )
        mock_frappe.get_doc.assert_not_called()

    def test_manager_passes_the_gate_and_reaches_business_logic(self):
        module, mock_frappe = self._mocked_module([MANAGER_ROLE])
        dp = MagicMock()
        dp.settlement_account = "Partner A - J"
        mock_frappe.get_doc.return_value = dp
        mock_frappe.get_all.return_value = []  # nothing unbilled
        result = module.settle_delivery_partner("Partner A")
        self.assertTrue(result["success"])
        self.assertEqual(result["order_count"], 0)
        mock_frappe.get_doc.assert_called_once()


if __name__ == "__main__":
    unittest.main()
