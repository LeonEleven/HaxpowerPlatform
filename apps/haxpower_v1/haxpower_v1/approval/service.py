"""Site-wide approval and decisions over immutable inventory submissions."""

from collections import defaultdict
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from functools import partial
import json
from uuid import uuid4

import frappe
from frappe.utils import now_datetime

from ..access import policy
from ..catalog.validation import KEY
from ..inventory.guard import lock_stock_writes
from ..inventory.operations import persistence as stock, service as kernel
from ..inventory.operations.dto import Command, Line, Result, StockError
from ..inventory.operations.transaction import connection
from . import CHANGE, EVENT, SETTINGS, SUBMISSION
from . import persistence as store
from . import policy as rules


@dataclass(frozen=True)
class Decision:
    company: str
    document_id: str
    submission_id: str
    expected_revision: int
    idempotency_key: str
    reason: str = ""


@dataclass(frozen=True)
class Settings:
    inventory_approval_enabled: bool
    revision: int


def get_settings():
    with connection(read_committed=True):
        lock_stock_writes()
        policy.require_operation(policy.Operation.SYSTEM_MAINTAIN, locked=True)
        current = store.settings()
        return Settings(bool(current.inventory_approval_enabled), current.revision)


def configuration_history():
    with connection(read_committed=True):
        lock_stock_writes()
        policy.require_operation(policy.Operation.SYSTEM_MAINTAIN, locked=True)
        return tuple(frappe.db.get_values(CHANGE, {}, (
            "revision", "previous_value", "new_value", "actor", "creation", "reason",
        ), as_dict=True, for_update=True, order_by="revision asc"))


def _revision(value):
    if type(value) is not int or not 1 <= value < 2_147_483_647:
        raise StockError("STOCK_INVALID_REVISION")


def _reason(value, required):
    kernel._text(value, 4000, required=required)
    if required and not value.strip():
        raise StockError("STOCK_INVALID_TEXT")


def configure(enabled, expected_revision, reason, *, _before_guard=None):
    """Change the site-wide setting with compare-and-set and an immutable audit row."""
    if type(enabled) is not bool:
        raise StockError("APPROVAL_INVALID_SETTING")
    _revision(expected_revision)
    _reason(reason, True)
    with connection(read_committed=True) as database:
        if _before_guard:
            _before_guard(database)
        lock_stock_writes()
        policy.require_operation(policy.Operation.SYSTEM_MAINTAIN, locked=True)
        actor = policy.active_actor(locked=True)
        current = store.settings()
        if current.revision != expected_revision:
            raise StockError("STOCK_REVISION_CONFLICT")
        if current.inventory_approval_enabled == enabled:
            return Settings(enabled, current.revision)
        revision = current.revision + 1
        frappe.db.sql(
            f"""UPDATE `tab{SETTINGS}` SET revision=%s, inventory_approval_enabled=%s,
                modified_by=%s, modified=%s WHERE name=%s AND revision=%s""",
            (revision, int(enabled), actor, now_datetime(), store.SETTINGS_ID, expected_revision),
        )
        stock._assert_updated("STOCK_REVISION_CONFLICT")
        stock.insert(CHANGE, name=str(revision), revision=revision, previous_value=current.inventory_approval_enabled,
                     new_value=int(enabled), actor=actor, reason=reason)
        kernel._commit(database)
        return Settings(enabled, revision)


def submit_identity(command, actor, action):
    return kernel._digest((command.company, actor, "Submit", action.name, command.idempotency_key))


def _command_payload(command):
    payload = asdict(command)
    del payload["idempotency_key"]
    return payload


def _data(submission):
    if not submission:
        raise StockError("APPROVAL_SUBMISSION_INCONSISTENT")
    payload = json.loads(submission.payload)
    command = payload["command"]
    if (kernel._digest(payload) != submission.payload_digest
            or (command["company"], command["document_id"], command["expected_revision"])
            != (submission.company, submission.document_id, submission.business_revision)):
        raise StockError("APPROVAL_SUBMISSION_INCONSISTENT")
    return payload


def authorize_snapshot(submission):
    return policy.lock_execution_locations(submission.company, _data(submission)["locations"])


