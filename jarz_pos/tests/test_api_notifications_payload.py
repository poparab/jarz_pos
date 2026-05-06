import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch


class TestNotificationPayloadContract(unittest.TestCase):

    def test_build_invoice_alert_payload_adds_display_safe_fields(self):
        from jarz_pos.api.notifications import _build_invoice_alert_payload

        invoice = SimpleNamespace(
            name="SINV-0001",
            pos_profile="Nasr City",
            customer_name="",
            grand_total=125,
            net_total=100,
            outstanding_amount=0,
            custom_sales_invoice_state="Received",
            posting_date="2026-05-03",
            posting_time="10:00:00",
            custom_kanban_profile=None,
            custom_is_pickup=0,
            custom_delivery_date=None,
            custom_delivery_time_from=None,
            custom_acceptance_status="Pending",
            items=[
                SimpleNamespace(item_code="", item_name="", qty=2),
                SimpleNamespace(item_code="LATTE", item_name=None, qty=1),
            ],
        )

        with patch("jarz_pos.api.notifications._ensure_acceptance_defaults"), patch(
            "jarz_pos.api.notifications.frappe.get_doc",
            return_value=invoice,
        ), patch(
            "jarz_pos.api.notifications.frappe.utils.now_datetime",
            return_value=datetime(2026, 5, 3, 10, 0, 0),
        ):
            payload = _build_invoice_alert_payload(invoice)

        self.assertEqual(payload["customer_name"], "Walk-in")
        self.assertEqual(payload["branch_display"], "Nasr City")
        self.assertEqual(payload["total_display"], "125.00")
        self.assertEqual(payload["item_count"], 2)
        self.assertEqual(payload["item_summary"], "Item x 2, LATTE x 1")
        self.assertEqual(payload["title"], "New Order: Walk-in")
        self.assertEqual(
            payload["body"],
            "Nasr City | Total: 125.00 | Item x 2, LATTE x 1",
        )

    def test_prepare_invoice_data_payload_includes_android_display_contract(self):
        from jarz_pos.api.notifications import _prepare_invoice_data_payload

        payload = {
            "invoice_id": "SINV-0002",
            "customer_name": "",
            "pos_profile": "Heliopolis",
            "grand_total": 50,
            "sales_invoice_state": "Received",
            "timestamp": "2026-05-03T10:05:00",
            "requires_acceptance": True,
            "item_summary": "",
            "items": [{"item_name": "Mocha", "qty": 1}],
        }

        data = _prepare_invoice_data_payload("new_invoice", payload)

        self.assertEqual(data["invoice_id"], "SINV-0002")
        self.assertEqual(data["customer_name"], "Walk-in")
        self.assertEqual(data["pos_profile"], "Heliopolis")
        self.assertEqual(data["branch_display"], "Heliopolis")
        self.assertEqual(data["total_display"], "50.00")
        self.assertEqual(data["item_count"], "1")
        self.assertEqual(data["item_summary"], "Mocha x 1")
        self.assertEqual(data["title"], "New Order: Walk-in")
        self.assertEqual(data["body"], "Heliopolis | Total: 50.00 | Mocha x 1")
        self.assertIn("grand_total", data)
        self.assertIn("items", data)

    def test_resolve_notification_content_falls_back_when_title_and_body_are_blank(self):
        from jarz_pos.api.notifications import _resolve_notification_content

        title, body = _resolve_notification_content(
            {
                "type": "new_invoice",
                "customer_name": "",
                "pos_profile": "Maadi",
                "grand_total": "80",
                "item_count": "2",
                "title": "   ",
                "body": "",
            }
        )

        self.assertEqual(title, "New Order: Walk-in")
        self.assertEqual(body, "Maadi | Total: 80.00 | 2 items")

    def test_send_fcm_notifications_sends_new_invoice_with_notification_and_data(self):
        from jarz_pos.api import notifications

        fake_messaging = SimpleNamespace(
            Message=Mock(side_effect=lambda **kwargs: SimpleNamespace(token=kwargs["token"], kwargs=kwargs)),
            AndroidConfig=Mock(side_effect=lambda **kwargs: SimpleNamespace(**kwargs)),
            AndroidNotification=Mock(side_effect=lambda **kwargs: SimpleNamespace(**kwargs)),
            Notification=Mock(side_effect=lambda **kwargs: SimpleNamespace(**kwargs)),
            send=Mock(return_value="message-id"),
        )
        logger = SimpleNamespace(info=Mock())
        data_payload = {
            "type": "new_invoice",
            "invoice_id": "SINV-0003",
            "title": "New Order: Walk-in",
            "body": "Nasr City | Total: 100.00 | Latte x 1",
        }

        with patch.object(notifications, "_initialize_firebase_app", return_value=True), patch.object(
            notifications, "messaging", fake_messaging, create=True
        ), patch.object(notifications.frappe, "logger", return_value=logger), patch.object(
            notifications.frappe, "log_error"
        ):
            notifications._send_fcm_notifications(["token-1"], data_payload)

        message_kwargs = fake_messaging.Message.call_args.kwargs
        self.assertEqual(message_kwargs["data"], data_payload)
        self.assertEqual(message_kwargs["android"].priority, "high")
        self.assertIn("notification", message_kwargs)
        self.assertEqual(message_kwargs["notification"].title, "New Order: Walk-in")
        self.assertEqual(message_kwargs["notification"].body, "Nasr City | Total: 100.00 | Latte x 1")
        self.assertEqual(message_kwargs["android"].notification.channel_id, "jarz_order_alerts")
        self.assertEqual(message_kwargs["android"].notification.sound, "default")
        self.assertEqual(message_kwargs["android"].notification.tag, "SINV-0003")
        fake_messaging.Notification.assert_called_once_with(
            title="New Order: Walk-in",
            body="Nasr City | Total: 100.00 | Latte x 1",
        )
        fake_messaging.AndroidNotification.assert_called_once_with(
            sound='default',
            channel_id='jarz_order_alerts',
            tag='SINV-0003',
        )
        fake_messaging.send.assert_called_once()

    def test_send_fcm_notifications_keeps_notification_payload_for_non_order_types(self):
        from jarz_pos.api import notifications

        fake_messaging = SimpleNamespace(
            Message=Mock(side_effect=lambda **kwargs: SimpleNamespace(token=kwargs["token"], kwargs=kwargs)),
            AndroidConfig=Mock(side_effect=lambda **kwargs: SimpleNamespace(**kwargs)),
            AndroidNotification=Mock(side_effect=lambda **kwargs: SimpleNamespace(**kwargs)),
            Notification=Mock(side_effect=lambda **kwargs: SimpleNamespace(**kwargs)),
            send=Mock(return_value="message-id"),
        )
        logger = SimpleNamespace(info=Mock())
        data_payload = {
            "type": "shift_started",
            "invoice_id": "",
        }

        with patch.object(notifications, "_initialize_firebase_app", return_value=True), patch.object(
            notifications, "messaging", fake_messaging, create=True
        ), patch.object(notifications, "_resolve_notification_content", return_value=("Shift Started", "Open shift")), patch.object(
            notifications.frappe, "logger", return_value=logger
        ), patch.object(notifications.frappe, "log_error"):
            notifications._send_fcm_notifications(["token-2"], data_payload)

        message_kwargs = fake_messaging.Message.call_args.kwargs
        self.assertIn("notification", message_kwargs)
        self.assertEqual(message_kwargs["notification"].title, "Shift Started")
        self.assertEqual(message_kwargs["notification"].body, "Open shift")
        self.assertEqual(message_kwargs["android"].priority, "high")
        self.assertEqual(message_kwargs["android"].notification.channel_id, "jarz_shift_updates")
        fake_messaging.Notification.assert_called_once_with(title="Shift Started", body="Open shift")
        fake_messaging.AndroidNotification.assert_called_once_with(
            sound='default',
            channel_id='jarz_shift_updates',
            tag='',
        )
        fake_messaging.send.assert_called_once()