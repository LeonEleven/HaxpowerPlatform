"""Verify Stage 1F through Frappe WSGI and independently reconnect for evidence."""

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from decimal import Decimal
from pathlib import Path
from threading import Barrier
from traceback import extract_tb
from unittest.mock import patch
from urllib.parse import quote

import frappe
from frappe.database import get_db
from frappe.utils import get_datetime

import stage1b_verify
from haxpower_platform import audit, audit_fixtures as fixtures, transit
from haxpower_platform.stock import QUERY_ROLE, ROLE, batch_key
from stage1b_verify import ROOT, new_connection, require, session
from stage1d_native import make_company, make_item, make_warehouses, stock_doc
from stage1d_verify import selection, successful, wsgi_request
from stage1e_evidence import decimal, digest, history, reconcile, records, rows

API = "/api/method/haxpower_platform.audit."
SOURCE, OTHER, TARGET = fixtures.LOCATIONS
USERS = {
    "operator": ("stage1f-operator@example.invalid", [ROLE], (SOURCE, TARGET)),
    "query": ("stage1f-query@example.invalid", [QUERY_ROLE], (SOURCE,)),
    "limited": ("stage1f-limited@example.invalid", [ROLE], (OTHER,)),
    "native": ("stage1f-native@example.invalid", ["Stock User", "Stock Manager"], (SOURCE, TARGET)),
}


def setup():
    if not frappe.db.exists("Company", fixtures.COMPANY):
        make_company(fixtures.COMPANY, "H1F")
        places = make_warehouses(fixtures.COMPANY, "HP1F")
        require(places == {"source": fixtures.LOCATIONS[SOURCE], "other": fixtures.LOCATIONS[OTHER],
                           "target": fixtures.LOCATIONS[TARGET], "transit": fixtures.TRANSIT},
                "Unexpected lab warehouse mapping")
        for item in fixtures.ITEMS.values():
            make_item(item)
        frappe.db.commit()


def snapshot():
    new_connection()
    return {
        **records(fixtures.COMPANY),
        "Batch": rows("Batch", item=["in", list(fixtures.ITEMS.values())]),
        audit.EVENT: rows(audit.EVENT), audit.TASK: rows(audit.TASK),
    }


def movement(action, sku, request_id, qty, number, **locations):
    body = {"action": action, "sku": sku, "quantity": str(qty), "requestId": request_id,
            "batchSelection": selection(number), "location": SOURCE}
    return {**body, **locations}


def accept(send, endpoint, body, batch=None, **expected):
    reply = send(endpoint, body)
    result = successful(reply, expected)
    require("HP-NB-" not in reply.text and "HP-RB-" not in reply.text, "Internal ID in business response")
    require(result["batch"] == batch, "Incorrect domain batch")
    return result


def reject(send, endpoint, body, reason, status=417, **options):
    before = digest(snapshot())
    reply = send(endpoint, body, **options)
    require(reply.status_code == status and reason in reply.text,
            f"Expected {status} {reason}, got {reply.status_code}: {reply.text[-3600:]}")
    require(digest(snapshot()) == before, f"Rejected operation changed DB: {reason}")
    return {"http": status, "reason": reason, "snapshot": before}


def task_for(send, sku, number, label, **options):
    return accept(send, "create_count", {"sku": sku, "location": SOURCE, "batchSelection": selection(number),
                                        "requestId": label, **options}, batch=number)


def count_body(task, key, qty):
    return {"taskId": task["taskId"], "requestId": key, "quantity": str(qty)}


def balance(sku, number, *quantities):
    new_connection()
    item = fixtures.ITEMS[sku]
    try:
        actual = [audit.bucket(item, batch_key(item, number), location)[0]
                  for location in (SOURCE, "TRANSIT", TARGET, OTHER)]
    finally:
        frappe.db.rollback()
    require(actual == list(quantities), f"Unexpected balance for {sku} {number}: {actual}")
    return actual


