"""Guard and reconcile native Stock Entry rows and their signed stock effects."""

from collections import defaultdict
from contextvars import ContextVar
from dataclasses import dataclass, field
from decimal import Decimal

import frappe

from ...catalog.sku_persistence import SKU
from ...inventory.batch.dto import BatchKind
from ...inventory.batch.persistence import MAPPING
from ...inventory.operations.dto import Bucket, NativeLink, StockError
from ...inventory.operations.transaction import _native_orm_active

ENTRY = "Stock Entry"
DETAIL = "Stock Entry Detail"
BUNDLE = "Serial and Batch Bundle"
BUNDLE_ROW = "Serial and Batch Entry"
SLE = "Stock Ledger Entry"
RECEIPT = "Material Receipt"
ISSUE = "Material Issue"
TRANSFER = "Material Transfer"
DISPATCH_TYPE = "HP V1 Transfer Dispatch"
_scope = ContextVar("hp_v1_native_stock_entry", default=None)


@dataclass(frozen=True)
class Leg:
    line_id: str
    bucket: Bucket
    item: str
    warehouse: str
    batch: str
    uom: str
    quantity: Decimal
    counterpart: str | None = None

    @property
    def source(self):
        return self.quantity < 0


@dataclass(frozen=True)
class Posting:
    actor: str
    document_id: str
    company: str
    entry: object
    legs: tuple[Leg, ...]
    details: dict[str, Leg]
    bundles: dict[tuple[str, str], str]
    purpose: str = RECEIPT
    inward: dict[str, Leg] = field(default_factory=dict)

    @property
    def action(self):
        return {ISSUE: "Issue", RECEIPT: "Receipt", TRANSFER: "Dispatch"}[self.purpose]


def _number(value):
    return Decimal(str(value or 0))


def _leg(row, leg):
    warehouse, opposite = (row.s_warehouse, row.t_warehouse) if leg.source else (row.t_warehouse, row.s_warehouse)
    return (
        (row.item_code, warehouse, row.batch_no, row.uom, row.stock_uom)
        == (leg.item, leg.warehouse, leg.batch, leg.uom, leg.uom)
        and _number(row.qty) == leg.quantity.copy_abs() and _number(row.conversion_factor) == 1
        and (opposite or None) == leg.counterpart and not row.serial_no and row.use_serial_batch_fields == 1
        and (not leg.counterpart or not any(row.get(key) for key in (
            "ste_detail", "against_stock_entry", "material_request", "material_request_item",
            "sco_rm_detail", "pick_list_item", "job_card_item",
        )))
    )


def _bundle(doc, leg):
    if len(doc.entries) != 1:
        return False
    row, = doc.entries
    return (
        (doc.item_code, doc.warehouse, doc.type_of_transaction) == (leg.item, leg.warehouse, "Outward" if leg.source else "Inward")
        and not doc.is_cancelled and not doc.has_serial_no
        and row.batch_no == leg.batch and not row.serial_no and _number(row.qty) == leg.quantity
    )


def _entry_shape(entry, scope):
    transit = scope.purpose == TRANSFER
    return (
        entry.company == scope.company and entry.purpose == scope.purpose
        and entry.stock_entry_type == (DISPATCH_TYPE if transit else scope.purpose)
        and entry.add_to_transit == int(transit) and not entry.from_warehouse
        and (scope.purpose == RECEIPT or not entry.to_warehouse)
        and (not transit or not any(entry.get(key) for key in (
            "outgoing_stock_entry", "work_order", "subcontracting_order", "pick_list", "is_return",
            "additional_costs", "project", "inspection_required", "from_bom", "job_card",
        )))
        and len(entry.items) == len(scope.legs)
        and all(_leg(row, leg) for row, leg in zip(entry.items, scope.legs, strict=True))
    )


def allows_dispatch_write(doc, event):
    scope = _scope.get()
    return bool(scope and scope.purpose == TRANSFER and allows_write(doc, event))


