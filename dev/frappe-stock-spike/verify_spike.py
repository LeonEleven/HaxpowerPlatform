"""Run repeatable synthetic cases against the isolated site's actual Frappe WSGI application."""

import json
import re
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from threading import Barrier
from time import monotonic, sleep
from unittest.mock import patch
from urllib.parse import quote

import frappe
from frappe.app import application
from frappe.core.doctype.user.user import generate_keys
from werkzeug.test import Client

from haxpower_stock_spike import api, store

API = "/api/method/haxpower_stock_spike.api."
SOURCE, TARGET, OTHER = "L1", "L2", "L3"
REAL, OTHER_BATCH = "STD61", "STD62"
PROFILES = {
    "operator": ([api.OPERATOR], "ALL", []),
    "query": ([api.QUERY], "SELECTED", ["W1"]),
    "restricted": ([api.OPERATOR, api.QUERY], "SELECTED", ["W1"]),
    "none": ([api.QUERY], "NONE", []),
    "outsider": ([], None, []),
}
TABLES = (store.BALANCE, store.EVENT, store.OPERATION, store.TRANSFER, store.TASK)
SKUS = ("STOCK", "TR-REAL", "TR-NULL", "TR-SHARED", "RACE-REAL", "RACE-NULL", "SAME", "ROLL", "COUNT", "ZERO", "FIX", "FRAC")


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def connect():
    frappe.destroy()
    frappe.init(site=store.SITE, sites_path=".")
    frappe.connect()
    store.require_site()
    require(set(frappe.get_installed_apps()) == {"frappe", "haxpower_stock_spike"}, "Wrong installed apps")


def ensure(doctype, key, **fields):
    data = frappe.get_doc(doctype, key) if frappe.db.exists(doctype, key) else frappe.get_doc({"doctype": doctype, **fields}).insert()
    require(data.name == key and all(data.get(field) == value for field, value in fields.items()),
            f"Unexpected fixture {doctype} {key}")


def fixtures(tag):
    for warehouse in ("W1", "W2", "W3", "TRANSIT"):
        ensure(store.WAREHOUSE, warehouse, code=warehouse)
    for location, warehouse, transit in ((SOURCE, "W1", 0), (TARGET, "W2", 0), (OTHER, "W3", 0),
                                         (store.TRANSIT, "TRANSIT", 1)):
        ensure(store.LOCATION, location, code=location, warehouse_id=warehouse, in_transit=transit)
    for item in SKUS:
        ensure(store.SKU, f"{tag}-{item}", code=f"{tag}-{item}", precision=3 if item == "FRAC" else 0)
    for role in (api.OPERATOR, api.QUERY):
        ensure("Role", role, role_name=role)
    frappe.db.commit()


def identities(tag):
    clients = {}
    for profile, (roles, mode, warehouses) in PROFILES.items():
        user = account(tag, profile)
        if not frappe.db.exists("User", user):
            doc = frappe.get_doc({"doctype": "User", "email": user, "first_name": "Fictional stage 2A",
                                  "user_type": "System User", "send_welcome_email": 0})
            doc.append_roles(*roles)
            doc.insert()
        require(set(frappe.get_roles(user)) & {api.OPERATOR, api.QUERY} == set(roles), f"Wrong roles: {profile}")
        if mode:
            ensure(store.SCOPE, user, actor=user, mode=mode, warehouses=store.encode(warehouses))
        keys = generate_keys(user)
        clients[profile] = {"Authorization": f"token {keys['api_key']}:{keys['api_secret']}"}
    frappe.db.commit()
    return clients


def account(tag, profile):
    return f"spike-{tag.lower()}-{profile}@example.invalid"


def request(headers, method, path, *, body=None, query=None):
    response = Client(application, use_cookies=False).open(
        path, method=method, base_url=f"http://{store.SITE}", headers=headers, json=body, query_string=query,
        environ_overrides={"REMOTE_ADDR": "127.0.0.1"}, buffered=True,
    )
    return response.status_code, response.get_json()


def snapshot():
    connect()
    records = {table: frappe.db.sql(f"select * from `tab{table}` order by name") for table in TABLES}
    return sha256(json.dumps(records, default=str, sort_keys=True).encode()).hexdigest()


def select(batch):
    return {"mode": "BATCH", "batchNo": batch} if batch is not None else {"mode": "NO_BATCH"}


def returned(reply):
    status, response = reply
    require(status == 200 and response and "message" in response, f"Expected HTTP 200, got {status}: {response}")
    return response["message"]


