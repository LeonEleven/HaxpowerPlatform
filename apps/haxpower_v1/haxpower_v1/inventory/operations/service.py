"""Atomic execution shared by stock commands."""

from dataclasses import asdict, dataclass, replace
from hashlib import sha256
import json
from typing import Callable
from uuid import uuid4

import frappe
from frappe.utils import now_datetime

from ...access import policy
from ...approval import policy as approval_policy
from ...catalog.validation import IDENTIFIER, KEY
from ..batch.dto import BatchError, BatchKind
from ..batch.service import _load_sku, lock_execution_mapping
from ..guard import lock_stock_writes
from . import persistence as store
from .dto import Command, Line, LineQuantity, NativeLink, QuantitySign, Result, StockError, sum_demands
from .quantity import canonical_quantity, quantity_text
from .transaction import adapter_transaction, connection


@dataclass(frozen=True)
class Plan:
    command: Command
    source_demand: dict
    target_demand: dict
    locations: dict
    skus: dict
    context: object = None


@dataclass(frozen=True)
class Continuation:
    command: Command
    status: str
    locations: dict
    context: object


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
    document_type: str | None = None
    prepare_continuation: Callable[..., Continuation] | None = None
    verify_history: Callable | None = None
    require_reason: bool = False
    load_document: Callable | None = None
    prepare_plan: Callable | None = None
    native_line_ids: Callable | None = None
    allow_empty_lines: bool = False


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
    _text(command.reason, 4000, required=action.require_reason)
    if action.require_reason and not command.reason.strip():
        raise StockError("STOCK_INVALID_TEXT")
    if command.notes is not None:
        _text(command.notes, 4000)
    _id(command.document_id)
    if command.original_operation is not None:
        _id(command.original_operation)
    if not isinstance(command.idempotency_key, str) or not KEY.fullmatch(command.idempotency_key):
        raise StockError("STOCK_INVALID_REQUEST_KEY")
    if type(command.expected_revision) is not int or not 1 <= command.expected_revision < 2_147_483_647:
        raise StockError("STOCK_INVALID_REVISION")
    if not isinstance(command.lines, tuple) or (not command.lines and not action.allow_empty_lines):
        raise StockError("STOCK_LINES_REQUIRED")
    ids, numbers = set(), set()
    for line in command.lines:
        _id(line.line_id)
        if action.prepare_continuation:
            if not isinstance(line, LineQuantity) or line.line_id in ids:
                raise StockError("STOCK_DUPLICATE_OR_INVALID_LINE")
            ids.add(line.line_id)
            continue
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


def payload_digest(command, actor, action_name):
    payload = asdict(command)
    del payload["idempotency_key"]
    if payload["snapshot_id"] is None:
        del payload["snapshot_id"]
    if payload["notes"] is None:
        del payload["notes"]
    for line in payload["lines"]:
        if "batch_no" in line and line["batch_no"] is None:
            del line["batch_no"]
    return _digest({"format": 1, "actor": actor, "command": action_name, "payload": payload})


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


def _lock_document(company, document_id, document_type, *, locked=True):
    document = store.current(store.DOCUMENT, document_id, (
        "name", "company", "document_type", "revision", "status", "effective_operation", "reason", "notes",
        "owner", "approval_submission",
    ), locked=locked)
    if not document or (document.name, document.company, document.document_type) != (
        document_id, company, document_type,
    ):
        policy.deny()
    rows = frappe.db.get_values(store.LINE, {"document_id": document.name}, (
        "name", "line_no", "sku_id", "quantity", "uom", "batch_kind", "batch_id", "source", "target", "reason", "notes",
        "batch_no",
    ), for_update=locked, as_dict=True, order_by="name asc")
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
    line_ids = (action.native_line_ids(plan) if action.native_line_ids
                else {line.line_id for line in plan.command.lines})
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


