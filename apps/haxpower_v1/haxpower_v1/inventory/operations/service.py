"""Atomic execution shared by stock commands."""

from dataclasses import asdict, dataclass, replace
from hashlib import sha256
import json
from typing import Callable
from uuid import uuid4

import frappe

from ...access import policy
from ...catalog.validation import IDENTIFIER, KEY
from ..batch.dto import BatchError, BatchKind
from ..batch.service import _load_sku, lock_execution_mapping
from ..guard import lock_stock_writes
from . import persistence as store
from .dto import Command, Line, NativeLink, QuantitySign, Result, StockError, sum_demands
from .quantity import canonical_quantity, quantity_text
from .transaction import adapter_transaction, connection


@dataclass(frozen=True)
class Plan:
    command: Command
    source_demand: dict
    target_demand: dict
    locations: dict
    skus: dict


@dataclass(frozen=True)
class Action:
    name: str
    operation: policy.Operation
    quantity_sign: QuantitySign
    limits: dict
    detail_fields: dict[tuple[str, str], str]
    authorize: Callable
    apply: Callable[[Plan], tuple[NativeLink, ...]]
    verify_reference: Callable[[Plan, NativeLink], bool]
    resolve_batches: Callable | None = None
    bind_batches_on_draft: bool = False
    native_orm: bool = False
    read_committed: bool = False
    final_status: str = "Effective"
    validate_locations: Callable | None = None
    record_effect: Callable | None = None


def _digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")).hexdigest()


