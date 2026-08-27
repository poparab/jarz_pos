"""
Centralised constants for the Jarz POS backend plugin.

Every hard-coded account name, role set, status string, event name,
and query limit lives here so that a rename or config change only
requires updating a single file (or the Jarz POS Settings doctype).
"""


# ── Account name defaults ──────────────────────────────────────────────
# These are used as *fallback* names when the Jarz POS Settings doctype
# field is empty.  The helper ``get_jarz_settings()`` returns the
# settings doc; consumers should prefer ``settings.<field>`` over
# these raw strings.

class ACCOUNTS:
    CASH_OVER_SHORT = "Cash Over Short"
    INDIRECT_EXPENSES = "Indirect Expenses"
    FREIGHT_AND_FORWARDING = "Freight and Forwarding Charges"
    COURIER_OUTSTANDING = "Courier Outstanding"
    CREDITORS = "Creditors"
    CASH_IN_HAND = "Cash In Hand"
    BANK_ACCOUNTS = "Bank Accounts"
    MOBILE_WALLET = "Mobile Wallet"
    INSTAPAY = "Instapay"
    PAYMENT_GATEWAY = "Payment Gateway"


# ── Role name sets ──────────────────────────────────────────────────────

class ROLES:
    # Individual role names first, so the sets below can be built from them
    # rather than repeating the literals. The sets used to come first, which is
    # why JARZ_MANAGER could not appear in them and why the divergence below
    # went unnoticed for so long.
    JARZ_MANAGER = "JARZ Manager"
    JARZ_LINE_MANAGER = "jarz line manager"
    #: Both spellings exist as real Role records on staging and production.
    #: Anywhere the line manager is named, name both — a set that carries only
    #: one silently excludes half the people holding the role.
    JARZ_LINE_MANAGER_ALT = "JARZ line manager"
    ADMINISTRATOR = "Administrator"
    SYSTEM_MANAGER = "System Manager"
    PRODUCTION_OPERATOR = "Production Operator"

    # JARZ Manager is a member of all four operational sets below. It was in
    # none of them, while the mobile drawer gated Cash Transfer, Stock Transfer,
    # Inventory Count and Purchase Invoice on `hasManagerAccess` — which does
    # include JARZ Manager. So the app showed a JARZ-Manager-only user four
    # entries and the server answered "Not permitted: Managers only" on every
    # one, along with the Production Board's item picker and Return-WIP.
    # The owner's call (2026-08-19) is that the server was wrong: a JARZ Manager
    # is meant to do these things.
    MANAGER = {"System Manager", "Accounts Manager", "Stock Manager",
               "Manufacturing Manager", "Purchase Manager", JARZ_MANAGER}
    STOCK = {"System Manager", "Stock Manager", "Manufacturing Manager",
             "Accounts Manager", JARZ_MANAGER}
    MANUFACTURING = {"System Manager", "Manufacturing Manager",
                     "Stock Manager", "Purchase Manager", JARZ_MANAGER}
    PURCHASE = {"System Manager", "Stock Manager", "Manufacturing Manager",
                "Purchase Manager", "Accounts Manager", JARZ_MANAGER}
    ADMIN = {"System Manager", "POS Manager"}
    # Everything a JARZ line manager may do, the manager tier and the
    # Administrator may do too.  The line manager is a *narrower* manager — a
    # floor supervisor — never the holder of an authority its own manager
    # lacks.  Gate on this set instead of naming the line manager by hand: the
    # hand-written sets drifted (cancel and return were line-manager-only for
    # months, so a JARZ Manager was refused on their own branch's orders).
    # Both spellings of the line-manager role are carried because both exist as
    # real Role records on staging and production.
    LINE_MANAGER_TIER = {
        JARZ_LINE_MANAGER,
        JARZ_LINE_MANAGER_ALT,
        JARZ_MANAGER,
        ADMINISTRATOR,
        SYSTEM_MANAGER,
    }
    #: Same membership, lowercased — for the gates that compare case-folded
    #: role names.  Keep the two in lockstep.
    LINE_MANAGER_TIER_LOWER = {role.lower() for role in LINE_MANAGER_TIER}
    # Move stock between warehouses (``api/transfer.py``).  Deliberately WIDER
    # than MANAGER: moving jars from the branch to Finished Goods (or between
    # branches) is floor-supervisor work, and gating it on MANAGER alone left a
    # line manager with no way to do it — the drawer entry was hidden and the
    # API answered "Not permitted" if they reached it another way.  Kept as its
    # own name rather than widening MANAGER, because MANAGER also guards Cash
    # Transfer and the Purchase Invoice, which commit money and stay closed to
    # the line manager.
    STOCK_TRANSFER = MANAGER | LINE_MANAGER_TIER
    # Read-only access to the Production Board.  Deliberately WIDER than
    # MANUFACTURING: the mobile screen has always been gated client-side on
    # "JARZ Manager", which MANUFACTURING does not contain — so a
    # JARZ-Manager-only user could open the screen and then hit
    # PermissionError on every call.  Widening MANUFACTURING itself would
    # change who may submit Work Orders, so the read path gets its own set.
    PRODUCTION_VIEW = MANUFACTURING | {JARZ_MANAGER, PRODUCTION_OPERATOR}
    # Start/finish a batch on the floor.  Same membership as PRODUCTION_VIEW
    # today, but kept as its own name so tightening execution later (or opening
    # the read path wider) does not require touching the other.
    PRODUCTION_EXECUTE = MANUFACTURING | {JARZ_MANAGER, PRODUCTION_OPERATOR}
    # Post a batch on an earlier date.  Operators are deliberately excluded:
    # backdating is how stock and COGS land in a closed period, and the whole
    # point of the floor role is that it cannot choose when a thing happened.
    PRODUCTION_BACKDATE = MANUFACTURING | {JARZ_MANAGER}
    # Raise an item request ("we're out of tomatoes").  Deliberately the widest
    # set in this class: asking for stock commits no money and no stock, and a
    # request flow only works if the people who actually notice a shortage —
    # floor staff, line managers, cashiers — can file one.  Buying against a
    # request stays on PURCHASE.
    PURCHASE_REQUEST = (
        PURCHASE
        | PRODUCTION_VIEW
        | {
            JARZ_MANAGER,
            JARZ_LINE_MANAGER,
            # Carried here too, and this one was genuinely missing: this set
            # named only the lower-case spelling, so a line manager holding the
            # "JARZ line manager" Role record — the capitalised one, which is
            # the spelling the mobile client matches on — could not raise an
            # item request at all. Every other tier check carries both.
            JARZ_LINE_MANAGER_ALT,
            PRODUCTION_OPERATOR,
            "POS User",
            "POS Manager",
        }
    )
    # Review the whole request queue and reject entries.  Requesters see only
    # their own branch's queue; this set sees and acts on everything.
    PURCHASE_REQUEST_REVIEW = PURCHASE | {JARZ_MANAGER}


