"""A site-wide DEFAULT purchase VAT, still overridable per item and per line.

Most of what Jarz buys is taxed at the standard 14%, so requiring every Item
master to spell that out means the cost of forgetting one is a silent
under-charge on the purchase — invisible until someone reconciles the tax
ledger. The default closes that gap; these tests pin the two things that make it
safe to have one at all:

* **precedence** — an explicit ``item_tax_template`` on the cart line always
  wins, *including an explicit empty string*. That empty string is the buyer
  saying "this one is not taxed", and a default that overrode it would quietly
  tax an exempt purchase. Only a wholly absent key falls back;
* **the migrate-window guard** — code reaches the server before ``bench
  migrate`` runs, so for that window the settings field does not exist as a
  column. Reading it must yield "no default", not a 500 on every purchase
  screen.

Also covered: a default that has since been deleted, disabled or moved to
another company degrades to "no default" rather than failing the purchase.

Pure ``unittest`` with mocks — no site.
"""

import unittest
from unittest.mock import MagicMock, patch

from jarz_pos.api import purchase as pu

COMPANY = "JARZ"
OTHER_COMPANY = "Some Other Co"
SETTINGS = "Jarz POS Settings"
FIELD = "purchase_default_item_tax_template"

SITE_DEFAULT = "Egypt VAT 14% - J"
ITEM_TEMPLATE = "Egypt Tax - J"


class _DefaultCase(unittest.TestCase):
    """Settings field present; the DB answers are set per test."""

    def _resolve(self, configured, template_row, has_field=True, company=None):
        """Run ``_default_item_tax_template`` against a canned site."""
        with patch.object(pu, "_has_field", return_value=has_field), patch.object(
            pu.frappe.db, "get_single_value", return_value=configured
        ) as single, patch.object(
            pu.frappe.db, "get_value", return_value=template_row
        ):
            result = pu._default_item_tax_template(company)
        return result, single


class TestConfiguredDefaultResolves(_DefaultCase):
    def test_configured_and_live_template_is_returned(self):
        row = {"company": COMPANY, "disabled": 0}
        result, _ = self._resolve(SITE_DEFAULT, row)
        self.assertEqual(result, SITE_DEFAULT)

    def test_matching_company_is_returned(self):
        row = {"company": COMPANY, "disabled": 0}
        result, _ = self._resolve(SITE_DEFAULT, row, company=COMPANY)
        self.assertEqual(result, SITE_DEFAULT)

    def test_surrounding_whitespace_is_ignored(self):
        row = {"company": COMPANY, "disabled": 0}
        result, _ = self._resolve(f"  {SITE_DEFAULT}  ", row)
        self.assertEqual(result, SITE_DEFAULT)


class TestDefaultDegradesInsteadOfFailing(_DefaultCase):
    """Every one of these must read as "no default", never as an exception.

    A purchase failing because a *default* went stale would be a worse outcome
    than the purchase simply carrying no VAT template, which the buyer can see
    and fix on the line.
    """

    def test_blank_setting_means_no_default(self):
        result, _ = self._resolve("", None)
        self.assertIsNone(result)

    def test_unset_setting_means_no_default(self):
        result, _ = self._resolve(None, None)
        self.assertIsNone(result)

    def test_deleted_template_means_no_default(self):
        # get_value returns nothing: the record the Link names is gone.
        result, _ = self._resolve(SITE_DEFAULT, None)
        self.assertIsNone(result)

    def test_disabled_template_means_no_default(self):
        row = {"company": COMPANY, "disabled": 1}
        result, _ = self._resolve(SITE_DEFAULT, row)
        self.assertIsNone(result)

    def test_cross_company_template_is_not_applied(self):
        """ERPNext would reject it on the invoice with a far less obvious error."""
        row = {"company": OTHER_COMPANY, "disabled": 0}
        result, _ = self._resolve(SITE_DEFAULT, row, company=COMPANY)
        self.assertIsNone(result)

    def test_a_failing_settings_read_means_no_default(self):
        with patch.object(pu, "_has_field", return_value=True), patch.object(
            pu.frappe.db, "get_single_value", side_effect=RuntimeError("boom")
        ):
            self.assertIsNone(pu._default_item_tax_template())


