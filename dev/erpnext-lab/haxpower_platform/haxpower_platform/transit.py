"""Single-line two-stage transfer experiment on native ERPNext Stock Entries."""

import json
import re
from decimal import Decimal
from hashlib import sha256

import frappe

from haxpower_platform.stock import (
    ROLE, SERVICE_WRITE, find_batch, parse_selection, require_site, require_warehouse,
)
from haxpower_platform.transit_fixtures import COMPANY, ITEMS, LOCATIONS, TRANSIT


def require_operator():
    require_site()
    if ROLE not in frappe.get_roles():
        frappe.throw("INVENTORY_OPERATION_DENIED", frappe.PermissionError)


def location_for(location_id):
    warehouse = LOCATIONS.get(location_id) if isinstance(location_id, str) else None
    require_warehouse(warehouse, COMPANY)
    return warehouse


def request_values(request_id, quantity):
    if not isinstance(quantity, str) or not re.fullmatch(r"[1-9][0-9]{0,5}", quantity):
        frappe.throw("INVALID_PC_QUANTITY")
    if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", request_id):
        frappe.throw("INVALID_REQUEST_ID")
    key = sha256(f"{frappe.session.user}\0{request_id}".encode()).hexdigest()
    return key, Decimal(quantity)


def lock_company():
    if not frappe.db.get_value("Company", COMPANY, "name", for_update=True):
        frappe.throw("TRANSFER_LAB_NOT_CONFIGURED")
    if (frappe.db.get_single_value("Stock Settings", "auto_create_serial_and_batch_bundle_for_outward")
            or frappe.db.get_single_value("Stock Settings", "allow_negative_stock_for_batch")
            or frappe.db.get_single_value("Stock Settings", "allow_negative_stock")
            or not frappe.db.get_single_value("Stock Settings", "use_serial_batch_fields")):
        frappe.throw("TRANSFER_LAB_STOCK_SETTINGS_CHANGED")
    if not frappe.db.exists("Warehouse", {"name": TRANSIT, "company": COMPANY, "warehouse_type": "Transit",
                                          "disabled": 0, "is_group": 0}):
        frappe.throw("TRANSFER_TRANSIT_WAREHOUSE_UNAVAILABLE")


def find_request(prefix):
    return frappe.db.get_value(
        "Stock Entry", {"company": COMPANY, "remarks": ["like", prefix + "%"]},
        ["name", "remarks", "docstatus", "stock_entry_type", "add_to_transit", "outgoing_stock_entry"],
        as_dict=True, for_update=True,
    )


def dispatch_line(voucher):
    if not voucher or voucher.docstatus != 1 or voucher.stock_entry_type != "Material Transfer" or not voucher.add_to_transit:
        frappe.throw("TRANSFER_NOT_FOUND")
    rows = frappe.db.get_values(
        "Stock Entry Detail", {"parent": voucher.name},
        ["name", "item_code", "s_warehouse", "t_warehouse", "transfer_qty", "transferred_qty", "batch_no"],
        as_dict=True, for_update=True,
    )
    if len(rows) != 1:
        frappe.throw("TRANSFER_UNEXPECTED_NATIVE_ROW")
    return rows[0]


def stock_quantity(item, batch, warehouse):
    # Current reads avoid using an earlier authentication read's database snapshot.
    ledger = frappe.qb.DocType("Stock Ledger Entry")
    allocation = frappe.qb.DocType("Serial and Batch Entry")
    rows = (
        frappe.qb.from_(ledger)
        .join(allocation).on(allocation.parent == ledger.serial_and_batch_bundle)
        .select(allocation.qty)
        .where((ledger.company == COMPANY) & (ledger.item_code == item) & (ledger.warehouse == warehouse)
               & (ledger.is_cancelled == 0) & (allocation.batch_no == batch) & (allocation.docstatus == 1))
        .for_update()
        .run()
    )
    return sum((Decimal(str(row[0])) for row in rows), Decimal(0))


def submit_stock(item, batch, qty, source, target, remarks, *, outgoing=None, source_row=None):
    row = {"item_code": item, "qty": qty, "uom": "PC", "use_serial_batch_fields": 1, "batch_no": batch,
           "allow_zero_valuation_rate": 1, "s_warehouse": source, "t_warehouse": target}
    if outgoing:
        row.update({"against_stock_entry": outgoing, "ste_detail": source_row})
    token = SERVICE_WRITE.set(True)
    try:
        doc = frappe.get_doc({
            "doctype": "Stock Entry", "company": COMPANY, "stock_entry_type": "Material Transfer",
            "from_warehouse": source, "to_warehouse": target, "remarks": remarks,
            "add_to_transit": int(outgoing is None), "outgoing_stock_entry": outgoing, "items": [row],
        }).insert(ignore_permissions=True)
        doc.submit()
    finally:
        SERVICE_WRITE.reset(token)
    expected = {source: -qty, target: qty}
    rows = frappe.get_all(
        "Stock Ledger Entry", filters={"company": COMPANY, "voucher_no": doc.name, "is_cancelled": 0,
                                       "voucher_type": "Stock Entry", "voucher_detail_no": doc.items[0].name,
                                       "item_code": item},
        fields=["warehouse", "actual_qty", "serial_and_batch_bundle"],
    )
    if len(rows) != 2 or {row.warehouse: Decimal(str(row.actual_qty)) for row in rows} != expected:
        frappe.throw("TRANSFER_UNEXPECTED_LEDGER")
    for row in rows:
        bundle = frappe.get_doc("Serial and Batch Bundle", row.serial_and_batch_bundle)
        if (bundle.docstatus != 1 or bundle.warehouse != row.warehouse or len(bundle.entries) != 1
                or bundle.entries[0].batch_no != batch or Decimal(str(bundle.entries[0].qty)) != expected[row.warehouse]):
            frappe.throw("TRANSFER_UNEXPECTED_BUNDLE")
    return doc.name


