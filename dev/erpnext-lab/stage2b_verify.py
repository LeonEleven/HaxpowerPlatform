"""Verify the lightweight operation log through real Frappe HTTP transactions."""

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from decimal import Decimal
from pathlib import Path
from threading import Barrier
from time import monotonic, sleep
from traceback import extract_tb
from unittest.mock import patch
from urllib.parse import quote

import frappe
from frappe.database import get_db
from frappe.utils import get_datetime

import stage1b_verify
from haxpower_platform import batch_input, lightweight as light, lightweight_fixtures as fixtures, transit
from haxpower_platform.stock import QUERY_ROLE, ROLE, batch_key
from stage1b_verify import ROOT, new_connection, require, session
from stage1d_native import make_company, make_item, make_warehouses, stock_doc
from stage1d_verify import selection, successful, wsgi_request
from stage1e_evidence import decimal, digest, reconcile, records, rows

API = "/api/method/haxpower_platform.lightweight."
SOURCE, OTHER, TARGET = fixtures.LOCATIONS
USERS = {
    "operator": ("stage2b-operator@example.invalid", [ROLE], tuple(fixtures.LOCATIONS)),
    "receiver": ("stage2b-receiver@example.invalid", [ROLE], (SOURCE, TARGET)),
    "limited": ("stage2b-limited@example.invalid", [ROLE], (SOURCE,)),
    "query": ("stage2b-query@example.invalid", [QUERY_ROLE], (SOURCE, TARGET)),
    "native": ("stage2b-native@example.invalid", ["Stock User", "Stock Manager"], (SOURCE, TARGET)),
}


def setup():
    if not frappe.db.exists("Company", fixtures.COMPANY):
        make_company(fixtures.COMPANY, "H2B")
        places = make_warehouses(fixtures.COMPANY, "HP2B")
        require(places == {"source": fixtures.LOCATIONS[SOURCE], "other": fixtures.LOCATIONS[OTHER],
                           "target": fixtures.LOCATIONS[TARGET], "transit": fixtures.TRANSIT}, places)
        for sku in fixtures.ITEMS.values():
            make_item(sku)
        frappe.db.commit()


def snapshot():
    new_connection()
    return {**records(fixtures.COMPANY), light.LOG: rows(light.LOG),
            "Batch": rows("Batch", item=["in", list(fixtures.ITEMS.values())])}


def post_body(action, sku, key, qty, number, location=SOURCE, **extra):
    return {"action": action, "sku": sku, "requestId": key, "quantity": str(qty), "location": location,
            "batchSelection": selection(number), **extra}


def accept(send, endpoint, body, number=None, **expected):
    response = send(endpoint, body)
    value = successful(response, expected)
    require(value["batch"] == number and "HP-NB-" not in response.text and "HP-RB-" not in response.text,
            "Operation returned an incorrect public batch mapping")
    return value


def reject(send, endpoint, body, code, status=417, **options):
    before = digest(snapshot())
    response = send(endpoint, body, **options)
    require(response.status_code == status and code in response.text,
            f"Expected {status} {code}, got {response.status_code}: {response.text[-3500:]}")
    require(digest(snapshot()) == before, f"Rejected {endpoint} changed persisted stock or log")
    return {"http": status, "code": code, "unchanged": before}


def balance(sku, number, *expected):
    new_connection()
    item = fixtures.ITEMS[sku]
    try:
        actual = [light.available(item, batch_key(item, number), warehouse) for warehouse in
                  (fixtures.LOCATIONS[SOURCE], fixtures.TRANSIT, fixtures.LOCATIONS[TARGET], fixtures.LOCATIONS[OTHER])]
        require(actual == list(expected), f"Unexpected {sku} {number} balances {actual}")
        return actual
    finally:
        frappe.db.rollback()


def voucher_proof(operation_id):
    data = snapshot()
    log = next(row for row in data[light.LOG] if row.name == operation_id)
    parent = next(row for row in data[log.document_type] if row.name == log.document_id)
    children = [row for row in data[log.document_type + (" Detail" if log.document_type == "Stock Entry" else " Item")]
                if row.parent == parent.name]
    native = [row for row in data["Stock Ledger Entry"] if row.voucher_no == parent.name]
    bundles = [row for row in data["Serial and Batch Bundle"] if row.voucher_no == parent.name]
    return {"log": log, "document": parent, "lines": children, "sle": native, "bundles": bundles}


