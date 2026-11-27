"""Validated inventory filters and the existing employee warehouse scope."""

from dataclasses import dataclass
from datetime import datetime, time, timedelta

import frappe

from ...access import policy
from ...catalog import validation
from ...catalog.dto import ACTIVE
from ...catalog.read import label, rows
from ...warehouse.persistence import LOCATION, WAREHOUSE

PRODUCT_FILTERS = frozenset({"query", "category", "brand", "sku_code", "attributes"})
INVENTORY_FILTERS = PRODUCT_FILTERS | {"company", "sku_id", "warehouse_id", "location_id", "batch", "include_zero"}
PERIOD_FILTERS = INVENTORY_FILTERS | {"start", "end"}
DOCUMENT_FILTERS = (PERIOD_FILTERS - {"include_zero"}) | {"action", "document_number"}
MOVEMENT_FILTERS = DOCUMENT_FILTERS | {"direction"}
ACTIONS = frozenset({
    "Material Receipt", "Material Issue", "Material Transfer", "Receive Transfer", "Return Transfer",
    "Stock Count", "Correct Effective Inventory",
})


def filters(value, allowed):
    if isinstance(value, str):
        if len(value) > 8192:
            frappe.throw("READ_FILTER_LIMIT_EXCEEDED")
        try:
            value = frappe.parse_json(value)
        except ValueError:
            frappe.throw("READ_INVALID_FILTER")
    if value is None:
        return {}
    if not isinstance(value, dict) or value.keys() - allowed:
        frappe.throw("READ_INVALID_FILTER")
    normalized = dict(value)
    if "query" in normalized:
        from ...catalog.sku import _search_text

        query = normalized["query"]
        if isinstance(query, str) and len(query) > 200:
            frappe.throw("PRODUCT_SEARCH_LIMIT_EXCEEDED")
        normalized["query"] = _search_text(validation.text(query, "Search query", 200))
        if len(normalized["query"]) > 200 or len(normalized["query"].split()) > 10:
            frappe.throw("PRODUCT_SEARCH_LIMIT_EXCEEDED")
    return normalized


@dataclass(frozen=True)
class Page:
    limit: int
    offset: int

    @classmethod
    def parse(cls, page=None, page_size=None, limit=None, offset=None):
        if (page is not None or page_size is not None) and (limit is not None or offset is not None):
            frappe.throw("READ_INVALID_PAGE")
        size = validation.order(page_size if page_size is not None else limit if limit is not None else 20,
                                "Page size")
        if not 1 <= size <= 100:
            frappe.throw("READ_INVALID_PAGE")
        if page is not None:
            page = validation.order(page, "Page")
            if not 1 <= page <= 100_001:
                frappe.throw("READ_INVALID_PAGE")
            offset = (page - 1) * size
        offset = 0 if offset is None else validation.order(offset, "Offset")
        if offset > 100_000:
            frappe.throw("READ_INVALID_PAGE")
        return cls(size, offset)

    def select(self, matches):
        return {"rows": matches[self.offset:self.offset + self.limit], "total": len(matches)}


@dataclass(frozen=True)
class Scope:
    company: str
    warehouses: dict
    locations: dict

    @classmethod
    def current(cls, company=None):
        authorized = policy.authorized_warehouses(policy.Operation.INVENTORY_READ, company=company)
        if not authorized:
            policy.deny()
        allowed = tuple(authorized)
        warehouse_labels = {row.name: label(row) for row in rows(
            WAREHOUSE, "name code display_name", {"name": ("in", allowed)},
        )}
        locations = {}
        for row in rows(LOCATION, "name code display_name warehouse native_warehouse",
                        {"warehouse": ("in", allowed), "status": ACTIVE}):
            warehouse = authorized[row.warehouse]
            if policy.location_mapping_is_visible(row, warehouse):
                locations[row.name] = dict(label(row), warehouse_id=row.warehouse)
        return cls(next(iter(authorized.values())).company, warehouse_labels, locations)

    def require_locations(self, *ids):
        if not ids or any(location not in self.locations for location in ids):
            policy.deny()


class Query:
    def __init__(self, options, scope, *, product_read):
        self.scope = scope
        self.product_read = product_read
        if not product_read and {"category", "attributes"} & options.keys():
            policy.deny()
        self.warehouse = options.get("warehouse_id")
        self.location = options.get("location_id")
        if self.warehouse is not None and policy._record_id(self.warehouse) not in scope.warehouses:
            policy.deny()
        if self.location is not None:
            scope.require_locations(policy._record_id(self.location))
            if self.warehouse is not None and scope.locations[self.location]["warehouse_id"] != self.warehouse:
                policy.deny()
        self.sku = options.get("sku_id")
        if self.sku is not None:
            policy._record_id(self.sku)
        self.batch_selected = "batch" in options
        self.batch = options.get("batch")
        if self.batch is not None:
            # Preserve the exact batch text, including any historical case.
            validation.text(self.batch, "Batch", 80, required=True)
        self.include_zero = validation.boolean(options.get("include_zero", False), "Include zero")
        self.product = {key: value for key, value in options.items() if key in PRODUCT_FILTERS}
        self.start = self.end = None
        if "start" in options:
            self.start = datetime.combine(validation.calendar_date(options["start"], "Start"), time.min)
        if "end" in options:
            last = validation.calendar_date(options["end"], "End")
            if last.year >= 9999:
                frappe.throw("READ_INVALID_DATE_RANGE")
            self.end = datetime.combine(last + timedelta(days=1), time.min)
        if self.start and self.end and self.start >= self.end:
            frappe.throw("READ_INVALID_DATE_RANGE")
        self.action = options.get("action")
        if self.action is not None and not (isinstance(self.action, str) and self.action in ACTIONS):
            frappe.throw("READ_INVALID_ACTION")
        self.number = options.get("document_number")
        if self.number is not None:
            self.number = validation.text(self.number, "Document number", 140, required=True)
        self.direction = options.get("direction")
        if self.direction is not None and self.direction not in ("INCREASE", "DECREASE"):
            frappe.throw("READ_INVALID_DIRECTION")

    def location_matches(self, location):
        return (location in self.scope.locations
                and (self.location is None or self.location == location)
                and (self.warehouse is None or self.scope.locations[location]["warehouse_id"] == self.warehouse))

    def batch_matches(self, batch):
        return not self.batch_selected or batch == self.batch

    @property
    def uses_product_fields(self):
        return self.product_read and any(self.product.get(key) for key in ("query", "category", "attributes"))

    def product_ids(self, *, candidates=None, active=True):
        from ...catalog.sku import search_sku_ids

        if self.sku is not None:
            candidates = {self.sku} if candidates is None else {self.sku} & candidates
        return search_sku_ids(
            self.product.get("query", ""), candidates=candidates, include_inactive=not active,
            stock_fields_only=not self.product_read,
            **{key: val for key, val in self.product.items() if key != "query"},
        )[0]
