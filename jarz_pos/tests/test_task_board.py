"""Task Board rules (services/task_board.py) -- pure mocks, no site needed.

Every rule the board enforces is a pure function over a Viewer and a task
dict, so the whole matrix is pinned here without touching the database:

* visibility -- full access, creator, assignee, subtask assignee, managers
  seeing line-manager work, and the one thing managers must NOT see: a task one
  manager handed another;
* who may create (line managers may not);
* status transitions -- who may make each move, the reason a send-back or a
  reopen requires, the open-subtask block on submit, and the assignee==creator
  shortcut straight to Done;
* subtask assignment (a line manager may only assign to themselves);
* @mention validation (a mentioned user must be able to see the task);
* who is notified of each event (never the actor);
* which reminders the daily pass sends, and that a re-run the same day sends none.

Deliberately plain ``unittest.TestCase`` with no FrappeTestCase: on ERPNext v16
that pulls in ``erpnext.tests.utils`` whose BootStrapTestData() collides with
the populated CI site. The module also runs without frappe installed at all
(a stub is registered below), so ``python -m pytest`` works outside a bench.
"""

import datetime
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

try:  # pragma: no cover - depends on the environment
    import frappe  # noqa: F401
except ImportError:  # pragma: no cover
    _fake = types.ModuleType("frappe")

    def _whitelist(*args, **kwargs):
        if args and callable(args[0]) and len(args) == 1 and not kwargs:
            return args[0]
        return lambda fn: fn

    _fake.whitelist = _whitelist
    _fake._ = lambda message: message
    _fake.local = SimpleNamespace()
    _fake.session = SimpleNamespace(user="Guest")
    _fake.db = SimpleNamespace(escape=lambda s: "'" + str(s) + "'")
    _fake.get_roles = lambda *a, **k: []
    _fake.log_error = lambda *a, **k: None
    _fake.get_traceback = lambda *a, **k: ""
    _fake.cache = lambda: SimpleNamespace(get_value=lambda *a: None, set_value=lambda *a, **k: None)
    sys.modules["frappe"] = _fake

from jarz_pos.services import task_board as tb

ADMIN = "Administrator"
OWNER = "owner@jarz.test"  # on the full-access list, holds no manager role
MGR1 = "mgr1@jarz.test"
MGR2 = "mgr2@jarz.test"
MGR3 = "mgr3@jarz.test"
LM1 = "lm1@jarz.test"
LM2 = "lm2@jarz.test"
CASHIER = "cashier@jarz.test"

ROLES_BY_USER = {
    ADMIN: {"Administrator", "System Manager"},
    OWNER: {"POS User"},
    MGR1: {"JARZ Manager"},
    MGR2: {"System Manager"},
    MGR3: {"JARZ Manager"},
    LM1: {"jarz line manager"},
    LM2: {"JARZ line manager"},  # the other spelling -- both are real Role records
    CASHIER: {"POS User"},
}
FULL_ACCESS = {OWNER}
MANAGERS = {ADMIN, MGR1, MGR2, MGR3}

TODAY = datetime.date(2026, 9, 23)
TOMORROW = TODAY + datetime.timedelta(days=1)
YESTERDAY = TODAY - datetime.timedelta(days=1)


def V(user):
    return tb.make_viewer(user, ROLES_BY_USER.get(user, set()), FULL_ACCESS, MANAGERS)


def task(**overrides):
    base = {
        "name": "TASK-2026-00001",
        "title": "Count the freezer",
        "status": tb.STATUS_TODO,
        "priority": tb.PRIORITY_NORMAL,
        "created_by": MGR1,
        "assigned_to": LM1,
        "archived": 0,
        "subtasks": [],
    }
    base.update(overrides)
    return base


def sub(**overrides):
    base = {"name": "row1", "title": "Top shelf", "assigned_to": None, "is_done": 0, "created_by": MGR1}
    base.update(overrides)
    return base


# ─────────────────────────────────────────────────────────────────────────────
# People
# ─────────────────────────────────────────────────────────────────────────────


class TestPeople(unittest.TestCase):
    def test_line_manager_both_spellings_are_board_users(self):
        for user in (LM1, LM2):
            viewer = V(user)
            self.assertTrue(viewer.is_board_user, user)
            self.assertTrue(viewer.is_line_manager, user)
            self.assertFalse(viewer.is_manager, user)

    def test_managers(self):
        for user in (ADMIN, MGR1, MGR2):
            self.assertTrue(V(user).is_manager, user)
            self.assertTrue(V(user).is_board_user, user)

    def test_full_access_user_without_roles_is_a_board_user_who_sees_all(self):
        viewer = V(OWNER)
        self.assertTrue(viewer.is_board_user)
        self.assertTrue(viewer.can_view_all)
        self.assertFalse(viewer.is_manager)

    def test_cashier_and_guest_are_not_board_users(self):
        self.assertFalse(V(CASHIER).is_board_user)
        self.assertFalse(V("Guest").is_board_user)
        self.assertFalse(V("").is_board_user)

    def test_only_managers_and_full_access_may_create(self):
        self.assertTrue(V(MGR1).can_create)
        self.assertTrue(V(MGR2).can_create)
        self.assertTrue(V(ADMIN).can_create)
        self.assertTrue(V(OWNER).can_create)
        self.assertFalse(V(LM1).can_create)
        self.assertFalse(V(LM2).can_create)
        self.assertFalse(V(CASHIER).can_create)

    def test_overview_follows_create(self):
        self.assertTrue(V(MGR1).can_view_overview)
        self.assertFalse(V(LM1).can_view_overview)


