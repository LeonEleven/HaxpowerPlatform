"""Reviewed ERPNext v16 master and stock DocTypes."""

NATIVE_MASTERS = frozenset({"Item", "Warehouse", "Item Group", "UOM", "Warehouse Type"})

# Hooks guard the normal ORM lifecycle too. The HTTP allowlist independently
# keeps the entire native inventory surface closed to all interactive roles.
STOCK_DOCUMENTS = (
    "Stock Entry", "Stock Reconciliation", "Stock Ledger Entry", "Bin", "Batch", "Serial No",
    "Serial and Batch Bundle", "Stock Reservation Entry", "Material Request",
    "Purchase Order", "Purchase Receipt", "Purchase Invoice", "Purchase Invoice Item",
    "Sales Order", "Delivery Note", "Sales Invoice", "Subcontracting Receipt",
    "Subcontracting Inward Order", "Job Card", "Work Order", "Pick List",
)

MAINTENANCE_DOCUMENTS = ("Repost Item Valuation", "Transaction Deletion Record")
_REPOST = "erpnext.stock.doctype.repost_item_valuation.repost_item_valuation"
DISABLED_SCHEDULED_JOBS = (
    f"{_REPOST}.run_parallel_reposting",
    f"{_REPOST}.repost_entries",
    "erpnext.stock.doctype.stock_reposting_settings.stock_reposting_settings.repost_incorrect_valuation_entries",
    "erpnext.accounts.doctype.gl_entry.gl_entry.rename_gle_sle_docs",
    "erpnext.utilities.bulk_transaction.retry",
    "erpnext.stock.reorder_item.reorder_item",
    "erpnext.stock.doctype.serial_no.serial_no.update_maintenance_status",
)
DISABLED_WORKER_METHODS = (
    *DISABLED_SCHEDULED_JOBS,
    f"{_REPOST}.execute_reposting_entry",
    f"{_REPOST}.repost",
)
