import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


class TestTerritoriesNewFeatures(unittest.TestCase):

    @patch('jarz_pos.api.territories.frappe')
    def test_get_sub_territories_success(self, mock_frappe):
        from jarz_pos.api.territories import get_sub_territories

        mock_frappe.db.exists.return_value = True
        mock_frappe.get_all.return_value = [
            SimpleNamespace(
                name='T-SUB-1',
                territory_name='Sub Territory 1',
                delivery_income=30,
                delivery_expense=20,
            )
        ]

        result = get_sub_territories('Main Territory')
        self.assertTrue(result['success'])
        self.assertEqual(len(result['data']), 1)
        self.assertEqual(result['data'][0]['name'], 'T-SUB-1')
        self.assertEqual(result['data'][0]['delivery_expense'], 20.0)

    @patch('jarz_pos.api.territories.frappe')
    def test_get_sub_territories_not_found(self, mock_frappe):
        from jarz_pos.api.territories import get_sub_territories

        mock_frappe.db.exists.return_value = False

        result = get_sub_territories('Unknown')
        self.assertFalse(result['success'])
        self.assertIn('not found', result['message'])

    @patch('jarz_pos.api.territories.frappe')
    def test_set_invoice_sub_territory_sets_field(self, mock_frappe):
        from jarz_pos.api.territories import set_invoice_sub_territory

        inv = MagicMock()
        inv.docstatus = 1
        inv.territory = 'Main Territory'

        def exists_side_effect(doctype, value):
            if doctype == 'Sales Invoice':
                return True
            if doctype == 'Territory':
                return True
            return False

        mock_frappe.db.exists.side_effect = exists_side_effect
        mock_frappe.get_doc.return_value = inv
        mock_frappe.db.get_value.side_effect = [
            'Main Territory',  # parent_territory for selected sub territory
            20,                # delivery_expense
            30,                # delivery_income
        ]

        result = set_invoice_sub_territory('SINV-1', 'Sub Territory 1')

        self.assertTrue(result['success'])
        self.assertEqual(result['sub_territory'], 'Sub Territory 1')
        self.assertEqual(result['delivery_expense'], 20.0)
        mock_frappe.db.set_value.assert_called_once()
        mock_frappe.db.commit.assert_called_once()

    @patch('jarz_pos.api.territories.frappe')
    def test_territory_has_children(self, mock_frappe):
        from jarz_pos.api.territories import territory_has_children

        mock_frappe.db.exists.return_value = 'TERR-CHILD'
        self.assertTrue(territory_has_children('Main'))

        mock_frappe.db.exists.return_value = None
        self.assertFalse(territory_has_children('Main'))