class TestMigrateWindowGuard(_DefaultCase):
    """The field does not exist as a column until ``bench migrate`` has run."""

    def test_absent_settings_field_means_no_default(self):
        result, _ = self._resolve(SITE_DEFAULT, {"company": COMPANY, "disabled": 0},
                                  has_field=False)
        self.assertIsNone(result)

    def test_absent_settings_field_is_not_even_queried(self):
        """Naming a not-yet-migrated column in a query 500s the endpoint."""
        _, single = self._resolve(SITE_DEFAULT, None, has_field=False)
        single.assert_not_called()

    def test_has_field_is_false_when_meta_blows_up(self):
        with patch.object(pu.frappe, "get_meta", side_effect=RuntimeError("no such doctype")):
            self.assertFalse(pu._has_field(SETTINGS, FIELD))


def _item_tax_rows(*rows):
    """Rows as ``frappe.get_all("Item Tax", ...)`` would return them."""
    return list(rows)


class TestBulkAppliesTheDefault(unittest.TestCase):
    """``_item_tax_templates_bulk`` collapses item master + site default.

    Layering it here rather than at each call site is what keeps the item
    search, the item detail, the requests list and the invoice builder agreeing
    on what a line is pre-filled with.
    """

    def _bulk(self, rows, default, **kwargs):
        with patch.object(pu.frappe, "get_all", return_value=rows), patch.object(
            pu, "_default_item_tax_template", return_value=default
        ):
            return pu._item_tax_templates_bulk(["RM-SUGAR", "PKG-JAR"], **kwargs)

    def test_item_master_template_beats_the_default(self):
        rows = _item_tax_rows(
            {"parent": "PKG-JAR", "item_tax_template": ITEM_TEMPLATE, "tax_category": None}
        )
        out = self._bulk(rows, SITE_DEFAULT)
        self.assertEqual(out["PKG-JAR"], ITEM_TEMPLATE)

    def test_item_declaring_nothing_gets_the_site_default(self):
        out = self._bulk(_item_tax_rows(), SITE_DEFAULT)
        self.assertEqual(out["RM-SUGAR"], SITE_DEFAULT)
        self.assertEqual(out["PKG-JAR"], SITE_DEFAULT)

    def test_no_configured_default_leaves_the_item_untaxed(self):
        out = self._bulk(_item_tax_rows(), None)
        self.assertEqual(out, {})

    def test_default_is_not_applied_when_opted_out(self):
        out = self._bulk(_item_tax_rows(), SITE_DEFAULT, include_default=False)
        self.assertEqual(out, {})

    def test_an_item_tax_row_naming_no_template_still_gets_the_default(self):
        """A blank row is not a decision to go untaxed.

        The buyer expresses "not taxed" per line, with an explicit empty
        template — not by leaving an empty row on the Item master.
        """
        rows = _item_tax_rows(
            {"parent": "PKG-JAR", "item_tax_template": None, "tax_category": None}
        )
        out = self._bulk(rows, SITE_DEFAULT)
        self.assertEqual(out["PKG-JAR"], SITE_DEFAULT)

    def test_categorised_row_is_ignored_and_the_default_applies(self):
        """A categorised row is conditional on a tax category a POS purchase
        never sets, so honouring it would apply a tax ERPNext would not."""
        rows = _item_tax_rows(
            {
                "parent": "PKG-JAR",
                "item_tax_template": ITEM_TEMPLATE,
                "tax_category": "Registered",
            }
        )
        out = self._bulk(rows, SITE_DEFAULT)
        self.assertEqual(out["PKG-JAR"], SITE_DEFAULT)

    def test_empty_item_list_short_circuits(self):
        with patch.object(pu.frappe, "get_all") as get_all, patch.object(
            pu, "_default_item_tax_template", return_value=SITE_DEFAULT
        ):
            self.assertEqual(pu._item_tax_templates_bulk([]), {})
        get_all.assert_not_called()

    def test_company_is_forwarded_to_the_default_resolution(self):
        """The invoice builder knows its company; the listing endpoints do not."""
        with patch.object(pu.frappe, "get_all", return_value=[]), patch.object(
            pu, "_default_item_tax_template", return_value=None
        ) as resolver:
            pu._item_tax_templates_bulk(["RM-SUGAR"], company=COMPANY)
        resolver.assert_called_once_with(COMPANY)

    def test_missing_settings_field_falls_back_to_item_masters_only(self):
        """End to end through the real resolver during the migrate window."""
        rows = _item_tax_rows(
            {"parent": "PKG-JAR", "item_tax_template": ITEM_TEMPLATE, "tax_category": None}
        )
        with patch.object(pu.frappe, "get_all", return_value=rows), patch.object(
            pu, "_has_field", return_value=False
        ):
            out = pu._item_tax_templates_bulk(["RM-SUGAR", "PKG-JAR"], company=COMPANY)
        self.assertEqual(out, {"PKG-JAR": ITEM_TEMPLATE})


