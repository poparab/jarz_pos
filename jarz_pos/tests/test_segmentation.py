"""``set_segment_override`` refuses a customer that does not exist.

``frappe.db.set_value`` on a missing name is a silent no-op, so the endpoint
used to answer ``{"status": "ok"}`` for nothing. Mocked, no site data needed.
"""

import unittest
from unittest.mock import MagicMock, patch

import frappe

from jarz_pos.api import segmentation as seg


def _fake(exists):
    fake = MagicMock()
    fake.ValidationError = frappe.ValidationError
    fake.DoesNotExistError = frappe.DoesNotExistError

    def _throw(msg, exc=frappe.ValidationError, *a, **kw):
        raise exc(msg)

    fake.throw.side_effect = _throw
    fake.db.exists.return_value = exists
    return fake


class TestSetSegmentOverride(unittest.TestCase):
    def _call(self, fake, *args, **kwargs):
        with patch.multiple(seg, frappe=fake, _=lambda s, *a, **k: s, _ensure_jarz_manager=MagicMock()):
            return seg.set_segment_override(*args, **kwargs)

    def test_unknown_customer_raises_does_not_exist(self):
        fake = _fake(False)
        with self.assertRaises(frappe.DoesNotExistError):
            self._call(fake, "No Such Customer", 1, "Loyal")
        fake.db.set_value.assert_not_called()

    def test_blank_customer_raises_does_not_exist(self):
        fake = _fake(True)
        with self.assertRaises(frappe.DoesNotExistError):
            self._call(fake, "  ", 0)
        fake.db.set_value.assert_not_called()

    def test_existing_customer_is_pinned(self):
        fake = _fake(True)
        out = self._call(fake, "CUST-1", "1", "Loyal")
        self.assertEqual(out, {"status": "ok", "customer": "CUST-1"})
        fake.db.set_value.assert_called_once_with(
            "Customer", "CUST-1", {"segment_override": 1, "customer_segment": "Loyal"}
        )

    def test_pseudo_segment_still_refused(self):
        fake = _fake(True)
        with self.assertRaises(frappe.ValidationError):
            self._call(fake, "CUST-1", 1, "Regular")
        fake.db.set_value.assert_not_called()


if __name__ == "__main__":
    unittest.main()
