"""Unit tests for the SOP / work-instruction API.

Same pattern as ``test_api_production``: pure ``unittest.TestCase``, module
imported lazily inside each test body, and every resolver patched so nothing
reaches a database.
"""

import re
import unittest
from contextlib import ExitStack
from unittest.mock import MagicMock, patch


def passthrough_translate():
    """Neutralise ``from frappe import _`` for tests that trip ``frappe.throw``.

    ``patch("...sop.frappe")`` does not cover that binding, and the real
    translator needs a request context to resolve a language.
    """
    return patch("jarz_pos.api.sop._", new=lambda msg: msg)


_TAGS = re.compile(r"<[^>]+>")


def fake_strip_html(html):
    return _TAGS.sub("", html or "")


class StubRow(dict):
    """A child row that answers to both attribute and key access."""

    def __getattr__(self, item):
        try:
            return self[item]
        except KeyError as exc:  # pragma: no cover - defensive
            raise AttributeError(item) from exc


class StubDoc:
    def __init__(self, **fields):
        self.__dict__.update(fields)


# One BOM batch of the cake: 1.83 Kg of spread, 0.5 Kg of flour.
MATERIAL_ROWS = [
    {"item_code": "PIST-SPR", "item_name": "Pistachio spread", "uom": "Kg", "required_qty": 1.83},
    {"item_code": "FLOUR", "item_name": "Cake flour", "uom": "Kg", "required_qty": 0.5},
]


def sop_doc(name="SOP-0001", version=1, **overrides):
    fields = {
        "name": name,
        "item_code": "PIST-CAKE",
        "item_name": "Pistachio cake",
        "bom": "BOM-PIST-CAKE-001",
        "version": version,
        "yield_percent": 95,
        "prep_time_mins": 20,
        "equipment": "Planetary mixer",
        "steps": [
            StubRow(
                step_no=1,
                idx=1,
                title="Weigh the spread",
                instruction="<p>Weigh {{item:PIST-SPR}} into the bowl.</p>",
                image="/files/step1.png",
                duration_mins=4,
                scaling_mode="Per Batch",
                requires_confirmation=1,
                capture_type="Number",
                capture_label="Weighed (kg)",
                capture_min=1.5,
                capture_max=2.5,
            ),
            StubRow(
                step_no=2,
                idx=2,
                title="Preheat",
                instruction="<p>Preheat to 180C.</p>",
                image=None,
                duration_mins=15,
                scaling_mode="Fixed",
                requires_confirmation=0,
                capture_type="None",
                capture_label=None,
                capture_min=0,
                capture_max=0,
            ),
        ],
    }
    fields.update(overrides)
    return StubDoc(**fields)


class SopApiTestCase(unittest.TestCase):
    """Shared harness: patches every resolver the payload builder reaches."""

    def _stack(
        self,
        stack,
        *,
        active_sop="SOP-0001",
        doc=None,
        bom_row=None,
        material_rows=None,
        translate=False,
    ):
        stack.enter_context(patch("jarz_pos.api.sop._ensure_production_view_access"))
        mocks = {
            "active": stack.enter_context(
                patch("jarz_pos.api.sop._resolve_active_sop", return_value=active_sop)
            ),
            "doc": stack.enter_context(
                patch("jarz_pos.api.sop._resolve_sop_doc", return_value=doc or sop_doc())
            ),
            "bom": stack.enter_context(
                patch(
                    "jarz_pos.api.sop._resolve_bom_row",
                    return_value=bom_row if bom_row is not None else {"quantity": 10, "company": "Jarz Co"},
                )
            ),
            "default_bom": stack.enter_context(
                patch("jarz_pos.api.sop._resolve_default_bom", return_value=None)
            ),
            "materials": stack.enter_context(
                patch(
                    "jarz_pos.api.sop._resolve_required_material_rows",
                    return_value=MagicMock(
                        return_value=MATERIAL_ROWS if material_rows is None else material_rows
                    ),
                )
            ),
            "strip": stack.enter_context(
                patch("jarz_pos.api.sop._resolve_strip_html", return_value=fake_strip_html)
            ),
        }
        if translate:
            stack.enter_context(passthrough_translate())
        mocks["frappe"] = stack.enter_context(patch("jarz_pos.api.sop.frappe"))
        return mocks


