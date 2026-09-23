"""Branch access: who may work the till at which branch, and for how long.

Branch membership is, and stays, the ``POS Profile User`` child table
(``POS Profile.applicable_for_users``). ``utils.access_control`` reads it for
every order, cash and stock action, so it is the one record that decides
whether somebody can use the POS at a branch. This module does not add a second
source of truth next to it; it only gives managers a safe way to edit it from
the app, plus a *day access* that adds a row for one branch day and takes it
away again.

Rules this module enforces (owner decisions, 2026-09-23)
--------------------------------------------------------
* **No change while the branch is open.** A branch with an open POS Opening
  Entry has a cashier mid-shift. Adding or removing a member under them changes
  who can move that shift's cash and who shows up in its realtime feed, so
  every change -- manual add/remove, a day access starting, expiring or being
  cancelled -- waits for the branch to close. The refusal names the branch and
  who holds the shift, because "not allowed" alone sends the manager hunting.
* **Every change is logged** to ``Jarz Branch Access Log``: who, when, which
  branch, which user, what, and why. The log is insert-only from here.
* **Scope.** JARZ Manager / System Manager / Administrator manage every enabled
  branch. A line manager manages only the branches they are themselves a member
  of, and never their own membership -- otherwise a line manager could grant
  themselves the branch they were just removed from.

Why rows are written directly and the POS Profile is never saved
----------------------------------------------------------------
Saving the parent POS Profile runs ERPNext's full profile validation (payment
modes, warehouses, write-off accounts, company defaults). Any unrelated drift
in that document -- a disabled mode of payment, a v16 default that moved -- then
refuses an access change that has nothing to do with it. Inserting or deleting
the single child row touches exactly what is being changed.

The day-access life cycle and why ``row_added`` exists
------------------------------------------------------
A grant only removes what it added. ``row_added=0`` means the user was already
a permanent member when the grant started, so ending it must remove nothing;
getting this wrong would lock a regular cashier out of their own till the
morning after somebody gave them a "day access" they did not need. The
opposite mistake -- a grant that never removes its row -- leaves a cover with
access forever, which is why two overlapping grants keep the row until the
LAST one ends rather than until the first.
"""

from __future__ import annotations

from datetime import date as date_cls
from datetime import datetime, time
from typing import Any, Dict, List, Optional, Tuple

import frappe
from frappe import _
from frappe.utils import add_days, cint, get_datetime, getdate, now_datetime

from jarz_pos.constants import ROLES
from jarz_pos.services import roster as roster_service
from jarz_pos.utils import access_control

LOG_DOCTYPE = "Jarz Branch Access Log"
DAY_ACCESS_DOCTYPE = "Jarz POS Day Access"
MEMBER_DOCTYPE = "POS Profile User"
MEMBER_PARENTTYPE = "POS Profile"
MEMBER_PARENTFIELD = "applicable_for_users"

#: How far ahead a day access may be booked. Two weeks covers a published rota;
#: anything further is a permanent change pretending to be a temporary one.
MAX_DAYS_AHEAD = 14

#: A day access expires at 03:00 on the morning after its date. The branch day
#: runs 12:30 -> ~00:30, and the closing count routinely runs past midnight, so
#: expiring at midnight would pull the till out from under the person closing.
EXPIRY_HOUR = 3

ACTION_ADDED = "Added"
ACTION_REMOVED = "Removed"
ACTION_SCHEDULED = "Day Access Scheduled"
ACTION_STARTED = "Day Access Started"
ACTION_ENDED = "Day Access Ended"
ACTION_CANCELLED = "Day Access Cancelled"

SOURCE_SCREEN = "Branch Access Screen"
SOURCE_SHIFT = "Shift Assignment"
SOURCE_COVER = "Day Off Cover"
SOURCE_SCHEDULER = "Scheduler"

STATUS_SCHEDULED = "Scheduled"
STATUS_ACTIVE = "Active"
STATUS_ENDED = "Ended"
STATUS_CANCELLED = "Cancelled"
OPEN_STATUSES = (STATUS_SCHEDULED, STATUS_ACTIVE)

#: Roles that manage every branch. Deliberately NOT the whole LINE_MANAGER_TIER:
#: that set is the door to the screen, this is who is not limited to their own
#: branches once inside.
MANAGE_ALL_ROLES = {ROLES.JARZ_MANAGER, ROLES.SYSTEM_MANAGER}

#: Both spellings exist as real Role records; see ``ROLES.JARZ_LINE_MANAGER_ALT``.
LINE_MANAGER_ROLES = {ROLES.JARZ_LINE_MANAGER, ROLES.JARZ_LINE_MANAGER_ALT}

#: Accounts that are never listed and never given branch access.
EXCLUDED_USERS = {"Administrator", "Guest"}

#: Upper bound per scheduler pass. At three branches and a handful of covers a
#: day this is never reached; it exists so a runaway backlog cannot hold the
#: hourly slot.
CYCLE_BATCH_LIMIT = 500


class BranchOpenError(frappe.ValidationError):
    """Raised when a branch-access change is attempted while the branch is open.

    Its own class so the client (and the roster's best-effort grant) can tell
    "try again after closing" apart from a real validation failure.
    """


class AlreadyHasAccessError(frappe.ValidationError):
    """The user already holds the access being granted (permanent or same day)."""


# ---------------------------------------------------------------------------
# Small helpers (kept separate so tests can replace them one by one)
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return now_datetime()


def _session_user() -> str:
    return frappe.session.user


def _roles(user: Optional[str] = None) -> set:
    return {str(r or "").strip() for r in (frappe.get_roles(user) or []) if str(r or "").strip()}


def _clean(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


def _fmt(value: Any) -> Optional[str]:
    """Render a date/datetime for the client in one fixed shape."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date_cls):
        return value.isoformat()
    return str(value)


def as_flag(value: Any) -> bool:
    """Read a form/JSON boolean the way the mobile client actually sends it.

    Dio posts form data, so ``allowed`` arrives as ``"1"``, ``"true"`` or
    ``"false"``. ``bool("false")`` is True, which would turn a remove into an
    add -- so the text is parsed, never truth-tested.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"", "0", "false", "no", "n", "off", "none", "null"}:
        return False
    frappe.throw(_("Expected yes or no (1 or 0), got {0}.").format(value))