def _id(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise StockError("STOCK_INVALID_ID")


def _text(value, limit, *, required=False):
    if not isinstance(value, str) or len(value) > limit or "\0" in value or (required and not value):
        raise StockError("STOCK_INVALID_TEXT")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise StockError("STOCK_INVALID_TEXT") from None


def _shape(command, action):
    _text(command.company, 140, required=True)
    _text(command.reason, 4000)
    if command.notes is not None:
        _text(command.notes, 4000)
    _id(command.document_id)
    if command.original_operation is not None:
        _id(command.original_operation)
    if not isinstance(command.idempotency_key, str) or not KEY.fullmatch(command.idempotency_key):
        raise StockError("STOCK_INVALID_REQUEST_KEY")
    if type(command.expected_revision) is not int or not 1 <= command.expected_revision < 2_147_483_647:
        raise StockError("STOCK_INVALID_REVISION")
    if not isinstance(command.lines, tuple) or not command.lines:
        raise StockError("STOCK_LINES_REQUIRED")
    ids, numbers = set(), set()
    for line in command.lines:
        _id(line.line_id)
        _id(line.sku_id)
        if (type(line.line_no) is not int or not 1 <= line.line_no <= 2_147_483_647
                or line.line_no in numbers or line.line_id in ids):
            raise StockError("STOCK_DUPLICATE_OR_INVALID_LINE")
        ids.add(line.line_id)
        numbers.add(line.line_no)
        if line.source is None and line.target is None:
            raise StockError("STOCK_LOCATION_REQUIRED")
        for location in (line.source, line.target):
            if location is not None:
                _id(location)
        if line.batch_kind not in (BatchKind.NO_BATCH, BatchKind.BATCH):
            raise StockError("STOCK_EXPLICIT_BATCH_REQUIRED")
        if action.resolve_batches:
            if line.batch_id is not None or (line.batch_no is not None) != (line.batch_kind == BatchKind.BATCH):
                raise StockError("STOCK_EXPLICIT_BATCH_REQUIRED")
            if line.batch_no is not None:
                _text(line.batch_no, 80, required=True)
        elif line.batch_no is not None:
            raise StockError("STOCK_INVALID_BATCH")
        elif line.batch_id is not None or line.batch_kind == BatchKind.BATCH:
            _id(line.batch_id)
        _text(line.uom, 140, required=True)
        _text(line.reason, 4000)
        _text(line.notes, 4000)
    return replace(command, lines=tuple(sorted(command.lines, key=lambda line: line.line_id)))


def _canonical(command):
    return replace(command, lines=tuple(
        replace(line, quantity=canonical_quantity(line.quantity)) for line in command.lines
    ))


def _request_identity(command, actor, action):
    key_digest = sha256(command.idempotency_key.encode("ascii")).hexdigest()
    identity = (command.company, actor, action.name, command.idempotency_key)
    return _digest(identity), key_digest


def _payload(command, actor, action):
    payload = asdict(command)
    del payload["idempotency_key"]
    if payload["notes"] is None:
        del payload["notes"]
    for line in payload["lines"]:
        if line["batch_no"] is None:
            del line["batch_no"]
    return _digest({"format": 1, "actor": actor, "command": action.name, "payload": payload})


def _authorize(company, lines, action):
    locations = policy.lock_execution_locations(company, (
        location for line in lines for location in (line.source, line.target) if location is not None
    ))
    candidate = action.authorize(company)
    if action.validate_locations:
        action.validate_locations(lines, locations, candidate)
    return locations


def _resources(command, action):
    try:
        skus = {sku_id: _load_sku(sku_id, locked=True) for sku_id in sorted({line.sku_id for line in command.lines})}
        if not action.resolve_batches:
            for sku_id, kind, batch_id in sorted({
                (line.sku_id, line.batch_kind, line.batch_id or "") for line in command.lines
            }, key=lambda entry: (entry[2], entry[0], entry[1])):
                lock_execution_mapping(skus[sku_id], kind, batch_id or None)
    except BatchError as error:
        raise StockError(error.code) from None
    for line in command.lines:
        quantity_text(line.quantity, line.uom, sku=skus[line.sku_id], sign=action.quantity_sign, limits=action.limits)
    return skus


def _lock_document(company, document_id, document_type):
    document = store.current(store.DOCUMENT, document_id, (
        "name", "company", "document_type", "revision", "status", "effective_operation", "reason", "notes",
    ))
    if not document or (document.name, document.company, document.document_type) != (
        document_id, company, document_type,
    ):
        policy.deny()
    rows = frappe.db.get_values(store.LINE, {"document_id": document.name}, (
        "name", "line_no", "sku_id", "quantity", "uom", "batch_kind", "batch_id", "source", "target", "reason", "notes",
        "batch_no",
    ), for_update=True, as_dict=True, order_by="name asc")
    if not rows:
        raise StockError("STOCK_DOCUMENT_INCONSISTENT")
    lines = tuple(Line(
        row.name, row.line_no, row.sku_id, row.quantity, row.uom, row.batch_kind,
        row.batch_id, row.source, row.target, row.reason or "", row.notes or "", row.batch_no or None,
    ) for row in rows)
    return document, lines


def _original(command):
    # V1-07 has a single apply action. Future compensation will lock and
    # authorize the original operation and its scope before enabling references.
    if command.original_operation is not None:
        raise StockError("STOCK_ORIGINAL_OPERATION_UNSUPPORTED")


def _check_links(plan, action, links):
    line_ids = {line.line_id for line in plan.command.lines}
    covered, seen = set(), set()
    for link in links:
        field = action.detail_fields.get((link.parent_type, link.detail_type))
        if link.line_id not in line_ids or link in seen or not field:
            raise StockError("STOCK_NATIVE_REFERENCE_INVALID")
        _text(link.role, 40, required=True)
        _text(link.parent_id, 140, required=True)
        _text(link.detail_id, 140, required=True)
        parent = store.current(link.parent_type, link.parent_id, ("name", "docstatus"))
        detail = store.current(link.detail_type, link.detail_id, ("name", "parent", "parenttype", "parentfield"))
        if (not parent or parent.name != link.parent_id or not detail or detail.name != link.detail_id
                or (detail.parent, detail.parenttype, detail.parentfield)
                != (link.parent_id, link.parent_type, field)
                or not action.verify_reference(plan, link)):
            raise StockError("STOCK_NATIVE_REFERENCE_INVALID")
        seen.add(link)
        covered.add(link.line_id)
    if covered != line_ids:
        raise StockError("STOCK_NATIVE_REFERENCE_INCOMPLETE")


def _replay(request, document, digest, final_status):
    if request.payload_digest != digest:
        raise StockError("STOCK_IDEMPOTENCY_CONFLICT")
    if request.status != "Completed" or request.document_id != document.name:
        raise StockError("STOCK_REQUEST_INCONSISTENT")
    log = store.current(store.LOG, request.result_operation, ("name", "request_id", "document_id", "result_revision"))
    if (document.status != final_status or document.revision != request.result_revision
            or document.effective_operation != request.result_operation
            or not log or (log.request_id, log.document_id, log.result_revision)
            != (request.name, document.name, request.result_revision)):
        raise StockError("STOCK_REQUEST_INCONSISTENT")
    return Result(document.name, request.result_operation, request.result_revision)


def _stage(database, command, action, *, recovery):
    lock_stock_writes(command.company)
    actor = policy.active_actor(locked=True)
    policy.require_operation(action.operation, locked=True)
    request_id, key_digest = _request_identity(command, actor, action)
    request = store.current(store.REQUEST, request_id, (
        "name", "document_id", "status", "payload_digest", "result_operation", "result_revision",
    ))
    if request:
        # Authorize the stored object even if the retry changed its target.
        document, stored_lines = _lock_document(command.company, request.document_id, action.name)
        _authorize(command.company, stored_lines, action)
        return _replay(request, document, _payload(command, actor, action), action.final_status), False

    document, stored_lines = _lock_document(command.company, command.document_id, action.name)
    locations = _authorize(command.company, stored_lines, action)
    if recovery:
        raise StockError("STOCK_RETRY_REQUIRED")
    if document.status != "Draft" or document.effective_operation is not None:
        raise StockError("STOCK_DOCUMENT_ALREADY_EFFECTIVE")
    if document.revision != command.expected_revision:
        raise StockError("STOCK_REVISION_CONFLICT")
    _original(command)
    # Actions which bind existing batches at draft time accept the original
    # selection on submit and verify the resolved identity before posting.
    input_lines = (
        tuple(replace(line, batch_id=None) for line in stored_lines) if action.bind_batches_on_draft else stored_lines
    )
    if tuple(replace(line, quantity="") for line in command.lines) != tuple(
        replace(line, quantity="") for line in input_lines
    ):
        raise StockError("STOCK_DOCUMENT_PAYLOAD_MISMATCH")
    store.insert(store.REQUEST, name=request_id, company=command.company, actor=actor, command=action.name,
                 idempotency_key=command.idempotency_key, key_digest=key_digest,
                 payload_digest=_payload(command, actor, action), document_id=document.name, status="Pending")
    skus = _resources(command, action)
    if (
        command.lines != input_lines
        or (action.final_status == "In Transit" and (command.reason, command.notes) != (document.reason, document.notes))
    ):
        raise StockError("STOCK_DOCUMENT_PAYLOAD_MISMATCH")

    with adapter_transaction(database, native_orm=action.native_orm):
        resolved = action.resolve_batches(command, skus) if action.resolve_batches else command
        if action.bind_batches_on_draft and any(
            (line.batch_id if line.batch_kind == BatchKind.BATCH else None) != stored.batch_id
            for line, stored in zip(resolved.lines, stored_lines, strict=True)
        ):
            raise StockError("BATCH_MAPPING_UNAVAILABLE")
        plan = Plan(resolved, sum_demands(command.company, resolved.lines, location_field="source"),
                    sum_demands(command.company, resolved.lines, location_field="target"), locations, skus)
        links = action.apply(plan)
        _check_links(plan, action, links)

    result = Result(document.name, uuid4().hex, document.revision + 1)
    store.insert(store.LOG, name=result.operation_id, action=action.name, document_id=document.name,
                 request_id=request_id, actor=actor, effect_key="APPLY", reason=command.reason,
                 original_operation=command.original_operation, status="Effective", result_revision=result.revision)
    for link in links:
        store.insert(store.LINK, operation_id=result.operation_id, **asdict(link))
    if action.final_status == "Effective":
        store.update_document(document, result)
    else:
        store.update_document(document, result, status=action.final_status)
    if action.record_effect:
        action.record_effect(plan, result, links)
    store.complete_request(request_id, result)
    return result, True


def _execute(command, action, *, before_guard=None, commit=None):
    command = _canonical(_shape(command, action))
    unconfirmed_commit = False
    try:
        with connection(read_committed=action.read_committed) as database:
            if before_guard:
                before_guard(database)
            result, changed = _stage(database, command, action, recovery=False)
            if changed:
                unconfirmed_commit = True
                (commit or _commit)(database)
                unconfirmed_commit = False
            return result
    except (StockError, frappe.PermissionError):
        if not unconfirmed_commit:
            raise
    except (frappe.QueryDeadlockError, frappe.QueryTimeoutError):
        if not unconfirmed_commit:
            raise StockError("STOCK_RETRY_REQUIRED") from None
    except Exception:
        if not unconfirmed_commit:
            raise StockError("STOCK_PERSISTENCE_FAILED") from None

    # Closing the failed connection precedes this current read on a new primary
    # connection. Taking the same guard waits out a server-side in-flight COMMIT.
    try:
        with connection(read_committed=action.read_committed) as database:
            result, _ = _stage(database, command, action, recovery=True)
            return result
    except (StockError, frappe.PermissionError):
        raise
    except Exception:
        raise StockError("STOCK_COMMIT_UNKNOWN") from None


def _commit(database):
    # Frappe's commit wrapper starts a new transaction and runs callbacks.
    # The action uses neither. Keep the outcome tied to this one statement.
    database.sql("COMMIT")


def _create_draft(command, action, *, notes=""):
    command = _canonical(_shape(command, action))
    _text(notes, 4000)
    with connection(read_committed=action.read_committed) as database:
        lock_stock_writes(command.company)
        policy.require_operation(action.operation, locked=True)
        _original(command)
        _authorize(command.company, command.lines, action)
        skus = _resources(command, action)
        draft = action.resolve_batches(command, skus) if action.bind_batches_on_draft else command
        store.insert(store.DOCUMENT, name=command.document_id, company=command.company, document_type=action.name,
                     document_number=f"{action.name}-{command.document_id}", status="Draft", revision=1, notes=notes,
                     reason=command.reason if action.final_status == "In Transit" else None)
        for line in draft.lines:
            fields = asdict(line)
            if line.batch_kind == BatchKind.NO_BATCH and action.bind_batches_on_draft:
                fields["batch_id"] = None
            line_id = fields.pop("line_id")
            store.insert(store.LINE, name=line_id, document_id=command.document_id, **fields)
        _commit(database)
    return command
