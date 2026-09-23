"""Task Board rules: who is who, who sees what, who may do what.

Every rule the Task Board enforces lives here, in one place, so the API
(``jarz_pos.api.tasks``), the Desk permission hooks (``jarz_pos.permissions.tasks``),
the pending-approvals counts and the daily reminder pass cannot disagree. The
contract is ``.scratch/task_board/CONTRACT.md`` (feature ``task-board``).

The module is split in two on purpose:

* **Pure rules** (the top half). They take a :class:`Viewer` and a task that is
  either a Document or a plain dict, read it through ``.get()`` only, and make
  no database call. That is what lets ``tests/test_task_board.py`` pin the
  whole visibility/permission/transition matrix with plain unittest and no site.
* **Lookups** (the bottom half). The few queries the rules need -- who holds a
  manager role, who is on the full-access list, who may be put on the board.
  They are memoised per request on ``frappe.local`` because a single board load
  asks "is this user a manager?" once per card.

People:

* **Board user** -- holds any of ``ROLES.LINE_MANAGER_TIER`` or is on the
  Jarz Task Settings full-access list.
* **Manager** -- Administrator, or holds ``JARZ Manager`` / ``System Manager``.
* **Line manager** -- a board user who is not a manager.
* **Full access** -- Administrator, or on the full-access list. Sees everything.

Visibility (task T, user U): U is full-access; or U created T, is its
assignee, or is assigned one of its subtasks; or U is a manager and T's
assignee is NOT a manager -- managers see all line-manager work, but a task one
manager hands another stays between the two of them.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import frappe

from jarz_pos.constants import ROLES

# ── DocTypes ────────────────────────────────────────────────────────────────

TASK_DOCTYPE = "Jarz Task"
SUBTASK_DOCTYPE = "Jarz Task Subtask"
ENTRY_DOCTYPE = "Jarz Task Entry"
SETTINGS_DOCTYPE = "Jarz Task Settings"
SETTINGS_USER_DOCTYPE = "Jarz Task Settings User"

# ── Status / priority ──────────────────────────────────────────────────────

STATUS_TODO = "To Do"
STATUS_IN_PROGRESS = "In Progress"
STATUS_IN_REVIEW = "In Review"
STATUS_DONE = "Done"
#: Board column order. The wire contract: the client renders these verbatim.
STATUSES: Tuple[str, ...] = (STATUS_TODO, STATUS_IN_PROGRESS, STATUS_IN_REVIEW, STATUS_DONE)
#: Statuses in which the assignee is still working the card.
OPEN_STATUSES: Tuple[str, ...] = (STATUS_TODO, STATUS_IN_PROGRESS)

PRIORITY_NORMAL = "Normal"
PRIORITY_HIGH = "High"
PRIORITY_URGENT = "Urgent"
PRIORITIES: Tuple[str, ...] = (PRIORITY_NORMAL, PRIORITY_HIGH, PRIORITY_URGENT)
#: Board sort: Urgent first, then High, then Normal.
PRIORITY_RANK: Dict[str, int] = {PRIORITY_URGENT: 0, PRIORITY_HIGH: 1, PRIORITY_NORMAL: 2}

# ── Entries ────────────────────────────────────────────────────────────────

KIND_LOG = "Log"
KIND_COMMENT = "Comment"
KIND_HISTORY = "History"
#: Kinds a person may write. History is written by the server only.
USER_ENTRY_KINDS: Tuple[str, ...] = (KIND_LOG, KIND_COMMENT)

# History ``event`` codes (Jarz Task Entry.event). Wire contract.
EV_CREATED = "created"
EV_REASSIGNED = "reassigned"
EV_STATUS = "status"
EV_DUE_DATE = "due_date"
EV_PRIORITY = "priority"
EV_TITLE = "title"
EV_BRANCH = "branch"
EV_SUBTASK_ADDED = "subtask_added"
EV_SUBTASK_DONE = "subtask_done"
EV_SUBTASK_UNDONE = "subtask_undone"
EV_SUBTASK_REMOVED = "subtask_removed"
EV_ATTACHMENT_ADDED = "attachment_added"
EV_ATTACHMENT_REMOVED = "attachment_removed"
EV_ARCHIVED = "archived"
EV_UNARCHIVED = "unarchived"

# Push ``event`` values (data payload of a ``task_notification``). Wire contract.
N_ASSIGNED = "assigned"
N_UNASSIGNED = "unassigned"
N_SUBTASK_ASSIGNED = "subtask_assigned"
N_SUBTASK_DONE = "subtask_done"
N_SUBMITTED = "submitted"
N_APPROVED = "approved"
N_SENT_BACK = "sent_back"
N_REOPENED = "reopened"
N_MENTIONED = "mentioned"
N_DUE_SOON = "due_soon"
N_OVERDUE = "overdue"

# ── Limits ─────────────────────────────────────────────────────────────────

#: Per attachment, measured AFTER base64 decoding.
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
#: The only content types ``download_attachment`` serves inline. Everything
#: else is forced to download: the response comes from the site's own origin
#: with the caller's session, so an HTML/SVG/XML file rendered inline is stored
#: XSS against whoever opens it.
INLINE_CONTENT_TYPES = frozenset(
    {"image/png", "image/jpeg", "image/gif", "image/webp", "application/pdf"}
)
#: Refused at upload outright -- markup and script a browser would execute.
BLOCKED_UPLOAD_EXTENSIONS = frozenset(
    {".html", ".htm", ".svg", ".xml", ".js", ".mjs", ".xhtml"}
)
#: Hard cap per reminder pass (tasks, and separately subtasks).
REMINDER_BATCH_CAP = 500
#: Safety cap on one board load. The board is a working surface, not an archive.
BOARD_ROW_CAP = 2000
TITLE_MAX_LENGTH = 140
MENTION_BODY_MAX = 120

#: The one user seeded onto the full-access list (only if the list is empty).
SEED_FULL_ACCESS_USER = "abdelrahmanmamdouh1996@gmail.com"

#: Roles that make a user a board "manager". ``ROLES.TASK_MANAGER`` minus the
#: Administrator role: the Administrator USER is a manager by name (see
#: ``Viewer.is_manager``), and the role alone is not something people hold.
MANAGER_ROLES = frozenset(ROLES.TASK_MANAGER - {ROLES.ADMINISTRATOR})
BOARD_ROLES = frozenset(ROLES.LINE_MANAGER_TIER)

MSG_OPEN_SUBTASKS = "Finish or remove the open subtasks first"
MSG_ARCHIVED = "This task is archived and read-only. Unarchive it first."
MSG_DONE_LOCKED = "This task is done. Reopen it first."


# ═══════════════════════════════════════════════════════════════════════════
# Pure rules
# ═══════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class Viewer:
    """Everything the rules need to know about the person asking.

    ``managers`` is the set of every manager user on the site. It is what rule 3
    of visibility ("T.assigned_to is not a manager") is evaluated against, and it
    is resolved once per request rather than once per card.
    """

    user: str
    roles: frozenset = frozenset()
    full_access: bool = False
    managers: frozenset = frozenset()

    @property
    def is_admin(self) -> bool:
        return self.user == ROLES.ADMINISTRATOR

    @property
    def is_manager(self) -> bool:
        return self.is_admin or bool(self.roles & MANAGER_ROLES)

    @property
    def can_view_all(self) -> bool:
        return self.is_admin or bool(self.full_access)

    @property
    def is_board_user(self) -> bool:
        if not self.user or self.user == "Guest":
            return False
        return self.can_view_all or bool(self.roles & BOARD_ROLES)

    @property
    def is_line_manager(self) -> bool:
        return self.is_board_user and not self.is_manager

    @property
    def can_create(self) -> bool:
        return self.is_board_user and (self.is_manager or self.can_view_all)

    @property
    def can_view_overview(self) -> bool:
        return self.can_create


def make_viewer(
    user: str,
    roles: Iterable[str],
    full_access_users: Iterable[str] = (),
    managers: Iterable[str] = (),
) -> Viewer:
    """Build a :class:`Viewer` from already-resolved facts (no DB)."""
    return Viewer(
        user=str(user or ""),
        roles=frozenset(str(r) for r in (roles or []) if r),
        full_access=str(user or "") in set(full_access_users or ()),
        managers=frozenset(str(m) for m in (managers or []) if m),
    )


def _val(obj: Any, key: str, default: Any = None) -> Any:
    """Read ``key`` from a Document, frappe._dict or plain dict."""
    if obj is None:
        return default
    getter = getattr(obj, "get", None)
    value = getter(key) if callable(getter) else getattr(obj, key, None)
    return default if value is None else value


def _flag(value: Any) -> bool:
    try:
        return bool(int(value or 0))
    except (TypeError, ValueError):
        return bool(value)


def _as_date(value: Any) -> Optional[datetime.date]:
    if not value:
        return None
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    try:
        return datetime.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def subtasks_of(task: Any) -> List[Any]:
    return list(_val(task, "subtasks", []) or [])


def open_subtasks(task: Any) -> List[Any]:
    return [s for s in subtasks_of(task) if not _flag(_val(s, "is_done"))]


def participants(task: Any) -> set:
    """Creator, assignee and every subtask assignee of *task*."""
    people = {_val(task, "created_by"), _val(task, "assigned_to")}
    people.update(_val(s, "assigned_to") for s in subtasks_of(task))
    return {p for p in people if p}


def is_visible(task: Any, viewer: Viewer) -> bool:
    """The contract's visibility rule. See the module docstring."""
    if not viewer.user or viewer.user == "Guest":
        return False
    if viewer.can_view_all:
        return True
    if viewer.user in participants(task):
        return True
    if viewer.is_manager:
        assignee = _val(task, "assigned_to")
        return bool(assignee) and assignee not in viewer.managers
    return False