# ─────────────────────────────────────────────────────────────────────────────
# Visibility
# ─────────────────────────────────────────────────────────────────────────────


class TestVisibility(unittest.TestCase):
    def test_full_access_sees_even_manager_to_manager_tasks(self):
        t = task(created_by=MGR1, assigned_to=MGR3)
        self.assertTrue(tb.is_visible(t, V(OWNER)))
        self.assertTrue(tb.is_visible(t, V(ADMIN)))

    def test_creator_and_assignee_see_it(self):
        t = task(created_by=MGR1, assigned_to=LM1)
        self.assertTrue(tb.is_visible(t, V(MGR1)))
        self.assertTrue(tb.is_visible(t, V(LM1)))

    def test_subtask_assignee_sees_it(self):
        t = task(subtasks=[sub(assigned_to=LM2)])
        self.assertTrue(tb.is_visible(t, V(LM2)))

    def test_manager_sees_line_manager_work_they_are_not_on(self):
        t = task(created_by=MGR1, assigned_to=LM1)
        self.assertTrue(tb.is_visible(t, V(MGR2)))

    def test_manager_does_not_see_a_task_one_manager_gave_another(self):
        t = task(created_by=MGR1, assigned_to=MGR3)
        self.assertFalse(tb.is_visible(t, V(MGR2)))
        # ...while both of the two managers involved do.
        self.assertTrue(tb.is_visible(t, V(MGR1)))
        self.assertTrue(tb.is_visible(t, V(MGR3)))

    def test_line_manager_does_not_see_other_peoples_tasks(self):
        t = task(created_by=MGR1, assigned_to=LM1)
        self.assertFalse(tb.is_visible(t, V(LM2)))

    def test_outsiders_see_nothing(self):
        t = task()
        self.assertFalse(tb.is_visible(t, V(CASHIER)))
        self.assertFalse(tb.is_visible(t, V("Guest")))

    def test_visibility_sql_for_a_manager_excludes_manager_assignees(self):
        fake_db = SimpleNamespace(escape=lambda s: "'" + str(s) + "'")
        with patch.object(tb.frappe, "db", fake_db, create=True):
            sql = tb.visibility_sql(V(MGR2), "t")
            lm_sql = tb.visibility_sql(V(LM1), "t")
            full_sql = tb.visibility_sql(V(OWNER), "t")
        self.assertIn("NOT IN", sql)
        for manager in MANAGERS:
            self.assertIn(f"'{manager}'", sql)
        self.assertNotIn("NOT IN", lm_sql)
        self.assertIn("t.`assigned_to` = 'lm1@jarz.test'", lm_sql)
        self.assertIn("jts_vis", lm_sql)
        self.assertEqual(full_sql, "")

    def test_visibility_sql_guest_sees_nothing(self):
        self.assertEqual(tb.visibility_sql(V("Guest"), "t"), "1=0")


# ─────────────────────────────────────────────────────────────────────────────
# Editing / archiving
# ─────────────────────────────────────────────────────────────────────────────


class TestEditRights(unittest.TestCase):
    def test_only_creator_or_full_access_edit(self):
        t = task()
        self.assertTrue(tb.can_edit_task(t, V(MGR1)))
        self.assertTrue(tb.can_edit_task(t, V(OWNER)))
        self.assertFalse(tb.can_edit_task(t, V(LM1)))
        self.assertFalse(tb.can_edit_task(t, V(MGR2)))

    def test_done_and_archived_are_locked_but_archivable(self):
        for t in (task(status=tb.STATUS_DONE), task(archived=1)):
            self.assertFalse(tb.can_edit_task(t, V(MGR1)))
            self.assertTrue(tb.can_archive(t, V(MGR1)))
            self.assertFalse(tb.can_archive(t, V(LM1)))

    def test_permissions_on_a_done_task_keep_comments_open(self):
        perms = tb.permissions_for(task(status=tb.STATUS_DONE), V(LM1))
        self.assertTrue(perms["can_comment"])
        self.assertFalse(perms["can_log"])
        self.assertFalse(perms["can_upload"])
        self.assertFalse(perms["can_add_subtask"])

    def test_archived_task_is_read_only(self):
        perms = tb.permissions_for(task(archived=1), V(MGR1))
        for key in ("can_edit", "can_add_subtask", "can_log", "can_comment", "can_upload"):
            self.assertFalse(perms[key], key)
        self.assertTrue(perms["can_archive"])
        self.assertEqual(perms["allowed_transitions"], [])

    def test_attachment_removal(self):
        t = task()
        self.assertTrue(tb.can_remove_attachment(t, V(LM1), LM1))  # uploader
        self.assertTrue(tb.can_remove_attachment(t, V(MGR1), LM1))  # creator
        self.assertTrue(tb.can_remove_attachment(t, V(OWNER), LM1))  # full access
        self.assertFalse(tb.can_remove_attachment(t, V(LM1), MGR1))
        self.assertFalse(tb.can_remove_attachment(task(status=tb.STATUS_DONE), V(MGR1), MGR1))


# ─────────────────────────────────────────────────────────────────────────────
# Transitions
# ─────────────────────────────────────────────────────────────────────────────