def _replay(request, document, digest, action, actor, expected_revision):
    if request.payload_digest != digest:
        raise StockError("STOCK_IDEMPOTENCY_CONFLICT")
    if request.status != "Completed" or request.document_id != document.name:
        raise StockError("STOCK_REQUEST_INCONSISTENT")
    log = store.current(store.LOG, request.result_operation, (
        "name", "request_id", "document_id", "result_revision", "result_status", "original_operation",
        "action", "actor", "effect_key", "status",
    ))
    if (
        not log or (log.request_id, log.document_id, log.result_revision, log.action, log.actor, log.status)
        != (request.name, document.name, request.result_revision, action.name, actor, "Effective")
        or request.result_revision != expected_revision + 1 or document.revision < log.result_revision
    ):
        raise StockError("STOCK_REQUEST_INCONSISTENT")
    status = log.result_status or action.final_status
    historical = document.revision > log.result_revision
    if (historical and not action.verify_history) or (not historical and document.status != status):
        raise StockError("STOCK_REQUEST_INCONSISTENT")
    if action.prepare_continuation:
        valid = (log.effect_key == request.name and log.result_status
                 and log.original_operation == document.effective_operation)
    else:
        valid = (log.effect_key == "APPLY" and not log.original_operation and status == action.final_status
                 and document.effective_operation == log.name)
    if not valid:
        raise StockError("STOCK_REQUEST_INCONSISTENT")
    return Result(document.name, request.result_operation, request.result_revision, status,
                  document.approval_submission if action.name in approval_policy.PRIMARY
                  and action.name != approval_policy.TRANSFER else None)


def _validate_payload(command, document, stored_lines, action, *, structure_only=False):
    # Bound drafts retain the selected text and separately verify the resolved mapping.
    input_lines = (tuple(replace(line, batch_id=None) for line in stored_lines)
                   if action.bind_batches_on_draft else stored_lines)
    submitted_lines = command.lines
    if structure_only:
        submitted_lines = tuple(replace(line, quantity="") for line in submitted_lines)
        input_lines = tuple(replace(line, quantity="") for line in input_lines)
    if submitted_lines != input_lines or (
        action.final_status == "In Transit" and
        (command.reason, command.notes) != (document.reason or "", document.notes)
    ):
        raise StockError("STOCK_DOCUMENT_PAYLOAD_MISMATCH")


def _reserve_request(command, action, actor, request_id, key_digest, digest):
    store.insert(store.REQUEST, name=request_id, company=command.company, actor=actor, command=action.name,
                 idempotency_key=command.idempotency_key, key_digest=key_digest,
                 payload_digest=digest, document_id=command.document_id, status="Pending")


def _apply(database, command, document, stored_lines, locations, action, *, verified_versions=None):
    continuation = action.prepare_continuation(command, document, stored_lines, locations) if action.prepare_continuation else None
    if continuation:
        command, locations = continuation.command, continuation.locations
    preparation = action.prepare_plan(command, document, stored_lines, locations) if action.prepare_plan else None
    skus = _resources(command, action)
    if not continuation:
        _validate_payload(command, document, stored_lines, action)

    from .. import versions
    before_versions = versions.verify() if verified_versions is None else verified_versions
    with adapter_transaction(database, native_orm=action.native_orm):
        resolved = action.resolve_batches(command, skus) if action.resolve_batches else command
        if action.bind_batches_on_draft and any(
            (line.batch_id if line.batch_kind == BatchKind.BATCH else None) != stored.batch_id
            for line, stored in zip(resolved.lines, stored_lines, strict=True)
        ):
            raise StockError("BATCH_MAPPING_UNAVAILABLE")
        plan = Plan(resolved, sum_demands(command.company, resolved.lines, location_field="source"),
                    sum_demands(command.company, resolved.lines, location_field="target"), locations, skus,
                    continuation.context if continuation else preparation)
        links = action.apply(plan)
        _check_links(plan, action, links)

    return plan, links, before_versions, continuation


