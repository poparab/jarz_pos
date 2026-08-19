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

REAL DOCUMENTS, NOT PLACEHOLDER IDs
-----------------------------------
The first version of this matrix called every document-scoped endpoint with a
deliberately nonexistent invoice/profile id. That reduced 21 of its results to
noise, in both directions:

* where the guard runs *after* the document is loaded, the endpoint threw "not
  found" before the permission check ever ran — and a harness that reads any
  non-refusal as "allowed" scored that as the caller being let in, failing a
  DENY expectation the gate had never been asked about;
* where the guard runs *before* but needs the branch off the document, "no
  document" meant "no branch", and the refusal that produced looked exactly
  like an authorisation denial — failing an ALLOW expectation.

Both are fixed the same way: :func:`_build_fixtures` creates real submitted
Sales Invoices — one on the personas' **own** branch, one on a **different**
enabled branch — plus a Custom Shipping Request, and the probes call the real
endpoints with them. Branch scoping is the whole point of most of these gates,
and it can only be proven by calling with a document that genuinely belongs to
a branch the caller is not assigned to. Sites with a single enabled POS Profile
cannot host that assertion, so those probes SKIP with that reason rather than
reporting a pass nobody earned.

Fixtures are created through ``b2b_accounting_validation``'s helpers, so they
land under the ``_B2BVALID_`` prefix that ``purge_test_fixtures`` sweeps —
grep for ``_B2BVALID_``, not for this file's name, when hunting residue.

Synthetic users are prefixed ``_ROLETEST_``. Users, profile links, fixtures and
the shipping request are all removed in ``finally``. Refuses to run against
production.

Run::

    bench --site frontend execute jarz_pos.scripts.role_matrix_validation.run
    bench --site frontend execute jarz_pos.scripts.role_matrix_validation.run \
        --kwargs "{'cleanup': False}"
"""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import frappe

from jarz_pos.constants import ROLES

# Fixture helpers are reused verbatim from the accounting harness — same
# prefixes, same cleanup, same creation path the POS itself uses — exactly as
# ``full_lifecycle_validation`` does. Writing a second invoice factory here
# would be a second thing to keep true.
from jarz_pos.scripts.b2b_accounting_validation import (
    RunContext as FixtureContext,
    _cleanup as _cleanup_fixtures,
    _create_invoice,
    _ensure_customer,
    _ensure_customer_group,
    _ensure_item_prices,
    _ensure_stock,
    _ensure_territory,
    _pick_pos_profile,
    _pick_sellable_items,
)

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

#: Reason text reused by every probe that needs a second branch to mean
#: anything. Stated once so the report reads identically wherever it appears.
_NO_SECOND_BRANCH = (
    "this site has exactly one enabled POS Profile, so there is no branch the "
    "personas are NOT assigned to. A cross-branch denial cannot be staged, and "
    "asserting the gate against a made-up branch id would prove nothing about "
    "real branch scoping"
)


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
        #: Sales Invoices / Stock Entries / Item Prices created for this run.
        #: Owned by ``b2b_accounting_validation._cleanup``.
        self.fixtures: FixtureContext = FixtureContext()
        #: Custom Shipping Requests, which ``_cleanup`` does not know about and
        #: which must be removed BEFORE their invoice or the delete is refused.
        self.shipping_requests: List[str] = []

    def record(self, name: str, passed: bool, detail: str = "") -> None:
        self.checks.append(
            {"name": name, "passed": bool(passed), "detail": detail, "skipped": False}
        )
        print(f"   [{'PASS' if passed else 'FAIL'}] {name}" + (f" :: {detail}" if detail else ""))

    def skip(self, name: str, reason: str) -> None:
        """A check that did not run because the environment could not host it.

        Same third state the accounting harnesses use, and here for the same
        reason: scoring an un-run check as a pass is how a suite reports green
        having proven nothing. Three things land here:

        * a persona with no real user to copy from;
        * a probe that needs a second branch on a single-branch site;
        * a call whose failure was **inconclusive** — it broke for a reason
          unrelated to authorisation (document missing, argument missing,
          feature flag off), so it says nothing about the gate in either
          direction. That third case is the one this harness used to score as a
          pass or a fail depending only on which way the expectation pointed.
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

def _enabled_profiles() -> List[str]:
    return sorted(
        frappe.get_all("POS Profile", filters={"disabled": 0}, pluck="name", limit_page_length=0)
        or []
    )


def _provision_user(
    ctx: RoleRunContext, persona: str, spec: Dict[str, Any], own_profile: Optional[str]
) -> Optional[str]:
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

        _pin_user_to_branch(ctx, email, own_profile)

        frappe.db.commit()
        ctx.record(
            f"provision.{persona}", True,
            f"{email} roles={spec['roles']} branch={own_profile}",
        )
        return email
    except Exception as exc:
        frappe.db.rollback()
        ctx.record(f"provision.{persona}", False, f"{type(exc).__name__}: {exc}")
        return None


def _pin_user_to_branch(
    ctx: RoleRunContext, email: str, own_profile: Optional[str]
) -> None:
    """Assign the synthetic user to exactly ONE branch.

    Branch scoping reads POS Profile links off the session user, so a persona
    with no link would be refused everywhere for the wrong reason and the matrix
    would read as "correctly locked down".

    Exactly one, rather than "whatever the template user has", because the
    cross-branch probes need an unambiguous answer to *which branch is not
    theirs*. A persona linked to two branches on a two-branch site has no
    outside, and every cross-branch assertion below would silently degrade into
    testing nothing. Roles still come from the real template user; only the
    branch is chosen by the harness.
    """
    if not own_profile:
        return
    for profile in _enabled_profiles():
        try:
            prof = frappe.get_doc("POS Profile", profile)
            rows = list(prof.get("applicable_for_users") or [])
            linked = any(r.user == email for r in rows)
            if profile == own_profile:
                if not linked:
                    prof.append("applicable_for_users", {"user": email})
                    prof.save(ignore_permissions=True)
                    ctx.profile_links.append((profile, email))
            elif linked:
                # Residue from an interrupted run. The email is synthetic, so
                # any link to it is ours to remove.
                prof.set("applicable_for_users", [r for r in rows if r.user != email])
                prof.save(ignore_permissions=True)
        except Exception:
            frappe.log_error(frappe.get_traceback(), "role_matrix: profile pin failed")


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
# Fixtures — the real documents the probes act on
# ---------------------------------------------------------------------------

