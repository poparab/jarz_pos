"""Jarz Customer Label -- one stock of one printed label: customer, flavour, branch.

Every flavour has its own artwork and every size its own sheet layout, and a
customer filled by two branches keeps a separate stock at each, so the tracked
unit is (customer, flavour item, storage location) -- the Item already encodes
the size through its Item Group (Medium / Large). The document holds only *policy*
(where the labels live, the safety floor, the batch size in sheets); every
quantity and cost on it is a cached read of ``Jarz Label Movement``, recomputed
by ``services.label_stock.refresh_label`` -- never a counter this controller
increments, which is how such counters drift.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint

# Kept in step with the ``naming_series`` field's options in the DocType JSON.
DEFAULT_NAMING_SERIES = "JLBL-.#####"


class JarzCustomerLabel(Document):
	def before_insert(self):
		# The form fills naming_series client-side, but API inserts and Data
		# Import do not -- leaving those paths to fail autoname with "Naming
		# Series mandatory". Fill it here so every path names alike.
		if not self.naming_series:
			self.naming_series = DEFAULT_NAMING_SERIES

	def validate(self):
		# JARZ prints every customer label (owner, 2026-10-22); customers never
		# bring their own. The column stays so older app builds still parse it,
		# but nothing can set it to 0 any more.
		self.we_print = 1
		self._require_flavour_when_tracked()
		self._default_title_from_item()

		if float(self.labels_per_unit or 0) <= 0:
			self.labels_per_unit = 1

		for field in ("min_stock_qty", "labels_per_sheet", "default_print_sheets"):
			if int(self.get(field) or 0) < 0:
				frappe.throw(_("{0} cannot be negative.").format(_(self.meta.get_label(field))))

		self._validate_unique_flavour()

	def _require_flavour_when_tracked(self):
		"""A label without a flavour cannot be matched to any invoice line.

		Every label is printed by JARZ, so every enabled label needs its item. A
		disabled (retired) record is left alone so old item-less rows can still
		be opened and kept disabled.
		"""
		if cint(self.enabled) and not (self.item or "").strip():
			frappe.throw(
				_(
					"Pick the flavour (Item) this label is printed for. "
					"Every flavour has its own label design, so one record "
					"tracks exactly one flavour."
				)
			)

	def _default_title_from_item(self):
		title = (self.label_title or "").strip()
		if not title:
			if self.item:
				title = frappe.db.get_value("Item", self.item, "item_name") or self.item
			else:
				title = "Default"
		self.label_title = title

	def _validate_unique_flavour(self):
		"""One label record per (customer, flavour, storage location).

		A customer filled by two branches keeps a separate physical stock of the
		same label at each, so the ledger key includes the location. Two records
		for the same flavour at the SAME location would leave consumption free to
		pick either, splitting one stock's history across two ledgers. A blank
		location is its own key (the legacy "any branch" label).
		"""
		if not self.customer or not (self.item or "").strip():
			return
		location = (self.storage_location or "").strip()
		self.storage_location = location or None
		filters = {
			"customer": self.customer,
			"item": self.item,
			"name": ["!=", self.name or ""],
		}
		# "is not set" matches both NULL and "" -- a blank location is one key.
		filters["storage_location"] = location if location else ["is", "not set"]
		clash = frappe.db.get_value(self.doctype, filters, "name")
		if clash:
			where = frappe.bold(location) if location else _("no storage location")
			frappe.throw(
				_("{0} already tracks a label for {1} at {2} ({3}).").format(
					frappe.bold(self.customer), frappe.bold(self.item), where, clash
				)
			)

	def on_update(self):
		# Retiring the design changes the status even though no label moved, so
		# recompute rather than leave a stale "Reorder Now" on a dead label.
		from jarz_pos.services import label_stock

		label_stock.refresh_label(self.name)

	def on_trash(self):
		"""Refuse to delete a label that has history -- disable it instead."""
		if frappe.db.exists("Jarz Label Movement", {"label": self.name}):
			frappe.throw(
				_(
					"This label has stock movements and cannot be deleted. "
					"Untick Enabled to retire it instead."
				)
			)
