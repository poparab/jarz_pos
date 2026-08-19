"""Role-scoped acceptance harness — what STAFF and LINE MANAGERS can actually do.

Every other harness in this directory runs as Administrator. That proves the
accounting is right when nothing is in the way, and proves nothing at all about
the gates: an endpoint with no permission check and an endpoint guarded by
``JARZ Manager`` behave identically under a user who holds every role. The
divergence that matters operationally — the Flutter client hides a button the
server would happily serve, or the server refuses something the client offers —
is invisible from there by construction.

So this module logs in over **real HTTP** as synthetic users carrying the same
role sets as real staff, and asserts each endpoint's verdict for each persona.
HTTP rather than ``frappe.set_user`` deliberately: set_user swaps the role list
but keeps the harness's own session, so it never exercises login, the session
cookie, ``allow_guest``, or the branch scoping that reads from the session
user's POS Profile links. Those are precisely the layers a staff phone goes
through.

Personas are not hand-written role lists. Each one is *copied from a real user*
on the site (see :func:`_derive_persona_roles`) so the matrix describes the
staff who exist, not the staff we imagined. A hardcoded fallback is used only
when the site has no such user, and the report says so when that happens.

Synthetic users are prefixed ``_ROLETEST_`` and deleted in ``finally``. Refuses
to run against production.

Run::

    bench --site frontend execute jarz_pos.scripts.role_matrix_validation.run
    bench --site frontend execute jarz_pos.scripts.role_matrix_validation.run \
        --kwargs "{'cleanup': False}"
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

import frappe

from jarz_pos.constants import ROLES

MARKER_START = "ROLE_MATRIX_JSON_START"
MARKER_END = "ROLE_MATRIX_JSON_END"

USER_PREFIX = "_ROLETEST_"
#: ``.invalid`` is reserved by RFC 2606 and can never resolve, so a stray
#: notification cannot reach a real inbox even if one escapes the no-mail flag.
USER_DOMAIN = "roletest.jarz.invalid"
TEST_PASSWORD = "R0leM@trix-Harness-2026"

#: Used only when the site has no real user to copy a persona from.
_FALLBACK_ROLES: Dict[str, List[str]] = {
    "staff": ["POS User"],
    "line_manager": ["POS User", ROLES.JARZ_LINE_MANAGER],
    "manager": ["POS User", ROLES.JARZ_MANAGER],
}


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

def _guard_environment() -> None:
    """Hard stop anywhere that looks like production.

    Mirrors the guard the other harnesses use rather than importing one, so this
    module stays runnable on its own if the others are ever moved.
    """
    try:
        base_url = frappe.utils.get_url() or ""
    except Exception:
        base_url = ""
    host = str(frappe.db.get_single_value("Website Settings", "subdomain") or "")
    if "erp.orderjarz.com" in base_url or "erp.orderjarz.com" in host:
        raise RuntimeError(
            f"role_matrix_validation refuses to run against production ({base_url!r})."
        )


# ---------------------------------------------------------------------------
# Result bookkeeping
# ---------------------------------------------------------------------------

class RoleRunContext:
    """Collects check results and the synthetic records to tear down."""

    def __init__(self) -> None:
        self.checks: List[Dict[str, Any]] = []
        self.users: List[str] = []
        self.personas: Dict[str, Dict[str, Any]] = {}
        self.profile_links: List[Tuple[str, str]] = []

    def record(self, name: str, passed: bool, detail: str = "") -> None:
        self.checks.append(
            {"name": name, "passed": bool(passed), "detail": detail, "skipped": False}
        )
        print(f"   [{'PASS' if passed else 'FAIL'}] {name}" + (f" :: {detail}" if detail else ""))

    def skip(self, name: str, reason: str) -> None:
        """A check that did not run because the environment could not host it.

        Same third state the accounting harnesses use, and here for the same
        reason: scoring an un-run check as a pass is how a suite reports green
        having proven nothing. The specific case this exists for is a site with
        no real user matching a persona — the matrix still runs against the
        fallback role set, but the report has to say the persona was invented
        rather than observed.
        """
        self.checks.append(
            {"name": name, "passed": False, "detail": reason, "skipped": True}
        )
        print(f"   [SKIP] {name}" + (f" :: {reason}" if reason else ""))

    @property
    def passed(self) -> int:
        return sum(1 for c in self.checks if c["passed"] and not c["skipped"])

    @property
    def failed(self) -> int:
        return sum(1 for c in self.checks if not c["passed"] and not c["skipped"])

    @property
    def skipped(self) -> int:
        return sum(1 for c in self.checks if c["skipped"])


# ---------------------------------------------------------------------------
# Persona derivation
# ---------------------------------------------------------------------------

def _enabled_human_users() -> List[str]:
    """Enabled login accounts, newest first.

    Excludes Administrator and Guest, and anything this harness created, so a
    previous run's residue can never become the template for the next one.
    """
    rows = frappe.get_all(
        "User",
        filters={"enabled": 1, "user_type": "System User"},
        fields=["name"],
        order_by="creation desc",
        limit_page_length=0,
    ) or []
    return [
        r["name"] for r in rows
        if r["name"] not in {"Administrator", "Guest"}
        and not r["name"].startswith(USER_PREFIX)
    ]


def _roles_of(user: str) -> set:
    return set(
        frappe.get_all("Has Role", filters={"parent": user, "parenttype": "User"}, pluck="role")
        or []
    )


def _derive_persona_roles(ctx: RoleRunContext) -> Dict[str, Dict[str, Any]]:
    """Copy each persona's role set from a real user on this site.

    Hand-writing the sets is how a harness ends up testing a staff member who
    does not exist. The staff selection is deliberately *negative* — a staff user
    is one who is not in the line-manager tier — because the tier sets are the
    thing under test and must not be used to define their own input.
    """
    tier_lower = {r.lower() for r in ROLES.LINE_MANAGER_TIER}
    manager_lower = {ROLES.JARZ_MANAGER.lower(), "system manager", "administrator"}
    line_only_lower = {ROLES.JARZ_LINE_MANAGER.lower(), "jarz line manager"}

    found: Dict[str, Dict[str, Any]] = {}
    for user in _enabled_human_users():
        roles = _roles_of(user)
        lowered = {r.lower() for r in roles}
        if not lowered:
            continue

        if "staff" not in found and not (lowered & tier_lower):
            found["staff"] = {"template": user, "roles": sorted(roles)}
        if (
            "line_manager" not in found
            and (lowered & line_only_lower)
            and not (lowered & manager_lower)
        ):
            found["line_manager"] = {"template": user, "roles": sorted(roles)}
        if (
            "manager" not in found
            and ROLES.JARZ_MANAGER.lower() in lowered
            and "administrator" not in lowered
        ):
            found["manager"] = {"template": user, "roles": sorted(roles)}

        if len(found) == 3:
            break

    for persona, fallback in _FALLBACK_ROLES.items():
        if persona not in found:
            found[persona] = {"template": None, "roles": list(fallback)}
            ctx.skip(
                f"persona.{persona}.template_found",
                "no real user on this site matches this persona; using the hardcoded "
                f"fallback {fallback}. The role gates below are still asserted, but "
                "against an invented user rather than an observed one.",
            )
        else:
            ctx.record(
                f"persona.{persona}.template_found", True,
                f"copied from {found[persona]['template']}: {found[persona]['roles']}",
            )
    return found


# ---------------------------------------------------------------------------
# Provisioning
# ---------------------------------------------------------------------------

def _pos_profiles_of(user: Optional[str]) -> List[str]:
    if not user:
        return []
    return frappe.get_all(
        "POS Profile User", filters={"user": user}, pluck="parent", limit_page_length=0
    ) or []


def _default_profiles() -> List[str]:
    name = frappe.db.get_value("POS Profile", {"disabled": 0}, "name")
    return [name] if name else []


def _provision_user(ctx: RoleRunContext, persona: str, spec: Dict[str, Any]) -> Optional[str]:
    """Create (or reset) one synthetic user carrying the persona's roles.

    Idempotent by design: a run interrupted before teardown leaves the user
    behind, and the next run must not die on a duplicate. The existing record is
    *reset* to the persona's current role set rather than reused as-is, so a
    stale user from an older matrix cannot silently change this run's verdicts.
    """
    email = f"{USER_PREFIX}{persona}@{USER_DOMAIN}"
    try:
        if frappe.db.exists("User", email):
            doc = frappe.get_doc("User", email)
            doc.set("roles", [])
        else:
            doc = frappe.new_doc("User")
            doc.email = email
            doc.first_name = f"RoleTest {persona}"
            doc.send_welcome_email = 0
        doc.enabled = 1
        doc.user_type = "System User"
        doc.new_password = TEST_PASSWORD
        # Frappe refuses a password it considers weak unless told otherwise;
        # this one is a throwaway on a synthetic account that is deleted below.
        doc.flags.ignore_password_policy = True
        doc.flags.no_welcome_mail = True
        for role in spec["roles"]:
            if frappe.db.exists("Role", role):
                doc.append("roles", {"role": role})
        doc.save(ignore_permissions=True)
        ctx.users.append(email)

        # Branch scoping reads POS Profile links off the session user, so a
        # persona with no link would be refused everywhere for the wrong reason
        # and the matrix would read as "correctly locked down".
        for profile in (_pos_profiles_of(spec.get("template")) or _default_profiles()):
            prof = frappe.get_doc("POS Profile", profile)
            if not any(r.user == email for r in (prof.get("applicable_for_users") or [])):
                prof.append("applicable_for_users", {"user": email})
                prof.save(ignore_permissions=True)
                ctx.profile_links.append((profile, email))

        frappe.db.commit()
        ctx.record(f"provision.{persona}", True, f"{email} roles={spec['roles']}")
        return email
    except Exception as exc:
        frappe.db.rollback()
        ctx.record(f"provision.{persona}", False, f"{type(exc).__name__}: {exc}")
        return None


def _deprovision(ctx: RoleRunContext) -> None:
    """Remove the synthetic users and the POS Profile links this run added."""
    for profile, email in ctx.profile_links:
        try:
            prof = frappe.get_doc("POS Profile", profile)
            rows = [r for r in (prof.get("applicable_for_users") or []) if r.user != email]
            if len(rows) != len(prof.get("applicable_for_users") or []):
                prof.set("applicable_for_users", rows)
                prof.save(ignore_permissions=True)
        except Exception:
            frappe.log_error(frappe.get_traceback(), "role_matrix: profile unlink failed")

    for email in ctx.users:
        try:
            frappe.delete_doc(
                "User", email, ignore_permissions=True, ignore_missing=True,
                force=True, delete_permanently=True,
            )
        except Exception:
            frappe.log_error(frappe.get_traceback(), f"role_matrix: could not delete {email}")
    frappe.db.commit()


# ---------------------------------------------------------------------------
# HTTP transport
# ---------------------------------------------------------------------------

#: Tried in order. The harness runs inside the backend container, so the site is
#: reachable on the app server's own port without going out to the internet and
#: back — which also means the synthetic passwords never leave the host.
_BASE_URL_CANDIDATES = (
    "http://localhost:8000",
    "http://127.0.0.1:8000",
    "http://backend:8000",
)


class ApiSession:
    """A logged-in HTTP session for one persona.

    Thin on purpose. It exists to answer one question per call — *did the server
    allow this?* — so it never raises on an HTTP error status; it returns the
    status and lets the matrix decide whether that status was the right answer.
    """

    def __init__(self, base_url: str, site: str, user: str) -> None:
        import requests

        self.base_url = base_url
        self.user = user
        self.session = requests.Session()
        # Frappe resolves the site from the Host header. Without it a
        # multi-site bench serves (or refuses) the wrong site entirely.
        self.session.headers.update({"Host": site, "Accept": "application/json"})
        self.logged_in = False

    def login(self, password: str) -> Tuple[bool, str]:
        try:
            resp = self.session.post(
                f"{self.base_url}/api/method/login",
                json={"usr": self.user, "pwd": password},
                timeout=30,
            )
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"
        self.logged_in = resp.status_code == 200
        return self.logged_in, f"HTTP {resp.status_code} {resp.text[:200]}"

    def call(
        self, method: str, params: Optional[Dict[str, Any]] = None, http: str = "POST"
    ) -> Dict[str, Any]:
        """Call a whitelisted method. Returns status + payload, never raises."""
        url = f"{self.base_url}/api/method/{method}"
        try:
            if http.upper() == "GET":
                resp = self.session.get(url, params=params or {}, timeout=60)
            else:
                resp = self.session.post(url, json=params or {}, timeout=60)
        except Exception as exc:
            return {"status": -1, "error": f"{type(exc).__name__}: {exc}", "body": ""}
        body = resp.text or ""
        payload: Any = None
        try:
            payload = resp.json()
        except Exception:
            pass
        return {
            "status": resp.status_code,
            "body": body[:800],
            "payload": payload,
            "exc_type": (payload or {}).get("exc_type") if isinstance(payload, dict) else None,
        }

    def logout(self) -> None:
        try:
            self.session.get(f"{self.base_url}/api/method/logout", timeout=15)
        except Exception:
            pass


def _resolve_base_url(ctx: RoleRunContext) -> Optional[str]:
    """Find a base URL this container can actually reach.

    Reported as a check rather than assumed: if none of the candidates answer,
    every downstream verdict would be "denied" for a networking reason and the
    matrix would look like a perfectly locked-down system.
    """
    import requests

    site = frappe.local.site
    for base in _BASE_URL_CANDIDATES:
        try:
            resp = requests.get(
                f"{base}/api/method/ping", headers={"Host": site}, timeout=10
            )
            if resp.status_code in (200, 401, 403):
                ctx.record("transport.base_url", True, f"{base} answered HTTP {resp.status_code}")
                return base
        except Exception:
            continue
    ctx.record(
        "transport.base_url", False,
        f"none of {_BASE_URL_CANDIDATES} answered for site {site}; "
        "the matrix cannot distinguish 'denied' from 'unreachable'",
    )
    return None


# ---------------------------------------------------------------------------
# Verdict classification
# ---------------------------------------------------------------------------

ALLOW = "allow"
DENY = "deny"

#: Substrings that identify a refusal, as opposed to any other failure. Frappe
#: reports PermissionError through ``exc_type``; the app's own guards throw
#: ValidationError with these messages, so both have to be recognised.
_DENIAL_MARKERS = (
    "permissionerror",
    "not permitted",
    "managers only",
    "access required",
    "branchaccesserror",
    "you do not have access",
    "shiftrequirederror",
    "closedshifterror",
    "requires an open shift",
    "only managers",
    "only jarz manager",
)


def _classify(result: Dict[str, Any]) -> str:
    """Reduce a response to ``denied`` / ``allowed`` / ``unreachable``.

    The distinction that matters is **denied versus everything else**, not
    success versus failure. Most probes below deliberately call money-moving
    endpoints with arguments that cannot succeed, because actually settling a
    real invoice to prove a manager is allowed to would be an absurd price for
    the information. So a validation error, a missing-document error, a
    500 — all of them mean *the caller got past the gate*, which is exactly what
    an ALLOW expectation is asserting. Only an authorisation refusal is a deny.
    """
    if result.get("status") == -1:
        return "unreachable"
    if result.get("status") == 403:
        return "denied"
    blob = " ".join([
        str(result.get("exc_type") or ""),
        str(result.get("body") or ""),
    ]).lower()
    if any(marker in blob for marker in _DENIAL_MARKERS):
        return "denied"
    return "allowed"


class Probe:
    """One endpoint, and what each persona should get from it.

    ``expect`` maps persona -> ALLOW/DENY. A persona absent from the map is not
    asserted, which is how a probe stays honest about the cases it has not
    thought through rather than inventing an expectation for them.
    """

    def __init__(
        self,
        key: str,
        method: str,
        expect: Dict[str, str],
        params: Optional[Dict[str, Any]] = None,
        http: str = "POST",
        note: str = "",
        known_open: bool = False,
    ) -> None:
        self.key = key
        self.method = method
        self.expect = expect
        self.params = params or {}
        self.http = http
        self.note = note
        #: Set when this probe encodes a gap we already know about. It is still
        #: asserted — the point is that the report names it as a known hole
        #: rather than letting it read as a fresh surprise every run.
        self.known_open = known_open


# ---------------------------------------------------------------------------
# The matrix
# ---------------------------------------------------------------------------

#: A Sales Invoice name that certainly does not exist. Probes that must not have
#: side effects use it: the permission guard runs before the document is loaded,
#: so a denial still surfaces, while an allowed caller falls through to a
#: "not found" that :func:`_classify` reads as ``allowed``.
_NONEXISTENT_INVOICE = "_ROLETEST_NO_SUCH_INVOICE_"
_NONEXISTENT_PROFILE = "_ROLETEST_NO_SUCH_PROFILE_"


def _matrix() -> List[Probe]:
    """Every capability boundary worth asserting, per persona.

    Ordered by the journey a person actually performs rather than by module, so
    a failure reads as "staff cannot do X" instead of "endpoint Y returned Z".
    """
    return [
        # -- identity and bootstrap: everyone -----------------------------
        Probe("bootstrap.roles", "jarz_pos.api.user.get_current_user_roles",
              {"staff": ALLOW, "line_manager": ALLOW, "manager": ALLOW}),
        Probe("bootstrap.pos_profiles", "jarz_pos.api.pos.get_pos_profiles",
              {"staff": ALLOW, "line_manager": ALLOW, "manager": ALLOW}),
        Probe("bootstrap.receipt_config", "jarz_pos.api.pos.get_receipt_config",
              {"staff": ALLOW, "line_manager": ALLOW, "manager": ALLOW}),

        # -- pricing: line-manager tier and up ----------------------------
        Probe("pricing.price_lists", "jarz_pos.api.pos.get_pos_price_lists",
              {"staff": DENY, "line_manager": ALLOW, "manager": ALLOW},
              params={"profile": _NONEXISTENT_PROFILE},
              note="manager pricing gate (api/pos.py:22)"),
        Probe("pricing.commercial_policies", "jarz_pos.api.pos.get_commercial_policies",
              {"staff": DENY, "line_manager": ALLOW, "manager": ALLOW}),
        Probe("pricing.read_price_lists", "jarz_pos.api.price_lists.get_price_lists",
              {"staff": DENY, "line_manager": ALLOW, "manager": ALLOW}),
        Probe("pricing.write_create_list", "jarz_pos.api.price_lists.create_price_list",
              {"staff": DENY, "line_manager": DENY, "manager": ALLOW},
              params={"price_list_name": ""},
              note="writes need pricing AND B2B; a line manager is read-only here"),

        # -- cancel and return: the line-manager tier, and nobody below ---
        Probe("cancel.invoice", "jarz_pos.api.kanban.cancel_invoice",
              {"staff": DENY, "line_manager": ALLOW, "manager": ALLOW},
              params={"invoice_name": _NONEXISTENT_INVOICE},
              note="kanban.py:2132 - the regression that broke cancel for months"),
        Probe("returns.preview", "jarz_pos.api.returns.get_return_preview",
              {"staff": DENY, "line_manager": ALLOW, "manager": ALLOW},
              params={"invoice_name": _NONEXISTENT_INVOICE}),
        Probe("returns.submit", "jarz_pos.api.returns.submit_invoice_return",
              {"staff": DENY, "line_manager": ALLOW, "manager": ALLOW},
              params={"invoice_name": _NONEXISTENT_INVOICE, "items": "[]"}),

        # -- manager dashboard / shift monitor ----------------------------
        Probe("manager.dashboard", "jarz_pos.api.manager.get_manager_dashboard_summary",
              {"staff": DENY, "line_manager": ALLOW, "manager": ALLOW}),
        Probe("manager.shift_monitor", "jarz_pos.api.manager.get_pos_shift_monitor",
              {"staff": DENY, "line_manager": ALLOW, "manager": ALLOW}),
        Probe("manager.force_close_shift", "jarz_pos.api.shift.force_close_shift",
              {"staff": DENY, "line_manager": ALLOW, "manager": ALLOW},
              params={"pos_opening_entry": "_ROLETEST_NONE_", "reason": "role matrix probe"}),
        Probe("manager.master_orders", "jarz_pos.api.orders.get_master_orders",
              {"staff": DENY, "line_manager": ALLOW, "manager": ALLOW}),

        # -- reports: materials is the tier, the rest is JARZ Manager -----
        Probe("reports.materials", "jarz_pos.api.reports.get_materials_report",
              {"staff": DENY, "line_manager": ALLOW, "manager": ALLOW}),
        Probe("reports.final_products", "jarz_pos.api.reports.get_final_products_report",
              {"staff": DENY, "line_manager": DENY, "manager": ALLOW},
              note="_ensure_jarz_manager - the one tile a line manager may not open"),

        # -- the four sets the owner corrected on 2026-08-19 --------------
        # JARZ Manager was absent from ROLES.MANAGER / STOCK / PURCHASE /
        # MANUFACTURING while the drawer showed all four entries to them.
        Probe("ops.cash_transfer", "jarz_pos.api.cash_transfer.list_accounts",
              {"staff": DENY, "manager": ALLOW},
              note="ROLES.MANAGER - JARZ Manager added 2026-08-19"),
        Probe("ops.stock_transfer", "jarz_pos.api.transfer.list_pos_profiles",
              {"staff": DENY, "manager": ALLOW},
              note="ROLES.MANAGER"),
        Probe("ops.inventory_count", "jarz_pos.api.inventory_count.list_warehouses",
              {"staff": DENY, "manager": ALLOW},
              note="ROLES.STOCK"),
        Probe("ops.purchase_suppliers", "jarz_pos.api.purchase.get_suppliers",
              {"staff": DENY, "manager": ALLOW},
              note="ROLES.PURCHASE"),
        Probe("ops.manufacturing_boms", "jarz_pos.api.manufacturing.list_default_bom_items",
              {"staff": DENY, "manager": ALLOW},
              note="ROLES.MANUFACTURING - the Production Board item picker"),

        # -- expenses: anyone may raise, only a manager approves ----------
        Probe("expenses.bootstrap", "jarz_pos.api.expenses.get_expense_bootstrap",
              {"staff": ALLOW, "line_manager": ALLOW, "manager": ALLOW}),
        Probe("expenses.approve", "jarz_pos.api.expenses.approve_expense",
              {"staff": DENY, "line_manager": DENY, "manager": ALLOW},
              params={"name": "_ROLETEST_NONE_"},
              note="JARZ Manager only - a line manager may not approve"),

        # -- item requests: deliberately the widest set in the app --------
        Probe("purchase_request.list", "jarz_pos.api.purchase_request.list_requests",
              {"staff": ALLOW, "line_manager": ALLOW, "manager": ALLOW},
              note="floor staff must be able to file and see their own"),
        Probe("purchase_request.stop", "jarz_pos.api.purchase_request.stop_request",
              {"staff": DENY, "manager": ALLOW},
              params={"name": "_ROLETEST_NONE_"}),

        # -- B2B: walled off from the line manager by design --------------
        Probe("b2b.leads", "jarz_pos.api.leads.get_leads",
              {"staff": DENY, "line_manager": DENY, "manager": ALLOW}),
        Probe("b2b.labels", "jarz_pos.api.labels.get_label_settings",
              {"staff": DENY, "line_manager": DENY, "manager": ALLOW}),

        # -- custom shipping: request is open, approval is JARZ Manager ---
        Probe("shipping.approve", "jarz_pos.api.custom_shipping.approve_custom_shipping",
              {"staff": DENY, "line_manager": DENY, "manager": ALLOW},
              params={"invoice_name": _NONEXISTENT_INVOICE}),

        # -- settlement: guarded 2026-08-19, previously wide open ---------
        # These are the endpoints the mobile client drives for the whole
        # out-for-delivery pay-now flow. Both carried no guard at all.
        Probe("settlement.preview", "jarz_pos.api.couriers.generate_settlement_preview",
              {"staff": DENY, "line_manager": DENY, "manager": DENY},
              params={"invoice": _NONEXISTENT_INVOICE},
              note="branch-scoped: nobody may preview an invoice outside their branch"),
        Probe("settlement.confirm", "jarz_pos.api.couriers.confirm_settlement",
              {"staff": DENY, "line_manager": DENY, "manager": DENY},
              params={"invoice": _NONEXISTENT_INVOICE, "preview_token": "x", "mode": "pay_now"},
              note="branch + shift gated"),

        # -- the service twins the mobile client calls directly -----------
        # The app posts to jarz_pos.jarz_pos.services.delivery_handling.* rather
        # than the api/couriers.py wrappers, so the wrappers' branch + shift
        # guards never ran on these. Guarded INSIDE the service 2026-08-19 (the
        # whitelist cannot be removed — the shipped build calls them by name).
        # A DENY expectation that fails here means a twin went open again.
        Probe("twins.settle_delivery_party",
              "jarz_pos.jarz_pos.services.delivery_handling.settle_delivery_party",
              {"staff": DENY},
              params={"pos_profile": _NONEXISTENT_PROFILE},
              note="guarded in-service; also closes the pos_profile=None bypass"),
        Probe("twins.settle_courier",
              "jarz_pos.jarz_pos.services.delivery_handling.settle_courier",
              {"staff": DENY},
              params={"courier": "_ROLETEST_NONE_", "pos_profile": _NONEXISTENT_PROFILE},
              note="delegates to settle_delivery_party, inherits its guard"),
        Probe("twins.sales_partner_unpaid_ofd",
              "jarz_pos.jarz_pos.services.delivery_handling.sales_partner_unpaid_out_for_delivery",
              {"staff": DENY},
              params={"invoice_name": _NONEXISTENT_INVOICE, "pos_profile": _NONEXISTENT_PROFILE},
              note="the pos_profile branch check runs BEFORE the invoice is loaded, "
                   "which is what makes a nonexistent-invoice probe meaningful here"),
        Probe("twins.create_pos_invoice",
              "jarz_pos.jarz_pos.services.invoice_creation.create_pos_invoice",
              {"staff": DENY},
              params={"cart_json": "[]", "customer_name": "", "pos_profile_name": _NONEXISTENT_PROFILE},
              known_open=True,
              note="bypasses the branch+shift guard on api/invoices.py:105"),

        # -- cross-branch reads that leak the whole company ---------------
        Probe("leak.recent_invoices", "jarz_pos.api.notifications.get_recent_invoices",
              {"staff": DENY}, known_open=True,
              note="returns every POS invoice site-wide, unscoped"),
        Probe("leak.courier_balances", "jarz_pos.api.couriers.get_courier_balances",
              {"staff": DENY}, known_open=True),
        Probe("leak.branch_drawer", "jarz_pos.api.pos.get_pos_profile_account_balance",
              {"staff": DENY}, params={"pos_profile": _NONEXISTENT_PROFILE}, known_open=True,
              note="any branch's cash drawer balance"),

        # -- schema / data mutation: System Manager only since 2026-08-19 --
        Probe("debug.reload_doctypes", "jarz_pos.api.test_connection.reload_jarz_doctypes",
              {"staff": DENY, "line_manager": DENY},
              note="reloads DocTypes from disk (schema change)"),
        Probe("debug.bulk_write_invoices",
              "jarz_pos.api.test_kanban_setup.fix_existing_invoices_state",
              {"staff": DENY, "line_manager": DENY},
              note="bulk-writes submitted invoices"),
        Probe("debug.create_state_field",
              "jarz_pos.api.test_kanban_setup.create_sales_invoice_state_field",
              {"staff": DENY, "line_manager": DENY},
              note="inserts a Custom Field on Sales Invoice (schema change)"),
    ]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(cleanup: bool = True) -> Dict[str, Any]:
    if isinstance(cleanup, str):
        cleanup = cleanup.strip().lower() not in {"", "0", "false", "no"}

    _guard_environment()
    frappe.flags.in_test = True
    frappe.flags.ignore_woo_outbound = True

    ctx = RoleRunContext()
    sessions: Dict[str, ApiSession] = {}
    try:
        base_url = _resolve_base_url(ctx)
        if not base_url:
            return _summary(ctx, skipped_reason="no reachable base URL")

        personas = _derive_persona_roles(ctx)
        ctx.personas = personas
        site = frappe.local.site

        for persona, spec in personas.items():
            email = _provision_user(ctx, persona, spec)
            if not email:
                continue
            session = ApiSession(base_url, site, email)
            ok, detail = session.login(TEST_PASSWORD)
            ctx.record(f"login.{persona}", ok, detail)
            if ok:
                sessions[persona] = session

        if not sessions:
            return _summary(ctx, skipped_reason="no persona could log in")

        for probe in _matrix():
            for persona, expected in probe.expect.items():
                session = sessions.get(persona)
                if not session:
                    continue
                result = session.call(probe.method, probe.params, http=probe.http)
                verdict = _classify(result)

                if verdict == "unreachable":
                    ctx.record(f"{probe.key}[{persona}]", False,
                               f"transport failure: {result.get('error')}")
                    continue

                passed = (verdict == "denied") if expected == DENY else (verdict != "denied")
                detail = f"expected={expected} got={verdict} http={result.get('status')}"
                if probe.note:
                    detail += f" :: {probe.note}"
                if not passed and probe.known_open:
                    detail += " :: KNOWN OPEN - this gap is already on the fix list"
                ctx.record(f"{probe.key}[{persona}]", passed, detail)

        return _summary(ctx)
    finally:
        for session in sessions.values():
            session.logout()
        if cleanup:
            try:
                _deprovision(ctx)
            except Exception:
                frappe.log_error(frappe.get_traceback(), "role_matrix: teardown failed")


def _summary(ctx: RoleRunContext, skipped_reason: str = "") -> Dict[str, Any]:
    summary = {
        "passed": ctx.passed,
        "failed": ctx.failed,
        "skipped": ctx.skipped,
        "checks": ctx.checks,
        "personas": {
            k: {"template": v.get("template"), "roles": v.get("roles")}
            for k, v in ctx.personas.items()
        },
    }
    if skipped_reason:
        summary["note"] = skipped_reason
    print(
        f"role_matrix_validation: {ctx.passed} passed, {ctx.failed} failed, "
        f"{ctx.skipped} skipped"
    )
    print(MARKER_START)
    print(json.dumps(summary, indent=2, default=str))
    print(MARKER_END)
    return summary
