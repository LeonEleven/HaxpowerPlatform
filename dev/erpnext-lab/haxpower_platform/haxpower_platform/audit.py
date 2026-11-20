"""Stage 1F single-line audit and count version experiment. No service commits."""

import json
import re
from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal
from hashlib import sha256

import frappe
from frappe.utils import get_datetime

from haxpower_platform import audit_fixtures as fixtures, lightweight_fixtures, transit
from haxpower_platform.integrity import quantity_for, validate_count_ledger
from haxpower_platform.stock import SITE, SERVICE_WRITE, batch_key, find_batch, parse_selection, post_entry, require_warehouse

EVENT = "Stage 1F Audit Event"
TASK = "Stage 1F Count Task"
LOCATIONS = {**fixtures.LOCATIONS, "TRANSIT": fixtures.TRANSIT}
STOCK_WRITE = ContextVar("stage1f_stock_write", default=False)
APPEND = ContextVar("stage1f_append", default=False)


@contextmanager
def stock_write():
    token = STOCK_WRITE.set(True)
    shared = SERVICE_WRITE.set(True)
    try:
        yield
    finally:
        SERVICE_WRITE.reset(shared)
        STOCK_WRITE.reset(token)


def refuse_change(doc, *args):
    frappe.throw("STAGE1F_APPEND_ONLY", frappe.PermissionError)


def require_append(doc):
    if not APPEND.get() or not doc.is_new():
        refuse_change(doc)


def append_record(doctype, **fields):
    token = APPEND.set(True)
    try:
        return frappe.get_doc({"doctype": doctype, **fields}).insert(ignore_permissions=True)
    finally:
        APPEND.reset(token)


def protect_stock(doc, method=None):
    if frappe.local.site != SITE:
        return
    for version in (doc, doc.get_doc_before_save()):
        if version and (version.get("company") == fixtures.COMPANY or version.get("item_code") in fixtures.ITEMS.values()
                        or any(row.item_code in fixtures.ITEMS.values() for row in version.get("items") or ())):
            if not STOCK_WRITE.get() or method in ("before_cancel", "on_trash"):
                frappe.throw("STAGE1F_USE_SERVICE", frappe.PermissionError)


def protect_repost(doc, method=None):
    if frappe.local.site != SITE or not doc.recreate_stock_ledgers:
        return
    protected = (fixtures, lightweight_fixtures)
    voucher_company = (
        frappe.db.get_value(doc.voucher_type, doc.voucher_no, "company")
        if doc.voucher_type in ("Stock Entry", "Stock Reconciliation") and doc.voucher_no else None
    )
    if any(doc.company == scope.COMPANY or voucher_company == scope.COMPANY or doc.item_code in scope.ITEMS.values()
           for scope in protected):
        frappe.throw("STAGE1F_RECREATE_LEDGER_DENIED", frappe.PermissionError)


def protect_deletion(doc, method=None):
    if frappe.local.site != SITE:
        return
    if doc.company in (fixtures.COMPANY, lightweight_fixtures.COMPANY) or any(
        row.doctype_name in (EVENT, TASK, lightweight_fixtures.LOG) for row in doc.doctypes_to_delete
    ):
        frappe.throw("STAGE1F_BULK_DELETE_DENIED", frappe.PermissionError)


def require_record_id(key):
    if not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{64}", key):
        frappe.throw("INVALID_STAGE1F_RECORD_ID")


def scope(sku, location, selection):
    warehouse = LOCATIONS.get(location) if isinstance(location, str) and location != "TRANSIT" else None
    require_warehouse(warehouse, fixtures.COMPANY)
    if not isinstance(sku, str) or sku not in fixtures.ITEMS:
        frappe.throw("PRODUCT_NOT_FOUND")
    return fixtures.ITEMS[sku], parse_selection(selection)


def request_identity(request_id, payload):
    if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", request_id):
        frappe.throw("INVALID_REQUEST_ID")
    transit.lock_company(fixtures)
    if frappe.db.get_value("Company", fixtures.COMPANY, "enable_perpetual_inventory", for_update=True):
        frappe.throw("STAGE1F_PERPETUAL_INVENTORY_ENABLED")
    key = sha256(f"{fixtures.COMPANY}\0{frappe.session.user}\0{request_id}".encode()).hexdigest()
    fingerprint = sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()
    return key, fingerprint


