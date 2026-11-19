"""Verify durable transfers through Frappe WSGI without starting a second service."""

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from decimal import Decimal
from pathlib import Path
from threading import Barrier

import frappe
from frappe.app import application
from werkzeug.test import Client

import stage1b_verify
import stage1c_verify
from haxpower_platform.stock import QUERY_ROLE, ROLE, SERVICE_WRITE, batch_key, find_batch
from haxpower_platform.transit_fixtures import COMPANY, ITEMS, LOCATIONS, TRANSIT
from stage1b_verify import API, NO_BATCH, ROOT, SITE, committed_snapshot, new_connection, require, session
from stage1c_verify import fingerprint
from stage1d_native import make_company, make_item, make_warehouses, post, stock_doc, stock_evidence


ENDPOINT = "/api/method/haxpower_platform.transit."
GROUPS = {"GIT-STD61": "STD61", "GIT-NO-BATCH": None}
PROFILES = {
    "operator": ("stage1d-operator@example.invalid", [ROLE], tuple(LOCATIONS)),
    "source_only": ("stage1d-source@example.invalid", [ROLE], ("W1/L1",)),
    "target_only": ("stage1d-target@example.invalid", [ROLE], ("W2/L1",)),
    "query": ("stage1d-query@example.invalid", [QUERY_ROLE], tuple(LOCATIONS)),
    "native": ("stage1d-native@example.invalid", ["Stock User", "Stock Manager"], (*LOCATIONS, "Transit")),
}
POSITIONS = {key: LOCATIONS[loc] for key, loc in (("source", "W1/L1"), ("other", "W1/L2"), ("target", "W2/L1"))}
POSITIONS["transit"] = TRANSIT


class HTTPResponse:
    def __init__(self, response):
        self.content = response.get_data()
        self.headers = response.headers
        self.status_code = response.status_code

    def json(self):
        return json.loads(self.content)

    @property
    def text(self):
        return self.content.decode("utf-8", errors="replace")

    @property
    def ok(self):
        return self.status_code < 400


def wsgi_request(client, verb, path, params=None, json=None, barrier=None):
    if barrier:
        barrier.wait(timeout=10)
    # Each request uses real Frappe authentication and commit/rollback in its own context.
    response = Client(application, use_cookies=False).open(
        path, method=verb, base_url=f"http://{SITE}", query_string=params, json=json,
        headers=dict(client.headers), environ_overrides={"REMOTE_ADDR": "127.0.0.1"}, buffered=True,
    )
    return HTTPResponse(response)


def prepare_data():
    require(not frappe.db.count("Workflow", {"document_type": "Stock Entry", "is_active": 1}),
            "Unexpected active Stock Entry workflow")
    if frappe.db.exists("Company", COMPANY):
        require(not frappe.db.get_value("Company", COMPANY, "enable_perpetual_inventory"),
                "Unexpected costing settings")
        for sku, item in ITEMS.items():
            require(frappe.db.exists("Item", item), "Existing transfer company has incomplete fixtures")
            for label in ("source", "other-source", "other-position"):
                require(frappe.db.exists("Stock Entry", {"company": COMPANY, "docstatus": 1,
                                                        "remarks": f"HP1D-SEED:{sku}:{label}"}),
                        "Existing experimental stock baseline is incomplete")
        return
    make_company(COMPANY, "H1D")
    require(make_warehouses(COMPANY, "HP1D") == POSITIONS, "Warehouse fixture mismatch")
    token = SERVICE_WRITE.set(True)
    try:
        for sku, number in GROUPS.items():
            item = make_item(ITEMS[sku])
            batch, other = find_batch(item, number, create=True), find_batch(item, "STD62", create=True)
            for label, batch_id, amount, location in (
                ("source", batch, "12", "source"), ("other-source", other, "20", "source"),
                ("other-position", other, "2", "other"),
            ):
                doc = stock_doc(COMPANY, item, batch_id, amount, "Material Receipt", target=POSITIONS[location])
                doc.remarks = f"HP1D-SEED:{sku}:{label}"
                post(doc)
    finally:
        SERVICE_WRITE.reset(token)
    frappe.db.commit()


def selection(number):
    return NO_BATCH if number is None else {"mode": "BATCH", "batchNo": number}


def successful(response, expected):
    require(response.status_code == 200, f"Expected HTTP success, got {response.status_code}: {response.text[:700]}")
    require("HP-NB-" not in response.text and "HP-RB-" not in response.text, "Internal Batch ID exposed")
    result = response.json()["message"]
    for key, value in expected.items():
        require(result[key] == value, f"Expected {key}={value}, got {result[key]}")
    return result


