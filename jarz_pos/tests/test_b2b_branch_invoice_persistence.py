"""Real-stack B2B order proof with disposable Customer and Address records.

The invoice service is exercised against the site's real Commercial Policy,
Price List, Item, POS Profile, Territory, Sales Invoice validation, and submit
hooks. Every write remains in the test transaction; tearDown rolls it back and
also removes only the uniquely tracked fixtures if a downstream hook committed.
"""

from __future__ import annotations

import json
import unittest
import uuid

import frappe

from jarz_pos.services.commercial_policy import resolve_commercial_policy
from jarz_pos.services.invoice_creation import create_pos_invoice
from jarz_pos.utils.customer_address_utils import get_linked_customer_address_names


class TestB2BBranchInvoicePersistence(unittest.TestCase):
    def setUp(self):
        self.created = {"Sales Invoice": [], "Address": [], "Customer": []}

    def tearDown(self):
        frappe.db.rollback()
        leaked = any(
            frappe.db.exists(doctype, name)
            for doctype, names in self.created.items()
            for name in names
        )
        if not leaked:
            return

        # Defensive cleanup for any downstream hook that committed. Names are
        # captured from records created by this test only; no business data is
        # searched, merged, or deleted.
        for invoice_name in reversed(self.created["Sales Invoice"]):
            if frappe.db.exists("Sales Invoice", invoice_name):
                invoice = frappe.get_doc("Sales Invoice", invoice_name)
                if invoice.docstatus == 1:
                    invoice.cancel()
                frappe.delete_doc(
                    "Sales Invoice", invoice_name, force=True, ignore_permissions=True
                )
        for address_name in reversed(self.created["Address"]):
            if frappe.db.exists("Address", address_name):
                frappe.delete_doc(
                    "Address", address_name, force=True, ignore_permissions=True
                )
        for customer_name in reversed(self.created["Customer"]):
            if frappe.db.exists("Customer", customer_name):
                frappe.delete_doc(
                    "Customer", customer_name, force=True, ignore_permissions=True
                )
        frappe.db.commit()

    @staticmethod
    def _fixture_inputs():
        territory = frappe.get_all(
            "Territory",
            filters={
                "is_group": 0,
                "pos_profile": ["is", "set"],
                "delivery_income": [">", 0],
            },
            fields=["name", "pos_profile", "delivery_income"],
            order_by="name asc",
            limit_page_length=1,
        )
        if not territory:
            raise unittest.SkipTest("site has no delivery Territory with a POS Profile")
        territory = territory[0]
        profile = frappe.db.get_value(
            "POS Profile",
            territory.pos_profile,
            ["name", "company", "warehouse", "selling_price_list", "currency", "disabled"],
            as_dict=True,
        )
        if not profile or profile.disabled:
            raise unittest.SkipTest("territory POS Profile is missing or disabled")

        price = frappe.get_all(
            "Item Price",
            filters={"selling": 1, "price_list": "B2B Selling"},
            fields=["item_code", "price_list_rate", "uom"],
            order_by="item_code asc",
            limit_page_length=20,
        )
        price = next(
            (
                row
                for row in price
                if frappe.db.get_value("Item", row.item_code, "disabled") == 0
                and frappe.db.get_value("Item", row.item_code, "is_sales_item") == 1
            ),
            None,
        )
        if not price:
            raise unittest.SkipTest("site has no enabled sales Item priced in B2B Selling")
        return territory, profile, price

    def test_selected_branch_and_b2b_price_survive_submitted_invoice(self):
        territory, profile, price = self._fixture_inputs()
        other_territory = frappe.db.get_value(
            "Territory",
            {"is_group": 0, "name": ["!=", territory.name]},
            "name",
        ) or territory.name
        suffix = uuid.uuid4().hex[:10]
        group = "B2B" if frappe.db.exists("Customer Group", "B2B") else None
        payment_terms = frappe.db.get_value("Payment Terms Template", {}, "name")
        customer_payload = {
            "doctype": "Customer",
            "customer_name": f"_TEST B2B Branch {suffix}",
            "customer_type": "Company",
            "territory": other_territory,
            "default_price_list": "B2B Selling",
        }
        if group:
            customer_payload["customer_group"] = group
        if payment_terms:
            customer_payload["payment_terms"] = payment_terms
        customer = frappe.get_doc(customer_payload).insert(ignore_permissions=True)
        self.created["Customer"].append(customer.name)

        address = frappe.get_doc(
            {
                "doctype": "Address",
                "address_title": "All Seasons",
                "address_type": "Shipping",
                "address_line1": f"_TEST Madinaty {suffix}",
                "city": territory.name,
                "country": frappe.db.get_single_value("System Settings", "country")
                or "Egypt",
                "is_shipping_address": 1,
                "links": [
                    {"link_doctype": "Customer", "link_name": customer.name}
                ],
            }
        ).insert(ignore_permissions=True)
        self.created["Address"].append(address.name)

        commercial_before = frappe.db.get_value(
            "Customer",
            customer.name,
            [
                "customer_type",
                "customer_group",
                "territory",
                "default_price_list",
                "payment_terms",
                "customer_primary_address",
            ],
            as_dict=True,
        )
        self.assertIn(address.name, get_linked_customer_address_names(customer.name))

        policy_decision = resolve_commercial_policy(
            order_purpose="B2B Supply",
            pos_profile=frappe.get_doc("POS Profile", profile.name),
            logger=frappe.logger("jarz_pos.tests.b2b_branch_invoice"),
        )

        result = create_pos_invoice(
            cart_json=json.dumps(
                [
                    {
                        "item_code": price.item_code,
                        "qty": 1,
                        "uom": price.uom,
                    }
                ]
            ),
            customer_name=customer.name,
            pos_profile_name=profile.name,
            shipping_address_name=address.name,
            price_list="B2B Selling",
            order_purpose="B2B Supply",
            channel="flutter",
        )
        self.created["Sales Invoice"].append(result["invoice_name"])
        invoice = frappe.get_doc("Sales Invoice", result["invoice_name"])
        line = invoice.items[0]
        shipping_rows = [
            row
            for row in invoice.taxes
            if str(row.description or "").lower().startswith("shipping income")
        ]

        self.assertEqual(invoice.docstatus, 1)
        self.assertEqual(invoice.customer, customer.name)
        self.assertEqual(invoice.shipping_address_name, address.name)
        self.assertEqual(invoice.customer_address, address.name)
        self.assertEqual(invoice.territory, territory.name)
        self.assertEqual(invoice.selling_price_list, "B2B Selling")
        self.assertEqual(invoice.custom_order_purpose, "B2B Supply")
        self.assertAlmostEqual(float(line.price_list_rate), float(price.price_list_rate), places=2)
        self.assertAlmostEqual(float(line.rate), float(price.price_list_rate), places=2)
        persisted_shipping_fee = sum(float(row.tax_amount or 0) for row in shipping_rows)
        expected_shipping_fee = (
            0.0
            if policy_decision.suppress_shipping_income
            else float(territory.delivery_income)
        )
        self.assertAlmostEqual(persisted_shipping_fee, expected_shipping_fee, places=2)
        if policy_decision.suppress_shipping_income:
            self.assertEqual(shipping_rows, [])
        else:
            self.assertEqual(len(shipping_rows), 1)
        self.assertEqual(
            frappe.db.get_value(
                "Dynamic Link",
                {
                    "parenttype": "Address",
                    "parent": address.name,
                    "link_doctype": "Customer",
                    "link_name": customer.name,
                },
                "link_name",
            ),
            customer.name,
        )
        commercial_after = frappe.db.get_value(
            "Customer",
            customer.name,
            list(commercial_before.keys()),
            as_dict=True,
        )
        self.assertEqual(commercial_after, commercial_before)
        if payment_terms:
            self.assertEqual(invoice.payment_terms_template, payment_terms)
            self.assertTrue(invoice.payment_schedule)
            self.assertAlmostEqual(
                sum(float(row.invoice_portion or 0) for row in invoice.payment_schedule),
                100.0,
                places=2,
            )

        print(
            "B2B_BRANCH_INVOICE_EVIDENCE "
            + frappe.as_json(
                {
                    "invoice": invoice.name,
                    "address_owner": customer.name,
                    "shipping_address_name": invoice.shipping_address_name,
                    "territory": invoice.territory,
                    "selling_price_list": invoice.selling_price_list,
                    "order_purpose": invoice.custom_order_purpose,
                    "item_code": line.item_code,
                    "price_list_rate": line.price_list_rate,
                    "rate": line.rate,
                    "shipping_tax": persisted_shipping_fee,
                    "policy_suppresses_shipping": policy_decision.suppress_shipping_income,
                    "payment_terms_template": invoice.payment_terms_template,
                    "payment_schedule_rows": len(invoice.payment_schedule),
                    "customer_before": commercial_before,
                    "customer_after": commercial_after,
                }
            )
        )
