"""Run the Stage 1 probes in the existing lab and roll back all fixture writes."""

import json
import subprocess
import sys
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import erpnext
import frappe
from erpnext.stock.report.batch_wise_balance_history.batch_wise_balance_history import (
    execute as batch_report,
)
from frappe.utils import strip_html
from frappe.utils.background_jobs import get_jobs


SITE = "haxpower.localhost"
BENCH = Path("/workspace/development/frappe-bench")
COMMITS = {
    "frappe": "90c8b61afd60ee49f982ca1a9751008e4ae1e24c",
    "erpnext": "8e10a348daaa93853de8c36dc55ec789a440c219",
}
SETTINGS = (
    "enable_serial_and_batch_no_for_item", "auto_create_serial_and_batch_bundle_for_outward",
    "use_serial_batch_fields", "pick_serial_and_batch_based_on", "allow_negative_stock",
)
TRANSACTIONS = (
    "Stock Entry", "Stock Reconciliation", "Stock Ledger Entry", "Bin", "Batch",
    "Serial and Batch Bundle", "Serial and Batch Entry", "Item", "Item Price",
    "GL Entry", "Repost Item Valuation", "Purchase Order", "Sales Order",
    "Purchase Invoice", "Sales Invoice", "Purchase Receipt", "Delivery Note",
    "Journal Entry", "Payment Entry", "Workflow",
)
FIXTURES = ("Company", "Warehouse", "Account", "Cost Center", "Brand", "UOM", "Item Group")
NUMERIC_FIELDS = {
    "actual_qty", "qty_after_transaction", "qty", "total_qty", "basic_rate", "valuation_rate",
    "incoming_rate", "outgoing_rate", "stock_value", "stock_value_difference",
    "basic_amount", "amount", "debit", "credit",
}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def read_settings():
    return {key: frappe.db.get_single_value("Stock Settings", key, cache=False) for key in SETTINGS}


def baseline():
    return {doctype: frappe.db.count(doctype) for doctype in TRANSACTIONS + FIXTURES}


def check_environment():
    require(frappe.local.site == SITE, "Incorrect site")
    require(frappe.conf.db_host == "mariadb", "Incorrect lab database host")
    require(set(frappe.get_installed_apps()) == set(COMMITS), "Unexpected installed apps")
    require(frappe.is_setup_complete(), "Complete Setup Wizard first")
    setup = frappe.get_all("Installed Application", fields=["app_name", "is_setup_complete"])
    require({row.app_name: row.is_setup_complete for row in setup} == dict.fromkeys(COMMITS, 1),
            "Both application setup flags must be set")
    require(frappe.db.get_single_value("System Settings", "time_zone") == "Asia/Shanghai",
            "Unexpected site time zone")
    require(frappe.__version__ == erpnext.__version__ == "16.55.0", "Unexpected app version")
    require(sys.version_info[:3] == (3, 14, 10), "Unexpected Python version")
    for app, commit in COMMITS.items():
        command = ["git", "-C", str(BENCH / "apps" / app)]
        require(subprocess.check_output(command + ["rev-parse", "HEAD"], text=True).strip() == commit,
                f"Unexpected {app} commit")
        require(not subprocess.check_output(command + ["status", "--porcelain"], text=True),
                f"Dirty {app} working tree")
    require(frappe.db.sql("select version()")[0][0].startswith("11.8.9-MariaDB"),
            "Unexpected MariaDB version")
    state = baseline()
    require(state["Company"] == 1, "Review company baseline before running")
    require(not any(state[doctype] for doctype in TRANSACTIONS), "Review existing business rows first")
    settings = read_settings()
    require(settings["allow_negative_stock"] == 0, "Negative stock must be disabled")
    return state, settings


def new_doc(doctype, **values):
    return frappe.get_doc({"doctype": doctype, **values}).insert()


def configure_stock(**values):
    settings = frappe.get_doc("Stock Settings")
    settings.update(values)
    settings.save()
    frappe.db.value_cache.pop("Stock Settings", None)


def make_company(run, label, perpetual):
    company = new_doc(
        "Company", company_name=f"{run} {label}", abbr=f"{run[-6:]}{label[0]}",
        country="China", default_currency="CNY", valuation_method="FIFO",
        enable_perpetual_inventory=perpetual, enable_provisional_accounting_for_non_stock_items=0,
    ).reload()
    root = frappe.db.get_value("Warehouse", {"company": company.name, "is_group": 1}, "name")
    warehouse = new_doc(
        "Warehouse", warehouse_name=f"{run} W1", company=company.name, is_group=1,
        parent_warehouse=root,
    ).name
    other = new_doc(
        "Warehouse", warehouse_name=f"{run} W2", company=company.name, is_group=1,
        parent_warehouse=root,
    ).name
    locations = {
        label: new_doc(
            "Warehouse", warehouse_name=f"{run} {label}", company=company.name,
            parent_warehouse=parent,
        ).name
        for label, parent in (("W1-L1", warehouse), ("W1-L2", warehouse), ("W2-L1", other))
    }
    return company, locations