class TestTransitions(unittest.TestCase):
    def test_assignee_on_todo(self):
        allowed = tb.allowed_transitions(task(), V(LM1))
        self.assertEqual(allowed, [tb.STATUS_IN_PROGRESS, tb.STATUS_IN_REVIEW])

    def test_creator_on_todo_may_not_submit(self):
        self.assertEqual(tb.allowed_transitions(task(), V(MGR1)), [tb.STATUS_IN_PROGRESS])

    def test_observing_manager_may_not_move_it(self):
        self.assertEqual(tb.allowed_transitions(task(), V(MGR2)), [])
        err = tb.transition_error(task(), V(MGR2), tb.STATUS_IN_PROGRESS)
        self.assertEqual(err[0], "permission")

    def test_in_progress_back_to_todo(self):
        t = task(status=tb.STATUS_IN_PROGRESS)
        self.assertIsNone(tb.transition_error(t, V(LM1), tb.STATUS_TODO))
        self.assertIsNone(tb.transition_error(t, V(MGR1), tb.STATUS_TODO))
        self.assertIsNone(tb.transition_error(t, V(OWNER), tb.STATUS_TODO))

    def test_in_review_only_creator_approves_or_sends_back(self):
        t = task(status=tb.STATUS_IN_REVIEW)
        self.assertEqual(tb.allowed_transitions(t, V(MGR1)), [tb.STATUS_IN_PROGRESS, tb.STATUS_DONE])
        self.assertEqual(tb.needs_reason_for(t, V(MGR1)), [tb.STATUS_IN_PROGRESS])
        self.assertEqual(tb.allowed_transitions(t, V(LM1)), [])
        self.assertEqual(tb.allowed_transitions(t, V(OWNER)), [tb.STATUS_IN_PROGRESS, tb.STATUS_DONE])

    def test_send_back_requires_a_reason(self):
        t = task(status=tb.STATUS_IN_REVIEW)
        self.assertEqual(
            tb.transition_error(t, V(MGR1), tb.STATUS_IN_PROGRESS, "  "),
            ("validation", "A reason is required"),
        )
        self.assertIsNone(tb.transition_error(t, V(MGR1), tb.STATUS_IN_PROGRESS, "Photos missing"))

    def test_reopen_requires_a_reason_and_the_creator(self):
        t = task(status=tb.STATUS_DONE)
        self.assertEqual(tb.allowed_transitions(t, V(MGR1)), [tb.STATUS_IN_PROGRESS])
        self.assertEqual(tb.transition_error(t, V(MGR1), tb.STATUS_IN_PROGRESS)[0], "validation")
        self.assertIsNone(tb.transition_error(t, V(MGR1), tb.STATUS_IN_PROGRESS, "Came back"))
        self.assertEqual(tb.transition_error(t, V(LM1), tb.STATUS_IN_PROGRESS, "x")[0], "permission")

    def test_submit_refused_while_a_subtask_is_open(self):
        t = task(status=tb.STATUS_IN_PROGRESS, subtasks=[sub(is_done=1), sub(name="row2", is_done=0)])
        self.assertEqual(
            tb.transition_error(t, V(LM1), tb.STATUS_IN_REVIEW),
            ("validation", tb.MSG_OPEN_SUBTASKS),
        )
        # Still offered, so the client can show the button and the reason.
        self.assertIn(tb.STATUS_IN_REVIEW, tb.allowed_transitions(t, V(LM1)))
        done = task(status=tb.STATUS_IN_PROGRESS, subtasks=[sub(is_done=1)])
        self.assertIsNone(tb.transition_error(done, V(LM1), tb.STATUS_IN_REVIEW))

    def test_assignee_who_is_the_creator_may_go_straight_to_done(self):
        t = task(created_by=MGR1, assigned_to=MGR1, status=tb.STATUS_IN_PROGRESS)
        self.assertIn(tb.STATUS_DONE, tb.allowed_transitions(t, V(MGR1)))
        self.assertIsNone(tb.transition_error(t, V(MGR1), tb.STATUS_DONE))
        blocked = task(created_by=MGR1, assigned_to=MGR1, subtasks=[sub()])
        self.assertEqual(tb.transition_error(blocked, V(MGR1), tb.STATUS_DONE)[1], tb.MSG_OPEN_SUBTASKS)

    def test_ordinary_assignee_may_not_skip_review(self):
        self.assertEqual(tb.transition_error(task(), V(LM1), tb.STATUS_DONE)[0], "permission")

    def test_archived_task_does_not_move(self):
        err = tb.transition_error(task(archived=1), V(MGR1), tb.STATUS_IN_PROGRESS)
        self.assertEqual(err, ("validation", tb.MSG_ARCHIVED))

    def test_unknown_and_same_status(self):
        self.assertEqual(tb.transition_error(task(), V(LM1), "Blocked")[0], "validation")
        self.assertEqual(tb.transition_error(task(), V(LM1), tb.STATUS_TODO)[0], "validation")

    def test_transition_events(self):
        self.assertEqual(tb.transition_event(tb.STATUS_IN_PROGRESS, tb.STATUS_IN_REVIEW), tb.N_SUBMITTED)
        self.assertEqual(tb.transition_event(tb.STATUS_IN_REVIEW, tb.STATUS_DONE), tb.N_APPROVED)
        self.assertEqual(tb.transition_event(tb.STATUS_IN_REVIEW, tb.STATUS_IN_PROGRESS), tb.N_SENT_BACK)
        self.assertEqual(tb.transition_event(tb.STATUS_DONE, tb.STATUS_IN_PROGRESS), tb.N_REOPENED)
        self.assertIsNone(tb.transition_event(tb.STATUS_TODO, tb.STATUS_IN_PROGRESS))

    def test_transition_updates_stamp_and_clear(self):
        now = datetime.datetime(2026, 9, 23, 10, 0)
        done = tb.transition_updates(task(status=tb.STATUS_IN_REVIEW), tb.STATUS_DONE, MGR1, now)
        self.assertEqual(done["completed_by"], MGR1)
        self.assertEqual(done["completed_on"], now)
        back = tb.transition_updates(task(status=tb.STATUS_DONE, started_on=now), tb.STATUS_IN_PROGRESS, MGR1, now)
        self.assertIsNone(back["completed_on"])
        self.assertIsNone(back["completed_by"])
        self.assertIsNone(back["submitted_on"])
        self.assertNotIn("started_on", back)


