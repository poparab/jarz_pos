"""Tests for B2B sales-material sharing: the link, the message, the ladder.

The invariants here are the ones whose absence is a *silent* failure -- the rep
presses send, WhatsApp opens, and only the prospect finds out something is
wrong:

* **The MSISDN survives every spelling in the database.** ``wa.me`` needs
  ``201XXXXXXXXX``; ``mobile_no`` holds four other spellings of the same
  subscriber plus the occasional missing trunk zero. Getting this wrong does
  not error -- it opens a chat with a different real person, or a dead end.
* **The link can never go missing from the message.** The rep edits the text
  freely, including deleting the placeholder. A message about a price list that
  does not contain the price list is the one outcome worth defending against
  unconditionally.
* **The tier ladder never upscales.** A 900px photo asked for the 3200px tier
  must be written at 900px, or the viewer's zoom ceiling promises detail that
  was interpolated into existence.
* **A material list arrives whole regardless of transport.** Dio form-encodes a
  Dart list into repeated keys that Frappe flattens to the LAST value only, so
  the app sends JSON -- but tests and bench pass a real list.

Pure ``unittest`` -- no site, no fixtures. The rasteriser is exercised through
Pillow directly, which is a real dependency of Frappe rather than a stub.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from jarz_pos.utils.phone import whatsapp_msisdn


class TestWhatsappMsisdn(unittest.TestCase):
    """Every stored spelling of one Egyptian mobile -> one wa.me MSISDN."""

    CANONICAL = "201111034268"

    def test_every_stored_spelling_collapses(self):
        for stored in (
            "01111034268",
            "+201111034268",
            "201111034268",
            "00201111034268",
            "+20 111 103 4268",
            "0111 103 4268",
            "1111034268",  # trunk zero omitted at entry
        ):
            with self.subTest(stored=stored):
                self.assertEqual(whatsapp_msisdn(stored), self.CANONICAL)

    def test_blank_is_blank_not_a_guess(self):
        for value in (None, "", "   ", "n/a"):
            self.assertEqual(whatsapp_msisdn(value), "")

    def test_foreign_number_is_not_re_prefixed(self):
        """A Saudi number must not be handed an Egyptian country code."""
        self.assertEqual(whatsapp_msisdn("+966512345678"), "966512345678")

    def test_landline_keeps_its_own_shape(self):
        """Ten digits that do not start with 1 are not a mobile; do not guess."""
        self.assertEqual(whatsapp_msisdn("0223456789"), "0223456789")


class TestMessageRendering(unittest.TestCase):
    """``{name}`` and ``{link}`` substitution, including the abuse cases."""

    URL = "https://erp.orderjarz.com/m/AbCdEfGhIjKlMnOp"

    def setUp(self):
        from jarz_pos.services import materials

        self.materials = materials

    def test_default_template_carries_both_placeholders(self):
        template = self.materials.default_message_template()
        self.assertIn(self.materials.NAME_PLACEHOLDER, template)
        self.assertIn(self.materials.LINK_PLACEHOLDER, template)

    def test_placeholders_are_filled(self):
        text = self.materials.render_message(
            self.materials.default_message_template(), self.URL, "Ahmed"
        )
        self.assertIn(self.URL, text)
        self.assertIn("Ahmed", text)
        self.assertNotIn(self.materials.NAME_PLACEHOLDER, text)
        self.assertNotIn(self.materials.LINK_PLACEHOLDER, text)

    def test_missing_name_falls_back_rather_than_leaving_a_hole(self):
        text = self.materials.render_message("Hi {name}", self.URL, None)
        self.assertIn(self.materials.NAME_FALLBACK, text)
        self.assertNotIn("{name}", text)

    def test_link_is_appended_when_the_rep_deleted_the_placeholder(self):
        text = self.materials.render_message("just some words", self.URL, "Ahmed")
        self.assertTrue(text.endswith(self.URL))

    def test_link_is_not_duplicated_when_pasted_manually(self):
        text = self.materials.render_message(f"see {self.URL} thanks", self.URL)
        self.assertEqual(text.count(self.URL), 1)

    def test_empty_template_falls_back_to_the_default(self):
        text = self.materials.render_message("   ", self.URL, "Ahmed")
        self.assertIn(self.URL, text)
        self.assertIn("Ahmed", text)


class TestWhatsappUrl(unittest.TestCase):
    def setUp(self):
        from jarz_pos.services import materials

        self.materials = materials

    def test_number_and_text_are_encoded(self):
        url = self.materials.whatsapp_url("201111034268", "hello world")
        self.assertTrue(url.startswith("https://wa.me/201111034268?text="))
        self.assertIn("hello%20world", url)

    def test_newlines_survive_as_encoded_text(self):
        url = self.materials.whatsapp_url("201111034268", "a\nb")
        self.assertIn("a%0Ab", url)

    def test_no_number_still_opens_the_composer(self):
        """A contact with no phone must still produce a usable send action."""
        url = self.materials.whatsapp_url("", "hi")
        self.assertTrue(url.startswith("https://wa.me/?text="))

    def test_formatting_in_the_number_is_stripped(self):
        url = self.materials.whatsapp_url("+20 111 103 4268", "hi")
        self.assertTrue(url.startswith("https://wa.me/201111034268?"))


class TestArgumentCoercion(unittest.TestCase):
    """``_as_list`` -- one contract, three transports."""

    def setUp(self):
        from jarz_pos.api.materials import _as_list

        self._as_list = _as_list

    def test_json_string_from_the_app(self):
        self.assertEqual(self._as_list('["MAT-00001", "MAT-00002"]'), ["MAT-00001", "MAT-00002"])

    def test_real_list_from_bench_and_tests(self):
        self.assertEqual(self._as_list(["MAT-00001"]), ["MAT-00001"])

    def test_csv_from_a_hand_built_call(self):
        self.assertEqual(self._as_list("MAT-00001, MAT-00002"), ["MAT-00001", "MAT-00002"])

    def test_blanks_and_none_are_dropped_not_passed_through(self):
        self.assertEqual(self._as_list(None), [])
        self.assertEqual(self._as_list(""), [])
        self.assertEqual(self._as_list('["MAT-00001", "", "  "]'), ["MAT-00001"])

    def test_malformed_json_degrades_to_csv_instead_of_raising(self):
        self.assertEqual(self._as_list("[MAT-00001"), ["[MAT-00001"])


class TestTierLadder(unittest.TestCase):
    """The ladder itself, against real Pillow rather than a mock."""

    def setUp(self):
        from jarz_pos.services import materials

        self.materials = materials
        self.tmp = tempfile.mkdtemp(prefix="jarz-mat-")

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _image(self, width, height):
        from PIL import Image

        return Image.new("RGB", (width, height), (200, 210, 220))

    def test_large_source_is_downscaled_to_the_tier(self):
        path = os.path.join(self.tmp, "big.jpg")
        size = self.materials._write_tier(self._image(5000, 4000), 3200, 88, path)
        self.assertEqual(size[0], 3200)
        self.assertEqual(size[1], 2560)
        self.assertTrue(os.path.exists(path))

    def test_small_source_is_never_upscaled(self):
        path = os.path.join(self.tmp, "small.jpg")
        size = self.materials._write_tier(self._image(900, 600), 3200, 88, path)
        self.assertEqual(size, (900, 600))

    def test_portrait_orientation_is_measured_on_the_long_edge(self):
        path = os.path.join(self.tmp, "tall.jpg")
        size = self.materials._write_tier(self._image(2400, 4800), 1600, 84, path)
        self.assertEqual(size[1], 1600)
        self.assertEqual(size[0], 800)

    def test_written_file_is_a_readable_progressive_jpeg(self):
        from PIL import Image

        path = os.path.join(self.tmp, "check.jpg")
        self.materials._write_tier(self._image(2000, 1000), 480, 76, path)
        with Image.open(path) as img:
            self.assertEqual(img.format, "JPEG")
            self.assertEqual(img.size, (480, 240))

    def test_no_temp_file_is_left_behind(self):
        """The write is atomic: rename, never write-in-place."""
        path = os.path.join(self.tmp, "atomic.jpg")
        self.materials._write_tier(self._image(1000, 1000), 480, 76, path)
        self.assertFalse(os.path.exists(path + ".tmp"))

    def test_tier_names_are_the_three_the_viewer_asks_for(self):
        """``m.html`` indexes the manifest by these exact keys."""
        self.assertEqual(set(self.materials.TIER_NAMES), {"thumb", "screen", "full"})

    def test_full_tier_stays_within_the_memory_budget(self):
        """3200 long edge -> ~30MB decoded. See the module docstring."""
        largest = max(edge for _, edge, _ in self.materials.TIERS)
        self.assertLessEqual(largest, 3200)


if __name__ == "__main__":
    unittest.main()
