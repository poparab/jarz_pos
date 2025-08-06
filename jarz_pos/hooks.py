app_name = "jarz_pos"
app_title = "jarz pos"
app_publisher = "Abdelrahman Mamdouh"
app_description = "Customized POS for JARZ company."
app_email = "abdelrahmanmamdouh1996@gmail.com"
app_license = "mit"

fixtures = [
    "Custom Field"
]

# The original POS frontend assets have been archived under `frontend_archive/`.
# To keep this backend app headless/API-only, we disable page-level JS inclusion.
page_js = {}


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
# page_js = {"page" : "public/js/file.js"}

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
                "label": "Sales Invoice Kanban",
                "format": "{} Kanban",
                "link_to": "kanban_board",
                "type": "Page",
                "icon": "fa fa-columns",
                "color": "#3498db"
            }
        ],
        "cards": []
    }
]

# include js in doctype views
# doctype_js = {"doctype" : "public/js/doctype.js"}
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

# before_uninstall = "jarz_pos.uninstall.before_uninstall"
# after_uninstall = "jarz_pos.uninstall.after_uninstall"

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

# override_doctype_class = {
# 	"ToDo": "custom_app.overrides.CustomToDo"
# }

# Document Events
# ---------------
# Hook on document methods and events

doc_events = {
    "Sales Invoice": {
        # Emit WebSocket event after each POS invoice is inserted
        "after_insert": "jarz_pos.jarz_pos.events.sales_invoice.publish_new_invoice"
    }
}

# Scheduled Tasks
# ---------------

# scheduler_events = {
# 	"all": [
# 		"jarz_pos.tasks.all"
# 	],
# 	"daily": [
# 		"jarz_pos.tasks.daily"
# 	],
# 	"hourly": [
# 		"jarz_pos.tasks.hourly"
# 	],
# 	"weekly": [
# 		"jarz_pos.tasks.weekly"
# 	],
# 	"monthly": [
# 		"jarz_pos.tasks.monthly"
# 	],
# }

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
    "get_pos_profiles": "jarz_pos.api.pos.get_pos_profiles",
    "get_profile_bundles": "jarz_pos.api.pos.get_profile_bundles",
    "get_profile_products": "jarz_pos.api.pos.get_profile_products",
}

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
# before_request = ["jarz_pos.utils.before_request"]
# after_request = ["jarz_pos.utils.after_request"]

# Job Events
# ----------
# before_job = ["jarz_pos.utils.before_job"]
# after_job = ["jarz_pos.utils.after_job"]

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

