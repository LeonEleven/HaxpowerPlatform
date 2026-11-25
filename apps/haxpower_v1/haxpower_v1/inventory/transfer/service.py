"""Authorized transfer drafts, dispatch and settlement of original-line quantities."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from decimal import Decimal
from functools import partial
from uuid import uuid4

import frappe

from ...access import policy
from ...adapters.erpnext import stock_entry as stock, transfer as native
from .. import availability
from ..batch.dto import BatchError
from ..batch.service import _load_sku
from ..guard import lock_stock_writes
from ..operations import persistence as store
from ..operations.dto import Command, Line, LineQuantity, QuantitySign, StockError
from ..operations.quantity import quantity_text
from ..operations.service import Action, Continuation, _create_draft, _execute, _lock_document, _revise_draft
from ..operations.transaction import connection
from ..posting import batch_input, require_fixture, require_native_precision, selection
from . import transit
from .dto import (
    Dispatch, DispatchLine, DispatchResult, DispatchTransfer, ReceiveTransfer, ReturnTransfer,
    TransferActionResult, TransferDraft, TransferDraftInput, TransferLine, TransferQuantity,
)

IN_TRANSIT = transit.IN_TRANSIT
_candidate = ContextVar("hp_v1_transfer_candidate", default=None)


@dataclass(frozen=True)
class Candidate:
    actor: str
    company: str
    limits: dict
    allow_same_warehouse: bool


@contextmanager
def synthetic_candidate(company, *, limits, allow_same_warehouse=False):
    if type(allow_same_warehouse) is not bool:
        raise StockError("TRANSFER_POLICY_INVALID")
    token = _candidate.set(Candidate(frappe.session.user, company, dict(limits), allow_same_warehouse))
    try:
        _authorize(company)
        yield
    finally:
        _candidate.reset(token)


def _authorize(company):
    return require_fixture(_candidate.get(), company, "TRANSFER_NOT_ENABLED")


def _validate_locations(lines, locations, candidate):
    for line in lines:
        if line.source is None or line.target is None or line.source == line.target:
            raise StockError("TRANSFER_DISTINCT_LOCATIONS_REQUIRED")
        if locations[line.source].warehouse == locations[line.target].warehouse and not candidate.allow_same_warehouse:
            raise StockError("TRANSFER_SAME_WAREHOUSE_UNAPPROVED")


def _action(candidate):
    return Action(
        native.PURPOSE, policy.Operation.TRANSFER, QuantitySign.POSITIVE, candidate.limits,
        {(native.ENTRY, native.DETAIL): "items"}, _authorize, native.post, native.verify_link,
        resolve_batches=availability.resolve_existing_lines, bind_batches_on_draft=True,
        native_orm=True, read_committed=True, final_status=IN_TRANSIT, validate_locations=_validate_locations,
        record_effect=transit.record_dispatch, verify_history=_lifecycle,
    )


def _line(line, number, identity):
    parsed = selection(line.batch)
    return Line(
        identity, number, line.sku_id, line.quantity, line.uom, parsed.kind,
        source=line.source_location_id, target=line.target_location_id, reason=line.reason, notes=line.notes,
        batch_no=parsed.normalization.original if parsed.normalization else None,
    )


def create_draft(payload: TransferDraftInput) -> TransferDraft:
    candidate = _authorize(payload.company)
    lines = tuple(_line(line, number, uuid4().hex) for number, line in enumerate(payload.lines, 1))
    command = Command(payload.company, uuid4().hex, 1, uuid4().hex, lines, payload.reason)
    draft = _create_draft(command, _action(candidate), notes=payload.notes)
    public = tuple(
        TransferLine(line.sku_id, line.source, line.target, batch_input(line), line.quantity, line.uom,
                     line.reason, line.notes, line.line_id)
        for line in sorted(draft.lines, key=lambda line: line.line_no)
    )
    return TransferDraft(draft.company, draft.document_id, draft.expected_revision, public, draft.reason, payload.notes)


def revise_draft(draft: TransferDraft) -> TransferDraft:
    candidate = _authorize(draft.company)
    ids = tuple(line.transfer_line_id or uuid4().hex for line in draft.lines)
    new_ids = {id_ for line, id_ in zip(draft.lines, ids, strict=True) if not line.transfer_line_id}
    lines = tuple(_line(line, number, id_) for number, (line, id_) in enumerate(
        zip(draft.lines, ids, strict=True), 1,
    ))
    command = Command(draft.company, draft.transfer_id, draft.revision, uuid4().hex, lines, draft.reason)
    updated = _revise_draft(command, _action(candidate), new_line_ids=new_ids, notes=draft.notes)
    return replace(draft, revision=updated.expected_revision, lines=tuple(replace(line, transfer_line_id=id_) for line, id_ in zip(draft.lines, ids, strict=True)))


def _dispatch_command(payload):
    draft = payload.draft
    candidate = _authorize(draft.company)
    lines = tuple(_line(line, number, line.transfer_line_id) for number, line in enumerate(draft.lines, 1))
    command = Command(draft.company, draft.transfer_id, draft.revision, payload.idempotency_key, lines,
                      draft.reason, notes=draft.notes)
    return command, _action(candidate)


def submit(payload: DispatchTransfer):
    from ...approval.service import submit_transfer

    return submit_transfer(*_dispatch_command(payload))


def dispatch(payload: DispatchTransfer) -> DispatchResult:
    return _dispatch(payload)


def _dispatch(payload, *, before_guard=None, commit=None):
    command, action = _dispatch_command(payload)
    try:
        result = _execute(command, action, before_guard=before_guard, commit=commit)
    except StockError as error:
        if error.code == "STOCK_ADAPTER_SIDE_EFFECT":
            frappe.logger("haxpower_v1", allow_site=True).warning(
                "Unsafe ERPNext transaction side effect during transfer dispatch %s", command.document_id,
            )
        raise
    return DispatchResult(result.document_id, result.operation_id, result.status, result.revision)


def get_dispatch(company, transfer_id) -> Dispatch:
    """Read the original dispatch through an independent authorized transaction."""
    try:
        with connection(read_committed=True):
            return lock_original_dispatch(company, transfer_id)
    except (frappe.QueryDeadlockError, frappe.QueryTimeoutError):
        raise StockError("STOCK_RETRY_REQUIRED") from None


def lock_original_dispatch(company, transfer_id) -> Dispatch:
    """Verify the original line and hold the shared stock guard in the caller's transaction."""
    _authorize(company)
    policy._record_id(transfer_id)
    lock_stock_writes(company)
    policy.require_operation(policy.Operation.TRANSFER, locked=True)
    doc, lines = _lock_document(company, transfer_id, native.PURPOSE)
    policy.lock_execution_locations(company, (location for line in lines for location in (line.source, line.target)))
    lifecycle, log, owned, _ = _lifecycle(doc, lines)
    result = tuple(
        DispatchLine(
            line.line_id, line.sku_id, line.source, line.target, batch_input(line), line.batch_id,
            line.quantity, line.uom, lifecycle.leaf.name, owned[line.line_id].ownership_id,
            owned[line.line_id].native_entry, owned[line.line_id].native_detail, owned[line.line_id].inward_sle,
            format(lifecycle.received[line.line_id], "f"), format(lifecycle.returned[line.line_id], "f"),
            format(lifecycle.remaining(owned[line.line_id]), "f"),
        ) for line in sorted(lines, key=lambda line: line.line_no)
    )
    return Dispatch(company, transfer_id, log.name, doc.status, doc.revision, log.actor,
                    log.creation, log.reason, doc.notes or "", result)