def _fixture_invoice(ctx: RoleRunContext, profile: str, suffix: str) -> str:
    """One submitted POS Sales Invoice on *profile*, created the way POS does.

    Goes through ``services.invoice_creation.create_pos_invoice`` (via the
    accounting harness's ``_create_invoice``) rather than a hand-built document,
    so the branch fields the gates read — ``custom_kanban_profile`` first,
    ``pos_profile`` as the fallback — are populated exactly as they are in
    production.
    """
    company = frappe.db.get_value("POS Profile", profile, "company")
    items = _pick_sellable_items(profile, 2)
    territory = _ensure_territory(company)
    group = _ensure_customer_group()
    customer = _ensure_customer(ctx.fixtures, f"ROLEMATRIX-{suffix}", territory, group)
    return _create_invoice(
        ctx.fixtures,
        customer=customer,
        pos_profile=profile,
        items=items,
        order_purpose=None,
        commercial_policy=None,
        policy_reason=None,
        pickup=False,
    )


def _seed_branch_stock(ctx: RoleRunContext, profile: str) -> None:
    """Prices + stock for the items this branch will invoice. Once per branch."""
    items = _pick_sellable_items(profile, 2)
    _ensure_item_prices(ctx.fixtures, items)
    _ensure_stock(ctx.fixtures, items, profile)


def _latest_opening_entry(profile: str) -> Optional[str]:
    """The most recent submitted POS Opening Entry on *profile*, if any.

    Read, never created: opening a shift here would take the branch offline for
    real staff (one open shift per profile) and, per ``start_shift``, would book
    any drawer difference to Cash Over/Short. The force-close probes only need a
    document whose ``pos_profile`` is real — every assertion they make is
    answered by a gate that runs before anything is written.
    """
    rows = frappe.get_all(
        "POS Opening Entry",
        filters={"pos_profile": profile, "docstatus": 1},
        fields=["name"],
        order_by="period_start_date desc, modified desc",
        limit=1,
    ) or []
    return rows[0]["name"] if rows else None


def _existing_price_list(profile: str) -> Optional[str]:
    """An enabled selling Price List that already exists.

    ``create_price_list`` is get-or-create: handed a name that exists it returns
    it untouched. That turns the manager's ALLOW expectation into a real,
    side-effect-free proof — the call genuinely succeeds — instead of the old
    probe's empty name, which only ever reached "price_list_name is required".
    """
    candidate = frappe.db.get_value("POS Profile", profile, "selling_price_list")
    if candidate and frappe.db.exists("Price List", candidate):
        return candidate
    return frappe.db.get_value("Price List", {"enabled": 1, "selling": 1}, "name")


def _shift_gate_reason(own_profile: str, emails: Sequence[str]) -> Optional[str]:
    """Why a shift-gated ALLOW probe cannot be trusted in this environment.

    ``_guard_invoice_action`` applies branch scoping AND, for the posting
    endpoints, an open-shift requirement. Both raise something this harness
    classifies as a denial, so on a branch with no open shift a shift refusal
    would masquerade as a branch refusal and fail an ALLOW expectation that has
    nothing to do with shifts.
    """
    try:
        from jarz_pos.utils.access_control import (
            get_open_shift_for_profile,
            user_requires_pos_shift,
        )
    except Exception:
        return None
    needy = [e for e in emails if e and user_requires_pos_shift(e)]
    if not needy:
        return None
    if get_open_shift_for_profile(own_profile):
        return None
    return (
        f"{len(needy)} persona(s) carry custom_require_pos_shift and branch "
        f"{own_profile} has no open shift, so this endpoint's SHIFT gate would "
        "answer before its branch gate; the branch scoping this probe exists to "
        "prove would go untested and the refusal would read as a false failure"
    )


def _build_fixtures(ctx: RoleRunContext) -> Dict[str, Any]:
    """Create every document the matrix needs, and say what could not be made.

    Returns the ``env`` the matrix is built from. Anything it could not create
    is left ``None``; :func:`_matrix` turns each absence into a SKIP carrying the
    reason, never into a pass and never into a failure of the gate under test.
    """
    env: Dict[str, Any] = {
        "own_profile": None,
        "other_profile": None,
        "own_invoice": None,
        "other_invoice": None,
        "cancelable": {},
        "shipping_invoice": None,
        "shipping_request": None,
        "own_opening": None,
        "other_opening": None,
        "price_list": None,
        "persona_profiles": {},
        "shift_gate_reason": None,
    }

    own = _pick_pos_profile()
    env["own_profile"] = own
    others = [p for p in _enabled_profiles() if p != own]
    env["other_profile"] = others[0] if others else None

    if env["other_profile"]:
        ctx.record(
            "fixture.second_branch_available", True,
            f"own branch={own}, other branch={env['other_profile']} — cross-branch "
            "denials can be staged against a real document",
        )
    else:
        ctx.skip("fixture.second_branch_available", _NO_SECOND_BRANCH)

    env["price_list"] = _existing_price_list(own)

    # --- own-branch documents -------------------------------------------
    try:
        _seed_branch_stock(ctx, own)
        env["own_invoice"] = _fixture_invoice(ctx, own, "OWN")
        env["shipping_invoice"] = _fixture_invoice(ctx, own, "SHIP")
        # One cancellable invoice PER persona: cancelling is the only honest way
        # to prove the tier may cancel, and a cancelled invoice cannot serve the
        # next persona. Staff gets one too — if the role gate ever regresses,
        # the damage lands on a fixture instead of on the invoice every later
        # probe reads.
        for persona in ("staff", "line_manager", "manager"):
            env["cancelable"][persona] = _fixture_invoice(ctx, own, f"CANCEL-{persona}")
        frappe.db.commit()
        ctx.record(
            "fixture.own_branch_invoices", True,
            f"branch={own} read={env['own_invoice']} shipping={env['shipping_invoice']} "
            f"cancellable={env['cancelable']}",
        )
    except Exception as exc:
        frappe.db.rollback()
        # A partial set is worse than none: a probe handed one persona's fixture
        # and None for the next reads as half a verdict. Whatever was created is
        # still tracked for cleanup.
        env["cancelable"] = {}
        # Not a skip: a site where the POS cannot create an invoice at all is a
        # real problem, and every document-scoped verdict below is unavailable.
        ctx.record(
            "fixture.own_branch_invoices", False,
            f"{type(exc).__name__}: {exc} — no complete own-branch fixture set exists, "
            "so every probe that needs one is skipped below",
        )

    # --- other-branch document ------------------------------------------
    if env["other_profile"]:
        try:
            _seed_branch_stock(ctx, env["other_profile"])
            env["other_invoice"] = _fixture_invoice(ctx, env["other_profile"], "OTHER")
            frappe.db.commit()
            ctx.record(
                "fixture.other_branch_invoice", True,
                f"{env['other_invoice']} on {env['other_profile']} — the document every "
                "cross-branch DENY expectation is asserted against",
            )
        except Exception as exc:
            frappe.db.rollback()
            ctx.record(
                "fixture.other_branch_invoice", False,
                f"{type(exc).__name__}: {exc} — cross-branch probes cannot run",
            )
    else:
        ctx.skip("fixture.other_branch_invoice", _NO_SECOND_BRANCH)

    # --- a real Custom Shipping Request ---------------------------------
    if env["shipping_invoice"]:
        try:
            from jarz_pos.api.custom_shipping import request_custom_shipping

            res = request_custom_shipping(
                invoice_name=env["shipping_invoice"],
                amount=25.0,
                reason="Role matrix harness fixture — approval gate probe",
            )
            env["shipping_request"] = (res or {}).get("request")
            ctx.record(
                "fixture.custom_shipping_request", bool(env["shipping_request"]),
                f"{env['shipping_request']} on {env['shipping_invoice']}",
            )
            if env["shipping_request"]:
                ctx.shipping_requests.append(env["shipping_request"])
        except Exception as exc:
            ctx.skip(
                "fixture.custom_shipping_request",
                f"could not raise a request ({type(exc).__name__}: {exc}); the approval "
                "probe falls back to asserting the role gate alone",
            )
    else:
        ctx.skip(
            "fixture.custom_shipping_request",
            "no own-branch invoice to raise a request against",
        )

    # --- shifts (read only) ---------------------------------------------
    env["own_opening"] = _latest_opening_entry(own)
    if env["other_profile"]:
        env["other_opening"] = _latest_opening_entry(env["other_profile"])

    return env