# ── WebSocket / realtime event names ────────────────────────────────────
# Every name Flutter listens to must exist verbatim in the Flutter ``WsEvents``
# class.  The reverse does NOT hold and never has: this class carries events with
# no Dart counterpart (the TRIP_* and CUSTOM_SHIPPING_* families), and ``WsEvents``
# carries names with no Python counterpart.  The invariant is Python ⊆ Dart for
# listened events — not set equality.  Do not write an equality test.

class WS_EVENTS:
    NEW_INVOICE = "jarz_pos_new_invoice"
    INVOICE_STATE_CHANGE = "jarz_pos_invoice_state_change"
    KANBAN_UPDATE = "kanban_update"
    INVOICE_CANCELLED = "jarz_pos_invoice_cancelled"
    INVOICE_ACCEPTED = "jarz_pos_invoice_accepted"
    OUT_FOR_DELIVERY_TRANSITION = "jarz_pos_out_for_delivery_transition"
    COURIER_OUTSTANDING = "jarz_pos_courier_outstanding"
    COURIER_EXPENSE_PAID = "jarz_pos_courier_expense_paid"
    COURIER_SETTLED = "jarz_pos_courier_settled"
    SALES_PARTNER_COLLECT_PROMPT = "jarz_pos_sales_partner_collect_prompt"
    SALES_PARTNER_UNPAID_OFD = "jarz_pos_sales_partner_unpaid_ofd"
    SALES_PARTNER_PAID_OFD = "jarz_pos_sales_partner_paid_ofd"
    COURIER_EXPENSE_ONLY = "jarz_pos_courier_expense_only"
    SINGLE_COURIER_SETTLEMENT = "jarz_pos_single_courier_settlement"
    COURIER_COLLECTED_SETTLEMENT = "jarz_pos_courier_collected_settlement"
    TRIP_CREATED = "jarz_pos_trip_created"
    TRIP_OFD = "jarz_pos_trip_ofd"
    TRIP_COMPLETED = "jarz_pos_trip_completed"
    CUSTOM_SHIPPING_REQUESTED = "jarz_pos_custom_shipping_requested"
    CUSTOM_SHIPPING_APPROVED = "jarz_pos_custom_shipping_approved"
    CUSTOM_SHIPPING_REJECTED = "jarz_pos_custom_shipping_rejected"
    SHIFT_STARTED = "jarz_pos_shift_started"
    SHIFT_ENDED = "jarz_pos_shift_ended"
    # Courier app (see COURIER_CONTRACTS.md §7 — frozen 2026-08-05).
    COURIER_STOP_ARRIVED = "jarz_pos_courier_stop_arrived"
    COURIER_STOP_DELIVERED = "jarz_pos_courier_stop_delivered"
    COURIER_STOP_FAILED = "jarz_pos_courier_stop_failed"
    # Trip leg. Not a state change — the board does not move — so these are
    # separate events rather than another COURIER_STOP_* variant.
    COURIER_LEG_STARTED = "jarz_pos_courier_leg_started"
    COURIER_LEG_ENDED = "jarz_pos_courier_leg_ended"
    COURIER_DUTY_CHANGED = "jarz_pos_courier_duty_changed"
    COURIER_DEPOSIT_DECLARED = "jarz_pos_courier_deposit_declared"
    ADDRESS_PIN_UPDATED = "jarz_pos_address_pin_updated"
    # B2B printed-label stock running low (see services/label_stock.py).
    LABEL_STOCK_ALERT = "jarz_pos_label_stock_alert"
    TEST_EVENT = "test_event"


