"""Read historical quantity evidence under the shared stock write guard."""

from collections import defaultdict
from dataclasses import asdict, replace
from decimal import Decimal, localcontext
import json

import frappe

from ...access import policy
from ...approval import EVENT
from ...approval import policy as rules, persistence as approvals
from .. import versions
from ..batch.persistence import MAPPING
from ..operations import persistence as stock
from ..operations.dto import Bucket, Line, StockError
from ..operations.service import _lock_document
from ..stock_count import service as counts
from ..transfer import service as transfers
from . import CASE

INVALID = "CORRECTION_ORIGINAL_FACT_INVALID"
ORDER = "CORRECTION_HISTORY_ORDER_UNPROVEN"
DOWNSTREAM = "CORRECTION_DOWNSTREAM_QUANTITY"
RECOUNT = "CORRECTION_DOWNSTREAM_COUNT"
TRANSFER = "CORRECTION_TRANSFER_REVIEW"
UNSUPPORTED = "CORRECTION_ORIGIN_UNSUPPORTED"

OPERATIONS = {
    rules.RECEIPT: policy.Operation.RECEIVE,
    rules.ISSUE: policy.Operation.ISSUE,
    rules.COUNT: policy.Operation.COUNT,
    rules.TRANSFER: policy.Operation.TRANSFER,
}


def document(company, document_id):
    row = stock.current(stock.DOCUMENT, document_id, (
        "name", "company", "document_type", "effective_operation", "status", "revision", "owner",
    ))
    if not row or row.name != document_id or row.company != company:
        policy.deny()
    return row


def scope(company, header):
    if header.document_type == rules.COUNT:
        task = stock.current(versions.TASK, header.name, ("company", "location_id"))
        if not task or task.company != company:
            raise StockError(INVALID)
        return tuple(policy.lock_execution_locations(company, (task.location_id,)))
    _, lines = _lock_document(company, header.name, header.document_type)
    locations = policy.lock_execution_locations(company, (
        location for line in lines for location in (line.source, line.target) if location
    ))
    return tuple(locations)


def origin(company, operation_id, *, capability=True):
    operation = stock.current(stock.LOG, operation_id, (
        "name", "document_id", "creation", "status", "action", "effect_key", "quantity_effect",
        "result_revision", "result_status", "request_id", "original_operation", "actor", "native_actor",
    ))
    if not operation or operation.name != operation_id:
        policy.deny()
    header = document(company, operation.document_id)
    if capability:
        policy.require_operation(OPERATIONS.get(header.document_type, policy.Operation.CORRECTION_APPROVE), locked=True)
    locations = scope(company, header)
    if operation.status != "Effective" or operation.result_revision > header.revision:
        raise StockError(INVALID)
    if header.document_type in (*OPERATIONS, rules.CORRECTION):
        if header.effective_operation != operation_id or operation.action != header.document_type:
            raise StockError(INVALID)
    return operation, header, locations


def _verify_origin_request(company, operation, header):
    request = stock.current(stock.REQUEST, operation.request_id, (
        "company", "actor", "command", "document_id", "result_operation", "status", "result_revision",
    ))
    if not request or tuple(request.values()) != (
        company, operation.actor, operation.action, header.name, operation.name, "Completed", operation.result_revision,
    ):
        raise StockError(INVALID)


def original_lines(company, header):
    if header.document_type == rules.COUNT:
        return counts._kernel_lines(counts._rows(header.name))
    lines = _lock_document(company, header.name, header.document_type)[1]
    if header.document_type != rules.RECEIPT:
        return lines
    # Receipt drafts predate batch binding. Resolve the recorded domain text
    # against the submitted allocation when validating the native quantity.
    resolved = []
    for line in lines:
        if line.batch_kind == "BATCH":
            rows = frappe.db.sql(f"""SELECT name FROM `tab{MAPPING}`
                WHERE sku=%s AND kind='BATCH' AND BINARY original_text=BINARY %s FOR UPDATE""",
                                 (line.sku_id, line.batch_no))
            line = replace(line, batch_id=rows[0][0] if len(rows) == 1 else None)
        resolved.append(line)
    return tuple(resolved)


def _bucket(company, line):
    return Bucket(company, line.sku_id, line.source or line.target, line.batch_kind,
                  line.batch_id if line.batch_kind == "BATCH" else None)


