"""Run native Material Issue on the locked disposable ERPNext sites."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

import frappe

from haxpower_v1.access.config import set_scope
from haxpower_v1.access.policy import Mode, Operation
from haxpower_v1.adapters.erpnext import issue as native, stock_entry as stock
from haxpower_v1.catalog.sku_persistence import SKU
from haxpower_v1.inventory.batch.persistence import MAPPING
from haxpower_v1.inventory.issue import service
from haxpower_v1.inventory.issue.dto import IssueDraftInput, IssueLineInput, SubmitIssue
from haxpower_v1.inventory.operations import persistence as store
from haxpower_v1.inventory.operations.dto import StockError
from haxpower_v1.inventory.operations.transaction import connection
from haxpower_v1.inventory.receipt import service as receipts
from haxpower_v1.inventory.receipt.dto import ReceiptDraftInput, ReceiptLineInput, SubmitReceipt
from haxpower_v1.warehouse import service as warehouse
from haxpower_v1.warehouse.persistence import LOCATION

from access_probe import synthetic_user
from batch_probe import _sku_id
from identity import require
from receipt_probe import LIMITS, NO_BATCH, PIECES, STD61, STD62, checked_result, fresh, whole_unit_sku
from sku_probe import PREFIX
from stock_probe import actor_session, expect, session
from warehouse_probe import _company_and_parent

LIMITS = {**LIMITS, PIECES: "50"}


def batch(name):
    return {"mode": "BATCH", "batchNo": name}


def command(company, *lines):
    return SubmitIssue(service.create_draft(IssueDraftInput(company, lines, "Synthetic issue")), uuid4().hex)


def seed_receipts(company, actor, lines, *, rate=Decimal(0), allow_gl=False):
    payload = ReceiptDraftInput(company, tuple(
        ReceiptLineInput(line.sku_id, line.source_location_id, line.batch, line.quantity, line.uom)
        for line in lines
    ))
    with actor_session(actor, company), receipts.synthetic_candidate(
        company, limits=LIMITS, rate=rate, allow_gl=allow_gl,
    ):
        draft = receipts.create_draft(payload)
        before = fresh()
        result = receipts.submit(SubmitReceipt(draft, uuid4().hex))
    return checked_result(result, draft, before, actor, expected_gl=2 if allow_gl else 0)


def reject(code, call):
    before = fresh()
    expect(code, call)
    require(fresh() == before, f"{code} left a partial native, accounting or business write")


def available(company, line):
    state = service.get_availability(company, line.sku_id, line.source_location_id, line.batch)
    require(state.batch == line.batch.get("batchNo") and state.uom == line.uom,
            "availability did not return the domain Batch and UOM")
    return Decimal(state.quantity)


def post(payload, actor, *, expected_gl=0):
    before = fresh()
    result = service.submit(payload)
    checked_result(result, payload.draft, before, actor, expected_gl=expected_gl, purpose=native.PURPOSE)
    return result


def validation(company, line, unauthorized, ungranted, transit, pc):
    draft_input = IssueDraftInput(company, (line,))
    with actor_session(unauthorized, company), service.synthetic_candidate(company, limits=LIMITS):
        reject("denied", lambda: service.create_draft(draft_input))
        reject("denied", lambda: available(company, line))
    candidate_cases = (
        ("denied", replace(line, source_location_id="0" * 32)),
        ("denied", replace(line, source_location_id=ungranted)),
        ("denied", replace(line, source_location_id=transit)),
        ("BATCH_SKU_UNAVAILABLE", replace(line, sku_id="0" * 32)),
        ("BATCH_MAPPING_UNAVAILABLE", replace(line, batch=batch("ABSENT"))),
        ("BATCH_SELECTION_REQUIRED", replace(line, batch=None)),
        ("INVALID_BATCH_SELECTION", replace(line, batch={"mode": "NATIVE"})),
        ("BATCH_TEXT_EMPTY", replace(line, batch=batch(""))),
        ("INVALID_BATCH_TEXT", replace(line, batch=batch(None))),
        ("BATCH_TEXT_RESERVED", replace(line, batch=batch("HP-NB-" + "0" * 32))),
        ("STOCK_QUANTITY_INVALID", replace(line, quantity="NaN")),
        ("STOCK_QUANTITY_INVALID", replace(line, quantity="Infinity")),
        ("STOCK_QUANTITY_INVALID", replace(line, quantity=1.5)),
        ("STOCK_QUANTITY_SIGN", replace(line, quantity="0")),
        ("STOCK_QUANTITY_PRECISION", replace(line, quantity="0.0001")),
        ("STOCK_QUANTITY_LIMIT", replace(line, quantity="1000.001")),
        ("STOCK_UOM_MISMATCH", replace(line, uom=PIECES)),
        ("STOCK_QUANTITY_PRECISION", replace(pc, quantity="0.5")),
    )
    for code, bad in candidate_cases:
        reject(code, lambda: command(company, bad))
    reject("denied", lambda: available(company, replace(line, source_location_id=ungranted)))


def compete(site, company, actor, line, *, repeatable_read=False):
    first, second = (command(company, replace(line, quantity=qty)) for qty in ("7", "6"))
    before = fresh()
    with connection():
        item = frappe.db.get_value(SKU, line.sku_id, "native_item")
        leaf = frappe.db.get_value(LOCATION, line.source_location_id, "native_warehouse")
        batch_name = frappe.db.get_value(
            MAPPING, {"sku": line.sku_id, "original_text": line.batch["batchNo"]}, "native_batch",
        )
    ready = Barrier(2, timeout=30)
    pre_reads, locked_reads = [], []

    def read_snapshot(db):
        connection_id, qty = db.sql(
            """
            SELECT CONNECTION_ID(), CAST(SUM(e.qty) AS CHAR)
            FROM `tabSerial and Batch Entry` e
            WHERE e.item_code=%s AND e.warehouse=%s AND e.batch_no=%s AND e.docstatus=1 AND e.is_cancelled=0
            """, (item, leaf, batch_name),
        )[0]
        return connection_id, Decimal(qty)

    def before_guard(db):
        pre_reads.append(read_snapshot(db))
        ready.wait()

    read_source = native.availability.read_source

    def observe_locked(company, pairs, skus, *, posting_at=None, locked=True):
        require(locked, "stock writer attempted a nonlocking availability read")
        state = read_source(company, pairs, skus, posting_at=posting_at, locked=locked)
        if posting_at is not None:
            _, consistent_qty = read_snapshot(frappe.db)
            locked_reads.append((consistent_qty, state[0][0][item, leaf, batch_name]))
        return state

    def submit(payload):
        with session(site, actor, company), service.synthetic_candidate(company, limits=LIMITS):
            try:
                return service._submit(payload, before_guard=before_guard)
            except StockError as error:
                return error.code

    action = service._action

    def use_isolation(candidate):
        return replace(action(candidate), read_committed=not repeatable_read)

    with (patch.object(service, "_action", use_isolation),
          patch.object(native.availability, "read_source", observe_locked),
          ThreadPoolExecutor(max_workers=2) as pool):
        results = tuple(pool.map(submit, (first, second)))
    outcomes = tuple(value if isinstance(value, str) else "Effective" for value in results)
    require(results.count("INSUFFICIENT_STOCK") == 1,
            f"same-bucket independent issues did not prevent overdraw: {outcomes}")
    winner, = [result for result in results if not isinstance(result, str)]
    selected = first if winner.document_id == first.draft.document_id else second
    rejected = second if selected is first else first
    with connection() as verifier:
        verifier_id = verifier.sql("SELECT CONNECTION_ID()")[0][0]
        loser, = verifier.get_values(store.DOCUMENT, {"name": rejected.draft.document_id}, ("status", "revision"))
        require(loser == ("Draft", 1), "losing draft acquired an effective status")
    require(len({verifier_id, *(identity for identity, _ in pre_reads)}) == 3
            and [qty for _, qty in pre_reads] == [Decimal(10), Decimal(10)],
            "two independent writers did not both read the original 10 units before locking")
    checked_result(winner, selected.draft, before, actor, purpose=native.PURPOSE)
    remaining = Decimal(10) - Decimal(selected.draft.lines[0].quantity)
    require(available(company, line) == remaining, "concurrent issue left the wrong stock")
    require(locked_reads == [(Decimal(10), Decimal(10)), (Decimal(10) if repeatable_read else remaining, remaining)],
            "locked read did not refresh the old view for the losing issue")
    isolation = "REPEATABLE READ" if repeatable_read else "READ COMMITTED"
    print(f"Material Issue concurrency ({isolation}): two pre-lock reads of 10, one winner and one "
          "insufficient-stock result, reconciled independently.", flush=True)


def faults(company, line, actor):
    payload = command(company, replace(line, quantity="0.125"), replace(line, quantity="0.125"))
    reached = []
    detail = type(frappe.get_doc({"doctype": native.DETAIL}))
    bundle = type(frappe.get_doc({"doctype": stock.BUNDLE}))
    detail_insert, bundle_insert, bundle_submit = detail.db_insert, bundle.db_insert, bundle.on_submit
    insert, complete = store.insert, store.complete_request
    verify = native._verify_posting
    detail_count = 0

    def fail(label):
        reached.append(label)
        raise RuntimeError("Synthetic issue fault")

    def second_detail(doc, *args, **kwargs):
        nonlocal detail_count
        detail_insert(doc, *args, **kwargs)
        detail_count += 1
        if detail_count == 2:
            fail("second native detail")

    def create_bundle(doc, *args, **kwargs):
        bundle_insert(doc, *args, **kwargs)
        fail("Bundle creation")

    def submit_bundle(doc):
        bundle_submit(doc)
        fail("Bundle submission")

    def log(doc_type, **fields):
        identity = insert(doc_type, **fields)
        if doc_type == store.LOG:
            fail("operation log")
        return identity

    def request(*args):
        complete(*args)
        fail("request result")

    def early_commit(*args, **kwargs):
        reached.append("early COMMIT")
        try:
            frappe.db.commit()
        except StockError:
            pass
        return verify(*args, **kwargs)

    def before_commit(db):
        db.close()
        raise ConnectionError("Synthetic disconnect before COMMIT")

    def lost_response(db):
        db.sql("COMMIT")
        raise ConnectionError("Synthetic response lost after COMMIT")

    for label, code, injection in (
        ("second native detail", "STOCK_PERSISTENCE_FAILED", patch.object(detail, "db_insert", second_detail)),
        ("Bundle creation", "STOCK_PERSISTENCE_FAILED", patch.object(bundle, "db_insert", create_bundle)),
        ("Bundle submission", "STOCK_PERSISTENCE_FAILED", patch.object(bundle, "on_submit", submit_bundle)),
        ("operation log", "STOCK_PERSISTENCE_FAILED", patch.object(store, "insert", log)),
        ("request result", "STOCK_PERSISTENCE_FAILED", patch.object(store, "complete_request", request)),
        ("early COMMIT", "STOCK_ADAPTER_SIDE_EFFECT", patch.object(native, "_verify_posting", early_commit)),
    ):
        with injection:
            reject(code, lambda: service.submit(payload))
        require(reached == [label], f"fault injection did not reach {label}")
        reached.clear()
        print(f"Material Issue rolled back after {label}.", flush=True)

    reject("STOCK_RETRY_REQUIRED", lambda: service._submit(payload, commit=before_commit))
    before = fresh()
    result = service._submit(payload, commit=lost_response)
    checked_result(result, payload.draft, before, actor, purpose=native.PURPOSE)
    committed = fresh()
    require(service.submit(payload) == result and fresh() == committed,
            "successful COMMIT replay changed the issue result or wrote a second posting")
    reject("STOCK_IDEMPOTENCY_CONFLICT", lambda: service.submit(replace(
        payload, draft=replace(payload.draft, reason="Changed"),
    )))
    reject("STOCK_DOCUMENT_ALREADY_EFFECTIVE", lambda: service.submit(replace(payload, idempotency_key=uuid4().hex)))
    print("Material Issue COMMIT recovery: disconnect retried with the same key, committed response replayed.", flush=True)


def run(site):
    company, _ = _company_and_parent()
    if not frappe.db.exists("Stock Entry Type", native.PURPOSE):
        frappe.get_doc(doctype="Stock Entry Type", purpose=native.PURPOSE, is_standard=1).insert(
            set_name=native.PURPOSE,
        )
    warehouses = warehouse.list_warehouses(company=company)
    code = "I" + uuid4().hex[:10].upper()
    source, alternate = (
        warehouse.create_location(row.id, code=code, display_name="Synthetic Issue " + code).id
        for row in warehouses[:2]
    )
    hidden = warehouse.list_locations(warehouses[2].id)[-1].id
    transit = frappe.db.get_value(LOCATION, {"warehouse": frappe.db.get_value(
        "HP V1 Warehouse", {"company": company, "code": "TRANSIT"},
    )})
    actor, _ = synthetic_user(uuid4().hex[:10], "issue", Operation.ISSUE, Operation.RECEIVE)
    unauthorized, _ = synthetic_user(uuid4().hex[:10], "receipt", Operation.RECEIVE)
    set_scope(actor, company, Mode.SELECTED, [record.id for record in warehouses[:2]])
    set_scope(unauthorized, company, Mode.ALL)
    frappe.db.commit()

    first, second = _sku_id("A"), _sku_id("B")
    pc = IssueLineInput(whole_unit_sku(first), source, NO_BATCH, "2", PIECES)
    target = IssueLineInput(first, source, STD61, "5", PREFIX)
    other = replace(target, batch=STD62, quantity="20")
    no_batch = replace(target, batch=NO_BATCH, quantity="10")
    brand = replace(target, sku_id=second, quantity="4")
    other_leaf = replace(target, source_location_id=alternate, quantity="3")
    precise = replace(target, batch=batch("DECIMAL-" + uuid4().hex), quantity="1.125")
    race = replace(target, batch=batch("RACE-" + uuid4().hex), quantity="10")
    repeatable_race = replace(race, batch=batch("OLD-VIEW-" + uuid4().hex))
    seed_receipts(company, actor, (target, other, no_batch, brand, other_leaf, pc, precise, race, repeatable_race))

    with actor_session(actor, company):
        payload = IssueDraftInput(company, (target,))
        reject("ISSUE_NOT_ENABLED", lambda: service.create_draft(payload))
        reject("ISSUE_NOT_ENABLED", lambda: available(company, target))
        with service.synthetic_candidate(company, limits=LIMITS):
            validation(company, target, unauthorized, hidden, transit, pc)
            reject("BATCH_MAPPING_UNAVAILABLE", lambda: command(company, replace(precise, sku_id=second)))
            initial = available(company, target)
            require(initial == 5, "STD61 sample did not start with exactly five physical units")
            insufficient = replace(target, quantity="6")
            rejected = command(company, insufficient)
            reject("INSUFFICIENT_STOCK", lambda: service.submit(rejected))
            same_bucket = command(company, replace(target, quantity="3"), replace(target, quantity="3"))
            reject("INSUFFICIENT_STOCK", lambda: service.submit(same_bucket))
            mixed_failure = command(company, replace(other, quantity="1"), insufficient)
            reject("INSUFFICIENT_STOCK", lambda: service.submit(mixed_failure))

            post(command(company, replace(target, quantity="1"), replace(target, quantity="2")), actor)
            require(available(company, target) == 2, "same-bucket lines did not deduct three units")
            post(command(company, replace(target, quantity="2")), actor)
            require(available(company, target) == 0, "exact depletion did not reach zero")
            empty = command(company, replace(target, quantity="1"))
            reject("INSUFFICIENT_STOCK", lambda: service.submit(empty))
            post(command(company, replace(no_batch, quantity="1")), actor)
            post(command(company, replace(other, quantity="2"), replace(brand, quantity="1"),
                         replace(other_leaf, quantity="1"), pc), actor)
            fractional = command(company, replace(precise, quantity="0.125"))
            post(fractional, actor)
            require(available(company, precise) == Decimal("1.000"), "1.125 - 0.125 failed exact reconciliation")
            faults(company, precise, actor)
            compete(site, company, actor, race)
            compete(site, company, actor, repeatable_race, repeatable_read=True)
            check_precision_and_integrity(company, precise)
            permission_change = command(company, replace(other, quantity="1"), replace(other_leaf, quantity="1"))

    set_scope(actor, company, Mode.SELECTED, [warehouses[0].id])
    frappe.db.commit()
    with actor_session(actor, company), service.synthetic_candidate(company, limits=LIMITS):
        reject("denied", lambda: service.submit(permission_change))
    accounting_candidate(precise)


    print("Material Issue: individual STD61, NO_BATCH, separate brand and warehouse, distinct same-bucket "
          "rows, integer and exact three-place quantities, zero and overdraw checks.", flush=True)



def check_precision_and_integrity(company, line):
    payload = command(company, replace(line, quantity="0.125"))
    with connection():
        raw_precision = frappe.db.get_default("float_precision")
    frappe.db.set_default("float_precision", "2")
    frappe.db.commit()
    try:
        reject("ISSUE_NATIVE_PRECISION_UNCONFIGURED", lambda: service.submit(payload))
    finally:
        frappe.db.set_default("float_precision", raw_precision)
        frappe.db.commit()

    def negative_setting():
        return frappe.db.sql(
            "SELECT value FROM `tabSingles` WHERE doctype='Stock Settings' AND field='allow_negative_stock_for_batch'",
        )

    previous = negative_setting()
    frappe.db.set_single_value("Stock Settings", "allow_negative_stock_for_batch", 1, update_modified=False)
    frappe.db.commit()
    try:
        reject("ISSUE_NATIVE_NEGATIVE_STOCK_ENABLED", lambda: service.submit(payload))
    finally:
        if previous:
            frappe.db.set_single_value(
                "Stock Settings", "allow_negative_stock_for_batch", previous[0][0], update_modified=False,
            )
        else:
            frappe.db.delete("Singles", {"doctype": "Stock Settings", "field": "allow_negative_stock_for_batch"})
        frappe.db.commit()
    require(negative_setting() == previous, "issue test changed the native negative stock setting")

    balances = stock._balances

    def inconsistent_read(company, pairs, *, error, posting_at=None, scales=None, locked=True):
        db = frappe.db
        sql = db.sql

        def corrupt_query(query, *args, **kwargs):
            rows = sql(query, *args, **kwargs)
            if not rows:
                return rows
            if mismatch == "Bin" and "SELECT CAST(actual_qty AS CHAR) FROM `tabBin`" in str(query):
                return (("123456",),)
            if "LEFT JOIN `tabSerial and Batch Bundle`" in str(query):
                first = list(rows[0])
                if mismatch == "missing Bundle":
                    first[4] = None
                if mismatch == "quantity precision":
                    first[1] = "0.0001"
                return (tuple(first), *rows[1:])
            return rows

        with patch.object(db, "sql", corrupt_query):
            return balances(company, pairs, error=error, posting_at=posting_at, scales=scales, locked=locked)

    for mismatch in ("Bin", "missing Bundle", "quantity precision"):
        with patch.object(stock, "_balances", inconsistent_read):
            reject("ISSUE_NATIVE_RECONCILIATION_FAILED", lambda: service.submit(payload))
    with patch.object(stock, "_batch_totals", side_effect=lambda names, **_options: dict.fromkeys(names, Decimal("123456"))):
        reject("ISSUE_NATIVE_RECONCILIATION_FAILED", lambda: service.submit(payload))
    print("Material Issue rejected inconsistent Bundle, Bin, Batch and precision reads without writes.", flush=True)


def accounting_candidate(sample):
    from erpnext.accounts.doctype.accounting_period import accounting_period
    from erpnext.stock import stock_ledger
    from erpnext.stock.doctype.stock_ledger_entry import stock_ledger_entry
    from frappe.utils import now_datetime
    from haxpower_v1.warehouse.test_warehouse import make_company

    marker = uuid4().hex[:5].upper()
    company, parent = make_company(f"I{marker}")
    master = frappe.get_doc("Company", company)
    master.enable_perpetual_inventory = 1
    master.default_inventory_account = frappe.db.get_value(
        "Account", {"company": company, "account_type": "Stock", "is_group": 0},
    )
    master.stock_adjustment_account = frappe.db.get_value(
        "Account", {"company": company, "account_type": "Stock Adjustment", "is_group": 0},
    )
    master.save(ignore_permissions=True)
    logical = warehouse.create_warehouse(company=company, native_parent=parent, code="W1", display_name="Synthetic GL")
    leaf = warehouse.create_location(logical.id, code="A1", display_name="Synthetic GL A1")
    actor, _ = synthetic_user(marker.lower(), "issue-gl", Operation.ISSUE, Operation.RECEIVE)
    set_scope(actor, company, Mode.SELECTED, [logical.id])
    frappe.db.commit()

    line = replace(sample, source_location_id=leaf.id, quantity="2")
    seed_receipts(company, actor, (line,), rate=Decimal("4.25"), allow_gl=True)
    with actor_session(actor, company), service.synthetic_candidate(company, limits=LIMITS):
        payload = command(company, replace(line, quantity="1"))
        reject("ISSUE_GL_NOT_ALLOWED", lambda: service.submit(payload))

    verify = native._verify_posting
    observed = []

    def revalue(scope, *args, **kwargs):
        fields = ("name", "item_code", "warehouse", "voucher_type", "voucher_no", "actual_qty",
                  "qty_after_transaction", "serial_and_batch_bundle", "posting_date", "posting_time", "creation")
        sle, = frappe.db.get_values(stock.SLE, {"voucher_type": native.ENTRY, "voucher_no": scope.entry.name},
                                    fields, as_dict=True, for_update=True)
        pair = sle.item_code, sle.warehouse
        before = stock._balances(company, {pair})
        totals = stock._batch_totals({leg.batch for leg in scope.legs})
        stock_ledger.update_entries_after({
            "item_code": sle.item_code, "warehouse": sle.warehouse, "posting_date": sle.posting_date,
            "posting_time": sle.posting_time, "voucher_type": sle.voucher_type, "voucher_no": sle.voucher_no,
            "sle_id": sle.name, "creation": sle.creation, "reserved_stock": 0,
        }, allow_negative_stock=False)
        require(frappe.db.get_value(stock.SLE, sle.name, fields, as_dict=True, for_update=True) == sle
                and stock._balances(company, {pair}) == before and stock._batch_totals(totals) == totals,
                "valuation replay changed outbound quantity facts")
        observed.append("revaluation")
        return verify(scope, *args, **kwargs)

    with actor_session(actor, company), service.synthetic_candidate(company, limits=LIMITS, allow_gl=True):
        before = fresh()
        with patch.object(native, "_verify_posting", revalue):
            result = service.submit(payload)
        posted = checked_result(result, payload.draft, before, actor, expected_gl=2, purpose=native.PURPOSE)
    ledger, = posted[stock.SLE]
    part, = posted[stock.BUNDLE_ROW]
    detail, = posted[stock.DETAIL]
    debit, = [row for row in posted["GL Entry"] if row.debit]
    credit, = [row for row in posted["GL Entry"] if row.credit]
    # The locked batch path gets outbound cost from the Bundle. The SLE
    # carries the stock value delta and leaves its own outgoing_rate at zero.
    require(observed == ["revaluation"]
            and Decimal(str(part.incoming_rate)) == Decimal(str(detail.valuation_rate)) == Decimal("4.25")
            and Decimal(str(part.stock_value_difference)) == Decimal(str(ledger.stock_value_difference))
            == Decimal("-4.25")
            and debit.account == master.stock_adjustment_account and credit.account == master.default_inventory_account
            and Decimal(str(debit.debit)) == Decimal(str(credit.credit)) == Decimal("4.25"),
            "Material Issue did not post the expected native valuation and GL")

    today = now_datetime().date()
    period = frappe.get_doc(
        doctype="Accounting Period", period_name=f"Synthetic Issue Close {marker}", company=company,
        start_date=today, end_date=today, closed_documents=[{"document_type": native.ENTRY, "closed": 1}],
    ).insert()
    frappe.db.commit()
    original_close = accounting_period.validate_accounting_period_on_doc_save

    def confirm_closed(doc, method=None):
        try:
            return original_close(doc, method)
        except accounting_period.ClosedAccountingPeriod:
            observed.append("closed")
            raise

    with actor_session(actor, company), service.synthetic_candidate(company, limits=LIMITS, allow_gl=True):
        blocked = command(company, replace(line, quantity="1"))
        with patch.object(accounting_period, "validate_accounting_period_on_doc_save", confirm_closed):
            reject("STOCK_PERSISTENCE_FAILED", lambda: service.submit(blocked))
    frappe.delete_doc(period.doctype, period.name)

    def frozen():
        return frappe.db.sql(
            "SELECT value FROM `tabSingles` WHERE doctype=%s AND field=%s", ("Stock Settings", "stock_frozen_upto"),
        )

    previous = frozen()
    frappe.db.set_single_value("Stock Settings", "stock_frozen_upto", today.isoformat(), update_modified=False)
    frappe.clear_document_cache("Stock Settings", "Stock Settings")
    frappe.db.commit()
    original_freeze = stock_ledger_entry.check_stock_frozen_date

    def confirm_frozen(posting_date):
        try:
            return original_freeze(posting_date)
        except stock_ledger_entry.StockFreezeError:
            observed.append("frozen")
            raise

    try:
        with actor_session(actor, company), service.synthetic_candidate(company, limits=LIMITS, allow_gl=True):
            with patch.object(stock_ledger_entry, "check_stock_frozen_date", confirm_frozen):
                reject("STOCK_PERSISTENCE_FAILED", lambda: service.submit(blocked))
    finally:
        if previous:
            frappe.db.set_single_value("Stock Settings", "stock_frozen_upto", previous[0][0], update_modified=False)
        else:
            frappe.db.delete("Singles", {"doctype": "Stock Settings", "field": "stock_frozen_upto"})
        frappe.clear_document_cache("Stock Settings", "Stock Settings")
        frappe.db.commit()
    require(frozen() == previous and observed == ["revaluation", "closed", "frozen"],
            "outbound revaluation, accounting closure or native freeze was not exercised")
    print("Material Issue valuation: one unit at 4.25, stock value decreased 4.25 and GL debits/credits were "
          "each 4.25. GL rejection, closed period and stock freeze rolled back; revaluation preserved quantities.",
          flush=True)

def verify_receipts():
    from collections import defaultdict

    from haxpower_v1.inventory.availability import read_source
    from haxpower_v1.inventory.guard import lock_stock_writes

    with connection():
        lock_stock_writes()
        sources = defaultdict(set)
        rows = frappe.db.sql(
            """
            SELECT d.company, l.sku_id, l.target
            FROM `tabHP V1 Stock Document` d
            JOIN `tabHP V1 Stock Document Line` l ON l.document_id = d.name
            WHERE d.document_type = 'Material Receipt' AND d.status = 'Effective'
            FOR UPDATE
            """,
        )
        require(rows, "upgrade fixture must contain real V1-08 receipts")
        for company, sku_id, location_id in rows:
            sources[company].add((sku_id, location_id))
        for company, locations in sorted(sources.items()):
            skus = {sku_id: frappe.get_doc(SKU, sku_id, for_update=True) for sku_id, _ in sorted(locations)}
            pairs = {(skus[sku_id].native_item, frappe.db.get_value(
                LOCATION, location, "native_warehouse", for_update=True,
            )) for sku_id, location in locations}
            read_source(company, pairs, skus.values())
    print(f"Historical receipts re-reconciled across {sum(map(len, sources.values()))} SKU/location pairs.", flush=True)
