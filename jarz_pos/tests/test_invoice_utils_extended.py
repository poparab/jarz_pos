import unittest
import frappe

from jarz_pos.utils import invoice_utils


class DummyLogger:
    def __init__(self):
        self.messages = {"debug": [], "info": [], "error": []}

    def debug(self, message):
        self.messages["debug"].append(message)

    def info(self, message):
        self.messages["info"].append(message)

    def error(self, message):
        self.messages["error"].append(message)


class DummyInvoice:
    def __init__(self):
        self.items = []

    def append(self, table, _):
        assert table == "items"
        row = frappe._dict()
        self.items.append(row)
        return row


class TestInvoiceUtils(unittest.TestCase):
    def test_set_invoice_fields_populates_basic_values(self):
        invoice_doc = frappe._dict()
        customer = frappe._dict(name="CUST-1", customer_name="Alice", territory="Metro")
        profile = frappe._dict(
            name="POS-1",
            company="Jarz Trading",
            selling_price_list="Standard Selling",
            currency="PHP",
        )
        logger = DummyLogger()

        invoice_utils.set_invoice_fields(
            invoice_doc,
            customer,
            profile,
            "2025-01-02 14:30:00",
            logger,
        )

        self.assertEqual(invoice_doc.customer, "CUST-1")
        self.assertEqual(invoice_doc.customer_name, "Alice")
        self.assertEqual(invoice_doc.company, "Jarz Trading")
        self.assertEqual(invoice_doc.pos_profile, "POS-1")
        self.assertEqual(invoice_doc.custom_delivery_time_from, "14:30:00")
        self.assertIsNotNone(invoice_doc.custom_delivery_date)

    def test_add_items_to_invoice_respects_discount_percentage(self):
        invoice_doc = DummyInvoice()
        logger = DummyLogger()

        items = [
            {
                "item_code": "ITEM-1",
                "item_name": "Sample",
                "qty": 2,
                "price_list_rate": 50,
                "discount_percentage": 10,
                "uom": "Nos",
            }
        ]

        invoice_utils.add_items_to_invoice(invoice_doc, items, logger)

        self.assertEqual(len(invoice_doc.items), 1)
        line = invoice_doc.items[0]
        self.assertEqual(line.item_code, "ITEM-1")
        self.assertEqual(line.price_list_rate, 50)
        self.assertAlmostEqual(float(line.qty), 2, places=6)
        self.assertAlmostEqual(float(line.discount_percentage), 10, places=6)

    def test_format_invoice_data_emits_delivery_fields(self):
        class DummyDoc:
            def __init__(self):
                self.name = "SINV-0005"
                self.customer_name = "Alice"
                self.customer = "CUST-1"
                self.territory = "Metro"
                self.posting_date = frappe.utils.today()
                self.grand_total = 150
                self.net_total = 130
                self.total_taxes_and_charges = 20
                self.custom_delivery_date = "2025-01-02"
                self.custom_delivery_time_from = "10:00:00"
                self.custom_delivery_duration = 45
                self.custom_delivery_slot_label = "Morning Slot"
                self.items = [
                    frappe._dict(
                        item_code="ITEM-1",
                        item_name="Sample",
                        qty=1,
                        rate=130,
                        amount=130,
                    )
                ]

            def get(self, key):
                # Mimic frappe Document get with attribute fallback
                return getattr(self, key, None)

        invoice_doc = DummyDoc()

        formatted = invoice_utils.format_invoice_data(invoice_doc)
        self.assertEqual(formatted["name"], "SINV-0005")
        self.assertEqual(formatted["invoice_id_short"], "0005")
        self.assertEqual(formatted["delivery_slot_label"], "Morning Slot")
        self.assertEqual(formatted["items"][0]["item_code"], "ITEM-1")

    def test_apply_invoice_filters_builds_range_queries(self):
        filters = {
            "dateFrom": "2025-01-01",
            "dateTo": "2025-01-31",
            "customer": "CUST-1",
            "amountFrom": 100,
            "amountTo": 500,
        }

        result = invoice_utils.apply_invoice_filters(filters)

        self.assertEqual(result["docstatus"], 1)
        self.assertEqual(result["is_pos"], 1)
        self.assertEqual(result["posting_date"], ["between", ["2025-01-01", "2025-01-31"]])
        self.assertEqual(result["customer"], "CUST-1")
        self.assertEqual(result["grand_total"], ["between", [100, 500]])

    def test_verify_invoice_totals_accepts_matching_totals(self):
        class Line:
            def __init__(self, amount):
                self.amount = amount

        class Doc:
            def __init__(self):
                self.net_total = 110
                self.grand_total = 120
                self.items = [Line(60), Line(50)]

        invoice_doc = Doc()
        logger = DummyLogger()

        # Should not raise
        invoice_utils.verify_invoice_totals(invoice_doc, logger)


if __name__ == "__main__":
    unittest.main()
