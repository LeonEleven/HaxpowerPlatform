"""Native dispatch, ownership, denial and concurrency rehearsal on disposable sites."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

import frappe

from haxpower_v1.access.config import set_scope
from haxpower_v1.access.policy import Mode, Operation
from haxpower_v1.adapters.erpnext import stock_entry as stock
from haxpower_v1.catalog.sku_persistence import SKU
from haxpower_v1.inventory.batch.persistence import MAPPING
from haxpower_v1.inventory.issue import service as issues
from haxpower_v1.inventory.issue.dto import IssueDraftInput, IssueLineInput
from haxpower_v1.inventory.operations import persistence as store
from haxpower_v1.inventory.operations.dto import StockError
from haxpower_v1.inventory.operations.transaction import connection
from haxpower_v1.inventory.transfer import service, transit
from haxpower_v1.inventory.transfer.dto import DispatchTransfer, TransferDraftInput, TransferLineInput
from haxpower_v1.warehouse import service as warehouse
from haxpower_v1.warehouse.persistence import LOCATION

from access_probe import synthetic_user
from batch_probe import _sku_id
from identity import require
from issue_probe import seed_receipts
from receipt_probe import LIMITS, NO_BATCH, STD61, STD62, snapshot
from sku_probe import PREFIX
from stock_probe import actor_session, expect, session
from warehouse_probe import _company_and_parent, make_company


def fresh():
    with connection():
        return {**snapshot(), transit.OWNERSHIP: frappe.get_all(transit.OWNERSHIP, fields=["*"], order_by="name")}


def reject(code, call):
    before = fresh()
    expect(code, call)
    require(fresh() == before, f"{code} changed persisted stock, accounting or dispatch evidence")


def dispatch_command(draft):
    if hasattr(service, "submit"):  # The V1-12 upgrade fixture predates transfer submission.
        submitted = service.submit(DispatchTransfer(draft, uuid4().hex))
        require(submitted.status == "Ready to Dispatch", "fixture requires exempt transfer submission")
        draft = replace(draft, revision=submitted.revision)
    return DispatchTransfer(draft, uuid4().hex)


def command(company, *lines):
    draft = service.create_draft(TransferDraftInput(company, lines, "Synthetic dispatch", "Dispatch notes"))
    return dispatch_command(draft)


def _new_rows(before, after, table):
    existing = {row.name for row in before[table]}
    return [row for row in after[table] if row.name not in existing]


def checked(payload, result, before, *, value=None):
    after = fresh()
    dispatch = service.get_dispatch(payload.draft.company, result.transfer_id)
    require(
        (dispatch.status, dispatch.operation_id, dispatch.revision, dispatch.dispatched_by, dispatch.reason, dispatch.notes)
        == ("In Transit", result.operation_id, payload.draft.revision + 1, frappe.session.user, payload.draft.reason, payload.draft.notes)
        and dispatch.dispatched_at, "incorrect dispatch header",
    )
    native, = _new_rows(before, after, stock.ENTRY)
    request, = _new_rows(before, after, store.REQUEST)
    log, = _new_rows(before, after, store.LOG)
    require(
        (native.purpose, native.add_to_transit, native.docstatus, native.owner)
        == (stock.TRANSFER, 1, 1, dispatch.dispatched_by)
        and not native.outgoing_stock_entry
        and (request.result_operation, log.name, log.request_id) == (dispatch.operation_id, dispatch.operation_id, request.name),
        "dispatch parent, request or log is inconsistent",
    )
    details = _new_rows(before, after, stock.DETAIL)
    sle = _new_rows(before, after, stock.SLE)
    bundles = _new_rows(before, after, stock.BUNDLE)
    parts = _new_rows(before, after, stock.BUNDLE_ROW)
    links = _new_rows(before, after, store.LINK)
    ownerships = _new_rows(before, after, transit.OWNERSHIP)
    require(
        len(dispatch.lines) == len(details) == len(links) == len(ownerships) == len(payload.draft.lines)
        and len(sle) == len(bundles) == len(parts) == 2 * len(details)
        and after["GL Entry"] == before["GL Entry"],
        "dispatch cardinality or GL changed unexpectedly",
    )

    def totals(state, item, leaf, batch=None):
        if batch is None:
            return sum(Decimal(str(row.actual_qty)) for row in state[stock.SLE]
                       if row.item_code == item and row.warehouse == leaf and row.docstatus == 1 and not row.is_cancelled)
        return sum(Decimal(str(row.qty)) for row in state[stock.BUNDLE_ROW]
                   if row.item_code == item and row.warehouse == leaf and row.batch_no == batch
                   and row.docstatus == 1 and not row.is_cancelled)

    affected = {}
    with connection():
        for line, seed in zip(dispatch.lines, payload.draft.lines, strict=True):
            detail, = [row for row in details if row.name == line.native_detail]
            ownership, = [row for row in ownerships if row.name == line.ownership_id]
            link, = [row for row in links if row.line_id == line.transfer_line_id]
            src, dst, t = (frappe.db.get_value(LOCATION, identity, "native_warehouse") for identity in (
                line.source_location_id, line.target_location_id, line.transit_location_id,
            ))
            sku = frappe.db.get_value(SKU, line.sku_id, "native_item")
            qty = Decimal(line.dispatched_quantity)
            require(
                (line.transfer_line_id, line.sku_id, line.source_location_id, line.target_location_id, line.batch, qty, line.uom)
                == (seed.transfer_line_id, seed.sku_id, seed.source_location_id, seed.target_location_id,
                    seed.batch, Decimal(seed.quantity), seed.uom)
                and (line.native_entry, detail.parent, link.parent_id, link.detail_id, link.role)
                == (native.name, native.name, native.name, detail.name, "Dispatch")
                and (ownership.operation_id, ownership.line_id, ownership.transit_location,
                     ownership.native_detail, ownership.inward_sle)
                == (dispatch.operation_id, line.transfer_line_id, line.transit_location_id, detail.name, line.native_inward_sle)
                and (detail.item_code, detail.s_warehouse, detail.t_warehouse, Decimal(str(detail.transfer_qty)))
                == (sku, src, t, qty) and dst not in (src, t),
                "dispatch changed the original line or referenced the wrong native detail",
            )
            mapping = frappe.db.get_value(MAPPING, {"native_batch": detail.batch_no}, ("name", "kind", "sku"))
            require(mapping[1:] == (line.batch["mode"], line.sku_id)
                    and line.batch_id == (mapping[0] if mapping[1] == "BATCH" else None)
                    and detail.batch_no not in str(line.batch), "domain Batch identity was not preserved")
            for leaf, signed, direction in ((src, -qty, "Outward"), (t, qty, "Inward")):
                movement, = [row for row in sle if (row.voucher_detail_no, row.warehouse) == (detail.name, leaf)]
                bundle, = [row for row in bundles if row.name == movement.serial_and_batch_bundle]
                part, = [row for row in parts if row.parent == bundle.name]
                require(
                    (movement.company, movement.voucher_no, movement.item_code, movement.docstatus, movement.is_cancelled,
                     Decimal(str(movement.actual_qty)), bundle.type_of_transaction, bundle.docstatus, bundle.is_cancelled,
                     bundle.warehouse, bundle.voucher_detail_no, part.batch_no, Decimal(str(part.qty)), part.docstatus)
                    == (dispatch.company, native.name, sku, 1, 0, signed, direction, 1, 0, leaf, detail.name,
                        detail.batch_no, signed, 1)
                    and (movement.name == line.native_inward_sle if signed > 0 else detail.serial_and_batch_bundle == bundle.name),
                    "dispatch legs and their exact batch Bundles do not reconcile",
                )
                if value is not None:
                    require(Decimal(str(movement.stock_value_difference)) == signed * value, "valuation did not move in kind")
                key = sku, leaf, detail.batch_no
                affected[key] = affected.get(key, Decimal(0)) + signed
            affected.setdefault((sku, dst, detail.batch_no), Decimal(0))
    for key, delta in affected.items():
        require(totals(after, *key) - totals(before, *key) == delta, "incorrect source, transit or target bucket")
        item, leaf, _ = key
        raw = [Decimal(str(row.actual_qty)) for row in after["Bin"] if (row.item_code, row.warehouse) == (item, leaf)]
        require(len(raw) <= 1 and sum(raw, Decimal(0)) == totals(after, item, leaf), "effective SLE do not match Bin")
    require({row.name: row.batch_qty for row in after["Batch"]} == {row.name: row.batch_qty for row in before["Batch"]},
            "dispatch changed global Batch totals")
    def company_totals(state):
        totals = {}
        for movement in state[stock.SLE]:
            if movement.company == dispatch.company and movement.docstatus == 1 and not movement.is_cancelled:
                totals[movement.item_code] = totals.get(movement.item_code, Decimal(0)) + Decimal(str(movement.actual_qty))
        return totals

    require(company_totals(after) == company_totals(before), "company quantity changed")
    return dispatch


def post(payload, *, value=None):
    before = fresh()
    return checked(payload, service.dispatch(payload), before, value=value)


def faults(company, line):
    payload = command(company, replace(line, quantity="0.125"), replace(line, quantity="0.125"))
    detail = type(frappe.get_doc({"doctype": stock.DETAIL}))
    bundle = type(frappe.get_doc({"doctype": stock.BUNDLE}))
    sle = type(frappe.get_doc({"doctype": stock.SLE}))
    reached = []

    def after(original, label, when):
        def failing(doc, *args, **kwargs):
            result = original(doc, *args, **kwargs)
            if when(doc):
                reached.append(label)
                raise RuntimeError("Synthetic transfer failure")
            return result
        return failing

    def after_write(doctype):
        write = store.insert

        def failing(table, **kwargs):
            result = write(table, **kwargs)
            if table == doctype:
                reached.append(doctype)
                raise RuntimeError("Synthetic dispatch persistence failure")
            return result
        return failing

    def early_commit(scope):
        reached.append("early COMMIT")
        frappe.db.commit()

    def before_commit(db):
        db.close()
        raise ConnectionError("Synthetic dispatch disconnect before COMMIT")

    def lost_response(db):
        db.sql("COMMIT")
        raise ConnectionError("Synthetic dispatch response lost after COMMIT")

    for label, code, injection in (
        ("second Detail", "STOCK_PERSISTENCE_FAILED", patch.object(detail, "db_insert", after(
            detail.db_insert, "second Detail", lambda row: row.idx == 2))),
        ("inward Bundle insert", "STOCK_PERSISTENCE_FAILED", patch.object(bundle, "db_insert", after(
            bundle.db_insert, "inward Bundle insert", lambda doc: doc.type_of_transaction == "Inward"))),
        ("inward Bundle submit", "STOCK_PERSISTENCE_FAILED", patch.object(bundle, "on_submit", after(
            bundle.on_submit, "inward Bundle submit", lambda doc: doc.type_of_transaction == "Inward"))),
        ("inward SLE", "STOCK_PERSISTENCE_FAILED", patch.object(sle, "on_submit", after(
            sle.on_submit, "inward SLE", lambda doc: doc.actual_qty > 0))),
        (store.LOG, "STOCK_PERSISTENCE_FAILED", patch.object(store, "insert", after_write(store.LOG))),
        (transit.OWNERSHIP, "STOCK_PERSISTENCE_FAILED", patch.object(store, "insert", after_write(transit.OWNERSHIP))),
        ("early COMMIT", "STOCK_ADAPTER_SIDE_EFFECT", patch.object(stock, "verify_entry", early_commit)),
    ):
        with injection:
            reject(code, lambda: service.dispatch(payload))
        require(reached == [label], f"dispatch fault did not reach {label}")
        reached.clear()
        print(f"Transfer rollback verified on a fresh connection after {label}.", flush=True)

    reject("STOCK_RETRY_REQUIRED", lambda: service._dispatch(payload, commit=before_commit))
    before = fresh()
    result = service._dispatch(payload, commit=lost_response)
    checked(payload, result, before)
    persisted = fresh()
    require(service.dispatch(payload) == result and fresh() == persisted, "same-key replay wrote new stock or ownership")
    reject("STOCK_IDEMPOTENCY_CONFLICT", lambda: service.dispatch(replace(
        payload, draft=replace(payload.draft, notes="Changed notes"),
    )))
    reject("STOCK_DOCUMENT_ALREADY_EFFECTIVE", lambda: service.dispatch(replace(payload, idempotency_key=uuid4().hex)))
    print("Transfer COMMIT recovery verified for disconnect and response loss.", flush=True)


def compete(site, company, actor, line, companion, *, replay=False):
    source, other = replace(line, quantity="7"), replace(companion, quantity="1")
    first = command(company, source, other)
    second = first if replay else command(company, other, source)
    with connection():
        leaf = frappe.db.get_value(LOCATION, source.source_location_id, "native_warehouse")
        native_batch = frappe.db.get_value(MAPPING, {
            "sku": source.sku_id, "kind": "BATCH", "original_text": source.batch["batchNo"],
        }, "native_batch")
    ready = Barrier(2, timeout=30)
    observations = []
    before = fresh()

    def attempt(payload):
        with session(site, actor, company), service.synthetic_candidate(company, limits=LIMITS):
            def before_guard(db):
                observed, = db.sql(
                    """
                    SELECT CONNECTION_ID(), SUM(qty) FROM `tabSerial and Batch Entry`
                    WHERE warehouse = %s AND batch_no = %s AND docstatus = 1 AND is_cancelled = 0
                    """, (leaf, native_batch),
                )
                observations.append(observed)
                ready.wait()

            try:
                return service._dispatch(payload, before_guard=before_guard)
            except StockError as error:
                return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, (first, second)))
    with connection() as observer:
        observer_id, = observer.sql("SELECT CONNECTION_ID()")[0]
    require(len({observer_id, *(identity for identity, _ in observations)}) == 3,
            "writers and observer did not use independent MariaDB connections")
    require([quantity for _, quantity in observations] == [10, 10], "both writers must read ten before the lock")
    if replay:
        require(results[0] == results[1] and not isinstance(results[0], str), f"same-key contention failed: {results}")
        checked(first, results[0], before)
    else:
        require(results.count("INSUFFICIENT_STOCK") == 1, f"source demand in the other lock holder was not rechecked: {results}")
        index = 0 if results[1] == "INSUFFICIENT_STOCK" else 1
        checked((first, second)[index], results[index], before)
    print(f"Transfer {'same-key' if replay else 'reversed-line source contention'} verified on two writer connections.", flush=True)


def native_denials(dispatch):
    from erpnext.stock.doctype.stock_entry.stock_entry import make_stock_in_entry

    line = dispatch.lines[0]
    original = frappe.get_doc(stock.ENTRY, line.native_entry)
    detail = original.items[0]

    def write_native(*, dispatch_to_transit, end):
        entry = frappe.get_doc(doctype=stock.ENTRY, company=dispatch.company, purpose=stock.TRANSFER,
                               stock_entry_type=stock.DISPATCH_TYPE if dispatch_to_transit else stock.TRANSFER,
                               add_to_transit=int(dispatch_to_transit),
                               outgoing_stock_entry=original.name if end else None)
        entry.append("items", {
            "s_warehouse": detail.t_warehouse if end else detail.s_warehouse,
            "t_warehouse": detail.s_warehouse if end else detail.t_warehouse,
            "item_code": detail.item_code, "qty": 1, "uom": detail.uom, "batch_no": detail.batch_no,
        })
        with connection():
            entry.insert(ignore_permissions=True)

    def cancel():
        with connection():
            original.cancel()

    def bin_edit():
        with connection():
            name = frappe.db.get_value("Bin", {"warehouse": detail.t_warehouse, "item_code": detail.item_code}, "name")
            frappe.get_doc("Bin", name).db_set("actual_qty", 0)

    def remove_owner():
        with connection():
            frappe.delete_doc(transit.OWNERSHIP, line.ownership_id, ignore_permissions=True)

    reject("denied", lambda: write_native(dispatch_to_transit=True, end=False))
    reject("denied", lambda: write_native(dispatch_to_transit=False, end=True))
    reject("denied", lambda: make_stock_in_entry(original.name))
    reject("denied", cancel)
    reject("denied", bin_edit)
    reject("denied", remove_owner)
    print("Native Transfer, End Transit, cancellation, direct Bin write and ownership removal rejected.", flush=True)


def ownership_drift(company, payload, dispatch):
    line = dispatch.lines[0]
    before = fresh()

    def redirect(inward_sle):
        with connection() as db:
            db.sql(f"UPDATE `tab{transit.OWNERSHIP}` SET inward_sle = %s WHERE name = %s",
                   (inward_sle, line.ownership_id))
            db.sql("COMMIT")

    redirect(uuid4().hex)
    try:
        reject(transit.INCONSISTENT, lambda: service.get_dispatch(company, dispatch.transfer_id))
        reject(transit.INCONSISTENT, lambda: service.dispatch(payload))
    finally:
        redirect(line.native_inward_sle)
    require(fresh() == before, "synthetic drift fixture did not restore the original state")
    print("Missing ownership detected on both original-dispatch read and a new dispatch.", flush=True)


def verify_history():
    company, _ = _company_and_parent()
    with connection():
        leaf = transit.resolve(company)
        skus = frappe.get_all(SKU, fields=["name", "native_item"], order_by="name")
        items = {row.native_item for row in skus}
        from haxpower_v1.inventory import availability
        from haxpower_v1.inventory.batch.service import _load_sku

        (buckets, _), _ = availability.read_source(
            company, {(item, leaf.native_warehouse) for item in items},
            [_load_sku(row.name, locked=True) for row in skus],
        )
        owned = transit.reconcile(company, items, leaf, buckets).originals
        require(len(owned) == frappe.db.count(transit.OWNERSHIP, {"transit_location": leaf.name}), "historical transit lines were lost")
        print(f"Reconciled {len(owned)} persisted original transit lines against native inventory.", flush=True)


def accounting_candidate(sample):
    marker = "G" + uuid4().hex[:5].upper()
    company, parent = make_company(marker)
    master = frappe.get_doc("Company", company)
    master.enable_perpetual_inventory = 1
    master.default_inventory_account = frappe.db.get_value(
        "Account", {"company": company, "account_type": "Stock", "is_group": 0},
    )
    master.stock_adjustment_account = frappe.db.get_value(
        "Account", {"company": company, "account_type": "Stock Adjustment", "is_group": 0},
    )
    master.save(ignore_permissions=True)
    account = frappe.get_doc(
        doctype="Account", account_name="Synthetic Dispatch Source", account_type="Stock",
        company=company, parent_account=frappe.db.get_value("Account", master.default_inventory_account, "parent_account"),
    ).insert().name
    source_native = frappe.get_doc(
        doctype="Warehouse", warehouse_name="Synthetic Dispatch Source", company=company,
        parent_warehouse=parent, account=account,
    ).insert()
    source, target = (
        warehouse.create_warehouse(company=company, native_parent=parent, code=code, display_name=code)
        for code in ("W1", "W2")
    )
    source_leaf = warehouse.create_location(source.id, code="A01", display_name="Source", existing_native=source_native.name)
    target_leaf = warehouse.create_location(target.id, code="A01", display_name="Target")
    warehouse.provision_system_transit(company=company, native_parent=parent,
                                       warehouse_code="TRANSIT", location_code="T1", display_name="Transit")
    actor, _ = synthetic_user(marker.lower(), "transfer-gl", Operation.TRANSFER, Operation.RECEIVE)
    set_scope(actor, company, Mode.SELECTED, [source.id, target.id])
    frappe.db.commit()
    line = replace(sample, source_location_id=source_leaf.id, target_location_id=target_leaf.id, quantity="2")
    seed_receipts(company, actor, (line,), rate=Decimal("4.25"), allow_gl=True)

    observed, verify = [], stock.verify_entry

    def inspect_gl(scope):
        links = verify(scope)
        observed.extend(frappe.db.get_values("GL Entry", {"voucher_no": scope.entry.name},
                                             ("account", "debit", "credit"), as_dict=True, for_update=True))
        return links

    with actor_session(actor, company), service.synthetic_candidate(company, limits=LIMITS):
        payload = command(company, replace(line, quantity="1"))
        with patch.object(stock, "verify_entry", inspect_gl):
            reject("DISPATCH_GL_NOT_ALLOWED", lambda: service.dispatch(payload))
    require({(row.account, Decimal(str(row.debit)), Decimal(str(row.credit))) for row in observed}
            == {(account, Decimal(0), Decimal("4.25")),
                (master.default_inventory_account, Decimal("4.25"), Decimal(0))},
            "native cross-account GL did not move 4.25 between inventory accounts")
    print("Native perpetual inventory generated balanced 4.25 transfer GL; dispatch rejected and rolled it back.", flush=True)


def run(site):
    company, _ = _company_and_parent()
    warehouses = warehouse.list_warehouses(company=company)
    label = "T" + uuid4().hex[:10].upper()
    source, target, alternate = (
        warehouse.create_location(row.id, code=label, display_name="Synthetic transfer " + label).id
        for row in warehouses[:3]
    )
    same = warehouse.list_locations(warehouses[0].id)[0].id
    transit_id = transit.resolve(company).name
    other_company, other_parent = make_company("T" + uuid4().hex[:4].upper())
    foreign = warehouse.create_warehouse(company=other_company, native_parent=other_parent, code="LOCAL",
                                         display_name="Synthetic foreign warehouse")
    foreign_leaf = warehouse.create_location(foreign.id, code="A01", display_name="Synthetic foreign leaf").id
    actor, _ = synthetic_user(uuid4().hex[:10], "transfer", Operation.TRANSFER, Operation.RECEIVE, Operation.ISSUE)
    unqualified, _ = synthetic_user(uuid4().hex[:10], "transfer-read", Operation.RECEIVE)
    set_scope(actor, company, Mode.SELECTED, [row.id for row in warehouses[:3]])
    set_scope(unqualified, company, Mode.ALL)
    if not frappe.db.exists("Stock Entry Type", stock.TRANSFER):
        frappe.get_doc(doctype="Stock Entry Type", purpose=stock.TRANSFER, is_standard=1).insert(set_name=stock.TRANSFER)
    if not frappe.db.exists("Stock Entry Type", stock.DISPATCH_TYPE):
        frappe.get_doc(doctype="Stock Entry Type", name=stock.DISPATCH_TYPE, purpose=stock.TRANSFER,
                       add_to_transit=1, is_standard=0).insert(set_name=stock.DISPATCH_TYPE)
    frappe.db.commit()

    first, second = _sku_id("A"), _sku_id("B")
    sample = TransferLineInput(first, source, target, STD61, "10", PREFIX)
    mixed = (
        sample, replace(sample, batch=STD62), replace(sample, batch=NO_BATCH),
        replace(sample, sku_id=second, source_location_id=alternate),
        replace(sample, batch={"mode": "BATCH", "batchNo": label}, quantity="1.125"),
        replace(sample, batch={"mode": "BATCH", "batchNo": label + "R"}),
        replace(sample, batch={"mode": "BATCH", "batchNo": label + "K"}),
        replace(sample, source_location_id=target, quantity="2"),
    )
    valued = replace(sample, batch={"mode": "BATCH", "batchNo": label + "V"}, quantity="2")
    seed_receipts(company, actor, mixed)
    seed_receipts(company, actor, (valued,), rate=Decimal("4.25"))
    with actor_session(unqualified, company), service.synthetic_candidate(company, limits=LIMITS):
        reject("denied", lambda: command(company, sample))
    with actor_session(actor, company):
        reject("TRANSFER_NOT_ENABLED", lambda: command(company, sample))
        with service.synthetic_candidate(company, limits=LIMITS):
            for code, line in (
                ("denied", replace(sample, target_location_id=transit_id)),
                ("denied", replace(sample, source_location_id=transit_id)),
                ("denied", replace(sample, target_location_id=foreign_leaf)),
                ("denied", replace(sample, source_location_id=foreign_leaf)),
                ("TRANSFER_DISTINCT_LOCATIONS_REQUIRED", replace(sample, target_location_id=source)),
                ("TRANSFER_SAME_WAREHOUSE_UNAPPROVED", replace(sample, target_location_id=same)),
                ("BATCH_SELECTION_REQUIRED", replace(sample, batch=None)),
                ("STOCK_UOM_MISMATCH", replace(sample, uom="Other")),
                ("STOCK_QUANTITY_PRECISION", replace(sample, quantity="0.0001")),
                ("STOCK_QUANTITY_LIMIT", replace(sample, quantity="1000.001")),
            ):
                reject(code, lambda: command(company, line))
            permission = command(company, replace(sample, quantity="1"))
            for grants in ([warehouses[0].id], [warehouses[1].id]):
                with actor_session("Administrator", company):
                    set_scope(actor, company, Mode.SELECTED, grants)
                    frappe.db.commit()
                reject("denied", lambda: command(company, sample))
                reject("denied", lambda: service.dispatch(permission))
            with actor_session("Administrator", company):
                set_scope(actor, company, Mode.SELECTED, [row.id for row in warehouses[:3]])
                frappe.db.commit()
            rejected = command(company, replace(mixed[1], quantity="1"), replace(sample, quantity="11"))
            reject("INSUFFICIENT_STOCK", lambda: service.dispatch(rejected))
            aggregated = command(company, replace(sample, quantity="6"), replace(sample, quantity="5"))
            reject("INSUFFICIENT_STOCK", lambda: service.dispatch(aggregated))
            reject("STOCK_DOCUMENT_PAYLOAD_MISMATCH", lambda: service.dispatch(replace(
                permission, draft=replace(permission.draft, lines=(replace(
                    permission.draft.lines[0], target_location_id=alternate,
                ),)),
            )))
            p1 = post(command(company, replace(sample, quantity="1"), replace(sample, quantity="2"),
                              replace(mixed[1], quantity="2"), replace(mixed[2], quantity="1"),
                              replace(mixed[3], quantity="3"), replace(mixed[4], quantity="0.125")))
            p2 = post(permission)
            with connection():
                require(service.lock_original_dispatch(company, p2.transfer_id) == p2,
                        "transaction-local original dispatch read changed the result")
            persisted_p1 = service.get_dispatch(company, p1.transfer_id)
            require(persisted_p1 == p1 and p1.lines[0].batch_id == p2.lines[0].batch_id
                    and p1.lines[0].transit_location_id == p2.lines[0].transit_location_id
                    and p1.lines[0].ownership_id != p2.lines[0].ownership_id,
                    "two transfers shared ownership or changed the original dispatch")
            native_denials(p1)
            with issues.synthetic_candidate(company, limits=LIMITS):
                reject("denied", lambda: issues.create_draft(IssueDraftInput(company, (
                    IssueLineInput(first, transit_id, sample.batch, "1", PREFIX),
                ))))
            ownership_drift(company, command(company, replace(sample, quantity="1")), p1)
            post(command(company, replace(valued, quantity="1")), value=Decimal("4.25"))
            faults(company, mixed[4])
            compete(site, company, actor, mixed[5], mixed[3])
            compete(site, company, actor, mixed[6], mixed[3], replay=True)
            with actor_session("Administrator", company):
                set_scope(actor, company, Mode.SELECTED, [warehouses[0].id])
                frappe.db.commit()
            reject("denied", lambda: service.get_dispatch(company, p2.transfer_id))
            reject("denied", lambda: service.dispatch(permission))
            with actor_session("Administrator", company):
                set_scope(actor, company, Mode.SELECTED, [row.id for row in warehouses[:3]])
                frappe.db.commit()
        with service.synthetic_candidate(company, limits=LIMITS, allow_same_warehouse=True):
            post(command(company, replace(sample, quantity="1", target_location_id=same)))
    verify_history()
    accounting_candidate(sample)
    print("Transfer: source and target independently authorized, exact Decimal buckets, native two-leg identity, "
          "distinct same-bucket ownerships and isolated same-warehouse fixture verified.", flush=True)


UPGRADE_MARKER = "V1-11 historical dispatch"


def followup_fixture():
    company, _ = _company_and_parent()
    warehouse_ids = [row.id for row in warehouse.list_warehouses(company=company)[:2]]
    label = "R" + uuid4().hex[:10].upper()
    source, target = [warehouse.create_location(identifier, code=label, display_name=label).id
                      for identifier in warehouse_ids]
    actor, _ = synthetic_user(label.lower(), "transfer-followup", Operation.TRANSFER, Operation.RECEIVE)
    set_scope(actor, company, Mode.SELECTED, warehouse_ids)
    for kind, add_to_transit in ((stock.TRANSFER, 0), (stock.DISPATCH_TYPE, 1)):
        if not frappe.db.exists("Stock Entry Type", kind):
            frappe.get_doc(doctype="Stock Entry Type", purpose=stock.TRANSFER,
                           add_to_transit=add_to_transit, is_standard=int(not add_to_transit)).insert(set_name=kind)
    frappe.db.commit()
    return actor, company, warehouse_ids, TransferLineInput(_sku_id("A"), source, target, STD61, "10", PREFIX)


def seed_history():
    # Use the legacy two-phase DTOs from the pinned V1-12 baseline.
    actor, company, _, sample = followup_fixture()
    seed_receipts(company, actor, (replace(sample, quantity="20"),))
    with actor_session(actor, company), service.synthetic_candidate(company, limits=LIMITS):
        for index in (1, 2):
            reason = f"{UPGRADE_MARKER} {index}"
            draft = service.create_draft(TransferDraftInput(company, (sample,), reason, "Original V1-10 notes"))
            post(DispatchTransfer(draft, f"v111-upgrade-dispatch-{index}"))
    print("Persisted two real baseline dispatches before changing App revision.", flush=True)
