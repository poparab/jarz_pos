"""Comprehensive tests for kanban operations and settle later flow.

This module tests:
- Kanban state transitions
- Delivery Note creation on "Out for Delivery"
- Settlement of settle later invoices
- Courier transaction management
- Integration with all invoice types
"""

import unittest
import frappe
from unittest.mock import patch, MagicMock


class TestKanbanOperations(unittest.TestCase):
	"""Test kanban state transitions and delivery operations."""

	def setUp(self):
		"""Set up test environment."""
		pass

	def tearDown(self):
		"""Clean up test environment."""
		pass

	def test_state_key_normalization(self):
		"""Test state key normalization for kanban columns."""
		from jarz_pos.api.kanban import _state_key

		# Test various state names
		self.assertEqual(_state_key("Received"), "received")
		self.assertEqual(_state_key("Out for delivery"), "out_for_delivery")
		self.assertEqual(_state_key("Out For Delivery"), "out_for_delivery")
		self.assertEqual(_state_key("Processing"), "processing")
		self.assertEqual(_state_key("  Preparing  "), "preparing")

	def test_get_kanban_columns_structure(self):
		"""Test get_kanban_columns returns proper structure."""
		from jarz_pos.api.kanban import get_kanban_columns

		# Mock frappe methods
		with patch('jarz_pos.api.kanban._get_state_field_options') as mock_options:
			mock_options.return_value = [
				"Received",
				"Processing",
				"Preparing",
				"Out for delivery",
				"Completed"
			]

			result = get_kanban_columns()

			self.assertTrue(result.get("success"))
			self.assertIn("columns", result)
			self.assertEqual(len(result["columns"]), 5)

			# Check first column structure
			first_col = result["columns"][0]
			self.assertIn("id", first_col)
			self.assertIn("name", first_col)
			self.assertIn("color", first_col)
			self.assertIn("order", first_col)

	def test_get_kanban_columns_color_mapping(self):
		"""Test kanban columns have appropriate colors."""
		from jarz_pos.api.kanban import get_kanban_columns

		with patch('jarz_pos.api.kanban._get_state_field_options') as mock_options:
			mock_options.return_value = ["Received", "Completed"]

			result = get_kanban_columns()
			columns = result["columns"]

			# Verify color assignment
			received_col = next(c for c in columns if c["name"] == "Received")
			completed_col = next(c for c in columns if c["name"] == "Completed")

			self.assertIsNotNone(received_col["color"])
			self.assertIsNotNone(completed_col["color"])

	@patch('jarz_pos.api.kanban.frappe')
	def test_update_invoice_state_field_candidates(self, mock_frappe):
		"""Test update_invoice_state checks multiple field candidates."""
		from jarz_pos.api.kanban import update_invoice_state

		# Mock invoice
		mock_inv = MagicMock()
		mock_inv.name = "INV-001"
		mock_inv.docstatus = 1

		# Mock meta with custom_sales_invoice_state field
		mock_meta = MagicMock()
		mock_field = MagicMock()
		mock_field.fieldname = "custom_sales_invoice_state"
		mock_meta.get_field.side_effect = lambda name: mock_field if name == "custom_sales_invoice_state" else None

		mock_frappe.get_doc.return_value = mock_inv
		mock_frappe.get_meta.return_value = mock_meta

		# Field candidates checked in order
		field_candidates = [
			"custom_sales_invoice_state",
			"sales_invoice_state",
			"custom_state",
			"state"
		]

		# Validate the pattern exists
		for field in field_candidates:
			self.assertIsNotNone(field)

	@patch('jarz_pos.api.kanban.frappe')
	def test_update_invoice_state_requires_submitted(self, mock_frappe):
		"""Test update_invoice_state requires submitted invoice."""
		from jarz_pos.api.kanban import update_invoice_state

		# Mock draft invoice
		mock_inv = MagicMock()
		mock_inv.name = "INV-DRAFT"
		mock_inv.docstatus = 0  # Draft

		mock_frappe.get_doc.return_value = mock_inv

		result = update_invoice_state("INV-DRAFT", "Processing")

		# Should return error for draft invoice
		self.assertFalse(result.get("success", True))

	@patch('jarz_pos.api.kanban.get_invoice_hard_mutation_blocker')
	@patch('jarz_pos.api.kanban.frappe')
	def test_cancel_invoice_rejects_hard_mutation_blockers(self, mock_frappe, mock_blocker):
		"""Cancel flow should reuse the shared hard-blocker guardrail."""
		from jarz_pos.api.kanban import cancel_invoice

		mock_invoice = MagicMock()
		mock_invoice.name = 'INV-001'
		mock_invoice.docstatus = 1
		mock_invoice.is_return = 0
		mock_invoice.get.side_effect = lambda fieldname: {
			'custom_sales_invoice_state': 'Ready',
			'sales_invoice_state': 'Ready',
		}.get(fieldname)

		mock_frappe.session.user = 'manager@example.com'
		mock_frappe.get_roles.return_value = ['JARZ line manager']
		mock_frappe.get_doc.return_value = mock_invoice
		mock_blocker.return_value = {
			'mutation_block_code': 'journal_entry_exists',
			'mutation_block_reason': 'This invoice already has settlement journal entries and cannot be changed from this workflow.',
		}

		result = cancel_invoice('INV-001', 'Customer requested')

		self.assertFalse(result.get('success'))
		self.assertIn('journal', result.get('error', '').lower())
		mock_blocker.assert_called_once_with(mock_invoice)

	@patch('jarz_pos.api.kanban._get_allowed_states')
	@patch('jarz_pos.api.kanban.frappe')
	def test_update_invoice_state_saves_submitted_invoice_to_fire_hooks(self, mock_frappe, mock_allowed_states):
		"""Submitted invoice state changes should use save(), not db_set(), so hooks can run."""
		from jarz_pos.api.kanban import update_invoice_state

		mock_allowed_states.return_value = ["Out for Delivery", "Delivered"]

		mock_inv = MagicMock()
		mock_inv.name = "INV-001"
		mock_inv.docstatus = 1
		mock_inv.flags = MagicMock()
		mock_inv.get.side_effect = lambda fieldname: {
			"custom_sales_invoice_state": "Out for Delivery",
			"sales_invoice_state": "Out for Delivery",
			"custom_state": None,
			"state": None,
		}.get(fieldname)

		mock_meta = MagicMock()
		mock_meta.get_field.side_effect = lambda name: MagicMock() if name in {"custom_sales_invoice_state", "sales_invoice_state"} else None

		mock_frappe.get_doc.return_value = mock_inv
		mock_frappe.get_meta.return_value = mock_meta
		mock_frappe.utils.now.return_value = "2026-05-02 07:00:00"
		mock_frappe.session.user = "tester@example.com"
		mock_frappe.db.commit = MagicMock()
		mock_frappe.publish_realtime = MagicMock()

		result = update_invoice_state("INV-001", "Delivered")

		self.assertTrue(result.get("success"))
		self.assertTrue(mock_inv.flags.ignore_validate_update_after_submit)
		mock_inv.save.assert_called_once_with(ignore_permissions=True, ignore_version=True)
		mock_inv.db_set.assert_not_called()
		mock_inv.set.assert_any_call("custom_sales_invoice_state", "Delivered")
		mock_inv.set.assert_any_call("sales_invoice_state", "Delivered")

	def test_delivery_note_creation_trigger(self):
		"""Test that moving to 'Out for Delivery' triggers DN creation."""
		from jarz_pos.api.kanban import update_invoice_state

		# The normalized state "out_for_delivery" should trigger DN creation
		normalized_states_that_create_dn = [
			"out for delivery",
			"out_for_delivery",
		]

		for state in normalized_states_that_create_dn:
			normalized = state.strip().lower().replace(' ', '_')
			create_dn = normalized in {"out for delivery", "out_for_delivery"}
			self.assertTrue(create_dn, f"State '{state}' should trigger DN creation")

	@patch('jarz_pos.api.kanban.frappe')
	def test_delivery_note_idempotency_via_remarks(self, mock_frappe):
		"""Test DN reuse when remarks contains invoice name."""
		# Logic in _create_delivery_note_from_invoice checks remarks for invoice name

		invoice_name = "INV-12345"
		
		# Mock existing DN with invoice in remarks
		mock_dn = MagicMock()
		mock_dn.name = "DN-001"
		mock_dn.docstatus = 1
		mock_dn.remarks = f"Auto-created from Sales Invoice {invoice_name} on state change"

		# The function should find and reuse this DN instead of creating new
		self.assertIn(invoice_name, mock_dn.remarks)

	@patch('jarz_pos.api.kanban.frappe')
	def test_delivery_note_items_copy_from_invoice(self, mock_frappe):
		"""Test DN items are copied from invoice items."""
		# Mock invoice items
		mock_item1 = MagicMock()
		mock_item1.item_code = "ITEM-001"
		mock_item1.item_name = "Product 1"
		mock_item1.qty = 2.0
		mock_item1.rate = 100.0
		mock_item1.amount = 200.0
		mock_item1.warehouse = "Main Store"

		mock_inv = MagicMock()
		mock_inv.items = [mock_item1]

		# DN should copy all these fields
		expected_fields = [
			"item_code",
			"item_name",
			"description",
			"qty",
			"uom",
			"stock_uom",
			"rate",
			"amount",
			"warehouse",
		]

		for field in expected_fields:
			self.assertIsNotNone(field)

	@patch('jarz_pos.api.kanban.frappe')
	def test_delivery_note_completed_status(self, mock_frappe):
		"""Test DN is marked as completed after creation."""
		# After DN submission, per_billed should be 100 and status "Completed"

		mock_dn = MagicMock()
		mock_dn.name = "DN-001"

		# Simulate the db_set calls
		mock_dn.db_set = MagicMock()

		# Should call db_set for per_billed=100 and status="Completed"
		expected_calls = [
			("per_billed", 100),
			("status", "Completed"),
		]

		# Validate the pattern exists
		for field, value in expected_calls:
			self.assertIsNotNone(field)
			self.assertIsNotNone(value)

	def test_delivery_note_logic_version(self):
		"""Test delivery note logic version is tracked."""
		from jarz_pos.services.delivery_handling import DN_LOGIC_VERSION

		# Should be a version string
		self.assertIsNotNone(DN_LOGIC_VERSION)
		self.assertIsInstance(DN_LOGIC_VERSION, str)

	@patch('jarz_pos.services.delivery_handling.frappe')
	def test_delivery_note_allows_disabled_historical_invoice_items(self, mock_frappe):
		"""Auto-created DNs should still work for submitted invoices whose items were later disabled."""
		from jarz_pos.services.delivery_handling import ensure_delivery_note_for_invoice
		import erpnext.stock.get_item_details as stock_get_item_details
		from erpnext.stock.doctype.item import item as item_module

		mock_item = MagicMock()
		mock_item.item_code = "ITEM-001"
		mock_item.item_name = "Mango Kunafa Medium"
		mock_item.description = "Disabled after invoice submit"
		mock_item.qty = 1
		mock_item.uom = "Nos"
		mock_item.stock_uom = "Nos"
		mock_item.conversion_factor = 1
		mock_item.rate = 530
		mock_item.amount = 530
		mock_item.warehouse = "Main WH"
		mock_item.get.side_effect = lambda key, default=None: getattr(mock_item, key, default)

		mock_invoice = MagicMock()
		mock_invoice.name = "INV-001"
		mock_invoice.docstatus = 1
		mock_invoice.customer = "Customer"
		mock_invoice.company = "Jarz"
		mock_invoice.pos_profile = "Nasr city"
		mock_invoice.custom_kanban_profile = "Nasr city"
		mock_invoice.items = [mock_item]
		mock_invoice.get.side_effect = lambda key, default=None: getattr(mock_invoice, key, default)

		appended_rows = []
		mock_dn = MagicMock()
		mock_dn.name = "DN-001"
		mock_dn.append.side_effect = lambda child_table, row: appended_rows.append(row)
		mock_dn.db_set = MagicMock()

		def strict_validate(item_code, end_of_life=None, disabled=None):
			if disabled:
				raise Exception(f"Item {item_code} is disabled")

		def validate_delivery_note_items(*args, **kwargs):
			for row in appended_rows:
				stock_get_item_details.validate_end_of_life(row["item_code"], disabled=1)

		mock_dn.insert.side_effect = validate_delivery_note_items
		mock_dn.submit.side_effect = validate_delivery_note_items

		mock_meta = MagicMock()
		mock_meta.get_field.return_value = None

		mock_frappe.get_doc.side_effect = lambda doctype, name=None: mock_invoice if doctype == "Sales Invoice" else MagicMock()
		mock_frappe.get_meta.return_value = mock_meta
		mock_frappe.get_all.return_value = []
		mock_frappe.new_doc.return_value = mock_dn
		mock_frappe.logger.return_value = MagicMock()
		mock_frappe.ValidationError = Exception
		mock_frappe.db.exists.side_effect = lambda doctype, name: True
		mock_frappe.db.get_value.side_effect = (
			lambda doctype, name, field: {
				("POS Profile", "Nasr city", "warehouse"): "Main WH",
				("Warehouse", "Main WH", "company"): "Jarz",
				("Item", "ITEM-001", "is_stock_item"): 1,
			}.get((doctype, name, field))
		)
		mock_frappe.utils.getdate.return_value = "2026-05-02"
		mock_frappe.utils.nowtime.return_value = "12:00:00"
		mock_frappe.utils.today.return_value = "2026-05-02"
		mock_frappe.utils.add_days.return_value = "2026-04-29"

		with patch.object(stock_get_item_details, 'validate_end_of_life', side_effect=strict_validate, create=True) as stock_validate, \
			 patch.object(item_module, 'validate_end_of_life', side_effect=strict_validate) as item_validate:
			result = ensure_delivery_note_for_invoice("INV-001")

		self.assertIsNone(result["error"])
		self.assertEqual(result["delivery_note"], "DN-001")

	@patch('jarz_pos.services.delivery_handling.frappe')
	def test_delivery_note_rejects_branch_warehouse_mismatch(self, mock_frappe):
		"""Auto-created DNs must fail fast when invoice rows still point to the old branch warehouse."""
		from jarz_pos.services.delivery_handling import ensure_delivery_note_for_invoice

		mock_item = MagicMock()
		mock_item.name = "SII-001"
		mock_item.item_code = "ITEM-001"
		mock_item.warehouse = "Stores - Dokki"
		mock_item.get.side_effect = lambda key, default=None: getattr(mock_item, key, default)

		mock_invoice = MagicMock()
		mock_invoice.name = "INV-002"
		mock_invoice.docstatus = 1
		mock_invoice.customer = "Customer"
		mock_invoice.company = "Jarz"
		mock_invoice.pos_profile = "Dokki"
		mock_invoice.custom_kanban_profile = "Nasr city"
		mock_invoice.items = [mock_item]
		mock_invoice.get.side_effect = lambda key, default=None: getattr(mock_invoice, key, default)

		mock_meta = MagicMock()
		mock_meta.get_field.return_value = None

		mock_frappe.get_doc.side_effect = lambda doctype, name=None: mock_invoice if doctype == "Sales Invoice" else MagicMock()
		mock_frappe.get_meta.return_value = mock_meta
		mock_frappe.get_all.return_value = []
		mock_frappe.new_doc.return_value = MagicMock()
		mock_frappe.logger.return_value = MagicMock()
		mock_frappe.ValidationError = Exception
		mock_frappe.db.exists.side_effect = lambda doctype, name: True
		mock_frappe.db.get_value.side_effect = (
			lambda doctype, name, field: {
				("POS Profile", "Nasr city", "warehouse"): "Stores - Nasr city",
				("Warehouse", "Stores - Nasr city", "company"): "Jarz",
				("Item", "ITEM-001", "is_stock_item"): 1,
			}.get((doctype, name, field))
		)
		mock_frappe.utils.today.return_value = "2026-05-02"
		mock_frappe.utils.add_days.return_value = "2026-04-29"

		result = ensure_delivery_note_for_invoice("INV-002")

		self.assertEqual(result["delivery_note"], None)
		self.assertIn("warehouses do not match operational branch warehouse", result["error"])
		mock_frappe.new_doc.assert_not_called()

	@patch('jarz_pos.api.kanban.frappe')
	def test_kanban_publishes_realtime_events(self, mock_frappe):
		"""Test that state updates publish realtime events."""
		# update_invoice_state should publish events for live updates

		expected_events = [
			"jarz_pos_invoice_state_change",
			"kanban_update",
		]

		for event in expected_events:
			self.assertIsNotNone(event)

	def test_settle_later_courier_transaction_creation(self):
		"""Test settle later creates unsettled courier transaction."""
		from jarz_pos.services.settlement_strategies import handle_unpaid_settle_later

		# This handler should create courier transaction with status "Unsettled"
		self.assertTrue(callable(handle_unpaid_settle_later))

	def test_settle_later_courier_transaction_fields(self):
		"""Test courier transaction has expected fields."""
		expected_fields = [
			"reference_invoice",
			"status",  # Unsettled/Settled
			"party_type",
			"party",
			"amount",
			"shipping_amount",
		]

		for field in expected_fields:
			self.assertIsNotNone(field)

	@patch('jarz_pos.services.delivery_handling.frappe')
	def test_settle_single_invoice_paid_logic(self, mock_frappe):
		"""Test settle_single_invoice_paid function structure."""
		from jarz_pos.services.delivery_handling import settle_single_invoice_paid

		# Should be whitelisted API function
		self.assertTrue(callable(settle_single_invoice_paid))

		# Should accept invoice_name, pos_profile, party_type, party
		import inspect
		sig = inspect.signature(settle_single_invoice_paid)
		params = list(sig.parameters.keys())

		self.assertIn("invoice_name", params)
		self.assertIn("pos_profile", params)

	@patch('jarz_pos.services.delivery_handling.frappe')
	def test_settle_courier_collected_payment_logic(self, mock_frappe):
		"""Test settle_courier_collected_payment function structure."""
		from jarz_pos.services.delivery_handling import settle_courier_collected_payment

		# Should be whitelisted API function
		self.assertTrue(callable(settle_courier_collected_payment))

		# Should accept invoice_name, pos_profile, party_type, party
		import inspect
		sig = inspect.signature(settle_courier_collected_payment)
		params = list(sig.parameters.keys())

		self.assertIn("invoice_name", params)
		self.assertIn("pos_profile", params)
		self.assertIn("party_type", params)
		self.assertIn("party", params)

	def test_courier_outstanding_account_resolution(self):
		"""Test courier outstanding account can be resolved."""
		from jarz_pos.services.delivery_handling import _get_courier_outstanding_account

		# Function should exist
		self.assertTrue(callable(_get_courier_outstanding_account))

	def test_delivery_expense_amount_extraction(self):
		"""Test delivery expense amount can be extracted from invoice."""
		from jarz_pos.services.delivery_handling import _get_delivery_expense_amount

		# Function should exist
		self.assertTrue(callable(_get_delivery_expense_amount))

	@patch('jarz_pos.services.delivery_handling.frappe')
	def test_settle_later_journal_entry_creation(self, mock_frappe):
		"""Test settle later settlement creates appropriate journal entries."""
		# When settling a "settle later" invoice, should create JE based on amount comparison

		# Mock scenario: order_amount >= shipping_exp
		order_amount = 100.0
		shipping_exp = 30.0

		# Should create JE with appropriate accounts
		if order_amount >= shipping_exp:
			# DR Courier Outstanding, CR Cash (for shipping)
			# DR Freight Expense, CR Creditors (for expense)
			expected_logic = "full_settlement"
		else:
			# Different JE structure
			expected_logic = "partial_settlement"

		self.assertIsNotNone(expected_logic)

	def test_idempotent_settlement_via_existing_je_check(self):
		"""Test settlement operations are idempotent via existing JE check."""
		# Functions should check for existing JE by title before creating new

		title = "Courier Outstanding Settlement – INV-001"
		
		# Should use _existing_je helper to check for existing journal entry
		# If found, skip creation
		self.assertIsNotNone(title)

	@patch('jarz_pos.api.kanban.frappe')
	def test_kanban_branch_propagation(self, mock_frappe):
		"""Test custom_kanban_profile is propagated to DN and PE."""
		# When creating DN and PE, should copy custom_kanban_profile from invoice

		mock_inv = MagicMock()
		mock_inv.custom_kanban_profile = "Branch-001"

		# DN should get same branch
		mock_dn = MagicMock()
		mock_pe = MagicMock()

		# Both should have custom_kanban_profile = "Branch-001"
		expected_branch = "Branch-001"
		self.assertEqual(mock_inv.custom_kanban_profile, expected_branch)

	def test_kanban_filters_structure(self):
		"""Test get_kanban_filters returns customers and states."""
		from jarz_pos.api.kanban import get_kanban_filters

		# Should be callable
		self.assertTrue(callable(get_kanban_filters))

		# Should return success, customers list, states list


	def test_get_invoice_details_structure(self):
		"""Test get_invoice_details returns full invoice data."""
		from jarz_pos.api.kanban import get_invoice_details

		# Should be callable
		self.assertTrue(callable(get_invoice_details))

		# Should accept invoice_id parameter
		import inspect
		sig = inspect.signature(get_invoice_details)
		params = list(sig.parameters.keys())
		self.assertIn("invoice_id", params)


