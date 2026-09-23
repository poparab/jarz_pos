"""Daily Task Board reminders (cron ``0 9 * * *``, site time Africa/Cairo).

One pass a morning:

* a card due TOMORROW -> ``due_soon`` to its assignee;
* a card PAST its due date -> ``overdue`` to its assignee every day, and to its
  creator once (the first overdue day only);
* an open, assigned subtask due tomorrow / overdue -> the same to the subtask's
  assignee, once per day.

Only live cards count: not archived, and To Do / In Progress for the card
reminders (a card In Review is waiting on its reviewer, not its assignee).

What was sent is recorded on the rows themselves (``last_reminder_on``,
``creator_overdue_notified``, ``reminder_sent_on``), written BEFORE the push is
queued, so a re-run the same day -- a retried job, a manual run -- never sends a
second copy. The decision logic is pure (``services.task_board.plan_*``) and is
what the tests pin; this module only reads, writes the bookkeeping and queues.

Batched and capped (``REMINDER_BATCH_CAP`` rows per bucket -- due-tomorrow and
overdue, cards and subtasks -- each selected separately, so a pile of overdue
rows can never starve tomorrow's reminders) so a backlog can never crowd the
rest of the scheduler slot. Never raises.
"""

from __future__ import annotations

from typing import Any, Dict

import frappe
from frappe.utils import add_days, getdate, nowdate

from jarz_pos.services import task_board as tb


def _queue(task_name: str, event: str, recipients, extra: Dict[str, Any]) -> bool:
    from jarz_pos.api.notifications import enqueue_task_notification

    return enqueue_task_notification(task_name, event, recipients, actor=None, extra=extra)


#: The two date buckets, each selected (and capped) on its own so a backlog of
#: overdue rows can never starve the due-tomorrow ones. Neither matches a row
#: due TODAY -- nothing is sent for those.
DUE_BUCKETS = {
    "due_soon": "= %(tomorrow)s",
    "overdue": "< %(today)s",
}


def _safe_log(title: str) -> None:
    """``frappe.log_error`` that cannot itself take the pass down."""
    try:
        frappe.log_error(frappe.get_traceback(), title[:140])
    except Exception:
        pass


def _card_pass(today, tomorrow, summary: Dict[str, int], bucket: str) -> None:
    rows = frappe.db.sql(
        f"""
        SELECT name, title, status, archived, assigned_to, created_by, due_date,
               last_reminder_on, creator_overdue_notified
        FROM `tabJarz Task`
        WHERE archived = 0 AND status IN %(open)s
          AND due_date IS NOT NULL AND due_date {DUE_BUCKETS[bucket]}
          AND (last_reminder_on IS NULL OR last_reminder_on < %(today)s)
        ORDER BY due_date ASC, name ASC
        LIMIT %(cap)s
        """,
        {"open": tb.OPEN_STATUSES, "tomorrow": tomorrow, "today": today, "cap": tb.REMINDER_BATCH_CAP},
        as_dict=True,
    )
    for row in rows or []:
        try:
            plan = tb.plan_task_reminder(row, today)
            if not plan:
                continue
            frappe.db.set_value(tb.TASK_DOCTYPE, row.name, plan["updates"], update_modified=False)
            if plan["recipients"] and _queue(
                row.name,
                plan["event"],
                plan["recipients"],
                {"due_date": str(row.due_date or ""), "recipients": plan["recipients"]},
            ):
                summary["cards"] += 1
        except Exception:
            summary["errors"] += 1
            _safe_log(f"task_reminders: card {row.get('name')} failed")


def _subtask_pass(today, tomorrow, summary: Dict[str, int], bucket: str) -> None:
    rows = frappe.db.sql(
        f"""
        SELECT s.name, s.parent, s.title AS subtask_title, s.assigned_to, s.due_date,
               s.is_done, s.reminder_sent_on, t.status, t.archived
        FROM `tabJarz Task Subtask` s
        INNER JOIN `tabJarz Task` t ON t.name = s.parent
        WHERE s.parenttype = 'Jarz Task' AND s.is_done = 0
          AND IFNULL(s.assigned_to, '') != ''
          AND s.due_date IS NOT NULL AND s.due_date {DUE_BUCKETS[bucket]}
          AND (s.reminder_sent_on IS NULL OR s.reminder_sent_on < %(today)s)
          AND t.archived = 0 AND t.status != %(done)s
        ORDER BY s.due_date ASC, s.name ASC
        LIMIT %(cap)s
        """,
        {"tomorrow": tomorrow, "today": today, "done": tb.STATUS_DONE, "cap": tb.REMINDER_BATCH_CAP},
        as_dict=True,
    )
    for row in rows or []:
        try:
            task = {"status": row.status, "archived": row.archived}
            plan = tb.plan_subtask_reminder(row, task, today)
            if not plan:
                continue
            frappe.db.set_value(tb.SUBTASK_DOCTYPE, row.name, plan["updates"], update_modified=False)
            if plan["recipients"] and _queue(
                row.parent,
                plan["event"],
                plan["recipients"],
                {
                    "due_date": str(row.due_date or ""),
                    "subtask_title": row.subtask_title or "",
                    "recipients": plan["recipients"],
                },
            ):
                summary["subtasks"] += 1
        except Exception:
            summary["errors"] += 1
            _safe_log(f"task_reminders: subtask {row.get('name')} failed")


def run_task_reminders() -> Dict[str, int]:
    """Scheduler entry point. Returns ``{cards, subtasks, errors}``. Never raises."""
    summary = {"cards": 0, "subtasks": 0, "errors": 0}
    try:
        if not frappe.db.exists("DocType", tb.TASK_DOCTYPE):
            return summary
        today = getdate(nowdate())
        tomorrow = getdate(add_days(today, 1))
        # Each bucket in its own try: one failing query must not cost the rest.
        for step in (_card_pass, _subtask_pass):
            for bucket in DUE_BUCKETS:
                try:
                    step(today, tomorrow, summary, bucket)
                except Exception:
                    summary["errors"] += 1
                    _safe_log(f"task_reminders: {getattr(step, '__name__', 'pass')} {bucket} failed")
    except Exception:
        summary["errors"] += 1
        _safe_log("task_reminders: pass failed")
    return summary