def reject(send, client, endpoint, data, status, reason=None):
    before = fingerprint()
    response = send(client, "POST", endpoint, json=data)
    require(fingerprint() == before, f"Rejected request changed inventory: {data.get('requestId', endpoint)}")
    require(response.status_code == status and (not reason or reason in response.text),
            f"Expected HTTP {status} {reason}, got {response.status_code}: {response.text[:700]}")
    return {"status": status, "reason": reason}


def check_stage(sku, number, balance, state, voucher=None):
    frappe.db.rollback()
    doc = frappe.get_doc("Stock Entry", voucher) if voucher else None
    item = ITEMS[sku]
    detail = stock_evidence(COMPANY, item, batch_key(item, number), batch_key(item, "STD62"),
                            POSITIONS, balance, doc)
    if doc:
        require(len(detail["ledger"]) == 2
                and sum(Decimal(str(row["actual_qty"])) for row in detail["ledger"]) == 0,
                "A transfer must have two balanced SLE rows")
        detail.update(outgoing_stock_entry=doc.outgoing_stock_entry, against_stock_entry=doc.items[0].against_stock_entry,
                      original_row=doc.items[0].ste_detail, state=state, docstatus=doc.docstatus)
    frappe.db.rollback()
    return detail


def document_for(kind, key):
    frappe.db.rollback()
    found = frappe.db.get_value("Stock Entry", {"company": COMPANY, "remarks": ["like", f"HP1D-{kind}:{key}:%"],
                                               "docstatus": 1}, "name")
    require(found, "Committed transfer document not found")
    frappe.db.rollback()
    return found


def initial_seed(sku, number):
    item = ITEMS[sku]
    key, other = batch_key(item, number), batch_key(item, "STD62")
    seeds = {}
    for label, warehouse, batch, amount in (
        ("source", POSITIONS["source"], key, 12),
        ("other-source", POSITIONS["source"], other, 20),
        ("other-position", POSITIONS["other"], other, 2),
    ):
        voucher = frappe.db.get_value("Stock Entry", {"company": COMPANY, "docstatus": 1,
                                                     "remarks": f"HP1D-SEED:{sku}:{label}"}, "name")
        rows = frappe.get_all("Stock Ledger Entry", filters={"voucher_no": voucher, "is_cancelled": 0},
                              fields=["warehouse", "actual_qty", "item_code", "serial_and_batch_bundle"])
        require(len(rows) == 1 and (rows[0].warehouse, rows[0].item_code, rows[0].actual_qty) ==
                (warehouse, item, amount), "Seed ledger mismatch")
        bundle = frappe.get_doc("Serial and Batch Bundle", rows[0].serial_and_batch_bundle)
        require(len(bundle.entries) == 1 and bundle.entries[0].batch_no == batch, "Seed bundle mismatch")
        seeds[label] = voucher
    frappe.db.rollback()
    return {"balances": {"source": 12, "transit": 0, "target": 0, "other": 0}, "source": "committed seed SLE",
            "vouchers": seeds}


