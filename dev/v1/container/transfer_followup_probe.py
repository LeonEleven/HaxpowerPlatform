"""Native End Transit and return acceptance on a disposable MariaDB site."""

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields, replace
from decimal import Decimal
from threading import Barrier, Event, current_thread
from unittest.mock import patch
from uuid import uuid4

import frappe

from haxpower_v1.access.config import set_scope
from haxpower_v1.access.policy import Mode, Operation
from haxpower_v1.adapters.erpnext import stock_entry as stock
from haxpower_v1.inventory.operations import persistence as store
from haxpower_v1.inventory.operations.dto import StockError
from haxpower_v1.inventory.operations.transaction import connection
from haxpower_v1.inventory.transfer import dto, service, transit
from haxpower_v1.warehouse import service as warehouse
from haxpower_v1.warehouse.persistence import LOCATION

from access_probe import synthetic_user
from batch_probe import _sku_id
from identity import require
from issue_probe import seed_receipts
from receipt_probe import LIMITS, NO_BATCH
from stock_probe import actor_session, session
from transfer_probe import UPGRADE_MARKER, command, followup_fixture, fresh, native_denials, post, reject

def payload(dispatch, *quantities, action=stock.RECEIVE, revision=None):
    cls = dto.ReceiveTransfer if action == stock.RECEIVE else dto.ReturnTransfer
    return cls(
        dispatch.company, dispatch.transfer_id, revision if revision is not None else dispatch.revision, uuid4().hex,
        tuple(dto.TransferQuantity(dispatch.lines[index].transfer_line_id, amount) for index, amount in quantities),
        "Synthetic warehouse confirmation", "Synthetic shift note",
    )


def _row(state, table, name):
    row, = [row for row in state[table] if row.name == name]
    return row


def _by_bucket(state):
    batches = {part.parent: part.batch_no for part in state[stock.BUNDLE_ROW]}
    buckets = defaultdict(Decimal)
    for sle in state[stock.SLE]:
        if sle.voucher_type == stock.ENTRY and sle.docstatus == 1 and not sle.is_cancelled:
            buckets[sle.item_code, sle.warehouse, batches[sle.serial_and_batch_bundle]] += Decimal(str(sle.actual_qty))
    return buckets