def make_item(run, suffix, brand, *, batched=False, automatic=False):
    return new_doc(
        "Item", item_code=f"{run}-{suffix}", item_name=f"{run} 喷油嘴 {suffix}",
        item_group="All Item Groups", stock_uom="PC", brand=brand, is_stock_item=1,
        has_batch_no=int(batched), create_new_batch=int(automatic),
        batch_number_series=f"{run}-AUTO-.####" if automatic else "",
    ).name


def make_batch(run, item, label):
    return new_doc("Batch", batch_id=f"{run}-{label}", item=item).name


def rows(doctype, filters, *fields):
    records = frappe.get_all(doctype, filters=filters, fields=["name", *fields], order_by="name")
    for record in records:
        for field in NUMERIC_FIELDS.intersection(fields):
            if record[field] is not None:
                record[field] = quantity(record[field])
    return records


def quantity(value):
    return Decimal(str(value if value is not None else 0))


def snapshot(company):
    match = {"company": company}
    ledgers = rows(
        "Stock Ledger Entry", match, "item_code", "warehouse", "voucher_no", "voucher_detail_no",
        "actual_qty", "qty_after_transaction", "incoming_rate", "outgoing_rate", "valuation_rate",
        "stock_value", "stock_value_difference", "batch_no", "serial_and_batch_bundle",
        "auto_created_serial_and_batch_bundle", "is_cancelled",
    )
    warehouses = frappe.get_all("Warehouse", filters=match, pluck="name")
    bins = rows("Bin", {"warehouse": ["in", warehouses]}, "item_code", "warehouse", "actual_qty")
    bundles = rows(
        "Serial and Batch Bundle", match, "item_code", "warehouse", "voucher_no",
        "voucher_detail_no", "type_of_transaction", "total_qty", "docstatus", "is_cancelled",
    )
    entries = rows(
        "Serial and Batch Entry", {"parent": ["in", [row.name for row in bundles]]},
        "parent", "batch_no", "qty", "incoming_rate", "stock_value_difference",
    ) if bundles else []
    today = frappe.utils.today()
    _, report = batch_report(frappe._dict(company=company, from_date=today, to_date=today))
    debits = rows("GL Entry", match, "voucher_no", "account", "cost_center", "debit", "credit")
    documents = rows("Stock Entry", match, "remarks", "docstatus")
    return {
        "documents": documents,
        "lines": rows(
            "Stock Entry Detail", {"parent": ["in", [document.name for document in documents]]},
            "parent", "item_code", "qty", "basic_rate", "valuation_rate", "basic_amount", "amount",
            "allow_zero_valuation_rate", "set_basic_rate_manually", "expense_account", "cost_center",
            "batch_no", "serial_and_batch_bundle", "use_serial_batch_fields",
        ) if documents else [],
        "ledgers": ledgers, "bins": bins, "bundles": bundles, "bundle_entries": entries,
        "batches": rows("Batch", {"item": ["like", "HP-S1-%"]}, "item", "batch_id"),
        "batch_report": [
            dict(item=row[0], warehouse=row[3], batch=row[4], balance=quantity(row[8])) for row in report
        ],
        "gl": debits, "reposts": rows("Repost Item Valuation", match, "voucher_no", "status", "docstatus"),
    }


def reconcile(state):
    totals = defaultdict(Decimal)
    by_batch = defaultdict(Decimal)
    bundles = {row.name: row for row in state["bundles"]}
    entries = defaultdict(list)
    for row in state["bundle_entries"]:
        entries[row.parent].append(row)
    for sle in state["ledgers"]:
        require(not sle.is_cancelled, "Unexpected cancelled row")
        key = (sle.item_code, sle.warehouse)
        delta = quantity(sle.actual_qty)
        totals[key] += delta
        if not sle.serial_and_batch_bundle:
            continue
        bundle = bundles[sle.serial_and_batch_bundle]
        children = entries[bundle.name]
        require((bundle.item_code, bundle.warehouse) == key, "Bundle item/location mismatch")
        require(bundle.docstatus == 1 and not bundle.is_cancelled, "Inactive Bundle")
        require((bundle.voucher_no, bundle.voucher_detail_no) == (sle.voucher_no, sle.voucher_detail_no),
                "Bundle voucher mismatch")
        require(sum(quantity(row.qty) for row in children) == delta == quantity(bundle.total_qty),
                "Bundle quantity mismatch")
        for row in children:
            by_batch[(*key, row.batch_no)] += quantity(row.qty)
    bins = {(row.item_code, row.warehouse): quantity(row.actual_qty) for row in state["bins"]}
    report = {(row["item"], row["warehouse"], row["batch"]): row["balance"] for row in state["batch_report"]}
    require(dict(totals) == bins, "SLE and Bin totals differ")
    require(dict(by_batch) == report, "Bundle and Batch-wise Balance History totals differ")


