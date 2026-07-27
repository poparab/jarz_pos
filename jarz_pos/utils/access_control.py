"""Centralised branch (POS Profile) access control and shift enforcement.

Before this module there were two independent answers to "which branches may
this user touch?":

* ``jarz_pos.api.kanban._get_current_user_pos_profiles`` — POS Profile User
  links only, no role bypass.
* ``jarz_pos.api.manager._current_user_allowed_profiles`` — same, but System
  Manager / POS Manager silently received *every* profile.

The two disagreed, so a manager could list an order in the manager feed and
then get a 403 opening the same order on the board.  Everything now resolves
through :func:`get_user_pos_profiles`.

Branch membership is the POS Profile User child table — a manager is scoped
exactly like a staff member and must be added to the branch.  ``Administrator``
is the single escape hatch so support access and background jobs keep working.

Shift enforcement lives here too because it asks the same question one step
further: not just "is this your branch?" but "is your branch open right now?".
"""
from __future__ import annotations

import frappe
from frappe import _
from typing import Any, Dict, List, Optional, Sequence

#: Users that bypass branch scoping entirely.
UNRESTRICTED_USERS = {"Administrator"}

#: ``frappe.flags`` that mean we are running outside a real user session.
#:
#: ``in_test`` is deliberately absent. Including it switched branch scoping off
#: for the whole test run, so no test could ever exercise the guard — the first
#: test written against it failed for that reason. Test suites run as
#: ``Administrator``, which is already unrestricted, so nothing needs the flag.
_SYSTEM_CONTEXT_FLAGS = (
    "in_install",
    "in_migrate",
    "in_patch",
    "in_import",
    "in_setup_wizard",
)


class BranchAccessError(frappe.PermissionError):
    """Raised when a user acts on an invoice outside their assigned branches."""


class ShiftRequiredError(frappe.ValidationError):
    """Raised when a cash/stock action is attempted with no open shift.

    Frappe surfaces the class name as ``exc_type`` on the JSON error response,
    which is what lets the Flutter client route the user to Start Shift instead
    of showing a generic red toast.
    """


# ---------------------------------------------------------------------------
# Context helpers
# ---------------------------------------------------------------------------

def _is_system_context() -> bool:
    """True when running under migrate/install/test rather than a user request."""
    flags = getattr(frappe, "flags", None)
    if flags is None:
        return False
    return any(bool(getattr(flags, flag, False)) for flag in _SYSTEM_CONTEXT_FLAGS)


def is_unrestricted_user(user: Optional[str] = None) -> bool:
    """True when *user* bypasses branch scoping (Administrator or system context)."""
    resolved = user or frappe.session.user
    return resolved in UNRESTRICTED_USERS or _is_system_context()


def _request_cache() -> Optional[Dict[str, Any]]:
    """Per-request cache dict.

    Deliberately ``frappe.local`` rather than a module-level dict: a module-level
    cache is shared across users inside one worker process and has already
    caused cross-user leakage here once.
    """
    cache = getattr(frappe.local, "jarz_access_cache", None)
    if cache is None:
        try:
            cache = {}
            frappe.local.jarz_access_cache = cache
        except Exception:
            return None
    return cache if isinstance(cache, dict) else None


# ---------------------------------------------------------------------------
# Branch resolution
# ---------------------------------------------------------------------------

def get_user_pos_profiles(user: Optional[str] = None) -> List[str]:
    """Return the enabled POS Profiles *user* is assigned to.

    Assignment is the POS Profile User child table for everyone, managers
    included.  ``Administrator`` receives every enabled profile.

    Returns an empty list when the user is assigned to no branch — callers that
    need a hard failure should use :func:`ensure_user_pos_profiles`.
    """
    resolved = user or frappe.session.user
    if not resolved or resolved == "Guest":
        return []

    cache = _request_cache()
    cache_key = f"pos_profiles::{resolved}"
    if cache is not None and cache_key in cache:
        return list(cache[cache_key])

    profiles: List[str] = []
    try:
        if is_unrestricted_user(resolved):
            profiles = frappe.get_all(
                "POS Profile", filters={"disabled": 0}, pluck="name"
            ) or []
        else:
            linked = frappe.get_all(
                "POS Profile User",
                filters={"user": resolved, "parenttype": "POS Profile"},
                pluck="parent",
            ) or []
            if linked:
                profiles = frappe.get_all(
                    "POS Profile",
                    filters={"name": ["in", list(set(linked))], "disabled": 0},
                    pluck="name",
                ) or []
    except Exception:
        # Never fail the caller on a lookup error, but never fail *invisibly*
        # either — a silent [] here reads as "assigned to no branch" and would
        # hide the real cause behind an empty board.
        frappe.log_error(
            frappe.get_traceback(),
            f"Failed to resolve POS Profiles for {resolved}",
        )
        return []

    profiles = sorted({str(p).strip() for p in profiles if str(p or "").strip()})
    if cache is not None:
        cache[cache_key] = list(profiles)
    return profiles


def ensure_user_pos_profiles(
    user: Optional[str] = None,
    *,
    action_label: str = "this action",
) -> List[str]:
    """Like :func:`get_user_pos_profiles` but throws when the user has no branch.

    Used by write paths.  Read paths generally prefer the soft variant so the
    board can render an explanatory empty state rather than an error dialog.
    """
    profiles = get_user_pos_profiles(user)
    if not profiles:
        frappe.throw(
            _(
                "You are not assigned to any branch (POS Profile), so {0} is not "
                "available. Ask a manager to add you to a branch."
            ).format(action_label),
            BranchAccessError,
            title=_("No Branch Assigned"),
        )
    return profiles