class TestLinePrecedence(unittest.TestCase):
    """Explicit line value (even empty) > item master > site default > nothing.

    ``inherited`` arrives from ``_item_tax_templates_bulk``, which has already
    collapsed the item master and the site default into one answer per item.
    """

    INHERITED = {"PKG-JAR": ITEM_TEMPLATE, "RM-SUGAR": SITE_DEFAULT}

    def _resolve(self, row):
        return pu._resolve_line_tax_template(row, self.INHERITED)

    def test_explicit_template_wins(self):
        row = {"item_code": "PKG-JAR", "item_tax_template": "Zero Rated - J"}
        self.assertEqual(self._resolve(row), "Zero Rated - J")

    def test_explicit_empty_string_beats_the_site_default(self):
        """The whole reason a default is safe: the buyer can still say "no VAT".

        Falling back here would tax an exempt purchase, and nothing on the
        screen would show that it had happened.
        """
        row = {"item_code": "RM-SUGAR", "item_tax_template": ""}
        self.assertEqual(self._resolve(row), "")

    def test_explicit_empty_string_beats_the_item_master(self):
        row = {"item_code": "PKG-JAR", "item_tax_template": ""}
        self.assertEqual(self._resolve(row), "")

    def test_explicit_none_is_treated_as_untaxed(self):
        """A JSON ``null`` is the client sending the key, not omitting it."""
        row = {"item_code": "RM-SUGAR", "item_tax_template": None}
        self.assertEqual(self._resolve(row), "")

    def test_explicit_whitespace_is_treated_as_untaxed(self):
        row = {"item_code": "RM-SUGAR", "item_tax_template": "   "}
        self.assertEqual(self._resolve(row), "")

    def test_absent_key_inherits_the_item_master_template(self):
        self.assertEqual(self._resolve({"item_code": "PKG-JAR"}), ITEM_TEMPLATE)

    def test_absent_key_inherits_the_site_default(self):
        self.assertEqual(self._resolve({"item_code": "RM-SUGAR"}), SITE_DEFAULT)

    def test_absent_key_with_nothing_to_inherit_is_untaxed(self):
        self.assertEqual(self._resolve({"item_code": "RM-SALT"}), "")

    def test_legacy_item_key_is_honoured(self):
        """``create_purchase_invoice`` accepts ``item`` as well as ``item_code``."""
        self.assertEqual(self._resolve({"item": "PKG-JAR"}), ITEM_TEMPLATE)


class TestTemplateListingMarksTheDefault(unittest.TestCase):
    """Additive ``is_default`` so the picker can show which rate a line gets."""

    def test_only_the_configured_template_is_flagged(self):
        templates = [
            {"name": SITE_DEFAULT, "title": "Egypt VAT 14%", "company": COMPANY},
            {"name": ITEM_TEMPLATE, "title": "Egypt Tax", "company": COMPANY},
        ]

        def _get_all(doctype, **kwargs):
            if doctype == "Item Tax Template":
                return templates
            return [
                {"parent": SITE_DEFAULT, "tax_type": "GST - J", "tax_rate": 14},
                {"parent": ITEM_TEMPLATE, "tax_type": "GST - J", "tax_rate": 10},
            ]

        with patch.object(pu, "_ensure_manager_access"), patch.object(
            pu.frappe, "get_all", side_effect=_get_all
        ), patch.object(pu, "_default_item_tax_template", return_value=SITE_DEFAULT):
            rows = pu.get_item_tax_templates(COMPANY)

        flags = {r["name"]: r["is_default"] for r in rows}
        self.assertEqual(flags, {SITE_DEFAULT: 1, ITEM_TEMPLATE: 0})
        self.assertEqual({r["name"]: r["rate"] for r in rows}[SITE_DEFAULT], 14)