def rejection(reply, status, message):
    actual, body = reply
    require(actual == status and (message is None or message in json.dumps(body, default=str)),
            f"Expected {status} {message}, got {actual}: {body}")
    return {"http": actual, "code": message}


class Cases:
    def __init__(self, tag, pool, clients):
        self.tag, self.pool, self.clients = tag, pool, clients

    def call(self, endpoint, body, profile="operator", method="POST"):
        kwargs = {"query": body} if method == "GET" else {"body": body}
        return self.pool.submit(request, self.clients[profile], method, API + endpoint, **kwargs).result(timeout=45)

    def reject(self, endpoint, body, reason, status=417, **options):
        before = snapshot()
        reply = rejection(self.call(endpoint, body, **options), status, reason)
        require(snapshot() == before, f"Rejected {endpoint} changed stock state")
        return reply

    def get(self, sku, batch, location=SOURCE, profile="operator"):
        selection = {"batchMode": "NO_BATCH"} if batch is None else {"batchMode": "BATCH", "batchNo": batch}
        return returned(self.call("balance", {"sku": self.tag + "-" + sku, "location": location, **selection},
                                  profile=profile, method="GET"))

    def line(self, sku, qty, batch, location=SOURCE):
        return {"sku": self.tag + "-" + sku, "location": location, "quantity": str(qty), "batchSelection": select(batch)}

    def movement(self, action, key, *lines):
        return {"action": action, "requestId": self.tag + "-" + key, "lines": lines}

    def move(self, action, key, sku, qty, batch=None, location=SOURCE):
        return returned(self.call("move", self.movement(action, key, self.line(sku, qty, batch, location))))

    def task(self, sku, batch, key):
        return returned(self.call("create_count", {"sku": self.tag + "-" + sku, "location": SOURCE,
                                                   "batchSelection": select(batch), "requestId": self.tag + "-" + key}))

    def count(self, task, key, qty):
        return {"taskId": task["taskId"], "requestId": self.tag + "-" + key, "quantityText": str(qty)}

    def quantities(self, sku, batch, expected):
        dim = self.tag + "-" + sku
        connect()
        rows = frappe.db.sql("""
            select location, cast(quantity as char) from `tabHP Spike Balance`
            where sku = %s and batch_id <=> %s and (batch_id is null or binary batch_id = binary %s)
        """, (dim, batch, batch))
        amounts = dict(rows)
        actual = [Decimal(amounts.get(location, "0")) for location in (SOURCE, store.TRANSIT, TARGET)]
        require(list(expected) == actual, f"Unexpected balances: {sku} {batch} = {actual}")
        return [api.amount_text(value) for value in actual]

    def stock(self):
        seeds = [self.move("RECEIPT", key, "STOCK", qty, batch) for key, qty, batch in (
            ("real1", 12, REAL), ("real2", 20, OTHER_BATCH), ("null1", 9, None), ("null2", 6, None))]
        initial = [self.get("STOCK", batch) for batch in (REAL, OTHER_BATCH, None)]
        require([row["quantity"] for row in initial] == ["12", "20", "15"], initial)
        for key, qty, batch in (("out1", 5, REAL), ("out2", 2, OTHER_BATCH), ("out-null", 4, None)):
            self.move("ISSUE", key, "STOCK", qty, batch)
        actual = [self.get("STOCK", batch) for batch in (REAL, OTHER_BATCH, None)]
        require([row["quantity"] for row in actual] == ["7", "18", "11"]
                and [row["batch"] for row in actual] == [REAL, OTHER_BATCH, None], actual)
        missing = self.line("STOCK", 1, None)
        missing.pop("batchSelection")
        denials = [self.reject("move", self.movement("ISSUE", "missing", missing), "BATCH_SELECTION_REQUIRED")]
        for index, qty, batch, code in ((0, 8, REAL, "INSUFFICIENT_STOCK"), (1, 12, None, "INSUFFICIENT_STOCK"),
                                        (2, -1, None, "INVALID_QUANTITY")):
            denials.append(self.reject("move", self.movement("ISSUE", f"bad-{index}", self.line("STOCK", qty, batch)), code))

        connect()
        dims = (self.tag + "-STOCK", SOURCE)
        require(frappe.db.sql("""
            select count(*) from `tabHP Spike Balance` where sku=%s and location=%s and batch_id is null
        """, dims)[0][0] == 1, "Physical null bucket not unique")
        constraints = {}
        for label, value, code, expected_key in (("duplicate", "1", 1062, "hp_stock_bucket"),
                                                  ("negative", "-1", 4025, "hp_balance_valid")):
            frappe.db.savepoint("stage2a_probe")
            try:
                store.insert_row(store.BALANCE, store.identity(self.tag, "probe", label), {
                    "sku": dims[0], "location": dims[1], "batch_id": None if label == "duplicate" else "NEG",
                    "quantity": value, "bucket_version": 0,
                })
            except Exception as error:
                require(error.args[0] == code and expected_key in str(error), f"Wrong constraint error: {error!r}")
                constraints[label] = {"errno": code, "constraint": expected_key}
            else:
                raise RuntimeError(f"Database did not reject {label}")
            finally:
                frappe.db.rollback(save_point="stage2a_probe")
        frappe.db.rollback()
        self.move("RECEIPT", "fraction-in", "FRAC", "1.125", REAL)
        self.move("ISSUE", "fraction-out", "FRAC", "0.125", REAL)
        precision = self.get("FRAC", REAL)
        require(precision["quantity"] == "1", precision)
        denials.append(self.reject("move", self.movement("ISSUE", "fraction-overscale", self.line("FRAC", "0.0001", REAL)),
                                   "INVALID_QUANTITY"))
        return {"seed_operations": [e["operationId"] for e in seeds], "initial": initial, "final": actual,
                "errors": denials, "constraints": constraints, "decimal": precision}

    def transfers(self):
        groups = []
        for sku, batch in (("TR-REAL", REAL), ("TR-NULL", None)):
            key = self.tag + "-" + sku
            self.move("RECEIPT", sku + "-seed", sku, 12, batch)
            balances = [self.quantities(sku, batch, (12, 0, 0))]
            start = returned(self.call("dispatch", {"sku": self.tag + "-" + sku, "source": SOURCE, "target": TARGET,
                                                    "quantityText": "10", "batchSelection": select(batch),
                                                    "requestId": key + "-start"}))
            require(start["batch"] == batch and start["received"] == "0" and start["remaining"] == "10", start)
            balances.append(self.quantities(sku, batch, (2, 10, 0)))
            partial_body = {"transferId": start["transferId"], "quantityText": "6", "requestId": key + "-partial"}
            partial = returned(self.call("receive", partial_body))
            require(partial["batch"] == batch and partial["received"] == "6" and partial["remaining"] == "4", partial)
            balances.append(self.quantities(sku, batch, (2, 4, 6)))
            before = snapshot()
            require(returned(self.call("receive", partial_body)) == {**partial, "replayed": True}
                    and snapshot() == before, "Partial replay changed state")
            excess = self.reject("receive", {**partial_body, "quantityText": "5", "requestId": key + "-excess"},
                                 "TRANSFER_RECEIPT_EXCEEDS_REMAINING")
            denied = self.reject("receive", {**partial_body, "requestId": key + "-no-target"},
                                 "WAREHOUSE_ACCESS_DENIED", 403, profile="restricted")
            final_bodies = [{**partial_body, "quantityText": "4", "requestId": key + f"-last-{i}"} for i in range(2)]
            replies, locks = self.overlap("receive", "current", final_bodies, self.transfer_lock)
            require(sorted(reply[0] for reply in replies) == [200, 417], replies)
            complete = returned(next(reply for reply in replies if reply[0] == 200))
            rejection(next(reply for reply in replies if reply[0] != 200), 417, "TRANSFER_COMPLETED")
            require(complete["received"] == "10" and complete["remaining"] == "0", complete)
            balances.append(self.quantities(sku, batch, (2, 0, 10)))
            closed = self.reject("receive", {**partial_body, "quantityText": "1", "requestId": key + "-after"},
                                 "TRANSFER_COMPLETED")
            groups.append({"batch": batch, "start": start, "partial": partial, "complete": complete,
                           "balances": balances,
                           "excess": excess, "closed": closed, "denied": denied, "concurrency": locks})
        # An independent pair shares one transit bucket, but its two limits remain separate.
        sku = self.tag + "-TR-SHARED"
        self.move("RECEIPT", "shared-seed", "TR-SHARED", 20, REAL)
        shared = [returned(self.call("dispatch", {"sku": sku, "source": SOURCE, "target": TARGET,
                    "quantityText": str(qty), "batchSelection": select(REAL), "requestId": self.tag + "-shared-" + key}))
                  for key, qty in (("A", 10), ("B", 8))]
        partial = {"transferId": shared[0]["transferId"], "quantityText": "6", "requestId": self.tag + "-shared-receive"}
        returned(self.call("receive", partial))
        physical = self.quantities("TR-SHARED", REAL, (2, 12, 6))
        denial = self.reject("receive", {**partial, "quantityText": "5", "requestId": self.tag + "-shared-excess"},
                             "TRANSFER_RECEIPT_EXCEEDS_REMAINING")
        return {"round_trips": groups, "shared_bucket": {"balances": physical, "denial": denial}}

    @staticmethod
    def transfer_lock(args, kwargs):
        return args[0] == store.TRANSFER

    def overlap(self, endpoint, function, bodies, at_lock=lambda args, kwargs: True):
        gate = Barrier(2)
        original = getattr(store, function)
        observations = []

        def intersect(*args, **kwargs):
            if not at_lock(args, kwargs):
                return original(*args, **kwargs)
            connection, isolation = frappe.db.sql("select connection_id(), @@tx_isolation")[0]
            gate.wait(timeout=12)
            start = monotonic()
            result = original(*args, **kwargs)
            elapsed = int((monotonic() - start) * 1000)
            observations.append({"connection": connection, "isolation": isolation, "lock_wait_ms": elapsed})
            sleep(0.25)
            return result

        with patch.object(store, function, intersect):
            futures = [self.pool.submit(request, self.clients["operator"], "POST", API + endpoint, body=body)
                       for body in bodies]
            replies = [job.result(timeout=45) for job in futures]
        require(len({item["connection"] for item in observations}) == 2
                and max(item["lock_wait_ms"] for item in observations) >= 180,
                f"Requests did not overlap with database lock contention: {observations}")
        return replies, observations

    def concurrency(self):
        result = {}
        for sku, batch in (("RACE-REAL", REAL), ("RACE-NULL", None)):
            self.move("RECEIPT", sku + "-seed", sku, 10, batch)
            bodies = [self.movement("ISSUE", sku + str(i), self.line(sku, 7, batch)) for i in range(2)]
            replies, locks = self.overlap("move", "lock_buckets", bodies)
            require(sorted(reply[0] for reply in replies) == [200, 417], replies)
            denied = rejection(next(reply for reply in replies if reply[0] == 417), 417, "INSUFFICIENT_STOCK")
            accepted = returned(next(reply for reply in replies if reply[0] == 200))
            self.quantities(sku, batch, (3, 0, 0))
            result[sku] = {"locks": locks, "accepted": accepted, "denied": denied, "balance": "3"}
        self.move("RECEIPT", "same-seed", "SAME", 10, None)
        body = self.movement("ISSUE", "same-key", self.line("SAME", 7, None))
        replies, locks = self.overlap("move", "reserve_operation", [body, body])
        first, replay = sorted((returned(reply) for reply in replies), key=lambda item: item["replayed"])
        require(first["replayed"] is False and replay == {**first, "replayed": True}, "Concurrent replay mismatch")
        self.quantities("SAME", None, (3, 0, 0))
        conflict = self.reject("move", {**body, "lines": [self.line("SAME", 1, None)]}, "IDEMPOTENCY_CONFLICT")
        result["same_key"] = {"locks": locks, "accepted": first, "replayed": replay, "conflict": conflict, "balance": "3"}
        return result

    def faults(self):
        self.move("RECEIPT", "roll-first", "ROLL", 10, REAL)
        self.move("RECEIPT", "roll-second", "ROLL", 2, None)
        multi = self.movement("ISSUE", "roll-multi", self.line("ROLL", 3, REAL), self.line("ROLL", 3, None))
        original_leg = store.post_leg
        saw_first = []

        def observe_first(*args, **kwargs):
            result = original_leg(*args, **kwargs)
            if args[2] == 0:
                observed = frappe.db.sql("""
                    select cast(b.quantity as char), cast(e.delta as char)
                    from `tabHP Spike Balance` b join `tabHP Spike Event` e on e.bucket = b.name
                    where e.name = %s
                """, (result,))
                require(observed and list(map(Decimal, observed[0])) == [Decimal(7), Decimal(-3)],
                        "First leg is not visible in the failing transaction")
                saw_first.append(result)
            return result

        with patch.object(store, "post_leg", observe_first):
            failure = self.reject("move", multi, "INSUFFICIENT_STOCK")
        require(len(saw_first) == 1, "The second-row rejection happened before first-row writes")
        self.quantities("ROLL", REAL, (10, 0, 0))
        self.quantities("ROLL", None, (2, 0, 0))

        body = self.movement("ISSUE", "roll-event", self.line("ROLL", 3, REAL))
        event_attempts = []

        def fail_event(event_id, fields):
            row = store.current(store.BALANCE, fields["bucket"], "cast(quantity as char) as quantity")
            operation = store.current(store.OPERATION, fields["operation"], "outcome")
            require(Decimal(row.quantity) == 7 and operation and operation.outcome is None,
                    "Audit fault injected before inventory and operation writes")
            event_attempts.append([event_id, fields["operation"], frappe.db.sql("select connection_id()")[0][0]])
            raise RuntimeError("STAGE2A_INJECTED_AUDIT_FAILURE")

        with patch.object(store, "append_event", fail_event):
            audit_failure = self.reject("move", body, "STAGE2A_INJECTED_AUDIT_FAILURE", 500)
        require(len(event_attempts) == 1, "Audit failure did not reach the append")
        self.quantities("ROLL", REAL, (10, 0, 0))
        successful = returned(self.call("move", body))
        require(successful["eventIds"] == [event_attempts[0][0]] and not successful["replayed"],
                "Failed write left an idempotency result")
        before = snapshot()
        require(returned(self.call("move", body)) == {**successful, "replayed": True}
                and before == snapshot(), "Retry posted twice")
        self.quantities("ROLL", REAL, (7, 0, 0))
        return {"second_line": {**failure, "uncommitted_first_event": saw_first[0]},
                "audit_failure": {**audit_failure, "attempt": event_attempts[0], "retry": successful}}

    def counts(self):
        self.move("RECEIPT", "count-61-seed", "COUNT", 10, REAL)
        self.move("RECEIPT", "count-null-seed", "COUNT", 10, None)
        old_in = self.task("COUNT", REAL, "before-in")
        old_out = self.task("COUNT", None, "before-out")
        self.move("RECEIPT", "count-in", "COUNT", 2, REAL)
        self.move("ISSUE", "count-out", "COUNT", 2, None)
        stale_in = self.reject("submit_count", self.count(old_in, "in-stale", 5), "COUNT_STALE")
        stale_out = self.reject("submit_count", self.count(old_out, "out-stale", 5), "COUNT_STALE")
        before = self.task("COUNT", None, "round-before")
        self.move("ISSUE", "count-round-out", "COUNT", 2, None)
        self.move("RECEIPT", "count-round-in", "COUNT", 2, None)
        after = self.task("COUNT", None, "round-after")
        require(before["baseline"] == after["baseline"] == "8" and after["version"] == before["version"] + 2,
                [before, after])
        round_trip = self.reject("submit_count", self.count(before, "round-stale", 6), "COUNT_STALE")
        other = self.task("COUNT", None, "same-baseline")
        require(other["version"] == after["version"] and other["taskId"] != after["taskId"], other)
        self.move("RECEIPT", "count-unrelated", "COUNT", 1, OTHER_BATCH)
        bodies = [self.count(task, "apply-" + str(i), qty) for i, (task, qty) in enumerate(((after, 6), (other, 5)))]
        replies, locks = self.overlap("submit_count", "lock_buckets", bodies)
        require(sorted(reply[0] for reply in replies) == [200, 417], replies)
        winner = 0 if replies[0][0] == 200 else 1
        applied = returned(replies[winner])
        rejection(replies[1 - winner], 417, "COUNT_STALE")
        require(self.get("COUNT", None)["quantity"] == applied["quantity"], applied)
        record = snapshot()
        require(returned(self.call("submit_count", bodies[winner])) == {**applied, "replayed": True}
                and record == snapshot(), "Successful count was applied twice")
        reused = self.reject("submit_count", {**bodies[winner], "requestId": self.tag + "-count-reused"},
                             "COUNT_ALREADY_SUBMITTED")
        task = self.task("COUNT", None, "no-adjustment")
        unchanged = self.count(task, "no-adjustment-submit", task["baseline"])
        accepted = returned(self.call("submit_count", unchanged))
        require(not accepted["eventIds"] and self.get("COUNT", None)["version"] == task["version"],
                "No-change count modified stock")
        no_adjustment = self.reject("submit_count", {**unchanged, "requestId": self.tag + "-no-adjustment-reused"},
                                    "COUNT_ALREADY_SUBMITTED")
        zero = []
        for batch, label, qty in ((None, "null", 3), (REAL, "real", 2)):
            task = self.task("ZERO", batch, "zero-" + label)
            require(task["baseline"] == "0" and task["version"] == 0 and task["batch"] == batch, task)
            result = returned(self.call("submit_count", self.count(task, "zero-apply-" + label, qty)))
            require(self.get("ZERO", batch)["quantity"] == str(qty) and result["batch"] == batch, result)
            zero.append({"task": task, "result": result})
        return {"stale_receipt": stale_in, "stale_issue": stale_out, "round_trip": [before, after, round_trip],
                "concurrent": {"locks": locks, "applied": applied, "loser": "COUNT_STALE"},
                "already_submitted": reused, "no_adjustment": {"result": accepted, "reused": no_adjustment},
                "zero": zero}

    def corrections(self):
        results = {}
        for kind, key, batch, qty, initial in (("RECEIPT", "fix-receipt", REAL, 12, 0),
                                               ("ISSUE", "fix-issue", None, 4, 12)):
            if kind == "ISSUE":
                self.move("RECEIPT", "fix-null-seed", "FIX", initial, batch)
            root = self.move(kind, key, "FIX", qty, batch)
            event = root["eventIds"][0]
            connect()
            before = frappe.db.sql("select * from `tabHP Spike Event` where name = %s", (event,))
            body = {"eventId": event, "requestId": self.tag + "-" + key + "-reverse"}
            correction = returned(self.call("correct", body))
            require(correction["batch"] == batch and self.get("FIX", batch)["quantity"] == str(initial), correction)
            connect()
            after = frappe.db.sql("select * from `tabHP Spike Event` where name = %s", (event,))
            related = frappe.db.sql("select reversal_of from `tabHP Spike Event` where name=%s",
                                    (correction["eventIds"][0],))
            require(before == after and related == ((event,),), "Original event changed or reversal link is missing")
            digest = snapshot()
            require(returned(self.call("correct", body)) == {**correction, "replayed": True}
                    and snapshot() == digest, "Correction replay changed stock")
            results[kind] = {"original": event, "correction": correction, "restored_balance": initial}
        receipt = self.move("RECEIPT", "downstream-receipt", "FIX", 10, OTHER_BATCH)
        self.move("ISSUE", "downstream-issue", "FIX", 7, OTHER_BATCH)
        require(self.get("FIX", OTHER_BATCH)["quantity"] == "3", "Incorrect downstream control")
        body = {"eventId": receipt["eventIds"][0], "requestId": self.tag + "-downstream-reverse"}
        results["downstream"] = self.reject("correct", body, "DOWNSTREAM_STOCK_REVIEW_REQUIRED")
        # Replenishment cannot establish that the originally received physical stock is still present.
        self.move("RECEIPT", "downstream-replenish", "FIX", 10, OTHER_BATCH)
        results["downstream_after_restock"] = self.reject("correct", body, "DOWNSTREAM_STOCK_REVIEW_REQUIRED")
        return results

    def permissions(self):
        require(self.get("STOCK", REAL, profile="query")["quantity"] == "7", "Query could not read authorized stock")
        require(self.get("STOCK", REAL, profile="restricted")["quantity"] == "7", "Scoped Operator could not read")
        allowed = self.movement("RECEIPT", "permission-allowed", self.line("STOCK", 1, REAL))
        require(not returned(self.call("move", allowed, profile="restricted"))["replayed"],
                "Scoped Operator could not write")
        errors = {
            "query_write": self.reject("move", allowed, "INVENTORY_OPERATION_DENIED", 403, profile="query"),
            "unauthorized_read": self.reject("balance", {"sku": self.tag + "-STOCK", "location": TARGET,
                                             "batchMode": "NO_BATCH"}, "WAREHOUSE_ACCESS_DENIED", 403,
                                             profile="restricted", method="GET"),
            "scope_before_sku_lookup": self.reject("balance", {"sku": "UNKNOWN", "location": TARGET,
                                                  "batchMode": "NO_BATCH"}, "WAREHOUSE_ACCESS_DENIED", 403,
                                                  profile="restricted", method="GET"),
            "unauthorized_write": self.reject("move", self.movement("RECEIPT", "permission-target",
                                              self.line("STOCK", 1, REAL, TARGET)),
                                              "WAREHOUSE_ACCESS_DENIED", 403, profile="restricted"),
            "none": self.reject("balance", {"sku": self.tag + "-STOCK", "location": SOURCE, "batchMode": "NO_BATCH"},
                                "WAREHOUSE_ACCESS_DENIED", 403, profile="none", method="GET"),
            "no_role": self.reject("balance", {"sku": self.tag + "-STOCK", "location": SOURCE, "batchMode": "NO_BATCH"},
                                   "INVENTORY_OPERATION_DENIED", 403, profile="outsider", method="GET"),
            "transit": self.reject("move", self.movement("ISSUE", "permission-transit",
                                   self.line("STOCK", 1, REAL, store.TRANSIT)), "WAREHOUSE_ACCESS_DENIED", 403),
        }
        warehouse, location = "W4-" + self.tag, "L4-" + self.tag
        connect()
        ensure(store.WAREHOUSE, warehouse, code=warehouse)
        ensure(store.LOCATION, location, code=location, warehouse_id=warehouse, in_transit=0)
        frappe.db.commit()
        self.move("RECEIPT", "dynamic", "STOCK", 2, REAL, location)
        require(self.get("STOCK", REAL, location)["quantity"] == "2", "ALL did not include the new warehouse")
        errors["selected_new_warehouse"] = self.reject("balance", {"sku": self.tag + "-STOCK", "location": location,
                                                       "batchMode": "NO_BATCH"}, "WAREHOUSE_ACCESS_DENIED", 403,
                                                       profile="restricted", method="GET")

        connect()
        sku = self.tag + "-STOCK"
        samples = {table: frappe.db.get_value(table, {"sku": sku}, "name") for table in (store.BALANCE, store.EVENT)}
        before = snapshot()
        native = []
        for profile in ("operator", "query", "outsider"):
            for table, row in samples.items():
                collection = "/api/resource/" + quote(table)
                for verb, path, body in (("GET", collection, None), ("POST", collection, {"sku": sku}),
                                         ("PUT", collection + "/" + row, {"quantity": "999", "delta": "999"}),
                                         ("DELETE", collection + "/" + row, None)):
                    reply = self.pool.submit(request, self.clients[profile], verb, path, body=body).result(timeout=45)
                    native.append([profile, table, verb, rejection(reply, 403, None)["http"]])
        require(snapshot() == before, "Native REST changed experiment rows")
        sample = frappe.get_doc(store.EVENT, samples[store.EVENT])
        sample.delta = 999
        try:
            sample.save()
        except frappe.PermissionError as error:
            require("STOCK_SERVICE_REQUIRED" in str(error), f"Unexpected admin write rejection: {error}")
        else:
            raise RuntimeError("Generic administrator document write reached the event store")
        finally:
            frappe.db.rollback()
        require(snapshot() == before, "Administrator document write changed stock")
        return {"denials": errors, "native": native, "dynamic_warehouse": warehouse, "admin_doc_mutation": "denied"}


