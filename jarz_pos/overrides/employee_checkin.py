from __future__ import annotations

import frappe
from hrms.hr.doctype.employee_checkin.employee_checkin import (
	EmployeeCheckin as HRMSEmployeeCheckin,
)

from jarz_pos.services import checkin_location


class EmployeeCheckin(HRMSEmployeeCheckin):
	def validate_distance_from_shift_location(self):
		# An any-branch employee gets HRMS's rule widened, not replaced: only a
		# press too far from the rostered branch is re-measured against every
		# branch. Everybody else keeps HRMS's rule untouched.
		if not checkin_location.is_any_branch(self.employee):
			return super().validate_distance_from_shift_location()
		checkin_location.widen_rostered_check(self, self._rostered_shift_location())

	def _rostered_shift_location(self) -> str | None:
		# The exact lookup HRMS's validate_distance_from_shift_location uses.
		locations = frappe.get_all(
			"Shift Assignment",
			filters={
				"employee": self.employee,
				"shift_type": self.shift,
				"start_date": ["<=", self.time],
				"shift_location": ["is", "set"],
				"docstatus": 1,
				"status": "Active",
			},
			or_filters=[["end_date", ">=", self.time], ["end_date", "is", "not set"]],
			pluck="shift_location",
		)
		return locations[0] if locations else None