def is_archived(task: Any) -> bool:
    return _flag(_val(task, "archived"))


def is_done(task: Any) -> bool:
    return _val(task, "status") == STATUS_DONE


def is_locked(task: Any) -> bool:
    """Archived tasks are read-only; Done tasks accept only reopen/archive."""
    return is_archived(task) or is_done(task)


def lock_reason(task: Any) -> Optional[str]:
    if is_archived(task):
        return MSG_ARCHIVED
    if is_done(task):
        return MSG_DONE_LOCKED
    return None


def is_creator(task: Any, viewer: Viewer) -> bool:
    return bool(viewer.user) and _val(task, "created_by") == viewer.user


def is_assignee(task: Any, viewer: Viewer) -> bool:
    return bool(viewer.user) and _val(task, "assigned_to") == viewer.user


def can_edit_task(task: Any, viewer: Viewer) -> bool:
    """Title, description, priority, due date, branch, reassign."""
    return not is_locked(task) and (is_creator(task, viewer) or viewer.can_view_all)


def can_archive(task: Any, viewer: Viewer) -> bool:
    """Archive AND unarchive: allowed on any status, including Done."""
    return is_creator(task, viewer) or viewer.can_view_all


def can_assign_subtask_to_others(task: Any, viewer: Viewer) -> bool:
    """Only managers ever hand work to someone else."""
    if is_locked(task):
        return False
    if is_creator(task, viewer) or viewer.can_view_all:
        return True
    return viewer.is_manager and is_visible(task, viewer)