def _safe_log(title: str) -> None:
    """``frappe.log_error`` that cannot itself take the caller down.

    ``log_error`` inserts an Error Log row and can raise (a broken connection,
    a title over 140 characters on older sites). In the scheduler that would
    turn one bad grant into a dead hourly job.
    """
    try:
        frappe.log_error(frappe.get_traceback(), str(title)[:140])
    except Exception:
        pass


def _full_name(user: Optional[str]) -> Optional[str]:
    if not user:
        return None
    try:
        return frappe.db.get_value("User", user, "full_name") or user
    except Exception:
        return user


def _employee_for_user(user: str) -> Tuple[Optional[str], Optional[str]]:
    """(employee, employee_name) for a user, preferring an Active record.

    ``status asc`` puts Active before Inactive/Left/Suspended, so a rehired
    person with an old Left record resolves to the current one.
    """
    try:
        rows = frappe.get_all(
            "Employee",
            filters={"user_id": user},
            fields=["name", "employee_name"],
            order_by="status asc",
            limit_page_length=1,
        ) or []
    except Exception:
        return None, None
    if not rows:
        return None, None
    return rows[0].get("name"), rows[0].get("employee_name")


def _user_row(user: str) -> Optional[Dict[str, Any]]:
    row = frappe.db.get_value("User", user, ["name", "full_name", "enabled"], as_dict=True)
    return dict(row) if row else None


def _enabled_profiles() -> List[str]:
    return sorted(
        frappe.get_all("POS Profile", filters={"disabled": 0}, pluck="name") or []
    )


def _user_profiles(user: str) -> List[str]:
    return access_control.get_user_pos_profiles(user) or []


def _open_shift(pos_profile: str) -> Optional[Dict[str, Any]]:
    return access_control.get_open_shift_for_profile(pos_profile)


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------


def has_access(user: Optional[str] = None) -> bool:
    """The screen's door: the same LINE_MANAGER_TIER that gates the roster."""
    user = user or _session_user()
    if user == ROLES.ADMINISTRATOR:
        return True
    return bool(_roles(user).intersection(ROLES.LINE_MANAGER_TIER))


def ensure_access() -> None:
    if not has_access():
        frappe.throw(
            _("Not permitted: branch access is managed by managers and line managers."),
            frappe.PermissionError,
        )


def can_manage_all(user: Optional[str] = None) -> bool:
    user = user or _session_user()
    if user == ROLES.ADMINISTRATOR:
        return True
    return bool(_roles(user).intersection(MANAGE_ALL_ROLES))


def manageable_profiles(user: Optional[str] = None) -> List[str]:
    """Enabled branches this user may change membership on.

    A line manager's reach is their own POS Profile User rows -- the same
    answer ``access_control`` gives for which branch's orders they may touch,
    so "may run this branch" and "may staff this branch" cannot drift apart.
    Anyone below the line-manager tier manages nothing.
    """
    user = user or _session_user()
    enabled = _enabled_profiles()
    if can_manage_all(user):
        return enabled
    if not _roles(user).intersection(LINE_MANAGER_ROLES):
        return []
    # Only PERMANENT membership widens a line manager's reach. A day access
    # lent to a line manager is for working that branch's till, not for
    # deciding who else may work it.
    own = {p for p in _user_profiles(user) if _is_permanent_member(user, p)}
    return [p for p in enabled if p in own]


def ensure_can_manage(pos_profile: str, target_user: Optional[str] = None) -> None:
    """Assert the caller may change *target_user*'s access at *pos_profile*."""
    ensure_access()
    profile = _clean(pos_profile)
    enabled = _enabled_profiles()
    if not profile or profile not in enabled:
        frappe.throw(_("{0} is not an active branch.").format(pos_profile or _("(no branch)")))

    caller = _session_user()
    if can_manage_all(caller):
        return

    if profile not in manageable_profiles(caller):
        frappe.throw(
            _(
                "You can only change access for branches you belong to, and {0} is not one of them."
            ).format(profile),
            frappe.PermissionError,
        )
    if target_user and target_user == caller:
        # A line manager editing their own membership is the one change that
        # would let them undo a manager's decision about them.
        frappe.throw(
            _("You cannot change your own branch access. Ask a JARZ Manager."),
            frappe.PermissionError,
        )
    if target_user and target_user != caller and _is_manager_or_line_manager(target_user):
        # A line manager sits under the manager tier, so removing a JARZ
        # Manager would lock their own manager out. And two line managers who
        # could add each other to their branches would, between them, reach
        # every branch either one runs -- so line managers' access is a
        # manager's decision only.
        frappe.throw(
            _("Only a JARZ Manager can change a manager's or line manager's branch access."),
            frappe.PermissionError,
        )


def _is_manager_or_line_manager(user: str) -> bool:
    return can_manage_all(user) or bool(_roles(user).intersection(LINE_MANAGER_ROLES))


def can_edit_user(target_user: str, caller: Optional[str] = None) -> bool:
    """Whether the caller may change *target_user*'s access at all (any branch).

    The client uses this to lock a whole row instead of letting every tap end
    in a refusal; ``ensure_can_manage`` stays the authority.
    """
    caller = caller or _session_user()
    if can_manage_all(caller):
        return True
    if target_user == caller:
        return False
    return not _is_manager_or_line_manager(target_user)


def _ensure_target_user(user: Optional[str], *, for_adding: bool) -> Dict[str, Any]:
    """Resolve and sanity-check the user whose access is being changed.

    Removing access from a disabled user is allowed (it is cleanup); adding it
    is not, because a disabled account holding a branch is exactly the stale
    membership this screen exists to clear.
    """
    user = _clean(user)
    if not user:
        frappe.throw(_("User is required."))
    if user in EXCLUDED_USERS:
        frappe.throw(_("{0} cannot be given or refused branch access.").format(user))
    row = _user_row(user)
    if not row:
        frappe.throw(_("No such user: {0}").format(user))
    if for_adding and not cint(row.get("enabled")):
        frappe.throw(
            _("{0} is disabled and cannot be given branch access.").format(
                row.get("full_name") or user
            )
        )
    return row


# ---------------------------------------------------------------------------
# The open-branch rule
# ---------------------------------------------------------------------------