def _set_state(document, result, submission_id):
    frappe.db.sql(
        f"""UPDATE `tab{stock.DOCUMENT}` SET status=%s, revision=%s, approval_submission=%s,
            modified_by=%s, modified=%s WHERE name=%s AND revision=%s AND status=%s AND effective_operation IS NULL""",
        (result.status, result.revision, submission_id, frappe.session.user, now_datetime(),
         document.name, document.revision, document.status),
    )
    stock._assert_updated("STOCK_DOCUMENT_CHANGED")


def stage_submission(command, action, document, stored_lines, locations, actor, identity, digest):
    config = store.settings()
    kind = document.document_type
    if config.inventory_approval_enabled or kind == rules.TRANSFER:
        kernel._resources(command, action)
        kernel._validate_payload(command, document, stored_lines, action)
    count = None
    if kind == rules.COUNT:
        from ..inventory.stock_count import service as counts

        if config.inventory_approval_enabled:
            action.prepare_plan(command, document, stored_lines, locations)
        count = counts._approval_snapshot(document.name)
    scope = sorted({*locations, *([count["location_id"]] if count else [])})
    snapshot = {"command": _command_payload(command), "locations": scope, "count": count,
                "document_notes": document.notes, "document_reason": document.reason}
    submission_id = stock.insert(
        SUBMISSION, company=document.company, document_id=document.name, document_type=kind,
        business_revision=document.revision, creator=document.owner, submitter=actor,
        configuration_revision=config.revision, approval_required=config.inventory_approval_enabled,
        payload=store.encode(snapshot), payload_digest=kernel._digest(snapshot),
    )
    submission = store.submission(submission_id)
    store.event(submission, "Resubmit" if document.approval_submission else "Submit", document.status,
                Result(document.name, None, document.revision, document.status, submission_id), actor, identity,
                reason=command.reason)
    if config.inventory_approval_enabled or kind == rules.TRANSFER:
        status = rules.PENDING if config.inventory_approval_enabled else rules.READY
        result = Result(document.name, None, document.revision + 1, status, submission_id)
        _set_state(document, result, submission_id)
        event = "Await Approval" if config.inventory_approval_enabled else "Approval Exempt"
        store.event(submission, event, document.status, result, actor, identity,
                    reason="" if config.inventory_approval_enabled else "Global approval disabled at submission.")
        store.save_request(identity, document.company, actor, document.name, submission_id, "Submit", digest, result)
        return submission, result

    # Direct posting advances the business revision with the original stock request.
    frappe.db.sql(f"UPDATE `tab{stock.DOCUMENT}` SET approval_submission=%s WHERE name=%s",
                  (submission_id, document.name))
    stock._assert_updated("STOCK_DOCUMENT_CHANGED")
    return submission, None


def record_direct(submission, document, result, actor, identity):
    store.event(submission, "Approval Exempt", document.status, result, actor, identity,
                reason="Global approval disabled at submission.", native_actor=actor if _has_native_effect(result) else None)


def _has_native_effect(result):
    return stock.current(stock.LOG, result.operation_id, ("quantity_effect",)).quantity_effect == "Native"


def submit_transfer(command, action, *, before_guard=None, commit=None):
    command = kernel._canonical(kernel._shape(command, action))
    return kernel._run(partial(kernel._stage, command=command, action=action, transfer_submission=True),
                       read_committed=True, before_guard=before_guard, commit=commit)


def _belongs(submission, document):
    if not submission or (submission.company, submission.document_id, submission.document_type) != (
        document.company, document.name, document.document_type,
    ):
        policy.deny()


def dispatch_revision(document, *, locked=True):
    if not document.approval_submission:
        return 1  # V1-10 through V1-12 direct dispatch.
    submission = store.submission(document.approval_submission, locked=locked)
    _belongs(submission, document)
    event = "Approve" if submission.approval_required else "Approval Exempt"
    status = rules.APPROVED if submission.approval_required else rules.READY
    revisions = frappe.db.get_values(EVENT, {
        "submission_id": submission.name, "company": document.company, "document_id": document.name,
        "action": event, "result": "Succeeded", "resulting_status": status,
    }, "business_revision", pluck=True, for_update=locked)
    if revisions != [submission.business_revision + (2 if submission.approval_required else 1)]:
        raise StockError("APPROVAL_STATE_CONFLICT")
    return revisions[0]


