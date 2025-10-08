"""Script to fix the HR-EMP naming series counter.

Run this on the staging server with:
    bench --site frontend execute jarz_pos.scripts.fix_employee_series
"""
import frappe

def execute():
    """Fix the HR-EMP naming series by setting the correct current value."""
    # Get the highest existing Employee ID
    last_emp = frappe.db.sql("""
        SELECT name 
        FROM `tabEmployee` 
        WHERE name LIKE 'HR-EMP-%' 
        ORDER BY name DESC 
        LIMIT 1
    """, as_dict=True)
    
    if last_emp:
        last_id = last_emp[0].name
        print(f"Last Employee ID: {last_id}")
        
        # Extract the number from HR-EMP-00003 -> 3
        try:
            number = int(last_id.replace('HR-EMP-', '').replace('-', ''))
            next_number = number + 1
            
            print(f"Setting HR-EMP- series current to {next_number}")
            
            # Update or insert the series current value
            if frappe.db.exists("Series", "HR-EMP-"):
                frappe.db.sql("UPDATE `tabSeries` SET current = %s WHERE name = %s", (next_number, "HR-EMP-"))
            else:
                frappe.db.sql("INSERT INTO `tabSeries` (name, current) VALUES (%s, %s)", ("HR-EMP-", next_number))
            
            frappe.db.commit()
            print(f"✓ Series updated successfully. Next Employee will be HR-EMP-{str(next_number).zfill(5)}")
            
        except ValueError as e:
            print(f"Error parsing Employee ID: {e}")
    else:
        print("No existing Employees found with HR-EMP- pattern")
