"""Unit tests for the Jarz SOP DocType controller.

The controller is exercised without ever constructing a real ``Document``:
instances are built with ``__new__`` and the two frappe touch points
(``frappe.db`` and ``frappe.get_all``) are patched.  ``renumber_steps`` is a
module-level pure function and is tested directly.
"""

import unittest
from unittest.mock import patch


def _ensure_frappe_model_stub():
    """Make ``from frappe.model.document import Document`` importable.

    A no-op under the real frappe.  Under the stubbed frappe used by the
    standalone runner it installs a minimal base class, so the controller can
    be imported without a bench.
    """
    try:
        import frappe.model.document  # noqa: F401

        return
    except Exception:
        pass

    import sys
    import types

    import frappe

    class _Document:
        pass

    model = types.ModuleType("frappe.model")
    model.__path__ = []
    document = types.ModuleType("frappe.model.document")
    document.Document = _Document
    model.document = document
    frappe.model = model
    sys.modules["frappe.model"] = model
    sys.modules["frappe.model.document"] = document


def load():
    _ensure_frappe_model_stub()
    from jarz_pos.doctype.jarz_sop import jarz_sop

    return jarz_sop


def passthrough_translate():
    return patch("jarz_pos.doctype.jarz_sop.jarz_sop._", new=lambda msg: msg)


class Row:
    """A stand-in for a child row that is not a dict."""

    def __init__(self, idx=None, title=None):
        self.idx = idx
        self.title = title
        self.step_no = None


def make_sop(module, **fields):
    sop = module.JarzSOP.__new__(module.JarzSOP)
    sop.name = "SOP-0001"
    sop.item_code = "PIST-CAKE"
    sop.is_active = 1
    sop.steps = []
    sop.has_value_changed = lambda field: False
    for key, value in fields.items():
        setattr(sop, key, value)
    return sop


class TestRenumberSteps(unittest.TestCase):
    def test_steps_are_renumbered_one_to_n_by_idx(self):
        module = load()
        rows = [
            {"idx": 3, "title": "Bake"},
            {"idx": 1, "title": "Weigh"},
            {"idx": 2, "title": "Mix"},
        ]

        ordered = module.renumber_steps(rows)

        self.assertEqual(["Weigh", "Mix", "Bake"], [r["title"] for r in ordered])
        self.assertEqual([1, 2, 3], [r["step_no"] for r in ordered])

    def test_an_authored_step_no_is_overwritten(self):
        # Letting the author type it guarantees a duplicate "step 4" eventually.
        module = load()
        rows = [{"idx": 1, "step_no": 7}, {"idx": 2, "step_no": 7}]

        module.renumber_steps(rows)

        self.assertEqual([1, 2], [r["step_no"] for r in rows])

    def test_rows_without_an_idx_keep_their_arrival_order(self):
        module = load()
        rows = [{"title": "A"}, {"title": "B"}, {"title": "C"}]

        ordered = module.renumber_steps(rows)

        self.assertEqual(["A", "B", "C"], [r["title"] for r in ordered])
        self.assertEqual([1, 2, 3], [r["step_no"] for r in ordered])

    def test_object_rows_are_supported_as_well_as_dicts(self):
        module = load()
        rows = [Row(idx=2, title="Mix"), Row(idx=1, title="Weigh")]

        ordered = module.renumber_steps(rows)

        self.assertEqual(["Weigh", "Mix"], [r.title for r in ordered])
        self.assertEqual([1, 2], [r.step_no for r in ordered])

    def test_no_steps_is_not_an_error(self):
        module = load()
        self.assertEqual([], module.renumber_steps(None))
        self.assertEqual([], module.renumber_steps([]))


