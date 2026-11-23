"""Multi-line receipt drafts and submission through the stock execution kernel."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from decimal import Decimal
import os
from uuid import uuid4

import frappe

from ...access import policy
from ...adapters.erpnext import receipt as native
from ..batch import service as batches
from ..batch.authorization import SYNTHETIC_SITE
from ..batch.dto import BatchError, BatchKind
from ..batch.normalization import normalize_selection
from ..operations.dto import Command, Line, QuantitySign, StockError
from ..operations.quantity import canonical_quantity
from ..operations.service import Action, _create_draft, _execute
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
    candidate = _candidate.get()
    site = frappe.local.site or ""
    project = os.environ.get("HP_V1_PROJECT", "")
    actor = frappe.session.user
    if (
        not candidate or candidate.actor != actor or candidate.company != company
        or not isinstance(actor, str) or not actor.endswith("@example.invalid")
        or not isinstance(company, str) or not company.startswith("Synthetic V1 Warehouse ")
        or getattr(frappe.local, "request", None) is not None
        or not project.startswith("haxpower-v1-ci-") or not SYNTHETIC_SITE.fullmatch(site)
        or site not in (f"{project}.localhost", f"{project}-upgrade.localhost")
        or frappe.conf.get("db_host") != "mariadb"
    ):
        raise StockError("RECEIPT_NOT_ENABLED")
    return candidate


def _selection(payload):
    try:
        selection = normalize_selection(payload)
    except BatchError as error:
        raise StockError(error.code) from None
    if selection.normalization and selection.normalization.original != selection.normalization.candidate:
        raise StockError("BATCH_NORMALIZATION_CONFIRMATION_REQUIRED")
    return selection


def _batch_input(line):
    if line.batch_kind == BatchKind.BATCH:
        return {"mode": "BATCH", "batchNo": line.batch_no}
    return {"mode": "NO_BATCH"}


def _line(line, number, line_id):
    selection = _selection(line.batch)
    return Line(line_id, number, line.sku_id, line.quantity, line.uom, selection.kind,
                target=line.location_id, reason=line.reason, notes=line.notes,
                batch_no=selection.normalization.original if selection.normalization else None)


def _public_lines(lines):
    return tuple(ReceiptLine(
        line.sku_id, line.target, _batch_input(line), line.quantity, line.uom, line.reason, line.notes, line.line_id,
    ) for line in sorted(lines, key=lambda line: line.line_no))


def _resolve_batches(command, skus):
    from frappe.utils import cint

    # ERPNext's SLE accumulator defaults to two decimals in the absence of
    # an explicit default, even when native quantity fields accept three.
    precision = cint(frappe.db.get_default("float_precision"))
    if not 1 <= precision <= 9 or any(sku.quantity_precision > precision for sku in skus.values()):
        raise StockError("RECEIPT_NATIVE_PRECISION_UNCONFIGURED")

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
                    skus[line.sku_id], _selection(_batch_input(line)), authorize,
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
        native.verify_link, resolve_batches=_resolve_batches,
    )


def create_draft(payload: ReceiptDraftInput) -> ReceiptDraft:
    candidate = _authorize(payload.company)
    lines = tuple(_line(line, index, uuid4().hex) for index, line in enumerate(payload.lines, 1))
    command = Command(payload.company, uuid4().hex, 1, uuid4().hex, lines, payload.reason)
    draft = _create_draft(command, _action(candidate), notes=payload.notes)
    return ReceiptDraft(draft.company, draft.document_id, draft.expected_revision, _public_lines(draft.lines), draft.reason)


def submit(payload: SubmitReceipt) -> ReceiptResult:
    return _submit(payload)


def _submit(payload, *, before_guard=None, commit=None):
    draft = payload.draft
    candidate = _authorize(draft.company)
    lines = tuple(_line(line, index, line.line_id) for index, line in enumerate(draft.lines, 1))
    command = Command(draft.company, draft.document_id, draft.revision, payload.idempotency_key, lines, draft.reason)
    result = _execute(command, _action(candidate), before_guard=before_guard, commit=commit)
    canonical = tuple(replace(line, quantity=canonical_quantity(line.quantity)) for line in lines)
    return ReceiptResult(result.document_id, result.operation_id, "Effective", result.revision, _public_lines(canonical))
