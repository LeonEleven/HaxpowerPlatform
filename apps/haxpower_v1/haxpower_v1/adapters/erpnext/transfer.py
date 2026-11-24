"""Dispatch against native source and transit legs of the same Stock Entry Detail."""

from collections import defaultdict
from dataclasses import replace
from decimal import Decimal

import frappe
from frappe.utils import now_datetime

from ...inventory import availability
from ...inventory.operations.dto import StockError
from ...inventory.operations.transaction import _native_orm_active
from ...inventory.posting import require_native_precision
from ...inventory.transfer import transit
from . import stock_entry as stock
from .issue import check_negative_stock

ENTRY, DETAIL, PURPOSE = stock.ENTRY, stock.DETAIL, stock.TRANSFER
NATIVE_ERROR = "DISPATCH_NATIVE_REFERENCE_INVALID"


def _verify_valuation(entry):
    precision = entry.get_debit_field_precision()
    quantum = Decimal(1).scaleb(-precision)
    values = defaultdict(Decimal)
    for detail, actual, value in frappe.db.sql(
        """
        SELECT voucher_detail_no, CAST(actual_qty AS CHAR), CAST(stock_value_difference AS CHAR)
        FROM `tabStock Ledger Entry` WHERE voucher_type = 'Stock Entry' AND voucher_no = %s
        FOR UPDATE
        """, (entry.name,),
    ):
        qty, amount = stock._number(actual), stock._number(value)
        if qty and amount and (qty > 0) != (amount > 0):
            raise StockError("TRANSFER_VALUATION_INCONSISTENT")
        values[detail] += amount.quantize(quantum)
    if any(values.values()):
        raise StockError("TRANSFER_VALUATION_INCONSISTENT")


def _check_configuration(sources):
    entry_type = frappe.db.get_value(
        "Stock Entry Type", stock.DISPATCH_TYPE, ("purpose", "add_to_transit", "is_standard"), for_update=True,
    )
    if entry_type != (PURPOSE, 1, 0):
        raise StockError("TRANSFER_NATIVE_CONFIGURATION")
    check_negative_stock(sources, "TRANSFER_NATIVE_NEGATIVE_STOCK_ENABLED")


def post(plan):
    if not _native_orm_active.get():
        raise StockError("STOCK_ADAPTER_SIDE_EFFECT")
    company = plan.command.company
    skus = plan.skus.values()
    require_native_precision(skus, "TRANSFER_NATIVE_PRECISION_UNCONFIGURED")
    leaf = transit.resolve(company)
    sources = tuple(replace(leg, counterpart=leaf.native_warehouse) for leg in stock.posting_legs(plan, source=True))
    inward = {leg.line_id: replace(
        leg, warehouse=leaf.native_warehouse, counterpart=leg.warehouse, quantity=leg.quantity.copy_abs(),
        bucket=replace(leg.bucket, location_id=leaf.name),
    ) for leg in sources}
    _check_configuration(sources)
    demand = {(leg.item, leg.warehouse, leg.batch): plan.source_demand[leg.bucket] for leg in sources}
    if len(demand) != len(plan.source_demand):
        raise StockError(NATIVE_ERROR)
    pairs = {(leg.item, leg.warehouse) for leg in (*sources, *inward.values())}
    pairs.update((plan.skus[line.sku_id].native_item, plan.locations[line.target].native_warehouse)
                 for line in plan.command.lines)
    now = now_datetime()
    before, batch_totals = availability.read_source(company, pairs, skus, posting_at=now)
    transit.active_inward(company, {leg.item for leg in sources}, leaf, before[0])
    if any(before[0].get(key, Decimal(0)) < quantity for key, quantity in demand.items()):
        raise StockError("INSUFFICIENT_STOCK")

    accounts = frappe.db.get_value(
        "Company", company, ("enable_perpetual_inventory", "stock_adjustment_account", "cost_center"),
        as_dict=True, for_update=True,
    )
    if accounts.enable_perpetual_inventory and (not accounts.stock_adjustment_account or not accounts.cost_center):
        raise StockError("TRANSFER_VALUATION_CONFIGURATION")
    entry = frappe.get_doc(
        doctype=ENTRY, stock_entry_type=stock.DISPATCH_TYPE, purpose=PURPOSE, company=company, add_to_transit=1,
        naming_series="MAT-STE-.YYYY.-", posting_date=now.date(), posting_time=now.time(), set_posting_time=1,
    )
    for leg in sources:
        entry.append("items", {
            "item_code": leg.item, "s_warehouse": leg.warehouse, "t_warehouse": leaf.native_warehouse,
            "qty": str(leg.quantity.copy_abs()), "uom": leg.uom, "stock_uom": leg.uom, "conversion_factor": 1,
            "use_serial_batch_fields": 1, "batch_no": leg.batch, "allow_zero_valuation_rate": 1,
            "expense_account": accounts.stock_adjustment_account if accounts.enable_perpetual_inventory else None,
            "cost_center": accounts.cost_center if accounts.enable_perpetual_inventory else None,
        })
    scope = stock.Posting(frappe.session.user, plan.command.document_id, company, entry, sources, {}, {}, PURPOSE, inward)
    token = stock._scope.set(scope)
    try:
        entry.insert(ignore_permissions=True)
        scope.details.update((row.name, leg) for row, leg in zip(entry.items, sources, strict=True))
        entry.submit()
        deltas = defaultdict(Decimal)
        for leg in (*sources, *inward.values()):
            deltas[leg.item, leg.warehouse, leg.batch] += leg.quantity
        links = stock._verify_posting(scope, before, batch_totals, deltas, allow_gl=False)
        _verify_valuation(entry)
        return links
    finally:
        stock._scope.reset(token)


def verify_link(plan, link):
    return (
        link.role == "Dispatch"
        and frappe.db.get_value(
            ENTRY, link.parent_id, ("company", "purpose", "docstatus", "add_to_transit"), for_update=True,
        ) == (plan.command.company, PURPOSE, 1, 1)
    )
