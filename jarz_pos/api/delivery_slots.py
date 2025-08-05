"""Jarz POS – Delivery Time Slot API endpoints.

Provides delivery time slot management based on POS Profile Timetable configuration.
"""

from __future__ import annotations
import frappe
import json
from datetime import datetime, timedelta, time
from typing import List, Dict, Any, Union


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
        # Check if POS Profile exists
        print(f"\n🔍 STEP 1: Checking if POS Profile exists...")
        profile_exists = frappe.db.exists("POS Profile", pos_profile_name)
        print(f"📊 POS Profile '{pos_profile_name}' exists: {profile_exists}")
        
        if not profile_exists:
            error_msg = f"POS Profile '{pos_profile_name}' does not exist"
            print(f"❌ ERROR: {error_msg}")
            logger.error(f"❌ {error_msg}")
            frappe.throw(error_msg)
        
        # Get POS Profile Timetable configuration
        print(f"\n🔍 STEP 2: Looking for POS Profile Timetable...")
        print(f"🔎 Searching for timetable with pos_profile = '{pos_profile_name}'")
        
        # First, let's check what POS Profile Timetables exist
        all_timetables = frappe.get_all("POS Profile Timetable", fields=["name", "pos_profile", "slot_hours"])
        print(f"📋 All available timetables: {all_timetables}")
        
        timetable_config = frappe.get_value(
            "POS Profile Timetable",
            {"pos_profile": pos_profile_name},
            ["name", "slot_hours"],
            as_dict=True
        )
        
        print(f"📊 Timetable config found: {timetable_config}")
        
        if not timetable_config:
            error_msg = f"No timetable configured for POS Profile '{pos_profile_name}'"
            print(f"❌ ERROR: {error_msg}")
            logger.error(f"❌ {error_msg}")
            frappe.throw(error_msg)
        
        slot_hours = int(timetable_config.slot_hours)
        timetable_name = timetable_config.name
        
        print(f"\n🔍 STEP 3: Processing timetable configuration...")
        print(f"📊 Timetable name: {timetable_name}")
        print(f"⏰ Slot hours: {slot_hours}")
        
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
                timing['same_day'] = 'Same Day'  # Default value
        
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
            same_day = timing.get('same_day', 'Same Day')  # Default to 'Same Day' if not present
            day_config[timing.day] = {
                'opening_time': timing.opening_time,
                'closing_time': timing.closing_time,
                'same_day': same_day
            }
            print(f"📅 Day {timing.day}: {timing.opening_time} - {timing.closing_time} (same_day: {same_day})")
        
        logger.info(f"✅ Found {len(day_timings)} day configurations with {slot_hours} hour slots")
        print(f"\n✅ Found {len(day_timings)} day configurations with {slot_hours} hour slots")
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
                slot_hours,
                current_datetime if day_offset == 0 else None  # Only check current time for today
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
    slot_hours: int,
    current_datetime: datetime = None
) -> List[Dict[str, Any]]:
    """
    Generate time slots for a specific day
    
    Args:
        target_date: Date to generate slots for
        opening_time: Store opening time (can be time or timedelta)
        closing_time: Store closing time (can be time or timedelta)
        same_day: "Same Day" or "Next Day" - indicates if closing time is same day or next day
        slot_hours: Duration of each slot in hours
        current_datetime: Current datetime (only provided for today)
    
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
    if same_day == "Next Day":
        # Closing time is on the next day
        end_time = datetime.combine(target_date + timedelta(days=1), closing_time)
        print(f"🌙 Next day closing: {end_time}")
    else:
        # Closing time is on the same day
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
            slots_to_skip = int(minutes_since_opening / (slot_hours * 60)) + 1
            current_slot_time += timedelta(hours=slot_hours * slots_to_skip)
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
        slot_end_time = current_slot_time + timedelta(hours=slot_hours)
        
        print(f"🔄 Iteration {iteration_count}: Checking slot {current_slot_time} - {slot_end_time}")
        
        # Don't create slots that extend beyond closing time
        if slot_end_time > end_time:
            print(f"🛑 Slot would extend beyond closing time: {slot_end_time} > {end_time}")
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
        
        current_slot_time += timedelta(hours=slot_hours)
    
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
