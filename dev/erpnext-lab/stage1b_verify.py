"""Verify durable Stage 1B results and normal access-control boundaries via local HTTP."""

import json
from collections import defaultdict
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from urllib.parse import quote

import frappe
import requests
from erpnext.stock.report.batch_wise_balance_history.batch_wise_balance_history import execute as batch_report
from frappe.core.doctype.user.user import generate_keys
from frappe.utils import today

from haxpower_platform.stock import COMPANY, ITEMS, LOCATIONS, ROLE, SITE, batch_key


HTTP = "http://127.0.0.1:8011"
ROOT = Path("/workspace/development/frappe-bench/sites")
API = "/api/method/haxpower_platform.stock."
REPORT = "Batch-Wise Balance History"
OPERATOR = "stage1b-operator@example.invalid"
STOCK_USER = "stage1b-stock@example.invalid"
NO_BATCH = {"mode": "NO_BATCH"}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def dec(value):
    return Decimal(str(value))


def new_connection():
    frappe.destroy()
    frappe.init(site=SITE, sites_path=".")
    frappe.connect()
    require(frappe.conf.db_host == "mariadb" and frappe.conf.db_name == "haxpower_stage1b_lab",
            "Wrong lab database")
    require(frappe.is_setup_complete() and "haxpower_platform" in frappe.get_installed_apps(),
            "Prototype not ready")


def session(user, roles, locations):
    if not frappe.db.exists("User", user):
        doc = frappe.get_doc({
            "doctype": "User", "email": user, "first_name": "Stage 1B fictitious user",
            "user_type": "System User", "enabled": 1, "send_welcome_email": 0,
        })
        doc.append_roles(*roles)
        doc.insert()
    current = set(frappe.get_roles(user)) - {"All", "Desk User", "Guest"}
    require(current <= set(roles), "Unexpected test user roles")
    if missing := set(roles) - current:
        doc = frappe.get_doc("User", user)
        doc.append_roles(*sorted(missing))
        doc.save()
    allowed = {LOCATIONS[location] for location in locations}
    filters = {"user": user, "allow": "Warehouse"}
    grants = frappe.get_all("User Permission", filters=filters,
                            fields=["for_value", "apply_to_all_doctypes"])
    require(all(grant.for_value in allowed and grant.apply_to_all_doctypes for grant in grants),
            "Unexpected Warehouse grant on the test user")
    for warehouse in sorted(allowed - {grant.for_value for grant in grants}):
        frappe.get_doc({"doctype": "User Permission", **filters, "for_value": warehouse,
                        "apply_to_all_doctypes": 1, "hide_descendants": 1}).insert()
    keys = generate_keys(user)
    frappe.db.commit()
    client = requests.Session()
    client.trust_env = False
    client.headers.update({"Host": SITE, "Authorization": f"token {keys['api_key']}:{keys['api_secret']}"})
    identity = send(client, "GET", "/api/method/frappe.auth.get_logged_user")
    require(identity.status_code == 200 and identity.json().get("message") == user,
            "Wrong HTTP test identity")
    return client


def send(client, method, route, **kwargs):
    return client.request(method, HTTP + route, timeout=25, allow_redirects=False, **kwargs)


def payload(action, request_id, quantity, selection, sku="SKU-A", loc="W1/L1"):
    body = {
        "action": action, "requestId": "stage1b-" + request_id, "quantity": quantity,
        "stockItemId": sku, "warehouseLocationId": loc,
    }
    if selection is not None:
        body["batchSelection"] = selection
    return body


def accepted(client, body):
    response = send(client, "POST", API + "post_stock", json=body)
    require(response.status_code == 200,
            f"{body['requestId']} HTTP {response.status_code}: {response.text[:900]}")
    return response.json()["message"]


def balance(client, sku="SKU-A", loc="W1/L1"):
    response = send(client, "GET", API + "balances", params={"stockItemId": sku, "warehouseLocationId": loc})
    require(response.status_code == 200, f"Balance request returned HTTP {response.status_code}")
    result = response.json()["message"]["items"]
    require("HP-NB-" not in response.text and "HP-RB-" not in response.text, "Internal ID in public response")
    return {row["batch"]: dec(row["quantity"]) for row in result}


