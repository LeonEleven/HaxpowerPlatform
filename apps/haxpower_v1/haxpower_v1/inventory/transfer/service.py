"""Authorized transfer drafts, dispatch and original-line reads for future receipt."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from uuid import uuid4

import frappe

from ...access import policy
from ...adapters.erpnext import transfer as native
from .. import availability
from ..batch.dto import BatchError
from ..batch.service import _load_sku
from ..guard import lock_stock_writes
from ..operations import persistence as store
from ..operations.dto import Command, Line, QuantitySign, StockError
from ..operations.service import Action, _create_draft, _execute, _lock_document
from ..operations.transaction import connection
from ..posting import batch_input, require_fixture, require_native_precision, selection
from . import transit
from .dto import Dispatch, DispatchLine, DispatchResult, DispatchTransfer, TransferDraft, TransferDraftInput, TransferLine

IN_TRANSIT = "In Transit"
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
        record_effect=transit.record_dispatch,
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


def dispatch(payload: DispatchTransfer) -> DispatchResult:
    return _dispatch(payload)


def _dispatch(payload, *, before_guard=None, commit=None):
    draft = payload.draft
    candidate = _authorize(draft.company)
    lines = tuple(_line(line, number, line.transfer_line_id) for number, line in enumerate(draft.lines, 1))
    command = Command(draft.company, draft.transfer_id, draft.revision, payload.idempotency_key, lines, draft.reason, notes=draft.notes)
    try:
        result = _execute(command, _action(candidate), before_guard=before_guard, commit=commit)
    except StockError as error:
        if error.code == "STOCK_ADAPTER_SIDE_EFFECT":
            frappe.logger("haxpower_v1", allow_site=True).warning(
                "Unsafe ERPNext transaction side effect during transfer dispatch %s", command.document_id,
            )
        raise
    return DispatchResult(result.document_id, result.operation_id, IN_TRANSIT, result.revision)


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
    if doc.status != IN_TRANSIT or not doc.effective_operation:
        raise StockError("TRANSFER_NOT_DISPATCHED")
    log = store.current(store.LOG, doc.effective_operation, ("name", "actor", "creation", "reason"))
    if not log or log.reason != doc.reason:
        raise StockError(transit.INCONSISTENT)
    leaf = transit.resolve(company)
    try:
        skus = [_load_sku(identity, locked=True) for identity in sorted({line.sku_id for line in lines})]
    except BatchError as error:
        raise StockError(error.code) from None
    require_native_precision(skus, "TRANSFER_NATIVE_PRECISION_UNCONFIGURED")
    items = {sku.native_item for sku in skus}
    (buckets, _), _ = availability.read_source(
        company, {(item, leaf.native_warehouse) for item in items}, skus,
    )
    owned = {row.line_id: row for row in transit.active_inward(company, items, leaf, buckets).values()
             if row.operation_id == log.name}
    if owned.keys() != {line.line_id for line in lines}:
        raise StockError(transit.INCONSISTENT)
    result = tuple(
        DispatchLine(
            line.line_id, line.sku_id, line.source, line.target, batch_input(line), line.batch_id,
            line.quantity, line.uom, leaf.name, owned[line.line_id].ownership_id,
            owned[line.line_id].native_entry, owned[line.line_id].native_detail, owned[line.line_id].name,
        ) for line in sorted(lines, key=lambda line: line.line_no)
    )
    return Dispatch(company, transfer_id, log.name, doc.status, doc.revision, log.actor,
                    log.creation, log.reason, doc.notes or "", result)
