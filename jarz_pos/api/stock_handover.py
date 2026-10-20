"""Stock Handover endpoints: one side declares a stock movement, the other confirms it.

``/api/method/jarz_pos.api.stock_handover.<fn>``. Thin wrappers; every rule
(who may act for which side, whose turn it is, item scope, row locks, posting
the Stock Entry on confirm) lives in :mod:`jarz_pos.services.stock_handover`.
"""

from __future__ import annotations

from typing import Any

import frappe

from jarz_pos.services import stock_handover as service


@frappe.whitelist()
def get_context(pos_profile: str | None = None) -> dict[str, Any]:
	"""The sides the user may act for (``my_sides``), every possible counterpart, and ``can_manage``."""
	return service.get_context(pos_profile=pos_profile)


@frappe.whitelist()
def list_items(my_warehouse: str, counterpart_warehouse: str, search: str | None = None) -> dict[str, Any]:
	"""Items that may be handed over between the two sides (no stock numbers)."""
	return service.list_items(my_warehouse, counterpart_warehouse, search=search)


@frappe.whitelist(methods=["POST"])
def declare(
	my_warehouse: str,
	counterpart_warehouse: str,
	direction: str,
	lines: Any,
	pos_profile: str | None = None,
) -> dict[str, Any]:
	"""Declare a movement: ``direction`` "in" = I received from them, "out" = they took from me."""
	return service.declare(my_warehouse, counterpart_warehouse, direction, lines, pos_profile=pos_profile)


@frappe.whitelist()
def list_handovers(
	my_warehouse: str | None = None,
	pos_profile: str | None = None,
	limit: Any = 30,
) -> dict[str, Any]:
	"""``{waiting_for_me, waiting_for_them, recent}`` for the user's sides."""
	return service.list_handovers(my_warehouse=my_warehouse, pos_profile=pos_profile, limit=limit)


@frappe.whitelist(methods=["POST"])
def respond(
	name: str,
	action: str,
	lines: Any = None,
	reason: str | None = None,
	pos_profile: str | None = None,
) -> dict[str, Any]:
	"""Answer a pending handover: ``confirm`` (posts the transfer), ``change`` (new qtys) or ``reject``."""
	return service.respond(name, action, lines=lines, reason=reason, pos_profile=pos_profile)


@frappe.whitelist(methods=["POST"])
def cancel(name: str, pos_profile: str | None = None) -> dict[str, Any]:
	"""Withdraw your own declaration while it is pending and nobody has changed it."""
	return service.cancel(name, pos_profile=pos_profile)
