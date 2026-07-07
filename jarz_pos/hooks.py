app_name = "jarz_pos"
app_title = "jarz pos"
app_publisher = "Abdelrahman Mamdouh"
app_description = "Customized POS for JARZ company."
app_email = "abdelrahmanmamdouh1996@gmail.com"
app_license = "mit"

# Fixtures
fixtures = [
    {"dt": "Custom Field", "filters": [["dt", "in", [
        "Print Settings", "Sales Invoice", "Sales Invoice Item", "Address", "Supplier", "Quotation", "Sales Order", "Customer", "Sales Partner", "User", "Employee", "Account", "Item", "Lead", "Opportunity"
    ]]]},
    {"dt": "Jarz POS Settings"}
]

# Ensure conflicting Custom Fields are removed before fixtures import
before_migrate = [
    "jarz_pos.utils.cleanup.remove_conflicting_territory_delivery_fields",
    # Remove any existing Custom Fields that collide with our fixtures by dt+fieldname
    "jarz_pos.utils.cleanup.remove_colliding_custom_fields_for_fixtures",
    # Ensure Territory has delivery_income and delivery_expense fields
    "jarz_pos.utils.cleanup.ensure_territory_delivery_fields",
    # Ensure new delivery slot fields exist before fixtures import / migrations
    "jarz_pos.utils.cleanup.ensure_delivery_slot_fields",
    # Remove legacy single datetime field
    "jarz_pos.utils.cleanup.remove_required_delivery_datetime_field",
]

after_migrate = [
    # Add Inventory Forecast shortcut to JARZ POS workspace (idempotent)
    "jarz_pos.utils.setup_forecast.ensure_forecast_workspace_shortcuts",
    # Seed B2B master data (idempotent, create-only)
    "jarz_pos.setup.b2b_master_data.ensure_b2b_master_data",
    # Seed CRM config: Assignment Rule + Opportunity Workflow (idempotent, guarded)
    "jarz_pos.setup.crm_setup.ensure_crm_setup",
]

# Apps
# ------------------

# required_apps = []

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "jarz_pos",
# 		"logo": "/assets/jarz_pos/logo.png",
# 		"title": "jarz pos",
# 		"route": "/jarz_pos",
# 		"has_permission": "jarz_pos.api.permission.has_app_permission"
# 	}
# ]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/jarz_pos/css/jarz_pos.css"
# app_include_js = "/assets/jarz_pos/js/jarz_pos.js"

# include js, css files in header of web template
# web_include_css = "/assets/jarz_pos/css/jarz_pos.css"
# web_include_js = "/assets/jarz_pos/js/jarz_pos.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "jarz_pos/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
page_js = {"point-of-sale": "public/js/point_of_sale_close_fix.js"}

# Workspaces
# ----------

# List of workspaces that should be created
workspaces = [
    {
        "name": "JARZ POS",
        "category": "Modules",
        "public": 1,
        "icon": "fa fa-shopping-cart",
        "color": "#FF6B35",
        "sequence_id": 1,
        "charts": [],
        "shortcuts": [
            {
                "label": "Sales Invoice List",
                "format": "{} List",
                "link_to": "Sales Invoice",
                "type": "DocType",
                "icon": "fa fa-file-text",
                "color": "#3498db"
            },
            {
                "label": "POS Profile",
                "format": "{} Settings",
                "link_to": "POS Profile",
                "type": "DocType",
                "icon": "fa fa-cog",
                "color": "#e74c3c"
            },
            {
                "label": "Executive Overview",
                "link_to": "executive-analytics",
                "type": "Page",
                "icon": "fa fa-dashboard",
                "color": "#FF6B35"
            },
            {
                "label": "Product Analytics",
                "link_to": "product-analytics",
                "type": "Page",
                "icon": "fa fa-cube",
                "color": "#7B61FF"
            },
            {
                "label": "Shipping Analytics",
                "link_to": "shipping-analytics",
                "type": "Page",
                "icon": "fa fa-truck",
                "color": "#FF6B35"
            },
            {
                "label": "Customer Analytics",
                "link_to": "customer-analytics",
                "type": "Page",
                "icon": "fa fa-users",
                "color": "#2980b9"
            },
            {
                "label": "Inventory Intelligence",
                "link_to": "inventory-analytics",
                "type": "Page",
                "icon": "fa fa-bar-chart",
                "color": "#16a085"
            },
            {
                "label": "Inventory Forecast",
                "link_to": "Jarz Forecast Settings",
                "type": "DocType",
                "icon": "fa fa-bar-chart",
                "color": "#27ae60"
            }
        ],
        "cards": []
    }
]