# ─────────────────────────────────────────────────────────────────────────────
# Subtasks
# ─────────────────────────────────────────────────────────────────────────────


class TestSubtasks(unittest.TestCase):
    def test_line_manager_assignee_may_only_assign_themselves(self):
        t = task()
        viewer = V(LM1)
        self.assertTrue(tb.can_add_subtask(t, viewer))
        self.assertFalse(tb.can_assign_subtask_to_others(t, viewer))
        self.assertIsNone(tb.subtask_assignee_error(t, viewer, None))
        self.assertIsNone(tb.subtask_assignee_error(t, viewer, LM1))
        self.assertIsNotNone(tb.subtask_assignee_error(t, viewer, LM2))

    def test_creator_and_visible_manager_may_assign_anyone(self):
        t = task()
        for user in (MGR1, MGR2, OWNER):
            self.assertIsNone(tb.subtask_assignee_error(t, V(user), LM2), user)

    def test_manager_who_cannot_see_the_task_may_not_add(self):
        t = task(created_by=MGR1, assigned_to=MGR3)
        self.assertFalse(tb.can_add_subtask(t, V(MGR2)))

    def test_subtask_assignee_who_is_not_the_card_assignee_may_not_add(self):
        t = task(subtasks=[sub(assigned_to=LM2)])
        self.assertFalse(tb.can_add_subtask(t, V(LM2)))

    def test_no_subtasks_on_a_done_task(self):
        self.assertFalse(tb.can_add_subtask(task(status=tb.STATUS_DONE), V(MGR1)))

    def test_edit_and_toggle_rights(self):
        row = sub(created_by=LM1, assigned_to=LM2)
        t = task(subtasks=[row])
        self.assertTrue(tb.can_edit_subtask(t, row, V(LM1)))  # subtask creator
        self.assertTrue(tb.can_edit_subtask(t, row, V(MGR1)))  # task creator
        self.assertFalse(tb.can_edit_subtask(t, row, V(LM2)))  # only its assignee
        self.assertTrue(tb.can_toggle_subtask(t, row, V(LM2)))  # subtask assignee
        self.assertTrue(tb.can_toggle_subtask(t, row, V(LM1)))  # card assignee
        self.assertFalse(tb.can_toggle_subtask(t, row, V(MGR2)))  # an observer


# ─────────────────────────────────────────────────────────────────────────────
# Mentions
# ─────────────────────────────────────────────────────────────────────────────


class TestMentions(unittest.TestCase):
    def test_only_users_who_can_see_the_task_may_be_mentioned(self):
        t = task()
        known = {u: V(u) for u in ROLES_BY_USER}
        bad = tb.invalid_mentions(t, [MGR2, OWNER, LM2, "ghost@jarz.test"], known.get)
        self.assertEqual(bad, [LM2, "ghost@jarz.test"])

    def test_no_mentions_is_fine(self):
        self.assertEqual(tb.invalid_mentions(task(), [], lambda u: None), [])


# ─────────────────────────────────────────────────────────────────────────────
# Notifications
# ─────────────────────────────────────────────────────────────────────────────


