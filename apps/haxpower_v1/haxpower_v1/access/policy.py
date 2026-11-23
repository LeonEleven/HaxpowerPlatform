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


def active_actor():
    user = frappe.session.user
    if not user or user == "Guest" or not frappe.db.get_value("User", user, "enabled"):
        deny()
    return user


def granted_operations():
    actor = active_actor()
    assignments = frappe.qb.DocType("Has Role")
    roles = frappe.qb.DocType("Role")
    enabled_roles = set(
        frappe.qb.from_(assignments).join(roles).on(assignments.role == roles.name)
        .select(roles.name).where(
            (assignments.parenttype == "User") & (assignments.parentfield == "roles")
            & (assignments.parent == actor) & (roles.disabled == 0)
        ).run(pluck=True)
    )
    return frozenset(operation for operation in Operation if operation.role in enabled_roles)


def require_operation(operation: Operation):
    if operation not in granted_operations():
        deny()


def _selected_warehouses(scope):
    return frappe.get_all(SCOPE_ROW, filters={
        "parent": scope, "parenttype": SCOPE, "parentfield": "warehouses",
    }, pluck="warehouse")


def _authorized_warehouses(*, company=None):
    scope = frappe.db.get_value(SCOPE, {"user": active_actor()}, ("name", "company", "mode"), as_dict=True)
    if not scope or scope.mode not in (Mode.ALL, Mode.SELECTED):
        return {}
    if company is not None and scope.company != company:
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