# include js in doctype views
doctype_js = {
    "POS Closing Entry": "public/js/pos_closing_entry_fix.js",
    "Sales Invoice": "public/js/sales_invoice_cancelled_fields.js",
}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "jarz_pos/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "jarz_pos.utils.jinja_methods",
# 	"filters": "jarz_pos.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "jarz_pos.install.before_install"
# after_install = "jarz_pos.install.after_install"

# Uninstallation
# ------------

# Provide a light uninstall cleanup to remove legacy fields (safe no-ops if absent)
before_uninstall = "jarz_pos.utils.cleanup.remove_conflicting_territory_delivery_fields"
after_uninstall = "jarz_pos.utils.cleanup.remove_required_delivery_datetime_field"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "jarz_pos.utils.before_app_install"
# after_app_install = "jarz_pos.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "jarz_pos.utils.before_app_uninstall"
# after_app_uninstall = "jarz_pos.utils.after_app_uninstall"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "jarz_pos.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# DocType Class
# ---------------
# Override standard doctype classes

override_doctype_class = {
    "POS Closing Entry": "jarz_pos.overrides.pos_closing_entry.POSClosingEntry",
}

# Document Events
# ---------------
# Hook on document methods and events

doc_events = {
    "Sales Invoice": {
        # Promo-code engine: single apply path for Woo / Desk invoices. Runs
        # before validate so calculate_taxes_and_totals picks up discount_amount.
        "before_validate": "jarz_pos.services.promo_codes.apply_promo_codes_before_validate",
        # Seed custom_kanban_profile from pos_profile on drafts; preserve submitted reassignments
        "validate": "jarz_pos.events.sales_invoice.sync_kanban_profile",
    # Emit WebSocket event when POS invoice is submitted (ensures final totals/state)
    "on_submit": [
        "jarz_pos.events.sales_invoice.publish_new_invoice",
        # CRM bridge: link B2B sale to Opportunity (never raises, fast-exits Standard)
        "jarz_pos.crm.pos_bridge.link_b2b_sale_to_opportunity",
        # Promo-code engine: record redemptions (concurrency-safe, may abort submit)
        "jarz_pos.services.promo_codes.record_redemptions_on_submit",
    ],
    # Emit state-change events for already-submitted invoices edited elsewhere
    "on_update_after_submit": [
        "jarz_pos.events.sales_invoice.publish_state_change_if_needed",
        "jarz_pos.services.consumable_deduction.deduct_consumables_on_ofd",
        "jarz_pos.events.sales_invoice.stamp_out_for_delivery_flag",
        # CRM: Sample/Trial delivery -> feedback / check-up follow-up (never raises)
        "jarz_pos.crm.pos_bridge.create_delivery_followup_on_state",
    ],
        # Keep operational workflow fields aligned across all cancellation paths.
        "on_cancel": [
            "jarz_pos.events.sales_invoice.mark_cancelled_invoice_workflow_fields",
            "jarz_pos.services.consumable_deduction.reverse_consumable_deduction_on_cancel",
            # Promo-code engine: reverse redemptions, recompute times_used
            "jarz_pos.services.promo_codes.reverse_redemptions_on_cancel",
        ],
        # Validate bundle items before submission
        "before_submit": "jarz_pos.events.sales_invoice.validate_invoice_before_submit"
    }
}

# Scheduled Tasks
# ---------------

scheduler_events = {
    "daily": [
        "jarz_pos.tasks.run_nightly_rfm_segmentation",
        "jarz_pos.tasks.run_daily_inventory_digest",
        # CRM automation (guarded, never raise)
        # Lead-score auto-recompute DISABLED by product decision: the catalog
        # fit score (custom_fit_score) is manually/Excel owned and must NEVER
        # change automatically. Re-enable only if the CRM-computed
        # custom_lead_score is wanted again.
        # "jarz_pos.crm.lead_scoring.compute_lead_scores",
        "jarz_pos.crm.follow_ups.run_followup_reminders",
        "jarz_pos.crm.reorder_forecast.compute_reorder_forecast",
    ],
    "weekly": [
        "jarz_pos.tasks.run_weekly_velocity_update",
    ],
}

# Testing
# -------

# before_tests = "jarz_pos.install.before_tests"

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "jarz_pos.event.get_events"
# }

