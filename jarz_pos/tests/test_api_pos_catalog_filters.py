import unittest
from unittest.mock import patch


class TestPOSCatalogFilters(unittest.TestCase):

	@patch('jarz_pos.api.pos.frappe')
	def test_get_profile_products_filters_disabled_and_non_sales_items(self, mock_frappe):
		from jarz_pos.api.pos import get_profile_products

		expected_items = [
			{
				'id': 'ITEM-VALID',
				'name': 'Valid Product',
				'price': 35,
				'item_group': 'Hot Drinks',
			}
		]

		def get_all_side_effect(doctype, filters=None, pluck=None, fields=None, **kwargs):
			if doctype == 'POS Profile Item Group':
				self.assertEqual(filters, {'parent': 'POS-1'})
				self.assertEqual(pluck, 'item_group')
				return ['Hot Drinks']

			if doctype == 'Item':
				self.assertEqual(
					filters,
					{'item_group': ('in', ['Hot Drinks']), 'disabled': 0, 'is_sales_item': 1},
				)
				return expected_items

			raise AssertionError(f'Unexpected get_all call for {doctype}')

		mock_frappe.get_all.side_effect = get_all_side_effect
		mock_frappe.db.get_value.return_value = None

		result = get_profile_products(profile='POS-1')

		self.assertEqual(result, expected_items)

	def test_get_profile_bundles_filters_invalid_bundles_and_empty_required_groups(self):
		from jarz_pos.api.pos import get_profile_bundles

		source_bundles = [
			{
				'id': 'BUNDLE-VALID',
				'name': 'Valid Bundle',
				'price': 120,
				'free_shipping': '1',
				'erpnext_item': 'ERP-VALID',
				'disabled': 0,
			},
			{
				'id': 'BUNDLE-DISABLED',
				'name': 'Disabled Bundle',
				'price': 90,
				'free_shipping': '0',
				'erpnext_item': 'ERP-DISABLED-BUNDLE',
				'disabled': 1,
			},
			{
				'id': 'BUNDLE-PARENT-DISABLED',
				'name': 'Parent Disabled Bundle',
				'price': 80,
				'free_shipping': '0',
				'erpnext_item': 'ERP-DISABLED',
				'disabled': 0,
			},
			{
				'id': 'BUNDLE-PARENT-NONSALE',
				'name': 'Parent Non Sales Bundle',
				'price': 75,
				'free_shipping': '0',
				'erpnext_item': 'ERP-NONSALE',
				'disabled': 0,
			},
			{
				'id': 'BUNDLE-EMPTY-GROUP',
				'name': 'Empty Group Bundle',
				'price': 60,
				'free_shipping': '0',
				'erpnext_item': 'ERP-EMPTY',
				'disabled': 0,
			},
		]

		bundle_groups = {
			'BUNDLE-VALID': [{'item_group': 'Hot Drinks', 'quantity': 1}],
			'BUNDLE-EMPTY-GROUP': [{'item_group': 'Pastries', 'quantity': 1}],
		}

		group_items = {
			'Hot Drinks': [
				{'id': 'ITEM-VALID', 'name': 'Valid Product', 'price': 35},
			],
			'Pastries': [],
		}

		with patch('jarz_pos.utils.validation_utils.assert_pos_profile_enabled') as mock_assert_profile, patch('jarz_pos.api.pos.frappe') as mock_frappe:
			def has_column_side_effect(doctype, column):
				return doctype == 'Jarz Bundle' and column == 'disabled'

			def get_all_side_effect(doctype, filters=None, fields=None, pluck=None, order_by=None, **kwargs):
				if doctype == 'Jarz Bundle':
					self.assertEqual(filters, {'disabled': 0})
					self.assertIn('erpnext_item', fields)
					return [
						{
							'id': row['id'],
							'name': row['name'],
							'price': row['price'],
							'free_shipping': row['free_shipping'],
							'erpnext_item': row['erpnext_item'],
						}
						for row in source_bundles
						if row['disabled'] == 0
					]

				if doctype == 'Item' and pluck == 'name':
					self.assertEqual(
						filters,
						{
							'name': ('in', ['ERP-VALID', 'ERP-DISABLED', 'ERP-NONSALE', 'ERP-EMPTY']),
							'disabled': 0,
							'is_sales_item': 1,
						},
					)
					return ['ERP-VALID', 'ERP-EMPTY']

				if doctype == 'Jarz Bundle Item Group':
					return bundle_groups.get(filters['parent'], [])

				if doctype == 'Item':
					self.assertEqual(filters['disabled'], 0)
					self.assertEqual(filters['is_sales_item'], 1)
					return group_items[filters['item_group']]

				raise AssertionError(f'Unexpected get_all call for {doctype}')

			def get_value_side_effect(doctype, name_or_filters, fieldname=None, *args, **kwargs):
				if doctype == 'POS Profile' and fieldname in ('selling_price_list', 'warehouse'):
					return None

				raise AssertionError(f'Unexpected get_value call for {doctype}')

			mock_frappe.db.has_column.side_effect = has_column_side_effect
			mock_frappe.get_all.side_effect = get_all_side_effect
			mock_frappe.db.get_value.side_effect = get_value_side_effect

			result = get_profile_bundles(profile='POS-1')

		mock_assert_profile.assert_called_once_with('POS-1')
		self.assertEqual([bundle['id'] for bundle in result], ['BUNDLE-VALID'])
		self.assertEqual(result[0]['free_shipping'], 1)
		self.assertNotIn('erpnext_item', result[0])
		self.assertEqual(
			result[0]['item_groups'],
			[
				{
					'group_name': 'Hot Drinks',
					'quantity': 1,
					'items': [
						{'id': 'ITEM-VALID', 'name': 'Valid Product', 'price': 35},
					],
				}
			],
		)