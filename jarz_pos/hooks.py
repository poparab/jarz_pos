app_name = "jarz_pos"
app_title = "Jarz POS"
app_publisher = "Abdelrahman Mamdouh"
app_description = "Customized POS for JARZ company."
app_email = "abdelrahmanmamdouh1996@gmail.com"
app_license = "mit"

# Desk identity. In Frappe v16 the sidebar header shows the logo + title of the
# app owning the current workspace, sourced from `bootinfo.app_data` — which is
# built for every installed app whether or not it declares `add_to_apps_screen`.
# Without these three hooks Jarz inherited the generic Frappe logo and rendered
# as the raw module name.
app_logo_url = "/assets/jarz_pos/images/jarz-pos-logo.svg"
app_home = "/desk/jarz-pos"

# Fixtures
fixtures = [
    {"dt": "Custom Field", "filters": [["dt", "in", [
        "Print Settings", "Sales Invoice", "Sales Invoice Item", "Address", "Supplier", "Quotation", "Sales Order", "Customer", "Sales Partner", "User", "Employee", "Account", "Item", "Lead", "Opportunity", "Work Order",
        # Purchasing: idempotency key on the invoice, requester/branch/note on
        # the item request. Omitting a dt here means its fields silently never
        # migrate, so this list must track the fixture file.
        "Purchase Invoice", "Material Request"
    ]]]},
    # "Jarz POS Settings" is deliberately NOT a fixture. Frappe imports a Single
    # fixture with force=True + delete_old_doc, i.e. it REBUILDS the document from
    # the JSON — so every field absent from that file reverted to its doctype
    # default on every `bench migrate`. The file listed no feature flags, so every
    # flag on this Single was silently switched off by every backend deploy,
    # including enable_invoice_returns, whose own description calls it "the instant
    # rollback lever — it needs no deploy and no restart".
    #
    # Seeding now happens in setup.settings_defaults.ensure_settings_defaults
    # (after_migrate), which fills only fields that are EMPTY and never overwrites
    # an operator's choice.
]

# Ensure conflicting Custom Fields are removed before fixtures import
before_migrate = [
    "jarz_pos.utils.cleanup.remove_conflicting_territory_delivery_fields",
    # Remove any existing Custom Fields that collide with our fixtures by dt+fieldname
    "jarz_pos.utils.cleanup.remove_colliding_custom_fields_for_fixtures",
    # Courier app schema (COURIER_CONTRACTS §2 and §3). Deliberately AFTER the
    # collision sweep: that sweep deletes any Custom Field whose name differs
    # from the fixture's, so seeding first would only hand it something to
    # delete. Both seeders are create-only and swallow their own exceptions.
    "jarz_pos.utils.cleanup.ensure_courier_delivery_fields",
    "jarz_pos.utils.cleanup.ensure_address_geo_fields",
    # Customer tracking token. Kept out of ensure_courier_delivery_fields because
    # COURIER_CONTRACTS §2 freezes that block at eight fields and a guard test
    # asserts the set.
    "jarz_pos.utils.cleanup.ensure_tracking_fields",
    # Ensure Territory has delivery_income and delivery_expense fields
    "jarz_pos.utils.cleanup.ensure_territory_delivery_fields",
    # Ensure new delivery slot fields exist before fixtures import / migrations
    "jarz_pos.utils.cleanup.ensure_delivery_slot_fields",
    # Remove legacy single datetime field
    "jarz_pos.utils.cleanup.remove_required_delivery_datetime_field",
]

