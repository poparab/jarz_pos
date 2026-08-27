"""The line-manager tier invariant.

Everything a ``jarz line manager`` may do, a ``JARZ Manager``, a ``System
Manager`` and the ``Administrator`` may do too. The line manager is a *narrower*
manager — a floor supervisor — never the holder of an authority its own manager
lacks.

This used to be maintained by hand at every gate and drifted: ``cancel_invoice``
and the return workflow were gated on ``{Administrator, jarz line manager}``, so
a JARZ Manager was refused on their own branch's orders. These tests fail if any
gate goes back to naming the line manager alone.
"""

import unittest
from unittest.mock import MagicMock, patch

from jarz_pos.constants import ROLES


#: Every role that must clear a line-manager gate. Both spellings of the line
#: manager exist as real Role records on staging and production.
TIER_MEMBERS = [
    "jarz line manager",
    "JARZ line manager",
    "JARZ Manager",
    "System Manager",
    "Administrator",
]


class TestLineManagerTierConstant(unittest.TestCase):
    def test_tier_contains_manager_and_admin(self):
        for role in TIER_MEMBERS:
            self.assertIn(role, ROLES.LINE_MANAGER_TIER, role)

    def test_lower_variant_stays_in_lockstep(self):
        self.assertEqual(
            ROLES.LINE_MANAGER_TIER_LOWER,
            {role.lower() for role in ROLES.LINE_MANAGER_TIER},
        )

    def test_tier_excludes_the_rank_and_file(self):
        for role in ("POS User", "Sales User", "Moderator", "Jarz POS Staff"):
            self.assertNotIn(role, ROLES.LINE_MANAGER_TIER, role)
            self.assertNotIn(role.lower(), ROLES.LINE_MANAGER_TIER_LOWER, role)


class TestReturnGate(unittest.TestCase):
    """``returns._ensure_return_permission`` accepts the whole tier."""

    def _run(self, roles):
        from jarz_pos.api import returns

        with patch.object(returns, "frappe") as mock_frappe:
            mock_frappe.PermissionError = PermissionError
            mock_frappe.throw.side_effect = PermissionError
            mock_frappe.session.user = "someone@example.com"
            mock_frappe.get_roles.return_value = roles
            returns._ensure_return_permission()

    def test_every_tier_member_is_allowed(self):
        for role in TIER_MEMBERS:
            with self.subTest(role=role):
                self._run([role])

    def test_plain_pos_user_is_refused(self):
        with self.assertRaises(PermissionError):
            self._run(["POS User", "Sales User", "Moderator"])


class TestCancelGate(unittest.TestCase):
    """``kanban.cancel_invoice`` accepts the whole tier.

    Asserted by the *absence* of the role refusal: the call is stopped one step
    later by a hard mutation blocker, which only runs once the role check has
    passed.
    """

    REFUSAL = "You are not permitted to cancel orders"

    def _run(self, roles):
        from jarz_pos.api.kanban import cancel_invoice

        invoice = MagicMock()
        invoice.name = "INV-001"
        invoice.docstatus = 1
        invoice.is_return = 0
        invoice.get.side_effect = lambda fieldname: {
            "custom_sales_invoice_state": "Ready",
            "sales_invoice_state": "Ready",
        }.get(fieldname)

        with patch("jarz_pos.api.kanban.get_invoice_hard_mutation_blocker") as blocker, \
                patch("jarz_pos.api.kanban.ensure_profile_scoped_invoice_access"), \
                patch("jarz_pos.api.kanban.frappe") as mock_frappe:
            mock_frappe.session.user = "someone@example.com"
            mock_frappe.get_roles.return_value = roles
            mock_frappe.get_doc.return_value = invoice
            blocker.return_value = {
                "mutation_block_code": "journal_entry_exists",
                "mutation_block_reason": "blocked further down",
            }
            return cancel_invoice("INV-001", "Customer requested")

    def test_every_tier_member_clears_the_role_check(self):
        for role in TIER_MEMBERS:
            with self.subTest(role=role):
                result = self._run([role])
                self.assertNotIn(self.REFUSAL, result.get("error", ""))

    def test_plain_pos_user_is_refused(self):
        result = self._run(["POS User", "Sales User", "Moderator"])
        self.assertFalse(result.get("success"))
        self.assertIn(self.REFUSAL, result.get("error", ""))


class TestStockTransferGate(unittest.TestCase):
    """``transfer._ensure_transfer_access`` accepts the whole tier.

    Stock Transfer used to sit on the bare ``ROLES.MANAGER`` set, so a line
    manager could neither see the drawer entry nor call the API. Moving jars
    between a branch and Finished Goods is floor-supervisor work; Cash Transfer
    and the Purchase Invoice stay on ``ROLES.MANAGER`` because they commit money.
    """

    def _run(self, roles):
        from jarz_pos.api import transfer

        with patch.object(transfer, "frappe") as mock_frappe:
            mock_frappe.PermissionError = PermissionError
            mock_frappe.throw.side_effect = PermissionError
            mock_frappe.get_roles.return_value = roles
            transfer._ensure_transfer_access()

    def test_every_tier_member_is_allowed(self):
        for role in TIER_MEMBERS:
            with self.subTest(role=role):
                self._run([role])

    def test_the_manager_set_keeps_its_access(self):
        for role in sorted(ROLES.MANAGER):
            with self.subTest(role=role):
                self._run([role])

    def test_plain_pos_user_is_refused(self):
        with self.assertRaises(PermissionError):
            self._run(["POS User", "Sales User", "Moderator"])

    def test_cash_transfer_stays_narrower(self):
        """The line manager is widened into Stock Transfer only."""
        for role in (ROLES.JARZ_LINE_MANAGER, ROLES.JARZ_LINE_MANAGER_ALT):
            with self.subTest(role=role):
                self.assertIn(role, ROLES.STOCK_TRANSFER)
                self.assertNotIn(role, ROLES.MANAGER)
                self.assertNotIn(role, ROLES.STOCK)
                self.assertNotIn(role, ROLES.PURCHASE)
