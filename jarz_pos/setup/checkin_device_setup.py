"""Custom fields for check-in device flagging (idempotent, after_migrate).

Reuses ``roster_setup._ensure_fields`` so the mechanism is identical to the
roster fields: one ``create_custom_fields`` call per field, failures logged and
never raised, safe on a bench without HRMS.
"""

from __future__ import annotations

from typing import Dict, List

from jarz_pos.services.checkin_device import (
    EMPLOYEE_DEVICE_FIELD,
    EMPLOYEE_REGISTERED_ON_FIELD,
    FLAG_NEW,
    FLAG_NONE_AVAILABLE,
    FLAG_SHARED,
)
from jarz_pos.setup.roster_setup import _ensure_fields


def ensure_checkin_device_fields() -> Dict[str, List[str]]:
    log: Dict[str, List[str]] = {"created": [], "existing": []}

    _ensure_fields(
        "Employee Checkin",
        [
            {
                "fieldname": "custom_device_token",
                "label": "Device Token",
                "fieldtype": "Data",
                "insert_after": "device_id",
                "read_only": 1,
                "description": "Full browser token. Paste into the employee's Check-in Device to approve a new phone.",
            },
            {
                "fieldname": "custom_client_ip",
                "label": "Client IP",
                "fieldtype": "Data",
                "insert_after": "custom_device_token",
                "read_only": 1,
            },
            {
                "fieldname": "custom_user_agent",
                "label": "User Agent",
                "fieldtype": "Small Text",
                "insert_after": "custom_client_ip",
                "read_only": 1,
            },
            {
                "fieldname": "custom_device_flag",
                "label": "Device Flag",
                "fieldtype": "Select",
                "options": "\n".join(["", FLAG_NEW, FLAG_SHARED, FLAG_NONE_AVAILABLE]),
                "insert_after": "custom_user_agent",
                "read_only": 1,
                "in_list_view": 1,
                "in_standard_filter": 1,
            },
            {
                "fieldname": "custom_device_flag_detail",
                "label": "Device Flag Detail",
                "fieldtype": "Small Text",
                "insert_after": "custom_device_flag",
                "read_only": 1,
            },
        ],
        log,
    )

    _ensure_fields(
        "Employee",
        [
            {
                "fieldname": EMPLOYEE_DEVICE_FIELD,
                "label": "Check-in Device",
                "fieldtype": "Data",
                "insert_after": "custom_roster_checkin_exempt",
                "description": (
                    "Registered phone token. Clear to re-register on the next check-in; "
                    "paste a token from a flagged check-in to approve that phone."
                ),
            },
            {
                "fieldname": EMPLOYEE_REGISTERED_ON_FIELD,
                "label": "Check-in Device Registered On",
                "fieldtype": "Datetime",
                "insert_after": EMPLOYEE_DEVICE_FIELD,
                "read_only": 1,
            },
        ],
        log,
    )
    return log