def reconcile(tag):
    connect()
    # A new connection verifies every persisted bucket, not the in-process return values.
    storage = frappe.db.sql("""
        select table_name, column_name, column_type, is_nullable, extra
        from information_schema.columns where table_schema = database()
        and table_name in ('tabHP Spike Balance', 'tabHP Spike Event', 'tabHP Spike Count Task')
        and column_name in ('batch_id', 'batch_slot', 'quantity', 'delta', 'baseline')
        order by table_name, column_name
    """)
    require(all(coltype == "decimal(21,9)" for _, col, coltype, _, _ in storage
                if col in ("quantity", "delta", "baseline")), storage)
    require(all(nullable == "YES" for _, col, _, nullable, _ in storage if col == "batch_id"), storage)
    buckets = frappe.db.sql("""
        select name, sku, location, batch_id, cast(quantity as char) as quantity, bucket_version
        from `tabHP Spike Balance` where sku like %s order by name
    """, (tag + "-%",), as_dict=True)
    events = frappe.db.sql("""
        select name, operation, operation_type, bucket, sku, location, batch_id, bucket_version, owner, creation,
            cast(delta as char) as delta, reversal_of
        from `tabHP Spike Event` where sku like %s order by bucket, bucket_version
    """, (tag + "-%",), as_dict=True)
    operations = frappe.db.sql("""
        select name, operation_type, actor, transfer_ref, outcome from `tabHP Spike Operation`
        where actor like %s order by name
    """, (f"spike-{tag.lower()}-%@example.invalid",), as_dict=True)
    by_bucket, by_operation = defaultdict(list), defaultdict(list)
    for event in events:
        by_bucket[event.bucket].append(event)
        by_operation[event.operation].append(event)
    for bucket in buckets:
        steps = by_bucket.pop(bucket.name, [])
        total = Decimal(0)
        for number, row in enumerate(steps, 1):
            require((row.sku, row.location, row.batch_id) == (bucket.sku, bucket.location, bucket.batch_id)
                    and row.bucket_version == number, f"Bucket identity/version mismatch: {row.name}")
            total += Decimal(row.delta)
            require(total >= 0, f"Negative historical quantity at {row.name}")
        require(total == Decimal(bucket.quantity) and len(steps) == bucket.bucket_version, bucket)
    require(not by_bucket, "Events without balances")
    for op in operations:
        require(op.outcome, f"Unfinished operation: {op.name}")
        outcome = json.loads(op.outcome)["result"]
        steps = by_operation.pop(op.name, [])
        require(outcome["operationId"] == op.name and set(outcome.get("eventIds", [])) == {row.name for row in steps}
                and all(row.owner == op.actor and row.operation_type == op.operation_type for row in steps),
                f"Operation and event mismatch: {op.name}")
        if op.operation_type == "RECEIVE":
            require(op.transfer_ref == outcome["transferId"], "Operation lacked native transfer reference")
    require(not by_operation, "Events without operations")

    transfers = frappe.db.sql("""
        select name, sku, source, target, batch_id, cast(dispatched as char) as dispatched,
            cast(received as char) as received
        from `tabHP Spike Transfer` where sku like %s order by name
    """, (tag + "-%",), as_dict=True)
    for transfer in transfers:
        inbound = {op.name for op in operations if op.transfer_ref == transfer.name and op.operation_type == "RECEIVE"}
        dispatched = {row.location: Decimal(row.delta) for row in events if row.operation == transfer.name}
        require(dispatched == {transfer.source: -Decimal(transfer.dispatched), store.TRANSIT: Decimal(transfer.dispatched)},
                "Incorrect dispatch ledger legs")
        received = [row for row in events if row.operation in inbound]
        require(all(row.batch_id == transfer.batch_id and row.sku == transfer.sku
                    and row.location in (transfer.target, store.TRANSIT) for row in received)
                and sum(Decimal(row.delta) for row in received if row.location == transfer.target) == Decimal(transfer.received)
                and sum(Decimal(row.delta) for row in received) == 0,
                "Receipt ledger and transfer relation do not match")

    tasks = frappe.db.sql("""
        select name, sku, location, batch_id, cast(baseline as char) as baseline, baseline_version, applied_operation
        from `tabHP Spike Count Task` where sku like %s order by name
    """, (tag + "-%",), as_dict=True)
    for task in tasks:
        scoped = [row for row in events if (row.sku, row.location, row.batch_id) == (task.sku, task.location, task.batch_id)]
        require(sum(Decimal(row.delta) for row in scoped if row.bucket_version <= task.baseline_version)
                == Decimal(task.baseline), f"Task baseline drift: {task.name}")
        if count := task.applied_operation:
            require(any(op.name == count and op.operation_type == "COUNT" for op in operations),
                    "Task does not reference its committed count")
    return {"apps": sorted(frappe.get_installed_apps()), "connection": frappe.db.sql("select connection_id(), @@tx_isolation")[0],
            "storage": storage, "balances": buckets, "events": events,
            "operations": [{**op, "outcome": json.loads(op.outcome)} for op in operations],
            "transfers": transfers, "count_tasks": tasks}