class TestSeederIsCreateOnly(unittest.TestCase):
    """The template seeder must never invent an Account or clobber an edit."""

    def setUp(self):
        from jarz_pos.setup import purchase_setup

        self.ps = purchase_setup

    def test_no_tax_account_is_a_noop_not_a_throw(self):
        """The chart of accounts is not this app's decision to make.

        Inventing a tax account would post VAT into a ledger nobody reconciles;
        an exception would abort the shared ``bench migrate``.
        """
        with patch.object(self.ps, "_resolve_company", return_value=COMPANY), patch.object(
            self.ps.frappe.db, "get_value", return_value=None
        ), patch.object(self.ps, "_resolve_tax_account", return_value=None), patch.object(
            self.ps.frappe, "new_doc"
        ) as new_doc, patch.object(self.ps, "_logger", return_value=MagicMock()):
            log = self.ps.ensure_purchase_vat_template()

        new_doc.assert_not_called()
        self.assertEqual(log["set"], [])
        self.assertTrue(log["skipped"])

    def test_existing_template_is_left_alone(self):
        with patch.object(self.ps, "_resolve_company", return_value=COMPANY), patch.object(
            self.ps.frappe.db, "get_value", return_value=SITE_DEFAULT
        ), patch.object(self.ps.frappe, "new_doc") as new_doc, patch.object(
            self.ps, "_logger", return_value=MagicMock()
        ):
            log = self.ps.ensure_purchase_vat_template()

        new_doc.assert_not_called()
        self.assertEqual(log["existing"], [f"item tax template {SITE_DEFAULT}"])

    def test_template_is_created_at_the_standard_rate(self):
        doc = MagicMock()
        doc.name = SITE_DEFAULT
        with patch.object(self.ps, "_resolve_company", return_value=COMPANY), patch.object(
            self.ps.frappe.db, "get_value", return_value=None
        ), patch.object(
            self.ps, "_resolve_tax_account", return_value="GST - J"
        ), patch.object(self.ps.frappe, "new_doc", return_value=doc), patch.object(
            self.ps, "_logger", return_value=MagicMock()
        ):
            log = self.ps.ensure_purchase_vat_template()

        doc.append.assert_called_once_with(
            "taxes", {"tax_type": "GST - J", "tax_rate": 14.0}
        )
        doc.insert.assert_called_once()
        self.assertEqual(self.ps.DEFAULT_VAT_RATE, 14.0)
        self.assertTrue(log["set"])

    def test_ambiguous_company_is_a_noop(self):
        with patch.object(self.ps, "_resolve_company", return_value=None), patch.object(
            self.ps.frappe, "new_doc"
        ) as new_doc, patch.object(self.ps, "_logger", return_value=MagicMock()):
            log = self.ps.ensure_purchase_vat_template()

        new_doc.assert_not_called()
        self.assertEqual(log["skipped"], ["no default company resolved"])


class TestSettingsSeedNeverPointsAtAMissingRecord(unittest.TestCase):
    """A Link on this Single naming a missing record poisons every later save.

    That has already broken unrelated seeders on production once, so the
    dynamic default must stay silent unless the template really exists.
    """

    def setUp(self):
        from jarz_pos.setup import settings_defaults

        self.sd = settings_defaults

    def test_missing_template_seeds_nothing(self):
        with patch(
            "jarz_pos.setup.purchase_setup.default_item_tax_template_name",
            return_value=None,
        ):
            self.assertEqual(self.sd._dynamic_defaults(), {})

    def test_existing_template_is_seeded(self):
        with patch(
            "jarz_pos.setup.purchase_setup.default_item_tax_template_name",
            return_value=SITE_DEFAULT,
        ):
            self.assertEqual(
                self.sd._dynamic_defaults().get("purchase_default_item_tax_template"),
                SITE_DEFAULT,
            )

    def test_a_failing_lookup_seeds_nothing(self):
        # "Nothing" means nothing FOR THIS KEY. _dynamic_defaults also resolves
        # the label accounting names (a separate, independently-guarded lookup),
        # so asserting the whole dict empty would couple this test to every
        # future dynamic default and break on each one added.
        with patch(
            "jarz_pos.setup.purchase_setup.default_item_tax_template_name",
            side_effect=RuntimeError("boom"),
        ):
            self.assertNotIn(
                "purchase_default_item_tax_template", self.sd._dynamic_defaults()
            )


if __name__ == "__main__":
    unittest.main()
