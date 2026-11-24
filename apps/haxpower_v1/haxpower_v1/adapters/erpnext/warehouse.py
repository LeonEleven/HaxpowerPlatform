"""ERPNext v16 Warehouse mapping and lifecycle guards."""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.utils import cint

from ...access.execution import allows_native_write, native_write
from ...warehouse.dto import MappingDiagnostic
from ...warehouse.persistence import LOCATION, TRANSIT, WAREHOUSE, require_maintainer, require_system_operator

NATIVE = "Warehouse"
MARKER = "custom_hp_v1_location_id"
PERMLEVEL = 2
PARENT_FIELDS = ("company", "warehouse_name", "parent_warehouse", "is_group", "disabled", "warehouse_type", "account")
COMPANY_FIELDS = ("company_name", "abbr", "is_group", "docstatus")
REFERENCES = (
    ("Stock Entry", "from_warehouse"),
    ("Stock Entry", "to_warehouse"),
    ("Stock Entry Detail", "s_warehouse"),
    ("Stock Entry Detail", "t_warehouse"),
    ("Stock Reconciliation Item", "warehouse"),
    ("Stock Reservation Entry", "warehouse"),
    ("Material Request Item", "warehouse"),
    ("Material Request Item", "from_warehouse"),
    ("Purchase Order Item", "warehouse"),
    ("Purchase Receipt Item", "warehouse"),
    ("Sales Order Item", "warehouse"),
    ("Delivery Note Item", "warehouse"),
    ("Item Default", "default_warehouse"),
)
WAREHOUSE_SERVICE = "Warehouse mapping"


def ensure_schema():
    create_custom_fields({NATIVE: [{
        "fieldname": MARKER, "label": "Haxpower V1 Location ID", "fieldtype": "Data",
        "length": 32, "permlevel": PERMLEVEL, "hidden": 1, "read_only": 1, "no_copy": 1,
        "search_index": 1, "insert_after": "warehouse_name",
    }]}, update=False)
    field = frappe.get_meta(NATIVE).get_field(MARKER)
    if (
        not field or field.fieldtype != "Data" or cint(field.length) != 32
        or cint(field.permlevel) != PERMLEVEL
        or not all(field.get(key) for key in ("hidden", "read_only", "no_copy", "search_index"))
        or not frappe.db.has_column(NATIVE, MARKER)
    ):
        frappe.throw("Incorrect Haxpower location marker schema on ERPNext Warehouse.")


def _company(name, *, locked=False):
    company = frappe.db.get_value(
        "Company", name, ("name", "is_group", "docstatus"), as_dict=True, for_update=locked,
    )
    # Company has no standard disabled field in this ERPNext release.
    if company and company.name == name and not company.is_group and company.docstatus == 0:
        return company


def _parent(name, company, *, locked=False):
    parent = frappe.db.get_value(
        NATIVE, name, ("name", "company", "is_group", "disabled", "parent_warehouse", "warehouse_type",
                       "lft", "rgt"), as_dict=True, for_update=locked,
    )
    if (
        parent and parent.name == name and parent.company == company and parent.is_group
        and not parent.disabled and not parent.parent_warehouse and parent.warehouse_type != TRANSIT
        and parent.lft and parent.rgt > parent.lft
    ):
        return parent


def operational_parent(warehouse):
    return _company(warehouse.company) and _parent(warehouse.native_parent, warehouse.company)


def validate_parent(company, parent_name):
    if not _company(company, locked=True):
        frappe.throw("Warehouse requires an existing operational ERPNext Company.")
    if not (parent := _parent(parent_name, company, locked=True)):
        frappe.throw("Select an enabled top-level native group Warehouse in the same Company.")
    return parent


def _leaf_matches(leaf, warehouse, parent):
    return (
        leaf.company == warehouse.company and leaf.parent_warehouse == warehouse.native_parent
        and not leaf.is_group and not leaf.disabled
        and (leaf.warehouse_type == TRANSIT) == (warehouse.kind == TRANSIT)
        and leaf.lft and leaf.rgt == leaf.lft + 1
        and parent.lft < leaf.lft < leaf.rgt < parent.rgt
    )


def _allow_binding(name, location_id, warehouse, *, existing):
    authorize = require_system_operator if warehouse.kind == TRANSIT else require_maintainer
    return native_write(
        authorize, service=WAREHOUSE_SERVICE, native_name=name, domain_id=location_id, existing=existing,
    )