def run(tag):
    require(Path.cwd() == Path("/workspace/development/frappe-bench/sites"), "Run only from the existing lab Bench sites directory")
    require(re.fullmatch(r"[A-Z0-9]{3,12}", tag), "Run ID must contain 3–12 uppercase letters or digits")
    connect()
    require(not frappe.db.exists(store.SKU, tag + "-STOCK"), "Use a fresh synthetic run ID")
    fixtures(tag)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            clients = identities(tag)
            for profile, headers in clients.items():
                reply = pool.submit(request, headers, "GET", "/api/method/frappe.auth.get_logged_user").result(timeout=45)
                require(returned(reply) == account(tag, profile), "Unexpected authenticated test identity")
            cases = Cases(tag, pool, clients)
            results = {func: getattr(cases, func)() for func in ("stock", "transfers", "concurrency", "faults",
                       "counts", "corrections", "permissions")}
        results["fresh_connection"] = reconcile(tag)
        return results
    finally:
        connect()
        for profile in PROFILES:
            user = account(tag, profile)
            if frappe.db.exists("User", user):
                doc = frappe.get_doc("User", user)
                doc.api_key = doc.api_secret = None
                doc.save()
        frappe.db.commit()
        require(all(not any(frappe.db.get_value("User", account(tag, profile), ["api_key", "api_secret"]))
                    for profile in PROFILES if frappe.db.exists("User", account(tag, profile))),
                "Temporary test API key remains enabled")
        frappe.destroy()


if __name__ == "__main__":
    output = sys.stdout
    with redirect_stdout(sys.stderr):
        args = sys.argv[1:]
        result = reconcile(args[1]) if len(args) == 2 and args[0] == "final" else run(args[0])
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str), file=output)