def can_add_subtask(task: Any, viewer: Viewer) -> bool:
    if is_locked(task):
        return False
    return can_assign_subtask_to_others(task, viewer) or is_assignee(task, viewer)


def subtask_assignee_error(task: Any, viewer: Viewer, assignee: Optional[str]) -> Optional[str]:
    """Why *viewer* may not put *assignee* on a subtask of *task*, or None.

    Unassigned and "myself" are always fine for whoever may touch the subtask;
    anybody else needs :func:`can_assign_subtask_to_others`. Whether *assignee*
    is a board user at all is a lookup, so the API checks it separately.
    """
    if not assignee or assignee == viewer.user:
        return None
    if can_assign_subtask_to_others(task, viewer):
        return None
    return "You can only assign subtasks to yourself or leave them unassigned"


def can_edit_subtask(task: Any, subtask: Any, viewer: Viewer) -> bool:
    """Edit or delete: the subtask's creator, the task creator, full access."""
    if is_locked(task):
        return False
    return (
        (bool(viewer.user) and _val(subtask, "created_by") == viewer.user)
        or is_creator(task, viewer)
        or viewer.can_view_all
    )


def can_toggle_subtask(task: Any, subtask: Any, viewer: Viewer) -> bool:
    """Tick/untick: subtask assignee, card assignee, task creator, full access."""
    if is_locked(task):
        return False
    return (
        (bool(viewer.user) and _val(subtask, "assigned_to") == viewer.user)
        or is_assignee(task, viewer)
        or is_creator(task, viewer)
        or viewer.can_view_all
    )


def can_log(task: Any, viewer: Viewer) -> bool:
    return is_visible(task, viewer) and not is_locked(task)


def can_comment(task: Any, viewer: Viewer) -> bool:
    """Comments stay open on a Done task -- it is how a reopen gets discussed."""
    return is_visible(task, viewer) and not is_archived(task)


def can_upload(task: Any, viewer: Viewer) -> bool:
    return is_visible(task, viewer) and not is_locked(task)