class TestGetSopForItem(SopApiTestCase):
    def test_missing_sop_returns_has_sop_false_without_throwing(self):
        # The board calls this for every card and most items have no SOP.  A
        # throw here would blank the whole screen instead of one card.
        from jarz_pos.api import sop

        with ExitStack() as stack:
            mocks = self._stack(stack, active_sop=None)
            result = sop.get_sop_for_item("PLAIN-ITEM")

        self.assertEqual({"has_sop": False, "item_code": "PLAIN-ITEM"}, result)
        mocks["doc"].assert_not_called()
        mocks["frappe"].throw.assert_not_called()

    def test_a_blank_item_code_is_not_an_error_either(self):
        from jarz_pos.api import sop

        with ExitStack() as stack:
            mocks = self._stack(stack, active_sop=None)
            result = sop.get_sop_for_item("")

        self.assertFalse(result["has_sop"])
        mocks["frappe"].throw.assert_not_called()

    def test_a_lookup_failure_degrades_to_has_sop_false_and_logs(self):
        from jarz_pos.api import sop

        with ExitStack() as stack:
            mocks = self._stack(stack)
            mocks["active"].side_effect = Exception("table is missing")

            result = sop.get_sop_for_item("PIST-CAKE")

        self.assertEqual({"has_sop": False, "item_code": "PIST-CAKE"}, result)
        # Degrade if you must, but never silently.
        mocks["frappe"].log_error.assert_called()

    def test_an_unrenderable_sop_degrades_to_has_sop_false_and_logs(self):
        from jarz_pos.api import sop

        with ExitStack() as stack:
            mocks = self._stack(stack)
            mocks["doc"].side_effect = Exception("corrupt doc")

            result = sop.get_sop_for_item("PIST-CAKE")

        self.assertEqual({"has_sop": False, "item_code": "PIST-CAKE"}, result)
        mocks["frappe"].log_error.assert_called()

    def test_a_broken_bom_still_returns_the_procedure(self):
        # Instructions without numbers beat no instructions; the unresolved
        # token is reported so the gap is visible.
        from jarz_pos.api import sop

        with ExitStack() as stack:
            mocks = self._stack(stack)
            mocks["materials"].return_value.side_effect = Exception("BOM exploded, badly")

            result = sop.get_sop_for_item("PIST-CAKE")

        self.assertTrue(result["has_sop"])
        self.assertEqual(["PIST-SPR"], result["unresolved_tokens"])
        self.assertIn("{{item:PIST-SPR}}", result["steps"][0]["instruction_html"])
        mocks["frappe"].log_error.assert_called()

    def test_returns_the_documented_shape(self):
        from jarz_pos.api import sop

        with ExitStack() as stack:
            self._stack(stack)
            result = sop.get_sop_for_item("PIST-CAKE")

        for key in (
            "has_sop",
            "sop",
            "version",
            "item_code",
            "item_name",
            "bom",
            "yield_percent",
            "prep_time_mins",
            "equipment",
            "batches",
            "units",
            "total_duration_mins",
            "steps",
            "unresolved_tokens",
        ):
            self.assertIn(key, result)

        step = result["steps"][0]
        for key in (
            "step_no",
            "title",
            "instruction_text",
            "instruction_html",
            "image_url",
            "duration_mins",
            "scaling_mode",
            "requires_confirmation",
            "capture_type",
            "capture_label",
            "capture_min",
            "capture_max",
        ):
            self.assertIn(key, step)

    def test_one_batch_renders_the_authored_quantities(self):
        from jarz_pos.api import sop

        with ExitStack() as stack:
            self._stack(stack)
            result = sop.get_sop_for_item("PIST-CAKE")

        self.assertEqual(1.0, result["batches"])
        self.assertEqual(10.0, result["units"])  # one batch of a BOM that yields 10
        self.assertIn("1.830 Kg Pistachio spread", result["steps"][0]["instruction_html"])

    def test_three_batches_triple_every_quantity(self):
        from jarz_pos.api import sop

        with ExitStack() as stack:
            self._stack(stack)
            result = sop.get_sop_for_item("PIST-CAKE", batches=3)

        self.assertEqual(3.0, result["batches"])
        self.assertEqual(30.0, result["units"])
        self.assertIn("5.490 Kg Pistachio spread", result["steps"][0]["instruction_html"])
        # 4 mins Per Batch x3, plus a 15 min Fixed preheat
        self.assertEqual(27.0, result["total_duration_mins"])

    def test_batches_arrives_as_the_string_http_sends(self):
        from jarz_pos.api import sop

        with ExitStack() as stack:
            self._stack(stack)
            result = sop.get_sop_for_item("PIST-CAKE", batches="3")

        self.assertEqual(3.0, result["batches"])

    def test_instruction_text_is_stripped_server_side(self):
        # The app renders the plain text; it must never need an HTML package.
        from jarz_pos.api import sop

        with ExitStack() as stack:
            self._stack(stack)
            result = sop.get_sop_for_item("PIST-CAKE")

        step = result["steps"][0]
        self.assertEqual("Weigh 1.830 Kg Pistachio spread into the bowl.", step["instruction_text"])
        self.assertTrue(step["instruction_html"].startswith("<p>"))

    def test_an_explicit_bom_overrides_the_sop_default(self):
        from jarz_pos.api import sop

        with ExitStack() as stack:
            mocks = self._stack(stack)
            result = sop.get_sop_for_item("PIST-CAKE", bom="BOM-OVERRIDE")

        self.assertEqual("BOM-OVERRIDE", result["bom"])
        mocks["bom"].assert_called_once_with("BOM-OVERRIDE")

    def test_requires_production_access(self):
        from jarz_pos.api import sop

        with patch(
            "jarz_pos.api.sop._ensure_production_view_access",
            side_effect=PermissionError("nope"),
        ), patch("jarz_pos.api.sop._resolve_active_sop") as mock_active, patch(
            "jarz_pos.api.sop.frappe"
        ):
            with self.assertRaises(PermissionError):
                sop.get_sop_for_item("PIST-CAKE")

        mock_active.assert_not_called()


