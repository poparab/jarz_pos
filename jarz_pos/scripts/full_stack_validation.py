"""One command that runs every validation suite and reports the whole picture.

The suites in this directory each prove one slice — invoice lifecycle, the
non-invoice money paths, returns, the commercial-policy layer, the role gates,
the settings, WooCommerce. Run one at a time they answer one question each, and
nobody runs all of them, so the answer to "is the system correct right now" has
never existed as a single artefact. This module is that artefact.

It is deliberately thin: it owns no assertions of its own. Its job is to
(1) fingerprint the environment so a report can be trusted later, (2) run each
suite in a fixed order, (3) survive a suite that crashes so one broken slice
cannot hide the other seven, and (4) sweep fixtures afterwards and *verify the
sweep*, so a clean report also means a clean site.

The fingerprint matters more than it looks. Every suite's verdict depends on
site settings — whether returns are enabled, whether the Woo outbox is switched
on, which POS profile it picks. A green report from a site with the feature
under test switched off is worse than no report, so the settings are captured
into the output rather than assumed.

Refuses to run against production. Nothing here mutates a real document: the
suites create their own prefixed fixtures and clean them up.

Run::

    bench --site frontend execute jarz_pos.scripts.full_stack_validation.run

    # a subset, by suite key:
    bench --site frontend execute jarz_pos.scripts.full_stack_validation.run \
        --kwargs "{'only': ['roles', 'lifecycle']}"

    # keep the created documents for inspection (skips the fixture sweep):
    bench --site frontend execute jarz_pos.scripts.full_stack_validation.run \
        --kwargs "{'cleanup': False}"
"""
from __future__ import annotations

import json
import time
import traceback
from typing import Any, Dict, List, Optional

import frappe

MARKER_START = "FULL_STACK_VALIDATION_JSON_START"
MARKER_END = "FULL_STACK_VALIDATION_JSON_END"

#: Suite key -> (dotted module path, entry point, kwargs).
#: Ordered deliberately. The read-only surveys run first so a site that is
#: already broken is reported as broken before anything writes to it; the
#: mutating suites follow; the fixture sweep runs last.
SUITES: List[Dict[str, Any]] = [
    {
        "key": "settings",
        "module": "jarz_pos.scripts.settings_matrix_validation",
        "entry": "run",
        "kwargs": {},
        "what": "every settings flag actually changes the behaviour it names",
    },
    {
        "key": "roles",
        "module": "jarz_pos.scripts.role_matrix_validation",
        "entry": "run",
        "kwargs": {},
        "what": "staff / line manager / manager can do exactly what they should",
    },
    {
        "key": "b2b",
        "module": "jarz_pos.scripts.b2b_accounting_validation",
        "entry": "run",
        "kwargs": {},
        "what": "commercial-policy purposes book correctly; Standard is unchanged",
    },
    {
        "key": "lifecycle",
        "module": "jarz_pos.scripts.full_lifecycle_validation",
        "entry": "run",
        "kwargs": {},
        "what": "create -> dispatch -> settle -> cancel, every path, ledger asserted",
    },
    {
        "key": "operations",
        "module": "jarz_pos.scripts.operations_accounting_validation",
        "entry": "run",
        "kwargs": {},
        "what": "cash transfer, expenses, stock moves, inventory count, shift open/close",
    },
    {
        "key": "returns",
        "module": "jarz_pos.scripts.return_flow_validation",
        "entry": "run",
        "kwargs": {},
        "what": "post-dispatch returns book correctly and never void the source invoice",
    },
    {
        "key": "money_paths",
        "module": "jarz_pos.scripts.money_paths_validation",
        "entry": "run",
        "kwargs": {},
        "what": (
            "payment-method change after dispatch, amendment, address/territory change, "
            "InstaPay confirm, batch vs single settlement, cancel guards"
        ),
    },
    {
        "key": "woo",
        "module": "jarz_pos.scripts.woo_parity_validation",
        "entry": "run",
        "kwargs": {},
        "what": "WooCommerce inbound and outbound round-trip, operationally and in the ledger",
    },
]


# ---------------------------------------------------------------------------
# Guards and fingerprint
# ---------------------------------------------------------------------------

#: Hosts this suite is allowed to write to. Anything else is refused.
_ALLOWED_HOSTS = ("erpstg.orderjarz.com", "localhost", "127.0.0.1", "frontend", "development")

_PRODUCTION_HOSTS = ("erp.orderjarz.com",)