def lock_dispatch_submission(document, command):
    submission = store.submission(document.approval_submission)
    _belongs(submission, document)
    if (bool(submission.approval_required) != (document.status == rules.APPROVED)
            or dispatch_revision(document) != document.revision):
        raise StockError("APPROVAL_STATE_CONFLICT")
    original = replace(command, expected_revision=submission.business_revision)
    if store.encode(_data(submission)["command"]) != store.encode(_command_payload(original)):
        raise StockError("STOCK_DOCUMENT_PAYLOAD_MISMATCH")
    return submission


def _lock_document(company, document_id, *, specialist=False):
    document = stock.current(stock.DOCUMENT, document_id, (
        "name", "company", "owner", "document_type", "revision", "status", "effective_operation",
        "approval_submission", "reason", "notes",
    ))
    if (not document or document.name != document_id or document.company != company
            or document.document_type not in ({rules.CORRECTION} if specialist else rules.PRIMARY)):
        policy.deny()
    return document


def _operation(kind):
    return {
        rules.RECEIPT: policy.Operation.RECEIVE, rules.ISSUE: policy.Operation.ISSUE,
        rules.TRANSFER: policy.Operation.TRANSFER, rules.COUNT: policy.Operation.COUNT,
    }[kind]


@contextmanager
def _posting_principal(submission):
    """Run the exact locked submission with the original operator's current permissions."""
    actor = submission.submitter
    # Only the scoped Bench candidate path can enter here. Neither the approver
    # nor a caller-supplied user can be substituted for the original operator.
    if (not isinstance(actor, str) or not actor.endswith("@example.invalid")
            or getattr(frappe.local, "request", None) is not None):
        policy.deny()
    if frappe.session.user == actor:
        yield
        return
    local = frappe.local
    attributes = ("session", "cache", "form_dict", "jenv_restricted", "jenv_unrestricted",
                  "role_permissions", "new_doc_templates", "user_perms", "request_cache")
    saved = {key: getattr(local, key) for key in attributes if hasattr(local, key)}
    try:
        local.session = frappe._dict(local.session)
        frappe.set_user(actor)
        local.request_cache = defaultdict(dict)
        yield
    finally:
        for key in attributes:
            if key in saved:
                setattr(local, key, saved[key])
            elif hasattr(local, key):
                delattr(local, key)


def _action(kind, company):
    from ..inventory.issue import service as issues
    from ..inventory.receipt import service as receipts
    from ..inventory.stock_count import service as counts
    from ..inventory.transfer import service as transfers

    module = {rules.RECEIPT: receipts, rules.ISSUE: issues, rules.COUNT: counts, rules.TRANSFER: transfers}[kind]
    return module._action(module._authorize(company))


def _check_current(payload, submission, document, allowed):
    expected = submission.business_revision + (2 if allowed == rules.APPROVED else 1)
    if (
        document.approval_submission != submission.name or document.status != allowed
        or document.effective_operation is not None or document.revision != expected
    ):
        raise StockError("APPROVAL_STATE_CONFLICT")
    if document.revision != payload.expected_revision:
        raise StockError("STOCK_REVISION_CONFLICT")
    if not submission.approval_required:
        raise StockError("APPROVAL_STATE_CONFLICT")


def _authorize_decision(verb, actor, document, submission):
    if verb == "Withdraw":
        if actor != submission.creator or actor != document.owner:
            policy.deny()
        policy.require_operation(_operation(document.document_type), locked=True)
    else:
        policy.require_operation(policy.Operation.APPROVE, locked=True)
    authorize_snapshot(submission)


def _approve(database, submission, document, actor, identity):
    data = _data(submission)
    command_data = data["command"]
    command = Command(**{
        **command_data, "expected_revision": document.revision, "idempotency_key": identity,
        "lines": tuple(Line(**line) for line in command_data["lines"]),
    })
    with _posting_principal(submission):
        policy.require_operation(_operation(document.document_type), locked=True)
        authorize_snapshot(submission)
        action = _action(document.document_type, document.company)
        command = kernel._canonical(kernel._shape(command, action))
        loaded, lines = (action.load_document or kernel._lock_document)(document.company, document.name,
                                                                        document.document_type)
        locations = kernel._authorize(command.company, lines, action)
        kernel._validate_payload(command, loaded, lines, action)
        if (data["document_notes"], data["document_reason"]) != (document.notes, document.reason):
            raise StockError("STOCK_DOCUMENT_PAYLOAD_MISMATCH")
        if document.document_type == rules.COUNT:
            from ..inventory.stock_count import service as counts

            if data["count"] != counts._approval_snapshot(document.name):
                raise StockError("COUNT_SNAPSHOT_MISMATCH")
        if document.document_type == rules.TRANSFER:
            kernel._resources(command, action)
            return Result(document.name, None, document.revision + 1, rules.APPROVED, submission.name), None
        # Namespace the internal inventory request separately from any user
        # key. The approved payload and actor are bound to the decision above.
        request_id = kernel._digest(("Approval Inventory Effect", identity))
        digest = kernel.payload_digest(command, actor, action.name)
        kernel._reserve_request(command, action, actor, request_id, request_id, digest)
        applied = kernel._apply(database, command, loaded, lines, locations, action)
    native_actor = submission.submitter if applied[1] else None
    result = kernel._record(document, action, applied, request_id, actor, native_actor, submission.name)
    return result, native_actor


