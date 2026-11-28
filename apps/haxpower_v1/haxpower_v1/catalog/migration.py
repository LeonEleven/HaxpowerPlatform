"""Validate historical catalog data after schema sync, then reserve numbers and backfill units."""

from collections import defaultdict

import frappe

from .numbers import SKU_CODE, SPEC_SEQUENCE, reserve_through, sku_serial, spec_serial
from .persistence import LEGACY_MAINTAINER, SPECIFICATION, catalog_rows, catalog_write, require_site_operator
from .sku_persistence import SKU


def legacy_maintenance_assignments():
    """Report assignments for operator review. No role gets inferred from the old grant."""
    require_site_operator()
    return tuple(frappe.get_all("Has Role", filters={
        "parenttype": "User", "parentfield": "roles", "role": LEGACY_MAINTAINER,
    }, pluck="parent", order_by="parent"))


def reconcile():
    # Runs on every migrate, after DDL and patches, so a failed validation is retried.
    with catalog_write():
        specs = {row.name: row for row in catalog_rows(
            SPECIFICATION, {}, ["name", "code", "stock_uom", "quantity_precision"],
        )}
        units = defaultdict(set)
        suffixes = {spec.name: 0 for spec in specs.values() if spec_serial(spec.code)}
        for row in catalog_rows(SKU, {}, ["name", "code", "specification", "stock_uom", "quantity_precision"]):
            spec = specs.get(row.specification)
            if not spec or not row.stock_uom or type(row.quantity_precision) is not int or not 0 <= row.quantity_precision <= 9:
                frappe.throw(f"Catalog upgrade: SKU {row.code} has an invalid specification or stock unit.")
            units[spec.name].add((row.stock_uom, row.quantity_precision))
            if SKU_CODE.fullmatch(row.code) and spec_serial(row.code.split("-", 1)[0]):
                suffix = sku_serial(row.code, spec.code)
                if suffix is None:
                    frappe.throw(f"Catalog upgrade: SKU {row.code} conflicts with the numbering namespace of {spec.code}.")
                suffixes[spec.name] = max(suffixes[spec.name], suffix)

        backfill = {}
        for spec in specs.values():
            observed = units[spec.name]
            if len(observed) > 1 or (spec.stock_uom and observed and
                                     observed != {(spec.stock_uom, spec.quantity_precision)}):
                frappe.throw(f"Catalog upgrade: conflicting SKU stock units or precision for {spec.code}: {sorted(observed)}.")
            if observed and not spec.stock_uom:
                backfill[spec.name], = observed

        reserve_through(SPEC_SEQUENCE, max((spec_serial(row.code) or 0 for row in specs.values()), default=0))
        for namespace, suffix in sorted(suffixes.items()):
            reserve_through(namespace, suffix)
        for spec_id, (uom, precision) in backfill.items():
            frappe.db.set_value(SPECIFICATION, spec_id, {"stock_uom": uom, "quantity_precision": precision},
                                update_modified=False)

    assignments = legacy_maintenance_assignments()
    if assignments:
        print(f"Catalog upgrade: {len(assignments)} legacy maintenance assignments require explicit role review.")