def _guard_environment() -> str:
    """Refuse to run anywhere that is not a known non-production site.

    Deliberately **fails closed**, which is the difference between this guard
    and the one the older harnesses share. Theirs asks "is this production?" and
    proceeds on no. That answers wrongly in the one case that matters: a site
    whose host it cannot read — a restored clone, a new environment, a
    misconfigured ``host_name`` — looks exactly like "not production" and gets
    written to. Asking instead "is this a site I was told to write to?" turns an
    unknown environment into a stop rather than a green light.

    Both questions are asked, not just the second, so a site that somehow
    matches an allowed name *and* production is still refused.
    """
    try:
        base_url = frappe.utils.get_url() or ""
    except Exception:
        base_url = ""
    host_name = str(frappe.conf.get("host_name") or "")
    site = frappe.local.site or ""
    haystack = " ".join([base_url, host_name, site]).lower()

    for prod in _PRODUCTION_HOSTS:
        if prod in haystack:
            raise RuntimeError(
                f"full_stack_validation refuses to run against production "
                f"(site={site!r} host_name={host_name!r} url={base_url!r}). "
                "Production is verified read-only."
            )

    if not any(allowed in haystack for allowed in _ALLOWED_HOSTS):
        raise RuntimeError(
            f"full_stack_validation refuses to run on an unrecognised site "
            f"(site={site!r} host_name={host_name!r} url={base_url!r}). "
            f"This suite writes documents; it only runs on {_ALLOWED_HOSTS}. "
            "Add the host deliberately if this environment is safe to mutate."
        )
    return host_name or base_url or site or "unknown"


def _raw_singles(doctype: str) -> Dict[str, Any]:
    """Read a Single straight out of ``tabSingles``.

    Not ``get_single_value``: that casts through ``cint`` for Check and Int
    fields, so a flag that was never written is indistinguishable from one an
    operator deliberately set to 0. A validation report has to be able to say
    "nobody has ever set this", so it reads the rows.
    """
    try:
        rows = frappe.db.sql(
            "SELECT field, value FROM tabSingles WHERE doctype = %s", (doctype,), as_dict=True
        ) or []
    except Exception:
        return {}
    return {r["field"]: r["value"] for r in rows}


def _fingerprint() -> Dict[str, Any]:
    """Everything needed to judge, months later, what this report actually meant."""
    fp: Dict[str, Any] = {
        "site": frappe.local.site,
        "base_url": (frappe.utils.get_url() or ""),
        "run_started": frappe.utils.now(),
        "installed_apps": [],
        "app_versions": {},
        "settings": {},
        "counts": {},
    }
    try:
        fp["installed_apps"] = list(frappe.get_installed_apps())
    except Exception:
        pass
    for app in fp["installed_apps"]:
        try:
            fp["app_versions"][app] = frappe.get_attr(f"{app}.__version__")
        except Exception:
            fp["app_versions"][app] = "unknown"

    for single in ("Jarz POS Settings", "Jarz Woocommerce Settings", "Woocommerce Settings"):
        raw = _raw_singles(single)
        if raw:
            # Never let a credential into a report that gets pasted around.
            fp["settings"][single] = {
                k: ("<set>" if _is_secret(k) and v else v)
                for k, v in sorted(raw.items())
            }

    for doctype in ("POS Profile", "Sales Invoice", "Courier Transaction", "Customer"):
        try:
            fp["counts"][doctype] = frappe.db.count(doctype)
        except Exception:
            fp["counts"][doctype] = None
    return fp


def _is_secret(fieldname: str) -> bool:
    lowered = fieldname.lower()
    return any(
        token in lowered
        for token in ("secret", "password", "key", "token", "vapid", "credential")
    )


# ---------------------------------------------------------------------------
# Suite normalisation
# ---------------------------------------------------------------------------

