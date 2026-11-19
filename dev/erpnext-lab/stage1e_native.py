"""Run native counting, cancellation, precision and two-line probes, then roll back."""
import json
import sys
from contextlib import redirect_stdout
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import frappe
from frappe.utils import strip_html

from stage1b_verify import ROOT, new_connection, require
from stage1d_native import insert, make_company, make_item, make_warehouses, post, qty_at, receipt_doc, stock_doc
from stage1e_evidence import decimal, digest, history, reconcile, records


def count_doc(company, item, warehouse, counts, remarks=None):
    return frappe.get_doc({
        "doctype": "Stock Reconciliation", "company": company, "purpose": "Stock Reconciliation",
        "remarks": remarks, "items": [{
            "item_code": item, "warehouse": warehouse, "batch_no": batch, "qty": Decimal(qty),
            "use_serial_batch_fields": 1, "reconcile_all_serial_batch": 0,
            "valuation_rate": 0, "allow_zero_valuation_rate": 1,
        } for batch, qty in counts],
    })


def probe(company, operation):
    before = digest(records(company))
    frappe.db.savepoint("stage1e_native_probe")
    try:
        doc = operation()
        result = {"accepted": True, "name": doc.name, "state": reconcile(company)}
    except frappe.ValidationError as error:
        result = {"accepted": False, "error_type": type(error).__name__, "error": strip_html(str(error)),
                  "uncommitted": {k: len(v) for k, v in records(company).items()}}
    finally:
        frappe.db.rollback(save_point="stage1e_native_probe")
        frappe.db.release_savepoint("stage1e_native_probe")
    require(digest(records(company)) == before, "Savepoint rollback changed inventory")
    return result


def fixture(run, group, numbers, *, uom="PC"):
    code = f"{run}-{group}"
    item = make_item(code) if uom == "PC" else insert(
        "Item", item_code=code, item_name=code, item_group="All Item Groups", stock_uom=uom,
        has_batch_no=1, is_stock_item=1, create_new_batch=0,
    ).name
    batches = [f"{code}-{number}" for number in numbers]
    for batch in batches:
        insert("Batch", batch_id=batch, item=item)
    return item, batches


def movement(company, item, batch, qty, purpose, *, source=None, target=None, dispatch=False, uom="PC"):
    doc = stock_doc(company, item, batch, qty, purpose, source=source, target=target, dispatch=dispatch)
    doc.items[0].update({"uom": uom, "stock_uom": uom})
    return doc


def check_balances(item, warehouse, batches, quantities):
    actual = [qty_at(item, batch, warehouse) for batch in batches]
    require(actual == list(map(Decimal, quantities)), f"Unexpected batch quantities: {actual}")
    return actual


def counting(company, source, run):
    item, (std61, std62, null) = fixture(run, "COUNT", ("STD61", "STD62", "NULL-SIMULATION"))
    batches = (std61, std62, null)
    for batch, qty in zip(batches, ("12", "20", "15"), strict=True):
        post(movement(company, item, batch, qty, "Material Receipt", target=source))
    before = check_balances(item, source, batches, ("12", "20", "15"))
    doc = post(count_doc(company, item, source, zip(batches, ("11", "20", "17"), strict=True)))
    after = check_balances(item, source, batches, ("11", "20", "17"))
    count_rows = [{"name": row.name, "batch": row.batch_no, "before": row.current_qty,
                   "after": row.qty, "difference": row.quantity_difference} for row in doc.items]
    require([decimal(row["difference"]) for row in count_rows] == [-1, 2], "Unexpected count row differences")
    evidence = reconcile(company)
    zero = post(count_doc(company, item, source, [(std61, "0")]))
    after_zero = check_balances(item, source, batches, ("0", "20", "17"))
    missing = probe(company, lambda: post(count_doc(company, item, source, [(None, "1")])))
    unknown = probe(company, lambda: post(count_doc(company, item, source, [(run + "-MISSING", "1")])))
    require(not missing["accepted"] and not unknown["accepted"], "Native count accepted unspecified or invalid batch")
    return {"before": before, "after": after, "after_zero": after_zero, "doc": doc.name, "rows": count_rows,
            "zero_doc": zero.name, "missing": missing, "unknown": unknown, "evidence": evidence,
            "zero_evidence": reconcile(company)}


def audit(company, doc, expected_qty):
    before = records(company)
    doc.cancel()
    after = records(company)
    before_sle = {row.name: row for row in before["Stock Ledger Entry"] if row.voucher_no == doc.name}
    after_sle = {row.name: row for row in after["Stock Ledger Entry"] if row.voucher_no == doc.name}
    require(doc.reload().docstatus == 2, "Cancelled source document missing")
    state = reconcile(company)
    actual = {(row["item"], row["warehouse"], row["batch"]): row["quantity"] for row in state["balances"]}
    require(all(actual.get(key, 0) == qty for key, qty in expected_qty.items()), "Reversal balance mismatch")
    return {"doc": doc.name, "original_sle": list(before_sle.values()),
            "cancelled_sle": list(after_sle.values()),
            "missing_sle": sorted(before_sle.keys() - after_sle.keys()),
            "changed_fields": {name: {key: [value, after_sle[name][key]] for key, value in row.items()
                                       if value != after_sle[name][key]}
                               for name, row in before_sle.items() if name in after_sle},
            "new_sle": sorted(after_sle.keys() - before_sle.keys()), "state": state}