after_migrate = [
    # Create the POS ledger accounts Jarz POS Settings names (idempotent,
    # create-only). Runs FIRST: a settings field pointing at a non-existent
    # account fails link validation for every later full save of that Single,
    # which is how the purchasing seeder below silently did nothing on
    # production while succeeding on staging.
    "jarz_pos.setup.accounts_setup.ensure_pos_accounts",
    # Create the default 14% purchase VAT Item Tax Template (create-only, uses
    # an EXISTING tax account, no-ops if the company has none). Must run BEFORE
    # settings_defaults: that seeder points the Jarz POS Settings link at this
    # template, and it will only do so once the template actually exists — a
    # Link naming a missing record poisons every later full save of the Single.
    "jarz_pos.setup.purchase_setup.ensure_purchase_vat_template",
    # Create the label-COGS masters (Labels Inventory / Label Cost accounts and
    # the Customer Label Printing item). Deliberately NOT gated on settings the
    # way ensure_pos_accounts is: account-if-configured plus seed-if-exists
    # deadlocks for a brand-new account, so this creates first and
    # settings_defaults links after.
    "jarz_pos.setup.label_setup.ensure_label_accounting",
    # Fill ONLY the empty fields on Jarz POS Settings. Replaces the Single
    # fixture, which rebuilt the doc every migrate and reverted every feature
    # flag. Must run after ensure_pos_accounts so the account names it seeds
    # already exist — a Link to a missing record fails validation on every later
    # full save of this Single.
    "jarz_pos.setup.settings_defaults.ensure_settings_defaults",
    # Rebuild every Jarz Desk surface from jarz_pos.utils.setup_workspace (idempotent):
    # the JARZ POS workspace, the Jarz POS sidebar, and the additive Home entry.
    "jarz_pos.utils.setup_workspace.ensure_jarz_desk",
    # Seed B2B master data (idempotent, create-only)
    "jarz_pos.setup.b2b_master_data.ensure_b2b_master_data",
    # Seed CRM config: Assignment Rule + Opportunity Workflow (idempotent, guarded)
    "jarz_pos.setup.crm_setup.ensure_crm_setup",
    # Create the Production Operator role + role profile + doc perms (idempotent)
    "jarz_pos.setup.production_setup.ensure_production_setup",
    # Seed the courier app's Delivery Failure Reason master data (idempotent,
    # create-only, swallows every exception so it can never abort the shared
    # bench migrate).
    "jarz_pos.setup.courier_setup.ensure_courier_setup",
    # Seed purchasing warehouse routing (idempotent, create-only). Keeps the
    # "where does purchased stock land" table identical on staging and prod
    # instead of being set by hand on each.
    "jarz_pos.setup.purchase_setup.ensure_purchase_setup",
    # Park fully-returned orders in the "Returned" column (idempotent). Must run
    # here rather than as a patch: post_model_sync patches execute BEFORE
    # sync_fixtures(), so the "Returned" Select option would not exist yet.
    # Re-running every migrate is deliberate — it also reconciles returns whose
    # best-effort board move failed.
    "jarz_pos.setup.return_board_state.ensure_returned_board_state",
]

# Apps
# ------------------

# required_apps = []

# Puts Jarz POS in the desk app switcher with its own logo, so the whole module
# is one click from anywhere in the Desk. `route` must stay in sync with the
# workspace name — Workspace autonames on its label, so "JARZ POS" slugs to
# /desk/jarz-pos.
add_to_apps_screen = [
    {
        "name": "jarz_pos",
        "logo": "/assets/jarz_pos/images/jarz-pos-logo.svg",
        "title": "Jarz POS",
        "route": "/desk/jarz-pos",
    }
]

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

# There is no `workspaces` hook in Frappe — nothing reads it. A 90-line one
# lived here declaring the JARZ POS workspace and had never had any effect;
# the workspace that exists was built entirely by the after_migrate hook.
# The real definition is jarz_pos/utils/setup_workspace.py.

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

