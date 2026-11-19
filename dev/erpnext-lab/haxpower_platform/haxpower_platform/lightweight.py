"""Single-line Stage 2B experiment. ERPNext owns every quantity and movement leg."""

import json
import re
from decimal import Decimal
from hashlib import sha256

import frappe
from frappe.utils import get_datetime

from haxpower_platform import audit, batch_input, lightweight_fixtures as fixtures, transit
from haxpower_platform.integrity import quantity_for
from haxpower_platform.stock import batch_key, find_batch, post_entry, require_warehouse

LOG = fixtures.LOG
REVIEW_REQUIRED = "STAGE2B_TRANSFER_MANUAL_REVIEW"
CORRECTIONS = ("RECEIPT", "ISSUE", "COUNT", "DISPATCH")


def scope(sku, location, selection):
    warehouse = transit.location_for(location, fixtures)
    if not isinstance(sku, str) or sku not in fixtures.ITEMS:
        frappe.throw("PRODUCT_NOT_FOUND")
    try:
        number = batch_input.normalize_selection(selection)
    except batch_input.BatchInputError as error:
        frappe.throw(str(error))
    return fixtures.ITEMS[sku], warehouse, number


def request_identity(request_id, payload):
    if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", request_id):
        frappe.throw("INVALID_REQUEST_ID")
    transit.lock_company(fixtures)
    if frappe.db.get_value("Company", fixtures.COMPANY, "enable_perpetual_inventory", for_update=True):
        frappe.throw("STAGE2B_PERPETUAL_INVENTORY_ENABLED")
    key = sha256(f"{fixtures.COMPANY}\0{frappe.session.user}\0{request_id}".encode()).hexdigest()
    return key, sha256(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def operation(key):
    return audit.stored(LOG, key)


def load_document(log):
    if log.document_type not in ("Stock Entry", "Stock Reconciliation"):
        frappe.throw("STAGE2B_DOCUMENT_MISMATCH")
    doc = frappe.get_doc(log.document_type, log.document_id, for_update=True)
    if (doc.company != fixtures.COMPANY or doc.docstatus != 1 or len(doc.items) != 1
            or doc.items[0].name != log.document_line or doc.items[0].item_code not in fixtures.ITEMS.values()):
        frappe.throw("STAGE2B_DOCUMENT_MISMATCH")
    return doc


def replay(key, fingerprint):
    existing = operation(key)
    if existing:
        if existing.payload_hash != fingerprint:
            frappe.throw("IDEMPOTENCY_CONFLICT")
        load_document(existing)
    return existing


def result(record_id, number, replayed):
    return {"operationId": record_id, "batch": number, "replayed": replayed}


def write_log(key, fingerprint, action, doc, **relations):
    return audit.append_record(
        LOG, record_id=key, payload_hash=fingerprint, operation_type=action, document_type=doc.doctype,
        document_id=doc.name, document_line=doc.items[0].name, actor=frappe.session.user,
        operated_at=get_datetime(), **relations,
    )


def batch_from_line(row):
    number = None if row.batch_no == batch_key(row.item_code, None) else frappe.db.get_value(
        "Batch", row.batch_no, "description", for_update=True,
    )
    if find_batch(row.item_code, number) != row.batch_no:
        frappe.throw("STAGE2B_BATCH_MISMATCH")
    return number


def available(item, batch, warehouse):
    return transit.stock_quantity(item, batch, warehouse, fixtures.COMPANY)


def adjustment(item, batch, amount, source=None, target=None):
    voucher_id = post_entry("ISSUE" if source else "RECEIPT", item, source or target, amount, batch,
                            "HP2B fixture", company=fixtures.COMPANY)
    return frappe.get_doc("Stock Entry", voucher_id)


def transfer(item, batch, amount, source, target, *, outgoing=None, source_row=None):
    voucher_id = transit.submit_stock(
        item, batch, amount, source, target, "HP2B fixture", company=fixtures.COMPANY,
        outgoing=outgoing, source_row=source_row, start_transit=target == fixtures.TRANSIT,
    )
    return frappe.get_doc("Stock Entry", voucher_id)


def already_corrected(record_id):
    return bool(frappe.db.get_value(LOG, {"related_operation": record_id, "operation_type": "CORRECTION"},
                                   "name", for_update=True))


@frappe.whitelist(methods=["POST"])
def post(action, sku, location, quantity, requestId, batchSelection=None, targetLocation=None):
    transit.require_operator()
    if action not in CORRECTIONS or (action == "DISPATCH") != bool(targetLocation):
        frappe.throw("INVALID_MOVEMENT")
    item, warehouse, number = scope(sku, location, batchSelection)
    target = transit.location_for(targetLocation, fixtures) if action == "DISPATCH" else None
    if target == warehouse:
        frappe.throw("TRANSFER_SAME_LOCATION")
    amount = quantity_for(quantity, 0, allow_zero=action == "COUNT")
    key, fingerprint = request_identity(requestId, [action, item, warehouse, number, str(amount), target])
    if replay(key, fingerprint):
        return result(key, number, True)
    with audit.stock_write():
        batch = find_batch(item, number, create=action == "RECEIPT")
        if action in ("ISSUE", "DISPATCH") and available(item, batch, warehouse) < amount:
            frappe.throw("INSUFFICIENT_STOCK")
        if action == "DISPATCH":
            doc = transfer(item, batch, amount, warehouse, fixtures.TRANSIT)
        elif action == "COUNT":
            previous = available(item, batch, warehouse)
            if not previous or previous == amount:
                frappe.throw("STAGE2B_COUNT_FIXTURE_REQUIRES_DIFFERENCE")
            task = frappe._dict(batch_key=batch, item_code=item, location=location)
            doc = audit.reconcile_count(task, previous, amount, scope=fixtures)
        else:
            doc = adjustment(item, batch, amount, source=warehouse if action == "ISSUE" else None, target=warehouse)
    write_log(key, fingerprint, action, doc, planned_target=target)
    return result(key, number, False)


def dispatch_state(record):
    doc = load_document(record)
    row = doc.items[0]
    if (not doc.add_to_transit or row.t_warehouse != fixtures.TRANSIT or not record.planned_target
            or row.s_warehouse == record.planned_target):
        frappe.throw("STAGE2B_TRANSFER_MISMATCH")
    require_warehouse(row.s_warehouse, fixtures.COMPANY)
    require_warehouse(record.planned_target, fixtures.COMPANY)
    remaining = Decimal(str(row.transfer_qty)) - Decimal(str(row.transferred_qty))
    if not 0 <= remaining <= Decimal(str(row.transfer_qty)):
        frappe.throw("STAGE2B_TRANSFER_MISMATCH")
    return row, remaining


@frappe.whitelist(methods=["POST"])
def receive(dispatchId, quantity, requestId):
    transit.require_operator()
    audit.require_record_id(dispatchId)
    amount = quantity_for(quantity, 0)
    key, fingerprint = request_identity(requestId, ["RECEIVE", dispatchId, str(amount)])
    origin = operation(dispatchId)
    if not origin or origin.operation_type != "DISPATCH":
        frappe.throw("STAGE2B_TRANSFER_NOT_FOUND")
    row, remaining = dispatch_state(origin)
    number = batch_from_line(row)
    if replay(key, fingerprint):
        return result(key, number, True)
    if already_corrected(origin.name) or not remaining:
        frappe.throw("STAGE2B_TRANSFER_CLOSED")
    if amount > remaining:
        frappe.throw("TRANSFER_RECEIPT_EXCEEDS_REMAINING")
    if available(row.item_code, row.batch_no, fixtures.TRANSIT) < amount:
        frappe.throw("INSUFFICIENT_IN_TRANSIT")
    with audit.stock_write():
        doc = transfer(row.item_code, row.batch_no, amount, fixtures.TRANSIT, origin.planned_target,
                       outgoing=origin.document_id, source_row=row.name)
    _, new_remaining = dispatch_state(origin)
    if new_remaining != remaining - amount:
        frappe.throw("STAGE2B_TRANSFER_MISMATCH")
    write_log(key, fingerprint, "RECEIVE", doc, related_operation=origin.name)
    return result(key, number, False)


@frappe.whitelist(methods=["POST"])
def correct(originalId, reason, requestId):
    transit.require_operator()
    audit.require_record_id(originalId)
    if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 400:
        frappe.throw("CORRECTION_REASON_REQUIRED")
    reason = reason.strip()
    key, fingerprint = request_identity(requestId, ["CORRECTION", originalId, reason])
    origin = operation(originalId)
    if not origin or origin.operation_type not in CORRECTIONS:
        frappe.throw("STAGE2B_CORRECTION_NOT_SUPPORTED")
    direction = origin.operation_type
    doc = load_document(origin)
    row = doc.items[0]
    if direction == "DISPATCH":
        row, amount = dispatch_state(origin)
        source, target = fixtures.TRANSIT, row.s_warehouse
    elif direction == "COUNT":
        require_warehouse(row.warehouse, fixtures.COMPANY)
        delta = Decimal(str(row.quantity_difference))
        amount = abs(delta)
        source, target = (row.warehouse, None) if delta > 0 else (None, row.warehouse)
    else:
        source, target = row.t_warehouse or None, row.s_warehouse or None
        require_warehouse(source or target, fixtures.COMPANY)
        amount = Decimal(str(row.transfer_qty))
    number = batch_from_line(row)
    if replay(key, fingerprint):
        return result(key, number, True)
    if already_corrected(origin.name):
        frappe.throw("STAGE2B_ALREADY_CORRECTED")
    if direction == "DISPATCH" and not amount:
        frappe.throw(f"{REVIEW_REQUIRED} 移库已完成。请人工核对目标库位和下游出库，再确认补偿单据。")
    if amount <= 0:
        frappe.throw("STAGE2B_CORRECTION_NOT_SUPPORTED")
    if source and available(row.item_code, row.batch_no, source) < amount:
        frappe.throw("INSUFFICIENT_STOCK_FOR_CORRECTION 请人工核对库存去向。")
    with audit.stock_write():
        writer = transfer if direction == "DISPATCH" else adjustment
        corrected = writer(row.item_code, row.batch_no, amount, source=source, target=target)
    write_log(key, fingerprint, "CORRECTION", corrected, related_operation=origin.name, reason=reason)
    return result(key, number, False)
