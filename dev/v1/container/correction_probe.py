"""V1-14 native correction, workflow, concurrency and restore probes."""

from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

import frappe

from haxpower_v1.access.config import set_scope
from haxpower_v1.access.policy import Mode, Operation
from haxpower_v1.adapters.erpnext import receipt as native_receipt
from haxpower_v1.approval import persistence as approvals
from haxpower_v1.approval import service as approval
from haxpower_v1.approval.policy import PENDING
from haxpower_v1.inventory import versions
from haxpower_v1.inventory.corrections import TABLES as CORRECTION_TABLES
from haxpower_v1.inventory.corrections import service, facts
from haxpower_v1.inventory.issue import service as issues
from haxpower_v1.inventory.issue.dto import IssueDraftInput, IssueLineInput, SubmitIssue
from haxpower_v1.inventory.operations import persistence as stock, trace
from haxpower_v1.inventory.operations.dto import StockError
from haxpower_v1.inventory.operations.transaction import connection
from haxpower_v1.inventory.receipt import service as receipts
from haxpower_v1.inventory.stock_count.dto import Scope
from haxpower_v1.inventory.transfer import service as transfers
from haxpower_v1.warehouse import service as warehouses

from access_probe import synthetic_user
from approval_probe import Fixture as ApprovalFixture, current_state, no_quantity, race, read_state
from count_probe import actual, create, finish
from identity import require
from receipt_probe import LIMITS
from sku_probe import PREFIX
from haxpower_v1.inventory.receipt.dto import ReceiptDraftInput
from stock_probe import actor_session, expect

MARKER = "Synthetic V1-14 recovery"
BASELINE = "Synthetic V1-14 prior release"


class Fixture(ApprovalFixture):
    def __init__(self):
        super().__init__()
        suffix = uuid4().hex[:10]
        self.specialist = synthetic_user(suffix, "specialist", Operation.CORRECTION_APPROVE)[0]
        self.self_specialist = synthetic_user(suffix, "self-specialist", Operation.CORRECTION_APPROVE,
                                              Operation.RECEIVE)[0]
        for user in (self.specialist, self.self_specialist):
            set_scope(user, self.company, Mode.SELECTED, self.warehouses)
        frappe.db.commit()

    @contextmanager
    def specialist_session(self, actor=None):
        with actor_session(actor or self.specialist, self.company), service.synthetic_candidate(self.company, limits=LIMITS):
            yield

    def submit(self, result, quantities, *, reason="Synthetic B07 request", actor=None, replacement_required=False):
        with actor_session(actor or self.operator, self.company):
            lines = self.original_rows(result.operation_id)
            adjustments = tuple(service.Adjustment(line_id, qty) for line_id, qty in zip(lines, quantities))
            draft = service.save_draft(self.company, result.operation_id, adjustments, reason,
                                       replacement_required=replacement_required)
            command = service.Submission(self.company, draft.document_id, draft.revision, uuid4().hex)
            before = current_state()
            pending = service.submit(command)
            require(pending.status == PENDING and not pending.operation_id, "correction skipped specialist approval")
            no_quantity(before)
            require(service.submit(command) == pending, "correction submission did not replay")
            return pending

    def original_rows(self, operation):
        return [line["line_id"] for line in trace.detail(self.company, operation)["lines"]]

    def check(self, result, status="Effective", *, actor=None, blocked_code=None, applicant=None, decision=None):
        decision = decision or self.decision(result)
        before = current_state()
        with self.specialist_session(actor):
            approved = service.approve(decision)
            require(service.approve(decision) == approved, "correction approval replay changed its response")
            expect("STOCK_IDEMPOTENCY_CONFLICT", lambda: service.approve(replace(decision, reason="Changed decision")))
            if approved.status != status:
                history = service.history(self.company, result.document_id)
                codes = sorted({row["code"] for review in history["reviews"] for row in review["evidence"]})
                require(False, f"expected correction status {status}, observed {approved.status} {codes}")
            expect("APPROVAL_STATE_CONFLICT", lambda: service.approve(replace(decision, idempotency_key=uuid4().hex)))
            if blocked_code:
                history = service.history(self.company, result.document_id)
                require(blocked_code in {reason["code"] for reason in history["reviews"][0]["evidence"]},
                        f"manual review evidence missing {blocked_code}")
        if status == "Effective":
            after = read_state(approved, before, "Correct Effective Inventory", "Stock Entry",
                               SimpleNamespace(operator=applicant or self.operator, company=self.company),
                               actor=actor or self.specialist)
            for table, fields in (
                ("Stock Ledger Entry", ("actual_qty", "serial_and_batch_bundle", "voucher_detail_no", "docstatus", "is_cancelled")),
                ("Serial and Batch Entry", ("qty", "batch_no", "warehouse", "docstatus", "is_cancelled")),
                (stock.LOG, ("document_id", "request_id", "result_revision")),
            ):
                prior = {row.name: tuple(row[field] for field in fields) for row in before[table]}
                unchanged = {row.name: tuple(row[field] for field in fields) for row in after[table] if row.name in prior}
                require(unchanged == prior, f"correction changed previously committed quantity evidence in {table}")
        else:
            no_quantity(before)
        return approved


