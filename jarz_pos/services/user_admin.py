"""User administration from the POS app: list, create, edit, disable, delete,
and set a new password for the site's system users.

Who may use it
--------------
The JARZ Manager, plus System Manager and Administrator (``ACCESS_ROLES``).
Nobody below the manager tier, not even a line manager, may reach any of it.

Rules this module enforces
--------------------------
* **Roles come from Role Profiles, never from direct grants.** Frappe v16
  rebuilds ``User.roles`` from ``User.role_profiles`` on every save, so a role
  added directly to a profile-driven account is silently dropped. This screen
  therefore edits the profile list and nothing else. An empty list is refused:
  ``populate_role_profile_roles`` does nothing when the list is empty, so
  "remove every profile" would keep every role the user had. That is the
  opposite of what the manager meant.
* **Privileged accounts are out of reach.** A user holding System Manager or
  Administrator can be listed and viewed by a JARZ Manager, but not changed.
  Otherwise a JARZ Manager could reset the owner's password and sign in as a
  System Manager. The same rule covers role profiles: one that grants a
  privileged role can only be assigned by a System Manager.
* **Nobody locks themselves out.** A caller cannot disable or delete their own
  account, or change their own role profiles.
* **Delete is for mistakes; disable is for leavers.** Frappe refuses to delete a
  user that other records link to (an Employee, a branch membership). That
  refusal is turned into a message that says to disable instead, and the
  request rolls back as a whole.
* **Disabling or resetting a password signs the user out everywhere.** A fired
  cashier's phone must not keep a live session.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional

import frappe
from frappe import _
from frappe.utils import cint, cstr

from jarz_pos.constants import ROLES

ACCESS_ROLES = {ROLES.JARZ_MANAGER, ROLES.SYSTEM_MANAGER, ROLES.ADMINISTRATOR}
PRIVILEGED_ROLES = {ROLES.SYSTEM_MANAGER, ROLES.ADMINISTRATOR}
STANDARD_USERS = {"Administrator", "Guest"}
MIN_PASSWORD_LENGTH = 8

MANAGER_MODULES = "Jarz POS Manager Modules"
STAFF_MODULES = "Jarz POS Staff Modules"

# The app's own tiers, highest first. It is only a label for the list, so an
# account is placed by the strongest role it holds.
_TIERS = (
    ("manager", {ROLES.JARZ_MANAGER}),
    ("line_manager", {ROLES.JARZ_LINE_MANAGER, ROLES.JARZ_LINE_MANAGER_ALT}),
    ("moderator", {"Moderator"}),
    ("b2b", {"B2B Sales Rep"}),
    ("production", {ROLES.PRODUCTION_OPERATOR}),
    ("staff", {"POS User"}),
)


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------


def _session_user() -> str:
    return frappe.session.user


def _roles(user: str) -> set:
    return set(frappe.get_roles(user) or [])


def has_access(user: Optional[str] = None) -> bool:
    user = user or _session_user()
    if user == ROLES.ADMINISTRATOR:
        return True
    return bool(_roles(user) & ACCESS_ROLES)


def ensure_access() -> None:
    if not has_access():
        frappe.throw(
            _("Not permitted: only a JARZ Manager can manage users."),
            frappe.PermissionError,
        )


def is_privileged_caller(user: Optional[str] = None) -> bool:
    user = user or _session_user()
    return user == ROLES.ADMINISTRATOR or bool(_roles(user) & PRIVILEGED_ROLES)


def _effective_roles(
    user: str,
    roles: Optional[Iterable[str]] = None,
    profiles: Optional[Iterable[str]] = None,
    definitions: Optional[Dict[str, List[str]]] = None,
) -> set:
    """What *user* holds now, plus what their Role Profiles grant now.

    ``Has Role`` alone can be stale: when a profile gains a role, Frappe pushes
    it to members from a background job. Every save here rebuilds roles from the
    profile, so judging a target by ``Has Role`` alone would let a manager take
    over an account that the very next save makes a System Manager.
    """
    held = set(roles) if roles is not None else _roles(user)
    if profiles is None:
        profiles = _profiles_by_user([user]).get(user, [])
    if definitions is None:
        definitions = _profile_roles()
    for profile in profiles:
        held |= set(definitions.get(profile, []))
    return held


def is_privileged_target(user: str, roles: Optional[Iterable[str]] = None, **kw) -> bool:
    if user in STANDARD_USERS:
        return True
    return bool(_effective_roles(user, roles, **kw) & PRIVILEGED_ROLES)


def can_modify(user: str, roles: Optional[Iterable[str]] = None, **kw) -> bool:
    """A System Manager may change anyone but the built-in accounts. A JARZ
    Manager may change anyone below the manager tier, plus their own name and
    mobile -- never a System Manager and never another JARZ Manager: one stolen
    manager session must not be able to reset every peer's password or lock
    them all out."""
    caller = _session_user()
    if user in STANDARD_USERS:
        return False
    if is_privileged_caller(caller):
        return True
    effective = _effective_roles(user, roles, **kw)
    if effective & PRIVILEGED_ROLES:
        return False
    if user != caller and ROLES.JARZ_MANAGER in effective:
        return False
    return True


def _ensure_can_modify(user: str) -> Dict[str, Any]:
    """Resolve *user* and assert the caller may change it."""
    ensure_access()
    user = cstr(user).strip()
    if not user:
        frappe.throw(_("User is required."))
    row = frappe.db.get_value(
        "User", user, ["name", "full_name", "enabled", "user_type"], as_dict=True
    )
    if not row:
        frappe.throw(_("No such user: {0}").format(user), frappe.DoesNotExistError)
    # The lookup is case-insensitive ("guest" finds Guest), so every check
    # below runs on the canonical name, never on what the client sent.
    if row.name in STANDARD_USERS:
        frappe.throw(_("{0} is a built-in account and cannot be changed here.").format(row.name))
    if is_privileged_target(row.name) and not is_privileged_caller():
        frappe.throw(
            _("{0} is a system administrator account. Only a System Manager can change it.").format(
                row.full_name or row.name
            ),
            frappe.PermissionError,
        )
    if not can_modify(row.name):
        frappe.throw(
            _("{0} is a JARZ Manager. Only a System Manager can change another manager's account.").format(
                row.full_name or row.name
            ),
            frappe.PermissionError,
        )
    return row


def _ensure_not_self(user: str, action: str) -> None:
    if user == _session_user():
        frappe.throw(
            _("You cannot {0} your own account. Ask another manager.").format(action),
            frappe.PermissionError,
        )


# ---------------------------------------------------------------------------
# Role profiles
# ---------------------------------------------------------------------------


def _profile_roles() -> Dict[str, List[str]]:
    rows = frappe.get_all(
        "Has Role",
        filters={"parenttype": "Role Profile"},
        fields=["parent", "role"],
        limit_page_length=0,
    )
    out: Dict[str, List[str]] = {name: [] for name in frappe.get_all("Role Profile", pluck="name")}
    for r in rows:
        if r.parent in out:
            out[r.parent].append(r.role)
    return {name: sorted(roles) for name, roles in out.items()}


def list_role_profiles() -> List[Dict[str, Any]]:
    privileged_caller = is_privileged_caller()
    profiles = []
    for name, roles in sorted(_profile_roles().items()):
        privileged = bool(set(roles) & PRIVILEGED_ROLES)
        profiles.append(
            {
                "name": name,
                "roles": roles,
                "tier": _tier(roles),
                "privileged": privileged,
                "assignable": privileged_caller or not privileged,
            }
        )
    # Jarz's own four first, strongest first; everything else after, by name.
    order = {"Jarz POS Manager": 0, "Jarz POS Line Manager": 1, "Jarz POS Moderator": 2, "Jarz POS Staff": 3}
    profiles.sort(key=lambda p: (order.get(p["name"], 99), p["name"]))
    return profiles


def _parse_profiles(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            value = json.loads(text)
        except ValueError:
            value = [part for part in text.split(",")]
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        frappe.throw(_("Role profiles must be a list of names."))
    seen: List[str] = []
    for item in value or []:
        name = cstr(item.get("role_profile") if isinstance(item, dict) else item).strip()
        if name and name not in seen:
            seen.append(name)
    return seen


def _validate_profiles(names: List[str]) -> Dict[str, List[str]]:
    if not names:
        frappe.throw(_("Choose at least one role profile."))
    available = _profile_roles()
    privileged_caller = is_privileged_caller()
    chosen: Dict[str, List[str]] = {}
    for name in names:
        if name not in available:
            frappe.throw(_("Role profile {0} does not exist.").format(name))
        if set(available[name]) & PRIVILEGED_ROLES and not privileged_caller:
            frappe.throw(
                _("Role profile {0} grants system administrator rights. Only a System Manager can assign it.").format(name),
                frappe.PermissionError,
            )
        chosen[name] = available[name]
    return chosen


def _module_profile_for(roles: Iterable[str]) -> Optional[str]:
    target = MANAGER_MODULES if ROLES.JARZ_MANAGER in set(roles) else STAFF_MODULES
    return target if frappe.db.exists("Module Profile", target) else None


def _apply_profiles(doc, names: List[str]) -> None:
    chosen = _validate_profiles(names)
    doc.set("role_profiles", [{"role_profile": n} for n in names])
    # The old single-value field is folded into the table on validate; clear it
    # so a stale value cannot re-add a profile the manager just removed.
    doc.role_profile_name = None
    granted = {role for roles in chosen.values() for role in roles}
    module_profile = _module_profile_for(granted)
    if module_profile:
        doc.module_profile = module_profile


def _tier(roles: Iterable[str]) -> str:
    held = set(roles)
    for tier, markers in _TIERS:
        if held & markers:
            return tier
    return "other"


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def _roles_by_user(users: List[str]) -> Dict[str, List[str]]:
    if not users:
        return {}
    rows = frappe.get_all(
        "Has Role",
        filters={"parenttype": "User", "parent": ["in", users]},
        fields=["parent", "role"],
        limit_page_length=0,
    )
    out: Dict[str, List[str]] = {u: [] for u in users}
    for r in rows:
        out.setdefault(r.parent, []).append(r.role)
    return out


def _profiles_by_user(users: List[str]) -> Dict[str, List[str]]:
    if not users:
        return {}
    rows = frappe.get_all(
        "User Role Profile",
        filters={"parenttype": "User", "parent": ["in", users]},
        fields=["parent", "role_profile", "idx"],
        order_by="idx asc",
        limit_page_length=0,
    )
    out: Dict[str, List[str]] = {u: [] for u in users}
    for r in rows:
        out.setdefault(r.parent, []).append(r.role_profile)
    return out


def _branches_by_user(users: List[str]) -> Dict[str, List[str]]:
    if not users:
        return {}
    rows = frappe.get_all(
        "POS Profile User",
        filters={"parenttype": "POS Profile", "user": ["in", users]},
        fields=["parent", "user"],
        limit_page_length=0,
    )
    out: Dict[str, List[str]] = {u: [] for u in users}
    for r in rows:
        if r.parent not in out.setdefault(r.user, []):
            out[r.user].append(r.parent)
    return {u: sorted(b) for u, b in out.items()}


def _employees_by_user(users: List[str]) -> Dict[str, Dict[str, Any]]:
    if not users:
        return {}
    rows = frappe.get_all(
        "Employee",
        filters={"user_id": ["in", users]},
        fields=["name", "employee_name", "branch", "user_id", "status"],
        limit_page_length=0,
    )
    return {r.user_id: r for r in rows}


def _shape(row: Dict[str, Any], roles, profiles, branches, employee, definitions=None) -> Dict[str, Any]:
    caller = _session_user()
    definitions = definitions if definitions is not None else _profile_roles()
    privileged = is_privileged_target(row["name"], roles, profiles=profiles, definitions=definitions)
    return {
        "name": row["name"],
        "email": row.get("email") or row["name"],
        "full_name": row.get("full_name") or row["name"],
        "first_name": row.get("first_name"),
        "last_name": row.get("last_name"),
        "mobile_no": row.get("mobile_no"),
        "enabled": cint(row.get("enabled")),
        "require_pos_shift": cint(row.get("custom_require_pos_shift")),
        "last_login": cstr(row.get("last_login") or "") or None,
        "last_active": cstr(row.get("last_active") or "") or None,
        "creation": cstr(row.get("creation") or "") or None,
        "role_profiles": profiles,
        "roles": sorted(roles),
        "tier": _tier(roles),
        "branches": branches,
        "employee": employee.get("name") if employee else None,
        "employee_name": employee.get("employee_name") if employee else None,
        "employee_branch": employee.get("branch") if employee else None,
        "is_privileged": privileged,
        "is_self": row["name"] == caller,
        "can_edit": can_modify(row["name"], roles, profiles=profiles, definitions=definitions),
    }


_USER_FIELDS = [
    "name",
    "email",
    "full_name",
    "first_name",
    "last_name",
    "mobile_no",
    "enabled",
    "custom_require_pos_shift",
    "last_login",
    "last_active",
    "creation",
]


def _user_fields() -> List[str]:
    # The custom field ships with jarz_pos, but a list must not break on a site
    # that has not migrated yet.
    if frappe.get_meta("User").has_field("custom_require_pos_shift"):
        return _USER_FIELDS
    return [f for f in _USER_FIELDS if f != "custom_require_pos_shift"]


def list_users(search: Optional[str] = None, include_disabled: Any = 1) -> List[Dict[str, Any]]:
    ensure_access()
    filters: Dict[str, Any] = {"user_type": "System User", "name": ["not in", list(STANDARD_USERS)]}
    if not cint(include_disabled):
        filters["enabled"] = 1
    or_filters = None
    text = cstr(search).strip()
    if text:
        like = f"%{text}%"
        or_filters = {"name": ["like", like], "full_name": ["like", like], "mobile_no": ["like", like]}
    rows = frappe.get_all(
        "User",
        filters=filters,
        or_filters=or_filters,
        fields=_user_fields(),
        order_by="enabled desc, full_name asc",
        limit_page_length=0,
    )
    users = [r.name for r in rows]
    roles = _roles_by_user(users)
    profiles = _profiles_by_user(users)
    branches = _branches_by_user(users)
    employees = _employees_by_user(users)
    definitions = _profile_roles()
    return [
        _shape(
            r,
            roles.get(r.name, []),
            profiles.get(r.name, []),
            branches.get(r.name, []),
            employees.get(r.name),
            definitions,
        )
        for r in rows
    ]


def get_user(user: str) -> Dict[str, Any]:
    ensure_access()
    user = cstr(user).strip()
    row = frappe.db.get_value("User", user, _user_fields(), as_dict=True) if user else None
    if not row or row.name in STANDARD_USERS:
        frappe.throw(_("No such user: {0}").format(user), frappe.DoesNotExistError)
    user = row.name
    return _shape(
        row,
        _roles_by_user([user]).get(user, []),
        _profiles_by_user([user]).get(user, []),
        _branches_by_user([user]).get(user, []),
        _employees_by_user([user]).get(user),
    )


def get_context() -> Dict[str, Any]:
    ensure_access()
    return {
        "can_manage": True,
        "is_system_manager": is_privileged_caller(),
        "min_password_length": MIN_PASSWORD_LENGTH,
        "role_profiles": list_role_profiles(),
    }


def list_employees(search: Optional[str] = None) -> List[Dict[str, Any]]:
    """Active employees for the link picker, with whoever they are linked to now."""
    ensure_access()
    or_filters = None
    text = cstr(search).strip()
    if text:
        like = f"%{text}%"
        or_filters = {"name": ["like", like], "employee_name": ["like", like]}
    return frappe.get_all(
        "Employee",
        filters={"status": "Active"},
        or_filters=or_filters,
        fields=["name", "employee_name", "branch", "user_id"],
        order_by="employee_name asc",
        limit_page_length=50,
    )


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def _validate_password(password: Any) -> str:
    password = cstr(password)
    if len(password) < MIN_PASSWORD_LENGTH:
        frappe.throw(
            _("The password must be at least {0} characters.").format(MIN_PASSWORD_LENGTH)
        )
    return password


def _clean_mobile(value: Any) -> Optional[str]:
    text = cstr(value).strip()
    return text or None


def _link_employee(user: str, employee: Optional[str]) -> None:
    """Point *employee* at *user*, and unlink whichever employee pointed there before.

    Written with ``db_set`` rather than ``Employee.save()``: saving an Employee
    runs HRMS's ``update_user_permissions``, which can create a User Permission
    that pins the account to its own Employee record, and that has locked
    cashiers out of customers before.
    """
    employee = cstr(employee).strip() or None
    current = frappe.get_all("Employee", filters={"user_id": user}, pluck="name")
    if employee:
        row = frappe.db.get_value("Employee", employee, ["name", "user_id", "employee_name"], as_dict=True)
        if not row:
            frappe.throw(_("Employee {0} does not exist.").format(employee))
        if row.user_id and row.user_id != user:
            frappe.throw(
                _("{0} is already linked to the account {1}. Unlink it there first.").format(
                    row.employee_name or employee, row.user_id
                )
            )
    for name in current:
        if name != employee:
            frappe.db.set_value("Employee", name, "user_id", None)
            _audit_employee(name, _("Unlinked from user {0} by {1} (Users screen).").format(user, _session_user()))
    if employee and employee not in current:
        frappe.db.set_value("Employee", employee, "user_id", user)
        _audit_employee(employee, _("Linked to user {0} by {1} (Users screen).").format(user, _session_user()))


def _audit_employee(employee: str, text: str) -> None:
    # db.set_value writes no Version row, so the move is recorded here instead:
    # who an Employee resolves to decides branch, custody and courier identity.
    frappe.get_doc(
        {
            "doctype": "Comment",
            "comment_type": "Info",
            "reference_doctype": "Employee",
            "reference_name": employee,
            "content": text,
        }
    ).insert(ignore_permissions=True)


def _sign_out_everywhere(user: str) -> None:
    try:
        from frappe.sessions import clear_sessions

        clear_sessions(user=user, force=True)
    except Exception:
        frappe.log_error(title="user_admin: clear_sessions failed", message=frappe.get_traceback())


def create_user(
    email: str,
    first_name: str,
    password: str,
    role_profiles: Any,
    last_name: Optional[str] = None,
    mobile_no: Optional[str] = None,
    require_pos_shift: Any = 0,
    employee: Optional[str] = None,
) -> Dict[str, Any]:
    ensure_access()
    email = cstr(email).strip().lower()
    first_name = cstr(first_name).strip()
    if not email:
        frappe.throw(_("Email is required."))
    if not first_name:
        frappe.throw(_("First name is required."))
    frappe.utils.validate_email_address(email, throw=True)
    if frappe.db.exists("User", email):
        frappe.throw(_("A user with the email {0} already exists.").format(email), frappe.DuplicateEntryError)
    password = _validate_password(password)

    doc = frappe.new_doc("User")
    doc.email = email
    doc.first_name = first_name
    doc.last_name = cstr(last_name).strip() or None
    doc.mobile_no = _clean_mobile(mobile_no)
    doc.enabled = 1
    doc.user_type = "System User"
    doc.send_welcome_email = 0
    if doc.meta.has_field("custom_require_pos_shift"):
        doc.custom_require_pos_shift = cint(require_pos_shift)
    _apply_profiles(doc, _parse_profiles(role_profiles))
    doc.new_password = password
    doc.flags.ignore_permissions = True
    doc.insert(ignore_permissions=True)

    if employee:
        _link_employee(doc.name, employee)
    return get_user(doc.name)


def update_user(
    user: str,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    mobile_no: Optional[str] = None,
    role_profiles: Any = None,
    require_pos_shift: Any = None,
    employee: Any = None,
    clear_employee: Any = 0,
) -> Dict[str, Any]:
    """Change only what was sent: ``None`` leaves a field as it is."""
    row = _ensure_can_modify(user)
    doc = frappe.get_doc("User", row.name)
    if first_name is not None:
        first_name = cstr(first_name).strip()
        if not first_name:
            frappe.throw(_("First name is required."))
        doc.first_name = first_name
    if last_name is not None:
        doc.last_name = cstr(last_name).strip() or None
    if mobile_no is not None:
        doc.mobile_no = _clean_mobile(mobile_no)
    if require_pos_shift is not None and doc.meta.has_field("custom_require_pos_shift"):
        if cint(require_pos_shift) != cint(doc.get("custom_require_pos_shift")):
            _ensure_not_self(doc.name, _("change the shift requirement of"))
        doc.custom_require_pos_shift = cint(require_pos_shift)
    if role_profiles is not None:
        names = _parse_profiles(role_profiles)
        current = [r.role_profile for r in doc.get("role_profiles") or []]
        if names != current:
            _ensure_not_self(doc.name, _("change the role profiles of"))
            _apply_profiles(doc, names)
    doc.flags.ignore_permissions = True
    doc.save(ignore_permissions=True)

    if cint(clear_employee) or (employee is not None and cstr(employee).strip()):
        # Taking a cashier's Employee record would take their branch, custody
        # and courier identity with it.
        _ensure_not_self(doc.name, _("change the employee link of"))
    if cint(clear_employee):
        _link_employee(doc.name, None)
    elif employee is not None and cstr(employee).strip():
        _link_employee(doc.name, employee)
    return get_user(doc.name)


def set_enabled(user: str, enabled: Any) -> Dict[str, Any]:
    row = _ensure_can_modify(user)
    enabled = cint(enabled)
    if not enabled:
        _ensure_not_self(row.name, _("disable"))
    doc = frappe.get_doc("User", row.name)
    if cint(doc.enabled) != enabled:
        doc.enabled = enabled
        doc.flags.ignore_permissions = True
        doc.save(ignore_permissions=True)
    if not enabled:
        _sign_out_everywhere(row.name)
    return get_user(row.name)


def reset_password(user: str, new_password: str, sign_out: Any = 1) -> Dict[str, Any]:
    """Set a new password. Anyone else is ALWAYS signed out everywhere: a reset
    usually means the old password leaked, and a live session is exactly what
    the thief holds. ``sign_out`` is accepted for old clients and ignored."""
    row = _ensure_can_modify(user)
    password = _validate_password(new_password)
    doc = frappe.get_doc("User", row.name)
    doc.new_password = password
    doc.flags.ignore_permissions = True
    doc.save(ignore_permissions=True)
    if row.name != _session_user():
        _sign_out_everywhere(row.name)
    return {"ok": True, "user": row.name}


def delete_user(user: str) -> Dict[str, Any]:
    row = _ensure_can_modify(user)
    _ensure_not_self(row.name, _("delete"))
    # User.on_trash deletes ToDos, shares and permissions BEFORE Frappe checks
    # links, so a refused delete must roll those back too.
    frappe.db.savepoint("user_admin_delete")
    try:
        frappe.delete_doc("User", row.name, ignore_permissions=True)
    except frappe.LinkExistsError:
        frappe.db.rollback(save_point="user_admin_delete")
        frappe.throw(
            _(
                "{0} is used by other records (orders, an employee or a branch), so the account cannot be deleted. Disable it instead."
            ).format(row.full_name or row.name),
            title=_("Cannot Delete"),
        )
    return {"ok": True, "deleted": row.name}