def _since_text(value: Any) -> str:
    """When the shift opened. The date is kept when it is not today, because
    shifts here are sometimes left open 22-59 hours and "since 12:47" would
    then point at the wrong day."""
    if not value:
        return _("an unknown time")
    try:
        opened = get_datetime(value)
    except Exception:
        return str(value)
    if opened.date() == _now().date():
        return opened.strftime("%H:%M")
    return opened.strftime("%Y-%m-%d %H:%M")


def open_shift_message(pos_profile: str, shift: Dict[str, Any]) -> str:
    holder = _full_name(shift.get("user")) or _("someone")
    return _(
        "{0} has an open shift ({1} since {2}). Branch access can only change when the branch is closed."
    ).format(pos_profile, holder, _since_text(shift.get("period_start_date")))


def ensure_branch_closed(pos_profile: str) -> None:
    """Refuse with :class:`BranchOpenError` while *pos_profile* has an open shift.

    ``get_open_shift_for_profile`` logs and returns None on a lookup failure,
    so a broken query lets the change through rather than freezing every
    branch's access; the change is still logged either way.
    """
    shift = _open_shift(pos_profile)
    if shift:
        frappe.throw(
            open_shift_message(pos_profile, shift),
            BranchOpenError,
            title=_("Branch Is Open"),
        )


def _open_shift_payload(pos_profile: str) -> Optional[Dict[str, Any]]:
    shift = _open_shift(pos_profile)
    if not shift:
        return None
    return {
        "name": shift.get("name"),
        "user": shift.get("user"),
        "user_full_name": _full_name(shift.get("user")),
        "since": _fmt(shift.get("period_start_date")),
    }


# ---------------------------------------------------------------------------
# POS Profile User rows
# ---------------------------------------------------------------------------


def _lock_branch(pos_profile: str) -> None:
    """Serialise every membership change on one branch.

    The hourly job and a manager can act on the same grant at once -- the job
    reads ``row_added=1`` and deletes the row while the manager is converting
    that grant to permanent. Locking the POS Profile row first makes the
    second writer wait and then re-read what the first one committed.
    """
    try:
        frappe.db.get_value(MEMBER_PARENTTYPE, pos_profile, "name", for_update=True)
    except Exception:
        pass


def _touch_profile(pos_profile: str) -> None:
    """Bump the POS Profile's ``modified`` after a direct child-row write.

    Without it a copy of the profile already open in Desk would save over the
    change: Frappe rewrites the whole child table from the form, and the
    stale-document check compares ``modified``, which a direct row write never
    moves. The row just added would vanish with no trace in the log.
    """
    try:
        frappe.db.set_value(
            MEMBER_PARENTTYPE,
            pos_profile,
            {"modified": _now(), "modified_by": _session_user()},
            update_modified=False,
        )
    except Exception:
        _safe_log(f"Could not touch POS Profile {pos_profile}")


def _membership_rows(user: str, pos_profile: str) -> List[str]:
    """Names of the POS Profile User rows linking *user* to *pos_profile*.

    Filtered exactly like ``access_control.get_user_pos_profiles`` (parent +
    parenttype, not parentfield), so "is a member" here means precisely what
    the access check will conclude.
    """
    return frappe.get_all(
        MEMBER_DOCTYPE,
        filters={"parent": pos_profile, "parenttype": MEMBER_PARENTTYPE, "user": user},
        pluck="name",
    ) or []


def is_member(user: str, pos_profile: str) -> bool:
    return bool(_membership_rows(user, pos_profile))


def _insert_membership_row(user: str, pos_profile: str) -> str:
    """Add one POS Profile User row without saving the parent profile.

    ``default`` stays 0: a day access must never become somebody's default
    branch, or their app would open on a branch they lose at 03:00.
    """
    result = frappe.db.sql(
        """SELECT COALESCE(MAX(idx), 0) FROM `tabPOS Profile User`
        WHERE parent = %s AND parenttype = %s""",
        (pos_profile, MEMBER_PARENTTYPE),
    )
    next_idx = cint(result[0][0] if result else 0) + 1
    row = frappe.get_doc(
        {
            "doctype": MEMBER_DOCTYPE,
            "parent": pos_profile,
            "parenttype": MEMBER_PARENTTYPE,
            "parentfield": MEMBER_PARENTFIELD,
            "user": user,
            "default": 0,
            "idx": next_idx,
        }
    )
    row.flags.ignore_permissions = True
    row.insert(ignore_permissions=True)
    _touch_profile(pos_profile)
    return row.name


def _delete_membership_rows(user: str, pos_profile: str) -> int:
    """Delete every row linking *user* to *pos_profile*; returns how many.

    All of them, not the first: a duplicated row (Desk allows it) would
    otherwise leave the user a member after a "remove" that reported success.
    """
    names = _membership_rows(user, pos_profile)
    for name in names:
        frappe.db.delete(MEMBER_DOCTYPE, {"name": name})
    if names:
        _touch_profile(pos_profile)
    return len(names)


def _invalidate(user: str, pos_profile: Optional[str] = None) -> None:
    """Drop cached answers about *user*'s branches for the rest of this request.

    ``access_control`` caches ``pos_profiles::<user>`` on ``frappe.local`` for
    the request. Without clearing it, a scheduler pass or a roster request that
    reads a branch after changing it would act on the pre-change membership.
    The POS Profile document cache is cleared too because the parent is never
    saved, so nothing else would evict a cached copy of its child table.
    """
    try:
        cache = access_control._request_cache()
        if cache is not None:
            cache.pop(f"pos_profiles::{user}", None)
    except Exception:
        pass
    if pos_profile:
        try:
            frappe.clear_document_cache(MEMBER_PARENTTYPE, pos_profile)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Log
# ---------------------------------------------------------------------------


def _log(
    user: str,
    pos_profile: str,
    action: str,
    source: str,
    *,
    notes: Optional[str] = None,
    day_access: Optional[str] = None,
    changed_by: Optional[str] = None,
) -> str:
    """Insert one Jarz Branch Access Log row and return its name.

    Not wrapped in a try: the log is the audit trail the owner asked for, so a
    change that cannot be logged is rolled back with the request rather than
    applied silently.
    """
    changed_by = changed_by or _session_user()
    employee, employee_name = _employee_for_user(user)
    doc = frappe.new_doc(LOG_DOCTYPE)
    doc.user = user
    doc.user_full_name = _full_name(user)
    doc.employee = employee
    doc.employee_name = employee_name
    doc.pos_profile = pos_profile
    doc.action = action
    doc.source = source
    doc.day_access = day_access
    doc.notes = notes
    doc.changed_by = changed_by
    doc.changed_by_name = _full_name(changed_by)
    doc.flags.ignore_permissions = True
    doc.insert(ignore_permissions=True)
    return doc.name