override_whitelisted_methods = {
    # POS API Methods
    "get_pos_profiles": "jarz_pos.api.pos.get_pos_profiles",
    "get_pos_profile_data": "jarz_pos.api.pos.get_pos_profile_data",
    "get_products": "jarz_pos.api.pos.get_products",
    "get_bundles": "jarz_pos.api.pos.get_bundles",
    "create_pos_invoice": "jarz_pos.api.invoices.create_pos_invoice",
    "simple_invoice": "jarz_pos.api.invoices.simple_invoice",
    "get_pos_profiles_with_items": "jarz_pos.api.pos.get_pos_profiles_with_items",
    "get_active_pos_profiles": "jarz_pos.api.pos.get_active_pos_profiles",
    "update_profile_status": "jarz_pos.api.pos.update_profile_status",
    "process_bundle": "jarz_pos.api.pos.process_bundle",
    "get_item_details": "jarz_pos.api.pos.get_item_details",
    "get_profile_bundles": "jarz_pos.api.pos.get_profile_bundles",
    "test_bundle_debug": "jarz_pos.api.pos.test_bundle_debug",
    "get_territory_pos_profile": "jarz_pos.api.pos.get_territory_pos_profile",
    # User API Methods
    "jarz_pos.api.user.get_current_user_roles": "jarz_pos.api.user.get_current_user_roles",
    # Shift API Methods
    "jarz_pos.api.shift.get_active_shift": "jarz_pos.api.shift.get_active_shift",
    "jarz_pos.api.shift.get_shift_payment_methods": "jarz_pos.api.shift.get_shift_payment_methods",
    "jarz_pos.api.shift.start_shift": "jarz_pos.api.shift.start_shift",
    "jarz_pos.api.shift.get_shift_summary": "jarz_pos.api.shift.get_shift_summary",
    "jarz_pos.api.shift.end_shift": "jarz_pos.api.shift.end_shift",
    # Notification API Methods
    "jarz_pos.api.notifications.get_pending_alerts": "jarz_pos.api.notifications.get_pending_alerts",
    "jarz_pos.api.notifications.register_device_token": "jarz_pos.api.notifications.register_device_token",
    "jarz_pos.api.notifications.accept_invoice": "jarz_pos.api.notifications.accept_invoice",
}

# Ensure API modules are imported at startup so @frappe.whitelist() decorators register
try:
    from jarz_pos.api import manager as _mgr
    _mgr.get_manager_dashboard_summary
    _mgr.get_manager_orders
except Exception:
    pass

try:
    # Ensure B2B CRM endpoints register their @frappe.whitelist() decorators.
    from jarz_pos.api import crm as _crm
    _crm.get_b2b_pipeline
    _crm.get_account
    _crm.advance_stage
    _crm.create_lead
    _crm.log_activity
    _crm.get_my_followups
    _crm.get_reorder_due
    _crm.request_sample
    _crm.place_b2b_order
except Exception:
    pass

try:
    # Ensure Leads catalog endpoints register their @frappe.whitelist() decorators.
    from jarz_pos.api import leads as _leads
    _leads.get_leads
    _leads.get_lead
    _leads.save_lead
    _leads.set_lead_address
    _leads.get_lead_categories
    _leads.save_lead_category
except Exception:
    pass

try:
    from jarz_pos.api import user as _user
    from jarz_pos.api import notifications as _notif
    from jarz_pos.api import shift as _shift
    # Touch the functions to ensure they're loaded
    _user.get_current_user_roles
    _notif.get_pending_alerts
    _notif.register_device_token
    _notif.accept_invoice
    _shift.get_active_shift
    _shift.get_shift_payment_methods
    _shift.start_shift
    _shift.get_shift_summary
    _shift.end_shift
    # Defensive registration: ensure methods are marked as whitelisted
    _shift.get_active_shift.whitelisted = True
    _shift.get_shift_payment_methods.whitelisted = True
    _shift.start_shift.whitelisted = True
    _shift.get_shift_summary.whitelisted = True
    _shift.end_shift.whitelisted = True
    _shift.get_active_shift.allow_guest = False
    _shift.get_shift_payment_methods.allow_guest = False
    _shift.start_shift.allow_guest = False
    _shift.get_shift_summary.allow_guest = False
    _shift.end_shift.allow_guest = False
except Exception:
    pass

# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "jarz_pos.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
before_request = ["jarz_pos.observability.sentry_bootstrap.before_request"]
after_request = ["jarz_pos.observability.sentry_bootstrap.after_request"]

# Job Events
# ----------
before_job = ["jarz_pos.observability.sentry_bootstrap.before_job"]
after_job = ["jarz_pos.observability.sentry_bootstrap.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"jarz_pos.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

