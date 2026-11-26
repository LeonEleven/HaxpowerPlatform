"""Protected approval records and non-quantity request results."""

from dataclasses import asdict
import json

import frappe

from . import EVENT, REQUEST, SETTINGS, SUBMISSION
from ..inventory.guard import lock_stock_writes
from ..inventory.operations import persistence as stock
from ..inventory.operations.dto import Result, StockError

SETTINGS_ID = "inventory"

SUBMISSION_FIELDS = (
    "name", "company", "document_id", "document_type", "business_revision", "creator",
    "submitter", "creation", "configuration_revision", "approval_required", "payload",
    "payload_digest",
)


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def settings():
    row = stock.current(SETTINGS, SETTINGS_ID, ("revision", "inventory_approval_enabled"))
    if not row or row.revision < 1 or row.inventory_approval_enabled not in (0, 1):
        raise StockError("APPROVAL_CONFIGURATION_UNAVAILABLE")
    return row


def ensure_schema():
    lock_stock_writes()
    if not stock.current(SETTINGS, SETTINGS_ID, ("name",)):
        stock.insert(SETTINGS, name=SETTINGS_ID, revision=1, inventory_approval_enabled=0)
    settings()


def request(identity):
    return stock.current(REQUEST, identity, ("company", "actor", "document_id", "submission_id",
                                              "action", "payload_digest", "response", "failure_code"))


def save_request(identity, company, actor, document_id, submission_id, action, digest,
                 result=None, failure_code=None):
    stock.insert(REQUEST, name=identity, company=company, actor=actor, document_id=document_id,
                 submission_id=submission_id, action=action, payload_digest=digest,
                 response=encode(asdict(result)) if result is not None else None,
                 failure_code=failure_code)


def replay(row, digest):
    if row.payload_digest != digest:
        raise StockError("STOCK_IDEMPOTENCY_CONFLICT")
    if row.failure_code:
        raise StockError(row.failure_code)
    return Result(**json.loads(row.response))


def submission(identity):
    return stock.current(SUBMISSION, identity, SUBMISSION_FIELDS) if identity else None


def event(submission, action, before, result, actor, identity, *, reason="", failure_code=None,
          native_actor=None):
    stock.insert(EVENT, submission_id=submission.name, document_id=submission.document_id,
                 company=submission.company, action=action, previous_status=before,
                 resulting_status=result.status, actor=actor, business_revision=result.revision,
                 request_id=identity, reason=reason, result="Failed" if failure_code else "Succeeded",
                 failure_code=failure_code, operation_id=result.operation_id, native_actor=native_actor)