# ── Query limits ────────────────────────────────────────────────────────

class QUERY_LIMITS:
    GL_ENTRIES = 1000
    KANBAN_INVOICES = 5000
    TERRITORIES = 250
    DEFAULT_LIST = 100
    NOTIFICATIONS = 50
    SEARCH = 20
    RECENT = 10


# ── Status / document strings ──────────────────────────────────────────

class STATUS:
    DRAFT = "Draft"
    OPEN = "Open"
    PAID = "Paid"
    UNPAID = "Unpaid"
    CANCELLED = "Cancelled"
    SUBMITTED = "Submitted"
    RETURN = "Return"


# ── Payment modes ──────────────────────────────────────────────────────

class PAYMENT_MODES:
    CASH = "Cash"
    CASH_LOWER = "cash"
    ONLINE = "Online"
    ONLINE_LOWER = "online"


# ── Delivery groups ────────────────────────────────────────────────────

class DELIVERY_GROUPS:
    EMPLOYEE_GROUP = "Delivery"
    SUPPLIER_GROUP = "Delivery"


# ── Timing modes ───────────────────────────────────────────────────────

class TIMING_MODES:
    SAME_DAY = "Same Day"
    NEXT_DAY = "Next Day"


# ── Price lists ─────────────────────────────────────────────────────────

class PRICE_LISTS:
    STANDARD_BUYING = "Standard Buying"


# ── Default UOM ─────────────────────────────────────────────────────────

DEFAULT_UOM = "Nos"


# ── Voucher types ───────────────────────────────────────────────────────

class VOUCHER_TYPES:
    SALES_INVOICE = "Sales Invoice"
    JOURNAL_ENTRY = "Journal Entry"
    PAYMENT_ENTRY = "Payment Entry"
    POS_INVOICE = "POS Invoice"
    DELIVERY_NOTE = "Delivery Note"
