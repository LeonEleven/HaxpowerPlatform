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
    transit.reconcile(company, {leg.item for leg in sources}, leaf, before[0])
    if any(before[0].get(key, Decimal(0)) < quantity for key, quantity in demand.items()):
        raise StockError("INSUFFICIENT_STOCK")

    return _post_entry(plan, stock.DISPATCH, sources, inward, before, batch_totals, now)


def _post_entry(plan, action, sources, inward, before, batch_totals, now, original_totals=None, original_percent=Decimal(0)):
    company = plan.command.company
    accounts = frappe.db.get_value(
        "Company", company, ("enable_perpetual_inventory", "stock_adjustment_account", "cost_center"),
        as_dict=True, for_update=True,
    )
    if accounts.enable_perpetual_inventory and (not accounts.stock_adjustment_account or not accounts.cost_center):
        raise StockError("TRANSFER_VALUATION_CONFIGURATION")
    entry = frappe.get_doc(
        doctype=ENTRY, stock_entry_type=stock.DISPATCH_TYPE if action == stock.DISPATCH else PURPOSE,
        purpose=PURPOSE, company=company, add_to_transit=int(action == stock.DISPATCH),
        outgoing_stock_entry=sources[0].original_entry,
        naming_series="MAT-STE-.YYYY.-", posting_date=now.date(), posting_time=now.time(), set_posting_time=1,
    )
    for leg in sources:
        entry.append("items", {
            "item_code": leg.item, "s_warehouse": leg.warehouse, "t_warehouse": leg.counterpart,
            "qty": str(leg.quantity.copy_abs()), "uom": leg.uom, "stock_uom": leg.uom, "conversion_factor": 1,
            "use_serial_batch_fields": 1, "batch_no": leg.batch, "allow_zero_valuation_rate": 1,
            "expense_account": accounts.stock_adjustment_account if accounts.enable_perpetual_inventory else None,
            "cost_center": accounts.cost_center if accounts.enable_perpetual_inventory else None,
            "ste_detail": leg.original_detail, "against_stock_entry": leg.original_entry,
        })
    scope = stock.Posting(
        frappe.session.user, plan.command.document_id, company, entry, sources, {}, {}, PURPOSE, inward,
        action, original_totals or {}, original_percent,
    )
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


def _original_snapshot(entry, *, cumulative=False):
    data = entry.as_dict()
    if cumulative:
        for field in ("per_transferred", "modified", "modified_by"):
            data.pop(field, None)
        for row in data["items"]:
            row.pop("transferred_qty", None)
    return data


def post_followup(plan, action):
    if not _native_orm_active.get():
        raise StockError("STOCK_ADAPTER_SIDE_EFFECT")
    company, originals = plan.command.company, plan.context
    skus = plan.skus.values()
    require_native_precision(skus, "TRANSFER_NATIVE_PRECISION_UNCONFIGURED")
    if frappe.db.get_value(
        "Stock Entry Type", PURPOSE, ("purpose", "add_to_transit"), for_update=True,
    ) != (PURPOSE, 0):
        raise StockError("TRANSFER_NATIVE_CONFIGURATION")
    inward = {leg.line_id: leg for leg in stock.posting_legs(plan)}
    groups = defaultdict(list)
    for leg in stock.posting_legs(plan, source=True):
        original = originals[leg.line_id]
        receive = action == stock.RECEIVE
        groups[original.native_entry if receive else None].append(replace(
            leg, counterpart=inward[leg.line_id].warehouse,
            original_entry=original.native_entry if receive else None,
            original_detail=original.native_detail if receive else None,
            ownership_id=original.ownership_id,
        ))
    check_negative_stock(tuple(leg for group in groups.values() for leg in group), "TRANSFER_NATIVE_NEGATIVE_STOCK_ENABLED")
    outgoing = {name: frappe.get_doc(ENTRY, name, for_update=True) for name in sorted({
        original.native_entry for original in originals.values()
    })}
    snapshots = {name: _original_snapshot(doc, cumulative=action == stock.RECEIVE) for name, doc in outgoing.items()}

    links, expected_totals = [], {}
    for outgoing_id, group in groups.items():
        sources = tuple(group)
        incoming = {
            leg.line_id: replace(inward[leg.line_id], counterpart=leg.warehouse) for leg in sources
        }
        pairs = {
            (leg.item, warehouse) for leg in sources for warehouse in (
                leg.warehouse, originals[leg.line_id].source_leaf, originals[leg.line_id].target_leaf,
            )
        }
        now = now_datetime()
        before, batch_totals = availability.read_source(company, pairs, skus, posting_at=now)
        demand = defaultdict(Decimal)
        for leg in sources:
            demand[leg.item, leg.warehouse, leg.batch] += leg.quantity.copy_abs()
        if any(before[0].get(key, Decimal(0)) < qty for key, qty in demand.items()):
            raise StockError("TRANSFER_TRANSIT_NATIVE_INSUFFICIENT")
        cumulative = {}
        percent = Decimal(0)
        if outgoing_id:
            entry = outgoing[outgoing_id]
            amounts = {leg.original_detail: leg.quantity.copy_abs() for leg in sources}
            expected_totals[outgoing_id] = totals = {
                row.name: stock._number(row.transferred_qty) + amounts.get(row.name, Decimal(0)) for row in entry.items
            }
            cumulative = {name: totals[name] for name in amounts}
            percent = stock.transfer_percent(
                sum(stock._number(row.transfer_qty) for row in entry.items), sum(totals.values()),
            )
        links.extend(_post_entry(
            plan, action, sources, incoming, before, batch_totals, now,
            original_totals=cumulative, original_percent=percent,
        ))

    for name, snapshot in snapshots.items():
        original = frappe.get_doc(ENTRY, name, for_update=True)
        if _original_snapshot(original, cumulative=action == stock.RECEIVE) != snapshot:
            raise StockError("TRANSFER_ORIGINAL_DISPATCH_CHANGED")
        if name in expected_totals and {
            row.name: stock._number(row.transferred_qty) for row in original.items
        } != expected_totals[name]:
            raise StockError("RECEIVE_NATIVE_CUMULATIVE_INVALID")
    return tuple(links)


def verify_followup(plan, link, action):
    original = plan.context[link.line_id]
    outgoing = original.native_entry if action == stock.RECEIVE else None
    entry = frappe.db.get_value(
        ENTRY, link.parent_id, ("company", "purpose", "docstatus", "add_to_transit", "outgoing_stock_entry"),
        for_update=True,
    )
    return (
        link.role == action and link.ownership_id == original.ownership_id and entry
        and tuple(entry[:4]) == (plan.command.company, PURPOSE, 1, 0) and (entry[4] or None) == outgoing
    )


def verify_link(plan, link):
    return (
        link.role == "Dispatch"
        and frappe.db.get_value(
            ENTRY, link.parent_id, ("company", "purpose", "docstatus", "add_to_transit"), for_update=True,
        ) == (plan.command.company, PURPOSE, 1, 1)
    )