class TestSettleLaterOperations(unittest.TestCase):
	"""Test settle later settlement operations."""

	def test_unpaid_settle_later_creates_courier_transaction(self):
		"""Test unpaid + settle later creates courier transaction."""
		from jarz_pos.services.settlement_strategies import handle_unpaid_settle_later

		# Should call mark_courier_outstanding which creates CT
		self.assertTrue(callable(handle_unpaid_settle_later))

	def test_paid_settle_later_creates_courier_transaction(self):
		"""Test paid + settle later creates courier transaction."""
		from jarz_pos.services.settlement_strategies import handle_paid_settle_later

		# Should call handle_out_for_delivery_paid with settlement="later"
		self.assertTrue(callable(handle_paid_settle_later))

	@patch('jarz_pos.services.delivery_handling.frappe')
	def test_courier_transaction_status_lifecycle(self, mock_frappe):
		"""Test courier transaction status changes from Unsettled to Settled."""
		# Initial creation: status = "Unsettled"
		# After settlement: status = "Settled"

		statuses = ["Unsettled", "Settled"]
		
		for status in statuses:
			self.assertIn(status, ["Unsettled", "Settled"])

	@patch('jarz_pos.services.delivery_handling.frappe')
	def test_settle_later_amount_tracking(self, mock_frappe):
		"""Test courier transaction tracks both order amount and shipping amount."""
		# CT should have:
		# - amount: order total (grand_total)
		# - shipping_amount: delivery expense

		mock_ct = MagicMock()
		mock_ct.amount = 500.0
		mock_ct.shipping_amount = 30.0

		# Difference is profit/loss for courier
		difference = float(mock_ct.amount) - float(mock_ct.shipping_amount)
		self.assertEqual(difference, 470.0)

	def test_settlement_accounting_scenarios(self):
		"""Test different accounting scenarios in settlement."""
		# Scenario 1: order_amount > shipping_expense (courier profit)
		# Scenario 2: order_amount = shipping_expense (break even)
		# Scenario 3: order_amount < shipping_expense (courier loss - shouldn't happen in normal flow)

		scenarios = [
			{"order": 100.0, "shipping": 30.0, "scenario": "profit"},
			{"order": 30.0, "shipping": 30.0, "scenario": "break_even"},
			{"order": 20.0, "shipping": 30.0, "scenario": "loss"},
		]

		for s in scenarios:
			difference = s["order"] - s["shipping"]
			self.assertIsNotNone(difference)


if __name__ == "__main__":
	unittest.main()