def correct_body(original, key, reason):
    return {"originalId": original["operationId"], "requestId": key, "reason": reason}


def corrected(send, original, key, number, reason):
    before = voucher_proof(original["operationId"])
    body = correct_body(original, key, reason)
    posted = accept(send, "correct", body, number, replayed=False)
    require(voucher_proof(original["operationId"]) == before, "Correction changed original document or SLE")
    proof = voucher_proof(posted["operationId"])
    log = proof["log"]
    require(log.related_operation == original["operationId"] and log.reason == reason
            and log.actor == USERS["operator"][0] and log.operated_at and log.document_id != before["document"].name,
            "Missing correction relationship, actor or reason")
    baseline = digest(snapshot())
    accept(send, "correct", body, number, operationId=posted["operationId"], replayed=True)
    require(digest(snapshot()) == baseline, "Retry applied a correction twice")
    other_key = reject(send, "correct", {**body, "requestId": key + "-duplicate"}, "STAGE2B_ALREADY_CORRECTED")
    changed = reject(send, "correct", {**body, "reason": reason + " 复核"}, "IDEMPOTENCY_CONFLICT")
    return {"original": original, "correction": posted, "native": [before["document"].name, proof["document"].name],
            "same_key_replay": True, "different_key": other_key, "changed_reason": changed}


def simple(send):
    output = {}
    for action, number, qty, initial, after in (
        ("RECEIPT", "STD61", 12, 0, 12), ("ISSUE", None, 4, 12, 8), ("COUNT", None, 5, 8, 5),
    ):
        if initial:
            accept(send, "post", post_body("RECEIPT", action, action + "-seed", initial, number), number)
        original = accept(send, "post", post_body(action, action, action + "-original", qty, number), number)
        balance(action, number, after, 0, 0, 0)
        final = corrected(send, original, action + "-correct", number, "实验原单录入错误")
        balance(action, number, initial, 0, 0, 0)
        output[action] = {**final, "balances": [initial, after, initial]}
    return output


def transfers(send):
    output = {}
    for sku, number, qty in (("PARTIAL", None, 6), ("COMPLETE", "STD61", 10)):
        accept(send, "post", post_body("RECEIPT", sku, sku + "-seed", 12, number), number)
        dispatch = accept(send, "post", post_body("DISPATCH", sku, sku + "-dispatch", 10, number,
                                                  targetLocation=TARGET), number)
        balance(sku, number, 2, 10, 0, 0)
        body = {"dispatchId": dispatch["operationId"], "quantity": str(qty), "requestId": sku + "-receive"}
        response = send("receive", body, profile="receiver")
        received = successful(response, {"batch": number})
        balance(sku, number, 2, 10 - qty, qty, 0)
        proof = voucher_proof(received["operationId"])
        origin = voucher_proof(dispatch["operationId"])
        line = proof["lines"][0]
        require((proof["log"].related_operation, line.against_stock_entry, line.ste_detail)
                == (dispatch["operationId"], origin["document"].name, origin["lines"][0].name)
                and proof["log"].actor == USERS["receiver"][0], "Receipt lost origin or receiver")
        if sku == "PARTIAL":
            final = corrected(send, dispatch, "partial-return", number, "退回未接收数量")
            balance(sku, number, 6, 0, 6, 0)
            closed = reject(send, "receive", {**body, "quantity": "1", "requestId": "after-return"},
                            "STAGE2B_TRANSFER_CLOSED", profile="receiver")
            output[sku] = {**final, "receipt": received, "receipts_closed": closed,
                           "balances": [[12, 0, 0], [2, 10, 0], [2, 4, 6], [6, 0, 6]]}
        else:
            downstream = accept(send, "post", post_body("ISSUE", sku, "downstream-issue", 7, number,
                                                        location=TARGET), number)
            balance(sku, number, 2, 0, 3, 0)
            blocked = reject(send, "correct", correct_body(dispatch, "complete-correct", "撤回整张完成移库"),
                             light.REVIEW_REQUIRED)
            require("人工核对" in json.dumps(send("correct", correct_body(dispatch, "complete-explain", "同一阻塞")).json(),
                                             ensure_ascii=False), "No business explanation")
            output[sku] = {"dispatch": dispatch, "receipt": received, "downstream": downstream,
                           "blocked": blocked, "balances": [2, 0, 3]}
    return output


