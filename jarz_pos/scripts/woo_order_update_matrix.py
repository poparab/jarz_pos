"""Staging-only WooCommerce order update matrix runner.

This runner builds on ``woo_staging_full_cycle`` and focuses on order status,
item, price, and cross-system update behavior. It is safe by default: live
Woo/ERP mutations only run when ``allow_staging_mutations`` is true.
"""

from __future__ import annotations

import json
import time
import traceback
from datetime import datetime
from typing import Any

import frappe
from frappe.utils import now_datetime

from jarz_pos.scripts.woo_staging_full_cycle import (
    FullCycleRunner,
    _health_counters,
    _json_safe,
    _next_delivery_slot,
)


REPORT_MARKER_START = "WOO_ORDER_UPDATE_MATRIX_JSON_START"
REPORT_MARKER_END = "WOO_ORDER_UPDATE_MATRIX_JSON_END"

CORE_WOO_STATUSES = (
    "pending",
    "on-hold",
    "processing",
    "pre-nasrcity",
    "pre-ismailia",
    "pre-hadayk",
    "pre-hadayek",
    "pre-dokki",
    "out-for-delivery",
    "completed",
    "cancelled",
    "refunded",
    "failed",
)

TERMINAL_WOO_STATUSES = {"cancelled", "refunded", "failed"}
SUBMITTED_WOO_STATUSES = {
    "processing",
    "pre-nasrcity",
    "pre-ismailia",
    "pre-hadayk",
    "pre-hadayek",
    "pre-dokki",
    "out-for-delivery",
    "completed",
}
PROCESSING_EQUIVALENT_STATUSES = {
    "processing",
    "pre-nasrcity",
    "pre-ismailia",
    "pre-hadayk",
    "pre-hadayek",
    "pre-dokki",
}

PAYMENT_METHOD_CASES = (
    {
        "key": "cod",
        "payment_method": "cod",
        "payment_method_title": "Cash",
        "expected_erp_method": "Cash",
        "should_auto_pay": False,
    },
    {
        "key": "instapay",
        "payment_method": "instapay",
        "payment_method_title": "Instapay",
        "expected_erp_method": "Instapay",
        "should_auto_pay": False,
    },
    {
        "key": "kashier_card",
        "payment_method": "kashier_card",
        "payment_method_title": "Kashier Card",
        "expected_erp_method": "Kashier Card",
        "should_auto_pay": True,
    },
    {
        "key": "kashier_wallet",
        "payment_method": "kashier_wallet",
        "payment_method_title": "Kashier Wallet",
        "expected_erp_method": "Kashier Wallet",
        "should_auto_pay": True,
    },
)

MATRIX_TRANSITIONS = (
    ("processing", "completed"),
    ("processing", "out-for-delivery"),
    ("out-for-delivery", "completed"),
    ("completed", "processing"),
    ("processing", "cancelled"),
    ("processing", "refunded"),
    ("processing", "failed"),
    ("cancelled", "processing"),
    ("on-hold", "processing"),
)

ITEM_MUTATIONS = (
    "quantity_increase",
    "quantity_decrease",
    "line_total_change",
    "shipping_total_change",
    "remove_line",
    "add_line",
)

ERP_STATE_TRANSITIONS = (
    "Out for Delivery",
    "Delivered",
)


