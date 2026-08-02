"""Unit tests for the split start/finish production flow (Stage 2).

Follows the ``test_api_manufacturing_precheck`` pattern: pure
``unittest.TestCase``, module imported lazily inside each test body, and the
module-level ``frappe`` symbol patched wholesale so nothing reaches a database.

Deliberately NOT modelled on ``test_api_manufacturing.py`` — that module calls
the endpoints with the wrong signatures and swallows the TypeError in a bare
``except``, so it passes without testing anything.
"""

import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


SCHEDULED = datetime(2026, 8, 2, 9, 30, 0)


class Thrown(Exception):
    """What a wired-up ``frappe.throw`` raises in these tests."""


def passthrough_translate():
    """Neutralise ``from frappe import _``.

    ``patch("...manufacturing.frappe")`` does not cover that binding, and the
    real translator needs a request context to resolve a language.
    """
    return patch("jarz_pos.api.manufacturing._", new=lambda msg: msg)


def wire_throw(mock_frappe):
    """Make ``frappe.throw`` actually raise, carrying its message.

    Without this a bare ``MagicMock`` returns quietly and the code under test
    walks straight past its own guard — which would make every rejection test a
    false pass.  A bare ``side_effect = Thrown`` is not enough either: mock
    raises the class with no arguments, so the message never reaches the test.
    """

    def _throw(message, *_args, **_kwargs):
        raise Thrown(str(message))

    mock_frappe.throw.side_effect = _throw
    return mock_frappe


def work_order(**overrides):
    """A submitted Work Order with material already in WIP."""
    doc = {
        "name": "WO-0001",
        "docstatus": 1,
        "company": "Jarz Co",
        "production_item": "PIST-CAKE",
        "bom_no": "BOM-PIST-CAKE",
        "qty": 50.0,
        "produced_qty": 0.0,
        "material_transferred_for_manufacturing": 50.0,
        "status": "In Process",
        "stock_uom": "Nos",
        "wip_warehouse": "WIP - J",
        "fg_warehouse": "Finished Goods - J",
        "source_warehouse": "Raw Material - J",
    }
    doc.update(overrides)
    return SimpleNamespace(**doc)


PRICED = {
    "components": [
        {
            "item_code": "FLOUR",
            "item_name": "Flour",
            "uom": "Kg",
            "required_qty": 12.0,
            "source_warehouse": "Raw Material - J",
            "valuation_rate": 20.0,
            "estimated_amount": 240.0,
        }
    ],
    "total_value": 240.0,
}