class TestNotificationRecipients(unittest.TestCase):
    def test_assigned_goes_to_the_assignee_never_the_actor(self):
        self.assertEqual(tb.notification_recipients(tb.N_ASSIGNED, task(), MGR1), [LM1])
        self.assertEqual(tb.notification_recipients(tb.N_ASSIGNED, task(assigned_to=MGR1), MGR1), [])

    def test_unassigned_goes_to_the_previous_assignee(self):
        rec = tb.notification_recipients(tb.N_UNASSIGNED, task(assigned_to=LM2), MGR1, {"previous_assignee": LM1})
        self.assertEqual(rec, [LM1])

    def test_subtask_assigned(self):
        rec = tb.notification_recipients(tb.N_SUBTASK_ASSIGNED, task(), MGR1, {"subtask_assignee": LM2})
        self.assertEqual(rec, [LM2])
        self.assertEqual(
            tb.notification_recipients(tb.N_SUBTASK_ASSIGNED, task(), LM1, {"subtask_assignee": LM1}), []
        )

    def test_subtask_done_tells_task_and_subtask_creators(self):
        rec = tb.notification_recipients(tb.N_SUBTASK_DONE, task(), LM2, {"subtask_created_by": LM1})
        self.assertEqual(rec, sorted([MGR1, LM1]))
        # Same person twice -> once; the actor -> never.
        rec = tb.notification_recipients(tb.N_SUBTASK_DONE, task(), LM1, {"subtask_created_by": MGR1})
        self.assertEqual(rec, [MGR1])

    def test_status_events(self):
        self.assertEqual(tb.notification_recipients(tb.N_SUBMITTED, task(), LM1), [MGR1])
        for event in (tb.N_APPROVED, tb.N_SENT_BACK, tb.N_REOPENED):
            self.assertEqual(tb.notification_recipients(event, task(), MGR1), [LM1], event)

    def test_mentions_exclude_the_author(self):
        rec = tb.notification_recipients(tb.N_MENTIONED, task(), LM1, {"mentions": [LM1, MGR2, MGR2]})
        self.assertEqual(rec, [MGR2])

    def test_reminder_recipients(self):
        rec = tb.notification_recipients(tb.N_OVERDUE, task(), None, {"recipients": [LM1, MGR1]})
        self.assertEqual(rec, sorted([LM1, MGR1]))
        self.assertEqual(tb.notification_recipients(tb.N_DUE_SOON, task(), None), [LM1])

    def test_texts(self):
        title, body = tb.notification_text(tb.N_ASSIGNED, "Count", "Mona", {"due_date": "2026-09-24"})
        self.assertEqual(title, "New task: Count")
        self.assertEqual(body, "From Mona · due 2026-09-24")
        title, body = tb.notification_text(tb.N_SENT_BACK, "Count", "Mona", {"reason": "Photos missing"})
        self.assertEqual((title, body), ("Task sent back: Count", "Photos missing"))
        title, _ = tb.notification_text(tb.N_SUBTASK_ASSIGNED, "Count", "Mona", {"subtask_title": "Top"})
        self.assertEqual(title, "New subtask: Top (Count)")
        title, body = tb.notification_text(tb.N_MENTIONED, "Count", "Mona", {"content": "x" * 300})
        self.assertEqual(title, "Mona mentioned you: Count")
        self.assertLessEqual(len(body), tb.MENTION_BODY_MAX)
        self.assertEqual(tb.notification_text(tb.N_OVERDUE, "Count", "", {})[0], "Overdue: Count")
        self.assertEqual(tb.notification_text(tb.N_DUE_SOON, "Count", "", {})[0], "Due tomorrow: Count")


# ─────────────────────────────────────────────────────────────────────────────
# Reminders
# ─────────────────────────────────────────────────────────────────────────────


class TestTaskReminders(unittest.TestCase):
    def test_due_tomorrow(self):
        plan = tb.plan_task_reminder(task(due_date=TOMORROW), TODAY)
        self.assertEqual(plan["event"], tb.N_DUE_SOON)
        self.assertEqual(plan["recipients"], [LM1])
        self.assertEqual(plan["updates"], {"last_reminder_on": TODAY})

    def test_same_day_rerun_sends_nothing(self):
        self.assertIsNone(tb.plan_task_reminder(task(due_date=TOMORROW, last_reminder_on=TODAY), TODAY))
        self.assertIsNone(tb.plan_task_reminder(task(due_date=YESTERDAY, last_reminder_on=str(TODAY)), TODAY))

    def test_first_overdue_day_tells_the_creator_once(self):
        plan = tb.plan_task_reminder(task(due_date=YESTERDAY), TODAY)
        self.assertEqual(plan["event"], tb.N_OVERDUE)
        self.assertEqual(plan["recipients"], sorted([LM1, MGR1]))
        self.assertEqual(plan["updates"]["creator_overdue_notified"], 1)
        later = tb.plan_task_reminder(
            task(due_date=YESTERDAY, creator_overdue_notified=1, last_reminder_on=YESTERDAY), TODAY
        )
        self.assertEqual(later["recipients"], [LM1])
        self.assertNotIn("creator_overdue_notified", later["updates"])

    def test_creator_who_is_the_assignee_is_told_once(self):
        plan = tb.plan_task_reminder(task(created_by=MGR1, assigned_to=MGR1, due_date=YESTERDAY), TODAY)
        self.assertEqual(plan["recipients"], [MGR1])

    def test_skipped_cards(self):
        for t in (
            task(due_date=YESTERDAY, status=tb.STATUS_IN_REVIEW),
            task(due_date=YESTERDAY, status=tb.STATUS_DONE),
            task(due_date=YESTERDAY, archived=1),
            task(due_date=None),
            task(due_date=TODAY),
            task(due_date=TODAY + datetime.timedelta(days=5)),
        ):
            self.assertIsNone(tb.plan_task_reminder(t, TODAY), t)

    def test_string_dates_are_accepted(self):
        plan = tb.plan_task_reminder(task(due_date=str(TOMORROW)), TODAY)
        self.assertEqual(plan["event"], tb.N_DUE_SOON)


