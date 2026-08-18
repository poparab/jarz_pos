"""Jarz POS – Delivery Time Slot API endpoints.

Provides delivery time slot management based on POS Profile Timetable configuration.
"""

from __future__ import annotations
import frappe
import json
import logging
from datetime import datetime, timedelta, time
from typing import List, Dict, Any, Union
from jarz_pos.constants import TIMING_MODES


# ──────────────────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────────────────

#: site_config.json key that raises this module's log level, e.g.
#: ``"jarz_delivery_slots_log_level": "DEBUG"``. Accepts any standard level name.
_LOG_LEVEL_CONF_KEY = "jarz_delivery_slots_log_level"

_LOGGER_NAME = "jarz_pos.api.delivery_slots"


def _get_logger() -> logging.Logger:
    """Return this module's logger with its level set explicitly.

    ``frappe.get_logger`` applies ``frappe.log_level or default_log_level``, and
    ``default_log_level`` is ERROR on anything that is not a dev server (see
    ``frappe/utils/logger.py``). Staging and production set neither
    ``developer_mode`` nor ``log_level``, so a bare ``.info()`` — and ``.warning()``
    — is silently discarded there. Only ERROR and above ever reach
    ``sites/<site>/logs/jarz_pos.api.delivery_slots.log``.

    Defaulting to ERROR here is a deliberate choice, not an accident.
    ``get_available_delivery_slots`` runs on every POS profile load and its
    slot-by-slot trace was ~182 lines of ``print()`` per call on the backend
    container's stdout. That trace is now ``.debug()``: off in normal operation,
    and recoverable without a deploy by setting ``jarz_delivery_slots_log_level``
    to ``"DEBUG"`` in ``site_config.json``. Genuine faults — schema drift, the
    infinite-loop guard, unhandled exceptions — are logged at ERROR and are
    therefore always written.

    Debug calls use lazy ``%s`` formatting so the arguments are not rendered at
    all while the level is ERROR; the trace costs nothing on the hot path.
    """
    try:
        logger = frappe.logger(_LOGGER_NAME, allow_site=frappe.local.site)
    except Exception:
        # No site context (e.g. imported outside a request). A plain stdlib
        # logger keeps error reporting working rather than dropping it.
        logger = logging.getLogger(_LOGGER_NAME)

    logger.setLevel(logging.ERROR)  # floor: guarantees failures are never lost

    try:
        configured = frappe.conf.get(_LOG_LEVEL_CONF_KEY)
    except Exception as exc:
        logger.error("Could not read %s from site config: %s", _LOG_LEVEL_CONF_KEY, exc)
        return logger

    if configured:
        level = logging.getLevelName(str(configured).strip().upper())
        if isinstance(level, int):
            logger.setLevel(level)
        else:
            logger.error(
                "Ignoring invalid %s=%r in site config; keeping ERROR.",
                _LOG_LEVEL_CONF_KEY,
                configured,
            )

    return logger


