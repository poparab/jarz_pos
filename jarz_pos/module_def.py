"""Module definition helper for Frappe desk loading.
Provides get_data so Module appears in Desk Module view.
"""
from frappe import _

def get_data():
    return [
        {
            "module_name": "jarz pos",
            "category": "Modules",
            "label": _("Jarz POS"),
            "color": "#3498db",
            "icon": "octicon octicon-device-desktop",
            "type": "module",
            "description": "Jarz POS Customizations"
        }
    ]