def stock_and_corrections(send):
    result = {}
    for action, qty, after, restored in (("RECEIPT", 12, 12, 0), ("ISSUE", 4, 8, 12)):
        if action == "ISSUE":
            accept(send, "move", movement("RECEIPT", "AUDIT", "audit-NB-seed", 12, None))
        number = "STD61" if action == "RECEIPT" else None
        original = accept(send, "move", movement(action, "AUDIT", f"audit-{action}", qty, number),
                          batch=number)
        require(balance("AUDIT", number, after, 0, 0, 0)[0] == after, "Wrong movement balance")
        before_event = next(row for row in snapshot()[audit.EVENT] if row.name == original["eventId"])
        body = {"eventId": original["eventId"], "requestId": f"audit-correct-{action}"}
        correction = accept(send, "correct", body, batch=number, replayed=False)
        check = snapshot()
        require(next(row for row in check[audit.EVENT] if row.name == original["eventId"]) == before_event,
                "Original audit row changed during correction")
        reverse = next(row for row in check[audit.EVENT] if row.name == correction["eventId"])
        require(reverse.reversal_of == original["eventId"], "Missing correction link")
        balance("AUDIT", number, restored, 0, 0, 0)
        unchanged = digest(snapshot())
        accept(send, "correct", body, batch=number, replayed=True, eventId=correction["eventId"])
        require(digest(snapshot()) == unchanged, "Correction replay changed stock")
        result[action] = {"event": original, "correction": correction, "balances": [after, restored]}
    return result


def transfers(send):
    results = {}
    accept(send, "move", movement("RECEIPT", "TRANSFER", "transfer-NB-seed", 12, None))
    original = accept(send, "move", movement("DISPATCH", "TRANSFER", "partial-dispatch", 10, None,
                                            targetLocation=TARGET))
    dispatch = {"dispatchId": original["eventId"], "targetLocation": TARGET}
    balance("TRANSFER", None, 2, 10, 0, 0)
    partial = accept(send, "move", movement("RECEIVE", "TRANSFER", "partial-receive", 6, None, **dispatch))
    balance("TRANSFER", None, 2, 4, 6, 0)
    correction = accept(send, "correct", {"eventId": original["eventId"], "requestId": "partial-correct"})
    balance("TRANSFER", None, 6, 0, 6, 0)
    results["closed_reception"] = reject(send, "move", movement("RECEIVE", "TRANSFER", "after-return", 1, None,
                                                               **dispatch), "STAGE1F_TRANSFER_CLOSED_OR_EXCEEDS_REMAINDER")
    results["partial"] = {"dispatch": original, "receive": partial, "return": correction,
                          "balances": [[2, 10, 0], [2, 4, 6], [6, 0, 6]]}

    accept(send, "move", movement("RECEIPT", "TRANSFER", "transfer-real-seed", 10, "STD61"), batch="STD61")
    done = accept(send, "move", movement("DISPATCH", "TRANSFER", "complete-dispatch", 10, "STD61",
                                        targetLocation=TARGET), batch="STD61")
    receive = accept(send, "move", movement("RECEIVE", "TRANSFER", "complete-receive", 10, "STD61",
                                           dispatchId=done["eventId"], targetLocation=TARGET), batch="STD61")
    downstream = accept(send, "move", movement("ISSUE", "TRANSFER", "downstream-issue", 7, "STD61",
                                              location=TARGET), batch="STD61")
    balance("TRANSFER", "STD61", 0, 0, 3, 0)
    results["completed"] = {"dispatch": done, "receive": receive, "downstream": downstream,
                            "balances": [0, 0, 3],
                            "correction": reject(send, "correct", {"eventId": done["eventId"],
                                                                  "requestId": "completed-correct"},
                                                 "STAGE1F_COMPLETED_TRANSFER_REQUIRES_REVIEW")}
    return results