class TestGetSopForWorkOrder(SopApiTestCase):
    WO = {
        "name": "MFG-WO-0001",
        "production_item": "PIST-CAKE",
        "qty": 30,
        "bom_no": "BOM-PIST-CAKE-001",
        "company": "Jarz Co",
        "jarz_sop_version": "SOP-OLD#2",
    }

    def _run(self, stack, wo=None, **kwargs):
        from jarz_pos.api import sop

        mocks = self._stack(stack, **kwargs)
        mocks["wo"] = stack.enter_context(
            patch("jarz_pos.api.sop._resolve_work_order_row", return_value=dict(wo or self.WO))
        )
        return sop, mocks

    def test_prefers_the_stamped_version_over_the_active_one(self):
        # The acceptance test for the whole versioning story: editing an SOP
        # must not rewrite the history of batches already run.
        with ExitStack() as stack:
            sop, mocks = self._run(
                stack,
                active_sop="SOP-NEW",
                doc=sop_doc(name="SOP-OLD", version=2),
            )
            result = sop.get_sop_for_work_order("MFG-WO-0001")

        mocks["doc"].assert_called_once_with("SOP-OLD")
        mocks["active"].assert_not_called()
        self.assertEqual("SOP-OLD", result["sop"])
        self.assertEqual(2, result["version"])
        self.assertTrue(result["is_stamped"])
        self.assertEqual("SOP-OLD#2", result["sop_version_stamp"])

    def test_falls_back_to_the_active_sop_when_unstamped(self):
        with ExitStack() as stack:
            wo = dict(self.WO, jarz_sop_version=None)
            sop, mocks = self._run(
                stack, wo=wo, active_sop="SOP-NEW", doc=sop_doc(name="SOP-NEW", version=5)
            )
            result = sop.get_sop_for_work_order("MFG-WO-0001")

        mocks["active"].assert_called_once_with("PIST-CAKE")
        mocks["doc"].assert_called_once_with("SOP-NEW")
        self.assertEqual("SOP-NEW", result["sop"])
        self.assertFalse(result["is_stamped"])

    def test_a_dangling_stamp_falls_back_to_the_active_sop_and_logs(self):
        with ExitStack() as stack:
            sop, mocks = self._run(stack, active_sop="SOP-NEW")
            mocks["doc"].side_effect = [Exception("deleted"), sop_doc(name="SOP-NEW", version=5)]

            result = sop.get_sop_for_work_order("MFG-WO-0001")

        self.assertEqual("SOP-NEW", result["sop"])
        self.assertFalse(result["is_stamped"])
        mocks["frappe"].log_error.assert_called()

    def test_batches_come_from_wo_qty_over_bom_qty(self):
        with ExitStack() as stack:
            sop, _ = self._run(stack, doc=sop_doc(name="SOP-OLD", version=2))
            result = sop.get_sop_for_work_order("MFG-WO-0001")

        # 30 units off a BOM that yields 10 = 3 batches
        self.assertEqual(3.0, result["batches"])
        self.assertEqual(30.0, result["units"])
        self.assertIn("5.490 Kg Pistachio spread", result["steps"][0]["instruction_html"])
        self.assertEqual("MFG-WO-0001", result["work_order"])

    def test_an_item_with_no_sop_reports_has_sop_false(self):
        with ExitStack() as stack:
            wo = dict(self.WO, jarz_sop_version=None)
            sop, _ = self._run(stack, wo=wo, active_sop=None)
            result = sop.get_sop_for_work_order("MFG-WO-0001")

        self.assertFalse(result["has_sop"])
        self.assertEqual("MFG-WO-0001", result["work_order"])

    def test_unknown_work_order_is_rejected(self):
        from jarz_pos.api import sop

        with ExitStack() as stack:
            mocks = self._stack(stack, translate=True)
            stack.enter_context(
                patch("jarz_pos.api.sop._resolve_work_order_row", return_value=None)
            )
            mocks["frappe"].throw.side_effect = ValueError("not found")

            with self.assertRaises(ValueError):
                sop.get_sop_for_work_order("NOPE")

    def test_requires_production_access(self):
        from jarz_pos.api import sop

        with patch(
            "jarz_pos.api.sop._ensure_production_view_access",
            side_effect=PermissionError("nope"),
        ), patch("jarz_pos.api.sop._resolve_work_order_row") as mock_wo, patch(
            "jarz_pos.api.sop.frappe"
        ):
            with self.assertRaises(PermissionError):
                sop.get_sop_for_work_order("MFG-WO-0001")

        mock_wo.assert_not_called()