def _lifecycle(doc, lines):
    if doc.status not in transit.DISPATCHED_STATUSES or not doc.effective_operation:
        raise StockError("TRANSFER_NOT_DISPATCHED")
    leaf = transit.resolve(doc.company)
    try:
        skus = {identity: _load_sku(identity, locked=True) for identity in sorted({line.sku_id for line in lines})}
    except BatchError as error:
        raise StockError(error.code) from None
    require_native_precision(skus.values(), "TRANSFER_NATIVE_PRECISION_UNCONFIGURED")
    items = {sku.native_item for sku in skus.values()}
    (buckets, _), _ = availability.read_source(
        doc.company, {(item, leaf.native_warehouse) for item in items}, skus.values(),
    )
    lifecycle = transit.reconcile(doc.company, items, leaf, buckets)
    log, owned = transit.verify_document(doc, lines, lifecycle)
    return lifecycle, log, owned, skus


def _prepare_followup(command, document, stored_lines, locations, *, action, limits):
    lifecycle, log, originals, skus = _lifecycle(document, stored_lines)
    if document.status in (transit.COMPLETED, transit.CLOSED_RETURN):
        raise StockError("TRANSFER_ALREADY_CLOSED")
    if any(line.line_id not in originals for line in command.lines):
        raise StockError("TRANSFER_ORIGINAL_LINE_INVALID")

    remaining = {key: lifecycle.remaining(row) for key, row in originals.items()}
    received = {key: lifecycle.received[key] for key in originals}
    returned = {key: lifecycle.returned[key] for key in originals}
    lines = []
    requested = {line.line_id: line.quantity for line in command.lines}
    for line in stored_lines:
        if line.line_id not in requested:
            continue
        text = quantity_text(requested[line.line_id], line.uom, sku=skus[line.sku_id],
                             sign=QuantitySign.POSITIVE, limits=limits)
        qty = Decimal(text)
        if qty > remaining[line.line_id]:
            raise StockError("TRANSFER_LINE_REMAINING_INSUFFICIENT")
        remaining[line.line_id] -= qty
        totals = received if action == stock.RECEIVE else returned
        totals[line.line_id] += qty
        original = originals[line.line_id]
        lines.append(replace(
            line, quantity=text, source=lifecycle.leaf.name, target=line.target if action == stock.RECEIVE else line.source,
            batch_id=original.mapping_id, batch_no=None,
        ))
    return Continuation(
        replace(command, lines=tuple(lines), original_operation=log.name),
        transit.status_for(remaining, received, returned),
        {**locations, lifecycle.leaf.name: lifecycle.leaf}, {line.line_id: originals[line.line_id] for line in lines},
    )