def batches(send):
    legacy = [None, "", "　 ", '""', "‘’", " STD61 ", "STD61", "Std61", ' Ａ-"B" ', "hp-nb-reserved"]
    preview = [{"row": i, "raw": raw, **batch_input.preview_legacy(raw)} for i, raw in enumerate(legacy, 1)]
    rejected = []
    probe = post_body("RECEIPT", "BATCH", "batch-invalid", 1, None)
    for index, (batch, code) in enumerate((
        ({}, "BATCH_SELECTION_REQUIRED"), (selection(""), "BATCH_TEXT_EMPTY"),
        (selection("　 \t"), "BATCH_TEXT_EMPTY"), (selection("''"), "BATCH_QUOTES_REQUIRE_CONFIRMATION"),
        (selection("“ ”"), "BATCH_QUOTES_REQUIRE_CONFIRMATION"),
        (selection(" hp-NB-reserved "), "INTERNAL_BATCH_ID_FORBIDDEN"),
        ({"mode": "NO_BATCH", "batchNo": "STD61"}, "INVALID_BATCH_SELECTION"),
    )):
        rejected.append(reject(send, "post", {**probe, "requestId": f"batch-invalid-{index}", "batchSelection": batch}, code))
    batch_results = {}
    for label, raw, normalized, qty in (("trim", "　STD61 ", "STD61", 3), ("null", None, None, 4),
                                        ("meaning", '　Ａ-"B" ', 'Ａ-"B"', 2), ("case", "Std61", "Std61", 1)):
        body = post_body("RECEIPT", "BATCH", "batch-" + label, qty, raw)
        posted = accept(send, "post", body, normalized, replayed=False)
        accept(send, "post", {**body, "batchSelection": selection(normalized)}, normalized,
               replayed=True, operationId=posted["operationId"])
        balance("BATCH", normalized, qty, 0, 0, 0)
        batch_results[label] = posted
    return {"rejected": rejected, "posted": batch_results, "synthetic_import_preview": preview}


def faults(send, pool, client):
    original = accept(send, "post", post_body("RECEIPT", "FAULT", "fault-origin", 10, "STD61"), "STD61")
    body = correct_body(original, "fault-correct", "测试纠错日志原子性")
    voucher_view = {}

    def inspect(voucher, expected_logs):
        filters = {"company": fixtures.COMPANY, "voucher_no": voucher}
        require(frappe.db.get_value("Stock Entry", voucher, "docstatus") == 1
                and frappe.db.count("Stock Ledger Entry", filters) == 1
                and frappe.db.count("Serial and Batch Bundle", filters) == 1
                and frappe.db.count(light.LOG, {"document_id": voucher}) == expected_logs,
                "Incorrect failure injection stage")
        voucher_view.update({"voucher": voucher, "local_logs": expected_logs,
                             "connection": frappe.db.sql("select connection_id()")[0][0]})

    def fail_log_insert(doc):
        inspect(doc.document_id, 0)
        frappe.throw("STAGE2B_INJECTED_LOG_INSERT_FAILURE")

    from haxpower_platform.haxpower_platform.doctype.stage_2b_stock_operation.stage_2b_stock_operation import (
        Stage2BStockOperation,
    )

    with patch.object(Stage2BStockOperation, "before_insert", fail_log_insert):
        rejected = reject(send, "correct", body, "STAGE2B_INJECTED_LOG_INSERT_FAILURE")
    assert_no_log = {"observed": dict(voucher_view), "rejected": rejected}
    balance("FAULT", "STD61", 10, 0, 0, 0)

    normal_write = light.write_log

    def fail_commit(*args, **kwargs):
        log = normal_write(*args, **kwargs)

        def disconnect():
            inspect(log.document_id, 1)
            conf = frappe.conf
            other = get_db(host=conf.db_host, port=conf.db_port, user=conf.db_user or conf.db_name,
                           password=conf.db_password, cur_db_name=conf.db_name)
            try:
                donor = other.sql("select connection_id()")[0][0]
                require(donor != voucher_view["connection"], "Fault must act on a distinct lab connection")
                other.sql("kill connection %s", (voucher_view["connection"],))
                voucher_view["fault_connection"] = donor
            finally:
                other.close()

        frappe.db.before_commit.add(disconnect)
        return log

    before = digest(snapshot())
    with patch.object(light, "write_log", fail_commit):
        try:
            send("correct", body)
        except Exception as exc:
            require(exc.args[0] in (2006, 2013, 4031) and any(
                frame.name == "commit" for frame in extract_tb(exc.__traceback__)
            ), f"Unexpected commit exception {exc!r}")
            voucher_view["db_error"] = exc.args[0]
        else:
            raise RuntimeError("Commit must fail after the test request connection was stopped")
    require(digest(snapshot()) == before, "Commit failure left stock or light audit records")
    balance("FAULT", "STD61", 10, 0, 0, 0)

    gate, locks = Barrier(2), []
    normal_lock = transit.lock_company

    def concurrent_lock(scope):
        connection = frappe.db.sql("select connection_id()")[0][0]
        gate.wait(timeout=12)
        start = monotonic()
        normal_lock(scope)
        locks.append({"connection": connection, "wait_ms": round(1000 * (monotonic() - start))})
        sleep(0.25)

    with patch.object(transit, "lock_company", concurrent_lock):
        futures = [pool.submit(wsgi_request, client, "POST", API + "correct", json=body) for _ in range(2)]
        replies = [successful(job.result(timeout=45), {"batch": "STD61"}) for job in futures]
    require(len({row["connection"] for row in locks}) == 2 and max(row["wait_ms"] for row in locks) >= 180,
            f"Didn't establish real request overlap: {locks}")
    require({row["replayed"] for row in replies} == {False, True}
            and len({row["operationId"] for row in replies}) == 1, "Same-key correction applied twice")
    balance("FAULT", "STD61", 0, 0, 0, 0)
    return {"insert_failure": assert_no_log, "commit_failure": voucher_view, "before_after": before,
            "concurrent_retry": {"responses": replies, "locks": locks}}