def _check_once(command, result, old, before, action):
    after = fresh()
    current = service.get_dispatch(old.company, old.transfer_id)
    require((result.revision, result.status, current.operation_id)
            == (old.revision + 1, current.status, old.operation_id)
            and current.revision == result.revision
            and after[transit.OWNERSHIP] == before[transit.OWNERSHIP]
            and after[store.LINE] == before[store.LINE], "settlement changed original identity")
    log = _row(after, store.LOG, result.operation_id)
    req = _row(after, store.REQUEST, log.request_id)
    require((log.actor, log.original_operation, log.reason, log.notes, log.result_revision, log.result_status)
            == (frappe.session.user, old.operation_id, command.reason, command.notes, result.revision, result.status)
            and (req.result_operation, req.result_revision, req.status, log.effect_key)
            == (log.name, result.revision, "Completed", req.name), "request/log identity mismatch")
    links = {row.line_id: row for row in after[store.LINK] if row.operation_id == log.name}
    selected = {row.transfer_line_id: Decimal(row.quantity) for row in command.lines}
    added = {row.name for row in after[stock.ENTRY]} - {row.name for row in before[stock.ENTRY]}
    require(links.keys() == selected.keys() and {link.parent_id for link in links.values()} == added
            and after["GL Entry"] == before["GL Entry"], "wrong native/GL cardinality")

    old_buckets, new_buckets = _by_bucket(before), _by_bucket(after)
    conserved = defaultdict(Decimal)
    amounts = ("dispatched_quantity", "received_quantity", "returned_quantity", "remaining_quantity")
    for original, updated in zip(old.lines, current.lines, strict=True):
        stable = [f.name for f in fields(updated) if f.name not in amounts]
        require(all(getattr(original, f) == getattr(updated, f) for f in stable), "original line was rewritten")
        sent, received, returned, remaining = (Decimal(getattr(updated, field)) for field in amounts)
        qty = selected.get(original.transfer_line_id, Decimal(0))
        require(sent == received + returned + remaining and min(received, returned, remaining) >= 0
                and sent == Decimal(original.dispatched_quantity)
                and received == Decimal(original.received_quantity) + (qty if action == stock.RECEIVE else 0)
                and returned == Decimal(original.returned_quantity) + (qty if action == stock.RETURN else 0),
                "original-line quantity conservation failed")
        sent_before = _row(before, stock.DETAIL, original.native_detail)
        sent_after = _row(after, stock.DETAIL, original.native_detail)
        require(Decimal(str(sent_after.transferred_qty)) == received
                and {k: v for k, v in sent_after.items() if k != "transferred_qty"}
                == {k: v for k, v in sent_before.items() if k != "transferred_qty"},
                "only native received cumulative may change")
        if not qty:
            continue
        link = links[original.transfer_line_id]
        detail = _row(after, stock.DETAIL, link.detail_id)
        entry = _row(after, stock.ENTRY, link.parent_id)
        destination = (original.target_location_id if action == stock.RECEIVE else original.source_location_id)
        with connection():
            leaf = frappe.db.get_value(LOCATION, destination, "native_warehouse")
        outgoing = original.native_entry if action == stock.RECEIVE else None
        detail_ref = original.native_detail if outgoing else None
        require((link.role, link.ownership_id) == (action, original.ownership_id)
                and (entry.add_to_transit, entry.outgoing_stock_entry or None) == (0, outgoing)
                and (detail.ste_detail or None, detail.against_stock_entry or None) == (detail_ref, outgoing)
                and (detail.item_code, detail.batch_no, detail.s_warehouse, detail.t_warehouse)
                == (sent_after.item_code, sent_after.batch_no, sent_after.t_warehouse, leaf),
                "native reference, destination or batch drift")
        legs = {row.warehouse: row for row in after[stock.SLE] if row.voucher_detail_no == detail.name}
        require(set(legs) == {detail.s_warehouse, detail.t_warehouse}, "missing native legs")
        value_delta = Decimal(0)
        for leaf_id, effect in ((detail.s_warehouse, -qty), (detail.t_warehouse, qty)):
            ledger = legs[leaf_id]
            require(ledger.docstatus == 1 and not ledger.is_cancelled, "ineffective ledger leg")
            bundle = _row(after, stock.BUNDLE, ledger.serial_and_batch_bundle)
            part, = [row for row in after[stock.BUNDLE_ROW] if row.parent == bundle.name]
            require((Decimal(str(ledger.actual_qty)), part.batch_no, Decimal(str(part.qty)),
                     bundle.type_of_transaction, bundle.docstatus)
                    == (effect, detail.batch_no, effect, "Outward" if effect < 0 else "Inward", 1),
                    "native Bundle quantity or direction mismatch")
            value_delta += Decimal(str(ledger.stock_value_difference))
            conserved[detail.item_code, leaf_id, detail.batch_no] += effect
        require(value_delta.quantize(Decimal("0.01")) == 0, "native inventory value not conserved")
    require(all(new_buckets[key] - old_buckets[key] == conserved[key]
                for key in set(new_buckets) | set(old_buckets) | set(conserved)),
            "settlement altered an unselected item, location or batch")
    for parent_id in {line.native_entry for line in current.lines}:
        header = _row(after, stock.ENTRY, parent_id)
        native = [row for row in after[stock.DETAIL] if row.parent == parent_id]
        percentage = stock.transfer_percent(sum(Decimal(str(row.transfer_qty)) for row in native),
                                            sum(Decimal(str(row.transferred_qty)) for row in native))
        require(stock.percent_matches(header.per_transferred, percentage), "ERPNext percentage mismatch")
    return current


def act(old, *quantities, action=stock.RECEIVE, commit=None, prepared=None):
    command = prepared or payload(old, *quantities, action=action)
    before = fresh()
    result = service._followup(command, action, commit=commit)
    return _check_once(command, result, old, before, action), result, command


def _assert_replay(call, result):
    before = fresh()
    require(call() == result and fresh() == before, "historical replay returned a different result or wrote data")