def count_cases(send):
    require_b = "STAGE1F_COUNT_STALE"
    result = {}
    for action, number in (("RECEIPT", "STD61"), ("ISSUE", None), ("DISPATCH", "STD62")):
        accept(send, "move", movement("RECEIPT", "COUNT", f"stale-{action}-seed", 10, number), batch=number)
        old = task_for(send, "COUNT", number, f"before-{action}")
        require(old["baseline"] == "10", "Wrong count baseline")
        change = {"targetLocation": TARGET} if action == "DISPATCH" else {}
        delta = accept(send, "move", movement(action, "COUNT", f"stale-{action}-write", 2, number, **change),
                       batch=number)
        now = task_for(send, "COUNT", number, f"after-{action}")
        require(now["version"] != old["version"], "Version did not advance")
        rejection = reject(send, "submit_count", count_body(old, f"stale-{action}-submit", 5), require_b)
        result[action] = {"old": old, "change": delta, "new": now, "rejection": rejection}

    before = task_for(send, "COUNT", None, "aba-before")
    accept(send, "move", movement("ISSUE", "COUNT", "aba-issue", 2, None))
    accept(send, "move", movement("RECEIPT", "COUNT", "aba-receipt", 2, None))
    after = task_for(send, "COUNT", None, "aba-after")
    require(before["baseline"] == after["baseline"] == "8" and before["version"] != after["version"],
            "Expected identical balance with changed version after round trip")
    result["round_trip"] = {"before": before, "after": after,
                            "rejection": reject(send, "submit_count", count_body(before, "aba-submit", 6), require_b)}

    old = task_for(send, "COUNT", None, "two-1")
    second = task_for(send, "COUNT", None, "two-2")
    require(old["baseline"] == second["baseline"] == "8" and old["version"] == second["version"]
            and old["taskId"] != second["taskId"], "Two independent tasks did not share baseline")
    # Changes to another batch/warehouse must not invalidate this exact bucket.
    accept(send, "move", movement("RECEIPT", "COUNT", "unrelated-batch", 1, "STD61"), batch="STD61")
    accept(send, "move", movement("RECEIPT", "COUNT", "unrelated-warehouse", 1, None, location=TARGET))
    first = count_body(old, "two-submit-1", 5)
    posted = accept(send, "submit_count", first, replayed=False)
    balance("COUNT", None, 5, 0, 1, 0)
    prior = digest(snapshot())
    accept(send, "submit_count", first, replayed=True, eventId=posted["eventId"])
    require(digest(snapshot()) == prior, "Count replay changed pending or committed stock")
    different_payload = reject(send, "submit_count", {**first, "quantity": "6"}, "IDEMPOTENCY_CONFLICT")
    second_reject = reject(send, "submit_count", count_body(second, "two-submit-2", 6), require_b)
    reused = reject(send, "submit_count", count_body(old, "reuse-task", 6), "STAGE1F_COUNT_ALREADY_SUBMITTED")
    adjusted = accept(send, "correct", {"eventId": posted["eventId"], "requestId": "correct-count"})
    balance("COUNT", None, 8, 0, 1, 0)
    result["two_tasks"] = {"tasks": [old, second], "accepted": posted, "corrected": adjusted,
                           "same_key_replay": True, "different_payload": different_payload, "stale": second_reject,
                           "already_submitted": reused, "balances": [8, 5, 8]}

    result["zero"] = []
    for number, suffix, qty in ((None, "null", 3), ("STD61", "batch", 2)):
        zero = task_for(send, "ZERO", number, "zero-" + suffix)
        require(zero["baseline"] == "0", "Empty bucket did not produce zero baseline")
        positive = accept(send, "submit_count", count_body(zero, "zero-submit-" + suffix, qty), batch=number)
        balance("ZERO", number, qty, 0, 0, 0)
        result["zero"].append({"task": zero, "accepted": positive, "balance": qty})
    return result


def concurrent_counts(send, pool, client):
    accept(send, "move", movement("RECEIPT", "RACE", "race-seed", 10, "STD61"), batch="STD61")
    tasks = [task_for(send, "RACE", "STD61", f"race-create-{i}") for i in range(2)]
    require(tasks[0]["version"] == tasks[1]["version"] and tasks[0]["baseline"] == tasks[1]["baseline"] == "10",
            "Concurrent tasks do not share baseline")
    gate = Barrier(2)
    connections = []
    original = transit.lock_company

    def overlap(fixtures):
        connections.append(frappe.db.sql("select connection_id()")[0][0])
        gate.wait(timeout=10)
        return original(fixtures)

    with patch.object(transit, "lock_company", overlap):
        futures = [pool.submit(wsgi_request, client, "POST", API + "submit_count",
                               json=count_body(task, f"race-submit-{i}", qty))
                   for i, (task, qty) in enumerate(zip(tasks, (7, 8), strict=True))]
        replies = [future.result(timeout=45) for future in futures]
    require(len(set(connections)) == 2 and sorted(reply.status_code for reply in replies) == [200, 417],
            f"Unexpected concurrent responses/connections: {[reply.text[-1500:] for reply in replies]}")
    failed = next(reply for reply in replies if not reply.ok)
    require("STAGE1F_COUNT_STALE" in failed.text, "Race loser was not rejected by the version check")
    winner = 0 if replies[0].ok else 1
    latest = balance("RACE", "STD61", (7, 8)[winner], 0, 0, 0)
    return {"tasks": tasks, "http": [reply.status_code for reply in replies], "connections": connections,
            "winner": replies[winner].json()["message"], "balance": latest[0], "reason": "STAGE1F_COUNT_STALE"}