def native_cases(f):
    with f.operate():
        command = f.receipt_command("100")
        first, = command.draft.lines
        draft = receipts.create_draft(ReceiptDraftInput(
            f.company, (replace(first, line_id=None), replace(first, line_id=None, sku_id=f.brand)),
        ))
        original = receipts.submit(replace(command, draft=draft))
    one = f.submit(original, ("20", "5"))
    with actor_session(f.approver, f.company), service.synthetic_candidate(f.company, limits=LIMITS):
        expect("denied", lambda: service.approve(f.decision(one)))
    f.check(one)
    second = f.submit(original, ("5",))
    f.check(second)
    with f.operate():
        view = service.eligibility(f.company, original.operation_id)
        identity = f.original_rows(original.operation_id)[0]
        require(Decimal(view["lines"][identity]["used"]) == 25, "same original line lost its cumulative amount")
        require(len(view["lines"][identity]["corrections"]) == 2, "partial correction history incomplete")
        original_issue = issues.submit(f.issue_command("1.125"))
        request = f.submit(original_issue, ("0.125",))
    f.check(request)

    with f.operate():
        # Two real batches at a new physical location. The original is
        # reconciled from all SLE components, including ERPNext's net-zero legs.
        for batch in ("V114-A", "V114-B"):
            receipts.submit(f.receipt_command("8", source=f.target, batch={"mode": "BATCH", "batchNo": batch}))
        task = create(f.company, f.target, Scope.SKU, f.sku)
        task = actual(task, {line.line_id: ("5" if line.batch_no == "V114-A" else "10") for line in task.lines})
        count = finish(task)
        eligibility = service.eligibility(f.company, count.operation_id)
        require(sorted(Decimal(line["net"]) for line in eligibility["lines"].values()) == [-3, 2],
                "count correction used observed amount instead of native net difference")
        mixed = f.submit(count, ("1", "1"))
    before = current_state()
    posting = native_receipt.post

    def reject_after_second(plan, **kwargs):
        posting(plan, **kwargs)
        raise StockError("STOCK_PERSISTENCE_FAILED")

    with f.specialist_session(), patch.object(native_receipt, "post", side_effect=reject_after_second) as second:
        expect("STOCK_PERSISTENCE_FAILED", lambda: service.approve(f.decision(mixed)))
        require(second.call_count == 1, "rollback probe never reached the second native posting")
    no_quantity(before)
    approved = f.check(mixed)
    after = current_state()
    require(len(after["Stock Entry"]) == len(before["Stock Entry"]) + 2,
            "mixed reversal did not post both directions")
    with f.specialist_session():
        detail = trace.detail(f.company, approved.operation_id)
        require(sorted(Decimal(e["quantity"]) for line in detail["lines"] for e in line["effects"]) == [-1, 1],
                "mixed reversal facts could not be traced")
    with f.operate():
        real = receipts.submit(f.receipt_command("2", source=f.target, batch={"mode": "BATCH", "batchNo": "V114-C"}))
    f.check(f.submit(real, ("2",)))


