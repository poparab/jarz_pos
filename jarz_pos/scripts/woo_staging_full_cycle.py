"""Staging-only WooCommerce full-cycle verification runner.

Run from the staging backend container with:
    bench --site frontend execute jarz_pos.scripts.woo_staging_full_cycle.run_json --kwargs '{"environment":"staging"}'

The runner is safe by default. It records preflight and fixture-discovery
evidence without mutating WooCommerce or ERPNext unless explicitly enabled.
"""

from __future__ import annotations

import json
import re
import time
import traceback
from datetime import datetime, timedelta
from typing import Any

import frappe
from frappe.utils import now_datetime
from frappe.utils.password import get_decrypted_password


FLAGS = [
    "enable_inbound_orders",
    "enable_inbound_amendment",
    "enable_outbound_customers",
    "enable_outbound_orders",
    "enable_sync_event_ledger",
    "sync_event_shadow_mode",
    "use_outbox_for_customer_push",
    "use_outbox_for_invoice_push",
    "use_inbox_for_order_webhook",
    "use_inbox_for_customer_webhook",
    "use_inbox_for_order_polling",
    "use_event_reconciliation",
    "sync_event_worker_enabled",
    "sync_event_max_attempts",
    "sync_event_batch_size",
    "sync_event_success_retention_days",
    "sync_event_lock_ttl_seconds",
    "sync_event_shadow_alert_threshold",
    "sync_event_circuit_breaker_threshold",
    "sync_event_circuit_breaker_window_seconds",
    "sync_event_circuit_breaker_cooldown_seconds",
]

REPORT_MARKER_START = "WOO_STAGING_FULL_CYCLE_JSON_START"
REPORT_MARKER_END = "WOO_STAGING_FULL_CYCLE_JSON_END"


