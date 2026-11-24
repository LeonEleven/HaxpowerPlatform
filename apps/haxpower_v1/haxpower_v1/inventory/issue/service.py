"""Authorized multi-line issue drafts and submission through the stock kernel."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from uuid import uuid4

import frappe

from ...access import policy
from ...adapters.erpnext import issue as native
from .. import availability
from ..operations.dto import Command, Line, QuantitySign, StockError
from ..operations.quantity import canonical_quantity
from ..operations.service import Action, _create_draft, _execute
from ..posting import batch_input, require_fixture, selection
from .dto import IssueDraft, IssueDraftInput, IssueLine, IssueResult, SubmitIssue

ACTION = "Material Issue"
_candidate = ContextVar("hp_v1_issue_candidate", default=None)


@dataclass(frozen=True)
class Candidate:
    actor: str
    company: str
    limits: dict
    allow_gl: bool


@contextmanager
def synthetic_candidate(company, *, limits, allow_gl=False):
    """Enable issue commands for the explicitly selected disposable fixture."""
    if type(allow_gl) is not bool:
        raise StockError("ISSUE_VALUATION_INVALID")
    token = _candidate.set(Candidate(frappe.session.user, company, dict(limits), allow_gl))
    try:
        _authorize(company)
        yield
    finally:
        _candidate.reset(token)


def _authorize(company):
    return require_fixture(_candidate.get(), company, "ISSUE_NOT_ENABLED")


def _line(line, number, line_id):
    parsed = selection(line.batch)
    return Line(line_id, number, line.sku_id, line.quantity, line.uom, parsed.kind,
                source=line.source_location_id, reason=line.reason, notes=line.notes,
                batch_no=parsed.normalization.original if parsed.normalization else None)


def _public_lines(lines):
    return tuple(IssueLine(
        line.sku_id, line.source, batch_input(line), line.quantity, line.uom, line.reason, line.notes, line.line_id,
    ) for line in sorted(lines, key=lambda line: line.line_no))


def _action(candidate):
    return Action(
        ACTION, policy.Operation.ISSUE, QuantitySign.POSITIVE, candidate.limits,
        {(native.ENTRY, native.DETAIL): "items"}, _authorize,
        lambda plan: native.post(plan, allow_gl=candidate.allow_gl), native.verify_link,
        resolve_batches=availability.resolve_existing_lines, bind_batches_on_draft=True, native_orm=True, read_committed=True,
    )


def create_draft(payload: IssueDraftInput) -> IssueDraft:
    candidate = _authorize(payload.company)
    lines = tuple(_line(line, number, uuid4().hex) for number, line in enumerate(payload.lines, 1))
    command = Command(payload.company, uuid4().hex, 1, uuid4().hex, lines, payload.reason)
    draft = _create_draft(command, _action(candidate), notes=payload.notes)
    return IssueDraft(draft.company, draft.document_id, draft.expected_revision, _public_lines(draft.lines), draft.reason)


def submit(payload: SubmitIssue) -> IssueResult:
    return _submit(payload)


def _submit(payload, *, before_guard=None, commit=None):
    draft = payload.draft
    candidate = _authorize(draft.company)
    lines = tuple(_line(line, number, line.line_id) for number, line in enumerate(draft.lines, 1))
    command = Command(draft.company, draft.document_id, draft.revision, payload.idempotency_key, lines, draft.reason)
    result = _execute(command, _action(candidate), before_guard=before_guard, commit=commit)
    canonical = tuple(replace(line, quantity=canonical_quantity(line.quantity)) for line in lines)
    return IssueResult(result.document_id, result.operation_id, "Effective", result.revision, _public_lines(canonical))


def get_availability(company, sku_id, source_location_id, batch):
    return availability.get_available(company, sku_id, source_location_id, batch, authorize=_authorize)