def flow(company, sample, mixed, actor, scopes):
    first = command(company, sample)
    initial = post(first)
    original = service.dispatch(first)
    peer = post(command(company, replace(sample, quantity="20")))
    current, received, request = act(initial, (0, "6"))
    require(Decimal(service.get_dispatch(company, peer.transfer_id).lines[0].remaining_quantity) == 20,
            "receiving T1 consumed part of peer T2")
    reject("TRANSFER_LINE_REMAINING_INSUFFICIENT", lambda: service.receive(payload(current, (0, "5"))))
    reject("TRANSFER_LINE_REMAINING_INSUFFICIENT", lambda: service.return_remaining(
        payload(current, (0, "5"), action=stock.RETURN)))
    foreign = dto.TransferQuantity(peer.lines[0].transfer_line_id, "1")
    reject("TRANSFER_ORIGINAL_LINE_INVALID", lambda: service.receive(replace(payload(current, (0, "1")), lines=(foreign,))))
    for code, quantities in (
        ("STOCK_LINES_REQUIRED", ()), ("STOCK_DUPLICATE_OR_INVALID_LINE", ((0, "1"), (0, "1"))),
        ("STOCK_QUANTITY_INVALID", ((0, "NaN"),)), ("STOCK_QUANTITY_INVALID", ((0, 0.25),)),
        ("STOCK_QUANTITY_SIGN", ((0, "-1"),)), ("STOCK_QUANTITY_SIGN", ((0, "0"),)),
        ("STOCK_QUANTITY_PRECISION", ((0, "0.0001"),)), ("STOCK_QUANTITY_LIMIT", ((0, "1000.001"),)),
    ):
        reject(code, lambda: service.receive(payload(current, *quantities)))
    reject("STOCK_INVALID_ID", lambda: service.receive(replace(
        payload(current, (0, "1")), lines=(dto.TransferQuantity(current.lines[0].native_detail, "1"),))))
    reject("STOCK_INVALID_TEXT", lambda: service.return_remaining(replace(
        payload(current, (0, "1"), action=stock.RETURN), reason=" ")))
    closed, returned, return_request = act(current, (0, "4"), action=stock.RETURN)
    require(closed.status == transit.CLOSED_RETURN, "receive 6 / return 4 did not close with return")
    for verb, cmd, result in ((service.receive, request, received), (service.return_remaining, return_request, returned)):
        _assert_replay(lambda: verb(cmd), result)
        reject("STOCK_IDEMPOTENCY_CONFLICT", lambda: verb(replace(cmd, notes="Changed note")))
    _assert_replay(lambda: service.dispatch(first), original)
    reject("TRANSFER_ALREADY_CLOSED", lambda: service.receive(payload(closed, (0, "1"))))
    reject("TRANSFER_ALREADY_CLOSED", lambda: service.return_remaining(payload(closed, (0, "1"), action=stock.RETURN)))

    for granted in scopes:
        with actor_session("Administrator", company):
            set_scope(actor, company, Mode.SELECTED, [granted])
            frappe.db.commit()
        reject("denied", lambda: service.receive(payload(peer, (0, "1"))))
        reject("denied", lambda: service.return_remaining(payload(peer, (0, "1"), action=stock.RETURN)))
        reject("denied", lambda: service.receive(request))
        reject("denied", lambda: service.return_remaining(return_request))
        reject("denied", lambda: service.dispatch(first))
    with actor_session("Administrator", company):
        set_scope(actor, company, Mode.SELECTED, scopes)
        frappe.db.commit()

    old = post(command(company, *mixed))
    current, result, first_request = act(old, (1, "0.125"), (0, "3"))
    _assert_replay(lambda: service.receive(replace(first_request, lines=first_request.lines[::-1])), result)
    current, _, _ = act(current, (0, "2"), (2, "2.125"))
    current, _, _ = act(current, (0, "5"), (1, "2"))
    require(current.status == transit.COMPLETED, "3 + 2 + 5 and mixed NO_BATCH/SKU lines were not closed")
    _assert_replay(lambda: service.receive(first_request), result)
    closed_peer, _, _ = act(peer, (0, "20"))
    require(closed_peer.status == transit.COMPLETED, "single full End Transit did not complete")

    all_return = post(command(company, sample))
    all_return, _, _ = act(all_return, (0, "10"), action=stock.RETURN)
    require(all_return.status == transit.CLOSED_RETURN and Decimal(all_return.lines[0].received_quantity) == 0,
            "full return was recorded as a receipt")
    current = post(command(company, sample))
    current, _, _ = act(current, (0, "6"))
    current, _, _ = act(current, (0, "1.125"), action=stock.RETURN)
    current, _, _ = act(current, (0, "2.875"), action=stock.RETURN)
    require(current.status == transit.CLOSED_RETURN, "multiple fractional returns failed to close")
    native_denials(closed)
    return closed, peer


