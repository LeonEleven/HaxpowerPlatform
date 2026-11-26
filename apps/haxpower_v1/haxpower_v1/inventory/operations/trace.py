"""Warehouse-authorized operation details for correction and reporting consumers."""

from dataclasses import asdict

import frappe

from ...access import policy
from ...approval import policy as rules
from .. import versions
from ..corrections import CASE, facts
from ..corrections.service import _assess, _public_evidence, _require_read
from ..guard import lock_stock_writes
from . import persistence as stock, service as kernel
from .dto import StockError
from .transaction import connection


def _operations(doc):
    return frappe.db.get_values(stock.LOG, {"document_id": doc.name, "status": "Effective"},
        ("name", "action", "creation", "result_revision", "result_status", "actor", "native_actor", "original_operation"),
        as_dict=True, for_update=True, order_by="result_revision asc")


def by_document(company, document_id):
    kernel._id(document_id)
    with connection(read_committed=True):
        lock_stock_writes(company)
        document = facts.document(company, document_id)
        _authorize(company, document)
        return _operations(document)


def _authorize(company, document):
    if document.document_type == rules.CORRECTION:
        case = stock.current(CASE, document.name, ("original_operation",))
        if not case:
            raise StockError(facts.INVALID)
        _, original, _ = facts.origin(company, case.original_operation, capability=False)
        _require_read(company, original)
    else:
        _require_read(company, document)


def detail(company, operation_id):
    kernel._id(operation_id)
    with connection(read_committed=True):
        lock_stock_writes(company)
        log = stock.current(stock.LOG, operation_id, ("document_id", "name"))
        if not log or log.name != operation_id:
            policy.deny()
        document = facts.document(company, log.document_id)
        _authorize(company, document)
        operation = next((row for row in _operations(document) if row.name == operation_id), None)
        if not operation:
            raise StockError(facts.INVALID)
        lines = facts.original_lines(company, document)
        versions_before, entries = versions.verify(include_entries=True)
        allocations = facts._allocations(company, entries)
        references = frappe.db.get_values(stock.LINK, {"operation_id": operation_id},
            ("name", "line_id", "role", "parent_type", "parent_id", "detail_id"),
            as_dict=True, for_update=True)
        refs = {(row.line_id, row.parent_type, row.parent_id, row.detail_id): row.name for row in references}
        effects = {}
        for bucket, rows in allocations.items():
            for link, _, quantity in rows:
                if link.operation_id != operation_id:
                    continue
                key = (link.line_id, link.parent_type, link.parent_id, link.detail_id)
                effects.setdefault(link.line_id, []).append({
                    "reference_id": refs[key],
                    "posting_id": kernel._digest((link.parent_type, link.parent_id)),
                    "role": link.role, "bucket": asdict(bucket), "quantity": str(quantity),
                })
        entitlements = {}
        related_lines = {}
        original_document = None
        correction_id = None
        if document.document_type in (rules.RECEIPT, rules.ISSUE, rules.COUNT):
            _, evidence, _ = _assess(company, operation, document, lines, native=(versions_before, allocations))
            entitlements = _public_evidence(evidence)
        if document.document_type == rules.CORRECTION:
            case = stock.current(CASE, document.name, ("original_operation", "line_origins"))
            related_lines = facts.line_origins(case)
            _, original, _ = facts.origin(company, case.original_operation, capability=False)
            original_document = original.name
        elif candidate := frappe.db.get_value(CASE, {"replacement_document": document.name}, "name", for_update=True):
            try:
                _authorize(company, facts.document(company, candidate))
                correction_id = candidate
            except frappe.PermissionError:
                pass
        return {"document_id": document.name, "type": document.document_type, "operation": operation,
                "original_document_id": original_document, "replaces_correction_id": correction_id,
                "lines": [{**asdict(line), "effects": effects.get(line.line_id, []),
                           "original_line_id": related_lines.get(line.line_id),
                           "entitlement": entitlements.get(line.line_id)} for line in lines]}
