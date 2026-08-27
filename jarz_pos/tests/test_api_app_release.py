"""Tests for the mobile release floor and the request gate that enforces it.

Runs against a stubbed ``frappe`` so it works on the CI logic gate, which
executes before ``bench migrate`` and therefore cannot see the new
``mobile_minimum_android_build`` field in the database.

The bias under test is "fail open": every unreadable setting, unparseable
header and missing build number must let the request through. A version check
that wrongly blocks is worse than one that wrongly allows, because the blocked
device has no way to fix itself.
"""

import importlib
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock


class _Headers(dict):
    """Werkzeug-ish headers: ``get`` is case-insensitive."""

    def get(self, key, default=None):
        for existing, value in self.items():
            if existing.lower() == str(key).lower():
                return value
        return default


class AppReleaseTestCase(unittest.TestCase):
    def _load_module(self, settings=None, url="https://erp.example.com"):
        """Import ``app_release`` against a stub frappe wired to ``settings``."""
        values = dict(settings or {})

        fake_frappe = types.ModuleType("frappe")
        fake_frappe.whitelist = lambda *args, **kwargs: (lambda fn: fn)
        fake_frappe.log_error = MagicMock()
        fake_frappe.get_traceback = MagicMock(return_value="traceback")
        fake_frappe.db = SimpleNamespace(
            get_single_value=MagicMock(side_effect=lambda _dt, field: values.get(field))
        )
        fake_frappe.utils = SimpleNamespace(get_url=MagicMock(return_value=url))
        fake_frappe.local = SimpleNamespace(request=None, form_dict={}, response={})

        class _ValidationError(Exception):
            pass

        fake_frappe.exceptions = SimpleNamespace(ValidationError=_ValidationError)

        original = sys.modules.get("frappe")
        sys.modules["frappe"] = fake_frappe
        try:
            sys.modules.pop("jarz_pos.api.app_release", None)
            module = importlib.import_module("jarz_pos.api.app_release")
            module = importlib.reload(module)
        finally:
            if original is not None:
                sys.modules["frappe"] = original
            else:
                sys.modules.pop("frappe", None)
        return module

    @staticmethod
    def _request(module, build=None, platform="android", path="/api/method/jarz_pos.api.pos.create_order"):
        headers = _Headers()
        if build is not None:
            headers[module.BUILD_HEADER] = build
        if platform is not None:
            headers[module.PLATFORM_HEADER] = platform
        module.frappe.local.request = SimpleNamespace(headers=headers, path=path)
        module.frappe.local.form_dict = {}
        module.frappe.local.response = {}


class TestResolveRequirement(AppReleaseTestCase):
    def test_unset_floor_never_blocks(self):
        """An untouched Single reads 0 for both Ints - the disabled state."""
        module = self._load_module(settings={})

        result = module.get_app_requirement(platform="android", build_number="900")

        self.assertFalse(result["update_required"])
        self.assertFalse(result["update_available"])
        self.assertEqual(result["minimum_build"], 0)

    def test_build_below_floor_is_blocked(self):
        module = self._load_module(settings={"mobile_minimum_android_build": 1200})

        result = module.get_app_requirement(platform="android", build_number="1199")

        self.assertTrue(result["update_required"])
        self.assertEqual(result["current_build"], 1199)
        self.assertEqual(result["minimum_build"], 1200)

    def test_build_exactly_at_floor_is_allowed(self):
        """The floor is inclusive: the build named in the setting still runs."""
        module = self._load_module(settings={"mobile_minimum_android_build": 1200})

        result = module.get_app_requirement(platform="android", build_number="1200")

        self.assertFalse(result["update_required"])

    def test_non_android_platform_is_never_gated(self):
        """Web is whatever the server just served; it has no APK to install."""
        module = self._load_module(settings={"mobile_minimum_android_build": 1200})

        result = module.get_app_requirement(platform="web", build_number="1")

        self.assertFalse(result["gated"])
        self.assertFalse(result["update_required"])

    def test_missing_build_number_fails_open(self):
        """Builds predating this feature report nothing and must not be blocked."""
        module = self._load_module(settings={"mobile_minimum_android_build": 1200})

        result = module.get_app_requirement(platform="android", build_number=None)

        self.assertIsNone(result["current_build"])
        self.assertFalse(result["update_required"])

    def test_garbage_build_number_fails_open(self):
        module = self._load_module(settings={"mobile_minimum_android_build": 1200})

        for value in ("1.0.0+42", "", "  ", "abc", "-5", "0"):
            with self.subTest(value=value):
                result = module.get_app_requirement(platform="android", build_number=value)
                self.assertIsNone(result["current_build"])
                self.assertFalse(result["update_required"])

    def test_latest_build_drives_the_soft_nudge_only(self):
        module = self._load_module(
            settings={"mobile_minimum_android_build": 1000, "mobile_latest_android_build": 1300}
        )

        result = module.get_app_requirement(platform="android", build_number="1200")

        self.assertTrue(result["update_available"])
        self.assertFalse(result["update_required"])

    def test_download_url_falls_back_to_site_download_page(self):
        module = self._load_module(settings={}, url="https://erp.example.com/")

        result = module.get_app_requirement(platform="android", build_number="1")

        self.assertEqual(result["download_url"], "https://erp.example.com/pos/download/")

    def test_configured_download_url_wins(self):
        module = self._load_module(settings={"mobile_apk_download_url": "https://cdn.example.com/app.apk"})

        result = module.get_app_requirement(platform="android", build_number="1")

        self.assertEqual(result["download_url"], "https://cdn.example.com/app.apk")

    def test_settings_read_failure_fails_open(self):
        """A broken settings read must not be the reason the POS will not open."""
        module = self._load_module(settings={"mobile_minimum_android_build": 9999})
        module.frappe.db.get_single_value.side_effect = RuntimeError("db down")

        result = module.get_app_requirement(platform="android", build_number="1")

        self.assertTrue(result["ok"])
        self.assertFalse(result["update_required"])
        self.assertEqual(result["minimum_build"], 0)