def stored(doctype, key):
    return frappe.db.get_value(doctype, key, "*", as_dict=True, for_update=True)


def result(event, replayed):
    return {"eventId": event.name, "batch": event.batch_number or None, "replayed": replayed}


def replay(key, fingerprint):
    event = stored(EVENT, key)
    if event and event.payload_hash != fingerprint:
        frappe.throw("IDEMPOTENCY_CONFLICT")
    if event and frappe.db.get_value(event.document_type, event.document_id, "docstatus", for_update=True) != 1:
        frappe.throw("STAGE1F_AUDIT_MISMATCH")
    return event


def get_events(**filters):
    return frappe.db.get_values(EVENT, filters, "*", as_dict=True, for_update=True)


def bucket(item, batch, location):
    """Current read under the writer lock. Compare the signed net of every voucher."""
    signed = defaultdict(Decimal)
    event_ids = []
    for field, direction in (("source_location", -1), ("target_location", 1)):
        for event in get_events(item_code=item, batch_key=batch, **{field: location}):
            signed[event.document_type, event.document_id, event.document_line] += direction * abs(Decimal(str(event.quantity_delta)))
            event_ids.append(event.name)

    sle = frappe.qb.DocType("Stock Ledger Entry")
    allocation = frappe.qb.DocType("Serial and Batch Entry")
    entries = (
        frappe.qb.from_(sle)
        .join(allocation).on(allocation.parent == sle.serial_and_batch_bundle)
        .select(sle.name, allocation.name, sle.voucher_type, sle.voucher_no, sle.voucher_detail_no, allocation.qty)
        .where((sle.company == fixtures.COMPANY) & (sle.item_code == item) & (sle.warehouse == LOCATIONS[location])
               & (sle.is_cancelled == 0) & (allocation.batch_no == batch) & (allocation.docstatus == 1))
        .for_update().run()
    )
    native = defaultdict(Decimal)
    for _, _, kind, voucher, line, quantity in entries:
        native[kind, voucher, line] += Decimal(str(quantity))
    if native != signed:
        frappe.throw("STAGE1F_AUDIT_MISMATCH")
    version = sha256(json.dumps([sorted(event_ids), sorted(entries)], default=str).encode()).hexdigest()
    return sum(signed.values(), Decimal(0)), version


def save_event(key, fingerprint, action, voucher, item, number, quantity, source, target, **relations):
    return append_record(
        EVENT, record_id=key, payload_hash=fingerprint, action=action, document_type=voucher.doctype,
        document_id=voucher.name, document_line=voucher.items[0].name, item_code=item, batch_number=number,
        batch_key=batch_key(item, number), source_location=source, target_location=target,
        quantity_delta=-quantity if source and not target else quantity, actor=frappe.session.user,
        occurred_at=get_datetime(), **relations,
    )


def native_transfer(item, batch, quantity, source, target, dispatch=None):
    outgoing = dispatch.document_id if dispatch else None
    row = dispatch.document_line if dispatch else None
    name = transit.submit_stock(
        item, batch, quantity, LOCATIONS[source], LOCATIONS[target], "HP1F fixture",
        company=fixtures.COMPANY, outgoing=outgoing, source_row=row, start_transit=target == "TRANSIT",
    )
    return frappe.get_doc("Stock Entry", name)


def native_adjustment(item, batch, quantity, source, target):
    location = source or target
    name = post_entry("ISSUE" if source else "RECEIPT", item, LOCATIONS[location], quantity, batch, "HP1F fixture",
                      company=fixtures.COMPANY)
    return frappe.get_doc("Stock Entry", name)


def dispatch_remaining(dispatch):
    receipts = get_events(dispatch_event=dispatch.name, action="RECEIVE")
    received = sum((Decimal(str(event.quantity_delta)) for event in receipts), Decimal(0))
    posted = frappe.db.get_value("Stock Entry Detail", dispatch.document_line, "transferred_qty", for_update=True)
    if posted is None or received != Decimal(str(posted)):
        frappe.throw("STAGE1F_TRANSFER_MISMATCH")
    return Decimal(str(dispatch.quantity_delta)) - received