class FullCycleRunner:
    def __init__(
        self,
        *,
        environment: str = "staging",
        allow_staging_mutations: bool = False,
        run_id: str | None = None,
    ) -> None:
        self.environment = (environment or "").strip().lower()
        self.allow_staging_mutations = bool(allow_staging_mutations)
        self.run_id = run_id or f"COPILOT-STG-{now_datetime().strftime('%Y%m%d-%H%M%S')}"
        self.started_on = now_datetime()
        self.fixture_catalog: dict[str, Any] = {}
        self.runtime_state: dict[str, Any] = {}
        self._woo_client_cached = None
        self.report: dict[str, Any] = {
            "run_id": self.run_id,
            "environment": self.environment,
            "allow_staging_mutations": self.allow_staging_mutations,
            "started_on": self.started_on.isoformat(),
            "site": getattr(frappe.local, "site", None),
            "cases": [],
            "assertions": [],
            "created_records": [],
            "concerns": [],
            "errors": [],
        }

    def run(self) -> dict[str, Any]:
        try:
            self._guard_environment()
            self._case("PF-01", "Preflight", self._preflight)
            self._case("PF-02", "Dynamic fixture discovery", self._discover_fixtures)
            self._case("REL-01", "Webhook ACK and invalid signature", self._webhook_reliability_checks)
            if self.allow_staging_mutations:
                self._case("WI-CUST-01", "Woo customer inbound create", self._inbound_customer_create)
                self._case("WI-CUST-02", "Woo customer inbound update", self._inbound_customer_update)
                self._case("WI-ADDR-01", "Woo customer inbound address update", self._inbound_customer_address_update)
                self._case("REL-02", "Customer webhook replay is idempotent", self._customer_webhook_replay)
                self._case("WI-ORD-01", "Woo order inbound create", self._inbound_order_create)
                self._case("WI-ORD-02", "Woo order inbound replay is idempotent", self._inbound_order_replay)
                self._case("WI-ORD-03", "Woo order inbound item edit amends submitted invoice", self._inbound_order_update_amendment)
                self._case("WI-ORD-04", "Woo order inbound customer detail edit amends submitted invoice", self._inbound_order_customer_detail_amendment)
                self._case("WI-ORD-05", "Woo order inbound terminal status update is held for review", self._inbound_order_status_manual_review)
                self._case("WI-ORD-06", "Woo order inbound cancellation is held for review", self._inbound_order_cancel_manual_review)
                self._case("EO-CUST-01", "ERP customer creation outbound", self._outbound_customer_create)
                self._case("EO-ADDR-01", "ERP customer address outbound", self._outbound_customer_address_update)
                self._case("X-CUST-01", "Customer round trip preserves linkage across ERP and Woo", self._cross_customer_round_trip)
                self._case("EO-ORD-01", "ERP POS invoice outbound", self._outbound_order_create)
                self._case("X-ORD-01", "Order round trip amends ERP-originated invoice from Woo edit", self._cross_order_round_trip)
                self._case("EO-PAY-01", "ERP invoice payment outbound", self._outbound_payment)
                self._case("EO-AMEND-01", "ERP invoice amendment outbound", self._outbound_amendment)
                self._case("EO-STATE-01", "ERP invoice state outbound", self._outbound_state_transition)
                self._case("EO-CANCEL-01", "ERP invoice cancellation outbound", self._outbound_cancel)
        except Exception as exc:  # noqa: BLE001
            self.report["errors"].append({
                "error": str(exc),
                "traceback": traceback.format_exc(limit=12),
            })
        finally:
            self._finish_report()
        return self.report

    def _guard_environment(self) -> None:
        if self.environment != "staging":
            raise RuntimeError("This runner only supports environment='staging'.")
        if self.allow_staging_mutations and self.environment != "staging":
            raise RuntimeError("Mutation mode is only allowed on staging.")

    def _case(self, case_id: str, title: str, fn) -> None:
        started = now_datetime()
        case = {
            "case_id": case_id,
            "title": title,
            "status": "Running",
            "started_on": started.isoformat(),
            "assertions": [],
            "evidence": {},
        }
        self.report["cases"].append(case)
        try:
            evidence = fn(case)
            case["evidence"] = _json_safe(evidence or {})
            failing = [item for item in case["assertions"] if item.get("status") == "Fail"]
            concerns = [item for item in case["assertions"] if item.get("status") == "Concern"]
            if failing:
                case["status"] = "Fail"
            elif concerns:
                case["status"] = "Concern"
            else:
                case["status"] = "Pass"
        except Exception as exc:  # noqa: BLE001
            case["status"] = "Fail"
            case["error"] = str(exc)
            case["traceback"] = traceback.format_exc(limit=12)
            self.report["errors"].append({
                "case_id": case_id,
                "error": str(exc),
                "traceback": case["traceback"],
            })
        finally:
            ended = now_datetime()
            case["ended_on"] = ended.isoformat()
            case["duration_seconds"] = round((ended - started).total_seconds(), 3)

    def _assert(
        self,
        case: dict[str, Any],
        assertion_id: str,
        description: str,
        passed: bool,
        *,
        expected: Any = None,
        actual: Any = None,
        concern: bool = False,
    ) -> bool:
        status = "Pass" if passed else ("Concern" if concern else "Fail")
        row = {
            "case_id": case["case_id"],
            "assertion_id": assertion_id,
            "description": description,
            "status": status,
            "expected": _json_safe(expected),
            "actual": _json_safe(actual),
        }
        case["assertions"].append(row)
        self.report["assertions"].append(row)
        if status == "Concern":
            self.report["concerns"].append(row)
        return passed

    def _preflight(self, case: dict[str, Any]) -> dict[str, Any]:
        from jarz_woocommerce_integration.api.settings import test_saved_connection
        from jarz_woocommerce_integration.doctype.woocommerce_settings.woocommerce_settings import (
            WooCommerceSettings,
        )

        settings = WooCommerceSettings.get_settings()
        base_url = str(getattr(settings, "base_url", "") or "").rstrip("/")
        host_name = str(getattr(frappe.conf, "host_name", "") or "").rstrip("/")
        if not host_name:
            try:
                host_name = str((frappe.get_site_config() or {}).get("host_name", "") or "").rstrip("/")
            except Exception:  # noqa: BLE001
                host_name = ""

        connection_result: dict[str, Any]
        try:
            connection_result = test_saved_connection()
        except Exception as exc:  # noqa: BLE001
            connection_result = {"success": False, "error": str(exc)}

        db_version = frappe.db.sql("SELECT VERSION() AS version", as_dict=True)[0].version
        event_tables_exist = all(
            frappe.db.table_exists(doctype)
            for doctype in ("WooCommerce Sync Event", "WooCommerce Sync Log", "WooCommerce Order Map")
        )
        webhook_secret_present = bool(_get_decrypted_single_password(settings.name, "webhook_secret"))
        consumer_secret_present = bool(_get_decrypted_single_password(settings.name, "consumer_secret"))

        self._assert(case, "PF-01.01", "Frappe site is frontend", frappe.local.site == "frontend", expected="frontend", actual=frappe.local.site)
        self._assert(case, "PF-01.02", "Host name points to staging", "erpstg" in host_name, expected="contains erpstg", actual=host_name)
        self._assert(case, "PF-01.03", "Woo base URL is configured", bool(base_url), expected="non-empty", actual=base_url)
        self._assert(case, "PF-01.04", "Woo saved connection succeeds", bool(connection_result.get("success")), expected=True, actual=connection_result)
        self._assert(case, "PF-01.05", "Woo consumer secret is present", consumer_secret_present, expected=True, actual=consumer_secret_present)
        self._assert(case, "PF-01.06", "Woo webhook secret is present", webhook_secret_present, expected=True, actual=webhook_secret_present)
        self._assert(case, "PF-01.07", "Sync ledger DocTypes exist", event_tables_exist, expected=True, actual=event_tables_exist)
        self._assert(case, "PF-01.08", "Database version supports modern MariaDB checks", _mariadb_version_at_least(db_version, 10, 6), expected=">=10.6", actual=db_version, concern=True)

        flags = _snapshot_flags(settings)
        counters = _health_counters(self.started_on)
        severe_queue_blockers = (
            int(counters.get("dead_letter_events_since_start") or 0)
            + int(counters.get("failed_events_since_start") or 0)
        )
        self._assert(case, "PF-01.09", "No severe event failures created during this run", severe_queue_blockers == 0, expected=0, actual=severe_queue_blockers)

        return {
            "host_name": host_name,
            "woo_base_url": base_url,
            "db_version": db_version,
            "flags": flags,
            "health_counters": counters,
            "email_safety_note": "WooCommerce customer/order emails are assumed disabled in WP admin; runner still uses synthetic local-domain data.",
        }

    def _discover_fixtures(self, case: dict[str, Any]) -> dict[str, Any]:
        territories = frappe.db.sql(
            """
            SELECT t.name AS territory, t.territory_name, t.pos_profile,
                   p.warehouse, p.selling_price_list AS price_list
            FROM `tabTerritory` t
            INNER JOIN `tabPOS Profile` p ON p.name = t.pos_profile
            WHERE IFNULL(t.is_group, 0) = 0
              AND IFNULL(t.pos_profile, '') != ''
              AND IFNULL(p.warehouse, '') != ''
              AND IFNULL(p.selling_price_list, '') != ''
            ORDER BY t.modified DESC
            LIMIT 10
            """,
            as_dict=True,
        )
        price_lists = sorted({row.price_list for row in territories if row.get("price_list")})
        items = []
        if price_lists:
            items = frappe.db.sql(
                """
                SELECT i.name AS item_code, i.item_name, i.woo_product_id, i.woo_variation_id,
                       ip.price_list, ip.price_list_rate
                FROM `tabItem` i
                INNER JOIN `tabItem Price` ip ON ip.item_code = i.name
                WHERE IFNULL(i.disabled, 0) = 0
                  AND IFNULL(i.woo_product_id, '') != ''
                  AND ip.selling = 1
                  AND ip.price_list IN %(price_lists)s
                  AND IFNULL(ip.price_list_rate, 0) > 0
                ORDER BY i.modified DESC
                LIMIT 20
                """,
                {"price_lists": tuple(price_lists)},
                as_dict=True,
            )

        payment_modes = frappe.db.sql(
            """
            SELECT name
            FROM `tabMode of Payment`
            WHERE IFNULL(enabled, 1) = 1
              AND name IN ('Cash', 'Instapay', 'Mobile Wallet', 'Kashier Card', 'Kashier Wallet')
            ORDER BY name
            """,
            as_dict=True,
        )
        company = frappe.defaults.get_global_default("company")

        self._assert(case, "PF-02.01", "At least two territory/POS fixtures are available", len(territories) >= 2, expected=">=2", actual=len(territories))
        self._assert(case, "PF-02.02", "At least two Woo-mapped item fixtures are available", len(items) >= 2, expected=">=2", actual=len(items))
        self._assert(case, "PF-02.03", "Company default is configured", bool(company), expected="non-empty", actual=company)
        self._assert(case, "PF-02.04", "At least one expected payment mode is enabled", bool(payment_modes), expected="non-empty", actual=[row.name for row in payment_modes])

        evidence = {
            "territories": territories[:5],
            "items": items[:8],
            "payment_modes": [row.name for row in payment_modes],
            "company": company,
            "next_delivery_slot": _next_delivery_slot(),
        }
        self.fixture_catalog = evidence
        return evidence

    def _webhook_reliability_checks(self, case: dict[str, Any]) -> dict[str, Any]:
        import requests

        host_name = self._host_name()
        order_webhook_url = (
            f"{host_name}/api/method/"
            "jarz_woocommerce_integration.api.orders.woo_order_webhook?d=1"
        )

        ack_response = requests.post(
            order_webhook_url,
            data=b"",
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        invalid_response = requests.post(
            order_webhook_url,
            data=json.dumps({"id": 999999999, "status": "processing"}).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-WC-Webhook-Signature": "invalid",
            },
            timeout=30,
        )

        ack_body = _safe_json_body(ack_response)
        invalid_body = _safe_json_body(invalid_response)
        ack_payload = ack_body.get("message") if isinstance(ack_body.get("message"), dict) else ack_body
        invalid_payload = invalid_body.get("message") if isinstance(invalid_body.get("message"), dict) else invalid_body

        self._assert(case, "REL-01.01", "Empty webhook body ACKs successfully", ack_response.status_code == 200, expected=200, actual=ack_response.status_code)
        self._assert(case, "REL-01.02", "Empty webhook ACK body reports success", bool(ack_payload.get("success")) and bool(ack_payload.get("ack")), expected={"success": True, "ack": True}, actual=ack_body)
        self._assert(case, "REL-01.03", "Invalid signature returns 403", invalid_response.status_code == 403, expected=403, actual=invalid_response.status_code)
        self._assert(case, "REL-01.04", "Invalid signature body is explicit", invalid_payload.get("error") == "invalid_signature", expected="invalid_signature", actual=invalid_body)

        return {
            "order_webhook_url": order_webhook_url,
            "ack_status": ack_response.status_code,
            "ack_body": ack_body,
            "invalid_signature_status": invalid_response.status_code,
            "invalid_signature_body": invalid_body,
        }

    def _inbound_customer_create(self, case: dict[str, Any]) -> dict[str, Any]:
        fixture = self._primary_territory_fixture()
        slug = re.sub(r"[^a-z0-9]+", "", self.run_id.lower())
        first_name = "Woo"
        last_name = self.run_id
        email = f"woo.{slug}.customer@orderjarz.local"
        phone = self._unique_mobile()
        billing_line1 = f"{self.run_id} Woo Billing A"
        shipping_line1 = f"{self.run_id} Woo Shipping A"

        create_payload = self._build_woo_customer_payload(
            first_name=first_name,
            last_name=last_name,
            email=email,
            phone=phone,
            billing_line1=billing_line1,
            shipping_line1=shipping_line1,
            territory_fixture=fixture,
            billing_postcode="WIC001",
            shipping_postcode="WIS001",
            username=f"woo-{slug}-customer",
            password="Copilot123!",
        )
        created_woo_customer = self._woo_client().post("customers", create_payload)
        woo_customer_id = str(created_woo_customer.get("id") or "")
        if not woo_customer_id:
            raise RuntimeError(f"Woo customer create did not return an id: {created_woo_customer!r}")

        customer_payload = self._woo_customer(woo_customer_id) or created_woo_customer
        webhook = self._post_signed_woo_webhook(
            api_method="jarz_woocommerce_integration.api.webhooks.woo_customer_webhook",
            payload=customer_payload,
            topic="customer.created",
        )
        inbound_sync = self._ensure_inbound_event_processed(
            object_type="Customer",
            source_id=woo_customer_id,
            event_name=str((webhook.get("payload") or {}).get("event_name") or ""),
        )

        customer_name = self._find_customer_by_woo_customer_id(woo_customer_id)
        customer_doc = frappe.get_doc("Customer", customer_name) if customer_name else None
        addresses = self._customer_addresses(customer_name) if customer_name else []
        billing_address = self._find_customer_address(addresses, address_type="Billing", address_line1=billing_line1)
        shipping_address = self._find_customer_address(addresses, address_type="Shipping", address_line1=shipping_line1)
        duplicate_count = self._count_customers_by_woo_customer_id(woo_customer_id)
        latest_outbound_event = (
            self._latest_sync_event(
                direction="Outbound",
                object_type="Customer",
                source_id=customer_name,
                created_after=self.started_on,
            )
            if customer_name
            else None
        )

        self._assert(case, "WI-CUST-01.01", "Woo customer create returns an id", bool(woo_customer_id), expected="non-empty", actual=created_woo_customer)
        self._assert(case, "WI-CUST-01.02", "Customer webhook queued successfully", webhook.get("status_code") == 200 and bool((webhook.get("payload") or {}).get("queued")), expected={"status_code": 200, "queued": True}, actual=webhook)
        self._assert(case, "WI-CUST-01.03", "Inbound customer event exists", bool(inbound_sync.get("event_name")), expected="non-empty", actual=inbound_sync)
        self._assert(case, "WI-CUST-01.04", "Inbound customer event reaches Succeeded", str(((inbound_sync.get("latest_event") or {}).get("status") or "")) == "Succeeded", expected="Succeeded", actual=inbound_sync)
        self._assert(case, "WI-CUST-01.05", "ERP customer is linked to Woo customer id", bool(customer_name), expected="non-empty", actual=customer_name)
        self._assert(case, "WI-CUST-01.06", "Exactly one ERP customer is bound to the Woo customer", duplicate_count == 1, expected=1, actual=duplicate_count)
        self._assert(case, "WI-CUST-01.07", "ERP customer display name matches Woo customer name", str(getattr(customer_doc, "customer_name", "") or "") == f"{first_name} {last_name}", expected=f"{first_name} {last_name}", actual=getattr(customer_doc, "customer_name", None) if customer_doc else None)
        self._assert(case, "WI-CUST-01.08", "ERP customer mobile matches Woo billing phone", str(getattr(customer_doc, "mobile_no", "") or "") == phone, expected=phone, actual=getattr(customer_doc, "mobile_no", None) if customer_doc else None)
        self._assert(case, "WI-CUST-01.09", "ERP customer email matches Woo email", str(getattr(customer_doc, "email_id", "") or "") == email, expected=email, actual=getattr(customer_doc, "email_id", None) if customer_doc else None)
        self._assert(case, "WI-CUST-01.10", "Billing address exists in ERP", bool(billing_address), expected=True, actual=billing_address)
        self._assert(case, "WI-CUST-01.11", "Shipping address exists in ERP", bool(shipping_address), expected=True, actual=shipping_address)
        self._assert(case, "WI-CUST-01.12", "Customer territory resolves from Woo address state", str(getattr(customer_doc, "territory", "") or "") == fixture["territory"], expected=fixture["territory"], actual=getattr(customer_doc, "territory", None) if customer_doc else None)
        self._assert(case, "WI-CUST-01.13", "Inbound customer sync does not emit a same-run outbound customer event", latest_outbound_event is None, expected=None, actual=latest_outbound_event)

        self.runtime_state["inbound_customer"] = {
            "woo_customer_id": woo_customer_id,
            "customer_name": customer_name,
            "first_name": first_name,
            "last_name": last_name,
            "email": email,
            "phone": phone,
            "billing_line1": billing_line1,
            "shipping_line1": shipping_line1,
            "territory": fixture,
        }
        self._record_created("Woo Customer", woo_customer_id, note="WI-CUST-01 inbound Woo customer")
        if customer_name:
            self._record_created("Customer", customer_name, note="WI-CUST-01 inbound ERP customer")
        if billing_address:
            self._record_created("Address", str(billing_address.get("name") or ""), note="WI-CUST-01 billing address")
        if shipping_address and str(shipping_address.get("name") or "") != str((billing_address or {}).get("name") or ""):
            self._record_created("Address", str(shipping_address.get("name") or ""), note="WI-CUST-01 shipping address")

        return {
            "create_payload": create_payload,
            "created_woo_customer": created_woo_customer,
            "webhook": webhook,
            "inbound_sync": inbound_sync,
            "customer": customer_doc.as_dict() if customer_doc else None,
            "addresses": addresses,
        }

    def _inbound_customer_update(self, case: dict[str, Any]) -> dict[str, Any]:
        runtime_customer = self.runtime_state.get("inbound_customer") or {}
        woo_customer_id = str(runtime_customer.get("woo_customer_id") or "")
        customer_name = str(runtime_customer.get("customer_name") or "")
        if not woo_customer_id or not customer_name:
            raise RuntimeError("WI-CUST-01 must run successfully before WI-CUST-02")

        fixture = dict(runtime_customer.get("territory") or self._primary_territory_fixture())
        slug = re.sub(r"[^a-z0-9]+", "", self.run_id.lower())
        updated_first_name = "WooUpdated"
        updated_last_name = self.run_id
        updated_email = f"woo.{slug}.updated@orderjarz.local"
        updated_phone = f"011{''.join(ch for ch in self.run_id if ch.isdigit())[-8:].rjust(8, '0')}"

        update_payload = self._build_woo_customer_payload(
            first_name=updated_first_name,
            last_name=updated_last_name,
            email=updated_email,
            phone=updated_phone,
            billing_line1=str(runtime_customer.get("billing_line1") or ""),
            shipping_line1=str(runtime_customer.get("shipping_line1") or ""),
            territory_fixture=fixture,
            billing_postcode="WIC002",
            shipping_postcode="WIS002",
        )
        updated_woo_customer = self._woo_client().put(f"customers/{woo_customer_id}", update_payload)
        customer_payload = self._woo_customer(woo_customer_id) or updated_woo_customer
        webhook = self._post_signed_woo_webhook(
            api_method="jarz_woocommerce_integration.api.webhooks.woo_customer_webhook",
            payload=customer_payload,
            topic="customer.updated",
        )
        inbound_sync = self._ensure_inbound_event_processed(
            object_type="Customer",
            source_id=woo_customer_id,
            event_name=str((webhook.get("payload") or {}).get("event_name") or ""),
        )

        refreshed_customer_name = self._find_customer_by_woo_customer_id(woo_customer_id)
        customer_doc = frappe.get_doc("Customer", refreshed_customer_name) if refreshed_customer_name else None
        duplicate_count = self._count_customers_by_woo_customer_id(woo_customer_id)
        latest_outbound_event = (
            self._latest_sync_event(
                direction="Outbound",
                object_type="Customer",
                source_id=refreshed_customer_name,
                created_after=self.started_on,
            )
            if refreshed_customer_name
            else None
        )

        self._assert(case, "WI-CUST-02.01", "Customer update webhook queued successfully", webhook.get("status_code") == 200 and bool((webhook.get("payload") or {}).get("queued")), expected={"status_code": 200, "queued": True}, actual=webhook)
        self._assert(case, "WI-CUST-02.02", "Inbound customer update event reaches Succeeded", str(((inbound_sync.get("latest_event") or {}).get("status") or "")) == "Succeeded", expected="Succeeded", actual=inbound_sync)
        self._assert(case, "WI-CUST-02.03", "Inbound update stays bound to one ERP customer", duplicate_count == 1, expected=1, actual=duplicate_count)
        self._assert(case, "WI-CUST-02.04", "Inbound update keeps the same ERP customer record", refreshed_customer_name == customer_name, expected=customer_name, actual=refreshed_customer_name)
        self._assert(case, "WI-CUST-02.05", "ERP customer display name reflects the Woo update", str(getattr(customer_doc, "customer_name", "") or "") == f"{updated_first_name} {updated_last_name}", expected=f"{updated_first_name} {updated_last_name}", actual=getattr(customer_doc, "customer_name", None) if customer_doc else None)
        self._assert(case, "WI-CUST-02.06", "ERP customer mobile reflects the Woo update", str(getattr(customer_doc, "mobile_no", "") or "") == updated_phone, expected=updated_phone, actual=getattr(customer_doc, "mobile_no", None) if customer_doc else None)
        self._assert(case, "WI-CUST-02.07", "ERP customer email reflects the Woo update", str(getattr(customer_doc, "email_id", "") or "") == updated_email, expected=updated_email, actual=getattr(customer_doc, "email_id", None) if customer_doc else None)
        self._assert(case, "WI-CUST-02.08", "Inbound customer update does not emit a same-run outbound customer event", latest_outbound_event is None, expected=None, actual=latest_outbound_event)

        self.runtime_state["inbound_customer"].update({
            "email": updated_email,
            "phone": updated_phone,
            "first_name": updated_first_name,
            "last_name": updated_last_name,
            "customer_name": refreshed_customer_name,
        })

        return {
            "update_payload": update_payload,
            "updated_woo_customer": updated_woo_customer,
            "webhook": webhook,
            "inbound_sync": inbound_sync,
            "customer": customer_doc.as_dict() if customer_doc else None,
        }

    def _inbound_customer_address_update(self, case: dict[str, Any]) -> dict[str, Any]:
        runtime_customer = self.runtime_state.get("inbound_customer") or {}
        woo_customer_id = str(runtime_customer.get("woo_customer_id") or "")
        customer_name = str(runtime_customer.get("customer_name") or "")
        if not woo_customer_id or not customer_name:
            raise RuntimeError("WI-CUST-01 must run successfully before WI-ADDR-01")

        fixture = self._secondary_territory_fixture()
        billing_line1 = f"{self.run_id} Woo Billing B"
        shipping_line1 = f"{self.run_id} Woo Shipping B"
        update_payload = self._build_woo_customer_payload(
            first_name=str(runtime_customer.get("first_name") or "WooUpdated"),
            last_name=str(runtime_customer.get("last_name") or self.run_id),
            email=str(runtime_customer.get("email") or f"woo.{self.run_id.lower()}@orderjarz.local"),
            phone=str(runtime_customer.get("phone") or self._unique_mobile()),
            billing_line1=billing_line1,
            shipping_line1=shipping_line1,
            territory_fixture=fixture,
            billing_postcode="WIB003",
            shipping_postcode="WIS003",
        )
        updated_woo_customer = self._woo_client().put(f"customers/{woo_customer_id}", update_payload)
        customer_payload = self._woo_customer(woo_customer_id) or updated_woo_customer
        webhook = self._post_signed_woo_webhook(
            api_method="jarz_woocommerce_integration.api.webhooks.woo_customer_webhook",
            payload=customer_payload,
            topic="customer.updated",
        )
        inbound_sync = self._ensure_inbound_event_processed(
            object_type="Customer",
            source_id=woo_customer_id,
            event_name=str((webhook.get("payload") or {}).get("event_name") or ""),
        )

        customer_doc = frappe.get_doc("Customer", customer_name)
        addresses = self._customer_addresses(customer_name)
        billing_address = self._find_customer_address(addresses, address_type="Billing", address_line1=billing_line1)
        shipping_address = self._find_customer_address(addresses, address_type="Shipping", address_line1=shipping_line1)
        duplicate_count = self._count_customers_by_woo_customer_id(woo_customer_id)

        self._assert(case, "WI-ADDR-01.01", "Customer address update webhook queued successfully", webhook.get("status_code") == 200 and bool((webhook.get("payload") or {}).get("queued")), expected={"status_code": 200, "queued": True}, actual=webhook)
        self._assert(case, "WI-ADDR-01.02", "Inbound address update event reaches Succeeded", str(((inbound_sync.get("latest_event") or {}).get("status") or "")) == "Succeeded", expected="Succeeded", actual=inbound_sync)
        self._assert(case, "WI-ADDR-01.03", "Billing address exists after Woo address update", bool(billing_address), expected=True, actual=billing_address)
        self._assert(case, "WI-ADDR-01.04", "Shipping address exists after Woo address update", bool(shipping_address), expected=True, actual=shipping_address)
        self._assert(case, "WI-ADDR-01.05", "Billing address postcode matches Woo update", str((billing_address or {}).get("pincode") or "") == "WIB003", expected="WIB003", actual=(billing_address or {}).get("pincode"))
        self._assert(case, "WI-ADDR-01.06", "Shipping address postcode matches Woo update", str((shipping_address or {}).get("pincode") or "") == "WIS003", expected="WIS003", actual=(shipping_address or {}).get("pincode"))
        self._assert(case, "WI-ADDR-01.07", "Shipping address phone matches Woo update", str((shipping_address or {}).get("phone") or "") == str(runtime_customer.get("phone") or ""), expected=runtime_customer.get("phone"), actual=(shipping_address or {}).get("phone"))
        self._assert(case, "WI-ADDR-01.08", "Customer territory updates from the Woo address state", str(getattr(customer_doc, "territory", "") or "") == fixture["territory"], expected=fixture["territory"], actual=getattr(customer_doc, "territory", None))
        self._assert(case, "WI-ADDR-01.09", "Inbound address update does not create a duplicate ERP customer", duplicate_count == 1, expected=1, actual=duplicate_count)

        if billing_address:
            self._record_created("Address", str(billing_address.get("name") or ""), note="WI-ADDR-01 billing address update")
        if shipping_address and str(shipping_address.get("name") or "") != str((billing_address or {}).get("name") or ""):
            self._record_created("Address", str(shipping_address.get("name") or ""), note="WI-ADDR-01 shipping address update")

        self.runtime_state["inbound_customer"].update({
            "billing_line1": billing_line1,
            "shipping_line1": shipping_line1,
            "territory": fixture,
        })

        return {
            "update_payload": update_payload,
            "updated_woo_customer": updated_woo_customer,
            "webhook": webhook,
            "inbound_sync": inbound_sync,
            "customer": customer_doc.as_dict(),
            "addresses": addresses,
        }

    def _customer_webhook_replay(self, case: dict[str, Any]) -> dict[str, Any]:
        runtime_customer = self.runtime_state.get("inbound_customer") or {}
        woo_customer_id = str(runtime_customer.get("woo_customer_id") or "")
        customer_name = str(runtime_customer.get("customer_name") or "")
        if not woo_customer_id or not customer_name:
            self._assert(
                case,
                "REL-02.00",
                "WI-CUST-01 prerequisite succeeded",
                False,
                expected=True,
                actual=runtime_customer,
                concern=True,
            )
            return {"prerequisite": "WI-CUST-01"}

        customer_payload = self._woo_customer(woo_customer_id)
        if not customer_payload:
            raise RuntimeError(f"Unable to load Woo customer payload for replay: {woo_customer_id}")

        addresses_before = self._customer_addresses(customer_name)
        duplicate_before = self._count_customers_by_woo_customer_id(woo_customer_id)
        webhook = self._post_signed_woo_webhook(
            api_method="jarz_woocommerce_integration.api.webhooks.woo_customer_webhook",
            payload=customer_payload,
            topic="customer.updated",
        )
        inbound_sync = self._ensure_inbound_event_processed(
            object_type="Customer",
            source_id=woo_customer_id,
            event_name=str((webhook.get("payload") or {}).get("event_name") or ""),
        )

        refreshed_customer_name = self._find_customer_by_woo_customer_id(woo_customer_id)
        customer_doc = frappe.get_doc("Customer", refreshed_customer_name) if refreshed_customer_name else None
        addresses_after = self._customer_addresses(refreshed_customer_name) if refreshed_customer_name else []
        duplicate_after = self._count_customers_by_woo_customer_id(woo_customer_id)

        self._assert(case, "REL-02.01", "Replay customer webhook queued successfully", webhook.get("status_code") == 200 and bool((webhook.get("payload") or {}).get("queued")), expected={"status_code": 200, "queued": True}, actual=webhook)
        self._assert(case, "REL-02.02", "Replay inbound customer event exists", bool(inbound_sync.get("event_name")), expected="non-empty", actual=inbound_sync)
        self._assert(case, "REL-02.03", "Replay inbound customer event reaches Succeeded", str(((inbound_sync.get("latest_event") or {}).get("status") or "")) == "Succeeded", expected="Succeeded", actual=inbound_sync)
        self._assert(case, "REL-02.04", "Replay keeps the same ERP customer record", refreshed_customer_name == customer_name, expected=customer_name, actual=refreshed_customer_name)
        self._assert(case, "REL-02.05", "Replay does not create a duplicate ERP customer", duplicate_after == max(duplicate_before, 1) == 1, expected=1, actual={"before": duplicate_before, "after": duplicate_after})
        self._assert(case, "REL-02.06", "Replay keeps linked address count stable", len(addresses_after) == len(addresses_before), expected=len(addresses_before), actual=len(addresses_after))
        self._assert(case, "REL-02.07", "Replay preserves ERP customer mobile", str(getattr(customer_doc, "mobile_no", "") or "") == str(runtime_customer.get("phone") or ""), expected=runtime_customer.get("phone"), actual=getattr(customer_doc, "mobile_no", None) if customer_doc else None)
        self._assert(case, "REL-02.08", "Replay preserves ERP customer email", str(getattr(customer_doc, "email_id", "") or "") == str(runtime_customer.get("email") or ""), expected=runtime_customer.get("email"), actual=getattr(customer_doc, "email_id", None) if customer_doc else None)

        return {
            "customer_payload": customer_payload,
            "webhook": webhook,
            "inbound_sync": inbound_sync,
            "customer": customer_doc.as_dict() if customer_doc else None,
            "addresses_before": addresses_before,
            "addresses_after": addresses_after,
            "duplicate_before": duplicate_before,
            "duplicate_after": duplicate_after,
        }

    def _inbound_order_create(self, case: dict[str, Any]) -> dict[str, Any]:
        runtime_customer = dict(self.runtime_state.get("inbound_customer") or {})
        woo_customer_id = str(runtime_customer.get("woo_customer_id") or "")
        customer_name = str(runtime_customer.get("customer_name") or "")
        if not woo_customer_id or not customer_name:
            raise RuntimeError("WI-CUST-01 must run successfully before WI-ORD-01")

        fixture = dict(runtime_customer.get("territory") or self._primary_territory_fixture())
        delivery_slot = dict(self.fixture_catalog.get("next_delivery_slot") or _next_delivery_slot())
        fixture_items = self._order_fixture_items(str(fixture.get("price_list") or self._primary_territory_fixture().get("price_list") or ""))
        order_items = [
            {
                **dict(row),
                "qty": 1,
            }
            for row in fixture_items[:2]
        ]

        order_payload = self._build_woo_order_payload(
            woo_customer_id=woo_customer_id,
            first_name=str(runtime_customer.get("first_name") or "WooUpdated"),
            last_name=str(runtime_customer.get("last_name") or self.run_id),
            email=str(runtime_customer.get("email") or f"woo.{self.run_id.lower()}@orderjarz.local"),
            phone=str(runtime_customer.get("phone") or self._unique_mobile()),
            billing_line1=str(runtime_customer.get("billing_line1") or f"{self.run_id} Woo Billing B"),
            shipping_line1=str(runtime_customer.get("shipping_line1") or f"{self.run_id} Woo Shipping B"),
            territory_fixture=fixture,
            item_rows=order_items,
            delivery_slot=delivery_slot,
            status="processing",
            payment_method="cod",
            payment_method_title="Cash",
        )

        collision_attempts: list[dict[str, Any]] = []
        created_woo_order: dict[str, Any] | None = None
        woo_order_id = ""
        mapped_order_id_ceiling = self._max_mapped_woo_order_id()
        attempt = 0
        max_attempts = 5
        hard_attempt_cap = 250
        while attempt < max_attempts:
            attempt += 1
            attempt_payload = dict(order_payload)
            attempt_payload["meta_data"] = list(order_payload.get("meta_data") or []) + [
                {"key": "copilot_attempt", "value": str(attempt)},
            ]
            candidate_order = self._woo_client().post("orders", attempt_payload)
            candidate_order_id = str(candidate_order.get("id") or "")
            if not candidate_order_id:
                raise RuntimeError(f"Woo order create did not return an id: {candidate_order!r}")
            collision = self._preexisting_inbound_order_artifacts(candidate_order_id)
            if not collision["has_collision"]:
                created_woo_order = candidate_order
                woo_order_id = candidate_order_id
                break

            try:
                candidate_order_id_int = int(candidate_order_id)
            except Exception:
                candidate_order_id_int = 0

            if mapped_order_id_ceiling and candidate_order_id_int:
                # Staging can lag Woo auto-increment behind ERP's historical order-map range.
                # Keep allocating until we step past the highest mapped order id plus one buffer slot.
                required_attempts = (mapped_order_id_ceiling - candidate_order_id_int) + 2
                if required_attempts > 0:
                    max_attempts = min(hard_attempt_cap, max(max_attempts, attempt + required_attempts))

            collision_attempts.append(
                {
                    "attempt": attempt,
                    "woo_order_id": candidate_order_id,
                    "collision": collision,
                }
            )

        if not created_woo_order or not woo_order_id:
            raise RuntimeError(
                f"Unable to allocate an unmapped Woo order id after {attempt} attempts (mapped ceiling={mapped_order_id_ceiling}): {collision_attempts!r}"
            )

        order_payload_full = self._woo_order(woo_order_id) or created_woo_order
        webhook = self._post_signed_woo_webhook(
            api_method="jarz_woocommerce_integration.api.orders.woo_order_webhook",
            payload=order_payload_full,
            topic="order.created",
        )
        inbound_sync = self._ensure_inbound_event_processed(
            object_type="Order",
            source_id=woo_order_id,
            event_name=str((webhook.get("payload") or {}).get("event_name") or ""),
        )

        invoice_rows = self._active_invoices_for_woo_order_id(woo_order_id)
        invoice_name = str(invoice_rows[0].get("name") or "") if invoice_rows else ""
        invoice_doc = frappe.get_doc("Sales Invoice", invoice_name) if invoice_name else None
        order_map = self._order_map_row(woo_order_id)
        order_map_link_field = self._order_map_link_field()
        expected_signature = self._cart_signature(order_items)
        actual_signature = self._invoice_item_signature(invoice_doc) if invoice_doc else []

        self._assert(case, "WI-ORD-01.01", "Woo order create returns an id", bool(woo_order_id), expected="non-empty", actual=created_woo_order)
        self._assert(case, "WI-ORD-01.02", "Order webhook queued successfully", webhook.get("status_code") == 200 and bool((webhook.get("payload") or {}).get("queued")), expected={"status_code": 200, "queued": True}, actual=webhook)
        self._assert(case, "WI-ORD-01.03", "Inbound order event exists", bool(inbound_sync.get("event_name")), expected="non-empty", actual=inbound_sync)
        self._assert(case, "WI-ORD-01.04", "Inbound order event reaches Succeeded", str(((inbound_sync.get("latest_event") or {}).get("status") or "")) == "Succeeded", expected="Succeeded", actual=inbound_sync)
        self._assert(case, "WI-ORD-01.05", "Exactly one active ERP invoice is linked to the Woo order", len(invoice_rows) == 1, expected=1, actual=[row.get("name") for row in invoice_rows])
        self._assert(case, "WI-ORD-01.06", "Inbound Woo order creates an ERP invoice", bool(invoice_name and frappe.db.exists("Sales Invoice", invoice_name)), expected=True, actual=invoice_name)
        self._assert(case, "WI-ORD-01.07", "Inbound invoice is submitted", int(getattr(invoice_doc, "docstatus", 0) or 0) == 1, expected=1, actual=getattr(invoice_doc, "docstatus", 0) if invoice_doc else None)
        self._assert(case, "WI-ORD-01.08", "Inbound invoice stays bound to the Woo customer record", str(getattr(invoice_doc, "customer", "") or "") == customer_name, expected=customer_name, actual=getattr(invoice_doc, "customer", None) if invoice_doc else None)
        self._assert(case, "WI-ORD-01.09", "Inbound invoice items match the Woo order items", actual_signature == expected_signature, expected=expected_signature, actual=actual_signature)
        self._assert(case, "WI-ORD-01.10", "Inbound invoice payment method maps from Woo payment method", str(getattr(invoice_doc, "custom_payment_method", "") or "") == "Cash", expected="Cash", actual=getattr(invoice_doc, "custom_payment_method", None) if invoice_doc else None)
        self._assert(
            case,
            "WI-ORD-01.11",
            "Inbound invoice state remains Woo-processing compatible",
            self._invoice_woo_status_key(invoice_doc) == "processing",
            expected="processing",
            actual={
                "resolved_status": self._invoice_woo_status_key(invoice_doc) if invoice_doc else None,
                "state_candidates": self._invoice_state_candidates(invoice_doc) if invoice_doc else [],
            },
        )
        self._assert(case, "WI-ORD-01.12", "Woo order map exists for the inbound-created invoice", bool(order_map), expected=True, actual=order_map, concern=True)
        if order_map:
            self._assert(case, "WI-ORD-01.13", "Woo order map points to the inbound invoice", str(order_map.get(order_map_link_field) or "") == invoice_name, expected=invoice_name, actual=order_map)

        self.runtime_state["inbound_order"] = {
            "woo_order_id": woo_order_id,
            "invoice_name": invoice_name,
            "item_signature": expected_signature,
            "order_payload": order_payload_full,
        }
        self._record_created("Woo Order", woo_order_id, note="WI-ORD-01 inbound Woo order")
        if invoice_name:
            self._record_created("Sales Invoice", invoice_name, note="WI-ORD-01 inbound ERP invoice")

        return {
            "order_payload": order_payload,
            "created_woo_order": created_woo_order,
            "collision_attempts": collision_attempts,
            "mapped_order_id_ceiling": mapped_order_id_ceiling,
            "webhook": webhook,
            "inbound_sync": inbound_sync,
            "invoice_rows": invoice_rows,
            "invoice": invoice_doc.as_dict() if invoice_doc else None,
            "order_map": order_map,
        }

    def _inbound_order_replay(self, case: dict[str, Any]) -> dict[str, Any]:
        runtime_order = dict(self.runtime_state.get("inbound_order") or {})
        woo_order_id = str(runtime_order.get("woo_order_id") or "")
        invoice_name = str(runtime_order.get("invoice_name") or "")
        if not woo_order_id or not invoice_name:
            self._assert(
                case,
                "WI-ORD-02.00",
                "WI-ORD-01 prerequisite succeeded",
                False,
                expected=True,
                actual=runtime_order,
                concern=True,
            )
            return {"prerequisite": "WI-ORD-01"}

        order_payload = dict(runtime_order.get("order_payload") or self._woo_order(woo_order_id) or {})
        if not order_payload:
            raise RuntimeError(f"Unable to load Woo order payload for replay: {woo_order_id}")

        webhook = self._post_signed_woo_webhook(
            api_method="jarz_woocommerce_integration.api.orders.woo_order_webhook",
            payload=order_payload,
            topic="order.updated",
        )
        inbound_sync = self._ensure_inbound_event_processed(
            object_type="Order",
            source_id=woo_order_id,
            event_name=str((webhook.get("payload") or {}).get("event_name") or ""),
        )

        latest_event = dict(inbound_sync.get("latest_event") or {})
        process_result = dict(inbound_sync.get("process_result") or {})
        invoice_rows = self._active_invoices_for_woo_order_id(woo_order_id)
        order_map = self._order_map_row(woo_order_id)
        order_map_link_field = self._order_map_link_field()
        linked_invoices = [str(row.get("name") or "") for row in invoice_rows]
        resolved_status = str(process_result.get("status") or latest_event.get("status") or "").strip().lower()

        self._assert(case, "WI-ORD-02.01", "Replay webhook queued successfully", webhook.get("status_code") == 200 and bool((webhook.get("payload") or {}).get("queued")), expected={"status_code": 200, "queued": True}, actual=webhook)
        self._assert(case, "WI-ORD-02.02", "Replay inbound order event exists", bool(inbound_sync.get("event_name")), expected="non-empty", actual=inbound_sync)
        self._assert(case, "WI-ORD-02.03", "Replay resolves without creating a new invoice", resolved_status in {"skipped", "succeeded"}, expected="skipped/succeeded", actual={"resolved_status": resolved_status, "latest_event": latest_event, "process_result": process_result})
        self._assert(case, "WI-ORD-02.04", "Replay keeps exactly one active ERP invoice linked", len(invoice_rows) == 1, expected=1, actual=linked_invoices)
        self._assert(case, "WI-ORD-02.05", "Replay keeps the original ERP invoice mapping", linked_invoices == [invoice_name], expected=[invoice_name], actual=linked_invoices)
        if order_map:
            self._assert(case, "WI-ORD-02.06", "Replay preserves the Woo order map target", str(order_map.get(order_map_link_field) or "") == invoice_name, expected=invoice_name, actual=order_map)
        else:
            self._assert(case, "WI-ORD-02.06", "Replay preserves the Woo order map target", False, expected=invoice_name, actual=order_map)

        return {
            "order_payload": order_payload,
            "webhook": webhook,
            "inbound_sync": inbound_sync,
            "invoice_rows": invoice_rows,
            "order_map": order_map,
        }

    def _inbound_order_update_amendment(self, case: dict[str, Any]) -> dict[str, Any]:
        runtime_order = dict(self.runtime_state.get("inbound_order") or {})
        woo_order_id = str(runtime_order.get("woo_order_id") or "")
        source_invoice_name = str(runtime_order.get("invoice_name") or "")
        if not woo_order_id or not source_invoice_name:
            self._assert(
                case,
                "WI-ORD-03.00",
                "WI-ORD-01 prerequisite succeeded",
                False,
                expected=True,
                actual=runtime_order,
                concern=True,
            )
            return {"prerequisite": "WI-ORD-01"}

        settings = frappe.get_single("WooCommerce Settings")
        inbound_amendment_enabled = bool(int(getattr(settings, "enable_inbound_amendment", 0) or 0))
        self._assert(
            case,
            "WI-ORD-03.01",
            "Inbound amendment is enabled on staging",
            inbound_amendment_enabled,
            expected=True,
            actual=str(getattr(settings, "enable_inbound_amendment", 0) or "0"),
        )
        if not inbound_amendment_enabled:
            return {"flags": _snapshot_flags(settings)}

        current_order = dict(self._woo_order(woo_order_id) or runtime_order.get("order_payload") or {})
        line_items = [dict(row) for row in (current_order.get("line_items") or [])]
        if len(line_items) < 2:
            raise RuntimeError(f"WI-ORD-03 requires at least two Woo line items: {current_order!r}")

        first_line = dict(line_items[0])
        first_line_id = first_line.get("id")
        first_line_qty = int(first_line.get("quantity") or 0)
        if not first_line_id or first_line_qty < 1:
            raise RuntimeError(f"WI-ORD-03 could not resolve the first Woo line item id/qty: {first_line!r}")

        update_payload = {
            "status": "processing",
            "line_items": [
                {
                    "id": first_line_id,
                    "quantity": first_line_qty + 1,
                }
            ],
        }
        updated_woo_order = self._woo_client().put(f"orders/{woo_order_id}", update_payload)
        expected_signature = self._woo_order_item_signature(updated_woo_order)

        webhook = self._post_signed_woo_webhook(
            api_method="jarz_woocommerce_integration.api.orders.woo_order_webhook",
            payload=updated_woo_order,
            topic="order.updated",
        )
        inbound_sync = self._ensure_inbound_event_processed(
            object_type="Order",
            source_id=woo_order_id,
            event_name=str((webhook.get("payload") or {}).get("event_name") or ""),
        )

        process_result = dict(inbound_sync.get("process_result") or {})
        queue_result = dict(process_result.get("result") or {})
        latest_event = dict(inbound_sync.get("latest_event") or {})
        amendment_wait = self._wait_for_inbound_amendment(source_invoice_name, woo_order_id, timeout_seconds=45)
        replacement_invoice_name = str(amendment_wait.get("replacement_invoice_name") or "")
        replacement_invoice = amendment_wait.get("replacement_invoice_doc")
        active_invoices = amendment_wait.get("active_invoices") or []
        linked_invoice_names = [str(row.get("name") or "") for row in active_invoices]
        order_map = amendment_wait.get("order_map")
        order_map_link_field = self._order_map_link_field()
        source_invoice_doc = frappe.get_doc("Sales Invoice", source_invoice_name)
        replacement_signature = self._invoice_item_signature(replacement_invoice) if replacement_invoice else []

        self._assert(case, "WI-ORD-03.02", "Woo order update returns the same order id", str(updated_woo_order.get("id") or "") == woo_order_id, expected=woo_order_id, actual=updated_woo_order)
        self._assert(case, "WI-ORD-03.03", "Updated Woo order line items reflect the item edit", bool(expected_signature and expected_signature != runtime_order.get("item_signature")), expected={"changed": True, "signature": expected_signature}, actual={"before": runtime_order.get("item_signature"), "after": expected_signature})
        self._assert(case, "WI-ORD-03.04", "Updated-order webhook queued successfully", webhook.get("status_code") == 200 and bool((webhook.get("payload") or {}).get("queued")), expected={"status_code": 200, "queued": True}, actual=webhook)
        self._assert(case, "WI-ORD-03.05", "Updated inbound order event exists", bool(inbound_sync.get("event_name")), expected="non-empty", actual=inbound_sync)
        self._assert(case, "WI-ORD-03.06", "Submitted Woo item edit enqueues amendment handling", str(queue_result.get("status") or "").strip().lower() == "queued" and str(queue_result.get("reason") or "").strip().lower() == "amendment_enqueued", expected={"status": "queued", "reason": "amendment_enqueued"}, actual={"latest_event": latest_event, "process_result": process_result, "queue_result": queue_result})
        self._assert(case, "WI-ORD-03.07", "Source invoice is cancelled after inbound amendment", int(getattr(source_invoice_doc, "docstatus", 0) or 0) == 2, expected=2, actual=getattr(source_invoice_doc, "docstatus", 0))
        self._assert(case, "WI-ORD-03.08", "Replacement invoice is created for the Woo item edit", bool(replacement_invoice_name and replacement_invoice), expected="non-empty", actual=amendment_wait)
        self._assert(case, "WI-ORD-03.09", "Replacement invoice preserves amended_from linkage", str(getattr(replacement_invoice, "amended_from", "") or "") == source_invoice_name, expected=source_invoice_name, actual=getattr(replacement_invoice, "amended_from", None) if replacement_invoice else None)
        self._assert(case, "WI-ORD-03.10", "Replacement invoice items match the updated Woo order", replacement_signature == expected_signature, expected=expected_signature, actual=replacement_signature)
        self._assert(case, "WI-ORD-03.11", "Only one active ERP invoice remains linked after amendment", linked_invoice_names == ([replacement_invoice_name] if replacement_invoice_name else []), expected=[replacement_invoice_name] if replacement_invoice_name else ["non-empty"], actual=linked_invoice_names)
        self._assert(case, "WI-ORD-03.12", "Woo order map relinks to the replacement invoice", bool(order_map and str(order_map.get(order_map_link_field) or "") == replacement_invoice_name), expected=replacement_invoice_name, actual=order_map)

        if replacement_invoice_name:
            self._record_created("Sales Invoice", replacement_invoice_name, note="WI-ORD-03 replacement invoice")
            self.runtime_state["inbound_order"] = {
                "woo_order_id": woo_order_id,
                "invoice_name": replacement_invoice_name,
                "source_invoice_name": source_invoice_name,
                "item_signature": expected_signature,
                "order_payload": updated_woo_order,
            }

        return {
            "update_payload": update_payload,
            "updated_woo_order": updated_woo_order,
            "webhook": webhook,
            "inbound_sync": inbound_sync,
            "amendment_wait": amendment_wait,
            "order_map": order_map,
        }

    def _inbound_order_customer_detail_amendment(self, case: dict[str, Any]) -> dict[str, Any]:
        runtime_order = dict(self.runtime_state.get("inbound_order") or {})
        woo_order_id = str(runtime_order.get("woo_order_id") or "")
        source_invoice_name = str(runtime_order.get("invoice_name") or "")
        if not woo_order_id or not source_invoice_name:
            self._assert(
                case,
                "WI-ORD-04.00",
                "WI-ORD-03 prerequisite succeeded",
                False,
                expected=True,
                actual=runtime_order,
                concern=True,
            )
            return {"prerequisite": "WI-ORD-03"}

        current_order = dict(self._woo_order(woo_order_id) or runtime_order.get("order_payload") or {})
        billing_before = dict(current_order.get("billing") or {})
        shipping_before = dict(current_order.get("shipping") or {})
        updated_billing_line = f"{self.run_id} Woo Billing C"
        updated_shipping_line = f"{self.run_id} Woo Shipping C"
        updated_phone = f"012{''.join(ch for ch in self.run_id if ch.isdigit())[-8:].rjust(8, '0')}"
        updated_note = f"{self.run_id} Woo customer detail amendment"

        update_payload = {
            "status": "processing",
            "billing": {
                **billing_before,
                "address_1": updated_billing_line,
                "phone": updated_phone,
            },
            "shipping": {
                **shipping_before,
                "address_1": updated_shipping_line,
                "phone": updated_phone,
            },
            "customer_note": updated_note,
        }
        updated_woo_order = self._woo_client().put(f"orders/{woo_order_id}", update_payload)
        webhook = self._post_signed_woo_webhook(
            api_method="jarz_woocommerce_integration.api.orders.woo_order_webhook",
            payload=updated_woo_order,
            topic="order.updated",
        )
        inbound_sync = self._ensure_inbound_event_processed(
            object_type="Order",
            source_id=woo_order_id,
            event_name=str((webhook.get("payload") or {}).get("event_name") or ""),
        )

        process_result = dict(inbound_sync.get("process_result") or {})
        queue_result = dict(process_result.get("result") or {})
        latest_event = dict(inbound_sync.get("latest_event") or {})
        amendment_wait = self._wait_for_inbound_amendment(source_invoice_name, woo_order_id, timeout_seconds=45)
        replacement_invoice_name = str(amendment_wait.get("replacement_invoice_name") or "")
        replacement_invoice = amendment_wait.get("replacement_invoice_doc")
        active_invoices = amendment_wait.get("active_invoices") or []
        linked_invoice_names = [str(row.get("name") or "") for row in active_invoices]
        order_map = amendment_wait.get("order_map")
        order_map_link_field = self._order_map_link_field()
        source_invoice_doc = frappe.get_doc("Sales Invoice", source_invoice_name)
        billing_address_doc = frappe.get_doc("Address", replacement_invoice.customer_address) if replacement_invoice and getattr(replacement_invoice, "customer_address", None) else None
        shipping_address_doc = frappe.get_doc("Address", replacement_invoice.shipping_address_name) if replacement_invoice and getattr(replacement_invoice, "shipping_address_name", None) else None
        updated_signature = self._woo_order_item_signature(updated_woo_order)

        self._assert(case, "WI-ORD-04.01", "Woo customer-detail update returns the same order id", str(updated_woo_order.get("id") or "") == woo_order_id, expected=woo_order_id, actual=updated_woo_order)
        self._assert(case, "WI-ORD-04.02", "Customer-detail webhook queued successfully", webhook.get("status_code") == 200 and bool((webhook.get("payload") or {}).get("queued")), expected={"status_code": 200, "queued": True}, actual=webhook)
        self._assert(case, "WI-ORD-04.03", "Updated inbound order event exists", bool(inbound_sync.get("event_name")), expected="non-empty", actual=inbound_sync)
        self._assert(case, "WI-ORD-04.04", "Customer-detail update enqueues amendment handling", str(queue_result.get("status") or "").strip().lower() == "queued" and str(queue_result.get("reason") or "").strip().lower() == "amendment_enqueued", expected={"status": "queued", "reason": "amendment_enqueued"}, actual={"latest_event": latest_event, "process_result": process_result, "queue_result": queue_result})
        self._assert(case, "WI-ORD-04.05", "Prior invoice is cancelled after customer-detail amendment", int(getattr(source_invoice_doc, "docstatus", 0) or 0) == 2, expected=2, actual=getattr(source_invoice_doc, "docstatus", 0))
        self._assert(case, "WI-ORD-04.06", "Replacement invoice is created for customer-detail amendment", bool(replacement_invoice_name and replacement_invoice), expected="non-empty", actual=amendment_wait)
        self._assert(case, "WI-ORD-04.07", "Replacement invoice preserves amended_from linkage", str(getattr(replacement_invoice, "amended_from", "") or "") == source_invoice_name, expected=source_invoice_name, actual=getattr(replacement_invoice, "amended_from", None) if replacement_invoice else None)
        self._assert(case, "WI-ORD-04.08", "Replacement invoice billing address reflects Woo update", str(getattr(billing_address_doc, "address_line1", "") or "") == updated_billing_line, expected=updated_billing_line, actual=getattr(billing_address_doc, "address_line1", None) if billing_address_doc else None)
        self._assert(case, "WI-ORD-04.09", "Replacement invoice shipping address reflects Woo update", str(getattr(shipping_address_doc, "address_line1", "") or "") == updated_shipping_line, expected=updated_shipping_line, actual=getattr(shipping_address_doc, "address_line1", None) if shipping_address_doc else None)
        self._assert(case, "WI-ORD-04.10", "Replacement invoice shipping phone reflects Woo update", str(getattr(shipping_address_doc, "phone", "") or "") == updated_phone, expected=updated_phone, actual=getattr(shipping_address_doc, "phone", None) if shipping_address_doc else None)
        self._assert(case, "WI-ORD-04.11", "Replacement invoice remarks capture the Woo customer note", updated_note in str(getattr(replacement_invoice, "remarks", "") or ""), expected=updated_note, actual=getattr(replacement_invoice, "remarks", None) if replacement_invoice else None)
        self._assert(case, "WI-ORD-04.12", "Only one active ERP invoice remains linked after customer-detail amendment", linked_invoice_names == ([replacement_invoice_name] if replacement_invoice_name else []), expected=[replacement_invoice_name] if replacement_invoice_name else ["non-empty"], actual=linked_invoice_names)
        self._assert(case, "WI-ORD-04.13", "Woo order map relinks to the replacement invoice", bool(order_map and str(order_map.get(order_map_link_field) or "") == replacement_invoice_name), expected=replacement_invoice_name, actual=order_map)

        if replacement_invoice_name:
            self._record_created("Sales Invoice", replacement_invoice_name, note="WI-ORD-04 replacement invoice")
            self.runtime_state["inbound_order"] = {
                "woo_order_id": woo_order_id,
                "invoice_name": replacement_invoice_name,
                "source_invoice_name": source_invoice_name,
                "item_signature": updated_signature,
                "order_payload": updated_woo_order,
            }

        return {
            "update_payload": update_payload,
            "updated_woo_order": updated_woo_order,
            "webhook": webhook,
            "inbound_sync": inbound_sync,
            "amendment_wait": amendment_wait,
            "order_map": order_map,
        }

    def _inbound_order_status_manual_review(self, case: dict[str, Any]) -> dict[str, Any]:
        runtime_order = dict(self.runtime_state.get("inbound_order") or {})
        woo_order_id = str(runtime_order.get("woo_order_id") or "")
        invoice_name = str(runtime_order.get("invoice_name") or "")
        if not woo_order_id or not invoice_name:
            self._assert(
                case,
                "WI-ORD-05.00",
                "WI-ORD-04 prerequisite succeeded",
                False,
                expected=True,
                actual=runtime_order,
                concern=True,
            )
            return {"prerequisite": "WI-ORD-04"}

        invoice_before = frappe.get_doc("Sales Invoice", invoice_name)
        state_before = self._invoice_state_candidates(invoice_before)
        order_map_before = self._order_map_row(woo_order_id)

        update_payload = {"status": "completed"}
        updated_woo_order = self._woo_client().put(f"orders/{woo_order_id}", update_payload)
        webhook = self._post_signed_woo_webhook(
            api_method="jarz_woocommerce_integration.api.orders.woo_order_webhook",
            payload=updated_woo_order,
            topic="order.updated",
        )
        inbound_sync = self._ensure_inbound_event_processed(
            object_type="Order",
            source_id=woo_order_id,
            event_name=str((webhook.get("payload") or {}).get("event_name") or ""),
        )

        process_result = dict(inbound_sync.get("process_result") or {})
        process_reason = str(process_result.get("reason") or ((process_result.get("result") or {}).get("reason")) or "")
        latest_event = dict(inbound_sync.get("latest_event") or {})
        invoice_after = frappe.get_doc("Sales Invoice", invoice_name)
        active_invoices = self._active_invoices_for_woo_order_id(woo_order_id)
        linked_invoice_names = [str(row.get("name") or "") for row in active_invoices]
        order_map_after = self._order_map_row(woo_order_id)
        order_map_link_field = self._order_map_link_field()

        self._assert(case, "WI-ORD-05.01", "Terminal-status webhook queued successfully", webhook.get("status_code") == 200 and bool((webhook.get("payload") or {}).get("queued")), expected={"status_code": 200, "queued": True}, actual=webhook)
        self._assert(case, "WI-ORD-05.02", "Terminal-status inbound order event exists", bool(inbound_sync.get("event_name")), expected="non-empty", actual=inbound_sync)
        self._assert(case, "WI-ORD-05.03", "Terminal Woo status change is skipped for the submitted ERP invoice", str(process_result.get("status") or "").strip().lower() == "skipped", expected="skipped", actual=process_result)
        self._assert(case, "WI-ORD-05.04", "Terminal Woo status change is flagged for manual review", process_reason == "needs_manual_review", expected="needs_manual_review", actual={"process_result": process_result, "latest_event": latest_event})
        self._assert(case, "WI-ORD-05.05", "Inbound event is recorded as Skipped", str(latest_event.get("status") or "") == "Skipped", expected="Skipped", actual=latest_event)
        self._assert(case, "WI-ORD-05.06", "Woo order status changed to completed", str(updated_woo_order.get("status") or "") == "completed", expected="completed", actual=updated_woo_order.get("status"))
        self._assert(case, "WI-ORD-05.07", "ERP invoice remains submitted after terminal Woo status change", int(getattr(invoice_after, "docstatus", 0) or 0) == 1, expected=1, actual=getattr(invoice_after, "docstatus", 0))
        self._assert(case, "WI-ORD-05.08", "ERP invoice state remains unchanged after terminal Woo status change", self._invoice_state_candidates(invoice_after) == state_before, expected=state_before, actual=self._invoice_state_candidates(invoice_after))
        self._assert(case, "WI-ORD-05.09", "Only the current ERP invoice remains active after terminal Woo status change", linked_invoice_names == [invoice_name], expected=[invoice_name], actual=linked_invoice_names)
        self._assert(case, "WI-ORD-05.10", "Woo order map stays linked to the current invoice", bool(order_map_after and str(order_map_after.get(order_map_link_field) or "") == invoice_name), expected=invoice_name, actual={"before": order_map_before, "after": order_map_after})

        return {
            "update_payload": update_payload,
            "updated_woo_order": updated_woo_order,
            "webhook": webhook,
            "inbound_sync": inbound_sync,
            "invoice_before": invoice_before.as_dict(),
            "invoice_after": invoice_after.as_dict(),
            "order_map_before": order_map_before,
            "order_map_after": order_map_after,
        }

    def _inbound_order_cancel_manual_review(self, case: dict[str, Any]) -> dict[str, Any]:
        runtime_order = dict(self.runtime_state.get("inbound_order") or {})
        woo_order_id = str(runtime_order.get("woo_order_id") or "")
        invoice_name = str(runtime_order.get("invoice_name") or "")
        if not woo_order_id or not invoice_name:
            self._assert(
                case,
                "WI-ORD-06.00",
                "WI-ORD-04 prerequisite succeeded",
                False,
                expected=True,
                actual=runtime_order,
                concern=True,
            )
            return {"prerequisite": "WI-ORD-04"}

        invoice_before = frappe.get_doc("Sales Invoice", invoice_name)
        state_before = self._invoice_state_candidates(invoice_before)
        order_map_before = self._order_map_row(woo_order_id)

        update_payload = {"status": "cancelled"}
        updated_woo_order = self._woo_client().put(f"orders/{woo_order_id}", update_payload)
        webhook = self._post_signed_woo_webhook(
            api_method="jarz_woocommerce_integration.api.orders.woo_order_webhook",
            payload=updated_woo_order,
            topic="order.updated",
        )
        inbound_sync = self._ensure_inbound_event_processed(
            object_type="Order",
            source_id=woo_order_id,
            event_name=str((webhook.get("payload") or {}).get("event_name") or ""),
        )

        process_result = dict(inbound_sync.get("process_result") or {})
        process_reason = str(process_result.get("reason") or ((process_result.get("result") or {}).get("reason")) or "")
        latest_event = dict(inbound_sync.get("latest_event") or {})
        invoice_after = frappe.get_doc("Sales Invoice", invoice_name)
        active_invoices = self._active_invoices_for_woo_order_id(woo_order_id)
        linked_invoice_names = [str(row.get("name") or "") for row in active_invoices]
        order_map_after = self._order_map_row(woo_order_id)
        order_map_link_field = self._order_map_link_field()

        self._assert(case, "WI-ORD-06.01", "Cancellation webhook queued successfully", webhook.get("status_code") == 200 and bool((webhook.get("payload") or {}).get("queued")), expected={"status_code": 200, "queued": True}, actual=webhook)
        self._assert(case, "WI-ORD-06.02", "Cancellation inbound order event exists", bool(inbound_sync.get("event_name")), expected="non-empty", actual=inbound_sync)
        self._assert(case, "WI-ORD-06.03", "Woo cancellation is skipped for the submitted ERP invoice", str(process_result.get("status") or "").strip().lower() == "skipped", expected="skipped", actual=process_result)
        self._assert(case, "WI-ORD-06.04", "Woo cancellation is flagged for manual review", process_reason == "needs_manual_review", expected="needs_manual_review", actual={"process_result": process_result, "latest_event": latest_event})
        self._assert(case, "WI-ORD-06.05", "Inbound cancellation event is recorded as Skipped", str(latest_event.get("status") or "") == "Skipped", expected="Skipped", actual=latest_event)
        self._assert(case, "WI-ORD-06.06", "Woo order status changed to cancelled", str(updated_woo_order.get("status") or "") == "cancelled", expected="cancelled", actual=updated_woo_order.get("status"))
        self._assert(case, "WI-ORD-06.07", "ERP invoice remains submitted after Woo cancellation", int(getattr(invoice_after, "docstatus", 0) or 0) == 1, expected=1, actual=getattr(invoice_after, "docstatus", 0))
        self._assert(case, "WI-ORD-06.08", "ERP invoice state remains unchanged after Woo cancellation", self._invoice_state_candidates(invoice_after) == state_before, expected=state_before, actual=self._invoice_state_candidates(invoice_after))
        self._assert(case, "WI-ORD-06.09", "Only the current ERP invoice remains active after Woo cancellation", linked_invoice_names == [invoice_name], expected=[invoice_name], actual=linked_invoice_names)
        self._assert(case, "WI-ORD-06.10", "Woo order map stays linked to the current invoice after Woo cancellation", bool(order_map_after and str(order_map_after.get(order_map_link_field) or "") == invoice_name), expected=invoice_name, actual={"before": order_map_before, "after": order_map_after})

        return {
            "update_payload": update_payload,
            "updated_woo_order": updated_woo_order,
            "webhook": webhook,
            "inbound_sync": inbound_sync,
            "invoice_before": invoice_before.as_dict(),
            "invoice_after": invoice_after.as_dict(),
            "order_map_before": order_map_before,
            "order_map_after": order_map_after,
        }

    def _outbound_customer_create(self, case: dict[str, Any]) -> dict[str, Any]:
        from jarz_pos.api.customer import create_customer

        fixture = self._primary_territory_fixture()
        display_name = f"Copilot {self.run_id}"
        mobile = self._unique_mobile()
        address_line = f"{self.run_id} Primary Address"

        result = create_customer(
            customer_name=display_name,
            mobile_no=mobile,
            customer_primary_address=address_line,
            territory_id=fixture["territory"],
        )
        frappe.db.commit()

        customer_name = str(result.get("name") or "")
        customer_doc = frappe.get_doc("Customer", customer_name)
        linked_addresses = self._customer_addresses(customer_name)
        sync_result = self._ensure_customer_synced_to_woo(customer_name)
        customer_doc = frappe.get_doc("Customer", customer_name)
        woo_customer_id = str(getattr(customer_doc, "woo_customer_id", "") or "")
        woo_customer = self._woo_customer(woo_customer_id) if woo_customer_id else None

        self._assert(case, "EO-CUST-01.01", "Customer API returns an ERP customer name", bool(customer_name), expected="non-empty", actual=customer_name)
        self._assert(case, "EO-CUST-01.02", "Customer exists in ERP", bool(frappe.db.exists("Customer", customer_name)), expected=True, actual=bool(frappe.db.exists("Customer", customer_name)))
        self._assert(case, "EO-CUST-01.03", "Customer has at least one linked address", len(linked_addresses) >= 1, expected=">=1", actual=len(linked_addresses))
        self._assert(case, "EO-CUST-01.04", "Customer outbound path used event processing", sync_result.get("mode") == "event", expected="event", actual=sync_result.get("mode"), concern=True)
        self._assert(case, "EO-CUST-01.05", "Customer has Woo customer ID after sync", bool(woo_customer_id), expected="non-empty", actual=woo_customer_id)
        self._assert(case, "EO-CUST-01.06", "Customer outbound status is Synced", getattr(customer_doc, "woo_outbound_status", "") == "Synced", expected="Synced", actual=getattr(customer_doc, "woo_outbound_status", ""))
        self._assert(case, "EO-CUST-01.07", "Woo customer exists", isinstance(woo_customer, dict) and bool(woo_customer.get("id")), expected=True, actual=woo_customer)
        self._assert(case, "EO-CUST-01.08", "Woo customer phone matches ERP mobile", ((woo_customer or {}).get("billing") or {}).get("phone") == mobile, expected=mobile, actual=((woo_customer or {}).get("billing") or {}).get("phone"))
        self._assert(case, "EO-CUST-01.09", "Woo customer email uses local-domain placeholder", str((woo_customer or {}).get("email") or "").endswith("@orderjarz.local"), expected="*@orderjarz.local", actual=(woo_customer or {}).get("email"))

        self.runtime_state["customer"] = {
            "customer_name": customer_name,
            "display_name": display_name,
            "mobile": mobile,
            "territory": fixture,
            "primary_address": address_line,
            "woo_customer_id": woo_customer_id,
        }
        self._record_created("Customer", customer_name, note="EO-CUST-01 synthetic customer")
        if linked_addresses:
            self._record_created("Address", linked_addresses[0]["name"], note="EO-CUST-01 initial address")
        if woo_customer_id:
            self._record_created("Woo Customer", woo_customer_id, note="EO-CUST-01 outbound customer")

        return {
            "customer_result": result,
            "sync_result": sync_result,
            "customer_doc": customer_doc.as_dict(),
            "linked_addresses": linked_addresses,
            "woo_customer": woo_customer,
        }

    def _outbound_customer_address_update(self, case: dict[str, Any]) -> dict[str, Any]:
        from jarz_pos.api.customer import save_customer_shipping_address, update_customer_shipping_address

        runtime_customer = self.runtime_state.get("customer") or {}
        customer_name = str(runtime_customer.get("customer_name") or "")
        if not customer_name:
            raise RuntimeError("EO-CUST-01 must run successfully before EO-ADDR-01")

        fixture = self._primary_territory_fixture()
        mobile = str(runtime_customer.get("mobile") or self._unique_mobile())
        linked_before = self._customer_addresses(customer_name)
        save_address_line = f"{self.run_id} Secondary Address"
        save_result = save_customer_shipping_address(
            customer=customer_name,
            phone=mobile,
            address=save_address_line,
            set_as_primary=1,
        )
        frappe.db.commit()

        selected_address_name = str(save_result.get("selected_address_name") or "")
        linked_after_save = self._customer_addresses(customer_name)
        sync_after_save = self._ensure_customer_synced_to_woo(customer_name, scope="shipping")
        woo_after_save = self._woo_customer(self._customer_woo_id(customer_name))

        updated_address_line = f"{self.run_id} Secondary Address Updated"
        update_result = update_customer_shipping_address(
            customer=customer_name,
            address_name=selected_address_name,
            address_line1=updated_address_line,
            city=fixture["territory_name"],
            phone=mobile,
            pincode="STG001",
        )
        frappe.db.commit()

        linked_after_update = self._customer_addresses(customer_name)
        sync_after_update = self._ensure_customer_synced_to_woo(customer_name, scope="shipping")
        woo_after_update = self._woo_customer(self._customer_woo_id(customer_name))

        self._assert(case, "EO-ADDR-01.01", "save_customer_shipping_address returned success", bool(save_result.get("success")), expected=True, actual=save_result)
        self._assert(case, "EO-ADDR-01.02", "A selected address name was returned", bool(selected_address_name), expected="non-empty", actual=selected_address_name)
        self._assert(case, "EO-ADDR-01.03", "Address count increased after adding a new address", len(linked_after_save) >= len(linked_before) + 1, expected=f">={len(linked_before) + 1}", actual=len(linked_after_save))
        self._assert(case, "EO-ADDR-01.04", "Address outbound after save used event processing", sync_after_save.get("mode") == "event", expected="event", actual=sync_after_save.get("mode"), concern=True)
        self._assert(case, "EO-ADDR-01.05", "Woo shipping address reflects saved address", ((woo_after_save or {}).get("shipping") or {}).get("address_1") == save_address_line, expected=save_address_line, actual=((woo_after_save or {}).get("shipping") or {}).get("address_1"))
        self._assert(case, "EO-ADDR-01.06", "Address count is stable after updating the same address", len(linked_after_update) == len(linked_after_save), expected=len(linked_after_save), actual=len(linked_after_update))
        self._assert(case, "EO-ADDR-01.07", "Address outbound after update used event processing", sync_after_update.get("mode") == "event", expected="event", actual=sync_after_update.get("mode"), concern=True)
        self._assert(case, "EO-ADDR-01.08", "Woo shipping address reflects updated address", ((woo_after_update or {}).get("shipping") or {}).get("address_1") == updated_address_line, expected=updated_address_line, actual=((woo_after_update or {}).get("shipping") or {}).get("address_1"))
        self._assert(case, "EO-ADDR-01.09", "Woo shipping city reflects updated city", ((woo_after_update or {}).get("shipping") or {}).get("city") == fixture["territory_name"], expected=fixture["territory_name"], actual=((woo_after_update or {}).get("shipping") or {}).get("city"))
        self._assert(case, "EO-ADDR-01.10", "Woo shipping postcode reflects updated postcode", ((woo_after_update or {}).get("shipping") or {}).get("postcode") == "STG001", expected="STG001", actual=((woo_after_update or {}).get("shipping") or {}).get("postcode"))

        self.runtime_state["customer"]["secondary_address"] = updated_address_line
        self.runtime_state["customer"]["secondary_address_name"] = selected_address_name
        self._record_created("Address", selected_address_name, note="EO-ADDR-01 synthetic secondary address")

        return {
            "linked_before": linked_before,
            "save_result": save_result,
            "linked_after_save": linked_after_save,
            "sync_after_save": sync_after_save,
            "woo_after_save": woo_after_save,
            "update_result": update_result,
            "linked_after_update": linked_after_update,
            "sync_after_update": sync_after_update,
            "woo_after_update": woo_after_update,
        }

    def _cross_customer_round_trip(self, case: dict[str, Any]) -> dict[str, Any]:
        runtime_customer = self.runtime_state.get("customer") or {}
        customer_name = str(runtime_customer.get("customer_name") or "")
        woo_customer_id = str(runtime_customer.get("woo_customer_id") or self._customer_woo_id(customer_name) or "")
        if not customer_name or not woo_customer_id:
            self._assert(
                case,
                "X-CUST-01.00",
                "EO-CUST-01 prerequisite succeeded",
                False,
                expected=True,
                actual=runtime_customer,
                concern=True,
            )
            return {"prerequisite": "EO-CUST-01"}

        case_started = now_datetime()
        fixture = dict(runtime_customer.get("territory") or self._primary_territory_fixture())
        duplicate_before = self._count_customers_by_woo_customer_id(woo_customer_id)
        slug = re.sub(r"[^a-z0-9]+", "", self.run_id.lower())
        updated_first_name = "RoundTrip"
        updated_last_name = self.run_id
        updated_email = f"roundtrip.{slug}@orderjarz.local"
        updated_phone = f"012{''.join(ch for ch in self.run_id if ch.isdigit())[-8:].rjust(8, '0')}"
        billing_line1 = f"{self.run_id} RT Billing"
        shipping_line1 = f"{self.run_id} RT Shipping"

        update_payload = self._build_woo_customer_payload(
            first_name=updated_first_name,
            last_name=updated_last_name,
            email=updated_email,
            phone=updated_phone,
            billing_line1=billing_line1,
            shipping_line1=shipping_line1,
            territory_fixture=fixture,
            billing_postcode="XCB001",
            shipping_postcode="XCS001",
        )
        updated_woo_customer = self._woo_client().put(f"customers/{woo_customer_id}", update_payload)
        webhook = self._post_signed_woo_webhook(
            api_method="jarz_woocommerce_integration.api.webhooks.woo_customer_webhook",
            payload=updated_woo_customer,
            topic="customer.updated",
        )
        inbound_sync = self._ensure_inbound_event_processed(
            object_type="Customer",
            source_id=woo_customer_id,
            event_name=str((webhook.get("payload") or {}).get("event_name") or ""),
        )

        refreshed_customer_name = self._find_customer_by_woo_customer_id(woo_customer_id)
        customer_doc = frappe.get_doc("Customer", refreshed_customer_name) if refreshed_customer_name else None
        addresses = self._customer_addresses(refreshed_customer_name) if refreshed_customer_name else []
        billing_address = self._find_customer_address(addresses, address_type="Billing", address_line1=billing_line1)
        shipping_address = self._find_customer_address(addresses, address_type="Shipping", address_line1=shipping_line1)
        duplicate_count = self._count_customers_by_woo_customer_id(woo_customer_id)
        latest_outbound_event = (
            self._latest_sync_event(
                direction="Outbound",
                object_type="Customer",
                source_id=refreshed_customer_name,
                created_after=case_started,
            )
            if refreshed_customer_name
            else None
        )

        self._assert(case, "X-CUST-01.01", "Round-trip customer webhook queued successfully", webhook.get("status_code") == 200 and bool((webhook.get("payload") or {}).get("queued")), expected={"status_code": 200, "queued": True}, actual=webhook)
        self._assert(case, "X-CUST-01.02", "Round-trip inbound customer event reaches Succeeded", str(((inbound_sync.get("latest_event") or {}).get("status") or "")) == "Succeeded", expected="Succeeded", actual=inbound_sync)
        self._assert(case, "X-CUST-01.03", "Round-trip update keeps the same ERP customer record", refreshed_customer_name == customer_name, expected=customer_name, actual=refreshed_customer_name)
        self._assert(case, "X-CUST-01.04", "Round-trip update keeps ERP customer binding count stable", duplicate_count == duplicate_before, expected=duplicate_before, actual={"before": duplicate_before, "after": duplicate_count})
        self._assert(case, "X-CUST-01.05", "ERP customer display name reflects the Woo round-trip update", str(getattr(customer_doc, "customer_name", "") or "") == f"{updated_first_name} {updated_last_name}", expected=f"{updated_first_name} {updated_last_name}", actual=getattr(customer_doc, "customer_name", None) if customer_doc else None)
        self._assert(case, "X-CUST-01.06", "ERP customer mobile reflects the Woo round-trip update", str(getattr(customer_doc, "mobile_no", "") or "") == updated_phone, expected=updated_phone, actual=getattr(customer_doc, "mobile_no", None) if customer_doc else None)
        self._assert(case, "X-CUST-01.07", "ERP customer email reflects the Woo round-trip update", str(getattr(customer_doc, "email_id", "") or "") == updated_email, expected=updated_email, actual=getattr(customer_doc, "email_id", None) if customer_doc else None)
        self._assert(case, "X-CUST-01.08", "Billing address reflects the Woo round-trip update", bool(billing_address and str(billing_address.get("pincode") or "") == "XCB001"), expected={"address_1": billing_line1, "pincode": "XCB001"}, actual=billing_address)
        self._assert(case, "X-CUST-01.09", "Shipping address reflects the Woo round-trip update", bool(shipping_address and str(shipping_address.get("pincode") or "") == "XCS001"), expected={"address_1": shipping_line1, "pincode": "XCS001"}, actual=shipping_address)
        self._assert(case, "X-CUST-01.10", "Customer territory reflects the Woo round-trip state", str(getattr(customer_doc, "territory", "") or "") == fixture["territory"], expected=fixture["territory"], actual=getattr(customer_doc, "territory", None) if customer_doc else None)
        self._assert(case, "X-CUST-01.11", "Round-trip customer update does not emit a same-case outbound event", latest_outbound_event is None, expected=None, actual=latest_outbound_event)

        if refreshed_customer_name:
            self.runtime_state["customer"].update({
                "mobile": updated_phone,
                "territory": fixture,
                "woo_customer_id": woo_customer_id,
            })
        if shipping_address:
            self.runtime_state["customer"]["secondary_address_name"] = str(shipping_address.get("name") or "")

        return {
            "update_payload": update_payload,
            "updated_woo_customer": updated_woo_customer,
            "webhook": webhook,
            "inbound_sync": inbound_sync,
            "customer": customer_doc.as_dict() if customer_doc else None,
            "addresses": addresses,
            "duplicate_before": duplicate_before,
            "duplicate_after": duplicate_count,
        }

    def _outbound_order_create(self, case: dict[str, Any]) -> dict[str, Any]:
        runtime_customer = self.runtime_state.get("customer") or {}
        customer_name = str(runtime_customer.get("customer_name") or "")
        invoice_run = self._create_and_sync_invoice(payment_method="Cash")
        result = invoice_run["create_result"]
        sync_result = invoice_run["sync_result"]
        invoice_name = str(invoice_run["invoice_name"] or "")
        invoice_doc = invoice_run["invoice_doc"]
        woo_order_id = str(invoice_run["woo_order_id"] or "")
        woo_order = invoice_run["woo_order"]
        order_map = invoice_run["order_map"]
        items = invoice_run["cart_items"]
        expected_signature = self._cart_signature(items)
        actual_signature = self._invoice_item_signature(invoice_doc)
        woo_signature = self._woo_order_item_signature(woo_order)
        woo_meta = {str(entry.get("key") or ""): entry.get("value") for entry in ((woo_order or {}).get("meta_data") or []) if entry.get("key")}
        order_map_link_field = self._order_map_link_field()

        self._assert(case, "EO-ORD-01.01", "Invoice creation returned success", bool(result.get("success")), expected=True, actual=result)
        self._assert(case, "EO-ORD-01.02", "Invoice was created in ERP", bool(invoice_name and frappe.db.exists("Sales Invoice", invoice_name)), expected=True, actual=invoice_name)
        self._assert(case, "EO-ORD-01.03", "Invoice is submitted", int(getattr(invoice_doc, "docstatus", 0) or 0) == 1, expected=1, actual=getattr(invoice_doc, "docstatus", 0))
        self._assert(case, "EO-ORD-01.04", "Invoice uses the expected customer", getattr(invoice_doc, "customer", "") == customer_name, expected=customer_name, actual=getattr(invoice_doc, "customer", ""))
        self._assert(case, "EO-ORD-01.05", "Invoice items match requested cart items", actual_signature == expected_signature, expected=expected_signature, actual=actual_signature)
        self._assert(case, "EO-ORD-01.06", "Invoice outbound path used event processing", sync_result.get("mode") == "event", expected="event", actual=sync_result.get("mode"), concern=True)
        self._assert(case, "EO-ORD-01.07", "Invoice has Woo order ID after sync", bool(woo_order_id), expected="non-empty", actual=woo_order_id)
        self._assert(case, "EO-ORD-01.08", "Invoice outbound status is Synced", getattr(invoice_doc, "woo_outbound_status", "") == "Synced", expected="Synced", actual=getattr(invoice_doc, "woo_outbound_status", ""))
        self._assert(case, "EO-ORD-01.09", "Woo order exists", isinstance(woo_order, dict) and bool(woo_order.get("id")), expected=True, actual=woo_order)
        self._assert(case, "EO-ORD-01.10", "Woo order line items match requested cart items", woo_signature == expected_signature, expected=expected_signature, actual=woo_signature)
        self._assert(case, "EO-ORD-01.11", "Woo order references the ERP invoice in meta", str(woo_meta.get("erpnext_sales_invoice") or "") == invoice_name, expected=invoice_name, actual=woo_meta.get("erpnext_sales_invoice"))
        self._assert(case, "EO-ORD-01.12", "Woo order customer matches the synced Woo customer", str((woo_order or {}).get("customer_id") or "") == str(runtime_customer.get("woo_customer_id") or self._customer_woo_id(customer_name)), expected=str(runtime_customer.get("woo_customer_id") or self._customer_woo_id(customer_name)), actual=(woo_order or {}).get("customer_id"))
        self._assert(case, "EO-ORD-01.13", "Woo order map exists for outbound-created order", bool(order_map), expected=True, actual=order_map, concern=True)
        if order_map:
            self._assert(case, "EO-ORD-01.14", "Woo order map points to the invoice", str(order_map.get(order_map_link_field) or "") == invoice_name, expected=invoice_name, actual=order_map)

        self.runtime_state["invoice"] = {
            "invoice_name": invoice_name,
            "woo_order_id": woo_order_id,
            "shipping_address_name": invoice_run["shipping_address_name"],
            "cart_items": items,
        }
        self._record_created("Sales Invoice", invoice_name, note="EO-ORD-01 synthetic invoice")
        if woo_order_id:
            self._record_created("Woo Order", woo_order_id, note="EO-ORD-01 outbound order")

        return {
            "create_result": result,
            "sync_result": sync_result,
            "invoice": invoice_doc.as_dict(),
            "woo_order": woo_order,
            "order_map": order_map,
            "delivery_slot": invoice_run["delivery_slot"],
            "cart_items": items,
        }

    def _cross_order_round_trip(self, case: dict[str, Any]) -> dict[str, Any]:
        runtime_invoice = dict(self.runtime_state.get("invoice") or {})
        woo_order_id = str(runtime_invoice.get("woo_order_id") or "")
        source_invoice_name = str(runtime_invoice.get("invoice_name") or "")
        if not woo_order_id or not source_invoice_name:
            self._assert(
                case,
                "X-ORD-01.00",
                "EO-ORD-01 prerequisite succeeded",
                False,
                expected=True,
                actual=runtime_invoice,
                concern=True,
            )
            return {"prerequisite": "EO-ORD-01"}

        current_order = dict(self._woo_order(woo_order_id) or {})
        line_items = [dict(row) for row in (current_order.get("line_items") or [])]
        if len(line_items) < 2:
            raise RuntimeError(f"X-ORD-01 requires at least two Woo line items: {current_order!r}")

        first_line = dict(line_items[0])
        first_line_id = first_line.get("id")
        first_line_qty = int(first_line.get("quantity") or 0)
        if not first_line_id or first_line_qty < 1:
            raise RuntimeError(f"X-ORD-01 could not resolve the first Woo line item id/qty: {first_line!r}")

        amended_items = [dict(row) for row in (runtime_invoice.get("cart_items") or [])]
        if not amended_items:
            raise RuntimeError(f"X-ORD-01 missing cached cart items for {source_invoice_name}")
        amended_items[0]["qty"] = float(amended_items[0].get("qty", 1) or 1) + 1

        update_payload = {
            "status": "processing",
            "line_items": [
                {
                    "id": first_line_id,
                    "quantity": first_line_qty + 1,
                }
            ],
        }
        updated_woo_order = self._woo_client().put(f"orders/{woo_order_id}", update_payload)
        expected_signature = self._cart_signature(amended_items)

        webhook = self._post_signed_woo_webhook(
            api_method="jarz_woocommerce_integration.api.orders.woo_order_webhook",
            payload=updated_woo_order,
            topic="order.updated",
        )
        inbound_sync = self._ensure_inbound_event_processed(
            object_type="Order",
            source_id=woo_order_id,
            event_name=str((webhook.get("payload") or {}).get("event_name") or ""),
        )

        process_result = dict(inbound_sync.get("process_result") or {})
        queue_result = dict(process_result.get("result") or {})
        latest_event = dict(inbound_sync.get("latest_event") or {})
        amendment_wait = self._wait_for_inbound_amendment(source_invoice_name, woo_order_id, timeout_seconds=45)
        replacement_invoice_name = str(amendment_wait.get("replacement_invoice_name") or "")
        replacement_invoice = amendment_wait.get("replacement_invoice_doc")
        active_invoices = amendment_wait.get("active_invoices") or []
        linked_invoice_names = [str(row.get("name") or "") for row in active_invoices]
        order_map = amendment_wait.get("order_map")
        order_map_link_field = self._order_map_link_field()
        source_invoice_doc = frappe.get_doc("Sales Invoice", source_invoice_name)
        replacement_signature = self._invoice_item_signature(replacement_invoice) if replacement_invoice else []

        self._assert(case, "X-ORD-01.01", "Woo round-trip order update returns the same order id", str(updated_woo_order.get("id") or "") == woo_order_id, expected=woo_order_id, actual=updated_woo_order)
        self._assert(case, "X-ORD-01.02", "Woo round-trip order line items reflect the edit", replacement_signature == expected_signature if replacement_invoice else False, expected=expected_signature, actual=replacement_signature)
        self._assert(case, "X-ORD-01.03", "Woo round-trip order webhook queued successfully", webhook.get("status_code") == 200 and bool((webhook.get("payload") or {}).get("queued")), expected={"status_code": 200, "queued": True}, actual=webhook)
        self._assert(case, "X-ORD-01.04", "Woo round-trip inbound order event exists", bool(inbound_sync.get("event_name")), expected="non-empty", actual=inbound_sync)
        self._assert(case, "X-ORD-01.05", "Woo round-trip edit enqueues amendment handling", str(queue_result.get("status") or "").strip().lower() == "queued" and str(queue_result.get("reason") or "").strip().lower() == "amendment_enqueued", expected={"status": "queued", "reason": "amendment_enqueued"}, actual={"latest_event": latest_event, "process_result": process_result, "queue_result": queue_result})
        self._assert(case, "X-ORD-01.06", "ERP-originated source invoice is cancelled after the Woo edit", int(getattr(source_invoice_doc, "docstatus", 0) or 0) == 2, expected=2, actual=getattr(source_invoice_doc, "docstatus", 0))
        self._assert(case, "X-ORD-01.07", "ERP-originated replacement invoice is created", bool(replacement_invoice_name and replacement_invoice), expected="non-empty", actual=amendment_wait)
        self._assert(case, "X-ORD-01.08", "ERP-originated replacement invoice preserves amended_from linkage", str(getattr(replacement_invoice, "amended_from", "") or "") == source_invoice_name, expected=source_invoice_name, actual=getattr(replacement_invoice, "amended_from", None) if replacement_invoice else None)
        self._assert(case, "X-ORD-01.09", "Only one active ERP invoice remains linked after the Woo round-trip edit", linked_invoice_names == ([replacement_invoice_name] if replacement_invoice_name else []), expected=[replacement_invoice_name] if replacement_invoice_name else ["non-empty"], actual=linked_invoice_names)
        self._assert(case, "X-ORD-01.10", "Woo order map relinks to the ERP-originated replacement invoice", bool(order_map and str(order_map.get(order_map_link_field) or "") == replacement_invoice_name), expected=replacement_invoice_name, actual=order_map)

        if replacement_invoice_name:
            self._record_created("Sales Invoice", replacement_invoice_name, note="X-ORD-01 replacement invoice")
            self.runtime_state["invoice"] = {
                "invoice_name": replacement_invoice_name,
                "woo_order_id": woo_order_id,
                "shipping_address_name": runtime_invoice.get("shipping_address_name"),
                "cart_items": amended_items,
            }

        return {
            "update_payload": update_payload,
            "updated_woo_order": updated_woo_order,
            "webhook": webhook,
            "inbound_sync": inbound_sync,
            "amendment_wait": amendment_wait,
            "order_map": order_map,
        }

    def _outbound_payment(self, case: dict[str, Any]) -> dict[str, Any]:
        from jarz_pos.api.invoices import pay_invoice

        fixture = self._primary_territory_fixture()
        available_modes = {str(name or "").strip().lower() for name in (self.fixture_catalog.get("payment_modes") or [])}
        online_mode = "instapay" if "instapay" in available_modes else "wallet"
        online_payment_method = "Instapay" if online_mode == "instapay" else "Mobile Wallet"

        cash_run = self._create_and_sync_invoice(payment_method="Cash")
        cash_pay_result = pay_invoice(
            invoice_name=cash_run["invoice_name"],
            payment_mode="cash",
            pos_profile=fixture["pos_profile"],
        )
        frappe.db.commit()
        cash_sync = self._ensure_invoice_synced_to_woo(cash_run["invoice_name"])
        cash_invoice = frappe.get_doc("Sales Invoice", cash_run["invoice_name"])
        cash_payment_entry = str(cash_pay_result.get("payment_entry") or "")
        cash_payment_doc = frappe.get_doc("Payment Entry", cash_payment_entry) if cash_payment_entry else None
        cash_woo_order = self._woo_order(str(getattr(cash_invoice, "woo_order_id", "") or ""))

        online_run = self._create_and_sync_invoice(payment_method=online_payment_method)
        online_pay_result = pay_invoice(
            invoice_name=online_run["invoice_name"],
            payment_mode=online_mode,
        )
        frappe.db.commit()
        online_sync = self._ensure_invoice_synced_to_woo(online_run["invoice_name"])
        online_invoice = frappe.get_doc("Sales Invoice", online_run["invoice_name"])
        online_payment_entry = str(online_pay_result.get("payment_entry") or "")
        online_payment_doc = frappe.get_doc("Payment Entry", online_payment_entry) if online_payment_entry else None
        online_woo_order = self._woo_order(str(getattr(online_invoice, "woo_order_id", "") or ""))

        self._assert(case, "EO-PAY-01.01", "Cash payment API succeeds", bool(cash_pay_result.get("success")), expected=True, actual=cash_pay_result)
        self._assert(case, "EO-PAY-01.02", "Cash payment entry exists", bool(cash_payment_entry and frappe.db.exists("Payment Entry", cash_payment_entry)), expected=True, actual=cash_payment_entry)
        self._assert(case, "EO-PAY-01.03", "Cash payment entry is submitted", int(getattr(cash_payment_doc, "docstatus", 0) or 0) == 1, expected=1, actual=getattr(cash_payment_doc, "docstatus", 0) if cash_payment_doc else None)
        self._assert(case, "EO-PAY-01.04", "Cash invoice is fully paid", float(getattr(cash_invoice, "outstanding_amount", 0) or 0) <= 0.01, expected="<=0.01", actual=getattr(cash_invoice, "outstanding_amount", 0))
        self._assert(case, "EO-PAY-01.05", "Cash payment outbound path used event processing", cash_sync.get("mode") == "event", expected="event", actual=cash_sync.get("mode"), concern=True)
        self._assert(case, "EO-PAY-01.06", "Cash Woo order is marked paid", bool((cash_woo_order or {}).get("date_paid") or (cash_woo_order or {}).get("date_paid_gmt")), expected=True, actual={"date_paid": (cash_woo_order or {}).get("date_paid"), "date_paid_gmt": (cash_woo_order or {}).get("date_paid_gmt")}, concern=True)
        self._assert(case, "EO-PAY-01.07", "Cash Woo order payment title is Cash", "cash" in str((cash_woo_order or {}).get("payment_method_title") or "").lower(), expected="contains cash", actual=(cash_woo_order or {}).get("payment_method_title"))

        self._assert(case, "EO-PAY-01.08", "Online payment API succeeds", bool(online_pay_result.get("success")), expected=True, actual=online_pay_result)
        self._assert(case, "EO-PAY-01.09", "Online payment entry exists", bool(online_payment_entry and frappe.db.exists("Payment Entry", online_payment_entry)), expected=True, actual=online_payment_entry)
        self._assert(case, "EO-PAY-01.10", "Online payment entry is submitted", int(getattr(online_payment_doc, "docstatus", 0) or 0) == 1, expected=1, actual=getattr(online_payment_doc, "docstatus", 0) if online_payment_doc else None)
        self._assert(case, "EO-PAY-01.11", "Online invoice is fully paid", float(getattr(online_invoice, "outstanding_amount", 0) or 0) <= 0.01, expected="<=0.01", actual=getattr(online_invoice, "outstanding_amount", 0))
        self._assert(case, "EO-PAY-01.12", "Online payment outbound path used event processing", online_sync.get("mode") == "event", expected="event", actual=online_sync.get("mode"), concern=True)
        self._assert(case, "EO-PAY-01.13", "Online Woo order is marked paid", bool((online_woo_order or {}).get("date_paid") or (online_woo_order or {}).get("date_paid_gmt")), expected=True, actual={"date_paid": (online_woo_order or {}).get("date_paid"), "date_paid_gmt": (online_woo_order or {}).get("date_paid_gmt")}, concern=True)
        self._assert(case, "EO-PAY-01.14", "Online Woo payment method matches expected mode", str((online_woo_order or {}).get("payment_method") or "") == ("instapay" if online_mode == "instapay" else "wallet"), expected=("instapay" if online_mode == "instapay" else "wallet"), actual=(online_woo_order or {}).get("payment_method"))

        if cash_payment_entry:
            self._record_created("Payment Entry", cash_payment_entry, note="EO-PAY-01 cash payment")
        if online_payment_entry:
            self._record_created("Payment Entry", online_payment_entry, note=f"EO-PAY-01 {online_mode} payment")

        return {
            "cash": {
                "invoice_name": cash_run["invoice_name"],
                "pay_result": cash_pay_result,
                "sync_result": cash_sync,
                "invoice": cash_invoice.as_dict(),
                "payment_entry": cash_payment_doc.as_dict() if cash_payment_doc else None,
                "woo_order": cash_woo_order,
            },
            "online": {
                "invoice_name": online_run["invoice_name"],
                "payment_mode": online_mode,
                "pay_result": online_pay_result,
                "sync_result": online_sync,
                "invoice": online_invoice.as_dict(),
                "payment_entry": online_payment_doc.as_dict() if online_payment_doc else None,
                "woo_order": online_woo_order,
            },
        }

    def _outbound_amendment(self, case: dict[str, Any]) -> dict[str, Any]:
        from jarz_pos.api.manager import submit_invoice_amendment

        source_run = self._create_and_sync_invoice(payment_method="Cash")
        source_invoice = source_run["invoice_doc"]
        fixture = source_run["fixture"]
        amended_items = [dict(source_run["cart_items"][0]), dict(source_run["cart_items"][1])]
        amended_items[0]["qty"] = 2

        amendment_result = submit_invoice_amendment(
            invoice_id=source_run["invoice_name"],
            cart_json=self._build_cart_json(amended_items),
            customer_name=str(source_invoice.get("customer") or ""),
            shipping_address_name=source_run["shipping_address_name"],
            pos_profile_name=fixture["pos_profile"],
            required_delivery_datetime=source_run["delivery_slot"]["required_delivery_datetime"],
            payment_method="Cash",
            expected_source_grand_total=float(getattr(source_invoice, "grand_total", 0) or 0),
            expected_source_item_count=len(source_run["cart_items"]),
        )
        frappe.db.commit()

        replacement_invoice_name = str(
            amendment_result.get("replacement_invoice_id")
            or ((amendment_result.get("invoice") or {}).get("name") if isinstance(amendment_result.get("invoice"), dict) else "")
            or self._replacement_invoice_name(source_run["invoice_name"])
            or ""
        )
        source_after = frappe.get_doc("Sales Invoice", source_run["invoice_name"])

        self._assert(case, "EO-AMEND-01.01", "Invoice amendment API succeeds", bool(amendment_result.get("success")), expected=True, actual=amendment_result)
        self._assert(case, "EO-AMEND-01.02", "Replacement invoice ID is returned or discoverable", bool(replacement_invoice_name), expected="non-empty", actual={"amendment_result": amendment_result, "discovered_replacement_invoice": replacement_invoice_name})
        if not replacement_invoice_name:
            return {
                "source_invoice_name": source_run["invoice_name"],
                "amendment_result": amendment_result,
                "source_invoice": source_after.as_dict(),
                "cart_items": amended_items,
            }

        replacement_sync = self._ensure_invoice_synced_to_woo(replacement_invoice_name)
        replacement_invoice = frappe.get_doc("Sales Invoice", replacement_invoice_name)
        replacement_woo_order_id = str(getattr(replacement_invoice, "woo_order_id", "") or source_run["woo_order_id"] or "")
        replacement_woo_order = self._woo_order(replacement_woo_order_id) if replacement_woo_order_id else None
        replacement_order_map = self._order_map_row(replacement_woo_order_id)
        replacement_meta = {str(entry.get("key") or ""): entry.get("value") for entry in ((replacement_woo_order or {}).get("meta_data") or []) if entry.get("key")}
        order_map_link_field = self._order_map_link_field()
        expected_signature = self._cart_signature(amended_items)

        self._assert(case, "EO-AMEND-01.03", "Source invoice is cancelled", int(getattr(source_after, "docstatus", 0) or 0) == 2, expected=2, actual=getattr(source_after, "docstatus", 0))
        self._assert(case, "EO-AMEND-01.04", "Replacement invoice is submitted", int(getattr(replacement_invoice, "docstatus", 0) or 0) == 1, expected=1, actual=getattr(replacement_invoice, "docstatus", 0))
        self._assert(case, "EO-AMEND-01.05", "Replacement invoice preserves amended_from linkage", str(getattr(replacement_invoice, "amended_from", "") or "") == source_run["invoice_name"], expected=source_run["invoice_name"], actual=getattr(replacement_invoice, "amended_from", ""))
        self._assert(case, "EO-AMEND-01.06", "Replacement invoice keeps Woo order linkage", bool(replacement_woo_order_id), expected="non-empty", actual=replacement_woo_order_id)
        self._assert(case, "EO-AMEND-01.07", "Replacement invoice items match amended cart", self._invoice_item_signature(replacement_invoice) == expected_signature, expected=expected_signature, actual=self._invoice_item_signature(replacement_invoice))
        self._assert(case, "EO-AMEND-01.08", "Replacement outbound path used event processing", replacement_sync.get("mode") == "event", expected="event", actual=replacement_sync.get("mode"), concern=True)
        self._assert(case, "EO-AMEND-01.09", "Woo order references the replacement invoice", str(replacement_meta.get("erpnext_sales_invoice") or "") == replacement_invoice_name, expected=replacement_invoice_name, actual=replacement_meta.get("erpnext_sales_invoice"))
        self._assert(case, "EO-AMEND-01.10", "Woo order items match amended cart", self._woo_order_item_signature(replacement_woo_order) == expected_signature, expected=expected_signature, actual=self._woo_order_item_signature(replacement_woo_order))
        self._assert(case, "EO-AMEND-01.11", "Woo order map points to the replacement invoice", bool(replacement_order_map and str(replacement_order_map.get(order_map_link_field) or "") == replacement_invoice_name), expected=replacement_invoice_name, actual=replacement_order_map, concern=True)

        self._record_created("Sales Invoice", replacement_invoice_name, note="EO-AMEND-01 replacement invoice")

        return {
            "source_invoice_name": source_run["invoice_name"],
            "replacement_invoice_name": replacement_invoice_name,
            "amendment_result": amendment_result,
            "replacement_sync": replacement_sync,
            "source_invoice": source_after.as_dict(),
            "replacement_invoice": replacement_invoice.as_dict(),
            "woo_order": replacement_woo_order,
            "order_map": replacement_order_map,
            "cart_items": amended_items,
        }

    def _outbound_state_transition(self, case: dict[str, Any]) -> dict[str, Any]:
        from jarz_pos.api.kanban import update_invoice_state

        fixture = self._primary_territory_fixture()
        state_items = self._order_fixture_items(
            fixture["price_list"],
            warehouse=str(fixture.get("warehouse") or ""),
            require_stock=True,
        )
        state_run = self._create_and_sync_invoice(payment_method="Cash", cart_items=state_items)
        invoice_name = state_run["invoice_name"]

        ofd_result = update_invoice_state(invoice_id=invoice_name, new_state="Out for Delivery")
        frappe.db.commit()
        ofd_sync = self._ensure_invoice_synced_to_woo(invoice_name)
        invoice_after_ofd = frappe.get_doc("Sales Invoice", invoice_name)
        ofd_woo_order = self._woo_order(str(getattr(invoice_after_ofd, "woo_order_id", "") or ""))
        delivery_note_name = str(ofd_result.get("delivery_note") or "")

        delivered_result = update_invoice_state(invoice_id=invoice_name, new_state="Delivered")
        frappe.db.commit()
        delivered_sync = self._ensure_invoice_synced_to_woo(invoice_name)
        invoice_after_delivered = frappe.get_doc("Sales Invoice", invoice_name)
        delivered_woo_order = self._woo_order(str(getattr(invoice_after_delivered, "woo_order_id", "") or ""))

        self._assert(case, "EO-STATE-01.01", "Out-for-delivery transition succeeds", bool(ofd_result.get("success")), expected=True, actual=ofd_result)
        self._assert(case, "EO-STATE-01.02", "Delivery Note is created on out-for-delivery", bool(delivery_note_name and frappe.db.exists("Delivery Note", delivery_note_name)), expected=True, actual=delivery_note_name)
        self._assert(case, "EO-STATE-01.03", "Invoice state is out-for-delivery after first transition", self._invoice_state_key(invoice_after_ofd) == "out-for-delivery", expected="out-for-delivery", actual=self._invoice_state_key(invoice_after_ofd))
        self._assert(case, "EO-STATE-01.04", "Out-for-delivery outbound path used event processing", ofd_sync.get("mode") == "event", expected="event", actual=ofd_sync.get("mode"), concern=True)
        self._assert(case, "EO-STATE-01.05", "Woo order status is out-for-delivery", str((ofd_woo_order or {}).get("status") or "") == "out-for-delivery", expected="out-for-delivery", actual=(ofd_woo_order or {}).get("status"))

        self._assert(case, "EO-STATE-01.06", "Delivered transition succeeds", bool(delivered_result.get("success")), expected=True, actual=delivered_result)
        self._assert(case, "EO-STATE-01.07", "Invoice state is delivered after second transition", self._invoice_state_key(invoice_after_delivered) in {"delivered", "completed"}, expected="delivered/completed", actual=self._invoice_state_key(invoice_after_delivered))
        self._assert(case, "EO-STATE-01.08", "Delivered outbound path used event processing", delivered_sync.get("mode") == "event", expected="event", actual=delivered_sync.get("mode"), concern=True)
        self._assert(case, "EO-STATE-01.09", "Woo order status is completed after delivery", str((delivered_woo_order or {}).get("status") or "") == "completed", expected="completed", actual=(delivered_woo_order or {}).get("status"))

        if delivery_note_name:
            self._record_created("Delivery Note", delivery_note_name, note="EO-STATE-01 delivery note")

        return {
            "invoice_name": invoice_name,
            "cart_items": state_run["cart_items"],
            "out_for_delivery": {
                "result": ofd_result,
                "sync_result": ofd_sync,
                "invoice": invoice_after_ofd.as_dict(),
                "woo_order": ofd_woo_order,
            },
            "delivered": {
                "result": delivered_result,
                "sync_result": delivered_sync,
                "invoice": invoice_after_delivered.as_dict(),
                "woo_order": delivered_woo_order,
            },
        }

    def _outbound_cancel(self, case: dict[str, Any]) -> dict[str, Any]:
        from jarz_pos.api.kanban import cancel_invoice

        cancel_run = self._create_and_sync_invoice(payment_method="Cash")
        invoice_name = cancel_run["invoice_name"]
        cancel_result = cancel_invoice(
            invoice_id=invoice_name,
            reason="Copilot staging full-cycle cancellation",
            notes=self.run_id,
        )
        frappe.db.commit()
        cancel_sync = self._ensure_invoice_synced_to_woo(invoice_name, cancel=True)
        cancelled_invoice = frappe.get_doc("Sales Invoice", invoice_name)
        cancelled_woo_order = self._woo_order(str(getattr(cancelled_invoice, "woo_order_id", "") or ""))

        self._assert(case, "EO-CANCEL-01.01", "Invoice cancellation API succeeds", bool(cancel_result.get("success")), expected=True, actual=cancel_result)
        self._assert(case, "EO-CANCEL-01.02", "Cancelled invoice docstatus is 2", int(getattr(cancelled_invoice, "docstatus", 0) or 0) == 2, expected=2, actual=getattr(cancelled_invoice, "docstatus", 0))
        self._assert(case, "EO-CANCEL-01.03", "Cancelled invoice state is cancelled", self._invoice_state_key(cancelled_invoice) == "cancelled", expected="cancelled", actual=self._invoice_state_key(cancelled_invoice))
        self._assert(case, "EO-CANCEL-01.04", "Cancellation outbound path used event processing", cancel_sync.get("mode") == "event", expected="event", actual=cancel_sync.get("mode"), concern=True)
        self._assert(case, "EO-CANCEL-01.05", "Woo order status is cancelled", str((cancelled_woo_order or {}).get("status") or "") == "cancelled", expected="cancelled", actual=(cancelled_woo_order or {}).get("status"))

        return {
            "invoice_name": invoice_name,
            "cancel_result": cancel_result,
            "sync_result": cancel_sync,
            "invoice": cancelled_invoice.as_dict(),
            "woo_order": cancelled_woo_order,
        }

    def _primary_territory_fixture(self) -> dict[str, Any]:
        territories = list(self.fixture_catalog.get("territories") or [])
        if not territories:
            raise RuntimeError("No territory fixtures are available")
        return dict(territories[0])

    def _secondary_territory_fixture(self) -> dict[str, Any]:
        territories = list(self.fixture_catalog.get("territories") or [])
        if len(territories) >= 2:
            return dict(territories[1])
        return self._primary_territory_fixture()

    def _default_shipping_address_name(self, customer_name: str) -> str | None:
        runtime_customer = self.runtime_state.get("customer") or {}
        if runtime_customer.get("secondary_address_name"):
            return str(runtime_customer["secondary_address_name"])
        if runtime_customer.get("customer_primary_address"):
            return str(runtime_customer["customer_primary_address"])
        addresses = self._customer_addresses(customer_name)
        return str(addresses[0]["name"]) if addresses else None

    def _order_fixture_items(
        self,
        price_list: str,
        *,
        warehouse: str | None = None,
        require_stock: bool = False,
    ) -> list[dict[str, Any]]:
        return self._select_order_fixture_items(
            price_list,
            warehouse=warehouse,
            require_stock=require_stock,
        )

    def _select_order_fixture_items(
        self,
        price_list: str,
        *,
        warehouse: str | None = None,
        require_stock: bool = False,
    ) -> list[dict[str, Any]]:
        if require_stock and warehouse:
            stocked = self._stocked_order_fixture_items(price_list=price_list, warehouse=warehouse)
            if len(stocked) >= 2:
                return stocked[:2]

        all_items = list(self.fixture_catalog.get("items") or [])
        matching = [dict(row) for row in all_items if row.get("price_list") == price_list]
        if len(matching) >= 2:
            return matching[:2]
        if len(all_items) >= 2:
            return [dict(all_items[0]), dict(all_items[1])]
        raise RuntimeError("At least two item fixtures are required for EO-ORD-01")

    def _stocked_order_fixture_items(self, *, price_list: str, warehouse: str) -> list[dict[str, Any]]:
        rows = frappe.db.sql(
            """
            SELECT DISTINCT
                   i.name AS item_code,
                   i.item_name,
                   i.woo_product_id,
                   i.woo_variation_id,
                   ip.price_list,
                   ip.price_list_rate,
                   b.actual_qty
            FROM `tabBin` b
            INNER JOIN `tabItem` i ON i.name = b.item_code
            INNER JOIN `tabItem Price` ip ON ip.item_code = i.name
            WHERE b.warehouse = %(warehouse)s
              AND IFNULL(b.actual_qty, 0) >= 1
              AND IFNULL(i.disabled, 0) = 0
              AND IFNULL(i.woo_product_id, '') != ''
              AND ip.selling = 1
              AND ip.price_list = %(price_list)s
              AND IFNULL(ip.price_list_rate, 0) > 0
            ORDER BY b.actual_qty DESC, i.modified DESC
            LIMIT 10
            """,
            {"warehouse": warehouse, "price_list": price_list},
            as_dict=True,
        )
        return [dict(row) for row in rows]

    def _build_cart_json(self, items: list[dict[str, Any]]) -> str:
        return json.dumps([
            {
                "item_code": row["item_code"],
                "item_name": row.get("item_name"),
                "qty": float(row.get("qty", 1) or 1),
                "rate": float(row.get("rate") or row["price_list_rate"]),
                "price_list_rate": float(row["price_list_rate"]),
            }
            for row in items
        ])

    def _create_and_sync_invoice(
        self,
        *,
        cart_items: list[dict[str, Any]] | None = None,
        shipping_address_name: str | None = None,
        payment_method: str = "Cash",
    ) -> dict[str, Any]:
        from jarz_pos.services.invoice_creation import create_pos_invoice

        runtime_customer = self.runtime_state.get("customer") or {}
        customer_name = str(runtime_customer.get("customer_name") or "")
        if not customer_name:
            raise RuntimeError("EO-CUST-01 must run successfully before invoice lifecycle cases")

        fixture = self._primary_territory_fixture()
        items = [dict(row) for row in (cart_items or self._order_fixture_items(fixture["price_list"]))]
        delivery_slot = dict(self.fixture_catalog.get("next_delivery_slot") or _next_delivery_slot())
        selected_shipping_address_name = shipping_address_name or self._default_shipping_address_name(customer_name)

        result = create_pos_invoice(
            cart_json=self._build_cart_json(items),
            customer_name=customer_name,
            pos_profile_name=fixture["pos_profile"],
            required_delivery_datetime=delivery_slot["required_delivery_datetime"],
            shipping_address_name=selected_shipping_address_name,
            payment_method=payment_method,
        )
        frappe.db.commit()

        invoice_name = str(result.get("invoice_name") or "")
        invoice_doc = frappe.get_doc("Sales Invoice", invoice_name)
        sync_result = self._ensure_invoice_synced_to_woo(invoice_name)
        frappe.db.commit()
        invoice_doc = frappe.get_doc("Sales Invoice", invoice_name)
        woo_order_id = str(getattr(invoice_doc, "woo_order_id", "") or "")
        woo_order = self._woo_order(woo_order_id) if woo_order_id else None
        order_map = self._order_map_row(woo_order_id)

        return {
            "fixture": fixture,
            "cart_items": items,
            "delivery_slot": delivery_slot,
            "shipping_address_name": selected_shipping_address_name,
            "create_result": result,
            "sync_result": sync_result,
            "invoice_name": invoice_name,
            "invoice_doc": invoice_doc,
            "woo_order_id": woo_order_id,
            "woo_order": woo_order,
            "order_map": order_map,
        }

    def _cart_signature(self, items: list[dict[str, Any]]) -> list[tuple[str, float]]:
        return sorted(
            (
                str(row.get("item_code") or ""),
                float(row.get("qty", 1) or 1),
            )
            for row in items
        )

    def _invoice_item_signature(self, invoice_doc) -> list[tuple[str, float]]:
        return sorted(
            (
                str(getattr(item, "item_code", "") or ""),
                float(getattr(item, "qty", 0) or 0),
            )
            for item in getattr(invoice_doc, "items", []) or []
        )

    def _woo_order_item_signature(self, woo_order: dict[str, Any] | None) -> list[tuple[str, float]]:
        signature: list[tuple[str, float]] = []
        for line in (woo_order or {}).get("line_items") or []:
            item_code = str(line.get("name") or "")
            for meta in line.get("meta_data") or []:
                if str(meta.get("key") or "") == "erpnext_item_code":
                    item_code = str(meta.get("value") or item_code)
                    break
            if not item_code or item_code == str(line.get("name") or ""):
                resolved_item_code = self._item_code_for_woo_line(line)
                if resolved_item_code:
                    item_code = resolved_item_code
            signature.append((item_code, float(line.get("quantity") or 0)))
        return sorted(signature)

    def _item_code_for_woo_line(self, line: dict[str, Any] | None) -> str:
        variation_id = str((line or {}).get("variation_id") or "").strip()
        product_id = str((line or {}).get("product_id") or "").strip()

        if variation_id:
            item_code = frappe.db.get_value("Item", {"woo_variation_id": variation_id}, "name")
            if item_code:
                return str(item_code)
        if product_id:
            item_code = frappe.db.get_value("Item", {"woo_product_id": product_id}, "name")
            if item_code:
                return str(item_code)
        return ""

    def _invoice_state_key(self, invoice_doc) -> str:
        state_value = (
            getattr(invoice_doc, "custom_sales_invoice_state", None)
            or getattr(invoice_doc, "sales_invoice_state", None)
            or getattr(invoice_doc, "state", None)
            or ""
        )
        return re.sub(r"[\s_]+", "-", str(state_value or "").strip().lower())

    def _invoice_state_candidates(self, invoice_doc) -> list[str]:
        if not invoice_doc:
            return []
        states: list[str] = []
        seen: set[str] = set()
        for field_name in ("custom_sales_invoice_state", "sales_invoice_state", "state"):
            state_key = re.sub(r"[\s_]+", "-", str(getattr(invoice_doc, field_name, None) or "").strip().lower())
            if state_key and state_key not in seen:
                seen.add(state_key)
                states.append(state_key)
        return states

    def _invoice_woo_status_key(self, invoice_doc) -> str:
        if not invoice_doc:
            return ""
        states = self._invoice_state_candidates(invoice_doc)
        if int(getattr(invoice_doc, "docstatus", 0) or 0) == 2:
            return "cancelled"
        if any(state in {"cancelled", "canceled"} for state in states):
            return "cancelled"
        if any(state in {"delivered", "completed"} for state in states):
            return "completed"
        if any(state == "out-for-delivery" for state in states):
            return "out-for-delivery"
        if states:
            return "processing"
        return ""

    def _replacement_invoice_name(self, source_invoice_name: str) -> str:
        replacement = frappe.db.get_value(
            "Sales Invoice",
            {"amended_from": source_invoice_name},
            "name",
            order_by="creation desc",
        )
        return str(replacement or "")

    def _wait_for_inbound_amendment(self, source_invoice_name: str, woo_order_id: str, *, timeout_seconds: int = 45) -> dict[str, Any]:
        deadline = time.monotonic() + max(timeout_seconds, 1)
        last_snapshot: dict[str, Any] = {
            "replacement_invoice_name": "",
            "source_docstatus": None,
            "active_invoices": [],
            "order_map": None,
        }

        while time.monotonic() < deadline:
            frappe.db.commit()
            replacement_invoice_name = self._replacement_invoice_name(source_invoice_name)
            source_docstatus = frappe.db.get_value("Sales Invoice", source_invoice_name, "docstatus")
            active_invoices = self._active_invoices_for_woo_order_id(woo_order_id)
            order_map = self._order_map_row(woo_order_id)
            replacement_invoice_doc = frappe.get_doc("Sales Invoice", replacement_invoice_name) if replacement_invoice_name and frappe.db.exists("Sales Invoice", replacement_invoice_name) else None

            last_snapshot = {
                "replacement_invoice_name": replacement_invoice_name,
                "replacement_invoice_doc": replacement_invoice_doc,
                "source_docstatus": int(source_docstatus or 0) if source_docstatus is not None else None,
                "active_invoices": active_invoices,
                "order_map": order_map,
            }
            if replacement_invoice_doc and int(getattr(replacement_invoice_doc, "docstatus", 0) or 0) == 1 and int(source_docstatus or 0) == 2:
                return last_snapshot

            time.sleep(1)

        return last_snapshot

    def _host_name(self) -> str:
        host_name = str(getattr(frappe.conf, "host_name", "") or "").rstrip("/")
        if not host_name:
            try:
                host_name = str((frappe.get_site_config() or {}).get("host_name", "") or "").rstrip("/")
            except Exception:  # noqa: BLE001
                host_name = ""
        if not host_name:
            raise RuntimeError("Site host_name is not configured")
        return host_name

    def _unique_mobile(self) -> str:
        digits = "".join(ch for ch in self.run_id if ch.isdigit())[-8:].rjust(8, "0")
        return f"010{digits}"

    def _woo_client(self):
        if self._woo_client_cached is not None:
            return self._woo_client_cached
        from jarz_woocommerce_integration.doctype.woocommerce_settings.woocommerce_settings import (
            WooCommerceSettings,
        )
        from jarz_woocommerce_integration.utils.http_client import WooClient

        settings = WooCommerceSettings.get_settings()
        self._woo_client_cached = WooClient(
            base_url=str(getattr(settings, "base_url", "") or "").rstrip("/"),
            consumer_key=str(getattr(settings, "consumer_key", "") or ""),
            consumer_secret=settings.get_consumer_secret(),
            api_version=getattr(settings, "api_version", "v3") or "v3",
        )
        return self._woo_client_cached

    def _woo_customer(self, woo_customer_id: str | None) -> dict[str, Any] | None:
        if not woo_customer_id:
            return None
        return self._woo_client().get_customer(woo_customer_id)

    def _woo_order(self, woo_order_id: str | None) -> dict[str, Any] | None:
        if not woo_order_id:
            return None
        return self._woo_client().get_order(woo_order_id)

    def _build_woo_customer_payload(
        self,
        *,
        first_name: str,
        last_name: str,
        email: str,
        phone: str,
        billing_line1: str,
        shipping_line1: str,
        territory_fixture: dict[str, Any],
        billing_postcode: str,
        shipping_postcode: str,
        username: str | None = None,
        password: str | None = None,
    ) -> dict[str, Any]:
        state_value = str(territory_fixture.get("territory") or "")
        city_value = str(territory_fixture.get("territory_name") or state_value or "Unknown")
        payload = {
            "email": email,
            "first_name": first_name,
            "last_name": last_name,
            "meta_data": [{"key": "copilot_run_id", "value": self.run_id}],
            "billing": {
                "first_name": first_name,
                "last_name": last_name,
                "company": f"{first_name} {last_name}".strip(),
                "address_1": billing_line1,
                "address_2": "",
                "city": city_value,
                "state": state_value,
                "postcode": billing_postcode,
                "country": "EG",
                "email": email,
                "phone": phone,
            },
            "shipping": {
                "first_name": first_name,
                "last_name": last_name,
                "company": f"{first_name} {last_name}".strip(),
                "address_1": shipping_line1,
                "address_2": "",
                "city": city_value,
                "state": state_value,
                "postcode": shipping_postcode,
                "country": "EG",
                "phone": phone,
            },
        }
        if username:
            payload["username"] = username
        if password:
            payload["password"] = password
        return payload

    def _build_woo_order_payload(
        self,
        *,
        woo_customer_id: str,
        first_name: str,
        last_name: str,
        email: str,
        phone: str,
        billing_line1: str,
        shipping_line1: str,
        territory_fixture: dict[str, Any],
        item_rows: list[dict[str, Any]],
        delivery_slot: dict[str, str],
        status: str,
        payment_method: str,
        payment_method_title: str,
    ) -> dict[str, Any]:
        state_value = str(territory_fixture.get("territory") or "")
        city_value = str(territory_fixture.get("territory_name") or state_value or "Unknown")

        line_items: list[dict[str, Any]] = []
        for row in item_rows:
            product_id = int(row.get("woo_product_id") or 0)
            variation_id = int(row.get("woo_variation_id") or 0)
            line_item = {
                "product_id": product_id,
                "quantity": int(float(row.get("qty", 1) or 1)),
            }
            if variation_id > 0:
                line_item["variation_id"] = variation_id
            line_items.append(line_item)

        return {
            "status": status,
            "customer_id": int(woo_customer_id),
            "payment_method": payment_method,
            "payment_method_title": payment_method_title,
            "set_paid": False,
            "billing": {
                "first_name": first_name,
                "last_name": last_name,
                "company": f"{first_name} {last_name}".strip(),
                "address_1": billing_line1,
                "address_2": "",
                "city": city_value,
                "state": state_value,
                "postcode": "WOOI001",
                "country": "EG",
                "email": email,
                "phone": phone,
            },
            "shipping": {
                "first_name": first_name,
                "last_name": last_name,
                "company": f"{first_name} {last_name}".strip(),
                "address_1": shipping_line1,
                "address_2": "",
                "city": city_value,
                "state": state_value,
                "postcode": "WOOI002",
                "country": "EG",
                "phone": phone,
            },
            "line_items": line_items,
            "shipping_lines": [
                {
                    "method_id": "flat_rate",
                    "method_title": "Shipping",
                    "total": "0.00",
                }
            ],
            "meta_data": [
                {"key": "copilot_run_id", "value": self.run_id},
                {"key": "Delivery Date", "value": delivery_slot["delivery_date"]},
                {"key": "Time Slot", "value": "12:00-14:00"},
            ],
        }

    def _post_signed_woo_webhook(
        self,
        *,
        api_method: str,
        payload: dict[str, Any],
        topic: str,
    ) -> dict[str, Any]:
        import base64
        import hashlib
        import hmac

        import requests

        url = f"{self._host_name()}/api/method/{api_method}"
        raw_body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        secret = self._webhook_secret()
        signature = base64.b64encode(hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).digest()).decode("utf-8")
        response = requests.post(
            url,
            data=raw_body,
            headers={
                "Content-Type": "application/json",
                "X-WC-Webhook-Signature": signature,
                "X-WC-Webhook-Topic": topic,
            },
            timeout=60,
        )
        body = _safe_json_body(response)
        payload_body = body.get("message") if isinstance(body, dict) and isinstance(body.get("message"), dict) else body
        return {
            "url": url,
            "status_code": response.status_code,
            "body": body,
            "payload": payload_body,
        }

    def _webhook_secret(self) -> str:
        from jarz_woocommerce_integration.doctype.woocommerce_settings.woocommerce_settings import (
            WooCommerceSettings,
        )

        settings = WooCommerceSettings.get_settings()
        secret = _get_decrypted_single_password(settings.name, "webhook_secret")
        if not secret:
            secret = str(getattr(settings, "webhook_secret", "") or "")
        if not secret:
            raise RuntimeError("Woo webhook secret is not configured")
        return secret

    def _find_customer_by_woo_customer_id(self, woo_customer_id: str | None) -> str:
        if not woo_customer_id:
            return ""
        for fieldname in self._customer_woo_id_fields():
            customer_name = frappe.db.get_value("Customer", {fieldname: str(woo_customer_id)}, "name")
            if customer_name:
                return str(customer_name)
        return ""

    def _count_customers_by_woo_customer_id(self, woo_customer_id: str | None) -> int:
        if not woo_customer_id:
            return 0
        names: set[str] = set()
        for fieldname in self._customer_woo_id_fields():
            rows = frappe.get_all("Customer", filters={fieldname: str(woo_customer_id)}, fields=["name"], limit_page_length=20)
            names.update(str(row.get("name") or "") for row in rows if row.get("name"))
        return len(names)

    def _customer_woo_id_fields(self) -> list[str]:
        try:
            columns = set(frappe.db.get_table_columns("Customer") or [])
        except Exception:
            columns = set()
        return [fieldname for fieldname in ("woo_customer_id", "custom_woo_customer_id") if fieldname in columns]

    def _find_customer_address(
        self,
        addresses: list[dict[str, Any]],
        *,
        address_type: str,
        address_line1: str,
    ) -> dict[str, Any] | None:
        for row in addresses:
            if str(row.get("address_type") or "") != address_type:
                continue
            if str(row.get("address_line1") or "") == address_line1:
                return dict(row)
        return None

    def _customer_woo_id(self, customer_name: str) -> str:
        return str(frappe.db.get_value("Customer", customer_name, "woo_customer_id") or "")

    def _customer_addresses(self, customer_name: str) -> list[dict[str, Any]]:
        links = frappe.get_all(
            "Dynamic Link",
            filters={
                "link_doctype": "Customer",
                "link_name": customer_name,
                "parenttype": "Address",
            },
            fields=["parent"],
            order_by="creation asc",
        )
        names = [row.parent for row in links if getattr(row, "parent", None)]
        if not names:
            return []
        return frappe.get_all(
            "Address",
            filters={"name": ["in", names]},
            fields=[
                "name",
                "address_type",
                "address_line1",
                "address_line2",
                "city",
                "state",
                "pincode",
                "country",
                "phone",
                "email_id",
                "is_primary_address",
                "is_shipping_address",
            ],
            order_by="modified desc",
        )

    def _latest_sync_event(
        self,
        *,
        object_type: str,
        source_id: str,
        direction: str = "Outbound",
        created_after: datetime | None = None,
    ) -> dict[str, Any] | None:
        filters: dict[str, Any] = {
            "direction": direction,
            "object_type": object_type,
            "source_id": source_id,
        }
        if created_after is not None:
            filters["creation"] = [">=", created_after]
        events = frappe.get_all(
            "WooCommerce Sync Event",
            filters=filters,
            fields=[
                "name",
                "status",
                "last_error",
                "manual_review_reason",
                "creation",
                "modified",
                "event_type",
            ],
            order_by="creation desc",
            limit=1,
        )
        return dict(events[0]) if events else None

    def _ensure_inbound_event_processed(
        self,
        *,
        object_type: str,
        source_id: str,
        event_name: str = "",
    ) -> dict[str, Any]:
        from jarz_woocommerce_integration.services import sync_events

        frappe.db.commit()
        latest_event = self._latest_sync_event(
            direction="Inbound",
            object_type=object_type,
            source_id=source_id,
            created_after=self.started_on,
        )
        resolved_event_name = event_name or str((latest_event or {}).get("name") or "")
        if not resolved_event_name:
            return {
                "event_name": "",
                "process_result": {"status": "missing_event"},
                "latest_event": latest_event,
            }

        current_status = ""
        if frappe.db.exists("WooCommerce Sync Event", resolved_event_name):
            current_status = str(frappe.db.get_value("WooCommerce Sync Event", resolved_event_name, "status") or "")

        if current_status in {"Pending", "RetryScheduled", "Processing"}:
            process_result = sync_events.process_sync_event(resolved_event_name)
            frappe.db.commit()
        else:
            process_result = {
                "event_name": resolved_event_name,
                "status": (current_status or "missing").lower(),
                "skipped": True,
            }

        latest_event = self._latest_sync_event(
            direction="Inbound",
            object_type=object_type,
            source_id=source_id,
            created_after=self.started_on,
        )
        return {
            "event_name": resolved_event_name,
            "process_result": process_result,
            "latest_event": latest_event,
        }

    def _order_map_link_field(self) -> str:
        try:
            cols = set(frappe.db.get_table_columns("WooCommerce Order Map") or [])
        except Exception:
            cols = set()
        if "erpnext_sales_invoice" in cols:
            return "erpnext_sales_invoice"
        if "sales_invoice" in cols:
            return "sales_invoice"
        return "erpnext_sales_invoice"

    def _order_map_row(self, woo_order_id: str | None) -> dict[str, Any] | None:
        if not woo_order_id:
            return None
        link_field = self._order_map_link_field()
        row = frappe.db.get_value(
            "WooCommerce Order Map",
            {"woo_order_id": woo_order_id},
            ["name", "woo_order_id", link_field, "status", "hash", "synced_on"],
            as_dict=True,
        )
        return dict(row) if row else None

    def _active_invoices_for_woo_order_id(self, woo_order_id: str | None) -> list[dict[str, Any]]:
        if not woo_order_id:
            return []
        return frappe.get_all(
            "Sales Invoice",
            filters={
                "woo_order_id": woo_order_id,
                "docstatus": ["<", 2],
            },
            fields=["name", "customer", "docstatus", "custom_payment_method", "custom_sales_invoice_state"],
            order_by="creation desc",
            limit_page_length=10,
        )

    def _preexisting_inbound_order_artifacts(self, woo_order_id: str | None) -> dict[str, Any]:
        invoice_rows = self._active_invoices_for_woo_order_id(woo_order_id)
        order_map = self._order_map_row(woo_order_id)
        return {
            "has_collision": bool(invoice_rows or order_map),
            "invoice_rows": invoice_rows,
            "order_map": order_map,
        }

    def _max_mapped_woo_order_id(self) -> int:
        rows = frappe.db.sql(
            """
            SELECT MAX(CAST(woo_order_id AS UNSIGNED)) AS max_woo_order_id
            FROM `tabWooCommerce Order Map`
            WHERE IFNULL(woo_order_id, '') != ''
            """,
            as_dict=True,
        )
        if not rows:
            return 0
        try:
            return int(rows[0].get("max_woo_order_id") or 0)
        except Exception:
            return 0

    def _ensure_customer_synced_to_woo(self, customer_name: str, scope: str | None = None) -> dict[str, Any]:
        from jarz_woocommerce_integration.services import outbound_sync, sync_events

        latest_event = self._latest_sync_event(object_type="Customer", source_id=customer_name)
        if latest_event:
            if latest_event.get("status") in {"Pending", "RetryScheduled", "Processing"}:
                process_result = sync_events.process_sync_event(str(latest_event["name"]))
                frappe.db.commit()
                latest_event = self._latest_sync_event(object_type="Customer", source_id=customer_name)
                return {
                    "mode": "event",
                    "result": process_result,
                    "latest_event": latest_event,
                }
            return {
                "mode": "event",
                "result": {"status": latest_event.get("status")},
                "latest_event": latest_event,
            }

        fallback_result = outbound_sync.sync_customer(
            customer_name,
            reason=f"woo_staging_full_cycle:{self.run_id}",
            force=True,
            scope=scope,
        )
        frappe.db.commit()
        return {
            "mode": "direct_fallback",
            "result": fallback_result,
            "latest_event": self._latest_sync_event(object_type="Customer", source_id=customer_name),
        }

    def _ensure_invoice_synced_to_woo(self, invoice_name: str, *, cancel: bool = False) -> dict[str, Any]:
        from jarz_woocommerce_integration.services import outbound_sync, sync_events

        latest_event = self._latest_sync_event(object_type="Sales Invoice", source_id=invoice_name)
        if latest_event:
            if latest_event.get("status") in {"Pending", "RetryScheduled", "Processing"}:
                process_result = sync_events.process_sync_event(str(latest_event["name"]))
                frappe.db.commit()
                latest_event = self._latest_sync_event(object_type="Sales Invoice", source_id=invoice_name)
                return {
                    "mode": "event",
                    "result": process_result,
                    "latest_event": latest_event,
                }
            return {
                "mode": "event",
                "result": {"status": latest_event.get("status")},
                "latest_event": latest_event,
            }

        fallback_result = outbound_sync.sync_sales_invoice(
            invoice_name,
            reason=f"woo_staging_full_cycle:{self.run_id}",
            cancel=cancel,
            force=True,
        )
        frappe.db.commit()
        return {
            "mode": "direct_fallback",
            "result": fallback_result,
            "latest_event": self._latest_sync_event(object_type="Sales Invoice", source_id=invoice_name),
        }

    def _record_created(self, record_type: str, record_name: str, *, note: str = "") -> None:
        self.report["created_records"].append({
            "record_type": record_type,
            "record_name": record_name,
            "note": note,
        })

    def _finish_report(self) -> None:
        ended = now_datetime()
        cases = self.report.get("cases", [])
        assertions = self.report.get("assertions", [])
        self.report.update({
            "ended_on": ended.isoformat(),
            "duration_seconds": round((ended - self.started_on).total_seconds(), 3),
            "summary": {
                "cases_total": len(cases),
                "cases_passed": sum(1 for row in cases if row.get("status") == "Pass"),
                "cases_failed": sum(1 for row in cases if row.get("status") == "Fail"),
                "cases_concern": sum(1 for row in cases if row.get("status") == "Concern"),
                "assertions_total": len(assertions),
                "assertions_passed": sum(1 for row in assertions if row.get("status") == "Pass"),
                "assertions_failed": sum(1 for row in assertions if row.get("status") == "Fail"),
                "assertions_concern": sum(1 for row in assertions if row.get("status") == "Concern"),
            },
        })