class TestStartProductionBatch(unittest.TestCase):
    def _run(self, mock_se, **overrides):
        from jarz_pos.api import manufacturing

        kwargs = {"item_code": "PIST-CAKE", "bom_name": "BOM-PIST-CAKE", "item_qty": 5}
        kwargs.update(overrides)

        with patch("jarz_pos.api.manufacturing._ensure_production_execute_access"), patch(
            "jarz_pos.api.manufacturing._get_bom_company", return_value="Jarz Co"
        ), patch("jarz_pos.api.manufacturing._assert_posting_date_allowed"), patch(
            "jarz_pos.api.manufacturing._assert_material_availability"
        ), patch(
            "jarz_pos.api.manufacturing._assert_batch_value_within_threshold", return_value=PRICED
        ), patch(
            "jarz_pos.api.manufacturing._get_mfg_defaults", return_value={}
        ), patch(
            "jarz_pos.api.manufacturing._resolve_work_order_warehouses",
            return_value={"wip_warehouse": "WIP - J", "fg_warehouse": "Finished Goods - J"},
        ), patch(
            "jarz_pos.api.manufacturing._resolve_scheduled_datetime", return_value=SCHEDULED
        ), patch(
            "jarz_pos.api.manufacturing._ensure_work_order", return_value="WO-0001"
        ) as mock_ensure_wo, patch(
            "jarz_pos.api.manufacturing._make_and_submit_se", mock_se
        ), patch(
            "jarz_pos.api.manufacturing._stamp_work_order"
        ) as mock_stamp, patch(
            "jarz_pos.api.manufacturing._resolve_current_user", return_value="ops@jarz.test"
        ), patch(
            # Pinned so the stamp assertions stay about start/finish. The SOP
            # stamp has its own coverage in TestSopVersionStamp; left unpatched
            # it resolves through the frappe mock and leaks a MagicMock repr
            # into the stamped values.
            "jarz_pos.api.manufacturing._resolve_active_sop_stamp", return_value=""
        ), patch(
            "jarz_pos.api.manufacturing._resolve_work_order_doc", return_value=work_order()
        ), passthrough_translate(), patch(
            "jarz_pos.api.manufacturing.frappe"
        ) as mock_frappe:
            wire_throw(mock_frappe)
            result = manufacturing.start_production_batch(**kwargs)

        return result, mock_ensure_wo, mock_stamp

    def test_posts_exactly_one_material_transfer_and_no_manufacture_entry(self):
        """The core regression.

        Starting a batch must move material into WIP and stop there. A second
        Stock Entry here would silently restore the old quick-produce behaviour:
        finished goods booked hours before anything was actually made.
        """
        mock_se = MagicMock(return_value="STE-TRANSFER")

        result, _, _ = self._run(mock_se)

        self.assertEqual(1, mock_se.call_count)
        self.assertEqual(
            ("WO-0001", "Material Transfer for Manufacture", 5.0, SCHEDULED),
            mock_se.call_args.args,
        )
        self.assertEqual(
            {"Material Transfer for Manufacture"},
            {c.args[1] for c in mock_se.call_args_list},
        )
        self.assertEqual("STE-TRANSFER", result["material_transfer"])
        self.assertNotIn("manufacture_entry", result)

    def test_returns_the_planned_batch_shape_the_board_renders(self):
        result, _, _ = self._run(MagicMock(return_value="STE-TRANSFER"))

        self.assertEqual("WO-0001", result["work_order"])
        self.assertEqual(5.0, result["planned_qty"])
        self.assertEqual("WIP - J", result["wip_warehouse"])
        self.assertEqual("Finished Goods - J", result["fg_warehouse"])
        self.assertEqual(240.0, result["estimated_material_cost"])
        self.assertEqual(1, len(result["components"]))
        self.assertEqual("FLOUR", result["components"][0]["item_code"])
        self.assertEqual(240.0, result["components"][0]["estimated_amount"])

    def test_stamps_who_started_the_batch_and_when(self):
        _, _, mock_stamp = self._run(MagicMock(return_value="STE-TRANSFER"))

        mock_stamp.assert_called_once_with(
            "WO-0001", {"jarz_started_by": "ops@jarz.test", "jarz_started_at": SCHEDULED}
        )

    def test_rejects_a_non_positive_quantity(self):
        for bad in (0, -3, "0"):
            with self.subTest(qty=bad):
                with self.assertRaises(Thrown):
                    self._run(MagicMock(), item_qty=bad)

    def test_guards_run_before_any_work_order_exists(self):
        """A blocked start must leave nothing behind.

        Order matters: the Work Order is submitted and cannot be deleted, so a
        guard that fired after ``_ensure_work_order`` would litter the system
        with orphan submitted documents every time an operator got refused.
        """
        from jarz_pos.api import manufacturing

        for guard in (
            "_assert_posting_date_allowed",
            "_assert_material_availability",
            "_assert_batch_value_within_threshold",
        ):
            with self.subTest(guard=guard):
                with patch(
                    "jarz_pos.api.manufacturing._ensure_production_execute_access"
                ), patch(
                    "jarz_pos.api.manufacturing._get_bom_company", return_value="Jarz Co"
                ), patch(
                    "jarz_pos.api.manufacturing._assert_posting_date_allowed"
                ), patch(
                    "jarz_pos.api.manufacturing._assert_material_availability"
                ), patch(
                    "jarz_pos.api.manufacturing._assert_batch_value_within_threshold",
                    return_value=PRICED,
                ), patch(
                    f"jarz_pos.api.manufacturing.{guard}", side_effect=Thrown("blocked")
                ), patch(
                    "jarz_pos.api.manufacturing._resolve_scheduled_datetime",
                    return_value=SCHEDULED,
                ), patch(
                    "jarz_pos.api.manufacturing._ensure_work_order"
                ) as mock_ensure_wo, patch(
                    "jarz_pos.api.manufacturing._make_and_submit_se"
                ) as mock_se, passthrough_translate(), patch(
                    "jarz_pos.api.manufacturing.frappe"
                ) as mock_frappe:
                    wire_throw(mock_frappe)
                    with self.assertRaises(Thrown):
                        manufacturing.start_production_batch("PIST-CAKE", "BOM-PIST-CAKE", 5)

                mock_ensure_wo.assert_not_called()
                mock_se.assert_not_called()


