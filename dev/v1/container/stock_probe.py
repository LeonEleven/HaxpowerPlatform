"""Exercise the stock kernel on independent MariaDB connections with synthetic documents only."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from decimal import Decimal, localcontext
from pathlib import Path
from threading import Barrier, Event
from time import monotonic, sleep
from unittest.mock import patch
from uuid import uuid4

import frappe

from haxpower_v1.access.config import set_scope
from haxpower_v1.access.policy import Mode, Operation
from haxpower_v1.catalog.sku_persistence import SKU
from haxpower_v1.catalog.test_sku import NO_STOCK_WRITES
from haxpower_v1.inventory.batch.authorization import POLICY_SETTING
from haxpower_v1.inventory.batch.dto import BatchKind
from haxpower_v1.inventory.batch.normalization import CANDIDATE_VERSION
from haxpower_v1.inventory.batch.persistence import MAPPING
from haxpower_v1.inventory.batch.service import resolve_or_create
from haxpower_v1.inventory.guard import lock_stock_writes
from haxpower_v1.inventory.operations import persistence as store, service, synthetic
from haxpower_v1.inventory.operations.dto import Line, QuantitySign, StockError
from haxpower_v1.inventory.operations.quantity import quantity_text
from haxpower_v1.inventory.operations.transaction import connection
from haxpower_v1.warehouse import service as warehouse

from access_probe import synthetic_user
from batch_probe import _sku_id
from identity import require
from sku_probe import NATIVE, PREFIX
from warehouse_probe import _company_and_parent

LIMITS = {PREFIX: "1000"}


def snapshot():
    return {
        doctype: {
            "schema": frappe.db.sql(f"SHOW CREATE TABLE `tab{doctype}`")[0][1],
            "rows": frappe.get_all(doctype, fields=["*"], order_by="name"),
        } if frappe.db.table_exists(doctype) else None
        for doctype in store.TABLES
    }


def fresh_state():
    with connection() as db:
        connection_id, = db.sql("SELECT CONNECTION_ID()")[0]
        return connection_id, {**snapshot(), "native_stock": native_stock()}


def native_stock():
    return {
        name: frappe.get_all(name, fields=["*"], order_by="name")
        for name in NO_STOCK_WRITES if name != "Batch"
    }


@contextmanager
def session(site, actor, company):
    frappe.init(site=site, sites_path=str(Path.cwd()))
    frappe.connect()
    frappe.set_user(actor)
    frappe.conf[synthetic.SETTING] = {actor: company}
    try:
        yield
    finally:
        frappe.destroy()


@contextmanager
def actor_session(actor, company):
    previous = frappe.session.user
    config = frappe.conf.get(synthetic.SETTING)
    frappe.set_user(actor)
    frappe.conf[synthetic.SETTING] = {actor: company}
    try:
        yield
    finally:
        frappe.set_user(previous)
        if config is None:
            frappe.conf.pop(synthetic.SETTING, None)
        else:
            frappe.conf[synthetic.SETTING] = config


def expect(code, call):
    try:
        call()
    except StockError as error:
        require(error.code == code, f"expected {code}, observed {error.code}")
    except frappe.PermissionError:
        require(code == "denied", f"expected {code}, observed permission denial")
    else:
        raise AssertionError(f"expected {code}, observed success")


def rows(state, doctype, **fields):
    return [row for row in state[doctype]["rows"] if all(row[key] == value for key, value in fields.items())]


def verify_result(command, result, state):
    document, = rows(state, store.DOCUMENT, name=command.document_id)
    request, = rows(state, store.REQUEST, document_id=command.document_id)
    log, = rows(state, store.LOG, document_id=command.document_id)
    links = rows(state, store.LINK, operation_id=log.name)
    lines = rows(state, store.LINE, document_id=command.document_id)
    parents = rows(state, store.SYNTHETIC_DOCUMENT, document_id=command.document_id)
    require({line.name for line in lines} == {line.line_id for line in command.lines}
            and len(lines) == len(command.lines), "business line identities changed")
    require(document.revision == request.result_revision == log.result_revision == result.revision == 2
            and document.status == log.status == "Effective" and request.status == "Completed"
            and document.effective_operation == request.result_operation == log.name == result.operation_id
            and request.name == log.request_id and log.actor == request.actor == document.owner
            and request.idempotency_key == command.idempotency_key
            and log.reason == command.reason and not log.original_operation
            and len(request.payload_digest) == 64 and request.completed_at, "stock result graph does not agree")
    require(len(parents) == 2 and len(links) == len(lines) * 2
            and {link.parent_id for link in links} == {parent.name for parent in parents}
            and {link.detail_id for link in links} == {detail.name for parent in parents for detail in
                rows(state, store.SYNTHETIC_DETAIL, parent=parent.name)}, "synthetic native coverage is incomplete")
    for line in lines:
        references = [link for link in links if link.line_id == line.name]
        require({link.role for link in references} == {"Primary", "Secondary"}, "missing native roles for line")
        for reference in references:
            detail, = rows(state, store.SYNTHETIC_DETAIL, name=reference.detail_id)
            require(detail.parent == reference.parent_id and detail.line_id == line.name
                    and detail.parenttype == reference.parent_type == store.SYNTHETIC_DOCUMENT
                    and reference.detail_type == store.SYNTHETIC_DETAIL, "native row identity is invalid")


def race(site, actor, first, second, *, expected, same_result=False):
    ready = Barrier(2, timeout=15)

    def attempt(command):
        with session(site, actor, command.company):
            identity = None
            commits = 0

            def before_guard(db):
                nonlocal identity
                identity, isolation = db.sql("SELECT CONNECTION_ID(), @@tx_isolation")[0]
                require(isolation == "REPEATABLE-READ", "the stale snapshot test requires repeatable read")
                doc = db.get_value(store.DOCUMENT, command.document_id, "status")
                require(doc == "Draft" and not db.exists(store.REQUEST, {"document_id": command.document_id}),
                        "both transactions must establish a pre-commit read view")
                ready.wait()

            def commit(db):
                nonlocal commits
                db.sql("COMMIT")
                commits += 1

            try:
                result = synthetic.execute(command, limits=LIMITS, before_guard=before_guard, commit=commit)
                return identity, "ok", commits, result
            except StockError as failure:
                return identity, failure.code, commits, None

    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(attempt, command) for command in (first, second)]
        left, right = [job.result(timeout=45) for job in jobs]
    verifier, state = fresh_state()
    require(len({verifier, left[0], right[0]}) == 3, "race did not use three independent connections")
    require(sorted([left[1], right[1]]) == sorted(expected), f"unexpected concurrent outcomes {left[1]}, {right[1]}")
    for command, outcome in ((first, left), (second, right)):
        if outcome[1] == "ok":
            verify_result(command, outcome[3], state)
    if same_result:
        require(left[3] == right[3] and left[2] + right[2] == 1, "same-key replay repeated execution")
    else:
        require(left[2] + right[2] == expected.count("ok"), "one commit per winning action required")
    print(f"Two REPEATABLE READ connections and fresh verifier: {', '.join(expected)}.", flush=True)
    return left[3] or right[3]


def _exception(*_args):
    raise RuntimeError("synthetic failure")


def verify_faults(draft, execute):
    def fault_insert(doctype):
        original = store.insert

        def duplicate(table, **kwargs):
            identity = original(table, **kwargs)
            if table == doctype:
                original(table, **dict(kwargs, name=identity))
            return identity

        return patch.object(store, "insert", duplicate)

    def introduce_stale_version(document, result):
        frappe.db.set_value(store.DOCUMENT, document.name, "revision", document.revision + 1)
        update_document(document, result)

    def introduce_stale_request(request_id, result):
        frappe.db.set_value(store.REQUEST, request_id, "status", "Completed")
        complete_request(request_id, result)

    def attempt_early_commit(_plan, _index):
        # Even a swallowed guard error poisons the adapter attempt.
        try:
            frappe.db.commit()
        except StockError:
            pass

    def attempt_callback(_plan, _index):
        frappe.db.after_commit.add(_exception)

    def attempt_sql_commit(_plan, _index):
        frappe.db.sql("COMMIT")

    def wrong_native_reference(plan, index):
        if index == 2:
            frappe.db.sql(f"UPDATE `tab{store.SYNTHETIC_DETAIL}` SET line_id=%s WHERE idx=2 AND parent IN "
                          f"(SELECT name FROM `tab{store.SYNTHETIC_DOCUMENT}` WHERE document_id=%s)",
                          (plan.command.lines[0].line_id, plan.command.document_id))

    def invalid_second_line(command):
        return replace(command, lines=(
            command.lines[0], replace(command.lines[1], quantity="-0.125"), *command.lines[2:],
        ))

    def drop_before_commit(db):
        db.close()
        raise ConnectionError("synthetic disconnect before COMMIT")

    def trip_second_detail(_plan, index):
        if index == 2:
            _exception()

    update_document = store.update_document
    complete_request = store.complete_request
    command = draft(reason="faults")
    _, before = fresh_state()
    cases = (
        ("invalid second quantity", "STOCK_QUANTITY_SIGN", lambda: execute(invalid_second_line(command))),
        ("second synthetic native insert", "STOCK_PERSISTENCE_FAILED",
         lambda: execute(command, after_detail=trip_second_detail)),
        ("invalid native detail identity", "STOCK_NATIVE_REFERENCE_INVALID",
         lambda: execute(command, after_detail=wrong_native_reference)),
        ("swallowed adapter commit", "STOCK_ADAPTER_SIDE_EFFECT",
         lambda: execute(command, after_detail=attempt_early_commit)),
        ("adapter callback", "STOCK_ADAPTER_SIDE_EFFECT", lambda: execute(command, after_detail=attempt_callback)),
        ("adapter SQL commit", "STOCK_ADAPTER_SIDE_EFFECT",
         lambda: execute(command, after_detail=attempt_sql_commit)),
        ("real duplicate key during log head write", "STOCK_PERSISTENCE_FAILED",
         lambda: _with_fault(fault_insert(store.LOG), execute, command)),
        ("real duplicate key during log link write", "STOCK_PERSISTENCE_FAILED",
         lambda: _with_fault(fault_insert(store.LINK), execute, command)),
        ("document version compare-and-set", "STOCK_DOCUMENT_CHANGED",
         lambda: _with_fault(patch.object(store, "update_document", introduce_stale_version), execute, command)),
        ("request result compare-and-set", "STOCK_REQUEST_CHANGED",
         lambda: _with_fault(patch.object(store, "complete_request", introduce_stale_request), execute, command)),
        ("disconnect before COMMIT", "STOCK_RETRY_REQUIRED", lambda: execute(command, commit=drop_before_commit)),
    )
    for label, code, operation in cases:
        expect(code, operation)
        require(fresh_state()[1] == before, f"fresh connection found side effects after {label}")
        print(f"Fresh database connection verified full rollback after {label}.", flush=True)
    verify_result(command, execute(command), fresh_state()[1])


def _with_fault(fault, execute, command):
    with fault:
        return execute(command)


def verify_lock_timeout(draft, execute):
    command = draft(reason="lock timeout")
    _, before = fresh_state()
    # Hold the SKU so the command blocks after staging the uncommitted request.
    with connection() as blocker:
        blocker.get_value(SKU, command.lines[0].sku_id, for_update=True)
        expect("STOCK_RETRY_REQUIRED", lambda: execute(
            command, before_guard=lambda db: db.sql("SET SESSION innodb_lock_wait_timeout=1"),
        ))
        require(fresh_state()[1] == before, "lock timeout left partial writes on a fresh connection")
    verify_result(command, execute(command), fresh_state()[1])
    print("Real MariaDB lock timeout rolled back the staged request and allowed a same-key retry.", flush=True)


def verify_lost_response(draft, execute):
    command = draft(reason="lost response")
    applied_connections, recovered_connections = [], []
    original_stage = service._stage

    def stage(db, *args, recovery, **kwargs):
        if recovery:
            recovered_connections.append(db.sql("SELECT CONNECTION_ID()")[0][0])
        return original_stage(db, *args, recovery=recovery, **kwargs)

    def lost(db):
        applied_connections.append(db.sql("SELECT CONNECTION_ID()")[0][0])
        db.sql("COMMIT")
        db.close()
        raise ConnectionError("synthetic response loss after COMMIT")

    with patch.object(service, "_stage", stage):
        result = execute(command, commit=lost)
    verifier, before = fresh_state()
    require(len(set([verifier, *applied_connections, *recovered_connections])) == 3,
            "lost result was not resolved on a new connection")
    require(execute(command, commit=_exception) == result and fresh_state()[1] == before,
            "lost-response retry changed the committed result")
    verify_result(command, result, before)
    print("COMMIT succeeded before connection loss. A new connection returned the same persisted result.", flush=True)


def verify_batch_competition(site, actor, command, existing):
    selection = {"mode": "BATCH", "batchNo": f"STOCK-EXECUTION-{command.document_id}"}
    ready, read_view = Event(), Event()
    sku_id = existing.sku

    def create_batch():
        with session(site, "Administrator", command.company):
            frappe.conf[POLICY_SETTING] = CANDIDATE_VERSION
            lock_stock_writes()
            result = resolve_or_create(sku_id, selection)
            db_id, = frappe.db.sql("SELECT CONNECTION_ID()")[0]
            require(result.created, "expected a first batch mapping")
            ready.set()
            require(read_view.wait(15), "execution did not establish a read view")
            deadline = monotonic() + 10
            while not frappe.db.sql(
                "SELECT 1 FROM information_schema.PROCESSLIST WHERE ID=%s AND LOWER(INFO) LIKE %s",
                (seen[0], "%tabhp v1 stock write guard%for update%"),
            ):
                require(monotonic() < deadline, "stock execution did not wait on the shared guard")
                sleep(0.02)
            frappe.db.commit()
            return db_id, result.batch.id

    seen = []

    def run_with_old_view(db):
        seen.append(db.sql("SELECT CONNECTION_ID()")[0][0])
        require(not db.exists(MAPPING, {"sku": sku_id, "original_text": selection["batchNo"]}),
                "uncommitted Batch was visible before lock acquisition")
        read_view.set()

    def execute():
        require(ready.wait(15), "batch creator did not reach the guard")
        with session(site, actor, command.company):
            return synthetic.execute(command, limits=LIMITS, before_guard=run_with_old_view)

    with ThreadPoolExecutor(max_workers=2) as pool:
        batch_job, stock_job = pool.submit(create_batch), pool.submit(execute)
        batch_connection, batch_id = batch_job.result(timeout=45)
        result = stock_job.result(timeout=45)

    with connection() as db:
        verifier, = db.sql("SELECT CONNECTION_ID()")[0]
        require(len({verifier, batch_connection, *seen}) == 3, "Batch and stock contention reused a connection")
        require(db.get_value(MAPPING, batch_id, "original_text") == selection["batchNo"]
                and db.get_value(MAPPING, existing.name, "*", as_dict=True) == existing,
                "existing Batch identity drifted while a new one was provisioned")
        verify_result(command, result, snapshot())
    print("Shared guard serialized V1-06 first Batch creation with stock execution. Existing mapping stayed unchanged.",
          flush=True)


def run(site):
    company, _ = _company_and_parent()
    first, second = warehouse.list_warehouses(company=company)[:2]
    a, b = [warehouse.list_locations(record.id)[0].id for record in (first, second)]
    sku_id, other_sku = _sku_id("A"), _sku_id("B")
    mapped = frappe.db.get_value(MAPPING, {"sku": sku_id, "original_text": "STD61"}, "*", as_dict=True)
    require(mapped, "expected a V1-06 migrated Batch mapping")
    before_stock = native_stock()
    previous = {row.name for row in frappe.get_all(store.REQUEST, fields=["name"])}
    run_id = f"v107-{uuid4().hex[:12]}"
    actor, _ = synthetic_user(run_id, "stock", Operation.RECEIVE)
    denied, _ = synthetic_user(run_id, "no-capability")
    disabled, _ = synthetic_user(run_id, "disabled", Operation.RECEIVE, enabled=0)
    no_scope, _ = synthetic_user(run_id, "no-scope", Operation.RECEIVE)
    set_scope(actor, company, Mode.SELECTED, [first.id, second.id])
    set_scope(denied, company, Mode.ALL)
    set_scope(disabled, company, Mode.ALL)
    frappe.db.commit()

    lines = (
        Line("", 1, sku_id, "1.125", PREFIX, BatchKind.NO_BATCH, source=a, target=b),
        Line("", 2, sku_id, "0.125", PREFIX, BatchKind.NO_BATCH, source=a, target=b),
        Line("", 3, sku_id, "2.375", PREFIX, BatchKind.BATCH, mapped.name, source=a, target=b),
        Line("", 4, other_sku, "1.000", PREFIX, BatchKind.NO_BATCH, source=b, target=a),
    )

    def draft(reason="", *, command_lines=lines):
        with actor_session(actor, company):
            return synthetic.create_draft(company, command_lines, limits=LIMITS, reason=reason)

    def execute(command, **kwargs):
        with actor_session(actor, company):
            return synthetic.execute(command, limits=LIMITS, **kwargs)

    command = draft("same normalized payload")
    equivalent = replace(command, lines=tuple(replace(line, quantity=line.quantity + (
        "00" if "." in line.quantity else ".000"
    )) for line in reversed(command.lines)))
    result = race(site, actor, command, equivalent, expected=("ok", "ok"), same_result=True)
    require(execute(equivalent, commit=_exception) == result, "completed response was not replayed")
    original_plan = synthetic._native_links
    demands = []

    def inspect_demands(plan):
        demands.append(plan.source_demand)
        return original_plan(plan)

    conflicting = draft("conflicting payload")
    race(site, actor, conflicting, replace(conflicting, reason="different"),
         expected=("ok", "STOCK_IDEMPOTENCY_CONFLICT"))

    competing = draft("different keys")
    race(site, actor, competing, replace(competing, idempotency_key=uuid4().hex),
         expected=("ok", "STOCK_DOCUMENT_ALREADY_EFFECTIVE"))
    left, right = draft("first document"), draft("reversed different document", command_lines=tuple(reversed(lines)))
    race(site, actor, left, replace(right, lines=tuple(reversed(right.lines))), expected=("ok", "ok"))

    unprovisioned = frappe.db.get_value(SKU, {"native_item": NATIVE})
    require(unprovisioned, "expected an adopted synthetic SKU")
    novel = draft("first unprovisioned bucket", command_lines=(
        Line("", 1, unprovisioned, "1.000", PREFIX, BatchKind.NO_BATCH, target=b),
    ))
    with patch.object(synthetic, "_native_links", inspect_demands):
        precise = draft("decimal round trip")
        with localcontext() as context:
            context.prec = 2
            precise_result = execute(precise)
        novel_result = execute(novel)
    state = fresh_state()[1]
    verify_result(precise, precise_result, state)
    verify_result(novel, novel_result, state)
    require({(key.sku_id, key.batch_kind, key.batch_id): value for key, value in demands[0].items()} == {
        (sku_id, BatchKind.NO_BATCH, None): Decimal("1.250"),
        (sku_id, BatchKind.BATCH, mapped.name): Decimal("2.375"),
        (other_sku, BatchKind.NO_BATCH, None): Decimal("1.000"),
    }, "same-bucket quantity aggregation lost precision or crossed a batch boundary")
    require({row.quantity for row in rows(state, store.LINE, document_id=precise.document_id)}
            == {"1.125", "0.125", "2.375", "1"}, "canonical decimal failed the database round trip")
    require(not frappe.db.exists(MAPPING, {"sku": novel.lines[0].sku_id}),
            "a first bucket unexpectedly created a Batch mapping")
    print("Decimal round trip and aggregation verified from real persisted lines. First bucket made no mapping writes.",
          flush=True)

    denied_case = draft("unauthorized")
    before = fresh_state()[1]
    for unauthorized in (denied, disabled, no_scope, "Administrator"):
        with actor_session(unauthorized, company):
            expect("denied", lambda: synthetic.execute(denied_case, limits=LIMITS))
        require(fresh_state()[1] == before, "denied actor wrote persisted execution rows")
    set_scope(actor, company, Mode.SELECTED, [first.id])
    frappe.db.commit()
    expect("denied", lambda: execute(command))  # already committed
    expect("denied", lambda: execute(denied_case))
    require(fresh_state()[1] == before, "scope revocation changed persisted execution rows")
    set_scope(actor, company, Mode.SELECTED, [first.id, second.id])
    frappe.db.sql("DELETE FROM `tabHas Role` WHERE parent=%s AND role=%s", (actor, Operation.RECEIVE.role))
    frappe.db.commit()
    expect("denied", lambda: execute(command))
    require(fresh_state()[1] == before, "capability revocation changed persisted execution rows")
    frappe.get_doc("User", actor).add_roles(Operation.RECEIVE.role)
    frappe.db.commit()
    require(execute(command) == result, "restored authorization did not return the original result")
    print("Fresh authorization rejected replay after operation and warehouse grants were separately revoked.", flush=True)

    with actor_session(actor, company):
        with connection():
            stored = frappe.get_doc(store.DOCUMENT, command.document_id)
            stored.status = "Draft"
            expect("denied", lambda: stored.save(ignore_permissions=True))
            expect("denied", lambda: frappe.delete_doc(store.DOCUMENT, stored.name, ignore_permissions=True,
                                                       force=True))
    require(fresh_state()[1] == before, "ordinary maintenance changed an effective document")

    sku = frappe.db.get_value(SKU, sku_id, ("stock_uom", "quantity_precision"), as_dict=True)
    validation_cases = (
        (1.0, PREFIX, QuantitySign.POSITIVE, LIMITS, "STOCK_QUANTITY_INVALID"),
        ("NaN", PREFIX, QuantitySign.POSITIVE, LIMITS, "STOCK_QUANTITY_INVALID"),
        ("Infinity", PREFIX, QuantitySign.POSITIVE, LIMITS, "STOCK_QUANTITY_INVALID"),
        ("alphabetic", PREFIX, QuantitySign.POSITIVE, LIMITS, "STOCK_QUANTITY_INVALID"),
        ("1e140", PREFIX, QuantitySign.POSITIVE, LIMITS, "STOCK_QUANTITY_INVALID"),
        ("0", PREFIX, QuantitySign.POSITIVE, LIMITS, "STOCK_QUANTITY_SIGN"),
        ("1.0001", PREFIX, QuantitySign.POSITIVE, LIMITS, "STOCK_QUANTITY_PRECISION"),
        ("1", "other unit", QuantitySign.POSITIVE, LIMITS, "STOCK_UOM_MISMATCH"),
        ("1000.001", PREFIX, QuantitySign.POSITIVE, LIMITS, "STOCK_QUANTITY_LIMIT"),
        ("1", PREFIX, QuantitySign.POSITIVE, {}, "STOCK_QUANTITY_LIMIT_UNCONFIGURED"),
    )
    for quantity, unit, sign, limits, error in validation_cases:
        expect(error, lambda: quantity_text(quantity, unit, sku=sku, sign=sign, limits=limits))
    require(quantity_text("-0.000", PREFIX, sku=sku, sign=QuantitySign.NONNEGATIVE, limits=LIMITS) == "0"
            and quantity_text("-1.250", PREFIX, sku=sku, sign=QuantitySign.NONZERO, limits=LIMITS) == "-1.25",
            "zero and signed Decimal semantics failed")
    verify_faults(draft, execute)
    verify_lock_timeout(draft, execute)
    verify_lost_response(draft, execute)
    verify_batch_competition(site, actor, draft("shared batch guard"), mapped)
    frappe.db.rollback()
    require(native_stock() == before_stock,
            "synthetic command changed native ERPNext stock, bundle, ledger or accounting rows")
    require({request.actor for request in fresh_state()[1][store.REQUEST]["rows"]
             if request.name not in previous} == {actor},
            "execution did not persist the authenticated synthetic actor")
    verify_persisted()


def verify_persisted():
    _, state = fresh_state()
    requests = state[store.REQUEST]["rows"]
    links = state[store.LINK]["rows"]
    require(requests and len(requests) == len(state[store.LOG]["rows"])
            and len(links) == len(state[store.SYNTHETIC_DETAIL]["rows"])
            and {link.operation_id for link in links} == {req.result_operation for req in requests},
            "persisted stock execution records have missing references")
    for req in requests:
        log, = rows(state, store.LOG, name=req.result_operation)
        document, = rows(state, store.DOCUMENT, name=req.document_id)
        require(req.status == "Completed" and len(req.payload_digest) == 64
                and req.result_operation == document.effective_operation and log.request_id == req.name,
                "persisted operation or request identity drift")
    print(f"Fresh connection verified {len(requests)} committed requests and {len(links)} stable native links.",
          flush=True)
