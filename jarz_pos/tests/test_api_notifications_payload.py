import unittest
import os
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch


class TestNotificationPayloadContract(unittest.TestCase):

    def test_health_check_firebase_resolves_relative_path_from_bench_root(self):
        from jarz_pos.api import notifications

        site_root = os.path.normpath("/home/frappe/frappe-bench/sites/frontend")
        expected_path = os.path.normpath(
            "/home/frappe/frappe-bench/jarz-pos-firebase-adminsdk-fbsvc-5842596d09.json"
        )

        def fake_get_site_path(*parts):
            return os.path.join(site_root, *parts) if parts else site_root

        def fake_exists(path):
            return os.path.normpath(path) == expected_path

        local_stub = SimpleNamespace(
            conf={"fcm_service_account_path": "jarz-pos-firebase-adminsdk-fbsvc-5842596d09.json"}
        )

        with patch.object(notifications, "FIREBASE_AVAILABLE", True), patch.object(
            notifications.frappe, "local", local_stub, create=True
        ), patch.object(
            notifications.frappe, "get_site_path", side_effect=fake_get_site_path, create=True
        ), patch.object(
            notifications.os.path, "exists", side_effect=fake_exists
        ), patch.object(
            notifications.firebase_admin, "get_app", side_effect=ValueError("not initialized")
        ), patch.object(
            notifications, "_initialize_firebase_app", return_value=False
        ):
            result = notifications.health_check_firebase()

        self.assertFalse(result["ok"])
        self.assertEqual(result["raw_path"], "jarz-pos-firebase-adminsdk-fbsvc-5842596d09.json")
        self.assertEqual(result["resolved_path"], expected_path)
        self.assertEqual(result["path_source"], "bench_path")
        self.assertTrue(result["file_exists"])

    def test_health_check_warns_for_non_shared_absolute_service_account_path(self):
        from jarz_pos.api import notifications

        site_root = os.path.normpath("/home/frappe/frappe-bench/sites/frontend")
        configured_path = os.path.normpath(
            "/home/frappe/frappe-bench/jarz-pos-firebase-adminsdk-fbsvc-5842596d09.json"
        )

        def fake_get_site_path(*parts):
            return os.path.join(site_root, *parts) if parts else site_root

        def fake_exists(path):
            return os.path.normpath(path) == configured_path

        local_stub = SimpleNamespace(conf={"fcm_service_account_path": configured_path})
        notifications._FIREBASE_INIT_STATE["path_warning"] = None

        with patch.object(notifications, "FIREBASE_AVAILABLE", True), patch.object(
            notifications.frappe, "local", local_stub, create=True
        ), patch.object(
            notifications.frappe, "get_site_path", side_effect=fake_get_site_path, create=True
        ), patch.object(
            notifications.os.path, "exists", side_effect=fake_exists
        ), patch.object(
            notifications.firebase_admin, "get_app", return_value=object()
        ):
            result = notifications.health_check_firebase()

        self.assertTrue(result["ok"])
        self.assertEqual(result["path_source"], "configured_path")
        self.assertIn("outside the site private files", result["warning"])

    def test_initialize_firebase_app_uses_bench_root_fallback_for_relative_path(self):
        from jarz_pos.api import notifications

        site_root = os.path.normpath("/home/frappe/frappe-bench/sites/frontend")
        expected_path = os.path.normpath(
            "/home/frappe/frappe-bench/jarz-pos-firebase-adminsdk-fbsvc-5842596d09.json"
        )

        def fake_get_site_path(*parts):
            return os.path.join(site_root, *parts) if parts else site_root

        def fake_exists(path):
            return os.path.normpath(path) == expected_path

        local_stub = SimpleNamespace(
            conf={"fcm_service_account_path": "jarz-pos-firebase-adminsdk-fbsvc-5842596d09.json"}
        )
        fake_credentials = SimpleNamespace(Certificate=Mock(return_value="cred"))

        with patch.object(notifications, "FIREBASE_AVAILABLE", True), patch.object(
            notifications.frappe, "local", local_stub, create=True
        ), patch.object(
            notifications.frappe, "get_site_path", side_effect=fake_get_site_path, create=True
        ), patch.object(
            notifications.os.path, "exists", side_effect=fake_exists
        ), patch.object(
            notifications.firebase_admin, "get_app", side_effect=ValueError("not initialized")
        ), patch.object(
            notifications.firebase_admin, "initialize_app"
        ) as initialize_app, patch.object(
            notifications, "credentials", fake_credentials
        ), patch.object(
            notifications.frappe, "log_error"
        ):
            notifications._FIREBASE_INIT_STATE = {
                "failed_logged": False,
                "ok": False,
                "raw_path": None,
                "resolved_path": None,
                "path_source": None,
            }
            ok = notifications._initialize_firebase_app()

        self.assertTrue(ok)
        fake_credentials.Certificate.assert_called_once_with(expected_path)
        initialize_app.assert_called_once_with("cred")
        self.assertEqual(notifications._FIREBASE_INIT_STATE["resolved_path"], expected_path)
        self.assertEqual(notifications._FIREBASE_INIT_STATE["path_source"], "bench_path")

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

    def test_build_invoice_alert_payload_preserves_original_pos_profile_and_sets_effective_branch(self):
        from jarz_pos.api.notifications import _build_invoice_alert_payload

        invoice = SimpleNamespace(
            name="SINV-0099",
            pos_profile="Dokki",
            customer_name="",
            grand_total=75,
            net_total=60,
            outstanding_amount=0,
            custom_sales_invoice_state="Received",
            posting_date="2026-05-03",
            posting_time="10:10:00",
            custom_kanban_profile="Nasr City",
            custom_is_pickup=0,
            custom_delivery_date=None,
            custom_delivery_time_from=None,
            custom_acceptance_status="Pending",
            items=[SimpleNamespace(item_code="LATTE", item_name=None, qty=1)],
        )

        with patch("jarz_pos.api.notifications._ensure_acceptance_defaults"), patch(
            "jarz_pos.api.notifications.frappe.get_doc",
            return_value=invoice,
        ), patch(
            "jarz_pos.api.notifications.frappe.utils.now_datetime",
            return_value=datetime(2026, 5, 3, 10, 10, 0),
        ):
            payload = _build_invoice_alert_payload(invoice)

        self.assertEqual(payload["pos_profile"], "Dokki")
        self.assertEqual(payload["kanban_profile"], "Nasr City")
        self.assertEqual(payload["custom_kanban_profile"], "Nasr City")
        self.assertEqual(payload["effective_pos_profile"], "Nasr City")
        self.assertEqual(payload["branch_display"], "Nasr City")
        self.assertEqual(
            payload["body"],
            "Nasr City | Total: 75.00 | LATTE x 1",
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

    def test_prepare_invoice_data_payload_keeps_original_pos_profile_and_sends_effective_branch(self):
        from jarz_pos.api.notifications import _prepare_invoice_data_payload

        payload = {
            "invoice_id": "SINV-0200",
            "customer_name": "",
            "pos_profile": "Dokki",
            "kanban_profile": "Nasr City",
            "custom_kanban_profile": "Nasr City",
            "effective_pos_profile": "Nasr City",
            "grand_total": 50,
            "sales_invoice_state": "Received",
            "timestamp": "2026-05-03T10:05:00",
            "requires_acceptance": True,
            "item_summary": "",
            "items": [{"item_name": "Mocha", "qty": 1}],
        }

        data = _prepare_invoice_data_payload("new_invoice", payload)

        self.assertEqual(data["pos_profile"], "Dokki")
        self.assertEqual(data["kanban_profile"], "Nasr City")
        self.assertEqual(data["custom_kanban_profile"], "Nasr City")
        self.assertEqual(data["effective_pos_profile"], "Nasr City")
        self.assertEqual(data["branch_display"], "Nasr City")
        self.assertEqual(data["body"], "Nasr City | Total: 50.00 | Mocha x 1")

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

    def test_prepare_invoice_status_data_payload_for_accepted_includes_customer_territory_and_pos(self):
        from jarz_pos.api.notifications import _prepare_invoice_status_data_payload

        data = _prepare_invoice_status_data_payload(
            "invoice_accepted",
            {
                "invoice_id": "SINV-0004",
                "customer_name": "Ahmed Ali",
                "territory": "Cairo",
                "pos_profile": "Nasr City",
                "accepted_by": "manager@example.com",
                "accepted_on": "2026-05-03T10:06:00",
                "sales_invoice_state": "Accepted",
                "timestamp": "2026-05-03T10:06:00",
            },
        )

        self.assertEqual(data["customer_name"], "Ahmed Ali")
        self.assertEqual(data["territory"], "Cairo")
        self.assertEqual(data["pos_profile"], "Nasr City")
        self.assertEqual(data["title"], "Order Accepted: Ahmed Ali")
        self.assertEqual(
            data["body"],
            "Territory: Cairo | POS Profile: Nasr City | By: manager@example.com",
        )

    def test_prepare_invoice_status_data_payload_uses_effective_branch_but_keeps_original_pos(self):
        from jarz_pos.api.notifications import _prepare_invoice_status_data_payload

        data = _prepare_invoice_status_data_payload(
            "invoice_accepted",
            {
                "invoice_id": "SINV-0004",
                "customer_name": "Ahmed Ali",
                "territory": "Cairo",
                "pos_profile": "Dokki",
                "kanban_profile": "Nasr City",
                "custom_kanban_profile": "Nasr City",
                "effective_pos_profile": "Nasr City",
                "accepted_by": "manager@example.com",
                "accepted_on": "2026-05-03T10:06:00",
                "sales_invoice_state": "Accepted",
                "timestamp": "2026-05-03T10:06:00",
            },
        )

        self.assertEqual(data["pos_profile"], "Dokki")
        self.assertEqual(data["kanban_profile"], "Nasr City")
        self.assertEqual(data["custom_kanban_profile"], "Nasr City")
        self.assertEqual(data["effective_pos_profile"], "Nasr City")
        self.assertEqual(
            data["body"],
            "Territory: Cairo | POS Profile: Nasr City | By: manager@example.com",
        )

    def test_prepare_invoice_status_data_payload_for_cancelled_keeps_reason_with_context(self):
        from jarz_pos.api.notifications import _prepare_invoice_status_data_payload

        data = _prepare_invoice_status_data_payload(
            "invoice_cancelled",
            {
                "invoice_id": "SINV-0005",
                "customer_name": "Mona Hassan",
                "territory": "Alexandria",
                "pos_profile": "Smouha",
                "reason": "Customer requested cancellation",
                "sales_invoice_state": "Cancelled",
                "timestamp": "2026-05-03T10:07:00",
            },
        )

        self.assertEqual(data["customer_name"], "Mona Hassan")
        self.assertEqual(data["territory"], "Alexandria")
        self.assertEqual(data["pos_profile"], "Smouha")
        self.assertEqual(data["title"], "Order Cancelled: Mona Hassan")
        self.assertEqual(
            data["body"],
            "Territory: Alexandria | POS Profile: Smouha | Reason: Customer requested cancellation",
        )

    def test_prepare_invoice_status_data_payload_overrides_inherited_new_order_title_and_body(self):
        from jarz_pos.api.notifications import _prepare_invoice_status_data_payload

        data = _prepare_invoice_status_data_payload(
            "invoice_cancelled",
            {
                "invoice_id": "SINV-0006",
                "customer_name": "Sara Nabil",
                "territory": "Giza",
                "pos_profile": "Dokki",
                "reason": "Customer requested cancellation",
                "title": "New Order: Sara Nabil",
                "body": "Dokki | Total: 100.00 | Latte x 1",
                "sales_invoice_state": "Cancelled",
                "timestamp": "2026-05-03T10:08:00",
            },
        )

        self.assertEqual(data["title"], "Order Cancelled: Sara Nabil")
        self.assertEqual(
            data["body"],
            "Territory: Giza | POS Profile: Dokki | Reason: Customer requested cancellation",
        )

    def test_resolve_recipients_for_payload_uses_effective_branch_only(self):
        from jarz_pos.api import notifications

        payload = {
            "pos_profile": "Dokki",
            "kanban_profile": "Nasr City",
            "custom_kanban_profile": "Nasr City",
            "effective_pos_profile": "Nasr City",
        }

        with patch.object(notifications, "_get_users_for_pos_profiles", return_value=["branchb@example.com"]) as users_mock:
            recipients = notifications._resolve_recipients_for_payload(payload)

        users_mock.assert_called_once_with(["Nasr City"])
        self.assertEqual(recipients, ["branchb@example.com"])

    def test_get_pending_alert_rows_for_profiles_excludes_transferred_rows_from_old_branch(self):
        from jarz_pos.api import notifications

        with patch.object(
            notifications.frappe,
            "get_all",
            side_effect=[
                [],
                [
                    {
                        "name": "SINV-TRANSFERRED",
                        "creation": "2026-05-03 10:00:00",
                        "custom_kanban_profile": "Nasr City",
                    },
                    {
                        "name": "SINV-LEGACY",
                        "creation": "2026-05-03 10:01:00",
                        "custom_kanban_profile": None,
                    },
                ],
            ],
        ):
            rows = notifications._get_pending_alert_rows_for_profiles(
                ["Dokki"],
                "2026-05-03 09:00:00",
            )

        self.assertEqual([row["name"] for row in rows], ["SINV-LEGACY"])

    def test_get_pending_alert_rows_for_profiles_includes_transferred_rows_for_new_branch(self):
        from jarz_pos.api import notifications

        with patch.object(
            notifications.frappe,
            "get_all",
            side_effect=[
                [{"name": "SINV-TRANSFERRED", "creation": "2026-05-03 10:00:00"}],
                [],
            ],
        ):
            rows = notifications._get_pending_alert_rows_for_profiles(
                ["Nasr City"],
                "2026-05-03 09:00:00",
            )

        self.assertEqual([row["name"] for row in rows], ["SINV-TRANSFERRED"])

    def test_ensure_user_can_accept_checks_effective_branch_only(self):
        from jarz_pos.api import notifications

        doc = SimpleNamespace(pos_profile="Dokki", custom_kanban_profile="Nasr City")

        with patch.object(
            notifications,
            "_get_users_for_pos_profiles",
            return_value=["branchb@example.com"],
        ) as users_mock:
            notifications._ensure_user_can_accept(doc, "branchb@example.com")

        users_mock.assert_called_once_with(["Nasr City"])

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
            result = notifications._send_fcm_notifications(["token-1"], data_payload)

        message_kwargs = fake_messaging.Message.call_args.kwargs
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "sent")
        self.assertEqual(result["success_count"], 1)
        self.assertEqual(message_kwargs["data"], data_payload)
        self.assertEqual(message_kwargs["android"].priority, "high")
        self.assertIn("notification", message_kwargs)
        self.assertEqual(message_kwargs["notification"].title, "New Order: Walk-in")
        self.assertEqual(message_kwargs["notification"].body, "Nasr City | Total: 100.00 | Latte x 1")
        self.assertEqual(message_kwargs["android"].notification.channel_id, "jarz_order_alerts")
        self.assertEqual(
            message_kwargs["android"].notification.sound,
            notifications.ANDROID_ORDER_ALERT_SOUND,
        )
        self.assertEqual(message_kwargs["android"].notification.tag, "SINV-0003")
        fake_messaging.Notification.assert_called_once_with(
            title="New Order: Walk-in",
            body="Nasr City | Total: 100.00 | Latte x 1",
        )
        fake_messaging.AndroidNotification.assert_called_once_with(
            sound=notifications.ANDROID_ORDER_ALERT_SOUND,
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
            result = notifications._send_fcm_notifications(["token-2"], data_payload)

        message_kwargs = fake_messaging.Message.call_args.kwargs
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "sent")
        self.assertEqual(result["success_count"], 1)
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

    def test_send_fcm_notifications_adds_webpush_config_when_supported(self):
        from jarz_pos.api import notifications

        fake_messaging = SimpleNamespace(
            Message=Mock(side_effect=lambda **kwargs: SimpleNamespace(token=kwargs["token"], kwargs=kwargs)),
            AndroidConfig=Mock(side_effect=lambda **kwargs: SimpleNamespace(**kwargs)),
            AndroidNotification=Mock(side_effect=lambda **kwargs: SimpleNamespace(**kwargs)),
            Notification=Mock(side_effect=lambda **kwargs: SimpleNamespace(**kwargs)),
            WebpushNotification=Mock(side_effect=lambda **kwargs: SimpleNamespace(**kwargs)),
            WebpushConfig=Mock(side_effect=lambda **kwargs: SimpleNamespace(**kwargs)),
            WebpushFCMOptions=Mock(side_effect=lambda **kwargs: SimpleNamespace(**kwargs)),
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
        ), patch.object(
            notifications.frappe.utils, "get_url", return_value="https://erpstg.orderjarz.com", create=True
        ):
            result = notifications._send_fcm_notifications(["token-1"], data_payload)

        message_kwargs = fake_messaging.Message.call_args.kwargs
        self.assertTrue(result["ok"])
        self.assertEqual(message_kwargs["webpush"].headers, {"Urgency": "high"})
        self.assertEqual(message_kwargs["webpush"].notification.tag, "SINV-0003")
        self.assertTrue(message_kwargs["webpush"].notification.require_interaction)
        self.assertEqual(message_kwargs["webpush"].fcm_options.link, "https://erpstg.orderjarz.com")
        fake_messaging.WebpushNotification.assert_called_once_with(
            title="New Order: Walk-in",
            body="Nasr City | Total: 100.00 | Latte x 1",
            icon=notifications.WEBPUSH_NOTIFICATION_ICON,
            badge=notifications.WEBPUSH_NOTIFICATION_BADGE,
            tag="SINV-0003",
            require_interaction=True,
        )
        fake_messaging.WebpushFCMOptions.assert_called_once_with(
            link="https://erpstg.orderjarz.com"
        )
        fake_messaging.WebpushConfig.assert_called_once_with(
            headers={"Urgency": "high"},
            notification=message_kwargs["webpush"].notification,
            fcm_options=message_kwargs["webpush"].fcm_options,
        )