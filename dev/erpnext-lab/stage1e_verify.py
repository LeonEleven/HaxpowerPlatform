"""Exercise committed Stage 1E stock writes with independent Frappe WSGI transactions."""
import json
from functools import partial
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from decimal import Decimal
from pathlib import Path
from threading import Barrier
from time import monotonic_ns, sleep
from unittest.mock import patch

import frappe

import stage1b_verify
from haxpower_platform import integrity, integrity_fixtures as fixtures, transit
from haxpower_platform.stock import QUERY_ROLE, ROLE, SERVICE_WRITE, batch_key, find_batch
from stage1b_verify import ROOT, new_connection, require, session
from stage1d_native import insert, make_company, make_item, make_warehouses, post, receipt_doc, stock_doc
from stage1d_verify import selection, successful, wsgi_request
from stage1e_evidence import decimal, digest, history, reconcile, records, rows

ENDPOINT = "/api/method/haxpower_platform.integrity."
OPERATOR = "stage1e-operator@example.invalid"
USERS = {
    "operator": (OPERATOR, [ROLE], tuple(fixtures.LOCATIONS)),
    "query": ("stage1e-query@example.invalid", [QUERY_ROLE], ("W1/L1",)),
    "wrong_warehouse": ("stage1e-other@example.invalid", [ROLE], ("W1/L2",)),
}
SOURCE = fixtures.LOCATIONS["W1/L1"]
OTHER = fixtures.LOCATIONS["W1/L2"]
TARGET = fixtures.LOCATIONS["W2/L1"]
SEEDS = (
    ("COUNT", "STD61", 12, SOURCE), ("COUNT", "STD62", 20, SOURCE), ("COUNT", None, 15, SOURCE),
    ("COUNT", None, 2, OTHER), ("COUNT-ZERO", "STD61", 4, SOURCE),
    ("COUNT-ZERO", "STD62", 8, SOURCE), ("COUNT-RACE", None, 10, SOURCE),
    ("RACE-STD61", "STD61", 10, SOURCE), ("RACE-STD61", "STD62", 12, SOURCE),
    ("RACE-NULL", None, 10, SOURCE), ("RACE-NULL", "STD61", 12, SOURCE),
    ("PC", "STD61", 5, SOURCE), ("SHARED-TRANSIT", "STD61", 20, SOURCE),
    ("SHARED-TRANSIT", "STD62", 9, SOURCE), ("SHARED-TRANSIT", "STD61", 1, OTHER),
)


NULL_TRANSFER_SEEDS = (("SHARED-TRANSIT-NULL", None, 20, SOURCE),
                       ("SHARED-TRANSIT-NULL", "STD62", 9, SOURCE),
                       ("SHARED-TRANSIT-NULL", None, 1, OTHER))


def setup():
    existing = frappe.db.exists("Company", fixtures.COMPANY)
    if existing:
        require(not frappe.db.get_value("Company", fixtures.COMPANY, "enable_perpetual_inventory"),
                "Unexpected perpetual inventory setting")
        for sku, number, qty, wh in SEEDS:
            require(frappe.db.exists("Stock Entry", {"company": fixtures.COMPANY, "docstatus": 1,
                                                     "remarks": seed_id(sku, number, qty, wh)}),
                    "Existing experimental seed missing")
        if frappe.db.exists("Item", fixtures.ITEMS["SHARED-TRANSIT-NULL"]):
            for sku, number, qty, wh in NULL_TRANSFER_SEEDS:
                require(frappe.db.exists("Stock Entry", {"company": fixtures.COMPANY, "docstatus": 1,
                                                         "remarks": seed_id(sku, number, qty, wh)}),
                        "Existing NO_BATCH transfer seed missing")
            return
        items = ("SHARED-TRANSIT-NULL",)
        seeds = NULL_TRANSFER_SEEDS
    else:
        require(not any(frappe.db.exists("Item", item) for item in fixtures.ITEMS.values()), "Fixture SKU already exists")
        make_company(fixtures.COMPANY, "H1E")
        places = make_warehouses(fixtures.COMPANY, "HP1E")
        require(places == {"source": SOURCE, "other": OTHER, "target": TARGET, "transit": fixtures.TRANSIT},
                "Wrong fixture Warehouses")
        insert("UOM", uom_name=fixtures.FRACTION_UOM, must_be_whole_number=0)
        items = fixtures.ITEMS
        seeds = SEEDS + NULL_TRANSFER_SEEDS
    for sku in items:
        item = fixtures.ITEMS[sku]
        if sku == "FRACTION":
            insert("Item", item_code=item, item_name=item, item_group="All Item Groups",
                   stock_uom=fixtures.FRACTION_UOM, has_batch_no=1, is_stock_item=1, create_new_batch=0)
        else:
            make_item(item)
    token = SERVICE_WRITE.set(True)
    try:
        for sku, number, qty, wh in seeds:
            item = fixtures.ITEMS[sku]
            batch = find_batch(item, number, create=True)
            doc = stock_doc(fixtures.COMPANY, item, batch, str(qty), "Material Receipt", target=wh)
            doc.remarks = seed_id(sku, number, qty, wh)
            post(doc)
    finally:
        SERVICE_WRITE.reset(token)
    frappe.db.commit()