def upload_error(task: Any, viewer: Viewer, entry: Any = None) -> Optional[Tuple[str, str]]:
    """``(kind, message)`` refusing an upload, or None.

    Card-level files follow :func:`can_upload` (not on Done or archived cards).
    A file on an entry follows the ENTRY's own rule instead -- a Comment may be
    written on a Done card, so its files must be uploadable there too -- and
    only the entry's author may attach to it.
    """
    if entry is None:
        if can_upload(task, viewer):
            return None
        reason = lock_reason(task)
        return ("validation", reason) if reason else ("permission", "You cannot attach files to this task.")
    kind = _val(entry, "kind")
    if kind not in USER_ENTRY_KINDS:
        return ("validation", "Files can only be attached to logs and comments")
    if not viewer.user or _val(entry, "author") != viewer.user:
        return ("permission", "You can only attach files to your own log entries and comments.")
    allowed = can_comment(task, viewer) if kind == KIND_COMMENT else can_log(task, viewer)
    if allowed:
        return None
    reason = lock_reason(task)
    return ("validation", reason) if reason else ("permission", "You cannot attach files to this task.")


def blocked_upload_extension(filename: Any) -> Optional[str]:
    """The refused extension of *filename* (lower-cased), or None."""
    text = str(filename or "").strip().lower()
    dot = text.rfind(".")
    if dot < 0:
        return None
    ext = text[dot:]
    return ext if ext in BLOCKED_UPLOAD_EXTENSIONS else None


def content_disposition_for(content_type: Any) -> str:
    """``inline`` for the safe allowlist, ``attachment`` (forced download) otherwise."""
    value = str(content_type or "").split(";", 1)[0].strip().lower()
    return "inline" if value in INLINE_CONTENT_TYPES else "attachment"


def can_remove_attachment(task: Any, viewer: Viewer, uploaded_by: Optional[str]) -> bool:
    """Card-level attachments only; entry attachments are immutable."""
    if is_locked(task):
        return False
    return (
        (bool(viewer.user) and uploaded_by == viewer.user)
        or is_creator(task, viewer)
        or viewer.can_view_all
    )


# ── Status transitions ─────────────────────────────────────────────────────


def _transition_candidates(task: Any, viewer: Viewer) -> Dict[str, bool]:
    """``{target_status: reason_required}`` the viewer may move *task* to.

    Deliberately does NOT look at open subtasks: a submit (or approve) blocked
    by an open subtask is still offered, so the client shows the button and the
    server answers with the reason (``MSG_OPEN_SUBTASKS``) instead of the button
    silently disappearing.
    """
    if is_archived(task) or not is_visible(task, viewer):
        return {}
    status = _val(task, "status") or STATUS_TODO
    creator = is_creator(task, viewer)
    assignee = is_assignee(task, viewer)
    full = viewer.can_view_all
    out: Dict[str, bool] = {}

    if status in OPEN_STATUSES:
        other = STATUS_IN_PROGRESS if status == STATUS_TODO else STATUS_TODO
        if assignee or creator or full:
            out[other] = False
        if assignee:
            out[STATUS_IN_REVIEW] = False
            # Nobody to review your own task: the assignee who is also the
            # creator may close it directly.
            if _val(task, "assigned_to") == _val(task, "created_by"):
                out[STATUS_DONE] = False
    elif status == STATUS_IN_REVIEW:
        if creator or full:
            out[STATUS_DONE] = False
            out[STATUS_IN_PROGRESS] = True  # send back
    elif status == STATUS_DONE:
        if creator or full:
            out[STATUS_IN_PROGRESS] = True  # reopen
    return out


def allowed_transitions(task: Any, viewer: Viewer) -> List[str]:
    candidates = _transition_candidates(task, viewer)
    return [s for s in STATUSES if s in candidates]


def needs_reason_for(task: Any, viewer: Viewer) -> List[str]:
    candidates = _transition_candidates(task, viewer)
    return [s for s in STATUSES if candidates.get(s)]


