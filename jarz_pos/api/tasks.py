"""Task Board API (feature ``task-board``).

The board is the line-manager tier's to-do system: managers hand out cards,
line managers work them, log progress, tick subtasks and submit them for
review. The wire contract is ``.scratch/task_board/CONTRACT.md``; the Flutter
client is built against it, so names and response shapes here are frozen.

Every rule -- visibility, who may edit/move/assign, which transitions exist --
is in ``jarz_pos.services.task_board`` and is only *called* from here. This
module is the authority for the DocTypes: their only DocPerm is System Manager,
so every read below uses ``frappe.get_all`` / raw SQL (which ignore
permissions) and every write uses ``ignore_permissions=True``, after the
service rules have said yes.

Concurrency: every mutation first locks the task row (``SELECT ... FOR
UPDATE``) and then re-reads the task WITH a locking read
(``frappe.get_doc(..., for_update=True)``). A plain read after the lock would
still answer from the transaction's REPEATABLE READ snapshot, so two people
ticking the last subtask / approving at once could each act on a stale card.

Notifications are queued after commit (``api.notifications.enqueue_task_notification``)
and never include the person who acted.
"""

from __future__ import annotations

import base64
import binascii
import json
import mimetypes
import os
from typing import Any, Dict, Iterable, List, Optional

import frappe
from frappe import _
from frappe.utils import add_days, cint, get_datetime, getdate, now_datetime, nowdate

from jarz_pos.services import task_board as tb

# ── Small helpers ────────────────────────────────────────────────────────────


def _s(value: Any) -> Optional[str]:
    """JSON-friendly scalar: None stays None, everything else becomes a string."""
    if value in (None, ""):
        return None
    return str(value)


def _truthy(value: Any, default: bool = False) -> bool:
    """Accept 1/0, true/false, "1"/"0", "true"/"false" from any client."""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off", "null", "none"):
        return False
    return bool(cint(value))


def _clean_text(value: Any) -> str:
    raw = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in raw.split("\n")).strip()


def _clean_title(value: Any) -> str:
    return " ".join(str(value or "").split())[: tb.TITLE_MAX_LENGTH]


def _throw_invalid(message: str) -> None:
    frappe.throw(message, frappe.ValidationError)


def _throw_denied(message: str) -> None:
    frappe.throw(message, frappe.PermissionError)


def _parse_list(value: Any, label: str) -> List[Any]:
    if value in (None, "", []):
        return []
    parsed = value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            _throw_invalid(_("{0} must be a JSON list").format(label))
    if not isinstance(parsed, list):
        _throw_invalid(_("{0} must be a JSON list").format(label))
    return parsed


def _parse_date(value: Any, label: str):
    if value in (None, ""):
        return None
    try:
        return getdate(value)
    except Exception:
        _throw_invalid(_("{0} is not a valid date").format(label))


def _parse_priority(value: Any) -> str:
    text = str(value or "").strip() or tb.PRIORITY_NORMAL
    if text not in tb.PRIORITIES:
        _throw_invalid(_("Priority must be one of: {0}").format(", ".join(tb.PRIORITIES)))
    return text


