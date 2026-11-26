"""B07 specialist workflow, with native posting limited to isolated Bench fixtures."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, replace
from decimal import Decimal
from functools import partial
import json
from uuid import uuid4

import frappe

from ...access import policy
from ...adapters.erpnext import issue, receipt
from ...approval import SUBMISSION
from ...approval import policy as rules, service as approval
from ...approval import persistence as approvals
from .. import versions
from ..batch.service import lock_execution_mapping
from ..batch.dto import BatchError
from ..guard import lock_stock_writes
from ..operations import persistence as stock, service as kernel
from ..operations.dto import Command, QuantitySign, Result, StockError, sum_demands
from ..operations.quantity import canonical_quantity
from ..operations.transaction import connection
from ..posting import require_fixture, require_native_precision
from . import CASE, REVIEW, facts

MANUAL = "Manual Review"
CLOSED = "Closed without Posting"
_candidate = ContextVar("hp_v1_correction_candidate", default=None)
_execution = ContextVar("hp_v1_correction_execution", default=None)
INCONSISTENCIES = frozenset((
    facts.INVALID, "CORRECTION_ENTITLEMENT_INVALID", "INVENTORY_NATIVE_HISTORY_INVALID",
    "INVENTORY_VERSION_NOT_READY", "INVENTORY_QUANTITY_HISTORY_CHANGED",
    "BATCH_MAPPING_UNAVAILABLE", "STOCK_NATIVE_REFERENCE_INVALID",
    "STOCK_NATIVE_REFERENCE_INCOMPLETE",
))
REVIEW_FAILURES = INCONSISTENCIES | {"INSUFFICIENT_STOCK", "CORRECTION_VALUATION_UNSUPPORTED"}


@dataclass(frozen=True)
class Adjustment:
    original_line_id: str
    quantity: str
    reason: str = ""
    notes: str = ""


@dataclass(frozen=True)
class Candidate:
    actor: str
    company: str
    limits: dict


@contextmanager
def synthetic_candidate(company, *, limits):
    """Explicitly enable zero-value, no-GL posting in the isolated V1 environment."""
    candidate = Candidate(frappe.session.user, company, dict(limits))
    require_fixture(candidate, company, "CORRECTION_NOT_ENABLED")
    token = _candidate.set(candidate)
    try:
        yield
    finally:
        _candidate.reset(token)


def _case(document_id):
    case = stock.current(CASE, document_id, (
        "name", "original_operation", "line_origins", "replacement_document", "replacement_required",
    ))
    if not case:
        policy.deny()
    return case


def _load_case(company, document_id):
    document, lines = kernel._lock_document(company, document_id, rules.CORRECTION)
    case = _case(document_id)
    mapping = facts.line_origins(case)
    if set(mapping) != {line.line_id for line in lines}:
        raise StockError(facts.INVALID)
    return document, lines, case, mapping


def _origin(company, operation_id, *, capability=True):
    operation, document, locations = facts.origin(company, operation_id, capability=capability)
    return operation, document, locations, facts.original_lines(company, document)


def _assess(company, operation, document, originals, selected=None, *, native=None):
    try:
        return facts.assess(company, operation, document, originals, selected, native=native)
    except StockError as error:
        if error.code not in INCONSISTENCIES:
            raise
        return None, {}, [facts._reason(error.code, operation_id=operation.name)]


def _public_evidence(evidence):
    # Stable domain identities are public; underlying stock-table identifiers
    # and verification digests are retained in the specialist snapshot.
    return {identity: {k: v for k, v in row.items() if k not in ("native_sle", "fact_digest")}
            for identity, row in evidence.items()}


def _public_reasons(company, reasons):
    visible = {}
    result = []
    for reason in reasons:
        operation_id = reason["operation_id"]
        if operation_id and operation_id not in visible:
            document_id = frappe.db.get_value(stock.LOG, operation_id, "document_id", for_update=True)
            try:
                if document_id:
                    facts.scope(company, facts.document(company, document_id))
                visible[operation_id] = bool(document_id)
            except frappe.PermissionError:
                visible[operation_id] = False
        result.append(reason if visible.get(operation_id) else {**reason, "operation_id": None})
    return result


def eligibility(company, operation_id):
    kernel._id(operation_id)
    with connection(read_committed=True):
        lock_stock_writes(company)
        operation, document, _, originals = _origin(company, operation_id)
        _, evidence, reasons = _assess(company, operation, document, originals)
        return {"document_id": document.name, "operation_id": operation.name, "type": document.document_type,
                "lines": _public_evidence(evidence), "reasons": _public_reasons(company, reasons),
                "transfer": facts.transfer_history(company, document) if document.document_type == rules.TRANSFER else ()}


def _draft_lines(kind, original_document, originals, adjustments, previous_ids):
    if not isinstance(adjustments, (tuple, list)) or not adjustments:
        raise StockError("STOCK_LINES_REQUIRED")
    originals = {line.line_id: line for line in originals}
    count_rows = {row.name: row for row in facts.counts._rows(original_document)} if kind == rules.COUNT else {}
    lines, mapping = [], {}
    for number, item in enumerate(adjustments, 1):
        if not isinstance(item, Adjustment):
            raise StockError("CORRECTION_LINE_INVALID")
        kernel._id(item.original_line_id)
        original = originals.pop(item.original_line_id, None)
        quantity = canonical_quantity(item.quantity)
        if not original or Decimal(quantity) <= 0:
            raise StockError("CORRECTION_LINE_INVALID")
        kernel._text(item.reason, 4000)
        kernel._text(item.notes, 4000)
        if kind == rules.COUNT:
            count = count_rows[original.line_id]
            if not count.difference_quantity or not Decimal(count.difference_quantity):
                raise StockError("CORRECTION_NO_QUANTITY_EFFECT")
            decrease = Decimal(count.difference_quantity) > 0
        else:
            decrease = kind in (rules.RECEIPT, rules.TRANSFER)
        source = (original.source or original.target) if decrease else None
        target = (original.target or original.source) if not decrease else None
        identity = previous_ids.get(original.line_id) or uuid4().hex
        lines.append(replace(original, line_id=identity, line_no=number, quantity=quantity,
                             source=source, target=target, batch_no=None, reason=item.reason, notes=item.notes))
        mapping[identity] = original.line_id
    return tuple(sorted(lines, key=lambda line: line.line_id)), mapping


def save_draft(company, original_operation, adjustments, reason, *, correction_id=None, revision=1,
               replacement_required=None):
    """Create or revise an applicant-owned draft using original line identities."""
    kernel._id(original_operation)
    approval._revision(revision)
    approval._reason(reason, True)
    if correction_id is not None:
        kernel._id(correction_id)
    if replacement_required is not None and type(replacement_required) is not bool:
        raise StockError("CORRECTION_ASSOCIATION_INVALID")
    with connection(read_committed=True) as database:
        lock_stock_writes(company)
        actor = policy.active_actor(locked=True)
        previous = {}
        if correction_id is not None:
            document, _, case, mapping = _load_case(company, correction_id)
            if document.owner != actor or case.original_operation != original_operation:
                policy.deny()
            if document.status not in ("Draft", rules.REJECTED) or document.effective_operation:
                raise StockError("STOCK_DOCUMENT_NOT_EDITABLE")
            if document.revision != revision:
                raise StockError("STOCK_REVISION_CONFLICT")
            previous = {value: key for key, value in mapping.items()}
        _, original, _, originals = _origin(company, original_operation)
        lines, mapping = _draft_lines(original.document_type, original.name, originals, adjustments, previous)
        if correction_id is None:
            if revision != 1:
                raise StockError("STOCK_REVISION_CONFLICT")
            correction_id = uuid4().hex
            stock.insert(stock.DOCUMENT, name=correction_id, company=company, document_type=rules.CORRECTION,
                         document_number=f"CORRECTION-{correction_id}", status="Draft", revision=revision, reason=reason)
            stock.insert(CASE, name=correction_id, original_operation=original_operation,
                         line_origins=approvals.encode(mapping), replacement_required=int(bool(replacement_required)))
        else:
            # Previously submitted versions remain in immutable approval snapshots.
            frappe.db.sql(f"DELETE FROM `tab{stock.LINE}` WHERE document_id=%s", (correction_id,))
            revision += 1
            frappe.db.sql(f"UPDATE `tab{stock.DOCUMENT}` SET revision=%s,reason=%s WHERE name=%s",
                          (revision, reason, correction_id))
            frappe.db.sql(f"""UPDATE `tab{CASE}`
                SET line_origins=%s,replacement_required=COALESCE(%s,replacement_required) WHERE name=%s""",
                          (approvals.encode(mapping), replacement_required, correction_id))
        for line in lines:
            kernel._insert_line(correction_id, line, _DRAFT_ACTION)
        kernel._commit(database)
        return Result(correction_id, None, revision, "Draft" if not previous else document.status)


@dataclass(frozen=True)
class Submission:
    company: str
    document_id: str
    expected_revision: int
    idempotency_key: str


# Drafts only use domain batch identities. Native NO_BATCH is resolved under
# the adapter scope for each posting.
_DRAFT_ACTION = kernel.Action(rules.CORRECTION, policy.Operation.CORRECTION_APPROVE, QuantitySign.POSITIVE,
                              {}, {}, None, None, None)


def _selected(lines, mapping):
    return {mapping[line.line_id]: Decimal(line.quantity) for line in lines}


def _submit_stage(database, payload, *, recovery):
    lock_stock_writes(payload.company)
    actor = policy.active_actor(locked=True)
    identity = kernel._digest((payload.company, actor, "Correction Submit", payload.idempotency_key))
    digest = kernel._digest(asdict(payload))
    request = approvals.request(identity)
    document, lines, case, mapping = _load_case(payload.company, request.document_id if request else payload.document_id)
    if document.owner != actor:
        policy.deny()
    operation, original, locations, originals = _origin(payload.company, case.original_operation)
    if request:
        approval.authorize_snapshot(approvals.submission(request.submission_id))
        return approvals.replay(request, digest), False
    if recovery:
        raise StockError("STOCK_RETRY_REQUIRED")
    if document.revision != payload.expected_revision:
        raise StockError("STOCK_REVISION_CONFLICT")
    if document.status not in ("Draft", rules.REJECTED) or document.effective_operation:
        raise StockError("APPROVAL_STATE_CONFLICT")
    _, evidence, reasons = _assess(payload.company, operation, original, originals, _selected(lines, mapping))
    command = Command(payload.company, document.name, document.revision, payload.idempotency_key, lines,
                      document.reason, original_operation=operation.name)
    snapshot = {"command": approval._command_payload(command), "locations": sorted(locations),
                "requirement": rules.requirement(rules.CORRECTION), "line_origins": mapping,
                "original_document": original.name, "original_type": original.document_type,
                "evidence": evidence, "reasons": reasons, "replacement_required": bool(case.replacement_required)}
    submission_id = stock.insert(
        SUBMISSION, company=document.company, document_id=document.name, document_type=rules.CORRECTION,
        business_revision=document.revision, creator=document.owner, submitter=actor,
        configuration_revision=approvals.settings().revision, approval_required=1,
        payload=approvals.encode(snapshot), payload_digest=kernel._digest(snapshot),
    )
    submission = approvals.submission(submission_id)
    result = Result(document.name, None, document.revision + 1, rules.PENDING, submission_id)
    approval._set_state(document, result, submission_id)
    approvals.event(submission, "Resubmit" if document.approval_submission else "Submit",
                    document.status, result, actor, identity, reason=document.reason)
    approvals.save_request(identity, document.company, actor, document.name, submission_id, "Submit", digest, result)
    return result, True


def submit(payload, *, _before_guard=None, _commit=None):
    kernel._text(payload.company, 140, required=True)
    kernel._id(payload.document_id)
    approval._revision(payload.expected_revision)
    if not isinstance(payload.idempotency_key, str) or not approval.KEY.fullmatch(payload.idempotency_key):
        raise StockError("STOCK_INVALID_REQUEST_KEY")
    return kernel._run(partial(_submit_stage, payload=payload), read_committed=True,
                       before_guard=_before_guard, commit=_commit)


def _review(document, submission, actor, action, reasons, *, reason="", resolution=None):
    return stock.insert(REVIEW, correction_id=document.name, submission_id=submission.name,
                        actor=actor, action=action, evidence=approvals.encode(reasons), reason=reason,
                        resolution_document=resolution)


def _manual(document, submission, actor, reasons, reason=""):
    _review(document, submission, actor, "Open", reasons, reason=reason)
    return Result(document.name, None, document.revision + 1, MANUAL, submission.name)


def _require_applicant(company, original_type, locations):
    policy.require_operation(facts.OPERATIONS.get(original_type, policy.Operation.CORRECTION_APPROVE), locked=True)
    return policy.lock_execution_locations(company, locations)


def _resolve(command, skus):
    lines = []
    for line in command.lines:
        try:
            mapping = lock_execution_mapping(skus[line.sku_id], line.batch_kind, line.batch_id)
        except BatchError as error:
            raise StockError(error.code) from None
        if not mapping:
            raise StockError("BATCH_MAPPING_UNAVAILABLE")
        lines.append(replace(line, batch_id=mapping.id))
    return replace(command, lines=tuple(lines))


def _post(plan):
    execution = _execution.get()
    domain_command = replace(plan.command, lines=tuple(
        replace(line, batch_id=None) if line.batch_kind == "NO_BATCH" else line for line in plan.command.lines
    ))
    if execution != (domain_command, frappe.session.user):
        raise StockError("CORRECTION_SPECIALIST_APPROVAL_REQUIRED")
    require_native_precision(plan.skus.values(), "CORRECTION_NATIVE_PRECISION_UNCONFIGURED")
    if any(not sku.code.startswith("SYNTHETIC-") for sku in plan.skus.values()):
        policy.deny()
    links = []
    for direction in ("source", "target"):
        lines = tuple(line for line in plan.command.lines if getattr(line, direction))
        if not lines:
            continue
        command = replace(plan.command, lines=lines)
        current = replace(plan, command=command,
                          source_demand=sum_demands(command.company, lines, location_field="source"),
                          target_demand=sum_demands(command.company, lines, location_field="target"))
        try:
            posted = issue.post(current, allow_gl=False) if direction == "source" else receipt.post(
                current, rate=Decimal(0), allow_gl=False)
        except StockError as error:
            if error.code.endswith("_GL_NOT_ALLOWED"):
                raise StockError("CORRECTION_VALUATION_UNSUPPORTED") from None
            raise
        links.extend(posted)
    parents = tuple(sorted({link.parent_id for link in links}))
    if frappe.db.sql("""SELECT name FROM `tabStock Ledger Entry`
        WHERE voucher_type='Stock Entry' AND voucher_no IN %s AND stock_value_difference<>0 FOR UPDATE""",
                     (parents,)):
        raise StockError("CORRECTION_VALUATION_UNSUPPORTED")
    return tuple(links)


def _verify_link(plan, link):
    line = next(line for line in plan.command.lines if line.line_id == link.line_id)
    return (issue if line.source else receipt).verify_link(plan, link)


def _approved_effect(database, submission, document, actor, identity, *, failed=None, reason=""):
    """Called only by the specialist decision path after the CAS and scope check."""
    data = approval._data(submission)
    candidate = _candidate.get()
    if failed is None:
        require_fixture(candidate, document.company, "CORRECTION_NOT_ENABLED")
    with approval._posting_principal(submission):
        loaded, lines, case, mapping = _load_case(document.company, document.name)
        operation, original, scope, originals = _origin(document.company, case.original_operation)
        locations = _require_applicant(document.company, original.document_type, scope)
        command = Command(document.company, loaded.name, loaded.revision, identity, lines, loaded.reason,
                          original_operation=case.original_operation)
        frozen = replace(command, expected_revision=submission.business_revision)
        if (data.get("requirement") != rules.CORRECTION_APPROVAL or data.get("line_origins") != mapping
                or sorted(locations) != data["locations"] or data["original_document"] != original.name
                or data["original_type"] != original.document_type
                or data["replacement_required"] != bool(case.replacement_required)
                or approvals.encode(data["command"]) != approvals.encode(approval._command_payload(frozen))):
            raise StockError("STOCK_DOCUMENT_PAYLOAD_MISMATCH")
        if failed:
            if failed not in REVIEW_FAILURES:
                return None, None
            reasons = [facts._reason(failed, operation_id=operation.name)]
        else:
            before, evidence, reasons = _assess(document.company, operation, original, originals,
                                                _selected(lines, mapping))
            if not reasons and (data["evidence"].keys() != evidence.keys() or any(
                any(evidence[line_id][field] != expected[field] for field in ("bucket", "net", "uom", "native_sle", "generation"))
                for line_id, expected in data["evidence"].items()
            )):
                reasons = [facts._reason(facts.INVALID, operation_id=operation.name)]
            if not reasons and any(
                line.uom != evidence[mapping[line.line_id]]["uom"]
                or asdict(facts._bucket(document.company, line)) != evidence[mapping[line.line_id]]["bucket"]
                or bool(line.source) != (Decimal(evidence[mapping[line.line_id]]["net"]) > 0)
                for line in lines
            ):
                reasons = [facts._reason(facts.INVALID, operation_id=operation.name)]
        if not reasons:
            action = replace(_DRAFT_ACTION, limits=candidate.limits, detail_fields={(receipt.ENTRY, receipt.DETAIL): "items"},
                             apply=_post, verify_reference=_verify_link, resolve_batches=_resolve, native_orm=True)
            request_id = kernel._digest(("Specialist Correction Effect", identity))
            digest = kernel.payload_digest(command, actor, action.name)
            kernel._reserve_request(command, action, actor, request_id, request_id, digest)
            token = _execution.set((command, submission.submitter))
            try:
                applied = kernel._apply(database, command, loaded, lines, locations, action, verified_versions=before)
            finally:
                _execution.reset(token)
    if reasons:
        return _manual(document, submission, actor, reasons, reason), None
    return kernel._record(document, action, applied, request_id, actor, submission.submitter, submission.name), submission.submitter


def _authorize_decision(verb, actor, document, submission):
    case = _case(document.name)
    _, original, scope = facts.origin(document.company, case.original_operation, capability=False)
    data = approval._data(submission)
    if (data.get("requirement") != rules.CORRECTION_APPROVAL or not submission.approval_required
            or data.get("original_document") != original.name or sorted(scope) != data["locations"]):
        raise StockError("APPROVAL_SUBMISSION_INCONSISTENT")
    if verb == "Withdraw":
        if actor != submission.creator or actor != document.owner:
            policy.deny()
        _require_applicant(document.company, original.document_type, scope)


def approve(payload, *, _before_guard=None, _commit=None):
    return approval._decide(payload, "Approve", specialist=True, before_guard=_before_guard, commit=_commit)


def reject(payload):
    return approval._decide(payload, "Reject", specialist=True)


def withdraw(payload):
    return approval._decide(payload, "Withdraw", specialist=True)


def _require_read(company, header):
    operations = policy.granted_operations(locked=True)
    allowed = {policy.Operation.INVENTORY_READ, policy.Operation.CORRECTION_APPROVE,
               facts.OPERATIONS.get(header.document_type)}
    if not operations & allowed:
        policy.deny()
    return facts.scope(company, header)


def _association(company, document_id, *, effective=False):
    header = facts.document(company, document_id)
    if header.document_type not in (rules.RECEIPT, rules.ISSUE, rules.COUNT, rules.TRANSFER):
        policy.deny()
    _require_read(company, header)
    if effective and not header.effective_operation:
        raise StockError("CORRECTION_ASSOCIATION_NOT_EFFECTIVE")
    return header


def _replacement(company, correction, document_id):
    if correction.status != "Effective" or not correction.effective_operation:
        raise StockError("APPROVAL_STATE_CONFLICT")
    normal = _association(company, document_id)
    if normal.effective_operation:
        before = stock.current(stock.LOG, correction.effective_operation, ("creation",))
        after, _, _ = facts.origin(company, normal.effective_operation, capability=False)
        if not before or not before.creation or not after.creation or after.creation <= before.creation:
            raise StockError("CORRECTION_ASSOCIATION_ORDER_INVALID")
    return normal


def _review_stage(database, payload, action, related, *, recovery):
    lock_stock_writes(payload.company)
    actor = policy.active_actor(locked=True)
    policy.require_operation(policy.Operation.CORRECTION_APPROVE, locked=True)
    identity = kernel._digest((payload.company, actor, action, payload.idempotency_key))
    digest = kernel._digest((asdict(payload), related))
    request = approvals.request(identity)
    document = approval._lock_document(payload.company, request.document_id if request else payload.document_id,
                                       specialist=True)
    submission = approvals.submission(request.submission_id if request else payload.submission_id)
    approval._belongs(submission, document)
    _authorize_decision(action, actor, document, submission)
    if related:
        original = _case(document.name).original_operation
        associated = (_replacement(payload.company, document, related) if action == "Link Replacement"
                      else _association(payload.company, related, effective=True))
        if associated.effective_operation == original:
            raise StockError("CORRECTION_ASSOCIATION_INVALID")
    if request:
        return approvals.replay(request, digest), False
    if recovery:
        raise StockError("STOCK_RETRY_REQUIRED")
    if (document.revision, document.approval_submission) != (payload.expected_revision, payload.submission_id):
        raise StockError("STOCK_REVISION_CONFLICT")
    if action == "Link Replacement":
        if document.status != "Effective" or not document.effective_operation:
            raise StockError("APPROVAL_STATE_CONFLICT")
        case = _case(document.name)
        if case.replacement_document:
            if case.replacement_document != related:
                raise StockError("CORRECTION_ASSOCIATION_EXISTS")
        else:
            if frappe.db.get_value(CASE, {"replacement_document": related}, "name", for_update=True):
                raise StockError("CORRECTION_ASSOCIATION_EXISTS")
            frappe.db.sql(f"UPDATE `tab{CASE}` SET replacement_document=%s WHERE name=%s", (related, document.name))
        result = Result(document.name, document.effective_operation, document.revision, document.status, submission.name)
    else:
        if document.status != MANUAL or document.effective_operation:
            raise StockError("APPROVAL_STATE_CONFLICT")
        status = {"Close Review": CLOSED, "Reject Review": rules.REJECTED, "Review Note": MANUAL}[action]
        _review(document, submission, actor, action, [], reason=payload.reason, resolution=related)
        result = Result(document.name, None, document.revision + 1, status, submission.name)
        approval._set_state(document, result, submission.name)
    approvals.event(submission, action, document.status, result, actor, identity, reason=payload.reason)
    approvals.save_request(identity, document.company, actor, document.name, submission.name, action, digest, result)
    return result, True


def _review_action(payload, action, related):
    kernel._text(payload.company, 140, required=True)
    kernel._id(payload.document_id)
    kernel._id(payload.submission_id)
    if related is not None:
        kernel._id(related)
        if related == payload.document_id:
            raise StockError("CORRECTION_ASSOCIATION_INVALID")
    approval._revision(payload.expected_revision)
    approval._reason(payload.reason, True)
    if not isinstance(payload.idempotency_key, str) or not approval.KEY.fullmatch(payload.idempotency_key):
        raise StockError("STOCK_INVALID_REQUEST_KEY")
    return kernel._run(partial(_review_stage, payload=payload, action=action, related=related), read_committed=True)


def record_review(payload):
    return _review_action(payload, "Review Note", None)


def close_review(payload, *, resolution_document=None):
    return _review_action(payload, "Close Review", resolution_document)


def reject_review(payload):
    return _review_action(payload, "Reject Review", None)


def link_replacement(payload, replacement_document):
    kernel._id(replacement_document)
    return _review_action(payload, "Link Replacement", replacement_document)


def history(company, correction_id):
    kernel._id(correction_id)
    with connection(read_committed=True):
        lock_stock_writes(company)
        document, lines, case, mapping = _load_case(company, correction_id)
        _, original, _ = facts.origin(company, case.original_operation, capability=False)
        _require_read(company, original)
        replacement = None
        if case.replacement_document:
            current = _replacement(company, document, case.replacement_document)
            replacement = {"document_id": current.name, "operation_id": current.effective_operation,
                           "status": current.status}
        fields = ("name", "submission_id", "actor", "action", "creation", "evidence", "reason", "resolution_document")
        reviews = frappe.db.get_values(REVIEW, {"correction_id": correction_id}, fields,
                                       for_update=True, as_dict=True, order_by="creation asc, name asc")
        for review in reviews:
            if review.resolution_document:
                _association(company, review.resolution_document, effective=True)
            review.evidence = _public_reasons(company, json.loads(review.evidence))
        submissions = []
        for identity in frappe.db.get_values(SUBMISSION, {"document_id": correction_id}, "name",
                                             for_update=True, pluck=True, order_by="business_revision asc"):
            submission = approvals.submission(identity)
            approval._belongs(submission, document)
            approval.authorize_snapshot(submission)
            data = approval._data(submission)
            submissions.append({"submission_id": identity, "creator": submission.creator,
                                "submitter": submission.submitter, "created_at": submission.creation,
                                "revision": submission.business_revision, "requirement": data["requirement"],
                                "line_origins": data["line_origins"], "reason": data["command"]["reason"],
                                "lines": data["command"]["lines"], "reasons": _public_reasons(company, data["reasons"]),
                                "evidence": _public_evidence(data["evidence"])})
        events = frappe.db.get_values(approval.EVENT, {"document_id": correction_id, "company": company},
            ("name", "submission_id", "actor", "native_actor", "creation", "action", "business_revision",
             "reason", "operation_id", "resulting_status", "failure_code"),
            for_update=True, as_dict=True, order_by="business_revision asc, creation asc, name asc")
        return {"correction_id": correction_id, "original_document_id": original.name,
                "original_operation": case.original_operation, "original_type": original.document_type,
                "operation_id": document.effective_operation, "status": document.status, "revision": document.revision,
                "applicant": document.owner, "reason": document.reason,
                "lines": [{**asdict(line), "original_line_id": mapping[line.line_id]} for line in lines],
                "submissions": submissions, "events": events, "reviews": reviews, "replacement": replacement,
                "replacement_required": bool(case.replacement_required),
                "replacement_pending": document.status == "Effective" and bool(case.replacement_required)
                and not (replacement and replacement["operation_id"])}
