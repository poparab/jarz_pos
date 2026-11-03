"""Any-branch check-in: ``Employee.custom_checkin_any_branch``.

A flagged employee (a manager who visits another branch before their own) is
geofenced against EVERY branch instead of only the rostered one. Still
geofenced: a press away from all branches is refused. Everybody else keeps
HRMS's single-branch rule untouched.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from jarz_pos.events import employee_checkin as checkin_guard
from jarz_pos.services import attendance as attendance_service
from jarz_pos.services import checkin_location

# Production coordinates, 200 m radius each.
NASR_CITY = {"name": "Nasr City", "checkin_radius": 200, "latitude": 30.0584955, "longitude": 31.3616313}
DOKKI = {"name": "Dokki", "checkin_radius": 200, "latitude": 30.0465478, "longitude": 31.2149796}
LOCATIONS = [DOKKI, NASR_CITY]

AT_DOKKI = (30.0466, 31.2150)
AT_NASR_CITY = (30.0585, 31.3616)
AT_HOME = (30.1000, 31.3000)  # several km from every branch


class _Blocked(Exception):
	pass


def _doc(lat_lng, employee="HR-EMP-00001"):
	doc = MagicMock()
	doc.employee = employee
	doc.latitude, doc.longitude = lat_lng if lat_lng else (None, None)
	doc.get = lambda key: getattr(doc, key)
	return doc


class TestEnforceAnyBranch(unittest.TestCase):
	def _run(self, lat_lng, geo_on=True, locations=LOCATIONS):
		with (
			patch.object(checkin_location, "frappe") as mock_frappe,
			patch.object(checkin_location, "measurable_locations", return_value=locations),
		):
			mock_frappe.db.get_single_value.return_value = 1 if geo_on else 0

			def fake_throw(msg, title=None, exc=None):
				raise _Blocked(str(msg))

			mock_frappe.throw.side_effect = fake_throw
			return checkin_location.enforce_any_branch(_doc(lat_lng))

	def test_other_branch_is_accepted(self):
		"""The whole point: standing in Dokki while rostered at Nasr City."""
		self.assertEqual(self._run(AT_DOKKI), "Dokki")

	def test_home_branch_still_accepted(self):
		self.assertEqual(self._run(AT_NASR_CITY), "Nasr City")

	def test_away_from_every_branch_is_refused(self):
		"""Any-branch is not anywhere."""
		with self.assertRaises(_Blocked) as ctx:
			self._run(AT_HOME)
		self.assertIn("200", str(ctx.exception))

	def test_missing_coordinates_refused(self):
		with self.assertRaises(_Blocked):
			self._run(None)

	def test_geolocation_off_is_a_no_op(self):
		self.assertIsNone(self._run(AT_HOME, geo_on=False))

	def test_no_measurable_location_is_a_no_op(self):
		"""Same as HRMS when no radius is configured anywhere."""
		self.assertIsNone(self._run(AT_HOME, locations=[]))


class TestWidenNeverReplaces(unittest.TestCase):
	"""The flag only rescues a press HRMS would refuse for distance."""

	def _run(self, lat_lng, rostered=NASR_CITY, geo_on=True):
		with (
			patch.object(checkin_location, "frappe") as mock_frappe,
			patch.object(checkin_location, "measurable_locations", return_value=LOCATIONS),
		):
			mock_frappe.db.get_single_value.return_value = 1 if geo_on else 0
			mock_frappe.db.get_value.return_value = (
				{k: rostered[k] for k in ("checkin_radius", "latitude", "longitude")} if rostered else None
			)

			def fake_throw(msg, title=None, exc=None):
				raise _Blocked(str(msg))

			mock_frappe.throw.side_effect = fake_throw
			return checkin_location.widen_rostered_check(_doc(lat_lng), rostered["name"] if rostered else None)

	def test_at_rostered_branch_passes(self):
		self.assertEqual(self._run(AT_NASR_CITY), "Nasr City")

	def test_miss_at_rostered_but_at_other_branch_passes(self):
		self.assertEqual(self._run(AT_DOKKI), "Dokki")

	def test_miss_everywhere_refused(self):
		with self.assertRaises(_Blocked):
			self._run(AT_HOME)

	def test_radius_zero_rostered_location_stays_unmeasured(self):
		"""HRMS lets this through anywhere; the flag must not lock it out."""
		self.assertIsNone(self._run(AT_HOME, rostered=dict(NASR_CITY, checkin_radius=0)))

	def test_no_rostered_location_stays_unmeasured(self):
		"""Off-shift / roster-exempt: HRMS measures nothing, nor do we."""
		self.assertIsNone(self._run(AT_HOME, rostered=None))

	def test_missing_coordinates_still_refused(self):
		with self.assertRaises(_Blocked):
			self._run(None)


class TestOffWindowGateUsesAnyBranch(unittest.TestCase):
	"""The roster gate's own geofence (off-window press) follows the flag too."""

	def test_flagged_employee_is_measured_against_every_branch(self):
		doc = _doc(AT_DOKKI)
		with (
			patch.object(checkin_location, "is_any_branch", return_value=True),
			patch.object(checkin_location, "widen_rostered_check") as mock_widen,
			patch.object(checkin_guard, "frappe") as mock_frappe,
		):
			checkin_guard._enforce_location(doc, "Nasr City")
		mock_widen.assert_called_once_with(doc, "Nasr City")
		mock_frappe.db.get_value.assert_not_called()

	def test_no_rostered_location_stays_unmeasured_even_with_flag(self):
		"""HRMS measures nothing here, so the flag must not start measuring."""
		doc = _doc(AT_HOME)
		with (
			patch.object(checkin_location, "is_any_branch", return_value=True),
			patch.object(checkin_location, "widen_rostered_check") as mock_widen,
		):
			checkin_guard._enforce_location(doc, None)
		mock_widen.assert_not_called()

	def test_unflagged_employee_keeps_rostered_branch(self):
		doc = _doc(AT_DOKKI)
		with (
			patch.object(checkin_location, "is_any_branch", return_value=False),
			patch.object(checkin_location, "enforce_any_branch") as mock_any,
			patch.object(checkin_guard, "frappe") as mock_frappe,
		):
			mock_frappe.db.get_single_value.return_value = 1
			mock_frappe.db.get_value.return_value = (200, NASR_CITY["latitude"], NASR_CITY["longitude"])
			mock_frappe.throw.side_effect = lambda *a, **k: (_ for _ in ()).throw(_Blocked())
			with self.assertRaises(_Blocked):
				checkin_guard._enforce_location(doc, "Nasr City")
		mock_any.assert_not_called()