def permissions(send, request, clients):
    data = snapshot()
    delta = post_body("ISSUE", "BATCH", "perm-issue", 1, None)
    sample = next(row for row in data[light.LOG] if row.operation_type == "RECEIPT")
    corrections = next(row for row in data[light.LOG] if row.operation_type == "CORRECTION")
    dispatch = next(row for row in data[light.LOG] if row.operation_type == "DISPATCH")
    denials = {
        "query": reject(send, "post", delta, "INVENTORY_OPERATION_DENIED", 403, profile="query"),
        "unauthorized_target": reject(send, "correct", {"originalId": dispatch.name, "reason": "权限探针",
                                                       "requestId": "perm-correct"}, "WAREHOUSE_ACCESS_DENIED",
                                      403, profile="limited"),
        "insufficient_no_batch": reject(send, "post", {**delta, "quantity": "5"}, "INSUFFICIENT_STOCK"),
    }
    base = "/api/resource/" + quote(light.LOG)
    native = []
    for profile in ("operator", "query", "native"):
        for method, path in (("POST", base), ("PUT", base + "/" + corrections.name),
                             ("DELETE", base + "/" + corrections.name)):
            reply = request(clients[profile], method, path, json={"reason": "FORGED"})
            require(reply.status_code == 403, f"Log REST {profile} {method}: {reply.status_code}")
            native.append([profile, method, reply.status_code])
    candidate = stock_doc(fixtures.COMPANY, fixtures.ITEMS["BATCH"], batch_key(fixtures.ITEMS["BATCH"], None),
                          "1", "Material Receipt", target=fixtures.LOCATIONS[SOURCE]).as_dict()
    reply = request(clients["native"], "POST", "/api/resource/Stock%20Entry", json=candidate)
    require(reply.status_code == 403 and "STAGE1B_USE_ADAPTER" in reply.text, "Native stock bypass was not rejected")
    require(digest(snapshot()) == digest(data), "Native write changed stock or logs")

    def denied(fn, code):
        before = digest(snapshot())
        try:
            fn()
        except frappe.PermissionError as error:
            require(code in str(error), f"Unexpected administrator rejection: {error}")
        else:
            raise RuntimeError("Unsafe administrator action succeeded")
        finally:
            frappe.db.rollback()
        require(digest(snapshot()) == before, "Privileged denial changed stock or logs")
        return code

    def edit_log():
        doc = frappe.get_doc(light.LOG, corrections.name)
        doc.reason = "FORGED"
        doc.save()

    def queue_rebuild():
        frappe.get_doc({"doctype": "Repost Item Valuation", "company": fixtures.COMPANY,
                        "based_on": "Transaction", "voucher_type": sample.document_type,
                        "voucher_no": sample.document_id, "posting_date": get_datetime().date(),
                        "recreate_stock_ledgers": 1}).insert()

    denials["admin_cancel"] = denied(lambda: frappe.get_doc(sample.document_type, sample.document_id).cancel(),
                                    "STAGE1B_USE_ADAPTER")
    denials["admin_change_log"] = denied(edit_log, "STAGE1F_APPEND_ONLY")
    denials["admin_delete_log"] = denied(lambda: frappe.delete_doc(light.LOG, corrections.name),
                                        "STAGE1F_APPEND_ONLY")
    denials["admin_rebuild"] = denied(queue_rebuild, "STAGE1F_RECREATE_LEDGER_DENIED")
    denials["admin_delete_company"] = denied(lambda: frappe.get_doc({
        "doctype": "Transaction Deletion Record", "company": fixtures.COMPANY,
    }).insert(), "STAGE1F_BULK_DELETE_DENIED")
    return {"denials": denials, "native_log": native, "native_stock_entry_http": reply.status_code}