def race(site, actor, old, actions, *, fits=False, same_key=False):
    barrier = Barrier(2, timeout=25)
    first = payload(old, (0, "2" if fits else "3"), action=actions[0])
    second = first if same_key else payload(old, (0, "2"), action=actions[1])

    def attempt(command, action):
        with session(site, actor, old.company), service.synthetic_candidate(old.company, limits=LIMITS):
            connection_id = None

            def wait(db):
                nonlocal connection_id
                connection_id = db.sql("SELECT CONNECTION_ID()")[0][0]
                require(db.get_value(store.DOCUMENT, old.transfer_id, "revision") == old.revision
                        and db.get_value(stock.DETAIL, old.lines[0].native_detail, "transferred_qty") == 0
                        and Decimal(old.lines[0].remaining_quantity) == 4,
                        "both writers must observe the original four available units before locking")
                barrier.wait()

            try:
                result = service._followup(command, action, before_guard=wait)
                return connection_id, result, command, action
            except StockError as failure:
                return connection_id, failure.code, command, action

    before = fresh()
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = (pool.submit(attempt, first, actions[0]), pool.submit(attempt, second, actions[1]))
        rows = [job.result() for job in jobs]
    successes = [row for row in rows if isinstance(row[1], dto.TransferActionResult)]
    failures = [row for row in rows if isinstance(row[1], str)]
    with connection() as database:
        identity = database.sql("SELECT CONNECTION_ID()")[0][0]
        require(len({identity, *(row[0] for row in rows)}) == 3, "concurrency reused a MariaDB connection")
    if same_key:
        require(len(successes) == 2 and successes[0][1] == successes[1][1], "same-key concurrent replay was not identical")
    else:
        require(len(successes) == 1 and len(failures) == 1 and failures[0][1] == "STOCK_REVISION_CONFLICT",
                f"contending receive/return did not honor current revision: {rows}")
    _, result, request, action = successes[0]
    current = _check_once(request, result, old, before, action)
    if not same_key:
        _, _, request, action = failures[0]
        retry = replace(request, expected_revision=current.revision)
        if fits:
            current, _, _ = act(current, action=action, prepared=retry)
            require(Decimal(current.lines[0].remaining_quantity) == 0, "two sequentially valid actions lost stock")
        else:
            reject("TRANSFER_LINE_REMAINING_INSUFFICIENT", lambda: service._followup(retry, action))


def stale_snapshot(site, actor, old):
    from haxpower_v1.inventory.operations import service as kernel

    reader_ready, writer_finished = Event(), Event()
    selected = payload(old, (0, "3"))
    stale = payload(old, (0, "2"), action=stock.RETURN, revision=old.revision + 1)
    executor = current_thread()
    observations = []

    def isolation(*, read_committed):
        # Only the losing test writer is forced onto a persistent old snapshot.
        return connection(read_committed=read_committed and current_thread() == executor)

    def lose():
        with session(site, actor, old.company), service.synthetic_candidate(old.company, limits=LIMITS):
            def snapshot(database):
                def revision():
                    return database.get_value(store.DOCUMENT, old.transfer_id, "revision")
                observations.append((database.sql("SELECT CONNECTION_ID()")[0][0], revision()))
                reader_ready.set()
                require(writer_finished.wait(30), "writer did not complete while snapshot was held")
                require(revision() == old.revision, "repeatable read snapshot advanced unexpectedly")
            try:
                service._followup(stale, stock.RETURN, before_guard=snapshot)
            except StockError as error:
                return error.code

    def commit(database):
        observations.append((database.sql("SELECT CONNECTION_ID()")[0][0], old.revision + 1))
        database.sql("COMMIT")

    before = fresh()
    with patch.object(kernel, "connection", isolation), ThreadPoolExecutor(max_workers=1) as pool:
        loser = pool.submit(lose)
        require(reader_ready.wait(30), "snapshot reader did not start")
        try:
            result = service._followup(selected, stock.RECEIVE, commit=commit)
        finally:
            writer_finished.set()
        require(loser.result() == "TRANSFER_LINE_REMAINING_INSUFFICIENT",
                "old snapshot bypassed current remaining quantity despite current expected revision")
    current = _check_once(selected, result, old, before, stock.RECEIVE)
    with connection() as database:
        identity = database.sql("SELECT CONNECTION_ID()")[0][0]
        require(len({identity, *(writer for writer, _ in observations)}) == 3
                and Decimal(current.lines[0].remaining_quantity) == 1, "stale snapshot test did not reconcile")


