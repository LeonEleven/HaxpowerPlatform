"""Internal maintenance and stable-ID reads for V1 warehouse master data."""

from uuid import uuid4

import frappe

from ..adapters.erpnext import warehouse as native
from ..catalog import validation as check
from ..catalog.dto import ACTIVE
from .dto import Location, MappingDiagnostic, Warehouse
from .persistence import (
    LOCATION, PHYSICAL, TRANSIT, WAREHOUSE, require_maintainer, require_system_operator, warehouse_write,
)


def _warehouse(record_id, *, system=False, locked=False):
    record = frappe.get_doc(WAREHOUSE, check.identifier(record_id, "Warehouse ID"), for_update=locked)
    if not system and record.kind != PHYSICAL:
        frappe.throw("This warehouse is reserved for system transit.", frappe.PermissionError)
    return record


def _location(record_id, *, system=False, locked=False):
    record = frappe.get_doc(LOCATION, check.identifier(record_id, "Location ID"), for_update=locked)
    return record, _warehouse(record.warehouse, system=system, locked=locked)


def _warehouse_dto(doc):
    return Warehouse(doc.name, doc.company, doc.code, doc.display_name, doc.description or "",
                     doc.status, doc.kind, doc.revision)


def _location_dto(doc):
    return Location(doc.name, doc.warehouse, doc.code, doc.display_name, doc.description or "",
                    doc.status, doc.revision)


def _description(display_name, description):
    return {
        "display_name": check.text(display_name, "Display name", 140, required=True),
        "description": check.text(description, "Description", 4000),
    }


def _new_warehouse(company, native_parent, code, display_name, description, kind):
    company = check.text(company, "Company", 140, required=True)
    native_parent = check.text(native_parent, "Native parent Warehouse", 140, required=True)
    code = check.key(code, "Warehouse code")
    native.validate_parent(company, native_parent)
    if frappe.db.get_value(WAREHOUSE, {"company": company, "code": code}, for_update=True):
        frappe.throw("Warehouse code already exists within the company.")
    doc = frappe.get_doc(doctype=WAREHOUSE, company=company, native_parent=native_parent, code=code,
                         status=ACTIVE, kind=kind, revision=1, **_description(display_name, description))
    return doc.insert(ignore_permissions=True, set_name=uuid4().hex)


def create_warehouse(*, company, native_parent, code, display_name, description="") -> Warehouse:
    require_maintainer()
    with warehouse_write():
        return _warehouse_dto(_new_warehouse(company, native_parent, code, display_name, description, PHYSICAL))


def _new_location(warehouse, code, display_name, description="", existing_native=None):
    _require_active(warehouse)
    code = check.key(code, "Location code")
    fields = _description(display_name, description)
    if frappe.db.get_value(LOCATION, {"warehouse": warehouse.name, "code": code}, for_update=True):
        frappe.throw("Location code already exists within the warehouse.")
    location_id = uuid4().hex
    if existing_native is not None:
        existing_native = check.text(existing_native, "ERPNext Warehouse", 140, required=True)
    native_name = native.bind_location(warehouse, location_id, existing_native)
    doc = frappe.get_doc(
        doctype=LOCATION, warehouse=warehouse.name, code=code, native_warehouse=native_name,
        status=ACTIVE, revision=1, **fields,
    )
    return doc.insert(ignore_permissions=True, set_name=location_id)


def create_location(warehouse_id, *, code, display_name, description="", existing_native=None) -> Location:
    require_maintainer()
    with warehouse_write():
        warehouse = _warehouse(warehouse_id, locked=True)
        return _location_dto(_new_location(warehouse, code, display_name, description, existing_native))


def provision_system_transit(*, company, native_parent, warehouse_code, location_code,
                             display_name) -> tuple[Warehouse, Location]:
    """Provision a distinct transit warehouse and its leaf as one system operation."""
    require_system_operator()
    with warehouse_write():
        warehouse = _new_warehouse(company, native_parent, warehouse_code, display_name, "", TRANSIT)
        location = _new_location(warehouse, location_code, display_name)
        return _warehouse_dto(warehouse), _location_dto(location)