def durable():
    data = snapshot()
    engine = reconcile(fixtures.COMPANY)
    logs = {row.name: row for row in data[light.LOG]}
    docs = {(kind, row.name): row for kind in ("Stock Entry", "Stock Reconciliation") for row in data[kind]}
    lines = {row.name: row for row in data["Stock Entry Detail"] + data["Stock Reconciliation Item"]}
    require(len(docs) == len(logs) and set(docs) == {(e.document_type, e.document_id) for e in logs.values()},
            "Missing stock operation or duplicate voucher")
    require(all(not row.is_cancelled for row in data["Stock Ledger Entry"]), "Unexpected cancelled native SLE")
    detail = {}
    for log in logs.values():
        key = (log.document_type, log.document_id)
        doc, line = docs[key], lines[log.document_line]
        require(doc.docstatus == 1 and line.parent == doc.name and doc.owner == log.actor
                and doc.creation and log.operated_at, "Native voucher and audit log did not match")
        selected = [row for row in data["Stock Ledger Entry"] if (row.voucher_type, row.voucher_no) == key]
        number = light.batch_from_line(line)
        require(selected and all(sl.voucher_detail_no == line.name and sl.item_code == line.item_code
                                for sl in selected), "Missing effective SLE")
        allocations = [b for b in data["Serial and Batch Entry"] if b.parent in {
            row.serial_and_batch_bundle for row in selected
        }]
        require(allocations and all(row.batch_no == line.batch_no for row in allocations), "Wrong effective Batch")
        by_wh = {}
        for sl in selected:
            by_wh[sl.warehouse] = by_wh.get(sl.warehouse, Decimal(0)) + decimal(sl.actual_qty)
        if log.operation_type == "CORRECTION":
            original = logs[log.related_operation]
            prior_line = lines[original.document_line]
            require(log.reason and (line.batch_no, line.item_code) == (prior_line.batch_no, prior_line.item_code),
                    "Correction lost batch, SKU or reason")
            orig_sle = [row for row in data["Stock Ledger Entry"] if row.voucher_no == original.document_id]
            if original.operation_type == "DISPATCH":
                after_receipts = decimal(prior_line.transfer_qty) - decimal(prior_line.transferred_qty)
                require(by_wh == {prior_line.s_warehouse: after_receipts, fixtures.TRANSIT: -after_receipts},
                        "Return amount differed from dispatch remainder")
            else:
                net = sum((decimal(sl.actual_qty) for sl in orig_sle), Decimal(0))
                require(len(by_wh) == 1 and sum(by_wh.values()) == -net
                        and set(by_wh) == {sl.warehouse for sl in orig_sle}, "Wrong inverse delta")
        version = frappe.get_all("Version", filters={"ref_doctype": log.document_type, "docname": doc.name},
                                 fields=["owner", "creation", "data"], order_by="creation")
        comments = frappe.get_all("Comment", filters={"reference_doctype": log.document_type,
                                  "reference_name": doc.name}, fields=["owner", "creation", "comment_type", "content"])
        detail[log.name] = {
            "type": log.operation_type, "doc": doc.name, "row": line.name, "batch": number,
            "related": log.related_operation, "reason": log.reason, "actor": log.actor, "operated_at": log.operated_at,
            "native": {"owner": doc.owner, "creation": doc.creation, "modified": doc.modified,
                       "modified_by": doc.modified_by, "posting_date": doc.posting_date, "posting_time": doc.posting_time,
                       "docstatus": doc.docstatus, "amended_from": doc.amended_from,
                       "version_fields": [json.loads(v.data).get("changed", []) for v in version],
                       "comments": comments, "signed_by_warehouse": by_wh, "sle": [sl.name for sl in selected]},
        }
    frappe.db.rollback()
    return {"size": {table: len(values) for table, values in data.items()},
            "connection": frappe.db.sql("select connection_id(), @@tx_isolation")[0],
            "stock": engine, "log": detail, "log_digest": digest(data[light.LOG]),
            "log_schema": [df.fieldname for df in frappe.get_meta(light.LOG).fields]}


