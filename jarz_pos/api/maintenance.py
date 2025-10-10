"""API endpoint to fix the Employee naming series counter.

This is a workaround for when CLI access is not available.
Call via: POST /api/method/jarz_pos.api.maintenance.fix_employee_series
"""
import frappe


@frappe.whitelist(allow_guest=False)
def fix_employee_series():
    """Fix the HR-EMP naming series by setting the correct current value.

    Returns:
        dict: Status message with the updated series information
    """
    # Only allow System Manager or HR Manager to run this
    if not (frappe.session.user == "Administrator" or
            "System Manager" in frappe.get_roles() or
            "HR Manager" in frappe.get_roles()):
        frappe.throw("Only System Manager or HR Manager can fix naming series")

    # Get the highest existing Employee ID
    last_emp = frappe.db.sql("""
        SELECT name
        FROM `tabEmployee`
        WHERE name LIKE 'HR-EMP-%'
        ORDER BY name DESC
        LIMIT 1
    """, as_dict=True)

    if not last_emp:
        return {
            "success": False,
            "message": "No existing Employees found with HR-EMP- pattern"
        }

    last_id = last_emp[0].name
    frappe.logger().info(f"Last Employee ID: {last_id}")

    try:
        # Extract the number from HR-EMP-00003 -> 3
        number = int(last_id.replace('HR-EMP-', '').replace('-', ''))
        next_number = number + 1

        frappe.logger().info(f"Setting HR-EMP- series current to {next_number}")

        # Update or insert the series current value
        series_exists = frappe.db.sql("""
            SELECT name FROM `tabSeries` WHERE name = %s
        """, ("HR-EMP-",))

        if series_exists:
            frappe.db.sql("""
                UPDATE `tabSeries` SET current = %s WHERE name = %s
            """, (next_number, "HR-EMP-"))
            action = "updated"
        else:
            frappe.db.sql("""
                INSERT INTO `tabSeries` (name, current) VALUES (%s, %s)
            """, ("HR-EMP-", next_number))
            action = "created"

        frappe.db.commit()

        next_id = f"HR-EMP-{str(next_number).zfill(5)}"

        return {
            "success": True,
            "message": f"Series {action} successfully",
            "last_employee": last_id,
            "series_current": next_number,
            "next_employee_id": next_id
        }

    except ValueError as e:
        frappe.logger().error(f"Error parsing Employee ID: {e}")
        return {
            "success": False,
            "message": f"Error parsing Employee ID: {e!s}"
        }
    except Exception as e:
        frappe.logger().error(f"Error fixing series: {e}")
        frappe.logger().error(frappe.get_traceback())
        return {
            "success": False,
            "message": f"Error fixing series: {e!s}"
        }