# Website routes
# --------------
#
# The customer-facing delivery tracking page. `/track/<token>` resolves to
# `jarz_pos/www/track.html` (+ `track.py`), and werkzeug puts the captured
# `token` segment into `frappe.form_dict` — which is what the controller reads.
#
# The dynamic segment MUST be a route rule rather than a query string: the link
# goes into an SMS/WhatsApp message, and `?token=` gets mangled by link
# shorteners and truncated by preview crawlers far more often than a path does.
#
# Keep the prefix in step with `jarz_pos.services.tracking.TRACKING_ROUTE_PREFIX`,
# which is what builds the absolute URL the Woo app and the POS both hand out.
website_route_rules = [
    {"from_route": "/track/<token>", "to_route": "track"},
    # B2B sales material, sent to a prospect on WhatsApp. Same shape as
    # /track: the token segment is mapped away before TemplatePage sees it,
    # which is exactly why www/m.py renders nothing per-token.
    {"from_route": "/m/<token>", "to_route": "m"},
]

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
    # Keep Address.custom_geo_confidence in step with custom_geo_source. This
    # fires on EVERY Address save site-wide, including the WooCommerce bulk
    # customer sync — so the handler makes zero DB queries, zero network calls,
    # never raises, and never touches a field in Woo's outbound trigger set.
    "Address": {
        "before_save": "jarz_pos.events.address.clamp_geo_confidence",
    },
    "Sales Invoice": {
        # Promo-code engine: single apply path for Woo / Desk invoices. Runs
        # before validate so calculate_taxes_and_totals picks up discount_amount.
        "before_validate": "jarz_pos.services.promo_codes.apply_promo_codes_before_validate",
        # Seed custom_kanban_profile from pos_profile on drafts; preserve submitted reassignments
        "validate": [
            "jarz_pos.events.sales_invoice.sync_kanban_profile",
            # ERPNext's set_pos_fields() re-applies the POS Profile's own
            # update_stock during validate, and every profile here has it on —
            # so stock must be re-suppressed on EVERY save, not just at creation.
            "jarz_pos.events.sales_invoice.suppress_pos_invoice_stock_update",
        ],
    # Emit WebSocket event when POS invoice is submitted (ensures final totals/state)
    "on_submit": [
        "jarz_pos.events.sales_invoice.publish_new_invoice",
        # CRM bridge: link B2B sale to Opportunity (never raises, fast-exits Standard)
        "jarz_pos.crm.pos_bridge.link_b2b_sale_to_opportunity",
        # Promo-code engine: record redemptions (concurrency-safe, may abort submit)
        "jarz_pos.services.promo_codes.record_redemptions_on_submit",
        # B2B printed labels: draw down the customer's label stock. Deliberately
        # on submit rather than on the OFD transition where consumable_deduction
        # sits — a B2B supply order does not have to travel the dispatch kanban,
        # and one that never reached OFD would silently never consume a label.
        "jarz_pos.services.label_stock.consume_labels_on_invoice_submit",
        # Branch/territory parity catch. Files a "Jarz Territory Exception" when
        # the branch that shipped is not the branch the delivery territory points
        # at, or when the order has no territory at all. Deliberately a doc event
        # rather than a call inside services/invoice_creation: this way it covers
        # the WooCommerce inbound lane too, which never goes through that module.
        # Never raises, never touches the invoice, idempotent per (invoice, type).
        "jarz_pos.services.territory_exceptions.record_territory_exception_on_submit",
    ],
    # Emit state-change events for already-submitted invoices edited elsewhere
    "on_update_after_submit": [
        "jarz_pos.events.sales_invoice.publish_state_change_if_needed",
        "jarz_pos.services.consumable_deduction.deduct_consumables_on_ofd",
        "jarz_pos.events.sales_invoice.stamp_out_for_delivery_flag",
        # Customer delivery tracking: mint the opaque /track token on the first
        # Out for Delivery move. Here rather than in api/kanban + api/trips
        # because every dispatch path saves the submitted invoice, so one hook
        # covers all of them (never raises).
        "jarz_pos.events.sales_invoice.mint_tracking_token_on_ofd",
        # CRM: Sample/Trial delivery -> feedback / check-up follow-up (never raises)
        "jarz_pos.crm.pos_bridge.create_delivery_followup_on_state",
    ],
        # FIX 5 (2026-07-20): document-level guard — refuse to cancel a dispatched
        # invoice from ANY path (Desk / script / API), not just kanban.cancel_invoice.
        "before_cancel": "jarz_pos.events.sales_invoice.block_cancel_if_dispatched",
        # Keep operational workflow fields aligned across all cancellation paths.
        "on_cancel": [
            "jarz_pos.events.sales_invoice.mark_cancelled_invoice_workflow_fields",
            "jarz_pos.services.consumable_deduction.reverse_consumable_deduction_on_cancel",
            # Promo-code engine: reverse redemptions, recompute times_used
            "jarz_pos.services.promo_codes.reverse_redemptions_on_cancel",
            # B2B printed labels: hand back whatever this invoice consumed.
            "jarz_pos.services.label_stock.reverse_labels_on_invoice_cancel",
        ],
        # Validate bundle items before submission
        "before_submit": "jarz_pos.events.sales_invoice.validate_invoice_before_submit"
    }
}