def blocked_cases(f):
    with f.operate():
        original = receipts.submit(f.receipt_command("10"))
        pending = f.submit(original, ("10",))
        issues.submit(f.issue_command("7"))
        receipts.submit(f.receipt_command("20"))
    blocked = f.check(pending, service.MANUAL, blocked_code=facts.DOWNSTREAM)
    with f.specialist_session():
        recorded = service.record_review(f.decision(blocked))
        closed = service.close_review(f.decision(recorded))
        require(closed.status == service.CLOSED, "manual review did not close without a quantity action")
    with f.operate():
        original = receipts.submit(f.receipt_command("2"))
        task = f.count_task()
        before = current_state()
        no_difference = finish(task)
        after = current_state()
        require(before["Stock Ledger Entry"] == after["Stock Ledger Entry"], "test count was not no-difference")
        original_line, = f.original_rows(no_difference.operation_id)
        expect("CORRECTION_NO_QUANTITY_EFFECT", lambda: service.save_draft(
            f.company, no_difference.operation_id, (service.Adjustment(original_line, "1"),),
            "No quantity adjustment in the original count",
        ))
    f.check(f.submit(original, ("1",)), service.MANUAL, blocked_code=facts.RECOUNT)

    with f.operate():
        original = receipts.submit(f.receipt_command("3"))
        # Global approval is disabled during this probe; dispatch retains its
        # explicit second step.
        from haxpower_v1.inventory.transfer.dto import DispatchTransfer, ReceiveTransfer, ReturnTransfer, TransferQuantity
        command = f.transfer_command("2")
        staged = transfers.submit(command)
        dispatched = transfers.dispatch(DispatchTransfer(replace(command.draft, revision=staged.revision), uuid4().hex))
        eligible = service.eligibility(f.company, dispatched.operation_id)
        line = (TransferQuantity(eligible["transfer"][0]["line_id"], "1"),)
        received = transfers.receive(ReceiveTransfer(
            f.company, dispatched.transfer_id, dispatched.revision, uuid4().hex, line,
        ))
        eligible = service.eligibility(f.company, dispatched.operation_id)
        require(Decimal(eligible["transfer"][0]["remaining"]) == Decimal(eligible["transfer"][0]["received"]) == 1,
                "V1-11 remaining and received quantities were not traced independently")
        returned = transfers.return_remaining(ReturnTransfer(
            f.company, dispatched.transfer_id, received.revision, uuid4().hex, line, "Synthetic ordinary transit return",
        ))
        require(returned.status == "Closed with Return", "existing V1-11 return path changed")
    f.check(f.submit(original, ("1",)), service.MANUAL, blocked_code=facts.DOWNSTREAM)
    f.check(f.submit(dispatched, ("1",)), service.MANUAL, blocked_code=facts.TRANSFER)

    with f.operate():
        original = receipts.submit(f.receipt_command("4", lines=2))
        first, second = f.original_rows(original.operation_id)
    f.check(f.submit(original, ("1",)))
    with f.operate():
        draft = service.save_draft(f.company, original.operation_id, (service.Adjustment(second, "1"),),
                                   "Different original line in the same bucket")
        pending = service.submit(service.Submission(f.company, draft.document_id, draft.revision, uuid4().hex))
        evidence = service.eligibility(f.company, original.operation_id)["lines"]
        require(Decimal(evidence[first]["used"]) == 1 and Decimal(evidence[second]["used"]) == 0,
                "same-bucket lines merged their entitlements")
    f.check(pending, service.MANUAL, blocked_code=facts.DOWNSTREAM)


