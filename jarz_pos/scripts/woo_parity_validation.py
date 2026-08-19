"""Staging-only WooCommerce <-> ERPNext money-parity validation runner.

Run from the STAGING backend container with::

    bench --site frontend execute \
        jarz_pos.scripts.woo_parity_validation.run_json \
        --kwargs '{"environment":"staging","allow_staging_mutations":true}'

This is the *money* companion to :mod:`jarz_pos.scripts.woo_staging_full_cycle`.
That runner proves the plumbing moves documents; this one proves the plumbing
moves the right **amount**, and asserts the ledger under every case where money
is involved.

Ten cases (W1..W10) are covered. Several of them assert behaviour that is
currently *wrong* — Woo shipping is never billed, a partial refund is invisible,
a positive fee line is dropped. Those are asserted as they actually behave today
and flagged ``Gap`` in the report, deliberately: an assertion written against the
behaviour we *want* would fail every run and be ignored within a week, while one
written loosely enough to tolerate both would pass by accident on the day the bug
gets worse. A ``Gap`` check that stops holding is a hard ``Fail`` — that is the
signal that the underlying behaviour moved and the report needs rewriting.

Everything this runner mutates, it created. Nothing pre-existing is ever touched.

Cross-app access
----------------
jarz_pos and jarz_woocommerce_integration are peers and must stay independent, so
this module contains **no import statement** naming the WooCommerce app: nothing
here adds an edge to the app dependency graph, and loading jarz_pos never pulls
the other app in. The harness resolves the WooCommerce services at *call* time
through :func:`frappe.get_module`, which is the standard Frappe indirection and
fails with a plain, obvious error on a site where that app is not installed. This
is a cross-app *validation harness*, not app code, and it is deliberately the
only shape of coupling it has. (The alternative — hosting this file inside
jarz_woocommerce_integration — is equally defensible; it lives here to sit
alongside ``woo_staging_full_cycle.py``, whose scaffolding it reuses wholesale.)

Safety
------
Staging is a **production clone**, so ``tabCustomer`` and
``tabWooCommerce Order Map`` already carry ids up to *production's* counter while
the demo store's own auto-increment sits BELOW that range. A newly created demo
order or customer can therefore be handed an id that already maps to a real
cloned record, and the inbound sync overwrites that record in place. That is how
roughly 17 real customer records were corrupted once already. Every id this
runner binds to is allocated *above* the mapped ceiling through the helpers in
``woo_staging_full_cycle`` (``_allocate_unhijacked_woo_customer``,
``_max_mapped_woo_order_id``, ``_preexisting_inbound_order_artifacts``), and
every mutation asserts membership in ``created_records`` first.
"""

from __future__ import annotations

import json
import re
import time
import traceback
from datetime import datetime, timedelta
from typing import Any, Callable

import frappe
from frappe.utils import now_datetime

from jarz_pos.scripts.woo_staging_full_cycle import (
    FullCycleRunner,
    _json_safe,
    _next_delivery_slot,
)


MARKER_START = "WOO_PARITY_JSON_START"
MARKER_END = "WOO_PARITY_JSON_END"

STATUS_PASS = "Pass"
STATUS_FAIL = "Fail"
STATUS_SKIP = "Skip"
STATUS_GAP = "Gap"

_ORDER_SYNC_MODULE = "jarz_woocommerce_integration.services.order_sync"
_OUTBOUND_SYNC_MODULE = "jarz_woocommerce_integration.services.outbound_sync"

#: Hosts this runner refuses outright. Production is never a valid target for a
#: runner that posts orders, mints coupons and files refunds.
PRODUCTION_HOST_MARKERS = ("erp.orderjarz.com",)
#: Positive proof of staging. Absence is a refusal, not a warning — "could not
#: prove it is staging" and "is staging" must never collapse into the same branch.
STAGING_HOST_MARKERS = ("erpstg",)
#: The WooCommerce store must itself be a non-production store. W3 creates a real
#: coupon and W2 files a real refund; pointed at the live shop that is damage, not
#: a test. Staging syncs to demo.orderjarz.com.
NON_PRODUCTION_STORE_MARKERS = ("demo.", "staging", "stg.", "-stg", "test.", "localhost", "127.0.0.1")

#: WooCommerce Settings fields this runner writes. Snapshotted and restored in a
#: ``finally``; the restore is itself asserted.
SETTINGS_FLAGS_TOUCHED = (
    "enable_outbound_orders",
)
#: Cursor fields. Not written deliberately, but the inbound crons move them under
#: us and a corrupted cursor silently stops ingestion, so they are snapshotted and
#: their drift is reported. (Six fields, not four: two cursors x three columns.)
SETTINGS_CURSOR_FIELDS = (
    "live_order_cursor_modified_gmt",
    "live_order_cursor_order_id",
    "live_order_cursor_synced_on",
    "cancelled_order_cursor_modified_gmt",
    "cancelled_order_cursor_order_id",
    "cancelled_order_cursor_synced_on",
)

#: Results from ``pull_single_order_phase1`` that mean "a live cron holds this
#: order right now". The */2, 7/22/37/52, :17 and 42 3,9,15,21 schedules all race
#: this runner; a lock is the lock doing its job, never a parity failure.
RETRYABLE_PULL_REASONS = {"locked", "db_locked"}

_JUNK_STATE = "ZZ-PARITY-NO-SUCH-STATE"


def _woo_module(dotted_path: str):
    """Resolve a WooCommerce service module at call time.

    Deliberately not an import: see the module docstring. ``frappe.get_module`` is
    the same indirection Frappe uses for every hook, and on a site without the
    WooCommerce app it raises a clear ``ModuleNotFoundError`` here rather than
    breaking jarz_pos at load time.
    """
    return frappe.get_module(dotted_path)


