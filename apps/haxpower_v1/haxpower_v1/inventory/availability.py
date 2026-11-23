"""Current physical availability from native quantity records under the stock guard."""

from dataclasses import dataclass
from decimal import Decimal

import frappe

from ..access import policy
from ..adapters.erpnext import batch as native_batch, stock_entry as stock
from .batch.dto import BatchError, MappingStatus
from .batch.persistence import MAPPING, mappings
from .batch.service import _load_sku, _resolve_existing
from .guard import lock_stock_writes
from .operations.dto import StockError
from .operations.transaction import connection
from .posting import require_native_precision, selection

RECONCILIATION_FAILED = "ISSUE_NATIVE_RECONCILIATION_FAILED"


@dataclass(frozen=True)
class Availability:
    company: str
    sku_id: str
    location_id: str
    batch: str | None
    quantity: str
    uom: str


def resolve_existing(sku, parsed):
    if not sku.code.startswith("SYNTHETIC-"):
        policy.deny()
    try:
        batch = _resolve_existing(sku, parsed, locked=True).batch
    except BatchError as error:
        raise StockError(error.code) from None
    return batch


def _batch_totals(company, pairs, batch_items, bucket_totals):
    """Corroborate the leaf projection and global Batch totals from submitted rows."""
    native_totals = stock._batch_totals(batch_items)
    for batch, item in sorted(batch_items.items()):
        rows = frappe.db.sql(
            """
            SELECT e.name, CAST(e.qty AS CHAR), e.item_code, e.warehouse, b.company, b.item_code,
                   b.warehouse, b.type_of_transaction, b.voucher_type, b.voucher_no, b.voucher_detail_no,
                   s.name, s.company, s.item_code, s.warehouse, s.voucher_type, s.voucher_no, s.voucher_detail_no
            FROM `tabSerial and Batch Entry` e
            JOIN `tabSerial and Batch Bundle` b ON b.name = e.parent
            LEFT JOIN `tabStock Ledger Entry` s
              ON s.serial_and_batch_bundle = b.name AND s.docstatus = 1 AND s.is_cancelled = 0
            WHERE e.batch_no = %s AND e.docstatus = 1 AND e.is_cancelled = 0
              AND b.docstatus = 1 AND b.is_cancelled = 0
            FOR UPDATE
            """, (batch,),
        )
        seen, by_leaf = set(), {}
        for (name, raw_qty, part_item, part_leaf, bundle_company, bundle_item, bundle_leaf, direction,
             voucher_type, voucher, detail, sle, *ledger_ref) in rows:
            qty = stock._number(raw_qty)
            if (
                not sle or not part_leaf or not bundle_company or not voucher_type or not voucher or not detail
                or name in seen or part_item != item or bundle_item != item
                or (bundle_company, item, part_leaf, voucher_type, voucher, detail) != tuple(ledger_ref)
                or bundle_leaf != part_leaf or direction not in ("Inward", "Outward")
                or (direction == "Inward" and qty < 0) or (direction == "Outward" and qty > 0)
            ):
                raise StockError(RECONCILIATION_FAILED)
            seen.add(name)
            key = bundle_company, part_leaf
            by_leaf[key] = by_leaf.get(key, Decimal(0)) + qty
        if (
            any(qty < 0 for qty in by_leaf.values())
            or native_totals[batch] != sum(by_leaf.values(), Decimal(0))
            or any(
                bucket_totals.get((item, leaf, batch), Decimal(0)) != by_leaf.get((company, leaf), Decimal(0))
                for pair_item, leaf in pairs if pair_item == item
            )
        ):
            raise StockError(RECONCILIATION_FAILED)
    return native_totals


def read_source(company, pairs, skus, *, posting_at=None):
    """Read and reconcile every batch in the source pairs in the caller's transaction."""
    pairs = set(pairs)
    scales = {sku.native_item: Decimal(1).scaleb(-sku.quantity_precision) for sku in skus}
    buckets, bins = stock._balances(
        company, pairs, error=RECONCILIATION_FAILED, posting_at=posting_at, scales=scales,
    )
    batch_items = {}
    for sku in skus:
        by_name = {}
        for row in mappings({"sku": sku.name}, locked=True):
            if native_batch.mapping_status(row, sku, locked=True) != MappingStatus.VERIFIED:
                raise StockError("BATCH_MAPPING_UNAVAILABLE")
            by_name[row.native_batch] = sku.native_item
        if any(batch not in by_name for item, _, batch in buckets if item == sku.native_item):
            raise StockError("BATCH_MAPPING_UNAVAILABLE")
        batch_items.update(by_name)
    if any(qty < 0 for qty in buckets.values()):
        raise StockError(RECONCILIATION_FAILED)
    return (buckets, bins), _batch_totals(company, pairs, batch_items, buckets)


def get_available(company, sku_id, location_id, payload, *, authorize):
    """Return a single authorized domain bucket and its physical quantity."""
    try:
        with connection():
            lock_stock_writes(company)
            policy.require_operation(policy.Operation.ISSUE, locked=True)
            location = policy.lock_execution_locations(company, (location_id,))[location_id]
            authorize(company)
            sku = _load_sku(sku_id, locked=True)
            selected = resolve_existing(sku, selection(payload))
            require_native_precision((sku,), "ISSUE_NATIVE_PRECISION_UNCONFIGURED")
            batch = frappe.db.get_value(MAPPING, selected.id, "native_batch", for_update=True)
            pair = sku.native_item, location.native_warehouse
            (buckets, _), _ = read_source(company, (pair,), (sku,))
            quantity = buckets.get((*pair, batch), Decimal(0))
            return Availability(company, sku_id, location_id, selected.batch, format(quantity, "f"), sku.stock_uom)
    except BatchError as error:
        raise StockError(error.code) from None
    except (frappe.QueryDeadlockError, frappe.QueryTimeoutError):
        raise StockError("STOCK_RETRY_REQUIRED") from None
    except (StockError, frappe.PermissionError):
        raise
    except Exception:
        raise StockError("STOCK_PERSISTENCE_FAILED") from None