def seed_id(sku, number, qty, wh):
    return f"HP1E-SEED:{sku}:{number}:{qty}:{wh}"


def move(sku, action, key, qty, number):
    return {"action": action, "stockItemId": sku, "requestId": key, "quantity": qty,
            "batchSelection": selection(number), "warehouseLocationId": "W1/L1"}


def count(sku, key, *selection_qty):
    return {"stockItemId": sku, "requestId": key, "warehouseLocationId": "W1/L1",
            "counts": [{"batchSelection": selection(number), "quantity": qty} for number, qty in selection_qty]}


def check(items, balances):
    frappe.db.rollback()
    state = reconcile(fixtures.COMPANY)
    actual = {(row["item"], row["warehouse"], row["batch"]): row["quantity"] for row in state["balances"]
              if row["item"] in {fixtures.ITEMS[sku] for sku in items}}
    expected = {(fixtures.ITEMS[sku], wh, batch_key(fixtures.ITEMS[sku], number)): Decimal(qty)
                for (sku, wh, number), qty in balances.items()}
    require({k: v for k, v in actual.items() if v} == {k: v for k, v in expected.items() if v},
            f"Unexpected batch quantities: {actual}")
    return state


def database_state():
    frappe.db.rollback()
    state = records(fixtures.COMPANY)
    state["Stage 1E Count Request"] = rows("Stage 1E Count Request")
    return state


def sizes(state):
    return {k: len(v) for k, v in state.items()}


def require_growth(before, after, **increments):
    names = {k.replace("_", " "): v for k, v in increments.items()}
    delta = {k: len(after[k]) - len(before[k]) for k in before if len(after[k]) != len(before[k])}
    require(delta == names, f"Unexpected stock record increases: {delta}")
    return delta


def rejected(send, body, endpoint, reason, *, profile="operator", status=417):
    before = database_state()
    response = send(endpoint, body, profile=profile)
    require(digest(before) == digest(database_state()), f"Rejected request mutated inventory: {body}")
    require(response.status_code == status and reason in response.text,
            f"Expected {status} {reason}, got {response.status_code}: {response.json().get('exc', response.text)[-3100:]}")
    return {"request": body, "http": status, "reason": reason, "stock_digest": digest(before)}


def replay(send, endpoint, body, expected):
    before = database_state()
    result = successful(send(endpoint, body), {"replayed": True, **expected})
    require(digest(before) == digest(database_state()), "Replay changed inventory")
    return {"request": body, "http": 200, "result": result}


