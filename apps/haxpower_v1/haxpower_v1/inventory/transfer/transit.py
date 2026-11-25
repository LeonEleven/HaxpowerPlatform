"""Resolve the system transit leaf and bind immutable ownership to original lines."""

from collections import defaultdict
from dataclasses import dataclass, replace
from decimal import Decimal

import frappe

from ...adapters.erpnext import stock_entry as stock, warehouse as native_warehouse
from ...catalog.dto import ACTIVE
from ...warehouse.persistence import LOCATION, TRANSIT, WAREHOUSE
from ..operations import persistence as store
from ..batch.dto import BatchKind
from ..operations.dto import Bucket, Command, LineQuantity, StockError
from ..operations.quantity import canonical_quantity

OWNERSHIP = store.TRANSIT_OWNERSHIP
INCONSISTENT = "TRANSFER_TRANSIT_OWNERSHIP_INCONSISTENT"
IN_TRANSIT = "In Transit"
PARTIAL = "Partially Received"
COMPLETED = "Completed"
CLOSED_RETURN = "Closed with Return"
DISPATCHED_STATUSES = (IN_TRANSIT, PARTIAL, COMPLETED, CLOSED_RETURN)
COMMANDS = {stock.RECEIVE: "Receive Transfer", stock.RETURN: "Return Transfer"}


def resolve(company):
    warehouses = frappe.db.get_values(
        WAREHOUSE, {"company": company, "kind": TRANSIT}, ("name", "company", "kind", "native_parent", "status"),
        for_update=True, as_dict=True, order_by="name asc",
    )
    locations = frappe.db.get_values(
        LOCATION, {"warehouse": ("in", [row.name for row in warehouses])},
        ("name", "warehouse", "native_warehouse", "status"), for_update=True, as_dict=True, order_by="name asc",
    ) if warehouses else ()
    if (
        len(warehouses) != 1 or warehouses[0].status != ACTIVE
        or len(locations) != 1 or locations[0].status != ACTIVE
        or native_warehouse.mapping_status(locations[0], warehouses[0], locked=True) != "Verified"
    ):
        raise StockError("TRANSFER_TRANSIT_UNAVAILABLE")
    return locations[0]


@dataclass(frozen=True)
class Effect:
    operation: str
    line: str
    role: str
    quantity: Decimal


@dataclass(frozen=True)
class Lifecycle:
    leaf: object
    originals: dict
    effects: tuple[Effect, ...]
    received: dict
    returned: dict

    def remaining(self, original):
        return stock._number(original.quantity) - self.received[original.line_id] - self.returned[original.line_id]


def status_for(remaining, received, returned):
    if any(remaining.values()):
        return PARTIAL if any(received.values()) else IN_TRANSIT
    return CLOSED_RETURN if any(returned.values()) else COMPLETED


def _originals(company, item_codes, leaf):
    rows = frappe.db.sql(
        f"""
        SELECT own.name AS ownership_id, own.inward_sle, own.operation_id, own.native_detail,
               line.name AS line_id, line.quantity, line.document_id, line.sku_id,
               line.batch_kind, line.batch_id, line.batch_no, line.source, line.target, line.uom,
               doc.company, doc.document_type, doc.status, doc.effective_operation,
               op.document_id AS operation_document, op.action, op.status AS operation_status, op.actor,
               link.line_id AS link_line, link.parent_type, link.parent_id AS native_entry,
               link.detail_type, link.ownership_id AS link_ownership,
               sku.native_item AS item, source.native_warehouse AS source_leaf, target.native_warehouse AS target_leaf
        FROM `tab{OWNERSHIP}` own
        LEFT JOIN `tab{store.LINE}` line ON line.name = own.line_id
        LEFT JOIN `tab{store.DOCUMENT}` doc ON doc.name = line.document_id
        LEFT JOIN `tab{store.LOG}` op ON op.name = own.operation_id
        LEFT JOIN `tab{store.LINK}` link ON link.operation_id = own.operation_id
            AND link.detail_id = own.native_detail AND link.role = 'Dispatch'
        LEFT JOIN `tabHP V1 Stock Item` sku ON sku.name = line.sku_id
        LEFT JOIN `tab{LOCATION}` source ON source.name = line.source
        LEFT JOIN `tab{LOCATION}` target ON target.name = line.target
        WHERE own.transit_location = %s AND (sku.native_item IN %s OR sku.native_item IS NULL)
        ORDER BY own.name FOR UPDATE
        """, (leaf.name, tuple(sorted(item_codes))), as_dict=True,
    )
    originals = {}
    for row in rows:
        if (
            row.ownership_id in originals or row.company != company or row.document_type != stock.TRANSFER
            or row.status not in DISPATCHED_STATUSES or row.operation_id != row.effective_operation
            or (row.operation_document, row.action, row.operation_status) != (row.document_id, stock.TRANSFER, "Effective")
            or not row.actor or (row.link_line, row.parent_type, row.detail_type) != (row.line_id, stock.ENTRY, stock.DETAIL)
            or not row.native_entry or row.link_ownership or not row.source_leaf or not row.target_leaf
            or len({row.source_leaf, row.target_leaf, leaf.native_warehouse}) != 3 or stock._number(row.quantity) <= 0
        ):
            raise StockError(INCONSISTENT)
        originals[row.ownership_id] = row
    return originals


