"""Employee-facing warehouse and product projections."""

import frappe

from ..catalog import validation
from ..catalog.dto import ACTIVE
from ..catalog import read as catalog
from ..inventory.operations.transaction import connection
from ..inventory.read.query import Page, PRODUCT_FILTERS, filters
from ..inventory.read.service import product_data
from ..warehouse.persistence import LOCATION, WAREHOUSE
from .policy import (
    Operation, active_actor, authorized_warehouse_ids, deny, location_mapping_is_visible,
    require_location_access, require_operation, require_warehouse_access,
)

PUBLIC_FIELDS = ["name", "code", "display_name"]


def _summary(row):
    return {"id": row.name, "code": row.code, "display_name": row.display_name}


def _row(doctype, name):
    row = frappe.db.get_value(doctype, name, PUBLIC_FIELDS, as_dict=True)
    if not row:
        deny()
    return row


def _page(rows, query, limit, offset):
    query = validation.text(query, "Search", 140).casefold()
    if not 1 <= limit <= 100 or offset < 0:
        frappe.throw("Use a page size of 1–100 and a non-negative offset.")
    matches = [row for row in rows if query in row["code"].casefold() or query in row["display_name"].casefold()]
    return {"rows": matches[offset:offset + limit], "total": len(matches)}


@frappe.whitelist(methods=["GET"])
def list_warehouses(query: str = "", limit: int = 20, offset: int = 0):
    allowed = authorized_warehouse_ids(Operation.WAREHOUSE_READ)
    rows = frappe.get_all(
        WAREHOUSE, filters={"name": ("in", allowed)}, fields=PUBLIC_FIELDS, order_by="code, name",
    ) if allowed else []
    return _page([_summary(row) for row in rows], query, limit, offset)


@frappe.whitelist(methods=["GET"])
def get_warehouse(warehouse_id: str):
    warehouse = require_warehouse_access(warehouse_id, Operation.WAREHOUSE_READ)
    return _summary(_row(WAREHOUSE, warehouse.name))


@frappe.whitelist(methods=["GET"])
def list_locations(warehouse_id: str, query: str = "", limit: int = 20, offset: int = 0):
    warehouse = require_warehouse_access(warehouse_id, Operation.WAREHOUSE_READ)
    rows = frappe.get_all(
        LOCATION, filters={"warehouse": warehouse.name, "status": ACTIVE},
        fields=[*PUBLIC_FIELDS, "native_warehouse"], order_by="code, name",
    )
    visible = [
        dict(_summary(row), warehouse_id=warehouse.name) for row in rows
        if location_mapping_is_visible(row, warehouse)
    ]
    return _page(visible, query, limit, offset)


@frappe.whitelist(methods=["GET"])
def get_location(location_id: str):
    location = require_location_access(location_id, Operation.WAREHOUSE_READ)
    return dict(_summary(_row(LOCATION, location.name)), warehouse_id=location.warehouse)


@frappe.whitelist(methods=["GET"])
def get_specification(specification_id: str):
    with connection(snapshot=True):
        return catalog.get_specification(specification_id)


@frappe.whitelist(methods=["GET"])
def get_sku(sku_id: str):
    with connection(snapshot=True):
        return catalog.get_sku(sku_id)


@frappe.whitelist(methods=["GET"])
def search_products(criteria=None, page: int = None, pageSize: int = None, limit: int = None, offset: int = None):
    return product_data(filters(criteria, PRODUCT_FILTERS), Page.parse(page, pageSize, limit, offset))[0]


@frappe.whitelist(methods=["GET"])
def product_filters():
    with connection(snapshot=True):
        return catalog.filter_options()



@frappe.whitelist(methods=["GET"])
def csrf_token():
    from frappe.sessions import get_csrf_token

    active_actor()
    return {"csrf_token": get_csrf_token()}