def _cleanup_shipping_requests(ctx: RoleRunContext) -> None:
    """Remove the Custom Shipping Requests first — the invoice delete needs it.

    A submitted request links the Sales Invoice, and ``_cleanup`` would refuse
    to delete the invoice underneath it.
    """
    for name in list(dict.fromkeys(ctx.shipping_requests)):
        try:
            doc = frappe.get_doc("Custom Shipping Request", name)
            doc.flags.ignore_permissions = True
            if int(getattr(doc, "docstatus", 0) or 0) == 1:
                doc.flags.ignore_links = True
                doc.cancel()
            frappe.delete_doc(
                "Custom Shipping Request", name, force=True,
                ignore_permissions=True, delete_permanently=True,
            )
        except frappe.DoesNotExistError:
            continue
        except Exception:
            frappe.log_error(
                frappe.get_traceback(), f"role_matrix: could not remove CSR {name}"
            )
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

#: The four things a response can mean.
DENIED = "denied"
ALLOWED = "allowed"
INCONCLUSIVE = "inconclusive"
UNREACHABLE = "unreachable"

#: Substrings that identify a refusal, as opposed to any other failure. Frappe
#: reports PermissionError through ``exc_type``; the app's own guards throw
#: ValidationError with these messages, so both have to be recognised. Several
#: endpoints (``kanban.cancel_invoice``, ``services.invoice_return``) return
#: their refusal as an HTTP 200 payload, which is why the body is searched too.
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
    "limited to the branches you are assigned",
    "not assigned to any branch",
)

#: Exception classes that mean "the call broke before, or independently of, any
#: authorisation decision".
_INCONCLUSIVE_EXC_TYPES = (
    "doesnotexisterror",
    "linkvalidationerror",
)

#: Message shapes that mean the same thing. Deliberately narrow: every entry is
#: a phrase a Frappe/ERPNext validation produces for a MISSING or UNRESOLVABLE
#: input, never for a refusal. Anything broader would start swallowing real
#: denials, which is the failure mode this whole classification exists to avoid.
_INCONCLUSIVE_MARKERS = (
    "not found",
    "does not exist",
    "is required",
    "are required",
    "required argument",
    "unexpected keyword argument",
    "missing 1 required",
    "no such",
    "not enabled",
    "could not be found",
)


def _looks_like_failure(result: Dict[str, Any]) -> bool:
    """True when the response is the server saying no, for whatever reason.

    Both marker lists are substring searches over the response body, and a
    successful payload is full of somebody else's free text — a lead note
    reading "the invoice is required before delivery" would otherwise be read
    as a missing-argument error and turn a genuine PASS into a SKIP. So the
    markers are only consulted when the response is a failure in the first
    place: an error status, a Frappe exception envelope, or one of the
    endpoints that answers HTTP 200 with ``success: False``.
    """
    status = result.get("status") or 0
    if isinstance(status, int) and status >= 400:
        return True
    payload = result.get("payload")
    if isinstance(payload, dict):
        if payload.get("exc_type") or payload.get("exception") or payload.get("_server_messages"):
            return True
        message = payload.get("message")
        if isinstance(message, dict) and (message.get("success") is False or message.get("error")):
            return True
    return False


def _classify(result: Dict[str, Any]) -> str:
    """Reduce a response to denied / allowed / inconclusive / unreachable.

    The distinction that matters is **denied versus allowed**, and the reason
    ``inconclusive`` exists is that the old two-way split silently invented one
    of those two answers for every call that failed for a third reason.

    Order is the whole design:

    1. transport failure — nothing was asked, let alone answered;
    2. **denial wins over everything else.** A body that carries both a denial
       marker and a not-found marker ("Not permitted: … this order belongs to
       BranchB" alongside a stale link error) is a denial. Checking denials
       first is what stops the inconclusive rule from swallowing real refusals;
    3. inconclusive — a missing document, a missing argument, a feature flag
       that is off, a 500. None of those is an authorisation answer, and the
       caller records them as SKIPs;
    4. anything else — including a validation error raised *after* the gate —
       means the caller got through.

    HTTP 500 with no denial marker is inconclusive by status alone: Frappe maps
    PermissionError to 403 and its own ``throw`` to 417, so a 500 is a crash,
    and a crash is not a permission verdict. 404 likewise — it is usually a
    method that does not exist, which is how a probe pointed at a renamed
    endpoint used to read as "the manager was allowed".
    """
    status = result.get("status")
    if status == -1:
        return UNREACHABLE
    if status == 403:
        return DENIED

    failed = _looks_like_failure(result)
    exc = str(result.get("exc_type") or "").strip().lower()
    blob = " ".join([exc, str(result.get("body") or "")]).lower()

    if failed and any(marker in blob for marker in _DENIAL_MARKERS):
        return DENIED
    if exc in _INCONCLUSIVE_EXC_TYPES:
        return INCONCLUSIVE
    if status in (404, 500):
        return INCONCLUSIVE
    if failed and any(marker in blob for marker in _INCONCLUSIVE_MARKERS):
        return INCONCLUSIVE
    return ALLOWED