def fault_cases(send):
    result = {}
    for stage, key, number in (("event_insert", "fault-insert", "STD61"), ("database_commit", "fault-commit", None)):
        observations = {}
        before = snapshot()
        body = movement("RECEIPT", "FAULT", key, 3, number)
        original = audit.save_event

        def inspect_local(voucher, expected_events):
            filters = {"company": fixtures.COMPANY, "voucher_no": voucher}
            require(frappe.db.get_value("Stock Entry", voucher, "docstatus") == 1
                    and frappe.db.count("Stock Ledger Entry", filters) == 1
                    and frappe.db.count("Serial and Batch Bundle", filters) == 1
                    and frappe.db.count(audit.EVENT, {"document_id": voucher}) == expected_events,
                    "Failure was injected at the wrong boundary")
            observations.update({"voucher": voucher, "events_in_failed_tx": expected_events,
                                 "connection": frappe.db.sql("select connection_id()")[0][0]})

        def fail_at_append(doc):
            inspect_local(doc.document_id, 0)
            raise RuntimeError("STAGE1F_TEST_AUDIT_INSERT_FAILURE")

        def fail_at_commit(*args, **kwargs):
            event = original(*args, **kwargs)

            def disconnect_request_before_commit():
                inspect_local(event.document_id, 1)
                audit.require_warehouse(fixtures.LOCATIONS[SOURCE], fixtures.COMPANY)
                conf = frappe.conf
                killer = get_db(host=conf.db_host, port=conf.db_port, user=conf.db_user or conf.db_name,
                                password=conf.db_password, cur_db_name=conf.db_name)
                try:
                    # Same site principal terminates only the connection of this synthetic request.
                    other = killer.sql("select connection_id()")[0][0]
                    require(other != observations["connection"], "Fault donor reused the request connection")
                    killer.sql("kill connection %s", (observations["connection"],))
                    observations["fault_connection"] = other
                finally:
                    killer.close()

            frappe.db.before_commit.add(disconnect_request_before_commit)
            return event

        if stage == "event_insert":
            # Enter the Audit Event controller before its INSERT and fail while the stock is visible locally.
            from haxpower_platform.haxpower_platform.doctype.stage_1f_audit_event.stage_1f_audit_event import (
                Stage1FAuditEvent,
            )

            with patch.object(Stage1FAuditEvent, "before_insert", fail_at_append):
                rejected = send("move", body)
            require(rejected.status_code == 500 and "STAGE1F_TEST_AUDIT_INSERT_FAILURE" in rejected.text,
                    f"Wrong append error: {rejected.text[-2000:]}")
            observations["http"] = rejected.status_code
        else:
            with patch.object(audit, "save_event", fail_at_commit):
                try:
                    send("move", body)
                except Exception as error:
                    trace = extract_tb(error.__traceback__)
                    require(any(frame.name == "commit" for frame in trace)
                            and error.args[0] in (2006, 2013, 4031), f"Unexpected commit exception: {error!r}")
                    observations["database_error"] = error.args[0]
                else:
                    raise RuntimeError("Database commit unexpectedly succeeded")
        require(digest(snapshot()) == digest(before), f"Partial rows survived {stage}")
        accepted = accept(send, "move", body, batch=number, replayed=False)
        accept(send, "move", body, batch=number, replayed=True, eventId=accepted["eventId"])
        balance("FAULT", number, 3, 0, 0, 0)
        result[stage] = {**observations, "before_after_sha256": digest(before), "successful_retry": accepted}
    return result