def _get_decrypted_single_password(docname: str, fieldname: str) -> str:
    try:
        return get_decrypted_password("WooCommerce Settings", docname, fieldname) or ""
    except Exception:  # noqa: BLE001
        return ""


def _snapshot_flags(settings: Any) -> dict[str, str]:
    return {field: str(getattr(settings, field, "") or "0") for field in FLAGS}


def _health_counters(started_on: datetime) -> dict[str, Any]:
    counters: dict[str, Any] = {}
    counters["duplicate_active_woo_order_ids"] = _single_int(
        """
        SELECT COUNT(*) AS count FROM (
          SELECT woo_order_id
          FROM `tabSales Invoice`
          WHERE docstatus < 2 AND IFNULL(woo_order_id, 0) > 0
          GROUP BY woo_order_id
          HAVING COUNT(*) > 1
        ) d
        """
    )
    counters["manual_review_order_maps"] = frappe.db.count("WooCommerce Order Map", {"needs_manual_review": 1})
    counters["customer_outbound_errors"] = _single_int("SELECT COUNT(*) AS count FROM `tabCustomer` WHERE IFNULL(woo_outbound_status, '') = 'Error'")
    counters["invoice_outbound_errors"] = _single_int("SELECT COUNT(*) AS count FROM `tabSales Invoice` WHERE IFNULL(woo_outbound_status, '') = 'Error'")
    counters["stale_processing_events"] = _single_int("SELECT COUNT(*) AS count FROM `tabWooCommerce Sync Event` WHERE status = 'Processing' AND locked_until < NOW()")
    counters["pending_due_events"] = _single_int("SELECT COUNT(*) AS count FROM `tabWooCommerce Sync Event` WHERE status IN ('Pending', 'RetryScheduled') AND (next_attempt_at IS NULL OR next_attempt_at <= NOW())")
    counters["failed_events_since_start"] = _single_int(
        "SELECT COUNT(*) AS count FROM `tabWooCommerce Sync Event` WHERE creation >= %s AND status = 'Failed'",
        (started_on,),
    )
    counters["dead_letter_events_since_start"] = _single_int(
        "SELECT COUNT(*) AS count FROM `tabWooCommerce Sync Event` WHERE creation >= %s AND status = 'DeadLetter'",
        (started_on,),
    )
    counters["needs_review_events_since_start"] = _single_int(
        "SELECT COUNT(*) AS count FROM `tabWooCommerce Sync Event` WHERE creation >= %s AND status = 'NeedsReview'",
        (started_on,),
    )
    counters["woo_error_logs_since_start"] = _single_int(
        """
        SELECT COUNT(*) AS count
        FROM `tabError Log`
        WHERE creation >= %s
          AND (method LIKE %s OR error LIKE %s)
        """,
        (started_on, "%Woo%", "%woo%"),
    )
    return counters