def transition_error(
    task: Any, viewer: Viewer, target: str, reason: Optional[str] = None
) -> Optional[Tuple[str, str]]:
    """``(kind, message)`` refusing the move, or None when it is allowed.

    ``kind`` is ``"permission"`` (the caller may not make this move at all) or
    ``"validation"`` (the move is theirs but a rule blocks it right now).
    """
    if target not in STATUSES:
        return ("validation", f"Unknown status: {target}")
    if is_archived(task):
        return ("validation", MSG_ARCHIVED)
    current = _val(task, "status") or STATUS_TODO
    if target == current:
        return ("validation", f"The task is already {current}")
    candidates = _transition_candidates(task, viewer)
    if target not in candidates:
        return ("permission", f"You cannot move this task from {current} to {target}")
    if candidates[target] and not str(reason or "").strip():
        return ("validation", "A reason is required")
    # Covers submit, the assignee==creator shortcut AND approve: a subtask
    # added while the card sat In Review must not be approved past.
    if target in (STATUS_IN_REVIEW, STATUS_DONE) and open_subtasks(task):
        return ("validation", MSG_OPEN_SUBTASKS)
    return None


def transition_event(from_status: str, to_status: str) -> Optional[str]:
    """Which push a status move sends, if any."""
    if to_status == STATUS_IN_REVIEW:
        return N_SUBMITTED
    if to_status == STATUS_DONE:
        return N_APPROVED
    if to_status == STATUS_IN_PROGRESS and from_status == STATUS_IN_REVIEW:
        return N_SENT_BACK
    if to_status == STATUS_IN_PROGRESS and from_status == STATUS_DONE:
        return N_REOPENED
    return None


def transition_updates(task: Any, target: str, actor: str, now: Any) -> Dict[str, Any]:
    """Field writes that accompany a move to *target*.

    Moving back to a working column clears the review/completion stamps, so
    ``submitted_on`` / ``completed_on`` always describe the CURRENT round and
    the overview's on-time maths never reads a stale completion.
    """
    updates: Dict[str, Any] = {"status": target}
    if target in OPEN_STATUSES:
        if target == STATUS_IN_PROGRESS and not _val(task, "started_on"):
            updates["started_on"] = now
        updates.update({"submitted_on": None, "completed_on": None, "completed_by": None})
    elif target == STATUS_IN_REVIEW:
        updates["submitted_on"] = now
        if not _val(task, "started_on"):
            updates["started_on"] = now
    elif target == STATUS_DONE:
        updates["completed_on"] = now
        updates["completed_by"] = actor
        if not _val(task, "started_on"):
            updates["started_on"] = now
    return updates


def permissions_for(task: Any, viewer: Viewer) -> Dict[str, Any]:
    """The ``Permissions`` object of the contract."""
    return {
        "can_edit": can_edit_task(task, viewer),
        "can_archive": can_archive(task, viewer),
        "can_add_subtask": can_add_subtask(task, viewer),
        "can_assign_subtask_to_others": can_assign_subtask_to_others(task, viewer),
        "can_log": can_log(task, viewer),
        "can_comment": can_comment(task, viewer),
        "can_upload": can_upload(task, viewer),
        "allowed_transitions": allowed_transitions(task, viewer),
        "needs_reason_for": needs_reason_for(task, viewer),
    }


def invalid_mentions(
    task: Any, mentions: Sequence[str], viewer_for: Callable[[str], Optional[Viewer]]
) -> List[str]:
    """Mentioned users who could not see *task* (or do not exist).

    ``viewer_for(user)`` returns that user's :class:`Viewer`, or None for an
    unknown/disabled user. Injected so this stays pure.
    """
    bad: List[str] = []
    for user in mentions or []:
        other = viewer_for(user)
        if other is None or not is_visible(task, other):
            bad.append(user)
    return bad


# ── Notifications ──────────────────────────────────────────────────────────


def clean_recipients(recipients: Iterable[Any], actor: Optional[str] = None) -> List[str]:
    """Unique, sorted, non-empty recipients -- never the actor, never Guest."""
    out = set()
    for user in recipients or []:
        text = str(user or "").strip()
        if not text or text == "Guest" or (actor and text == actor):
            continue
        out.add(text)
    return sorted(out)


def notification_recipients(
    event: str, task: Any, actor: Optional[str], extra: Optional[Dict[str, Any]] = None
) -> List[str]:
    """Who hears about *event* on *task*. The actor is always excluded."""
    extra = extra or {}
    if event in (N_ASSIGNED, N_APPROVED, N_SENT_BACK, N_REOPENED):
        base = [_val(task, "assigned_to")]
    elif event == N_UNASSIGNED:
        base = [extra.get("previous_assignee")]
    elif event == N_SUBTASK_ASSIGNED:
        base = [extra.get("subtask_assignee")]
    elif event == N_SUBTASK_DONE:
        base = [_val(task, "created_by"), extra.get("subtask_created_by")]
    elif event == N_SUBMITTED:
        base = [_val(task, "created_by")]
    elif event == N_MENTIONED:
        base = list(extra.get("mentions") or [])
    elif event in (N_DUE_SOON, N_OVERDUE):
        base = list(extra.get("recipients") or [_val(task, "assigned_to")])
    else:
        base = []
    return clean_recipients(base, actor)


