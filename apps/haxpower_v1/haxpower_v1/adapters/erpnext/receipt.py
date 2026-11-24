"""ERPNext v16 Material Receipt with native quantity reconciliation."""

import frappe
from frappe.utils import now_datetime

from ...inventory.operations.dto import StockError
from ...inventory.operations.transaction import _native_orm_active
from .stock_entry import (
    BUNDLE, BUNDLE_ROW, DETAIL, ENTRY, SLE, RECEIPT as PURPOSE, Posting,
    _balances, _batch_totals, _scope, _verify_posting, posting_legs,
)


def post(plan, *, rate, allow_gl):
    if not _native_orm_active.get():
        raise StockError("STOCK_ADAPTER_SIDE_EFFECT")
    legs = tuple(posting_legs(plan))
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