@frappe.whitelist(methods=["POST"])
def move(action, sku, location, batchSelection, quantity, requestId, targetLocation=None, dispatchId=None):
    transit.require_operator()
    item, number = scope(sku, location, batchSelection)
    amount = quantity_for(quantity, 0)
    if action not in ("RECEIPT", "ISSUE", "DISPATCH", "RECEIVE"):
        frappe.throw("INVALID_MOVEMENT")
    if action in ("DISPATCH", "RECEIVE"):
        scope(sku, targetLocation, batchSelection)
        if targetLocation == location:
            frappe.throw("TRANSFER_SAME_LOCATION")
    if ((action == "RECEIVE") != bool(dispatchId)
            or (action in ("DISPATCH", "RECEIVE")) != bool(targetLocation)):
        frappe.throw("INVALID_MOVEMENT")
    if action == "RECEIVE":
        require_record_id(dispatchId)
    key, fingerprint = request_identity(requestId, [action, sku, location, number, str(amount), targetLocation, dispatchId])
    if event := replay(key, fingerprint):
        return result(event, True)

    source, target, dispatch = None, None, None
    relations = {}
    with stock_write():
        batch = find_batch(item, number, create=action == "RECEIPT")
        if action == "RECEIVE":
            dispatch = stored(EVENT, dispatchId)
            if (not dispatch or dispatch.action != "DISPATCH" or dispatch.item_code != item
                    or dispatch.batch_key != batch or dispatch.source_location != location
                    or dispatch.planned_target != targetLocation):
                frappe.throw("STAGE1F_TRANSFER_NOT_FOUND")
            if get_events(reversal_of=dispatch.name) or amount > dispatch_remaining(dispatch):
                frappe.throw("STAGE1F_TRANSFER_CLOSED_OR_EXCEEDS_REMAINDER")
            source, target = "TRANSIT", targetLocation
            relations["dispatch_event"] = dispatch.name
        elif action == "DISPATCH":
            source, target = location, "TRANSIT"
            relations["planned_target"] = targetLocation
        elif action == "RECEIPT":
            target = location
        else:
            source = location
        if source and bucket(item, batch, source)[0] < amount:
            frappe.throw("INSUFFICIENT_STOCK")
        if target:
            bucket(item, batch, target)
        if source and target:
            voucher = native_transfer(item, batch, amount, source, target, dispatch)
        else:
            voucher = native_adjustment(item, batch, amount, source, target)
    event = save_event(key, fingerprint, action, voucher, item, number, amount, source, target, **relations)
    return result(event, False)


@frappe.whitelist(methods=["POST"])
def correct(eventId, requestId):
    transit.require_operator()
    require_record_id(eventId)
    key, fingerprint = request_identity(requestId, ["CORRECT", eventId])
    original = stored(EVENT, eventId)
    if not original or original.action not in ("RECEIPT", "ISSUE", "COUNT", "DISPATCH"):
        frappe.throw("STAGE1F_UNSUPPORTED_CORRECTION")
    for location in {original.source_location, original.target_location, original.planned_target} - {None, "", "TRANSIT"}:
        require_warehouse(LOCATIONS[location], fixtures.COMPANY)
    if event := replay(key, fingerprint):
        return result(event, True)
    if get_events(reversal_of=original.name):
        frappe.throw("STAGE1F_ALREADY_CORRECTED")
    if frappe.db.get_value(original.document_type, original.document_id, "docstatus", for_update=True) != 1:
        frappe.throw("STAGE1F_AUDIT_MISMATCH")
    source, target = original.target_location or None, original.source_location or None
    amount = abs(Decimal(str(original.quantity_delta)))
    if original.action == "DISPATCH":
        amount = dispatch_remaining(original)
        if amount <= 0:
            frappe.throw("STAGE1F_COMPLETED_TRANSFER_REQUIRES_REVIEW")
    if source and bucket(original.item_code, original.batch_key, source)[0] < amount:
        frappe.throw("INSUFFICIENT_STOCK_FOR_CORRECTION")
    if target:
        bucket(original.item_code, original.batch_key, target)
    with stock_write():
        writer = native_transfer if source and target else native_adjustment
        voucher = writer(original.item_code, original.batch_key, amount, source, target)
    event = save_event(key, fingerprint, "RETURN_TRANSIT" if original.action == "DISPATCH" else "CORRECTION",
                       voucher, original.item_code, original.batch_number or None, amount, source, target,
                       reversal_of=original.name)
    return result(event, False)


