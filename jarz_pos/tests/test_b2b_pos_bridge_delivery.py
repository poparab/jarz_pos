"""POS->CRM delivery-followup bridge tests (pure mock / unittest).

Asserts ``pos_bridge.create_delivery_followup_on_state``:
  - fast-exits a Standard (non-B2B) invoice (no ToDo, no raise),
  - fast-exits a Sample/Trial invoice that has NOT reached a delivery state,
  - creates the correct follow-up ToDo text for Sample vs Trial once Delivered/OFD,
  - references the source Opportunity when stamped, else the Customer,
  - never raises even if ToDo creation blows up.

``_ensure_todo`` (imported lazily from jarz_pos.crm.follow_ups) is patched to capture
the call; no DB is touched. No FrappeTestCase / erpnext.tests.utils import.
"""

from __future__ import annotations

import types
import unittest
from unittest.mock import patch

from jarz_pos.crm import pos_bridge as pb


def _inv(purpose, state, customer="ACME Co", name="SI-0001", src_opp=None):
    return types.SimpleNamespace(
        custom_order_purpose=purpose,
        custom_sales_invoice_state=state,
        customer=customer,
        name=name,
        custom_source_opportunity=src_opp,
        owner="rep@example.com",
    )


class _Capture:
    """Records the last _ensure_todo invocation."""

    def __init__(self):
        self.calls = []

    def __call__(self, reference_type, reference_name, owner, description, date=None):
        self.calls.append(
            {
                "reference_type": reference_type,
                "reference_name": reference_name,
                "owner": owner,
                "description": description,
                "date": date,
            }
        )
        return "TODO-1"


def _run(doc, capture):
    # Patch the lazily-imported _ensure_todo at its source module.
    with patch("jarz_pos.crm.follow_ups._ensure_todo", capture), patch.object(
        pb.frappe.db, "exists", return_value=True
    ):
        pb.create_delivery_followup_on_state(doc)


class TestFastExits(unittest.TestCase):
    def test_standard_purpose_fast_exits(self):
        cap = _Capture()
        _run(_inv("Standard", "Delivered"), cap)
        self.assertEqual(cap.calls, [])

    def test_empty_purpose_fast_exits(self):
        cap = _Capture()
        _run(_inv("", "Delivered"), cap)
        self.assertEqual(cap.calls, [])

    def test_non_delivery_state_fast_exits(self):
        # Sample purpose, but the invoice hasn't reached a delivery state yet.
        cap = _Capture()
        _run(_inv("Sample - Courier", "In Progress"), cap)
        self.assertEqual(cap.calls, [])

    def test_none_doc_no_raise(self):
        cap = _Capture()
        _run(None, cap)
        self.assertEqual(cap.calls, [])


class TestSampleVsTrialText(unittest.TestCase):
    def test_sample_delivered_creates_feedback_todo(self):
        cap = _Capture()
        _run(_inv("Sample - Courier", "Delivered", customer="ACME Co"), cap)
        self.assertEqual(len(cap.calls), 1)
        desc = cap.calls[0]["description"]
        self.assertIn("Collect sample feedback for ACME Co", desc)
        self.assertIn("SI-0001", desc)  # invoice name stamped for traceability

    def test_trial_delivered_creates_checkup_todo(self):
        cap = _Capture()
        _run(_inv("Trial", "Delivered", customer="ACME Co"), cap)
        self.assertEqual(len(cap.calls), 1)
        desc = cap.calls[0]["description"]
        self.assertIn("Do check-up call for ACME Co", desc)

    def test_out_for_delivery_state_also_fires(self):
        cap = _Capture()
        _run(_inv("Sample - No Courier", "Out for Delivery"), cap)
        self.assertEqual(len(cap.calls), 1)
        self.assertIn("Collect sample feedback", cap.calls[0]["description"])


class TestReference(unittest.TestCase):
    def test_references_source_opportunity_when_stamped(self):
        cap = _Capture()
        _run(_inv("Sample - Courier", "Delivered", src_opp="OPP-0001"), cap)
        self.assertEqual(cap.calls[0]["reference_type"], "Opportunity")
        self.assertEqual(cap.calls[0]["reference_name"], "OPP-0001")

    def test_references_customer_when_no_opportunity(self):
        cap = _Capture()
        _run(_inv("Trial", "Delivered", customer="ACME Co", src_opp=None), cap)
        self.assertEqual(cap.calls[0]["reference_type"], "Customer")
        self.assertEqual(cap.calls[0]["reference_name"], "ACME Co")


class TestNeverRaises(unittest.TestCase):
    def test_ensure_todo_failure_swallowed(self):
        def _boom(*a, **k):
            raise RuntimeError("todo backend down")

        # Must not propagate — blocking here would break invoice submission flow.
        with patch("jarz_pos.crm.follow_ups._ensure_todo", _boom), patch.object(
            pb.frappe.db, "exists", return_value=True
        ):
            pb.create_delivery_followup_on_state(_inv("Sample - Courier", "Delivered"))

    def test_missing_customer_no_todo(self):
        cap = _Capture()
        _run(_inv("Sample - Courier", "Delivered", customer=None), cap)
        self.assertEqual(cap.calls, [])


if __name__ == "__main__":
    unittest.main()