TRANSITIONS = {
    "Approve": (rules.PENDING, rules.APPROVED),
    "Reject": (rules.PENDING, rules.REJECTED),
    "Withdraw": (rules.PENDING, "Draft"),
    "Revoke Approval": (rules.APPROVED, "Draft"),
}

SAFE_FAILURES = frozenset((
    "INSUFFICIENT_STOCK", "COUNT_GENERATION_CHANGED", "COUNT_SCOPE_CHANGED", "COUNT_BUCKET_CHANGED",
    "COUNT_ACTUAL_INCOMPLETE", "COUNT_SNAPSHOT_MISMATCH", "COUNT_SNAPSHOT_UNTRUSTED",
    "COUNT_NATIVE_PRECISION_UNCONFIGURED", "BATCH_MAPPING_UNAVAILABLE", "STOCK_DOCUMENT_PAYLOAD_MISMATCH",
    "STOCK_QUANTITY_PRECISION", "STOCK_QUANTITY_LIMIT", "STOCK_UOM_MISMATCH",
    "RECEIPT_NATIVE_PRECISION_UNCONFIGURED", "ISSUE_NATIVE_PRECISION_UNCONFIGURED",
    "TRANSFER_NATIVE_PRECISION_UNCONFIGURED",
    "STOCK_ADAPTER_SIDE_EFFECT", "STOCK_PERSISTENCE_FAILED",
))


class _RolledBackApproval(Exception):
    def __init__(self, code):
        self.code = code


def _decision_stage(database, payload, verb, *, recovery, failed=None, specialist=False):
    lock_stock_writes(payload.company)
    actor = policy.active_actor(locked=True)
    if verb != "Withdraw":
        policy.require_operation(
            policy.Operation.CORRECTION_APPROVE if specialist else policy.Operation.APPROVE, locked=True,
        )
    action_key = f"Correction {verb}" if specialist else verb
    identity = kernel._digest((payload.company, actor, action_key, payload.idempotency_key))
    digest = kernel._digest(asdict(payload))
    request = store.request(identity)
    document = _lock_document(payload.company, request.document_id if request else payload.document_id,
                              specialist=specialist)
    submission = store.submission(request.submission_id if request else payload.submission_id)
    _belongs(submission, document)
    if specialist:
        from ..inventory.corrections import service as corrections

        corrections._authorize_decision(verb, actor, document, submission)
    else:
        _authorize_decision(verb, actor, document, submission)
    if request:
        return store.replay(request, digest), False
    if recovery:
        raise StockError("STOCK_RETRY_REQUIRED")

    before, after = TRANSITIONS[verb]
    if verb == "Revoke Approval" and document.document_type != rules.TRANSFER:
        raise StockError("APPROVAL_STATE_CONFLICT")
    _check_current(payload, submission, document, before)
    if failed:
        # This transaction follows the closed, rolled-back quantity attempt.
        # Recheck the original operator as well as the decision maker.
        if specialist:
            review, _ = corrections._approved_effect(database, submission, document, actor, identity,
                                                      failed=failed, reason=payload.reason)
            if review:
                _set_state(document, review, submission.name)
                store.event(submission, "Manual Review", before, review, actor, identity,
                            reason=payload.reason, failure_code=failed)
                store.save_request(identity, document.company, actor, document.name, submission.name, verb, digest, review)
                return review, True
        else:
            with _posting_principal(submission):
                policy.require_operation(_operation(document.document_type), locked=True)
                authorize_snapshot(submission)
        result = Result(document.name, None, document.revision, document.status, submission.name)
        store.event(submission, "Approval Failed", before, result, actor, identity, reason=payload.reason,
                    failure_code=failed)
        store.save_request(identity, document.company, actor, document.name, submission.name, verb, digest,
                           failure_code=failed)
        return result, True

    try:
        native_actor = None
        if verb == "Approve":
            if specialist:
                result, native_actor = corrections._approved_effect(database, submission, document, actor, identity,
                                                                      reason=payload.reason)
            else:
                result, native_actor = _approve(database, submission, document, actor, identity)
        else:
            result = Result(document.name, None, document.revision + 1, after, submission.name)
        if result.operation_id is None:
            _set_state(document, result, submission.name)
        event = "Manual Review" if specialist and result.status == corrections.MANUAL else verb
        store.event(submission, event, before, result, actor, identity, reason=payload.reason,
                    native_actor=native_actor)
        store.save_request(identity, document.company, actor, document.name, submission.name, verb, digest, result)
        return result, True
    except (frappe.PermissionError, frappe.QueryDeadlockError, frappe.QueryTimeoutError):
        raise
    except Exception as error:
        if isinstance(error, StockError) and error.code == "STOCK_RETRY_REQUIRED":
            raise
        if verb != "Approve":
            raise
        code = getattr(error, "code", None)
        safe = SAFE_FAILURES | corrections.REVIEW_FAILURES if specialist else SAFE_FAILURES
        raise _RolledBackApproval(code if code in safe else "APPROVAL_EXECUTION_FAILED") from None


