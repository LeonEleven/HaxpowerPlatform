"""Stage 2A API for the independent Frappe inventory experiment."""

import json
import re
from decimal import Decimal

import frappe

from haxpower_stock_spike import store

OPERATOR = "HP Spike Operator"
QUERY = "HP Spike Query"


def fail(message, permission=False):
    frappe.throw(message, frappe.PermissionError if permission else frappe.ValidationError)


def require_role(*, write=False):
    store.require_site()
    allowed = {OPERATOR} if write else {OPERATOR, QUERY}
    if allowed.isdisjoint(frappe.get_roles()):
        fail("INVENTORY_OPERATION_DENIED", permission=True)


def require_locations(locations):
    scope = frappe.db.get_value(store.SCOPE, frappe.session.user, ["mode", "warehouses"], as_dict=True)
    if not scope or scope.mode not in ("ALL", "SELECTED"):
        fail("WAREHOUSE_ACCESS_DENIED", permission=True)
    allowed = set(json.loads(scope.warehouses or "[]")) if scope.mode == "SELECTED" else None
    for name in set(locations):
        location = frappe.db.get_value(store.LOCATION, name, ["name", "warehouse_id", "in_transit"], as_dict=True)
        if (not location or location.name != name or location.in_transit
                or (allowed is not None and location.warehouse_id not in allowed)):
            fail("WAREHOUSE_ACCESS_DENIED", permission=True)


def name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value):
        fail("INVALID_IDENTIFIER")
    return value


