"""Request boundaries for the isolated Stage 1E stock experiments."""

import json
import re
from decimal import Decimal
from hashlib import sha256

import frappe
from frappe.utils import get_datetime

from haxpower_platform import integrity_fixtures as fixtures, transit
from haxpower_platform.stock import SERVICE_WRITE, find_batch, parse_selection, post_entry


def quantity_for(value, places, *, allow_zero=False):
    pattern = r"(?:0|[1-9][0-9]{0,5})"
    if places:
        pattern += rf"(?:\.[0-9]{{1,{places}}})?"
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        frappe.throw("INVALID_STOCK_QUANTITY")
    qty = Decimal(value)
    if not qty and not allow_zero:
        frappe.throw("INVALID_STOCK_QUANTITY")
    return qty


def request_identity(request_id, payload):
    if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", request_id):
        frappe.throw("INVALID_REQUEST_ID")
    transit.lock_company(fixtures)
    if frappe.db.get_value("Company", fixtures.COMPANY, "enable_perpetual_inventory", for_update=True):
        frappe.throw("STAGE1E_PERPETUAL_INVENTORY_ENABLED")
    key = sha256(f"{frappe.session.user}\0{request_id}".encode()).hexdigest()
    fingerprint = sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()
    return key, fingerprint


def movement_record(key, fingerprint):
    prefix = f"HP1E:{key}:"
    remarks = prefix + fingerprint
    record = frappe.db.get_value(
        "Stock Entry", {"company": fixtures.COMPANY, "remarks": ["like", prefix + "%"]},
        ["name", "docstatus", "remarks"], as_dict=True, for_update=True,
    )
    if record and (record.remarks != remarks or record.docstatus != 1):
        frappe.throw("IDEMPOTENCY_CONFLICT")
    return remarks, record


@frappe.whitelist(methods=["POST"])
def movement(action, stockItemId, warehouseLocationId, quantity, requestId, batchSelection=None):
    transit.require_operator()
    warehouse = transit.location_for(warehouseLocationId, fixtures)
    item = fixtures.ITEMS.get(stockItemId) if isinstance(stockItemId, str) else None
    if not item or stockItemId in fixtures.TRANSFER_ITEMS or action not in ("RECEIPT", "ISSUE"):
        frappe.throw("INVALID_MOVEMENT")
    number = parse_selection(batchSelection)
    uom = fixtures.FRACTION_UOM if stockItemId == "FRACTION" else "PC"
    qty = quantity_for(quantity, 3 if stockItemId == "FRACTION" else 0)
    remarks, existing = movement_record(*request_identity(requestId, [action, item, warehouse, number, quantity]))
    if existing:
        return {"documentId": existing.name, "replayed": True}
    token = SERVICE_WRITE.set(True)
    try:
        batch = find_batch(item, number, create=action == "RECEIPT")
        if action == "ISSUE" and transit.stock_quantity(item, batch, warehouse, fixtures.COMPANY) < qty:
            frappe.throw("INSUFFICIENT_STOCK")
        name = post_entry(action, item, warehouse, qty, batch, remarks, company=fixtures.COMPANY, uom=uom)
    finally:
        SERVICE_WRITE.reset(token)
    return {"documentId": name, "replayed": False}


def validate_count_ledger(doc, expected):
    actual = {}
    for row in doc.items:
        previous, measured = expected[row.batch_no]
        if (Decimal(str(row.current_qty)) != previous or Decimal(str(row.qty)) != measured
                or Decimal(str(row.quantity_difference)) != measured - previous):
            frappe.throw("COUNT_STOCK_CHANGED")
        actual[row.batch_no] = (previous, measured)
    if actual != {batch: values for batch, values in expected.items() if values[0] != values[1]}:
        frappe.throw("COUNT_UNEXPECTED_ROWS")
    entries = frappe.get_all(
        "Stock Ledger Entry", filters={"voucher_type": doc.doctype, "voucher_no": doc.name, "is_cancelled": 0},
        fields=["actual_qty", "serial_and_batch_bundle", "voucher_detail_no", "warehouse"],
    )
    allocated = {row.name: [] for row in doc.items}
    for entry in entries:
        bundle = frappe.get_doc("Serial and Batch Bundle", entry.serial_and_batch_bundle)
        child = next((row for row in doc.items if row.name == entry.voucher_detail_no), None)
        if (not child or child.warehouse != entry.warehouse or bundle.warehouse != entry.warehouse
                or bundle.docstatus != 1 or len(bundle.entries) != 1
                or bundle.entries[0].batch_no != child.batch_no
                or Decimal(str(bundle.entries[0].qty)) != Decimal(str(entry.actual_qty))):
            frappe.throw("COUNT_UNEXPECTED_BUNDLE")
        allocated[child.name].append(Decimal(str(entry.actual_qty)))
    for row in doc.items:
        previous, measured = expected[row.batch_no]
        if sorted(allocated[row.name]) != sorted(value for value in (-previous, measured) if value):
            frappe.throw("COUNT_UNEXPECTED_LEDGER")