class TestFinishProductionBatch(unittest.TestCase):
    def _run(self, wo=None, refreshed=None, allow_over=False, order=None, **kwargs):
        """Drive ``finish_production_batch`` and record the call order.

        Deliberately does NOT swallow ``Thrown`` — a helper that ate the
        rejection would turn every guard test into a false pass.  Callers that
        need the trace of a rejected run pass their own ``order`` list in.
        """
        from jarz_pos.api import manufacturing

        wo = wo or work_order()
        order = [] if order is None else order

        def _lock(name, for_update=False):
            order.append(("lock", name, for_update))
            return wo

        def _se(*args, **_kw):
            order.append(("stock_entry", args[1], args[2]))
            return "STE-MANUFACTURE"

        call = {"work_order": "WO-0001", "actual_qty": 47}
        call.update(kwargs)

        with patch("jarz_pos.api.manufacturing._ensure_production_execute_access"), patch(
            "jarz_pos.api.manufacturing._resolve_work_order_doc", side_effect=_lock
        ) as mock_lock, patch(
            "jarz_pos.api.manufacturing._setting_flag", return_value=allow_over
        ), patch(
            "jarz_pos.api.manufacturing._resolve_scheduled_datetime", return_value=SCHEDULED
        ), patch(
            "jarz_pos.api.manufacturing._assert_posting_date_allowed"
        ), patch(
            "jarz_pos.api.manufacturing._make_and_submit_se", side_effect=_se
        ) as mock_se, patch(
            "jarz_pos.api.manufacturing._stamp_work_order"
        ) as mock_stamp, patch(
            "jarz_pos.api.manufacturing._resolve_current_user", return_value="ops@jarz.test"
        ), patch(
            "jarz_pos.api.manufacturing._set_work_order_actual_dates"
        ) as mock_dates, patch(
            "jarz_pos.api.manufacturing._resolve_update_work_order_status",
            return_value=refreshed,
        ) as mock_status, patch(
            "jarz_pos.api.manufacturing._compute_batch_cost", return_value={"material_cost": 900.0}
        ), passthrough_translate(), patch(
            "jarz_pos.api.manufacturing.frappe"
        ) as mock_frappe:
            wire_throw(mock_frappe)
            result = manufacturing.finish_production_batch(**call)

        return SimpleNamespace(
            result=result,
            order=order,
            lock=mock_lock,
            se=mock_se,
            stamp=mock_stamp,
            dates=mock_dates,
            status=mock_status,
        )

    def test_posts_the_actual_quantity_not_the_planned_one(self):
        """A batch of 50 that yields 47 must book 47.

        Booking the planned quantity is how finished-goods stock becomes a lie
        from the first short shift, and the difference never shows up anywhere.
        """
        run = self._run(
            wo=work_order(qty=50.0, material_transferred_for_manufacturing=50.0),
            refreshed=SimpleNamespace(status="Completed", produced_qty=47.0, bom_no="BOM-PIST-CAKE"),
            actual_qty=47,
        )

        self.assertEqual(1, run.se.call_count)
        self.assertEqual(
            ("WO-0001", "Manufacture", 47.0, SCHEDULED), run.se.call_args.args
        )
        self.assertEqual(47.0, run.result["actual_qty"])
        self.assertEqual("STE-MANUFACTURE", run.result["manufacture_entry"])

    def test_takes_the_row_lock_before_anything_is_mutated(self):
        """Two tablets finishing the same batch is an ordinary Tuesday."""
        run = self._run(
            refreshed=SimpleNamespace(status="Completed", produced_qty=47.0, bom_no="BOM-PIST-CAKE")
        )

        run.lock.assert_called_once_with("WO-0001", for_update=True)
        self.assertEqual(("lock", "WO-0001", True), run.order[0])
        self.assertEqual("stock_entry", run.order[1][0])

    def test_refuses_a_work_order_that_was_never_started(self):
        with self.assertRaises(Thrown) as ctx:
            self._run(wo=work_order(material_transferred_for_manufacturing=0.0))

        self.assertIn("never started", str(ctx.exception))

    def test_refuses_a_draft_work_order(self):
        with self.assertRaises(Thrown) as ctx:
            self._run(wo=work_order(docstatus=0))

        self.assertIn("not submitted", str(ctx.exception))

    def test_rejects_a_non_positive_actual_quantity(self):
        for bad in (0, -1, "0"):
            with self.subTest(actual_qty=bad):
                with self.assertRaises(Thrown):
                    self._run(actual_qty=bad)

    def test_rejects_over_production_while_the_setting_is_off(self):
        with self.assertRaises(Thrown) as ctx:
            self._run(
                wo=work_order(qty=50.0, material_transferred_for_manufacturing=70.0),
                actual_qty=60,
                allow_over=False,
            )

        self.assertIn("Over-production", str(ctx.exception))

    def test_allows_over_production_once_the_setting_is_on(self):
        run = self._run(
            wo=work_order(qty=50.0, material_transferred_for_manufacturing=70.0),
            refreshed=SimpleNamespace(status="Completed", produced_qty=60.0, bom_no="BOM-PIST-CAKE"),
            actual_qty=60,
            allow_over=True,
        )

        self.assertEqual(("WO-0001", "Manufacture", 60.0, SCHEDULED), run.se.call_args.args)

    def test_pre_validates_against_what_is_left_in_wip(self):
        """ERPNext throws here too, but about ``fg_completed_qty``.

        That message means nothing to somebody holding a tray, so the check is
        duplicated on our side and phrased for the floor.
        """
        with self.assertRaises(Thrown) as ctx:
            self._run(
                wo=work_order(qty=50.0, material_transferred_for_manufacturing=50.0, produced_qty=45.0),
                actual_qty=10,
            )

        message = str(ctx.exception)
        self.assertIn("still in WIP", message)

    def test_no_stock_entry_is_posted_when_a_guard_rejects(self):
        """Every rejection path must stop at the lock, having posted nothing."""
        cases = {
            "never started": {"wo": work_order(material_transferred_for_manufacturing=0.0)},
            "zero qty": {"actual_qty": 0},
            "over production": {
                "wo": work_order(qty=50.0, material_transferred_for_manufacturing=70.0),
                "actual_qty": 60,
            },
            "more than is in wip": {
                "wo": work_order(qty=50.0, material_transferred_for_manufacturing=50.0, produced_qty=45.0),
                "actual_qty": 10,
            },
        }
        for label, kwargs in cases.items():
            with self.subTest(case=label):
                order = []
                with self.assertRaises(Thrown):
                    self._run(order=order, **kwargs)

                self.assertEqual([("lock", "WO-0001", True)], order)

    def test_records_scrap_and_notes_without_moving_any_stock(self):
        """Scrap is a reported figure, not a stock movement.

        Routing it to a scrap warehouse would need per-item scrap items that do
        not exist; inventing them silently is worse than a number somebody can
        be asked about.
        """
        run = self._run(
            refreshed=SimpleNamespace(status="Completed", produced_qty=47.0, bom_no="BOM-PIST-CAKE"),
            scrap_qty=3,
            notes="two trays dropped",
        )

        self.assertEqual(1, run.se.call_count)
        self.assertEqual("Manufacture", run.se.call_args.args[1])
        run.stamp.assert_called_once_with(
            "WO-0001",
            {
                "jarz_finished_by": "ops@jarz.test",
                "jarz_finished_at": SCHEDULED,
                "jarz_scrap_qty": 3.0,
                "jarz_batch_notes": "two trays dropped",
            },
        )
        self.assertEqual(3.0, run.result["scrap_qty"])

    def test_surfaces_the_material_stranded_in_wip(self):
        """50 transferred, 47 made — the other 3 units' worth is still in WIP."""
        run = self._run(
            wo=work_order(qty=50.0, material_transferred_for_manufacturing=50.0),
            refreshed=SimpleNamespace(status="Completed", produced_qty=47.0, bom_no="BOM-PIST-CAKE"),
            actual_qty=47,
        )

        self.assertEqual(3.0, run.result["wip_leftover_qty"])
        self.assertEqual(47.0, run.result["produced_qty"])
        self.assertEqual("Completed", run.result["status"])

    def test_refreshes_actual_dates_and_status_after_posting(self):
        run = self._run(
            refreshed=SimpleNamespace(status="Completed", produced_qty=47.0, bom_no="BOM-PIST-CAKE")
        )

        run.dates.assert_called_once_with("WO-0001", SCHEDULED)
        run.status.assert_called_once_with("WO-0001")

    def test_requires_a_work_order(self):
        from jarz_pos.api import manufacturing

        with patch("jarz_pos.api.manufacturing._ensure_production_execute_access"), patch(
            "jarz_pos.api.manufacturing._resolve_work_order_doc"
        ) as mock_lock, passthrough_translate(), patch(
            "jarz_pos.api.manufacturing.frappe"
        ) as mock_frappe:
            wire_throw(mock_frappe)
            with self.assertRaises(Thrown):
                manufacturing.finish_production_batch("  ", 5)

        mock_lock.assert_not_called()