def workflow(f):
    with f.operate():
        original = receipts.submit(f.receipt_command("2"))
    f.toggle(True)
    try:
        rejected = f.submit(original, ("2",), replacement_required=True)
        with f.specialist_session():
            revised = service.reject(f.decision(rejected))
        with f.operate():
            line_id, = f.original_rows(original.operation_id)
            draft = service.save_draft(f.company, original.operation_id, (service.Adjustment(line_id, "1"),),
                                       "Revised synthetic request", correction_id=revised.document_id,
                                       revision=revised.revision)
            command = service.Submission(f.company, draft.document_id, draft.revision, uuid4().hex)
            requested = service.submit(command)
            withdrawn = service.withdraw(f.decision(requested))
            requested = service.submit(replace(command, expected_revision=withdrawn.revision, idempotency_key=uuid4().hex))
        reversed_ = f.check(requested)
        with f.operate():
            normal = receipts.submit(f.receipt_command("1", sku=f.brand, source=f.target))
            require(normal.status == PENDING, "normal replacement had to obey its own approval policy")
        with f.specialist_session():
            require(service.history(f.company, reversed_.document_id)["replacement_pending"],
                    "unresolved two-step correction lost the pending replacement status")
            service.link_replacement(f.decision(reversed_), normal.document_id)
            require(service.history(f.company, reversed_.document_id)["replacement_pending"],
                    "a linked pending replacement was reported as effective")
        with f.operate(), actor_session(f.approver, f.company):
            approval.approve(f.decision(normal))
        with f.specialist_session():
            history = service.history(f.company, reversed_.document_id)
            require(not history["replacement_pending"] and history["replacement"]["operation_id"]
                    and len(history["submissions"]) == 3, "replacement or immutable submission history is incomplete")
    finally:
        f.toggle(False)


def rights(f):
    with f.operate():
        original = receipts.submit(f.receipt_command("2"))
    pending = f.submit(original, ("1",))
    decision = f.decision(pending)
    for user in (f.operator, f.specialist):
        before = current_state()
        with actor_session("Administrator", f.company):
            set_scope(user, f.company, Mode.NONE)
            frappe.db.commit()
        try:
            with f.specialist_session():
                expect("denied", lambda: service.approve(decision))
        finally:
            with actor_session("Administrator", f.company):
                set_scope(user, f.company, Mode.SELECTED, f.warehouses)
                frappe.db.commit()
        no_quantity(before)
    f.check(pending)
    with actor_session(f.approver, f.company):
        expect("denied", lambda: service.history(f.company, pending.document_id))


def competing(site, f):
    with f.operate():
        original = receipts.submit(f.receipt_command("5"))
    one, two = (f.submit(original, ("4",)) for _ in range(2))

    def approve(result, before_guard):
        with service.synthetic_candidate(f.company, limits=LIMITS):
            return service.approve(f.decision(result), _before_guard=before_guard)

    outcomes = race(site, f, tuple((f.specialist, lambda guard, row=row: approve(row, guard)) for row in (one, two)))
    require(sorted(result.status for result in outcomes) == ["Effective", service.MANUAL],
            "concurrent approval exceeded the original line entitlement")
    with f.operate():
        eligibility = service.eligibility(f.company, original.operation_id)
        require([Decimal(line["remaining"]) for line in eligibility["lines"].values()] == [1],
                "independent read did not reconcile competing approvals")
        original = receipts.submit(f.receipt_command("5", sku=f.brand, source=f.target))
        task = actual(create(f.company, f.target, Scope.BUCKET, f.brand))
    pending = f.submit(original, ("1",))
    outcomes = race(site, f, (
        (f.specialist, lambda guard: approve(pending, guard)),
        (f.operator, lambda guard: finish(task, before_guard=guard)),
    ))
    if outcomes[0].status == "Effective":
        require(outcomes[1] in ("COUNT_SCOPE_CHANGED", "COUNT_BUCKET_CHANGED"), "correction did not stale an old count")
    else:
        require(outcomes[0].status == service.MANUAL and outcomes[1].status == "Completed",
                "completed no-difference count and same-bucket correction both took effect")

    with f.operate():
        batch = {"mode": "BATCH", "batchNo": "V114-RACE-" + uuid4().hex[:8]}
        original = receipts.submit(f.receipt_command("5", batch=batch))
        draw = issues.create_draft(IssueDraftInput(f.company, (
            IssueLineInput(f.sku, f.source, batch, "4", PREFIX),
        )))
    pending = f.submit(original, ("4",))
    outcomes = race(site, f, (
        (f.specialist, lambda guard: approve(pending, guard)),
        (f.operator, lambda guard: issues._submit(SubmitIssue(draw, uuid4().hex), before_guard=guard)),
    ))
    if outcomes[0].status == "Effective":
        require(outcomes[1] == "INSUFFICIENT_STOCK", "correction and subsequent outflow overspent the bucket")
    else:
        require(outcomes[0].status == service.MANUAL and outcomes[1].status == "Effective",
                "correction failed to recognize the competing downstream issue")

    with f.operate():
        original = receipts.submit(f.receipt_command("5"))
    pending = f.submit(original, ("1",))
    outcomes = race(site, f, (
        (f.specialist, lambda guard: approve(pending, guard)),
        (f.operator, lambda guard: approval._decide(f.decision(pending), "Withdraw", specialist=True, before_guard=guard)),
    ))
    winners = [result.status for result in outcomes if not isinstance(result, str)]
    require(winners in (["Effective"], ["Draft"]) and "APPROVAL_STATE_CONFLICT" in outcomes,
            "specialist approval and withdrawal did not produce one durable winner")