def permissions(send, request, clients):
    data = snapshot()
    sample = next(e for e in data[audit.EVENT] if e.item_code == fixtures.ITEMS["AUDIT"] and e.action == "RECEIPT")
    require(sample.actor == USERS["operator"][0], "Wrong audit actor")
    endpoint = "/api/resource/" + quote(audit.EVENT)
    disabled = [("POST", endpoint, {"action": "FORGED"}), ("PUT", endpoint + "/" + sample.name, {"action": "FORGED"}),
                ("DELETE", endpoint + "/" + sample.name, None)]
    attempts = []
    for profile in ("operator", "query", "native"):
        for verb, path, body in disabled:
            reply = request(clients[profile], verb, path, json=body)
            require(reply.status_code == 403, f"Audit REST {profile} {verb}: {reply.status_code}")
            attempts.append([profile, verb, reply.status_code])
    require(digest(snapshot()) == digest(data), "REST attempt changed audit or stock")

    candidate = movement("ISSUE", "AUDIT", "permission-issue", 1, None)
    denials = {
        "query": reject(send, "move", candidate, "INVENTORY_OPERATION_DENIED", 403, profile="query"),
        "range": reject(send, "move", candidate, "WAREHOUSE_ACCESS_DENIED", 403, profile="limited"),
        "shortage": reject(send, "move", {**candidate, "quantity": "13"}, "INSUFFICIENT_STOCK"),
        "selection": reject(send, "move", {**candidate, "batchSelection": {}}, "BATCH_SELECTION_REQUIRED"),
        "transfer_target": reject(send, "move", {**candidate, "action": "DISPATCH", "targetLocation": OTHER},
                                  "WAREHOUSE_ACCESS_DENIED", 403),
    }

    native_data = stock_doc(fixtures.COMPANY, sample.item_code, sample.batch_key, "1", "Material Receipt",
                            target=fixtures.LOCATIONS[SOURCE]).as_dict()
    native = request(clients["native"], "POST", "/api/resource/Stock%20Entry", json=native_data)
    require(native.status_code == 403, f"Stock Manager REST returned {native.status_code}")
    denials["stock_manager_rest"] = {"http": native.status_code, "hook": "STAGE1F_USE_SERVICE" in native.text}

    def denied_as_admin(label, fn, reason):
        before = digest(snapshot())
        try:
            fn()
        except frappe.PermissionError as exc:
            require(reason in str(exc), f"{label} did not reach expected guard: {exc}")
        else:
            raise RuntimeError(f"{label} unexpectedly allowed")
        finally:
            frappe.db.rollback()
        require(digest(snapshot()) == before, f"{label} mutated durable stock")
        return reason

    denials["administrator_submit"] = denied_as_admin(
        "native submit", lambda: frappe.get_doc(native_data).insert().submit(), "STAGE1F_USE_SERVICE")
    denials["administrator_cancel"] = denied_as_admin(
        "native cancel", lambda: frappe.get_doc("Stock Entry", sample.document_id).cancel(), "STAGE1F_USE_SERVICE")
    denials["administrator_event_insert"] = denied_as_admin(
        "administrator forge", lambda: frappe.get_doc({**sample, "doctype": audit.EVENT}).insert(),
        "STAGE1F_APPEND_ONLY")

    def edit_event():
        doc = frappe.get_doc(audit.EVENT, sample.name)
        doc.action = "FORGED"
        doc.save()

    denials["administrator_event_edit"] = denied_as_admin("administrator edit", edit_event, "STAGE1F_APPEND_ONLY")
    denials["administrator_event_delete"] = denied_as_admin(
        "administrator delete", lambda: frappe.delete_doc(audit.EVENT, sample.name), "STAGE1F_APPEND_ONLY")

    def queue_rebuild():
        frappe.get_doc({
            "doctype": "Repost Item Valuation", "based_on": "Transaction", "company": fixtures.COMPANY,
            "voucher_type": sample.document_type, "voucher_no": sample.document_id,
            "posting_date": get_datetime().date(), "recreate_stock_ledgers": 1,
        }).insert().submit()

    denials["recreate_ledger"] = denied_as_admin("native ledger recreation", queue_rebuild,
                                               "STAGE1F_RECREATE_LEDGER_DENIED")
    deletion_count = frappe.db.count("Transaction Deletion Record")
    denials["bulk_delete"] = denied_as_admin(
        "native bulk delete request creation",
        lambda: frappe.get_doc({"doctype": "Transaction Deletion Record", "company": fixtures.COMPANY}).insert(),
        "STAGE1F_BULK_DELETE_DENIED",
    )
    require(frappe.db.count("Transaction Deletion Record") == deletion_count, "Deletion request was persisted")
    return {"rest_events": attempts, "denials": denials}


