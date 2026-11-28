"""Controller for the Jarz SOP DocType.

Two rules live here and both exist to keep production history honest:

* ``step_no`` is renumbered from ``idx`` on every save, so the number an
  operator confirms on the phone is the number stored in the execution log.
  Letting the author type it by hand guarantees a duplicate "step 4" eventually.
* A procedure that has already been stamped onto a Work Order cannot have its
  steps edited.  Without that, version-stamping is decoration: the natural
  thing to do is open the SOP and fix the step, which silently rewrites what
  every past batch was made to.

The Recipe Book adds a third: only an **Approved** recipe that has an Item AND
at least one step may be active, because "active" is what puts a recipe on the
Production Board.  A Trial or Retired version, or one with no steps (a
layers-only or ingredients-only version), is forced inactive, and a recipe with
no Item is identified by its ``recipe_title``.

A procedure used in production is frozen: its steps, its jar layers and its
ingredient rows cannot change (a new version carries the change instead).
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.model.document import Document

from jarz_pos.services.sop_rendering import VERSION_STAMP_SEPARATOR

SOP_DOCTYPE = "Jarz SOP"
WORK_ORDER_STAMP_FIELD = "jarz_sop_version"

# What "the same rows" means for the frozen-once-used guard, per child table.
_FROZEN_TABLES: dict[str, tuple[str, ...]] = {
	"steps": (
		"title",
		"instruction",
		"image",
		"duration_mins",
		"scaling_mode",
		"requires_confirmation",
		"capture_type",
		"capture_label",
		"capture_min",
		"capture_max",
	),
	"layers": ("layer", "grams", "note"),
	"ingredients": ("ingredient", "item_code", "qty", "uom", "section", "note"),
}

STAGE_APPROVED = "Approved"
STAGE_TRIAL = "Trial"
STAGE_RETIRED = "Retired"
STAGES = (STAGE_APPROVED, STAGE_TRIAL, STAGE_RETIRED)


def _field(row: Any, name: str, default: Any = None) -> Any:
	if isinstance(row, dict):
		return row.get(name, default)
	return getattr(row, name, default)


def _set_field(row: Any, name: str, value: Any) -> None:
	if isinstance(row, dict):
		row[name] = value
	else:
		setattr(row, name, value)


def _idx_of(row: Any) -> int:
	try:
		return int(_field(row, "idx") or 0)
	except (TypeError, ValueError):
		return 0


def renumber_steps(rows: Any) -> list[Any]:
	"""Assign ``step_no`` 1..n in ``idx`` order.

	``sorted`` is stable, so rows that share an ``idx`` (or have none) keep the
	order they arrived in rather than being shuffled.  Returns the ordered rows
	so callers can assert on them.
	"""
	ordered = sorted(list(rows or []), key=_idx_of)
	for position, row in enumerate(ordered, start=1):
		_set_field(row, "step_no", position)
	return ordered


def _clean(value: Any) -> str:
	return str(value or "").strip()


def _cell(value: Any) -> Any:
	"""One comparable cell: ``None``/``""`` alike, numbers to 6 places."""
	if value in (None, ""):
		return ""
	if isinstance(value, bool):
		return int(value)
	if isinstance(value, (int, float)):
		return round(float(value), 6)
	return str(value).strip()


def table_signature(rows: Any, fields: tuple[str, ...]) -> list[tuple[Any, ...]]:
	"""The rows of a child table as comparable tuples, in ``idx`` order."""
	ordered = sorted(list(rows or []), key=_idx_of)
	return [tuple(_cell(_field(row, name)) for name in fields) for row in ordered]


class JarzSOP(Document):
	def validate(self):
		self._apply_recipe_book_rules()
		renumber_steps(self.steps)
		self._guard_steps_are_immutable_once_used()

	def _apply_recipe_book_rules(self):
		"""Identity, stage and the ``is_active`` rule (see the module docstring)."""
		stage = _clean(getattr(self, "stage", None)) or STAGE_APPROVED
		if stage not in STAGES:
			frappe.throw(_("Stage must be one of: {0}").format(", ".join(STAGES)))
		self.stage = stage

		item_code = _clean(getattr(self, "item_code", None))
		title = _clean(getattr(self, "recipe_title", None))
		if not title and not item_code:
			frappe.throw(_("Set a Recipe Title or an Item Code."))
		if not title:
			title = _clean(getattr(self, "item_name", None)) or item_code
		self.recipe_title = title

		if stage != STAGE_APPROVED or not item_code:
			# A trial, a withdrawn recipe, or one with no Item never reaches the
			# Production Board.  Forced quietly rather than refused: ``is_active``
			# defaults to 1, so refusing would make every new trial typed in
			# Desk throw until somebody found the box to untick.
			self.is_active = 0
		elif not list(getattr(self, "steps", None) or []):
			# Nothing to run: a layers-only or ingredients-only version must never
			# reach the Production Board.
			self.is_active = 0

	def _table_changed(self, fieldname: str) -> bool:
		"""True when the rows of ``fieldname`` differ from what is stored.

		``has_value_changed`` compares two lists of child Documents, which are
		never equal (no ``__eq__``), so it answers "changed" for every save.  It
		stays the first gate; the row values decide.
		"""
		if not self.has_value_changed(fieldname):
			return False
		getter = getattr(self, "get_doc_before_save", None)
		previous = getter() if callable(getter) else None
		if previous is None:
			return True
		fields = _FROZEN_TABLES[fieldname]
		return table_signature(previous.get(fieldname), fields) != table_signature(
			getattr(self, fieldname, None), fields
		)

	def _guard_steps_are_immutable_once_used(self):
		"""Refuse a step, layer or ingredient edit on a procedure some batch was made to."""
		if not frappe.db.exists(
			"Work Order",
			{WORK_ORDER_STAMP_FIELD: ["like", f"{self.name}{VERSION_STAMP_SEPARATOR}%"]},
		):
			return
		if any(self._table_changed(name) for name in _FROZEN_TABLES):
			frappe.throw(_("This SOP has been used in production. Create a new version instead."))

	def on_update(self):
		self._deactivate_other_versions()

	def _deactivate_other_versions(self):
		"""Keep at most one active SOP per item, and say so out loud.

		Silently flipping another record's flag is the kind of side effect that
		gets discovered three weeks later, so it is announced.
		"""
		if not self.is_active or not _clean(self.item_code):
			return

		others = frappe.get_all(
			SOP_DOCTYPE,
			filters={
				"item_code": self.item_code,
				"is_active": 1,
				"name": ["!=", self.name],
			},
			pluck="name",
		)
		if not others:
			return

		for name in others:
			frappe.db.set_value(SOP_DOCTYPE, name, "is_active", 0, update_modified=False)

		frappe.msgprint(
			_("Deactivated {0} other active SOP(s) for {1}: {2}").format(
				len(others), self.item_code, ", ".join(others)
			)
		)