def _record_followup(plan, result, links):
    doc, lines = _lock_document(plan.command.company, result.document_id, native.PURPOSE)
    _lifecycle(doc, lines)


def receive(payload: ReceiveTransfer) -> TransferActionResult:
    return _followup(payload, stock.RECEIVE)


def return_remaining(payload: ReturnTransfer) -> TransferActionResult:
    return _followup(payload, stock.RETURN)


def _followup(payload, action, *, before_guard=None, commit=None):
    candidate = _authorize(payload.company)
    if not isinstance(payload.lines, tuple) or any(not isinstance(line, TransferQuantity) for line in payload.lines):
        raise StockError("STOCK_DUPLICATE_OR_INVALID_LINE")
    command = Command(
        payload.company, payload.transfer_id, payload.expected_revision, payload.idempotency_key,
        tuple(LineQuantity(line.transfer_line_id, line.quantity) for line in payload.lines),
        payload.reason, notes=payload.notes,
    )
    result = _execute(command, Action(
        transit.COMMANDS[action], policy.Operation.TRANSFER, QuantitySign.POSITIVE, candidate.limits,
        {(native.ENTRY, native.DETAIL): "items"}, _authorize,
        partial(native.post_followup, action=action), partial(native.verify_followup, action=action),
        native_orm=True, read_committed=True, validate_locations=_validate_locations,
        record_effect=_record_followup, document_type=native.PURPOSE, verify_history=_lifecycle,
        prepare_continuation=partial(_prepare_followup, action=action, limits=candidate.limits),
        require_reason=action == stock.RETURN,
    ), before_guard=before_guard, commit=commit)
    return TransferActionResult(result.document_id, result.operation_id, result.status, result.revision)