def probe(results, label, company, purpose, item, amount, *, keep=True, posting=None, **fields):
    before = snapshot(company.name)
    savepoint = f"probe_{len(results)}"
    frappe.db.savepoint(savepoint)
    row = {"item_code": item, "qty": Decimal(amount), "uom": "PC", "stock_uom": "PC",
           "conversion_factor": Decimal(1), **fields}
    doc = frappe.get_doc({
        "doctype": "Stock Entry", "stock_entry_type": purpose, "company": company.name,
        "remarks": label, "items": [row],
    })
    if posting:
        doc.update(posting)
    outcome = {"case": label, "company": company.name, "purpose": purpose, "input": row,
               "posting": posting,
               "auto_outward": read_settings()["auto_create_serial_and_batch_bundle_for_outward"],
               "before": before}
    stage = "insert"
    try:
        doc.insert()
        stage = "submit"
        doc.submit()
    except frappe.ValidationError as error:
        outcome.update(result="rejected", stage=stage, error_type=type(error).__name__,
                       error=strip_html(str(error)), attempted_document=doc.name)
        frappe.db.rollback(save_point=savepoint)
        after = snapshot(company.name)
        require(before == after, f"{label}: failed submission changed inventory or documents")
        outcome["after"] = after
    else:
        outcome.update(result="submitted", document=doc.name, after=snapshot(company.name))
        reconcile(outcome["after"])
        if not keep:
            frappe.db.rollback(save_point=savepoint)
            require(snapshot(company.name) == before, f"{label}: control rollback mismatch")
    frappe.db.release_savepoint(savepoint)
    results.append(outcome)
    return outcome["result"] == "submitted"


def run_batch(run, brand, results):
    configure_stock(enable_serial_and_batch_no_for_item=1)
    company, locations = make_company(run, "Batch", 0)
    sku = make_item(run, "SKU-A", brand, batched=True)
    l1, l2, w2 = (locations[key] for key in ("W1-L1", "W1-L2", "W2-L1"))
    receipt = "Material Receipt"
    issue = "Material Issue"
    explicit_rates = {"basic_rate": Decimal("7.50"), "set_basic_rate_manually": 1}
    for label, qty in (("STD61", "12"), ("STD62", "20")):
        batch = make_batch(run, sku, label)
        require(probe(results, f"E01 in {label}", company, receipt, sku, qty, t_warehouse=l1,
                      batch_no=batch, use_serial_batch_fields=1, **explicit_rates),
                "Required real-batch receipt control failed")
    for label, amount, location in (("L1", "15", l1), ("L2", "12", l2)):
        probe(results, f"E01 empty {label}", company, receipt, sku, amount,
              t_warehouse=location, batch_no=None, use_serial_batch_fields=1, **explicit_rates)

    e02 = make_item(run, "E02-SKU-A", brand, batched=True)
    for legacy in (0, 1):
        probe(results, f"E02 empty legacy={legacy}", company, receipt, e02, "3",
              t_warehouse=l1, use_serial_batch_fields=legacy, **explicit_rates)
    batch = make_batch(run, e02, "E02-STD61")
    probe(results, "E02 in STD61", company, receipt, e02, "3", t_warehouse=l1,
          batch_no=batch, use_serial_batch_fields=1, **explicit_rates)
    automatic = make_item(run, "E02-AUTO", brand, batched=True, automatic=True)
    probe(results, "E02 auto-create", company, receipt, automatic, "3",
          t_warehouse=l1, use_serial_batch_fields=0, **explicit_rates)

    for auto in (0, 1):
        configure_stock(auto_create_serial_and_batch_bundle_for_outward=auto)
        probe(results, f"E04 explicit null auto={auto}", company, issue, sku, "4",
              keep=False, s_warehouse=l1, batch_no=None, use_serial_batch_fields=1)
        for legacy in (0, 1):
            probe(results, f"E05 omit issue legacy={legacy} auto={auto}", company, issue, sku, "4",
                  keep=False, s_warehouse=l1, use_serial_batch_fields=legacy)
        probe(results, f"E05 omit transfer auto={auto}", company, "Material Transfer", sku, "4",
              keep=False, s_warehouse=l1, t_warehouse=w2, use_serial_batch_fields=0)