def _protected_rows():
    with connection(read_committed=True):
        return {table: frappe.get_all(table, fields=["*"], order_by="name") for table in CORRECTION_TABLES}


def failures(f):
    with f.operate():
        original = receipts.submit(f.receipt_command("5"))
    pending = f.submit(original, ("1",))
    with f.specialist_session():
        native_insert = stock.insert

        def fail_insert(doctype, **fields):
            result = native_insert(doctype, **fields)
            if doctype == target:
                raise StockError("STOCK_PERSISTENCE_FAILED")
            return result

        for target in (stock.LOG, stock.LINK):
            decision = f.decision(pending)
            before = current_state()
            protected = _protected_rows()
            with patch.object(stock, "insert", fail_insert):
                expect("STOCK_PERSISTENCE_FAILED", lambda: service.approve(decision))
            no_quantity(before)
            require(_protected_rows() == protected, "rollback altered correction or review evidence")

        advance = versions.advance

        def fail_advance(*args):
            advance(*args)
            raise StockError("STOCK_PERSISTENCE_FAILED")

        before = current_state()
        with patch.object(versions, "advance", fail_advance):
            expect("STOCK_PERSISTENCE_FAILED", lambda: service.approve(f.decision(pending)))
        no_quantity(before)

        event, save_request = approvals.event, approvals.save_request

        def fail_event(*args, **kwargs):
            event(*args, **kwargs)
            if args[1] == "Approve":
                raise StockError("STOCK_PERSISTENCE_FAILED")

        def fail_result(*args, **kwargs):
            save_request(*args, **kwargs)
            if not kwargs.get("failure_code"):
                raise StockError("STOCK_PERSISTENCE_FAILED")

        for target, injected in (("event", fail_event), ("save_request", fail_result)):
            before = current_state()
            with patch.object(approvals, target, injected):
                expect("STOCK_PERSISTENCE_FAILED", lambda: service.approve(f.decision(pending)))
            no_quantity(before)

        def disconnected(database):
            database.close()
            raise OSError("synthetic disconnect before COMMIT")

        before = current_state()
        decision = f.decision(pending)
        expect("STOCK_RETRY_REQUIRED", lambda: service.approve(decision, _commit=disconnected))
        no_quantity(before)

        def lost_response(database):
            database.sql("COMMIT")
            raise OSError("synthetic lost response")

        result = service.approve(decision, _commit=lost_response)
        require(result.status == "Effective" and service.approve(decision) == result,
                "ambiguous COMMIT reposted or lost the committed specialist effect")