def get_invoice_branch(inv: Any) -> str:
    """Return the operational branch of an invoice.

    ``custom_kanban_profile`` is the source of truth — ``pos_profile`` is frozen
    at submit and goes stale the moment an order is transferred between branches.
    """
    if inv is None:
        return ""
    getter = inv.get if hasattr(inv, "get") else None
    if getter is None:
        return ""
    return str(getter("custom_kanban_profile") or getter("pos_profile") or "").strip()


def ensure_profile_scoped_invoice_access(
    inv: Any,
    *,
    action_label: str,
    extra_profiles: Optional[Sequence[str]] = None,
) -> None:
    """Assert the current user may act on *inv*'s branch.

    ``extra_profiles`` covers actions that name a second branch (an amendment
    that re-targets a profile); every named branch must be one of the user's.
    """
    if is_unrestricted_user():
        return

    allowed = set(ensure_user_pos_profiles(action_label=action_label))

    required: List[str] = []
    branch = get_invoice_branch(inv)
    if branch:
        required.append(branch)
    for profile in extra_profiles or []:
        cleaned = str(profile or "").strip()
        if cleaned:
            required.append(cleaned)

    unauthorized = sorted({p for p in required if p not in allowed})
    if unauthorized:
        frappe.throw(
            _(
                "Not permitted: {0} is limited to the branches you are assigned "
                "to. This order belongs to {1}."
            ).format(action_label, ", ".join(unauthorized)),
            BranchAccessError,
            title=_("Wrong Branch"),
        )


def get_users_for_pos_profiles(profiles: Sequence[str]) -> List[str]:
    """Return the users assigned to any of *profiles*.

    Used to address realtime events at the branch that owns an order instead of
    broadcasting them to every logged-in device.
    """
    filtered = sorted({str(p).strip() for p in (profiles or []) if str(p or "").strip()})
    if not filtered:
        return []

    try:
        rows = frappe.get_all(
            "POS Profile User",
            filters={"parent": ["in", filtered], "parenttype": "POS Profile"},
            fields=["user"],
        ) or []
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Failed to load POS Profile users")
        return []

    users: List[str] = []
    seen: set[str] = set()
    for row in rows:
        user = row.get("user")
        if user and user not in seen:
            seen.add(user)
            users.append(user)
    return users


# ---------------------------------------------------------------------------
# Shift enforcement
# ---------------------------------------------------------------------------

def user_requires_pos_shift(user: Optional[str] = None) -> bool:
    """True when *user* must have an open shift to move cash or stock.

    Driven by ``User.custom_require_pos_shift`` — the same flag the Flutter
    client reads — so back end and front end cannot disagree about who is
    exempt.  Unrestricted users are never gated.
    """
    resolved = user or frappe.session.user
    if not resolved or is_unrestricted_user(resolved):
        return False

    cache = _request_cache()
    cache_key = f"require_shift::{resolved}"
    if cache is not None and cache_key in cache:
        return bool(cache[cache_key])

    try:
        required = bool(
            int(frappe.db.get_value("User", resolved, "custom_require_pos_shift") or 0)
        )
    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            f"Failed to read custom_require_pos_shift for {resolved}",
        )
        required = False

    if cache is not None:
        cache[cache_key] = required
    return required


def get_open_shift_for_profile(pos_profile: str) -> Optional[Dict[str, Any]]:
    """Return the open POS Opening Entry on *pos_profile*, or None.

    Queried directly rather than through ``jarz_pos.api.shift`` to keep that
    module free to import this one.
    """
    profile = str(pos_profile or "").strip()
    if not profile:
        return None

    try:
        rows = frappe.get_all(
            "POS Opening Entry",
            filters={"pos_profile": profile, "status": "Open", "docstatus": 1},
            fields=["name", "user", "pos_profile", "period_start_date"],
            order_by="period_start_date desc, modified desc",
            limit=1,
        ) or []
    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            f"Failed to look up open shift for {profile}",
        )
        return None

    return rows[0] if rows else None


def ensure_open_shift(pos_profile: str, *, action_label: str) -> None:
    """Assert *pos_profile* has an open shift before money or stock moves.

    The check is "is this branch open?", not "did *you* open it?".  A branch can
    only hold one shift at a time, so requiring the caller to be the opener
    would lock out every other staff member working that branch.  What matters
    for the books is that the cash lands inside a shift window that somebody
    will be held to at close.

    Only applies to users carrying ``custom_require_pos_shift``; managers and
    back-office users configured without the flag are unaffected.
    """
    profile = str(pos_profile or "").strip()
    if not profile or not user_requires_pos_shift():
        return

    if get_open_shift_for_profile(profile):
        return

    frappe.throw(
        _(
            "No open shift on branch {0}, so {1} is not allowed. Start a shift "
            "on this branch first."
        ).format(profile, action_label),
        ShiftRequiredError,
        title=_("Shift Required"),
    )


def ensure_open_shift_for_invoice(inv: Any, *, action_label: str) -> None:
    """:func:`ensure_open_shift` for the branch that owns *inv*."""
    ensure_open_shift(get_invoice_branch(inv), action_label=action_label)
