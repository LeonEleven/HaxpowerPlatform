"""Exercise the pinned ERPNext End Transit workflow before loading the transfer adapter."""

import json
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import frappe
from erpnext.stock.doctype.serial_and_batch_bundle.serial_and_batch_bundle import get_batch_qty_for_stock
from erpnext.stock.doctype.stock_entry.stock_entry import make_stock_in_entry
from frappe.utils import strip_html

from stage1b_verify import ROOT, new_connection, require
from stage1c_verify import fingerprint


def insert(doctype, **fields):
    return frappe.get_doc({"doctype": doctype, **fields}).insert()


def make_company(name, abbr):
    return insert("Company", company_name=name, abbr=abbr, country="China", default_currency="CNY",
                  valuation_method="FIFO", enable_perpetual_inventory=0,
                  enable_provisional_accounting_for_non_stock_items=0).name


def make_warehouses(company, prefix):
    root = frappe.db.get_value("Warehouse", {"company": company, "is_group": 1}, "name")
    w1 = insert("Warehouse", warehouse_name=prefix + " W1", company=company,
                parent_warehouse=root, is_group=1).name
    w2 = insert("Warehouse", warehouse_name=prefix + " W2", company=company,
                parent_warehouse=root, is_group=1).name
    return {
        key: insert("Warehouse", warehouse_name=prefix + " " + label, company=company,
                    parent_warehouse=parent, warehouse_type=kind).name
        for key, label, parent, kind in (
            ("source", "W1 L1", w1, None), ("other", "W1 L2", w1, None),
            ("target", "W2 L1", w2, None), ("transit", "Transit", root, "Transit"),
        )
    }


def make_item(code):
    return insert("Item", item_code=code, item_name=code, item_group="All Item Groups",
                  stock_uom="PC", has_batch_no=1, is_stock_item=1, create_new_batch=0).name


def stock_doc(company, item, key, qty, purpose, *, source=None, target=None, dispatch=False):
    return frappe.get_doc({
        "doctype": "Stock Entry", "company": company, "stock_entry_type": purpose,
        "add_to_transit": int(dispatch), "from_warehouse": source, "to_warehouse": target,
        "items": [{"item_code": item, "qty": Decimal(qty), "uom": "PC", "stock_uom": "PC",
                   "conversion_factor": 1, "s_warehouse": source, "t_warehouse": target,
                   "batch_no": key, "use_serial_batch_fields": 1, "allow_zero_valuation_rate": 1}],
    })


def post(doc):
    doc.insert()
    doc.submit()
    return doc


def qty_at(item, key, warehouse):
    return Decimal(str(get_batch_qty_for_stock(batch_no=key, warehouse=warehouse, item_code=item,
                                               for_stock_levels=True, ignore_reserved_stock=True)))


def stock_evidence(company, item, key, other_key, locations, expected, voucher=None):
    amounts = {place: qty_at(item, key, locations[place]) for place in ("source", "transit", "target", "other")}
    require(tuple(amounts.values()) == tuple(Decimal(x) for x in (*expected, 0)), "Unexpected batch quantities")
    require(sum(amounts.values()) == 12, "Stock not conserved")
    other = {place: qty_at(item, other_key, wh) for place, wh in locations.items()}
    require(other == {"source": 20, "other": 2, "target": 0, "transit": 0}, "Other batch changed")
    for place, wh in locations.items():
        bin_qty = frappe.db.get_value("Bin", {"item_code": item, "warehouse": wh}, "actual_qty") or 0
        require(Decimal(str(bin_qty)) == amounts[place] + other[place], "Bin mismatch")
    require(not frappe.db.count("GL Entry", {"company": company}), "Unexpected GL entries")
    ledger = []
    if voucher:
        for row in frappe.get_all(
            "Stock Ledger Entry", filters={"voucher_no": voucher.name}, order_by="actual_qty",
            fields=["name", "warehouse", "actual_qty", "serial_and_batch_bundle", "valuation_rate",
                    "stock_value", "stock_value_difference", "voucher_detail_no"],
        ):
            require(row.voucher_detail_no == voucher.items[0].name, "SLE row mismatch")
            bundle = frappe.get_doc("Serial and Batch Bundle", row.serial_and_batch_bundle)
            require(bundle.docstatus == 1 and bundle.warehouse == row.warehouse
                    and len(bundle.entries) == 1 and bundle.entries[0].batch_no == key
                    and Decimal(str(bundle.entries[0].qty)) == Decimal(str(row.actual_qty)), "Bundle mismatch")
            require(not any(row[f] for f in ("valuation_rate", "stock_value", "stock_value_difference")),
                    "Unexpected valuation")
            ledger.append({**row, "batch": key, "direction": bundle.type_of_transaction})
    return {"balances": amounts, "other_batch": other, "ledger": ledger,
            "voucher": voucher.name if voucher else None, "voucher_row": voucher.items[0].name if voucher else None}


