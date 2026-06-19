"""B2B permission-gating tests (pure mock / unittest).

Covers the two new permission seams of the B2B-mode backend WITHOUT any DB:

  1. ``commercial_policy._ensure_policy_permission`` — a non-Standard order purpose is
     now permitted for the ``B2B Sales Rep`` role OR a manager-pricing user, while an
     explicit ``policy.require_role`` override still wins.
  2. ``invoice_creation._ensure_can_place_standard_order`` — blocks a B2B-Sales-Rep-ONLY
     user from placing a Standard (B2C) retail order, but is a strict NO-OP for managers,
     cashiers, and every non-B2B user — proving Standard B2C stays byte-identical.

We deliberately use plain ``unittest.TestCase`` and ``unittest.mock`` (no FrappeTestCase /
``erpnext.tests.utils``) so the module is non-destructive and CI-safe under
``--skip-before-tests`` on ERPNext v16.
"""

from __future__ import annotations

import types
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from jarz_pos.services import commercial_policy as cp
from jarz_pos.services import invoice_creation as ic


def _ns(**kwargs):
    return types.SimpleNamespace(**kwargs)


@contextmanager
def _roles(role_list):
    """Patch frappe.get_roles (in both modules) to a fixed role set.

    The code calls ``frappe.get_roles(frappe.session.user)``; mocking get_roles to
    ignore its argument lets us pin the role set without touching the (dotdict)
    session, whose attribute is not patchable via ``patch.object``.
    """
    with patch.object(cp.frappe, "get_roles", return_value=list(role_list)), patch.object(
        ic.frappe, "get_roles", return_value=list(role_list)
    ):
        yield


# ---------------------------------------------------------------------------
# _ensure_policy_permission — default gate (no explicit require_role)
# ---------------------------------------------------------------------------
class TestPolicyPermissionDefaultGate(unittest.TestCase):
    """Non-Standard purpose is allowed for B2B Sales Rep OR manager-pricing access."""

    def _policy(self):
        return _ns(order_purpose="B2B Supply", require_role=None)

    def test_b2b_sales_rep_permitted(self):
        # B2B Sales Rep is allowed even WITHOUT manager pricing access.
        with _roles(["B2B Sales Rep"]):
            with patch(
                "jarz_pos.services.invoice_creation._has_manager_pricing_access",
                return_value=False,
            ):
                cp._ensure_policy_permission(self._policy())  # must NOT raise

    def test_manager_pricing_permitted(self):
        # A manager (no B2B role) is still permitted via manager-pricing access.
        with _roles(["JARZ Manager"]):
            with patch(
                "jarz_pos.services.invoice_creation._has_manager_pricing_access",
                return_value=True,
            ):
                cp._ensure_policy_permission(self._policy())  # must NOT raise

    def test_neither_role_rejected(self):
        # A plain user with neither B2B nor manager pricing access is rejected.
        with _roles(["Sales User"]):
            with patch(
                "jarz_pos.services.invoice_creation._has_manager_pricing_access",
                return_value=False,
            ):
                with self.assertRaises(Exception):
                    cp._ensure_policy_permission(self._policy())


# ---------------------------------------------------------------------------
# _ensure_policy_permission — explicit require_role override
# ---------------------------------------------------------------------------
class TestPolicyPermissionRequireRole(unittest.TestCase):
    """An explicit policy.require_role overrides the default B2B/manager gate."""

    def test_require_role_granted_overrides_default(self):
        policy = _ns(order_purpose="B2B Supply", require_role="Special B2B Role")
        # Holds the required role but is NOT a B2B rep / manager -> still granted.
        with _roles(["Special B2B Role"]):
            cp._ensure_policy_permission(policy)  # must NOT raise

    def test_require_role_denied_even_if_b2b_rep(self):
        # The override is exclusive: holding "B2B Sales Rep" does NOT satisfy a
        # policy that explicitly requires a different role.
        policy = _ns(order_purpose="B2B Supply", require_role="Special B2B Role")
        with _roles(["B2B Sales Rep"]):
            with self.assertRaises(Exception):
                cp._ensure_policy_permission(policy)


# ---------------------------------------------------------------------------
# _ensure_can_place_standard_order — B2C retail guard
# ---------------------------------------------------------------------------
class TestStandardOrderGuard(unittest.TestCase):
    """Only a B2B-Sales-Rep-ONLY user is blocked from Standard; everyone else is inert."""

    # --- The single blocking case ----------------------------------------
    def test_b2b_rep_only_blocked_on_standard(self):
        with _roles(["B2B Sales Rep"]):
            with self.assertRaises(Exception):
                ic._ensure_can_place_standard_order("Standard")

    def test_b2b_rep_only_blocked_on_empty_purpose(self):
        # An absent/empty purpose is treated as Standard.
        with _roles(["B2B Sales Rep"]):
            with self.assertRaises(Exception):
                ic._ensure_can_place_standard_order("")
            with self.assertRaises(Exception):
                ic._ensure_can_place_standard_order(None)

    # --- NO-OP cases: Standard B2C stays byte-identical -------------------
    def test_manager_is_noop(self):
        with _roles(["B2B Sales Rep", "JARZ Manager"]):
            ic._ensure_can_place_standard_order("Standard")  # must NOT raise

    def test_cashier_is_noop(self):
        with _roles(["B2B Sales Rep", "Jarz POS Staff"]):
            ic._ensure_can_place_standard_order("Standard")  # must NOT raise

    def test_plain_non_b2b_user_is_noop(self):
        # A regular cashier/user without the B2B role is never touched.
        with _roles(["Jarz POS Staff"]):
            ic._ensure_can_place_standard_order("Standard")  # must NOT raise

    def test_system_manager_is_noop(self):
        with _roles(["B2B Sales Rep", "System Manager"]):
            ic._ensure_can_place_standard_order("Standard")  # must NOT raise

    # --- Non-Standard purpose: guard never fires regardless of roles ------
    def test_non_standard_purpose_is_noop_for_b2b_rep(self):
        with _roles(["B2B Sales Rep"]):
            ic._ensure_can_place_standard_order("B2B Supply")  # must NOT raise
            ic._ensure_can_place_standard_order("Sample - Courier")  # must NOT raise


if __name__ == "__main__":
    unittest.main()
