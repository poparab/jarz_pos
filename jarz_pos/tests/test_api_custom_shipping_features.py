import unittest
from unittest.mock import MagicMock, patch


class TestCustomShippingNewFeatures(unittest.TestCase):

    @patch('jarz_pos.api.custom_shipping.frappe')
    def test_is_manager_true_false(self, mock_frappe):
        from jarz_pos.api.custom_shipping import _is_manager

        mock_frappe.session.user = 'manager@example.com'
        mock_frappe.get_roles.return_value = ['JARZ Manager']
        self.assertTrue(_is_manager())

        mock_frappe.get_roles.return_value = ['Sales User']
        self.assertFalse(_is_manager())

    @patch('jarz_pos.api.custom_shipping.frappe')
    @patch('jarz_pos.api.custom_shipping._get_delivery_expense_amount')
    def test_request_custom_shipping_success(self, mock_get_exp, mock_frappe):
        from jarz_pos.api.custom_shipping import request_custom_shipping

        mock_frappe.session.user = 'sales@example.com'
        inv = MagicMock()
        inv.docstatus = 1
        mock_frappe.db.exists.side_effect = lambda doctype, _value: True
        mock_frappe.get_doc.return_value = inv
        mock_frappe.new_doc.return_value = MagicMock(name='CSR-1')
        mock_frappe.new_doc.return_value.name = 'CSR-1'
        mock_get_exp.return_value = 15

        result = request_custom_shipping(
            invoice_name='SINV-1',
            amount=35,
            reason='Customer is in a far area with increased shipping route cost',
        )

        self.assertTrue(result['success'])
        self.assertEqual(result['request'], 'CSR-1')
        self.assertEqual(result['original_amount'], 15)
        mock_frappe.db.set_value.assert_called()
        mock_frappe.db.commit.assert_called_once()

    @patch('jarz_pos.api.custom_shipping.frappe')
    def test_get_pending_custom_shipping_requests(self, mock_frappe):
        from jarz_pos.api.custom_shipping import get_pending_custom_shipping_requests

        mock_frappe.get_all.return_value = [
            {
                'name': 'CSR-1',
                'invoice': 'SINV-1',
                'requested_amount': 30,
                'status': 'Pending',
            }
        ]

        result = get_pending_custom_shipping_requests()
        self.assertTrue(result['success'])
        self.assertEqual(result['count'], 1)
        self.assertEqual(result['data'][0]['name'], 'CSR-1')

    @patch('jarz_pos.api.custom_shipping.frappe')
    def test_approve_custom_shipping_success(self, mock_frappe):
        from jarz_pos.api.custom_shipping import approve_custom_shipping

        csr = MagicMock()
        csr.docstatus = 0
        csr.status = 'Pending'
        csr.name = 'CSR-2'
        csr.invoice = 'SINV-2'
        csr.requested_amount = 50

        mock_frappe.get_doc.return_value = csr

        with patch('jarz_pos.api.custom_shipping._is_manager', return_value=True):
            result = approve_custom_shipping('CSR-2')

        self.assertTrue(result['success'])
        self.assertEqual(result['invoice'], 'SINV-2')
        csr.submit.assert_called_once()

    @patch('jarz_pos.api.custom_shipping.frappe')
    def test_reject_custom_shipping_draft(self, mock_frappe):
        from jarz_pos.api.custom_shipping import reject_custom_shipping

        csr = MagicMock()
        csr.docstatus = 0
        csr.name = 'CSR-3'
        csr.invoice = 'SINV-3'

        mock_frappe.get_doc.return_value = csr

        with patch('jarz_pos.api.custom_shipping._is_manager', return_value=True):
            result = reject_custom_shipping('CSR-3', rejection_reason='Not approved')

        self.assertTrue(result['success'])
        self.assertEqual(result['invoice'], 'SINV-3')
        csr.save.assert_called_once()