def receipt_doc(dispatch, qty, target, key):
    doc = make_stock_in_entry(dispatch.name)
    require(len(doc.items) == 1, "Native mapper did not return one receipt row")
    row = doc.items[0]
    require((doc.outgoing_stock_entry, row.against_stock_entry, row.ste_detail)
            == (dispatch.name, dispatch.name, dispatch.items[0].name), "Missing native receipt links")
    mapped = {"qty": row.qty, "batch": row.batch_no}
    require(row.batch_no == key, "Native mapper did not carry the selected Batch")
    doc.to_warehouse = target
    row.update({"t_warehouse": target, "qty": Decimal(qty), "allow_zero_valuation_rate": 1})
    return doc, mapped


def probe(name, operation):
    original = fingerprint(fresh=False)
    frappe.db.savepoint("stage1d_probe")
    try:
        result = {"name": name, "accepted": True, "voucher": operation().name}
    except frappe.ValidationError as error:
        result = {"name": name, "accepted": False, "error_type": type(error).__name__,
                  "error": strip_html(str(error))}
    finally:
        frappe.db.rollback(save_point="stage1d_probe")
        frappe.db.release_savepoint("stage1d_probe")
    require(fingerprint(fresh=False) == original, "Probe changed inventory after savepoint rollback")
    return result


def run_group(company, locations, run, group, number):
    item = make_item(f"{run}-NATIVE-{group}")
    key = f"{run}-{group}-{number or 'INTERNAL'}"
    other = f"{run}-{group}-STD62"
    for name in (key, other):
        insert("Batch", item=item, batch_id=name)
    for batch, qty, place in ((key, "12", "source"), (other, "20", "source"), (other, "2", "other")):
        post(stock_doc(company, item, batch, qty, "Material Receipt", target=locations[place]))
    stages = {"initial": stock_evidence(company, item, key, other, locations, (12, 0, 0))}
    dispatch = post(stock_doc(company, item, key, "10", "Material Transfer",
                              source=locations["source"], target=locations["transit"], dispatch=True))
    stages["dispatch"] = stock_evidence(company, item, key, other, locations, (2, 10, 0), dispatch)
    stages["dispatch"]["percent"] = dispatch.reload().per_transferred
    negatives = [probe("ordinary_material_issue_from_transit", lambda: post(stock_doc(
        company, item, key, "1", "Material Issue", source=locations["transit"],
    )))]
    first, mapped = receipt_doc(dispatch, "6", locations["target"], key)
    require(Decimal(str(mapped["qty"])) == 10, "Initial mapper quantity mismatch")
    post(first)
    stages["partial"] = stock_evidence(company, item, key, other, locations, (2, 4, 6), first)
    stages["partial"].update(percent=dispatch.reload().per_transferred, mapped=mapped,
                             source_row=first.items[0].ste_detail)
    negatives.append(probe("over_receipt_with_only_4_in_transit",
                           lambda: post(receipt_doc(dispatch, "5", locations["target"], key)[0])))

    def over_with_extra_stock():
        post(stock_doc(company, item, key, "1", "Material Receipt", target=locations["transit"]))
        return post(receipt_doc(dispatch, "5", locations["target"], key)[0])

    negatives.append(probe("over_receipt_with_5_available_in_transit", over_with_extra_stock))
    negatives.append(probe("receipt_different_destination",
                           lambda: post(receipt_doc(dispatch, "1", locations["other"], key)[0])))

    def other_batch():
        post(stock_doc(company, item, other, "1", "Material Receipt", target=locations["transit"]))
        doc, _ = receipt_doc(dispatch, "1", locations["target"], key)
        doc.items[0].batch_no = other
        return post(doc)

    negatives.append(probe("receipt_different_batch", other_batch))
    second, mapped = receipt_doc(dispatch, "4", locations["target"], key)
    require(Decimal(str(mapped["qty"])) == 4, "Partial mapper did not return remaining 4")
    post(second)
    stages["complete"] = stock_evidence(company, item, key, other, locations, (2, 0, 10), second)
    stages["complete"].update(percent=dispatch.reload().per_transferred, mapped=mapped,
                              source_row=second.items[0].ste_detail,
                              transferred=dispatch.items[0].transferred_qty,
                              remaining_rows=len(make_stock_in_entry(dispatch.name).items))
    require(stages["complete"]["percent"] == 100 and stages["complete"]["remaining_rows"] == 0,
            "Native completion mismatch")
    return {"group": group, "domain_batch": number, "stages": stages, "probes": negatives}


def main():
    require(Path.cwd() == ROOT, "Wrong Bench directory")
    new_connection()
    settings = frappe.get_doc("Stock Settings")
    require(settings.use_serial_batch_fields and not settings.auto_create_serial_and_batch_bundle_for_outward
            and not settings.allow_negative_stock and not settings.allow_negative_stock_for_batch,
            "Existing stock settings must support explicit batches and forbid negative stock")
    original = fingerprint()
    run = "HP1D-" + uuid4().hex[:10].upper()
    try:
        company = make_company(run, run[-6:])
        locations = make_warehouses(company, run)
        groups = [run_group(company, locations, run, group, number)
                  for group, number in (("STD61", "STD61"), ("NO-BATCH", None))]
    finally:
        frappe.db.rollback()
    require(fingerprint() == original, "Native experiment left persistent data")
    print(json.dumps({"run": run, "locations": locations, "groups": groups,
                      "historical_fingerprint": original}, ensure_ascii=False, default=str, indent=2))
    frappe.destroy()


if __name__ == "__main__":
    main()