# ---------------------------------------------------------------------------
# Day access records
# ---------------------------------------------------------------------------

_GRANT_FIELDS = [
    "name",
    "user",
    "pos_profile",
    "access_date",
    "status",
    "row_added",
    "starts_at",
    "expires_at",
    "granted_by",
    "last_deferral",
]


def window_for(access_date: Any) -> Tuple[datetime, datetime]:
    """(starts_at, expires_at) for a day access on *access_date*."""
    day = getdate(access_date)
    return (
        datetime.combine(day, time(0, 0)),
        datetime.combine(getdate(add_days(day, 1)), time(EXPIRY_HOUR, 0)),
    )


def _load_grant(name: str) -> Optional[Dict[str, Any]]:
    row = frappe.db.get_value(DAY_ACCESS_DOCTYPE, name, _GRANT_FIELDS, as_dict=True)
    return dict(row) if row else None


def _set_grant(name: str, values: Dict[str, Any]) -> None:
    """Write status fields straight to the row.

    ``db.set_value`` rather than ``doc.save()`` on purpose: the controller's
    duplicate check is a guard for *creating* a grant, and re-running it on a
    status change could refuse to end a grant that already exists.
    """
    frappe.db.set_value(DAY_ACCESS_DOCTYPE, name, values)


def _create_grant(values: Dict[str, Any]) -> str:
    doc = frappe.new_doc(DAY_ACCESS_DOCTYPE)
    doc.update(values)
    doc.flags.ignore_permissions = True
    doc.insert(ignore_permissions=True)
    return doc.name


def _active_grants(user: str, pos_profile: str, exclude: Optional[str] = None) -> List[Dict[str, Any]]:
    filters: Dict[str, Any] = {"user": user, "pos_profile": pos_profile, "status": STATUS_ACTIVE}
    if exclude:
        filters["name"] = ("!=", exclude)
    return [
        dict(r)
        for r in frappe.get_all(
            DAY_ACCESS_DOCTYPE, filters=filters, fields=["name", "row_added", "access_date"]
        )
        or []
    ]


def _open_grants_on(user: str, pos_profile: str, day: Any) -> List[str]:
    return frappe.get_all(
        DAY_ACCESS_DOCTYPE,
        filters={
            "user": user,
            "pos_profile": pos_profile,
            "access_date": getdate(day),
            "status": ("in", OPEN_STATUSES),
        },
        pluck="name",
    ) or []


def _is_permanent_member(user: str, pos_profile: str, exclude: Optional[str] = None) -> bool:
    """A row exists AND no running day access is the reason it exists."""
    if not _membership_rows(user, pos_profile):
        return False
    return not any(cint(g.get("row_added")) for g in _active_grants(user, pos_profile, exclude))


def grant_payload(grant: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "name": grant.get("name"),
        "user": grant.get("user"),
        "pos_profile": grant.get("pos_profile"),
        "access_date": _fmt(getdate(grant["access_date"])) if grant.get("access_date") else None,
        "status": grant.get("status"),
        "starts_at": _fmt(grant.get("starts_at")),
        "expires_at": _fmt(grant.get("expires_at")),
        "row_added": bool(cint(grant.get("row_added"))),
    }


def _start_grant(
    grant: Dict[str, Any],
    *,
    source: str,
    changed_by: Optional[str],
    notes: Optional[str] = None,
) -> int:
    """Activate a grant; returns its ``row_added``.

    A row that exists only because another running grant added it is NOT
    permanent access, so this grant claims it too (``row_added=1``). Marking it
    0 would mean neither grant removes the row once the first one has ended --
    the "cover keeps access forever" failure.
    """
    user, profile, name = grant["user"], grant["pos_profile"], grant["name"]
    if _membership_rows(user, profile):
        row_added = 0 if _is_permanent_member(user, profile, exclude=name) else 1
    else:
        _insert_membership_row(user, profile)
        row_added = 1
    _set_grant(
        name,
        {
            "status": STATUS_ACTIVE,
            "row_added": row_added,
            "started_at": _now(),
            "last_deferral": None,
        },
    )
    _invalidate(user, profile)
    note_parts = [notes]
    if not row_added:
        note_parts.append(_("Already a permanent member; nothing was added."))
    _log(
        user,
        profile,
        ACTION_STARTED,
        source,
        notes="\n".join(p for p in note_parts if p) or None,
        day_access=name,
        changed_by=changed_by,
    )
    return row_added


def _end_grant(
    grant: Dict[str, Any],
    *,
    status: str,
    action: str,
    source: str,
    changed_by: Optional[str],
    notes: Optional[str] = None,
) -> bool:
    """Close a running grant; returns whether a membership row was removed.

    The row goes only when this grant added it AND no other running grant for
    the same user and branch still stands on it.
    """
    user, profile, name = grant["user"], grant["pos_profile"], grant["name"]
    removed = False
    if cint(grant.get("row_added")) and not _active_grants(user, profile, exclude=name):
        removed = _delete_membership_rows(user, profile) > 0
    _set_grant(name, {"status": status, "ended_at": _now()})
    _invalidate(user, profile)
    if not removed and not notes:
        notes = (
            _("Access kept: another day access for this branch is still running.")
            if cint(grant.get("row_added"))
            else _("Permanent member; nothing was removed.")
        )
    _log(user, profile, action, source, notes=notes, day_access=name, changed_by=changed_by)
    return removed


# ---------------------------------------------------------------------------
# Public operations
# ---------------------------------------------------------------------------