def _inconclusive_reason(result: Dict[str, Any]) -> str:
    """The specific thing that made a response inconclusive, for the report."""
    status = result.get("status")
    exc = str(result.get("exc_type") or "").strip()
    if exc.lower() in _INCONCLUSIVE_EXC_TYPES:
        return f"{exc} — the document or link named in the call does not exist"
    if status in (404, 500):
        return (
            f"HTTP {status} — Frappe answers a refusal with 403 or 417, so this is a "
            "missing endpoint or a crash, not a permission verdict"
        )
    blob = str(result.get("body") or "").lower()
    for marker in _INCONCLUSIVE_MARKERS:
        if marker in blob:
            return f"the endpoint answered {marker!r} — an input problem, not a refusal"
    return "the response matched no authorisation outcome"


def _message(result: Dict[str, Any]) -> Any:
    """The whitelisted method's own return value, unwrapped from Frappe's envelope."""
    payload = result.get("payload")
    if isinstance(payload, dict) and "message" in payload:
        return payload["message"]
    return payload


# ---------------------------------------------------------------------------
# Content proofs for the cross-branch read leaks
# ---------------------------------------------------------------------------

def _foreign_invoices(names: Iterable[Any], allowed: Sequence[str]) -> Dict[str, str]:
    """Which of *names* belong to a branch outside *allowed*, and to which one.

    Resolved server-side from the same fields the gates read, so the answer is
    the branch the app itself would use, not a guess from the response text.
    """
    wanted = [str(n) for n in dict.fromkeys(names) if n]
    if not wanted:
        return {}
    rows = frappe.get_all(
        "Sales Invoice",
        filters={"name": ["in", wanted]},
        fields=["name", "custom_kanban_profile", "pos_profile"],
        limit_page_length=0,
    ) or []
    allowed_set = {str(p).strip() for p in allowed if str(p or "").strip()}
    foreign: Dict[str, str] = {}
    for row in rows:
        branch = str(row.get("custom_kanban_profile") or row.get("pos_profile") or "").strip()
        if branch and branch not in allowed_set:
            foreign[row["name"]] = branch
    return foreign


def _proof_recent_invoices(message: Any, allowed: Sequence[str]) -> Optional[str]:
    """HTTP 200 is not the finding — *whose orders came back* is."""
    if not isinstance(message, dict):
        return None
    names = [
        row.get("name")
        for group in ("new_invoices", "modified_invoices")
        for row in (message.get(group) or [])
        if isinstance(row, dict)
    ]
    foreign = _foreign_invoices(names, allowed)
    if not foreign:
        return None
    sample = ", ".join(f"{inv} (branch {branch})" for inv, branch in sorted(foreign.items())[:5])
    return (
        f"the body carries {len(foreign)} of {len(names)} returned invoice(s) from "
        f"branch(es) {sorted(set(foreign.values()))} this persona is not assigned to: {sample}"
    )


def _proof_courier_balances(message: Any, allowed: Sequence[str]) -> Optional[str]:
    """Same shape of claim, over the invoices embedded in each courier's rows."""
    if not isinstance(message, list):
        return None
    names = [
        detail.get("invoice")
        for group in message
        if isinstance(group, dict)
        for detail in (group.get("details") or [])
        if isinstance(detail, dict)
    ]
    foreign = _foreign_invoices(names, allowed)
    if not foreign:
        return None
    sample = ", ".join(f"{inv} (branch {branch})" for inv, branch in sorted(foreign.items())[:5])
    return (
        f"the body carries courier debt for {len(foreign)} invoice(s) on branch(es) "
        f"{sorted(set(foreign.values()))} this persona is not assigned to: {sample}"
    )


def _proof_branch_drawer(message: Any, allowed: Sequence[str]) -> Optional[str]:
    """The drawer balance of a branch the caller does not work at."""
    if not isinstance(message, dict):
        return None
    profile = str(message.get("profile") or "").strip()
    allowed_set = {str(p).strip() for p in allowed if str(p or "").strip()}
    if not profile or profile in allowed_set or "balance" not in message:
        return None
    return (
        f"the body carries the live cash position of branch {profile} "
        f"(account {message.get('account')}, balance {message.get('balance')} "
        f"{message.get('currency')}), which this persona is not assigned to"
    )


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------

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
        gate_is_first: bool = False,
        persona_params: Optional[Dict[str, Dict[str, Any]]] = None,
        leak_proof: Optional[Callable[[Any, Sequence[str]], Optional[str]]] = None,
        skip_reason: str = "",
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
        #: Set when, **for the arguments this probe sends**, the authorisation
        #: gate it asserts is the first thing the endpoint evaluates — verified
        #: against the source and cited in ``note``. Then, and only then, an
        #: inconclusive outcome is evidence in itself: nothing before the gate
        #: could have produced it, so the caller must have passed the gate. It
        #: is folded into ``allowed`` — which satisfies an ALLOW expectation and
        #: *fails* a DENY one, because a gate that lets a caller reach the
        #: endpoint's own argument validation did not refuse them.
        #:
        #: This is the opposite of the blanket "any error means allowed" rule
        #: that made the first version of this matrix useless: it is a per-probe,
        #: source-checked claim about one endpoint, not a default.
        self.gate_is_first = gate_is_first
        #: Per-persona argument overrides — used where each persona needs its
        #: own document (cancelling consumes the invoice it is given).
        self.persona_params = persona_params or {}
        #: For DENY probes that the server answers with HTTP 200: given the
        #: response body, return proof that it carried data from outside the
        #: persona's branches, or None. Without proof the probe SKIPs — a 200
        #: on its own does not establish a leak.
        self.leak_proof = leak_proof
        #: Set when the environment cannot host this probe at all.
        self.skip_reason = skip_reason

    def params_for(self, persona: str) -> Dict[str, Any]:
        return dict(self.persona_params.get(persona) or self.params)


