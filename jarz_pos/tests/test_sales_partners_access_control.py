"""Access-control gate for the Sales Partner settlement API.

Companion to ``test_delivery_partners_access_control.py``: a review found none of the
whitelisted endpoints in ``api/sales_partners.py`` carried any permission guard beyond
``@frappe.whitelist()``, so a plain POS cashier could call ``settle_sales_partner`` and
post the batch commission + VAT recognition Journal Entry.

The fix mirrors ``ROLES.MANAGER`` — the same tier ``api/cash_transfer.py`` uses — rather
than the wider ``LINE_MANAGER_TIER``: a Sales Partner (Talabat and the like) is a
company-wide relationship and the settlement JE is company money moving, with no
branch dimension to hand down to a floor supervisor.

These tests assert three things, for both whitelisted endpoints:

  1. A plain POS user is refused.
  2. A JARZ Manager is allowed through the gate.
  3. The gate is genuinely the FIRST statement — an unprivileged caller never reaches
     a single database call, let alone a posting.
"""

import unittest
from unittest.mock import patch

from jarz_pos.constants import ROLES

MANAGER_ROLE = ROLES.JARZ_MANAGER
POS_USER_ROLES = ["POS User", "Sales User"]


def _boom(*_args, **_kwargs):
    raise AssertionError("database was touched before the permission gate ran")


class TestGuardHelperDirectly(unittest.TestCase):
    """``_ensure_sales_partner_settlement_access`` in isolation."""

    def _run(self, roles):
        from jarz_pos.api import sales_partners

        with patch.object(sales_partners, "frappe") as mock_frappe:
            mock_frappe.PermissionError = PermissionError
            mock_frappe.throw.side_effect = PermissionError
            mock_frappe.get_roles.return_value = roles
            sales_partners._ensure_sales_partner_settlement_access()

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
        """Deliberately NOT widened to the line-manager tier: no branch owns a
        company-wide Sales Partner relationship."""
        with self.assertRaises(PermissionError):
            self._run([ROLES.JARZ_LINE_MANAGER, ROLES.JARZ_LINE_MANAGER_ALT])


class _EndpointGuardOrderingMixin:
    """Shared machinery: run an endpoint under a fully mocked ``frappe``."""

    def _mocked_module(self, roles, *, block_db=False):
        from jarz_pos.api import sales_partners

        patcher = patch.object(sales_partners, "frappe")
        mock_frappe = patcher.start()
        self.addCleanup(patcher.stop)
        mock_frappe.PermissionError = PermissionError
        mock_frappe.throw.side_effect = PermissionError
        mock_frappe.get_roles.return_value = roles
        if block_db:
            mock_frappe.db.sql.side_effect = _boom
            mock_frappe.get_all.side_effect = _boom
            mock_frappe.get_doc.side_effect = _boom
            mock_frappe.db.exists.side_effect = _boom
            mock_frappe.db.get_value.side_effect = _boom
            mock_frappe.db.set_value.side_effect = _boom
            mock_frappe.db.commit.side_effect = _boom
            mock_frappe.new_doc.side_effect = _boom
        return sales_partners, mock_frappe


class TestGetSalesPartnerBalancesGate(_EndpointGuardOrderingMixin, unittest.TestCase):
    def test_pos_user_refused_before_any_db_call(self):
        module, _mock_frappe = self._mocked_module(POS_USER_ROLES, block_db=True)
        with self.assertRaises(PermissionError):
            module.get_sales_partner_balances()

    def test_manager_reaches_the_query(self):
        module, mock_frappe = self._mocked_module([MANAGER_ROLE])
        mock_frappe.get_all.return_value = []
        result = module.get_sales_partner_balances()
        self.assertEqual(result, [])
        mock_frappe.get_all.assert_called_once()


class TestSettleSalesPartnerGate(_EndpointGuardOrderingMixin, unittest.TestCase):
    def test_pos_user_refused_before_the_partner_existence_check(self):
        """The gate fires even before ``frappe.db.exists`` is asked about the partner."""
        module, mock_frappe = self._mocked_module(POS_USER_ROLES, block_db=True)
        with self.assertRaises(PermissionError):
            module.settle_sales_partner("Talabat")
        mock_frappe.db.exists.assert_not_called()

    def test_manager_passes_the_gate_and_reaches_business_logic(self):
        module, mock_frappe = self._mocked_module([MANAGER_ROLE])
        mock_frappe.db.exists.return_value = True
        mock_frappe.get_all.return_value = []  # no unsettled transactions
        result = module.settle_sales_partner("Talabat")
        self.assertTrue(result["success"])
        self.assertEqual(result["settled_count"], 0)
        mock_frappe.db.exists.assert_called_once()


if __name__ == "__main__":
    unittest.main()
