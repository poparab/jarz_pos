"""Observability tests: request context, PII redaction, error envelope, release tag.

WRITTEN AS ``unittest.TestCase`` ON PURPOSE. This module used to be pytest-style
(bare ``def test_*``, a ``monkeypatch`` fixture, plain ``assert``). pytest is not
installed in the bench environment and ``bench run-tests`` drives the stdlib
unittest loader, which collects nothing from a bare function -- so every case here
silently reported "0 tests" and had NEVER executed, including the redaction case
somebody wrote believing it guarded PII. Do not reintroduce pytest constructs.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe

from jarz_pos.observability import error_response
from jarz_pos.observability import sentry_bootstrap
from jarz_pos.observability.request_context import infer_backend_app, normalize_environment


class TestRequestContext(unittest.TestCase):
    def test_normalize_environment_aliases(self):
        self.assertEqual(normalize_environment("prod"), "production")
        self.assertEqual(normalize_environment("production"), "production")
        self.assertEqual(normalize_environment("staging"), "staging")
        self.assertEqual(normalize_environment("testing"), "staging")

    def test_infer_backend_app_prefers_custom_app_prefixes(self):
        self.assertEqual(
            infer_backend_app(command="jarz_woocommerce_integration.api.orders.woo_order_webhook"),
            "jarz_woocommerce_integration",
        )
        self.assertEqual(
            infer_backend_app(path="/api/method/jarz_pos.api.test_connection.ping"),
            "jarz_pos",
        )


class TestBeforeSendRedaction(unittest.TestCase):
    """The only thing standing between a Sentry event and a leaked credential."""

    def _fake_frappe(self, **conf):
        return SimpleNamespace(conf=dict(conf))

    def test_before_send_redacts_sensitive_fields(self):
        event = {
            "request": {
                "headers": {
                    "Authorization": "Bearer secret",
                    "X-Test": "ok",
                },
                "data": {
                    "password": "hidden",
                    "customer": "CUST-0001",
                },
            },
            "extra": {
                "token": "private-token",
                "safe": "value",
            },
        }

        with patch.object(sentry_bootstrap, "frappe", self._fake_frappe()):
            sanitized = sentry_bootstrap._before_send(event, {})

        self.assertEqual(sanitized["request"]["headers"], {"X-Test": "ok"})
        self.assertEqual(sanitized["request"]["data"]["password"], "<redacted>")
        self.assertEqual(sanitized["request"]["data"]["customer"], "CUST-0001")
        self.assertEqual(sanitized["extra"]["token"], "<redacted>")
        self.assertEqual(sanitized["extra"]["safe"], "value")

    def test_before_send_drops_the_user_block_unless_staff_email_is_enabled(self):
        event = {"user": {"email": "cashier@example.com"}}

        with patch.object(sentry_bootstrap, "frappe", self._fake_frappe()):
            sanitized = sentry_bootstrap._before_send(dict(event), {})
        self.assertNotIn("user", sanitized)

        with patch.object(
            sentry_bootstrap, "frappe", self._fake_frappe(sentry_staff_email_enabled=1)
        ):
            sanitized = sentry_bootstrap._before_send(dict(event), {})
        self.assertIn("user", sanitized)


class TestReleaseName(unittest.TestCase):
    """FIX D -- the release tag must follow the deployed commit.

    Both servers reported ``jarz-pos-backend@bbc8f39c...`` (2026-05-04, 335 commits
    stale) because ``sentry_release_backend`` is a hand-typed site_config key that no
    deploy step ever rewrites. Stack-trace line numbers then belong to a different
    tree than the one running, and "first seen in release" means nothing.
    """

    SHA = "093cb540ca381e80c76c391c58e4a0fe62840116"
    OTHER_SHA = "1111111111111111111111111111111111111111"

    def setUp(self):
        sentry_bootstrap._reset_release_name_cache()
        self.addCleanup(sentry_bootstrap._reset_release_name_cache)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # <tmp>/apps/jarz_pos is the app dir; get_app_path() returns the package
        # dir one level deeper, exactly as frappe does on a real bench.
        self.app_dir = os.path.join(self._tmp.name, "apps", "jarz_pos")
        self.package_dir = os.path.join(self.app_dir, "jarz_pos")
        os.makedirs(self.package_dir)

    # -- helpers -----------------------------------------------------------
    def _fake_frappe(self, conf=None, app_path=None, get_app_path=None):
        if get_app_path is None:
            resolved = self.package_dir if app_path is None else app_path

            def get_app_path(app_name):  # noqa: ARG001
                return resolved

        return SimpleNamespace(conf=dict(conf or {}), get_app_path=get_app_path)

    def _write(self, relative_path, contents):
        full_path = os.path.join(self.app_dir, ".git", *relative_path.split("/"))
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "w", encoding="utf-8") as handle:
            handle.write(contents)

    # -- cases -------------------------------------------------------------
    def test_configured_release_is_returned_verbatim_and_skips_the_git_read(self):
        get_app_path = MagicMock(side_effect=AssertionError("must not read .git"))
        fake = self._fake_frappe(
            conf={"sentry_release_backend": "  jarz-pos-backend@deadbeef  "},
            get_app_path=get_app_path,
        )

        with patch.object(sentry_bootstrap, "frappe", fake):
            self.assertEqual(sentry_bootstrap._release_name(), "jarz-pos-backend@deadbeef")

        get_app_path.assert_not_called()

    def test_loose_ref_resolves_to_the_deployed_commit(self):
        self._write("HEAD", "ref: refs/heads/main\n")
        self._write("refs/heads/main", f"{self.SHA}\n")

        with patch.object(sentry_bootstrap, "frappe", self._fake_frappe()):
            self.assertEqual(
                sentry_bootstrap._release_name(), f"jarz-pos-backend@{self.SHA}"
            )

    def test_packed_refs_resolve_when_no_loose_ref_file_exists(self):
        # `git gc` / a fresh clone leaves refs only in packed-refs.
        self._write("HEAD", "ref: refs/heads/main\n")
        self._write(
            "packed-refs",
            "# pack-refs with: peeled fully-peeled sorted \n"
            f"{self.OTHER_SHA} refs/heads/develop\n"
            f"{self.SHA} refs/heads/main\n"
            f"{self.OTHER_SHA} refs/tags/v1.0\n"
            f"^{self.OTHER_SHA}\n",
        )

        with patch.object(sentry_bootstrap, "frappe", self._fake_frappe()):
            self.assertEqual(
                sentry_bootstrap._release_name(), f"jarz-pos-backend@{self.SHA}"
            )

    def test_detached_head_uses_the_sha_in_the_head_file(self):
        self._write("HEAD", f"{self.SHA}\n")

        with patch.object(sentry_bootstrap, "frappe", self._fake_frappe()):
            self.assertEqual(
                sentry_bootstrap._release_name(), f"jarz-pos-backend@{self.SHA}"
            )

    def test_missing_git_directory_returns_none_and_does_not_raise(self):
        # No .git written at all. This runs inside before_request: an exception
        # escaping here would break every HTTP request on the site.
        with patch.object(sentry_bootstrap, "frappe", self._fake_frappe()):
            self.assertIsNone(sentry_bootstrap._release_name())

    def test_unreadable_ref_returns_none_and_does_not_raise(self):
        self._write("HEAD", "ref: refs/heads/main\n")
        # HEAD points at a ref with neither a loose file nor a packed-refs entry.
        with patch.object(sentry_bootstrap, "frappe", self._fake_frappe()):
            self.assertIsNone(sentry_bootstrap._release_name())

    def test_garbage_head_contents_return_none(self):
        self._write("HEAD", "not-a-sha-at-all\n")
        with patch.object(sentry_bootstrap, "frappe", self._fake_frappe()):
            self.assertIsNone(sentry_bootstrap._release_name())

    def test_get_app_path_failure_is_swallowed(self):
        fake = self._fake_frappe(
            get_app_path=MagicMock(side_effect=RuntimeError("no bench here"))
        )
        with patch.object(sentry_bootstrap, "frappe", fake):
            self.assertIsNone(sentry_bootstrap._release_name())

    def test_result_is_cached_so_the_hot_path_reads_git_once(self):
        self._write("HEAD", "ref: refs/heads/main\n")
        self._write("refs/heads/main", f"{self.SHA}\n")
        get_app_path = MagicMock(return_value=self.package_dir)
        fake = self._fake_frappe(get_app_path=get_app_path)

        with patch.object(sentry_bootstrap, "frappe", fake):
            first = sentry_bootstrap._release_name()
            second = sentry_bootstrap._release_name()

        self.assertEqual(first, f"jarz-pos-backend@{self.SHA}")
        self.assertEqual(second, first)
        self.assertEqual(get_app_path.call_count, 1)

    def test_git_dir_pointer_file_is_followed(self):
        # A worktree/submodule checkout writes ".git" as a FILE holding "gitdir: ...".
        real_git_dir = os.path.join(self._tmp.name, "elsewhere", "gitdir")
        os.makedirs(os.path.join(real_git_dir, "refs", "heads"))
        with open(os.path.join(real_git_dir, "HEAD"), "w", encoding="utf-8") as handle:
            handle.write("ref: refs/heads/main\n")
        with open(
            os.path.join(real_git_dir, "refs", "heads", "main"), "w", encoding="utf-8"
        ) as handle:
            handle.write(f"{self.SHA}\n")
        with open(os.path.join(self.app_dir, ".git"), "w", encoding="utf-8") as handle:
            handle.write(f"gitdir: {real_git_dir}\n")

        with patch.object(sentry_bootstrap, "frappe", self._fake_frappe()):
            self.assertEqual(
                sentry_bootstrap._release_name(), f"jarz-pos-backend@{self.SHA}"
            )


class TestUnexpectedErrorResponse(unittest.TestCase):
    def test_unexpected_error_response_sets_status_and_error_id(self):
        previous_local = getattr(frappe, "local", None)
        previous_flags = getattr(frappe, "flags", None)

        try:
            with patch.object(error_response, "capture_exception", return_value=None), \
                 patch.object(frappe.utils, "now", return_value="2026-05-03 00:00:00"):
                frappe.local = frappe._dict(response=frappe._dict())
                frappe.flags = frappe._dict()

                result = error_response.unexpected_error_response(
                    ValueError("boom"),
                    summary="Unit test exception",
                    context="Unit Test",
                )
        finally:
            frappe.local = previous_local
            frappe.flags = previous_flags

        self.assertEqual(result["error_code"], "UNEXPECTED_SERVER_ERROR")
        self.assertEqual(result["context"], "Unit Test")
        self.assertTrue(result["error_id"])
        self.assertTrue(result["user_message"])
        self.assertFalse(result["success"])