class TestListRunningWorkOrders(unittest.TestCase):
    ROW = {
        "name": "WO-0001",
        "production_item": "PIST-CAKE",
        "item_name": "Pistachio Cheesecake",
        "qty": 50.0,
        "produced_qty": 0.0,
        "bom_no": "BOM-PIST-CAKE",
        "status": "In Process",
        "wip_warehouse": "WIP - J",
        "fg_warehouse": "Finished Goods - J",
        "stock_uom": "Nos",
        "material_transferred_for_manufacturing": 50.0,
        "jarz_started_by": "ops@jarz.test",
        "jarz_started_at": datetime(2026, 8, 2, 8, 0, 0),
    }

    def _run(self, rows, limit=50):
        from jarz_pos.api import manufacturing

        with patch("jarz_pos.api.manufacturing._ensure_production_view_access"), patch(
            "jarz_pos.api.manufacturing._fetch_running_work_orders", return_value=rows
        ) as mock_fetch, patch(
            "jarz_pos.api.manufacturing._resolve_now_datetime",
            return_value=datetime(2026, 8, 2, 9, 30, 0),
        ), patch("jarz_pos.api.manufacturing.frappe"):
            out = manufacturing.list_running_work_orders(limit)

        return out, mock_fetch

    def test_filters_on_docstatus_status_and_actual_wip_transfer(self):
        """A Work Order with nothing in WIP is not a batch anybody is standing over."""
        _, mock_fetch = self._run([])

        filters = mock_fetch.call_args.args[0]
        self.assertEqual(1, filters["docstatus"])
        self.assertEqual(["in", ["Not Started", "In Process"]], filters["status"])
        self.assertEqual([">", 0], filters["material_transferred_for_manufacturing"])

    def test_reports_elapsed_time_and_stranded_wip(self):
        out, _ = self._run([dict(self.ROW, produced_qty=20.0)])

        self.assertEqual(1, len(out))
        self.assertEqual(90.0, out[0]["elapsed_minutes"])
        self.assertEqual(30.0, out[0]["wip_leftover_qty"])
        self.assertEqual("ops@jarz.test", out[0]["jarz_started_by"])
        self.assertEqual("Nos", out[0]["stock_uom"])

    def test_a_batch_with_no_start_stamp_reports_no_elapsed_time(self):
        """Rather than a bogus zero — the stamp is best-effort by design."""
        out, _ = self._run([dict(self.ROW, jarz_started_at=None)])

        self.assertIsNone(out[0]["elapsed_minutes"])

    def test_limit_is_coerced_and_capped(self):
        from jarz_pos.api import manufacturing

        _, mock_fetch = self._run([], limit="nonsense")
        self.assertEqual(50, mock_fetch.call_args.args[1])

        _, mock_fetch = self._run([], limit=99999)
        self.assertEqual(manufacturing.MAX_RUNNING_WORK_ORDERS, mock_fetch.call_args.args[1])

    def test_missing_jarz_columns_degrade_loudly_not_silently(self):
        """Between deploy and migrate the custom fields do not exist yet.

        Losing two columns beats a 500, but a silent fallback is exactly how a
        v16 rejection hid for a month — so it has to reach the Error Log.
        """
        from jarz_pos.api import manufacturing

        with patch("jarz_pos.api.manufacturing.frappe") as mock_frappe:
            mock_frappe.get_all.side_effect = [Exception("Unknown column"), [self.ROW]]

            rows = manufacturing._fetch_running_work_orders({"docstatus": 1}, 50)

        self.assertEqual([self.ROW], rows)
        mock_frappe.log_error.assert_called_once()
        self.assertEqual(
            manufacturing.RUNNING_WORK_ORDER_FIELDS,
            mock_frappe.get_all.call_args.kwargs["fields"],
        )