# Scheduled Tasks
# ---------------

scheduler_events = {
    "daily": [
        # Automated site backup. Frappe ships NO backup-creating job — upstream
        # relies on a host crontab that a Docker deployment never gets — so
        # until this landed the only backups this deployment had ever taken
        # were a side effect of deploying, and passing -SkipBackup silently
        # reduced backup coverage to zero. See setup/backup_schedule.py.
        "jarz_pos.setup.backup_schedule.daily_backup",
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
        # B2B printed labels: refresh every label's cover and alert on the ones
        # that must go to the print house now to land before they run out.
        "jarz_pos.services.label_stock.run_label_stock_alerts",
        # Branch/territory parity: pick up anything the on_submit hook missed in
        # the last few days, then auto-close the exceptions whose territory has
        # since been corrected. Bounded window and a hard row cap, so this can
        # never run long enough to crowd the rest of the daily slot. The
        # HISTORICAL backfill is deliberately NOT here — it is a one-off run by
        # hand via territory_exceptions.backfill_territory_exceptions.
        "jarz_pos.services.territory_exceptions.run_territory_exception_sweep",
    ],
    "weekly": [
        "jarz_pos.tasks.run_weekly_velocity_update",
    ],
    "hourly": [
        # Escalate unpaid InstaPay/Mobile Wallet orders that have sat Out for
        # Delivery awaiting payment confirmation past the configured threshold.
        "jarz_pos.tasks.escalate_unconfirmed_online_payments",
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
    _leads.get_not_suitable_reasons
    _leads.set_lead_suitability
    _leads.get_merge_candidates
    _leads.merge_leads
except Exception:
    pass

try:
    # Courier app (COURIER_CONTRACTS): door-pin geo endpoints and the per-invoice
    # delivery transitions. Touched here so their @frappe.whitelist() decorators
    # register at startup like every other API module in this file.
    from jarz_pos.api import geo as _geo_api
    from jarz_pos.api import courier_delivery as _courier_api
    _geo_api.preview_maps_link
    _geo_api.get_address_pin
    _geo_api.set_address_pin
    _geo_api.dry_run_address_pin
    _courier_api.get_delivery_failure_reasons
    _courier_api.get_stop_outcome
    _courier_api.get_my_courier_identity
    _courier_api.mark_arrived
    _courier_api.mark_delivered
    _courier_api.mark_failed
except Exception:
    pass

try:
    # Customer delivery tracking. get_public_status is the ONLY allow_guest
    # endpoint in this app; touching it here registers its whitelist entry at
    # startup like every other API module, so the public page never 404s because
    # of lazy import order.
    from jarz_pos.api import tracking as _tracking_api
    _tracking_api.get_public_status
    _tracking_api.get_tracking_link
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