def set_branch_access(
    user: str,
    pos_profile: str,
    allowed: bool,
    notes: Optional[str] = None,
    source: str = SOURCE_SCREEN,
) -> Dict[str, Any]:
    """Make *user* a permanent member of *pos_profile* (``allowed``) or not.

    Adding while a day access is running converts it: the row that grant
    inserted becomes permanent (``row_added`` -> 0) instead of being removed at
    03:00 against the manager's latest decision. No row changes, so no
    open-branch check is needed for that case.

    Removing deletes the row(s) and cancels any running day access for the
    branch, because a grant still marked Active with no row behind it would
    tell the next manager the person has access when they do not. Scheduled
    future grants are left alone; they are a separate decision about a
    separate day.
    """
    # Door first: a caller below the line-manager tier must not learn which
    # users exist or are disabled from the target lookup's error message.
    ensure_access()
    target = _ensure_target_user(user, for_adding=bool(allowed))
    user = target["name"]
    profile = _clean(pos_profile)
    ensure_can_manage(profile, user)
    notes = _clean(notes)
    caller = _session_user()
    _lock_branch(profile)

    rows = _membership_rows(user, profile)
    running = _active_grants(user, profile)
    temporary = [g for g in running if cint(g.get("row_added"))]

    result: Dict[str, Any] = {
        "changed": False,
        "user": user,
        "pos_profile": profile,
        "allowed": bool(allowed),
        "log": None,
    }

    if allowed:
        if rows and not temporary:
            return result
        if not rows:
            ensure_branch_closed(profile)
            _insert_membership_row(user, profile)
            _invalidate(user, profile)
            log_notes = notes
        else:
            for grant in temporary:
                _set_grant(grant["name"], {"row_added": 0})
            converted = ", ".join(g["name"] for g in temporary)
            log_notes = "\n".join(
                filter(None, [_("Made permanent (was day access {0}).").format(converted), notes])
            )
        result["changed"] = True
        result["log"] = _log(user, profile, ACTION_ADDED, source, notes=log_notes, changed_by=caller)
        return result

    if not rows:
        # The row is already gone (removed in Desk, say). A grant still marked
        # Active would tell the next manager they have access when they do not,
        # so it is closed here; nothing changes on the branch, so no open-shift
        # check is needed.
        now = _now()
        for grant in running:
            _set_grant(grant["name"], {"status": STATUS_CANCELLED, "ended_at": now})
            _log(
                user,
                profile,
                ACTION_CANCELLED,
                source,
                notes=_("Cancelled: the branch access it relied on had already been removed."),
                day_access=grant["name"],
                changed_by=caller,
            )
        if running:
            result["changed"] = True
        return result

    ensure_branch_closed(profile)
    _delete_membership_rows(user, profile)
    _invalidate(user, profile)
    result["changed"] = True
    result["log"] = _log(user, profile, ACTION_REMOVED, source, notes=notes, changed_by=caller)
    now = _now()
    for grant in running:
        _set_grant(grant["name"], {"status": STATUS_CANCELLED, "ended_at": now})
        _log(
            user,
            profile,
            ACTION_CANCELLED,
            source,
            notes=_("Cancelled because branch access was removed."),
            day_access=grant["name"],
            changed_by=caller,
        )
    return result


def grant_day_access(
    user: str,
    pos_profile: str,
    access_date: Any,
    notes: Optional[str] = None,
    source: str = SOURCE_SCREEN,
    roster_day_off: Optional[str] = None,
) -> Dict[str, Any]:
    """Give *user* POS access to *pos_profile* for one branch day.

    Today starts at once, which is refused while the branch is open (a manager
    can retry after closing, or book it for tomorrow). A later day, up to
    ``MAX_DAYS_AHEAD``, is Scheduled and started by the hourly job once the
    branch is closed. Past days are refused: access that has already ended
    cannot be granted.
    """
    ensure_access()
    target = _ensure_target_user(user, for_adding=True)
    user = target["name"]
    profile = _clean(pos_profile)
    ensure_can_manage(profile, user)
    notes = _clean(notes)
    caller = _session_user()
    _lock_branch(profile)

    # Checked before getdate(): getdate(None) quietly answers "today", which
    # would turn a missing date into an immediate grant.
    if not access_date:
        frappe.throw(_("Pick the day the access is for."))
    try:
        day = getdate(access_date)
    except Exception:
        frappe.throw(_("{0} is not a valid date.").format(access_date))

    today = _now().date()
    if day < today:
        frappe.throw(
            _("{0} is in the past. Day access can only be given for today or a later day.").format(day)
        )
    last_day = getdate(add_days(today, MAX_DAYS_AHEAD))
    if day > last_day:
        frappe.throw(
            _("Day access can be booked at most {0} days ahead (until {1}).").format(
                MAX_DAYS_AHEAD, last_day
            )
        )

    display = target.get("full_name") or user
    if _is_permanent_member(user, profile):
        frappe.throw(
            _("{0} already has access to {1}.").format(display, profile), AlreadyHasAccessError
        )
    if _open_grants_on(user, profile, day):
        frappe.throw(
            _("{0} already has day access to {1} on {2}.").format(display, profile, day),
            AlreadyHasAccessError,
        )

    starts_today = day == today
    if starts_today:
        # Checked before anything is written so a refusal leaves no Scheduled
        # grant behind for the job to start later without the manager knowing.
        ensure_branch_closed(profile)

    starts_at, expires_at = window_for(day)
    employee, employee_name = _employee_for_user(user)
    name = _create_grant(
        {
            "user": user,
            "user_full_name": display,
            "employee": employee,
            "employee_name": employee_name,
            "pos_profile": profile,
            "access_date": day,
            "status": STATUS_SCHEDULED,
            "row_added": 0,
            "starts_at": starts_at,
            "expires_at": expires_at,
            "source": source,
            "roster_day_off": roster_day_off,
            "notes": notes,
            "granted_by": caller,
            "granted_by_name": _full_name(caller),
        }
    )
    grant = {
        "name": name,
        "user": user,
        "pos_profile": profile,
        "access_date": day,
        "status": STATUS_SCHEDULED,
        "row_added": 0,
        "starts_at": starts_at,
        "expires_at": expires_at,
    }
    if starts_today:
        # The manager's note rides on the Started row; a today-grant gets no
        # separate Scheduled row because it was never waiting for anything.
        grant["row_added"] = _start_grant(grant, source=source, changed_by=caller, notes=notes)
        grant["status"] = STATUS_ACTIVE
        message = _("{0} now has POS access to {1} until {2}.").format(
            display, profile, expires_at.strftime("%Y-%m-%d %H:%M")
        )
    else:
        _log(user, profile, ACTION_SCHEDULED, source, notes=notes, day_access=name, changed_by=caller)
        message = _(
            "{0} will get POS access to {1} on {2}, from when the branch is closed until {3}."
        ).format(display, profile, day, expires_at.strftime("%Y-%m-%d %H:%M"))

    return {"day_access": grant_payload(grant), "message": message}


