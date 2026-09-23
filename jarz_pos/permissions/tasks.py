"""Desk/REST permission hooks for the Task Board DocTypes.

The only DocPerm on ``Jarz Task`` and ``Jarz Task Entry`` is System Manager, so
Desk and ``/api/resource`` are already closed to everyone else. These hooks
narrow that one grant further to the board's visibility rule
(``services.task_board.is_visible``): Administrator and the full-access list
see every task; any other System Manager sees only the tasks the board itself
would show them. Without this, a System Manager could read a manager-to-manager
task in Desk that the board deliberately hides.

Frappe's controller hooks can narrow a role permission but never grant one, so
returning True here only lets the normal DocPerm check continue.
"""

from __future__ import annotations

from typing import Any, Optional

import frappe

from jarz_pos.services import task_board as tb


def _task_for_visibility(task_name: str) -> Optional[dict]:
    """Just enough of a task to evaluate visibility (creator, assignee, subtask assignees)."""
    if not task_name:
        return None
    row = frappe.db.get_value(
        tb.TASK_DOCTYPE, task_name, ["name", "created_by", "assigned_to"], as_dict=True
    )
    if not row:
        return None
    subs = frappe.db.sql(
        """
        SELECT assigned_to FROM `tabJarz Task Subtask`
        WHERE parent = %s AND parenttype = 'Jarz Task'
        """,
        (task_name,),
        as_dict=True,
    )
    row["subtasks"] = list(subs or [])
    return row


#: Read-only permission types the Desk may exercise, still narrowed to visibility.
READ_PTYPES = frozenset({"read", "select", "report", "print", "export"})


def has_permission(
    doc: Any,
    ptype: str = "read",
    user: Optional[str] = None,
    debug: bool = False,
) -> bool:
    """Deny any task (or task entry) the user could not see on the board.

    Anything beyond reading (write, create, delete, submit, cancel, share,
    email...) is Administrator-only in Desk/REST: every legitimate change goes
    through ``api.tasks``, which enforces the board rules and writes with
    ``ignore_permissions=True`` -- so this hook never sees it. A System Manager
    editing a task in Desk would otherwise bypass the transition rules, the
    history and the immutability of entries.
    """
    resolved_user = user or frappe.session.user
    if resolved_user == "Administrator":
        return True
    if (ptype or "read") not in READ_PTYPES:
        return False
    try:
        viewer = tb.get_viewer(resolved_user)
        if viewer.can_view_all:
            return True

        doctype = getattr(doc, "doctype", None) or (
            doc.get("doctype") if hasattr(doc, "get") else None
        )
        if doctype == tb.ENTRY_DOCTYPE:
            task_name = getattr(doc, "task", None) or (
                doc.get("task") if hasattr(doc, "get") else None
            )
            task = _task_for_visibility(task_name)
            # An entry with no (or a vanished) task has nothing to be visible through.
            return bool(task) and tb.is_visible(task, viewer)

        # A brand-new Jarz Task in Desk has no participants yet; the visibility
        # rule then answers for the manager tier only, which is the right bar.
        return tb.is_visible(doc, viewer)
    except Exception:
        # A permission lookup failure must never expose a task.
        return False


def get_permission_query_conditions(
    user: Optional[str] = None, doctype: Optional[str] = None
) -> str:
    """List/report filter for ``Jarz Task``."""
    try:
        viewer = tb.get_viewer(user or frappe.session.user)
        return tb.visibility_sql(viewer, "`tabJarz Task`")
    except Exception:
        return "1=0"


def get_entry_permission_query_conditions(
    user: Optional[str] = None, doctype: Optional[str] = None
) -> str:
    """List/report filter for ``Jarz Task Entry``: only entries of visible tasks."""
    try:
        viewer = tb.get_viewer(user or frappe.session.user)
        condition = tb.visibility_sql(viewer, "`jte_task`")
        if not condition:
            return ""
        return (
            "EXISTS (SELECT 1 FROM `tabJarz Task` `jte_task`"
            " WHERE `jte_task`.`name` = `tabJarz Task Entry`.`task`"
            f" AND {condition})"
        )
    except Exception:
        return "1=0"


# ── File guard (doc_events on "File") ────────────────────────────────────────
#
# Core's File DocPerm gives role "All" read/write/delete, and File's own
# has_permission answers True for the owner. So without this, whoever uploaded
# a task attachment could delete it, or re-point ``attached_to_name`` at another
# record, straight through /api/resource/File -- skipping the board's rules,
# its history entry and the immutability of log/comment attachments.
#
# Only EXISTING task files are guarded: inserts (upload_attachment) pass
# untouched, and a File attached to anything else returns before any rule is
# applied, so no other File operation on the site can be affected. The one
# legitimate removal path, api.tasks.remove_attachment, raises
# ``frappe.flags.jarz_task_file_ok`` around its delete.

TASK_FILE_DOCTYPES = frozenset({tb.TASK_DOCTYPE, tb.ENTRY_DOCTYPE})


def task_file_change_allowed(user: Optional[str], flags: Any) -> bool:
    """May *user* modify/delete an existing task attachment right now? (pure)"""
    if user == "Administrator":
        return True
    for flag in ("jarz_task_file_ok", "in_migrate", "in_install", "in_uninstall", "in_patch"):
        if getattr(flags, flag, False):
            return True
    return False


def _stored_attachment(name: Any) -> Optional[dict]:
    if not name:
        return None
    try:
        return frappe.db.get_value(
            "File", name, ["attached_to_doctype", "attached_to_name"], as_dict=True
        )
    except Exception:
        return None


def _is_task_file(*doctypes: Any) -> bool:
    return any(dt in TASK_FILE_DOCTYPES for dt in doctypes if dt)


def guard_task_file_trash(doc: Any, method: Optional[str] = None) -> None:
    """File.on_trash: refuse deleting a task attachment outside the Task Board."""
    try:
        stored = _stored_attachment(getattr(doc, "name", None)) or {}
        if not _is_task_file(getattr(doc, "attached_to_doctype", None), stored.get("attached_to_doctype")):
            return
        if task_file_change_allowed(frappe.session.user, frappe.flags):
            return
    except Exception:
        # Never let the guard itself break an unrelated File operation.
        return
    frappe.throw(
        frappe._("Task Board attachments can only be removed from the Task Board."),
        frappe.PermissionError,
    )


def guard_task_file_update(doc: Any, method: Optional[str] = None) -> None:
    """File.validate: refuse modifying an EXISTING task attachment outside the board.

    Judged on the DB-stored ``attached_to_doctype`` as well as the incoming one,
    so neither re-pointing a task file elsewhere nor pointing another file at a
    task gets through. Inserts are never touched.
    """
    try:
        if not getattr(doc, "name", None) or doc.is_new():
            return
        stored = _stored_attachment(doc.name)
        if not stored:
            # No row yet: this is an insert (upload_attachment), not a change.
            return
        if not _is_task_file(stored.get("attached_to_doctype"), getattr(doc, "attached_to_doctype", None)):
            return
        if task_file_change_allowed(frappe.session.user, frappe.flags):
            return
    except Exception:
        return
    frappe.throw(
        frappe._("Task Board attachments cannot be changed."),
        frappe.PermissionError,
    )
