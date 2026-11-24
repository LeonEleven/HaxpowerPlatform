"""Resolve the system transit leaf and bind immutable ownership to original lines."""

from collections import defaultdict
from decimal import Decimal

import frappe

from ...adapters.erpnext import stock_entry as stock, warehouse as native_warehouse
from ...catalog.dto import ACTIVE
from ...warehouse.persistence import LOCATION, TRANSIT, WAREHOUSE
from ..operations import persistence as store
from ..operations.dto import StockError

OWNERSHIP = store.TRANSIT_OWNERSHIP
INCONSISTENT = "TRANSFER_TRANSIT_OWNERSHIP_INCONSISTENT"


def resolve(company):
    warehouses = frappe.db.get_values(
        WAREHOUSE, {"company": company, "kind": TRANSIT}, ("name", "company", "kind", "native_parent", "status"),
        for_update=True, as_dict=True, order_by="name asc",
    )
    locations = frappe.db.get_values(
        LOCATION, {"warehouse": ("in", [row.name for row in warehouses])},
        ("name", "warehouse", "native_warehouse", "status"), for_update=True, as_dict=True, order_by="name asc",
    ) if warehouses else ()
    if (
        len(warehouses) != 1 or warehouses[0].status != ACTIVE
        or len(locations) != 1 or locations[0].status != ACTIVE
        or native_warehouse.mapping_status(locations[0], warehouses[0], locked=True) != "Verified"
    ):
        raise StockError("TRANSFER_TRANSIT_UNAVAILABLE")
    return locations[0]