def _normalise(result: Any) -> Dict[str, Any]:
    """Reduce any suite's return value to the same shape.

    The suites predate each other and return slightly different dicts — some
    carry a ``checks`` list, some only counts, one adds a report path. Rather
    than change seven modules (and the drivers that already parse them), the
    differences are absorbed here.
    """
    if not isinstance(result, dict):
        return {"passed": 0, "failed": 0, "checks": [], "note": f"non-dict result: {result!r}"}
    checks = result.get("checks") or []
    passed = result.get("passed")
    failed = result.get("failed")
    if passed is None:
        passed = sum(1 for c in checks if c.get("passed"))
    if failed is None:
        failed = sum(1 for c in checks if not c.get("passed"))
    out = {"passed": int(passed or 0), "failed": int(failed or 0), "checks": checks}
    # Skips are a first-class third state, not a footnote. A suite that skipped
    # forty checks because the site had no courier bound is not the same as one
    # that passed forty, and the aggregate total must not let the two look alike.
    skipped = result.get("skipped")
    if skipped is None:
        skipped = sum(1 for c in checks if c.get("skipped"))
    out["skipped"] = int(skipped or 0)
    for extra in ("report_path", "note", "skipped_checks"):
        if result.get(extra) is not None:
            out[extra] = result[extra]
    return out