def committed_snapshot():
    frappe.db.rollback()
    native = frappe.get_all(
        "Stock Ledger Entry", filters={"company": COMPANY}, order_by="name",
        fields=["name", "voucher_no", "voucher_detail_no", "item_code", "warehouse",
                "actual_qty", "qty_after_transaction", "serial_and_batch_bundle", "is_cancelled",
                "incoming_rate", "outgoing_rate", "valuation_rate", "stock_value", "stock_value_difference",
                "posting_date"],
    )
    posted = frappe.get_all(
        "Stock Entry", filters={"company": COMPANY}, order_by="name", fields=["name", "docstatus"],
    )
    batches = frappe.get_all(
        "Batch", filters={"item": ["in", list(ITEMS.values())]}, order_by="name",
        fields=["name", "item", "disabled", "description"],
    )
    bundles = frappe.get_all(
        "Serial and Batch Bundle", filters={"company": COMPANY}, order_by="name",
        fields=["name", "docstatus", "voucher_no", "voucher_detail_no", "warehouse",
                "item_code", "total_qty", "type_of_transaction"],
    )
    entries = frappe.get_all(
        "Serial and Batch Entry", filters={"parent": ["in", [row.name for row in bundles]]},
        fields=["name", "parent", "batch_no", "qty"], order_by="name",
    ) if bundles else []
    bins = frappe.get_all(
        "Bin", filters={"warehouse": ["in", list(LOCATIONS.values())]},
        fields=["item_code", "warehouse", "actual_qty"], order_by="name",
    )
    return {"ledger": native, "documents": posted, "batches": batches, "bundles": bundles,
            "entries": entries, "bins": bins, "gl": frappe.db.count("GL Entry", {"company": COMPANY}),
            "reposts": frappe.db.count("Repost Item Valuation", {"company": COMPANY})}


def report_filters(state):
    start = min(str(row.posting_date) for row in state["ledger"])
    return {"company": COMPANY, "from_date": start, "to_date": today()}


def reconcile(state):
    totals = defaultdict(Decimal)
    buckets = defaultdict(Decimal)
    bundles = {row.name: row for row in state["bundles"]}
    entries = {row.parent: row for row in state["entries"]}
    require(len(state["documents"]) == len(state["ledger"]) == len(bundles) == len(entries) == 8,
            "Expected eight durable, single-line transactions")
    require(all(doc.docstatus == 1 for doc in state["documents"]), "Unexpected draft or cancelled document")
    for row in state["ledger"]:
        bundle = bundles[row.serial_and_batch_bundle]
        entry = entries[bundle.name]
        key = row.item_code, row.warehouse
        qty = dec(row.actual_qty)
        require(not row.is_cancelled and bundle.docstatus == 1
                and (bundle.item_code, bundle.warehouse) == key
                and (bundle.voucher_no, bundle.voucher_detail_no) == (row.voucher_no, row.voucher_detail_no)
                and qty == dec(bundle.total_qty) == dec(entry.qty), "SLE and Bundle mismatch")
        require(all(dec(row[field]) == 0 for field in ("valuation_rate", "stock_value", "stock_value_difference")),
                "Nonzero valuation in the zero-rate lab")
        totals[key] += qty
        buckets[(*key, entry.batch_no)] += qty
    bins = {(row.item_code, row.warehouse): dec(row.actual_qty) for row in state["bins"]}
    require(dict(totals) == bins == {
        (ITEMS["SKU-A"], LOCATIONS["W1/L1"]): 38,
        (ITEMS["SKU-A"], LOCATIONS["W1/L2"]): 12,
        (ITEMS["SKU-B"], LOCATIONS["W1/L1"]): 1,
    }, "SLE/Bin reconciliation failed")
    expected = {("SKU-A", "W1/L1", "STD61"): 7, ("SKU-A", "W1/L1", "STD62"): 20,
                ("SKU-A", "W1/L1", None): 11, ("SKU-A", "W1/L2", None): 12,
                ("SKU-B", "W1/L1", None): 1}
    require(dict(buckets) == {(ITEMS[sku], LOCATIONS[loc], batch_key(ITEMS[sku], number)): qty
                             for (sku, loc, number), qty in expected.items()}, "Bucket reconciliation failed")
    _, report = batch_report(frappe._dict(report_filters(state)))
    require(dict(buckets) == {(row[0], row[3], row[4]): dec(row[8]) for row in report},
            "Batch-wise Balance History mismatch")
    require(len(state["batches"]) == 4 and not state["gl"] and not state["reposts"],
            "Unexpected Batch, GL, or repost")


def reject(client, body, reason):
    response = send(client, "POST", API + "post_stock", json=body)
    require(response.status_code >= 400 and reason in response.text,
            f"{body['requestId']} unexpected HTTP {response.status_code}")
    return {"status": response.status_code, "reason": reason}