def repost():
    from erpnext.stock.doctype.repost_item_valuation.repost_item_valuation import repost as run_job

    before = durable()
    candidate = next(row for row in before["log"].values()
                     if row["type"] == "RECEIPT" and row["batch"] == "STD61")
    doc = frappe.get_doc("Stock Entry", candidate["doc"])
    job = frappe.get_doc({"doctype": "Repost Item Valuation", "company": fixtures.COMPANY,
                         "based_on": "Transaction", "voucher_type": doc.doctype, "voucher_no": doc.name,
                         "posting_date": doc.posting_date, "posting_time": doc.posting_time,
                         "recreate_stock_ledgers": 0}).insert()
    job.submit()
    frappe.db.commit()
    run_job(job)
    after = durable()
    require(frappe.db.get_value(job.doctype, job.name, "status") == "Completed", "Native repost did not complete")
    require(before["log_digest"] == after["log_digest"]
            and all(before["stock"][key] == after["stock"][key] for key in ("balances", "bins", "bundle_entries")),
            "Repost changed quantities, batch allocation or operation log")
    comparable = ("actual_qty", "qty_after_transaction", "serial_and_batch_bundle", "is_cancelled", "voucher_no")
    changes = []
    for a, b in zip(before["stock"]["ledger"], after["stock"]["ledger"], strict=True):
        require(a["name"] == b["name"] and all(a[k] == b[k] for k in comparable), "Repost changed active ledger")
        if a["modified"] != b["modified"]:
            changes.append(a["name"])
    return {"job": job.name, "status": "Completed", "metadata_modified": changes,
            "log_and_quantities_identical": True}


def main(emit):
    mode, = sys.argv[1:]
    require(Path.cwd() == ROOT, "Wrong Bench directory")
    new_connection()
    if mode in ("final", "repost"):
        emit({"final": durable, "repost": repost}[mode]())
        frappe.destroy()
        return
    operations = {"simple": simple, "transfer": transfers, "batch": batches}
    require(mode in (*operations, "fault", "permissions"), "Unknown mode")
    setup()
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            def request(*args, **kwargs):
                return pool.submit(wsgi_request, *args, **kwargs).result(timeout=45)

            stage1b_verify.send = request
            clients = {name: session(*spec, warehouse_locations=fixtures.LOCATIONS) for name, spec in USERS.items()}

            def send(endpoint, body, *, profile="operator"):
                return request(clients[profile], "POST", API + endpoint, json=body)

            if mode == "fault":
                result = faults(send, pool, clients["operator"])
            elif mode == "permissions":
                result = permissions(send, request, clients)
            else:
                result = operations[mode](send)
            emit(result)
    finally:
        new_connection()
        for user, _, _ in USERS.values():
            if frappe.db.exists("User", user):
                doc = frappe.get_doc("User", user)
                doc.api_key = doc.api_secret = None
                doc.save()
        frappe.db.commit()
        require(all(not any(frappe.db.get_value("User", spec[0], ["api_key", "api_secret"]))
                    for spec in USERS.values() if frappe.db.exists("User", spec[0])), "Temporary keys were not removed")
        frappe.destroy()


if __name__ == "__main__":
    output = sys.stdout
    with redirect_stdout(sys.stderr):
        main(lambda evidence: print(json.dumps(evidence, indent=2, ensure_ascii=False, default=str), file=output))
