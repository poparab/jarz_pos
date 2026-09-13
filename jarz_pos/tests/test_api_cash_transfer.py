"""Tests for cash transfer API endpoints.

This module tests cash transfer and account management endpoints.
"""

import unittest
from unittest.mock import patch

import frappe


def _like(value, pattern):
	"""Case-insensitive SQL LIKE for the ``%text%`` patterns the module uses."""
	needle = (pattern or "").strip("%").lower()
	return needle in str(value or "").lower()


def _fake_account_get_all(accounts):
	"""A ``frappe.get_all`` stand-in that really applies Account filters.

	Equality, ``["in", [...]]`` filters and ``or_filters`` LIKE rows are
	evaluated against ``accounts``, so a test that plants a disabled or group
	account proves the query excludes it rather than the fake forgetting it.
	Any other DocType answers with no rows.
	"""

	def matches(row, field, cond):
		if isinstance(cond, (list, tuple)):
			op, val = cond[0], cond[1]
			if op == "in":
				return row.get(field) in val
			if op == "like":
				return _like(row.get(field), val)
			raise AssertionError(f"unsupported operator {op!r}")
		return row.get(field) == cond

	def fake_get_all(doctype, *args, filters=None, or_filters=None, fields=None, **kwargs):
		if doctype != "Account":
			return []
		out = []
		for row in accounts:
			if any(not matches(row, f, c) for f, c in (filters or {}).items()):
				continue
			if or_filters and not any(
				matches(row, of[1], [of[2], of[3]]) for of in or_filters
			):
				continue
			picked = {f: row.get(f) for f in fields} if fields else dict(row)
			out.append(picked)
		out.sort(key=lambda r: r.get("account_name") or "")
		return out

	return fake_get_all


def _account(name, account_type=None, is_group=0, disabled=0, root_type="Asset", company="JARZ"):
	return {
		"name": name,
		"account_name": name.split(" - ")[0],
		"account_type": account_type,
		"root_type": root_type,
		"is_group": is_group,
		"disabled": disabled,
		"company": company,
	}


