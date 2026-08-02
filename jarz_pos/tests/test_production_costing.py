"""Batch costing maths for the Production Board.

Every division in ``_compute_batch_cost`` has a denominator that is genuinely
zero in normal operation — a batch that was started but not finished has
``produced_qty == 0``, and an uncosted BOM has ``total_cost == 0``.  Those cases
must return ``None``, not raise and not report a confident zero.
"""

import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch


class Thrown(Exception):
    """What a wired-up ``frappe.throw`` raises in these tests."""


def passthrough_translate():
    return patch("jarz_pos.api.manufacturing._", new=lambda msg: msg)


def wire_throw(mock_frappe):
    """``frappe.throw`` must raise, and carry its message."""

    def _throw(message, *_args, **_kwargs):
        raise Thrown(str(message))

    mock_frappe.throw.side_effect = _throw
    return mock_frappe


def transfer_rows(*amounts):
    return [
        {"stock_entry": f"STE-{i}", "item_code": f"RM-{i}", "qty": 1.0, "amount": amount}
        for i, amount in enumerate(amounts, start=1)
    ]


class TestComputeBatchCost(unittest.TestCase):
    def _run(self, rows, produced_qty, bom=None):
        from jarz_pos.api import manufacturing

        with patch(
            "jarz_pos.api.manufacturing._resolve_stock_entry_detail_rows", return_value=rows
        ) as mock_rows, patch(
            "jarz_pos.api.manufacturing._resolve_bom_cost", return_value=bom
        ), patch("jarz_pos.api.manufacturing.frappe"):
            out = manufacturing._compute_batch_cost(
                "WO-0001", produced_qty=produced_qty, bom_no="BOM-PIST-CAKE"
            )

        return out, mock_rows

    def test_material_cost_sums_the_transfer_lines_only(self):
        """Not the Manufacture entry.

        The finished-good line is valued off the transfer anyway, so counting it
        too would double the cost of any batch — and a batch finished in two
        goes would double again.
        """
        out, mock_rows = self._run(transfer_rows(400.0, 500.0), produced_qty=50)

        self.assertEqual(900.0, out["material_cost"])
        self.assertEqual(
            ("WO-0001", "Material Transfer for Manufacture"), mock_rows.call_args.args
        )

    def test_cost_per_unit_divides_by_what_was_actually_produced(self):
        out, _ = self._run(transfer_rows(900.0), produced_qty=45)

        self.assertEqual(20.0, out["cost_per_unit"])
        self.assertEqual(45.0, out["produced_qty"])

    def test_a_started_but_unfinished_batch_returns_nulls_not_a_division_error(self):
        out, _ = self._run(
            transfer_rows(900.0), produced_qty=0, bom={"total_cost": 500.0, "quantity": 50.0}
        )

        self.assertEqual(900.0, out["material_cost"])
        self.assertIsNone(out["cost_per_unit"])
        self.assertIsNone(out["standard_cost"])
        self.assertIsNone(out["variance_amount"])
        self.assertIsNone(out["variance_pct"])

    def test_an_uncosted_bom_returns_a_null_standard_not_a_fake_zero(self):
        """``total_cost == 0`` means nobody ever costed the BOM.

        Dividing it out would render every single batch as "infinitely over
        standard", which reads as a costing crisis rather than missing setup.
        """
        out, _ = self._run(
            transfer_rows(900.0), produced_qty=50, bom={"total_cost": 0.0, "quantity": 50.0}
        )

        self.assertEqual(18.0, out["cost_per_unit"])
        self.assertIsNone(out["standard_per_unit"])
        self.assertIsNone(out["variance_amount"])
        self.assertIsNone(out["variance_pct"])

    def test_a_bom_with_zero_yield_returns_a_null_standard(self):
        out, _ = self._run(
            transfer_rows(900.0), produced_qty=50, bom={"total_cost": 500.0, "quantity": 0.0}
        )

        self.assertIsNone(out["standard_per_unit"])

    def test_an_unreadable_bom_returns_a_null_standard(self):
        out, _ = self._run(transfer_rows(900.0), produced_qty=50, bom=None)

        self.assertIsNone(out["standard_per_unit"])
        self.assertEqual(18.0, out["cost_per_unit"])

    def test_variance_is_reported_in_money_and_percent(self):
        # BOM says 1000 for 50 units => 20/unit. The batch spent 1100 on 50.
        out, _ = self._run(
            transfer_rows(1100.0), produced_qty=50, bom={"total_cost": 1000.0, "quantity": 50.0}
        )

        self.assertEqual(20.0, out["standard_per_unit"])
        self.assertEqual(22.0, out["cost_per_unit"])
        self.assertEqual(1000.0, out["standard_cost"])
        self.assertEqual(100.0, out["variance_amount"])
        # almostEqual on the percentage only: 100/1000*100 lands on
        # 10.000000000000002 in float, which is a true answer and a false test.
        self.assertAlmostEqual(10.0, out["variance_pct"])

    def test_a_favourable_variance_is_negative(self):
        out, _ = self._run(
            transfer_rows(900.0), produced_qty=50, bom={"total_cost": 1000.0, "quantity": 50.0}
        )

        self.assertEqual(-100.0, out["variance_amount"])
        self.assertAlmostEqual(-10.0, out["variance_pct"])

    def test_a_scaled_bom_yield_is_honoured(self):
        """A BOM that makes 10 at a time costs 1/10th of its total per unit."""
        out, _ = self._run(
            transfer_rows(1000.0), produced_qty=100, bom={"total_cost": 90.0, "quantity": 10.0}
        )

        self.assertEqual(9.0, out["standard_per_unit"])
        self.assertEqual(10.0, out["cost_per_unit"])

    def test_no_transfers_yet_costs_nothing_rather_than_raising(self):
        out, _ = self._run([], produced_qty=0)

        self.assertEqual(0.0, out["material_cost"])
        self.assertIsNone(out["cost_per_unit"])
        self.assertEqual([], out["transfer_entries"])

    def test_transfer_entries_are_listed_once_each(self):
        rows = transfer_rows(100.0, 200.0)
        rows[1]["stock_entry"] = rows[0]["stock_entry"]

        out, _ = self._run(rows, produced_qty=10)

        self.assertEqual(["STE-1"], out["transfer_entries"])
        self.assertEqual(300.0, out["material_cost"])

    def test_junk_amounts_do_not_take_the_whole_batch_down(self):
        rows = transfer_rows(100.0)
        rows.append({"stock_entry": "STE-2", "amount": None})

        out, _ = self._run(rows, produced_qty=10)

        self.assertEqual(100.0, out["material_cost"])


