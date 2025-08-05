"""
Sample script to create POS Profile Timetable configuration for testing
"""

import frappe

def create_sample_timetable():
    """Create a sample POS Profile Timetable for testing"""
    
    # Get the first available POS Profile
    pos_profile = frappe.get_value("POS Profile", {"disabled": 0}, "name")
    
    if not pos_profile:
        print("No POS Profile found. Please create a POS Profile first.")
        return
    
    print(f"Creating timetable for POS Profile: {pos_profile}")
    
    # Check if timetable already exists
    existing_timetable = frappe.get_value("POS Profile Timetable", {"pos_profile": pos_profile}, "name")
    
    if existing_timetable:
        print(f"Timetable already exists: {existing_timetable}")
        # Update the existing one
        timetable_doc = frappe.get_doc("POS Profile Timetable", existing_timetable)
    else:
        # Create new timetable
        timetable_doc = frappe.new_doc("POS Profile Timetable")
        timetable_doc.pos_profile = pos_profile
    
    # Set slot hours (2 hours per slot)
    timetable_doc.slot_hours = "2"
    
    # Clear existing timetable entries
    timetable_doc.timetable = []
    
    # Add working days (Monday to Saturday)
    working_days = [
        {"day": "Monday", "opening_time": "09:00:00", "closing_time": "21:00:00"},
        {"day": "Tuesday", "opening_time": "09:00:00", "closing_time": "21:00:00"},
        {"day": "Wednesday", "opening_time": "09:00:00", "closing_time": "21:00:00"},
        {"day": "Thursday", "opening_time": "09:00:00", "closing_time": "21:00:00"},
        {"day": "Friday", "opening_time": "09:00:00", "closing_time": "21:00:00"},
        {"day": "Saturday", "opening_time": "10:00:00", "closing_time": "20:00:00"},
    ]
    
    for day_timing in working_days:
        timetable_doc.append("timetable", day_timing)
    
    # Save the document
    timetable_doc.save(ignore_permissions=True)
    
    print(f"✅ POS Profile Timetable created/updated: {timetable_doc.name}")
    print(f"   - Slot Duration: {timetable_doc.slot_hours} hours")
    print(f"   - Working Days: {len(timetable_doc.timetable)}")
    
    for timing in timetable_doc.timetable:
        print(f"   - {timing.day}: {timing.opening_time} - {timing.closing_time}")
    
    frappe.db.commit()
    
    return timetable_doc.name

if __name__ == "__main__":
    create_sample_timetable()
