"""Multi-line receipt drafts and submission through the stock execution kernel."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from decimal import Decimal
from uuid import uuid4

import frappe

from ...access import policy
from ...adapters.erpnext import receipt as native
from ..batch import service as batches
from ..batch.dto import BatchError
from ..operations.dto import Command, Line, QuantitySign, StockError
from ..operations.quantity import canonical_quantity
from ..operations.service import Action, _create_draft, _execute, _revise_draft
from ..posting import batch_input, require_fixture, require_native_precision, selection
from .dto import ReceiptDraft, ReceiptDraftInput, ReceiptLine, ReceiptResult, SubmitReceipt

ACTION = "Material Receipt"
_candidate = ContextVar("hp_v1_receipt_candidate", default=None)


@dataclass(frozen=True)
class Candidate:
    actor: str
    company: str
    limits: dict
    rate: Decimal
    allow_gl: bool


@contextmanager
def synthetic_candidate(company, *, limits, rate=Decimal(0), allow_gl=False):
    """Enable only an explicitly invoked, process-local Bench fixture."""
    token = _candidate.set(Candidate(frappe.session.user, company, dict(limits), rate, allow_gl))
    try:
        _authorize(company)
        if not isinstance(rate, Decimal) or not rate.is_finite() or rate < 0 or type(allow_gl) is not bool:
            raise StockError("RECEIPT_VALUATION_INVALID")
        yield
    finally:
        _candidate.reset(token)


def _authorize(company):
    return require_fixture(_candidate.get(), company, "RECEIPT_NOT_ENABLED")


def _line(line, number, line_id):
    parsed = selection(line.batch)
    return Line(line_id, number, line.sku_id, line.quantity, line.uom, parsed.kind,
                target=line.location_id, reason=line.reason, notes=line.notes,
                batch_no=parsed.normalization.original if parsed.normalization else None)


def _public_lines(lines):
    return tuple(ReceiptLine(
        line.sku_id, line.target, batch_input(line), line.quantity, line.uom, line.reason, line.notes, line.line_id,
    ) for line in sorted(lines, key=lambda line: line.line_no))


def _resolve_batches(command, skus):
    require_native_precision(skus.values(), "RECEIPT_NATIVE_PRECISION_UNCONFIGURED")

    def authorize(sku):
        _authorize(command.company)
        if not sku.code.startswith("SYNTHETIC-"):
            policy.deny()

    resolved = {}
    lines = []
    for line in command.lines:
        key = (line.sku_id, line.batch_kind, line.batch_no)
        if key not in resolved:
            try:
                resolved[key] = batches._resolve_locked(
                    skus[line.sku_id], selection(batch_input(line)), authorize,
                ).batch.id
            except BatchError as error:
                raise StockError(error.code) from None
        lines.append(replace(line, batch_id=resolved[key]))
    return replace(command, lines=tuple(lines))


def _action(candidate):
    return Action(
        ACTION, policy.Operation.RECEIVE, QuantitySign.POSITIVE, candidate.limits,
        {(native.ENTRY, native.DETAIL): "items"}, _authorize,
        lambda plan: native.post(plan, rate=candidate.rate, allow_gl=candidate.allow_gl),
        native.verify_link, resolve_batches=_resolve_batches, native_orm=True,
    )


def create_draft(payload: ReceiptDraftInput) -> ReceiptDraft:
    candidate = _authorize(payload.company)
    lines = tuple(_line(line, index, uuid4().hex) for index, line in enumerate(payload.lines, 1))
    command = Command(payload.company, uuid4().hex, 1, uuid4().hex, lines, payload.reason)
    draft = _create_draft(command, _action(candidate), notes=payload.notes)
    return ReceiptDraft(draft.company, draft.document_id, draft.expected_revision, _public_lines(draft.lines), draft.reason)


def revise_draft(draft: ReceiptDraft, *, notes=None) -> ReceiptDraft:
    candidate = _authorize(draft.company)
    ids = tuple(line.line_id or uuid4().hex for line in draft.lines)
    new_ids = {id_ for line, id_ in zip(draft.lines, ids, strict=True) if not line.line_id}
    lines = tuple(_line(line, number, id_) for number, (line, id_) in enumerate(
        zip(draft.lines, ids, strict=True), 1,
    ))
    command = Command(draft.company, draft.document_id, draft.revision, uuid4().hex, lines, draft.reason)
    updated = _revise_draft(command, _action(candidate), new_line_ids=new_ids, notes=notes)
    return replace(draft, revision=updated.expected_revision, lines=_public_lines(updated.lines))


def submit(payload: SubmitReceipt) -> ReceiptResult:
    return _submit(payload)


def _submit(payload, *, before_guard=None, commit=None):
    draft = payload.draft
    candidate = _authorize(draft.company)
    lines = tuple(_line(line, index, line.line_id) for index, line in enumerate(draft.lines, 1))
    command = Command(draft.company, draft.document_id, draft.revision, payload.idempotency_key, lines, draft.reason)
    result = _execute(command, _action(candidate), before_guard=before_guard, commit=commit)
    canonical = tuple(replace(line, quantity=canonical_quantity(line.quantity)) for line in lines)
    return ReceiptResult(result.document_id, result.operation_id, result.status, result.revision,
                         _public_lines(canonical), result.submission_id)
