"""Branch access API -- who may use the POS at which branch.

Thin transport over :mod:`jarz_pos.services.branch_access`. The service owns
every rule (scope, the open-branch refusal, the day-access life cycle, the
log); this module only coerces what the mobile client posts and wraps the
answer in ``{"success": true, ...}``.

Coercion matters more than it looks here: Dio posts form data, so ``allowed``
arrives as the *string* ``"0"`` or ``"false"``, and a plain truth test would
turn every remove into an add.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import frappe
from frappe import _
from frappe.utils import cint, getdate

from jarz_pos.services import branch_access as service


def _clean(value: Any) -> str | None:
	text = str(value or "").strip()
	return text or None


def _coerce_date(value: Any, label: str) -> str:
	if not value:
		frappe.throw(_("{0} is required.").format(label))
	try:
		return str(getdate(value))
	except Exception:
		frappe.throw(_("{0} is not a valid date: {1}").format(label, value))


@frappe.whitelist()
def get_branch_access() -> dict[str, Any]:
	"""Branches (with open-shift state), users, memberships and day accesses."""
	data = service.get_branch_access()
	data["success"] = True
	return data


@frappe.whitelist(methods=["POST"])
def set_branch_access(
	user: str,
	pos_profile: str,
	allowed: Any,
	notes: str | None = None,
) -> dict[str, Any]:
	"""Add (``allowed=1``) or remove (``allowed=0``) a permanent membership.

	``changed`` is false when the user is already in the requested state; no
	log row is written then.
	"""
	result = service.set_branch_access(
		_clean(user),
		_clean(pos_profile),
		service.as_flag(allowed),
		notes=_clean(notes),
	)
	result["success"] = True
	return result


@frappe.whitelist(methods=["POST"])
def grant_day_access(
	user: str,
	pos_profile: str,
	access_date: str,
	notes: str | None = None,
) -> dict[str, Any]:
	"""One branch day of POS access: today starts now, a later day is scheduled."""
	result = service.grant_day_access(
		_clean(user),
		_clean(pos_profile),
		_coerce_date(access_date, _("Date")),
		notes=_clean(notes),
	)
	result["success"] = True
	return result


@frappe.whitelist(methods=["POST"])
def cancel_day_access(name: str) -> dict[str, Any]:
	"""Withdraw a scheduled or running day access."""
	result = service.cancel_day_access(_clean(name))
	result["success"] = True
	return result


@frappe.whitelist(methods=["POST"])
def move_branch(
	user: str,
	to_pos_profile: str,
	from_pos_profile: str | None = None,
	keep_old_access: Any = 0,
	notes: str | None = None,
) -> dict[str, Any]:
	"""Move somebody to another branch for good: POS access and future shifts.

	``from_pos_profile`` defaults to the branch their schedule puts them at;
	``keep_old_access=1`` keeps the old branch's POS access.
	"""
	return service.move_branch(
		_clean(user),
		_clean(to_pos_profile),
		from_pos_profile=_clean(from_pos_profile),
		keep_old_access=service.as_flag(keep_old_access),
		notes=_clean(notes),
	)


@frappe.whitelist()
def get_access_log(
	pos_profile: str | None = None,
	user: str | None = None,
	limit: Any = 50,
	start: Any = 0,
) -> dict[str, Any]:
	"""The change history, newest first, paged with ``start``/``limit``."""
	result = service.get_access_log(
		pos_profile=_clean(pos_profile),
		user=_clean(user),
		limit=cint(limit) or 50,
		start=cint(start),
	)
	result["success"] = True
	return result