class TestReturnWipToStore(unittest.TestCase):
    def test_manager_only_and_posts_a_plain_material_transfer_out_of_wip(self):
        from jarz_pos.api import manufacturing

        created = MagicMock()
        created.name = "STE-RETURN"

        with patch("jarz_pos.api.manufacturing._ensure_manager_access") as mock_gate, patch(
            "jarz_pos.api.manufacturing._resolve_work_order_doc", return_value=work_order()
        ), patch(
            "jarz_pos.api.manufacturing._get_wip_leftover_rows",
            return_value=[{"item_code": "FLOUR", "warehouse": "WIP - J", "qty": 4.0}],
        ), patch(
            "jarz_pos.api.manufacturing._resolve_work_order_source_warehouses",
            return_value={"FLOUR": "Raw Material - J"},
        ), patch(
            "jarz_pos.api.manufacturing._resolve_now_datetime", return_value=SCHEDULED
        ), passthrough_translate(), patch(
            "jarz_pos.api.manufacturing.frappe"
        ) as mock_frappe:
            wire_throw(mock_frappe)
            mock_frappe.get_doc.return_value = created

            out = manufacturing.return_wip_to_store("WO-0001")

        mock_gate.assert_called_once()
        payload = mock_frappe.get_doc.call_args.args[0]
        self.assertEqual("Material Transfer", payload["purpose"])
        self.assertEqual("WO-0001", payload["work_order"])
        self.assertEqual(
            [{"item_code": "FLOUR", "qty": 4.0, "s_warehouse": "WIP - J", "t_warehouse": "Raw Material - J"}],
            payload["items"],
        )
        self.assertEqual("STE-RETURN", out["stock_entry"])
        created.submit.assert_called_once()

    def test_refuses_when_wip_is_already_empty(self):
        from jarz_pos.api import manufacturing

        with patch("jarz_pos.api.manufacturing._ensure_manager_access"), patch(
            "jarz_pos.api.manufacturing._resolve_work_order_doc", return_value=work_order()
        ), patch(
            "jarz_pos.api.manufacturing._get_wip_leftover_rows", return_value=[]
        ), passthrough_translate(), patch(
            "jarz_pos.api.manufacturing.frappe"
        ) as mock_frappe:
            wire_throw(mock_frappe)
            with self.assertRaises(Thrown):
                manufacturing.return_wip_to_store("WO-0001")

            mock_frappe.get_doc.assert_not_called()

    def test_nets_off_returns_already_posted(self):
        """ERPNext's WIP reconciliation ignores plain Material Transfers.

        Without this, a second call would try to move stock that already went
        back to the store — and succeed, driving the store negative.
        """
        from jarz_pos.api import manufacturing

        with patch(
            "jarz_pos.api.manufacturing._resolve_wip_available_materials",
            return_value=[{"item_code": "FLOUR", "warehouse": "WIP - J", "qty": 4.0}],
        ), patch(
            "jarz_pos.api.manufacturing._resolve_stock_entry_detail_rows",
            return_value=[{"item_code": "FLOUR", "s_warehouse": "WIP - J", "qty": 4.0}],
        ), patch("jarz_pos.api.manufacturing.frappe"):
            self.assertEqual([], manufacturing._get_wip_leftover_rows("WO-0001"))

    def test_reports_a_partial_return(self):
        from jarz_pos.api import manufacturing

        with patch(
            "jarz_pos.api.manufacturing._resolve_wip_available_materials",
            return_value=[{"item_code": "FLOUR", "warehouse": "WIP - J", "qty": 4.0}],
        ), patch(
            "jarz_pos.api.manufacturing._resolve_stock_entry_detail_rows",
            return_value=[{"item_code": "FLOUR", "s_warehouse": "WIP - J", "qty": 1.5}],
        ), patch("jarz_pos.api.manufacturing.frappe"):
            rows = manufacturing._get_wip_leftover_rows("WO-0001")

        self.assertEqual(1, len(rows))
        self.assertAlmostEqual(2.5, rows[0]["qty"])