class TestKashierGatewayAccount(unittest.TestCase):
	"""The Kashier payment-gateway ledger is a Cash Transfer endpoint.

	On production it is ``kashier - J``: a plain Current Asset leaf that is
	neither Cash/Bank typed nor named Mobile/Wallet, so before this rule it was
	missing from the From/To pickers, the balances overview and the history.
	Pure mocks: ``frappe.get_all`` is replaced, no site is touched.
	"""

	BASE = [
		_account("Cash - J", account_type="Cash"),
		_account("CIB - J", account_type="Bank"),
		_account("Vodafone Wallet - J"),
		_account("kashier - J", account_type="Current Asset"),
	]

	def _cashlike(self, accounts):
		from jarz_pos.api import cash_transfer

		with patch.object(cash_transfer.frappe, "get_all", side_effect=_fake_account_get_all(accounts)):
			return cash_transfer._get_cashlike_accounts("JARZ")

	def _list_accounts(self, accounts, pos_rows=None, partner_rows=None):
		from jarz_pos.api import cash_transfer

		with patch.object(cash_transfer, "_ensure_manager_access"), patch.object(
			cash_transfer.frappe, "get_all", side_effect=_fake_account_get_all(accounts)
		), patch.object(
			cash_transfer, "_get_pos_profile_accounts", return_value=list(pos_rows or [])
		), patch.object(
			cash_transfer, "_get_sales_partner_accounts", return_value=list(partner_rows or [])
		), patch.object(cash_transfer, "_get_balance_on", return_value=92821.0):
			return cash_transfer.list_accounts(company="JARZ")

	def test_kashier_current_asset_is_returned_as_gateway(self):
		rows = self._cashlike(self.BASE)
		kashier = [r for r in rows if r["name"] == "kashier - J"]
		self.assertEqual(len(kashier), 1)
		self.assertEqual(kashier[0], {
			"name": "kashier - J",
			"account_name": "kashier",
			"account_type": "Current Asset",
			"company": "JARZ",
			"category": "gateway",
		})

	def test_list_accounts_exposes_kashier_row_shape(self):
		out = self._list_accounts(self.BASE)
		kashier = [a for a in out if a["account"] == "kashier - J"]
		self.assertEqual(kashier, [{
			"account": "kashier - J",
			"label": "kashier",
			"company": "JARZ",
			"type": "Current Asset",
			"category": "gateway",
			"balance": 92821.0,
		}])

	def test_match_is_case_insensitive(self):
		accounts = [_account("KASHIER Settlement - J", account_type="Current Asset")]
		rows = self._cashlike(accounts)
		self.assertEqual([(r["name"], r["category"]) for r in rows],
						 [("KASHIER Settlement - J", "gateway")])

	def test_kashier_not_duplicated_when_also_bank_typed(self):
		accounts = [_account("Kashier - J", account_type="Bank")]
		rows = self._cashlike(accounts)
		self.assertEqual([(r["name"], r["category"]) for r in rows], [("Kashier - J", "bank")])

	def test_kashier_not_duplicated_when_also_named_wallet(self):
		accounts = [_account("Kashier Wallet - J", account_type="Current Asset")]
		rows = self._cashlike(accounts)
		self.assertEqual([(r["name"], r["category"]) for r in rows],
						 [("Kashier Wallet - J", "mobile")])

	def test_kashier_not_duplicated_across_pos_profile_and_partner_rules(self):
		dup = {"name": "kashier - J", "account_name": "kashier", "account_type": None,
			   "company": "JARZ", "category": "sales_partner"}
		out = self._list_accounts(self.BASE, pos_rows=[dict(dup, category="pos_profile")],
								  partner_rows=[dup])
		kashier = [a for a in out if a["account"] == "kashier - J"]
		self.assertEqual(len(kashier), 1)
		self.assertEqual(kashier[0]["category"], "gateway")

	def test_disabled_kashier_is_not_returned(self):
		accounts = [_account("kashier - J", account_type="Current Asset", disabled=1)]
		self.assertEqual(self._cashlike(accounts), [])

	def test_group_kashier_is_not_returned(self):
		accounts = [_account("kashier - J", is_group=1)]
		self.assertEqual(self._cashlike(accounts), [])

	def test_non_asset_kashier_fee_account_is_not_returned(self):
		accounts = [_account("Kashier Fees - J", account_type="Expense Account", root_type="Expense")]
		self.assertEqual(self._cashlike(accounts), [])

	def test_existing_accounts_still_appear(self):
		out = self._list_accounts(self.BASE)
		by_account = {a["account"]: a["category"] for a in out}
		self.assertEqual(by_account, {
			"Cash - J": "cash",
			"CIB - J": "bank",
			"Vodafone Wallet - J": "mobile",
			"kashier - J": "gateway",
		})
		# Stable UI order: cash, bank, mobile, then the gateway.
		self.assertEqual([a["account"] for a in out],
						 ["Cash - J", "CIB - J", "Vodafone Wallet - J", "kashier - J"])

	def test_kashier_is_in_transfer_history_membership(self):
		from jarz_pos.api import cash_transfer

		with patch.object(cash_transfer.frappe, "get_all", side_effect=_fake_account_get_all(self.BASE)), \
				patch.object(cash_transfer, "_get_pos_profile_accounts", return_value=[]), \
				patch.object(cash_transfer, "_get_sales_partner_accounts", return_value=[]):
			names = cash_transfer._transferable_account_names("JARZ")
		self.assertIn("kashier - J", names)
		self.assertIn("Cash - J", names)


class TestCashTransferAPI(unittest.TestCase):
	"""Test class for Cash Transfer API functionality."""

	def test_list_accounts_structure(self):
		"""Test that list_accounts returns correct structure."""
		from jarz_pos.api.cash_transfer import list_accounts

		# Test requires manager access, may fail without proper role
		try:
			result = list_accounts()

			# Verify response is a list
			self.assertIsInstance(result, list, "Should return a list")

			# If there are accounts, verify their structure
			if result:
				account = result[0]
				self.assertIn("account", account, "Account should have account key")
				self.assertIn("balance", account, "Account should have balance")
		except frappe.PermissionError:
			# User doesn't have manager access, which is expected
			pass

	def test_list_accounts_company_filter(self):
		"""Test that list_accounts accepts company parameter."""
		from jarz_pos.api.cash_transfer import list_accounts

		try:
			# Test with company parameter
			result = list_accounts(company="Test Company")
			self.assertIsInstance(result, list, "Should return a list")
		except frappe.PermissionError:
			# User doesn't have manager access
			pass
		except Exception:
			# Company may not exist
			pass

	def test_submit_transfer_validation(self):
		"""Test that submit_transfer validates required parameters."""
		from jarz_pos.api.cash_transfer import submit_transfer

		try:
			# Test without required parameters should raise an error
			with self.assertRaises(Exception):
				submit_transfer(from_account="", to_account="", amount=0)
		except frappe.PermissionError:
			# User doesn't have manager access
			pass

	def test_submit_transfer_negative_amount(self):
		"""Test that submit_transfer rejects negative amounts."""
		from jarz_pos.api.cash_transfer import submit_transfer

		try:
			# Test with negative amount should raise an error
			with self.assertRaises(Exception):
				submit_transfer(from_account="Cash - TC", to_account="Bank - TC", amount=-100)
		except frappe.PermissionError:
			# User doesn't have manager access
			pass