def allows_write(doc, event):
    scope = _scope.get()
    if not scope or scope.actor != frappe.session.user or not _native_orm_active.get():
        return False
    entry = scope.entry
    if doc.doctype == ENTRY and doc is entry:
        return (
            event in ("before_insert", "before_validate", "before_save", "before_submit")
            and entry.docstatus == (1 if event == "before_submit" or entry._action == "submit" else 0)
            and _entry_shape(entry, scope)
        )
    if entry.docstatus != 1:
        return False
    if doc.doctype == "Bin":
        incoming = scope.legs if scope.purpose == RECEIPT else scope.inward.values()
        return (
            event in ("before_insert", "before_validate", "before_save") and doc.is_new()
            and any((doc.item_code, doc.warehouse) == (leg.item, leg.warehouse) for leg in incoming)
        )
    if doc.doctype not in (BUNDLE, SLE):
        return False
    detail = doc.voucher_detail_no
    if (
        (doc.voucher_type, doc.voucher_no, doc.company) != (ENTRY, entry.name, scope.company)
        or not (leg := scope.details.get(detail))
    ):
        return False
    if leg.counterpart and doc.warehouse == leg.counterpart:
        leg = scope.inward[leg.line_id]
    key = detail, leg.warehouse
    if doc.doctype == BUNDLE:
        if (event not in ("before_insert", "before_validate", "before_save", "before_submit", "before_change")
                or doc.docstatus not in (0, 1) or not _bundle(doc, leg)):
            return False
        if doc.is_new() and doc.name and event == "before_validate":
            scope.bundles.setdefault(key, doc.name)
        return (
            (event == "before_insert" and doc.is_new() and key not in scope.bundles)
            or (doc.name and scope.bundles.get(key) == doc.name)
        )
    return (
        event in ("before_insert", "before_validate", "before_submit", "before_change")
        and doc.docstatus == 1 and not doc.is_cancelled
        and (doc.item_code, doc.warehouse) == (leg.item, leg.warehouse)
        and _number(doc.actual_qty) == leg.quantity and bool(doc.serial_and_batch_bundle)
        and scope.bundles.get(key) == doc.serial_and_batch_bundle
    )


class StockEntryBundlePermissions:
    def has_permission(self, permtype="read", *, debug=False, user=None):
        # ERPNext saves the draft bundle and submits it from SLE.on_submit
        # using the current session. Grant only those document-level calls.
        events = {"create": "before_insert", "write": "before_validate", "submit": "before_submit"}
        if user in (None, frappe.session.user) and permtype in events and allows_write(self, events[permtype]):
            return True
        return super().has_permission(permtype, debug=debug, user=user)


def posting_legs(plan, *, source=False):
    error = "ISSUE_MAPPING_INVALID" if source else "RECEIPT_MAPPING_INVALID"
    for line in sorted(plan.command.lines, key=lambda line: line.line_no):
        item = frappe.db.get_value(SKU, line.sku_id, "native_item", for_update=True)
        batch = frappe.db.get_value(MAPPING, line.batch_id, "native_batch", for_update=True)
        if not item or not batch:
            raise StockError(error)
        location = line.source if source else line.target
        quantity = Decimal(line.quantity)
        bucket = Bucket(plan.command.company, line.sku_id, location, line.batch_kind,
                        line.batch_id if line.batch_kind == BatchKind.BATCH else None)
        yield Leg(line.line_id, bucket, item, plan.locations[location].native_warehouse, batch,
                  line.uom, quantity.copy_negate() if source else quantity)


def _balances(company, pairs, *, error="RECEIPT_NATIVE_RECONCILIATION_FAILED", posting_at=None, scales=None):
    buckets, bins = defaultdict(Decimal), {}
    for item, warehouse in sorted(pairs):
        scale = scales[item] if scales else None
        rows = frappe.db.sql(
            """
            SELECT s.name, CAST(s.actual_qty AS CHAR), e.batch_no, CAST(e.qty AS CHAR), b.docstatus, e.docstatus,
                   b.voucher_no, b.voucher_detail_no, s.voucher_no, s.voucher_detail_no,
                   b.is_cancelled, e.is_cancelled, b.company, b.item_code, b.warehouse,
                   b.voucher_type, s.voucher_type, e.item_code, e.warehouse, e.serial_no,
                   b.type_of_transaction, s.posting_datetime, b.name, e.parenttype, e.parentfield
            FROM `tabStock Ledger Entry` s
            LEFT JOIN `tabSerial and Batch Bundle` b ON b.name = s.serial_and_batch_bundle
            LEFT JOIN `tabSerial and Batch Entry` e ON e.parent = b.name
            WHERE s.company = %s AND s.item_code = %s AND s.warehouse = %s
              AND s.docstatus = 1 AND s.is_cancelled = 0
            FOR UPDATE
            """, (company, item, warehouse),
        )
        ledger, allocated, bundle_ledgers = {}, defaultdict(Decimal), {}
        for (name, qty, batch, part, bundle_status, child_status, voucher, detail, sle_voucher, sle_detail,
             bundle_cancelled, part_cancelled, bundle_company, bundle_item, bundle_warehouse, bundle_type,
             sle_type, part_item, part_warehouse, serial, direction, posted_at, bundle_name,
             parent_type, parent_field) in rows:
            qty, part = _number(qty), _number(part)
            if (not batch or bundle_status != 1 or child_status != 1 or bundle_cancelled or part_cancelled
                    or (voucher, detail, bundle_type) != (sle_voucher, sle_detail, sle_type)
                    or (bundle_company, bundle_item, bundle_warehouse) != (company, item, warehouse)
                    or (part_item, part_warehouse, parent_type, parent_field) != (item, warehouse, BUNDLE, "entries")
                    or serial or direction not in ("Inward", "Outward")
                    or (direction == "Inward" and not (qty >= 0 and part >= 0))
                    or (direction == "Outward" and not (qty <= 0 and part <= 0))
                    or (scale is not None and (qty.quantize(scale) != qty or part.quantize(scale) != part))
                    or bundle_ledgers.setdefault(bundle_name, name) != name
                    or (posting_at is not None and (posted_at is None or posted_at > posting_at))):
                raise StockError(error)
            ledger[name] = qty
            allocated[name] += part
            buckets[item, warehouse, batch] += part
        if ledger != allocated:
            raise StockError(error)
        native_bins = frappe.db.sql(
            "SELECT CAST(actual_qty AS CHAR) FROM `tabBin` WHERE item_code=%s AND warehouse=%s FOR UPDATE",
            (item, warehouse),
        )
        bins[item, warehouse] = _number(native_bins[0][0]) if native_bins else Decimal(0)
        if (
            len(native_bins) > 1 or (scales and ledger and not native_bins)
            or bins[item, warehouse] != sum(ledger.values(), Decimal(0))
        ):
            raise StockError(error)
    return buckets, bins


