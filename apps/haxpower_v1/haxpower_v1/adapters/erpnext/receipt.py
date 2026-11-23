"""ERPNext v16 Material Receipt and its native quantity reconciliation."""

from collections import defaultdict
from contextvars import ContextVar
from dataclasses import dataclass
from decimal import Decimal

import frappe
from frappe.utils import now_datetime

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
PURPOSE = "Material Receipt"
_scope = ContextVar("hp_v1_native_receipt", default=None)


@dataclass(frozen=True)
class Leg:
    line_id: str
    bucket: Bucket
    item: str
    warehouse: str
    batch: str
    uom: str
    quantity: Decimal


@dataclass(frozen=True)
class Posting:
    actor: str
    document_id: str
    company: str
    entry: object
    legs: tuple[Leg, ...]
    details: dict[str, Leg]
    bundles: dict[str, str]


def _number(value):
    return Decimal(str(value or 0))


def _leg(row, leg):
    return (
        (row.item_code, row.t_warehouse, row.batch_no, row.uom, row.stock_uom)
        == (leg.item, leg.warehouse, leg.batch, leg.uom, leg.uom)
        and _number(row.qty) == leg.quantity and _number(row.conversion_factor) == 1
        and not row.s_warehouse and not row.serial_no
    )


def _bundle(doc, leg):
    if len(doc.entries) != 1:
        return False
    row, = doc.entries
    return (
        (doc.item_code, doc.warehouse, doc.type_of_transaction) == (leg.item, leg.warehouse, "Inward")
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
                entry.company == scope.company and entry.purpose == PURPOSE and entry.stock_entry_type == PURPOSE
                and entry.docstatus == (1 if event == "before_submit" or entry._action == "submit" else 0)
                and not entry.from_warehouse and not entry.add_to_transit
                and len(entry.items) == len(scope.legs)
                and all(_leg(row, leg) for row, leg in zip(entry.items, scope.legs, strict=True))
            )
        return False
    if entry.docstatus != 1:
        return False
    if doc.doctype == "Bin":
        return (
            event in ("before_insert", "before_validate", "before_save") and doc.is_new()
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


class ReceiptBundlePermissions:
    def has_permission(self, permtype="read", *, debug=False, user=None):
        # ERPNext saves the draft bundle and submits it from SLE.on_submit
        # using the current session. Grant only those document-level calls.
        events = {"create": "before_insert", "write": "before_validate", "submit": "before_submit"}
        if user in (None, frappe.session.user) and permtype in events and allows_write(self, events[permtype]):
            return True
        return super().has_permission(permtype, debug=debug, user=user)


def _posting_legs(plan):
    for line in sorted(plan.command.lines, key=lambda line: line.line_no):
        item = frappe.db.get_value(SKU, line.sku_id, "native_item", for_update=True)
        batch = frappe.db.get_value(MAPPING, line.batch_id, "native_batch", for_update=True)
        if not item or not batch:
            raise StockError("RECEIPT_MAPPING_INVALID")
        bucket = Bucket(plan.command.company, line.sku_id, line.target, line.batch_kind,
                        line.batch_id if line.batch_kind == BatchKind.BATCH else None)
        yield Leg(line.line_id, bucket, item, plan.locations[line.target].native_warehouse, batch,
                  line.uom, Decimal(line.quantity))


def _balances(company, pairs):
    buckets, bins = defaultdict(Decimal), {}
    for item, warehouse in pairs:
        rows = frappe.db.sql(
            """
            SELECT s.name, CAST(s.actual_qty AS CHAR), e.batch_no, CAST(e.qty AS CHAR), b.docstatus, e.docstatus,
                   b.voucher_no, b.voucher_detail_no, s.voucher_no, s.voucher_detail_no,
                   b.is_cancelled, e.is_cancelled
            FROM `tabStock Ledger Entry` s
            LEFT JOIN `tabSerial and Batch Bundle` b ON b.name = s.serial_and_batch_bundle
            LEFT JOIN `tabSerial and Batch Entry` e ON e.parent = b.name
            WHERE s.company = %s AND s.item_code = %s AND s.warehouse = %s
              AND s.docstatus = 1 AND s.is_cancelled = 0
            FOR UPDATE
            """, (company, item, warehouse),
        )
        ledger, allocated = {}, defaultdict(Decimal)
        for (name, qty, batch, part, bundle_status, child_status, voucher, detail, sle_voucher, sle_detail,
             bundle_cancelled, part_cancelled) in rows:
            if (not batch or bundle_status != 1 or child_status != 1 or bundle_cancelled or part_cancelled
                    or (voucher, detail) != (sle_voucher, sle_detail)):
                raise StockError("RECEIPT_NATIVE_RECONCILIATION_FAILED")
            ledger[name] = _number(qty)
            allocated[name] += _number(part)
            buckets[item, warehouse, batch] += _number(part)
        if ledger != allocated:
            raise StockError("RECEIPT_NATIVE_RECONCILIATION_FAILED")
        native_bins = frappe.db.sql(
            "SELECT CAST(actual_qty AS CHAR) FROM `tabBin` WHERE item_code=%s AND warehouse=%s FOR UPDATE",
            (item, warehouse),
        )
        bins[item, warehouse] = _number(native_bins[0][0]) if native_bins else Decimal(0)
        if bins[item, warehouse] != sum(ledger.values(), Decimal(0)):
            raise StockError("RECEIPT_NATIVE_RECONCILIATION_FAILED")
    return buckets, bins


def _verify_posting(scope, before, before_batch, deltas, *, allow_gl):
    entry = scope.entry
    stored = frappe.get_doc(ENTRY, entry.name, for_update=True)
    if (stored.company, stored.purpose, stored.stock_entry_type, stored.docstatus, stored.owner) != (
        scope.company, PURPOSE, PURPOSE, 1, scope.actor,
    ) or {row.name for row in stored.items} != set(scope.details):
        raise StockError("RECEIPT_NATIVE_REFERENCE_INVALID")
    posted = frappe.db.get_values(
        SLE, {"voucher_no": entry.name, "voucher_type": ENTRY},
        ("voucher_detail_no", "item_code", "warehouse", "company", "serial_and_batch_bundle",
         "actual_qty", "docstatus", "is_cancelled"), as_dict=True, for_update=True,
    )
    by_detail = {row.voucher_detail_no: row for row in posted}
    if len(posted) != len(scope.details) or set(by_detail) != set(scope.details):
        raise StockError("RECEIPT_NATIVE_REFERENCE_INVALID")
    bundles = set()
    links = []
    for row in stored.items:
        leg = scope.details[row.name]
        sle = by_detail[row.name]
        if (
            not _leg(row, leg) or _number(row.transfer_qty) != leg.quantity
            or not row.serial_and_batch_bundle or row.serial_and_batch_bundle in bundles
            or (sle.item_code, sle.warehouse, sle.company, sle.serial_and_batch_bundle,
                _number(sle.actual_qty), sle.docstatus, sle.is_cancelled)
            != (leg.item, leg.warehouse, entry.company, row.serial_and_batch_bundle, leg.quantity, 1, 0)
        ):
            raise StockError("RECEIPT_NATIVE_REFERENCE_INVALID")
        bundle = frappe.get_doc(BUNDLE, row.serial_and_batch_bundle, for_update=True)
        if (
            (bundle.docstatus, bundle.voucher_type, bundle.voucher_no, bundle.voucher_detail_no, bundle.company)
            != (1, ENTRY, entry.name, row.name, entry.company)
            or not _bundle(bundle, leg)
        ):
            raise StockError("RECEIPT_NATIVE_REFERENCE_INVALID")
        bundles.add(bundle.name)
        links.append(NativeLink(leg.line_id, "Receipt", ENTRY, entry.name, DETAIL, row.name))

    if not allow_gl and frappe.db.get_value(
        "GL Entry", {"voucher_type": ENTRY, "voucher_no": entry.name}, for_update=True,
    ):
        raise StockError("RECEIPT_GL_NOT_ALLOWED")
    expected_buckets, expected_bins = before
    for (item, warehouse, batch), delta in deltas.items():
        expected_buckets[item, warehouse, batch] += delta
        expected_bins[item, warehouse] += delta
        before_batch[batch] += delta
    if (expected_buckets, expected_bins) != _balances(entry.company, expected_bins):
        raise StockError("RECEIPT_NATIVE_RECONCILIATION_FAILED")
    if before_batch != _batch_totals(before_batch):
        raise StockError("RECEIPT_NATIVE_RECONCILIATION_FAILED")
    return tuple(links)


def _batch_totals(names):
    return {
        name: _number(frappe.db.sql(
            "SELECT CAST(batch_qty AS CHAR) FROM `tabBatch` WHERE name=%s FOR UPDATE", (name,),
        )[0][0]) for name in sorted(names)
    }


def post(plan, *, rate, allow_gl):
    if not _native_orm_active.get():
        raise StockError("STOCK_ADAPTER_SIDE_EFFECT")
    legs = tuple(_posting_legs(plan))
    deltas = {(leg.item, leg.warehouse, leg.batch): plan.target_demand[leg.bucket] for leg in legs}
    if len(deltas) != len(plan.target_demand):
        raise StockError("RECEIPT_MAPPING_INVALID")
    company = plan.command.company
    entry_type = frappe.db.get_value("Stock Entry Type", PURPOSE, ("purpose", "add_to_transit"), for_update=True)
    if entry_type != (PURPOSE, 0):
        raise StockError("RECEIPT_NATIVE_CONFIGURATION")
    before = _balances(company, {(leg.item, leg.warehouse) for leg in legs})
    before_batch = _batch_totals({leg.batch for leg in legs})
    now = now_datetime()
    accounts = frappe.db.get_value(
        "Company", company, ("enable_perpetual_inventory", "stock_adjustment_account", "cost_center"),
        as_dict=True, for_update=True,
    )
    if accounts.enable_perpetual_inventory and (not accounts.stock_adjustment_account or not accounts.cost_center):
        raise StockError("RECEIPT_VALUATION_CONFIGURATION")
    entry = frappe.get_doc(
        doctype=ENTRY, stock_entry_type=PURPOSE, purpose=PURPOSE, company=company,
        naming_series="MAT-STE-.YYYY.-", posting_date=now.date(), posting_time=now.time(), set_posting_time=1,
    )
    for leg in legs:
        entry.append("items", {
            "item_code": leg.item, "t_warehouse": leg.warehouse,
            "qty": str(leg.quantity), "uom": leg.uom, "stock_uom": leg.uom, "conversion_factor": 1,
            "use_serial_batch_fields": 1, "batch_no": leg.batch,
            "set_basic_rate_manually": 1, "basic_rate": str(rate), "allow_zero_valuation_rate": int(rate == 0),
            "expense_account": accounts.stock_adjustment_account if accounts.enable_perpetual_inventory else None,
            "cost_center": accounts.cost_center if accounts.enable_perpetual_inventory else None,
        })
    scope = Posting(frappe.session.user, plan.command.document_id, company, entry, legs, {}, {})
    token = _scope.set(scope)
    try:
        entry.insert(ignore_permissions=True)
        scope.details.update((row.name, leg) for row, leg in zip(entry.items, legs, strict=True))
        entry.submit()
        return _verify_posting(scope, before, before_batch, deltas, allow_gl=allow_gl)
    finally:
        _scope.reset(token)


def verify_link(plan, link):
    return (
        link.role == "Receipt"
        and frappe.db.get_value(ENTRY, link.parent_id, ("company", "purpose", "docstatus"), for_update=True)
        == (plan.command.company, PURPOSE, 1)
    )