def faults(old):
    entry = type(frappe.get_doc({"doctype": stock.ENTRY}))
    native_detail = type(frappe.get_doc({"doctype": stock.DETAIL}))
    bundle = type(frappe.get_doc({"doctype": stock.BUNDLE}))
    sle = type(frappe.get_doc({"doctype": stock.SLE}))
    command = payload(old, (0, "1"), (1, "1"))
    seen = []

    def after(original, label, when=lambda *_args: True):
        def fail(*args, **kwargs):
            result = original(*args, **kwargs)
            if when(*args):
                seen.append(label)
                raise RuntimeError("Synthetic settlement rollback")
            return result
        return fail

    for obj, method, label, when in (
        (native_detail, "db_insert", "second native Detail", lambda row: row.idx == 2),
        (bundle, "on_submit", "Inward Bundle", lambda doc: doc.type_of_transaction == "Inward"),
        (sle, "on_submit", "Inward SLE", lambda doc: doc.actual_qty > 0),
        (entry, "update_transferred_qty", "native cumulative update", lambda doc: bool(doc.outgoing_stock_entry)),
        (store, "insert", "operation log", lambda table: table == store.LOG),
        (store, "insert", "ownership link", lambda table: table == store.LINK),
        (store, "update_document", "current status and revision", lambda *_args: True),
        (store, "complete_request", "completed request", lambda *_args: True),
    ):
        with patch.object(obj, method, after(getattr(obj, method), label, when)):
            reject("STOCK_PERSISTENCE_FAILED", lambda: service.receive(command))
        require(seen == [label], f"fault injection missed {label}")
        seen.clear()

    def premature_commit(_scope):
        frappe.db.commit()

    with patch.object(stock, "verify_entry", premature_commit):
        reject("STOCK_ADAPTER_SIDE_EFFECT", lambda: service.receive(command))

    original_run_method = entry.run_method

    def wrong(field, value):
        def run_method(doc, method, *args, **kwargs):
            if method == "before_insert" and doc.outgoing_stock_entry:
                doc.items[0].set(field, value)
            return original_run_method(doc, method, *args, **kwargs)
        return run_method

    for field, value in (
        ("t_warehouse", "wrong destination"), ("item_code", "wrong item"), ("batch_no", "wrong batch"),
        ("against_stock_entry", "wrong dispatch"), ("ste_detail", old.lines[1].native_detail),
    ):
        with patch.object(entry, "run_method", wrong(field, value)):
            reject("denied", lambda: service.receive(command))

    def no_commit(db):
        db.close()
        raise ConnectionError("Synthetic disconnect before COMMIT")

    def lost_response(db):
        db.sql("COMMIT")
        raise ConnectionError("Synthetic lost response after COMMIT")

    reject("STOCK_RETRY_REQUIRED", lambda: service._followup(command, stock.RECEIVE, commit=no_commit))
    settled, result, _ = act(old, prepared=command, commit=lost_response)
    _assert_replay(lambda: service.receive(command), result)
    print("Settlement rollback after second Detail, Bundle, SLE, cumulative update, audit/state and request writes passed.", flush=True)
    return settled


def multiple_native_parents(company, line):
    from frappe.utils import now_datetime

    from haxpower_v1.adapters.erpnext import transfer

    post_entry = transfer._post_entry

    def split_dispatch(plan, action, sources, inward, before, batches, _now):
        links = []
        for leg in sources:
            links.extend(post_entry(
                plan, action, (leg,), {leg.line_id: inward[leg.line_id]}, before, batches, now_datetime(),
            ))
        return tuple(links)

    dispatch = command(company, replace(line, quantity="2"), replace(line, quantity="3"))
    with patch.object(transfer, "_post_entry", split_dispatch):
        service.dispatch(dispatch)
    original = service.get_dispatch(company, dispatch.draft.transfer_id)
    require(len({line.native_entry for line in original.lines}) == 2, "fixture did not create two native dispatch parents")
    receive = payload(original, (0, "2"), (1, "3"))
    posted = []

    def fail_after_second(*args, **kwargs):
        links = post_entry(*args, **kwargs)
        posted.extend(link.parent_id for link in links)
        if len(posted) == 2:
            raise RuntimeError("Synthetic failure after the second End Transit")
        return links

    with patch.object(transfer, "_post_entry", fail_after_second):
        reject("STOCK_PERSISTENCE_FAILED", lambda: service.receive(receive))
    require(len(set(posted)) == 2, "receive never reached the second native parent")
    closed, _, _ = act(original, prepared=receive)
    require(closed.status == transit.COMPLETED, "one business action did not close both native parents")
    print("Two End Transit entries committed atomically and rolled back together after second-parent failure.", flush=True)