def state(transfer_id, number, dispatched, received, replayed, receipt_id=None):
    remaining = dispatched - received
    status = "Completed" if remaining == 0 else ("Partially Received" if received else "In Transit")
    result = {"transferId": transfer_id, "batch": number, "state": status, "replayed": replayed,
              "dispatched": format(dispatched.normalize(), "f"), "received": format(received.normalize(), "f"),
              "remaining": format(remaining.normalize(), "f")}
    if receipt_id:
        result["receiptId"] = receipt_id
    return result


@frappe.whitelist(methods=["POST"])
def dispatch(stockItemId, sourceLocationId, targetLocationId, quantity, requestId, batchSelection=None):
    require_operator()
    source, target = location_for(sourceLocationId), location_for(targetLocationId)
    if source == target:
        frappe.throw("TRANSFER_SAME_LOCATION")
    item = ITEMS.get(stockItemId) if isinstance(stockItemId, str) else None
    if not item:
        frappe.throw("PRODUCT_NOT_FOUND")
    number = parse_selection(batchSelection)
    key, qty = request_values(requestId, quantity)
    prefix = f"HP1D-D:{key}:"
    remarks = prefix + json.dumps([stockItemId, sourceLocationId, targetLocationId, number, quantity])
    lock_company()
    existing = find_request(prefix)
    if existing:
        if existing.remarks != remarks:
            frappe.throw("IDEMPOTENCY_CONFLICT")
        line = dispatch_line(existing)
        return state(key, number, Decimal(str(line.transfer_qty)), Decimal(str(line.transferred_qty)), True)
    batch = find_batch(item, number)
    if stock_quantity(item, batch, source) < qty:
        frappe.throw("INSUFFICIENT_STOCK")
    submit_stock(item, batch, qty, source, TRANSIT, remarks)
    return state(key, number, qty, Decimal(0), False)


@frappe.whitelist(methods=["POST"])
def receive(transferId, stockItemId, targetLocationId, quantity, requestId, batchSelection=None):
    require_operator()
    if not isinstance(transferId, str) or not re.fullmatch(r"[a-f0-9]{64}", transferId):
        frappe.throw("TRANSFER_NOT_FOUND")
    # Check both gates before disclosing the dispatch or recording a receipt.
    target = location_for(targetLocationId)
    lock_company()
    prefix = f"HP1D-D:{transferId}:"
    voucher = find_request(prefix)
    line = dispatch_line(voucher)
    sku, source_id, target_id, number, dispatch_qty = json.loads(voucher.remarks[len(prefix):])
    source = location_for(source_id)
    location_for(target_id)
    if targetLocationId != target_id or stockItemId != sku or parse_selection(batchSelection) != number:
        frappe.throw("TRANSFER_RECEIPT_MISMATCH")
    item = ITEMS[sku]
    batch = find_batch(item, number)
    dispatched, received = Decimal(str(line.transfer_qty)), Decimal(str(line.transferred_qty))
    if (line.item_code != item or line.s_warehouse != source or line.t_warehouse != TRANSIT
            or line.batch_no != batch or dispatched != Decimal(dispatch_qty) or not 0 <= received <= dispatched):
        frappe.throw("TRANSFER_UNEXPECTED_NATIVE_ROW")
    key, qty = request_values(requestId, quantity)
    prefix = f"HP1D-R:{key}:"
    remarks = prefix + json.dumps([transferId, quantity])
    existing = find_request(prefix)
    if existing:
        if existing.remarks != remarks or existing.docstatus != 1 or existing.outgoing_stock_entry != voucher.name:
            frappe.throw("IDEMPOTENCY_CONFLICT")
        return state(transferId, number, dispatched, received, True, key)
    if received == dispatched:
        frappe.throw("TRANSFER_COMPLETED")
    if qty > dispatched - received:
        frappe.throw("TRANSFER_RECEIPT_EXCEEDS_REMAINING")
    if stock_quantity(item, batch, TRANSIT) < qty:
        frappe.throw("INSUFFICIENT_IN_TRANSIT")
    submit_stock(item, batch, qty, TRANSIT, target, remarks, outgoing=voucher.name, source_row=line.name)
    actual = dispatch_line(voucher).transferred_qty
    if Decimal(str(actual)) != received + qty:
        frappe.throw("TRANSFER_UNEXPECTED_RECEIVED_TOTAL")
    return state(transferId, number, dispatched, received + qty, False, key)
