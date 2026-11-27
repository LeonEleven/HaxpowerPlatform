"""Consistent, reconciled native quantity facts for read services."""

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from functools import cached_property

import frappe

from ...adapters.erpnext import stock_entry
from ...approval import policy as rules
from ...catalog.read import rows
from ...catalog.sku_persistence import SKU
from ...warehouse.persistence import LOCATION, TRANSIT, WAREHOUSE
from .. import versions
from ..batch.persistence import MAPPING
from ..corrections import CASE, facts as correction
from ..operations import persistence as stock
from ..operations.dto import StockError
from ..operations.service import _lock_document
from ..transfer import transit

INVALID = "INVENTORY_READ_INCONSISTENT"


def quantity(value):
    return format(value, "f") if value else "0"


@dataclass(frozen=True)
class Movement:
    bucket: object
    operation: str
    line: str
    document: str
    action: str
    date: object
    quantity: Decimal


class Facts:
    def __init__(self, company):
        self.company = company
        _, entries = versions.verify(include_entries=True, locked=False, company=company)
        self.balances = {bucket: sum((Decimal(entry[-1]) for entry in allocations), Decimal(0))
                         for bucket, allocations in entries.items() if bucket.company == company}
        self.batches = {row.name: row.original_text for row in rows(
            MAPPING, "name original_text", {"kind": "BATCH"},
        )}
        self.entries = entries

    def batch(self, batch_id):
        if batch_id is None:
            return None
        if batch_id not in self.batches:
            raise StockError(INVALID)
        return self.batches[batch_id]

    transit_location = None

    @cached_property
    def transfers(self):
        transfer_leaves = {row.name for row in rows(LOCATION, "name", {
            "warehouse": ("in", frappe.db.get_values(
                WAREHOUSE, {"company": self.company, "kind": TRANSIT}, "name", pluck=True,
            )),
        })} if frappe.db.exists(WAREHOUSE, {"company": self.company, "kind": TRANSIT}) else set()
        transit_buckets = {bucket: qty for bucket, qty in self.balances.items() if bucket.location_id in transfer_leaves}
        if not transit_buckets:
            if frappe.db.exists(stock.DOCUMENT, {
                "company": self.company, "document_type": rules.TRANSFER,
                "status": ("in", transit.DISPATCHED_STATUSES),
            }):
                raise StockError(INVALID)
            return []
        leaf = transit.resolve(self.company, locked=False)
        self.transit_location = leaf.name
        ids = {bucket.sku_id for bucket in transit_buckets}
        skus = {row.name: row.native_item for row in rows(SKU, "name native_item", {"name": ("in", tuple(ids))})}
        buckets = {(skus[b.sku_id], leaf.native_warehouse, self.entries[b][0][-2]): qty
                   for b, qty in transit_buckets.items()}
        lifecycle = transit.reconcile(self.company, set(skus.values()), leaf, buckets, locked=False)
        result = []
        documents = {row.document_id for row in lifecycle.originals.values()}
        expected = set(frappe.db.get_values(stock.DOCUMENT, {
            "company": self.company, "document_type": rules.TRANSFER, "status": ("in", transit.DISPATCHED_STATUSES),
        }, "name", pluck=True))
        if documents != expected:
            raise StockError(INVALID)
        numbers = dict(frappe.db.get_values(stock.DOCUMENT, {"name": ("in", tuple(documents))},
                                            ("name", "document_number")))
        for identity in sorted(documents):
            doc, lines = _lock_document(self.company, identity, rules.TRANSFER, locked=False)
            log, owned = transit.verify_document(doc, lines, lifecycle, locked=False)
            for line in lines:
                original = owned[line.line_id]
                remain = lifecycle.remaining(original)
                received, returned = lifecycle.received[line.line_id], lifecycle.returned[line.line_id]
                status = transit.status_for({line.line_id: remain}, {line.line_id: received}, {line.line_id: returned})
                result.append(dict(
                    document_id=identity, document_number=numbers[identity], operation_id=log.name, line_id=line.line_id,
                    sku_id=line.sku_id, batch=self.batch(line.batch_id), batch_id=line.batch_id,
                    source_location_id=line.source, target_location_id=line.target, uom=line.uom,
                    dispatched=quantity(Decimal(line.quantity)), received=quantity(received), returned=quantity(returned),
                    remaining=quantity(remain), dispatched_at=log.creation, status=status,
                ))
        return result

    @cached_property
    def movements(self):
        # Transfer amounts and ownership must reconcile even when we expose
        # just one physically authorized leg.
        _ = self.transfers
        allocations = correction._allocations(self.company, self.entries, locked=False)
        self._verify_native_details()
        times = dict(frappe.db.sql(
            """SELECT name,posting_datetime FROM `tabStock Ledger Entry`
               WHERE company=%s AND docstatus=1 AND is_cancelled=0""", (self.company,),
        ))
        totals, details = defaultdict(Decimal), {}
        for bucket, rows_ in allocations.items():
            for link, sle, amount in rows_:
                if not times.get(sle):
                    raise StockError(INVALID)
                key = bucket, link.operation_id, link.line_id
                totals[key] += amount
                item = details.setdefault(key, (link.document_id, link.action, times[sle]))
                if item != (link.document_id, link.action, times[sle]):
                    raise StockError(INVALID)
        self._verify_line_effects(totals, details)
        return sorted(
            (Movement(bucket, operation, line, *details[bucket, operation, line], amount)
             for (bucket, operation, line), amount in totals.items() if amount),
            key=lambda item: (item.date, item.operation, item.line, item.bucket.location_id,
                              item.bucket.batch_id or ""),
        )

    def _verify_native_details(self):
        missing = frappe.db.sql(
            f"""SELECT l.name,l.parent_type,l.parent_id,l.detail_id
                FROM `tab{stock.LINK}` l
                JOIN `tab{stock.LOG}` o ON o.name=l.operation_id
                JOIN `tab{stock.DOCUMENT}` d ON d.name=o.document_id
                LEFT JOIN `tabStock Entry Detail` sd ON l.parent_type='Stock Entry'
                    AND l.detail_type='Stock Entry Detail' AND sd.name=l.detail_id AND sd.parent=l.parent_id
                    AND sd.parenttype='Stock Entry' AND sd.parentfield='items' AND sd.docstatus=1
                LEFT JOIN `tabStock Reconciliation Item` rd ON l.parent_type='Stock Reconciliation'
                    AND l.detail_type='Stock Reconciliation Item' AND rd.name=l.detail_id AND rd.parent=l.parent_id
                    AND rd.parenttype='Stock Reconciliation' AND rd.parentfield='items' AND rd.docstatus=1
                WHERE d.company=%s AND o.status='Effective'
                    AND l.parent_type IN ('Stock Entry','Stock Reconciliation')
                    AND sd.name IS NULL AND rd.name IS NULL
                ORDER BY l.name LIMIT 100""", (self.company,), as_dict=True,
        )
        if missing:
            raise StockError(INVALID, evidence=dict(check="NATIVE_DETAIL", links=missing))

    def _verify_line_effects(self, totals, details):
        expected = {}
        ordinary = (rules.RECEIPT, rules.ISSUE, rules.CORRECTION)
        for line in frappe.db.sql(
            f"""SELECT l.name,l.sku_id,l.source,l.target,l.batch_kind,l.batch_id,l.batch_no,l.quantity,
                d.name document,d.document_type,d.effective_operation FROM `tab{stock.LINE}` l
                JOIN `tab{stock.DOCUMENT}` d ON l.document_id=d.name
                WHERE d.company=%s AND d.document_type IN %s AND d.effective_operation IS NOT NULL""",
            (self.company, ordinary), as_dict=True,
        ):
            amount = Decimal(line.quantity)
            expected[line.effective_operation, line.name] = (
                line.document, line.document_type, line.sku_id, line.source or line.target, line.batch_kind,
                line.batch_id or None, line.batch_no or None, -amount if line.source else amount,
            )
        for line in frappe.db.sql(
            f"""SELECT l.name,l.sku_id,l.location_id,l.batch_kind,l.batch_id,l.batch_no,
                       l.snapshot_quantity,l.actual_quantity,l.difference_quantity,
                       d.name document,d.effective_operation
                FROM `tab{versions.LINE}` l JOIN `tab{stock.DOCUMENT}` d ON d.name=l.task_id
                WHERE d.company=%s AND d.document_type=%s AND d.effective_operation IS NOT NULL""",
            (self.company, rules.COUNT), as_dict=True,
        ):
            difference = Decimal(line.actual_quantity) - Decimal(line.snapshot_quantity)
            if difference != Decimal(line.difference_quantity):
                raise StockError(INVALID)
            if difference:
                expected[line.effective_operation, line.name] = (
                    line.document, rules.COUNT, line.sku_id, line.location_id, line.batch_kind,
                    line.batch_id or None, line.batch_no or None, difference,
                )
        seen, realized = set(), {}
        for (bucket, operation, line), amount in totals.items():
            document, action, _ = details[bucket, operation, line]
            if action in (stock_entry.TRANSFER, "Receive Transfer", "Return Transfer"):
                continue
            key = operation, line
            wanted = expected.get(key)
            if (not wanted or key in seen or wanted[:5] != (
                document, action, bucket.sku_id, bucket.location_id, bucket.batch_kind,
            ) or (wanted[5] and wanted[5] != bucket.batch_id)
                or (wanted[6] and wanted[6] != self.batch(bucket.batch_id)) or wanted[7] != amount):
                raise StockError(INVALID)
            seen.add(key)
            realized[key] = bucket, action, amount
        if seen != expected.keys():
            raise StockError(INVALID, evidence=dict(missing=sorted(expected.keys() - seen)[:100]))
        self._verify_corrections(realized)

    def _verify_corrections(self, realized):
        consumed = defaultdict(Decimal)
        cases = frappe.db.sql(
            f"""SELECT c.original_operation,c.name,c.line_origins,d.effective_operation
                FROM `tab{CASE}` c JOIN `tab{stock.DOCUMENT}` d ON d.name=c.name
                WHERE d.company=%s AND d.document_type=%s AND d.effective_operation IS NOT NULL""",
            (self.company, rules.CORRECTION), as_dict=True,
        )
        effects = defaultdict(set)
        for (operation, line), (_, action, _) in realized.items():
            if action == rules.CORRECTION:
                effects[operation].add(line)
        for case in cases:
            mapping = correction.line_origins(case)
            if effects.pop(case.effective_operation, set()) != mapping.keys():
                raise StockError(INVALID, evidence=dict(correction=case.name, check="CORRECTION_LINES"))
            for line, original in mapping.items():
                identity = case.original_operation, original
                original_fact = realized.get(identity)
                bucket, _, delta = realized[case.effective_operation, line]
                if not original_fact:
                    raise StockError(INVALID, evidence=dict(correction=case.name, check="CORRECTION_ORIGINAL"))
                original_bucket, action, baseline = original_fact
                consumed[identity] += abs(delta)
                if (bucket != original_bucket or action == rules.CORRECTION or delta * baseline >= 0
                        or consumed[identity] > abs(baseline)):
                    raise StockError(INVALID, evidence=dict(correction=case.name, line=line,
                                                            check="CORRECTION_ENTITLEMENT"))
        if effects:
            raise StockError(INVALID, evidence=dict(check="MISSING_CORRECTION_CASE", operations=sorted(effects)[:100]))