class TestUsedSopGuard(unittest.TestCase):
    def test_editing_a_used_sop_throws(self):
        # Without this, version-stamping is decoration: the natural thing to do
        # is open the SOP and fix the step, silently rewriting what every past
        # batch was made to.
        module = load()
        sop = make_sop(module, has_value_changed=lambda field: True)

        with passthrough_translate(), patch(
            "jarz_pos.doctype.jarz_sop.jarz_sop.frappe"
        ) as mock_frappe:
            mock_frappe.db.exists.return_value = True
            mock_frappe.throw.side_effect = ValueError("used in production")

            with self.assertRaises(ValueError):
                sop._guard_steps_are_immutable_once_used()

        mock_frappe.db.exists.assert_called_once_with(
            "Work Order", {"jarz_sop_version": ["like", "SOP-0001#%"]}
        )

    def test_editing_an_unused_sop_is_allowed(self):
        module = load()
        sop = make_sop(module, has_value_changed=lambda field: True)

        with passthrough_translate(), patch(
            "jarz_pos.doctype.jarz_sop.jarz_sop.frappe"
        ) as mock_frappe:
            mock_frappe.db.exists.return_value = False

            sop._guard_steps_are_immutable_once_used()

        mock_frappe.throw.assert_not_called()

    def test_a_metadata_only_edit_on_a_used_sop_is_allowed(self):
        # Fixing a typo in the notes of a used procedure must still be possible.
        module = load()
        sop = make_sop(module, has_value_changed=lambda field: False)

        with passthrough_translate(), patch(
            "jarz_pos.doctype.jarz_sop.jarz_sop.frappe"
        ) as mock_frappe:
            mock_frappe.db.exists.return_value = True

            sop._guard_steps_are_immutable_once_used()

        mock_frappe.throw.assert_not_called()

    def test_validate_renumbers_before_it_guards(self):
        module = load()
        rows = [{"idx": 2, "title": "Mix"}, {"idx": 1, "title": "Weigh"}]
        sop = make_sop(module, steps=rows)

        with passthrough_translate(), patch(
            "jarz_pos.doctype.jarz_sop.jarz_sop.frappe"
        ) as mock_frappe:
            mock_frappe.db.exists.return_value = False

            sop.validate()

        self.assertEqual([2, 1], [r["step_no"] for r in rows])
        mock_frappe.throw.assert_not_called()


class TestSingleActiveVersion(unittest.TestCase):
    def test_activating_a_version_deactivates_the_previous_ones(self):
        module = load()
        sop = make_sop(module, is_active=1)

        with passthrough_translate(), patch(
            "jarz_pos.doctype.jarz_sop.jarz_sop.frappe"
        ) as mock_frappe:
            mock_frappe.get_all.return_value = ["SOP-0000", "SOP-0002"]

            sop.on_update()

        mock_frappe.get_all.assert_called_once_with(
            "Jarz SOP",
            filters={"item_code": "PIST-CAKE", "is_active": 1, "name": ["!=", "SOP-0001"]},
            pluck="name",
        )
        self.assertEqual(2, mock_frappe.db.set_value.call_count)
        mock_frappe.db.set_value.assert_any_call(
            "Jarz SOP", "SOP-0000", "is_active", 0, update_modified=False
        )
        # Silently flipping another record's flag is the kind of side effect
        # that gets discovered three weeks later.
        mock_frappe.msgprint.assert_called_once()

    def test_no_other_active_version_means_no_write_and_no_message(self):
        module = load()
        sop = make_sop(module, is_active=1)

        with passthrough_translate(), patch(
            "jarz_pos.doctype.jarz_sop.jarz_sop.frappe"
        ) as mock_frappe:
            mock_frappe.get_all.return_value = []

            sop.on_update()

        mock_frappe.db.set_value.assert_not_called()
        mock_frappe.msgprint.assert_not_called()

    def test_saving_an_inactive_version_touches_nothing(self):
        module = load()
        sop = make_sop(module, is_active=0)

        with passthrough_translate(), patch(
            "jarz_pos.doctype.jarz_sop.jarz_sop.frappe"
        ) as mock_frappe:
            sop.on_update()

        mock_frappe.get_all.assert_not_called()
        mock_frappe.db.set_value.assert_not_called()
        mock_frappe.msgprint.assert_not_called()


if __name__ == "__main__":
    unittest.main()