class TestBeforeRequestGate(AppReleaseTestCase):
    def test_stale_android_build_is_refused_with_426(self):
        module = self._load_module(settings={"mobile_minimum_android_build": 1200})
        self._request(module, build="1100")

        with self.assertRaises(module.AppUpgradeRequired) as caught:
            module.before_request()

        # The attribute, not the response dict, is what Frappe's
        # handle_exception reads to pick the status it actually sends.
        self.assertEqual(caught.exception.http_status_code, 426)

        response = module.frappe.local.response
        self.assertEqual(response["http_status_code"], 426)
        self.assertTrue(response["update_required"])
        self.assertEqual(response["minimum_build"], 1200)
        self.assertEqual(response["download_url"], "https://erp.example.com/pos/download/")

    def test_current_build_passes_through(self):
        module = self._load_module(settings={"mobile_minimum_android_build": 1200})
        self._request(module, build="1200")

        module.before_request()

        self.assertEqual(module.frappe.local.response, {})

    def test_request_without_build_header_passes_through(self):
        """Desk, Woo webhooks and the deploy verifier send no build header."""
        module = self._load_module(settings={"mobile_minimum_android_build": 1200})
        self._request(module, build=None, platform=None)

        module.before_request()

        self.assertEqual(module.frappe.local.response, {})

    def test_non_android_build_header_passes_through(self):
        module = self._load_module(settings={"mobile_minimum_android_build": 1200})
        self._request(module, build="1", platform="web")

        module.before_request()

        self.assertEqual(module.frappe.local.response, {})

    def test_the_version_check_itself_is_exempt(self):
        """A blocked client must still be able to ask why it is blocked."""
        module = self._load_module(settings={"mobile_minimum_android_build": 1200})
        self._request(
            module,
            build="1100",
            path="/api/method/jarz_pos.api.app_release.get_app_requirement",
        )

        module.before_request()

        self.assertEqual(module.frappe.local.response, {})

    def test_exempt_method_resolved_from_cmd_form_key(self):
        """Frappe's older ``?cmd=`` calling convention resolves the same way."""
        module = self._load_module(settings={"mobile_minimum_android_build": 1200})
        self._request(module, build="1100", path="/")
        module.frappe.local.form_dict = {"cmd": "logout"}

        module.before_request()

        self.assertEqual(module.frappe.local.response, {})

    def test_no_request_context_passes_through(self):
        """Background jobs and bench console have no request to inspect."""
        module = self._load_module(settings={"mobile_minimum_android_build": 1200})
        module.frappe.local.request = None

        module.before_request()

    def test_gate_failure_never_blocks_the_site(self):
        """This runs ahead of every request; a bug here must not take the site down."""
        module = self._load_module(settings={"mobile_minimum_android_build": 1200})
        self._request(module, build="1100")
        module.frappe.db.get_single_value.side_effect = RuntimeError("db down")

        module.before_request()

        self.assertEqual(module.frappe.local.response, {})


if __name__ == "__main__":
    unittest.main()