def _decide(payload, verb, *, before_guard=None, commit=None, specialist=False):
    kernel._text(payload.company, 140, required=True)
    kernel._id(payload.document_id)
    kernel._id(payload.submission_id)
    _revision(payload.expected_revision)
    _reason(payload.reason, verb in ("Reject", "Revoke Approval"))
    if not isinstance(payload.idempotency_key, str) or not KEY.fullmatch(payload.idempotency_key):
        raise StockError("STOCK_INVALID_REQUEST_KEY")
    stage = partial(_decision_stage, payload=payload, verb=verb, specialist=specialist)
    try:
        return kernel._run(stage, read_committed=True, before_guard=before_guard, commit=commit,
                           passthrough=(_RolledBackApproval,))
    except _RolledBackApproval as failure:
        try:
            return _persist_failure(stage, failure.code)
        except StockError as error:
            if error.code != "APPROVAL_STATE_CONFLICT":
                raise
        raise StockError(failure.code) from None


def _persist_failure(stage, code):
    result = kernel._run(partial(stage, failed=code), read_committed=True)
    if result.status == rules.PENDING:
        raise StockError(code)
    return result


def approve(payload):
    return _decide(payload, "Approve")


def reject(payload):
    return _decide(payload, "Reject")


def withdraw(payload):
    return _decide(payload, "Withdraw")


def revoke_approval(payload):
    return _decide(payload, "Revoke Approval")


def history(company, document_id):
    """Return immutable submissions and approval events after current scope checks."""
    kernel._id(document_id)
    with connection(read_committed=True):
        lock_stock_writes(company)
        document = _lock_document(company, document_id)
        actor = policy.active_actor(locked=True)
        operations = policy.granted_operations(locked=True)
        can_approve = policy.Operation.APPROVE in operations
        if not can_approve and _operation(document.document_type) not in operations:
            policy.deny()
        ids = frappe.db.get_values(SUBMISSION, {"document_id": document.name}, "name",
                                   for_update=True, pluck=True, order_by="business_revision asc, name asc")
        if not ids:
            policy.deny()
        submissions = []
        for identity in ids:
            submission = store.submission(identity)
            _belongs(submission, document)
            authorize_snapshot(submission)
            submissions.append({**submission, "payload": _data(submission)})
        if not can_approve and actor != document.owner and all(row["submitter"] != actor for row in submissions):
            policy.deny()
        events = frappe.db.get_values(
            EVENT, {"document_id": document.name, "company": company}, (
                "name", "submission_id", "action", "previous_status", "resulting_status", "actor", "creation",
                "reason", "business_revision", "request_id", "result", "failure_code", "operation_id", "native_actor",
            ), as_dict=True, for_update=True, order_by="creation asc, name asc",
        )
        return {"document_id": document.name, "status": document.status, "revision": document.revision,
                "submissions": submissions, "events": events}