def run_group(pool, send, clients, sku, number):
    dispatch = {"stockItemId": sku, "sourceLocationId": "W1/L1", "targetLocationId": "W2/L1",
                "batchSelection": selection(number), "quantity": "10", "requestId": f"{sku}-dispatch"}
    operator = clients["operator"]
    call = lambda route, data: send(operator, "POST", ENDPOINT + route, json=data)
    negatives = {}

    def denied(label, route, data, reason, profile="operator", status=417):
        negatives[label] = reject(send, clients[profile], ENDPOINT + route, data, status, reason)

    denied("missing_batch", "dispatch", {k: v for k, v in dispatch.items() if k != "batchSelection"},
           "BATCH_SELECTION_REQUIRED")
    denied("selected_batch_insufficient", "dispatch", {**dispatch, "quantity": "13", "requestId": sku + "-13"},
           "INSUFFICIENT_STOCK")
    for profile in ("query", "source_only", "target_only"):
        reason = "INVENTORY_OPERATION_DENIED" if profile == "query" else "WAREHOUSE_ACCESS_DENIED"
        denied("dispatch_" + profile, "dispatch", dispatch, reason, profile, 403)
    denied("dispatch_system_warehouse", "dispatch", {**dispatch, "sourceLocationId": TRANSIT},
           "WAREHOUSE_ACCESS_DENIED", status=403)
    frappe.db.rollback()
    initial = frappe.db.count("Stock Entry", {"company": COMPANY, "add_to_transit": 1,
                                              "remarks": ["like", f'%"{sku}"%']}) == 0
    stages = {"initial": check_stage(sku, number, (12, 0, 0), None) if initial else initial_seed(sku, number)}
    outbound = successful(call("dispatch", dispatch), {"batch": number, "dispatched": "10"})
    transfer = outbound["transferId"]
    voucher = document_for("D", transfer)
    if outbound["received"] == "0":
        require(outbound["state"] == "In Transit", "Unexpected dispatch state")
        stages["dispatch"] = check_stage(sku, number, (2, 10, 0), outbound["state"], voucher)
    receipt = {"transferId": transfer, "stockItemId": sku, "targetLocationId": "W2/L1",
               "quantity": "6", "batchSelection": selection(number), "requestId": f"{sku}-receive-6"}
    for profile in ("query", "source_only", "target_only"):
        reason = "INVENTORY_OPERATION_DENIED" if profile == "query" else "WAREHOUSE_ACCESS_DENIED"
        denied("receive_" + profile, "receive", receipt, reason, profile, 403)
    denied("receipt_wrong_sku", "receive", {**receipt, "stockItemId": next(x for x in ITEMS if x != sku)},
           "TRANSFER_RECEIPT_MISMATCH")
    denied("receipt_wrong_batch", "receive", {**receipt, "batchSelection": selection("STD62")},
           "TRANSFER_RECEIPT_MISMATCH")
    denied("receipt_wrong_target", "receive", {**receipt, "targetLocationId": "W1/L2"},
           "TRANSFER_RECEIPT_MISMATCH")
    raw_issue = {"company": COMPANY, "doctype": "Stock Entry", "stock_entry_type": "Material Issue",
                 "items": [{"item_code": ITEMS[sku], "qty": 1, "uom": "PC", "s_warehouse": TRANSIT,
                            "batch_no": batch_key(ITEMS[sku], number), "use_serial_batch_fields": 1}]}
    raw_transfer = {**raw_issue, "stock_entry_type": "Material Transfer", "add_to_transit": 1,
                    "items": [{**raw_issue["items"][0], "s_warehouse": POSITIONS["source"], "t_warehouse": TRANSIT}]}
    dispatch_row = frappe.db.get_value("Stock Entry Detail", {"parent": voucher}, "name")
    frappe.db.rollback()
    raw_receipt = {**raw_issue, "stock_entry_type": "Material Transfer", "outgoing_stock_entry": voucher,
                   "items": [{**raw_issue["items"][0], "t_warehouse": POSITIONS["target"],
                              "against_stock_entry": voucher, "ste_detail": dispatch_row}]}
    for profile in ("operator", "native"):
        for label, raw in (("raw_issue", raw_issue), ("raw_transfer", raw_transfer), ("raw_receipt", raw_receipt)):
            negatives[f"{label}_{profile}"] = reject(send, clients[profile], "/api/resource/Stock%20Entry",
                                                       raw, 403, "STAGE1B_USE_ADAPTER" if profile == "native" else None)
    negatives["ordinary_api_transit"] = reject(
        send, operator, API + "post_stock", {"action": "ISSUE", "stockItemId": sku, "warehouseLocationId": TRANSIT,
                                           "quantity": "1", "requestId": sku + "-ordinary", "batchSelection": selection(number)},
        403, "WAREHOUSE_ACCESS_DENIED",
    )
    first = successful(call("receive", receipt), {"batch": number})
    voucher_first = document_for("R", first["receiptId"])
    if first["state"] != "Completed":
        require((first["state"], first["received"], first["remaining"]) == ("Partially Received", "6", "4"),
                "First receipt mismatch")
        stages["partial"] = check_stage(sku, number, (2, 4, 6), first["state"], voucher_first)
        denied("over_receive", "receive", {**receipt, "quantity": "5", "requestId": sku + "-five"},
               "TRANSFER_RECEIPT_EXCEEDS_REMAINING")
    before = fingerprint()
    successful(call("receive", receipt), {"receiptId": first["receiptId"], "replayed": True})
    require(fingerprint() == before, "Repeated receipt changed inventory")
    denied("same_key_changed_quantity", "receive", {**receipt, "quantity": "5"}, "IDEMPOTENCY_CONFLICT")

    barrier = Barrier(2)
    final = [{**receipt, "quantity": "4", "requestId": f"{sku}-receive-4-{attempt}"} for attempt in ("a", "b")]
    pending = [pool.submit(wsgi_request, operator, "POST", ENDPOINT + "receive", json=data, barrier=barrier)
               for data in final]
    responses = [job.result(timeout=40) for job in pending]
    require(sorted(r.status_code for r in responses) == [200, 417], "Competing receipts did not elect one winner")
    rejected = next(r for r in responses if r.status_code == 417)
    require("TRANSFER_COMPLETED" in rejected.text, "Competing receipt did not observe locked completion")
    second = successful(next(r for r in responses if r.status_code == 200),
                        {"state": "Completed", "received": "10", "remaining": "0", "batch": number})
    voucher_second = document_for("R", second["receiptId"])
    stages["complete"] = check_stage(sku, number, (2, 0, 10), second["state"], voucher_second)
    denied("after_complete", "receive", {**receipt, "quantity": "1", "requestId": sku + "-complete"},
           "TRANSFER_COMPLETED")
    frappe.db.rollback()
    original = frappe.get_doc("Stock Entry", voucher)
    received = frappe.get_all("Stock Entry", filters={"outgoing_stock_entry": voucher, "docstatus": 1},
                              pluck="name")
    require(set(received) == {voucher_first, voucher_second} and original.per_transferred == 100
            and original.items[0].transferred_qty == 10, "Unexpected number or quantity of linked receipts")
    for docname, amount in ((voucher_first, 6), (voucher_second, 4)):
        row = frappe.get_doc("Stock Entry", docname).items[0]
        require((row.against_stock_entry, row.ste_detail, row.transfer_qty) ==
                (original.name, original.items[0].name, amount), "Linked receipt does not point to the original row")
    frappe.db.rollback()
    return {"sku": sku, "domain_batch": number, "dispatch": voucher, "original_row": original.items[0].name,
            "stages": stages, "negative_cases": negatives, "competing_receipts": [r.status_code for r in responses],
            "competition_replayed": second["replayed"], "receipts": [voucher_first, voucher_second]}