def _truncate(text: Any, limit: int) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: max(limit - 1, 0)].rstrip() + "…"


def notification_text(
    event: str, task_title: str, actor_name: str, extra: Optional[Dict[str, Any]] = None
) -> Tuple[str, str]:
    """``(title, body)`` of a task push. English, per the contract."""
    extra = extra or {}
    title = _truncate(task_title, 80) or "Task"
    actor = actor_name or "Jarz"
    sub = _truncate(extra.get("subtask_title"), 60)
    due = str(extra.get("due_date") or "").strip()
    reason = _truncate(extra.get("reason"), MENTION_BODY_MAX)

    if event == N_ASSIGNED:
        return f"New task: {title}", f"From {actor}" + (f" · due {due}" if due else "")
    if event == N_UNASSIGNED:
        new_name = str(extra.get("new_assignee_name") or "").strip()
        body = f"{actor} reassigned it to {new_name}" if new_name else f"Reassigned by {actor}"
        return f"Task reassigned: {title}", body
    if event == N_SUBTASK_ASSIGNED:
        return f"New subtask: {sub} ({title})", f"From {actor}" + (f" · due {due}" if due else "")
    if event == N_SUBTASK_DONE:
        return f"Subtask done: {sub}", f"{actor} · {title}"
    if event == N_SUBMITTED:
        return f"Ready for review: {title}", f"{actor} submitted it for review"
    if event == N_APPROVED:
        return f"Task approved: {title}", f"Approved by {actor}"
    if event == N_SENT_BACK:
        return f"Task sent back: {title}", reason or f"Sent back by {actor}"
    if event == N_REOPENED:
        return f"Task reopened: {title}", reason or f"Reopened by {actor}"
    if event == N_MENTIONED:
        return f"{actor} mentioned you: {title}", _truncate(extra.get("content"), MENTION_BODY_MAX)
    if event == N_DUE_SOON:
        name = f"{sub} ({title})" if sub else title
        return f"Due tomorrow: {name}", f"Due {due}" if due else "Due tomorrow"
    if event == N_OVERDUE:
        name = f"{sub} ({title})" if sub else title
        return f"Overdue: {name}", f"Was due {due}" if due else "Past its due date"
    return title, f"Updated by {actor}"


# ── Reminders ──────────────────────────────────────────────────────────────


def plan_task_reminder(task: Any, today: datetime.date) -> Optional[Dict[str, Any]]:
    """The reminder one task earns today, or None.

    ``{"event", "recipients", "updates"}``. ``updates`` is the bookkeeping to
    write so the same reminder is not sent twice: ``last_reminder_on`` makes the
    assignee's reminder once-per-day, and ``creator_overdue_notified`` makes the
    creator's overdue notice once-per-task (reset when the due date moves).
    """
    if is_archived(task) or _val(task, "status") not in OPEN_STATUSES:
        return None
    due = _as_date(_val(task, "due_date"))
    if not due:
        return None
    if _as_date(_val(task, "last_reminder_on")) == today:
        return None
    assignee = _val(task, "assigned_to")
    creator = _val(task, "created_by")
    tomorrow = today + datetime.timedelta(days=1)

    if due == tomorrow:
        return {
            "event": N_DUE_SOON,
            "recipients": clean_recipients([assignee]),
            "updates": {"last_reminder_on": today},
        }
    if due < today:
        recipients = [assignee]
        updates: Dict[str, Any] = {"last_reminder_on": today}
        if not _flag(_val(task, "creator_overdue_notified")):
            updates["creator_overdue_notified"] = 1
            if creator and creator != assignee:
                recipients.append(creator)
        return {
            "event": N_OVERDUE,
            "recipients": clean_recipients(recipients),
            "updates": updates,
        }
    return None


def plan_subtask_reminder(subtask: Any, task: Any, today: datetime.date) -> Optional[Dict[str, Any]]:
    """The reminder one open, assigned, dated subtask earns today, or None."""
    if is_archived(task) or is_done(task):
        return None
    if _flag(_val(subtask, "is_done")):
        return None
    assignee = _val(subtask, "assigned_to")
    due = _as_date(_val(subtask, "due_date"))
    if not assignee or not due:
        return None
    if _as_date(_val(subtask, "reminder_sent_on")) == today:
        return None
    tomorrow = today + datetime.timedelta(days=1)
    if due == tomorrow:
        event = N_DUE_SOON
    elif due < today:
        event = N_OVERDUE
    else:
        return None
    return {
        "event": event,
        "recipients": clean_recipients([assignee]),
        "updates": {"reminder_sent_on": today},
    }