def _followups(originals):
    if not originals:
        return ()
    return frappe.db.sql(
        f"""
        SELECT link.operation_id, link.ownership_id, link.line_id, link.role, link.detail_id, link.parent_id,
               link.detail_type, link.parent_type, op.actor, op.original_operation, op.document_id,
               op.action, op.status
        FROM `tab{store.LINK}` link
        LEFT JOIN `tab{store.LOG}` op ON op.name = link.operation_id
        WHERE link.ownership_id IN %s OR (link.line_id IN %s AND link.role <> 'Dispatch')
        ORDER BY link.name FOR UPDATE
        """, (tuple(originals), tuple(row.line_id for row in originals.values())), as_dict=True,
    )


def _native_legs(company, row, parent, source, target, original, role, quantity, bundles):
    outgoing = original.native_entry if role == stock.RECEIVE else None
    detail = original.native_detail if outgoing else None
    bucket = Bucket(company, original.sku_id, original.source, original.batch_kind, original.batch_id)
    leg = stock.Leg(original.line_id, bucket, original.item, source, original.native_batch, original.uom,
                    quantity.copy_negate(), target, detail, outgoing)
    inward = replace(leg, warehouse=target, counterpart=source, quantity=quantity)
    scope = stock.Posting(parent.owner, original.document_id, company, parent, (leg,), {}, {}, stock.TRANSFER,
                          transfer_action=role)
    if (
        parent.docstatus != 1
        or not stock._entry_header(parent, scope) or not stock._leg(row, leg)
        or (row.parent, row.parenttype, row.parentfield, row.docstatus) != (parent.name, stock.ENTRY, "items", 1)
        or stock._number(row.transfer_qty) != quantity or quantity <= 0
    ):
        raise StockError(INCONSISTENT)
    posted = frappe.db.get_values(
        stock.SLE, {"voucher_detail_no": row.name},
        ("name", "voucher_detail_no", "item_code", "warehouse", "company", "serial_and_batch_bundle",
         "actual_qty", "docstatus", "is_cancelled", "voucher_no", "voucher_type"),
        as_dict=True, for_update=True,
    )
    if len(posted) != 2 or any((sle.voucher_no, sle.voucher_type) != (parent.name, stock.ENTRY) for sle in posted):
        raise StockError(INCONSISTENT)
    by_leg = {(sle.voucher_detail_no, sle.warehouse): sle for sle in posted}
    stock.verify_legs(company, parent.name, row, (leg, inward), by_leg, bundles, INCONSISTENT)
    return by_leg