def _needs(env: Dict[str, Any], *keys: str) -> str:
    """Skip reason when a fixture this probe depends on could not be created."""
    missing = [k for k in keys if not env.get(k)]
    if not missing:
        return ""
    if any(k in {"other_profile", "other_invoice", "other_opening"} for k in missing):
        return f"{_NO_SECOND_BRANCH} (missing: {', '.join(missing)})"
    return (
        f"the fixture(s) {', '.join(missing)} could not be created or found on this site, "
        "so this probe has no real document to act on; asserting it against a made-up id "
        "would only measure the endpoint's not-found path, which is what made this "
        "harness's failures meaningless in the first place"
    )


# ---------------------------------------------------------------------------
# The matrix
# ---------------------------------------------------------------------------

#: Deliberately unloadable arguments. They survive only where the gate under
#: test provably runs BEFORE the lookup (``gate_is_first``), and there they do
#: real work: they make the call inert, so a probe cannot post money even if the
#: guard it is testing has regressed open.
_NONEXISTENT_INVOICE = "_ROLETEST_NO_SUCH_INVOICE_"
_NONEXISTENT_PROFILE = "_ROLETEST_NO_SUCH_PROFILE_"
_NONEXISTENT_PARTY = "_ROLETEST_NO_SUCH_PARTY_"
_NONEXISTENT_ROW = "_ROLETEST_NO_SUCH_ROW_"