@frappe.whitelist()
def get_available_delivery_slots(pos_profile_name: str) -> List[Dict[str, Any]]:
    """
    Get available delivery time slots for the next 5 days based on POS Profile Timetable
    
    Args:
        pos_profile_name (str): POS Profile name to get timetable for
    
    Returns:
        List[Dict]: Available time slots with date, time, label, and datetime
    """
    
    logger = _get_logger()
    logger.debug(
        "get_available_delivery_slots called | user=%s | site=%s | pos_profile=%s",
        frappe.session.user,
        getattr(frappe.local, "site", None),
        pos_profile_name,
    )

    try:
        # Check if POS Profile exists and is enabled
        from jarz_pos.utils.validation_utils import assert_pos_profile_enabled
        assert_pos_profile_enabled(pos_profile_name)
        logger.debug("POS Profile %r is active", pos_profile_name)

        # Get POS Profile Timetable configuration
        timetable_config = frappe.get_value(
            "POS Profile Timetable",
            {"pos_profile": pos_profile_name},
            ["name", "slot_hours", "slot_minutes", "has_custom_last_slot", "last_slot_hours", "last_slot_minutes"],
            as_dict=True
        )

        logger.debug("Timetable config for %r: %s", pos_profile_name, timetable_config)

        if not timetable_config:
            error_msg = f"No timetable configured for POS Profile '{pos_profile_name}'"
            # Listing every timetable only earns its query cost on the failure
            # path - it used to run on every call just to feed a print().
            logger.error(
                "%s | timetables that do exist: %s",
                error_msg,
                frappe.get_all(
                    "POS Profile Timetable",
                    fields=["name", "pos_profile", "slot_hours", "slot_minutes"],
                ),
            )
            frappe.throw(error_msg)

        slot_duration_minutes = int(timetable_config.slot_hours or 1) * 60 + int(timetable_config.slot_minutes or 0)
        last_slot_duration_minutes = None
        if timetable_config.has_custom_last_slot:
            last_slot_duration_minutes = (
                int(timetable_config.last_slot_hours or 1) * 60
                + int(timetable_config.last_slot_minutes or 0)
            )
        timetable_name = timetable_config.name

        logger.debug(
            "Timetable %s | slot duration %s min | custom last slot %s min",
            timetable_name,
            slot_duration_minutes,
            last_slot_duration_minutes,
        )

        # Get day timings from the child table
        try:
            # Try to get fields including same_day
            day_timings = frappe.get_all(
                "POS Profile Day Timing",
                filters={"parent": timetable_name},
                fields=["day", "opening_time", "closing_time", "same_day"],
                order_by="idx"
            )
        except Exception as e:
            # Schema drift: without same_day every "Next Day" window silently
            # collapses to same-day and the late slots disappear. Log at ERROR
            # so this stays visible on staging/production, then degrade.
            logger.error(
                "same_day unavailable on POS Profile Day Timing, falling back to basic fields "
                "(Next Day windows will be treated as Same Day): %s",
                e,
            )
            # Fallback to basic fields if same_day doesn't exist
            day_timings = frappe.get_all(
                "POS Profile Day Timing",
                filters={"parent": timetable_name},
                fields=["day", "opening_time", "closing_time"],
                order_by="idx"
            )
            # Add default same_day value
            for timing in day_timings:
                timing['same_day'] = TIMING_MODES.SAME_DAY  # Default value

        logger.debug("Day timings for %s: %s record(s) %s", timetable_name, len(day_timings), day_timings)

        if not day_timings:
            error_msg = f"No day timings configured for POS Profile '{pos_profile_name}'"
            try:
                all_day_timings = frappe.get_all(
                    "POS Profile Day Timing",
                    fields=["name", "parent", "day", "opening_time", "closing_time", "same_day"],
                )
            except Exception as e:
                logger.error(
                    "Could not list POS Profile Day Timing with same_day (%s); retrying without it", e
                )
                all_day_timings = frappe.get_all(
                    "POS Profile Day Timing",
                    fields=["name", "parent", "day", "opening_time", "closing_time"],
                )
            logger.error(
                "%s | timetable=%s | day timings that do exist: %s",
                error_msg,
                timetable_name,
                all_day_timings,
            )
            frappe.throw(error_msg)

        # Create day mapping for quick lookup
        day_config = {}
        for timing in day_timings:
            # Handle same_day field with fallback
            same_day = timing.get('same_day', TIMING_MODES.SAME_DAY)  # Default to 'Same Day' if not present
            day_config[timing.day] = {
                'opening_time': timing.opening_time,
                'closing_time': timing.closing_time,
                'same_day': same_day
            }

        logger.debug(
            "Found %s day configuration(s) with %s minute slots: %s",
            len(day_timings),
            slot_duration_minutes,
            day_config,
        )

        # Generate slots for next 5 days
        slots = []
        current_datetime = frappe.utils.now_datetime()
        logger.debug("Current datetime: %s", current_datetime)

        # Create a more comprehensive day mapping that handles different day name formats
        day_name_variations = {
            'Monday': ['Monday', 'Mon', 'monday', 'MONDAY'],
            'Tuesday': ['Tuesday', 'Tue', 'tuesday', 'TUESDAY'],
            'Wednesday': ['Wednesday', 'Wed', 'wednesday', 'WEDNESDAY'],
            'Thursday': ['Thursday', 'Thu', 'thursday', 'THURSDAY'],
            'Friday': ['Friday', 'Fri', 'friday', 'FRIDAY'],
            'Saturday': ['Saturday', 'Sat', 'saturday', 'SATURDAY'],
            'Sunday': ['Sunday', 'Sun', 'sunday', 'SUNDAY']
        }

        for day_offset in range(5):  # Next 5 days
            target_date = current_datetime.date() + timedelta(days=day_offset)
            day_name = target_date.strftime('%A')  # Monday, Tuesday, etc.

            # Find the matching day configuration - use direct matching first
            matching_day_config = None

            # First try direct match
            if day_name in day_config:
                matching_day_config = day_config[day_name]
            else:
                # Then try variations
                for db_day_name, day_info in day_config.items():
                    # Check if the database day name matches any variation of the current day
                    for standard_day, variations in day_name_variations.items():
                        if standard_day == day_name and db_day_name in variations:
                            matching_day_config = day_info
                            logger.debug("Matched %s to stored day name %r", day_name, db_day_name)
                            break
                    if matching_day_config:
                        break

            # Check if this day is configured
            if not matching_day_config:
                logger.debug(
                    "No configuration for %s (%s); configured days: %s",
                    day_name,
                    target_date,
                    list(day_config.keys()),
                )
                continue

            opening_time = matching_day_config['opening_time']
            closing_time = matching_day_config['closing_time']
            same_day = matching_day_config['same_day']

            logger.debug(
                "Day %s of 5: %s (%s) hours %s - %s (same_day: %s)",
                day_offset + 1,
                day_name,
                target_date,
                opening_time,
                closing_time,
                same_day,
            )

            # Generate time slots for this day
            day_slots = _generate_day_slots(
                target_date,
                opening_time,
                closing_time,
                same_day,
                slot_duration_minutes,
                current_datetime if day_offset == 0 else None,  # Only check current time for today
                last_slot_duration_minutes
            )

            logger.debug("Generated %s slot(s) for %s", len(day_slots), day_name)
            slots.extend(day_slots)

        # Sort slots by datetime
        slots.sort(key=lambda x: x['datetime'])

        # Mark the next available slot as default
        if slots:
            slots[0]['is_default'] = True

        logger.debug(
            "Generated %s total delivery slot(s) for %r; default=%s",
            len(slots),
            pos_profile_name,
            slots[0]['label'] if slots else None,
        )

        return slots

    except Exception as e:
        error_msg = f"Error generating delivery slots: {str(e)}"
        logger.error("%s\n%s", error_msg, frappe.get_traceback())
        frappe.throw(f"Error loading delivery slots: {str(e)}")