def _record(document, action, applied, request_id, actor, native_actor, submission_id=None):
    from .. import versions

    plan, links, before_versions, continuation = applied
    command = plan.command
    status = continuation.status if continuation else action.final_status
    result = Result(document.name, uuid4().hex, document.revision + 1, status, submission_id)
    store.insert(store.LOG, name=result.operation_id, action=action.name, document_id=document.name,
                 request_id=request_id, actor=actor, native_actor=native_actor, effect_key=request_id if continuation else "APPLY",
                 reason=command.reason, notes=command.notes, original_operation=command.original_operation,
                 status="Effective", result_revision=result.revision, result_status=status,
                 quantity_effect=("None" if not links else "Native"))
    for link in links:
        store.insert(store.LINK, operation_id=result.operation_id, **asdict(link))
    store.update_document(document, result)
    store.complete_request(request_id, result)
    if action.record_effect:
        action.record_effect(plan, result, links)
    versions.advance(before_versions, links)
    return result


def _stage(database, command, action, *, recovery, transfer_submission=False):
    from ...approval import service as approval

    if action.name == approval_policy.CORRECTION or action.document_type == approval_policy.CORRECTION:
        raise StockError("CORRECTION_SPECIALIST_APPROVAL_REQUIRED")
    lock_stock_writes(command.company)
    actor = policy.active_actor(locked=True)
    policy.require_operation(action.operation, locked=True)
    request_id, key_digest = _request_identity(command, actor, action)
    digest = payload_digest(command, actor, action.name)
    document_type = action.document_type or action.name
    load = action.load_document or _lock_document
    request = None if transfer_submission else store.current(store.REQUEST, request_id, (
        "name", "document_id", "status", "payload_digest", "result_operation", "result_revision",
    ))
    if request:
        document, stored_lines = load(command.company, request.document_id, document_type)
        _authorize(command.company, stored_lines, action)
        result = _replay(request, document, digest, action, actor, command.expected_revision)
        if action.verify_history:
            action.verify_history(document, stored_lines)
        return result, False

    primary = action.name in approval_policy.PRIMARY
    submission_request = primary and (action.name != approval_policy.TRANSFER or transfer_submission)
    identity = approval.submit_identity(command, actor, action) if submission_request else None
    workflow = approval.store.request(identity) if identity else None
    if workflow:
        document, stored_lines = load(command.company, workflow.document_id, document_type)
        _authorize(command.company, stored_lines, action)
        approval.authorize_snapshot(approval.store.submission(workflow.submission_id))
        return approval.store.replay(workflow, digest), False

    document, stored_lines = load(command.company, command.document_id, document_type)
    locations = _authorize(command.company, stored_lines, action)
    if recovery:
        raise StockError("STOCK_RETRY_REQUIRED")
    expected_statuses = ("Draft", approval_policy.REJECTED) if submission_request else ("Draft",)
    if primary and action.name == approval_policy.TRANSFER and not transfer_submission:
        expected_statuses = (approval_policy.READY, approval_policy.APPROVED)
    if not action.prepare_continuation and (document.status not in expected_statuses or document.effective_operation is not None):
        raise StockError("STOCK_DOCUMENT_ALREADY_EFFECTIVE")
    if document.revision != command.expected_revision:
        raise StockError("STOCK_REVISION_CONFLICT")
    _original(command)
    if not action.prepare_continuation:
        _validate_payload(command, document, stored_lines, action, structure_only=True)
    submission = None
    if submission_request:
        submission, deferred = approval.stage_submission(
            command, action, document, stored_lines, locations, actor, identity, digest,
        )
        if deferred:
            return deferred, True
    elif primary and action.name == approval_policy.TRANSFER:
        approval.lock_dispatch_submission(document, command)
    elif action.prepare_continuation:
        approval_policy.requirement(action.name)

    _reserve_request(command, action, actor, request_id, key_digest, digest)
    applied = _apply(database, command, document, stored_lines, locations, action)
    result = _record(document, action, applied, request_id, actor, actor if applied[1] else None,
                     submission.name if submission_request else None)
    if submission_request:
        approval.record_direct(submission, document, result, actor, identity)
    return result, True