def durable():
    data = snapshot()
    engine = reconcile(fixtures.COMPANY)
    docs = {(kind, doc.name): doc for kind in ("Stock Entry", "Stock Reconciliation") for doc in data[kind]}
    lines = {row.name: row for row in data["Stock Entry Detail"] + data["Stock Reconciliation Item"]}
    events = {row.name: row for row in data[audit.EVENT]}
    tasks = {row.name: row for row in data[audit.TASK]}
    sle_by_voucher = {}
    for row in data["Stock Ledger Entry"]:
        require(row.is_cancelled == 0, f"Unexpected cancelled SLE {row.name}")
        sle_by_voucher.setdefault((row.voucher_type, row.voucher_no), []).append(row)
    counts = {event.count_task for event in events.values() if event.count_task}
    require(set(sle_by_voucher) == set(docs) == {(e.document_type, e.document_id) for e in events.values()}
            and len(docs) == len(events), "Documents, events and SLE do not form a bijection")
    require(all(key in tasks for key in counts), "Stock change has no count baseline")
    details = {}
    for e in events.values():
        expected = {}
        quantity = abs(decimal(e.quantity_delta))
        if e.source_location:
            expected[audit.LOCATIONS[e.source_location]] = -quantity
        if e.target_location:
            expected[audit.LOCATIONS[e.target_location]] = quantity
        key = (e.document_type, e.document_id)
        doc = docs[key]
        line = lines[e.document_line]
        leg_sle = sle_by_voucher[key]
        sums = {location: Decimal(0) for location in expected}
        for sl in leg_sle:
            require(sl.item_code == e.item_code and sl.voucher_detail_no == e.document_line
                    and sl.warehouse in expected, f"Unexpected SLE dimensions: {sl.name}")
            allocations = [row for row in data["Serial and Batch Entry"] if row.parent == sl.serial_and_batch_bundle]
            require(len(allocations) == 1 and allocations[0].batch_no == e.batch_key,
                    f"Unexpected SLE batch: {sl.name}")
            sums[sl.warehouse] += decimal(sl.actual_qty)
        require(sums == expected and doc.docstatus == 1 and line.parent == doc.name and line.item_code == e.item_code,
                f"Audit event did not match voucher {doc.name}")
        require(e.batch_key == batch_key(e.item_code, e.batch_number or None), "Wrong public batch mapping")
        if e.reversal_of:
            original = events[e.reversal_of]
            require((e.source_location, e.target_location, e.item_code, e.batch_key)
                    == (original.target_location, original.source_location, original.item_code, original.batch_key),
                    "Wrong correction relationship")
            if e.action == "RETURN_TRANSIT":
                received = sum((decimal(r.quantity_delta) for r in events.values() if r.dispatch_event == original.name),
                               Decimal(0))
                require(received + quantity == decimal(original.quantity_delta), "Return did not close the unreceived amount")
            else:
                require(quantity == abs(decimal(original.quantity_delta)), "Wrong reversed quantity")
        if e.dispatch_event:
            dispatch = events[e.dispatch_event]
            require(doc.outgoing_stock_entry == dispatch.document_id and line.ste_detail == dispatch.document_line
                    and line.against_stock_entry == dispatch.document_id, "Missing native original-line links")
        if e.count_task:
            task = tasks[e.count_task]
            require(task.item_code == e.item_code and task.batch_key == e.batch_key
                    and task.location in (e.source_location, e.target_location) and task.actor == e.actor,
                    "Mismatch between count event and frozen task scope")
        details[e.name] = {"type": e.action, "document": e.document_id, "row": e.document_line, "batch": e.batch_number or None,
                           "sku": e.item_code, "delta": e.quantity_delta, "source": e.source_location,
                           "target": e.target_location, "actor": e.actor, "time": e.occurred_at,
                           "nativeSLE": [{"id": sl.name, "quantity": sl.actual_qty, "bundle": sl.serial_and_batch_bundle}
                                         for sl in leg_sle],
                           "reversal_of": e.reversal_of, "dispatch_event": e.dispatch_event, "count_task": e.count_task}
    for row in engine["balances"]:
        location = next(key for key, warehouse in audit.LOCATIONS.items() if row["warehouse"] == warehouse)
        require(audit.bucket(row["item"], row["batch"], location)[0] == row["quantity"],
                "Stock audit, Batch report and Bin reconciliation did not agree")
    frappe.db.rollback()
    engine["events"] = details
    engine["table_counts"] = {key: len(value) for key, value in data.items()}
    engine["audit_digest"] = digest(data[audit.EVENT])
    engine["count_task_digest"] = digest(data[audit.TASK])
    engine["connection"] = frappe.db.sql("select connection_id(), @@tx_isolation")[0]
    engine["storage"] = [frappe.db.sql(f"show columns from `tab{doctype}` like %s", (field,))[0][1]
                         for doctype, field in ((audit.EVENT, "quantity_delta"), (audit.TASK, "baseline"))]
    require(all(field.startswith("decimal(") for field in engine["storage"]), "Stock evidence lost fixed precision")
    return engine


