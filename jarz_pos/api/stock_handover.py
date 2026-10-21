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
def list_items(
	my_warehouse: str,
	counterpart_warehouse: str | None = None,
	search: str | None = None,
	direction: str | None = None,
) -> dict[str, Any]:
	"""Items a side may hand over (no stock numbers).

	No counterpart = the "sent out" set, or with ``direction="in"`` the set an
	unsourced receipt may name (anything any known side may hand to ``my_warehouse``).
	"""
	return service.list_items(my_warehouse, counterpart_warehouse, search=search, direction=direction)


@frappe.whitelist(methods=["POST"])
def declare(
	my_warehouse: str,
	counterpart_warehouse: str | None = None,
	direction: str = "",
	lines: Any = None,
	pos_profile: str | None = None,
	client_request_id: str | None = None,
) -> dict[str, Any]:
	"""Declare a movement.

	``direction`` "in" = I received from ``counterpart_warehouse``; blank = an unsourced
	receipt a manager matches on the transit board. "out" = I sent stock out, no
	destination: it moves to the company's transit warehouse now (``transit_entry``).
	``client_request_id`` makes a repeated call (a double tap) return the first row.
	"""
	return service.declare(
		my_warehouse,
		counterpart_warehouse,
		direction,
		lines,
		pos_profile=pos_profile,
		client_request_id=client_request_id,
	)


@frappe.whitelist()
def list_handovers(
	my_warehouse: str | None = None,
	pos_profile: str | None = None,
	limit: Any = 30,
) -> dict[str, Any]:
	"""``{waiting_for_me, waiting_for_them, recent, to_match}`` (``to_match``: managers only)."""
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


@frappe.whitelist(methods=["POST"])
def confirm_match(
	source_warehouse: str,
	members: Any,
	lines: Any,
	note: str | None = None,
	pos_profile: str | None = None,
) -> dict[str, Any]:
	"""Manager: approve a sender's whole batch as ONE transfer split across its receivers."""
	return service.confirm_match(source_warehouse, members, lines, note=note, pos_profile=pos_profile)


@frappe.whitelist()
def get_transit_board(pos_profile: str | None = None) -> dict[str, Any]:
	"""Manager: every open send in transit and every receipt to match, with suggestions."""
	return service.get_transit_board(pos_profile=pos_profile)


@frappe.whitelist(methods=["POST"])
def approve_receipt(
	receipt: str,
	allocations: Any,
	received: Any = None,
	note: str | None = None,
	version: str | None = None,
	pos_profile: str | None = None,
) -> dict[str, Any]:
	"""Manager: approve a receipt as ONE Stock Entry from the chosen sends / senders."""
	return service.approve_receipt(
		receipt, allocations, received=received, note=note, version=version, pos_profile=pos_profile
	)


@frappe.whitelist(methods=["POST"])
def reject_receipt(receipt: str, reason: str | None = None, pos_profile: str | None = None) -> dict[str, Any]:
	"""Manager: reject a receipt on the transit board. Nothing posts."""
	return service.reject_receipt(receipt, reason=reason, pos_profile=pos_profile)


@frappe.whitelist(methods=["POST"])
def write_off_transit(
	dispatch: str, lines: Any = None, reason: str | None = None, pos_profile: str | None = None
) -> dict[str, Any]:
	"""Manager: write off part of a send as lost in transit (reason required)."""
	return service.write_off_transit(dispatch, lines, reason=reason, pos_profile=pos_profile)


@frappe.whitelist(methods=["POST"])
def return_to_sender(
	dispatch: str, lines: Any = None, reason: str | None = None, pos_profile: str | None = None
) -> dict[str, Any]:
	"""Manager: move part of a send back from transit to its sender."""
	return service.return_to_sender(dispatch, lines, reason=reason, pos_profile=pos_profile)
