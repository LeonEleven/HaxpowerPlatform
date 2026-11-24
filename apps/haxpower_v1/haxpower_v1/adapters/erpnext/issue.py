"""Post a Material Issue with explicit native Batch binding for each source leg."""

from decimal import Decimal

import frappe
from frappe.utils import now_datetime

from ...inventory import availability
from ...inventory.operations.dto import StockError
from ...inventory.operations.transaction import _native_orm_active
from ...inventory.posting import require_native_precision
from .stock_entry import DETAIL, ENTRY, ISSUE as PURPOSE, Posting, _number, _scope, _verify_posting, posting_legs


def _check_configuration(legs):
    entry_type = frappe.db.get_value("Stock Entry Type", PURPOSE, ("purpose", "add_to_transit"), for_update=True)
    if entry_type != (PURPOSE, 0):
        raise StockError("ISSUE_NATIVE_CONFIGURATION")
    settings = frappe.db.sql(
        """
        SELECT field, value FROM tabSingles
        WHERE doctype = 'Stock Settings' AND field IN ('allow_negative_stock', 'allow_negative_stock_for_batch')
        FOR UPDATE
        """,
    )
    if (
        any(value not in (None, "", "0") for _, value in settings)
        or any(frappe.db.get_value("Item", item, "allow_negative_stock", for_update=True)
               for item in sorted({leg.item for leg in legs}))
        or any(frappe.db.get_value("Batch", batch, "allow_negative_stock_for_batch", for_update=True)
               for batch in sorted({leg.batch for leg in legs}))
    ):
        raise StockError("ISSUE_NATIVE_NEGATIVE_STOCK_ENABLED")


def _verify_gl(entry, *, perpetual, allow_gl):
    if not allow_gl:
        return  # Shared posting verification already rejects any GL entry.
    rows = frappe.db.get_values(
        "GL Entry", {"voucher_type": ENTRY, "voucher_no": entry.name},
        ("company", "is_cancelled", "debit", "credit"), as_dict=True, for_update=True,
    )
    if (
        (rows and not perpetual)
        or any(row.company != entry.company or row.is_cancelled or min(_number(row.debit), _number(row.credit)) < 0
               or bool(row.debit) == bool(row.credit) for row in rows)
        or sum((_number(row.debit) - _number(row.credit) for row in rows), Decimal(0)) != 0
    ):
        raise StockError("ISSUE_GL_INCONSISTENT")


def post(plan, *, allow_gl):
    if not _native_orm_active.get():
        raise StockError("STOCK_ADAPTER_SIDE_EFFECT")
    skus = plan.skus.values()
    require_native_precision(skus, "ISSUE_NATIVE_PRECISION_UNCONFIGURED")
    legs = tuple(posting_legs(plan, source=True))
    demand = {(leg.item, leg.warehouse, leg.batch): plan.source_demand[leg.bucket] for leg in legs}
    if len(demand) != len(plan.source_demand):
        raise StockError("ISSUE_MAPPING_INVALID")
    _check_configuration(legs)
    company = plan.command.company
    pairs = {(leg.item, leg.warehouse) for leg in legs}
    now = now_datetime()
    before, batch_totals = availability.read_source(company, pairs, skus, posting_at=now)
    if any(before[0].get(key, Decimal(0)) < quantity for key, quantity in demand.items()):
        raise StockError("INSUFFICIENT_STOCK")

    accounts = frappe.db.get_value(
        "Company", company, ("enable_perpetual_inventory", "stock_adjustment_account", "cost_center"),
        as_dict=True, for_update=True,
    )
    perpetual = accounts.enable_perpetual_inventory
    if perpetual and (not accounts.stock_adjustment_account or not accounts.cost_center):
        raise StockError("ISSUE_VALUATION_CONFIGURATION")
    entry = frappe.get_doc(
        doctype=ENTRY, stock_entry_type=PURPOSE, purpose=PURPOSE, company=company,
        naming_series="MAT-STE-.YYYY.-", posting_date=now.date(), posting_time=now.time(), set_posting_time=1,
    )
    for leg in legs:
        entry.append("items", {
            "item_code": leg.item, "s_warehouse": leg.warehouse,
            "qty": str(leg.quantity.copy_abs()), "uom": leg.uom, "stock_uom": leg.uom, "conversion_factor": 1,
            "use_serial_batch_fields": 1, "batch_no": leg.batch, "allow_zero_valuation_rate": 1,
            "expense_account": accounts.stock_adjustment_account if perpetual else None,
            "cost_center": accounts.cost_center if perpetual else None,
        })
    scope = Posting(frappe.session.user, plan.command.document_id, company, entry, legs, {}, {}, PURPOSE)
    token = _scope.set(scope)
    try:
        entry.insert(ignore_permissions=True)
        scope.details.update((row.name, leg) for row, leg in zip(entry.items, legs, strict=True))
        entry.submit()
        deltas = {key: qty.copy_negate() for key, qty in demand.items()}
        links = _verify_posting(scope, before, batch_totals, deltas, allow_gl=allow_gl)
        availability.read_source(company, pairs, skus)
        _verify_gl(entry, perpetual=perpetual, allow_gl=allow_gl)
        return links
    finally:
        _scope.reset(token)


def verify_link(plan, link):
    return (
        link.role == "Issue"
        and frappe.db.get_value(ENTRY, link.parent_id, ("company", "purpose", "docstatus"), for_update=True)
        == (plan.command.company, PURPOSE, 1)
    )