def _generate_day_slots(
    target_date: datetime.date,
    opening_time: Union[time, timedelta],
    closing_time: Union[time, timedelta],
    same_day: str,
    slot_duration_minutes: int,
    current_datetime: datetime = None,
    last_slot_duration_minutes: int = None
) -> List[Dict[str, Any]]:
    """
    Generate time slots for a specific day.

    Args:
        target_date: Date to generate slots for
        opening_time: Store opening time (can be time or timedelta)
        closing_time: Store closing time (can be time or timedelta)
        same_day: "Same Day" or "Next Day" - indicates if closing time is same day or next day
        slot_duration_minutes: Duration of each regular slot in minutes
        current_datetime: Current datetime (only provided for today)
        last_slot_duration_minutes: If set, the final slot of the day uses this shorter duration
            instead of the regular one, allowing an extra slot to fill remaining time before closing.

    Returns:
        List of time slots for the day
    """
    slots = []
    logger = _get_logger()

    # Convert timedelta to time if needed (Frappe Time fields return timedelta)
    if isinstance(opening_time, timedelta):
        opening_time = (datetime.min + opening_time).time()
    if isinstance(closing_time, timedelta):
        closing_time = (datetime.min + closing_time).time()

    logger.debug(
        "Slot inputs for %s | opening=%s (%s) closing=%s (%s) same_day=%s",
        target_date,
        opening_time,
        type(opening_time).__name__,
        closing_time,
        type(closing_time).__name__,
        same_day,
    )

    # Convert times to datetime objects for easier calculation
    current_slot_time = datetime.combine(target_date, opening_time)

    # Handle same_day vs next_day closing times
    if same_day == TIMING_MODES.NEXT_DAY:
        end_time = datetime.combine(target_date + timedelta(days=1), closing_time)
    else:
        end_time = datetime.combine(target_date, closing_time)

    logger.debug("Slot generation window: %s to %s", current_slot_time, end_time)

    # Validate that end_time is after start_time
    if end_time <= current_slot_time:
        logger.debug(
            "Invalid time window for %s: end_time (%s) <= start_time (%s)",
            target_date,
            end_time,
            current_slot_time,
        )
        return slots

    # If this is today, ensure we only show future slots
    if current_datetime:
        # Add buffer of 30 minutes for preparation
        min_slot_time = current_datetime + timedelta(minutes=30)
        logger.debug("Minimum slot time (current + 30min buffer): %s", min_slot_time)

        if current_slot_time < min_slot_time:
            # Round up to next slot boundary
            minutes_since_opening = (min_slot_time - current_slot_time).total_seconds() / 60
            slots_to_skip = int(minutes_since_opening / slot_duration_minutes) + 1
            current_slot_time += timedelta(minutes=slot_duration_minutes * slots_to_skip)
            logger.debug(
                "Adjusted start time for today: %s (skipped %s slots)",
                current_slot_time,
                slots_to_skip,
            )

            # Check if adjusted time is still valid
            if current_slot_time >= end_time:
                logger.debug("No valid slots left today after time adjustment")
                return slots

    slot_count = 0
    max_iterations = 50  # Safety limit to prevent infinite loops
    iteration_count = 0

    while current_slot_time < end_time and iteration_count < max_iterations:
        iteration_count += 1
        slot_end_time = current_slot_time + timedelta(minutes=slot_duration_minutes)

        logger.debug(
            "Iteration %s: checking slot %s - %s", iteration_count, current_slot_time, slot_end_time
        )

        # Regular slot overflows closing time — try custom last slot before stopping
        if slot_end_time > end_time:
            logger.debug(
                "Regular slot would extend beyond closing time: %s > %s", slot_end_time, end_time
            )
            if last_slot_duration_minutes:
                last_slot_end = current_slot_time + timedelta(minutes=last_slot_duration_minutes)
                if last_slot_end <= end_time:
                    logger.debug(
                        "Adding custom last slot: %s - %s", current_slot_time, last_slot_end
                    )
                    slot_end_time = last_slot_end
                    # Fall through to append, then exit loop
                else:
                    logger.debug(
                        "Custom last slot also overflows: %s > %s", last_slot_end, end_time
                    )
                    break
            else:
                break

        # Format slot label
        day_label = _get_day_label(target_date)
        time_label = f"{current_slot_time.strftime('%I:%M %p')} - {slot_end_time.strftime('%I:%M %p')}"

        slot_data = {
            'date': target_date.isoformat(),
            'time': current_slot_time.time().isoformat(),
            'datetime': current_slot_time.isoformat(),
            'end_datetime': slot_end_time.isoformat(),
            'label': f"{day_label}, {time_label}",
            'day_label': day_label,
            'time_label': time_label,
            'is_default': False
        }

        slots.append(slot_data)
        slot_count += 1
        logger.debug("Generated slot %s: %s", slot_count, time_label)

        # If we appended a custom last slot, the loop must end now
        if slot_end_time != current_slot_time + timedelta(minutes=slot_duration_minutes):
            break

        current_slot_time += timedelta(minutes=slot_duration_minutes)

    if iteration_count >= max_iterations:
        # Not cosmetic: the returned list is truncated, so the POS silently
        # offers fewer slots than the timetable configures. ERROR so it is
        # actually visible on staging/production.
        logger.error(
            "Stopped slot generation for %s after %s iterations (infinite-loop guard); "
            "a %s minute slot over this window needs more slots than the guard allows",
            target_date,
            max_iterations,
            slot_duration_minutes,
        )

    logger.debug("Total slots generated for %s: %s", target_date, len(slots))
    return slots