def _reason(code, *, bucket=None, operation_id=None, line_id=None):
    return {"code": code, "bucket": asdict(bucket) if bucket else None,
            "operation_id": operation_id, "line_id": line_id}


def _native_index(company):
    rows = frappe.db.sql(
        f"""SELECT l.line_id,l.operation_id,l.parent_type,l.parent_id,l.detail_id,l.role,l.detail_type,
            o.creation,o.action,o.original_operation,o.document_id
            FROM `tab{stock.LINK}` l JOIN `tab{stock.LOG}` o ON o.name=l.operation_id
            JOIN `tab{stock.DOCUMENT}` d ON d.name=o.document_id
            WHERE d.company=%s AND o.status='Effective' FOR UPDATE""", (company,), as_dict=True,
    )
    return {(row.parent_type, row.parent_id, row.detail_id): row for row in rows}


def _allocations(company, entries):
    index = _native_index(company)
    allocations = defaultdict(list)
    for bucket, rows in entries.items():
        if bucket.company != company:
            continue
        for sle_id, qty, bundle, parent_type, parent_id, detail, part, _batch, allocated in rows:
            link = index.get((parent_type, parent_id, detail))
            if not link:
                raise StockError(INVALID)
            allocations[bucket].append((link, sle_id, Decimal(allocated)))
    return allocations


def _net(allocations, operation_id, line_id, expected_bucket):
    matches = [(bucket, link, sle, qty)
               for bucket, rows in allocations.items()
               for link, sle, qty in rows
               if (link.operation_id, link.line_id) == (operation_id, line_id)]
    return _net_rows(matches, expected_bucket)


def _net_rows(matches, expected_bucket):
    if any(bucket != expected_bucket for bucket, *_ in matches):
        raise StockError(INVALID)
    with localcontext() as context:
        context.prec = 64
        return sum((qty for *_, qty in matches), Decimal(0))


def _completed_counts(company, buckets, original):
    if not buckets:
        return []
    tasks = frappe.db.sql(
        f"""SELECT t.name,t.company,t.location_id,t.scope,t.sku_id,t.batch_kind,t.batch_id,t.generation,
            t.snapshot_at,o.creation,o.name operation_id,o.action,o.status,o.result_revision,d.revision
            FROM `tab{versions.TASK}` t JOIN `tab{stock.DOCUMENT}` d ON d.name=t.name
            LEFT JOIN `tab{stock.LOG}` o ON o.name=d.effective_operation AND o.document_id=d.name
            WHERE d.company=%s AND d.document_type=%s AND d.status='Completed' AND t.location_id IN %s
            FOR UPDATE""",
        (company, rules.COUNT, tuple(sorted({b.location_id for b in buckets}))), as_dict=True,
    )
    reasons = []
    for task in tasks:
        covered = [b for b in buckets if task.location_id == b.location_id and (
            task.scope == "LOCATION" or (task.sku_id == b.sku_id and (
                task.scope == "SKU" or (task.scope == "BUCKET"
                    and (task.batch_kind, task.batch_id or None) == (b.batch_kind, b.batch_id)))))]
        if task.operation_id == original.name or not covered:
            continue
        valid = (task.company == company and task.operation_id and task.action == rules.COUNT
                 and task.status == "Effective" and task.result_revision == task.revision
                 and task.snapshot_at and task.snapshot_at <= task.creation)
        if valid and task.creation < original.creation:
            continue
        if valid:
            valid = all(row.confirmed and row.actual_quantity is not None
                        for row in counts._rows(task.name))
        code = RECOUNT if valid and task.creation > original.creation else ORDER
        reasons.extend(_reason(code, bucket=bucket, operation_id=task.operation_id) for bucket in covered)
    return reasons


def line_origins(case):
    result = json.loads(case.line_origins)
    if (not isinstance(result, dict) or len(set(result.values())) != len(result)
            or not all(isinstance(value, str) for value in result.values())):
        raise StockError(INVALID)
    return result


