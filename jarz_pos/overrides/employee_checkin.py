from __future__ import annotations

from hrms.hr.doctype.employee_checkin.employee_checkin import (
	EmployeeCheckin as HRMSEmployeeCheckin,
)

from jarz_pos.services import checkin_location


class EmployeeCheckin(HRMSEmployeeCheckin):
	def validate_distance_from_shift_location(self):
		# An any-branch employee is measured against every branch, not only the
		# one on today's Shift Assignment. Everybody else keeps HRMS's rule.
		if checkin_location.is_any_branch(self.employee):
			checkin_location.enforce_any_branch(self)
			return
		super().validate_distance_from_shift_location()