def main(emit):
    require(Path.cwd() == ROOT, "Wrong Bench directory")
    mode, = sys.argv[1:]
    with ThreadPoolExecutor(max_workers=2) as pool:
        def send(*args, **kwargs):
            return pool.submit(wsgi_request, *args, **kwargs).result(timeout=45)

        stage1b_verify.send = send
        stage1c_verify.send = send
        if mode == "stage1b":
            stage1b_verify.main(emit)
            return
        if mode == "stage1c":
            stage1c_verify.main(emit)
            return
        require(mode == "transfers", "Unknown verification mode")
        new_connection()
        history = committed_snapshot()
        try:
            prepare_data()
            clients = {name: session(user, roles, locations, warehouse_locations={**LOCATIONS, "Transit": TRANSIT})
                       for name, (user, roles, locations) in PROFILES.items()}
            groups = [run_group(pool, send, clients, sku, number) for sku, number in GROUPS.items()]
            new_connection()
            require(committed_snapshot() == history, "Historical Stage 1B records were modified")
            counts = {doctype: frappe.db.count(doctype, {"company": COMPANY})
                      for doctype in ("Stock Entry", "Stock Ledger Entry", "Serial and Batch Bundle", "GL Entry")}
            require(counts == {"Stock Entry": 12, "Stock Ledger Entry": 18, "Serial and Batch Bundle": 18, "GL Entry": 0}
                    and frappe.db.count("Stock Entry", {"company": COMPANY, "docstatus": 1}) == 12,
                    "Unexpected persisted company stock records")
            emit(json.dumps({"site": SITE, "company": COMPANY, "groups": groups, "stage1b_unchanged": True,
                              "company_counts": counts,
                              "transaction_isolation": frappe.db.sql("select @@tx_isolation")[0][0],
                              "historical_documents": [row.name for row in history["documents"]]},
                             ensure_ascii=False, default=str, indent=2))
        finally:
            new_connection()
            for user, _, _ in PROFILES.values():
                if frappe.db.exists("User", user):
                    doc = frappe.get_doc("User", user)
                    doc.api_key = None
                    doc.api_secret = None
                    doc.save()
            frappe.db.commit()
            frappe.destroy()


if __name__ == "__main__":
    output = sys.stdout
    with redirect_stdout(sys.stderr):
        main(lambda value: print(value, file=output))