def reference(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        fail("INVALID_STOCK_REFERENCE")
    return value


def batch_id(selection):
    if not isinstance(selection, dict) or "mode" not in selection:
        fail("BATCH_SELECTION_REQUIRED")
    if selection == {"mode": "NO_BATCH"}:
        return None
    if set(selection) == {"mode", "batchNo"} and selection["mode"] == "BATCH":
        code = selection["batchNo"]
        if (isinstance(code, str) and 1 <= len(code) <= 80 and code == code.strip()
                and all(char.isprintable() and char not in "<>" for char in code)):
            return code
    fail("INVALID_BATCH_SELECTION")


def sku_precision(sku):
    row = frappe.db.get_value(store.SKU, name(sku), ["name", "precision"], as_dict=True)
    if not row or row.name != sku or row.precision not in (0, 3):
        fail("SKU_NOT_FOUND_OR_UNSUPPORTED_PRECISION")
    return row.precision


def quantity(value, precision, *, allow_zero=False):
    pattern = r"(?:0|[1-9][0-9]{0,5})"
    if precision:
        pattern += r"(?:\.[0-9]{1," + str(precision) + "})?"
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        fail("INVALID_QUANTITY")
    amount = Decimal(value)
    if not amount and not allow_zero:
        fail("INVALID_QUANTITY")
    return amount


def amount_text(value):
    return format(value.normalize(), "f")


def record(request_id, action, payload, process):
    request_id = name(request_id)
    key = store.identity(frappe.session.user, request_id)
    fingerprint = store.identity(action, payload)
    existing = store.reserve_operation(key, fingerprint, action)
    if existing.payload_hash != fingerprint:
        fail("IDEMPOTENCY_CONFLICT")
    if existing.outcome:
        saved = json.loads(existing.outcome)
        require_locations(saved["locations"])
        return {**saved["result"], "replayed": True}
    locations, result = process(key)
    result = {"operationId": key, **result}
    store.finish_operation(key, {"locations": list(locations), "result": result})
    return {**result, "replayed": False}


@frappe.whitelist(methods=["GET"])
def balance(sku, location, batchMode=None, batchNo=None):
    require_role()
    location = name(location)
    require_locations((location,))
    sku_precision(sku)
    selection = {"mode": batchMode} if batchMode else {}
    if batchNo is not None:
        selection["batchNo"] = batchNo
    batch = batch_id(selection)
    row = frappe.db.sql("""
        select cast(quantity as char), bucket_version
        from `tabHP Spike Balance` where name = %s
    """, (store.identity(sku, location, batch),))
    value, version = row[0] if row else ("0", 0)
    return {"sku": sku, "location": location, "batch": batch, "quantity": amount_text(Decimal(value)), "version": version}


@frappe.whitelist(methods=["POST"])
def move(action, requestId, lines):
    require_role(write=True)
    if action not in ("RECEIPT", "ISSUE"):
        fail("INVALID_MOVEMENT")
    if not isinstance(lines, list) or not 1 <= len(lines) <= 20:
        fail("INVALID_MOVEMENT_LINES")

    required = {"sku", "location", "quantity"}
    if any(not isinstance(line, dict) or not required <= set(line) <= required | {"batchSelection"} for line in lines):
        fail("INVALID_MOVEMENT_LINE")
    locations = sorted({name(line["location"]) for line in lines})
    require_locations(locations)
    prepared = []
    for line in lines:
        dim = (line["sku"], line["location"], batch_id(line.get("batchSelection")))
        value = quantity(line["quantity"], sku_precision(line["sku"]))
        prepared.append((dim, value if action == "RECEIPT" else -value))

    def apply(operation):
        buckets = store.lock_buckets(dim for dim, _ in prepared)
        event_ids = [store.post_leg(operation, action, index, buckets[dim], delta)
                     for index, (dim, delta) in enumerate(prepared)]
        return locations, {"eventIds": event_ids, "batches": [dim[2] for dim, _ in prepared]}

    return record(requestId, action, lines, apply)


def transfer_result(transfer, batch, received, total, event_ids):
    return {"transferId": transfer, "batch": batch, "received": amount_text(received),
            "remaining": amount_text(total - received), "eventIds": event_ids}


@frappe.whitelist(methods=["POST"])
def dispatch(sku, source, target, quantityText, requestId, batchSelection=None):
    require_role(write=True)
    source, target = name(source), name(target)
    require_locations((source, target))
    if source == target:
        fail("TRANSFER_SAME_LOCATION")
    batch = batch_id(batchSelection)
    qty = quantity(quantityText, sku_precision(sku))

    def apply(operation):
        src, dst = (sku, source, batch), (sku, store.TRANSIT, batch)
        buckets = store.lock_buckets((src, dst))
        ids = [store.post_leg(operation, "DISPATCH", 0, buckets[src], -qty),
               store.post_leg(operation, "DISPATCH", 0, buckets[dst], qty)]
        store.insert_row(store.TRANSFER, operation, {"sku": sku, "source": source, "target": target, "batch_id": batch,
                                                     "dispatched": str(qty), "received": "0"})
        return (source, target), transfer_result(operation, batch, Decimal(0), qty, ids)

    return record(requestId, "DISPATCH", [sku, source, target, batch, str(qty)], apply)


@frappe.whitelist(methods=["POST"])
def receive(transferId, quantityText, requestId):
    require_role(write=True)
    transfer_id = reference(transferId)

    def apply(operation):
        original = store.current(store.TRANSFER, transfer_id, """
            sku, source, target, batch_id, cast(dispatched as char) as dispatched, cast(received as char) as received
        """)
        if not original:
            fail("TRANSFER_ACCESS_DENIED", permission=True)
        locations = (original.source, original.target)
        require_locations(locations)
        qty = quantity(quantityText, sku_precision(original.sku))
        total, received = Decimal(original.dispatched), Decimal(original.received)
        if received == total:
            fail("TRANSFER_COMPLETED")
        if qty > total - received:
            fail("TRANSFER_RECEIPT_EXCEEDS_REMAINING")
        src = (original.sku, store.TRANSIT, original.batch_id)
        dst = (original.sku, original.target, original.batch_id)
        buckets = store.lock_buckets((src, dst))
        ids = [store.post_leg(operation, "RECEIVE", 0, buckets[src], -qty),
               store.post_leg(operation, "RECEIVE", 0, buckets[dst], qty)]
        frappe.db.sql("""
            update `tabHP Spike Transfer` set received = %s where name = %s
        """, (str(received + qty), transfer_id))
        frappe.db.sql("update `tabHP Spike Operation` set transfer_ref = %s where name = %s",
                      (transfer_id, operation))
        return locations, transfer_result(transfer_id, original.batch_id, received + qty, total, ids)

    return record(requestId, "RECEIVE", [transfer_id, quantityText], apply)


@frappe.whitelist(methods=["POST"])
def create_count(sku, location, requestId, batchSelection=None):
    require_role(write=True)
    sku_precision(sku)
    location = name(location)
    require_locations((location,))
    batch = batch_id(batchSelection)

    def apply(operation):
        dim = (sku, location, batch)
        bucket = store.lock_buckets((dim,))[dim]
        store.insert_row(store.TASK, operation, {"actor": frappe.session.user, "sku": sku, "location": location,
                                                 "batch_id": batch, "baseline": str(bucket.quantity),
                                                 "baseline_version": bucket.bucket_version})
        return (location,), {"taskId": operation, "baseline": amount_text(bucket.quantity),
                             "version": bucket.bucket_version, "batch": batch}

    return record(requestId, "COUNT_CREATE", [sku, location, batch], apply)


@frappe.whitelist(methods=["POST"])
def submit_count(taskId, quantityText, requestId):
    require_role(write=True)
    task_id = reference(taskId)

    def apply(operation):
        task = store.current(store.TASK, task_id, """
            actor, sku, location, batch_id, cast(baseline as char) as baseline, baseline_version, applied_operation
        """)
        if not task or task.actor != frappe.session.user:
            fail("COUNT_TASK_ACCESS_DENIED", permission=True)
        require_locations((task.location,))
        if task.applied_operation:
            fail("COUNT_ALREADY_SUBMITTED")
        measured = quantity(quantityText, sku_precision(task.sku), allow_zero=True)
        dim = (task.sku, task.location, task.batch_id)
        bucket = store.lock_buckets((dim,))[dim]
        if bucket.bucket_version != task.baseline_version or bucket.quantity != Decimal(task.baseline):
            fail("COUNT_STALE")
        delta = measured - bucket.quantity
        ids = [store.post_leg(operation, "COUNT", 0, bucket, delta)] if delta else []
        frappe.db.sql("update `tabHP Spike Count Task` set applied_operation = %s where name = %s",
                      (operation, task_id))
        return (task.location,), {"taskId": task_id, "batch": task.batch_id, "eventIds": ids,
                                  "quantity": amount_text(measured)}

    return record(requestId, "COUNT", [task_id, quantityText], apply)


@frappe.whitelist(methods=["POST"])
def correct(eventId, requestId):
    require_role(write=True)
    event_id = reference(eventId)

    def apply(operation):
        # The original event is immutable. Take no event lock before the bucket lock.
        originals = frappe.db.sql("""
            select operation_type, sku, location, batch_id, bucket_version, cast(delta as char) as delta
            from `tabHP Spike Event` where name = %s
        """, (event_id,), as_dict=True)
        if not originals:
            fail("STOCK_EVENT_ACCESS_DENIED", permission=True)
        original = originals[0]
        require_locations((original.location,))
        if original.operation_type not in ("RECEIPT", "ISSUE"):
            fail("UNSUPPORTED_CORRECTION")
        dim = (original.sku, original.location, original.batch_id)
        bucket = store.lock_buckets((dim,))[dim]
        later = frappe.db.sql("""
            select reversal_of, delta < 0 as outgoing from `tabHP Spike Event`
            where bucket = %s and bucket_version > %s for update
        """, (bucket.name, original.bucket_version), as_dict=True)
        if any(row.reversal_of == event_id for row in later):
            fail("ALREADY_CORRECTED")
        if original.operation_type == "RECEIPT" and any(row.outgoing for row in later):
            fail("DOWNSTREAM_STOCK_REVIEW_REQUIRED")
        correction = store.post_leg(operation, "CORRECTION", 0, bucket, -Decimal(original.delta), reversal_of=event_id)
        return (original.location,), {"eventIds": [correction], "batch": original.batch_id}

    return record(requestId, "CORRECTION", event_id, apply)