@frappe.whitelist(methods=["POST"])
def create_count(sku, location, batchSelection, requestId):
    transit.require_operator()
    item, number = scope(sku, location, batchSelection)
    key, _ = request_identity(requestId, ["COUNT_TASK", sku, location, number])
    # Separate namespaces for task IDs and stock request IDs.
    key = sha256(("COUNT_TASK:" + key).encode()).hexdigest()
    batch = batch_key(item, number)
    task = stored(TASK, key)
    if task and (task.item_code, task.location, task.batch_key) != (item, location, batch):
        frappe.throw("IDEMPOTENCY_CONFLICT")
    if not task:
        baseline, version = bucket(item, batch, location)
        task = append_record(TASK, record_id=key, item_code=item, location=location, batch_key=batch,
                             batch_number=number, baseline=baseline, ledger_version=version, actor=frappe.session.user)
    return {"taskId": key, "batch": number, "baseline": str(int(task.baseline)), "version": task.ledger_version}


def reconcile_count(task, previous, measured, *, scope=fixtures):
    batch, warehouse = task.batch_key, scope.LOCATIONS[task.location]
    now = get_datetime()
    bundle = frappe.get_doc({
        "doctype": "Serial and Batch Bundle", "company": scope.COMPANY, "item_code": task.item_code,
        "warehouse": warehouse, "posting_datetime": now, "voucher_type": "Stock Reconciliation",
        "type_of_transaction": "Outward",
        "entries": [{"batch_no": batch, "qty": -int(previous), "warehouse": warehouse}],
    }).insert(ignore_permissions=True)
    doc = frappe.get_doc({
        "doctype": "Stock Reconciliation", "company": scope.COMPANY, "purpose": "Stock Reconciliation",
        "posting_date": now.date(), "posting_time": now.time(), "set_posting_time": 1,
        "items": [{"item_code": task.item_code, "warehouse": warehouse, "batch_no": batch, "qty": int(measured),
                   "current_qty": int(previous), "current_serial_and_batch_bundle": bundle.name,
                   "use_serial_batch_fields": 1, "reconcile_all_serial_batch": 0,
                   "valuation_rate": 0, "allow_zero_valuation_rate": 1}],
    }).insert(ignore_permissions=True)
    doc.submit()
    validate_count_ledger(doc, {batch: (previous, measured)})
    return doc


@frappe.whitelist(methods=["POST"])
def submit_count(taskId, quantity, requestId):
    transit.require_operator()
    require_record_id(taskId)
    measured = quantity_for(quantity, 0, allow_zero=True)
    key, fingerprint = request_identity(requestId, ["COUNT", taskId, str(measured)])
    task = stored(TASK, taskId)
    if not task or task.actor != frappe.session.user:
        frappe.throw("STAGE1F_COUNT_NOT_FOUND", frappe.PermissionError)
    require_warehouse(LOCATIONS[task.location], fixtures.COMPANY)
    if event := replay(key, fingerprint):
        return result(event, True)
    if get_events(count_task=task.name):
        frappe.throw("STAGE1F_COUNT_ALREADY_SUBMITTED")
    previous, version = bucket(task.item_code, task.batch_key, task.location)
    if version != task.ledger_version or previous != Decimal(str(task.baseline)):
        frappe.throw("STAGE1F_COUNT_STALE")
    delta = measured - previous
    if not delta:
        frappe.throw("COUNT_WITHOUT_CHANGES")
    source, target = (task.location, None) if delta < 0 else (None, task.location)
    number = task.batch_number or None
    with stock_write():
        find_batch(task.item_code, number, create=not previous)
        if previous:
            voucher = reconcile_count(task, previous, measured)
        else:
            # The zero bucket has no current Bundle for ERPNext's scoped reconciliation.
            voucher = native_adjustment(task.item_code, task.batch_key, delta, None, task.location)
    event = save_event(key, fingerprint, "COUNT", voucher, task.item_code, number, abs(delta), source, target,
                       count_task=task.name)
    return result(event, False)
