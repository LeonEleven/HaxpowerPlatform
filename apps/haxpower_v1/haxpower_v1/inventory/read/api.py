"""Authenticated employee inventory RPCs."""

from decimal import localcontext

import frappe

from ..operations.dto import StockError
from ..operations.transaction import connection
from .documents import Documents
from .query import DOCUMENT_FILTERS, INVENTORY_FILTERS, MOVEMENT_FILTERS, PERIOD_FILTERS, Page, filters
from .service import Inventory, inventory_data


def _report(report, criteria, page, pageSize, limit, offset, *, allowed=INVENTORY_FILTERS):
    try:
        arguments = filters(criteria, allowed)
        pagination = Page.parse(page, pageSize, limit, offset)
        return inventory_data(report, arguments, pagination)[0]
    except StockError as error:
        frappe.throw("INVENTORY_READ_HISTORY_LIMIT" if error.code == "INVENTORY_READ_HISTORY_LIMIT"
                     else "INVENTORY_READ_UNAVAILABLE")
    except (frappe.QueryTimeoutError, frappe.QueryDeadlockError):
        frappe.throw("INVENTORY_READ_UNAVAILABLE")


@frappe.whitelist(methods=["GET"])
def stock(criteria=None, page: int = None, pageSize: int = None, limit: int = None, offset: int = None):
    return _report("stock", criteria, page, pageSize, limit, offset)


@frappe.whitelist(methods=["GET"])
def distribution(criteria=None, page: int = None, pageSize: int = None, limit: int = None, offset: int = None):
    return _report("distribution", criteria, page, pageSize, limit, offset)


@frappe.whitelist(methods=["GET"])
def batches(criteria=None, page: int = None, pageSize: int = None, limit: int = None, offset: int = None):
    return _report("batches", criteria, page, pageSize, limit, offset)


@frappe.whitelist(methods=["GET"])
def transit(criteria=None, page: int = None, pageSize: int = None, limit: int = None, offset: int = None):
    return _report("transit", criteria, page, pageSize, limit, offset)


@frappe.whitelist(methods=["GET"])
def ledger(criteria=None, page: int = None, pageSize: int = None, limit: int = None, offset: int = None):
    return _report("ledger", criteria, page, pageSize, limit, offset, allowed=MOVEMENT_FILTERS)


@frappe.whitelist(methods=["GET"])
def period(criteria=None, page: int = None, pageSize: int = None, limit: int = None, offset: int = None):
    return _report("period", criteria, page, pageSize, limit, offset, allowed=PERIOD_FILTERS)


@frappe.whitelist(methods=["GET"])
def documents(criteria=None, page: int = None, pageSize: int = None, limit: int = None, offset: int = None):
    return _report("documents", criteria, page, pageSize, limit, offset, allowed=DOCUMENT_FILTERS)


@frappe.whitelist(methods=["GET"])
def corrections(criteria=None, page: int = None, pageSize: int = None, limit: int = None, offset: int = None):
    return _report("corrections", criteria, page, pageSize, limit, offset, allowed=DOCUMENT_FILTERS)


def _detail(document_id, company, mode):
    try:
        with connection(snapshot=True), localcontext() as context:
            context.prec = 64
            service = Inventory({"company": company})
            detail = Documents(service)
            return (detail.detail(document_id) if mode == "document"
                    else detail.history(document_id, audit=mode == "audit"))
    except StockError as error:
        frappe.throw("INVENTORY_READ_HISTORY_LIMIT" if error.code == "INVENTORY_READ_HISTORY_LIMIT"
                     else "INVENTORY_READ_UNAVAILABLE")
    except (frappe.QueryTimeoutError, frappe.QueryDeadlockError):
        frappe.throw("INVENTORY_READ_UNAVAILABLE")


@frappe.whitelist(methods=["GET"])
def document(document_id: str, company: str = None):
    return _detail(document_id, company, "document")


@frappe.whitelist(methods=["GET"])
def history(document_id: str, company: str = None):
    return _detail(document_id, company, "history")


@frappe.whitelist(methods=["GET"])
def audit(document_id: str, company: str = None):
    return _detail(document_id, company, "audit")
