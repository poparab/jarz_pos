"""Jarz POS – Delivery Time Slot API endpoints.

Provides delivery time slot management based on POS Profile Timetable configuration.
"""

from __future__ import annotations
import contextlib
import frappe
import io
import json
from datetime import datetime, timedelta, time
from typing import List, Dict, Any, Union
from jarz_pos.constants import TIMING_MODES


@frappe.whitelist()
def get_available_delivery_slots(pos_profile_name: str) -> List[Dict[str, Any]]:
    """
    Get available delivery time slots for the next 5 days based on POS Profile Timetable
    
    Args:
        pos_profile_name (str): POS Profile name to get timetable for
    
    Returns:
        List[Dict]: Available time slots with date, time, label, and datetime
    """
    
    # Comprehensive debugging for development
    print("\n" + "="*80)
    print("🚀 DELIVERY SLOTS API CALL STARTED")
    print("="*80)
    print(f"📍 TIMESTAMP: {frappe.utils.now()}")
    print(f"👤 USER: {frappe.session.user}")
    print(f"🏢 POS PROFILE: {pos_profile_name}")
    print(f"🌐 SITE: {frappe.local.site}")
    print(f"🔗 METHOD: get_available_delivery_slots")
    
    # Frappe best practice: Use frappe.logger() for structured logging
    logger = frappe.logger("jarz_pos.api.delivery_slots", allow_site=frappe.local.site)
    
    frappe.log_error(
        title="Delivery Slots API Call Debug",
        message=f"""
API ENDPOINT: get_available_delivery_slots
TIMESTAMP: {frappe.utils.now()}
USER: {frappe.session.user}
POS_PROFILE: {pos_profile_name}
""",
        reference_doctype="POS Profile",
        reference_name=pos_profile_name
    )
    
    try:
        # Check if POS Profile exists and is enabled
        print(f"\n🔍 STEP 1: Checking if POS Profile exists and is enabled...")
        from jarz_pos.utils.validation_utils import assert_pos_profile_enabled
        assert_pos_profile_enabled(pos_profile_name)
        print(f"📊 POS Profile '{pos_profile_name}' is active")
        
        # Get POS Profile Timetable configuration
        print(f"\n🔍 STEP 2: Looking for POS Profile Timetable...")
        print(f"🔎 Searching for timetable with pos_profile = '{pos_profile_name}'")
        
        # First, let's check what POS Profile Timetables exist
        all_timetables = frappe.get_all("POS Profile Timetable", fields=["name", "pos_profile", "slot_hours", "slot_minutes"])
        print(f"📋 All available timetables: {all_timetables}")

        timetable_config = frappe.get_value(
            "POS Profile Timetable",
            {"pos_profile": pos_profile_name},
            ["name", "slot_hours", "slot_minutes", "has_custom_last_slot", "last_slot_hours", "last_slot_minutes"],
            as_dict=True
        )

        print(f"📊 Timetable config found: {timetable_config}")

        if not timetable_config:
            error_msg = f"No timetable configured for POS Profile '{pos_profile_name}'"
            print(f"❌ ERROR: {error_msg}")
            logger.error(f"❌ {error_msg}")
            frappe.throw(error_msg)

        slot_duration_minutes = int(timetable_config.slot_hours or 1) * 60 + int(timetable_config.slot_minutes or 0)
        last_slot_duration_minutes = None
        if timetable_config.has_custom_last_slot:
            last_slot_duration_minutes = (
                int(timetable_config.last_slot_hours or 1) * 60
                + int(timetable_config.last_slot_minutes or 0)
            )
        timetable_name = timetable_config.name

        print(f"\n🔍 STEP 3: Processing timetable configuration...")
        print(f"📊 Timetable name: {timetable_name}")
        print(f"⏰ Slot duration: {slot_duration_minutes} minutes, last slot: {last_slot_duration_minutes} minutes")
        
        # Get day timings from the child table
        print(f"\n🔍 STEP 4: Getting day timings from child table...")
        print(f"🔎 Searching POS Profile Day Timing with parent = '{timetable_name}'")
        
        # First, check what fields are available in the DocType
        try:
            # Try to get fields including same_day
            day_timings = frappe.get_all(
                "POS Profile Day Timing",
                filters={"parent": timetable_name},
                fields=["day", "opening_time", "closing_time", "same_day"],
                order_by="idx"
            )
            print(f"✅ Successfully queried with same_day field")
        except Exception as e:
            print(f"⚠️  same_day field not available, falling back to basic fields: {str(e)}")
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
        
        print(f"📊 Day timings found: {len(day_timings)} records")
        print(f"📋 Day timings details: {day_timings}")
        
        if not day_timings:
            print(f"🔍 No day timings found, checking all day timings...")
            try:
                all_day_timings = frappe.get_all("POS Profile Day Timing", fields=["name", "parent", "day", "opening_time", "closing_time", "same_day"])
            except:
                all_day_timings = frappe.get_all("POS Profile Day Timing", fields=["name", "parent", "day", "opening_time", "closing_time"])
            print(f"📋 All available day timings: {all_day_timings}")
            
            error_msg = f"No day timings configured for POS Profile '{pos_profile_name}'"
            print(f"❌ ERROR: {error_msg}")
            logger.error(f"❌ {error_msg}")
            frappe.throw(error_msg)
        
        # Create day mapping for quick lookup
        print(f"\n🔍 STEP 5: Creating day configuration mapping...")
        day_config = {}
        for timing in day_timings:
            # Handle same_day field with fallback
            same_day = timing.get('same_day', TIMING_MODES.SAME_DAY)  # Default to 'Same Day' if not present
            day_config[timing.day] = {
                'opening_time': timing.opening_time,
                'closing_time': timing.closing_time,
                'same_day': same_day
            }
            print(f"📅 Day {timing.day}: {timing.opening_time} - {timing.closing_time} (same_day: {same_day})")
        
        logger.info(f"✅ Found {len(day_timings)} day configurations with {slot_duration_minutes} minute slots")
        print(f"\n✅ Found {len(day_timings)} day configurations with {slot_duration_minutes} minute slots")
        print(f"📅 Day configurations: {day_config}")
        
        # Generate slots for next 5 days
        print(f"\n🔍 STEP 6: Generating slots for next 5 days...")
        slots = []
        current_datetime = frappe.utils.now_datetime()
        print(f"⏰ Current datetime: {current_datetime}")
        
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
            
            print(f"\n📅 Processing day {day_offset + 1}: {day_name} ({target_date})")
            
            # Find the matching day configuration - use direct matching first
            matching_day_config = None
            
            # First try direct match
            if day_name in day_config:
                matching_day_config = day_config[day_name]
                print(f"✅ Direct match found for {day_name}")
            else:
                # Then try variations
                for db_day_name, day_info in day_config.items():
                    # Check if the database day name matches any variation of the current day
                    for standard_day, variations in day_name_variations.items():
                        if standard_day == day_name and db_day_name in variations:
                            matching_day_config = day_info
                            print(f"✅ Found matching config for {day_name}: database has '{db_day_name}'")
                            break
                    if matching_day_config:
                        break
            
            # Check if this day is configured
            if not matching_day_config:
                print(f"⚠️  No configuration for {day_name} ({target_date})")
                print(f"📋 Available day configs: {list(day_config.keys())}")
                logger.info(f"📅 No configuration for {day_name} ({target_date})")
                continue
            
            opening_time = matching_day_config['opening_time']
            closing_time = matching_day_config['closing_time']
            same_day = matching_day_config['same_day']
            
            print(f"⏰ {day_name} hours: {opening_time} - {closing_time} (same_day: {same_day})")
            
            # Generate time slots for this day
            print(f"🔍 Generating slots for {day_name}...")
            day_slots = _generate_day_slots(
                target_date,
                opening_time,
                closing_time,
                same_day,
                slot_duration_minutes,
                current_datetime if day_offset == 0 else None,  # Only check current time for today
                last_slot_duration_minutes
            )
            
            print(f"📊 Generated {len(day_slots)} slots for {day_name}")
            slots.extend(day_slots)
        
        # Sort slots by datetime
        print(f"\n🔍 STEP 7: Finalizing slots...")
        print(f"📊 Total slots before sorting: {len(slots)}")
        slots.sort(key=lambda x: x['datetime'])
        
        # Mark the next available slot as default
        if slots:
            slots[0]['is_default'] = True
            print(f"🎯 Default slot set: {slots[0]['label']}")
        
        logger.info(f"✅ Generated {len(slots)} total delivery slots")
        print(f"\n✅ Generated {len(slots)} total delivery slots")
        print(f"📋 Sample slots: {slots[:3] if slots else 'None'}")
        
        print("\n" + "="*80)
        print("🎉 DELIVERY SLOTS API CALL COMPLETED SUCCESSFULLY")
        print("="*80)
        
        return slots
        
    except Exception as e:
        error_msg = f"Error generating delivery slots: {str(e)}"
        print(f"\n❌❌❌ EXCEPTION IN DELIVERY SLOTS API ❌❌❌")
        print(f"❌ Error: {error_msg}")
        logger.error(f"❌ {error_msg}")
        print(f"❌ Full traceback:")
        import traceback
        traceback.print_exc()
        print("="*80)
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

    # Convert timedelta to time if needed (Frappe Time fields return timedelta)
    if isinstance(opening_time, timedelta):
        opening_time = (datetime.min + opening_time).time()
    if isinstance(closing_time, timedelta):
        closing_time = (datetime.min + closing_time).time()

    print(f"🔍 Converting times - Opening: {opening_time} (type: {type(opening_time)}), Closing: {closing_time} (type: {type(closing_time)})")
    print(f"🔍 Same day setting: {same_day}")

    # Convert times to datetime objects for easier calculation
    current_slot_time = datetime.combine(target_date, opening_time)

    # Handle same_day vs next_day closing times
    if same_day == TIMING_MODES.NEXT_DAY:
        end_time = datetime.combine(target_date + timedelta(days=1), closing_time)
        print(f"🌙 Next day closing: {end_time}")
    else:
        end_time = datetime.combine(target_date, closing_time)
        print(f"🌅 Same day closing: {end_time}")

    print(f"🕐 Slot generation window: {current_slot_time} to {end_time}")

    # Validate that end_time is after start_time
    if end_time <= current_slot_time:
        print(f"❌ Invalid time window: end_time ({end_time}) <= start_time ({current_slot_time})")
        return slots

    # If this is today, ensure we only show future slots
    if current_datetime:
        # Add buffer of 30 minutes for preparation
        min_slot_time = current_datetime + timedelta(minutes=30)
        print(f"⏰ Minimum slot time (current + 30min buffer): {min_slot_time}")

        if current_slot_time < min_slot_time:
            # Round up to next slot boundary
            minutes_since_opening = (min_slot_time - current_slot_time).total_seconds() / 60
            slots_to_skip = int(minutes_since_opening / slot_duration_minutes) + 1
            current_slot_time += timedelta(minutes=slot_duration_minutes * slots_to_skip)
            print(f"⏭️  Adjusted start time for today: {current_slot_time} (skipped {slots_to_skip} slots)")

            # Check if adjusted time is still valid
            if current_slot_time >= end_time:
                print(f"⚠️  No valid slots for today after time adjustment")
                return slots

    slot_count = 0
    max_iterations = 50  # Safety limit to prevent infinite loops
    iteration_count = 0

    print(f"🔄 Starting slot generation loop...")
    while current_slot_time < end_time and iteration_count < max_iterations:
        iteration_count += 1
        slot_end_time = current_slot_time + timedelta(minutes=slot_duration_minutes)

        print(f"🔄 Iteration {iteration_count}: Checking slot {current_slot_time} - {slot_end_time}")

        # Regular slot overflows closing time — try custom last slot before stopping
        if slot_end_time > end_time:
            print(f"🛑 Regular slot would extend beyond closing time: {slot_end_time} > {end_time}")
            if last_slot_duration_minutes:
                last_slot_end = current_slot_time + timedelta(minutes=last_slot_duration_minutes)
                if last_slot_end <= end_time:
                    print(f"🔚 Adding custom last slot: {current_slot_time} - {last_slot_end}")
                    slot_end_time = last_slot_end
                    # Fall through to append, then exit loop
                else:
                    print(f"🛑 Custom last slot also overflows: {last_slot_end} > {end_time}")
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
        print(f"✅ Generated slot {slot_count}: {time_label}")

        # If we appended a custom last slot, the loop must end now
        if slot_end_time != current_slot_time + timedelta(minutes=slot_duration_minutes):
            break

        current_slot_time += timedelta(minutes=slot_duration_minutes)

    if iteration_count >= max_iterations:
        print(f"⚠️  Stopped slot generation after {max_iterations} iterations to prevent infinite loop")

    print(f"📊 Total slots generated for {target_date}: {len(slots)}")
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
        # The generator prints ~50 debug lines per day. The Desk form calls this
        # on every edit, so swallow that here rather than flooding the backend
        # container's stdout with a form preview.
        with contextlib.redirect_stdout(io.StringIO()):
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