def _completed_corrections(company, original, originals, allocations):
    """Reconcile every consumed entitlement with its submitted native effect."""
    used = defaultdict(Decimal)
    related = defaultdict(list)
    rows = frappe.db.sql(
        f"""SELECT c.name,c.line_origins,c.original_operation,d.effective_operation,d.approval_submission,
            d.status,o.original_operation log_origin,o.action,o.creation,o.result_revision,d.revision,o.request_id,
            o.actor,o.native_actor,o.status log_status,o.effect_key,o.quantity_effect
            FROM `tab{CASE}` c JOIN `tab{stock.DOCUMENT}` d ON d.name=c.name
            LEFT JOIN `tab{stock.LOG}` o ON o.name=d.effective_operation AND o.document_id=d.name
            WHERE d.company=%s AND (c.original_operation=%s OR o.original_operation=%s)
                AND (d.effective_operation IS NOT NULL OR d.status='Effective')
            FOR UPDATE""", (company, original.name, original.name), as_dict=True,
    )
    for row in rows:
        _, lines = _lock_document(company, row.name, rules.CORRECTION)
        mapping = line_origins(row)
        snapshot = approvals.submission(row.approval_submission)
        if (row.original_operation != original.name or row.log_origin != original.name
                or (row.status, row.action, row.log_status, row.effect_key, row.quantity_effect)
                != ("Effective", rules.CORRECTION, "Effective", "APPLY", "Native")
                or not row.creation or row.creation <= original.creation or row.result_revision != row.revision
                or set(mapping) != {line.line_id for line in lines} or not snapshot
                or not snapshot.approval_required
                or (snapshot.document_id, snapshot.company, snapshot.document_type)
                != (row.name, company, rules.CORRECTION)):
            raise StockError(INVALID)
        request = stock.current(stock.REQUEST, row.request_id, (
            "company", "document_id", "command", "status", "actor", "result_operation", "result_revision",
        ))
        event = frappe.db.get_values(EVENT, {
            "submission_id": snapshot.name, "action": "Approve", "result": "Succeeded",
            "operation_id": row.effective_operation,
        }, ("company", "document_id", "business_revision", "actor", "native_actor"), for_update=True)
        if not request or tuple(request[field] for field in (
            "company", "document_id", "command", "status", "actor", "result_operation", "result_revision",
        )) != (company, row.name, rules.CORRECTION, "Completed", row.actor, row.effective_operation, row.revision):
            raise StockError(INVALID)
        if event != [(company, row.name, row.revision, row.actor, row.native_actor)] or (
            row.native_actor != snapshot.submitter or snapshot.business_revision + 2 != row.revision
        ):
            raise StockError(INVALID)
        from ...approval.service import _data
        data = _data(snapshot)
        if (data.get("requirement"), data.get("line_origins"), data["command"]["original_operation"]) != (
            rules.CORRECTION_APPROVAL, mapping, original.name,
        ) or approvals.encode(data["command"]["lines"]) != approvals.encode([asdict(line) for line in lines]):
            raise StockError(INVALID)
        for line in lines:
            source_id = mapping[line.line_id]
            if source_id not in originals:
                raise StockError(INVALID)
            bucket, net, _ = originals[source_id]
            delta = Decimal(line.quantity)
            actual = _net(allocations, row.effective_operation, line.line_id, bucket)
            if (_bucket(company, line) != bucket or delta <= 0 or net == 0
                    or actual != (delta.copy_negate() if net > 0 else delta)
                    or bool(line.source) != (net > 0)):
                raise StockError(INVALID)
            used[source_id] += delta
            related[source_id].append((row.effective_operation, line.line_id))
    return used, related