def reconcile(company, item_codes, leaf, buckets):
    """Check each native Transit SLE and its owner in both directions."""
    ledger = set(frappe.db.get_values(
        stock.SLE, {"company": company, "warehouse": leaf.native_warehouse, "item_code": ("in", sorted(item_codes)),
                    "docstatus": 1, "is_cancelled": 0},
        "name", for_update=True, pluck=True,
    ))
    originals = _originals(company, item_codes, leaf)
    parents, details, batches, bundles, covered = {}, {}, {}, set(), set()
    parent_operations, linked_details = {}, set()

    def native_rows(parent_id, detail_id, actor, operation):
        if parent_id not in parents:
            if not store.current(stock.ENTRY, parent_id, "name"):
                raise StockError(INCONSISTENT)
            parent = parents[parent_id] = frappe.get_doc(stock.ENTRY, parent_id, for_update=True)
            details.update((row.name, row) for row in parent.items)
        parent = parents[parent_id]
        row = details.get(detail_id)
        if (
            not row or row.parent != parent_id or parent.owner != actor or detail_id in linked_details
            or parent_operations.setdefault(parent_id, operation) != operation
        ):
            raise StockError(INCONSISTENT)
        linked_details.add(detail_id)
        return row, parent

    for original in originals.values():
        row, parent = native_rows(original.native_entry, original.native_detail, original.actor, original.operation_id)
        batch_key = original.sku_id, original.batch_kind, row.batch_no
        if batch_key not in batches:
            batches[batch_key] = frappe.db.get_value(
                "HP V1 Batch Mapping", {"sku": original.sku_id, "kind": original.batch_kind, "native_batch": row.batch_no},
                ("name", "original_text"), as_dict=True, for_update=True,
            )
        mapping = batches[batch_key]
        original.native_batch = row.batch_no
        if (
            not mapping or not row.batch_no or stock._number(row.transferred_qty) < 0
            or original.batch_kind not in (BatchKind.NO_BATCH, BatchKind.BATCH)
            or (original.batch_kind == BatchKind.BATCH
                and (mapping.name, mapping.original_text) != (original.batch_id, original.batch_no))
            or (original.batch_kind == BatchKind.NO_BATCH and original.batch_id is not None)
        ):
            raise StockError(INCONSISTENT)
        original.mapping_id = mapping.name
        legs = _native_legs(company, row, parent, original.source_leaf, leaf.native_warehouse,
                            original, stock.DISPATCH, stock._number(original.quantity), bundles)
        if legs[row.name, leaf.native_warehouse].name != original.inward_sle:
            raise StockError(INCONSISTENT)
        covered.add(original.inward_sle)

    received, returned = defaultdict(Decimal), defaultdict(Decimal)
    effects, seen_lines, receive_details = [], set(), set()
    for link in _followups(originals):
        original = originals.get(link.ownership_id)
        if (
            not original or (link.line_id, link.document_id, link.original_operation)
            != (original.line_id, original.document_id, original.operation_id)
            or (link.parent_type, link.detail_type) != (stock.ENTRY, stock.DETAIL)
            or link.role not in (stock.RECEIVE, stock.RETURN)
            or link.action != COMMANDS[link.role] or link.status != "Effective"
            or (link.operation_id, link.line_id) in seen_lines
        ):
            raise StockError(INCONSISTENT)
        row, parent = native_rows(link.parent_id, link.detail_id, link.actor, link.operation_id)
        if stock._number(row.transferred_qty):
            raise StockError(INCONSISTENT)
        quantity = stock._number(row.transfer_qty)
        target = original.target_leaf if link.role == stock.RECEIVE else original.source_leaf
        legs = _native_legs(company, row, parent, leaf.native_warehouse, target, original, link.role, quantity, bundles)
        covered.add(legs[row.name, leaf.native_warehouse].name)
        seen_lines.add((link.operation_id, link.line_id))
        if link.role == stock.RECEIVE:
            receive_details.add(row.name)
        totals = received if link.role == stock.RECEIVE else returned
        totals[link.line_id] += quantity
        effects.append(Effect(link.operation_id, link.line_id, link.role, quantity))

    if originals:
        native_receives = set(frappe.db.get_values(
            stock.DETAIL, {"ste_detail": ("in", sorted(row.native_detail for row in originals.values())), "docstatus": 1},
            "name", for_update=True, pluck=True,
        ))
        if native_receives != receive_details:
            raise StockError(INCONSISTENT)

    lifecycle = Lifecycle(leaf, originals, tuple(effects), received, returned)
    bucket_remaining = defaultdict(Decimal)
    for original in originals.values():
        remaining = lifecycle.remaining(original)
        if remaining < 0 or stock._number(details[original.native_detail].transferred_qty) != received[original.line_id]:
            raise StockError(INCONSISTENT)
        bucket_remaining[original.item, leaf.native_warehouse, original.native_batch] += remaining
    for parent_id in {row.native_entry for row in originals.values()}:
        parent = parents[parent_id]
        quantity = sum(stock._number(row.transfer_qty) for row in parent.items)
        actual_received = sum(stock._number(row.transferred_qty) for row in parent.items)
        if quantity <= 0 or not stock.percent_matches(parent.per_transferred, stock.transfer_percent(quantity, actual_received)):
            raise StockError(INCONSISTENT)
    if (
        linked_details != {row.name for row in details.values() if row.item_code in item_codes}
        or covered != ledger
        or bucket_remaining != {key: qty for key, qty in buckets.items() if key[1] == leaf.native_warehouse}
    ):
        raise StockError(INCONSISTENT)
    return lifecycle