def _native(name, *, locked=False):
    if frappe.db.get_value(NATIVE, name, for_update=locked) == name:
        return frappe.get_doc(NATIVE, name, for_update=locked)


def bind_location(warehouse, location_id, existing_name=None):
    parent = validate_parent(warehouse.company, warehouse.native_parent)
    if warehouse.kind == TRANSIT and frappe.db.get_value("Warehouse Type", TRANSIT, for_update=True) != TRANSIT:
        frappe.throw("ERPNext must have its standard Transit Warehouse Type before provisioning system transit.")
    if existing_name is None:
        leaf = frappe.get_doc(
            doctype=NATIVE, warehouse_name=f"HPV1-{location_id}", company=warehouse.company,
            parent_warehouse=parent.name, is_group=0, disabled=0,
            warehouse_type=TRANSIT if warehouse.kind == TRANSIT else None,
            **{MARKER: location_id},
        )
        leaf.flags.notify_update = False
        with _allow_binding(leaf.warehouse_name, location_id, warehouse, existing=False):
            leaf.insert(ignore_permissions=True)
        # NestedSet updates its bounds in the database during insertion.
        leaf = _native(leaf.name, locked=True)
    else:
        leaf = _native(existing_name, locked=True)
        if not leaf or not _leaf_matches(leaf, warehouse, parent):
            frappe.throw("ERPNext Warehouse must be an enabled leaf of the required company, parent and type.")
        if leaf.get(MARKER) or _forward_mapping(leaf.name, locked=True):
            frappe.throw("This ERPNext Warehouse is already mapped to a location.")
        check_references(leaf.name)
        leaf.set(MARKER, location_id)
        leaf.flags.notify_update = False
        with _allow_binding(leaf.name, location_id, warehouse, existing=True):
            leaf.save(ignore_permissions=True)

    parent = validate_parent(warehouse.company, warehouse.native_parent)
    if not leaf or leaf.get(MARKER) != location_id or not _leaf_matches(leaf, warehouse, parent):
        frappe.throw("ERPNext did not persist a consistent leaf Warehouse mapping.")
    return leaf.name


def mapping_status(location, warehouse, *, locked=False):
    leaf = _native(location.native_warehouse, locked=locked)
    if not leaf:
        return "Missing Warehouse"
    if not _company(warehouse.company, locked=locked):
        return "Invalid Company"
    parent = _parent(warehouse.native_parent, warehouse.company, locked=locked)
    if not parent:
        return "Invalid Parent"
    markers = frappe.db.get_values(NATIVE, {MARKER: location.name}, "name", for_update=locked)
    if len(markers) > 1:
        return "Duplicate Marker"
    if leaf.get(MARKER) != location.name or not _leaf_matches(leaf, warehouse, parent):
        return "Warehouse Drift"
    return "Verified"


def _forward_mapping(name, *, locked=False):
    return frappe.db.get_value(LOCATION, {"native_warehouse": name}, ("name", "warehouse"),
                               as_dict=True, for_update=locked)


def mapping_for_native(name):
    location = _forward_mapping(name)
    if location:
        doc = frappe.get_doc(LOCATION, location.name)
        warehouse = frappe.get_doc(WAREHOUSE, location.warehouse)
        return MappingDiagnostic(doc.name, name, mapping_status(doc, warehouse))
    leaf = _native(name)
    if not leaf:
        return MappingDiagnostic(None, name, "Missing Warehouse")
    if leaf.get(MARKER):
        return MappingDiagnostic(None, name, "Unmatched Marker")
    return MappingDiagnostic(None, name, "Unmanaged")


def check_references(name):
    if frappe.db.get_value("Bin", {"warehouse": name, "actual_qty": ("!=", 0)}, for_update=True):
        frappe.throw("The ERPNext Warehouse has non-zero stock.")
    if frappe.db.get_value("Stock Ledger Entry", {"warehouse": name}, for_update=True):
        frappe.throw("The ERPNext Warehouse has stock ledger history.")
    if frappe.db.get_value(NATIVE, {"parent_warehouse": name}, for_update=True):
        frappe.throw("The ERPNext Warehouse has child warehouses.")
    if frappe.db.get_value("DocShare", {"share_doctype": NATIVE, "share_name": name}, for_update=True):
        frappe.throw("The ERPNext Warehouse has a native sharing grant.")
    for doctype, field in REFERENCES:
        if frappe.db.get_value(doctype, {field: name, "docstatus": ("<", 2)}, for_update=True):
            frappe.throw(f"The ERPNext Warehouse has a native reference in {doctype}.")