def active_inward(company, item_codes, leaf, buckets):
    """Each effective transit SLE must have exactly one original owner and vice versa."""
    rows = frappe.db.sql(
        f"""
        SELECT sle.name, sle.item_code, CAST(sle.actual_qty AS CHAR) AS actual_qty, part.batch_no,
               sle.voucher_no, sle.voucher_detail_no, sle.voucher_type,
               own.name AS ownership_id, own.operation_id, line.name AS line_id, line.quantity,
               line.document_id, line.sku_id, line.batch_kind, line.batch_id, line.source, line.target, line.uom,
               doc.company AS transfer_company, doc.document_type, doc.status, doc.effective_operation,
               op.document_id AS operation_document, op.action, op.status AS operation_status,
               entry.owner AS native_actor, op.actor,
               link.line_id AS link_line, link.parent_type, link.parent_id, link.detail_type,
               entry.name AS native_entry, detail.name AS native_detail, detail.item_code AS detail_item,
               detail.s_warehouse, source.native_warehouse AS source_leaf,
               detail.t_warehouse, detail.batch_no AS detail_batch, detail.stock_uom,
               detail.qty AS detail_qty, detail.transfer_qty, detail.transferred_qty,
               sku.native_item, mapping.name AS mapping_id, mapping.native_batch, mapping.kind AS mapped_kind, mapping.sku AS mapped_sku,
               detail.ste_detail, detail.against_stock_entry,
               entry.docstatus, entry.purpose, entry.stock_entry_type, entry.add_to_transit, entry.outgoing_stock_entry,
               out_sle.name AS outward_sle, CAST(out_sle.actual_qty AS CHAR) AS outward_qty,
               out_bundle.type_of_transaction AS out_direction, out_part.batch_no AS out_batch,
               CAST(out_part.qty AS CHAR) AS out_part_qty
        FROM `tabStock Ledger Entry` sle
        LEFT JOIN `tabSerial and Batch Entry` part ON part.parent = sle.serial_and_batch_bundle
        LEFT JOIN `tab{OWNERSHIP}` own ON own.inward_sle = sle.name
        LEFT JOIN `tab{store.LINE}` line ON line.name = own.line_id
        LEFT JOIN `tab{store.DOCUMENT}` doc ON doc.name = line.document_id
        LEFT JOIN `tab{store.LOG}` op ON op.name = own.operation_id
        LEFT JOIN `tab{store.LINK}` link
          ON link.operation_id = own.operation_id AND link.detail_id = own.native_detail AND link.role = 'Dispatch'
        LEFT JOIN `tabStock Entry Detail` detail ON detail.name = own.native_detail
        LEFT JOIN `tabStock Entry` entry ON entry.name = detail.parent
        LEFT JOIN `tabHP V1 Stock Item` sku ON sku.name = line.sku_id
        LEFT JOIN `tabHP V1 Batch Mapping` mapping
          ON mapping.native_batch = part.batch_no AND mapping.sku = line.sku_id
        LEFT JOIN `tab{LOCATION}` source ON source.name = line.source
        LEFT JOIN `tabStock Ledger Entry` out_sle
          ON out_sle.voucher_type = 'Stock Entry' AND out_sle.voucher_no = entry.name
          AND out_sle.voucher_detail_no = detail.name AND out_sle.warehouse = detail.s_warehouse
          AND out_sle.company = sle.company AND out_sle.item_code = sle.item_code
          AND out_sle.docstatus = 1 AND out_sle.is_cancelled = 0
          AND out_sle.serial_and_batch_bundle = detail.serial_and_batch_bundle
        LEFT JOIN `tabSerial and Batch Bundle` out_bundle
          ON out_bundle.name = out_sle.serial_and_batch_bundle AND out_bundle.docstatus = 1
          AND out_bundle.is_cancelled = 0 AND out_bundle.company = sle.company
          AND out_bundle.item_code = sle.item_code AND out_bundle.warehouse = detail.s_warehouse
          AND out_bundle.voucher_type = 'Stock Entry' AND out_bundle.voucher_no = entry.name
          AND out_bundle.voucher_detail_no = detail.name AND out_bundle.has_serial_no = 0
        LEFT JOIN `tabSerial and Batch Entry` out_part
          ON out_part.parent = out_bundle.name AND out_part.docstatus = 1 AND out_part.is_cancelled = 0
          AND out_part.item_code = sle.item_code AND out_part.warehouse = detail.s_warehouse
          AND out_part.parenttype = 'Serial and Batch Bundle' AND out_part.parentfield = 'entries'
          AND COALESCE(out_part.serial_no, '') = ''
        WHERE sle.company = %s AND sle.warehouse = %s AND sle.item_code IN %s
          AND sle.docstatus = 1 AND sle.is_cancelled = 0
        ORDER BY sle.name FOR UPDATE
        """, (company, leaf.native_warehouse, tuple(sorted(item_codes))), as_dict=True,
    )
    ownership_ids = set(frappe.db.sql(
        f"""
        SELECT own.name FROM `tab{OWNERSHIP}` own
        JOIN `tab{store.LINE}` line ON line.name = own.line_id
        JOIN `tabHP V1 Stock Item` sku ON sku.name = line.sku_id
        WHERE own.transit_location = %s AND sku.native_item IN %s
        FOR UPDATE
        """, (leaf.name, tuple(sorted(item_codes))), pluck=True,
    ))
    seen, totals = {}, defaultdict(Decimal)
    for row in rows:
        qty = stock._number(row.quantity)
        if (
            row.ownership_id not in ownership_ids or row.ownership_id in seen
            or row.transfer_company != company or row.document_type != stock.TRANSFER or row.status != "In Transit"
            or row.operation_id != row.effective_operation or row.action != stock.TRANSFER
            or row.operation_status != "Effective" or not row.operation_document or row.operation_document != row.document_id
            or row.native_actor != row.actor
            or (row.link_line, row.parent_type, row.parent_id, row.detail_type)
            != (row.line_id, stock.ENTRY, row.native_entry, stock.DETAIL)
            or (row.docstatus, row.purpose, row.stock_entry_type, row.add_to_transit)
            != (1, stock.TRANSFER, stock.DISPATCH_TYPE, 1)
            or row.outgoing_stock_entry or row.ste_detail or row.against_stock_entry or row.transferred_qty
            or not row.s_warehouse or row.s_warehouse != row.source_leaf or row.t_warehouse != leaf.native_warehouse
            or row.detail_item != row.item_code or row.native_item != row.item_code or row.stock_uom != row.uom
            or not row.batch_no or row.batch_no != row.detail_batch or row.batch_no != row.native_batch
            or row.batch_kind != row.mapped_kind or row.sku_id != row.mapped_sku
            or (row.batch_kind == "BATCH" and row.batch_id != row.mapping_id)
            or (row.batch_kind == "NO_BATCH" and row.batch_id is not None)
            or (row.native_entry, row.native_detail, stock.ENTRY) != (row.voucher_no, row.voucher_detail_no, row.voucher_type)
            or qty <= 0 or any(stock._number(value) != qty for value in (row.actual_qty, row.detail_qty, row.transfer_qty))
            or not row.outward_sle or row.out_direction != "Outward" or row.out_batch != row.batch_no
            or not all(stock._number(value) == qty.copy_negate() for value in (row.outward_qty, row.out_part_qty))
        ):
            raise StockError(INCONSISTENT)
        seen[row.ownership_id] = row
        totals[row.item_code, leaf.native_warehouse, row.batch_no] += qty
    if seen.keys() != ownership_ids or totals != {key: qty for key, qty in buckets.items() if key[1] == leaf.native_warehouse}:
        raise StockError(INCONSISTENT)
    return seen


def record_dispatch(plan, result, links):
    """Persist immutable business references after the operation log is staged."""
    from .. import availability

    company = plan.command.company
    leaf = resolve(company)
    for link in links:
        sle = frappe.db.get_value(stock.SLE, {
            "warehouse": leaf.native_warehouse, "company": company,
            "voucher_type": stock.ENTRY, "voucher_no": link.parent_id, "voucher_detail_no": link.detail_id,
            "docstatus": 1, "is_cancelled": 0,
        }, "name", for_update=True)
        if not sle:
            raise StockError(INCONSISTENT)
        store.insert(OWNERSHIP, operation_id=result.operation_id, line_id=link.line_id,
                     transit_location=leaf.name, native_detail=link.detail_id, inward_sle=sle)
    # The document has been marked In Transit within the same transaction.
    items = {sku.native_item for sku in plan.skus.values()}
    (buckets, _), _ = availability.read_source(
        company, {(item, leaf.native_warehouse) for item in items}, plan.skus.values(),
    )
    active_inward(company, items, leaf, buckets)