def native_access(client, key, bundle, dates):
    batch_url = "/api/resource/Batch/" + quote(key)
    batch_data = {"item": ITEMS["SKU-A"], "batch_id": "HP-NB-STAGE1B-FORBIDDEN"}
    stock = {
        "doctype": "Stock Entry", "stock_entry_type": "Material Issue", "company": COMPANY,
        "items": [{"item_code": ITEMS["SKU-A"], "qty": 1, "uom": "PC",
                   "s_warehouse": LOCATIONS["W1/L1"], "allow_zero_valuation_rate": 1}],
    }
    filters = json.dumps({**dates, "item_code": ITEMS["SKU-A"]})
    calls = (
        ("create_reserved_batch", "POST", "/api/resource/Batch", {"json": batch_data}),
        ("edit_reserved_batch", "PUT", batch_url, {"json": {"description": "changed"}}),
        ("stock_form_missing_selection", "POST", "/api/method/frappe.desk.form.save.savedocs",
         {"json": {"doc": json.dumps(stock), "action": "Save"}}),
        ("stock_rest_missing_selection", "POST", "/api/resource/Stock%20Entry", {"json": stock}),
        ("stock_rest_explicit_system_batch", "POST", "/api/resource/Stock%20Entry",
         {"json": {**stock, "items": [{**stock["items"][0], "use_serial_batch_fields": 1, "batch_no": key}]}}),
        ("stock_form_submit_missing_selection", "POST", "/api/method/frappe.desk.form.save.savedocs",
         {"json": {"doc": json.dumps(stock), "action": "Submit"}}),
        ("stock_submit_method_missing_selection", "POST", "/api/method/frappe.client.submit",
         {"json": {"doc": stock}}),
        ("bundle_rest_create", "POST", "/api/resource/Serial%20and%20Batch%20Bundle", {"json": {
            "company": COMPANY, "item_code": ITEMS["SKU-A"], "warehouse": LOCATIONS["W1/L1"],
            "voucher_type": "Stock Entry", "type_of_transaction": "Outward",
            "entries": [{"batch_no": key, "qty": -1}],
        }}),
        ("batch_detail_rest", "GET", batch_url, {}),
        ("batch_list_rest", "GET", "/api/resource/Batch",
         {"params": {"fields": json.dumps(["name", "batch_id"]), "limit_page_length": 30}}),
        ("bundle_detail_rest", "GET", "/api/resource/Serial%20and%20Batch%20Bundle/" + quote(bundle), {}),
        ("batch_detail_desk", "GET", "/api/method/frappe.desk.form.load.getdoc",
         {"params": {"doctype": "Batch", "name": key}}),
        ("standard_batch_report", "GET", "/api/method/frappe.desk.query_report.run",
         {"params": {"report_name": REPORT, "filters": filters, "ignore_prepared_report": "true"}}),
        ("standard_csv_export", "GET", "/api/method/frappe.desk.query_report.export_query",
         {"params": {"report_name": REPORT, "filters": filters, "file_format_type": "CSV",
                     "ignore_visible_idx": "true", "export_in_background": "0"}}),
    )
    outcomes = {}
    for label, method, route, args in calls:
        response = send(client, method, route, **args)
        result = {"status": response.status_code,
                  "exposes_internal_id": response.ok and key.encode() in response.content,
                  "batch_hook": "STAGE1B_BATCH_PROTECTED" in response.text,
                  "stock_hook": "STAGE1B_USE_ADAPTER" in response.text,
                  "content_type": response.headers.get("Content-Type")}
        if not response.ok:
            try:
                result["exception_type"] = response.json().get("exc_type")
            except ValueError:
                pass
        outcomes[label] = result
    for label, method, *_ in calls:
        if method not in ("POST", "PUT"):
            continue
        require(outcomes[label]["status"] in (403, 417),
                f"Expected blocked mutation: {label} {outcomes[label]}")
    return outcomes