class TestRecordSopStepCapture(unittest.TestCase):
    WO = {
        "name": "MFG-WO-0001",
        "production_item": "PIST-CAKE",
        "qty": 30,
        "bom_no": "BOM-PIST-CAKE-001",
        "jarz_sop_version": "SOP-0001#1",
    }

    def _stack(self, stack, doc=None):
        stack.enter_context(patch("jarz_pos.api.sop._ensure_production_view_access"))
        stack.enter_context(
            patch("jarz_pos.api.sop._resolve_work_order_row", return_value=dict(self.WO))
        )
        stack.enter_context(
            patch(
                "jarz_pos.api.sop._resolve_sop_for_work_order",
                return_value=(doc or sop_doc(), "SOP-0001#1", True),
            )
        )
        stack.enter_context(patch("jarz_pos.api.sop._resolve_session_user", return_value="ops@jarz"))
        stack.enter_context(
            patch("jarz_pos.api.sop._resolve_now_datetime", return_value="2026-08-01 09:00:00")
        )
        stack.enter_context(passthrough_translate())
        mock_frappe = stack.enter_context(patch("jarz_pos.api.sop.frappe"))
        mock_frappe.throw.side_effect = ValueError("rejected")
        return mock_frappe

    def test_a_reading_inside_the_range_is_logged(self):
        from jarz_pos.api import sop

        with ExitStack() as stack:
            mock_frappe = self._stack(stack)
            result = sop.record_sop_step_capture("MFG-WO-0001", 1, value="1.9")

        payload = mock_frappe.get_doc.call_args.args[0]
        self.assertEqual("Jarz SOP Execution Log", payload["doctype"])
        self.assertEqual("MFG-WO-0001", payload["work_order"])
        self.assertEqual("SOP-0001#1", payload["sop_version"])
        self.assertEqual(1, payload["step_no"])
        self.assertEqual(1.9, payload["value_float"])
        self.assertEqual("ops@jarz", payload["captured_by"])
        mock_frappe.get_doc.return_value.insert.assert_called_once()
        self.assertTrue(result["ok"])
        self.assertEqual(1.9, result["value_float"])

    def test_a_reading_outside_the_range_is_rejected(self):
        from jarz_pos.api import sop

        with ExitStack() as stack:
            mock_frappe = self._stack(stack)
            with self.assertRaises(ValueError):
                sop.record_sop_step_capture("MFG-WO-0001", 1, value=9.5)

        mock_frappe.get_doc.assert_not_called()

    def test_a_non_numeric_reading_is_rejected(self):
        from jarz_pos.api import sop

        with ExitStack() as stack:
            mock_frappe = self._stack(stack)
            with self.assertRaises(ValueError):
                sop.record_sop_step_capture("MFG-WO-0001", 1, value="about two")

        mock_frappe.get_doc.assert_not_called()

    def test_a_missing_reading_is_rejected(self):
        from jarz_pos.api import sop

        with ExitStack() as stack:
            mock_frappe = self._stack(stack)
            with self.assertRaises(ValueError):
                sop.record_sop_step_capture("MFG-WO-0001", 1)

        mock_frappe.get_doc.assert_not_called()

    def test_no_bounds_configured_means_any_reading_is_accepted(self):
        from jarz_pos.api import sop

        doc = sop_doc()
        doc.steps[0]["capture_min"] = 0
        doc.steps[0]["capture_max"] = 0

        with ExitStack() as stack:
            mock_frappe = self._stack(stack, doc=doc)
            sop.record_sop_step_capture("MFG-WO-0001", 1, value=9999)

        self.assertEqual(9999.0, mock_frappe.get_doc.call_args.args[0]["value_float"])

    def test_a_photo_step_requires_a_file(self):
        from jarz_pos.api import sop

        doc = sop_doc()
        doc.steps[0]["capture_type"] = "Photo"

        with ExitStack() as stack:
            mock_frappe = self._stack(stack, doc=doc)
            with self.assertRaises(ValueError):
                sop.record_sop_step_capture("MFG-WO-0001", 1)

        mock_frappe.get_doc.assert_not_called()

    def test_a_photo_step_stores_the_attachment(self):
        from jarz_pos.api import sop

        doc = sop_doc()
        doc.steps[0]["capture_type"] = "Photo"

        with ExitStack() as stack:
            mock_frappe = self._stack(stack, doc=doc)
            sop.record_sop_step_capture("MFG-WO-0001", 1, file_url="/files/proof.jpg")

        payload = mock_frappe.get_doc.call_args.args[0]
        self.assertEqual("/files/proof.jpg", payload["attachment"])
        self.assertIsNone(payload["value_float"])

    def test_a_plain_confirmation_step_records_free_text(self):
        from jarz_pos.api import sop

        with ExitStack() as stack:
            mock_frappe = self._stack(stack)
            sop.record_sop_step_capture("MFG-WO-0001", 2, value="looked fine")

        payload = mock_frappe.get_doc.call_args.args[0]
        self.assertEqual("None", payload["capture_type"])
        self.assertEqual("looked fine", payload["value_text"])
        self.assertIsNone(payload["value_float"])

    def test_an_unknown_step_is_rejected(self):
        from jarz_pos.api import sop

        with ExitStack() as stack:
            mock_frappe = self._stack(stack)
            with self.assertRaises(ValueError):
                sop.record_sop_step_capture("MFG-WO-0001", 99, value=1)

        mock_frappe.get_doc.assert_not_called()

    def test_a_work_order_with_no_sop_is_rejected(self):
        from jarz_pos.api import sop

        with ExitStack() as stack:
            stack.enter_context(patch("jarz_pos.api.sop._ensure_production_view_access"))
            stack.enter_context(
                patch("jarz_pos.api.sop._resolve_work_order_row", return_value=dict(self.WO))
            )
            stack.enter_context(
                patch(
                    "jarz_pos.api.sop._resolve_sop_for_work_order",
                    return_value=(None, None, False),
                )
            )
            stack.enter_context(passthrough_translate())
            mock_frappe = stack.enter_context(patch("jarz_pos.api.sop.frappe"))
            mock_frappe.throw.side_effect = ValueError("no sop")

            with self.assertRaises(ValueError):
                sop.record_sop_step_capture("MFG-WO-0001", 1, value=1.9)

        mock_frappe.get_doc.assert_not_called()

    def test_requires_production_access(self):
        from jarz_pos.api import sop

        with patch(
            "jarz_pos.api.sop._ensure_production_view_access",
            side_effect=PermissionError("nope"),
        ), patch("jarz_pos.api.sop._resolve_work_order_row") as mock_wo, patch(
            "jarz_pos.api.sop.frappe"
        ) as mock_frappe:
            with self.assertRaises(PermissionError):
                sop.record_sop_step_capture("MFG-WO-0001", 1, value=1.9)

        mock_wo.assert_not_called()
        mock_frappe.get_doc.assert_not_called()