def _execute(command, action, *, before_guard=None, commit=None):
    command = _canonical(_shape(command, action))
    return _run(lambda database, **kwargs: _stage(database, command, action, **kwargs),
                read_committed=action.read_committed, before_guard=before_guard, commit=commit)


def _run(stage, *, read_committed, before_guard=None, commit=None, passthrough=()):
    unconfirmed_commit = False
    try:
        with connection(read_committed=read_committed) as database:
            if before_guard:
                before_guard(database)
            result, changed = stage(database, recovery=False)
            if changed:
                unconfirmed_commit = True
                (commit or _commit)(database)
        return result
    except (StockError, frappe.PermissionError, *passthrough):
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
        with connection(read_committed=read_committed) as database:
            result, _ = stage(database, recovery=True)
            return result
    except (StockError, frappe.PermissionError, *passthrough):
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
            _insert_line(command.document_id, line, action)
        _commit(database)
    return command


def _insert_line(document_id, line, action):
    fields = asdict(line)
    if line.batch_kind == BatchKind.NO_BATCH and action.bind_batches_on_draft:
        fields["batch_id"] = None
    line_id = fields.pop("line_id")
    store.insert(store.LINE, name=line_id, document_id=document_id, **fields)


def _line_identity(line):
    return line.sku_id, line.source, line.target, line.batch_kind, line.batch_no, line.uom


def _revise_draft(command, action, *, new_line_ids, notes=None):
    command = _canonical(_shape(command, action))
    if notes is not None:
        _text(notes, 4000)
    with connection(read_committed=action.read_committed) as database:
        lock_stock_writes(command.company)
        policy.require_operation(action.operation, locked=True)
        document, old_lines = _lock_document(command.company, command.document_id, action.name)
        _authorize(command.company, old_lines, action)
        if document.status not in ("Draft", approval_policy.REJECTED) or document.effective_operation is not None:
            raise StockError("STOCK_DOCUMENT_NOT_EDITABLE")
        if document.revision != command.expected_revision:
            raise StockError("STOCK_REVISION_CONFLICT")
        _authorize(command.company, command.lines, action)
        skus = _resources(command, action)
        draft = action.resolve_batches(command, skus) if action.bind_batches_on_draft else command
        previous = {line.line_id: line for line in old_lines}
        for line in draft.lines:
            original = previous.get(line.line_id)
            if line.line_id in new_line_ids:
                if original:
                    raise StockError("STOCK_LINE_ID_CONFLICT")
            elif not original or _line_identity(original) != _line_identity(line):
                raise StockError("STOCK_LINE_ID_CONFLICT")
        # Free the original order numbers under lock before reordering the
        # retained lines. Removed identities survive in immutable submissions.
        frappe.db.sql(f"UPDATE `tab{store.LINE}` SET line_no=-line_no WHERE document_id=%s", (document.name,))
        for line in draft.lines:
            if line.line_id in new_line_ids:
                _insert_line(document.name, line, action)
            else:
                frappe.db.sql(f"""UPDATE `tab{store.LINE}`
                    SET line_no=%s, quantity=%s, reason=%s, notes=%s, modified=%s, modified_by=%s
                    WHERE name=%s AND document_id=%s""",
                    (line.line_no, line.quantity, line.reason, line.notes, now_datetime(),
                     frappe.session.user, line.line_id, document.name))
                store._assert_updated("STOCK_DOCUMENT_CHANGED")
        frappe.db.sql(f"DELETE FROM `tab{store.LINE}` WHERE document_id=%s AND line_no < 0", (document.name,))
        revision = document.revision + 1
        frappe.db.sql(f"""UPDATE `tab{store.DOCUMENT}` SET revision=%s, reason=%s, notes=%s, modified=%s,
            modified_by=%s WHERE name=%s AND revision=%s""",
            (revision, command.reason, document.notes if notes is None else notes, now_datetime(),
             frappe.session.user, document.name, document.revision))
        store._assert_updated("STOCK_DOCUMENT_CHANGED")
        _commit(database)
        return replace(command, expected_revision=revision)