class TestSubtaskReminders(unittest.TestCase):
    def test_due_tomorrow_and_overdue(self):
        live = task(status=tb.STATUS_IN_PROGRESS)
        soon = tb.plan_subtask_reminder(sub(assigned_to=LM2, due_date=TOMORROW), live, TODAY)
        self.assertEqual((soon["event"], soon["recipients"]), (tb.N_DUE_SOON, [LM2]))
        late = tb.plan_subtask_reminder(sub(assigned_to=LM2, due_date=YESTERDAY), live, TODAY)
        self.assertEqual(late["event"], tb.N_OVERDUE)
        self.assertEqual(late["updates"], {"reminder_sent_on": TODAY})

    def test_skipped_subtasks(self):
        live = task()
        for row in (
            sub(assigned_to=LM2, due_date=YESTERDAY, is_done=1),
            sub(assigned_to=None, due_date=YESTERDAY),
            sub(assigned_to=LM2, due_date=None),
            sub(assigned_to=LM2, due_date=YESTERDAY, reminder_sent_on=TODAY),
            sub(assigned_to=LM2, due_date=TODAY + datetime.timedelta(days=3)),
        ):
            self.assertIsNone(tb.plan_subtask_reminder(row, live, TODAY), row)
        row = sub(assigned_to=LM2, due_date=YESTERDAY)
        self.assertIsNone(tb.plan_subtask_reminder(row, task(status=tb.STATUS_DONE), TODAY))
        self.assertIsNone(tb.plan_subtask_reminder(row, task(archived=1), TODAY))


# ─────────────────────────────────────────────────────────────────────────────
# Board ordering / overdue flag
# ─────────────────────────────────────────────────────────────────────────────


class TestBoardOrdering(unittest.TestCase):
    def test_priority_then_due_date_then_newest(self):
        cards = [
            {"name": "a", "priority": "Normal", "due_date": None, "modified": "2026-09-20 10:00:00"},
            {"name": "b", "priority": "Urgent", "due_date": "2026-09-30", "modified": "2026-09-20 10:00:00"},
            {"name": "c", "priority": "Normal", "due_date": "2026-09-25", "modified": "2026-09-20 10:00:00"},
            {"name": "d", "priority": "High", "due_date": None, "modified": "2026-09-21 10:00:00"},
            {"name": "e", "priority": "Normal", "due_date": None, "modified": "2026-09-22 10:00:00"},
        ]
        self.assertEqual([c["name"] for c in tb.sort_cards(cards)], ["b", "d", "c", "e", "a"])

    def test_is_overdue(self):
        self.assertTrue(tb.is_overdue(task(due_date=YESTERDAY), TODAY))
        self.assertFalse(tb.is_overdue(task(due_date=YESTERDAY, status=tb.STATUS_DONE), TODAY))
        self.assertFalse(tb.is_overdue(task(due_date=TODAY), TODAY))
        self.assertFalse(tb.is_overdue(task(), TODAY))


# ─────────────────────────────────────────────────────────────────────────────
# Pending-approvals queues
# ─────────────────────────────────────────────────────────────────────────────


class TestApprovalQueues(unittest.TestCase):
    def setUp(self):
        from jarz_pos.api import approvals

        self.approvals = approvals

    def test_non_board_user_gets_no_task_queues(self):
        with patch.object(tb, "get_viewer", return_value=V(CASHIER)):
            self.assertIsNone(self.approvals._tasks_assigned_queue())
            self.assertIsNone(self.approvals._tasks_review_queue())

    def test_board_user_gets_counts(self):
        with patch.object(tb, "get_viewer", return_value=V(LM1)), \
                patch.object(tb, "count_assigned_open", return_value=4) as assigned, \
                patch.object(tb, "count_review_waiting", return_value=0):
            self.assertEqual(self.approvals._tasks_assigned_queue(), {"count": 4})
            self.assertEqual(self.approvals._tasks_review_queue(), {"count": 0})
        assigned.assert_called_once_with(LM1)

    def test_queue_keys_are_registered(self):
        keys = [key for key, _ in self.approvals._QUEUES]
        self.assertIn("tasks_assigned", keys)
        self.assertIn("tasks_review", keys)


# ─────────────────────────────────────────────────────────────────────────────
# Review follow-ups: attachments, Desk permissions, File guard, settings,
# approve-with-open-subtasks, reminder buckets, overview bound
# ─────────────────────────────────────────────────────────────────────────────


class TestApproveWithOpenSubtasks(unittest.TestCase):
    def test_in_review_to_done_refused_while_a_subtask_is_open(self):
        t = task(status=tb.STATUS_IN_REVIEW, subtasks=[sub(is_done=0)])
        self.assertEqual(
            tb.transition_error(t, V(MGR1), tb.STATUS_DONE), ("validation", tb.MSG_OPEN_SUBTASKS)
        )
        self.assertIsNone(
            tb.transition_error(task(status=tb.STATUS_IN_REVIEW, subtasks=[sub(is_done=1)]), V(MGR1), tb.STATUS_DONE)
        )

    def test_send_back_still_allowed_with_open_subtasks(self):
        t = task(status=tb.STATUS_IN_REVIEW, subtasks=[sub(is_done=0)])
        self.assertIsNone(tb.transition_error(t, V(MGR1), tb.STATUS_IN_PROGRESS, "Finish the shelf"))


