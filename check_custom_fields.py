#!/usr/bin/env python3

import frappe

def check_custom_fields():
    # Initialize Frappe
    frappe.init(site="development.localhost")
    frappe.connect()
    
    print("🔍 Checking Sales Invoice custom fields...")
    
    # Get all custom fields for Sales Invoice
    custom_fields = frappe.db.sql("""
        SELECT fieldname, label, fieldtype, insert_after 
        FROM `tabCustom Field` 
        WHERE dt='Sales Invoice' 
        ORDER BY idx
    """, as_dict=True)
    
    print(f"📋 Found {len(custom_fields)} custom fields:")
    for field in custom_fields:
        print(f"   - {field.fieldname} ({field.fieldtype}): {field.label}")
    
    # Check for delivery-related fields specifically
    delivery_fields = [f for f in custom_fields if 'delivery' in f.fieldname.lower() or 'required' in f.fieldname.lower()]
    
    print(f"\n🚚 Delivery-related fields ({len(delivery_fields)}):")
    for field in delivery_fields:
        print(f"   - {field.fieldname} ({field.fieldtype}): {field.label}")
    
    # Also check meta fields
    try:
        meta = frappe.get_meta("Sales Invoice")
        all_fields = [(field.fieldname, field.fieldtype, field.label) for field in meta.fields]
        delivery_meta_fields = [(name, ftype, label) for name, ftype, label in all_fields if 'delivery' in name.lower() or 'required' in name.lower()]
        
        print(f"\n📝 Meta delivery fields ({len(delivery_meta_fields)}):")
        for name, ftype, label in delivery_meta_fields:
            print(f"   - {name} ({ftype}): {label}")
    except Exception as e:
        print(f"❌ Error getting meta fields: {str(e)}")

if __name__ == "__main__":
    check_custom_fields()