def _parse_branch(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    if not text:
        return None
    if not frappe.db.exists("POS Profile", text):
        _throw_invalid(_("Branch {0} was not found").format(text))
    return text


def _require_assignable(user: Any) -> str:
    text = str(user or "").strip()
    if not text:
        _throw_invalid(_("Choose who the task is assigned to"))
    if not tb.is_assignable(text):
        _throw_invalid(_("{0} is not a Task Board user").format(text))
    return text


def _viewer() -> tb.Viewer:
    return tb.get_viewer(frappe.session.user)


def _require_board(viewer: tb.Viewer) -> None:
    if not viewer.is_board_user:
        _throw_denied(_("The task board is only available to managers and line managers."))


def _refuse(doc: Any, message: str) -> None:
    """Refuse a change: the lock reason when the card is locked, else *message*."""
    reason = tb.lock_reason(doc)
    if reason:
        _throw_invalid(_(reason))
    _throw_denied(message)


def _load_task(name: Any, viewer: tb.Viewer, *, lock: bool = False):
    """The visible task *name*; with ``lock=True`` row-locked and freshly re-read."""
    task_name = str(name or "").strip()
    if not task_name:
        _throw_invalid(_("Task is required"))
    if lock:
        # Take the row lock first, then re-read WITH a locking read: a plain
        # read here would answer from this transaction's snapshot, not from
        # what the lock is protecting.
        rows = frappe.db.sql(
            "SELECT name FROM `tabJarz Task` WHERE name = %s FOR UPDATE", (task_name,)
        )
        if not rows:
            frappe.throw(_("Task {0} not found").format(task_name), frappe.DoesNotExistError)
        doc = frappe.get_doc(tb.TASK_DOCTYPE, task_name, for_update=True)
    else:
        if not frappe.db.exists(tb.TASK_DOCTYPE, task_name):
            frappe.throw(_("Task {0} not found").format(task_name), frappe.DoesNotExistError)
        doc = frappe.get_doc(tb.TASK_DOCTYPE, task_name)
    if not tb.is_visible(doc, viewer):
        _throw_denied(_("You do not have access to this task."))
    return doc


def _find_subtask(doc: Any, subtask_id: Any):
    wanted = str(subtask_id or "").strip()
    for row in doc.get("subtasks") or []:
        if row.name == wanted:
            return row
    frappe.throw(_("Subtask not found"), frappe.DoesNotExistError)


def _names(users: Iterable[Any]) -> Dict[str, str]:
    wanted = sorted({str(u) for u in users if u})
    if not wanted:
        return {}
    rows = frappe.get_all(
        "User",
        filters={"name": ["in", wanted]},
        fields=["name", "full_name"],
        limit_page_length=0,
    )
    names = {r.name: (r.full_name or r.name) for r in rows}
    for user in wanted:
        names.setdefault(user, user)
    return names


def _user_ref(user: str, names: Dict[str, str], managers: Iterable[str]) -> Dict[str, Any]:
    return {
        "user": user,
        "full_name": names.get(user) or user,
        "is_manager": user == "Administrator" or user in set(managers),
    }


def _history(task_name: str, event: str, content: str, actor: str) -> None:
    frappe.get_doc(
        {
            "doctype": tb.ENTRY_DOCTYPE,
            "task": task_name,
            "kind": tb.KIND_HISTORY,
            "event": event,
            "content": content,
            "author": actor,
        }
    ).insert(ignore_permissions=True)


def _notify(doc: Any, event: str, actor: str, extra: Optional[Dict[str, Any]] = None) -> None:
    """Queue *event* for its recipients (never the actor). Never raises."""
    try:
        from jarz_pos.api.notifications import enqueue_task_notification

        extra = dict(extra or {})
        recipients = tb.notification_recipients(event, doc, actor, extra)
        if recipients:
            enqueue_task_notification(doc.name, event, recipients, actor=actor, extra=extra)
    except Exception:
        try:
            frappe.log_error(frappe.get_traceback(), "task_board: notify failed", defer_insert=True)
        except Exception:
            pass


# ── Shapes ───────────────────────────────────────────────────────────────────


def _aggregates(task_names: List[str]) -> Dict[str, Dict[str, int]]:
    out = {
        n: {"subtasks_total": 0, "subtasks_done": 0, "attachments_count": 0, "entries_count": 0}
        for n in task_names
    }
    if not task_names:
        return out
    params = {"names": tuple(task_names)}
    for parent, total, done in frappe.db.sql(
        """
        SELECT parent, COUNT(*), SUM(is_done) FROM `tabJarz Task Subtask`
        WHERE parenttype = 'Jarz Task' AND parent IN %(names)s
        GROUP BY parent
        """,
        params,
    ):
        if parent in out:
            out[parent]["subtasks_total"] = cint(total)
            out[parent]["subtasks_done"] = cint(done)
    for parent, count in frappe.db.sql(
        """
        SELECT attached_to_name, COUNT(*) FROM `tabFile`
        WHERE attached_to_doctype = 'Jarz Task' AND attached_to_name IN %(names)s
          AND IFNULL(is_folder, 0) = 0
        GROUP BY attached_to_name
        """,
        params,
    ):
        if parent in out:
            out[parent]["attachments_count"] = cint(count)
    for parent, count in frappe.db.sql(
        """
        SELECT task, COUNT(*) FROM `tabJarz Task Entry`
        WHERE task IN %(names)s AND kind IN ('Log', 'Comment')
        GROUP BY task
        """,
        params,
    ):
        if parent in out:
            out[parent]["entries_count"] = cint(count)
    return out


def _card(row: Any, agg: Dict[str, int], names: Dict[str, str], today_date) -> Dict[str, Any]:
    """The contract's ``CardSummary``."""
    assigned_to = row.get("assigned_to")
    created_by = row.get("created_by")
    return {
        "name": row.get("name"),
        "title": row.get("title"),
        "status": row.get("status") or tb.STATUS_TODO,
        "priority": row.get("priority") or tb.PRIORITY_NORMAL,
        "assigned_to": assigned_to,
        "assigned_to_name": names.get(assigned_to) or assigned_to,
        "created_by": created_by,
        "created_by_name": names.get(created_by) or created_by,
        "due_date": _s(row.get("due_date")),
        "is_overdue": tb.is_overdue(row, today_date),
        "branch": row.get("branch") or None,
        "subtasks_total": cint(agg.get("subtasks_total")),
        "subtasks_done": cint(agg.get("subtasks_done")),
        "attachments_count": cint(agg.get("attachments_count")),
        "entries_count": cint(agg.get("entries_count")),
        "archived": bool(cint(row.get("archived"))),
        "modified": _s(row.get("modified")),
        "submitted_on": _s(row.get("submitted_on")),
        "completed_on": _s(row.get("completed_on")),
    }


def _files_for(task_name: str, entry_names: List[str]) -> List[Dict[str, Any]]:
    return frappe.db.sql(
        """
        SELECT name, file_name, file_size, owner, creation,
               attached_to_doctype, attached_to_name
        FROM `tabFile`
        WHERE IFNULL(is_folder, 0) = 0
          AND (
            (attached_to_doctype = 'Jarz Task' AND attached_to_name = %(task)s)
            OR (attached_to_doctype = 'Jarz Task Entry' AND attached_to_name IN %(entries)s)
          )
        ORDER BY creation ASC, name ASC
        """,
        {"task": task_name, "entries": tuple(entry_names) or ("",)},
        as_dict=True,
    )


def _attachment(f: Any, doc: Any, viewer: tb.Viewer, names: Dict[str, str]) -> Dict[str, Any]:
    content_type = mimetypes.guess_type(f.get("file_name") or "")[0] or "application/octet-stream"
    entry = f.get("attached_to_name") if f.get("attached_to_doctype") == tb.ENTRY_DOCTYPE else None
    owner = f.get("owner")
    return {
        "name": f.get("name"),
        "file_name": f.get("file_name"),
        "file_size": cint(f.get("file_size")),
        "content_type": content_type,
        "uploaded_by": owner,
        "uploaded_by_name": names.get(owner) or owner,
        "creation": _s(f.get("creation")),
        "is_image": content_type.startswith("image/"),
        "entry": entry,
        # Entry attachments are part of an immutable entry: never removable.
        "can_remove": entry is None and tb.can_remove_attachment(doc, viewer, owner),
    }


def _mentionable_users(doc: Any, viewer: tb.Viewer) -> List[str]:
    """Every enabled user who can see *doc*, minus the caller."""
    candidates = set(tb.get_full_access_users()) | tb.participants(doc)
    assignee = doc.get("assigned_to")
    if assignee and assignee not in viewer.managers:
        # Rule 3: managers see line-manager work. The Administrator account is
        # not a person to @mention unless it is on the card itself.
        candidates |= set(viewer.managers) - {"Administrator"}
    candidates.discard(viewer.user)
    candidates.discard("Guest")
    if not candidates:
        return []
    return frappe.get_all(
        "User",
        filters={"name": ["in", sorted(candidates)], "enabled": 1},
        pluck="name",
        limit_page_length=0,
    )


def _task_payload(doc: Any, viewer: tb.Viewer) -> Dict[str, Any]:
    """The contract's ``get_task`` shape."""
    today_date = getdate(nowdate())
    name = doc.name
    subtasks = sorted(doc.get("subtasks") or [], key=lambda r: cint(r.idx))
    entries = frappe.get_all(
        tb.ENTRY_DOCTYPE,
        filters={"task": name},
        fields=["name", "kind", "content", "event", "author", "creation", "mentions"],
        order_by="creation asc, name asc",
        limit_page_length=0,
    )
    files = _files_for(name, [e.name for e in entries])
    mentionable = _mentionable_users(doc, viewer)

    parsed_mentions: Dict[str, List[str]] = {}
    for e in entries:
        try:
            value = json.loads(e.mentions) if e.mentions else []
        except ValueError:
            value = []
        parsed_mentions[e.name] = [str(u) for u in value if u] if isinstance(value, list) else []

    people = {
        doc.get("created_by"), doc.get("assigned_to"), doc.get("completed_by"), doc.get("archived_by"),
    }
    for row in subtasks:
        people.update([row.assigned_to, row.done_by, row.created_by])
    people.update(f.get("owner") for f in files)
    people.update(e.author for e in entries)
    people.update(mentionable)
    names = _names(people)

    attachments_by_entry: Dict[str, List[Dict[str, Any]]] = {}
    card_attachments: List[Dict[str, Any]] = []
    for f in files:
        item = _attachment(f, doc, viewer, names)
        if item["entry"]:
            attachments_by_entry.setdefault(item["entry"], []).append(item)
        else:
            card_attachments.append(item)

    agg = {
        "subtasks_total": len(subtasks),
        "subtasks_done": sum(1 for r in subtasks if cint(r.is_done)),
        "attachments_count": len(card_attachments),
        "entries_count": sum(1 for e in entries if e.kind in tb.USER_ENTRY_KINDS),
    }
    task = _card(doc, agg, names, today_date)
    task.update(
        {
            "description": doc.get("description") or "",
            "started_on": _s(doc.get("started_on")),
            "completed_by": doc.get("completed_by") or None,
            "completed_by_name": names.get(doc.get("completed_by")) if doc.get("completed_by") else None,
            "archived_on": _s(doc.get("archived_on")),
            "archived_by": doc.get("archived_by") or None,
        }
    )

    subtask_rows = []
    for row in subtasks:
        due = row.due_date
        subtask_rows.append(
            {
                "id": row.name,
                "title": row.title,
                "assigned_to": row.assigned_to or None,
                "assigned_to_name": names.get(row.assigned_to) if row.assigned_to else None,
                "due_date": _s(due),
                "is_done": bool(cint(row.is_done)),
                "done_by": row.done_by or None,
                "done_by_name": names.get(row.done_by) if row.done_by else None,
                "done_on": _s(row.done_on),
                "created_by": row.created_by,
                "is_overdue": bool(due) and not cint(row.is_done) and getdate(due) < today_date,
                "can_edit": tb.can_edit_subtask(doc, row, viewer),
                "can_toggle": tb.can_toggle_subtask(doc, row, viewer),
            }
        )

    entry_rows = [
        {
            "name": e.name,
            "kind": e.kind,
            "content": e.content or "",
            "event": e.event or None,
            "author": e.author,
            "author_name": names.get(e.author) or e.author,
            "creation": _s(e.creation),
            "mentions": parsed_mentions.get(e.name, []),
            "attachments": attachments_by_entry.get(e.name, []),
        }
        for e in entries
    ]

    mention_refs = sorted(
        (_user_ref(u, names, viewer.managers) for u in mentionable),
        key=lambda r: (str(r["full_name"]).lower(), r["user"]),
    )

    return {
        "task": task,
        "subtasks": subtask_rows,
        "attachments": card_attachments,
        "entries": entry_rows,
        "mentionable": mention_refs,
        "permissions": tb.permissions_for(doc, viewer),
    }


# ── Read endpoints ───────────────────────────────────────────────────────────


@frappe.whitelist()
def get_board_context() -> Dict[str, Any]:
    """Who am I on the board, and what does the board offer. Never throws for a non-board user."""
    user = frappe.session.user
    try:
        viewer = _viewer()
    except Exception:
        viewer = tb.Viewer(user=user)
    base: Dict[str, Any] = {
        "can_access": False,
        "is_manager": False,
        "can_create": False,
        "can_view_all": False,
        "can_view_overview": False,
        "me": {"user": user, "full_name": user, "is_manager": False},
        "users": [],
        "branches": [],
        "statuses": list(tb.STATUSES),
        "priorities": list(tb.PRIORITIES),
    }
    try:
        base["me"] = _user_ref(user, _names([user]), viewer.managers)
    except Exception:
        pass
    if not viewer.is_board_user:
        return base

    board_users = tb.get_board_users()
    names = {row["user"]: row["full_name"] for row in board_users}
    try:
        branches = frappe.get_all(
            "POS Profile",
            filters={"disabled": 0},
            pluck="name",
            order_by="name asc",
            limit_page_length=0,
        )
    except Exception:
        branches = []
    base.update(
        {
            "can_access": True,
            "is_manager": viewer.is_manager,
            "can_create": viewer.can_create,
            "can_view_all": viewer.can_view_all,
            "can_view_overview": viewer.can_view_overview,
            "me": _user_ref(user, {**_names([user]), **names}, viewer.managers),
            "users": [_user_ref(row["user"], names, viewer.managers) for row in board_users],
            "branches": branches,
        }
    )
    return base


_BOARD_VIEWS = ("all", "mine", "created", "review")


@frappe.whitelist()
def get_board(
    view: Optional[str] = "all",
    assigned_to: Optional[str] = None,
    created_by: Optional[str] = None,
    branch: Optional[str] = None,
    priority: Optional[str] = None,
    overdue_only: Any = 0,
    include_archived: Any = 0,
    search: Optional[str] = None,
    done_days: Any = 30,
) -> Dict[str, Any]:
    """The board: every visible card, grouped by status column."""
    viewer = _viewer()
    _require_board(viewer)
    view = str(view or "all").strip().lower()
    if view not in _BOARD_VIEWS:
        _throw_invalid(_("Unknown view: {0}").format(view))

    today_str = nowdate()
    where: List[str] = []
    params: Dict[str, Any] = {
        "me": viewer.user,
        "done": tb.STATUS_DONE,
        "in_review": tb.STATUS_IN_REVIEW,
        "today": today_str,
    }

    visibility = tb.visibility_sql(viewer, "t")
    if visibility:
        where.append(visibility)
    if not _truthy(include_archived):
        where.append("t.archived = 0")

    if view == "mine":
        where.append(
            "(t.assigned_to = %(me)s OR EXISTS ("
            " SELECT 1 FROM `tabJarz Task Subtask` s"
            " WHERE s.parent = t.name AND s.parenttype = 'Jarz Task'"
            " AND s.assigned_to = %(me)s AND s.is_done = 0))"
        )
    elif view == "created":
        where.append("t.created_by = %(me)s")
    elif view == "review":
        where.append("t.status = %(in_review)s")
        if not viewer.can_view_all:
            where.append("t.created_by = %(me)s")

    if assigned_to:
        where.append("t.assigned_to = %(assigned_to)s")
        params["assigned_to"] = str(assigned_to).strip()
    if created_by:
        where.append("t.created_by = %(created_by)s")
        params["created_by"] = str(created_by).strip()
    if branch:
        where.append("t.branch = %(branch)s")
        params["branch"] = str(branch).strip()
    if priority:
        where.append("t.priority = %(priority)s")
        params["priority"] = _parse_priority(priority)
    if _truthy(overdue_only):
        where.append("t.due_date IS NOT NULL AND t.due_date < %(today)s AND t.status != %(done)s")
    text = str(search or "").strip()
    if text:
        where.append("(t.title LIKE %(search)s OR t.description LIKE %(search)s OR t.name LIKE %(search)s)")
        params["search"] = f"%{text}%"
    days = cint(done_days)
    if days > 0:
        where.append("(t.status != %(done)s OR t.completed_on >= %(done_cutoff)s)")
        params["done_cutoff"] = add_days(now_datetime(), -days)

    rows = frappe.db.sql(
        f"""
        SELECT t.name, t.title, t.status, t.priority, t.assigned_to, t.created_by,
               t.due_date, t.branch, t.archived, t.modified, t.submitted_on, t.completed_on
        FROM `tabJarz Task` t
        WHERE {" AND ".join(where) if where else "1=1"}
        ORDER BY t.modified DESC
        LIMIT {int(tb.BOARD_ROW_CAP)}
        """,
        params,
        as_dict=True,
    )

    today_date = getdate(today_str)
    task_names = [r.name for r in rows]
    aggregates = _aggregates(task_names)
    names = _names([r.assigned_to for r in rows] + [r.created_by for r in rows])
    columns: Dict[str, List[Dict[str, Any]]] = {status: [] for status in tb.STATUSES}
    for row in rows:
        status = row.status if row.status in columns else tb.STATUS_TODO
        columns[status].append(_card(row, aggregates.get(row.name, {}), names, today_date))
    for status in columns:
        columns[status] = tb.sort_cards(columns[status])
    return {
        "columns": columns,
        "counts": {status: len(cards) for status, cards in columns.items()},
    }


@frappe.whitelist()
def get_task(name: str) -> Dict[str, Any]:
    viewer = _viewer()
    _require_board(viewer)
    doc = _load_task(name, viewer)
    return _task_payload(doc, viewer)


@frappe.whitelist()
def get_overview(days: Any = 30) -> Dict[str, Any]:
    """Per-person workload and on-time rate over the visible, non-archived tasks."""
    viewer = _viewer()
    _require_board(viewer)
    if not viewer.can_view_overview:
        _throw_denied(_("The task overview is for managers only."))
    period = cint(days)
    if period <= 0:
        period = 30
    cutoff = get_datetime(add_days(now_datetime(), -period))
    today_date = getdate(nowdate())

    # Done cards only matter inside the period: without this bound the query
    # would read every card ever completed just to discard most of them.
    where = [
        "t.archived = 0",
        "(t.status != %(done)s OR t.completed_on >= %(cutoff)s)",
    ]
    visibility = tb.visibility_sql(viewer, "t")
    if visibility:
        where.append(visibility)
    rows = frappe.db.sql(
        f"""
        SELECT t.name, t.status, t.assigned_to, t.due_date, t.completed_on
        FROM `tabJarz Task` t
        WHERE {" AND ".join(where)}
        """,
        {"done": tb.STATUS_DONE, "cutoff": cutoff},
        as_dict=True,
    )

    def _blank() -> Dict[str, int]:
        return {
            "open": 0, "in_progress": 0, "in_review": 0, "overdue": 0,
            "done_in_period": 0, "done_on_time_in_period": 0,
        }

    per_person: Dict[str, Dict[str, int]] = {}
    for row in rows:
        person = row.assigned_to
        if not person:
            continue
        stats = per_person.setdefault(person, _blank())
        if row.status != tb.STATUS_DONE:
            # "open" = every unfinished card; in_progress / in_review break it down.
            stats["open"] += 1
            if row.status == tb.STATUS_IN_PROGRESS:
                stats["in_progress"] += 1
            elif row.status == tb.STATUS_IN_REVIEW:
                stats["in_review"] += 1
            if tb.is_overdue(row, today_date):
                stats["overdue"] += 1
        elif row.completed_on and get_datetime(row.completed_on) >= cutoff:
            stats["done_in_period"] += 1
            due = row.due_date
            if not due or get_datetime(row.completed_on).date() <= getdate(due):
                stats["done_on_time_in_period"] += 1

    names = _names(per_person.keys())
    people = []
    for person, stats in per_person.items():
        done = stats["done_in_period"]
        people.append(
            {
                **_user_ref(person, names, viewer.managers),
                **stats,
                "on_time_rate": round(stats["done_on_time_in_period"] / done, 4) if done else None,
            }
        )
    people.sort(key=lambda p: (str(p["full_name"]).lower(), p["user"]))
    totals = {
        "open": sum(p["open"] for p in people),
        "overdue": sum(p["overdue"] for p in people),
        "in_review": sum(p["in_review"] for p in people),
        "done_in_period": sum(p["done_in_period"] for p in people),
    }
    return {"period_days": period, "people": people, "totals": totals}


@frappe.whitelist()
def get_task_counts() -> Dict[str, int]:
    """``{assigned_open, review_waiting, overdue}`` for the caller (zeros off the board)."""
    viewer = _viewer()
    if not viewer.is_board_user:
        return {"assigned_open": 0, "review_waiting": 0, "overdue": 0}
    return {
        "assigned_open": tb.count_assigned_open(viewer.user),
        "review_waiting": tb.count_review_waiting(viewer),
        "overdue": tb.count_overdue(viewer.user, nowdate()),
    }


@frappe.whitelist(methods=["GET", "POST"])
def download_attachment(file: str) -> None:
    """Stream a task/entry attachment after re-checking the task is visible."""
    viewer = _viewer()
    _require_board(viewer)
    file_name = str(file or "").strip()
    row = (
        frappe.db.get_value(
            "File",
            file_name,
            ["name", "file_name", "attached_to_doctype", "attached_to_name", "is_folder"],
            as_dict=True,
        )
        if file_name
        else None
    )
    if not row or cint(row.is_folder):
        frappe.throw(_("File not found"), frappe.DoesNotExistError)

    if row.attached_to_doctype == tb.TASK_DOCTYPE:
        task_name = row.attached_to_name
    elif row.attached_to_doctype == tb.ENTRY_DOCTYPE:
        task_name = frappe.db.get_value(tb.ENTRY_DOCTYPE, row.attached_to_name, "task")
    else:
        task_name = None
    if not task_name:
        # Not a task attachment: this endpoint must never become a general
        # private-file reader.
        _throw_denied(_("You do not have access to this file."))
    _load_task(task_name, viewer)

    file_doc = frappe.get_doc("File", row.name)
    # Raw bytes from disk: File.get_content() decodes text files to str.
    with open(file_doc.get_full_path(), "rb") as handle:
        content = handle.read()

    display_name = row.file_name or row.name
    content_type = mimetypes.guess_type(display_name)[0] or "application/octet-stream"
    frappe.local.response.filename = display_name
    frappe.local.response.filecontent = content
    frappe.local.response.content_type = content_type
    # Inline ONLY for types a browser cannot execute. This response is served
    # from the site's own origin with the caller's session cookie, so an HTML
    # or SVG file rendered inline would be stored XSS against whoever opens it.
    frappe.local.response.display_content_as = tb.content_disposition_for(content_type)
    frappe.local.response.type = "download"


# ── Task mutations ───────────────────────────────────────────────────────────


@frappe.whitelist(methods=["POST"])
def create_task(
    title: str,
    assigned_to: str,
    description: Optional[str] = None,
    priority: Optional[str] = tb.PRIORITY_NORMAL,
    due_date: Optional[str] = None,
    branch: Optional[str] = None,
    subtasks: Any = None,
) -> Dict[str, Any]:
    viewer = _viewer()
    _require_board(viewer)
    if not viewer.can_create:
        _throw_denied(_("Only managers can create tasks."))

    clean_title = _clean_title(title)
    if not clean_title:
        _throw_invalid(_("Title is required"))
    assignee = _require_assignable(assigned_to)
    clean_priority = _parse_priority(priority)
    due = _parse_date(due_date, _("Due date"))
    clean_branch = _parse_branch(branch)

    rows = []
    for spec in _parse_list(subtasks, _("Subtasks")):
        if not isinstance(spec, dict):
            _throw_invalid(_("Every subtask must be an object with a title"))
        sub_title = _clean_title(spec.get("title"))
        if not sub_title:
            _throw_invalid(_("Every subtask needs a title"))
        sub_assignee = str(spec.get("assigned_to") or "").strip() or None
        if sub_assignee:
            _require_assignable(sub_assignee)
        rows.append(
            {
                "title": sub_title,
                "assigned_to": sub_assignee,
                "due_date": _parse_date(spec.get("due_date"), _("Subtask due date")),
                "is_done": 0,
                "created_by": viewer.user,
            }
        )

    doc = frappe.get_doc(
        {
            "doctype": tb.TASK_DOCTYPE,
            "title": clean_title,
            "description": _clean_text(description) or None,
            "status": tb.STATUS_TODO,
            "priority": clean_priority,
            "assigned_to": assignee,
            "created_by": viewer.user,
            "due_date": due,
            "branch": clean_branch,
            "subtasks": rows,
        }
    )
    doc.insert(ignore_permissions=True)

    names = _names([assignee])
    content = f"Created and assigned to {names.get(assignee, assignee)}"
    if rows:
        content += f" with {len(rows)} subtask(s)"
    _history(doc.name, tb.EV_CREATED, content, viewer.user)

    _notify(doc, tb.N_ASSIGNED, viewer.user, {"due_date": _s(due)})
    for row in doc.get("subtasks") or []:
        # The card's assignee already heard about the whole card.
        if row.assigned_to and row.assigned_to != doc.assigned_to:
            _notify(
                doc,
                tb.N_SUBTASK_ASSIGNED,
                viewer.user,
                {"subtask_assignee": row.assigned_to, "subtask_title": row.title, "due_date": _s(row.due_date)},
            )
    return _task_payload(doc, viewer)


@frappe.whitelist(methods=["POST"])
def update_task(
    name: str,
    title: Optional[str] = None,
    description: Optional[str] = None,
    priority: Optional[str] = None,
    due_date: Optional[str] = None,
    branch: Optional[str] = None,
    assigned_to: Optional[str] = None,
    clear_due_date: Any = 0,
    clear_branch: Any = 0,
) -> Dict[str, Any]:
    viewer = _viewer()
    _require_board(viewer)
    doc = _load_task(name, viewer, lock=True)
    if not tb.can_edit_task(doc, viewer):
        _refuse(doc, _("Only the task creator can edit this task."))
    actor = viewer.user
    history: List[tuple] = []

    if title is not None:
        new_title = _clean_title(title)
        if not new_title:
            _throw_invalid(_("Title is required"))
        if new_title != doc.title:
            history.append((tb.EV_TITLE, f'Title changed from "{doc.title}" to "{new_title}"'))
            doc.title = new_title

    if description is not None:
        doc.description = _clean_text(description) or None

    if priority not in (None, ""):
        new_priority = _parse_priority(priority)
        if new_priority != doc.priority:
            history.append((tb.EV_PRIORITY, f"Priority changed from {doc.priority} to {new_priority}"))
            doc.priority = new_priority

    old_due = getdate(doc.due_date) if doc.due_date else None
    new_due = old_due
    if _truthy(clear_due_date):
        new_due = None
    elif due_date not in (None, ""):
        new_due = _parse_date(due_date, _("Due date"))
    if new_due != old_due:
        history.append(
            (tb.EV_DUE_DATE, f"Due date changed from {old_due or 'none'} to {new_due or 'none'}")
        )
        doc.due_date = new_due
        # A new deadline is a new promise: re-arm both reminders.
        doc.last_reminder_on = None
        doc.creator_overdue_notified = 0

    old_branch = doc.branch or None
    new_branch = old_branch
    if _truthy(clear_branch):
        new_branch = None
    elif branch not in (None, ""):
        new_branch = _parse_branch(branch)
    if new_branch != old_branch:
        history.append((tb.EV_BRANCH, f"Branch changed from {old_branch or 'none'} to {new_branch or 'none'}"))
        doc.branch = new_branch

    previous_assignee = None
    if assigned_to not in (None, "") and str(assigned_to).strip() != doc.assigned_to:
        new_assignee = _require_assignable(assigned_to)
        previous_assignee = doc.assigned_to
        names = _names([previous_assignee, new_assignee])
        history.append(
            (
                tb.EV_REASSIGNED,
                f"Reassigned from {names.get(previous_assignee, previous_assignee)} "
                f"to {names.get(new_assignee, new_assignee)}",
            )
        )
        doc.assigned_to = new_assignee
        doc.last_reminder_on = None
        doc.creator_overdue_notified = 0

    doc.save(ignore_permissions=True)
    for event, content in history:
        _history(doc.name, event, content, actor)

    if previous_assignee:
        new_name = _names([doc.assigned_to]).get(doc.assigned_to, doc.assigned_to)
        _notify(doc, tb.N_ASSIGNED, actor, {"due_date": _s(doc.due_date)})
        _notify(
            doc,
            tb.N_UNASSIGNED,
            actor,
            {"previous_assignee": previous_assignee, "new_assignee_name": new_name},
        )
    return _task_payload(doc, viewer)


@frappe.whitelist(methods=["POST"])
def set_status(name: str, status: str, reason: Optional[str] = None) -> Dict[str, Any]:
    viewer = _viewer()
    _require_board(viewer)
    doc = _load_task(name, viewer, lock=True)
    target = str(status or "").strip()
    error = tb.transition_error(doc, viewer, target, reason)
    if error:
        kind, message = error
        if kind == "permission":
            _throw_denied(_(message))
        _throw_invalid(_(message))

    previous = doc.status or tb.STATUS_TODO
    doc.update(tb.transition_updates(doc, target, viewer.user, now_datetime()))
    doc.save(ignore_permissions=True)

    reason_text = _clean_text(reason)
    content = f"{previous} → {target}"
    if reason_text:
        content += f"\nReason: {reason_text}"
    _history(doc.name, tb.EV_STATUS, content, viewer.user)

    event = tb.transition_event(previous, target)
    if event:
        _notify(doc, event, viewer.user, {"reason": reason_text})
    return _task_payload(doc, viewer)


@frappe.whitelist(methods=["POST"])
def archive_task(name: str, archived: Any = 1) -> Dict[str, Any]:
    viewer = _viewer()
    _require_board(viewer)
    doc = _load_task(name, viewer, lock=True)
    if not tb.can_archive(doc, viewer):
        _throw_denied(_("Only the task creator can archive this task."))
    want = _truthy(archived, default=True)
    if bool(cint(doc.archived)) == want:
        return _task_payload(doc, viewer)
    doc.archived = 1 if want else 0
    doc.archived_on = now_datetime() if want else None
    doc.archived_by = viewer.user if want else None
    doc.save(ignore_permissions=True)
    _history(
        doc.name,
        tb.EV_ARCHIVED if want else tb.EV_UNARCHIVED,
        "Archived" if want else "Unarchived",
        viewer.user,
    )
    return _task_payload(doc, viewer)


# ── Subtasks ─────────────────────────────────────────────────────────────────


@frappe.whitelist(methods=["POST"])
def add_subtask(
    name: str, title: str, assigned_to: Optional[str] = None, due_date: Optional[str] = None
) -> Dict[str, Any]:
    viewer = _viewer()
    _require_board(viewer)
    doc = _load_task(name, viewer, lock=True)
    if not tb.can_add_subtask(doc, viewer):
        _refuse(doc, _("You cannot add subtasks to this task."))

    sub_title = _clean_title(title)
    if not sub_title:
        _throw_invalid(_("Subtask title is required"))
    assignee = str(assigned_to or "").strip() or None
    error = tb.subtask_assignee_error(doc, viewer, assignee)
    if error:
        _throw_denied(_(error))
    if assignee:
        _require_assignable(assignee)
    due = _parse_date(due_date, _("Subtask due date"))

    doc.append(
        "subtasks",
        {
            "title": sub_title,
            "assigned_to": assignee,
            "due_date": due,
            "is_done": 0,
            "created_by": viewer.user,
        },
    )
    doc.save(ignore_permissions=True)

    content = f'Added subtask "{sub_title}"'
    if assignee:
        content += f" for {_names([assignee]).get(assignee, assignee)}"
    _history(doc.name, tb.EV_SUBTASK_ADDED, content, viewer.user)
    if assignee:
        _notify(
            doc,
            tb.N_SUBTASK_ASSIGNED,
            viewer.user,
            {"subtask_assignee": assignee, "subtask_title": sub_title, "due_date": _s(due)},
        )
    return _task_payload(doc, viewer)


@frappe.whitelist(methods=["POST"])
def update_subtask(
    name: str,
    subtask_id: str,
    title: Optional[str] = None,
    assigned_to: Optional[str] = None,
    due_date: Optional[str] = None,
    clear_assignee: Any = 0,
    clear_due_date: Any = 0,
) -> Dict[str, Any]:
    viewer = _viewer()
    _require_board(viewer)
    doc = _load_task(name, viewer, lock=True)
    row = _find_subtask(doc, subtask_id)
    if not tb.can_edit_subtask(doc, row, viewer):
        _refuse(doc, _("You cannot edit this subtask."))

    if title is not None:
        new_title = _clean_title(title)
        if not new_title:
            _throw_invalid(_("Subtask title is required"))
        row.title = new_title

    old_assignee = row.assigned_to or None
    new_assignee = old_assignee
    if _truthy(clear_assignee):
        new_assignee = None
    elif assigned_to not in (None, ""):
        new_assignee = str(assigned_to).strip()
    if new_assignee != old_assignee:
        error = tb.subtask_assignee_error(doc, viewer, new_assignee)
        if error:
            _throw_denied(_(error))
        if new_assignee:
            _require_assignable(new_assignee)
        row.assigned_to = new_assignee
        row.reminder_sent_on = None

    old_due = getdate(row.due_date) if row.due_date else None
    new_due = old_due
    if _truthy(clear_due_date):
        new_due = None
    elif due_date not in (None, ""):
        new_due = _parse_date(due_date, _("Subtask due date"))
    if new_due != old_due:
        row.due_date = new_due
        row.reminder_sent_on = None

    doc.save(ignore_permissions=True)
    if new_assignee and new_assignee != old_assignee:
        _notify(
            doc,
            tb.N_SUBTASK_ASSIGNED,
            viewer.user,
            {"subtask_assignee": new_assignee, "subtask_title": row.title, "due_date": _s(row.due_date)},
        )
    return _task_payload(doc, viewer)


@frappe.whitelist(methods=["POST"])
def delete_subtask(name: str, subtask_id: str) -> Dict[str, Any]:
    viewer = _viewer()
    _require_board(viewer)
    doc = _load_task(name, viewer, lock=True)
    row = _find_subtask(doc, subtask_id)
    if not tb.can_edit_subtask(doc, row, viewer):
        _refuse(doc, _("You cannot remove this subtask."))
    removed_title = row.title
    doc.remove(row)
    doc.save(ignore_permissions=True)
    _history(doc.name, tb.EV_SUBTASK_REMOVED, f'Removed subtask "{removed_title}"', viewer.user)
    return _task_payload(doc, viewer)


@frappe.whitelist(methods=["POST"])
def set_subtask_done(name: str, subtask_id: str, done: Any) -> Dict[str, Any]:
    viewer = _viewer()
    _require_board(viewer)
    doc = _load_task(name, viewer, lock=True)
    row = _find_subtask(doc, subtask_id)
    if not tb.can_toggle_subtask(doc, row, viewer):
        _refuse(doc, _("You cannot tick this subtask."))
    want = _truthy(done)
    if bool(cint(row.is_done)) == want:
        # Idempotent: a double tap (or two people at once) is not an error.
        return _task_payload(doc, viewer)

    row.is_done = 1 if want else 0
    row.done_by = viewer.user if want else None
    row.done_on = now_datetime() if want else None
    doc.save(ignore_permissions=True)
    _history(
        doc.name,
        tb.EV_SUBTASK_DONE if want else tb.EV_SUBTASK_UNDONE,
        f'{"Completed" if want else "Reopened"} subtask "{row.title}"',
        viewer.user,
    )
    if want:
        _notify(
            doc,
            tb.N_SUBTASK_DONE,
            viewer.user,
            {"subtask_title": row.title, "subtask_created_by": row.created_by},
        )
    return _task_payload(doc, viewer)


# ── Entries ──────────────────────────────────────────────────────────────────


def _viewer_for_mention(user: str) -> Optional[tb.Viewer]:
    try:
        if not frappe.db.get_value("User", user, "enabled"):
            return None
    except Exception:
        return None
    return tb.get_viewer(user)


@frappe.whitelist(methods=["POST"])
def add_entry(name: str, kind: str, content: str, mentions: Any = None) -> Dict[str, Any]:
    """Add a Log or a Comment. Returns the get_task shape plus ``entry`` (the new entry's name)."""
    viewer = _viewer()
    _require_board(viewer)
    doc = _load_task(name, viewer, lock=True)

    entry_kind = str(kind or "").strip().capitalize()
    if entry_kind not in tb.USER_ENTRY_KINDS:
        _throw_invalid(_("Kind must be Log or Comment"))
    allowed = tb.can_log(doc, viewer) if entry_kind == tb.KIND_LOG else tb.can_comment(doc, viewer)
    if not allowed:
        _refuse(doc, _("You cannot write on this task."))

    text = _clean_text(content)
    if not text:
        _throw_invalid(_("Write something first"))

    mention_list: List[str] = []
    for value in _parse_list(mentions, _("Mentions")):
        user = str(value or "").strip()
        if user and user != viewer.user and user not in mention_list:
            mention_list.append(user)
    bad = tb.invalid_mentions(doc, mention_list, _viewer_for_mention)
    if bad:
        _throw_invalid(_("{0} cannot see this task").format(", ".join(bad)))

    entry = frappe.get_doc(
        {
            "doctype": tb.ENTRY_DOCTYPE,
            "task": doc.name,
            "kind": entry_kind,
            "content": text,
            "author": viewer.user,
            "mentions": json.dumps(mention_list) if mention_list else None,
        }
    ).insert(ignore_permissions=True)

    if mention_list:
        _notify(doc, tb.N_MENTIONED, viewer.user, {"mentions": mention_list, "content": text})

    payload = _task_payload(doc, viewer)
    payload["entry"] = entry.name
    return payload


# ── Attachments ──────────────────────────────────────────────────────────────


def _decode_file(file_data: Any) -> bytes:
    raw = str(file_data or "").strip()
    if raw.startswith("data:") and "," in raw:
        raw = raw.split(",", 1)[1]
    raw = "".join(raw.split())
    if not raw:
        _throw_invalid(_("The file is empty"))
    limit_mb = tb.MAX_ATTACHMENT_BYTES // (1024 * 1024)
    # Cheap size check before decoding a large payload into memory.
    if (len(raw) * 3) // 4 > tb.MAX_ATTACHMENT_BYTES + 3:
        _throw_invalid(_("Files are limited to {0} MB").format(limit_mb))
    raw += "=" * (-len(raw) % 4)
    try:
        content = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        _throw_invalid(_("The file could not be read"))
    if not content:
        _throw_invalid(_("The file is empty"))
    if len(content) > tb.MAX_ATTACHMENT_BYTES:
        _throw_invalid(_("Files are limited to {0} MB").format(limit_mb))
    return content


@frappe.whitelist(methods=["POST"])
def upload_attachment(
    name: str, filename: str, file_data: str, entry: Optional[str] = None
) -> Dict[str, Any]:
    viewer = _viewer()
    _require_board(viewer)
    doc = _load_task(name, viewer, lock=True)

    target_doctype, target_name = tb.TASK_DOCTYPE, doc.name
    entry_row = None
    entry_name = str(entry or "").strip()
    if entry_name:
        entry_row = frappe.db.get_value(
            tb.ENTRY_DOCTYPE, entry_name, ["name", "task", "kind", "author"], as_dict=True
        )
        if not entry_row or entry_row.task != doc.name:
            frappe.throw(_("Entry not found on this task"), frappe.DoesNotExistError)
        target_doctype, target_name = tb.ENTRY_DOCTYPE, entry_row.name
    # Card files follow can_upload; an entry's files follow that entry's kind
    # (a Comment is allowed on a Done card, so its files must be too).
    error = tb.upload_error(doc, viewer, entry_row)
    if error:
        kind, message = error
        if kind == "permission":
            _throw_denied(_(message))
        _throw_invalid(_(message))

    clean_name = os.path.basename(str(filename or "").replace("\\", "/")).strip()
    if len(clean_name) > 140:
        # Keep the extension: it is what the content type is guessed from.
        stem, ext = os.path.splitext(clean_name)
        clean_name = stem[: max(140 - len(ext[:20]), 1)] + ext[:20]
    if not clean_name:
        _throw_invalid(_("File name is required"))
    blocked = tb.blocked_upload_extension(clean_name)
    if blocked:
        _throw_invalid(
            _("{0} files cannot be attached. Attach a photo, a PDF or a document instead.").format(blocked)
        )

    content = _decode_file(file_data)
    frappe.get_doc(
        {
            "doctype": "File",
            "file_name": clean_name,
            "is_private": 1,
            "content": content,
            "attached_to_doctype": target_doctype,
            "attached_to_name": target_name,
        }
    ).insert(ignore_permissions=True)

    if target_doctype == tb.TASK_DOCTYPE:
        _history(doc.name, tb.EV_ATTACHMENT_ADDED, f'Attached "{clean_name}"', viewer.user)
    return _task_payload(doc, viewer)


@frappe.whitelist(methods=["POST"])
def remove_attachment(name: str, file: str) -> Dict[str, Any]:
    viewer = _viewer()
    _require_board(viewer)
    doc = _load_task(name, viewer, lock=True)
    file_name = str(file or "").strip()
    row = (
        frappe.db.get_value(
            "File",
            file_name,
            ["name", "file_name", "owner", "attached_to_doctype", "attached_to_name"],
            as_dict=True,
        )
        if file_name
        else None
    )
    if row and row.attached_to_doctype == tb.ENTRY_DOCTYPE:
        entry_task = frappe.db.get_value(tb.ENTRY_DOCTYPE, row.attached_to_name, "task")
        if entry_task == doc.name:
            _throw_invalid(_("Files on log entries and comments cannot be removed."))
    if not row or row.attached_to_doctype != tb.TASK_DOCTYPE or row.attached_to_name != doc.name:
        frappe.throw(_("Attachment not found on this task"), frappe.DoesNotExistError)
    if not tb.can_remove_attachment(doc, viewer, row.owner):
        _refuse(doc, _("Only the uploader or the task creator can remove this file."))

    # The File guard (permissions.tasks.guard_task_file_*) refuses to delete a
    # task attachment unless this flag is up: it is what stops the uploader
    # doing the same through /api/resource/File behind the board's back.
    previous_flag = getattr(frappe.flags, "jarz_task_file_ok", False)
    frappe.flags.jarz_task_file_ok = True
    try:
        frappe.delete_doc("File", row.name, ignore_permissions=True)
    finally:
        frappe.flags.jarz_task_file_ok = previous_flag
    _history(doc.name, tb.EV_ATTACHMENT_REMOVED, f'Removed "{row.file_name}"', viewer.user)
    return _task_payload(doc, viewer)