def drift(old, peer):
    baseline = fresh()
    previous = old.lines[0]
    with connection() as database:
        link, = database.get_values(store.LINK, {"line_id": previous.transfer_line_id, "role": stock.RECEIVE},
                                    ("name", "ownership_id", "detail_id"), as_dict=True, for_update=True)

    def change(doctype, field, name, value):
        with connection() as database:
            database.sql(f"UPDATE `tab{doctype}` SET `{field}` = %s WHERE name = %s", (value, name))
            database.sql("COMMIT")

    with connection() as database:
        bundle = database.get_value(stock.SLE, previous.native_inward_sle, "serial_and_batch_bundle")
    for doctype, field, name, modified, original, code in (
        (store.LINK, "detail_id", link.name, uuid4().hex, link.detail_id, transit.INCONSISTENT),
        (store.LINK, "ownership_id", link.name, peer.lines[0].ownership_id, link.ownership_id, transit.INCONSISTENT),
        (stock.DETAIL, "transferred_qty", previous.native_detail, 0, previous.received_quantity, transit.INCONSISTENT),
        (stock.SLE, "serial_and_batch_bundle", previous.native_inward_sle, None, bundle, "ISSUE_NATIVE_RECONCILIATION_FAILED"),
    ):
        change(doctype, field, name, modified)
        try:
            reject(code, lambda: service.get_dispatch(old.company, old.transfer_id))
            reject(code, lambda: service.receive(payload(old, (0, "0.125"))))
        finally:
            change(doctype, field, name, original)
    require(fresh() == baseline, "drift checks failed to restore their fixture")
    print("Cross-transfer ownership, original cumulative drift and missing Bundle failed closed.", flush=True)


def run(site):
    actor, company, scopes, sample = followup_fixture()
    mixed = (sample, replace(sample, batch=NO_BATCH, quantity="2.125"),
             replace(sample, sku_id=_sku_id("B"), quantity="2.125"))
    seed_receipts(company, actor, (replace(sample, quantity="100"), *mixed[1:]))
    valued = replace(sample, batch={"mode": "BATCH", "batchNo": "VALUED"}, quantity="10")
    seed_receipts(company, actor, (valued,), rate=Decimal("4.25"))

    with actor_session(actor, company), service.synthetic_candidate(company, limits=LIMITS):
        _, peer = flow(company, sample, mixed, actor, scopes)
        for actions, fits, same_key in (
            ((stock.RECEIVE, stock.RECEIVE), False, False),
            ((stock.RECEIVE, stock.RETURN), False, False),
            ((stock.RETURN, stock.RETURN), False, False),
            ((stock.RECEIVE, stock.RETURN), True, False),
            ((stock.RECEIVE, stock.RECEIVE), False, True),
        ):
            initial = post(command(company, replace(sample, quantity="4")))
            race(site, actor, initial, actions, fits=fits, same_key=same_key)
        stale_snapshot(site, actor, post(command(company, replace(sample, quantity="4"))))
        print("Two MariaDB writers and an independent verifier passed receive/receive, receive/return, return/return "
              "and a pinned REPEATABLE READ view with a current expected revision.", flush=True)
        initial = post(command(company, replace(sample, quantity="3"), replace(sample, quantity="3")))
        drift(faults(initial), peer)
        multiple_native_parents(company, sample)
        initial = post(command(company, valued), value=Decimal("4.25"))
        initial, _, _ = act(initial, (0, "6"))
        initial, _, _ = act(initial, (0, "4"), action=stock.RETURN)
        require(initial.status == transit.CLOSED_RETURN, "nonzero valuation flow failed")
    with actor_session(actor, company), service.synthetic_candidate(company, limits=LIMITS):
        open_transfer = post(command(company, replace(sample, quantity="2")))
    period_and_freeze(actor, company, open_transfer)
    gl_candidate(sample)
    print("Native V1-11 lifecycle, history, authorized replay, exact quantities, two-leg valuation and rollback passed.", flush=True)


