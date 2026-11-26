"""Business and specialist views of documents with complete warehouse checks."""

from collections import defaultdict
from decimal import Decimal
import json

import frappe

from ...access import policy
from ...approval import EVENT, policy as rules
from ...catalog.read import rows
from .. import versions
from ..corrections import CASE, REVIEW
from ..corrections.facts import line_origins
from ..operations import persistence as stock, trace
from ..operations.dto import StockError
from .facts import INVALID, quantity

HEADER = "name document_number document_type status revision creation effective_operation owner"
LINE = (
    "name document_id line_no sku_id source target batch_kind batch_id batch_no quantity uom"
)
COUNT_LINE = (
    "name task_id line_no sku_id location_id batch_kind batch_id batch_no "
    "snapshot_quantity actual_quantity difference_quantity uom"
)


class Documents:
    def __init__(self, service):
        self.service = service
        self.scope = service.scope
        # Headers are company-bound before private document relationships are resolved.
        if frappe.db.count(stock.DOCUMENT, {"company": self.scope.company}) > 100_000:
            raise StockError("INVENTORY_READ_HISTORY_LIMIT")
        self.headers = {row.name: row for row in rows(stock.DOCUMENT, HEADER, {"company": self.scope.company})}
        self.lines = defaultdict(list)
        self.counts = defaultdict(list)
        self.tasks = {row.name: row.location_id for row in rows(
            versions.TASK, "name location_id", {"company": self.scope.company},
        )}
        if self.headers:
            for row in rows(stock.LINE, LINE, {"document_id": ("in", tuple(self.headers))}, order_by="line_no asc, name asc"):
                self.lines[row.document_id].append(row)
            for row in rows(versions.LINE, COUNT_LINE, {"task_id": ("in", tuple(self.headers))},
                            order_by="line_no asc, name asc"):
                self.counts[row.task_id].append(row)
        self.cases = {}
        ids = tuple(row.name for row in self.headers.values() if row.document_type == rules.CORRECTION)
        if ids:
            self.cases = {row.name: row for row in rows(
                CASE, "name original_operation line_origins replacement_document", {"name": ("in", ids)},
            )}
        self.operations = {row.effective_operation: row.name for row in self.headers.values() if row.effective_operation}
        self.actor = policy.active_actor()
        self.capabilities = policy.granted_operations()

    def locations(self, header):
        if header.document_type == rules.COUNT:
            return {self.tasks.get(header.name), *(row.location_id for row in self.counts[header.name])}
        return {location for row in self.lines[header.name] for location in (row.source, row.target) if location}

    def allowed(self, header):
        if not header:
            return False
        locations = self.locations(header)
        if not locations or not locations <= self.scope.locations.keys():
            return False
        if header.document_type == rules.CORRECTION:
            case = self.cases.get(header.name)
            original = self.headers.get(self.operations.get(case.original_operation)) if case else None
            if (not original or original.document_type == rules.CORRECTION
                    or not self.locations(original) or not self.locations(original) <= self.scope.locations.keys()):
                return False
        if header.effective_operation or header.owner == self.actor:
            return True
        return self.audit_capability(header) in self.capabilities

    @staticmethod
    def audit_capability(header):
        return (policy.Operation.CORRECTION_APPROVE if header.document_type == rules.CORRECTION
                else policy.Operation.APPROVE)

    def header(self, identity):
        row = self.headers.get(policy._record_id(identity))
        if not self.allowed(row):
            policy.deny()
        return row

    @staticmethod
    def summary(row):
        return dict(id=row.name, number=row.document_number, type=row.document_type, status=row.status,
                    created_at=row.creation, effective_operation_id=row.effective_operation)

    def _project_line(self, row, *, count=False):
        result = dict(line_id=row.name, number=row.line_no, sku_id=row.sku_id,
                      batch=row.batch_no or self.service.facts.batch(row.batch_id),
                      batch_id=row.batch_id or None, uom=row.uom)
        if count:
            result.update(location_id=row.location_id, book_quantity=row.snapshot_quantity,
                          counted_quantity=row.actual_quantity, difference=row.difference_quantity)
        else:
            result.update(quantity=row.quantity, source_location_id=row.source, target_location_id=row.target)
        return result

    def detail(self, identity):
        header = self.header(identity)
        if header.effective_operation:
            _ = self.service.facts.movements
        count = header.document_type == rules.COUNT
        lines = self.counts[identity] if count else self.lines[identity]
        projected = [self._project_line(row, count=count) for row in lines]
        result = dict(self.summary(header), lines=projected, operations=trace.business_operations(identity, locked=False))
        if header.document_type == rules.TRANSFER and header.effective_operation:
            result["transfers"] = [row for row in self.service._transit() if row["document_id"] == identity]
        elif header.document_type == rules.CORRECTION:
            case = self.cases[identity]
            mapping = line_origins(case)
            result["original_document_id"] = self.operations[case.original_operation]
            for row in projected:
                row["original_line_id"] = mapping[row["line_id"]]
            replacement = self.headers.get(case.replacement_document)
            if self.allowed(replacement):
                result["replacement"] = self.summary(replacement)
        return result

    def history(self, identity, *, audit=False):
        header = self.header(identity)
        result = self.detail(identity)
        corrections = defaultdict(list)
        for correction_id, case in self.cases.items():
            correction = self.headers[correction_id]
            if (case.original_operation == header.effective_operation and correction.effective_operation
                    and self.allowed(correction)):
                for row in self.detail(correction_id)["lines"]:
                    corrections[row["original_line_id"]].append(dict(
                        document_id=correction_id, line_id=row["line_id"], quantity=row["quantity"],
                        net_quantity=quantity(Decimal(row["quantity"]) * (-1 if row["source_location_id"] else 1)),
                    ))
        for row in result["lines"]:
            row["corrections"] = corrections[row["line_id"]]
            base = row.get("difference") if header.document_type == rules.COUNT else row.get("quantity")
            used = sum((Decimal(item["quantity"]) for item in row["corrections"]), Decimal(0))
            if (header.effective_operation and base is not None
                    and header.document_type in (rules.RECEIPT, rules.ISSUE, rules.COUNT)):
                available = abs(Decimal(base)) - used
                if available < 0:
                    raise StockError(INVALID)
                row["remaining_correction_quantity"] = quantity(available)
        if audit:
            if self.audit_capability(header) not in self.capabilities:
                policy.deny()
            events = rows(EVENT, "name action result previous_status resulting_status actor reason failure_code creation",
                          {"company": self.scope.company, "document_id": identity}, order_by="creation asc, name asc")
            result["approval_events"] = [dict(row) for row in events]
            if header.document_type == rules.CORRECTION:
                result["review_events"] = self._reviews(identity)
        return result

    def _reviews(self, identity):
        output = []
        for row in rows(REVIEW, "name actor action creation evidence reason resolution_document",
                        {"correction_id": identity}, order_by="creation asc, name asc"):
            resolution = self.headers.get(row.resolution_document)
            if row.resolution_document and not self.allowed(resolution):
                continue
            evidence = []
            for item in json.loads(row.evidence):
                # Evidence about a different operation is visible only after
                # checking that operation's own complete document range.
                related = item.get("operation_id")
                document = self.operations.get(related)
                if related and not self.allowed(self.headers.get(document)):
                    continue
                evidence.append(dict(code=item["code"], document_id=document, line_id=item.get("line_id")))
            output.append(dict(id=row.name, actor=row.actor, action=row.action, created_at=row.creation,
                               reason=row.reason, resolution_document_id=row.resolution_document, evidence=evidence))
        return output