def cancel_day_access(name: str) -> Dict[str, Any]:
    """Withdraw a day access before it runs out.

    Scheduled -> Cancelled with nothing else to undo. Active -> refused while
    the branch is open; otherwise the row it added is removed (same rule as
    expiry) and it is Cancelled.
    """
    ensure_access()
    name = _clean(name)
    grant = _load_grant(name) if name else None
    if not grant:
        frappe.throw(_("No such day access: {0}").format(name))

    ensure_can_manage(grant["pos_profile"], grant["user"])
    caller = _session_user()
    _lock_branch(grant["pos_profile"])
    grant = _load_grant(grant["name"]) or grant

    if grant["status"] == STATUS_SCHEDULED:
        _set_grant(grant["name"], {"status": STATUS_CANCELLED, "ended_at": _now()})
        _log(
            grant["user"],
            grant["pos_profile"],
            ACTION_CANCELLED,
            SOURCE_SCREEN,
            notes=_("Cancelled before it started."),
            day_access=grant["name"],
            changed_by=caller,
        )
        grant["status"] = STATUS_CANCELLED
    elif grant["status"] == STATUS_ACTIVE:
        ensure_branch_closed(grant["pos_profile"])
        _end_grant(
            grant,
            status=STATUS_CANCELLED,
            action=ACTION_CANCELLED,
            source=SOURCE_SCREEN,
            changed_by=caller,
        )
        grant["status"] = STATUS_CANCELLED
    else:
        frappe.throw(
            _("This day access is already {0}.").format(str(grant["status"] or "").lower())
        )

    return {"day_access": grant_payload(grant)}


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def profiles_by_shift_location() -> Dict[str, str]:
    """{Shift Location: enabled POS Profile}. First profile by name wins if
    two ever claim the same location."""
    if not roster_service._pos_profile_location_field_exists():
        return {}
    field = roster_service.POS_PROFILE_LOCATION_FIELD
    out: Dict[str, str] = {}
    try:
        rows = frappe.get_all(
            "POS Profile",
            filters={"disabled": 0, field: ("is", "set")},
            fields=["name", field],
            order_by="name asc",
        ) or []
    except Exception:
        return {}
    for row in rows:
        loc = row.get(field)
        if loc and loc not in out:
            out[loc] = row["name"]
    return out


def profile_for_shift_location(shift_location: Optional[str]) -> Optional[str]:
    if not shift_location:
        return None
    return profiles_by_shift_location().get(shift_location)


def get_branch_access() -> Dict[str, Any]:
    """Everything the Branch Access screen draws, in one call."""
    ensure_access()
    caller = _session_user()
    manage_all = can_manage_all(caller)
    manageable = set(manageable_profiles(caller))
    enabled = _enabled_profiles()

    location_by_profile = {p: loc for loc, p in profiles_by_shift_location().items()}
    branches = [
        {
            "pos_profile": profile,
            "shift_location": location_by_profile.get(profile),
            "manageable": profile in manageable,
            "open_shift": _open_shift_payload(profile),
        }
        for profile in enabled
    ]

    memberships: Dict[str, set] = {}
    if enabled:
        for row in frappe.get_all(
            MEMBER_DOCTYPE,
            filters={"parenttype": MEMBER_PARENTTYPE, "parent": ("in", enabled)},
            fields=["parent", "user"],
        ) or []:
            if row.get("user"):
                memberships.setdefault(row["user"], set()).add(row["parent"])

    employees: Dict[str, Dict[str, Any]] = {}
    active_employee_users: set = set()
    for row in frappe.get_all(
        "Employee",
        filters={"user_id": ("is", "set")},
        fields=["name", "employee_name", "user_id", "status"],
        order_by="status asc",
    ) or []:
        uid = row.get("user_id")
        if not uid:
            continue
        # status asc puts Active first, so the first record seen wins.
        employees.setdefault(uid, row)
        if row.get("status") == "Active":
            active_employee_users.add(uid)

    candidates = (active_employee_users | set(memberships)) - EXCLUDED_USERS
    users_info: Dict[str, Dict[str, Any]] = {}
    if candidates:
        for row in frappe.get_all(
            "User",
            filters={"name": ("in", sorted(candidates))},
            fields=["name", "full_name", "enabled"],
        ) or []:
            users_info[row["name"]] = row

    grants_by_user: Dict[str, List[Dict[str, Any]]] = {}
    if enabled:
        for row in frappe.get_all(
            DAY_ACCESS_DOCTYPE,
            filters={"status": ("in", OPEN_STATUSES), "pos_profile": ("in", enabled)},
            fields=["name", "user", "pos_profile", "access_date", "status", "expires_at", "row_added"],
            order_by="access_date asc",
        ) or []:
            grants_by_user.setdefault(row["user"], []).append(
                {
                    "name": row["name"],
                    "pos_profile": row["pos_profile"],
                    "access_date": _fmt(getdate(row["access_date"])) if row.get("access_date") else None,
                    "status": row["status"],
                    "expires_at": _fmt(row.get("expires_at")),
                    "row_added": bool(cint(row.get("row_added"))),
                }
            )

    users: List[Dict[str, Any]] = []
    for uid in candidates:
        info = users_info.get(uid)
        if not info:
            continue
        enabled_flag = bool(cint(info.get("enabled")))
        # Employee users are listed only while their login works; a member row
        # keeps a disabled user listed so the manager can see and remove it.
        if uid not in memberships and not enabled_flag:
            continue
        emp = employees.get(uid) or {}
        users.append(
            {
                "user": uid,
                "full_name": info.get("full_name") or uid,
                "employee": emp.get("name"),
                "employee_name": emp.get("employee_name"),
                "enabled": enabled_flag,
                "is_self": uid == caller,
                "editable": can_edit_user(uid, caller),
                "branches": sorted(memberships.get(uid, set())),
                "day_access": grants_by_user.get(uid, []),
            }
        )
    users.sort(key=lambda u: (str(u["full_name"] or "").lower(), u["user"]))

    return {"can_manage_all": manage_all, "branches": branches, "users": users}