def _matrix(env: Dict[str, Any]) -> List[Probe]:
    """Every capability boundary worth asserting, per persona.

    Ordered by the journey a person actually performs rather than by module, so
    a failure reads as "staff cannot do X" instead of "endpoint Y returned Z".
    """
    own_profile = env.get("own_profile")
    other_profile = env.get("other_profile")
    own_invoice = env.get("own_invoice")
    other_invoice = env.get("other_invoice")
    cancelable: Dict[str, str] = env.get("cancelable") or {}
    own_opening = env.get("own_opening")
    other_opening = env.get("other_opening")

    #: The branch id handed to the *profile-scoped* twins. A real second branch
    #: is the strong form of the claim; where the site has none, a profile that
    #: does not exist is still, unambiguously, "not one of your branches" — the
    #: guard is reached either way because it runs before any lookup.
    twin_profile = other_profile or _NONEXISTENT_PROFILE

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
              params={"profile": own_profile},
              skip_reason=_needs(env, "own_profile"),
              note="manager pricing gate (api/pos.py:129). The profile must be REAL: "
                   "assert_pos_profile_enabled runs on line 128, one line BEFORE the "
                   "gate, so the old made-up profile id answered 417 without the gate "
                   "ever being consulted"),
        Probe("pricing.commercial_policies", "jarz_pos.api.pos.get_commercial_policies",
              {"staff": DENY, "line_manager": ALLOW, "manager": ALLOW}),
        Probe("pricing.read_price_lists", "jarz_pos.api.price_lists.get_price_lists",
              {"staff": DENY, "line_manager": ALLOW, "manager": ALLOW}),
        Probe("pricing.write_create_list", "jarz_pos.api.price_lists.create_price_list",
              {"staff": DENY, "line_manager": DENY, "manager": ALLOW},
              params={"price_list_name": env.get("price_list")},
              skip_reason=_needs(env, "price_list"),
              note="writes need pricing AND B2B; a line manager is read-only here. "
                   "create_price_list is get-or-create, so handing it an existing list "
                   "makes the manager's ALLOW a real success rather than the old empty "
                   "name, which only ever reached 'price_list_name is required'"),

        # -- cancel: the line-manager tier, and nobody below --------------
        Probe("cancel.invoice.own_branch", "jarz_pos.api.kanban.cancel_invoice",
              # staff first: the role gate must refuse before the tier personas
              # consume their own fixtures.
              {"staff": DENY, "line_manager": ALLOW, "manager": ALLOW},
              persona_params={
                  persona: {"invoice_id": cancelable.get(persona), "reason": "role matrix probe"}
                  for persona in ("staff", "line_manager", "manager")
              },
              skip_reason=_needs(env, "cancelable"),
              note="kanban.py:2132 - the regression that broke cancel for months. Each "
                   "persona gets its OWN submitted fixture: the tier personas really do "
                   "cancel one, which is the only honest proof they may. The argument is "
                   "invoice_id, not invoice_name — the old probe's keyword did not exist "
                   "on the endpoint and 500'd before any gate"),
        Probe("cancel.invoice.other_branch", "jarz_pos.api.kanban.cancel_invoice",
              {"line_manager": DENY, "manager": DENY},
              params={"invoice_id": other_invoice, "reason": "role matrix cross-branch probe"},
              skip_reason=_needs(env, "other_invoice"),
              note="kanban.py:2138 ensure_profile_scoped_invoice_access — holding the "
                   "cancel role is not permission to cancel ANY branch's order"),

        # -- returns: same tier, plus branch scoping ----------------------
        Probe("returns.preview.own_branch", "jarz_pos.api.returns.get_return_preview",
              {"staff": DENY, "line_manager": ALLOW, "manager": ALLOW},
              params={"invoice_id": own_invoice},
              skip_reason=_needs(env, "own_invoice"),
              note="read-only twin of submit_invoice_return; same role gate "
                   "(returns.py:31) plus the branch check inside build_return_preview"),
        Probe("returns.preview.other_branch", "jarz_pos.api.returns.get_return_preview",
              {"line_manager": DENY, "manager": DENY},
              params={"invoice_id": other_invoice},
              skip_reason=_needs(env, "other_invoice"),
              note="invoice_return.py:431 — the branch gate, asserted with a document "
                   "that really belongs to another branch"),
        Probe("returns.submit.role_gate", "jarz_pos.api.returns.submit_invoice_return",
              {"staff": DENY, "line_manager": ALLOW, "manager": ALLOW},
              params={"invoice_id": own_invoice, "lines": "[]", "reason": "role matrix probe"},
              gate_is_first=True,
              skip_reason=_needs(env, "own_invoice"),
              note="_ensure_return_permission is the FIRST statement (returns.py:58), so "
                   "reaching the service's own 'select at least one line' / disabled-flag "
                   "answer proves the tier passed the role gate. Empty lines keep the "
                   "call unpostable"),
        Probe("returns.submit.other_branch", "jarz_pos.api.returns.submit_invoice_return",
              {"line_manager": DENY, "manager": DENY},
              params={
                  "invoice_id": other_invoice,
                  # Non-empty so the branch check is reached (empty lines are
                  # rejected first), but unresolvable so nothing can post even
                  # if the branch gate has regressed open.
                  "lines": json.dumps([{"si_detail": _NONEXISTENT_ROW, "qty": 1}]),
                  "reason": "role matrix cross-branch probe",
              },
              skip_reason=_needs(env, "other_invoice"),
              note="invoice_return.py:1239. NOT gate_is_first: returns_enabled() is "
                   "checked before the branch gate, so on a site with the workflow off "
                   "this SKIPs as inconclusive instead of inventing a verdict"),

        # -- manager dashboard / shift monitor ----------------------------
        Probe("manager.dashboard", "jarz_pos.api.manager.get_manager_dashboard_summary",
              {"staff": DENY, "line_manager": ALLOW, "manager": ALLOW}),
        Probe("manager.shift_monitor", "jarz_pos.api.manager.get_pos_shift_monitor",
              {"staff": DENY, "line_manager": ALLOW, "manager": ALLOW}),
        Probe("manager.force_close_preview.own_branch",
              "jarz_pos.api.shift.get_force_close_preview",
              {"staff": DENY, "line_manager": ALLOW, "manager": ALLOW},
              params={"pos_opening_entry": own_opening},
              skip_reason=_needs(env, "own_opening"),
              note="shift.py:1296-1301 — the read-only twin of force_close_shift, "
                   "carrying the identical role-then-branch gate pair. This is where the "
                   "tier's ALLOW is asserted, because proving it on the mutating endpoint "
                   "would mean closing a real branch's shift"),
        Probe("manager.force_close_preview.other_branch",
              "jarz_pos.api.shift.get_force_close_preview",
              {"line_manager": DENY, "manager": DENY},
              params={"pos_opening_entry": other_opening},
              skip_reason=_needs(env, "other_opening"),
              note="shift monitor access is not a licence to close another branch's shift"),
        Probe("manager.force_close_shift.role_gate", "jarz_pos.api.shift.force_close_shift",
              {"staff": DENY},
              params={"pos_opening_entry": own_opening, "reason": "role matrix probe"},
              skip_reason=_needs(env, "own_opening"),
              note="shift.py:1258 _ensure_shift_monitor_access refuses before anything "
                   "is written, so this DENY is provable against a real open shift "
                   "without ever closing it"),
        Probe("manager.force_close_shift.other_branch", "jarz_pos.api.shift.force_close_shift",
              {"line_manager": DENY, "manager": DENY},
              params={"pos_opening_entry": other_opening, "reason": "role matrix cross-branch probe"},
              skip_reason=_needs(env, "other_opening"),
              note="shift.py:1260 the branch gate, which also runs before _close_shift "
                   "posts anything"),
        Probe("manager.force_close_shift.own_branch_allow", "jarz_pos.api.shift.force_close_shift",
              {"line_manager": ALLOW, "manager": ALLOW},
              skip_reason=(
                  "asserting this would force-close a live shift on a real branch, book "
                  "its Cash Over/Short and lock out the staff working it. The identical "
                  "role-then-branch gate pair is asserted read-only through "
                  "manager.force_close_preview.own_branch"
              )),
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
              params={"name": ""},
              gate_is_first=True,
              note="JARZ Manager only - a line manager may not approve. _is_manager() is "
                   "the first statement (expenses.py:671) and the empty name is rejected "
                   "on the NEXT line, before any document is loaded — so the manager's "
                   "ALLOW is proven without touching a real expense request, and nothing "
                   "downstream can throw a branch error that masquerades as a denial"),

        # -- item requests: deliberately the widest set in the app --------
        Probe("purchase_request.list", "jarz_pos.api.purchase_request.list_requests",
              {"staff": ALLOW, "line_manager": ALLOW, "manager": ALLOW},
              note="floor staff must be able to file and see their own"),
        Probe("purchase_request.stop", "jarz_pos.api.purchase_request.stop_request",
              {"staff": DENY, "manager": ALLOW},
              params={"name": ""},
              gate_is_first=True,
              note="_ensure_review_access is the first statement (purchase_request.py:241) "
                   "and the empty name is rejected on the next line, so no real Material "
                   "Request is ever stopped to prove this"),

        # -- B2B: walled off from the line manager by design --------------
        Probe("b2b.leads", "jarz_pos.api.leads.get_leads",
              {"staff": DENY, "line_manager": DENY, "manager": ALLOW}),
        Probe("b2b.labels", "jarz_pos.api.labels.get_storage_locations",
              {"staff": DENY, "line_manager": DENY, "manager": ALLOW},
              note="labels.py:419 _ensure_access. The old probe called "
                   "jarz_pos.api.labels.get_label_settings, which is not a whitelisted "
                   "method on this app — every persona got the same 404/417 and the "
                   "matrix read it as the gate answering"),

        # -- custom shipping: request is open, approval is JARZ Manager ---
        Probe("shipping.approve", "jarz_pos.api.custom_shipping.approve_custom_shipping",
              {"staff": DENY, "line_manager": DENY, "manager": ALLOW},
              params={"request_name": env.get("shipping_request") or ""},
              gate_is_first=True,
              note="custom_shipping.py:126 _is_manager() is the first statement. The "
                   "argument is request_name and it names a Custom Shipping Request, not "
                   "an invoice — the old probe sent invoice_name and 500'd on the "
                   "signature before reaching the gate. With the fixture request present "
                   "the manager's ALLOW is a real approval"),

        # -- settlement: guarded 2026-08-19, previously wide open ---------
        # These are the endpoints the mobile client drives for the whole
        # out-for-delivery pay-now flow. Both carried no guard at all.
        Probe("settlement.preview.own_branch", "jarz_pos.api.couriers.generate_settlement_preview",
              {"staff": ALLOW, "line_manager": ALLOW, "manager": ALLOW},
              params={"invoice": own_invoice},
              skip_reason=_needs(env, "own_invoice"),
              note="couriers.py:600 is branch-scoped, NOT role-scoped, and previewing "
                   "posts nothing — so everyone assigned to the branch may look. This is "
                   "the half that proves the new guard does not over-block"),
        Probe("settlement.preview.other_branch", "jarz_pos.api.couriers.generate_settlement_preview",
              {"staff": DENY, "line_manager": DENY, "manager": DENY},
              params={"invoice": other_invoice},
              skip_reason=_needs(env, "other_invoice"),
              note="branch-scoped: nobody may preview an invoice outside their branch"),
        Probe("settlement.confirm.other_branch", "jarz_pos.api.couriers.confirm_settlement",
              {"staff": DENY, "line_manager": DENY, "manager": DENY},
              params={
                  "invoice": other_invoice,
                  "preview_token": "role-matrix-probe",
                  "mode": "pay_now",
              },
              skip_reason=_needs(env, "other_invoice"),
              note="couriers.py:768 — the guard runs before the preview token is even "
                   "looked up, so a cross-branch caller is refused without anything being "
                   "posted"),
        Probe("settlement.confirm.own_branch", "jarz_pos.api.couriers.confirm_settlement",
              {"staff": ALLOW, "line_manager": ALLOW, "manager": ALLOW},
              params={
                  "invoice": own_invoice,
                  "preview_token": "role-matrix-probe",
                  "mode": "pay_now",
              },
              gate_is_first=True,
              skip_reason=_needs(env, "own_invoice") or (env.get("shift_gate_reason") or ""),
              note="only two argument-presence checks precede _guard_invoice_action "
                   "(couriers.py:757-768) and this probe satisfies both, so reaching the "
                   "deliberately invalid preview token proves the branch gate let the "
                   "caller through — while the invalid token guarantees nothing settles"),

        # -- the service twins the mobile client calls directly -----------
        # The app posts to jarz_pos.jarz_pos.services.delivery_handling.* rather
        # than the api/couriers.py wrappers, so the wrappers' branch + shift
        # guards never ran on these. Guarded INSIDE the service 2026-08-19 (the
        # whitelist cannot be removed — the shipped build calls them by name).
        # A DENY expectation that fails here means a twin went open again.
        Probe("twins.settle_delivery_party",
              "jarz_pos.jarz_pos.services.delivery_handling.settle_delivery_party",
              {"staff": DENY},
              params={
                  "party_type": "Employee",
                  "party": _NONEXISTENT_PARTY,
                  "pos_profile": twin_profile,
              },
              gate_is_first=True,
              note="_resolve_guarded_settlement_profile is the first statement, so the "
                   "branch answer comes back before any Courier Transaction is read. The "
                   "party is deliberately unresolvable: if the guard HAS regressed open "
                   "the sweep matches no rows and settles nothing, and the fall-through "
                   "is reported as the failure it is"),
        Probe("twins.settle_courier",
              "jarz_pos.jarz_pos.services.delivery_handling.settle_courier",
              {"staff": DENY},
              params={"courier": _NONEXISTENT_PARTY, "pos_profile": twin_profile},
              gate_is_first=True,
              note="delegates to settle_delivery_party, inherits its guard"),
        Probe("twins.sales_partner_unpaid_ofd",
              "jarz_pos.jarz_pos.services.delivery_handling.sales_partner_unpaid_out_for_delivery",
              {"staff": DENY},
              params={"invoice_name": _NONEXISTENT_INVOICE, "pos_profile": twin_profile},
              gate_is_first=True,
              note="the pos_profile branch check runs BEFORE the invoice is loaded, which "
                   "is what makes an unloadable invoice the RIGHT argument here: the gate "
                   "is still reached, and a caller who gets past it can only reach a "
                   "not-found — never a payment"),
        Probe("twins.create_pos_invoice",
              "jarz_pos.jarz_pos.services.invoice_creation.create_pos_invoice",
              {"staff": DENY},
              params={"cart_json": "[]", "customer_name": "", "pos_profile_name": twin_profile},
              known_open=True,
              gate_is_first=True,
              note="bypasses the branch+shift guard on api/invoices.py:105. There is no "
                   "gate at all in this twin, so ANY outcome other than a refusal proves "
                   "the bypass — which is exactly what gate_is_first encodes here"),

        # -- cross-branch reads that leak the whole company ---------------
        # HTTP 200 alone proves nothing; each of these must show the body
        # carrying another branch's data before it is called a leak.
        Probe("leak.recent_invoices", "jarz_pos.api.notifications.get_recent_invoices",
              {"staff": DENY}, params={"minutes": 60},
              known_open=True, leak_proof=_proof_recent_invoices,
              note="returns every POS invoice site-wide, unscoped"),
        Probe("leak.courier_balances", "jarz_pos.api.couriers.get_courier_balances",
              {"staff": DENY}, known_open=True, leak_proof=_proof_courier_balances,
              note="every courier's outstanding rows, whatever branch they belong to"),
        Probe("leak.branch_drawer", "jarz_pos.api.pos.get_pos_profile_account_balance",
              {"staff": DENY}, params={"profile": other_profile},
              known_open=True, leak_proof=_proof_branch_drawer,
              skip_reason=_needs(env, "other_profile"),
              note="any branch's cash drawer balance. The argument is 'profile' — the old "
                   "probe sent pos_profile and 500'd on the signature"),

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