def save_count_request(key, fingerprint, voucher):
    frappe.get_doc({
        "doctype": "Stage 1E Count Request", "request_key": key, "payload_hash": fingerprint,
        "document": voucher,
    }).insert(ignore_permissions=True)


@frappe.whitelist(methods=["POST"])
def count(stockItemId, warehouseLocationId, requestId, counts):
    transit.require_operator()
    warehouse = transit.location_for(warehouseLocationId, fixtures)
    if stockItemId not in ("COUNT", "COUNT-ZERO", "COUNT-RACE"):
        frappe.throw("INVALID_COUNT_ITEM")
    if not isinstance(counts, list) or not 1 <= len(counts) <= 3:
        frappe.throw("INVALID_COUNT_ROWS")
    item = fixtures.ITEMS[stockItemId]
    selections = {}
    for row in counts:
        if not isinstance(row, dict) or set(row) - {"batchSelection", "quantity"}:
            frappe.throw("INVALID_COUNT_ROWS")
        number = parse_selection(row.get("batchSelection"))
        if number in selections:
            frappe.throw("DUPLICATE_COUNT_BATCH")
        selections[number] = quantity_for(row.get("quantity"), 0, allow_zero=True)
    payload = sorted(((number, str(qty)) for number, qty in selections.items()), key=lambda pair: pair[0] or "")
    key, fingerprint = request_identity(requestId, [item, warehouse, payload])
    existing = frappe.db.get_value(
        "Stage 1E Count Request", key, ["payload_hash", "document"], as_dict=True, for_update=True,
    )
    if existing:
        status = frappe.db.get_value("Stock Reconciliation", existing.document, "docstatus", for_update=True)
        if existing.payload_hash != fingerprint or status != 1:
            frappe.throw("IDEMPOTENCY_CONFLICT")
        return {"documentId": existing.document, "replayed": True}
    expected = {}
    for number, qty in selections.items():
        batch = find_batch(item, number)
        previous = transit.stock_quantity(item, batch, warehouse, fixtures.COMPANY)
        if previous != previous.to_integral_value():
            frappe.throw("COUNT_NON_INTEGER_STOCK")
        expected[batch] = (previous, qty)
    changed = {batch: values for batch, values in expected.items() if values[0] != values[1]}
    if not changed:
        frappe.throw("COUNT_WITHOUT_CHANGES")
    if any(not previous for previous, _ in changed.values()):
        frappe.throw("COUNT_EMPTY_BUCKET_UNSUPPORTED")
    token = SERVICE_WRITE.set(True)
    try:
        now = get_datetime()
        doc = frappe.get_doc({
            "doctype": "Stock Reconciliation", "company": fixtures.COMPANY, "purpose": "Stock Reconciliation",
            "posting_date": now.date(), "posting_time": now.time(), "set_posting_time": 1,
        })
        for batch, (previous, measured) in changed.items():
            # Pass the exact observed bucket as a native current Bundle. The implicit selector
            # requires broad Stock Reconciliation write permission even during document validation.
            bundle = frappe.get_doc({
                "doctype": "Serial and Batch Bundle", "company": fixtures.COMPANY,
                "item_code": item, "warehouse": warehouse, "posting_datetime": now,
                "voucher_type": doc.doctype, "type_of_transaction": "Outward",
                "entries": [{"batch_no": batch, "qty": -int(previous), "warehouse": warehouse}],
            }).insert(ignore_permissions=True)
            doc.append("items", {
                "item_code": item, "warehouse": warehouse, "batch_no": batch, "qty": int(measured),
                "current_qty": int(previous), "current_serial_and_batch_bundle": bundle.name,
                "use_serial_batch_fields": 1, "reconcile_all_serial_batch": 0,
                "valuation_rate": 0, "allow_zero_valuation_rate": 1,
            })
        doc.insert(ignore_permissions=True)
        doc.submit()
        validate_count_ledger(doc, expected)
        save_count_request(key, fingerprint, doc.name)
    finally:
        SERVICE_WRITE.reset(token)
    return {"documentId": doc.name, "replayed": False}


@frappe.whitelist(methods=["POST"])
def dispatch(stockItemId, sourceLocationId, targetLocationId, quantity, requestId, batchSelection=None):
    return transit.dispatch_transfer(fixtures, fixtures.TRANSFER_ITEMS, "HP1E", stockItemId, sourceLocationId,
                                     targetLocationId, quantity, requestId, batchSelection)


@frappe.whitelist(methods=["POST"])
def receive(transferId, stockItemId, targetLocationId, quantity, requestId, batchSelection=None):
    return transit.receive_transfer(fixtures, fixtures.TRANSFER_ITEMS, "HP1E", transferId, stockItemId,
                                    targetLocationId, quantity, requestId, batchSelection)