class TestQuickProducePathUnchanged(unittest.TestCase):
    def test_submit_work_orders_still_posts_both_entries(self):
        """The manager "Quick produce" path is explicitly not part of Stage 2."""
        from jarz_pos.api import manufacturing

        line = {"item_code": "PIST-CAKE", "bom_name": "BOM-PIST-CAKE", "item_qty": 5}

        with patch("jarz_pos.api.manufacturing._ensure_manager_access"), patch(
            "jarz_pos.api.manufacturing._get_basket_shortages", return_value=[]
        ), patch("jarz_pos.api.manufacturing._assert_material_availability"), patch(
            "jarz_pos.api.manufacturing._get_bom_company", return_value="Jarz Co"
        ), patch(
            "jarz_pos.api.manufacturing._get_mfg_defaults", return_value={}
        ), patch(
            "jarz_pos.api.manufacturing._resolve_work_order_warehouses", return_value={}
        ), patch(
            "jarz_pos.api.manufacturing._resolve_scheduled_datetime", return_value=SCHEDULED
        ), patch(
            "jarz_pos.api.manufacturing._ensure_work_order", return_value="WO-0001"
        ), patch(
            "jarz_pos.api.manufacturing._make_and_submit_se", side_effect=["STE-1", "STE-2"]
        ) as mock_se, patch(
            "jarz_pos.api.manufacturing._set_work_order_actual_dates"
        ), patch("jarz_pos.api.manufacturing.frappe"):
            manufacturing.submit_work_orders([line])

        self.assertEqual(
            ["Material Transfer for Manufacture", "Manufacture"],
            [c.args[1] for c in mock_se.call_args_list],
        )


