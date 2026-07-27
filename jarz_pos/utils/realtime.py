"""Branch-scoped realtime publishing.

Two different bugs lived in the ``publish_realtime`` calls across this app, and
they pull in opposite directions:

1. ``publish_realtime(event, payload, user="*")`` — Frappe has no wildcard user.
   ``user`` is turned into the room ``user:<value>``, so ``"*"`` addresses a room
   named ``user:*`` that nobody ever joins. Every one of those events was
   discarded by the socket server. The same applies to passing a *list* of users:
   the room becomes ``user:['a@b.com', ...]``, which is equally empty. That is
   why board updates from another device only ever showed up via the polling
   fallback.

2. ``publish_realtime(event, payload)`` with no ``user`` at all — falls through to
   the site-wide ``all`` room, delivering to *every* System User on the site
   regardless of branch.

So the same codebase was both dropping events on the floor and broadcasting
others to everyone. This module gives both cases one correct destination: the
users assigned to the branch the event belongs to.
"""
from __future__ import annotations

import frappe
from typing import Any, Dict, Iterable, List, Optional, Sequence

from jarz_pos.utils.access_control import get_invoice_branch, get_users_for_pos_profiles


def resolve_branch_recipients(
    profiles: Sequence[str],
    *,
    extra_users: Optional[Iterable[str]] = None,
) -> List[str]:
    """Users who should receive an event about *profiles*."""
    recipients = set(get_users_for_pos_profiles(profiles))
    for user in extra_users or []:
        cleaned = str(user or "").strip()
        if cleaned:
            recipients.add(cleaned)
    recipients.discard("Guest")
    return sorted(recipients)


def publish_to_branches(
    event: str,
    payload: Dict[str, Any],
    profiles: Sequence[str],
    *,
    extra_users: Optional[Iterable[str]] = None,
    after_commit: bool = False,
) -> List[str]:
    """Emit *event* to every user assigned to any of *profiles*.

    Returns the recipient list so callers can log or assert on it.

    When a branch has no assigned users the event is dropped and logged — those
    orders are invisible to everyone under branch scoping anyway, so falling
    back to a site-wide broadcast would leak them to unrelated branches rather
    than help anybody.
    """
    cleaned_profiles = sorted({str(p).strip() for p in (profiles or []) if str(p or "").strip()})
    recipients = resolve_branch_recipients(cleaned_profiles, extra_users=extra_users)

    if not recipients:
        frappe.logger().warning(
            f"REALTIME: no recipients for {event}; branches={cleaned_profiles or 'none'} — event dropped"
        )
        return []

    for user in recipients:
        try:
            frappe.publish_realtime(event, payload, user=user, after_commit=after_commit)
        except Exception:
            # One bad recipient must not stop the rest of the branch hearing it.
            frappe.log_error(
                frappe.get_traceback(),
                f"publish_realtime {event} failed for {user}",
            )
    return recipients


def publish_invoice_event(
    event: str,
    payload: Dict[str, Any],
    invoice: Any,
    *,
    extra_profiles: Optional[Sequence[str]] = None,
    extra_users: Optional[Iterable[str]] = None,
    after_commit: bool = False,
) -> List[str]:
    """Emit *event* to the branch that owns *invoice*.

    ``extra_profiles`` covers transfers, where both the branch losing the order
    and the branch receiving it need to redraw.
    """
    profiles: List[str] = []
    branch = get_invoice_branch(invoice) if invoice is not None else ""
    if branch:
        profiles.append(branch)
    for profile in extra_profiles or []:
        cleaned = str(profile or "").strip()
        if cleaned:
            profiles.append(cleaned)

    return publish_to_branches(
        event,
        payload,
        profiles,
        extra_users=extra_users,
        after_commit=after_commit,
    )


def publish_invoice_event_by_name(
    event: str,
    payload: Dict[str, Any],
    invoice_name: str,
    *,
    extra_profiles: Optional[Sequence[str]] = None,
    extra_users: Optional[Iterable[str]] = None,
) -> List[str]:
    """:func:`publish_invoice_event` when only the invoice name is at hand."""
    name = str(invoice_name or "").strip()
    row = None
    if name:
        try:
            row = frappe.db.get_value(
                "Sales Invoice",
                name,
                ["custom_kanban_profile", "pos_profile"],
                as_dict=True,
            )
        except Exception:
            frappe.log_error(
                frappe.get_traceback(),
                f"Failed to resolve branch for realtime event on {name}",
            )
            row = None

    return publish_invoice_event(
        event,
        payload,
        row,
        extra_profiles=extra_profiles,
        extra_users=extra_users,
    )