def concurrent(pool, client, endpoint, bodies):
    gate = Barrier(2)
    at_lock = Barrier(2)
    original = transit.lock_company
    lock_data = []

    def instrument(scope):
        # Independent request transactions deliberately overlap while the first holds the production lock.
        connection = frappe.db.sql("select connection_id(), @@tx_isolation")[0]
        at_lock.wait(timeout=10)
        start = monotonic_ns()
        original(scope)
        acquired = monotonic_ns()
        lock_data.append({"connection": connection[0], "isolation": connection[1], "start_ns": start,
                          "acquired_ns": acquired, "wait_ms": (acquired - start) // 1_000_000})
        sleep(0.30)

    with patch.object(transit, "lock_company", instrument):
        futures = [pool.submit(wsgi_request, client, "POST", ENDPOINT + endpoint, json=payload, barrier=gate)
                   for payload in bodies]
        responses = [future.result(timeout=45) for future in futures]
    require(len({e["connection"] for e in lock_data}) == 2, "Concurrent requests reused database connection")
    ordered = sorted(lock_data, key=lambda row: row["acquired_ns"])
    require(ordered[1]["start_ns"] < ordered[0]["acquired_ns"] + 100_000_000
            and ordered[1]["wait_ms"] >= 200, "Requests did not demonstrate lock contention")
    return responses, ordered


def same_key(pool, client, endpoint, body, result_id):
    responses, locks = concurrent(pool, client, endpoint, [body, body])
    results = [successful(response, {}) for response in responses]
    require({row["replayed"] for row in results} == {True, False}
            and results[0][result_id] == results[1][result_id], "Duplicate key did not converge")
    return {"request": body, "responses": results, "lock": locks}, results[0]


def contested(pool, client, endpoint, bodies, reason):
    responses, locks = concurrent(pool, client, endpoint, bodies)
    require(sorted(response.status_code for response in responses) == [200, 417],
            f"Unexpected stock race responses: {[(r.status_code, r.text[:450]) for r in responses]}")
    winner, loser = (0, 1) if responses[0].ok else (1, 0)
    require(reason in responses[loser].text, f"Unexpected collision reason: {responses[loser].text[:700]}")
    result = successful(responses[winner], {"replayed": False})
    return ({"requests": bodies, "winner": winner, "result": result, "rejected": reason, "lock": locks},
            bodies[loser], result)


def fault(send, endpoint, body, module, operation):
    before = database_state()
    original = getattr(module, operation)
    reached = []

    def fail_after_writes(*args, **kwargs):
        result = original(*args, **kwargs)
        # Read inside the failing request to prove it executed the actual business writes first.
        reached.append(sizes(records(fixtures.COMPANY)) | {"Stage 1E Count Request": len(rows("Stage 1E Count Request"))})
        raise RuntimeError("HP1E_CONTROLLED_FAILURE")

    with patch.object(module, operation, fail_after_writes):
        evidence = rejected(send, body, endpoint, "HP1E_CONTROLLED_FAILURE", status=500)
    require(len(reached) == 1 and reached[0] != sizes(before), "Fault occurred without transactional inventory writes")
    return {"rejected": evidence, "before_tables": sizes(before), "uncommitted_tables": reached[0]}


def count_cases(pool, clients, send):
    items = ("COUNT", "COUNT-ZERO", "COUNT-RACE")
    expected = {("COUNT", SOURCE, "STD61"): "12", ("COUNT", SOURCE, "STD62"): "20",
                ("COUNT", SOURCE, None): "15", ("COUNT", OTHER, None): "2",
                ("COUNT-ZERO", SOURCE, "STD61"): "4", ("COUNT-ZERO", SOURCE, "STD62"): "8",
                ("COUNT-RACE", SOURCE, None): "10"}
    body = count("COUNT", "hp1e-count", ("STD61", "11"), ("STD62", "20"), (None, "17"))
    zero = count("COUNT-ZERO", "hp1e-count-zero", ("STD61", "0"))
    already_counted = bool(rows("Stock Reconciliation", company=fixtures.COMPANY))
    if already_counted:
        expected.update({("COUNT", SOURCE, "STD61"): "11", ("COUNT", SOURCE, None): "17",
                         ("COUNT-ZERO", SOURCE, "STD61"): "0"})
    output = {"before": check(items, expected), "recovery_run": already_counted}
    missing = count("COUNT", "hp1e-count-missing", (None, "16"))
    missing["counts"][0].pop("batchSelection")
    output["denials"] = [
        rejected(send, missing, "count", "BATCH_SELECTION_REQUIRED"),
        rejected(send, count("COUNT", "hp1e-count-unknown", ("STD99", "1")), "count", "BATCH_NOT_FOUND"),
        rejected(send, body, "count", "WAREHOUSE_ACCESS_DENIED", profile="wrong_warehouse", status=403),
        rejected(send, body, "count", "INVENTORY_OPERATION_DENIED", profile="query", status=403),
    ]
    if already_counted:
        output["replay"] = replay(send, "count", body, {})
        output["zero"] = replay(send, "count", zero, {})
        race_body = count("COUNT-RACE", "hp1e-count-race-same", (None, "11"))
        expected["COUNT-RACE", SOURCE, None] = "11"
        changed_rows = 1
    else:
        race_body = body
        changed_rows = 2
    output["fault"] = fault(send, "count", race_body, integrity, "save_count_request")
    before = database_state()
    output["same_key"], _ = same_key(pool, clients["operator"], "count", race_body, "documentId")
    output["growth"] = require_growth(before, database_state(), Stock_Reconciliation=1,
                                      Stock_Reconciliation_Item=changed_rows, Stock_Ledger_Entry=2 * changed_rows,
                                      Serial_and_Batch_Bundle=2 * changed_rows, Serial_and_Batch_Entry=2 * changed_rows,
                                      **{"Stage_1E_Count_Request": 1})
    if not already_counted:
        output["replay"] = replay(send, "count", body, {})
        expected.update({("COUNT", SOURCE, "STD61"): "11", ("COUNT", SOURCE, None): "17"})
        output["zero"] = {"request": zero, "result": successful(send("count", zero), {"replayed": False})}
        expected["COUNT-ZERO", SOURCE, "STD61"] = "0"
    output["changed"] = rejected(send, count("COUNT", "hp1e-count", ("STD61", "9")), "count", "IDEMPOTENCY_CONFLICT")
    output["counted"] = check(items, expected)
    bodies = [count("COUNT-RACE", f"hp1e-count-race-{i}", (None, qty)) for i, qty in enumerate(("8", "9"))]
    responses, locks = concurrent(pool, clients["operator"], "count", bodies)
    results = [successful(response, {"replayed": False}) for response in responses]
    data = database_state()
    order = sorted((row for row in data["Stock Reconciliation"] if row.name in {r["documentId"] for r in results}),
                   key=lambda row: (row.posting_date, row.posting_time, row.creation))
    details = [next(row for row in data["Stock Reconciliation Item"] if row.parent == doc.name) for doc in order]
    require(len(details) == 2 and decimal(details[0].current_qty) == Decimal("11" if already_counted else "10")
            and decimal(details[1].current_qty) == decimal(details[0].qty), "Count race did not serialize")
    expected["COUNT-RACE", SOURCE, None] = str(details[-1].qty)
    output["distinct_keys"] = {"requests": bodies, "results": results, "lock": locks,
                               "order": [{"document": row.parent, "observed": row.current_qty, "counted": row.qty}
                                         for row in details],
                               "state": check(items, expected)}
    return output


def issue_cases(pool, clients, send):
    output = {}
    for sku, number, control in (("RACE-STD61", "STD61", "STD62"), ("RACE-NULL", None, "STD61")):
        before = database_state()
        check((sku,), {(sku, SOURCE, number): "10", (sku, SOURCE, control): "12"})
        bodies = [move(sku, "ISSUE", f"hp1e-{sku}-{i}", "7", number) for i in range(2)]
        fault_evidence = fault(send, "movement", bodies[0], integrity, "post_entry")
        race, loser, winner = contested(pool, clients["operator"], "movement", bodies, "INSUFFICIENT_STOCK")
        growth = require_growth(before, database_state(), Stock_Entry=1, Stock_Entry_Detail=1,
                                Stock_Ledger_Entry=1, Serial_and_Batch_Bundle=1, Serial_and_Batch_Entry=1)
        winning_request = bodies[race["winner"]]
        output[sku] = {"fault": fault_evidence, "race": race, "growth": growth,
                       "loser_retry": rejected(send, loser, "movement", "INSUFFICIENT_STOCK"),
                       "replay": replay(send, "movement", winning_request, {"documentId": winner["documentId"]}),
                       "changed": rejected(send, winning_request | {"quantity": "6"}, "movement", "IDEMPOTENCY_CONFLICT"),
                       "final": check((sku,), {(sku, SOURCE, number): "3", (sku, SOURCE, control): "12"})}
    body = move("PC", "ISSUE", "hp1e-issue-shared-key", "1", "STD61")
    output["same_key_fault"] = fault(send, "movement", body, integrity, "post_entry")
    before = database_state()
    output["e15_same_key"], _ = same_key(pool, clients["operator"], "movement", body, "documentId")
    output["same_key_growth"] = require_growth(before, database_state(), Stock_Entry=1, Stock_Entry_Detail=1,
                                                Stock_Ledger_Entry=1, Serial_and_Batch_Bundle=1, Serial_and_Batch_Entry=1)
    return output


def precision_storage():
    evidence = {}
    evidence["db_columns"] = frappe.db.sql("""
        SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE() AND (TABLE_NAME, COLUMN_NAME) IN
            (('tabStock Entry Detail', 'qty'), ('tabStock Entry Detail', 'transfer_qty'),
             ('tabStock Entry Detail', 'conversion_factor'), ('tabStock Ledger Entry', 'actual_qty'),
             ('tabStock Ledger Entry', 'qty_after_transaction'), ('tabBin', 'actual_qty'),
             ('tabSerial and Batch Entry', 'qty'), ('tabStock Reconciliation Item', 'qty'))
        ORDER BY TABLE_NAME, COLUMN_NAME
    """)
    evidence["unit"] = {
        "name": fixtures.FRACTION_UOM, "whole": frappe.db.get_value("UOM", fixtures.FRACTION_UOM, "must_be_whole_number"),
        "site_float_precision": frappe.get_system_settings("float_precision"),
        "site_rounding": frappe.get_system_settings("rounding_method"),
        "conversion": frappe.db.sql("""
            select cast(conversion_factor as char) from `tabStock Entry Detail` where item_code = %s
            order by creation
        """, fixtures.ITEMS["FRACTION"]),
        "raw_balance": frappe.db.sql("""
            select cast(actual_qty as char) from tabBin where item_code=%s and warehouse=%s
        """, (fixtures.ITEMS["FRACTION"], SOURCE))[0][0],
        "raw_ledger": frappe.db.sql("""
            select voucher_no, cast(actual_qty as char), cast(qty_after_transaction as char)
            from `tabStock Ledger Entry` where item_code=%s and warehouse=%s and is_cancelled=0
            order by posting_datetime, creation
        """, (fixtures.ITEMS["FRACTION"], SOURCE)),
    }
    require(Decimal(evidence["unit"]["raw_balance"]) == Decimal("1.000")
            and tuple(Decimal(row[1]) for row in evidence["unit"]["raw_ledger"]) == (Decimal("1.125"), Decimal("-0.125"))
            and all(column[2].startswith("decimal(") for column in evidence["db_columns"]),
            "Unexpected persistence precision")
    return evidence

def precision_cases(pool, clients, send):
    body = move("FRACTION", "RECEIPT", "hp1e-fraction-receipt", "1.125", "STD61")
    output = {"fault": fault(send, "movement", body, integrity, "post_entry")}
    before = database_state()
    output["same_key"], receipt = same_key(pool, clients["operator"], "movement", body, "documentId")
    output["growth"] = require_growth(before, database_state(), Stock_Entry=1, Stock_Entry_Detail=1,
                                      Stock_Ledger_Entry=1, Serial_and_Batch_Bundle=1, Serial_and_Batch_Entry=1,
                                      Batch=1, Bin=1)
    output["replay"] = replay(send, "movement", body, {"documentId": receipt["documentId"]})
    output["changed"] = rejected(send, body | {"quantity": "1.126"}, "movement", "IDEMPOTENCY_CONFLICT")
    out = move("FRACTION", "ISSUE", "hp1e-fraction-issue", "0.125", "STD61")
    output["issue"] = {"request": out, "result": successful(send("movement", out), {"replayed": False})}
    output["precision_denials"] = [
        rejected(send, move(sku, "ISSUE", f"hp1e-bad-qty-{i}", qty, "STD61"), "movement", "INVALID_STOCK_QUANTITY")
        for i, (sku, qty) in enumerate((("FRACTION", "0.1254"), ("PC", "0.5"), ("FRACTION", "-0.125"), ("PC", "0")))
    ]
    output["exact"] = check(("FRACTION",), {("FRACTION", SOURCE, "STD61"): "1.000"})
    output["storage"] = precision_storage()
    bodies = [move("PC", "RECEIPT", f"hp1e-receipt-independent-{i}", "1", "STD61") for i in range(2)]
    before = database_state()
    responses, locks = concurrent(pool, clients["operator"], "movement", bodies)
    results = [successful(response, {"replayed": False}) for response in responses]
    output["receipt_distinct_keys"] = {
        "requests": bodies, "results": results, "lock": locks,
        "growth": require_growth(before, database_state(), Stock_Entry=2, Stock_Entry_Detail=2,
                                 Stock_Ledger_Entry=2, Serial_and_Batch_Bundle=2, Serial_and_Batch_Entry=2),
        "state": check(("PC",), {("PC", SOURCE, "STD61"): "6"}),
    }
    return output


def transfer_cases(pool, clients, send, *, sku="SHARED-TRANSIT-NULL", number=None):
    label = "hp1e-null" if number is None else "hp1e"
    other = {(sku, SOURCE, "STD62"): "9", (sku, OTHER, number): "1"}

    def quantities(source, transit_qty, target):
        return check((sku,), {**other, (sku, SOURCE, number): source,
                             (sku, fixtures.TRANSIT, number): transit_qty, (sku, TARGET, number): target})

    def linked_state(*expectations):
        state = database_state()
        evidence = []
        for transfer, dispatched, received in expectations:
            outgoing = next(row for row in state["Stock Entry"] if row.remarks.startswith(f"HP1E-D:{transfer}:"))
            line = next(row for row in state["Stock Entry Detail"] if row.parent == outgoing.name)
            receipts = [row for row in state["Stock Entry"] if row.outgoing_stock_entry == outgoing.name]
            actual = [row for row in state["Stock Entry Detail"] if row.parent in {v.name for v in receipts}]
            require(decimal(line.transfer_qty) == Decimal(dispatched) and decimal(line.transferred_qty) == Decimal(received)
                    and sum(decimal(row.transfer_qty) for row in actual) == Decimal(received)
                    and all(row.ste_detail == line.name and row.against_stock_entry == outgoing.name
                            and row.batch_no == line.batch_no and row.item_code == line.item_code for row in actual),
                    "Receipt was assigned to the wrong dispatch line")
            evidence.append({"dispatch": outgoing.name, "line": line.name, "quantity": dispatched,
                             "received": received, "remaining": str(Decimal(dispatched) - Decimal(received)),
                             "receipts": [{key: row[key] for key in ("parent", "name", "ste_detail",
                                          "against_stock_entry", "batch_no", "qty")} for row in actual]})
        return evidence

    base = {"stockItemId": sku, "targetLocationId": "W2/L1", "batchSelection": selection(number)}
    if number == "STD61" and rows("Stock Entry", company=fixtures.COMPANY, remarks=["like", "HP1E-R:%"]):
        # Request IDs are bound to the HTTP user, so derive the two transfer IDs with that identity.
        original_user = frappe.session.user
        frappe.set_user(OPERATOR)
        try:
            a_id, _ = transit.request_values("hp1e-dispatch-a", "10")
            b_ids = [transit.request_values(f"hp1e-dispatch-b-{i}", "8")[0] for i in range(2)]
        finally:
            frappe.set_user(original_user)
        documents = database_state()["Stock Entry"]
        b_id = next(key for key in b_ids if any(row.remarks.startswith(f"HP1E-D:{key}:") for row in documents))
        output = {"before_resume": quantities("2", "1", "17"),
                  "links": linked_state((a_id, "10", "9"), (b_id, "8", "8"))}
        close = base | {"requestId": "hp1e-receive-a-final", "transferId": a_id, "quantity": "1"}
        output["complete"] = successful(send("receive", close), {"state": "Completed", "remaining": "0"})
        output["final"] = {"state": quantities("2", "0", "18"),
                           "links": linked_state((a_id, "10", "10"), (b_id, "8", "8"))}
        return output
    dispatch_a = {**base, "requestId": f"{label}-dispatch-a", "sourceLocationId": "W1/L1", "quantity": "10"}
    output = {"before": quantities("20", "0", "0"),
              "dispatch_fault": fault(send, "dispatch", dispatch_a, transit, "submit_stock")}
    before = database_state()
    output["dispatch_same_key"], a = same_key(pool, clients["operator"], "dispatch", dispatch_a, "transferId")
    output["dispatch_growth"] = require_growth(before, database_state(), Stock_Entry=1, Stock_Entry_Detail=1,
                                               Stock_Ledger_Entry=2, Serial_and_Batch_Bundle=2,
                                               Serial_and_Batch_Entry=2, Bin=1)
    output["dispatch_replay"] = replay(send, "dispatch", dispatch_a, {"transferId": a["transferId"]})
    output["dispatch_changed"] = rejected(send, dispatch_a | {"quantity": "9"}, "dispatch", "IDEMPOTENCY_CONFLICT")
    bodies = [dispatch_a | {"requestId": f"{label}-dispatch-b-{i}", "quantity": "8"} for i in range(2)]
    before = database_state()
    output["dispatch_distinct"], loser, b = contested(pool, clients["operator"], "dispatch", bodies, "INSUFFICIENT_STOCK")
    output["dispatch_distinct"]["growth"] = require_growth(before, database_state(), Stock_Entry=1, Stock_Entry_Detail=1,
                                                           Stock_Ledger_Entry=2, Serial_and_Batch_Bundle=2,
                                                           Serial_and_Batch_Entry=2)
    output["dispatch_loser_retry"] = rejected(send, loser, "dispatch", "INSUFFICIENT_STOCK")
    a_id, b_id = a["transferId"], b["transferId"]
    output["two_open"] = {"state": quantities("2", "18", "0"),
                          "links": linked_state((a_id, "10", "0"), (b_id, "8", "0"))}
    first = base | {"requestId": f"{label}-receive-a-first", "transferId": a_id, "quantity": "6"}
    output["receipt_fault"] = fault(send, "receive", first, transit, "submit_stock")
    before = database_state()
    output["receipt_same_key"], received = same_key(pool, clients["operator"], "receive", first, "receiptId")
    output["receipt_growth"] = require_growth(before, database_state(), Stock_Entry=1, Stock_Entry_Detail=1,
                                              Stock_Ledger_Entry=2, Serial_and_Batch_Bundle=2,
                                              Serial_and_Batch_Entry=2, Bin=1)
    output["receipt_replay"] = replay(send, "receive", first, {"receiptId": received["receiptId"]})
    output["receipt_changed"] = rejected(send, first | {"quantity": "5"}, "receive", "IDEMPOTENCY_CONFLICT")
    output["first_receipt"] = {"state": quantities("2", "12", "6"),
                               "links": linked_state((a_id, "10", "6"), (b_id, "8", "0"))}
    output["cannot_borrow"] = rejected(send, first | {"requestId": f"{label}-receive-too-much", "quantity": "5"},
                                        "receive", "TRANSFER_RECEIPT_EXCEEDS_REMAINING")
    # Direct native End Transit also rejects exceeding A's remainder despite the shared physical stock.
    outbound = frappe.get_doc("Stock Entry", output["first_receipt"]["links"][0]["dispatch"])
    before = database_state()
    frappe.db.savepoint("hp1e_native_transit_limit")
    try:
        native, _ = receipt_doc(outbound, "5", TARGET, batch_key(fixtures.ITEMS[sku], number))
        token = SERVICE_WRITE.set(True)
        try:
            post(native)
        finally:
            SERVICE_WRITE.reset(token)
    except frappe.ValidationError as error:
        output["native_limit"] = {"type": type(error).__name__, "error": str(error)}
    finally:
        frappe.db.rollback(save_point="hp1e_native_transit_limit")
        frappe.db.release_savepoint("hp1e_native_transit_limit")
    require("native_limit" in output and digest(before) == digest(database_state()),
            "Native per-dispatch cumulative limit failed or left stock changes")
    bodies = [first | {"requestId": f"{label}-receive-race-{i}", "quantity": "3"} for i in range(2)]
    before = database_state()
    output["receipt_distinct"], loser, _ = contested(
        pool, clients["operator"], "receive", bodies, "TRANSFER_RECEIPT_EXCEEDS_REMAINING",
    )
    output["receipt_distinct"]["growth"] = require_growth(before, database_state(), Stock_Entry=1, Stock_Entry_Detail=1,
                                                          Stock_Ledger_Entry=2, Serial_and_Batch_Bundle=2,
                                                          Serial_and_Batch_Entry=2)
    output["receipt_loser_retry"] = rejected(send, loser, "receive", "TRANSFER_RECEIPT_EXCEEDS_REMAINING")
    output["after_race"] = {"state": quantities("2", "9", "9"),
                            "links": linked_state((a_id, "10", "9"), (b_id, "8", "0"))}
    close_b = first | {"requestId": f"{label}-receive-b", "transferId": b_id, "quantity": "8"}
    output["complete_b"] = successful(send("receive", close_b), {"state": "Completed", "remaining": "0"})
    output["after_b"] = {"state": quantities("2", "1", "17"),
                         "links": linked_state((a_id, "10", "9"), (b_id, "8", "8"))}
    close_a = first | {"requestId": f"{label}-receive-a-final", "quantity": "1"}
    output["complete_a"] = successful(send("receive", close_a), {"state": "Completed", "remaining": "0"})
    output["final"] = {"state": quantities("2", "0", "18"),
                       "links": linked_state((a_id, "10", "10"), (b_id, "8", "8"))}
    return output


def main(emit):
    require(Path.cwd() == ROOT, "Wrong Bench directory")
    mode, = sys.argv[1:]
    operations = {"count": count_cases, "issue": issue_cases, "precision": precision_cases, "transit": transfer_cases,
                  "transit-real": partial(transfer_cases, sku="SHARED-TRANSIT", number="STD61")}
    require(mode in (*operations, "final"), "Unknown verification mode")
    new_connection()
    before = history()
    if mode == "final":
        output = {"history": before, "state": reconcile(fixtures.COMPANY), "sizes": sizes(database_state()),
                  "storage": precision_storage(),
                  "stock_settings": {key: frappe.db.get_single_value("Stock Settings", key) for key in (
                      "enable_serial_and_batch_no_for_item", "allow_negative_stock", "allow_negative_stock_for_batch",
                      "auto_create_serial_and_batch_bundle_for_outward", "use_serial_batch_fields")},
                  "users_keys_revoked": all(not any(frappe.db.get_value("User", user, ["api_key", "api_secret"]))
                                            for user, _, _ in USERS.values()),
                  "request_unique": frappe.db.sql("show index from `tabStage 1E Count Request`",
                                                 as_dict=True)}
        emit(json.dumps(output, ensure_ascii=False, default=str, indent=2))
        frappe.destroy()
        return
    require(frappe.db.exists("DocType", "Stage 1E Count Request"), "Load the Stage 1E request DocType first")
    setup()
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            def request(*args, **kwargs):
                return pool.submit(wsgi_request, *args, **kwargs).result(timeout=45)

            stage1b_verify.send = request
            clients = {name: session(user, roles, locations, warehouse_locations=fixtures.LOCATIONS)
                       for name, (user, roles, locations) in USERS.items()}

            def send(endpoint, body, *, profile="operator"):
                return request(clients[profile], "POST", ENDPOINT + endpoint, json=body)

            result = operations[mode](pool, clients, send)
        new_connection()
        require(history() == before, "Historical stock and permissions were changed")
        emit(json.dumps({"site": frappe.local.site, "mode": mode, "company": fixtures.COMPANY,
                         "result": result, "historical_unchanged": True}, ensure_ascii=False, default=str, indent=2))
    finally:
        new_connection()
        for user, _, _ in USERS.values():
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
        main(lambda result: print(result, file=output))
