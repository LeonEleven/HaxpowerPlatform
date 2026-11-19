"""Read-only stock evidence for the Stage 1E lab."""
import json
import sys
from collections import defaultdict
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

import frappe
from erpnext.stock.doctype.serial_and_batch_bundle.serial_and_batch_bundle import get_batch_qty_for_stock

from stage1b_verify import ROOT, new_connection, require
from stage1c_verify import grants_snapshot


def decimal(value):
    return Decimal(str(value or 0))


def rows(doctype, **filters):
    return frappe.get_all(doctype, filters=filters, fields=["*"], order_by="name")


def children(doctype, parents):
    return rows(doctype, parent=["in", [row.name for row in parents]]) if parents else []


def records(company):
    stock = rows("Stock Entry", company=company)
    counts = rows("Stock Reconciliation", company=company)
    bundles = rows("Serial and Batch Bundle", company=company)
    warehouses = rows("Warehouse", company=company)
    items = sorted({row.item_code for row in children("Stock Entry Detail", stock)})
    return {
        "Stock Entry": stock, "Stock Entry Detail": children("Stock Entry Detail", stock),
        "Stock Reconciliation": counts, "Stock Reconciliation Item": children("Stock Reconciliation Item", counts),
        "Serial and Batch Bundle": bundles, "Serial and Batch Entry": children("Serial and Batch Entry", bundles),
        "Stock Ledger Entry": rows("Stock Ledger Entry", company=company),
        "Bin": rows("Bin", warehouse=["in", [row.name for row in warehouses]]) if warehouses else [],
        "Batch": rows("Batch", item=["in", items]) if items else [],
        "GL Entry": rows("GL Entry", company=company),
        "Repost Item Valuation": rows("Repost Item Valuation", company=company),
    }


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def history():
    historical = {}
    for company, entries, ledger in (("HP Stage 1B Lab", 8, 8), ("HP Stage 1D Lab", 12, 18)):
        data = records(company)
        require(len(data["Stock Entry"]) == entries and all(row.docstatus == 1 for row in data["Stock Entry"])
                and len(data["Stock Ledger Entry"]) == ledger and len(data["Serial and Batch Bundle"]) == ledger,
                f"Historical fixture incomplete: {company}")
        historical[company] = {doctype: {"rows": len(values), "sha256": digest(values)}
                               for doctype, values in data.items()}
    historical["stage1c_grants"] = digest(grants_snapshot())
    return historical


def reconcile(company):
    data = records(company)
    sle = [row for row in data["Stock Ledger Entry"] if not row.is_cancelled]
    bundles = {row.name: row for row in data["Serial and Batch Bundle"]}
    entries = defaultdict(list)
    for row in data["Serial and Batch Entry"]:
        entries[row.parent].append(row)
    totals, buckets = defaultdict(Decimal), defaultdict(Decimal)
    for row in sle:
        bundle = bundles[row.serial_and_batch_bundle]
        lines = entries[bundle.name]
        require(bundle.docstatus == 1 and row.warehouse == bundle.warehouse
                and row.item_code == bundle.item_code and row.voucher_no == bundle.voucher_no
                and decimal(row.actual_qty) == sum(decimal(line.qty) for line in lines),
                f"Bundle/SLE mismatch: {row.name}")
        totals[row.item_code, row.warehouse] += decimal(row.actual_qty)
        for line in lines:
            buckets[row.item_code, row.warehouse, line.batch_no] += decimal(line.qty)
    bins = {(row.item_code, row.warehouse): decimal(row.actual_qty) for row in data["Bin"]}
    for key in totals.keys() | bins.keys():
        require(totals[key] == bins.get(key, 0) >= 0, f"Bin/SLE mismatch: {key}")
    for (item, warehouse, batch), qty in buckets.items():
        native = get_batch_qty_for_stock(item_code=item, warehouse=warehouse, batch_no=batch,
                                         for_stock_levels=True, ignore_reserved_stock=True)
        require(decimal(native) == qty >= 0, f"Batch/SLE mismatch: {item} {warehouse} {batch}")
    require(not data["GL Entry"], "Unexpected GL entries")
    return {
        "balances": [{"item": item, "warehouse": wh, "batch": batch, "quantity": qty}
                     for (item, wh, batch), qty in sorted(buckets.items())],
        "bins": [{"item": item, "warehouse": wh, "quantity": qty} for (item, wh), qty in sorted(bins.items())],
        "documents": [{key: row.get(key) for key in ("name", "docstatus", "posting_date", "posting_time",
                       "outgoing_stock_entry", "amended_from", "remarks")}
                      for row in data["Stock Entry"] + data["Stock Reconciliation"]],
        "ledger": [{key: row[key] for key in ("name", "actual_qty", "qty_after_transaction", "is_cancelled",
                    "voucher_no", "voucher_detail_no", "item_code", "warehouse", "serial_and_batch_bundle",
                    "stock_value", "creation", "modified")} for row in data["Stock Ledger Entry"]],
        "bundles": [{key: row[key] for key in ("name", "docstatus", "total_qty", "is_cancelled", "voucher_no",
                     "warehouse", "voucher_detail_no", "type_of_transaction")} for row in bundles.values()],
        "bundle_entries": [{key: row[key] for key in ("name", "parent", "batch_no", "qty", "docstatus")}
                           for lines in entries.values() for row in lines],
        "reposts": [{key: row.get(key) for key in ("name", "voucher_type", "voucher_no", "status")}
                    for row in data["Repost Item Valuation"]],
        "gl_rows": len(data["GL Entry"]),
    }


if __name__ == "__main__":
    require(Path.cwd() == ROOT, "Wrong Bench directory")
    require(len(sys.argv) == 1, "Unexpected arguments")
    new_connection()
    print(json.dumps({"history": history(), "database": frappe.db.sql("select version(), @@tx_isolation")[0],
                      "apps": frappe.get_installed_apps()}, indent=2))
    frappe.destroy()