class TestSopVersionStamp(unittest.TestCase):
    """The write side of the SOP version contract.

    Stage 3 reads `Work Order.jarz_sop_version` in two places — the stamped-
    version lookup and the "this SOP is already used in production" edit guard.
    Both are inert unless start_production_batch writes it, so these tests exist
    to keep the seam from silently reopening.
    """

    def test_stamp_uses_the_shared_format_helper(self):
        from jarz_pos.api import manufacturing
        from jarz_pos.services.sop_rendering import format_version_stamp

        with patch("jarz_pos.api.manufacturing.frappe") as mock_frappe:
            mock_frappe.db.get_value.return_value = {"name": "SOP-0007", "version": 3}

            stamp = manufacturing._resolve_active_sop_stamp("PIST-CAKE")

        # Must match what api/sop.py and jarz_sop.validate() parse.
        self.assertEqual(format_version_stamp("SOP-0007", 3), stamp)
        self.assertEqual("SOP-0007#3", stamp)

    def test_no_active_sop_yields_an_empty_stamp(self):
        from jarz_pos.api import manufacturing

        with patch("jarz_pos.api.manufacturing.frappe") as mock_frappe:
            mock_frappe.db.get_value.return_value = None
            self.assertEqual("", manufacturing._resolve_active_sop_stamp("NO-SOP"))

    def test_a_failing_lookup_never_blocks_the_batch(self):
        from jarz_pos.api import manufacturing

        with patch("jarz_pos.api.manufacturing.frappe") as mock_frappe:
            mock_frappe.db.get_value.side_effect = Exception("table missing")
            # Degrades to "unstamped", i.e. the pre-Stage-3 behaviour.
            self.assertEqual("", manufacturing._resolve_active_sop_stamp("PIST-CAKE"))

    def test_start_stamps_the_version_onto_the_work_order(self):
        from jarz_pos.api import manufacturing

        with patch("jarz_pos.api.manufacturing._ensure_production_execute_access"), patch(
            "jarz_pos.api.manufacturing._assert_posting_date_allowed"
        ), patch("jarz_pos.api.manufacturing._assert_material_availability"), patch(
            "jarz_pos.api.manufacturing._assert_batch_value_within_threshold"
        ), patch(
            "jarz_pos.api.manufacturing._get_bom_company", return_value="Jarz Co"
        ), patch("jarz_pos.api.manufacturing._get_mfg_defaults", return_value={}), patch(
            "jarz_pos.api.manufacturing._resolve_work_order_warehouses", return_value={}
        ), patch(
            "jarz_pos.api.manufacturing._ensure_work_order", return_value="WO-9001"
        ), patch(
            "jarz_pos.api.manufacturing._make_and_submit_se", return_value="STE-1"
        ), patch(
            "jarz_pos.api.manufacturing._resolve_current_user", return_value="op@jarz"
        ), patch(
            "jarz_pos.api.manufacturing._resolve_active_sop_stamp",
            return_value="SOP-0007#3",
        ), patch(
            "jarz_pos.api.manufacturing._stamp_work_order"
        ) as mock_stamp, patch("jarz_pos.api.manufacturing.frappe"):
            manufacturing.start_production_batch("PIST-CAKE", "BOM-PIST", 10)

        stamped = mock_stamp.call_args.args[1]
        self.assertEqual("SOP-0007#3", stamped["jarz_sop_version"])
        self.assertEqual("op@jarz", stamped["jarz_started_by"])

    def test_an_item_without_an_sop_is_stamped_with_nothing_at_all(self):
        from jarz_pos.api import manufacturing

        with patch("jarz_pos.api.manufacturing._ensure_production_execute_access"), patch(
            "jarz_pos.api.manufacturing._assert_posting_date_allowed"
        ), patch("jarz_pos.api.manufacturing._assert_material_availability"), patch(
            "jarz_pos.api.manufacturing._assert_batch_value_within_threshold"
        ), patch(
            "jarz_pos.api.manufacturing._get_bom_company", return_value="Jarz Co"
        ), patch("jarz_pos.api.manufacturing._get_mfg_defaults", return_value={}), patch(
            "jarz_pos.api.manufacturing._resolve_work_order_warehouses", return_value={}
        ), patch(
            "jarz_pos.api.manufacturing._ensure_work_order", return_value="WO-9002"
        ), patch(
            "jarz_pos.api.manufacturing._make_and_submit_se", return_value="STE-1"
        ), patch(
            "jarz_pos.api.manufacturing._resolve_current_user", return_value="op@jarz"
        ), patch(
            "jarz_pos.api.manufacturing._resolve_active_sop_stamp", return_value=""
        ), patch(
            "jarz_pos.api.manufacturing._stamp_work_order"
        ) as mock_stamp, patch("jarz_pos.api.manufacturing.frappe"):
            manufacturing.start_production_batch("NO-SOP", "BOM-NOSOP", 10)

        # An empty key would stamp "" and defeat the LIKE guard's own emptiness
        # check further down the line.
        self.assertNotIn("jarz_sop_version", mock_stamp.call_args.args[1])
