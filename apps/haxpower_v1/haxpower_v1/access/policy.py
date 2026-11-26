"""Employee capabilities and company-bound warehouse scopes."""

from enum import StrEnum

import frappe

from ..adapters.erpnext import warehouse as native
from ..catalog import validation
from ..catalog.dto import ACTIVE
from ..warehouse.persistence import LOCATION, PHYSICAL, WAREHOUSE

SCOPE = "HP V1 Warehouse Access"
SCOPE_ROW = "HP V1 Warehouse Access Row"


class Operation(StrEnum):
    PRODUCT_READ = "Product Read"
    PRODUCT_MAINTAIN = "Product Maintain"
    WAREHOUSE_READ = "Warehouse Read"
    WAREHOUSE_MAINTAIN = "Warehouse Maintain"
    INVENTORY_READ = "Inventory Read"
    RECEIVE = "Receive"
    ISSUE = "Issue"
    TRANSFER = "Transfer"
    COUNT = "Count"
    APPROVE = "Approve"
    CORRECTION_APPROVE = "Correction Approve"
    EXPORT = "Export"
    SYSTEM_MAINTAIN = "System Maintain"

    @property
    def role(self):
        return f"HP V1 {self.value}"


class Mode(StrEnum):
    ALL = "ALL"
    SELECTED = "SELECTED"
    NONE = "NONE"


def deny():
    frappe.throw("Haxpower access is denied.", frappe.PermissionError)


def active_actor(*, locked=False):
    user = frappe.session.user
    if not user or user == "Guest" or not frappe.db.get_value("User", user, "enabled", for_update=locked):
        deny()
    return user


def granted_operations(*, locked=False):
    actor = active_actor(locked=locked)
    assignments = frappe.qb.DocType("Has Role")
    roles = frappe.qb.DocType("Role")
    query = (
        frappe.qb.from_(assignments).join(roles).on(assignments.role == roles.name)
        .select(roles.name).where(
            (assignments.parenttype == "User") & (assignments.parentfield == "roles")
            & (assignments.parent == actor) & (roles.disabled == 0)
        )
    )
    enabled_roles = set((query.for_update() if locked else query).run(pluck=True))
    return frozenset(operation for operation in Operation if operation.role in enabled_roles)


def require_operation(operation: Operation, *, locked=False):
    if operation not in granted_operations(locked=locked):
        deny()


def _selected_warehouses(scope, *, locked=False):
    return frappe.db.get_values(SCOPE_ROW, {
        "parent": scope, "parenttype": SCOPE, "parentfield": "warehouses",
    }, "warehouse", pluck=True, for_update=locked)


def _scope(*, company=None, locked=False):
    scope = frappe.db.get_value(SCOPE, {"user": active_actor(locked=locked)}, ("name", "company", "mode"),
                                as_dict=True, for_update=locked)
    if scope and scope.mode in (Mode.ALL, Mode.SELECTED) and (company is None or scope.company == company):
        return scope


def _authorized_warehouses(*, company=None):
    if not (scope := _scope(company=company)):
        return {}
    filters = {"company": scope.company, "kind": PHYSICAL, "status": ACTIVE}
    if scope.mode == Mode.SELECTED:
        if not (selected := _selected_warehouses(scope.name)):
            return {}
        filters["name"] = ("in", selected)
    rows = frappe.get_all(WAREHOUSE, filters=filters,
                          fields=["name", "company", "kind", "native_parent"], order_by="code, name")
    return {
        row.name: row for row in rows
        if row.company == scope.company and native.operational_parent(row)
    }


def authorized_warehouse_ids(operation: Operation, *, company=None):
    require_operation(operation)
    return tuple(_authorized_warehouses(company=company))


def _record_id(value):
    # A malformed or unknown ID is indistinguishable from an inaccessible ID.
    if not isinstance(value, str) or not validation.IDENTIFIER.fullmatch(value):
        deny()
    return value


def require_warehouse_access(warehouse_id, operation: Operation):
    require_operation(operation)
    if not (warehouse := _authorized_warehouses().get(_record_id(warehouse_id))):
        deny()
    return warehouse


def _authorized_location(location_id, warehouses):
    location = frappe.db.get_value(
        LOCATION, {"name": _record_id(location_id), "status": ACTIVE},
        ("name", "warehouse", "native_warehouse"), as_dict=True,
    )
    if not location or not (warehouse := warehouses.get(location.warehouse)):
        deny()
    if not location_mapping_is_visible(location, warehouse):
        deny()
    return location


def location_mapping_is_visible(location, warehouse):
    return native.mapping_status(location, warehouse) == "Verified"


def require_location_access(location_id, operation: Operation):
    require_operation(operation)
    return _authorized_location(location_id, _authorized_warehouses())


def lock_execution_locations(company, location_ids):
    """Recheck scope and both physical mappings by current read under the stock guard."""
    if not (scope := _scope(company=company, locked=True)):
        deny()
    selected = set(_selected_warehouses(scope.name, locked=True)) if scope.mode == Mode.SELECTED else None
    locations = {}
    for location_id in sorted({_record_id(value) for value in location_ids}):
        row = frappe.db.get_value(LOCATION, location_id, ("name", "warehouse", "native_warehouse", "status"),
                                  as_dict=True, for_update=True)
        if not row or row.name != location_id or row.status != ACTIVE:
            deny()
        locations[location_id] = row
    warehouses = {}
    for warehouse_id in sorted({row.warehouse for row in locations.values()}):
        row = frappe.db.get_value(WAREHOUSE, warehouse_id, ("name", "company", "kind", "native_parent", "status"),
                                  as_dict=True, for_update=True)
        if (not row or row.company != company or row.kind != PHYSICAL or row.status != ACTIVE
                or (selected is not None and warehouse_id not in selected)):
            deny()
        warehouses[warehouse_id] = row
    for location in locations.values():
        if native.mapping_status(location, warehouses[location.warehouse], locked=True) != "Verified":
            deny()
    return locations


def require_transfer_access(source_warehouse_id, target_warehouse_id):
    """Check both sides of a cross-warehouse operation before future workflow checks."""
    require_operation(Operation.TRANSFER)
    allowed = _authorized_warehouses()
    source = _record_id(source_warehouse_id)
    target = _record_id(target_warehouse_id)
    if source == target or source not in allowed or target not in allowed:
        deny()


def ensure_schema():
    for operation in Operation:
        if not frappe.db.exists("Role", operation.role):
            frappe.get_doc(doctype="Role", role_name=operation.role, desk_access=0).insert(ignore_permissions=True)