def _run_probe(
    ctx: RoleRunContext,
    probe: Probe,
    persona: str,
    expected: str,
    session: ApiSession,
    allowed_profiles: Sequence[str],
) -> None:
    """Call one endpoint as one persona and score the answer."""
    name = f"{probe.key}[{persona}]"
    params = probe.params_for(persona)
    result = session.call(probe.method, params, http=probe.http)
    verdict = _classify(result)

    detail = f"expected={expected} got={verdict} http={result.get('status')}"
    if probe.note:
        detail += f" :: {probe.note}"

    if verdict == UNREACHABLE:
        ctx.record(name, False, f"transport failure: {result.get('error')}")
        return

    if verdict == INCONCLUSIVE:
        reason = _inconclusive_reason(result)
        if not probe.gate_is_first:
            # Never a pass, never a fail: the gate was not asked.
            ctx.skip(
                name,
                f"{detail} :: INCONCLUSIVE — {reason}. The call failed for a reason "
                f"unrelated to authorisation, so it is evidence of nothing in either "
                f"direction. body={str(result.get('body') or '')[:200]}",
            )
            return
        verdict = ALLOWED
        detail += (
            f" :: reached the endpoint's own validation past the gate ({reason}), "
            "which for this endpoint can only happen after the authorisation check"
        )

    if expected == DENY and verdict == ALLOWED and probe.leak_proof:
        proof = probe.leak_proof(_message(result), allowed_profiles)
        if not proof:
            ctx.skip(
                name,
                f"{detail} :: the server answered 200, but the body carried no data "
                "from outside this persona's branches, so the leak is NOT proven here. "
                "'Returned 200' is not the claim worth making — 'returned another "
                "branch's data' is",
            )
            return
        detail += f" :: LEAK PROVEN BY CONTENT — {proof}"

    passed = (verdict == DENIED) if expected == DENY else (verdict != DENIED)
    if not passed and probe.known_open:
        detail += " :: KNOWN OPEN - this gap is already on the fix list"
    if not passed:
        detail += f" :: body={str(result.get('body') or '')[:200]}"
    ctx.record(name, passed, detail)