def run(site):
    f = Fixture()
    f.toggle(False)
    native_cases(f)
    blocked_cases(f)
    workflow(f)
    rights(f)
    competing(site, f)
    failures(f)
    with f.operate(f.self_specialist):
        original = receipts.submit(f.receipt_command("3", sku=f.brand))
    pending = f.submit(original, ("1",), actor=f.self_specialist)
    f.check(pending, actor=f.self_specialist, applicant=f.self_specialist)
    print("V1-14 native mixed reversals, entitlement, blockers, specialist workflow, concurrency and COMMIT recovery passed.",
          flush=True)


def recovery_prepare():
    f = Fixture()
    f.toggle(False)
    for notes, outcome, blocker in ((BASELINE, "Effective", None),
                                    (BASELINE + " counted", service.MANUAL, facts.RECOUNT)):
        with f.operate():
            original_id = frappe.db.get_value(stock.DOCUMENT, {"notes": notes}, "effective_operation")
            require(original_id, "missing real V1-13 upgrade original")
        # A different authorized employee submits both historical requests.
        f.check(f.submit(SimpleNamespace(operation_id=original_id), ("1",)), outcome, blocked_code=blocker)
    with f.operate():
        original = receipts.submit(f.receipt_command("8"))
    submitted = f.submit(original, ("2",), reason=MARKER + " effective")
    f.check(submitted, decision=_restore_replay(f.company, submitted.document_id, submitted.revision,
                                               submitted.submission_id))
    f.submit(original, ("3",), reason=MARKER + " pending")
    with f.operate():
        receipts.submit(f.receipt_command("1", sku=f.brand))
        blocked = receipts.submit(f.receipt_command("2", sku=f.brand))
        pending = f.submit(blocked, ("1",), reason=MARKER + " manual")
        receipts.submit(f.receipt_command("3", sku=f.brand))
    f.check(pending, service.MANUAL, blocked_code=facts.DOWNSTREAM)


def recovery_finish():
    rows = {status: frappe.db.get_value(stock.DOCUMENT, {"reason": MARKER + " " + status},
                ("name", "company", "owner", "revision", "approval_submission", "status"), as_dict=True)
            for status in ("effective", "pending", "manual")}
    doc = rows["pending"]
    effective = rows["effective"]
    scopes = [row.id for row in warehouses.list_warehouses(company=doc.company)[:2]]
    approver = synthetic_user(uuid4().hex[:10], "restored-specialist", Operation.CORRECTION_APPROVE)[0]
    set_scope(approver, doc.company, Mode.SELECTED, scopes)
    frappe.db.commit()
    with actor_session(approver, doc.company):
        original = service.history(doc.company, effective.name)
        prior_actor, = [event.actor for event in original["events"] if event.action == "Approve"]
    with actor_session(prior_actor, doc.company):
        before = current_state()
        original_decision = _restore_replay(doc.company, effective.name, effective.revision - 1,
                                            effective.approval_submission)
        previous = service.approve(original_decision)
        require(previous.operation_id == original["operation_id"] and current_state() == before,
                "restore did not replay the original specialist request without writes")
    with actor_session(approver, doc.company), service.synthetic_candidate(doc.company, limits=LIMITS):
        manual = service.history(doc.company, rows["manual"].name)
        require(manual["status"] == service.MANUAL and bool(manual["reviews"]), "manual review missing after recovery")
        decision = approval.Decision(doc.company, doc.name, doc.approval_submission, doc.revision, uuid4().hex)
        approved = service.approve(decision)
        require(approved.status == "Effective" and service.approve(decision) == approved, "restored correction did not replay")
    with actor_session(doc.owner, doc.company):
        entitlement = service.eligibility(doc.company, original["original_operation"])
        require([Decimal(row["remaining"]) for row in entitlement["lines"].values()] == [3],
                "restore reset or duplicated cumulative correction quantity")
    print("V1-14 restored effective, pending and manual cases and continued the original partial entitlement.", flush=True)


def _restore_replay(company, document_id, revision, submission_id):
    # A stable input for the synthetic recovery probe, independent of DB state.
    return approval.Decision(company, document_id, submission_id, revision, f"recovery-{document_id}",
                             "Synthetic recovery specialist decision")