def _get_day_label(target_date: datetime.date) -> str:
    """
    Get human-readable day label (Today, Tomorrow, Monday, etc.)
    """
    today = datetime.now().date()
    
    if target_date == today:
        return "Today"
    elif target_date == today + timedelta(days=1):
        return "Tomorrow"
    else:
        return target_date.strftime('%A')  # Monday, Tuesday, etc.


@frappe.whitelist()
def get_next_available_slot(pos_profile_name: str) -> Dict[str, Any] | None:
    """
    Get the next available delivery slot for a POS profile
    
    Args:
        pos_profile_name (str): POS Profile name
    
    Returns:
        Dict: Next available slot or None
    """
    slots = get_available_delivery_slots(pos_profile_name)
    
    if slots:
        # Return the first slot (which is the next available)
        return slots[0]
    
    return None


# ──────────────────────────────────────────────────────────────────────────
# Timetable preview (Desk form) — see POS Profile Timetable client script.
# ──────────────────────────────────────────────────────────────────────────

WEEKDAYS = [
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
]

# Reference week used only to render labels; any Monday works because the
# generator is date-agnostic once "today" filtering is switched off.
_PREVIEW_WEEK_START = datetime(2024, 1, 1).date()  # a Monday


def _coerce_time(value: Union[str, time, timedelta, None]) -> time | None:
    """Accept whatever a Time field hands us and return a ``datetime.time``.

    Frappe Time fields come back as ``timedelta`` from the DB, but the Desk
    form posts them as ``"HH:MM:SS"`` (sometimes ``"HH:MM"``) strings.
    """
    if value is None or value == "":
        return None
    if isinstance(value, time):
        return value
    if isinstance(value, timedelta):
        return (datetime.min + value).time()
    text = str(value).strip()
    for fmt in ("%H:%M:%S.%f", "%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    return None


@frappe.whitelist()
def preview_timetable_slots(config: Union[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Render the weekly slot plan for an (possibly unsaved) timetable config.

    Drives the live preview on the POS Profile Timetable form so the operator
    can see what a slot-length or opening-hours change produces *before*
    saving. Deliberately calls the same ``_generate_day_slots`` the real API
    uses, so the preview cannot drift from what the POS actually offers.

    Args:
        config: dict (or JSON string) with ``slot_hours``, ``slot_minutes``,
            ``has_custom_last_slot``, ``last_slot_hours``, ``last_slot_minutes``
            and ``timetable``: a list of ``{day, opening_time, closing_time,
            same_day}`` rows.

    Returns:
        Dict with ``slot_duration_minutes``, ``last_slot_duration_minutes``,
        ``total_slots`` and ``days``: one entry per weekday holding the
        generated slots plus any uncovered tail before closing.
    """
    if isinstance(config, str):
        config = json.loads(config or "{}")
    config = config or {}

    slot_duration_minutes = (
        int(config.get("slot_hours") or 0) * 60 + int(config.get("slot_minutes") or 0)
    )
    if slot_duration_minutes <= 0:
        frappe.throw("Slot length must be greater than zero.")

    last_slot_duration_minutes = None
    if int(config.get("has_custom_last_slot") or 0):
        last_slot_duration_minutes = (
            int(config.get("last_slot_hours") or 0) * 60
            + int(config.get("last_slot_minutes") or 0)
        )
        if last_slot_duration_minutes <= 0:
            last_slot_duration_minutes = None

    rows_by_day: Dict[str, Dict[str, Any]] = {}
    for row in config.get("timetable") or []:
        day = (row or {}).get("day")
        if day in WEEKDAYS:
            rows_by_day[day] = row

    days = []
    total_slots = 0

    for offset, day in enumerate(WEEKDAYS):
        row = rows_by_day.get(day)
        entry: Dict[str, Any] = {
            "day": day,
            "configured": bool(row),
            "opening_time": None,
            "closing_time": None,
            "same_day": None,
            "slots": [],
            "slot_count": 0,
            "uncovered_minutes": 0,
            "note": None,
        }

        if not row:
            entry["note"] = "Not configured - no delivery slots on this day."
            days.append(entry)
            continue

        opening_time = _coerce_time(row.get("opening_time"))
        closing_time = _coerce_time(row.get("closing_time"))
        same_day = row.get("same_day") or TIMING_MODES.SAME_DAY

        entry["opening_time"] = opening_time.isoformat() if opening_time else None
        entry["closing_time"] = closing_time.isoformat() if closing_time else None
        entry["same_day"] = same_day

        if not opening_time or not closing_time:
            entry["note"] = "Opening or closing time missing."
            days.append(entry)
            continue

        target_date = _PREVIEW_WEEK_START + timedelta(days=offset)
        start_dt = datetime.combine(target_date, opening_time)
        if same_day == TIMING_MODES.NEXT_DAY:
            end_dt = datetime.combine(target_date + timedelta(days=1), closing_time)
        else:
            end_dt = datetime.combine(target_date, closing_time)

        if end_dt <= start_dt:
            entry["note"] = (
                "Closing time is not after opening time - set 'Next Day' if the "
                "branch closes after midnight."
            )
            days.append(entry)
            continue

        # current_datetime=None → full day, not filtered against "now".
        generated = _generate_day_slots(
            target_date,
            opening_time,
            closing_time,
            same_day,
            slot_duration_minutes,
            None,
            last_slot_duration_minutes,
        )

        entry["slots"] = [
            {
                "time_label": slot["time_label"],
                "start": slot["datetime"][11:16],
                "end": slot["end_datetime"][11:16],
            }
            for slot in generated
        ]
        entry["slot_count"] = len(generated)
        entry["open_minutes"] = int((end_dt - start_dt).total_seconds() // 60)

        if generated:
            last_end = datetime.fromisoformat(generated[-1]["end_datetime"])
            entry["uncovered_minutes"] = int((end_dt - last_end).total_seconds() // 60)
        else:
            entry["uncovered_minutes"] = entry["open_minutes"]
            entry["note"] = "Open window is shorter than one slot - no slots."

        if entry["uncovered_minutes"] > 0 and entry["slot_count"]:
            entry["note"] = (
                f"{entry['uncovered_minutes']} min before closing is not covered "
                "by any slot."
            )

        total_slots += entry["slot_count"]
        days.append(entry)

    return {
        "slot_duration_minutes": slot_duration_minutes,
        "last_slot_duration_minutes": last_slot_duration_minutes,
        "total_slots": total_slots,
        "days": days,
    }