def managed_warehouse(name, *, locked=False):
    return bool(name and (
        (frappe.db.has_column(NATIVE, MARKER) and frappe.db.get_value(NATIVE, name, MARKER, for_update=locked))
        or (frappe.db.table_exists(LOCATION) and _forward_mapping(name, locked=locked))
    ))


def _used_parent(name):
    return name and frappe.db.get_value(WAREHOUSE, {"native_parent": name}, for_update=True)


def _deny_warehouse_change():
    frappe.throw("Managed ERPNext Warehouse mappings can only be changed by the V1 warehouse service.",
                 frappe.PermissionError)


def guard_change(doc, method=None):
    existing = not doc.is_new()
    if allows_native_write(
        WAREHOUSE_SERVICE, doc.name if existing else doc.warehouse_name, doc.get(MARKER), existing=existing,
    ):
        return
    if doc.get(MARKER) or (existing and managed_warehouse(doc.name, locked=True)):
        _deny_warehouse_change()
    if existing and any(doc.has_value_changed(field) for field in PARENT_FIELDS) and _used_parent(doc.name):
        _deny_warehouse_change()


def guard_rename(doc, method, old, new, merge=False):
    for name in (old, new) if merge else (old,):
        if managed_warehouse(name, locked=True) or _used_parent(name):
            _deny_warehouse_change()


class WarehouseLifecycle:
    def on_trash(self):
        # Execute before ERPNext's own deletion cleanup touches Bin or the tree.
        if managed_warehouse(self.name, locked=True) or _used_parent(self.name):
            _deny_warehouse_change()
        super().on_trash()


def guard_company_change(doc, method=None):
    if (
        not doc.is_new() and any(doc.has_value_changed(field) for field in COMPANY_FIELDS)
        and frappe.db.get_value(WAREHOUSE, {"company": doc.name}, for_update=True)
    ):
        frappe.throw("Company identity must be retained while it owns V1 warehouses.", frappe.PermissionError)


def guard_company_identity(doc, method, old, new, merge=False):
    for name in (old, new) if merge else (old,):
        if (frappe.db.get_value("Company", name, for_update=True)
                and frappe.db.get_value(WAREHOUSE, {"company": name}, for_update=True)):
            frappe.throw("Company identity must be retained while it owns V1 warehouses.", frappe.PermissionError)


class CompanyLifecycle:
    def on_trash(self):
        guard_company_identity(self, "on_trash", self.name, None)
        super().on_trash()


def guard_warehouse_type_identity(doc, method, old, new, merge=False):
    for name in (old, new) if merge else (old,):
        if frappe.db.get_value("Warehouse Type", name, for_update=True):
            leaves = frappe.db.get_values(NATIVE, {"warehouse_type": name}, "name", for_update=True)
            if any(managed_warehouse(leaf, locked=True) or _used_parent(leaf) for leaf in leaves):
                _deny_warehouse_change()


def guard_warehouse_type_trash(doc, method=None):
    guard_warehouse_type_identity(doc, method, doc.name, None)


def native_warehouse_permission(doc, user=None):
    return (user or frappe.session.user) == "Administrator" or not managed_warehouse(doc.name)


def native_warehouse_query(user=None):
    if (user or frappe.session.user) == "Administrator":
        return ""
    return (
        f"(coalesce(`tabWarehouse`.`{MARKER}`, '') = '' and not exists "
        f"(select 1 from `tab{LOCATION}` as hp_location "
        "where hp_location.native_warehouse = `tabWarehouse`.name))"
    )


def _reject_transit(name):
    # V1-04 provisions transit atomically and never adopts an existing transit
    # leaf. Unrelated native stock posting does not need mapping locks.
    if name and frappe.db.table_exists(LOCATION) and (location := _forward_mapping(name)):
        if frappe.db.get_value(WAREHOUSE, location.warehouse, "kind") == TRANSIT:
            frappe.throw("V1 system transit requires the controlled transfer service.",
                         frappe.PermissionError)


def guard_stock_entry(doc, method=None):
    from .stock_entry import allows_dispatch_write

    if allows_dispatch_write(doc, method):
        return
    names = {doc.from_warehouse, doc.to_warehouse}
    for row in doc.items:
        names.update((row.s_warehouse, row.t_warehouse))
    for name in names:
        _reject_transit(name)


def guard_transit_posting(doc, method=None):
    from .stock_entry import allows_dispatch_write

    if not allows_dispatch_write(doc, method):
        _reject_transit(doc.warehouse)