def get_access_log(
    pos_profile: Optional[str] = None,
    user: Optional[str] = None,
    limit: int = 50,
    start: int = 0,
) -> Dict[str, Any]:
    """The change history, newest first. A line manager sees only their branches."""
    ensure_access()
    caller = _session_user()
    limit = max(1, min(cint(limit) or 50, 200))
    start = max(0, cint(start))
    pos_profile = _clean(pos_profile)
    user = _clean(user)

    filters: Dict[str, Any] = {}
    if not can_manage_all(caller):
        scope = manageable_profiles(caller)
        if pos_profile and pos_profile not in scope:
            frappe.throw(
                _("You can only see the history of branches you belong to."),
                frappe.PermissionError,
            )
        if not scope:
            return {"rows": [], "has_more": False}
        filters["pos_profile"] = pos_profile or ("in", scope)
    elif pos_profile:
        filters["pos_profile"] = pos_profile
    if user:
        filters["user"] = user

    rows = frappe.get_all(
        LOG_DOCTYPE,
        filters=filters,
        fields=[
            "name",
            "creation",
            "user",
            "user_full_name",
            "employee_name",
            "pos_profile",
            "action",
            "source",
            "notes",
            "changed_by",
            "changed_by_name",
            "day_access",
        ],
        order_by="creation desc",
        limit_start=start,
        limit_page_length=limit + 1,
    ) or []
    has_more = len(rows) > limit
    out = []
    for row in rows[:limit]:
        item = dict(row)
        item["creation"] = _fmt(item.get("creation"))
        out.append(item)
    return {"rows": out, "has_more": has_more}


# ---------------------------------------------------------------------------
# Roster integration
# ---------------------------------------------------------------------------


def _message_log_mark() -> Optional[int]:
    try:
        return len(frappe.local.message_log)
    except Exception:
        return None


def _message_log_reset(mark: Optional[int]) -> None:
    """Drop messages a caught ``frappe.throw`` queued.

    ``frappe.throw`` appends to ``message_log`` before raising, and Frappe
    ships that log to the client with the response. A refused grant that the
    roster handled would otherwise pop up as an error dialog on a roster write
    that succeeded.
    """
    if mark is None:
        return
    try:
        del frappe.local.message_log[mark:]
    except Exception:
        pass


def _exc_text(exc: Exception) -> str:
    text = str(exc or "").strip()
    return text or _("The POS access could not be given.")


def grant_for_roster(
    employee: str,
    on_date: Any,
    shift_location: Optional[str],
    *,
    source: str,
    roster_day_off: Optional[str] = None,
    notes: Optional[str] = None,
) -> Dict[str, Any]:
    """Best-effort day access for somebody the roster just placed at a branch.

    Never raises and never undoes the roster write: the rota is the decision
    the manager made, the POS access is a convenience on top of it. The grant
    runs inside a savepoint so a failure half-way (row inserted, log refused)
    is rolled back to nothing rather than leaving a membership row behind.
    """
    outcome: Dict[str, Any] = {
        "requested": True,
        "granted": False,
        "status": None,
        "already_member": False,
        "reason": None,
        "pos_profile": None,
        "day_access": None,
    }

    try:
        profile = profile_for_shift_location(shift_location)
    except Exception:
        profile = None
    if not profile:
        outcome["reason"] = _("{0} has no POS branch, so there is no POS access to give.").format(
            shift_location or _("This location")
        )
        return outcome
    outcome["pos_profile"] = profile

    try:
        user = frappe.db.get_value("Employee", employee, "user_id")
    except Exception:
        user = None
    if not user:
        name = frappe.db.get_value("Employee", employee, "employee_name") or employee
        outcome["reason"] = _("{0} has no user account, so they cannot be given POS access.").format(name)
        return outcome

    savepoint = "jarz_roster_pos_access"
    mark = _message_log_mark()
    frappe.db.savepoint(savepoint)
    try:
        result = grant_day_access(
            user, profile, on_date, notes=notes, source=source, roster_day_off=roster_day_off
        )
    except AlreadyHasAccessError as exc:
        frappe.db.rollback(save_point=savepoint)
        _message_log_reset(mark)
        outcome["already_member"] = True
        outcome["reason"] = _exc_text(exc)
        return outcome
    except Exception as exc:
        # BranchOpenError, PermissionError (not your branch / your own access),
        # a past date, or a real failure: all reported, none propagated.
        frappe.db.rollback(save_point=savepoint)
        _message_log_reset(mark)
        if not isinstance(exc, (frappe.ValidationError, frappe.PermissionError)):
            _safe_log(f"Roster POS access grant failed for {employee}")
        outcome["reason"] = _exc_text(exc)
        return outcome

    grant = result.get("day_access") or {}
    outcome["granted"] = True
    outcome["status"] = grant.get("status")
    outcome["day_access"] = grant.get("name")
    return outcome


def release_for_day_off(day_off: str) -> None:
    """Detach and wind down the day accesses a roster cover created.

    Called by the roster before it deletes a ``Jarz Roster Day Off``: the grant
    links to it, and Frappe refuses to delete a record something still links
    to, so without this an undone cover could never be undone. The cover is no
    longer happening, so its access goes too -- Scheduled ones are cancelled,
    Active ones are ended now if the branch is closed. An Active one on an open
    branch keeps running (no change while open) and the hourly job ends it at
    its normal expiry; only its link is cleared.
    """
    if not day_off:
        return
    try:
        names = frappe.get_all(
            DAY_ACCESS_DOCTYPE, filters={"roster_day_off": day_off}, pluck="name"
        ) or []
    except Exception:
        return
    caller = _session_user()
    now = _now()
    note = _("Cancelled because the cover it was given for was undone.")
    for name in names:
        grant = _load_grant(name)
        if grant and grant["status"] == STATUS_SCHEDULED:
            _set_grant(name, {"status": STATUS_CANCELLED, "ended_at": now})
            _log(
                grant["user"],
                grant["pos_profile"],
                ACTION_CANCELLED,
                SOURCE_COVER,
                notes=note,
                day_access=name,
                changed_by=caller,
            )
        elif grant and grant["status"] == STATUS_ACTIVE:
            _lock_branch(grant["pos_profile"])
            if not _open_shift(grant["pos_profile"]):
                _end_grant(
                    grant,
                    status=STATUS_CANCELLED,
                    action=ACTION_CANCELLED,
                    source=SOURCE_COVER,
                    changed_by=caller,
                    notes=note,
                )
        _set_grant(name, {"roster_day_off": None})


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