def verify_entry(scope):
    entry = scope.entry
    error = f"{scope.action.upper()}_NATIVE_REFERENCE_INVALID"
    stored = frappe.get_doc(ENTRY, entry.name, for_update=True)
    if (
        (stored.docstatus, stored.owner) != (1, scope.actor) or not _entry_shape(stored, scope)
        or {row.name for row in stored.items} != set(scope.details)
    ):
        raise StockError(error)
    posted = frappe.db.get_values(
        SLE, {"voucher_no": entry.name, "voucher_type": ENTRY},
        ("name", "voucher_detail_no", "item_code", "warehouse", "company", "serial_and_batch_bundle",
         "actual_qty", "docstatus", "is_cancelled"), as_dict=True, for_update=True,
    )
    by_leg = {(row.voucher_detail_no, row.warehouse): row for row in posted}
    if len(posted) != len(by_leg) or len(by_leg) != len(scope.details) + len(scope.inward):
        raise StockError(error)
    bundles = set()
    links = []
    for row in stored.items:
        leg = scope.details[row.name]
        if not _leg(row, leg) or _number(row.transfer_qty) != leg.quantity.copy_abs():
            raise StockError(error)
        expected = (leg, scope.inward[leg.line_id]) if scope.inward else (leg,)
        for index, effect in enumerate(expected):
            sle = by_leg.get((row.name, effect.warehouse))
            if not sle or (
                sle.item_code, sle.company, _number(sle.actual_qty), sle.docstatus, sle.is_cancelled
            ) != (effect.item, entry.company, effect.quantity, 1, 0):
                raise StockError(error)
            bundle_id = sle.serial_and_batch_bundle
            if (not bundle_id or bundle_id in bundles or (index == 0 and row.serial_and_batch_bundle != bundle_id)):
                raise StockError(error)
            bundle = frappe.get_doc(BUNDLE, bundle_id, for_update=True)
            if (
                (bundle.docstatus, bundle.voucher_type, bundle.voucher_no, bundle.voucher_detail_no, bundle.company)
                != (1, ENTRY, entry.name, row.name, entry.company) or not _bundle(bundle, effect)
            ):
                raise StockError(error)
            bundles.add(bundle.name)
        links.append(NativeLink(leg.line_id, scope.action, ENTRY, entry.name, DETAIL, row.name))
    return tuple(links)


def _verify_posting(scope, before, before_batch, deltas, *, allow_gl):
    links = verify_entry(scope)
    entry = scope.entry
    error = scope.action.upper()
    if not allow_gl and frappe.db.get_value(
        "GL Entry", {"voucher_type": ENTRY, "voucher_no": entry.name}, for_update=True,
    ):
        raise StockError(f"{error}_GL_NOT_ALLOWED")
    expected_buckets, expected_bins = before
    reconciliation = f"{error}_NATIVE_RECONCILIATION_FAILED"
    for (item, warehouse, batch), delta in deltas.items():
        expected_buckets[item, warehouse, batch] += delta
        expected_bins[item, warehouse] += delta
        before_batch[batch] += delta
    if (expected_buckets, expected_bins) != _balances(entry.company, expected_bins, error=reconciliation):
        raise StockError(reconciliation)
    if before_batch != _batch_totals(before_batch):
        raise StockError(reconciliation)
    return tuple(links)


def _batch_totals(names):
    totals = {}
    for name in sorted(names):
        rows = frappe.db.sql(
            "SELECT CAST(batch_qty AS CHAR) FROM `tabBatch` WHERE name=%s FOR UPDATE", (name,),
        )
        if len(rows) != 1:
            raise StockError("BATCH_MAPPING_UNAVAILABLE")
        totals[name] = _number(rows[0][0])
    return totals