class TestAttachmentSafety(unittest.TestCase):
    def test_markup_and_script_extensions_are_refused(self):
        for name in ("page.html", "PAGE.HTM", "logo.SVG", "feed.xml", "a.js", "a.mjs", "a.xhtml"):
            self.assertIsNotNone(tb.blocked_upload_extension(name), name)
        for name in ("photo.jpg", "scan.PDF", "sheet.xlsx", "notes.txt", "noextension", ""):
            self.assertIsNone(tb.blocked_upload_extension(name), name)

    def test_only_the_safe_allowlist_is_served_inline(self):
        for ctype in ("image/png", "image/jpeg", "image/gif", "image/webp", "application/pdf", "IMAGE/PNG"):
            self.assertEqual(tb.content_disposition_for(ctype), "inline", ctype)
        for ctype in (
            "text/html", "image/svg+xml", "application/xml", "text/xml", "application/javascript",
            "text/plain", "application/octet-stream", None, "",
        ):
            self.assertEqual(tb.content_disposition_for(ctype), "attachment", ctype)


class TestUploadRules(unittest.TestCase):
    def test_card_files(self):
        self.assertIsNone(tb.upload_error(task(), V(LM1)))
        self.assertEqual(tb.upload_error(task(status=tb.STATUS_DONE), V(LM1)), ("validation", tb.MSG_DONE_LOCKED))
        self.assertEqual(tb.upload_error(task(), V(CASHIER))[0], "permission")

    def test_comment_files_allowed_on_a_done_card(self):
        entry = {"kind": tb.KIND_COMMENT, "author": LM1}
        self.assertIsNone(tb.upload_error(task(status=tb.STATUS_DONE), V(LM1), entry))

    def test_log_files_follow_the_log_rule(self):
        entry = {"kind": tb.KIND_LOG, "author": LM1}
        self.assertIsNone(tb.upload_error(task(), V(LM1), entry))
        self.assertEqual(
            tb.upload_error(task(status=tb.STATUS_DONE), V(LM1), entry), ("validation", tb.MSG_DONE_LOCKED)
        )

    def test_only_the_author_attaches_to_an_entry(self):
        entry = {"kind": tb.KIND_COMMENT, "author": MGR1}
        self.assertEqual(tb.upload_error(task(), V(LM1), entry)[0], "permission")

    def test_history_entries_take_no_files(self):
        entry = {"kind": tb.KIND_HISTORY, "author": LM1}
        self.assertEqual(tb.upload_error(task(), V(LM1), entry)[0], "validation")

    def test_archived_card_takes_no_files_at_all(self):
        entry = {"kind": tb.KIND_COMMENT, "author": LM1}
        self.assertEqual(tb.upload_error(task(archived=1), V(LM1), entry), ("validation", tb.MSG_ARCHIVED))


def _perm_env(module, user, flags=None, stored=None):
    """Patch the frappe globals the File guard reads; frappe.throw raises."""
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(patch.object(module, "_stored_attachment", return_value=stored))
    stack.enter_context(patch.object(module.frappe, "session", SimpleNamespace(user=user), create=True))
    stack.enter_context(patch.object(module.frappe, "flags", flags or SimpleNamespace(), create=True))
    stack.enter_context(patch.object(module.frappe, "_", lambda m: m, create=True))
    stack.enter_context(patch.object(module.frappe, "PermissionError", PermissionError, create=True))
    stack.enter_context(
        patch.object(module.frappe, "throw", side_effect=PermissionError("refused"), create=True)
    )
    return stack


class TestDeskPermissionHook(unittest.TestCase):
    def setUp(self):
        from jarz_pos.permissions import tasks as ptasks

        self.p = ptasks

    def test_writes_are_administrator_only(self):
        doc = dict(task(), doctype=tb.TASK_DOCTYPE)
        self.assertTrue(self.p.has_permission(doc, "write", ADMIN))
        with patch.object(tb, "get_viewer", return_value=V(OWNER)) as get_viewer:
            for ptype in ("write", "create", "delete", "submit", "cancel", "share"):
                self.assertFalse(self.p.has_permission(doc, ptype, OWNER), ptype)
        get_viewer.assert_not_called()

    def test_reads_follow_visibility(self):
        doc = dict(task(), doctype=tb.TASK_DOCTYPE)
        with patch.object(tb, "get_viewer", side_effect=V):
            self.assertTrue(self.p.has_permission(doc, "read", MGR2))
            self.assertTrue(self.p.has_permission(doc, "print", LM1))
            self.assertFalse(self.p.has_permission(doc, "read", LM2))