def reversals(company, places, run):
    source, transit, target = (places[key] for key in ("source", "transit", "target"))
    output = {}
    for group in ("RECEIPT", "ISSUE", "COUNT-CORRECTION", "OPEN-TRANSFER", "PARTIAL-TRANSFER"):
        item, (batch,) = fixture(run, group, ("STD61",))
        key = lambda warehouse: (item, warehouse, batch)
        seed = post(movement(company, item, batch, "12", "Material Receipt", target=source))
        if group == "RECEIPT":
            output[group] = audit(company, seed, {key(source): 0})
            correction = movement(company, item, batch, "11", "Material Receipt", target=source)
            correction.amended_from = seed.name
            post(correction)
            check_balances(item, source, (batch,), ("11",))
            output[group]["amendment"] = {"name": correction.name, "amended_from": correction.amended_from,
                                          "state": reconcile(company)}
        elif group == "ISSUE":
            issue = post(movement(company, item, batch, "4", "Material Issue", source=source))
            output[group] = audit(company, issue, {key(source): 12})
        elif group == "COUNT-CORRECTION":
            count = post(count_doc(company, item, source, [(batch, "11")]))
            output[group] = audit(company, count, {key(source): 12})
            amendment = count_doc(company, item, source, [(batch, "10")])
            amendment.amended_from = count.name
            post(amendment)
            check_balances(item, source, (batch,), ("10",))
            output[group]["amendment"] = {"name": amendment.name, "amended_from": amendment.amended_from,
                                          "state": reconcile(company)}
        else:
            outbound = post(movement(company, item, batch, "10", "Material Transfer",
                                     source=source, target=transit, dispatch=True))
            if group == "OPEN-TRANSFER":
                output[group] = audit(company, outbound, {key(source): 12, key(transit): 0, key(target): 0})
            else:
                receipt, _ = receipt_doc(outbound, "6", target, batch)
                post(receipt)
                downstream = probe(company, lambda: outbound.reload().cancel())
                # Observe the native dependency rejection before attempting supported undo in reverse order.
                require(not downstream["accepted"], "Outward transfer with downstream receipt was cancelled")
                undo_receipt = audit(company, receipt, {key(source): 2, key(transit): 10, key(target): 0})
                undo_outbound = audit(company, outbound.reload(), {key(source): 12, key(transit): 0, key(target): 0})
                output[group] = {"outbound_cancel_with_receipt": downstream,
                                 "receipt_link": [receipt.outgoing_stock_entry, receipt.items[0].ste_detail],
                                 "undo_receipt": undo_receipt, "undo_outbound": undo_outbound}
    return output


def precision_and_atomicity(company, source, run):
    uom = insert("UOM", uom_name=run + " Decimal", must_be_whole_number=0).name
    item, (batch,) = fixture(run, "PRECISION", ("STD61",), uom=uom)
    def move(qty, purpose):
        parameters = {"target": source} if purpose == "Material Receipt" else {"source": source}
        return post(movement(company, item, batch, qty, purpose, uom=uom, **parameters))
    inward = move("1.125", "Material Receipt")
    outward = move("0.125", "Material Issue")
    check_balances(item, source, (batch,), ("1.000",))
    precision = {"unit": uom, "must_be_whole": frappe.db.get_value("UOM", uom, "must_be_whole_number"),
                 "receipt": inward.name, "issue": outward.name, "at_one": reconcile(company)}
    for qty in ("0.0001", "0.1254", "0", "-0.125"):
        precision[qty] = probe(company, lambda: move(qty, "Material Issue"))
    first, (first_batch,) = fixture(run, "ATOMIC-A", ("STD61",))
    second, (second_batch,) = fixture(run, "ATOMIC-B", ("STD61",))
    for sku, selected, qty in ((first, first_batch, "10"), (second, second_batch, "2")):
        post(movement(company, sku, selected, qty, "Material Receipt", target=source))
    precision["pc_half"] = probe(company, lambda: post(
        movement(company, first, first_batch, "0.5", "Material Issue", source=source)))
    invalid = movement(company, first, first_batch, "3", "Material Issue", source=source)
    invalid.append("items", {"item_code": second, "qty": 3, "uom": "PC", "s_warehouse": source,
                             "batch_no": second_batch, "use_serial_batch_fields": 1, "allow_zero_valuation_rate": 1})
    before = {k: len(v) for k, v in records(company).items()}
    atomic = {"before": before, **probe(company, lambda: post(invalid))}
    require(not atomic["accepted"], "Insufficient second row allowed entire document")
    check_balances(first, source, (first_batch,), ("10",))
    check_balances(second, source, (second_batch,), ("2",))
    return precision, atomic


def main(emit):
    require(Path.cwd() == ROOT, "Wrong Bench directory")
    new_connection()
    before = history()
    run = "HP1E-N-" + uuid4().hex[:8].upper()
    try:
        company = make_company(run, run[-5:])
        places = make_warehouses(company, run)
        result = {"run": run, "company": company, "locations": places}
        result["count"] = counting(company, places["source"], run)
        result["reversals"] = reversals(company, places, run)
        result["precision"], result["atomic"] = precision_and_atomicity(company, places["source"], run)
    finally:
        frappe.db.rollback()
        new_connection()
        require(not frappe.db.exists("Company", run) and history() == before, "Historical stock or isolation changed")
        frappe.destroy()
    emit(json.dumps(result, indent=2, default=str, ensure_ascii=False))


if __name__ == "__main__":
    output = sys.stdout
    with redirect_stdout(sys.stderr):
        main(lambda result: print(result, file=output))
