import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


class TestTripsNewFeatures(unittest.TestCase):

    @patch('jarz_pos.api.trips.frappe')
    def test_get_delivery_trips_returns_data(self, mock_frappe):
        from jarz_pos.api.trips import get_delivery_trips

        mock_frappe.get_all.return_value = [
            {
                'name': 'TRIP-1',
                'status': 'Created',
                'total_orders': 2,
                'total_amount': 200,
            }
        ]

        result = get_delivery_trips(status='Created', limit=10, offset=0)
        self.assertTrue(result['success'])
        self.assertEqual(result['count'], 1)
        self.assertEqual(result['data'][0]['name'], 'TRIP-1')

    @patch('jarz_pos.api.trips.frappe')
    def test_get_trip_details_maps_invoice_state(self, mock_frappe):
        from jarz_pos.api.trips import get_trip_details

        trip = MagicMock()
        trip.name = 'TRIP-2'
        trip.trip_date = '2026-03-24'
        trip.courier_party_type = 'Employee'
        trip.courier_party = 'EMP-1'
        trip.courier_display_name = 'Courier A'
        trip.status = 'Created'
        trip.is_double_shipping = 0
        trip.double_shipping_territory = None
        trip.total_orders = 1
        trip.total_amount = 80
        trip.total_shipping_expense = 20
        trip.notes = ''
        trip.invoices = [
            SimpleNamespace(
                invoice='SINV-1',
                customer_name='Customer A',
                territory='Main',
                sub_territory='Sub A',
                grand_total=80,
                shipping_expense=20,
                invoice_status='Ready',
            )
        ]

        mock_frappe.db.exists.return_value = True
        mock_frappe.get_doc.return_value = trip
        mock_frappe.db.get_value.return_value = 'Ready'

        result = get_trip_details('TRIP-2')
        self.assertTrue(result['success'])
        self.assertEqual(result['trip']['name'], 'TRIP-2')
        self.assertEqual(result['trip']['invoices'][0]['invoice'], 'SINV-1')

    @patch('jarz_pos.api.trips.frappe')
    def test_sync_trip_status_marks_completed(self, mock_frappe):
        from jarz_pos.api.trips import sync_trip_status

        trip = MagicMock()
        trip.name = 'TRIP-3'
        trip.status = 'Out for Delivery'
        trip.invoices = [
            SimpleNamespace(invoice='SINV-1', invoice_status='Out for Delivery'),
            SimpleNamespace(invoice='SINV-2', invoice_status='Out for Delivery'),
        ]

        # first call gets delivery trip link, then per-row state calls
        mock_frappe.db.get_value.side_effect = [
            'TRIP-3',
            'Delivered', 'Delivered',
            'Delivered', 'Delivered',
        ]
        mock_frappe.get_doc.return_value = trip

        sync_trip_status('SINV-1')

        self.assertEqual(trip.status, 'Completed')
        trip.save.assert_called_once()

    @patch('jarz_pos.api.trips.frappe')
    def test_sync_trip_status_no_trip_link_is_noop(self, mock_frappe):
        from jarz_pos.api.trips import sync_trip_status

        mock_frappe.db.get_value.return_value = None
        sync_trip_status('SINV-X')

        mock_frappe.get_doc.assert_not_called()

    @patch('jarz_pos.api.trips.frappe')
    def test_mark_trip_as_delivered_saves_invoice_to_fire_hooks(self, mock_frappe):
        from jarz_pos.api.trips import mark_trip_as_delivered

        trip = MagicMock()
        trip.name = 'TRIP-4'
        trip.status = 'Out for Delivery'
        trip.invoices = [
            SimpleNamespace(invoice='SINV-10', name='TRIPINV-10')
        ]

        invoice = MagicMock()
        invoice.flags = SimpleNamespace()
        invoice.get.side_effect = lambda field: {
            'custom_sales_invoice_state': 'Out for Delivery',
            'sales_invoice_state': 'Out for Delivery',
        }.get(field)

        mock_meta = MagicMock()
        mock_meta.get_field.side_effect = lambda name: MagicMock() if name in {'custom_sales_invoice_state', 'sales_invoice_state'} else None

        def fake_get_doc(doctype, name):
            if doctype == 'Delivery Trip':
                return trip
            if doctype == 'Sales Invoice':
                return invoice
            raise AssertionError(f'Unexpected doctype: {doctype}')

        mock_frappe.get_doc.side_effect = fake_get_doc
        mock_frappe.get_meta.return_value = mock_meta

        result = mark_trip_as_delivered('TRIP-4')

        self.assertTrue(result['success'])
        self.assertTrue(invoice.flags.ignore_validate_update_after_submit)
        invoice.save.assert_called_once_with(ignore_permissions=True, ignore_version=True)
        mock_frappe.db.set_value.assert_any_call(
            'Delivery Trip Invoice', 'TRIPINV-10', 'invoice_status', 'Delivered', update_modified=False
        )
        trip.save.assert_called_once_with(ignore_permissions=True)