# ── Board ordering ─────────────────────────────────────────────────────────


def sort_cards(cards: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Priority (Urgent, High, Normal), then due date (nulls last), then newest."""
    by_modified = sorted(cards, key=lambda c: str(c.get("modified") or ""), reverse=True)
    return sorted(
        by_modified,
        key=lambda c: (
            PRIORITY_RANK.get(c.get("priority") or PRIORITY_NORMAL, 9),
            0 if c.get("due_date") else 1,
            str(c.get("due_date") or ""),
        ),
    )


def is_overdue(task: Any, today: datetime.date) -> bool:
    due = _as_date(_val(task, "due_date"))
    return bool(due) and due < today and not is_done(task)


# ═══════════════════════════════════════════════════════════════════════════
# Lookups (memoised per request)
# ═══════════════════════════════════════════════════════════════════════════

_CACHE_ATTR = "jarz_task_board_cache"


def _cache() -> Dict[str, Any]:
    try:
        cache = getattr(frappe.local, _CACHE_ATTR, None)
        if cache is None:
            cache = {}
            setattr(frappe.local, _CACHE_ATTR, cache)
        return cache
    except Exception:
        return {}


def clear_cache() -> None:
    try:
        setattr(frappe.local, _CACHE_ATTR, {})
    except Exception:
        pass


def get_full_access_users() -> List[str]:
    """Users on the Jarz Task Settings full-access list."""
    cache = _cache()
    if "full_access" in cache:
        return cache["full_access"]
    try:
        rows = frappe.db.sql(
            """
            SELECT `user` FROM `tabJarz Task Settings User`
            WHERE parent = %s AND parenttype = %s AND IFNULL(`user`, '') != ''
            """,
            (SETTINGS_DOCTYPE, SETTINGS_DOCTYPE),
        )
        users = sorted({r[0] for r in rows or [] if r and r[0]})
    except Exception:
        # Missing table (pre-migrate) or a DB hiccup: nobody is full-access.
        # Fails closed -- the narrower rules still apply.
        users = []
    cache["full_access"] = users
    return users


def get_manager_users() -> frozenset:
    """Every user holding JARZ Manager or System Manager, plus Administrator.

    Not filtered on ``enabled``: whether an assignee is "a manager" is a fact
    about their roles, and dropping a disabled manager would suddenly expose
    their tasks to every other manager.
    """
    cache = _cache()
    if "managers" in cache:
        return cache["managers"]
    managers = {ROLES.ADMINISTRATOR}
    try:
        rows = frappe.db.sql(
            """
            SELECT DISTINCT parent FROM `tabHas Role`
            WHERE parenttype = 'User' AND role IN %(roles)s
            """,
            {"roles": tuple(sorted(MANAGER_ROLES))},
        )
        managers.update(r[0] for r in rows or [] if r and r[0])
    except Exception:
        frappe.log_error(frappe.get_traceback(), "task_board: manager lookup failed")
    result = frozenset(managers)
    cache["managers"] = result
    return result


def get_viewer(user: Optional[str] = None) -> Viewer:
    """The :class:`Viewer` for *user* (defaults to the session user)."""
    user = str(user or getattr(getattr(frappe, "session", None), "user", "") or "")
    cache = _cache()
    key = f"viewer:{user}"
    if key in cache:
        return cache[key]
    try:
        roles = frappe.get_roles(user) if user and user != "Guest" else []
    except Exception:
        roles = []
    viewer = make_viewer(user, roles, get_full_access_users(), get_manager_users())
    cache[key] = viewer
    return viewer


def get_board_users() -> List[Dict[str, str]]:
    """Enabled board users as ``[{user, full_name}]``, sorted by full name.

    Administrator is left out: it is a system account, not a person to hand a
    card to (it still sees everything through the full-access rule).
    """
    cache = _cache()
    if "board_users" in cache:
        return cache["board_users"]
    full_access = tuple(get_full_access_users()) or ("",)
    try:
        rows = frappe.db.sql(
            """
            SELECT DISTINCT u.name AS user, u.full_name
            FROM `tabUser` u
            LEFT JOIN `tabHas Role` hr
                ON hr.parent = u.name AND hr.parenttype = 'User'
            WHERE u.enabled = 1
              AND u.name NOT IN ('Guest', 'Administrator')
              AND (hr.role IN %(roles)s OR u.name IN %(full_access)s)
            """,
            {"roles": tuple(sorted(BOARD_ROLES)), "full_access": full_access},
            as_dict=True,
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), "task_board: board user lookup failed")
        rows = []
    seen = set()
    users: List[Dict[str, str]] = []
    for row in rows or []:
        name = row.get("user")
        if not name or name in seen:
            continue
        seen.add(name)
        users.append({"user": name, "full_name": row.get("full_name") or name})
    users.sort(key=lambda r: (str(r["full_name"]).lower(), r["user"]))
    cache["board_users"] = users
    return users


def is_assignable(user: Optional[str]) -> bool:
    """May *user* be put on a card or a subtask? (enabled board user)"""
    if not user:
        return False
    return any(row["user"] == user for row in get_board_users())


def visibility_sql(viewer: Viewer, table: str = "`tabJarz Task`") -> str:
    """The visibility rule as a SQL condition over *table* ("" = no restriction).

    Shared by the Desk permission hook and the board query so both answer the
    same question the same way. Users are escaped with ``frappe.db.escape``.
    """
    if viewer.can_view_all:
        return ""
    if not viewer.user or viewer.user == "Guest":
        return "1=0"
    me = frappe.db.escape(viewer.user)
    parts = [
        f"{table}.`created_by` = {me}",
        f"{table}.`assigned_to` = {me}",
        (
            "EXISTS (SELECT 1 FROM `tabJarz Task Subtask` `jts_vis`"
            f" WHERE `jts_vis`.`parent` = {table}.`name`"
            " AND `jts_vis`.`parenttype` = 'Jarz Task'"
            f" AND `jts_vis`.`assigned_to` = {me})"
        ),
    ]
    if viewer.is_manager:
        managers = sorted(viewer.managers)
        if managers:
            listed = ", ".join(frappe.db.escape(m) for m in managers)
            parts.append(
                f"(IFNULL({table}.`assigned_to`, '') != '' AND {table}.`assigned_to` NOT IN ({listed}))"
            )
        else:
            parts.append(f"IFNULL({table}.`assigned_to`, '') != ''")
    return "(" + " OR ".join(parts) + ")"


# ── Counts (pending panel + get_task_counts) ───────────────────────────────


def count_assigned_open(user: str) -> int:
    """My open cards (To Do / In Progress) + my open subtasks on live cards."""
    if not user:
        return 0
    cards = frappe.db.sql(
        """
        SELECT COUNT(*) FROM `tabJarz Task`
        WHERE assigned_to = %s AND archived = 0 AND status IN %s
        """,
        (user, OPEN_STATUSES),
    )
    subs = frappe.db.sql(
        """
        SELECT COUNT(*) FROM `tabJarz Task Subtask` s
        INNER JOIN `tabJarz Task` t ON t.name = s.parent
        WHERE s.parenttype = 'Jarz Task' AND s.assigned_to = %s AND s.is_done = 0
          AND t.archived = 0 AND t.status != %s
        """,
        (user, STATUS_DONE),
    )
    return int((cards[0][0] if cards else 0) or 0) + int((subs[0][0] if subs else 0) or 0)


def count_review_waiting(viewer: Viewer) -> int:
    """In Review cards the viewer may approve (creator, or everything for full access)."""
    if not viewer.user:
        return 0
    if viewer.can_view_all:
        rows = frappe.db.sql(
            "SELECT COUNT(*) FROM `tabJarz Task` WHERE status = %s AND archived = 0",
            (STATUS_IN_REVIEW,),
        )
    else:
        rows = frappe.db.sql(
            """
            SELECT COUNT(*) FROM `tabJarz Task`
            WHERE status = %s AND archived = 0 AND created_by = %s
            """,
            (STATUS_IN_REVIEW, viewer.user),
        )
    return int((rows[0][0] if rows else 0) or 0)


def count_overdue(user: str, today: Any) -> int:
    """My cards past their due date and not done."""
    if not user:
        return 0
    rows = frappe.db.sql(
        """
        SELECT COUNT(*) FROM `tabJarz Task`
        WHERE assigned_to = %s AND archived = 0 AND status != %s
          AND due_date IS NOT NULL AND due_date < %s
        """,
        (user, STATUS_DONE, today),
    )
    return int((rows[0][0] if rows else 0) or 0)
