"""Shared stock-write lock for site-wide mappings and company operations."""

import frappe

from ..access.policy import deny
from ..adapters.erpnext.warehouse import _company

GUARD = "HP V1 Stock Write Guard"
GUARD_ID = "stock"


def lock_stock_writes(company=None):
    # SKU and Batch identities span Companies, so they share the site guard.
    if frappe.db.get_value(GUARD, GUARD_ID, for_update=True) != GUARD_ID:
        deny()
    if company is not None and not _company(company, locked=True):
        deny()