def assess(company, operation, header, lines, selected=None):
    """Return version proof, per-line evidence, and review reasons."""
    if header.document_type == rules.TRANSFER:
        return None, {}, [_reason(TRANSFER, operation_id=operation.name)]
    if header.document_type not in (rules.RECEIPT, rules.ISSUE, rules.COUNT):
        return None, {}, [_reason(UNSUPPORTED, operation_id=operation.name)]
    _verify_origin_request(company, operation, header)
    baseline = stock.current(versions.EPOCH, "inventory", ("generation", "baseline_at"))
    before, entries = versions.verify(include_entries=True)
    allocations = _allocations(company, entries)
    if {link.line_id for rows in allocations.values() for link, _, _ in rows if link.operation_id == operation.name
        } - {line.line_id for line in lines}:
        raise StockError(INVALID)
    count_rows = {row.name: row for row in counts._rows(header.name)} if header.document_type == rules.COUNT else {}
    originals = {}
    with localcontext() as context:
        context.prec = 64
        for line in lines:
            bucket = _bucket(company, line)
            net = _net(allocations, operation.name, line.line_id, bucket)
            if header.document_type == rules.COUNT:
                count = count_rows.get(line.line_id)
                if not count or not count.confirmed or count.actual_quantity is None:
                    raise StockError(INVALID)
                expected = Decimal(count.actual_quantity) - Decimal(count.snapshot_quantity)
                if Decimal(count.difference_quantity) != expected:
                    raise StockError(INVALID)
            else:
                expected = Decimal(line.quantity) if header.document_type == rules.RECEIPT else Decimal(line.quantity).copy_negate()
            if net != expected:
                raise StockError(INVALID)
            originals[line.line_id] = (bucket, net, line.uom)
        used, related = _completed_corrections(company, operation, originals, allocations)
        evidence = {}
        reasons = []
        buckets = set()
        for identity, (bucket, net, uom) in originals.items():
            limit = net.copy_abs()
            if used[identity] > limit:
                raise StockError("CORRECTION_ENTITLEMENT_INVALID")
            active = selected is None or identity in selected
            if active:
                buckets.add(bucket)
            if active and selected is not None and selected[identity] > limit - used[identity]:
                reasons.append(_reason("CORRECTION_ENTITLEMENT_EXCEEDED", bucket=bucket, line_id=identity))
            facts = [(link.operation_id, sle) for link, sle, _ in allocations[bucket]
                     if link.operation_id == operation.name and link.line_id == identity]
            evidence[identity] = {"bucket": asdict(bucket), "net": str(net), "used": str(used[identity]),
                                  "remaining": str(limit - used[identity]), "uom": uom,
                                  "corrections": sorted(op for op, _ in related[identity]),
                                  "native_sle": sorted(sle for _, sle in facts),
                                  "generation": baseline.generation, "bucket_version": versions.read(
                                      bucket.company, bucket.location_id, "BUCKET", bucket.sku_id,
                                      bucket.batch_kind, bucket.batch_id),
                                  "fact_digest": before.get(bucket)}
            if not active:
                continue
            if facts and frappe.db.sql("""SELECT name FROM `tabStock Ledger Entry`
                WHERE name IN %s AND (stock_value_difference<>0 OR creation>%s) FOR UPDATE""",
                                     (tuple(sle for _, sle in facts), operation.creation)):
                reasons.append(_reason("CORRECTION_ORIGINAL_VALUATION_OR_ORDER", bucket=bucket, line_id=identity))
            if not baseline.baseline_at or operation.creation <= baseline.baseline_at:
                reasons.append(_reason(ORDER, bucket=bucket, line_id=identity))
            elif len({link.operation_id for link, _, _ in allocations[bucket]
                      if link.creation and link.creation > baseline.baseline_at}) < evidence[identity]["bucket_version"]:
                reasons.append(_reason(ORDER, bucket=bucket, line_id=identity))
            for link, _, _ in allocations[bucket]:
                if link.operation_id == operation.name or (link.operation_id, link.line_id) in related[identity]:
                    continue
                if not link.creation or link.creation == operation.creation:
                    reasons.append(_reason(ORDER, bucket=bucket, operation_id=link.operation_id, line_id=identity))
                elif link.creation > operation.creation:
                    reasons.append(_reason(DOWNSTREAM, bucket=bucket, operation_id=link.operation_id, line_id=identity))
        reasons.extend(_completed_counts(company, buckets, operation))
    return before, evidence, list({approvals.encode(reason): reason for reason in reasons}.values())


def transfer_history(company, header):
    document, lines = _lock_document(company, header.name, rules.TRANSFER)
    lifecycle, _, owned, _ = transfers._lifecycle(document, lines)
    return tuple({
        "line_id": line.line_id, "remaining": str(lifecycle.remaining(owned[line.line_id])),
        "received": str(lifecycle.received[line.line_id]),
        "returned": str(lifecycle.returned[line.line_id]),
        "operations": sorted({e.operation for e in lifecycle.effects if e.line == line.line_id}),
    } for line in lines)