def _run_suite(
    suite: Dict[str, Any], cleanup: bool, extra: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Run one suite, converting a crash into a reported failure.

    A suite that raises must not take the run down with it: the whole point of
    running eight of them together is to see all eight verdicts, and the most
    interesting run is the one where something is already broken.
    """
    started = time.time()
    record: Dict[str, Any] = {
        "key": suite["key"], "module": suite["module"], "what": suite["what"],
    }
    try:
        module = frappe.get_module(suite["module"])
    except Exception as exc:
        record.update({
            "status": "unavailable", "passed": 0, "failed": 0, "checks": [],
            "error": f"{type(exc).__name__}: {exc}",
        })
        return record

    entry = getattr(module, suite["entry"], None)
    if entry is None:
        record.update({
            "status": "unavailable", "passed": 0, "failed": 0, "checks": [],
            "error": f"{suite['module']} has no {suite['entry']}()",
        })
        return record

    kwargs = dict(suite.get("kwargs") or {})
    # Per-suite opt-ins the caller passes explicitly, filtered against the
    # entry point's real signature so a renamed parameter fails loudly here
    # rather than being silently dropped and leaving the suite in the mode
    # nobody asked for.
    if extra:
        kwargs.update(extra)
    # Only pass cleanup to suites that accept it; the signatures differ.
    try:
        import inspect

        params = inspect.signature(entry).parameters
        if "cleanup" in params:
            kwargs["cleanup"] = cleanup
        unknown = [k for k in kwargs if k not in params]
        if unknown:
            record.update({
                "status": "unavailable", "passed": 0, "failed": 1, "skipped": 0,
                "checks": [],
                "error": (
                    f"{suite['module']}.{suite['entry']}() does not accept {unknown}; "
                    "refusing to run it in a mode the caller did not ask for"
                ),
            })
            return record
    except (TypeError, ValueError):
        pass

    print(f"\n{'=' * 72}\n>>> {suite['key']}: {suite['what']}\n{'=' * 72}")
    try:
        result = entry(**kwargs)
        record.update(_normalise(result))
        record["status"] = "ok" if record["failed"] == 0 else "failed"
    except Exception as exc:
        frappe.db.rollback()
        record.update({
            "status": "crashed", "passed": 0, "failed": 1, "checks": [],
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc()[-4000:],
        })
        frappe.log_error(frappe.get_traceback(), f"full_stack_validation: {suite['key']} crashed")
    record["seconds"] = round(time.time() - started, 1)
    return record


# ---------------------------------------------------------------------------
# Fixture sweep
# ---------------------------------------------------------------------------

def _sweep(report: Dict[str, Any]) -> None:
    """Purge fixtures, then prove the purge worked.

    Running the purge is not the check. A purge that silently fails leaves a
    clean-looking report over a dirty site, and residue from one run becomes the
    next run's starting state — which is exactly how a suite stops being
    repeatable. So the sweep is followed by a count that has to be zero.
    """
    try:
        from jarz_pos.scripts import purge_test_fixtures as purge
    except Exception as exc:
        report["sweep"] = {"ran": False, "error": f"{type(exc).__name__}: {exc}"}
        return

    try:
        result = purge.run(dry_run=False)
        residue: Dict[str, int] = {}
        for prefix in purge.FIXTURE_PREFIXES:
            for doctype in ("Customer", "Territory", "Customer Group"):
                try:
                    n = frappe.db.count(doctype, {"name": ["like", f"{prefix}%"]})
                except Exception:
                    n = -1
                if n:
                    residue[f"{doctype}:{prefix}"] = n
        report["sweep"] = {
            "ran": True,
            "purged": result,
            "residue": residue,
            "clean": not residue,
        }
    except Exception as exc:
        report["sweep"] = {"ran": False, "error": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(
    cleanup: bool = True,
    only: Optional[List[str]] = None,
    skip: Optional[List[str]] = None,
    allow_woo_mutations: bool = False,
) -> Dict[str, Any]:
    """Run the suites.

    ``allow_woo_mutations`` is off by default and deliberately separate from
    ``cleanup``. The Woo suite's mutating cases create real orders and customers
    on the WooCommerce store, and staging's store shares an id space with the
    cloned production data — so turning them on is a decision someone makes on
    purpose, not something that rides along with a default. With it off the Woo
    suite still runs its capability probes and every pure-function case, which
    is most of what regresses.
    """
    if isinstance(cleanup, str):
        cleanup = cleanup.strip().lower() not in {"", "0", "false", "no"}
    if isinstance(allow_woo_mutations, str):
        allow_woo_mutations = allow_woo_mutations.strip().lower() not in {
            "", "0", "false", "no",
        }
    if isinstance(only, str):
        only = [s.strip() for s in only.split(",") if s.strip()]
    if isinstance(skip, str):
        skip = [s.strip() for s in skip.split(",") if s.strip()]

    site = _guard_environment()
    frappe.flags.in_test = True
    # Nothing this suite creates may ever be pushed to the live store. Each
    # suite sets this too; setting it here as well means a suite that forgets
    # cannot leak, and the Woo suite clears it deliberately for its own cases.
    frappe.flags.ignore_woo_outbound = True

    selected = [
        s for s in SUITES
        if (not only or s["key"] in only) and (not skip or s["key"] not in skip)
    ]

    report: Dict[str, Any] = {
        "site": site,
        "fingerprint": _fingerprint(),
        "suites": [],
        "cleanup": cleanup,
    }

    report["allow_woo_mutations"] = allow_woo_mutations

    started = time.time()
    for suite in selected:
        extra = (
            {"allow_staging_mutations": True}
            if allow_woo_mutations and suite["key"] == "woo"
            else None
        )
        report["suites"].append(_run_suite(suite, cleanup, extra=extra))

    if cleanup:
        _sweep(report)
    else:
        report["sweep"] = {"ran": False, "error": "skipped: cleanup=False"}

    report["passed"] = sum(s.get("passed", 0) for s in report["suites"])
    report["failed"] = sum(s.get("failed", 0) for s in report["suites"])
    report["skipped"] = sum(s.get("skipped", 0) for s in report["suites"])
    report["seconds"] = round(time.time() - started, 1)
    report["suites_ok"] = [s["key"] for s in report["suites"] if s.get("status") == "ok"]
    report["suites_bad"] = [
        s["key"] for s in report["suites"] if s.get("status") in {"failed", "crashed", "unavailable"}
    ]
    # A dirty site invalidates the next run, so it counts against this one.
    if cleanup and not (report.get("sweep") or {}).get("clean", False):
        report["failed"] += 1
        report["suites_bad"].append("sweep")

    _print_summary(report)
    print(MARKER_START)
    print(json.dumps(report, indent=2, default=str))
    print(MARKER_END)
    return report


def _print_summary(report: Dict[str, Any]) -> None:
    print("\n" + "=" * 72)
    print("FULL STACK VALIDATION")
    print("=" * 72)
    print(f"site      : {report['site']}")
    print(f"duration  : {report['seconds']}s")
    for suite in report["suites"]:
        status = suite.get("status", "?")
        line = (
            f"  {suite['key']:<12} {status:<12} "
            f"{suite.get('passed', 0):>4} passed  {suite.get('failed', 0):>3} failed  "
            f"{suite.get('skipped', 0):>3} skipped  {suite.get('seconds', 0):>6}s"
        )
        if suite.get("error"):
            line += f"\n      {suite['error']}"
        print(line)
    sweep = report.get("sweep") or {}
    print(f"  {'sweep':<12} {'clean' if sweep.get('clean') else 'DIRTY':<12} {sweep.get('residue') or ''}")
    print("-" * 72)
    print(
        f"TOTAL: {report['passed']} passed, {report['failed']} failed, "
        f"{report.get('skipped', 0)} skipped"
    )
    if report.get("skipped"):
        print(
            "       Skips are checks that never ran because this site could not host "
            "them.\n       They are not passes — read them before trusting the total."
        )
    if report["suites_bad"]:
        print(f"NEEDS ATTENTION: {', '.join(report['suites_bad'])}")
    print("=" * 72)