def repost_check():
    from erpnext.stock.doctype.repost_item_valuation.repost_item_valuation import repost

    first = durable()
    data = snapshot()
    candidate = next(e for e in data[audit.EVENT] if e.item_code == fixtures.ITEMS["AUDIT"] and e.action == "RECEIPT")
    doc = frappe.get_doc(candidate.document_type, candidate.document_id)
    job = frappe.get_doc({
        "doctype": "Repost Item Valuation", "based_on": "Transaction", "company": fixtures.COMPANY,
        "voucher_type": doc.doctype, "voucher_no": doc.name, "posting_date": doc.posting_date,
        "posting_time": doc.posting_time, "recreate_stock_ledgers": 0,
    }).insert()
    job.submit()
    frappe.db.commit()
    repost(job)
    after = durable()
    job_status = frappe.db.get_value(job.doctype, job.name, "status")
    require(job_status == "Completed", f"Targeted native revaluation failed: {job_status}")
    require(all(first[k] == after[k] for k in ("events", "audit_digest", "count_task_digest", "balances", "bins")),
            "Revaluation changed quantity evidence")
    ledger_updates = [(b["name"], b["modified"], a["modified"])
                      for b, a in zip(first["ledger"], after["ledger"], strict=True) if b != a]
    return {"job": job.name, "status": job_status, "identical_quantity_evidence": True,
            "changed_sle_metadata": ledger_updates, "state": after}


def historical():
    from haxpower_platform.integrity_fixtures import COMPANY

    new_connection()
    return {"history": history(), "stage1e_stock": {k: digest(v) for k, v in records(COMPANY).items()},
            "stage1e_requests": digest(rows("Stage 1E Count Request"))}


def main(emit):
    require(Path.cwd() == ROOT, "Wrong Bench directory")
    mode, = sys.argv[1:]
    operations = {"stock": stock_and_corrections, "transfer": transfers, "count": count_cases, "fault": fault_cases}
    new_connection()
    previous = historical()
    if mode in ("history", "final", "repost"):
        evidence = {"history": historical, "final": durable, "repost": repost_check}[mode]()
        require(historical() == previous, "Historical stock or grants changed")
        frappe.destroy()
        emit(json.dumps(evidence, ensure_ascii=False, indent=2, default=str))
        return
    require(mode in (*operations, "concurrent", "permissions"), "Unknown verification mode")
    setup()
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            def request(*args, **kwargs):
                return pool.submit(wsgi_request, *args, **kwargs).result(timeout=45)

            stage1b_verify.send = request
            clients = {profile: session(*details, warehouse_locations=fixtures.LOCATIONS)
                       for profile, details in USERS.items()}

            def send(endpoint, body, *, profile="operator"):
                return request(clients[profile], "POST", API + endpoint, json=body)

            if mode == "concurrent":
                evidence = concurrent_counts(send, pool, clients["operator"])
            elif mode == "permissions":
                evidence = permissions(send, request, clients)
            else:
                evidence = operations[mode](send)
        require(historical() == previous, "Historical stock or grants changed")
        emit(json.dumps(evidence, ensure_ascii=False, indent=2, default=str))
    finally:
        new_connection()
        for user, _, _ in USERS.values():
            if frappe.db.exists("User", user):
                doc = frappe.get_doc("User", user)
                doc.api_key = None
                doc.api_secret = None
                doc.save()
        frappe.db.commit()
        require(all(not any(frappe.db.get_value("User", user, ["api_key", "api_secret"])) for user, _, _ in USERS.values()
                    if frappe.db.exists("User", user)), "Temporary test keys still enabled")
        frappe.destroy()


if __name__ == "__main__":
    output = sys.stdout
    with redirect_stdout(sys.stderr):
        main(lambda evidence: print(evidence, file=output))