class OrderUpdateMatrixRunner(FullCycleRunner):
    def __init__(
        self,
        *,
        environment: str = "staging",
        allow_staging_mutations: bool = False,
        run_id: str | None = None,
        max_statuses: int | None = None,
    ) -> None:
        super().__init__(
            environment=environment,
            allow_staging_mutations=allow_staging_mutations,
            run_id=run_id or f"ORDER-MATRIX-{now_datetime().strftime('%Y%m%d-%H%M%S')}",
        )
        self.max_statuses = max_statuses
        self.report["matrix"] = {
            "scope": "staging_woo_order_update_matrix",
            "max_statuses": max_statuses,
            "mutation_policy": "leave_tagged_records_for_audit",
        }

    def run(self) -> dict[str, Any]:
        try:
            self._guard_environment()
            self._case("PF-01", "Preflight", self._preflight)
            self._case("PF-02", "Dynamic fixture discovery", self._discover_fixtures)
            self._case("REL-01", "Webhook ACK and invalid signature", self._webhook_reliability_checks)
            self._case("MX-DISC-01", "Discover Woo order statuses", self._discover_woo_statuses)
            if self.allow_staging_mutations:
                self._case("MX-CUST-01", "Matrix inbound Woo customer fixture", self._inbound_customer_create)
                self._case("MX-IN-STATUS-01", "Inbound Woo status and payment matrix", self._inbound_status_payment_matrix)
                self._case("MX-IN-TRANS-01", "Inbound Woo status transition matrix", self._inbound_transition_matrix)
                self._case("MX-IN-ITEM-01", "Inbound Woo item and price mutation matrix", self._inbound_item_price_matrix)
                self._case("MX-ERP-CUST-01", "ERP customer fixture for outbound matrix", self._matrix_outbound_customer_create)
                self._case("MX-ERP-STATUS-01", "ERP invoice state outbound matrix", self._erp_outbound_state_matrix)
                self._case("MX-ERP-ITEM-01", "ERP invoice item and price outbound matrix", self._erp_item_price_outbound_matrix)
                self._case("MX-AUDIT-01", "Run-scoped accounting and sync audit", self._run_scoped_audit)
        except Exception as exc:  # noqa: BLE001
            self.report["errors"].append({
                "error": str(exc),
                "traceback": traceback.format_exc(limit=12),
            })
        finally:
            self._finish_report()
        return self.report

    def _discover_woo_statuses(self, case: dict[str, Any]) -> dict[str, Any]:
        api_statuses: list[str] = []
        api_payload: Any = None
        api_error = ""
        try:
            api_payload = self._woo_client().get("orders/statuses")
            api_statuses = self._extract_status_slugs(api_payload)
        except Exception as exc:  # noqa: BLE001
            api_error = str(exc)

        recent_statuses: list[str] = []
        recent_error = ""
        try:
            recent_orders = self._woo_client().list_orders(params={"status": "any", "per_page": 100, "orderby": "modified", "order": "desc"})
            recent_statuses = sorted({str(row.get("status") or "").strip().lower() for row in recent_orders if row.get("status")})
        except Exception as exc:  # noqa: BLE001
            recent_error = str(exc)

        statuses = self._matrix_statuses(api_statuses, recent_statuses)
        self.runtime_state["matrix_statuses"] = statuses
        self.runtime_state["matrix_status_catalog"] = {
            "api_statuses": api_statuses,
            "recent_statuses": recent_statuses,
            "fallback_statuses": list(CORE_WOO_STATUSES),
            "api_error": api_error,
            "recent_error": recent_error,
        }

        self._assert(case, "MX-DISC-01.01", "At least one Woo status is available for the matrix", bool(statuses), expected="non-empty", actual=statuses)
        self._assert(case, "MX-DISC-01.02", "Processing status is included", "processing" in statuses, expected="processing", actual=statuses)
        self._assert(case, "MX-DISC-01.03", "Terminal statuses are represented", bool(TERMINAL_WOO_STATUSES.intersection(statuses)), expected=sorted(TERMINAL_WOO_STATUSES), actual=statuses, concern=True)

        return {
            "statuses": statuses,
            "api_statuses": api_statuses,
            "api_payload": api_payload,
            "api_error": api_error,
            "recent_statuses": recent_statuses,
            "recent_error": recent_error,
        }

    def _inbound_status_payment_matrix(self, case: dict[str, Any]) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        statuses = self._selected_matrix_statuses()
        for status in statuses:
            for payment in PAYMENT_METHOD_CASES:
                row = self._run_inbound_create_case(
                    status=status,
                    payment=payment,
                    case=case,
                    assertion_prefix=f"MX-IN-STATUS-01.{len(rows) + 1:03d}",
                )
                rows.append(row)

        failures = [row for row in rows if row.get("severity") == "fail"]
        concerns = [row for row in rows if row.get("severity") == "concern"]
        self._assert(case, "MX-IN-STATUS-01.SUMMARY", "Inbound status/payment matrix has no hard failures", not failures, expected=[], actual=failures)
        self._assert(case, "MX-IN-STATUS-01.CONCERNS", "Inbound status/payment matrix concerns are recorded", True, expected="recorded", actual=concerns)
        return {"rows": rows, "failures": failures, "concerns": concerns}

    def _inbound_transition_matrix(self, case: dict[str, Any]) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        supported = set(self._selected_matrix_statuses())
        for source_status, target_status in MATRIX_TRANSITIONS:
            if source_status not in supported or target_status not in supported:
                rows.append({
                    "source_status": source_status,
                    "target_status": target_status,
                    "severity": "concern",
                    "reason": "status_not_available_on_staging",
                })
                continue
            row = self._run_inbound_transition_case(
                source_status=source_status,
                target_status=target_status,
                case=case,
                assertion_prefix=f"MX-IN-TRANS-01.{len(rows) + 1:03d}",
            )
            rows.append(row)

        failures = [row for row in rows if row.get("severity") == "fail"]
        concerns = [row for row in rows if row.get("severity") == "concern"]
        self._assert(case, "MX-IN-TRANS-01.SUMMARY", "Inbound transition matrix has no hard failures", not failures, expected=[], actual=failures)
        self._assert(case, "MX-IN-TRANS-01.CONCERNS", "Inbound transition matrix concerns are recorded", True, expected="recorded", actual=concerns)
        return {"rows": rows, "failures": failures, "concerns": concerns}

    def _inbound_item_price_matrix(self, case: dict[str, Any]) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        for mutation in ITEM_MUTATIONS:
            row = self._run_inbound_item_mutation_case(
                mutation=mutation,
                case=case,
                assertion_prefix=f"MX-IN-ITEM-01.{len(rows) + 1:03d}",
            )
            rows.append(row)

        failures = [row for row in rows if row.get("severity") == "fail"]
        concerns = [row for row in rows if row.get("severity") == "concern"]
        self._assert(case, "MX-IN-ITEM-01.SUMMARY", "Inbound item/price mutation matrix has no hard failures", not failures, expected=[], actual=failures)
        self._assert(case, "MX-IN-ITEM-01.CONCERNS", "Inbound item/price concerns are recorded", True, expected="recorded", actual=concerns)
        return {"rows": rows, "failures": failures, "concerns": concerns}

    def _erp_outbound_state_matrix(self, case: dict[str, Any]) -> dict[str, Any]:
        from jarz_pos.api.kanban import cancel_invoice, update_invoice_state

        rows: list[dict[str, Any]] = []
        outbound_cart_items = self._outbound_stocked_cart_items()
        for target_state in ERP_STATE_TRANSITIONS:
            invoice_run = self._create_and_sync_invoice(payment_method="Cash", cart_items=outbound_cart_items)
            invoice_name = str(invoice_run["invoice_name"] or "")
            row: dict[str, Any] = {
                "target_state": target_state,
                "invoice_name": invoice_name,
                "woo_order_id": invoice_run.get("woo_order_id"),
            }
            try:
                result = update_invoice_state(invoice_id=invoice_name, new_state=target_state)
                frappe.db.commit()
                sync = self._ensure_invoice_synced_to_woo(invoice_name)
                invoice = frappe.get_doc("Sales Invoice", invoice_name)
                woo_order = self._woo_order(str(getattr(invoice, "woo_order_id", "") or ""))
                expected_status = "out-for-delivery" if target_state == "Out for Delivery" else "completed"
                passed = bool(result.get("success")) and str((woo_order or {}).get("status") or "") == expected_status
                row.update({
                    "severity": "pass" if passed else "fail",
                    "expected_woo_status": expected_status,
                    "actual_woo_status": (woo_order or {}).get("status"),
                    "result": result,
                    "sync": sync,
                    "invoice": invoice.as_dict(),
                    "woo_order": woo_order,
                })
                self._assert(case, f"MX-ERP-STATUS-01.{len(rows) + 1:03d}", f"ERP state {target_state} pushes expected Woo status", passed, expected=expected_status, actual=row)
                if target_state == "Out for Delivery":
                    delivery_note = str(result.get("delivery_note") or "")
                    if delivery_note:
                        self._record_created("Delivery Note", delivery_note, note=f"MX-ERP-STATUS-01 {target_state}")
            except Exception as exc:  # noqa: BLE001
                row.update({"severity": "fail", "error": str(exc), "traceback": traceback.format_exc(limit=8)})
                self._assert(case, f"MX-ERP-STATUS-01.{len(rows) + 1:03d}", f"ERP state {target_state} case executes", False, expected="no exception", actual=row)
            rows.append(row)

        cancel_run = self._create_and_sync_invoice(payment_method="Cash", cart_items=outbound_cart_items)
        cancel_invoice_name = str(cancel_run["invoice_name"] or "")
        cancel_row: dict[str, Any] = {"target_state": "Cancelled", "invoice_name": cancel_invoice_name, "woo_order_id": cancel_run.get("woo_order_id")}
        try:
            cancel_result = cancel_invoice(invoice_id=cancel_invoice_name, reason="Woo order update matrix cancellation", notes=self.run_id)
            frappe.db.commit()
            cancel_sync = self._ensure_invoice_synced_to_woo(cancel_invoice_name, cancel=True)
            cancelled_invoice = frappe.get_doc("Sales Invoice", cancel_invoice_name)
            cancelled_woo_order = self._woo_order(str(getattr(cancelled_invoice, "woo_order_id", "") or ""))
            passed = int(getattr(cancelled_invoice, "docstatus", 0) or 0) == 2 and str((cancelled_woo_order or {}).get("status") or "") == "cancelled"
            cancel_row.update({
                "severity": "pass" if passed else "fail",
                "expected_woo_status": "cancelled",
                "actual_woo_status": (cancelled_woo_order or {}).get("status"),
                "cancel_result": cancel_result,
                "sync": cancel_sync,
                "invoice": cancelled_invoice.as_dict(),
                "woo_order": cancelled_woo_order,
            })
            self._assert(case, "MX-ERP-STATUS-01.CANCEL", "ERP cancellation pushes Woo cancelled", passed, expected="cancelled", actual=cancel_row)
        except Exception as exc:  # noqa: BLE001
            cancel_row.update({"severity": "fail", "error": str(exc), "traceback": traceback.format_exc(limit=8)})
            self._assert(case, "MX-ERP-STATUS-01.CANCEL", "ERP cancellation case executes", False, expected="no exception", actual=cancel_row)
        rows.append(cancel_row)

        failures = [row for row in rows if row.get("severity") == "fail"]
        self._assert(case, "MX-ERP-STATUS-01.SUMMARY", "ERP outbound state matrix has no hard failures", not failures, expected=[], actual=failures)
        return {"rows": rows, "failures": failures}

    def _erp_item_price_outbound_matrix(self, case: dict[str, Any]) -> dict[str, Any]:
        from jarz_pos.api.manager import submit_invoice_amendment

        source_run = self._create_and_sync_invoice(payment_method="Cash", cart_items=self._outbound_stocked_cart_items())
        source_invoice_name = str(source_run["invoice_name"] or "")
        source_invoice = source_run["invoice_doc"]
        fixture = source_run["fixture"]
        amended_items = [dict(row) for row in source_run["cart_items"]]
        amended_items[0]["qty"] = float(amended_items[0].get("qty", 1) or 1) + 1
        if len(amended_items) > 1:
            amended_items[1]["rate"] = round(float(amended_items[1].get("rate", 0) or 0) + 1, 2)

        row: dict[str, Any] = {
            "source_invoice_name": source_invoice_name,
            "woo_order_id_before": source_run.get("woo_order_id"),
            "expected_signature": self._cart_signature(amended_items),
        }
        try:
            amendment_result = submit_invoice_amendment(
                invoice_id=source_invoice_name,
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
                or self._replacement_invoice_name(source_invoice_name)
                or ""
            )
            replacement_sync = self._ensure_invoice_synced_to_woo(replacement_invoice_name) if replacement_invoice_name else {}
            replacement_invoice = frappe.get_doc("Sales Invoice", replacement_invoice_name) if replacement_invoice_name else None
            woo_order_id = str(getattr(replacement_invoice, "woo_order_id", "") or source_run.get("woo_order_id") or "") if replacement_invoice else str(source_run.get("woo_order_id") or "")
            woo_order = self._woo_order(woo_order_id) if woo_order_id else None
            expected_signature = self._cart_signature(amended_items)
            invoice_signature = self._invoice_item_signature(replacement_invoice) if replacement_invoice else []
            woo_signature = self._woo_order_item_signature(woo_order)
            passed = bool(replacement_invoice_name) and invoice_signature == expected_signature and woo_signature == expected_signature
            row.update({
                "severity": "pass" if passed else "fail",
                "amendment_result": amendment_result,
                "replacement_invoice_name": replacement_invoice_name,
                "replacement_sync": replacement_sync,
                "replacement_invoice": replacement_invoice.as_dict() if replacement_invoice else None,
                "woo_order": woo_order,
                "invoice_signature": invoice_signature,
                "woo_signature": woo_signature,
            })
            self._assert(case, "MX-ERP-ITEM-01.001", "ERP amendment item/price update reaches Woo order", passed, expected=expected_signature, actual=row)
            if replacement_invoice_name:
                self._record_created("Sales Invoice", replacement_invoice_name, note="MX-ERP-ITEM-01 replacement invoice")
        except Exception as exc:  # noqa: BLE001
            row.update({"severity": "fail", "error": str(exc), "traceback": traceback.format_exc(limit=8)})
            self._assert(case, "MX-ERP-ITEM-01.001", "ERP item/price outbound case executes", False, expected="no exception", actual=row)

        return {"rows": [row], "failures": [row] if row.get("severity") == "fail" else []}

    def _run_scoped_audit(self, case: dict[str, Any]) -> dict[str, Any]:
        created_sales_invoices = [
            str(row.get("record_name") or "")
            for row in self.report.get("created_records", [])
            if row.get("record_type") == "Sales Invoice" and row.get("record_name")
        ]
        created_woo_orders = [
            str(row.get("record_name") or "")
            for row in self.report.get("created_records", [])
            if row.get("record_type") == "Woo Order" and row.get("record_name")
        ]
        invoice_audits = [self._audit_invoice(invoice_name) for invoice_name in created_sales_invoices]
        duplicate_active = self._duplicate_active_invoices_for_orders(created_woo_orders)
        events = self._run_events()
        logs = self._run_sync_logs(created_woo_orders)
        health = _health_counters(self.started_on)
        submitted_missing_gl = [row for row in invoice_audits if row.get("docstatus") == 1 and int(row.get("gl_entries") or 0) == 0]
        submitted_missing_ple = [row for row in invoice_audits if row.get("docstatus") == 1 and int(row.get("payment_ledger_entries") or 0) == 0]
        severe_events = [row for row in events if row.get("status") in {"Failed", "DeadLetter"}]

        self._assert(case, "MX-AUDIT-01.01", "No duplicate active Sales Invoices for matrix Woo orders", not duplicate_active, expected=[], actual=duplicate_active)
        self._assert(case, "MX-AUDIT-01.02", "Submitted matrix invoices have GL entries", not submitted_missing_gl, expected=[], actual=submitted_missing_gl)
        self._assert(case, "MX-AUDIT-01.03", "Submitted matrix invoices have Payment Ledger entries", not submitted_missing_ple, expected=[], actual=submitted_missing_ple, concern=True)
        self._assert(case, "MX-AUDIT-01.04", "No run-scoped events reached Failed or DeadLetter", not severe_events, expected=[], actual=severe_events)

        return {
            "created_sales_invoices": created_sales_invoices,
            "created_woo_orders": created_woo_orders,
            "invoice_audits": invoice_audits,
            "duplicate_active_invoices": duplicate_active,
            "sync_events": events,
            "sync_logs": logs,
            "health_counters": health,
        }

    def _matrix_outbound_customer_create(self, case: dict[str, Any]) -> dict[str, Any]:
        from jarz_pos.api.customer import create_customer

        fixture = self._primary_territory_fixture()
        display_name = f"Copilot ERP {self.run_id}"
        mobile = self._matrix_mobile("erp")
        address_line = f"{self.run_id} ERP Primary Address"

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

        self._assert(case, "MX-ERP-CUST-01.01", "Customer API returns an ERP customer name", bool(customer_name), expected="non-empty", actual=customer_name)
        self._assert(case, "MX-ERP-CUST-01.02", "Customer exists in ERP", bool(frappe.db.exists("Customer", customer_name)), expected=True, actual=bool(frappe.db.exists("Customer", customer_name)))
        self._assert(case, "MX-ERP-CUST-01.03", "Customer has at least one linked address", len(linked_addresses) >= 1, expected=">=1", actual=len(linked_addresses))
        self._assert(case, "MX-ERP-CUST-01.04", "Customer outbound path used event processing", sync_result.get("mode") == "event", expected="event", actual=sync_result.get("mode"), concern=True)
        self._assert(case, "MX-ERP-CUST-01.05", "Customer has Woo customer ID after sync", bool(woo_customer_id), expected="non-empty", actual=woo_customer_id)
        self._assert(case, "MX-ERP-CUST-01.06", "Customer outbound status is Synced", getattr(customer_doc, "woo_outbound_status", "") == "Synced", expected="Synced", actual=getattr(customer_doc, "woo_outbound_status", ""))
        self._assert(case, "MX-ERP-CUST-01.07", "Woo customer exists", isinstance(woo_customer, dict) and bool(woo_customer.get("id")), expected=True, actual=woo_customer)
        self._assert(case, "MX-ERP-CUST-01.08", "Woo customer phone matches ERP mobile", ((woo_customer or {}).get("billing") or {}).get("phone") == mobile, expected=mobile, actual=((woo_customer or {}).get("billing") or {}).get("phone"))

        self.runtime_state["customer"] = {
            "customer_name": customer_name,
            "display_name": display_name,
            "mobile": mobile,
            "territory": fixture,
            "primary_address": address_line,
            "woo_customer_id": woo_customer_id,
        }
        self._record_created("Customer", customer_name, note="MX-ERP-CUST-01 synthetic customer")
        if linked_addresses:
            self._record_created("Address", linked_addresses[0]["name"], note="MX-ERP-CUST-01 initial address")
        if woo_customer_id:
            self._record_created("Woo Customer", woo_customer_id, note="MX-ERP-CUST-01 outbound customer")

        return {
            "customer_result": result,
            "sync_result": sync_result,
            "customer_doc": customer_doc.as_dict(),
            "linked_addresses": linked_addresses,
            "woo_customer": woo_customer,
        }

    def _run_inbound_create_case(
        self,
        *,
        status: str,
        payment: dict[str, Any],
        case: dict[str, Any],
        assertion_prefix: str,
    ) -> dict[str, Any]:
        row: dict[str, Any] = {"status": status, "payment": payment.get("key")}
        try:
            customer = self._matrix_inbound_customer()
            fixture = dict(customer.get("territory") or self._primary_territory_fixture())
            order_items = self._matrix_order_items(fixture)
            payload = self._build_woo_order_payload(
                woo_customer_id=str(customer["woo_customer_id"]),
                first_name=str(customer.get("first_name") or "Matrix"),
                last_name=str(customer.get("last_name") or self.run_id),
                email=str(customer.get("email") or f"matrix.{self.run_id.lower()}@orderjarz.local"),
                phone=str(customer.get("phone") or self._unique_mobile()),
                billing_line1=f"{self.run_id} {status} {payment['key']} Billing",
                shipping_line1=f"{self.run_id} {status} {payment['key']} Shipping",
                territory_fixture=fixture,
                item_rows=order_items,
                delivery_slot=dict(self.fixture_catalog.get("next_delivery_slot") or _next_delivery_slot()),
                status=status,
                payment_method=str(payment["payment_method"]),
                payment_method_title=str(payment["payment_method_title"]),
            )
            payload["meta_data"] = list(payload.get("meta_data") or []) + [
                {"key": "order_update_matrix_case", "value": f"status:{status}:payment:{payment['key']}"},
            ]
            created_order = self._woo_client().post("orders", payload)
            woo_order_id = str(created_order.get("id") or "")
            full_order = self._woo_order(woo_order_id) or created_order
            self._record_created("Woo Order", woo_order_id, note=f"MX-IN-STATUS-01 {status}/{payment['key']}")
            webhook = self._post_signed_woo_webhook(
                api_method="jarz_woocommerce_integration.api.orders.woo_order_webhook",
                payload=full_order,
                topic="order.created",
            )
            inbound_sync = self._ensure_inbound_event_processed(
                object_type="Order",
                source_id=woo_order_id,
                event_name=str((webhook.get("payload") or {}).get("event_name") or ""),
            )
            outcome = self._expected_inbound_create_outcome(status, payment)
            settled = self._wait_for_inbound_order_outcome(
                woo_order_id,
                event_name=str((webhook.get("payload") or {}).get("event_name") or ""),
            )
            actual = dict(settled.get("actual") or self._inbound_actual_state(woo_order_id))
            latest_event = dict(settled.get("latest_event") or inbound_sync.get("latest_event") or {})
            severity, reason = self._compare_inbound_create_outcome(outcome, actual, latest_event=latest_event)
            row.update({
                "severity": severity,
                "reason": reason,
                "woo_order_id": woo_order_id,
                "expected": outcome,
                "actual": actual,
                "latest_event": latest_event,
                "webhook": webhook,
                "inbound_sync": inbound_sync,
                "created_order": created_order,
            })
            invoice_name = str(actual.get("invoice_name") or "")
            if invoice_name:
                self._record_created("Sales Invoice", invoice_name, note=f"MX-IN-STATUS-01 {status}/{payment['key']}")
            self._assert(case, assertion_prefix, f"Inbound create status={status} payment={payment['key']} behaves as expected", severity != "fail", expected=outcome, actual=row, concern=severity == "concern")
        except Exception as exc:  # noqa: BLE001
            row.update({"severity": "fail", "error": str(exc), "traceback": traceback.format_exc(limit=8)})
            self._assert(case, assertion_prefix, f"Inbound create status={status} payment={payment['key']} executes", False, expected="no exception", actual=row)
        return row

    def _run_inbound_transition_case(
        self,
        *,
        source_status: str,
        target_status: str,
        case: dict[str, Any],
        assertion_prefix: str,
    ) -> dict[str, Any]:
        row: dict[str, Any] = {"source_status": source_status, "target_status": target_status}
        try:
            created = self._create_matrix_order(source_status, payment=PAYMENT_METHOD_CASES[0], case_note=f"transition:{source_status}->{target_status}")
            woo_order_id = created["woo_order_id"]
            before = self._inbound_actual_state(woo_order_id)
            updated_order = self._woo_client().put(f"orders/{woo_order_id}", {"status": target_status})
            webhook = self._post_signed_woo_webhook(
                api_method="jarz_woocommerce_integration.api.orders.woo_order_webhook",
                payload=updated_order,
                topic="order.updated",
            )
            inbound_sync = self._ensure_inbound_event_processed(
                object_type="Order",
                source_id=woo_order_id,
                event_name=str((webhook.get("payload") or {}).get("event_name") or ""),
            )
            after = self._inbound_actual_state(woo_order_id)
            expected = self._expected_transition_outcome(source_status, target_status, before)
            severity, reason = self._compare_transition_outcome(expected, after, inbound_sync)
            row.update({
                "severity": severity,
                "reason": reason,
                "woo_order_id": woo_order_id,
                "invoice_before": before,
                "invoice_after": after,
                "expected": expected,
                "updated_order": updated_order,
                "webhook": webhook,
                "inbound_sync": inbound_sync,
            })
            self._assert(case, assertion_prefix, f"Inbound transition {source_status}->{target_status} behaves as expected", severity != "fail", expected=expected, actual=row, concern=severity == "concern")
        except Exception as exc:  # noqa: BLE001
            row.update({"severity": "fail", "error": str(exc), "traceback": traceback.format_exc(limit=8)})
            self._assert(case, assertion_prefix, f"Inbound transition {source_status}->{target_status} executes", False, expected="no exception", actual=row)
        return row

    def _run_inbound_item_mutation_case(
        self,
        *,
        mutation: str,
        case: dict[str, Any],
        assertion_prefix: str,
    ) -> dict[str, Any]:
        row: dict[str, Any] = {"mutation": mutation}
        try:
            initial_items = None
            if mutation == "quantity_decrease":
                initial_items = self._matrix_order_items(dict(self._matrix_inbound_customer().get("territory") or self._primary_territory_fixture()))
                if initial_items:
                    initial_items[0]["qty"] = 2

            created = self._create_matrix_order(
                "processing",
                payment=PAYMENT_METHOD_CASES[0],
                case_note=f"item:{mutation}",
                order_items=initial_items,
            )
            woo_order_id = created["woo_order_id"]
            source_invoice = str((created.get("actual") or {}).get("invoice_name") or "")
            current_order = self._woo_order(woo_order_id) or created["full_order"]
            update_payload = self._build_item_mutation_payload(mutation, current_order, created["fixture"])
            if update_payload.get("unsupported"):
                row.update({"severity": "concern", "reason": "unsupported_by_fixture", "details": update_payload})
                self._assert(case, assertion_prefix, f"Inbound item mutation {mutation} has a usable fixture", False, expected="supported", actual=row, concern=True)
                return row
            updated_order = self._woo_client().put(f"orders/{woo_order_id}", update_payload)
            webhook = self._post_signed_woo_webhook(
                api_method="jarz_woocommerce_integration.api.orders.woo_order_webhook",
                payload=updated_order,
                topic="order.updated",
            )
            inbound_sync = self._ensure_inbound_event_processed(
                object_type="Order",
                source_id=woo_order_id,
                event_name=str((webhook.get("payload") or {}).get("event_name") or ""),
            )
            settled = self._wait_for_inbound_order_outcome(
                woo_order_id,
                event_name=str((webhook.get("payload") or {}).get("event_name") or ""),
            )
            amendment_wait = self._wait_for_inbound_amendment(source_invoice, woo_order_id, timeout_seconds=45) if source_invoice else {}
            replacement_invoice_name = str(amendment_wait.get("replacement_invoice_name") or "")
            replacement_invoice = amendment_wait.get("replacement_invoice_doc")
            actual_signature = self._invoice_item_signature(replacement_invoice) if replacement_invoice else []
            expected_signature = self._woo_order_item_signature(updated_order)
            process_result = dict(inbound_sync.get("process_result") or {})
            queue_result = dict(process_result.get("result") or {})
            latest_event = dict(settled.get("latest_event") or inbound_sync.get("latest_event") or {})
            queued = str(queue_result.get("reason") or process_result.get("reason") or latest_event.get("last_error") or "").strip().lower() == "amendment_enqueued"
            passed = bool(replacement_invoice_name and replacement_invoice) and actual_signature == expected_signature
            severity = "pass" if passed else ("concern" if queued or str(latest_event.get("status") or "") == "RetryScheduled" else "fail")
            row.update({
                "severity": severity,
                "reason": "replacement_matches_woo" if passed else ("amendment_queued_but_replacement_not_confirmed" if queued else ("event_retry_locked" if str(latest_event.get("status") or "") == "RetryScheduled" else "mutation_not_applied")),
                "woo_order_id": woo_order_id,
                "source_invoice": source_invoice,
                "replacement_invoice_name": replacement_invoice_name,
                "expected_signature": expected_signature,
                "actual_signature": actual_signature,
                "update_payload": update_payload,
                "updated_order": updated_order,
                "webhook": webhook,
                "inbound_sync": inbound_sync,
                "latest_event": latest_event,
                "amendment_wait": amendment_wait,
            })
            if replacement_invoice_name:
                self._record_created("Sales Invoice", replacement_invoice_name, note=f"MX-IN-ITEM-01 {mutation} replacement invoice")
            self._assert(case, assertion_prefix, f"Inbound item mutation {mutation} amends or reports clearly", severity != "fail", expected=expected_signature, actual=row, concern=severity == "concern")
        except Exception as exc:  # noqa: BLE001
            row.update({"severity": "fail", "error": str(exc), "traceback": traceback.format_exc(limit=8)})
            self._assert(case, assertion_prefix, f"Inbound item mutation {mutation} executes", False, expected="no exception", actual=row)
        return row

    def _create_matrix_order(self, status: str, *, payment: dict[str, Any], case_note: str, order_items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        customer = self._matrix_inbound_customer()
        fixture = dict(customer.get("territory") or self._primary_territory_fixture())
        selected_order_items = [dict(row) for row in (order_items or self._matrix_order_items(fixture))]
        payload = self._build_woo_order_payload(
            woo_customer_id=str(customer["woo_customer_id"]),
            first_name=str(customer.get("first_name") or "Matrix"),
            last_name=str(customer.get("last_name") or self.run_id),
            email=str(customer.get("email") or f"matrix.{self.run_id.lower()}@orderjarz.local"),
            phone=str(customer.get("phone") or self._unique_mobile()),
            billing_line1=f"{self.run_id} {case_note} Billing",
            shipping_line1=f"{self.run_id} {case_note} Shipping",
            territory_fixture=fixture,
            item_rows=selected_order_items,
            delivery_slot=dict(self.fixture_catalog.get("next_delivery_slot") or _next_delivery_slot()),
            status=status,
            payment_method=str(payment["payment_method"]),
            payment_method_title=str(payment["payment_method_title"]),
        )
        payload["meta_data"] = list(payload.get("meta_data") or []) + [
            {"key": "order_update_matrix_case", "value": case_note},
        ]
        created_order = self._woo_client().post("orders", payload)
        woo_order_id = str(created_order.get("id") or "")
        full_order = self._woo_order(woo_order_id) or created_order
        self._record_created("Woo Order", woo_order_id, note=f"matrix {case_note}")
        webhook = self._post_signed_woo_webhook(
            api_method="jarz_woocommerce_integration.api.orders.woo_order_webhook",
            payload=full_order,
            topic="order.created",
        )
        inbound_sync = self._ensure_inbound_event_processed(
            object_type="Order",
            source_id=woo_order_id,
            event_name=str((webhook.get("payload") or {}).get("event_name") or ""),
        )
        settled = self._wait_for_inbound_order_outcome(
            woo_order_id,
            event_name=str((webhook.get("payload") or {}).get("event_name") or ""),
        )
        actual = dict(settled.get("actual") or self._inbound_actual_state(woo_order_id))
        invoice_name = str(actual.get("invoice_name") or "")
        if invoice_name:
            self._record_created("Sales Invoice", invoice_name, note=f"matrix {case_note}")
        return {
            "woo_order_id": woo_order_id,
            "created_order": created_order,
            "full_order": full_order,
            "webhook": webhook,
            "inbound_sync": inbound_sync,
            "actual": actual,
            "settled": settled,
            "fixture": fixture,
            "order_items": selected_order_items,
        }

    def _matrix_inbound_customer(self) -> dict[str, Any]:
        customer = dict(self.runtime_state.get("inbound_customer") or {})
        if not customer or not customer.get("woo_customer_id"):
            raise RuntimeError("MX-CUST-01 must create an inbound Woo customer before order matrix cases")
        return customer

    def _matrix_order_items(self, fixture: dict[str, Any]) -> list[dict[str, Any]]:
        rows = self._order_fixture_items(
            str(fixture.get("price_list") or self._primary_territory_fixture().get("price_list") or ""),
            warehouse=str(fixture.get("warehouse") or ""),
            require_stock=True,
        )
        return [{**dict(row), "qty": 1} for row in rows[:2]]

    def _outbound_stocked_cart_items(self) -> list[dict[str, Any]]:
        fixture = self._primary_territory_fixture()
        rows = self._order_fixture_items(
            str(fixture.get("price_list") or ""),
            warehouse=str(fixture.get("warehouse") or ""),
            require_stock=True,
        )
        stocked_rows = [
            dict(row)
            for row in rows
            if float(row.get("actual_qty") or 0) >= 1
        ]
        selected = stocked_rows[:2] or rows[:2]
        return [{**dict(row), "qty": 1} for row in selected]

    def _selected_matrix_statuses(self) -> list[str]:
        statuses = list(self.runtime_state.get("matrix_statuses") or CORE_WOO_STATUSES)
        if self.max_statuses and self.max_statuses > 0:
            return statuses[: self.max_statuses]
        return statuses

    def _matrix_statuses(self, api_statuses: list[str], recent_statuses: list[str]) -> list[str]:
        seen: set[str] = set()
        statuses: list[str] = []
        for status in list(api_statuses) + list(recent_statuses) + list(CORE_WOO_STATUSES):
            slug = self._normalize_status_slug(status)
            if slug and slug not in seen:
                seen.add(slug)
                statuses.append(slug)
        return statuses

    def _extract_status_slugs(self, payload: Any) -> list[str]:
        statuses: list[str] = []
        if isinstance(payload, dict):
            iterable = payload.items()
            for key, value in iterable:
                candidate = key
                if isinstance(value, dict):
                    candidate = str(value.get("slug") or value.get("status") or key)
                statuses.append(self._normalize_status_slug(candidate))
        elif isinstance(payload, list):
            for row in payload:
                if isinstance(row, dict):
                    statuses.append(self._normalize_status_slug(row.get("slug") or row.get("status") or row.get("id") or row.get("name")))
                else:
                    statuses.append(self._normalize_status_slug(row))
        return [status for status in statuses if status]

    def _normalize_status_slug(self, status: Any) -> str:
        slug = str(status or "").strip().lower()
        if slug.startswith("wc-"):
            slug = slug[3:]
        return slug

    def _expected_inbound_create_outcome(self, status: str, payment: dict[str, Any]) -> dict[str, Any]:
        if status in TERMINAL_WOO_STATUSES:
            docstatus = 2
            state = "cancelled"
        elif status in SUBMITTED_WOO_STATUSES:
            docstatus = 1
            state = "out-for-delivery" if status == "out-for-delivery" else ("completed" if status == "completed" else "processing")
        else:
            docstatus = 0
            state = "draft"
        should_auto_pay = bool(payment.get("should_auto_pay")) and status in PROCESSING_EQUIVALENT_STATUSES.union({"completed"})
        return {
            "docstatus": docstatus,
            "state": state,
            "payment_method": payment.get("expected_erp_method"),
            "should_auto_pay": should_auto_pay,
            "skip_reason": "pending_payment" if status == "pending" else "",
        }

    def _inbound_actual_state(self, woo_order_id: str) -> dict[str, Any]:
        order_map = self._order_map_row(woo_order_id)
        active_invoices = self._active_invoices_for_woo_order_id(woo_order_id)
        invoice_name = str(active_invoices[0].get("name") or "") if active_invoices else ""
        if not invoice_name and order_map:
            invoice_name = str(order_map.get(self._order_map_link_field()) or "")
        invoice = frappe.get_doc("Sales Invoice", invoice_name) if invoice_name and frappe.db.exists("Sales Invoice", invoice_name) else None
        payment_entries = self._payment_entries_for_invoice(invoice_name) if invoice_name else []
        return {
            "woo_order_id": woo_order_id,
            "invoice_name": invoice_name,
            "docstatus": int(getattr(invoice, "docstatus", 0) or 0) if invoice else None,
            "state": self._invoice_state_key(invoice) if invoice else "",
            "woo_status_key": self._invoice_woo_status_key(invoice) if invoice else "",
            "payment_method": str(getattr(invoice, "custom_payment_method", "") or "") if invoice else "",
            "grand_total": float(getattr(invoice, "grand_total", 0) or 0) if invoice else 0,
            "outstanding_amount": float(getattr(invoice, "outstanding_amount", 0) or 0) if invoice else 0,
            "item_signature": self._invoice_item_signature(invoice) if invoice else [],
            "active_invoices": active_invoices,
            "order_map": order_map,
            "payment_entries": payment_entries,
        }

    def _compare_inbound_create_outcome(self, expected: dict[str, Any], actual: dict[str, Any], *, latest_event: dict[str, Any] | None = None) -> tuple[str, str]:
        latest_event = latest_event or {}
        expected_skip_reason = str(expected.get("skip_reason") or "")
        actual_last_error = str(latest_event.get("last_error") or "").strip().lower()
        if expected_skip_reason and not actual.get("invoice_name") and actual_last_error == expected_skip_reason:
            return "pass", expected_skip_reason
        if actual.get("docstatus") != expected.get("docstatus"):
            return "fail", "docstatus_mismatch"
        expected_state = str(expected.get("state") or "")
        actual_woo_state = str(actual.get("woo_status_key") or "")
        actual_state = str(actual.get("state") or "")
        if expected_state == "draft":
            if actual.get("docstatus") != 0:
                return "fail", "draft_status_mismatch"
        elif expected_state and expected_state not in {actual_state, actual_woo_state}:
            return "fail", "state_mismatch"
        expected_payment_method = str(expected.get("payment_method") or "")
        if expected_payment_method and str(actual.get("payment_method") or "") != expected_payment_method:
            return "fail", "payment_method_mismatch"
        if expected.get("should_auto_pay") and not actual.get("payment_entries"):
            return "concern", "expected_auto_payment_entry_missing"
        return "pass", "matched"

    def _wait_for_inbound_order_outcome(
        self,
        woo_order_id: str,
        *,
        event_name: str = "",
        timeout_seconds: int = 20,
    ) -> dict[str, Any]:
        from jarz_woocommerce_integration.services import sync_events

        deadline = time.monotonic() + timeout_seconds
        last_snapshot = {
            "event_name": event_name,
            "latest_event": {},
            "actual": self._inbound_actual_state(woo_order_id),
        }

        while time.monotonic() < deadline:
            frappe.db.commit()
            latest_event = self._latest_sync_event(
                direction="Inbound",
                object_type="Order",
                source_id=woo_order_id,
                created_after=self.started_on,
            )
            latest_event = dict(latest_event or {})
            actual = self._inbound_actual_state(woo_order_id)
            last_snapshot = {
                "event_name": str(latest_event.get("name") or event_name or ""),
                "latest_event": latest_event,
                "actual": actual,
            }

            latest_status = str(latest_event.get("status") or "")
            if actual.get("invoice_name") or latest_status in {"Succeeded", "Skipped", "NeedsReview", "Failed", "DeadLetter"}:
                return last_snapshot

            latest_event_name = str(latest_event.get("name") or event_name or "")
            if latest_event_name and latest_status in {"Pending", "RetryScheduled", "Processing"}:
                try:
                    sync_events.process_sync_event(latest_event_name)
                    frappe.db.commit()
                except Exception:
                    frappe.db.rollback()

            time.sleep(1)

        return last_snapshot

    def _expected_transition_outcome(self, source_status: str, target_status: str, before: dict[str, Any]) -> dict[str, Any]:
        source_docstatus = before.get("docstatus")
        if source_docstatus == 0:
            return {"mode": "mutable_or_created", "target_status": target_status}
        if source_status == "out-for-delivery":
            return {"mode": "frozen", "reason": "out_for_delivery_locked", "docstatus": source_docstatus}
        if target_status in {"processing", "on-hold"} and source_status not in TERMINAL_WOO_STATUSES:
            return {"mode": "frozen_or_skipped", "docstatus": source_docstatus}
        return {"mode": "manual_review_or_frozen", "docstatus": source_docstatus}

    def _compare_transition_outcome(self, expected: dict[str, Any], after: dict[str, Any], inbound_sync: dict[str, Any]) -> tuple[str, str]:
        mode = expected.get("mode")
        process_result = dict(inbound_sync.get("process_result") or {})
        nested_result = dict(process_result.get("result") or {})
        reason = str(process_result.get("reason") or nested_result.get("reason") or "").strip().lower()
        status = str(process_result.get("status") or nested_result.get("status") or "").strip().lower()
        if mode == "mutable_or_created":
            return "pass", "draft_or_initial_order_mutable"
        if mode == "frozen" and reason in {"out_for_delivery_locked", "submitted_frozen", "needs_manual_review"}:
            return "pass", reason
        if mode in {"frozen_or_skipped", "manual_review_or_frozen"} and (status in {"skipped", "queued", "succeeded"} or reason in {"submitted_frozen", "needs_manual_review", "amendment_enqueued"}):
            return "pass", reason or status
        if after.get("docstatus") == expected.get("docstatus"):
            return "concern", reason or status or "same_docstatus_without_clear_reason"
        return "fail", reason or status or "unexpected_transition_result"

    def _build_item_mutation_payload(self, mutation: str, current_order: dict[str, Any], fixture: dict[str, Any]) -> dict[str, Any]:
        line_items = [dict(row) for row in (current_order.get("line_items") or [])]
        if not line_items:
            return {"unsupported": True, "reason": "no_line_items"}
        first_line = dict(line_items[0])
        first_line_id = first_line.get("id")
        if not first_line_id:
            return {"unsupported": True, "reason": "missing_first_line_id", "line": first_line}
        current_qty = int(first_line.get("quantity") or 1)
        if mutation == "quantity_increase":
            return {"status": "processing", "line_items": [{"id": first_line_id, "quantity": current_qty + 1}]}
        if mutation == "quantity_decrease":
            return {"status": "processing", "line_items": [{"id": first_line_id, "quantity": max(1, current_qty - 1)}]}
        if mutation == "line_total_change":
            current_total = float(first_line.get("total") or first_line.get("subtotal") or 0)
            return {"status": "processing", "line_items": [{"id": first_line_id, "total": f"{current_total + 1:.2f}", "subtotal": f"{current_total + 1:.2f}"}]}
        if mutation == "shipping_total_change":
            shipping_lines = [dict(row) for row in (current_order.get("shipping_lines") or [])]
            if not shipping_lines or not shipping_lines[0].get("id"):
                return {"unsupported": True, "reason": "missing_shipping_line_id", "shipping_lines": shipping_lines}
            return {"status": "processing", "shipping_lines": [{"id": shipping_lines[0]["id"], "total": "15.00"}]}
        if mutation == "remove_line":
            if len(line_items) < 2:
                return {"unsupported": True, "reason": "requires_two_line_items", "line_items": line_items}
            return {"status": "processing", "line_items": [{"id": line_items[-1].get("id"), "quantity": 0}]}
        if mutation == "add_line":
            available = self._matrix_order_items(fixture)
            existing_products = {int(row.get("product_id") or 0) for row in line_items}
            candidate = next((row for row in available if int(row.get("woo_product_id") or 0) not in existing_products), None)
            if not candidate:
                return {"unsupported": True, "reason": "no_distinct_additional_item", "available": available, "line_items": line_items}
            line_item = {"product_id": int(candidate.get("woo_product_id") or 0), "quantity": 1}
            variation_id = int(candidate.get("woo_variation_id") or 0)
            if variation_id > 0:
                line_item["variation_id"] = variation_id
            return {"status": "processing", "line_items": [line_item]}
        return {"unsupported": True, "reason": "unknown_mutation"}

    def _payment_entries_for_invoice(self, invoice_name: str) -> list[dict[str, Any]]:
        if not invoice_name:
            return []
        return frappe.db.sql(
            """
            SELECT pe.name, pe.docstatus, pe.payment_type, pe.mode_of_payment, pe.paid_amount
            FROM `tabPayment Entry` pe
            INNER JOIN `tabPayment Entry Reference` per ON per.parent = pe.name
            WHERE per.reference_doctype = 'Sales Invoice'
              AND per.reference_name = %s
            ORDER BY pe.creation DESC
            LIMIT 10
            """,
            (invoice_name,),
            as_dict=True,
        )

    def _audit_invoice(self, invoice_name: str) -> dict[str, Any]:
        invoice = frappe.get_doc("Sales Invoice", invoice_name) if frappe.db.exists("Sales Invoice", invoice_name) else None
        if not invoice:
            return {"invoice_name": invoice_name, "missing": True}
        return {
            "invoice_name": invoice_name,
            "docstatus": int(getattr(invoice, "docstatus", 0) or 0),
            "woo_order_id": str(getattr(invoice, "woo_order_id", "") or ""),
            "state": self._invoice_state_key(invoice),
            "grand_total": float(getattr(invoice, "grand_total", 0) or 0),
            "outstanding_amount": float(getattr(invoice, "outstanding_amount", 0) or 0),
            "payment_entries": self._payment_entries_for_invoice(invoice_name),
            "gl_entries": frappe.db.count("GL Entry", {"voucher_type": "Sales Invoice", "voucher_no": invoice_name}),
            "payment_ledger_entries": frappe.db.count("Payment Ledger Entry", {"voucher_type": "Sales Invoice", "voucher_no": invoice_name}) if frappe.db.table_exists("Payment Ledger Entry") else None,
            "stock_ledger_entries": frappe.db.count("Stock Ledger Entry", {"voucher_type": "Sales Invoice", "voucher_no": invoice_name}) if frappe.db.table_exists("Stock Ledger Entry") else None,
            "delivery_notes": self._delivery_notes_for_invoice(invoice_name),
            "amended_from": str(getattr(invoice, "amended_from", "") or ""),
        }

    def _delivery_notes_for_invoice(self, invoice_name: str) -> list[dict[str, Any]]:
        try:
            columns = set(frappe.db.get_table_columns("Delivery Note") or [])
        except Exception:
            columns = set()
        if "remarks" not in columns:
            return []
        return frappe.db.sql(
            """
            SELECT name, docstatus, status, grand_total
            FROM `tabDelivery Note`
            WHERE remarks LIKE %s
            ORDER BY creation DESC
            LIMIT 10
            """,
            (f"%{invoice_name}%",),
            as_dict=True,
        )

    def _duplicate_active_invoices_for_orders(self, woo_order_ids: list[str]) -> list[dict[str, Any]]:
        ids = [str(value or "") for value in woo_order_ids if value]
        if not ids:
            return []
        return frappe.db.sql(
            """
            SELECT woo_order_id, COUNT(*) AS active_count, GROUP_CONCAT(name ORDER BY creation DESC) AS invoices
            FROM `tabSales Invoice`
            WHERE docstatus < 2
              AND woo_order_id IN %(woo_order_ids)s
            GROUP BY woo_order_id
            HAVING COUNT(*) > 1
            """,
            {"woo_order_ids": tuple(ids)},
            as_dict=True,
        )

    def _run_events(self) -> list[dict[str, Any]]:
        return frappe.get_all(
            "WooCommerce Sync Event",
            filters={"creation": [">=", self.started_on]},
            fields=["name", "direction", "object_type", "source_id", "status", "review_state", "last_error", "creation", "modified"],
            order_by="creation desc",
            limit_page_length=500,
        )

    def _matrix_mobile(self, label: str) -> str:
        digits = "".join(ch for ch in self.run_id if ch.isdigit())[-8:].rjust(8, "0")
        prefix = {
            "erp": "011",
            "erp_alt": "012",
        }.get(label, "015")
        return f"{prefix}{digits}"

    def _run_sync_logs(self, woo_order_ids: list[str]) -> list[dict[str, Any]]:
        filters: dict[str, Any] = {"started_on": [">=", self.started_on]}
        if woo_order_ids:
            filters["woo_order_id"] = ["in", woo_order_ids]
        return frappe.get_all(
            "WooCommerce Sync Log",
            filters=filters,
            fields=["name", "operation", "woo_order_id", "status", "message", "started_on", "duration"],
            order_by="started_on desc",
            limit_page_length=500,
        )


def run(
    environment: str = "staging",
    allow_staging_mutations: bool = False,
    run_id: str | None = None,
    max_statuses: int | None = None,
) -> dict[str, Any]:
    """Run the staging Woo order update matrix and return structured evidence."""
    runner = OrderUpdateMatrixRunner(
        environment=environment,
        allow_staging_mutations=allow_staging_mutations,
        run_id=run_id,
        max_statuses=max_statuses,
    )
    return runner.run()


def run_json(
    environment: str = "staging",
    allow_staging_mutations: bool = False,
    run_id: str | None = None,
    max_statuses: int | None = None,
) -> dict[str, Any]:
    """Run and print a marker-wrapped JSON report for SSH wrappers."""
    report = run(
        environment=environment,
        allow_staging_mutations=allow_staging_mutations,
        run_id=run_id,
        max_statuses=max_statuses,
    )
    print(REPORT_MARKER_START)
    print(json.dumps(_json_safe(report), ensure_ascii=False, default=str, indent=2))
    print(REPORT_MARKER_END)
    return report