class TestGetBatchCostEndpoint(unittest.TestCase):
    def test_reads_produced_qty_and_bom_off_the_work_order(self):
        from jarz_pos.api import manufacturing

        with patch("jarz_pos.api.manufacturing._ensure_production_view_access") as mock_gate, patch(
            "jarz_pos.api.manufacturing._resolve_work_order_doc",
            return_value=SimpleNamespace(produced_qty=47.0, bom_no="BOM-PIST-CAKE"),
        ), patch(
            "jarz_pos.api.manufacturing._compute_batch_cost", return_value={"material_cost": 900.0}
        ) as mock_cost, passthrough_translate(), patch("jarz_pos.api.manufacturing.frappe"):
            out = manufacturing.get_batch_cost("WO-0001")

        mock_gate.assert_called_once()
        mock_cost.assert_called_once_with(
            "WO-0001", produced_qty=47.0, bom_no="BOM-PIST-CAKE"
        )
        self.assertEqual(900.0, out["material_cost"])

    def test_requires_a_work_order(self):
        from jarz_pos.api import manufacturing

        with patch("jarz_pos.api.manufacturing._ensure_production_view_access"), patch(
            "jarz_pos.api.manufacturing._resolve_work_order_doc"
        ) as mock_doc, passthrough_translate(), patch(
            "jarz_pos.api.manufacturing.frappe"
        ) as mock_frappe:
            wire_throw(mock_frappe)
            with self.assertRaises(Thrown):
                manufacturing.get_batch_cost("   ")

        mock_doc.assert_not_called()

    def test_is_gated_on_production_view_not_manager(self):
        """Costing is read-only; an operator seeing what the batch cost is fine."""
        from jarz_pos.api import manufacturing

        with patch(
            "jarz_pos.api.manufacturing._ensure_production_view_access",
            side_effect=Thrown("nope"),
        ), patch("jarz_pos.api.manufacturing._resolve_work_order_doc") as mock_doc, patch(
            "jarz_pos.api.manufacturing.frappe"
        ):
            with self.assertRaises(Thrown):
                manufacturing.get_batch_cost("WO-0001")

        mock_doc.assert_not_called()


class TestElapsedMinutes(unittest.TestCase):
    def test_rounds_to_one_decimal(self):
        from jarz_pos.api import manufacturing

        with patch("jarz_pos.api.manufacturing.frappe"):
            minutes = manufacturing._elapsed_minutes(
                datetime(2026, 8, 2, 8, 0, 0), datetime(2026, 8, 2, 9, 45, 30)
            )

        self.assertEqual(105.5, minutes)

    def test_a_missing_start_stamp_is_none_not_zero(self):
        from jarz_pos.api import manufacturing

        with patch("jarz_pos.api.manufacturing.frappe"):
            self.assertIsNone(manufacturing._elapsed_minutes(None))
            self.assertIsNone(manufacturing._elapsed_minutes(""))

    def test_junk_degrades_to_none(self):
        from jarz_pos.api import manufacturing

        with patch("jarz_pos.api.manufacturing.frappe"):
            self.assertIsNone(
                manufacturing._elapsed_minutes("not a date", datetime(2026, 8, 2, 9, 0, 0))
            )


class TestFloatCoercion(unittest.TestCase):
    def test_flt_degrades_instead_of_raising(self):
        from jarz_pos.api import manufacturing

        self.assertEqual(0.0, manufacturing._flt(None))
        self.assertEqual(0.0, manufacturing._flt("nonsense"))
        self.assertEqual(0.0, manufacturing._flt(object()))
        self.assertEqual(3.5, manufacturing._flt("3.5"))
        self.assertEqual(7.0, manufacturing._flt(7))