class ParityRunner(FullCycleRunner):
    """W1..W10 parity cases, composed on top of the full-cycle harness.

    Subclasses rather than copies: the HTTP layer (``_woo_client``), the
    id-collision helpers, ``_record_created``, the run-id-stamped fixture
    builders and the case/report scaffolding are all inherited unchanged. Only
    the cases and the money/ledger assertions are new.
    """

    def __init__(
        self,
        *,
        environment: str = "staging",
        allow_staging_mutations: bool = False,
        run_id: str | None = None,
    ) -> None:
        super().__init__(
            environment=environment,
            allow_staging_mutations=allow_staging_mutations,
            run_id=run_id or f"PARITY-STG-{now_datetime().strftime('%Y%m%d-%H%M%S')}",
        )
        self.capabilities: dict[str, Any] = {}
        self._created_index: set[tuple[str, str]] = set()
        self._settings_snapshot: dict[str, Any] = {}
        self._settings_restored = False
        self.report.update({
            "runner": "woo_parity_validation",
            "checks": [],
            "capabilities": {},
            "documented_gaps": [],
            "skips": [],
            "lock_retries": [],
            "settings_snapshot": {},
            "settings_restore": {},
            "cleanup": [],
        })

    # ------------------------------------------------------------------
    # Environment guard
    # ------------------------------------------------------------------

    def _guard_environment(self) -> None:
        """Refuse production, and refuse anything not *provably* staging.

        Three independent gates, all of which must pass. The environment label is
        the weakest of them — it is a kwarg a tired operator types — so the site
        host and the WooCommerce base URL are checked on their own terms.
        """
        env = (self.environment or "").strip().lower()
        if env in {"production", "prod", "live"}:
            raise RuntimeError(
                "REFUSED: woo_parity_validation posts orders, mints coupons and files "
                "refunds. It must never run against production."
            )
        if env != "staging":
            raise RuntimeError(
                f"REFUSED: environment={self.environment!r}. This runner only supports "
                "environment='staging'."
            )

        try:
            host_name = self._host_name().lower()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"REFUSED: site host_name could not be read ({exc}), so this run cannot "
                "prove it is on staging."
            ) from exc

        for marker in PRODUCTION_HOST_MARKERS:
            if marker in host_name:
                raise RuntimeError(f"REFUSED: site host_name {host_name!r} is production.")
        if not any(marker in host_name for marker in STAGING_HOST_MARKERS):
            raise RuntimeError(
                f"REFUSED: site host_name {host_name!r} carries no staging marker "
                f"{STAGING_HOST_MARKERS!r}. Not being able to prove this is staging is "
                "treated exactly like being production."
            )

        base_url = self._woo_base_url().lower()
        if not base_url:
            raise RuntimeError("REFUSED: WooCommerce base_url is not configured.")
        if not any(marker in base_url for marker in NON_PRODUCTION_STORE_MARKERS):
            raise RuntimeError(
                f"REFUSED: WooCommerce base_url {base_url!r} does not look like a "
                f"non-production store {NON_PRODUCTION_STORE_MARKERS!r}. This runner "
                "writes orders, coupons and refunds into whatever store it is pointed "
                "at; it will not guess."
            )

        if self.allow_staging_mutations and env != "staging":
            raise RuntimeError("REFUSED: mutation mode is only allowed on staging.")

    def _woo_base_url(self) -> str:
        try:
            value = frappe.db.get_single_value("WooCommerce Settings", "base_url")
            return str(value or "").strip().rstrip("/")
        except Exception:  # noqa: BLE001
            return ""

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def run(self, cleanup: bool = True) -> dict[str, Any]:  # type: ignore[override]
        try:
            self._guard_environment()
            self._snapshot_settings()
            self._case("PARITY-PF", "Preflight", self._preflight)
            self._case("PARITY-FX", "Fixture discovery", self._discover_fixtures)
            self._case("PARITY-CAP", "Store capability probe", self._probe_capabilities)
            if not self.allow_staging_mutations:
                # W6's parser table needs no store at all, so it still runs; its two
                # invoice-level halves skip with the read-only reason. W9 runs so its
                # skip is recorded rather than silently absent.
                self._case("W6", "Delivery slot formats", self._w6_delivery_slot_formats)
                self._case("W9", "Outbound kill-switch visibility", self._w9_kill_switch_visibility)
                self.report["skips"].append({
                    "scope": "W1,W2,W3,W4,W5,W7,W8,W10",
                    "reason": (
                        "allow_staging_mutations is false; every remaining case has to post a real "
                        "Woo order."
                    ),
                })
            else:
                self._case("PARITY-CUST", "Parity fixture customer", self._build_parity_customer)
                self._case("W1", "Woo shipping amount is never billed", self._w1_shipping_amount)
                self._case("W2", "Partial refund is structurally invisible", self._w2_partial_refund)
                self._case("W3", "Coupon passthrough", self._w3_coupon_passthrough)
                self._case("W4", "Non-coupon / dynamic discount", self._w4_noncoupon_discount)
                self._case("W5", "Positive fee lines are dropped", self._w5_positive_fee_line)
                # W6 runs AFTER the fixture customer so its two invoice-level halves
                # can post real orders instead of skipping.
                self._case("W6", "Delivery slot formats", self._w6_delivery_slot_formats)
                self._case("W7", "Terminal status on a submitted invoice", self._w7_terminal_status)
                self._case("W8", "Outbound invoice parity", self._w8_outbound_parity)
                self._case("W10", "Territory-unresolved fallback", self._w10_territory_unresolved)
                # W9 runs LAST: it needs a Sales Invoice this run created to push, and
                # pushing anything else would mutate a record cloned from production.
                self._case("W9", "Outbound kill-switch visibility", self._w9_kill_switch_visibility)
        except Exception as exc:  # noqa: BLE001
            self.report["errors"].append({
                "error": str(exc),
                "traceback": traceback.format_exc(limit=12),
            })
        finally:
            self._restore_settings()
            if cleanup:
                self._cleanup()
            self._finish_report()
            self.report["capabilities"] = _json_safe(self.capabilities)

        checks = self.report.get("checks", [])
        check_failures = sum(1 for row in checks if row.get("status") == STATUS_FAIL)

        # Preflight, fixture discovery and any case that RAISED record their
        # failures as inherited assertions or as a red case, not as checks. Folding
        # them in matters: a broken preflight with zero checks run would otherwise
        # report "0 failed" and read as a clean pass.
        check_ids = {str(row.get("check_id")) for row in checks}
        infra_failures = [
            row for row in self.report.get("assertions", [])
            if row.get("status") == "Fail" and str(row.get("assertion_id")) not in check_ids
        ]
        failing_cases = [
            str(row.get("case_id")) for row in self.report.get("cases", [])
            if row.get("status") == "Fail"
        ]
        failed = check_failures + len(infra_failures)
        if failing_cases and failed == 0:
            failed = len(failing_cases)

        return {
            "run_id": self.run_id,
            "environment": self.environment,
            "allow_staging_mutations": self.allow_staging_mutations,
            "cleanup": bool(cleanup),
            "passed": sum(1 for row in checks if row.get("status") in (STATUS_PASS, STATUS_GAP)),
            "failed": failed,
            "skipped": sum(1 for row in checks if row.get("status") == STATUS_SKIP),
            "check_failures": check_failures,
            "infra_failures": infra_failures,
            "failing_cases": failing_cases,
            "documented_gaps": len(self.report.get("documented_gaps", [])),
            "checks": checks,
            "capabilities": self.report.get("capabilities", {}),
            "created_records": self.report.get("created_records", []),
            "lock_retries": self.report.get("lock_retries", []),
            "settings_restore": self.report.get("settings_restore", {}),
            "cleanup_actions": self.report.get("cleanup", []),
            "errors": self.report.get("errors", []),
            "report": self.report,
        }

    # ------------------------------------------------------------------
    # Check recording
    # ------------------------------------------------------------------

    def _check(
        self,
        case: dict[str, Any],
        check_id: str,
        description: str,
        passed: bool,
        *,
        expected: Any = None,
        actual: Any = None,
        gap: str | None = None,
        skip: str | None = None,
        note: str | None = None,
    ) -> bool:
        """Record one parity check.

        ``skip`` is a *reason string*, never a boolean: a precondition the
        environment cannot satisfy has to say why, because a silent skip and a
        pass are indistinguishable in a report read six weeks later.
        """
        if skip:
            status = STATUS_SKIP
        elif passed:
            status = STATUS_GAP if gap else STATUS_PASS
        else:
            status = STATUS_FAIL

        row: dict[str, Any] = {
            "case_id": case["case_id"],
            "check_id": check_id,
            "description": description,
            "status": status,
            "expected": _json_safe(expected),
            "actual": _json_safe(actual),
        }
        if gap:
            row["documented_gap"] = gap
        if skip:
            row["skip_reason"] = skip
        if note:
            row["note"] = note
        self.report["checks"].append(row)

        if status == STATUS_GAP:
            self.report["documented_gaps"].append(row)
        if status == STATUS_SKIP:
            self.report["skips"].append(row)

        # Roll the check into the parent's per-case status machine so a failure
        # still turns the case red in the inherited summary.
        if status == STATUS_SKIP:
            self._assert(
                case, check_id, f"[SKIPPED] {description}", False,
                expected=expected,
                actual={"skip_reason": skip, "observed": _json_safe(actual)},
                concern=True,
            )
        elif status == STATUS_GAP:
            self._assert(
                case, check_id, f"[DOCUMENTED GAP] {description}", True,
                expected=expected, actual=actual,
            )
        else:
            self._assert(
                case, check_id, description, passed,
                expected=expected, actual=actual,
            )
        return status in (STATUS_PASS, STATUS_GAP)

    def _skip(
        self,
        case: dict[str, Any],
        check_id: str,
        description: str,
        reason: str,
        *,
        actual: Any = None,
    ) -> bool:
        return self._check(case, check_id, description, False, skip=reason, actual=actual)

    # ------------------------------------------------------------------
    # Created-record bookkeeping and mutation guards
    # ------------------------------------------------------------------

    def _record_created(self, record_type: str, record_name: str, *, note: str = "") -> None:
        super()._record_created(record_type, record_name, note=note)
        if record_name:
            self._created_index.add((str(record_type), str(record_name)))

    def _require_self_created(self, record_type: str, record_name: str) -> None:
        """Hard stop before any mutation of a record this run did not create.

        The whole class of damage this guard exists for looks like success from
        the inside: the sync happily rewrites a real cloned customer or invoice
        and every downstream assertion still passes, because the record it is
        checking is exactly the record it just overwrote.
        """
        key = (str(record_type), str(record_name))
        if key not in self._created_index:
            raise RuntimeError(
                f"REFUSED: {record_type} {record_name!r} was not created by this run "
                f"({self.run_id}). Mutating it could overwrite a real record cloned "
                "from production."
            )

    # ------------------------------------------------------------------
    # WooCommerce Settings snapshot / restore
    # ------------------------------------------------------------------

    def _read_single_raw(self, fieldnames: tuple[str, ...]) -> dict[str, Any]:
        """Read Single fields straight out of ``tabSingles``.

        Never ``get_single_value`` for these: it casts through ``cint()``, so
        "never written" and "operator deliberately set 0" come back identical. The
        distinction matters here — restoring a 0 onto a field that was never
        written is itself a change.
        """
        rows = frappe.db.sql(
            """
            SELECT field, value
            FROM `tabSingles`
            WHERE doctype = 'WooCommerce Settings'
              AND field IN %(fields)s
            """,
            {"fields": tuple(fieldnames)},
            as_dict=True,
        )
        present = {str(row.get("field")): row.get("value") for row in rows}
        return {
            field: {"present": field in present, "value": present.get(field)}
            for field in fieldnames
        }

    def _snapshot_settings(self) -> None:
        fields = tuple(SETTINGS_FLAGS_TOUCHED) + tuple(SETTINGS_CURSOR_FIELDS)
        self._settings_snapshot = self._read_single_raw(fields)
        self.report["settings_snapshot"] = _json_safe(self._settings_snapshot)

    def _write_single_field(self, fieldname: str, value: Any) -> None:
        """Write one field on the Single.

        ``frappe.db.set_value`` / ``set_single_value`` only — never ``doc.save()``.
        A full save of this Single validates every Link on it, and one dangling
        Link is enough to make the whole save throw, which then looks like the
        flag write failing for no reason.
        """
        try:
            frappe.db.set_value("WooCommerce Settings", "WooCommerce Settings", fieldname, value)
        except Exception:  # noqa: BLE001
            frappe.db.set_single_value("WooCommerce Settings", fieldname, value)
        frappe.clear_document_cache("WooCommerce Settings", "WooCommerce Settings")
        frappe.db.commit()

    def _restore_settings(self) -> None:
        if self._settings_restored or not self._settings_snapshot:
            return
        self._settings_restored = True
        restore: dict[str, Any] = {"flags": {}, "cursors": {}, "verified": True}

        for field in SETTINGS_FLAGS_TOUCHED:
            snap = self._settings_snapshot.get(field) or {}
            desired = snap.get("value")
            try:
                if snap.get("present"):
                    self._write_single_field(field, desired)
                current = (self._read_single_raw((field,)) or {}).get(field) or {}
                ok = str(current.get("value") or "") == str(desired or "")
                restore["flags"][field] = {
                    "expected": desired,
                    "actual": current.get("value"),
                    "restored": ok,
                }
                if not ok:
                    restore["verified"] = False
            except Exception as exc:  # noqa: BLE001
                restore["verified"] = False
                restore["flags"][field] = {"expected": desired, "error": str(exc)}

        # Cursors are reported, not rewritten: the live crons legitimately advance
        # them while this runs, and forcing them backwards would make the next
        # sweep re-ingest orders. Drift is evidence, not a fault.
        after = self._read_single_raw(tuple(SETTINGS_CURSOR_FIELDS))
        for field in SETTINGS_CURSOR_FIELDS:
            before_value = (self._settings_snapshot.get(field) or {}).get("value")
            after_value = (after.get(field) or {}).get("value")
            restore["cursors"][field] = {
                "before": before_value,
                "after": after_value,
                "moved_by_cron": str(before_value or "") != str(after_value or ""),
            }

        self.report["settings_restore"] = _json_safe(restore)

    # ------------------------------------------------------------------
    # Capability probing
    # ------------------------------------------------------------------

    def _probe_capabilities(self, case: dict[str, Any]) -> dict[str, Any]:
        """Find out what the demo store can actually do before asserting on it.

        The staging store is demo.orderjarz.com and carries a DIFFERENT plugin
        configuration from production. Nothing observed here is evidence about
        production's wire format; it only decides which checks are meaningful and
        which have to be skipped with a reason.
        """
        caps: dict[str, Any] = {}
        caps["store_base_url"] = self._woo_base_url()
        caps["store_is_demo"] = "demo." in caps["store_base_url"].lower()
        caps["orddd"] = self._probe_orddd()
        caps["woo_statuses"] = self._probe_woo_statuses()
        caps["bundles"] = self._probe_bundle_mappings()
        caps["coupons_api"] = self._probe_coupons_api()
        caps["delivery_territory"] = self._probe_delivery_territory()
        caps["promo_code"] = self._probe_promo_code()
        caps["stocked_items"] = self._probe_stocked_items()
        self.capabilities = caps

        self._check(
            case, "CAP.01",
            "Store is a non-production WooCommerce install",
            bool(caps["store_is_demo"]) or any(
                marker in caps["store_base_url"].lower() for marker in NON_PRODUCTION_STORE_MARKERS
            ),
            expected="non-production store",
            actual=caps["store_base_url"],
        )
        self._check(
            case, "CAP.02",
            "Store capabilities were probed before any wire-format assertion",
            True,
            expected="probe completed",
            actual={key: value for key, value in caps.items() if key != "store_base_url"},
            note=(
                "The demo store's plugin config differs from production. Nothing here is "
                "evidence about production's wire format."
            ),
        )
        return caps

    def _probe_orddd(self) -> dict[str, Any]:
        """Is the Order Delivery Date plugin present on this store?"""
        result: dict[str, Any] = {"present": None, "source": None, "detail": None}
        try:
            status = self._woo_client().get("system_status")
            plugins = (status or {}).get("active_plugins") if isinstance(status, dict) else None
            if isinstance(plugins, list) and plugins:
                blob = " ".join(
                    f"{row.get('plugin', '')} {row.get('name', '')}"
                    for row in plugins
                    if isinstance(row, dict)
                ).lower()
                result.update({
                    "present": ("orddd" in blob or "order-delivery-date" in blob),
                    "source": "system_status.active_plugins",
                })
                return result
        except Exception as exc:  # noqa: BLE001
            result["detail"] = f"system_status unavailable: {exc}"

        try:
            orders = self._woo_client().list_orders(
                params={"per_page": 20, "orderby": "date", "order": "desc"}
            )
            keys: set[str] = set()
            for order in orders or []:
                for meta in (order or {}).get("meta_data") or []:
                    keys.add(str((meta or {}).get("key") or ""))
            if keys:
                result.update({
                    "present": any(key.startswith("_orddd") for key in keys),
                    "source": "meta_data scan of the 20 most recent orders",
                    "sampled_meta_keys": sorted(
                        key for key in keys
                        if key.startswith("_orddd") or key in ("Delivery Date", "Time Slot")
                    ),
                })
            else:
                result["detail"] = f"{result['detail']} | no recent orders carried meta_data"
        except Exception as exc:  # noqa: BLE001
            result["detail"] = f"{result['detail']} | order scan failed: {exc}"
        return result

    def _probe_woo_statuses(self) -> dict[str, Any]:
        """Does this store carry the custom ``out-for-delivery`` order status?"""
        slugs: list[str] = []
        source = None
        detail = None
        for resource in ("orders/statuses", "reports/orders/totals"):
            try:
                body = self._woo_client().get(resource)
            except Exception as exc:  # noqa: BLE001
                detail = f"{resource}: {exc}"
                continue
            if isinstance(body, dict):
                slugs = [str(key) for key in body.keys()]
            elif isinstance(body, list):
                slugs = [str((row or {}).get("slug") or "") for row in body if isinstance(row, dict)]
            if slugs:
                source = resource
                break
        normalized = {slug.replace("wc-", "").strip().lower() for slug in slugs if slug}
        return {
            "slugs": sorted(normalized),
            "source": source,
            "detail": detail,
            "has_out_for_delivery": "out-for-delivery" in normalized,
        }

    def _probe_bundle_mappings(self) -> dict[str, Any]:
        try:
            rows = frappe.get_all(
                "Woo Jarz Bundle",
                fields=["name", "woo_bundle_id", "free_shipping"],
                limit_page_length=200,
            )
        except Exception as exc:  # noqa: BLE001
            return {"available": False, "detail": str(exc)}
        mapped = [row for row in rows if str(row.get("woo_bundle_id") or "").strip()]
        return {
            "available": bool(mapped),
            "total_bundles": len(rows),
            "mapped_bundles": len(mapped),
            "unmapped_bundles": len(rows) - len(mapped),
        }

    def _probe_coupons_api(self) -> dict[str, Any]:
        try:
            body = self._woo_client().get("coupons", params={"per_page": 1})
            return {"available": isinstance(body, (list, dict)), "detail": None}
        except Exception as exc:  # noqa: BLE001
            return {"available": False, "detail": str(exc)}

    def _probe_delivery_territory(self) -> dict[str, Any]:
        # ``delivery_income`` and ``pos_profile`` are custom fields. On a site where
        # a migrate has not landed them the query is a hard SQL error, which must
        # read as "capability absent", not as a crashed probe.
        try:
            rows = frappe.db.sql(
                """
                SELECT t.name AS territory, t.territory_name, t.pos_profile,
                       t.delivery_income, p.warehouse, p.selling_price_list AS price_list
                FROM `tabTerritory` t
                INNER JOIN `tabPOS Profile` p ON p.name = t.pos_profile
                WHERE IFNULL(t.is_group, 0) = 0
                  AND IFNULL(t.pos_profile, '') != ''
                  AND IFNULL(p.warehouse, '') != ''
                  AND IFNULL(p.selling_price_list, '') != ''
                  AND IFNULL(t.delivery_income, 0) > 0
                ORDER BY t.delivery_income ASC, t.modified DESC
                LIMIT 5
                """,
                as_dict=True,
            )
        except Exception as exc:  # noqa: BLE001
            return {"available": False, "candidates": [], "detail": str(exc)}
        return {"available": bool(rows), "candidates": [dict(row) for row in rows]}

    def _probe_promo_code(self) -> dict[str, Any]:
        """An enabled jarz_pos promo code, needed to give W8 a header discount."""
        try:
            columns = set(frappe.db.get_table_columns("Jarz Promo Code") or [])
        except Exception as exc:  # noqa: BLE001
            return {"available": False, "detail": f"Jarz Promo Code unavailable: {exc}"}
        if not columns:
            return {"available": False, "detail": "Jarz Promo Code has no table on this site"}

        filters: dict[str, Any] = {}
        if "disabled" in columns:
            filters["disabled"] = 0
        if "enabled" in columns:
            filters["enabled"] = 1
        try:
            rows = frappe.get_all(
                "Jarz Promo Code", filters=filters, fields=["name"], limit_page_length=5
            )
        except Exception as exc:  # noqa: BLE001
            return {"available": False, "detail": str(exc)}
        return {"available": bool(rows), "codes": [str(row.get("name")) for row in rows]}

    def _probe_stocked_items(self) -> dict[str, Any]:
        try:
            fixture = self._primary_territory_fixture()
        except Exception as exc:  # noqa: BLE001
            return {"available": False, "detail": str(exc)}
        try:
            rows = self._stocked_order_fixture_items(
                price_list=str(fixture.get("price_list") or ""),
                warehouse=str(fixture.get("warehouse") or ""),
            )
        except Exception as exc:  # noqa: BLE001
            return {"available": False, "detail": str(exc)}
        return {"available": len(rows) >= 1, "count": len(rows)}

    # ------------------------------------------------------------------
    # Fixture allocation
    # ------------------------------------------------------------------

    def _delivery_territory_fixture(self) -> dict[str, Any] | None:
        candidates = (self.capabilities.get("delivery_territory") or {}).get("candidates") or []
        return dict(candidates[0]) if candidates else None

    def _build_parity_customer(self, case: dict[str, Any]) -> dict[str, Any]:
        """One Woo customer, allocated above the mapped ceiling, reused by every case."""
        fixture = self._delivery_territory_fixture() or self._primary_territory_fixture()
        slug = re.sub(r"[^a-z0-9]+", "", self.run_id.lower())
        first_name = "Parity"
        last_name = self.run_id
        phone = self._unique_mobile()
        billing_line1 = f"{self.run_id} Parity Billing"
        shipping_line1 = f"{self.run_id} Parity Shipping"

        def payload_factory(attempt: int) -> dict[str, Any]:
            payload = self._build_woo_customer_payload(
                first_name=first_name,
                last_name=f"{last_name}-{attempt}",
                email=f"parity.{slug}.{attempt}@orderjarz.local",
                phone=phone,
                billing_line1=billing_line1,
                shipping_line1=shipping_line1,
                territory_fixture=fixture,
                billing_postcode="PAR001",
                shipping_postcode="PAR002",
            )
            payload["username"] = f"parity-{slug}-{attempt}"
            return payload

        ceiling = self._max_mapped_woo_customer_id()
        created, woo_customer_id, attempts = self._allocate_unhijacked_woo_customer(payload_factory)
        self._record_created("Woo Customer", woo_customer_id, note="PARITY-CUST fixture customer")

        collision = self._preexisting_customer_artifacts(woo_customer_id)
        self._check(
            case, "CUST.01",
            "Allocated Woo customer id sits above the mapped ceiling",
            int(woo_customer_id) > int(ceiling),
            expected=f"> {ceiling}",
            actual=woo_customer_id,
            note=(
                "Staging is a production clone; an id at or below the ceiling would bind to a real "
                "cloned Customer and the inbound sync would overwrite it in place."
            ),
        )
        self._check(
            case, "CUST.02",
            "No pre-existing ERP customer is bound to the allocated id",
            not collision["has_collision"],
            expected={"has_collision": False},
            actual=collision,
        )

        self.runtime_state["parity_customer"] = {
            "woo_customer_id": woo_customer_id,
            "email": created.get("email"),
            "first_name": first_name,
            "last_name": last_name,
            "phone": phone,
            "billing_line1": billing_line1,
            "shipping_line1": shipping_line1,
            "territory": fixture,
        }
        return {
            "woo_customer_id": woo_customer_id,
            "ceiling": ceiling,
            "burned_attempts": attempts,
            "territory_fixture": fixture,
        }

    def _parity_customer(self) -> dict[str, Any]:
        state = dict(self.runtime_state.get("parity_customer") or {})
        if not state.get("woo_customer_id"):
            raise RuntimeError("PARITY-CUST must run successfully before the order cases")
        return state

    # ------------------------------------------------------------------
    # Woo order allocation and mutation
    # ------------------------------------------------------------------

    def _max_bound_woo_order_id(self) -> int:
        """Ceiling across BOTH the order map and Sales Invoice.

        ``_max_mapped_woo_order_id`` only sees the map. An order deleted on the
        store leaves its ``woo_order_id`` on the invoice with no map row, so the
        map alone under-reports the used range — and under-reporting is the one
        failure mode that hands us a colliding id.
        """
        ceiling = int(self._max_mapped_woo_order_id() or 0)
        try:
            rows = frappe.db.sql(
                """
                SELECT MAX(CAST(woo_order_id AS UNSIGNED)) AS max_id
                FROM `tabSales Invoice`
                WHERE IFNULL(woo_order_id, 0) > 0
                """,
                as_dict=True,
            )
            ceiling = max(ceiling, int((rows[0].get("max_id") if rows else 0) or 0))
        except Exception:  # noqa: BLE001
            pass
        return ceiling

    def _allocate_unhijacked_woo_order(
        self,
        payload_factory: Callable[[int], dict[str, Any]],
        *,
        note: str,
    ) -> tuple[dict[str, Any], str, list[dict[str, Any]]]:
        """Create a Woo order whose id cannot belong to a real ERP invoice.

        Same shape as ``_allocate_unhijacked_woo_customer`` and the same
        deliberate strictness: an id is accepted only when it is ABOVE the bound
        ceiling, not merely when nothing occupies it today. A gap inside
        production's used range is unoccupied on this clone and claimed on the
        next one, so binding there produces a collision that surfaces long after
        this run reported success.
        """
        ceiling = self._max_bound_woo_order_id()
        attempts: list[dict[str, Any]] = []
        attempt = 0
        hard_cap = 250

        while attempt < hard_cap:
            attempt += 1
            candidate = self._woo_client().post("orders", payload_factory(attempt))
            candidate_id = str(candidate.get("id") or "")
            if not candidate_id:
                raise RuntimeError(f"Woo order create did not return an id: {candidate!r}")

            collision = self._preexisting_inbound_order_artifacts(candidate_id)
            try:
                candidate_id_int = int(candidate_id)
            except Exception:  # noqa: BLE001
                candidate_id_int = 0

            if collision.get("invoice_rows"):
                # An id already carrying a live invoice means the ceiling query is
                # not seeing what it should. Stop rather than burn past it.
                raise RuntimeError(
                    f"Refusing to continue: Woo order id {candidate_id} already maps to ERP "
                    f"invoice(s) {[row.get('name') for row in collision['invoice_rows']]!r}. "
                    "This run would have overwritten a real order."
                )

            if candidate_id_int > ceiling and not collision["has_collision"]:
                self._record_created("Woo Order", candidate_id, note=note)
                return candidate, candidate_id, attempts

            attempts.append({
                "attempt": attempt,
                "woo_order_id": candidate_id,
                "ceiling": ceiling,
                "collision": collision,
            })

        raise RuntimeError(
            f"Unable to allocate a Woo order id above the bound ceiling ({ceiling}) after "
            f"{attempt} attempts: {attempts!r}"
        )

    def _parity_order_payload(
        self,
        *,
        item_rows: list[dict[str, Any]],
        territory_fixture: dict[str, Any],
        status: str = "processing",
        delivery_slot: dict[str, str] | None = None,
        time_slot_label: str | None = "12:00-14:00",
        delivery_date_label: str | None = None,
        state_override: str | None = None,
        overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        customer = self._parity_customer()
        slot = dict(
            delivery_slot
            or self.fixture_catalog.get("next_delivery_slot")
            or _next_delivery_slot()
        )
        payload = self._build_woo_order_payload(
            woo_customer_id=str(customer["woo_customer_id"]),
            first_name=str(customer["first_name"]),
            last_name=str(customer["last_name"]),
            email=str(customer["email"] or f"parity.{self.run_id.lower()}@orderjarz.local"),
            phone=str(customer["phone"]),
            billing_line1=str(customer["billing_line1"]),
            shipping_line1=str(customer["shipping_line1"]),
            territory_fixture=territory_fixture,
            item_rows=item_rows,
            delivery_slot=slot,
            status=status,
            payment_method="cod",
            payment_method_title="Cash",
        )
        meta = [
            entry for entry in (payload.get("meta_data") or [])
            if str((entry or {}).get("key") or "") not in {"Delivery Date", "Time Slot"}
        ]
        meta.append({"key": "Delivery Date", "value": delivery_date_label or slot["delivery_date"]})
        if time_slot_label is not None:
            meta.append({"key": "Time Slot", "value": time_slot_label})
        payload["meta_data"] = meta

        if state_override is not None:
            for section in ("billing", "shipping"):
                payload[section] = {**payload[section], "state": state_override}

        for key, value in (overrides or {}).items():
            payload[key] = value
        return payload

    def _create_parity_order(self, *, note: str, **payload_kwargs: Any) -> dict[str, Any]:
        def factory(attempt: int) -> dict[str, Any]:
            payload = self._parity_order_payload(**payload_kwargs)
            payload["meta_data"] = list(payload.get("meta_data") or []) + [
                {"key": "parity_run_id", "value": self.run_id},
                {"key": "parity_attempt", "value": str(attempt)},
            ]
            return payload

        created, woo_order_id, attempts = self._allocate_unhijacked_woo_order(factory, note=note)
        refreshed = self._woo_order(woo_order_id) or created
        return {
            "woo_order_id": woo_order_id,
            "created": created,
            "order": refreshed,
            "burned_attempts": attempts,
        }

    def _put_created_order(self, woo_order_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._require_self_created("Woo Order", str(woo_order_id))
        return self._woo_client().put(f"orders/{woo_order_id}", payload)

    def _pull_created_order(self, woo_order_id: str, *, allow_update: bool = True) -> dict[str, Any]:
        """Drive inbound for an order THIS run created, retrying past cron locks."""
        self._require_self_created("Woo Order", str(woo_order_id))
        order_sync = _woo_module(_ORDER_SYNC_MODULE)

        attempts: list[dict[str, Any]] = []
        result: dict[str, Any] = {}
        for attempt in range(1, 5):
            result = order_sync.pull_single_order_phase1(woo_order_id, allow_update=allow_update) or {}
            frappe.db.commit()
            reason = str(result.get("reason") or "").strip().lower()
            if reason not in RETRYABLE_PULL_REASONS:
                break
            attempts.append({"attempt": attempt, "reason": reason})
            time.sleep(3)

        if attempts:
            self.report["lock_retries"].append({
                "woo_order_id": woo_order_id,
                "attempts": attempts,
                "final_reason": result.get("reason"),
            })
        result["_lock_retries"] = attempts
        return result

    def _pull_was_locked(self, result: dict[str, Any]) -> bool:
        return str((result or {}).get("reason") or "").strip().lower() in RETRYABLE_PULL_REASONS

    def _lock_skip_reason(self, result: dict[str, Any]) -> str:
        return (
            "A live cron held the order lock for the whole retry budget "
            f"(reason={result.get('reason')!r}). The */2, 7/22/37/52, :17 and 42 3,9,15,21 "
            "schedules race this runner; a held lock is the lock working, not a parity failure."
        )

    def _invoice_for_order(self, woo_order_id: str) -> Any:
        rows = self._active_invoices_for_woo_order_id(woo_order_id)
        if not rows:
            return None
        return frappe.get_doc("Sales Invoice", str(rows[0].get("name")))

    def _order_map_full(self, woo_order_id: str) -> dict[str, Any] | None:
        row = frappe.db.get_value(
            "WooCommerce Order Map",
            {"woo_order_id": woo_order_id},
            [
                "name", "woo_order_id", "erpnext_sales_invoice", "status", "hash",
                "needs_territory_recheck", "last_territory_error",
                "needs_manual_review", "manual_review_reason",
                "promo_mismatch", "promo_mismatch_note",
                "resolved_order_territory", "woo_billing_state", "woo_shipping_state",
            ],
            as_dict=True,
        )
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # Money / ledger helpers
    # ------------------------------------------------------------------

    def _shipping_income_row(self, invoice_doc: Any) -> dict[str, Any] | None:
        """The Shipping Income tax row — the ONLY source of truth for shipping.

        Never ``custom_delivery_income`` (NOT NULL DEFAULT 0, so it reads 0 for
        "never set") and never the Territory rate (that is the *policy* input, not
        what was billed). No row means zero shipping was billed, full stop.
        """
        for row in getattr(invoice_doc, "taxes", []) or []:
            description = str(getattr(row, "description", "") or "")
            if (
                str(getattr(row, "charge_type", "") or "") == "Actual"
                and description.startswith("Shipping Income")
            ):
                return {
                    "idx": getattr(row, "idx", None),
                    "description": description,
                    "account_head": getattr(row, "account_head", None),
                    "tax_amount": float(getattr(row, "tax_amount", 0) or 0),
                }
        return None

    def _gl_rows(self, invoice_name: str) -> list[dict[str, Any]]:
        rows = frappe.db.sql(
            """
            SELECT account, party_type, party,
                   ROUND(SUM(debit), 2) AS debit,
                   ROUND(SUM(credit), 2) AS credit
            FROM `tabGL Entry`
            WHERE voucher_type = 'Sales Invoice'
              AND voucher_no = %(voucher)s
              AND IFNULL(is_cancelled, 0) = 0
            GROUP BY account, party_type, party
            ORDER BY account
            """,
            {"voucher": invoice_name},
            as_dict=True,
        )
        return [dict(row) for row in rows]

    def _gl_summary(self, invoice_name: str) -> dict[str, Any]:
        rows = self._gl_rows(invoice_name)
        total_debit = round(sum(float(row.get("debit") or 0) for row in rows), 2)
        total_credit = round(sum(float(row.get("credit") or 0) for row in rows), 2)
        receivable = [row for row in rows if str(row.get("party_type") or "") == "Customer"]
        non_receivable = [row for row in rows if str(row.get("party_type") or "") != "Customer"]
        return {
            "rows": rows,
            "total_debit": total_debit,
            "total_credit": total_credit,
            "balanced": abs(total_debit - total_credit) <= 0.01,
            "receivable_debit": round(
                sum(float(row.get("debit") or 0) for row in receivable), 2
            ),
            "net_income_credit": round(
                sum(
                    float(row.get("credit") or 0) - float(row.get("debit") or 0)
                    for row in non_receivable
                ),
                2,
            ),
        }

    def _account_credit(self, gl: dict[str, Any], account: str | None) -> float:
        if not account:
            return 0.0
        return round(
            sum(
                float(row.get("credit") or 0) - float(row.get("debit") or 0)
                for row in gl.get("rows") or []
                if str(row.get("account") or "") == str(account)
            ),
            2,
        )

    def _assert_ledger(self, case: dict[str, Any], prefix: str, invoice_doc: Any) -> dict[str, Any]:
        """Balanced books, and the header arithmetic that produced grand_total."""
        invoice_name = str(getattr(invoice_doc, "name", "") or "")
        gl = self._gl_summary(invoice_name)
        net_total = float(getattr(invoice_doc, "net_total", 0) or 0)
        taxes = float(getattr(invoice_doc, "total_taxes_and_charges", 0) or 0)
        discount = float(getattr(invoice_doc, "discount_amount", 0) or 0)
        grand_total = float(getattr(invoice_doc, "grand_total", 0) or 0)
        expected_grand_total = round(net_total + taxes - discount, 2)

        if int(getattr(invoice_doc, "docstatus", 0) or 0) != 1:
            self._skip(
                case, f"{prefix}.LEDGER",
                "Ledger assertions on a submitted invoice",
                f"Invoice {invoice_name} is docstatus="
                f"{getattr(invoice_doc, 'docstatus', None)}; a draft posts no GL entries.",
                actual={"invoice": invoice_name},
            )
            return gl

        self._check(
            case, f"{prefix}.LEDGER.01",
            "GL entries balance (debits == credits)",
            gl["balanced"],
            expected={"total_debit": gl["total_credit"]},
            actual={"total_debit": gl["total_debit"], "total_credit": gl["total_credit"]},
        )
        self._check(
            case, f"{prefix}.LEDGER.02",
            "grand_total == net_total + total_taxes_and_charges - discount_amount",
            abs(round(grand_total, 2) - expected_grand_total) <= 0.01,
            expected=expected_grand_total,
            actual={
                "grand_total": round(grand_total, 2),
                "net_total": round(net_total, 2),
                "total_taxes_and_charges": round(taxes, 2),
                "discount_amount": round(discount, 2),
            },
        )
        self._check(
            case, f"{prefix}.LEDGER.03",
            "Receivable debit equals grand_total",
            abs(gl["receivable_debit"] - round(grand_total, 2)) <= 0.01,
            expected=round(grand_total, 2),
            actual=gl["receivable_debit"],
        )
        self._check(
            case, f"{prefix}.LEDGER.04",
            "Net non-receivable credit equals grand_total (income booked net of discount)",
            abs(gl["net_income_credit"] - round(grand_total, 2)) <= 0.01,
            expected=round(grand_total, 2),
            actual=gl["net_income_credit"],
        )
        return gl

    def _woo_merchandise_total(self, order: dict[str, Any]) -> float:
        """Woo's own merchandise total: order total less shipping, less fees."""
        try:
            total = float(order.get("total") or 0)
        except (TypeError, ValueError):
            return 0.0
        shipping = 0.0
        for line in order.get("shipping_lines") or []:
            try:
                shipping += float((line or {}).get("total") or 0)
            except (TypeError, ValueError):
                continue
        fees = 0.0
        for line in order.get("fee_lines") or []:
            try:
                fees += float((line or {}).get("total") or 0)
            except (TypeError, ValueError):
                continue
        return round(total - shipping - fees, 2)

    def _prices_agree(self, order: dict[str, Any], invoice_doc: Any) -> tuple[bool, dict[str, Any]]:
        """Do the store's merchandise prices match the ERPNext price list?

        Inbound deliberately ignores Woo prices and rebuilds every line from the
        ERPNext price list, so on a store whose catalogue prices differ, any
        ``order.total`` vs ``grand_total`` comparison measures the catalogue gap
        rather than the thing under test. Where that is the case the dependent
        check is skipped with this as the reason — never quietly passed.
        """
        woo_merchandise = self._woo_merchandise_total(order)
        erp_merchandise = round(
            float(getattr(invoice_doc, "net_total", 0) or 0)
            + float(getattr(invoice_doc, "discount_amount", 0) or 0),
            2,
        )
        evidence = {
            "woo_merchandise_total": woo_merchandise,
            "erp_net_total_plus_discount": erp_merchandise,
            "difference": round(woo_merchandise - erp_merchandise, 2),
        }
        return abs(woo_merchandise - erp_merchandise) <= 0.02, evidence

    # ------------------------------------------------------------------
    # W1 - Woo shipping amount is never billed
    # ------------------------------------------------------------------

    def _w1_shipping_amount(self, case: dict[str, Any]) -> dict[str, Any]:
        """``_resolve_delivery_charge_policy`` (order_sync.py:2501) bills the
        Territory rate. ``shipping_total`` is read by exactly one function,
        ``_woo_order_has_free_shipping`` (:2113), and only as a boolean.
        """
        territory = self._delivery_territory_fixture()
        if not territory:
            self._skip(
                case, "W1.00",
                "A Territory with delivery_income > 0 and a POS Profile exists",
                "No non-group Territory on this site has both a POS Profile and "
                "delivery_income > 0, so there is no configuration in which shipping income "
                "could be billed at all.",
                actual=self.capabilities.get("delivery_territory"),
            )
            return {"skipped": True}

        delivery_income = round(float(territory.get("delivery_income") or 0), 2)
        woo_shipping_total = round(delivery_income + 7.00, 2)
        items = self._order_fixture_items(str(territory.get("price_list") or ""))[:1]

        run = self._create_parity_order(
            note="W1 shipping-amount order",
            item_rows=[{**dict(items[0]), "qty": 1}],
            territory_fixture=territory,
            overrides={
                "shipping_lines": [
                    {
                        "method_id": "flat_rate",
                        "method_title": "Delivery",
                        "total": f"{woo_shipping_total:.2f}",
                    }
                ]
            },
        )
        woo_order_id = run["woo_order_id"]
        pull = self._pull_created_order(woo_order_id)
        if self._pull_was_locked(pull):
            self._skip(
                case, "W1.01", "Inbound sync created an invoice",
                self._lock_skip_reason(pull), actual=pull,
            )
            return {"pull": pull, "woo_order_id": woo_order_id}

        invoice = self._invoice_for_order(woo_order_id)
        order = self._woo_order(woo_order_id) or run["order"]
        self._check(
            case, "W1.01",
            "Inbound sync created an invoice for the order",
            invoice is not None,
            expected="one active Sales Invoice",
            actual=pull,
        )
        if invoice is None:
            return {"pull": pull, "order": order}

        self._record_created("Sales Invoice", invoice.name, note="W1 inbound invoice")
        shipping_row = self._shipping_income_row(invoice)
        billed = float((shipping_row or {}).get("tax_amount") or 0)
        order_shipping = round(float(order.get("shipping_total") or 0), 2)

        self._check(
            case, "W1.02",
            "The Woo order really carries the shipping amount we posted",
            abs(order_shipping - woo_shipping_total) <= 0.01,
            expected=woo_shipping_total,
            actual=order_shipping,
        )
        self._check(
            case, "W1.03",
            "Shipping Income is billed at the Territory rate, NOT the Woo shipping amount",
            abs(billed - delivery_income) <= 0.01,
            expected=delivery_income,
            actual={"shipping_income_row": shipping_row, "woo_shipping_total": order_shipping},
            gap=(
                "_resolve_delivery_charge_policy (order_sync.py:2501) reads "
                "Territory.delivery_income. The customer paid the Woo shipping amount; ERPNext "
                "bills the territory rate. The difference is never recorded anywhere."
            ),
        )
        self._check(
            case, "W1.04",
            "Customer-facing shipping delta is exactly the un-billed remainder",
            abs(round(order_shipping - billed, 2) - 7.00) <= 0.01,
            expected=7.00,
            actual={"woo_shipping_total": order_shipping, "billed_shipping_income": billed},
            gap="Quantifies the un-billed shipping: the store collected 7.00 more than ERPNext recorded.",
        )

        prices_agree, price_evidence = self._prices_agree(order, invoice)
        grand_total = round(float(getattr(invoice, "grand_total", 0) or 0), 2)
        order_total = round(float(order.get("total") or 0), 2)
        if prices_agree:
            self._check(
                case, "W1.05",
                "order.total - grand_total equals the un-billed shipping (7.00)",
                abs(round(order_total - grand_total, 2) - 7.00) <= 0.02,
                expected=7.00,
                actual={"order_total": order_total, "grand_total": grand_total},
                gap="The whole customer-facing gap on this order is the shipping the invoice never billed.",
            )
        else:
            self._skip(
                case, "W1.05",
                "order.total - grand_total equals the un-billed shipping (7.00)",
                "The demo store's catalogue prices differ from the ERPNext price list, and inbound "
                "rebuilds every line from the price list, so the order/invoice total gap is not "
                "attributable to shipping alone on this store.",
                actual={
                    "order_total": order_total,
                    "grand_total": grand_total,
                    "price_parity": price_evidence,
                },
            )

        gl = self._assert_ledger(case, "W1", invoice)
        if shipping_row and int(getattr(invoice, "docstatus", 0) or 0) == 1:
            account_credit = self._account_credit(gl, shipping_row.get("account_head"))
            self._check(
                case, "W1.06",
                "The shipping income account is credited exactly the tax row amount",
                abs(account_credit - billed) <= 0.01,
                expected=billed,
                actual={"account": shipping_row.get("account_head"), "net_credit": account_credit},
            )

        return {
            "woo_order_id": woo_order_id,
            "territory": territory,
            "pull": pull,
            "invoice": invoice.name,
            "shipping_row": shipping_row,
            "order_shipping_total": order_shipping,
            "price_parity": price_evidence,
            "gl": gl,
        }

    # ------------------------------------------------------------------
    # W2 - Partial refund is structurally invisible
    # ------------------------------------------------------------------

    def _w2_partial_refund(self, case: dict[str, Any]) -> dict[str, Any]:
        """``_compute_order_hash`` (order_sync.py:1073) hashes id, total, currency,
        shipping_total and per-line product_id/variation_id/quantity/total/subtotal.
        A partial refund changes none of them, and ``refunds[]`` is never read.
        """
        territory = self._delivery_territory_fixture() or self._primary_territory_fixture()
        items = self._order_fixture_items(str(territory.get("price_list") or ""))[:1]
        run = self._create_parity_order(
            note="W2 partial-refund order",
            item_rows=[{**dict(items[0]), "qty": 2}],
            territory_fixture=territory,
        )
        woo_order_id = run["woo_order_id"]
        pull = self._pull_created_order(woo_order_id)
        if self._pull_was_locked(pull):
            self._skip(
                case, "W2.01", "Inbound sync created an invoice",
                self._lock_skip_reason(pull), actual=pull,
            )
            return {"pull": pull}

        invoice = self._invoice_for_order(woo_order_id)
        self._check(
            case, "W2.01",
            "Inbound sync created an invoice for the order",
            invoice is not None,
            expected="one active Sales Invoice",
            actual=pull,
        )
        if invoice is None:
            return {"pull": pull}
        self._record_created("Sales Invoice", invoice.name, note="W2 inbound invoice")

        order_before = self._woo_order(woo_order_id) or run["order"]
        map_before = self._order_map_full(woo_order_id) or {}
        outstanding_before = round(float(getattr(invoice, "outstanding_amount", 0) or 0), 2)
        grand_total = round(float(getattr(invoice, "grand_total", 0) or 0), 2)
        gl_before = self._gl_summary(invoice.name)

        order_total = float(order_before.get("total") or 0)
        refund_amount = round(max(1.0, min(order_total * 0.25, order_total - 0.01)), 2)
        if order_total <= 1.0:
            self._skip(
                case, "W2.02",
                "A partial refund can be filed against the order",
                f"Order total {order_total} leaves no room for a partial refund.",
                actual=order_before.get("total"),
            )
            return {"woo_order_id": woo_order_id, "invoice": invoice.name}

        self._require_self_created("Woo Order", woo_order_id)
        try:
            refund = self._woo_client().post(
                f"orders/{woo_order_id}/refunds",
                {
                    "amount": f"{refund_amount:.2f}",
                    "reason": f"{self.run_id} parity partial refund",
                    # Never call the payment gateway from a harness.
                    "api_refund": False,
                },
            )
        except Exception as exc:  # noqa: BLE001
            self._skip(
                case, "W2.02",
                "A partial refund can be filed against the order",
                f"The store rejected POST orders/{{id}}/refunds: {exc}. The demo store's payment "
                "and refund plugin configuration differs from production.",
                actual=str(exc),
            )
            return {"woo_order_id": woo_order_id, "invoice": invoice.name}

        order_after = self._woo_order(woo_order_id) or {}
        order_sync = _woo_module(_ORDER_SYNC_MODULE)
        hash_before = order_sync._compute_order_hash(order_before)
        hash_after = order_sync._compute_order_hash(order_after)

        self._check(
            case, "W2.02",
            "The store recorded the partial refund",
            bool(order_after.get("refunds")),
            expected="non-empty refunds[]",
            actual={
                "refunds": order_after.get("refunds"),
                "refund_response_id": (refund or {}).get("id"),
            },
        )
        self._check(
            case, "W2.03",
            "The change hash is IDENTICAL before and after the refund",
            hash_before == hash_after,
            expected=hash_before,
            actual=hash_after,
            gap=(
                "_compute_order_hash (order_sync.py:1073) hashes id/total/currency/shipping_total "
                "and per-line product_id/variation_id/quantity/total/subtotal. A partial refund "
                "moves none of them, so the refund is structurally undetectable and refunds[] is "
                "never read."
            ),
        )

        pull_after = self._pull_created_order(woo_order_id)
        if self._pull_was_locked(pull_after):
            self._skip(
                case, "W2.04", "Re-pull reports the order unchanged",
                self._lock_skip_reason(pull_after), actual=pull_after,
            )
        else:
            reason = str(pull_after.get("reason") or "").strip().lower()
            self._check(
                case, "W2.04",
                "Re-pull after the refund is skipped as unchanged/frozen",
                str(pull_after.get("status") or "").lower() == "skipped"
                and reason in {"unchanged", "submitted_frozen", "already_mapped"},
                expected={"status": "skipped", "reason": "unchanged | submitted_frozen | already_mapped"},
                actual=pull_after,
                gap="Inbound sees nothing to do, so the refund never reaches ERPNext at all.",
            )

        invoice_after = frappe.get_doc("Sales Invoice", invoice.name)
        outstanding_after = round(float(getattr(invoice_after, "outstanding_amount", 0) or 0), 2)
        credit_notes = frappe.get_all(
            "Sales Invoice",
            filters={"return_against": invoice.name, "docstatus": ["<", 2]},
            fields=["name", "grand_total", "docstatus"],
            limit_page_length=10,
        )

        self._check(
            case, "W2.05",
            "No credit note exists for the refunded amount",
            not credit_notes,
            expected=[],
            actual=credit_notes,
            gap="A Woo partial refund produces no ERPNext return document of any kind.",
        )
        self._check(
            case, "W2.06",
            "outstanding_amount is unchanged by the refund",
            abs(outstanding_after - outstanding_before) <= 0.01,
            expected=outstanding_before,
            actual=outstanding_after,
            gap=(
                f"AR is overstated by exactly the refunded amount ({refund_amount:.2f}) on this "
                "order: the customer was refunded on the store and still owes the full invoice in "
                "ERPNext."
            ),
        )
        gl_after = self._gl_summary(invoice.name)
        self._check(
            case, "W2.07",
            "The ledger is untouched by the refund (receivable debit unchanged)",
            abs(gl_after["receivable_debit"] - gl_before["receivable_debit"]) <= 0.01,
            expected=gl_before["receivable_debit"],
            actual=gl_after["receivable_debit"],
            gap="Quantified AR overstatement, at the ledger rather than the document level.",
        )

        return {
            "woo_order_id": woo_order_id,
            "invoice": invoice.name,
            "refund_amount": refund_amount,
            "ar_overstatement": refund_amount,
            "grand_total": grand_total,
            "hash_before": hash_before,
            "hash_after": hash_after,
            "map_before": map_before,
            "map_after": self._order_map_full(woo_order_id),
            "pull_after": pull_after,
            "gl_before": gl_before,
            "gl_after": gl_after,
        }

    # ------------------------------------------------------------------
    # W3 - Coupon passthrough
    # ------------------------------------------------------------------

    def _w3_coupon_passthrough(self, case: dict[str, Any]) -> dict[str, Any]:
        if not (self.capabilities.get("coupons_api") or {}).get("available"):
            self._skip(
                case, "W3.00",
                "The store exposes the coupons API",
                "GET coupons failed on this store: "
                f"{(self.capabilities.get('coupons_api') or {}).get('detail')}",
                actual=self.capabilities.get("coupons_api"),
            )
            return {"skipped": True}

        territory = self._delivery_territory_fixture() or self._primary_territory_fixture()
        items = self._order_fixture_items(str(territory.get("price_list") or ""))[:1]
        coupon_code = f"parity-{re.sub(r'[^a-z0-9]+', '', self.run_id.lower())}"
        coupon_amount = 10.00
        try:
            coupon = self._woo_client().post(
                "coupons",
                {
                    "code": coupon_code,
                    "discount_type": "fixed_cart",
                    "amount": f"{coupon_amount:.2f}",
                    "individual_use": True,
                    "usage_limit": 5,
                    "description": f"{self.run_id} parity coupon",
                },
            )
        except Exception as exc:  # noqa: BLE001
            self._skip(
                case, "W3.00",
                "A real Woo coupon can be created on this store",
                f"POST coupons failed: {exc}",
                actual=str(exc),
            )
            return {"skipped": True}

        coupon_id = str((coupon or {}).get("id") or "")
        if coupon_id:
            self._record_created("Woo Coupon", coupon_id, note=f"W3 coupon {coupon_code}")

        run = self._create_parity_order(
            note="W3 coupon order",
            item_rows=[{**dict(items[0]), "qty": 2}],
            territory_fixture=territory,
            overrides={"coupon_lines": [{"code": coupon_code}]},
        )
        woo_order_id = run["woo_order_id"]
        order = self._woo_order(woo_order_id) or run["order"]
        discount_total = round(float(order.get("discount_total") or 0), 2)

        self._check(
            case, "W3.01",
            "The store applied the coupon to the order",
            discount_total > 0 and bool(order.get("coupon_lines")),
            expected="discount_total > 0 with a coupon line",
            actual={"discount_total": discount_total, "coupon_lines": order.get("coupon_lines")},
        )

        pull = self._pull_created_order(woo_order_id)
        if self._pull_was_locked(pull):
            self._skip(
                case, "W3.02", "Inbound sync created an invoice",
                self._lock_skip_reason(pull), actual=pull,
            )
            return {"pull": pull, "coupon": coupon_code}

        invoice = self._invoice_for_order(woo_order_id)
        self._check(
            case, "W3.02",
            "Inbound sync created an invoice for the coupon order",
            invoice is not None,
            expected="one active Sales Invoice",
            actual=pull,
        )
        if invoice is None:
            return {"pull": pull, "coupon": coupon_code}
        self._record_created("Sales Invoice", invoice.name, note="W3 inbound invoice")

        raw_codes = getattr(invoice, "custom_promo_codes", None)
        try:
            parsed_codes = json.loads(raw_codes) if raw_codes else []
        except Exception:  # noqa: BLE001
            parsed_codes = [str(raw_codes)]
        if not isinstance(parsed_codes, list):
            parsed_codes = [parsed_codes]
        passthrough_total = round(float(getattr(invoice, "custom_promo_woo_discount_total", 0) or 0), 2)
        order_map = self._order_map_full(woo_order_id) or {}

        self._check(
            case, "W3.03",
            "custom_promo_codes carries the coupon code",
            any(str(code).strip().lower() == coupon_code for code in parsed_codes),
            expected=[coupon_code],
            actual={"raw": raw_codes, "parsed": parsed_codes},
        )
        self._check(
            case, "W3.04",
            "custom_promo_woo_discount_total == order.discount_total",
            abs(passthrough_total - discount_total) <= 0.01,
            expected=discount_total,
            actual=passthrough_total,
        )
        self._check(
            case, "W3.05",
            "Order Map promo_mismatch is 0",
            int(order_map.get("promo_mismatch") or 0) == 0,
            expected=0,
            actual={
                "promo_mismatch": order_map.get("promo_mismatch"),
                "promo_mismatch_note": order_map.get("promo_mismatch_note"),
                "invoice_mismatch": getattr(invoice, "custom_promo_discount_mismatch", None),
            },
        )

        gl = self._assert_ledger(case, "W3", invoice)
        prices_agree, price_evidence = self._prices_agree(order, invoice)
        grand_total = round(float(getattr(invoice, "grand_total", 0) or 0), 2)
        order_total = round(float(order.get("total") or 0), 2)
        if prices_agree:
            self._check(
                case, "W3.06",
                "grand_total == order.total",
                abs(grand_total - order_total) <= 0.02,
                expected=order_total,
                actual=grand_total,
            )
        else:
            self._skip(
                case, "W3.06",
                "grand_total == order.total",
                "The demo store's catalogue prices differ from the ERPNext price list, and inbound "
                "rebuilds every line from the price list, so the two totals cannot agree for reasons "
                "that have nothing to do with the coupon.",
                actual={
                    "grand_total": grand_total,
                    "order_total": order_total,
                    "price_parity": price_evidence,
                },
            )

        if int(getattr(invoice, "docstatus", 0) or 0) == 1:
            self._check(
                case, "W3.07",
                "Income is credited net of the coupon discount",
                abs(gl["net_income_credit"] - grand_total) <= 0.01,
                expected=grand_total,
                actual={
                    "net_income_credit": gl["net_income_credit"],
                    "discount_amount": float(getattr(invoice, "discount_amount", 0) or 0),
                },
            )

        return {
            "coupon_code": coupon_code,
            "coupon_id": coupon_id,
            "woo_order_id": woo_order_id,
            "invoice": invoice.name,
            "discount_total": discount_total,
            "order_map": order_map,
            "price_parity": price_evidence,
            "gl": gl,
        }

    # ------------------------------------------------------------------
    # W4 - Non-coupon / dynamic discount
    # ------------------------------------------------------------------

    def _w4_noncoupon_discount(self, case: dict[str, Any]) -> dict[str, Any]:
        """Line total below line subtotal with NO coupon lines.

        ``_apply_noncoupon_woo_discount`` maps it onto the header discount, and
        the draft re-sync path resets it first (order_sync.py:4412) so a discount
        removed on the store is actually cleared.
        """
        territory = self._delivery_territory_fixture() or self._primary_territory_fixture()
        items = self._order_fixture_items(str(territory.get("price_list") or ""))[:1]
        product_id = int(items[0].get("woo_product_id") or 0)
        variation_id = int(items[0].get("woo_variation_id") or 0)
        subtotal = 100.00
        discounted = 90.00

        line: dict[str, Any] = {
            "product_id": product_id,
            "quantity": 1,
            "subtotal": f"{subtotal:.2f}",
            "total": f"{discounted:.2f}",
        }
        if variation_id > 0:
            line["variation_id"] = variation_id

        # "pending" maps to docstatus 0 (order_sync.py:407-433), which is what the
        # draft half of this case needs. It is flipped to "processing" below.
        run = self._create_parity_order(
            note="W4 dynamic-discount order",
            item_rows=[{**dict(items[0]), "qty": 1}],
            territory_fixture=territory,
            status="pending",
            overrides={"line_items": [line], "coupon_lines": []},
        )
        woo_order_id = run["woo_order_id"]
        order = self._woo_order(woo_order_id) or run["order"]
        woo_discount_total = round(float(order.get("discount_total") or 0), 2)

        self._check(
            case, "W4.01",
            "The store recorded a coupon-less discount (line total < subtotal)",
            woo_discount_total > 0 and not (order.get("coupon_lines") or []),
            expected={"discount_total": "> 0", "coupon_lines": []},
            actual={
                "discount_total": woo_discount_total,
                "coupon_lines": order.get("coupon_lines"),
                "line_items": [
                    {"subtotal": row.get("subtotal"), "total": row.get("total")}
                    for row in order.get("line_items") or []
                ],
            },
        )
        if woo_discount_total <= 0:
            self._skip(
                case, "W4.02",
                "The dynamic discount reaches ERPNext",
                "This store did not honour an explicit line subtotal/total split over REST, so no "
                "coupon-less discount exists to carry inbound.",
                actual=order.get("line_items"),
            )
            return {"woo_order_id": woo_order_id}

        pull = self._pull_created_order(woo_order_id)
        if self._pull_was_locked(pull):
            self._skip(
                case, "W4.02", "Inbound sync created an invoice",
                self._lock_skip_reason(pull), actual=pull,
            )
            return {"pull": pull}

        invoice = self._invoice_for_order(woo_order_id)
        self._check(
            case, "W4.02",
            "Inbound sync created an invoice for the discounted order",
            invoice is not None,
            expected="one active Sales Invoice",
            actual=pull,
        )
        if invoice is None:
            return {"pull": pull}
        self._record_created("Sales Invoice", invoice.name, note="W4 inbound invoice")

        self._check(
            case, "W4.03",
            "apply_discount_on is 'Grand Total'",
            str(getattr(invoice, "apply_discount_on", "") or "") == "Grand Total",
            expected="Grand Total",
            actual=getattr(invoice, "apply_discount_on", None),
        )
        self._check(
            case, "W4.04",
            "discount_amount equals the Woo discount",
            abs(round(float(getattr(invoice, "discount_amount", 0) or 0), 2) - woo_discount_total) <= 0.01,
            expected=woo_discount_total,
            actual=float(getattr(invoice, "discount_amount", 0) or 0),
        )
        self._check(
            case, "W4.05",
            "The invoice is a draft, so the reset path is reachable",
            int(getattr(invoice, "docstatus", 0) or 0) == 0,
            expected=0,
            actual=getattr(invoice, "docstatus", None),
            note="Woo status 'pending' maps to docstatus 0 (order_sync.py:433).",
        )

        # --- draft: remove the discount on the store, expect a reset ---
        current_lines = [dict(row) for row in (order.get("line_items") or [])]
        line_id = current_lines[0].get("id") if current_lines else None
        draft_reset: dict[str, Any] = {}
        if not line_id:
            self._skip(
                case, "W4.06",
                "Removing the discount on a DRAFT invoice resets discount_amount to 0",
                "The store did not return a line item id, so the discount cannot be removed by PUT.",
                actual=current_lines,
            )
        else:
            self._put_created_order(
                woo_order_id,
                {"line_items": [{
                    "id": line_id,
                    "subtotal": f"{subtotal:.2f}",
                    "total": f"{subtotal:.2f}",
                }]},
            )
            order_reset = self._woo_order(woo_order_id) or {}
            pull_reset = self._pull_created_order(woo_order_id)
            if self._pull_was_locked(pull_reset):
                self._skip(
                    case, "W4.06",
                    "Removing the discount on a DRAFT invoice resets discount_amount to 0",
                    self._lock_skip_reason(pull_reset), actual=pull_reset,
                )
            else:
                invoice_reset = self._invoice_for_order(woo_order_id)
                reset_discount = (
                    round(float(getattr(invoice_reset, "discount_amount", 0) or 0), 2)
                    if invoice_reset else None
                )
                draft_reset = {
                    "woo_discount_total": order_reset.get("discount_total"),
                    "pull": pull_reset,
                    "discount_amount": reset_discount,
                }
                self._check(
                    case, "W4.06",
                    "Removing the discount on a DRAFT invoice resets discount_amount to 0",
                    reset_discount == 0,
                    expected=0,
                    actual=draft_reset,
                    note="order_sync.py:4412 clears the header discount before re-applying.",
                )

        # --- submitted: the same edit must NOT move anything ---
        self._put_created_order(woo_order_id, {"status": "processing"})
        pull_submit = self._pull_created_order(woo_order_id)
        if self._pull_was_locked(pull_submit):
            self._skip(
                case, "W4.07",
                "The same discount edit on a SUBMITTED invoice changes nothing",
                self._lock_skip_reason(pull_submit), actual=pull_submit,
            )
            return {
                "woo_order_id": woo_order_id,
                "invoice": invoice.name,
                "draft_reset": draft_reset,
            }

        invoice_submitted = self._invoice_for_order(woo_order_id)
        if invoice_submitted is None or int(getattr(invoice_submitted, "docstatus", 0) or 0) != 1:
            self._skip(
                case, "W4.07",
                "The same discount edit on a SUBMITTED invoice changes nothing",
                "The invoice did not reach docstatus 1 after the status change, so the freeze cannot "
                "be exercised.",
                actual={
                    "pull": pull_submit,
                    "docstatus": getattr(invoice_submitted, "docstatus", None),
                },
            )
            return {
                "woo_order_id": woo_order_id,
                "invoice": invoice.name,
                "draft_reset": draft_reset,
            }

        discount_before = round(float(getattr(invoice_submitted, "discount_amount", 0) or 0), 2)
        grand_total_before = round(float(getattr(invoice_submitted, "grand_total", 0) or 0), 2)
        if line_id:
            self._put_created_order(
                woo_order_id,
                {"line_items": [{
                    "id": line_id,
                    "subtotal": f"{subtotal:.2f}",
                    "total": f"{discounted:.2f}",
                }]},
            )
        pull_frozen = self._pull_created_order(woo_order_id)
        invoice_frozen = frappe.get_doc("Sales Invoice", invoice_submitted.name)
        discount_after = round(float(getattr(invoice_frozen, "discount_amount", 0) or 0), 2)
        grand_total_after = round(float(getattr(invoice_frozen, "grand_total", 0) or 0), 2)
        submitted_evidence = {
            "pull": pull_frozen,
            "discount_before": discount_before,
            "discount_after": discount_after,
            "grand_total_before": grand_total_before,
            "grand_total_after": grand_total_after,
        }

        self._check(
            case, "W4.07",
            "The same discount edit on a SUBMITTED invoice changes nothing",
            discount_after == discount_before and grand_total_after == grand_total_before,
            expected={"discount_amount": discount_before, "grand_total": grand_total_before},
            actual=submitted_evidence,
            note=(
                "PROD-WOO-001: a submitted invoice is intentionally frozen. This is documented "
                "architecture, not a defect."
            ),
        )
        self._assert_ledger(case, "W4", invoice_frozen)

        return {
            "woo_order_id": woo_order_id,
            "invoice": invoice.name,
            "woo_discount_total": woo_discount_total,
            "draft_reset": draft_reset,
            "submitted": submitted_evidence,
        }

    # ------------------------------------------------------------------
    # W5 - Positive fee lines are dropped
    # ------------------------------------------------------------------

    def _w5_positive_fee_line(self, case: dict[str, Any]) -> dict[str, Any]:
        """``_woo_noncoupon_discount_total`` (order_sync.py:2743) walks
        ``fee_lines`` but only accumulates ``amount < 0`` (the guard at :2766).
        A positive fee — a service charge, a surcharge — is silently dropped.
        """
        territory = self._delivery_territory_fixture() or self._primary_territory_fixture()
        items = self._order_fixture_items(str(territory.get("price_list") or ""))[:1]
        fee_amount = 15.00
        fee_name = f"{self.run_id} Parity Service Fee"

        run = self._create_parity_order(
            note="W5 positive-fee order",
            item_rows=[{**dict(items[0]), "qty": 1}],
            territory_fixture=territory,
            overrides={
                "fee_lines": [
                    {"name": fee_name, "total": f"{fee_amount:.2f}", "tax_status": "none"}
                ]
            },
        )
        woo_order_id = run["woo_order_id"]
        order = self._woo_order(woo_order_id) or run["order"]
        store_fees = [
            {"name": row.get("name"), "total": row.get("total")}
            for row in order.get("fee_lines") or []
        ]
        posted_fee = round(
            sum(float((row or {}).get("total") or 0) for row in order.get("fee_lines") or []), 2
        )

        self._check(
            case, "W5.01",
            "The store recorded the positive fee line",
            abs(posted_fee - fee_amount) <= 0.01,
            expected=fee_amount,
            actual=store_fees,
        )
        if abs(posted_fee - fee_amount) > 0.01:
            self._skip(
                case, "W5.02",
                "The positive fee is measured against the invoice",
                "The store did not accept a positive fee_lines entry over REST, so there is nothing "
                "to measure. The demo store's fee handling differs from production.",
                actual=store_fees,
            )
            return {"woo_order_id": woo_order_id}

        pull = self._pull_created_order(woo_order_id)
        if self._pull_was_locked(pull):
            self._skip(
                case, "W5.02", "Inbound sync created an invoice",
                self._lock_skip_reason(pull), actual=pull,
            )
            return {"pull": pull}

        invoice = self._invoice_for_order(woo_order_id)
        self._check(
            case, "W5.02",
            "Inbound sync created an invoice for the fee-bearing order",
            invoice is not None,
            expected="one active Sales Invoice",
            actual=pull,
        )
        if invoice is None:
            return {"pull": pull}
        self._record_created("Sales Invoice", invoice.name, note="W5 inbound invoice")

        order_sync = _woo_module(_ORDER_SYNC_MODULE)
        computed = order_sync._woo_noncoupon_discount_total(order)
        fee_rows = [
            {
                "description": getattr(row, "description", None),
                "tax_amount": getattr(row, "tax_amount", None),
            }
            for row in getattr(invoice, "taxes", []) or []
            if fee_name.lower() in str(getattr(row, "description", "") or "").lower()
        ]
        fee_items = [
            {"item_code": getattr(row, "item_code", None), "amount": getattr(row, "amount", None)}
            for row in getattr(invoice, "items", []) or []
            if fee_name.lower() in str(getattr(row, "item_name", "") or "").lower()
        ]

        self._check(
            case, "W5.03",
            "_woo_noncoupon_discount_total ignores the positive fee entirely",
            abs(computed - round(float(order.get("discount_total") or 0), 2)) <= 0.01,
            expected=round(float(order.get("discount_total") or 0), 2),
            actual=computed,
            gap=(
                "order_sync.py:2743 sums fee_lines only where amount < 0 (guard at :2766), so a "
                "positive fee contributes nothing in either direction."
            ),
        )
        self._check(
            case, "W5.04",
            "No invoice row carries the positive fee",
            not fee_rows and not fee_items,
            expected={"tax_rows": [], "item_rows": []},
            actual={"tax_rows": fee_rows, "item_rows": fee_items},
            gap=f"The store charged {fee_amount:.2f} that ERPNext never billed: pure under-billing.",
        )

        grand_total = round(float(getattr(invoice, "grand_total", 0) or 0), 2)
        order_total = round(float(order.get("total") or 0), 2)
        prices_agree, price_evidence = self._prices_agree(order, invoice)
        if prices_agree:
            self._check(
                case, "W5.05",
                "order.total - grand_total equals the dropped fee",
                abs(round(order_total - grand_total, 2) - fee_amount) <= 0.02,
                expected=fee_amount,
                actual={"order_total": order_total, "grand_total": grand_total},
                gap=f"Quantified under-billing on this order: {fee_amount:.2f}.",
            )
        else:
            self._skip(
                case, "W5.05",
                "order.total - grand_total equals the dropped fee",
                "The demo store's catalogue prices differ from the ERPNext price list, so the total "
                "gap is not attributable to the fee alone.",
                actual={
                    "order_total": order_total,
                    "grand_total": grand_total,
                    "price_parity": price_evidence,
                },
            )

        gl = self._assert_ledger(case, "W5", invoice)
        return {
            "woo_order_id": woo_order_id,
            "invoice": invoice.name,
            "fee_amount": fee_amount,
            "under_billing": fee_amount,
            "computed_noncoupon_discount": computed,
            "price_parity": price_evidence,
            "gl": gl,
        }

    # ------------------------------------------------------------------
    # W6 - Delivery slot formats
    # ------------------------------------------------------------------

    #: ``(label, date_meta, slot_meta, expected_date, expected_time_from, expected_minutes, note)``
    SLOT_TABLE: tuple[tuple[str, str, str, str | None, str | None, int | None, str], ...] = (
        (
            "production date shape + 24h slot",
            "5 August, 2026", "19:00 - 20:30",
            "2026-08-05", "19:00:00", 90,
            "The shape that carries nearly every historical order.",
        ),
        (
            "weekday-labelled date + 12h slot",
            "Sunday, August 02, 2026", "01:00 PM - 02:30 PM",
            "2026-08-02", "13:00:00", 90,
            "The 12-hour slot used to be read as 01:00 — twelve hours early. "
            "_extract_meridiem_slot (order_sync.py:1749) now runs BEFORE the loose fallback, so the "
            "correct reading is 13:00.",
        ),
        (
            "ISO date + one-sided meridiem",
            "2026-08-02", "11:00 - 1:00 PM",
            "2026-08-02", "11:00:00", 120,
            "Only the end carries a marker; the start borrows it and flips when that would run the "
            "window backwards (order_sync.py:1786-1793).",
        ),
        (
            "midnight slot",
            "2026-08-02", "00:00-01:00",
            "2026-08-02", "00:00:00", 60,
            "Midnight has to survive: a falsy-zero bug dropped this slot outbound once already.",
        ),
        (
            "unparsable slot",
            "2026-08-02", "sometime after lunch",
            "2026-08-02", None, None,
            "The parser returns the date but no time; the CALLER then drops all three "
            "(order_sync.py:4066-4069).",
        ),
    )

    def _w6_delivery_slot_formats(self, case: dict[str, Any]) -> dict[str, Any]:
        order_sync = _woo_module(_ORDER_SYNC_MODULE)
        orddd = self.capabilities.get("orddd") or {}
        results: list[dict[str, Any]] = []

        for index, entry in enumerate(self.SLOT_TABLE, start=1):
            label, date_meta, slot_meta, want_date, want_time, want_minutes, note = entry
            payload = {
                "id": 0,
                "meta_data": [
                    {"key": "Delivery Date", "value": date_meta},
                    {"key": "Time Slot", "value": slot_meta},
                ],
            }
            got_date, got_time, got_minutes = order_sync._parse_delivery_parts(payload)
            row = {
                "label": label,
                "date_meta": date_meta,
                "slot_meta": slot_meta,
                "parsed": {
                    "date": got_date,
                    "time_from": got_time,
                    "duration_minutes": got_minutes,
                },
                "expected": {
                    "date": want_date,
                    "time_from": want_time,
                    "duration_minutes": want_minutes,
                },
            }
            results.append(row)
            self._check(
                case, f"W6.{index:02d}",
                f"Slot parse — {label}",
                got_date == want_date and got_time == want_time and got_minutes == want_minutes,
                expected=row["expected"],
                actual=row["parsed"],
                note=note,
            )
            if index == 2:
                self._check(
                    case, "W6.02b",
                    "12-hour slots are read in 24-hour terms (the AM/PM defect is fixed)",
                    got_time == "13:00:00",
                    expected="13:00:00",
                    actual=got_time,
                    note=(
                        "Historical note: this used to yield 01:00:00. Invoice ACC-SINV-2026-17139 "
                        "still holds 02:30 AM for a 2:30 PM order and was never backfilled."
                    ),
                )

        # Caller-level behaviour, asserted against real invoices rather than a
        # re-implementation of the all-or-nothing rule.
        if not self.allow_staging_mutations:
            self._skip(
                case, "W6.10",
                "custom_delivery_duration is stored in SECONDS on a real invoice",
                "allow_staging_mutations is false; this needs a real inbound order.",
            )
            self._skip(
                case, "W6.11",
                "An unparsable slot drops date, time AND duration on a real invoice",
                "allow_staging_mutations is false; this needs a real inbound order.",
            )
            return {"table": results, "orddd": orddd}

        if not self.runtime_state.get("parity_customer"):
            self._skip(
                case, "W6.10",
                "custom_delivery_duration is stored in SECONDS on a real invoice",
                "The parity fixture customer was not allocated, so no order can be posted. "
                "(W6 runs before PARITY-CUST so its parser table is always available.)",
            )
            self._skip(
                case, "W6.11",
                "An unparsable slot drops date, time AND duration on a real invoice",
                "The parity fixture customer was not allocated, so no order can be posted.",
            )
            return {"table": results, "orddd": orddd}

        territory = self._delivery_territory_fixture() or self._primary_territory_fixture()
        items = self._order_fixture_items(str(territory.get("price_list") or ""))[:1]
        slot = dict(self.fixture_catalog.get("next_delivery_slot") or _next_delivery_slot())

        good = self._create_parity_order(
            note="W6 parsable-slot order",
            item_rows=[{**dict(items[0]), "qty": 1}],
            territory_fixture=territory,
            delivery_slot=slot,
            delivery_date_label=slot["delivery_date"],
            time_slot_label="19:00 - 20:30",
        )
        good_pull = self._pull_created_order(good["woo_order_id"])
        good_invoice = (
            None if self._pull_was_locked(good_pull)
            else self._invoice_for_order(good["woo_order_id"])
        )
        if good_invoice is None:
            self._skip(
                case, "W6.10",
                "custom_delivery_duration is stored in SECONDS (90 minutes -> 5400)",
                self._lock_skip_reason(good_pull) if self._pull_was_locked(good_pull)
                else "Inbound did not produce an invoice for the parsable-slot order.",
                actual=good_pull,
            )
        else:
            self._record_created("Sales Invoice", good_invoice.name, note="W6 parsable-slot invoice")
            duration = getattr(good_invoice, "custom_delivery_duration", None)
            time_from = getattr(good_invoice, "custom_delivery_time_from", None)
            self._check(
                case, "W6.10",
                "custom_delivery_duration is stored in SECONDS (90 minutes -> 5400)",
                int(duration or 0) == 5400,
                expected=5400,
                actual={
                    "custom_delivery_duration": duration,
                    "custom_delivery_time_from": str(time_from),
                },
                note="order_sync.py:4437 multiplies the parsed minutes by 60.",
            )
            self._check(
                case, "W6.10b",
                "custom_delivery_time_from is the 24-hour start of the slot",
                str(time_from) in {"19:00:00", str(timedelta(hours=19))},
                expected="19:00:00",
                actual=str(time_from),
                note="A Frappe Time field comes back as a timedelta, not a time.",
            )

        bad = self._create_parity_order(
            note="W6 unparsable-slot order",
            item_rows=[{**dict(items[0]), "qty": 1}],
            territory_fixture=territory,
            delivery_slot=slot,
            delivery_date_label=slot["delivery_date"],
            time_slot_label="sometime after lunch",
        )
        bad_pull = self._pull_created_order(bad["woo_order_id"])
        bad_invoice = (
            None if self._pull_was_locked(bad_pull)
            else self._invoice_for_order(bad["woo_order_id"])
        )
        if bad_invoice is None:
            self._skip(
                case, "W6.11",
                "An unparsable slot drops date, time AND duration together",
                self._lock_skip_reason(bad_pull) if self._pull_was_locked(bad_pull)
                else "Inbound did not produce an invoice for the unparsable-slot order.",
                actual=bad_pull,
            )
        else:
            self._record_created("Sales Invoice", bad_invoice.name, note="W6 unparsable-slot invoice")
            dropped = {
                "custom_delivery_date": getattr(bad_invoice, "custom_delivery_date", None),
                "custom_delivery_time_from": getattr(bad_invoice, "custom_delivery_time_from", None),
                "custom_delivery_duration": getattr(bad_invoice, "custom_delivery_duration", None),
            }
            self._check(
                case, "W6.11",
                "An unparsable slot drops date, time AND duration together",
                not dropped["custom_delivery_date"]
                and not dropped["custom_delivery_time_from"]
                and not dropped["custom_delivery_duration"],
                expected={"all three": "empty"},
                actual={key: str(value) for key, value in dropped.items()},
                note=(
                    "order_sync.py:4066-4069 nulls all three unless all three parse. The delivery "
                    "date itself was perfectly readable and is discarded with the slot."
                ),
            )

        return {"table": results, "orddd": orddd, "good_pull": good_pull, "bad_pull": bad_pull}

    # ------------------------------------------------------------------
    # W7 - Terminal status on a submitted invoice
    # ------------------------------------------------------------------

    def _w7_terminal_status(self, case: dict[str, Any]) -> dict[str, Any]:
        territory = self._delivery_territory_fixture() or self._primary_territory_fixture()
        items = self._order_fixture_items(str(territory.get("price_list") or ""))[:1]
        run = self._create_parity_order(
            note="W7 terminal-status order",
            item_rows=[{**dict(items[0]), "qty": 1}],
            territory_fixture=territory,
        )
        woo_order_id = run["woo_order_id"]
        pull = self._pull_created_order(woo_order_id)
        if self._pull_was_locked(pull):
            self._skip(
                case, "W7.01", "Inbound sync created a submitted invoice",
                self._lock_skip_reason(pull), actual=pull,
            )
            return {"pull": pull}

        invoice = self._invoice_for_order(woo_order_id)
        self._check(
            case, "W7.01",
            "Inbound sync created a submitted invoice",
            invoice is not None and int(getattr(invoice, "docstatus", 0) or 0) == 1,
            expected={"docstatus": 1},
            actual={
                "pull": pull,
                "docstatus": getattr(invoice, "docstatus", None) if invoice else None,
            },
        )
        if invoice is None:
            return {"pull": pull}
        self._record_created("Sales Invoice", invoice.name, note="W7 inbound invoice")

        grand_total = round(float(getattr(invoice, "grand_total", 0) or 0), 2)
        gl_before = self._gl_summary(invoice.name)

        self._put_created_order(woo_order_id, {"status": "cancelled"})
        cancel_pull = self._pull_created_order(woo_order_id)
        if self._pull_was_locked(cancel_pull):
            self._skip(
                case, "W7.02",
                "A Woo cancellation either cancels the invoice or is flagged for review",
                self._lock_skip_reason(cancel_pull), actual=cancel_pull,
            )
            return {"pull": pull, "cancel_pull": cancel_pull}

        invoice_after = frappe.get_doc("Sales Invoice", invoice.name)
        docstatus = int(getattr(invoice_after, "docstatus", 0) or 0)
        reason = str(cancel_pull.get("reason") or "").strip().lower()
        order_map = self._order_map_full(woo_order_id) or {}
        gl_after = self._gl_summary(invoice.name)
        outcome = (
            "cancelled" if docstatus == 2
            else ("needs_return_workflow" if reason == "needs_return_workflow" else "other")
        )

        self._check(
            case, "W7.02",
            "A Woo cancellation resolves to a standard cancel or the manual-review path",
            outcome in {"cancelled", "needs_return_workflow"},
            expected="docstatus 2, or reason=needs_return_workflow",
            actual={"outcome": outcome, "pull": cancel_pull, "docstatus": docstatus},
        )

        if outcome == "cancelled":
            self._check(
                case, "W7.03",
                "Standard cancel: the invoice is cancelled and its revenue reversed",
                docstatus == 2 and abs(gl_after["receivable_debit"]) <= 0.01,
                expected={"docstatus": 2, "live_receivable_debit": 0},
                actual={"docstatus": docstatus, "gl_after": gl_after},
            )
            self._check(
                case, "W7.04",
                "The cancellation is labelled as WooCommerce-originated",
                str(getattr(invoice_after, "custom_cancellation_type", "") or "") == "WooCommerce Cancelled",
                expected="WooCommerce Cancelled",
                actual=getattr(invoice_after, "custom_cancellation_type", None),
            )
            self._skip(
                case, "W7.05",
                "Dispatched order: revenue stays standing and the phantom AR is FLAGGED",
                "This invoice was not dispatched, so jarz_pos permitted the cancel and the "
                "needs_return_workflow branch was never reached. Exercising it requires an "
                "Out-for-Delivery invoice, which needs stock and a delivery note.",
                actual={"outcome": outcome},
            )
        else:
            self._check(
                case, "W7.03",
                "Manual review: the invoice is STILL submitted",
                docstatus == 1,
                expected=1,
                actual=docstatus,
            )
            self._check(
                case, "W7.04",
                "Manual review: revenue is still standing (receivable unchanged)",
                abs(gl_after["receivable_debit"] - gl_before["receivable_debit"]) <= 0.01
                and abs(gl_after["receivable_debit"] - grand_total) <= 0.01,
                expected=grand_total,
                actual={
                    "before": gl_before["receivable_debit"],
                    "after": gl_after["receivable_debit"],
                },
            )
            self._check(
                case, "W7.05",
                "The phantom AR is FLAGGED for a human on the Order Map",
                int(order_map.get("needs_manual_review") or 0) == 1
                and bool(str(order_map.get("manual_review_reason") or "").strip()),
                expected={"needs_manual_review": 1, "manual_review_reason": "non-empty"},
                actual=order_map,
                note=(
                    "order_sync.py:2686-2725 — a refusal is reported as a SUCCESSFUL skip so the "
                    "reconcile sweep does not count it as an error, and the order is flagged instead."
                ),
            )
            self._check(
                case, "W7.06",
                "The cancellation result is reported as a success, not an error",
                bool(cancel_pull.get("success")),
                expected=True,
                actual=cancel_pull,
            )

        return {
            "woo_order_id": woo_order_id,
            "invoice": invoice.name,
            "outcome": outcome,
            "cancel_pull": cancel_pull,
            "order_map": order_map,
            "gl_before": gl_before,
            "gl_after": gl_after,
        }

    # ------------------------------------------------------------------
    # W8 - Outbound invoice parity
    # ------------------------------------------------------------------

    def _w8_outbound_parity(self, case: dict[str, Any]) -> dict[str, Any]:
        outbound_sync = _woo_module(_OUTBOUND_SYNC_MODULE)

        territory = self._delivery_territory_fixture()
        if not territory:
            self._skip(
                case, "W8.00",
                "A Territory with delivery_income > 0 and a POS Profile exists",
                "No Territory on this site has both a POS Profile and delivery_income > 0, so a paid "
                "shipping line cannot be produced through the supported POS path.",
                actual=self.capabilities.get("delivery_territory"),
            )
            return {"skipped": True}

        customer_name = self._resolve_outbound_customer()
        if not customer_name:
            self._skip(
                case, "W8.00",
                "An ERP customer exists for the outbound push",
                "No ERP customer was created by this run, so there is nothing safe to push. Reusing a "
                "cloned production customer is exactly the hijack this runner exists to prevent.",
            )
            return {"skipped": True}

        promo = self.capabilities.get("promo_code") or {}
        promo_codes = [promo["codes"][0]] if promo.get("available") and promo.get("codes") else None

        invoice_name = self._create_pos_invoice_for_outbound(
            territory=territory,
            customer_name=customer_name,
            promo_codes=promo_codes,
        )
        if not invoice_name:
            self._skip(
                case, "W8.00",
                "A POS invoice could be created for the outbound push",
                "create_pos_invoice did not return an invoice; the POS path is unavailable on this "
                "site right now (shift/branch gating, stock, or price list).",
            )
            return {"skipped": True}
        self._record_created("Sales Invoice", invoice_name, note="W8 outbound POS invoice")

        invoice = frappe.get_doc("Sales Invoice", invoice_name)
        self._ensure_delivery_window(invoice)
        invoice = frappe.get_doc("Sales Invoice", invoice_name)

        discount = round(float(getattr(invoice, "discount_amount", 0) or 0), 2)
        shipping_row = self._shipping_income_row(invoice)
        shipping_amount = round(float((shipping_row or {}).get("tax_amount") or 0), 2)

        payload = None
        if hasattr(outbound_sync, "_build_order_payload"):
            try:
                payload = outbound_sync._build_order_payload(invoice)
            except Exception as exc:  # noqa: BLE001
                payload = None
                self.report["concerns"].append({
                    "case_id": "W8",
                    "description": "_build_order_payload raised while being probed",
                    "error": str(exc),
                })
        if payload is not None:
            mismatch = outbound_sync._payload_total_mismatch(payload, invoice)
            self._check(
                case, "W8.01",
                "_payload_total_mismatch returns None (the payload's arithmetic reaches grand_total)",
                mismatch is None,
                expected=None,
                actual=mismatch,
            )
        else:
            self._skip(
                case, "W8.01",
                "_payload_total_mismatch returns None (the payload's arithmetic reaches grand_total)",
                "The outbound payload could not be built in isolation on this build, so the pre-push "
                "arithmetic guard cannot be evaluated on its own. The post-push total is still "
                "asserted through the Synced status below.",
            )

        sync_result = outbound_sync.sync_sales_invoice(
            invoice_name, reason=f"woo_parity_validation:{self.run_id}", force=True
        )
        frappe.db.commit()
        invoice = frappe.get_doc("Sales Invoice", invoice_name)
        woo_order_id = str(getattr(invoice, "woo_order_id", "") or "")
        if woo_order_id:
            self._record_created("Woo Order", woo_order_id, note="W8 outbound Woo order")
        woo_order = self._woo_order(woo_order_id) if woo_order_id else None

        self._check(
            case, "W8.02",
            "The invoice reached the store and is marked Synced",
            str(getattr(invoice, "woo_outbound_status", "") or "") == "Synced" and bool(woo_order),
            expected={"woo_outbound_status": "Synced", "woo_order": "exists"},
            actual={
                "woo_outbound_status": getattr(invoice, "woo_outbound_status", None),
                "woo_order_id": woo_order_id,
                "sync_result": sync_result,
            },
        )
        if not woo_order:
            return {"invoice": invoice_name, "sync_result": sync_result}

        fee_lines = [dict(row) for row in (woo_order.get("fee_lines") or [])]
        negative_fees = [row for row in fee_lines if float(row.get("total") or 0) < 0]
        shipping_lines = [dict(row) for row in (woo_order.get("shipping_lines") or [])]
        first_shipping = shipping_lines[0] if shipping_lines else {}

        if discount > 0:
            self._check(
                case, "W8.03",
                "The header discount is pushed as exactly one negative fee line",
                len(negative_fees) == 1
                and abs(float(negative_fees[0].get("total") or 0) + discount) <= 0.02,
                expected={"count": 1, "total": -discount},
                actual=fee_lines,
            )
        else:
            self._skip(
                case, "W8.03",
                "The header discount is pushed as exactly one negative fee line",
                "No enabled Jarz Promo Code exists on this site, so a header discount cannot be "
                "created through the supported POS path and there is nothing to render as a fee line.",
                actual={"discount_amount": discount, "promo_capability": promo},
            )

        expected_method = ("flat_rate", "Delivery") if shipping_amount > 0 else ("flat_rate", "Free Delivery")
        self._check(
            case, "W8.04",
            f"shipping_lines[0] is {expected_method[0]}/{expected_method[1]} for a "
            f"{'paid' if shipping_amount > 0 else 'free'} delivery",
            str(first_shipping.get("method_id") or "") == expected_method[0]
            and str(first_shipping.get("method_title") or "") == expected_method[1],
            expected={"method_id": expected_method[0], "method_title": expected_method[1]},
            actual=first_shipping,
            note=(
                "The literal 'Shipping' appears on exactly two orders in the whole store, both "
                "pushed from here — that was the giveaway."
            ),
        )
        self._check(
            case, "W8.05",
            "shipping_lines[0].total equals the Shipping Income tax row",
            abs(round(float(first_shipping.get("total") or 0), 2) - shipping_amount) <= 0.02,
            expected=shipping_amount,
            actual={"shipping_line_total": first_shipping.get("total"), "tax_row": shipping_row},
        )

        meta = {
            str((row or {}).get("key") or ""): (row or {}).get("value")
            for row in woo_order.get("meta_data") or []
        }
        expected_meta = outbound_sync._build_delivery_metadata(invoice)
        expected_note = outbound_sync._build_delivery_details_note(invoice)
        orddd = self.capabilities.get("orddd") or {}
        expected_slot = next(
            (str(row.get("value")) for row in expected_meta if row.get("key") == "_orddd_time_slot"),
            None,
        )

        if orddd.get("present") is False:
            self._skip(
                case, "W8.06",
                "The _orddd_* 24-hour meta reaches the store",
                "This store has no ORDDD plugin, so its meta shape is not evidence about "
                "production's wire format. Never derive wire-format truth from the demo store.",
                actual={"orddd": orddd, "pushed_meta": expected_meta},
            )
        elif not expected_slot:
            self._skip(
                case, "W8.06",
                "The _orddd_* 24-hour meta reaches the store",
                "The invoice carries no resolvable delivery window, so no slot meta is generated to "
                "compare against.",
                actual={"expected_meta": expected_meta},
            )
        else:
            self._check(
                case, "W8.06",
                "The _orddd_* 24-hour meta reaches the store",
                str(meta.get("_orddd_time_slot") or "") == str(expected_slot)
                and bool(meta.get("_orddd_delivery_date")),
                expected={"_orddd_time_slot": expected_slot},
                actual={
                    "_orddd_time_slot": meta.get("_orddd_time_slot"),
                    "_orddd_delivery_date": meta.get("_orddd_delivery_date"),
                    "Delivery Date": meta.get("Delivery Date"),
                    "Time Slot": meta.get("Time Slot"),
                },
            )

        notes = self._woo_order_notes(woo_order_id)
        note_bodies = [str((row or {}).get("note") or "") for row in notes]
        if not expected_note:
            self._skip(
                case, "W8.07",
                "The 12-hour 'Delivery details' order note is posted",
                "The invoice carries no delivery date, so no delivery-details note is generated.",
                actual={"custom_delivery_date": getattr(invoice, "custom_delivery_date", None)},
            )
        else:
            self._check(
                case, "W8.07",
                "The 12-hour 'Delivery details' order note is posted byte-for-byte",
                any(expected_note == body for body in note_bodies),
                expected=expected_note,
                actual=[body for body in note_bodies if "Delivery details" in body] or note_bodies[:3],
                note=(
                    "The plugin writes 24-hour in the meta and 12-hour in the note; that asymmetry "
                    "is the plugin's, and both have to be reproduced."
                ),
            )

        # --- remove the discount and re-push: the fee line must be deleted ---
        if discount > 0:
            removal = self._remove_discount_and_repush(invoice_name)
            if removal.get("skipped"):
                self._skip(
                    case, "W8.08",
                    "Removing the discount deletes the fee line on the store",
                    str(removal["skipped"]),
                    actual=removal,
                )
            else:
                after_order = self._woo_order(woo_order_id) or {}
                after_negative = [
                    row for row in (after_order.get("fee_lines") or [])
                    if float((row or {}).get("total") or 0) < 0
                ]
                self._check(
                    case, "W8.08",
                    "Removing the discount deletes the fee line on the store",
                    not after_negative,
                    expected=[],
                    actual=after_order.get("fee_lines"),
                    note=(
                        "outbound_sync.py:2769 sends {'id': <id>, 'name': None}, which is how Woo "
                        "deletes a fee line."
                    ),
                )
        else:
            self._skip(
                case, "W8.08",
                "Removing the discount deletes the fee line on the store",
                "No discount was applied in the first place (no enabled Jarz Promo Code), so there "
                "is no fee line to retract.",
            )

        return {
            "invoice": invoice_name,
            "woo_order_id": woo_order_id,
            "discount_amount": discount,
            "shipping_income": shipping_amount,
            "sync_result": sync_result,
            "expected_meta": expected_meta,
            "expected_note": expected_note,
            "store_fee_lines": fee_lines,
            "store_shipping_lines": shipping_lines,
        }

    def _resolve_outbound_customer(self) -> str:
        """An ERP customer THIS run brought into existence, never a cloned one."""
        for row in self.report.get("created_records") or []:
            if str(row.get("record_type")) == "Customer" and row.get("record_name"):
                return str(row["record_name"])
        parity = self.runtime_state.get("parity_customer") or {}
        woo_customer_id = str(parity.get("woo_customer_id") or "")
        if woo_customer_id:
            name = self._find_customer_by_woo_customer_id(woo_customer_id)
            if name:
                self._record_created(
                    "Customer", name, note="Created by inbound from this run's Woo customer"
                )
                return name
        return ""

    def _create_pos_invoice_for_outbound(
        self,
        *,
        territory: dict[str, Any],
        customer_name: str,
        promo_codes: list[str] | None,
    ) -> str:
        from jarz_pos.services.invoice_creation import create_pos_invoice

        items = self._order_fixture_items(
            str(territory.get("price_list") or ""),
            warehouse=str(territory.get("warehouse") or ""),
            require_stock=True,
        )[:1]
        slot = dict(self.fixture_catalog.get("next_delivery_slot") or _next_delivery_slot())
        try:
            result = create_pos_invoice(
                cart_json=self._build_cart_json([{**dict(items[0]), "qty": 1}]),
                customer_name=customer_name,
                pos_profile_name=str(territory.get("pos_profile") or ""),
                required_delivery_datetime=slot["required_delivery_datetime"],
                shipping_address_name=self._default_shipping_address_name(customer_name),
                payment_method="Cash",
                promo_codes=promo_codes,
                channel="parity-harness",
            )
            frappe.db.commit()
        except Exception as exc:  # noqa: BLE001
            self.report["concerns"].append({
                "case_id": "W8",
                "description": "create_pos_invoice failed",
                "error": str(exc),
            })
            return ""
        return str((result or {}).get("invoice_name") or "")

    def _ensure_delivery_window(self, invoice: Any) -> None:
        """Guarantee the invoice carries a full slot, so W8 tests a real window.

        ``create_pos_invoice`` takes a single ``required_delivery_datetime``. If it
        leaves the duration unset the outbound builder legitimately emits a
        single-point slot, and W8 would then be asserting the degenerate shape
        rather than the range. Only ever written when missing, and only on an
        invoice this run created.
        """
        self._require_self_created("Sales Invoice", str(invoice.name))
        updates: dict[str, Any] = {}
        if not getattr(invoice, "custom_delivery_time_from", None):
            updates["custom_delivery_time_from"] = "19:00:00"
        if not getattr(invoice, "custom_delivery_duration", None):
            updates["custom_delivery_duration"] = 5400
        if not getattr(invoice, "custom_delivery_date", None):
            updates["custom_delivery_date"] = (now_datetime() + timedelta(days=1)).date().isoformat()
        if not updates:
            return
        frappe.db.set_value("Sales Invoice", invoice.name, updates, update_modified=False)
        frappe.db.commit()
        self.report["concerns"].append({
            "case_id": "W8",
            "description": "Harness stamped a delivery window on its own invoice",
            "updates": _json_safe(updates),
        })

    def _remove_discount_and_repush(self, invoice_name: str) -> dict[str, Any]:
        outbound_sync = _woo_module(_OUTBOUND_SYNC_MODULE)

        self._require_self_created("Sales Invoice", invoice_name)
        invoice = frappe.get_doc("Sales Invoice", invoice_name)
        if int(getattr(invoice, "docstatus", 0) or 0) == 1:
            return {"skipped": (
                "The invoice is submitted, and a submitted invoice's discount cannot be changed "
                "without an amendment. The fee-line deletion path needs a discount that can be "
                "cleared in place."
            )}
        frappe.db.set_value("Sales Invoice", invoice_name, {"discount_amount": 0}, update_modified=False)
        frappe.db.commit()
        result = outbound_sync.sync_sales_invoice(
            invoice_name,
            reason=f"woo_parity_validation:{self.run_id}:discount_removed",
            force=True,
        )
        frappe.db.commit()
        return {"sync_result": result}

    def _woo_order_notes(self, woo_order_id: str) -> list[dict[str, Any]]:
        try:
            body = self._woo_client().get(f"orders/{woo_order_id}/notes", params={"per_page": 50})
        except Exception:  # noqa: BLE001
            return []
        return [dict(row) for row in body] if isinstance(body, list) else []

    # ------------------------------------------------------------------
    # W9 - Outbound kill-switch visibility
    # ------------------------------------------------------------------

    def _w9_kill_switch_visibility(self, case: dict[str, Any]) -> dict[str, Any]:
        """``enable_outbound_orders`` off used to be a bare ``return``.

        Staging sat with it off for seven weeks with zero log lines and a green
        dashboard. ``_note_outbound_disabled`` (outbound_sync.py:212) now files one
        throttled Error Log per switch per hour.
        """
        outbound_sync = _woo_module(_OUTBOUND_SYNC_MODULE)

        if not self.allow_staging_mutations:
            self._skip(
                case, "W9.00",
                "The outbound kill-switch can be toggled",
                "allow_staging_mutations is false; this case writes a WooCommerce Settings flag.",
            )
            return {"skipped": True}

        title = "WooCommerce: outbound push skipped (enable_outbound_orders is off)"
        cache_key = "woo_outbound_disabled::enable_outbound_orders"
        snapshot = self._settings_snapshot.get("enable_outbound_orders") or {}

        invoice_name = ""
        for row in reversed(self.report.get("created_records") or []):
            if str(row.get("record_type")) == "Sales Invoice" and row.get("record_name"):
                invoice_name = str(row["record_name"])
                break
        if not invoice_name:
            self._skip(
                case, "W9.00",
                "An invoice created by this run is available to push",
                "No Sales Invoice has been created by this run yet, and pushing a pre-existing "
                "invoice would mutate a record cloned from production.",
            )
            return {"skipped": True}

        prior_logs = frappe.get_all(
            "Error Log",
            filters={"method": title, "creation": [">", now_datetime() - timedelta(hours=1)]},
            fields=["name", "creation"],
            limit_page_length=5,
        )
        frappe.cache().delete_value(cache_key)
        if frappe.cache().get_value(cache_key):
            self._skip(
                case, "W9.00",
                "The throttle window is clear before the test",
                "Another process re-set the throttle key immediately after it was cleared. The live "
                "crons share this cache key, so the one-log-per-hour assertion cannot be trusted "
                "here.",
                actual={"prior_logs": prior_logs},
            )
            return {"skipped": True}

        marker = now_datetime()
        first: dict[str, Any] = {}
        second: dict[str, Any] = {}
        logs_after_first: list[dict[str, Any]] = []
        logs_after_second: list[dict[str, Any]] = []
        try:
            self._write_single_field("enable_outbound_orders", 0)
            first = outbound_sync.sync_sales_invoice(
                invoice_name, reason=f"woo_parity_validation:{self.run_id}"
            ) or {}
            logs_after_first = self._error_logs_since(title, marker)
            second = outbound_sync.sync_sales_invoice(
                invoice_name, reason=f"woo_parity_validation:{self.run_id}:again"
            ) or {}
            logs_after_second = self._error_logs_since(title, marker)
        finally:
            if snapshot.get("present"):
                self._write_single_field("enable_outbound_orders", snapshot.get("value"))
            restored = (
                self._read_single_raw(("enable_outbound_orders",)) or {}
            ).get("enable_outbound_orders") or {}

        self._check(
            case, "W9.01",
            "The push is skipped with reason 'disabled' while the switch is off",
            bool(first.get("skipped")) and str(first.get("reason") or "") == "disabled",
            expected={"skipped": True, "reason": "disabled"},
            actual=first,
        )
        if len(logs_after_first) == 0 and prior_logs:
            self._skip(
                case, "W9.02",
                "Exactly one throttled Error Log is filed",
                "No log was filed because a concurrent process had already consumed this hour's "
                "throttle window (a prior log exists inside the hour). The throttle is doing its "
                "job; the count cannot be asserted in this window.",
                actual={"prior_logs": prior_logs, "logs_after_first": logs_after_first},
            )
        else:
            self._check(
                case, "W9.02",
                "Exactly one throttled Error Log is filed",
                len(logs_after_first) == 1,
                expected=1,
                actual={"count": len(logs_after_first), "logs": logs_after_first},
            )
        self._check(
            case, "W9.03",
            "A second push inside the hour files no second log",
            len(logs_after_second) == len(logs_after_first),
            expected=len(logs_after_first),
            actual={"after_first": len(logs_after_first), "after_second": len(logs_after_second)},
        )
        self._check(
            case, "W9.04",
            "enable_outbound_orders is restored to its snapshotted value",
            str(restored.get("value") or "") == str(snapshot.get("value") or ""),
            expected=snapshot.get("value"),
            actual=restored.get("value"),
        )

        return {
            "invoice": invoice_name,
            "first_push": first,
            "second_push": second,
            "logs_after_first": logs_after_first,
            "logs_after_second": logs_after_second,
            "snapshot": snapshot,
            "restored": restored,
        }

    def _error_logs_since(self, title: str, marker: datetime) -> list[dict[str, Any]]:
        rows = frappe.get_all(
            "Error Log",
            filters={"method": title, "creation": [">=", marker]},
            fields=["name", "creation", "method"],
            order_by="creation asc",
            limit_page_length=20,
        )
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # W10 - Territory-unresolved fallback
    # ------------------------------------------------------------------

    def _w10_territory_unresolved(self, case: dict[str, Any]) -> dict[str, Any]:
        territory = self._delivery_territory_fixture() or self._primary_territory_fixture()
        items = self._order_fixture_items(str(territory.get("price_list") or ""))[:1]

        run = self._create_parity_order(
            note="W10 territory-unresolved order",
            item_rows=[{**dict(items[0]), "qty": 1}],
            territory_fixture=territory,
            state_override=_JUNK_STATE,
        )
        woo_order_id = run["woo_order_id"]
        pull = self._pull_created_order(woo_order_id)
        if self._pull_was_locked(pull):
            self._skip(
                case, "W10.01", "Inbound sync created an invoice",
                self._lock_skip_reason(pull), actual=pull,
            )
            return {"pull": pull}

        invoice = self._invoice_for_order(woo_order_id)
        order_map = self._order_map_full(woo_order_id) or {}
        self._check(
            case, "W10.01",
            "Inbound sync created an invoice despite the junk state",
            invoice is not None,
            expected="one active Sales Invoice",
            actual={"pull": pull, "order_map": order_map},
        )
        if invoice is None:
            return {"pull": pull, "order_map": order_map}
        self._record_created("Sales Invoice", invoice.name, note="W10 inbound invoice")

        territory_error = str(order_map.get("last_territory_error") or "")
        customer_territory = str(
            frappe.db.get_value(
                "Customer", str(getattr(invoice, "customer", "") or ""), "territory"
            ) or ""
        )

        self._check(
            case, "W10.02",
            "The junk state did not resolve to a Territory",
            not str(order_map.get("resolved_order_territory") or "").strip(),
            expected="",
            actual={
                "resolved_order_territory": order_map.get("resolved_order_territory"),
                "woo_shipping_state": order_map.get("woo_shipping_state"),
                "woo_billing_state": order_map.get("woo_billing_state"),
            },
        )
        if customer_territory:
            self._check(
                case, "W10.03",
                "last_territory_error names the Customer.territory fallback",
                "fell back to Customer.territory" in territory_error,
                expected="mentions 'fell back to Customer.territory'",
                actual=territory_error,
                note=(
                    "order_sync.py:943-988 — the fallback itself is correct and deliberately "
                    "unchanged; it was simply invisible before this string was written."
                ),
            )
        else:
            self._check(
                case, "W10.03",
                "last_territory_error says the order has NO territory at all",
                "carries no territory" in territory_error,
                expected="mentions 'carries no territory'",
                actual=territory_error,
            )

        shipping_row = self._shipping_income_row(invoice)
        recheck = int(order_map.get("needs_territory_recheck") or 0)
        fallback_income = 0.0
        if customer_territory:
            try:
                fallback_income = round(
                    float(
                        frappe.db.get_value("Territory", customer_territory, "delivery_income") or 0
                    ),
                    2,
                )
            except Exception:  # noqa: BLE001
                fallback_income = 0.0

        if customer_territory and fallback_income > 0:
            self._skip(
                case, "W10.04",
                "With no usable territory, needs_territory_recheck is 1 and NO Shipping Income row "
                "is added",
                f"The customer falls back to Territory {customer_territory!r}, whose delivery_income "
                f"is {fallback_income}. The order therefore DOES get shipping income, so the "
                "no-territory branch is not the one under test here.",
                actual={
                    "customer_territory": customer_territory,
                    "fallback_delivery_income": fallback_income,
                    "shipping_row": shipping_row,
                    "needs_territory_recheck": recheck,
                },
            )
        else:
            self._check(
                case, "W10.04",
                "With no usable territory, needs_territory_recheck is 1 and NO Shipping Income row "
                "is added",
                recheck == 1 and shipping_row is None,
                expected={"needs_territory_recheck": 1, "shipping_income_row": None},
                actual={
                    "needs_territory_recheck": recheck,
                    "shipping_income_row": shipping_row,
                    "customer_territory": customer_territory or "(empty)",
                },
                note=(
                    "needs_territory_recheck fires when no POS Profile could be resolved "
                    "(order_sync.py:3921-3922), which is the same condition that costs the order "
                    "its delivery income."
                ),
            )

        self._assert_ledger(case, "W10", invoice)
        return {
            "woo_order_id": woo_order_id,
            "invoice": invoice.name,
            "order_map": order_map,
            "customer_territory": customer_territory,
            "shipping_row": shipping_row,
        }

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def _cleanup(self) -> None:
        """Tidy up ONLY what this run created. Never deletes a Woo order.

        Woo order deletion is invisible to every inbound cron and has left phantom
        AR behind before, so orders are cancelled on the store rather than removed.
        Coupons are deleted outright — they are ours and nothing references them.
        """
        actions: list[dict[str, Any]] = []

        for record_type, record_name in sorted(self._created_index):
            if record_type != "Sales Invoice":
                continue
            try:
                if not frappe.db.exists("Sales Invoice", record_name):
                    continue
                doc = frappe.get_doc("Sales Invoice", record_name)
                if int(getattr(doc, "docstatus", 0) or 0) != 1:
                    actions.append({
                        "action": "skip_invoice",
                        "name": record_name,
                        "docstatus": doc.docstatus,
                    })
                    continue
                doc.flags.ignore_permissions = True
                doc.flags.ignore_woo_outbound = True
                doc.cancel()
                frappe.db.commit()
                actions.append({"action": "cancel_invoice", "name": record_name, "ok": True})
            except Exception as exc:  # noqa: BLE001
                frappe.db.rollback()
                actions.append({
                    "action": "cancel_invoice",
                    "name": record_name,
                    "ok": False,
                    "error": str(exc),
                })

        for record_type, record_name in sorted(self._created_index):
            if record_type != "Woo Order":
                continue
            try:
                self._woo_client().put(f"orders/{record_name}", {"status": "cancelled"})
                actions.append({
                    "action": "cancel_woo_order",
                    "woo_order_id": record_name,
                    "ok": True,
                })
            except Exception as exc:  # noqa: BLE001
                actions.append({
                    "action": "cancel_woo_order",
                    "woo_order_id": record_name,
                    "ok": False,
                    "error": str(exc),
                })

        for record_type, record_name in sorted(self._created_index):
            if record_type != "Woo Coupon":
                continue
            try:
                self._woo_client().delete(f"coupons/{record_name}", params={"force": True})
                actions.append({
                    "action": "delete_woo_coupon",
                    "coupon_id": record_name,
                    "ok": True,
                })
            except Exception as exc:  # noqa: BLE001
                actions.append({
                    "action": "delete_woo_coupon",
                    "coupon_id": record_name,
                    "ok": False,
                    "error": str(exc),
                })

        self.report["cleanup"] = _json_safe(actions)


def run(
    environment: str = "staging",
    allow_staging_mutations: bool = False,
    run_id: str | None = None,
    cleanup: bool = True,
) -> dict[str, Any]:
    """Run the WooCommerce parity suite and return a structured summary."""
    runner = ParityRunner(
        environment=environment,
        allow_staging_mutations=allow_staging_mutations,
        run_id=run_id,
    )
    return runner.run(cleanup=cleanup)


def run_json(
    environment: str = "staging",
    allow_staging_mutations: bool = False,
    run_id: str | None = None,
    cleanup: bool = True,
) -> dict[str, Any]:
    """Run and print a marker-wrapped JSON report for SSH wrappers."""
    summary = run(
        environment=environment,
        allow_staging_mutations=allow_staging_mutations,
        run_id=run_id,
        cleanup=cleanup,
    )
    print(MARKER_START)
    print(json.dumps(_json_safe(summary), ensure_ascii=False, default=str, indent=2))
    print(MARKER_END)
    return summary