def _scan(kind: str, now: datetime) -> List[str]:
    """Names of grants the cycle must look at for *kind*."""
    if kind == "start":
        filters = {"status": STATUS_SCHEDULED, "starts_at": ("<=", now), "expires_at": (">", now)}
        order = "starts_at asc"
    elif kind == "end":
        filters = {"status": STATUS_ACTIVE, "expires_at": ("<=", now)}
        order = "expires_at asc"
    else:
        filters = {"status": STATUS_SCHEDULED, "expires_at": ("<=", now)}
        order = "expires_at asc"
    return frappe.get_all(
        DAY_ACCESS_DOCTYPE,
        filters=filters,
        pluck="name",
        order_by=order,
        limit_page_length=CYCLE_BATCH_LIMIT,
    ) or []


def _defer(grant: Dict[str, Any], shift: Dict[str, Any], verb: str, now: datetime) -> None:
    _set_grant(
        grant["name"],
        {
            "last_deferral": _("Could not {0} at {1}: {2}").format(
                verb, now.strftime("%Y-%m-%d %H:%M"), open_shift_message(grant["pos_profile"], shift)
            )
        },
    )


def _start_blocker(grant: Dict[str, Any]) -> Optional[str]:
    """Why a grant booked days ago must not start now, or None.

    A grant can be booked 14 days ahead; by its day the person may have been
    disabled, the branch closed down, or the line manager who booked it moved
    off that branch. Starting it anyway would grant access nobody could grant
    today.
    """
    row = _user_row(grant["user"])
    if not row or not cint(row.get("enabled")):
        return _("The user is disabled.")
    if grant["pos_profile"] not in _enabled_profiles():
        return _("{0} is no longer an active branch.").format(grant["pos_profile"])
    granter = grant.get("granted_by")
    if (
        granter
        and granter != ROLES.ADMINISTRATOR
        and grant["pos_profile"] not in manageable_profiles(granter)
    ):
        return _("{0} no longer manages {1}.").format(_full_name(granter), grant["pos_profile"])
    return None


def _cycle_one(kind: str, name: str, now: datetime, summary: Dict[str, int]) -> None:
    grant = _load_grant(name)
    if not grant:
        return
    _lock_branch(grant["pos_profile"])
    # Re-read under the lock: a manager may have converted or cancelled it
    # between the scan and here.
    grant = _load_grant(name)
    if not grant:
        return
    changed_by = grant.get("granted_by") or _session_user()

    if kind == "start":
        if grant["status"] != STATUS_SCHEDULED:
            return
        blocker = _start_blocker(grant)
        if blocker:
            reason = _("Not started: {0}").format(blocker)
            _set_grant(
                grant["name"],
                {"status": STATUS_CANCELLED, "ended_at": now, "last_deferral": reason},
            )
            _log(
                grant["user"],
                grant["pos_profile"],
                ACTION_CANCELLED,
                SOURCE_SCHEDULER,
                notes=reason,
                day_access=grant["name"],
                changed_by=changed_by,
            )
            summary["cancelled"] += 1
            return
        shift = _open_shift(grant["pos_profile"])
        if shift:
            _defer(grant, shift, _("start"), now)
            summary["deferred"] += 1
            return
        _start_grant(grant, source=SOURCE_SCHEDULER, changed_by=changed_by)
        summary["started"] += 1
    elif kind == "end":
        if grant["status"] != STATUS_ACTIVE:
            return
        shift = _open_shift(grant["pos_profile"])
        if shift:
            # Leave it Active. Pulling access mid-shift is exactly what rule 3
            # forbids; the next hourly pass retries once the branch closes.
            _defer(grant, shift, _("end"), now)
            summary["deferred"] += 1
            return
        _end_grant(
            grant,
            status=STATUS_ENDED,
            action=ACTION_ENDED,
            source=SOURCE_SCHEDULER,
            changed_by=changed_by,
        )
        summary["ended"] += 1
    else:
        if grant["status"] != STATUS_SCHEDULED:
            return
        reason = _("Its day ended before the branch was ever closed long enough to start it.")
        if grant.get("last_deferral"):
            reason = f"{reason} {grant['last_deferral']}"
        _set_grant(
            grant["name"],
            {"status": STATUS_CANCELLED, "ended_at": now, "last_deferral": reason},
        )
        _log(
            grant["user"],
            grant["pos_profile"],
            ACTION_CANCELLED,
            SOURCE_SCHEDULER,
            notes=reason,
            day_access=grant["name"],
            changed_by=changed_by,
        )
        summary["cancelled"] += 1


def run_day_access_cycle() -> Dict[str, int]:
    """Hourly: start due grants, end expired ones, cancel ones that never ran.

    Never raises. Each grant is handled inside its own savepoint so one bad row
    is rolled back alone and the rest still move; a failure is logged through a
    guarded ``log_error`` because that call can itself raise.

    Order matters. Starting before ending means a person holding back-to-back
    days keeps their row continuously: tomorrow's grant claims it before
    today's lets go, instead of the row being deleted and re-inserted with a
    gap in between. Stale cancellation runs first so a grant whose window has
    already closed is never started.
    """
    summary = {"started": 0, "ended": 0, "cancelled": 0, "deferred": 0, "errors": 0}
    try:
        if not frappe.db.exists("DocType", DAY_ACCESS_DOCTYPE):
            return summary
        now = _now()
        for kind in ("stale", "start", "end"):
            try:
                names = _scan(kind, now)
            except Exception:
                summary["errors"] += 1
                _safe_log(f"Day access cycle: {kind} scan failed")
                continue
            for name in names:
                savepoint = "jarz_day_access_cycle"
                try:
                    frappe.db.savepoint(savepoint)
                    _cycle_one(kind, name, now, summary)
                except Exception:
                    summary["errors"] += 1
                    try:
                        frappe.db.rollback(save_point=savepoint)
                    except Exception:
                        pass
                    _safe_log(f"Day access cycle: {kind} failed for {name}")
    except Exception:
        summary["errors"] += 1
        _safe_log("Day access cycle failed")
    return summary
