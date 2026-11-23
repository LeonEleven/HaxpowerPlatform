"""Guard and reconcile native Stock Entry rows and their signed stock effects."""

from collections import defaultdict
from contextvars import ContextVar
from dataclasses import dataclass
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
    bundles: dict[str, str]
    purpose: str = RECEIPT

    @property
    def action(self):
        return "Issue" if self.purpose == ISSUE else "Receipt"


def _number(value):
    return Decimal(str(value or 0))


def _leg(row, leg):
    warehouse, opposite = (row.s_warehouse, row.t_warehouse) if leg.source else (row.t_warehouse, row.s_warehouse)
    return (
        (row.item_code, warehouse, row.batch_no, row.uom, row.stock_uom)
        == (leg.item, leg.warehouse, leg.batch, leg.uom, leg.uom)
        and _number(row.qty) == leg.quantity.copy_abs() and _number(row.conversion_factor) == 1
        and not opposite and not row.serial_no and row.use_serial_batch_fields == 1
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


def allows_write(doc, event):
    scope = _scope.get()
    if not scope or scope.actor != frappe.session.user or not _native_orm_active.get():
        return False
    entry = scope.entry
    if doc.doctype == ENTRY and doc is entry:
        if event in ("before_insert", "before_validate", "before_save", "before_submit"):
            return (
                entry.company == scope.company and entry.purpose == scope.purpose and entry.stock_entry_type == scope.purpose
                and entry.docstatus == (1 if event == "before_submit" or entry._action == "submit" else 0)
                and not entry.from_warehouse and not entry.add_to_transit
                and (scope.purpose == RECEIPT or not entry.to_warehouse)
                and len(entry.items) == len(scope.legs)
                and all(_leg(row, leg) for row, leg in zip(entry.items, scope.legs, strict=True))
            )
        return False
    if entry.docstatus != 1:
        return False
    if doc.doctype == "Bin":
        return (
            scope.purpose == RECEIPT and event in ("before_insert", "before_validate", "before_save") and doc.is_new()
            and any((doc.item_code, doc.warehouse) == (leg.item, leg.warehouse) for leg in scope.legs)
        )
    if doc.doctype not in (BUNDLE, SLE):
        return False
    if (
        (doc.voucher_type, doc.voucher_no, doc.company) != (ENTRY, entry.name, scope.company)
        or not (leg := scope.details.get(doc.voucher_detail_no))
    ):
        return False
    if doc.doctype == BUNDLE:
        if (event not in ("before_insert", "before_validate", "before_save", "before_submit", "before_change")
                or doc.docstatus not in (0, 1) or not _bundle(doc, leg)):
            return False
        if doc.is_new() and doc.name and event == "before_validate":
            scope.bundles.setdefault(doc.voucher_detail_no, doc.name)
        return (
            (event == "before_insert" and doc.is_new() and doc.voucher_detail_no not in scope.bundles)
            or (doc.name and scope.bundles.get(doc.voucher_detail_no) == doc.name)
        )
    return (
        event in ("before_insert", "before_validate", "before_submit", "before_change")
        and doc.docstatus == 1 and not doc.is_cancelled
        and (doc.item_code, doc.warehouse) == (leg.item, leg.warehouse)
        and _number(doc.actual_qty) == leg.quantity and bool(doc.serial_and_batch_bundle)
        and scope.bundles.get(doc.voucher_detail_no) == doc.serial_and_batch_bundle
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


def _verify_posting(scope, before, before_batch, deltas, *, allow_gl):
    entry = scope.entry
    error = scope.action.upper()
    stored = frappe.get_doc(ENTRY, entry.name, for_update=True)
    if (stored.company, stored.purpose, stored.stock_entry_type, stored.docstatus, stored.owner) != (
        scope.company, scope.purpose, scope.purpose, 1, scope.actor,
    ) or {row.name for row in stored.items} != set(scope.details):
        raise StockError(f"{error}_NATIVE_REFERENCE_INVALID")
    posted = frappe.db.get_values(
        SLE, {"voucher_no": entry.name, "voucher_type": ENTRY},
        ("voucher_detail_no", "item_code", "warehouse", "company", "serial_and_batch_bundle",
         "actual_qty", "docstatus", "is_cancelled"), as_dict=True, for_update=True,
    )
    by_detail = {row.voucher_detail_no: row for row in posted}
    if len(posted) != len(scope.details) or set(by_detail) != set(scope.details):
        raise StockError(f"{error}_NATIVE_REFERENCE_INVALID")
    bundles = set()
    links = []
    for row in stored.items:
        leg = scope.details[row.name]
        sle = by_detail[row.name]
        if (
            not _leg(row, leg) or _number(row.transfer_qty) != leg.quantity.copy_abs()
            or not row.serial_and_batch_bundle or row.serial_and_batch_bundle in bundles
            or (sle.item_code, sle.warehouse, sle.company, sle.serial_and_batch_bundle,
                _number(sle.actual_qty), sle.docstatus, sle.is_cancelled)
            != (leg.item, leg.warehouse, entry.company, row.serial_and_batch_bundle, leg.quantity, 1, 0)
        ):
            raise StockError(f"{error}_NATIVE_REFERENCE_INVALID")
        bundle = frappe.get_doc(BUNDLE, row.serial_and_batch_bundle, for_update=True)
        if (
            (bundle.docstatus, bundle.voucher_type, bundle.voucher_no, bundle.voucher_detail_no, bundle.company)
            != (1, ENTRY, entry.name, row.name, entry.company)
            or not _bundle(bundle, leg)
        ):
            raise StockError(f"{error}_NATIVE_REFERENCE_INVALID")
        bundles.add(bundle.name)
        links.append(NativeLink(leg.line_id, scope.action, ENTRY, entry.name, DETAIL, row.name))

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