def run(cleanup: bool = True) -> Dict[str, Any]:
    if isinstance(cleanup, str):
        cleanup = cleanup.strip().lower() not in {"", "0", "false", "no"}

    _guard_environment()
    frappe.flags.in_test = True
    # The fixtures below are created through the real POS invoice service; the
    # WooCommerce outbound hook must stay inert for them.
    frappe.flags.ignore_woo_outbound = True

    ctx = RoleRunContext()
    sessions: Dict[str, ApiSession] = {}
    summary: Dict[str, Any] = {}
    try:
        base_url = _resolve_base_url(ctx)
        if not base_url:
            summary = _summary(ctx, skipped_reason="no reachable base URL")
            return summary

        try:
            env = _build_fixtures(ctx)
        except Exception as exc:
            # The role-only probes (which need no document) must still run, and
            # every document-scoped probe must skip with this as the reason —
            # rather than the whole matrix dying on a fixture problem.
            frappe.db.rollback()
            ctx.record(
                "fixture.environment", False,
                f"{type(exc).__name__}: {exc} — no fixtures could be built; every "
                "document-scoped and branch-scoped probe below is skipped",
            )
            env = {
                "own_profile": None, "other_profile": None, "own_invoice": None,
                "other_invoice": None, "cancelable": {}, "shipping_invoice": None,
                "shipping_request": None, "own_opening": None, "other_opening": None,
                "price_list": None, "persona_profiles": {}, "shift_gate_reason": None,
            }

        personas = _derive_persona_roles(ctx)
        ctx.personas = personas
        site = frappe.local.site

        for persona, spec in personas.items():
            email = _provision_user(ctx, persona, spec, env.get("own_profile"))
            if not email:
                continue
            env["persona_profiles"][persona] = [env["own_profile"]] if env.get("own_profile") else []
            session = ApiSession(base_url, site, email)
            ok, detail = session.login(TEST_PASSWORD)
            ctx.record(f"login.{persona}", ok, detail)
            if ok:
                sessions[persona] = session

        if not sessions:
            summary = _summary(ctx, skipped_reason="no persona could log in")
            return summary

        # Only meaningful once the synthetic users exist.
        env["shift_gate_reason"] = _shift_gate_reason(
            env.get("own_profile") or "",
            [f"{USER_PREFIX}{p}@{USER_DOMAIN}" for p in sessions],
        )

        for probe in _matrix(env):
            for persona, expected in probe.expect.items():
                session = sessions.get(persona)
                if not session:
                    continue
                name = f"{probe.key}[{persona}]"
                if probe.skip_reason:
                    reason = probe.skip_reason
                    if probe.note:
                        reason += f" :: {probe.note}"
                    ctx.skip(name, reason)
                    continue
                _run_probe(
                    ctx, probe, persona, expected, session,
                    env["persona_profiles"].get(persona) or [],
                )

        summary = _summary(ctx)
        return summary
    finally:
        for session in sessions.values():
            session.logout()
        if cleanup:
            # Order matters: the shipping request links the invoice, and the
            # invoices link the stock entries.
            for step, fn in (
                ("shipping_requests", lambda: _cleanup_shipping_requests(ctx)),
                ("fixtures", lambda: _cleanup_fixtures(ctx.fixtures)),
                ("users", lambda: _deprovision(ctx)),
            ):
                try:
                    fn()
                except Exception as exc:
                    frappe.log_error(
                        frappe.get_traceback(), f"role_matrix: teardown failed ({step})"
                    )
                    # A teardown that dies halfway leaves submitted fixtures or
                    # live synthetic logins behind while the run still returns
                    # its counts. Make the residue visible in the report.
                    ctx.record(
                        f"cleanup.{step}_removed", False,
                        f"{type(exc).__name__}: {exc} — residue may remain; sweep the "
                        f"_B2BVALID_ and {USER_PREFIX} prefixes by hand",
                    )
                    if summary:
                        summary.update(_summary_payload(ctx))
        else:
            print(
                f"\nCleanup skipped (cleanup=False). Synthetic users ({USER_PREFIX}*), "
                "their POS Profile links and the _B2BVALID_ fixtures are retained."
            )


def _summary_payload(ctx: RoleRunContext, skipped_reason: str = "") -> Dict[str, Any]:
    summary: Dict[str, Any] = {
        "passed": ctx.passed,
        "failed": ctx.failed,
        "skipped": ctx.skipped,
        "checks": ctx.checks,
        # Surfaced separately so "nothing was proven here" can never be mistaken
        # for "everything passed".
        "skipped_checks": [
            {"name": c["name"], "reason": c["detail"]} for c in ctx.checks if c["skipped"]
        ],
        "personas": {
            k: {"template": v.get("template"), "roles": v.get("roles")}
            for k, v in ctx.personas.items()
        },
        "fixtures": {
            "invoices": list(dict.fromkeys(ctx.fixtures.invoices)),
            "stock_entries": list(dict.fromkeys(ctx.fixtures.stock_entries)),
            "item_prices": list(dict.fromkeys(ctx.fixtures.item_prices)),
            "customers": list(dict.fromkeys(ctx.fixtures.customers)),
            "shipping_requests": list(dict.fromkeys(ctx.shipping_requests)),
        },
    }
    if skipped_reason:
        summary["note"] = skipped_reason
    return summary


def _summary(ctx: RoleRunContext, skipped_reason: str = "") -> Dict[str, Any]:
    summary = _summary_payload(ctx, skipped_reason)
    print(
        f"role_matrix_validation: {ctx.passed} passed, {ctx.failed} failed, "
        f"{ctx.skipped} skipped"
    )
    print(MARKER_START)
    print(json.dumps(summary, indent=2, default=str))
    print(MARKER_END)
    return summary