def main():
    require(Path.cwd() == ROOT, "Wrong Bench directory")
    new_connection()
    require(not frappe.db.get_value("Company", COMPANY, "enable_perpetual_inventory"), "Wrong costing")
    require(not frappe.db.get_single_value("Stock Settings", "auto_create_serial_and_batch_bundle_for_outward"),
            "Wrong allocation configuration")
    operator = session(OPERATOR, [ROLE], tuple(LOCATIONS))
    stock_user = session(STOCK_USER, [ROLE, "Stock User", "Stock Manager", "Item Manager"], ("W1/L1",))
    std61 = {"mode": "BATCH", "batchNo": "STD61"}
    std62 = {"mode": "BATCH", "batchNo": "STD62"}
    commands = [
        payload("RECEIPT", "std61", "12", std61),
        payload("RECEIPT", "std62", "20", std62),
        payload("RECEIPT", "no-batch-1", "9", NO_BATCH),
        payload("RECEIPT", "no-batch-2", "6", NO_BATCH),
        payload("RECEIPT", "l2", "12", NO_BATCH, loc="W1/L2"),
        payload("RECEIPT", "sku-b", "1", NO_BATCH, sku="SKU-B"),
        payload("ISSUE", "out-std61", "5", std61),
        payload("ISSUE", "out-no-batch", "4", NO_BATCH),
    ]
    existing = frappe.get_all("Stock Entry", filters={"company": COMPANY}, fields=["docstatus", "remarks"])
    allowed = {
        sha256(f"{OPERATOR}\0{command['requestId']}".encode()).hexdigest() for command in commands
    }
    require(all(row.docstatus == 1 and row.remarks and row.remarks.split(":")[0] == "HP-STAGE1B"
                and len(row.remarks.split(":")) == 3 and row.remarks.split(":")[1] in allowed
                for row in existing), "Existing stock outside this experiment")
    fresh = not existing
    results = []
    for index, command in enumerate(commands):
        results.append(accepted(operator, command))
        if fresh and index in (2, 3, 6):
            expected = {"STD61": 7 if index == 6 else 12, "STD62": 20, None: 9 if index == 2 else 15}
            require(balance(operator) == expected, "Intermediate bucket changed unexpectedly")
    require(balance(operator) == {"STD61": 7, "STD62": 20, None: 11}, "W1/L1 balance mismatch")
    require(balance(operator, loc="W1/L2") == {None: 12}, "W1/L2 changed")
    require(balance(operator, sku="SKU-B") == {None: 1}, "SKU-B changed")
    new_connection()
    before = committed_snapshot()
    reconcile(before)
    replay = accepted(operator, commands[-1])
    require(replay == {"documentId": results[-1]["documentId"], "replayed": True}, "Replay mismatch")
    key = batch_key(ITEMS["SKU-A"], None)
    other_key = batch_key(ITEMS["SKU-B"], None)
    require(key != other_key, "System Batch IDs collide")
    rejections = {
        "insufficient_no_batch": reject(operator, payload("ISSUE", "overdraw", "12", NO_BATCH),
                                        "INSUFFICIENT_STOCK"),
        "missing_selection": reject(operator, payload("ISSUE", "missing", "1", None),
                                    "BATCH_SELECTION_REQUIRED"),
        "missing_identifier": reject(operator, payload("ISSUE", "missing-id", "1", {"mode": "BATCH"}),
                                      "BATCH_NOT_FOUND"),
        "unknown_identifier": reject(operator, payload("ISSUE", "unknown-id", "1",
                                     {"mode": "BATCH", "batchNo": "DOES-NOT-EXIST"}), "BATCH_NOT_FOUND"),
        "reserved_as_real": reject(operator, payload("ISSUE", "reserved", "1",
                                   {"mode": "BATCH", "batchNo": key}), "BATCH_NOT_FOUND"),
        "changed_replay": reject(operator, {**commands[-1], "quantity": "5"}, "IDEMPOTENCY_CONFLICT"),
    }
    bundle = next(row.serial_and_batch_bundle for row in before["ledger"]
                  if row.voucher_no == results[-1]["documentId"])
    dates = report_filters(before)
    access = {"operator": native_access(operator, key, bundle, dates),
              "native_stock_roles": native_access(stock_user, key, bundle, dates)}
    for label in ("create_reserved_batch", "edit_reserved_batch"):
        require(access["native_stock_roles"][label]["batch_hook"], f"Batch hook not reached: {label}")
    new_connection()
    after = committed_snapshot()
    require(after == before, "Denied requests or replay changed committed inventory")
    column_types = frappe.db.sql("""
        select table_name, column_name, column_type from information_schema.columns
        where table_schema = database() and ((table_name = 'tabStock Ledger Entry' and column_name = 'actual_qty')
            or (table_name = 'tabSerial and Batch Entry' and column_name = 'qty')
            or (table_name = 'tabBin' and column_name = 'actual_qty'))
    """)
    print(json.dumps({"commands": results, "replay": replay, "rejected": rejections, "access": access,
                      "same_inventory_after_rejections": True, "fresh_initial_run": fresh,
                      "quantity_column_types": column_types, "committed": after},
                     ensure_ascii=False, default=str, indent=2))


if __name__ == "__main__":
    try:
        main()
    finally:
        frappe.destroy()