def get_warehouse(warehouse_id) -> Warehouse:
    require_maintainer()
    return _warehouse_dto(_warehouse(warehouse_id))


def list_warehouses(*, company=None) -> list[Warehouse]:
    require_maintainer()
    filters = {"kind": PHYSICAL}
    if company is not None:
        filters["company"] = check.text(company, "Company", 140, required=True)
    return [_warehouse_dto(row) for row in frappe.get_all(
        WAREHOUSE, filters=filters, fields=["name", "company", "code", "display_name", "description", "kind",
                                          "status", "revision"], order_by="company, code, name",
    )]


def get_location(location_id) -> Location:
    require_maintainer()
    location, _ = _location(location_id)
    return _location_dto(location)


def list_locations(warehouse_id) -> list[Location]:
    require_maintainer()
    warehouse = _warehouse(warehouse_id)
    return [_location_dto(row) for row in frappe.get_all(
        LOCATION, filters={"warehouse": warehouse.name},
        fields=["name", "warehouse", "code", "display_name", "description", "status", "revision"],
        order_by="code, name",
    )]


def resolve_location(location_id) -> Location:
    """Resolve a selectable physical location after checking status and mapping."""
    require_maintainer()
    location, warehouse = _location(location_id)
    _require_active(warehouse, location)
    _require_mapping(location, warehouse)
    return _location_dto(location)


def _require_active(*records):
    if any(record.status != ACTIVE for record in records):
        frappe.throw("The warehouse and location must be active.")


def _require_mapping(location, warehouse, *, locked=False):
    if native.mapping_status(location, warehouse, locked=locked) != "Verified":
        frappe.throw("The ERPNext Warehouse mapping is inconsistent. Review it before use.")


def diagnose_mapping(location_id) -> MappingDiagnostic:
    require_maintainer()
    location, warehouse = _location(location_id, system=frappe.session.user == "Administrator")
    return MappingDiagnostic(location.name, location.native_warehouse, native.mapping_status(location, warehouse))


def diagnose_native_mapping(native_warehouse) -> MappingDiagnostic:
    require_system_operator()
    return native.mapping_for_native(check.text(native_warehouse, "ERPNext Warehouse", 140, required=True))


def _edit(doc, display_name, description, expected_revision):
    if type(expected_revision) is not int or expected_revision != doc.revision:
        frappe.throw("Warehouse master data changed since it was read.", frappe.TimestampMismatchError)
    doc.update(_description(display_name, description))
    doc.revision += 1
    doc.save(ignore_permissions=True)


def edit_warehouse(warehouse_id, *, display_name, description="", expected_revision) -> Warehouse:
    require_maintainer()
    with warehouse_write():
        doc = _warehouse(warehouse_id, locked=True)
        native.validate_parent(doc.company, doc.native_parent)
        _edit(doc, display_name, description, expected_revision)
        return _warehouse_dto(doc)


def edit_location(location_id, *, display_name, description="", expected_revision) -> Location:
    require_maintainer()
    with warehouse_write():
        location, warehouse = _location(location_id, locked=True)
        _require_mapping(location, warehouse, locked=True)
        _edit(location, display_name, description, expected_revision)
        return _location_dto(location)


def deactivate_location(location_id):
    require_maintainer()
    with warehouse_write():
        location, warehouse = _location(location_id, system=True, locked=True)
        if warehouse.kind == TRANSIT:
            frappe.throw("System transit locations must remain active.", frappe.PermissionError)
        _require_mapping(location, warehouse, locked=True)
        native.check_references(location.native_warehouse)
        frappe.throw("Location deactivation requires complete V1 reference checks and stock write coordination.")


def deactivate_warehouse(warehouse_id):
    require_maintainer()
    with warehouse_write():
        warehouse = _warehouse(warehouse_id, system=True, locked=True)
        if warehouse.kind == TRANSIT:
            frappe.throw("System transit warehouses must remain active.", frappe.PermissionError)
        if frappe.db.get_value(LOCATION, {"warehouse": warehouse.name}, for_update=True):
            frappe.throw("A warehouse with retained locations cannot be deactivated.")
        frappe.throw("Warehouse deactivation requires complete V1 reference checks.")