class TestOverrideClass(unittest.TestCase):
	def test_flag_widens_with_hrms_rostered_location(self):
		from jarz_pos.overrides.employee_checkin import EmployeeCheckin

		doc = MagicMock(spec=EmployeeCheckin)
		doc.employee = "HR-EMP-00001"
		doc._rostered_shift_location.return_value = "Nasr City"
		with (
			patch.object(checkin_location, "is_any_branch", return_value=True),
			patch.object(checkin_location, "widen_rostered_check") as mock_widen,
		):
			EmployeeCheckin.validate_distance_from_shift_location(doc)
		mock_widen.assert_called_once_with(doc, "Nasr City")

	def test_no_flag_keeps_hrms_rule(self):
		from hrms.hr.doctype.employee_checkin.employee_checkin import (
			EmployeeCheckin as HRMSEmployeeCheckin,
		)

		from jarz_pos.overrides.employee_checkin import EmployeeCheckin

		doc = EmployeeCheckin.__new__(EmployeeCheckin)
		doc.employee = "HR-EMP-00002"
		with (
			patch.object(checkin_location, "is_any_branch", return_value=False),
			patch.object(checkin_location, "widen_rostered_check") as mock_any,
			patch.object(HRMSEmployeeCheckin, "validate_distance_from_shift_location") as mock_hrms,
		):
			EmployeeCheckin.validate_distance_from_shift_location(doc)
		mock_hrms.assert_called_once()
		mock_any.assert_not_called()


class TestBoardGeoOk(unittest.TestCase):
	"""The attendance board must not accuse the visit the flag allows."""

	def test_other_branch_flagged_out_of_range_without_flag(self):
		self.assertFalse(attendance_service._geo_ok([AT_DOKKI], NASR_CITY))

	def test_other_branch_in_range_with_flag(self):
		self.assertTrue(attendance_service._geo_ok([AT_DOKKI], NASR_CITY, LOCATIONS))

	def test_away_from_every_branch_still_out_of_range_with_flag(self):
		self.assertFalse(attendance_service._geo_ok([AT_HOME], NASR_CITY, LOCATIONS))

	def test_unanswerable_stays_none(self):
		self.assertIsNone(attendance_service._geo_ok([], NASR_CITY, LOCATIONS))
		self.assertIsNone(attendance_service._geo_ok([AT_DOKKI], None))
