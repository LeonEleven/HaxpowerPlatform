"""B06 acceptance against native ERPNext and independent MariaDB connections."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from decimal import Decimal
from threading import Barrier, Event
from unittest.mock import patch
from uuid import uuid4

import frappe

from haxpower_v1.access.config import set_scope
from haxpower_v1.access.policy import Mode, Operation
from haxpower_v1.approval import EVENT, REQUEST, TABLES, persistence, policy, service
from haxpower_v1.inventory import versions
from haxpower_v1.inventory.guard import lock_stock_writes
from haxpower_v1.inventory.issue import service as issues
from haxpower_v1.inventory.issue.dto import IssueDraftInput, IssueLineInput, SubmitIssue
from haxpower_v1.inventory.operations import persistence as stock
from haxpower_v1.inventory.operations.dto import StockError
from haxpower_v1.inventory.operations.transaction import connection
from haxpower_v1.inventory.receipt import service as receipts
from haxpower_v1.inventory.receipt.dto import ReceiptDraftInput, ReceiptLineInput, SubmitReceipt
from haxpower_v1.inventory.stock_count import service as counts
from haxpower_v1.inventory.stock_count.dto import Scope
from haxpower_v1.inventory.transfer import dto as transfer, service as transfers
from haxpower_v1.warehouse import service as warehouses

from access_probe import synthetic_user
from batch_probe import _sku_id
from count_probe import actual, create, finish
from identity import require
from receipt_probe import LIMITS, NO_BATCH, snapshot
from sku_probe import PREFIX
from stock_probe import actor_session, expect, session
from warehouse_probe import _company_and_parent


def current_state():
    with connection():
        state = snapshot()
        for doctype in (*TABLES, stock.TRANSIT_OWNERSHIP):
            state[doctype] = frappe.get_all(doctype, fields=["*"], order_by="name")
        return state


def quantity_state(state):
    workflow = {*TABLES, stock.DOCUMENT, stock.LINE, versions.TASK, versions.LINE}
    return {table: rows for table, rows in state.items() if table not in workflow}


def no_quantity(before):
    require(quantity_state(current_state()) == quantity_state(before),
            "workflow changed native quantity, Batch, GL, ownership, versions or stock request/log")


def read_state(result, before, operation, native_doctype, fixture, actor=None):
    after = current_state()
    logs = [row for row in after[stock.LOG] if row.name == result.operation_id]
    previous = {row.name for row in before[native_doctype]}
    native = [row for row in after[native_doctype] if row.name not in previous]
    require(len(logs) == 1 and logs[0].action == operation and logs[0].actor == (actor or fixture.approver),
            "missing actual triggering actor in the operation log")
    require(bool(native) and all(row.owner == fixture.operator and row.docstatus == 1 for row in native)
            and logs[0].native_actor == fixture.operator, "native execution identity differs from the operator")
    with connection(read_committed=True):
        lock_stock_writes(fixture.company)
        versions.verify()
    return after


class Fixture:
    def __init__(self):
        self.company, _ = _company_and_parent()
        self.warehouses = [row.id for row in warehouses.list_warehouses(company=self.company)[:2]]
        code = "A" + uuid4().hex[:12].upper()
        self.source, self.target = [warehouses.create_location(wid, code=code, display_name=code).id
                                    for wid in self.warehouses]
        self.sku = _sku_id("A")
        self.brand = _sku_id("B")
        identity = uuid4().hex[:10]
        self.operator = synthetic_user(identity, "approval-stock", Operation.RECEIVE, Operation.ISSUE,
                                       Operation.COUNT, Operation.TRANSFER)[0]
        self.approver = synthetic_user(identity, "approval-only", Operation.APPROVE)[0]
        self.peer = synthetic_user(identity, "approval-peer", Operation.APPROVE)[0]
        self.self_approver = synthetic_user(identity, "approval-self", Operation.APPROVE, Operation.RECEIVE)[0]
        self.manager = synthetic_user(identity, "approval-config", Operation.SYSTEM_MAINTAIN)[0]
        for actor in (self.operator, self.approver, self.peer, self.self_approver):
            set_scope(actor, self.company, Mode.SELECTED, self.warehouses)
        frappe.db.commit()

    @contextmanager
    def operate(self, actor=None):
        with actor_session(actor or self.operator, self.company), self.candidates():
            yield

    @contextmanager
    def candidates(self):
        with ExitStack() as stack:
            for module in (receipts, issues, counts, transfers):
                stack.enter_context(module.synthetic_candidate(self.company, limits=LIMITS))
            yield

    def toggle(self, enabled):
        with actor_session(self.manager, self.company):
            settings = service.get_settings()
            return service.configure(enabled, settings.revision, "Synthetic B06 setting change")

    def receipt_command(self, quantity="10", *, source=None, batch=NO_BATCH, sku=None, lines=1):
        draft = receipts.create_draft(ReceiptDraftInput(self.company, tuple(
            ReceiptLineInput(sku or self.sku, source or self.source, batch, quantity, PREFIX)
            for _ in range(lines)
        ), "Synthetic receipt approval"))
        return SubmitReceipt(draft, uuid4().hex)

    def issue_command(self, quantity="1"):
        draft = issues.create_draft(IssueDraftInput(self.company, (
            IssueLineInput(self.sku, self.source, NO_BATCH, quantity, PREFIX),
        )))
        return SubmitIssue(draft, uuid4().hex)

    def transfer_command(self, quantity="1"):
        draft = transfers.create_draft(transfer.TransferDraftInput(self.company, (
            transfer.TransferLineInput(self.sku, self.source, self.target, NO_BATCH, quantity, PREFIX),
        ), "Synthetic transfer", "Synthetic notes"))
        return transfer.DispatchTransfer(draft, uuid4().hex)

    def count_task(self, difference=0, *, scope=Scope.BUCKET):
        task = create(self.company, self.source, scope, self.sku if scope != Scope.LOCATION else None)
        return actual(task, {line.line_id: str(Decimal(line.snapshot_quantity) + difference) for line in task.lines})

    def decision(self, submitted, *, reason="Synthetic approval evidence"):
        return service.Decision(self.company, submitted.document_id, submitted.submission_id,
                                submitted.revision, uuid4().hex, reason)


def pending(call):
    before = current_state()
    result = call()
    require(result.status == policy.PENDING and result.submission_id and not result.operation_id,
            "submission skipped the approval gate")
    no_quantity(before)
    return result


def failed_approval(decision, code):
    before = current_state()
    expect(code, lambda: service.approve(decision))
    after = current_state()
    require(all(after[table] == rows for table, rows in before.items() if table not in (EVENT, REQUEST)),
            "failed approval changed state beyond the failure event and response")
    added = [row for row in after[EVENT] if row.name not in {old.name for old in before[EVENT]}]
    require(len(added) == 1 and added[0].action == "Approval Failed" and added[0].failure_code == code
            and added[0].resulting_status == policy.PENDING and not added[0].operation_id,
            "failed approval did not preserve the pending submission with a sanitized reason")
    expect(code, lambda: service.approve(decision))
    require(current_state() == after, "failure replay appended another event")


def settings_and_decisions(f):
    with actor_session(f.manager, f.company):
        initial = service.get_settings()
        require(not initial.inventory_approval_enabled, "approval probe began with an enabled site")
    with f.operate():
        expect("denied", lambda: service.configure(True, 1, "Forbidden"))
        receipt_request = f.receipt_command()
        before = current_state()
        direct = receipts.submit(receipt_request)
        read_state(direct, before, policy.RECEIPT, "Stock Entry", f, actor=f.operator)
        require(receipts.submit(receipt_request) == direct, "disabled receipt did not replay")
        before = current_state()
        read_state(issues.submit(f.issue_command()), before, policy.ISSUE, "Stock Entry", f, actor=f.operator)
        task = f.count_task(1)
        before = current_state()
        read_state(finish(task), before, policy.COUNT, "Stock Reconciliation", f, actor=f.operator)
        draft = f.transfer_command()
        expect("STOCK_DOCUMENT_ALREADY_EFFECTIVE", lambda: transfers.dispatch(draft))
        before = current_state()
        ready = transfers.submit(draft)
        require(ready.status == policy.READY and not ready.operation_id, "disabled transfer did not become ready")
        no_quantity(before)
        sent = transfers.dispatch(replace(draft, draft=replace(draft.draft, revision=ready.revision)))
        require(sent.status == "In Transit", "exempt transfer failed to dispatch independently")

    enabled = f.toggle(True)
    with actor_session(f.manager, f.company):
        change = service.configuration_history()[-1]
        require((change.revision, change.previous_value, change.new_value, change.actor)
                == (enabled.revision, 0, 1, f.manager) and change.creation and change.reason,
                "configuration audit row is incomplete")
    with f.operate():
        require(receipts.submit(receipt_request) == direct, "enabling approval changed effective history")
        receipt_request = f.receipt_command("2")
        submitted = pending(lambda: receipts.submit(receipt_request))
        decision = f.decision(submitted)
        expect("denied", lambda: service.approve(decision))
        require(receipts.submit(receipt_request) == submitted, "pending submit replay changed the result")
        expect("STOCK_IDEMPOTENCY_CONFLICT", lambda: receipts.submit(replace(
            receipt_request, draft=replace(receipt_request.draft, reason="Changed request"),
        )))
        f.toggle(False)
        require(receipts.submit(receipt_request) == submitted, "disabling approval released a pending receipt")
        expect("STOCK_DOCUMENT_ALREADY_EFFECTIVE", lambda: receipts.submit(replace(
            receipt_request, draft=replace(receipt_request.draft, revision=submitted.revision), idempotency_key=uuid4().hex,
        )))
        with actor_session(f.approver, f.company):
            expect("denied", lambda: service.configure(False, enabled.revision, "Forbidden"))
            with receipts.synthetic_candidate(f.company, limits=LIMITS):
                expect("denied", lambda: receipts.submit(receipt_request))
            with issues.synthetic_candidate(f.company, limits=LIMITS):
                expect("denied", lambda: f.issue_command())
            with counts.synthetic_candidate(f.company, limits=LIMITS):
                expect("denied", lambda: f.count_task())
            before = current_state()
            posted = service.approve(decision)
            require(frappe.session.user == f.approver, "native principal leaked into current session")
            read_state(posted, before, policy.RECEIPT, "Stock Entry", f)
            require(service.approve(decision) == posted, "successful decision did not replay")
            expect("STOCK_IDEMPOTENCY_CONFLICT", lambda: service.approve(replace(decision, reason="different opinion")))
            history = service.history(f.company, submitted.document_id)
            require(history["submissions"][0]["configuration_revision"] == enabled.revision
                    and history["events"][-1].native_actor == f.operator, "submission policy or actor evidence missing")

        f.toggle(True)
        request = f.receipt_command("3")
        submitted = pending(lambda: receipts.submit(request))
        old = f.decision(submitted)
        with actor_session(f.approver, f.company):
            expect("denied", lambda: service.withdraw(old))
            rejected = service.reject(old)
            require(service.reject(old) == rejected and rejected.status == policy.REJECTED, "reject did not replay")
        old_line = request.draft.lines[0]
        changed = replace(request.draft, revision=rejected.revision)
        foreign = replace(old_line, location_id=f.target)
        expect("STOCK_LINE_ID_CONFLICT", lambda: receipts.revise_draft(replace(changed, lines=(foreign,))))
        changed = receipts.revise_draft(replace(changed, lines=(replace(old_line, quantity="4"),)))
        require(changed.lines[0].line_id == old_line.line_id, "edited quantity lost line identity")
        again = pending(lambda: receipts.submit(SubmitReceipt(changed, uuid4().hex)))
        withdrawn = service.withdraw(f.decision(again))
        require(withdrawn.status == "Draft", "creator cannot withdraw pending inventory")
        f.toggle(False)
        later = receipts.submit(SubmitReceipt(replace(changed, revision=withdrawn.revision), uuid4().hex))
        require(later.status == "Effective" and later.submission_id not in (submitted.submission_id, again.submission_id),
                "resubmission reused old approval or current policy")
        with actor_session(f.approver, f.company):
            expect("APPROVAL_STATE_CONFLICT", lambda: service.approve(old))
            submissions = service.history(f.company, request.draft.document_id)["submissions"]
            require([row["approval_required"] for row in submissions] == [1, 1, 0], "historical setting changed")
            require([row["payload"]["command"]["lines"][0]["quantity"] for row in submissions] == ["3", "4", "4"],
                    "resubmission overwrote its historical payload")
        with actor_session("Guest", f.company):
            expect("denied", lambda: service.history(f.company, request.draft.document_id))

    f.toggle(True)
    with f.operate(f.self_approver):
        submitted = pending(lambda: receipts.submit(f.receipt_command("1")))
        require(service.approve(f.decision(submitted)).status == "Effective", "authorized self-approval failed")
    print("B06 settings, immutable re-submissions, approval-only operator bridge and self approval passed.", flush=True)


def authorization_and_stock(f):
    with f.operate():
        issue = pending(lambda: issues.submit(f.issue_command("1")))
        decision = f.decision(issue)
        with actor_session("Administrator", f.company):
            set_scope(f.operator, f.company, Mode.SELECTED, [f.warehouses[1]])
            frappe.db.commit()
        with actor_session(f.approver, f.company):
            before = current_state()
            expect("denied", lambda: service.approve(decision))
            require(current_state() == before, "operator scope loss emitted an approval failure")
        with actor_session("Administrator", f.company):
            set_scope(f.operator, f.company, Mode.SELECTED, f.warehouses)
            frappe.db.commit()
        with actor_session(f.approver, f.company):
            before = current_state()
            result = service.approve(decision)
            read_state(result, before, policy.ISSUE, "Stock Entry", f)
        with actor_session("Administrator", f.company):
            frappe.db.set_value("User", f.approver, "enabled", 0)
            frappe.db.commit()
        with actor_session(f.approver, f.company):
            expect("denied", lambda: service.approve(decision))
            expect("denied", lambda: service.history(f.company, issue.document_id))
        with actor_session("Administrator", f.company):
            frappe.db.set_value("User", f.approver, "enabled", 1)
            frappe.db.commit()

        available = create(f.company, f.source, sku=f.sku)
        quantity = available.lines[0].snapshot_quantity
        contested = pending(lambda: issues.submit(f.issue_command(quantity)))
        f.toggle(False)
        issues.submit(f.issue_command(quantity))
        f.toggle(True)
        with actor_session(f.approver, f.company):
            failed_approval(f.decision(contested), "INSUFFICIENT_STOCK")
        f.toggle(False)
        receipts.submit(f.receipt_command(quantity))
        f.toggle(True)
        with actor_session(f.approver, f.company):
            require(service.approve(f.decision(contested)).status == "Effective",
                    "restored inventory could not be approved with a new request")

        missing = create(f.company, f.source, sku=f.sku)
        before = current_state()
        expect("COUNT_ACTUAL_INCOMPLETE", lambda: finish(missing))
        require(current_state() == before, "an unconfirmed count escaped validation")
        f.toggle(False)
        receipts.submit(f.receipt_command("10"))
        f.toggle(True)
        task = f.count_task(1)
        count = pending(lambda: finish(task))
        with actor_session(f.approver, f.company):
            before = current_state()
            read_state(service.approve(f.decision(count)), before, policy.COUNT, "Stock Reconciliation", f)

        task = f.count_task()
        unchanged = pending(lambda: finish(task))
        with actor_session(f.approver, f.company):
            before = current_state()
            completed = service.approve(f.decision(unchanged))
            after = current_state()
            require(completed.status == "Completed" and all(after[table] == before[table] for table in (
                "Stock Reconciliation", "Stock Ledger Entry", "Serial and Batch Bundle", "Bin", versions.VERSION,
            )), "no-difference approval fabricated quantity history")

        task = f.count_task(1)
        stale = pending(lambda: finish(task))
        f.toggle(False)
        receipts.submit(f.receipt_command("1"))
        issues.submit(f.issue_command("1"))
        f.toggle(True)
        with actor_session(f.approver, f.company):
            failed_approval(f.decision(stale), "COUNT_SCOPE_CHANGED")
        withdrawn = service.withdraw(f.decision(stale))
        expect("COUNT_SCOPE_CHANGED", lambda: actual(counts.get(f.company, task.task_id)))
        recount = counts.recount(f.company, task.task_id, withdrawn.revision, "Synthetic required recount")
        require(recount.snapshot_id != task.snapshot_id and not any(line.confirmed for line in recount.lines),
                "recount inherited expired snapshot or actuals")
        counted = pending(lambda: finish(actual(recount)))
        with actor_session(f.approver, f.company):
            require(service.approve(f.decision(counted)).status == "Completed", "recount did not complete")

        task = f.count_task()
        unrelated = pending(lambda: finish(task))
        scope_wide = pending(lambda: finish(f.count_task(scope=Scope.LOCATION)))
        f.toggle(False)
        receipts.submit(f.receipt_command("1", sku=f.brand, batch={"mode": "BATCH", "batchNo": uuid4().hex}))
        f.toggle(True)
        with actor_session(f.approver, f.company):
            require(service.approve(f.decision(unrelated)).status == "Completed", "unrelated SKU blocked bucket count")
            failed_approval(f.decision(scope_wide), "COUNT_SCOPE_CHANGED")

        expired_generation = pending(lambda: finish(f.count_task()))
        with actor_session(f.manager, f.company), connection(read_committed=True) as database:
            lock_stock_writes()
            versions._rotate_generation_locked("Synthetic approval epoch change")
            database.sql("COMMIT")
        with actor_session(f.approver, f.company):
            failed_approval(f.decision(expired_generation), "COUNT_GENERATION_CHANGED")
    print("B06 stock, independent native reconciliation, stale versions, new buckets and scope revalidation passed.", flush=True)


def company_boundary(f):
    foreign_company = frappe.db.get_value("Company", {"name": ("!=", f.company)}, "name")
    from haxpower_v1.warehouse.test_warehouse import make_company

    # Give the second company an independent role/scope and business document.
    if not foreign_company or not foreign_company.startswith("Synthetic V1 Warehouse "):
        foreign_company, parent = make_company("A" + uuid4().hex[:3].upper())
    else:
        parent = frappe.db.get_value("Warehouse", {
            "company": foreign_company, "is_group": 1, "parent_warehouse": ("is", "not set"),
        })
    code = "A" + uuid4().hex[:10].upper()
    warehouse = warehouses.create_warehouse(company=foreign_company, native_parent=parent, code=code,
                                             display_name=code)
    location = warehouses.create_location(warehouse.id, code=code, display_name=code)
    foreign = synthetic_user(uuid4().hex[:10], "approval-other-company", Operation.RECEIVE)[0]
    set_scope(foreign, foreign_company, Mode.SELECTED, [warehouse.id])
    frappe.db.commit()
    with actor_session(foreign, foreign_company), receipts.synthetic_candidate(foreign_company, limits=LIMITS):
        draft = receipts.create_draft(ReceiptDraftInput(foreign_company, (
            ReceiptLineInput(f.sku, location.id, NO_BATCH, "1", PREFIX),
        )))
        submitted = pending(lambda: receipts.submit(SubmitReceipt(draft, uuid4().hex)))
        decision = replace(f.decision(submitted), company=foreign_company)
        with actor_session(f.approver, f.company):
            expect("denied", lambda: service.approve(decision))
            expect("denied", lambda: service.history(foreign_company, submitted.document_id))
        f.toggle(False)
        draft = receipts.create_draft(ReceiptDraftInput(foreign_company, (
            ReceiptLineInput(f.sku, location.id, NO_BATCH, "1", PREFIX),
        )))
        require(receipts.submit(SubmitReceipt(draft, uuid4().hex)).status == "Effective",
                "site-wide switch did not apply to the second company's next submission")
    f.toggle(True)
    print("B06 site-wide setting was applied to an independent company, with cross-company approval denied.", flush=True)


def dispatch_lifecycle(f):
    with f.operate():
        draft = f.transfer_command("3")
        submitted = pending(lambda: transfers.submit(draft))
        command = replace(draft, draft=replace(draft.draft, revision=submitted.revision))
        expect("STOCK_DOCUMENT_ALREADY_EFFECTIVE", lambda: transfers.dispatch(command))
        for granted in f.warehouses:
            with actor_session("Administrator", f.company):
                set_scope(f.approver, f.company, Mode.SELECTED, [granted])
                frappe.db.commit()
            with actor_session(f.approver, f.company):
                expect("denied", lambda: service.approve(f.decision(submitted)))
                expect("denied", lambda: service.history(f.company, submitted.document_id))
        with actor_session("Administrator", f.company):
            set_scope(f.approver, f.company, Mode.SELECTED, f.warehouses)
            frappe.db.commit()
        before = current_state()
        with actor_session(f.approver, f.company):
            approved = service.approve(f.decision(submitted))
        require(approved.status == policy.APPROVED, "approved transfer did not wait for dispatch")
        no_quantity(before)
        with actor_session(f.approver, f.company):
            revoked = service.revoke_approval(f.decision(approved))
        require(revoked.status == "Draft", "authorized clear of pre-dispatch approval failed")
        no_quantity(before)
        draft = replace(draft, draft=replace(draft.draft, revision=revoked.revision), idempotency_key=uuid4().hex)
        submitted = pending(lambda: transfers.submit(draft))
        with actor_session(f.approver, f.company):
            approved = service.approve(f.decision(submitted))
        sent = transfers.dispatch(replace(draft, draft=replace(draft.draft, revision=approved.revision)))
        with actor_session(f.approver, f.company):
            expect("APPROVAL_STATE_CONFLICT", lambda: service.revoke_approval(f.decision(approved)))
        f.toggle(False)
        original = draft.draft.lines[0].transfer_line_id
        received = transfers.receive(transfer.ReceiveTransfer(
            f.company, sent.transfer_id, sent.revision, uuid4().hex, (transfer.TransferQuantity(original, "1"),),
        ))
        f.toggle(True)
        back = transfer.ReturnTransfer(f.company, sent.transfer_id, received.revision, uuid4().hex,
                                       (transfer.TransferQuantity(original, "2"),), " ")
        expect("STOCK_INVALID_TEXT", lambda: transfers.return_remaining(back))
        result = transfers.return_remaining(replace(back, reason="Synthetic return of remaining quantity"))
        require(result.status == "Closed with Return", "approval switch blocked legal in-transit settlement")

        overdrawn = f.transfer_command("999")
        submitted = pending(lambda: transfers.submit(overdrawn))
        with actor_session(f.approver, f.company):
            approved = service.approve(f.decision(submitted))
        before = current_state()
        expect("INSUFFICIENT_STOCK", lambda: transfers.dispatch(replace(
            overdrawn, draft=replace(overdrawn.draft, revision=approved.revision),
        )))
        require(current_state() == before, "insufficient dispatch consumed approval or changed quantity")
    print("B06 dual-warehouse approval, pre-dispatch revocation and in-transit receipt/return passed.", flush=True)


def race(site, fixture, contenders):
    barrier = Barrier(2, timeout=60)
    connections = []

    def before_guard(database):
        database.sql("SELECT COUNT(*) FROM `tabStock Ledger Entry`")
        connections.append(database.sql("SELECT CONNECTION_ID()")[0][0])
        barrier.wait()

    def attempt(entry):
        actor, action = entry
        with session(site, fixture.operator, fixture.company), fixture.candidates(), actor_session(actor, fixture.company):
            try:
                return action(before_guard)
            except StockError as error:
                return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(attempt, contenders))
    with connection(read_committed=True) as verifier:
        connections.append(verifier.sql("SELECT CONNECTION_ID()")[0][0])
        lock_stock_writes()
        versions.verify()
    require(len(set(connections)) == 3, "race did not use independent MariaDB connections")
    return results


def races(site, f):
    with f.operate():
        for action, actor in (("Approve", f.peer), ("Reject", f.peer), ("Withdraw", f.operator)):
            submitted = pending(lambda: receipts.submit(f.receipt_command("1")))
            first, second = f.decision(submitted), f.decision(submitted)
            before = current_state()
            results = race(site, f, (
                (f.approver, lambda guard: service._decide(first, "Approve", before_guard=guard)),
                (actor, lambda guard: service._decide(second, action, before_guard=guard)),
            ))
            require(results.count("APPROVAL_STATE_CONFLICT") == 1, f"non-serial {action} race result {results}")
            winner = next(result for result in results if not isinstance(result, str))
            if winner.status == "Effective":
                read_state(winner, before, policy.RECEIPT, "Stock Entry", f,
                           actor=f.approver if winner is results[0] else actor)
            else:
                no_quantity(before)

        a = pending(lambda: issues.submit(f.issue_command("10")))
        b = pending(lambda: issues.submit(f.issue_command("10")))
        # Set quantity to 10 exactly before a pair of debits. Obtain the
        # current balance from the test location's protected count snapshot.
        f.toggle(False)
        current = f.count_task()
        exact = actual(current, {current.lines[0].line_id: "10"})
        finish(exact)
        f.toggle(True)
        first, second = f.decision(a), f.decision(b)
        results = race(site, f, (
            (f.approver, lambda guard: service._decide(first, "Approve", before_guard=guard)),
            (f.peer, lambda guard: service._decide(second, "Approve", before_guard=guard)),
        ))
        require(results.count("INSUFFICIENT_STOCK") == 1 and sum(
            not isinstance(result, str) and result.status == "Effective" for result in results
        ) == 1, "competing approved debits used the same available quantity")

        f.toggle(False)
        receipts.submit(f.receipt_command("3"))
        f.toggle(True)
        draft = f.transfer_command()
        submitted = pending(lambda: transfers.submit(draft))
        with actor_session(f.approver, f.company):
            approved = service.approve(f.decision(submitted))
        dispatch = replace(draft, draft=replace(draft.draft, revision=approved.revision))
        revoke = f.decision(approved)
        results = race(site, f, (
            (f.operator, lambda guard: transfers._dispatch(dispatch, before_guard=guard)),
            (f.approver, lambda guard: service._decide(revoke, "Revoke Approval", before_guard=guard)),
        ))
        require(sum(not isinstance(result, str) for result in results) == 1 and (
            results[0] == "STOCK_DOCUMENT_ALREADY_EFFECTIVE" or results[1] == "APPROVAL_STATE_CONFLICT"
        ), f"dispatch and revocation did not serialize: {results}")

        receipts_request = f.receipt_command("1")
        with actor_session(f.manager, f.company):
            revision = service.get_settings().revision
        submitted, switched = race(site, f, (
            (f.operator, lambda guard: receipts._submit(receipts_request, before_guard=guard)),
            (f.manager, lambda guard: service.configure(False, revision, "Concurrent switch", _before_guard=guard)),
        ))
        history = service.history(f.company, submitted.document_id)
        evidence, = history["submissions"]
        require((submitted.status, evidence["configuration_revision"]) in (
            (policy.PENDING, revision), ("Effective", switched.revision),
        ), "configuration changed between snapshot and posting")
    f.toggle(True)
    print("B06 different-key decision, opposing decision, debit, dispatch/revoke and settings races passed.", flush=True)


def faults(site, f):
    from haxpower_v1.adapters.erpnext import receipt as native

    with f.operate():
        submitted = pending(lambda: receipts.submit(f.receipt_command("0.125", lines=2)))
        with actor_session(f.approver, f.company):
            detail = type(frappe.get_doc({"doctype": native.DETAIL}))
            insert_detail = detail.db_insert

            def second_line(doc, *args, **kwargs):
                result = insert_detail(doc, *args, **kwargs)
                if doc.idx == 2:
                    raise RuntimeError("synthetic second-line failure")
                return result

            write_event = persistence.event

            def event_failure(*args, **kwargs):
                result = write_event(*args, **kwargs)
                if args[1] == "Approve":
                    raise RuntimeError("synthetic success event failure")
                return result

            advance = versions.advance

            def version_failure(*args, **kwargs):
                advance(*args, **kwargs)
                raise RuntimeError("synthetic version failure")

            save_request = persistence.save_request

            def request_failure(*args, **kwargs):
                result = save_request(*args, **kwargs)
                if not kwargs.get("failure_code"):
                    raise RuntimeError("synthetic response failure")
                return result

            for target, name, injected in ((detail, "db_insert", second_line),
                                           (persistence, "event", event_failure),
                                           (versions, "advance", version_failure),
                                           (persistence, "save_request", request_failure)):
                with patch.object(target, name, injected):
                    failed_approval(f.decision(submitted), "APPROVAL_EXECUTION_FAILED")

            before = current_state()
            decision = f.decision(submitted)

            def disconnect(database):
                database.close()
                raise ConnectionError("synthetic disconnect before commit")

            expect("STOCK_RETRY_REQUIRED", lambda: service._decide(decision, "Approve", commit=disconnect))
            require(current_state() == before, "pre-COMMIT disconnect wrote a failure or quantity fact")

            def lose_response(database):
                database.sql("COMMIT")
                raise ConnectionError("synthetic response loss")

            recovered = service._decide(decision, "Approve", commit=lose_response)
            after = read_state(recovered, before, policy.RECEIPT, "Stock Entry", f)
            require(service.approve(decision) == recovered and current_state() == after,
                    "recovered COMMIT was executed twice")
            with connection():
                for table in TABLES:
                    row = frappe.db.get_value(table, {}, "name")
                    if row:
                        expect("denied", lambda: frappe.get_doc(table, row).save(ignore_permissions=True))

        submitted = pending(lambda: receipts.submit(f.receipt_command("1")))
        failed, good = f.decision(submitted), f.decision(submitted)
        rollback_complete, completed = Event(), Event()
        persist = service._persist_failure
        event = persistence.event
        failed_id = service.kernel._digest((f.company, f.approver, "Approve", failed.idempotency_key))

        def fail_event(*args, **kwargs):
            if args[1] == "Approve" and args[5] == failed_id:
                raise RuntimeError("synthetic success-event error on first contender")
            return event(*args, **kwargs)

        def defer_failure(stage, code):
            rollback_complete.set()
            require(completed.wait(60), "approval never completed after earlier rollback")
            return persist(stage, code)

        def old_attempt():
            with session(site, f.operator, f.company), f.candidates(), actor_session(f.approver, f.company):
                expect("APPROVAL_EXECUTION_FAILED", lambda: service.approve(failed))

        with (patch.object(persistence, "event", fail_event), patch.object(service, "_persist_failure", defer_failure),
              ThreadPoolExecutor(max_workers=1) as pool):
            future = pool.submit(old_attempt)
            try:
                require(rollback_complete.wait(60), "approval never rolled back")
                with actor_session(f.peer, f.company):
                    winner = service.approve(good)
            finally:
                completed.set()
            future.result()
        with actor_session(f.peer, f.company):
            history = service.history(f.company, submitted.document_id)
            require(winner.status == history["status"] == "Effective" and all(
                row.action != "Approval Failed" for row in history["events"]
            ), "stale failure overwrote a later approval")
    print("B06 native row, audit, version and response rollback, disconnect recovery and stale failure race passed.", flush=True)


def run(site):
    fixture = Fixture()
    try:
        settings_and_decisions(fixture)
        authorization_and_stock(fixture)
        company_boundary(fixture)
        dispatch_lifecycle(fixture)
        races(site, fixture)
        faults(site, fixture)
    finally:
        fixture.toggle(False)


RECOVERY = "Synthetic V1-13 recovery"


def recovery_prepare():
    f = Fixture()
    with f.operate():
        receipts.submit(f.receipt_command("10"))
        f.toggle(True)
        for state in ("pending", "rejected", "withdrawn", "effective"):
            command = f.receipt_command("1")
            draft = receipts.revise_draft(command.draft, notes=f"{RECOVERY} {state}")
            candidate = receipts.submit(replace(command, draft=draft))
            decision = f.decision(candidate)
            if state == "withdrawn":
                service.withdraw(decision)
            elif state != "pending":
                with actor_session(f.approver, f.company):
                    (service.approve if state == "effective" else service.reject)(decision)
        draft = f.transfer_command()
        revised = transfers.revise_draft(replace(draft.draft, notes=f"{RECOVERY} approved transfer"))
        candidate = transfers.submit(replace(draft, draft=revised))
        with actor_session(f.approver, f.company):
            service.approve(f.decision(candidate))
        from haxpower_v1.inventory.stock_count.dto import CreateCount

        task = counts.create(CreateCount(f.company, f.source, Scope.BUCKET, f.sku, NO_BATCH, f"{RECOVERY} stale"))
        finish(actual(task))
        f.toggle(False)
        receipts.submit(f.receipt_command("1"))
        f.toggle(True)


def recovery_finish():
    def find(marker):
        return frappe.db.get_value(stock.DOCUMENT, {"notes": f"{RECOVERY} {marker}"},
                                   ("name", "company", "owner", "revision", "approval_submission"), as_dict=True)

    pending_receipt = find("pending")
    stale = find("stale")
    company = pending_receipt.company
    grants = [row.id for row in warehouses.list_warehouses(company=company)[:2]]
    approver = synthetic_user(uuid4().hex[:10], "approval-restore", Operation.APPROVE, Operation.SYSTEM_MAINTAIN)[0]
    set_scope(approver, company, Mode.SELECTED, grants)
    frappe.db.commit()
    for document, expected in ((pending_receipt, "Effective"), (stale, "COUNT_SCOPE_CHANGED")):
        with actor_session(document.owner, company), receipts.synthetic_candidate(company, limits=LIMITS), (
            counts.synthetic_candidate(company, limits=LIMITS)
        ), actor_session(approver, company):
            require(service.get_settings().inventory_approval_enabled, "migrate or restore reset approval")
            decision = service.Decision(company, document.name, document.approval_submission, document.revision,
                                        uuid4().hex, "Synthetic recovery decision")
            if expected == "Effective":
                require(service.approve(decision).status == expected, "restored pending receipt did not post")
            else:
                failed_approval(decision, expected)
    approved = find("approved transfer")
    with actor_session(approved.owner, company), transfers.synthetic_candidate(company, limits=LIMITS):
        history = service.history(company, approved.name)
        payload = history["submissions"][-1]["payload"]["command"]
        lines = tuple(transfer.TransferLine(
            row["sku_id"], row["source"], row["target"], NO_BATCH, row["quantity"], row["uom"],
            row["reason"], row["notes"], row["line_id"],
        ) for row in payload["lines"])
        draft = transfer.TransferDraft(company, approved.name, approved.revision, lines, payload["reason"], payload["notes"])
        require(transfers.dispatch(transfer.DispatchTransfer(draft, uuid4().hex)).status == "In Transit",
                "restored approved transfer did not dispatch")
    with actor_session(approver, company):
        settings = service.get_settings()
        service.configure(False, settings.revision, "End synthetic recovery")
    print("Restored approval settings, pending receipt, approved transfer and stale count were revalidated.", flush=True)
