"""Accuracy of the AI order read ("Start order" in the chat inbox), managers only.

URL: ``/api/method/jarz_pos.api.chat_order_log.get_order_accuracy``
"""

from __future__ import annotations

from typing import Any

import frappe

from jarz_pos.constants import ROLES


def _gate() -> None:
	if frappe.session.user == ROLES.ADMINISTRATOR:
		return
	frappe.only_for(list(ROLES.MANAGER))


@frappe.whitelist(methods=["GET"])
def get_order_accuracy(days: int = 30) -> dict[str, Any]:
	"""Proposals vs billed invoices over the last *days* days."""
	_gate()
	from jarz_pos.services.chat_order_log import accuracy_summary

	return accuracy_summary(days)