def run_valuation(run, brand, results):
    configs = {}
    for label, perpetual in (("Periodic", 0), ("Evergreen", 1)):
        company, locations = make_company(run, label, perpetual)
        warehouse = locations["W1-L1"]
        configs[label] = {
            "company": company.name, "currency": company.default_currency,
            "valuation_method": company.valuation_method, "perpetual": company.enable_perpetual_inventory,
            "default_inventory_account": company.default_inventory_account,
            "stock_adjustment_account": company.stock_adjustment_account,
            "cost_center": company.cost_center,
            "warehouse_account": frappe.db.get_value("Warehouse", warehouse, "account"),
            "account_count": frappe.db.count("Account", {"company": company.name}),
        }
        rate_cases = (
            ("omitted", {}),
            ("zero-no-flag", {"basic_rate": Decimal(0)}),
            ("zero-allowed", {"basic_rate": Decimal(0), "allow_zero_valuation_rate": 1,
                              "set_basic_rate_manually": 1}),
            ("nonzero", {"basic_rate": Decimal("7.50"), "set_basic_rate_manually": 1}),
        )
        for scenario, fields in rate_cases:
            item = make_item(run, f"{label}-{scenario}", brand)
            submitted = probe(results, f"E14 {label} in {scenario}", company, "Material Receipt",
                              item, "10", t_warehouse=warehouse, **fields)
            if scenario == "zero-allowed" and submitted:
                for allow_zero in (0, 1):
                    probe(results, f"E14 {label} out zero allow={allow_zero}", company,
                          "Material Issue", item, "4", keep=bool(allow_zero),
                          s_warehouse=warehouse, allow_zero_valuation_rate=allow_zero)
        if perpetual:
            # Establish a later receipt for this item before inserting an earlier receipt.
            item = make_item(run, "E14-repost", brand)
            later = frappe.utils.now_datetime() - timedelta(seconds=5)
            earlier = later - timedelta(seconds=5)
            fields = {"t_warehouse": warehouse, "basic_rate": Decimal("7.50"),
                      "set_basic_rate_manually": 1}
            require(probe(results, "E14 repost later receipt", company, "Material Receipt", item, "2",
                          posting={"posting_date": str(later.date()), "posting_time": str(later.time()),
                                   "set_posting_time": 1}, **fields), "Repost control receipt failed")
            probe(results, "E14 repost earlier receipt", company, "Material Receipt", item, "1",
                  posting={"posting_date": str(earlier.date()), "posting_time": str(earlier.time()),
                           "set_posting_time": 1},
                  **fields)

    company, locations = make_company(run, "Accounts", 1)
    company.stock_adjustment_account = None
    company.save()
    require(not company.reload().stock_adjustment_account, "Account default was restored")
    item = make_item(run, "E14-no-difference-account", brand)
    probe(results, "E14 perpetual missing difference account", company, "Material Receipt",
          item, "10", t_warehouse=locations["W1-L1"], basic_rate=Decimal(0),
          allow_zero_valuation_rate=1, set_basic_rate_manually=1)
    return configs


def main():
    require(Path.cwd() == BENCH / "sites", "Run inside the lab Bench sites directory")
    run = f"HP-S1-{uuid4().hex[:10].upper()}"
    frappe.init(site=SITE, sites_path=".")
    frappe.connect()
    try:
        before, settings = check_environment()
        result = {"run": run, "site": SITE, "apps": COMMITS, "baseline": before,
                  "settings_before": settings, "probes": [], "started_at": frappe.utils.now(),
                  "jobs_before": get_jobs(site=SITE).get(SITE, [])}
        # The batch activation is a global setting with a disable guard after use.
        # Keep the setting change, master records and native submissions in one transaction.
        # Savepoints isolate probe rejections and restore balances between alternate scenarios.
        try:
            if not frappe.db.exists("UOM", "PC"):
                new_doc("UOM", uom_name="PC", must_be_whole_number=1)
            require(frappe.db.get_value("UOM", "PC", "must_be_whole_number") == 1, "PC must be whole")
            brand = new_doc("Brand", brand=f"{run} 品牌甲").name
            run_batch(run, brand, result["probes"])
            result["valuation_config"] = run_valuation(run, brand, result["probes"])
            result["inside_transaction"] = baseline()
            result["jobs_inside"] = get_jobs(site=SITE).get(SITE, [])
        finally:
            frappe.db.rollback()
            frappe.db.value_cache.pop("Stock Settings", None)
        result["after_rollback"] = baseline()
        result["settings_after_rollback"] = read_settings()
        result["jobs_after_rollback"] = get_jobs(site=SITE).get(SITE, [])
        require(result["after_rollback"] == before, "Experiment rows remain after rollback")
        require(result["settings_after_rollback"] == settings, "Stock Settings remain changed")
        print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    finally:
        frappe.db.rollback()
        frappe.destroy()


if __name__ == "__main__":
    main()