def _historical(index):
    from haxpower_v1.inventory.operations.service import _lock_document
    from haxpower_v1.inventory.posting import batch_input

    with connection():
        doc = frappe.db.get_value(store.DOCUMENT, {"reason": f"{UPGRADE_MARKER} {index}"},
                                  ("name", "company", "owner", "reason", "notes"), as_dict=True)
        _, lines = _lock_document(doc.company, doc.name, stock.TRANSFER)
    draft = dto.TransferDraft(
        doc.company, doc.name, 1, tuple(
            dto.TransferLine(line.sku_id, line.source, line.target, batch_input(line), line.quantity,
                             line.uom, line.reason, line.notes, line.line_id)
            for line in sorted(lines, key=lambda line: line.line_no)
        ), doc.reason, doc.notes,
    )
    return doc.owner, dto.DispatchTransfer(draft, f"v111-upgrade-dispatch-{index}")


def settle_history(*, recovered=False):
    for index in ((2,) if recovered else (1, 2)):
        actor, command = _historical(index)
        company, document = command.draft.company, command.draft.transfer_id
        with actor_session(actor, company), service.synthetic_candidate(company, limits=LIMITS):
            start = service.get_dispatch(company, document)
            replay = dto.DispatchResult(start.transfer_id, start.operation_id, transit.IN_TRANSIT, 2)
            _assert_replay(lambda: service.dispatch(command), replay)
            if not recovered:
                require(start.status == transit.IN_TRANSIT and start.revision == 2, "upgrade altered historical dispatch")
                start, _, _ = act(start, (0, "6"))
            else:
                require(start.status == transit.PARTIAL and start.revision == 3, "restore lost open settlement history")
            if index == 1 or recovered:
                start, _, _ = act(start, (0, "4"), action=stock.RETURN)
                require(start.status == transit.CLOSED_RETURN, "V1-10 dispatch did not close after restore/upgrade")
            _assert_replay(lambda: service.dispatch(command), replay)
    print("Reconciled V1-10 historic dispatches, genuine partial receipts and returns, and original-request replay.", flush=True)


def period_and_freeze(actor, company, original):
    from erpnext.accounts.doctype.accounting_period import accounting_period
    from erpnext.stock.doctype.stock_ledger_entry import stock_ledger_entry
    from frappe.utils import nowdate

    today = nowdate()
    period = frappe.get_doc(
        doctype="Accounting Period", period_name="Synthetic Transfer Settlement " + uuid4().hex[:8], company=company,
        start_date=today, end_date=today, closed_documents=[{"document_type": stock.ENTRY, "closed": 1}],
    ).insert()
    frappe.db.commit()
    blocked = ((service.receive, payload(original, (0, "1"))),
               (service.return_remaining, payload(original, (0, "1"), action=stock.RETURN)))
    reasons = []

    def observed(call, error):
        def check(*args, **kwargs):
            try:
                return call(*args, **kwargs)
            except error:
                reasons.append(error)
                raise
        return check

    try:
        with (actor_session(actor, company), service.synthetic_candidate(company, limits=LIMITS),
              patch.object(accounting_period, "validate_accounting_period_on_doc_save", observed(
                  accounting_period.validate_accounting_period_on_doc_save, accounting_period.ClosedAccountingPeriod))):
            for operation, command in blocked:
                reject("STOCK_PERSISTENCE_FAILED", lambda: operation(command))
    finally:
        frappe.delete_doc(period.doctype, period.name)
        frappe.db.commit()

    frozen_field = {"doctype": "Stock Settings", "field": "stock_frozen_upto"}
    previous = frappe.db.sql(
        "SELECT value FROM `tabSingles` WHERE doctype = %s AND field = %s", tuple(frozen_field.values()),
    )
    frappe.db.set_single_value("Stock Settings", "stock_frozen_upto", today, update_modified=False)
    frappe.clear_document_cache("Stock Settings", "Stock Settings")
    frappe.db.commit()
    try:
        with (actor_session(actor, company), service.synthetic_candidate(company, limits=LIMITS),
              patch.object(stock_ledger_entry, "check_stock_frozen_date", observed(
                  stock_ledger_entry.check_stock_frozen_date, stock_ledger_entry.StockFreezeError))):
            for operation, command in blocked:
                reject("STOCK_PERSISTENCE_FAILED", lambda: operation(command))
    finally:
        if previous:
            frappe.db.set_single_value("Stock Settings", "stock_frozen_upto", previous[0][0], update_modified=False)
        else:
            frappe.db.delete("Singles", frozen_field)
        frappe.clear_document_cache("Stock Settings", "Stock Settings")
        frappe.db.commit()
    require(reasons == [accounting_period.ClosedAccountingPeriod] * 2 + [stock_ledger_entry.StockFreezeError] * 2,
            "native closed-period or stock-freeze mechanisms were not exercised")