class TestTaskFileGuard(unittest.TestCase):
    def setUp(self):
        from jarz_pos.permissions import tasks as ptasks

        self.p = ptasks

    def _doc(self, attached_to=tb.TASK_DOCTYPE, is_new=False):
        return SimpleNamespace(name="F-1", attached_to_doctype=attached_to, is_new=lambda: is_new)

    def test_change_allowed_rule(self):
        self.assertTrue(self.p.task_file_change_allowed(ADMIN, SimpleNamespace()))
        self.assertTrue(self.p.task_file_change_allowed(LM1, SimpleNamespace(jarz_task_file_ok=True)))
        self.assertTrue(self.p.task_file_change_allowed(LM1, SimpleNamespace(in_migrate=True)))
        self.assertFalse(self.p.task_file_change_allowed(LM1, SimpleNamespace()))

    def test_uploader_cannot_delete_a_task_file(self):
        stored = {"attached_to_doctype": tb.ENTRY_DOCTYPE}
        with _perm_env(self.p, LM1, stored=stored):
            with self.assertRaises(PermissionError):
                self.p.guard_task_file_trash(self._doc(tb.ENTRY_DOCTYPE))

    def test_board_remove_path_and_administrator_may_delete(self):
        stored = {"attached_to_doctype": tb.TASK_DOCTYPE}
        with _perm_env(self.p, LM1, flags=SimpleNamespace(jarz_task_file_ok=True), stored=stored):
            self.p.guard_task_file_trash(self._doc())
        with _perm_env(self.p, ADMIN, stored=stored):
            self.p.guard_task_file_trash(self._doc())

    def test_other_files_are_untouched(self):
        stored = {"attached_to_doctype": "Sales Invoice"}
        with _perm_env(self.p, LM1, stored=stored):
            self.p.guard_task_file_trash(self._doc("Sales Invoice"))
            self.p.guard_task_file_update(self._doc("Sales Invoice"))

    def test_existing_task_file_cannot_be_modified_or_repointed(self):
        stored = {"attached_to_doctype": tb.TASK_DOCTYPE}
        with _perm_env(self.p, LM1, stored=stored):
            with self.assertRaises(PermissionError):
                self.p.guard_task_file_update(self._doc())
            with self.assertRaises(PermissionError):
                self.p.guard_task_file_update(self._doc("Customer"))

    def test_other_file_cannot_be_pointed_at_a_task(self):
        with _perm_env(self.p, LM1, stored={"attached_to_doctype": "Customer"}):
            with self.assertRaises(PermissionError):
                self.p.guard_task_file_update(self._doc(tb.TASK_DOCTYPE))

    def test_inserts_pass(self):
        with _perm_env(self.p, LM1, stored=None):
            self.p.guard_task_file_update(self._doc())  # no stored row yet
        with _perm_env(self.p, LM1, stored={"attached_to_doctype": tb.TASK_DOCTYPE}):
            self.p.guard_task_file_update(self._doc(is_new=True))


class TestSettingsGuard(unittest.TestCase):
    def setUp(self):
        try:
            from jarz_pos.doctype.jarz_task_settings import jarz_task_settings
        except ImportError:
            self.skipTest("frappe.model not importable outside a bench")
        self.s = jarz_task_settings

    def test_who_may_change_the_full_access_list(self):
        rule = self.s.may_change_full_access
        none = SimpleNamespace()
        self.assertTrue(rule(ADMIN, [], none))
        self.assertTrue(rule(OWNER, [OWNER], none))
        self.assertFalse(rule(MGR2, [OWNER], none))  # a System Manager adding themselves
        self.assertFalse(rule(MGR2, [], none))
        self.assertTrue(rule("", [], SimpleNamespace(in_migrate=True)))
        self.assertTrue(rule("", [], SimpleNamespace(in_install=True)))


class TestReminderBuckets(unittest.TestCase):
    def setUp(self):
        try:
            from jarz_pos.services import task_reminders
        except ImportError:
            self.skipTest("frappe.utils not importable outside a bench")
        self.r = task_reminders

    def test_buckets_are_separate_and_skip_today(self):
        self.assertEqual(self.r.DUE_BUCKETS, {"due_soon": "= %(tomorrow)s", "overdue": "< %(today)s"})

    def test_each_bucket_runs_even_if_another_fails(self):
        calls = []

        def card(today, tomorrow, summary, bucket):
            calls.append(("card", bucket))
            if bucket == "overdue":
                raise RuntimeError("boom")

        def subtask(today, tomorrow, summary, bucket):
            calls.append(("subtask", bucket))

        with patch.object(self.r, "_card_pass", side_effect=card), \
                patch.object(self.r, "_subtask_pass", side_effect=subtask), \
                patch.object(self.r, "_safe_log"), \
                patch.object(self.r, "nowdate", return_value="2026-09-23"), \
                patch.object(self.r.frappe, "db", SimpleNamespace(exists=lambda *a, **k: True), create=True):
            summary = self.r.run_task_reminders()
        self.assertEqual(
            calls,
            [("card", "due_soon"), ("card", "overdue"), ("subtask", "due_soon"), ("subtask", "overdue")],
        )
        self.assertEqual(summary["errors"], 1)


class TestOverviewQuery(unittest.TestCase):
    def setUp(self):
        try:
            from jarz_pos.api import tasks as tasks_api
        except ImportError:
            self.skipTest("frappe.utils not importable outside a bench")
        self.api = tasks_api

    def test_done_cards_are_bounded_to_the_period_in_sql(self):
        seen = {}

        def fake_sql(query, values=None, as_dict=False):
            seen["query"], seen["values"] = query, values
            return []

        with patch.object(self.api, "_viewer", return_value=V(MGR1)), \
                patch.object(tb, "visibility_sql", return_value=""), \
                patch.object(self.api, "nowdate", return_value="2026-09-23"), \
                patch.object(self.api, "now_datetime", return_value=datetime.datetime(2026, 9, 23, 9, 0)), \
                patch.object(self.api.frappe, "db", SimpleNamespace(sql=fake_sql), create=True):
            out = self.api.get_overview(days=7)
        self.assertIn("t.completed_on >= %(cutoff)s", seen["query"])
        self.assertIn("t.status != %(done)s", seen["query"])
        self.assertEqual(seen["values"]["done"], tb.STATUS_DONE)
        self.assertEqual(out["period_days"], 7)
        self.assertEqual(out["people"], [])


if __name__ == "__main__":
    unittest.main()