def list_documents(service, *, kind=None):
    documents = Documents(service)
    query = service.query
    permitted = [header for header in documents.headers.values() if documents.allowed(header)]
    ids = set(query.product_ids(candidates={
        row.sku_id for header in permitted for row in (*documents.lines[header.name], *documents.counts[header.name])
    }, active=False))
    result = []
    for header in sorted(permitted, key=lambda row: (row.creation, row.name), reverse=True):
        if kind and header.document_type != kind:
            continue
        if ((query.number and query.number != header.document_number)
                or (query.action and query.action != header.document_type)
                or (query.start and header.creation < query.start) or (query.end and header.creation >= query.end)
                or not any(query.location_matches(loc) for loc in documents.locations(header))):
            continue
        lines = documents.counts[header.name] if header.document_type == rules.COUNT else documents.lines[header.name]
        if any(row.sku_id in ids and query.batch_matches(row.batch_no or service.facts.batch(row.batch_id))
               and any(query.location_matches(loc) for loc in (
                   (row.location_id,) if header.document_type == rules.COUNT else (row.source, row.target)
               )) for row in lines):
            involved = documents.locations(header)
            if header.document_type == rules.CORRECTION:
                case = documents.cases[header.name]
                involved |= documents.locations(documents.headers[documents.operations[case.original_operation]])
            service.included_warehouses.update(documents.scope.locations[loc]["warehouse_id"] for loc in involved)
            result.append(documents.summary(header))
    return result
