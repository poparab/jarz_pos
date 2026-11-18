"""Approve what a moderator told the AI as a Jarz Brand Fact, from the app.

A draft written on a moderator's instruction lists ``fact_conflicts``: lines
"<what the moderator said> → <what our facts say>". A manager (anyone who may
create a Brand Fact: System Manager or JARZ Manager) can turn one line into an
Approved fact, so the next drafts may state it without the warning.

The link back to the line lives in the fact's ``source``
("Moderator override <draft> #<index>: <line>"), so no schema change is needed
and a second tap on the same line returns the fact already made.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

import frappe
from frappe import _

from jarz_pos.services import chat_common as cc

BRAND_FACT = "Jarz Brand Fact"
SOURCE_PREFIX = "Moderator override"
DEFAULT_CATEGORY = "Other"
TOPIC_MAX = 140
FACT_MAX = 1000
_MARKER = re.compile(r"^Moderator override (\S+) #(\d+)\b")


def source_marker(draft: str, index: int) -> str:
	"""``Moderator override <draft> #<index>``. Pure."""
	return f"{SOURCE_PREFIX} {draft} #{int(index)}"


def parse_marker(source: Any) -> tuple[str, int] | None:
	"""``(draft, index)`` from a fact's source, or ``None``. Pure."""
	match = _MARKER.match(str(source or "").strip())
	return (match.group(1), int(match.group(2))) if match else None


def can_approve(user: str | None = None) -> bool:
	"""May this user add Brand Facts? Never raises."""
	user = user or frappe.session.user
	if user == "Administrator":
		return True
	try:
		return bool(frappe.has_permission(BRAND_FACT, "create", user=user))
	except Exception:
		return False


def approved_indexes(drafts: Iterable[str]) -> dict[str, list[int]]:
	"""``{draft: [conflict index, …]}`` already approved as facts. Never raises."""
	wanted = {str(d) for d in drafts if d}
	if not wanted:
		return {}
	try:
		rows = frappe.get_all(
			BRAND_FACT,
			filters={"source": ["like", f"{SOURCE_PREFIX} %"]},
			fields=["source"],
			limit_page_length=2000,
			ignore_permissions=True,
		)
	except Exception:
		return {}
	out: dict[str, list[int]] = {}
	for row in rows:
		parsed = parse_marker(row.get("source"))
		if parsed and parsed[0] in wanted:
			out.setdefault(parsed[0], [])
			if parsed[1] not in out[parsed[0]]:
				out[parsed[0]].append(parsed[1])
	for indexes in out.values():
		indexes.sort()
	return out


def _category(value: Any) -> str:
	options = str(frappe.get_meta(BRAND_FACT).get_field("category").options or "").split("\n")
	value = str(value or "").strip()
	return value if value in options else DEFAULT_CATEGORY


def approve(
	draft: str,
	index: int,
	topic: str,
	fact_en: str | None = None,
	fact_ar: str | None = None,
	category: str | None = None,
) -> dict[str, Any]:
	"""Create the Approved Brand Fact for conflict ``index`` of ``draft``."""
	if not can_approve():
		frappe.throw(_("Only a manager can add approved facts."), frappe.PermissionError)
	row = frappe.db.get_value(
		cc.DRAFT, draft, ["name", "conversation", "fact_conflicts", "moderator_hint"], as_dict=True
	)
	if not row:
		frappe.throw(_("Draft {0} not found.").format(draft), frappe.DoesNotExistError)
	lines = cc.split_warnings(row.fact_conflicts)
	try:
		index = int(index)
	except (TypeError, ValueError):
		index = -1
	if not 0 <= index < len(lines):
		frappe.throw(_("That fact line is no longer on the draft."))
	topic = " ".join(str(topic or "").split())[:TOPIC_MAX]
	fact_en = str(fact_en or "").strip()[:FACT_MAX]
	fact_ar = str(fact_ar or "").strip()[:FACT_MAX]
	if not topic:
		frappe.throw(_("Give the fact a topic."))
	if not fact_en and not fact_ar:
		frappe.throw(_("Write the fact in English or Arabic."))

	marker = source_marker(row.name, index)
	existing = frappe.get_all(
		BRAND_FACT,
		filters={"source": ["like", f"{marker}%"]},
		pluck="name",
		limit_page_length=1,
		ignore_permissions=True,
	)
	if existing:
		name = existing[0]
	else:
		fact = frappe.get_doc(
			{
				"doctype": BRAND_FACT,
				"topic": topic,
				"category": _category(category),
				"status": "Approved",
				"fact_owner": frappe.session.user if frappe.session.user != "Administrator" else None,
				"approved_by": cc.full_name(frappe.session.user) or frappe.session.user,
				"approved_on": frappe.utils.getdate(cc.now()),
				"fact_en": fact_en,
				"fact_ar": fact_ar,
				"source": f"{marker}: {lines[index]}"[:FACT_MAX],
				"notes": (f"Moderator instruction ({row.conversation}): {row.moderator_hint or ''}")[
					:FACT_MAX
				],
			}
		)
		fact.insert(ignore_permissions=True)
		name = fact.name
		_clear_draft_context()
	return {
		"fact": name,
		"draft": row.name,
		"approved_conflicts": approved_indexes([row.name]).get(row.name, []),
	}


def _clear_draft_context() -> None:
	try:
		from jarz_pos.services.chat_settings import DRAFT_CONTEXT_CACHE_PREFIX

		frappe.cache().delete_keys(DRAFT_CONTEXT_CACHE_PREFIX)
	except Exception:
		pass