def verify_document(doc, lines, lifecycle):
    from ..operations.service import payload_digest

    originals = {row.line_id: row for row in lifecycle.originals.values() if row.operation_id == doc.effective_operation}
    if not doc.effective_operation or originals.keys() != {line.line_id for line in lines}:
        raise StockError(INCONSISTENT)
    logs = frappe.db.sql(
        f"""
        SELECT op.name, op.action, op.actor, op.creation, op.reason, op.notes, op.original_operation, op.status,
               op.result_revision, op.result_status, op.effect_key, op.request_id, req.status AS request_status,
               req.actor AS request_actor, req.command, req.company AS request_company,
               req.document_id AS request_document, req.result_operation, req.result_revision AS request_revision,
               req.payload_digest
        FROM `tab{store.LOG}` op
        LEFT JOIN `tab{store.REQUEST}` req ON req.name = op.request_id
        WHERE op.document_id = %s
        ORDER BY op.result_revision FOR UPDATE
        """, (doc.name,), as_dict=True,
    )
    if not logs or logs[0].name != doc.effective_operation:
        raise StockError(INCONSISTENT)
    effects = defaultdict(list)
    for effect in lifecycle.effects:
        if effect.line in originals:
            effects[effect.operation].append(effect)
    remaining = {identity: stock._number(row.quantity) for identity, row in originals.items()}
    received, returned = defaultdict(Decimal), defaultdict(Decimal)
    for revision, log in enumerate(logs, 2):
        if (
            (log.request_status, log.request_actor, log.request_company, log.request_document)
            != ("Completed", log.actor, doc.company, doc.name)
            or (log.command, log.result_operation, log.result_revision, log.request_revision, log.status)
            != (log.action, log.name, revision, revision, "Effective")
        ):
            raise StockError(INCONSISTENT)
        if revision == 2:
            if (log.action != stock.TRANSFER or log.effect_key != "APPLY" or log.original_operation
                    or log.reason != doc.reason or (log.result_status or IN_TRANSIT) != IN_TRANSIT):
                raise StockError(INCONSISTENT)
            native_links = frappe.db.get_values(
                store.LINK, {"operation_id": log.name}, ("line_id", "detail_id", "role"), for_update=True,
            )
            if len(native_links) != len(originals) or set(native_links) != {
                (row.line_id, row.native_detail, stock.DISPATCH) for row in originals.values()
            }:
                raise StockError(INCONSISTENT)
            command = Command(doc.company, doc.name, 1, "",
                              tuple(replace(line, batch_id=None) for line in lines), doc.reason, notes=doc.notes)
        else:
            operation_effects = effects.pop(log.name, ())
            if not operation_effects or log.original_operation != doc.effective_operation or log.effect_key != log.request_id:
                raise StockError(INCONSISTENT)
            for effect in operation_effects:
                prior = received if effect.role == stock.RECEIVE else returned
                remaining[effect.line] -= effect.quantity
                prior[effect.line] += effect.quantity
                if remaining[effect.line] < 0 or COMMANDS[effect.role] != log.action:
                    raise StockError(INCONSISTENT)
            if log.result_status != status_for(remaining, received, returned):
                raise StockError(INCONSISTENT)
            command = Command(
                doc.company, doc.name, revision - 1, "",
                tuple(LineQuantity(effect.line, canonical_quantity(format(effect.quantity, "f")))
                      for effect in sorted(operation_effects, key=lambda effect: effect.line)),
                log.reason, notes=log.notes,
            )
        if payload_digest(command, log.actor, log.action) != log.payload_digest:
            raise StockError(INCONSISTENT)
    if effects or (doc.status, doc.revision) != (status_for(remaining, received, returned), logs[-1].result_revision):
        raise StockError(INCONSISTENT)
    return logs[0], originals


def record_dispatch(plan, result, links):
    """Persist immutable business references after the operation log is staged."""
    from .. import availability
    from ..operations.service import _lock_document

    company = plan.command.company
    leaf = resolve(company)
    for link in links:
        sle = frappe.db.get_value(stock.SLE, {
            "warehouse": leaf.native_warehouse, "company": company,
            "voucher_type": stock.ENTRY, "voucher_no": link.parent_id, "voucher_detail_no": link.detail_id,
            "docstatus": 1, "is_cancelled": 0,
        }, "name", for_update=True)
        if not sle:
            raise StockError(INCONSISTENT)
        store.insert(OWNERSHIP, operation_id=result.operation_id, line_id=link.line_id,
                     transit_location=leaf.name, native_detail=link.detail_id, inward_sle=sle)
    # The document has been marked In Transit within the same transaction.
    items = {sku.native_item for sku in plan.skus.values()}
    (buckets, _), _ = availability.read_source(
        company, {(item, leaf.native_warehouse) for item in items}, plan.skus.values(),
    )
    lifecycle = reconcile(company, items, leaf, buckets)
    doc, original_lines = _lock_document(company, result.document_id, stock.TRANSFER)
    verify_document(doc, original_lines, lifecycle)