class TestListSops(unittest.TestCase):
    ROWS = [{"name": "SOP-0001", "item_code": "PIST-CAKE", "version": 2, "is_active": 1}]

    def test_active_only_is_the_default(self):
        from jarz_pos.api import sop

        with patch("jarz_pos.api.sop._ensure_production_view_access"), patch(
            "jarz_pos.api.sop.frappe"
        ) as mock_frappe:
            mock_frappe.get_all.return_value = self.ROWS
            result = sop.list_sops()

        filters = mock_frappe.get_all.call_args.kwargs["filters"]
        self.assertEqual({"is_active": 1}, filters)
        self.assertEqual(1, result["count"])

    def test_active_only_zero_returns_every_version(self):
        from jarz_pos.api import sop

        with patch("jarz_pos.api.sop._ensure_production_view_access"), patch(
            "jarz_pos.api.sop.frappe"
        ) as mock_frappe:
            mock_frappe.get_all.return_value = self.ROWS
            sop.list_sops(active_only=0)

        self.assertEqual({}, mock_frappe.get_all.call_args.kwargs["filters"])

    def test_item_code_narrows_the_list(self):
        from jarz_pos.api import sop

        with patch("jarz_pos.api.sop._ensure_production_view_access"), patch(
            "jarz_pos.api.sop.frappe"
        ) as mock_frappe:
            mock_frappe.get_all.return_value = self.ROWS
            sop.list_sops(item_code="PIST-CAKE", active_only="0")

        self.assertEqual(
            {"item_code": "PIST-CAKE"}, mock_frappe.get_all.call_args.kwargs["filters"]
        )

    def test_requires_production_access(self):
        from jarz_pos.api import sop

        with patch(
            "jarz_pos.api.sop._ensure_production_view_access",
            side_effect=PermissionError("nope"),
        ), patch("jarz_pos.api.sop.frappe") as mock_frappe:
            with self.assertRaises(PermissionError):
                sop.list_sops()

        mock_frappe.get_all.assert_not_called()


class TestAccessGate(unittest.TestCase):
    def test_jarz_manager_is_admitted(self):
        from jarz_pos.api import sop

        with patch("jarz_pos.api.sop.frappe") as mock_frappe:
            mock_frappe.get_roles.return_value = ["JARZ Manager"]
            sop._ensure_production_view_access()

        mock_frappe.throw.assert_not_called()

    def test_an_unrelated_role_is_rejected(self):
        from jarz_pos.api import sop

        with passthrough_translate(), patch("jarz_pos.api.sop.frappe") as mock_frappe:
            mock_frappe.get_roles.return_value = ["Sales User"]
            mock_frappe.throw.side_effect = PermissionError("denied")

            with self.assertRaises(PermissionError):
                sop._ensure_production_view_access()


if __name__ == "__main__":
    unittest.main()