def _single_int(sql: str, params: tuple[Any, ...] | None = None) -> int:
    rows = frappe.db.sql(sql, params or (), as_dict=True)
    if not rows:
        return 0
    return int(rows[0].get("count") or rows[0].get("c") or 0)


def _mariadb_version_at_least(raw_version: str, major: int, minor: int) -> bool:
    match = re.search(r"(\d+)\.(\d+)", str(raw_version or ""))
    if not match:
        return False
    version = (int(match.group(1)), int(match.group(2)))
    return version >= (major, minor)


def _next_delivery_slot() -> dict[str, str]:
    start = now_datetime() + timedelta(days=1)
    start = start.replace(hour=12, minute=0, second=0, microsecond=0)
    end = start + timedelta(hours=2)
    return {
        "delivery_date": start.date().isoformat(),
        "delivery_time_from": start.time().strftime("%H:%M:%S"),
        "delivery_duration": "7200",
        "required_delivery_datetime": start.strftime("%Y-%m-%d %H:%M:%S"),
        "delivery_end_datetime": end.strftime("%Y-%m-%d %H:%M:%S"),
    }


def _safe_json_body(response: Any) -> Any:
    try:
        return response.json()
    except Exception:  # noqa: BLE001
        return {"raw": getattr(response, "text", "")}


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(val) for key, val in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "as_dict"):
        try:
            return _json_safe(value.as_dict())
        except Exception:  # noqa: BLE001
            return str(value)
    return str(value)


def run(
    environment: str = "staging",
    allow_staging_mutations: bool = False,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Run the staging Woo full-cycle suite and return structured evidence."""
    runner = FullCycleRunner(
        environment=environment,
        allow_staging_mutations=allow_staging_mutations,
        run_id=run_id,
    )
    return runner.run()


def run_json(
    environment: str = "staging",
    allow_staging_mutations: bool = False,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Run and print a marker-wrapped JSON report for SSH wrappers."""
    report = run(
        environment=environment,
        allow_staging_mutations=allow_staging_mutations,
        run_id=run_id,
    )
    print(REPORT_MARKER_START)
    print(json.dumps(_json_safe(report), ensure_ascii=False, default=str, indent=2))
    print(REPORT_MARKER_END)
    return report