def gl_candidate(sample):
    from erpnext import is_perpetual_inventory_enabled

    from warehouse_probe import make_company

    marker = "L" + uuid4().hex[:5].upper()
    company, parent = make_company(marker)
    master = frappe.get_doc("Company", company)
    master.enable_perpetual_inventory = 0
    master.default_inventory_account = frappe.db.get_value(
        "Account", {"company": company, "account_type": "Stock", "is_group": 0},
    )
    master.stock_adjustment_account = frappe.db.get_value(
        "Account", {"company": company, "account_type": "Stock Adjustment", "is_group": 0},
    )
    master.save(ignore_permissions=True)
    warehouse_ids = [warehouse.create_warehouse(company=company, native_parent=parent, code=code, display_name=code).id
                     for code in ("W1", "W2")]
    physical = []
    for index, identity in enumerate(warehouse_ids, 1):
        account = frappe.get_doc(
            doctype="Account", account_name=f"Synthetic Settlement {index}", account_type="Stock",
            company=company, parent_account=frappe.db.get_value("Account", master.default_inventory_account, "parent_account"),
        ).insert().name
        leaf = frappe.get_doc(
            doctype="Warehouse", warehouse_name=f"Synthetic Settlement {index}", company=company,
            parent_warehouse=parent, account=account,
        ).insert().name
        physical.append(warehouse.create_location(identity, code="A01", display_name="A01", existing_native=leaf).id)
    warehouse.provision_system_transit(company=company, native_parent=parent,
                                       warehouse_code="TRANSIT", location_code="T1", display_name="Transit")
    actor, _ = synthetic_user(marker.lower(), "settlement-gl", Operation.TRANSFER, Operation.RECEIVE)
    set_scope(actor, company, Mode.SELECTED, warehouse_ids)
    frappe.db.commit()
    sample = replace(sample, source_location_id=physical[0], target_location_id=physical[1], quantity="2")
    seed_receipts(company, actor, (sample,), rate=Decimal("4.25"))
    with actor_session(actor, company), service.synthetic_candidate(company, limits=LIMITS):
        original = post(command(company, sample), value=Decimal("4.25"))

    require(not is_perpetual_inventory_enabled(company), "expected a cached pre-change accounting switch")
    frappe.db.set_value("Company", company, "enable_perpetual_inventory", 1)
    frappe.clear_document_cache("Company", company)
    frappe.db.commit()
    require(not is_perpetual_inventory_enabled(company), "fixture did not retain the caller's pre-change cache")
    observed, verify = [], stock.verify_entry

    def inspect_gl(scope):
        links = verify(scope)
        observed.append(tuple(frappe.db.get_values(
            "GL Entry", {"voucher_no": scope.entry.name}, ("debit", "credit"), for_update=True,
        )))
        return links

    with actor_session(actor, company), service.synthetic_candidate(company, limits=LIMITS):
        for action, operation in ((stock.RECEIVE, service.receive), (stock.RETURN, service.return_remaining)):
            with patch.object(stock, "verify_entry", inspect_gl):
                reject(f"{action.upper()}_GL_NOT_ALLOWED", lambda: operation(payload(original, (0, "1"), action=action)))
    require(len(observed) == 2 and all(
        {(Decimal(str(debit)), Decimal(str(credit))) for debit, credit in rows}
        == {(Decimal("4.25"), Decimal(0)), (Decimal(0), Decimal("4.25"))} for rows in observed
    ), "native GL evidence did not reconcile at 4.25 for both directions")
    print("Native period/freeze rejection and both directions of 4.25 GL rollback passed.", flush=True)
