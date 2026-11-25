"""Exercise native Material Receipts on the disposable Bench sites."""

from dataclasses import asdict, replace
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

import frappe
from frappe.utils import getdate

from haxpower_v1.access.config import set_scope
from haxpower_v1.access.policy import Mode, Operation
from haxpower_v1.adapters.erpnext import receipt as native
from haxpower_v1.catalog.sku_persistence import SKU
from haxpower_v1.inventory.batch.persistence import MAPPING
from haxpower_v1.inventory.operations import persistence as store
from haxpower_v1.inventory.operations.dto import StockError
from haxpower_v1.inventory.operations.transaction import connection
from haxpower_v1.inventory.receipt import service
from haxpower_v1.inventory.receipt.dto import ReceiptDraftInput, ReceiptLineInput, SubmitReceipt
from haxpower_v1.warehouse import service as warehouse
from haxpower_v1.warehouse.persistence import LOCATION

from access_probe import synthetic_user
from batch_probe import _sku_id
from identity import require
from sku_probe import PREFIX
from stock_probe import actor_session, expect, session
from warehouse_probe import _company_and_parent

STD61 = {"mode": "BATCH", "batchNo": "STD61"}
STD62 = {"mode": "BATCH", "batchNo": "STD62"}
NO_BATCH = {"mode": "NO_BATCH"}
LIMITS = {PREFIX: "1000"}
PIECES = "SYNTHETIC-RECEIPT-PC"


def seed():
    from catalog_probe import seed as seed_catalog
    from sku_probe import seed as seed_skus
    from warehouse_probe import seed as seed_warehouses

    seed_catalog()
    seed_skus()
    seed_warehouses()
    frappe.db.set_single_value("Stock Settings", "enable_serial_and_batch_no_for_item", 1)
    frappe.db.commit()


def snapshot():
    doctypes = (
        native.ENTRY, native.DETAIL, native.BUNDLE, native.BUNDLE_ROW, native.SLE,
        "Bin", "Batch", "GL Entry", "Payment Ledger Entry", "Repost Item Valuation",
        "Item", MAPPING, store.DOCUMENT, store.LINE, store.REQUEST, store.LOG, store.LINK,
    )
    state = {table: frappe.get_all(table, fields=["*"], order_by="name") for table in doctypes}
    for table in ('HP V1 Inventory Epoch','HP V1 Inventory Version','HP V1 Stock Count','HP V1 Stock Count Line',
                  'Stock Reconciliation','Stock Reconciliation Item'):
        if frappe.db.table_exists(table):
            state[table]=frappe.get_all(table,fields=['*'],order_by='name')
    state["Version"] = frappe.get_all(
        "Version", filters={"ref_doctype": ("in", [native.ENTRY, native.BUNDLE, native.SLE, "Item", "Batch"])},
        fields=["*"], order_by="name",
    )
    return state


def fresh():
    with connection():
        return snapshot()


def checked_result(result, draft, before, actor, *, expected_gl=0, purpose=native.PURPOSE):
    issue = purpose == "Material Issue"
    state = fresh()
    new = {table: [row for row in records if row.name not in {old.name for old in before[table]}]
           for table, records in state.items()}
    entry, = new[native.ENTRY]
    log, = new[store.LOG]
    request, = new[store.REQUEST]
    require((entry.owner, entry.company, entry.purpose, entry.docstatus) == (actor, draft.company, purpose, 1),
            "native stock entry ownership or submission status differs")
    require((log.name, log.actor, log.document_id, log.request_id, request.result_operation)
            == (result.operation_id, actor, result.document_id, request.name, result.operation_id),
            "request and operation do not reference the same document and actor")
    require(len(new["GL Entry"]) == expected_gl, "unexpected GL row count")
    require(not new["Repost Item Valuation"] and not new["Payment Ledger Entry"],
            "receipt unexpectedly created repost or payment work")
    require(result.lines == draft.lines and result.status == "Effective" and result.revision == draft.revision + 1,
            "domain stock result changed the draft lines")
    require(len(new[store.LINK]) == len(new[native.DETAIL]) == len(new[native.BUNDLE]) == len(new[native.SLE])
            == len(new[native.BUNDLE_ROW]) == len(draft.lines), "native line, bundle, SLE, or link count differs")
    new_doc = next(row for row in state[store.DOCUMENT] if row.name == draft.document_id)
    require((new_doc.status, new_doc.revision, new_doc.effective_operation, request.status)
            == ("Effective", result.revision, result.operation_id, "Completed"),
            "stock request and business document must become effective together")
    with connection():
        from erpnext.stock.doctype.serial_and_batch_bundle.serial_and_batch_bundle import get_batch_qty_for_stock

        expected = {}
        for line in draft.lines:
            link, = [row for row in new[store.LINK] if row.line_id == line.line_id]
            detail, = [row for row in new[native.DETAIL] if row.name == link.detail_id]
            ledger, = [row for row in new[native.SLE] if row.voucher_detail_no == detail.name]
            bundle, = [row for row in new[native.BUNDLE] if row.voucher_detail_no == detail.name]
            selection = {"sku": line.sku_id, "kind": line.batch["mode"]}
            if line.batch["mode"] == "BATCH":
                selection["original_text"] = line.batch["batchNo"]
            mapping = frappe.db.get_value(MAPPING, selection, ("name", "native_batch"), as_dict=True)
            batch = mapping.native_batch
            business, = [row for row in state[store.LINE] if row.name == line.line_id]
            if issue:
                require(business.document_id == result.document_id and business.source == line.source_location_id
                        and not business.target and business.sku_id == line.sku_id
                        and business.batch_id == (mapping.name if line.batch["mode"] == "BATCH" else None),
                        "issue did not preserve the domain source and explicit Batch identity")
            part, = [row for row in new[native.BUNDLE_ROW] if row.parent == bundle.name]
            item = frappe.db.get_value(SKU, line.sku_id, "native_item")
            location_id = line.source_location_id if issue else line.location_id
            leaf = frappe.db.get_value(LOCATION, location_id, "native_warehouse")
            key = item, leaf, batch
            qty = Decimal(line.quantity) * (-1 if issue else 1)
            expected[key] = expected.get(key, Decimal(0)) + qty
            require(
                (link.operation_id, link.parent_id, detail.parent, ledger.voucher_no, bundle.voucher_no)
                == (result.operation_id, entry.name, entry.name, entry.name, entry.name)
                and (detail.item_code, ledger.item_code, bundle.item_code) == (item, item, item)
                and ((detail.s_warehouse if issue else detail.t_warehouse), ledger.warehouse, bundle.warehouse)
                == (leaf, leaf, leaf)
                and not (detail.t_warehouse if issue else detail.s_warehouse)
                and bundle.type_of_transaction == ("Outward" if issue else "Inward")
                and link.role == ("Issue" if issue else "Receipt")
                and ledger.serial_and_batch_bundle == detail.serial_and_batch_bundle == bundle.name
                and not ledger.is_cancelled and ledger.docstatus == bundle.docstatus == part.docstatus == 1
                and not bundle.is_cancelled and part.batch_no == batch
                and Decimal(str(ledger.actual_qty)) == Decimal(str(part.qty)) == qty
                and batch not in repr(asdict(result)),
                "native stock row does not trace the expected domain line",
            )

        def total(tables, item, leaf, batch):
            bundles = {row.serial_and_batch_bundle for row in tables[native.SLE]
                       if row.item_code == item and row.warehouse == leaf and row.docstatus == 1 and not row.is_cancelled}
            return sum((Decimal(str(row.qty)) for row in tables[native.BUNDLE_ROW]
                        if row.parent in bundles and row.batch_no == batch), Decimal(0))

        for key, delta in expected.items():
            item, leaf, batch = key
            qty = get_batch_qty_for_stock(batch_no=batch, warehouse=leaf, item_code=item,
                                          do_not_check_future_batches=True, ignore_reserved_stock=True, for_stock_levels=True)
            require(Decimal(str(qty)) == total(before, *key) + delta == total(state, *key),
                    "native Batch quantity or effective bundle total differs")
            bin_qty, = [row.actual_qty for row in state["Bin"] if row.item_code == item and row.warehouse == leaf]
            sle_sum = sum(Decimal(str(row.actual_qty)) for row in state[native.SLE]
                          if row.item_code == item and row.warehouse == leaf and row.docstatus == 1
                          and not row.is_cancelled)
            require(Decimal(str(bin_qty)) == sle_sum, "Bin differs from effective SLE sum")
        existing_buckets = {
            (row.item_code, row.warehouse, row.batch_no) for row in state[native.BUNDLE_ROW]
        }
        for key in existing_buckets - expected.keys():
            require(total(state, *key) == total(before, *key), "an unselected bucket changed")
        if issue:
            touched_pairs = {key[:2] for key in expected}

            def untouched_bins(snapshot):
                return {(row.item_code, row.warehouse): Decimal(str(row.actual_qty)) for row in snapshot["Bin"]
                        if (row.item_code, row.warehouse) not in touched_pairs}

            require(untouched_bins(before) == untouched_bins(state), "an unselected Bin quantity changed")
        for mapping in state[MAPPING]:
            batch = mapping.native_batch
            native_batch, = [row for row in state["Batch"] if row.name == batch]
            total_batched = sum(Decimal(str(part.qty)) for part in state[native.BUNDLE_ROW]
                                if part.batch_no == batch and part.docstatus == 1 and not part.is_cancelled)
            require(Decimal(str(native_batch.batch_qty)) == total_batched,
                    "native Batch master balance differs from submitted bundles")
    if issue:
        for table in (native.ENTRY, native.DETAIL, native.BUNDLE, native.BUNDLE_ROW, native.SLE,
                      "GL Entry", store.DOCUMENT, store.LINE, store.REQUEST, store.LOG, store.LINK):
            old_rows = {row.name: row for row in before[table]
                        if table != store.DOCUMENT or row.name != result.document_id}
            require({row.name: row for row in state[table] if row.name in old_rows} == old_rows,
                    f"issue rewrote historical {table} rows")
    return new


def invalid_drafts(payload, ungranted_location, transit):
    line = payload.lines[0]
    cases = (
        ("STOCK_UOM_MISMATCH", {"uom": "incorrect"}),
        ("STOCK_QUANTITY_PRECISION", {"quantity": "1.0001"}),
        ("STOCK_QUANTITY_INVALID", {"quantity": "NaN"}),
        ("STOCK_QUANTITY_INVALID", {"quantity": "Infinity"}),
        ("STOCK_QUANTITY_SIGN", {"quantity": "0"}),
        ("STOCK_QUANTITY_SIGN", {"quantity": "-0.25"}),
        ("STOCK_QUANTITY_LIMIT", {"quantity": "1000.001"}),
        ("BATCH_SELECTION_REQUIRED", {"batch": None}),
        ("INVALID_BATCH_TEXT", {"batch": {"mode": "BATCH", "batchNo": None}}),
        ("BATCH_TEXT_EMPTY", {"batch": {"mode": "BATCH", "batchNo": ""}}),
        ("BATCH_QUOTES_REQUIRE_REVIEW", {"batch": {"mode": "BATCH", "batchNo": "''"}}),
        ("BATCH_TEXT_RESERVED", {"batch": {"mode": "BATCH", "batchNo": "HP-RB-" + uuid4().hex}}),
        ("STOCK_INVALID_ID", {"sku_id": "ERPNext Item"}),
        ("BATCH_SKU_UNAVAILABLE", {"sku_id": uuid4().hex}),
        ("denied", {"location_id": uuid4().hex}),
        ("denied", {"location_id": ungranted_location}),
        ("denied", {"location_id": transit}),
    )
    initial = fresh()
    for code, fields in cases:
        expect(code, lambda: service.create_draft(replace(payload, lines=(replace(line, **fields),))))
        require(fresh() == initial, f"invalid receipt draft changed persisted state after {code}")
    expect("STOCK_LINES_REQUIRED", lambda: service.create_draft(replace(payload, lines=())))


def faults(payload):
    # Use a new batch so each rollback must undo its domain and native identities.
    fresh_batch = {"mode": "BATCH", "batchNo": "FAULT-" + uuid4().hex}
    lines = (replace(payload.lines[0], batch=fresh_batch), payload.lines[1])
    draft = service.create_draft(replace(payload, lines=lines))
    command = SubmitReceipt(draft, uuid4().hex)
    original_insert = store.insert
    count = 0
    reached = []
    native_errors = []
    post = native.post

    def capture_native_error(*args, **kwargs):
        try:
            return post(*args, **kwargs)
        except Exception as error:
            native_errors.append(error)
            raise

    def failure(label):
        reached.append(label)
        raise RuntimeError("Synthetic receipt fault")

    def second_detail(doc, *args, **kwargs):
        nonlocal count
        original_detail_insert(doc, *args, **kwargs)
        if doc.parenttype == native.ENTRY:
            count += 1
            if count == 2:
                failure("second native detail insert")

    def fail_insert(table, label):
        def insert(doctype, **kwargs):
            identity = original_insert(doctype, **kwargs)
            if doctype == table:
                reached.append(label)
                original_insert(doctype, **dict(kwargs, name=identity))
            return identity
        return insert

    def fail_request(request, result):
        store_complete(request, result)
        failure("request result update")

    def refuse_callback(*_args, **_kwargs):
        reached.append("unreviewed transaction callback")
        frappe.db.after_commit.add(lambda: failure("unexpected callback delivery"))

    def refuse_commit(*_args, **_kwargs):
        reached.append("swallowed early commit")
        try:
            frappe.db.commit()
        except StockError:
            pass
        return verify_posting(*_args, **_kwargs)

    leaked_cache_keys = set()

    def refuse_enqueue(scope, *_args, **_kwargs):
        reached.append("background enqueue")
        entries = [(native.ENTRY, scope.entry.name)]
        entries.extend((native.BUNDLE, name) for name in scope.bundles.values())
        for doctype, name in entries:
            frappe.get_cached_doc(doctype, name)
            leaked_cache_keys.add(f"document_cache::{doctype}::{name}")
        frappe.enqueue("frappe.ping")

    def refuse_realtime(*_args, **_kwargs):
        reached.append("immediate realtime message")
        frappe.publish_realtime("synthetic_receipt_should_not_emit", {})

    def before_commit(database):
        database.close()
        raise ConnectionError("Synthetic disconnect before COMMIT")

    def lost_response(database):
        database.sql("COMMIT")
        raise ConnectionError("Synthetic lost response after COMMIT")

    details = frappe.get_doc({"doctype": native.DETAIL}).__class__
    original_detail_insert = details.db_insert
    store_complete = store.complete_request
    verify_posting = native._verify_posting
    original_bundle_submit = frappe.get_doc({"doctype": native.BUNDLE}).__class__.on_submit
    bundle_type = frappe.get_doc({"doctype": native.BUNDLE}).__class__

    def after_bundle_submit(doc):
        original_bundle_submit(doc)
        failure("native Bundle submit")

    initial = fresh()
    cases = (
        ("second native detail insert", "STOCK_PERSISTENCE_FAILED", patch.object(details, "db_insert", second_detail)),
        ("native Bundle submit", "STOCK_PERSISTENCE_FAILED", patch.object(bundle_type, "on_submit", after_bundle_submit)),
        ("operation log head", "STOCK_PERSISTENCE_FAILED",
         patch.object(store, "insert", fail_insert(store.LOG, "operation log head"))),
        ("operation line link", "STOCK_PERSISTENCE_FAILED",
         patch.object(store, "insert", fail_insert(store.LINK, "operation line link"))),
        ("request result update", "STOCK_PERSISTENCE_FAILED", patch.object(store, "complete_request", fail_request)),
        ("unreviewed transaction callback", "STOCK_ADAPTER_SIDE_EFFECT", patch.object(native, "_verify_posting", refuse_callback)),
        ("swallowed early commit", "STOCK_ADAPTER_SIDE_EFFECT", patch.object(native, "_verify_posting", refuse_commit)),
        ("background enqueue", "STOCK_ADAPTER_SIDE_EFFECT", patch.object(native, "_verify_posting", refuse_enqueue)),
        ("immediate realtime message", "STOCK_ADAPTER_SIDE_EFFECT",
         patch.object(native, "_verify_posting", refuse_realtime)),
    )
    for label, code, injection in cases:
        try:
            with injection, patch.object(native, "post", capture_native_error):
                expect(code, lambda: service.submit(command))
            require(reached == [label], f"fault injection did not reach {label}")
        except Exception as error:
            if native_errors:
                raise error from native_errors[-1]
            raise
        reached.clear()
        native_errors.clear()
        require(fresh() == initial, f"fresh connection found partial writes after {label}")
        print(f"Native receipt rolled back after {label}.", flush=True)
    require(leaked_cache_keys and all(
        frappe.cache.get_value(key, use_local_cache=False) is None for key in leaked_cache_keys
    ), "a rolled-back receipt or bundle entered shared Redis document cache")
    expect("STOCK_RETRY_REQUIRED", lambda: service._submit(command, commit=before_commit))
    require(fresh() == initial, "disconnect before COMMIT left native or domain records")
    result = service._submit(command, commit=lost_response)
    require(service.submit(command) == result, "lost-response replay did not return committed receipt")
    return result, draft, initial


def compete(site, actor, payload, *, same_key):
    with actor_session(actor, payload.company), service.synthetic_candidate(payload.company, limits=LIMITS):
        draft = service.create_draft(replace(payload, lines=(replace(payload.lines[0], quantity="0.125"),)))
    initial = fresh()
    ready = Barrier(2, timeout=20)
    connection_ids = []

    def before_guard(database):
        connection_ids.append(database.sql("SELECT CONNECTION_ID()")[0][0])
        ready.wait()

    def submit(index):
        with session(site, actor, payload.company), service.synthetic_candidate(payload.company, limits=LIMITS):
            command = SubmitReceipt(draft, f"race-{0 if same_key else index}-{draft.document_id}")
            try:
                return service._submit(command, before_guard=before_guard)
            except StockError as error:
                return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(submit, (1, 2)))
    if same_key:
        require(not isinstance(results[0], str) and results[0] == results[1], "concurrent same-key replay differed")
        result = results[0]
    else:
        require(results.count("STOCK_DOCUMENT_ALREADY_EFFECTIVE") == 1, "different keys resubmitted a receipt")
        result, = [value for value in results if not isinstance(value, str)]
    with connection() as db:
        verifier = db.sql("SELECT CONNECTION_ID()")[0][0]
    require(len({verifier, *connection_ids}) == 3, "receipt competition failed to use three independent connections")
    checked_result(result, draft, initial, actor)
    print(f"{'Same' if same_key else 'Different'}-key receipt competition: two writers, one posting, "
          "third connection reconciled.", flush=True)


def verify_guards(payload, actor, command, posted, warehouses):
    draft = command.draft
    initial = fresh()
    for actor_name, roles, mode in (
        ("no-operation", (), Mode.ALL),
        ("no-scope", (Operation.RECEIVE,), Mode.NONE),
    ):
        denied, _ = synthetic_user(f"v108-{uuid4().hex[:8]}", actor_name, *roles)
        set_scope(denied, payload.company, mode)
        frappe.db.commit()
        with actor_session(denied, payload.company), service.synthetic_candidate(payload.company, limits=LIMITS):
            expect("denied", lambda: service.create_draft(payload))
            expect("denied", lambda: service.submit(SubmitReceipt(draft, uuid4().hex)))
    require(fresh() == initial, "authorization denial changed persisted receipt state")

    set_scope(actor, payload.company, Mode.SELECTED, [warehouses[0].id])
    frappe.db.commit()
    with actor_session(actor, payload.company), service.synthetic_candidate(payload.company, limits=LIMITS):
        expect("denied", lambda: service.submit(command))
    set_scope(actor, payload.company, Mode.SELECTED, [record.id for record in warehouses[:2]])
    user = frappe.get_doc("User", actor)
    user.remove_roles(Operation.RECEIVE.role)
    frappe.db.commit()
    with actor_session(actor, payload.company), service.synthetic_candidate(payload.company, limits=LIMITS):
        expect("denied", lambda: service.submit(command))
    user.add_roles(Operation.RECEIVE.role)
    frappe.db.commit()
    require(fresh() == initial, "access revocation changed posted stock or requests")

    entry, = posted[native.ENTRY]
    bundle = posted[native.BUNDLE][0]
    with actor_session(actor, payload.company):
        for doctype, name in ((native.ENTRY, entry.name), (native.BUNDLE, bundle.name)):
            for operation in ("change", "cancel", "delete", "copy"):
                with connection():
                    doc = frappe.get_doc(doctype, name)
                    doc.flags.ignore_permissions = True
                    if operation == "change":
                        child = (doc.items if doctype == native.ENTRY else doc.entries)[0]
                        child.qty += 1
                        expect("denied", doc.save)
                    elif operation == "cancel":
                        expect("denied", doc.cancel)
                    elif operation == "delete":
                        try:
                            frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)
                        except frappe.PermissionError:
                            pass
                        except frappe.ValidationError as error:
                            require("Submitted Record cannot be deleted" in str(error),
                                    "delete failed before native lifecycle protection")
                        else:
                            raise AssertionError("submitted native receipt record was deleted")
                    else:
                        cloned = frappe.copy_doc(doc)
                        expect("denied", lambda: cloned.insert(ignore_permissions=True))
                require(fresh() == initial, f"direct maintenance attempt {operation} changed native quantity")
    print("Revoked Receive and warehouse grants reject replay. Direct native create, update, cancel and delete are blocked.",
          flush=True)


def unmanaged_draft(company, parent):
    from haxpower_v1.warehouse.test_warehouse import make_native

    before = fresh()
    with connection() as database:
        location = make_native(company, parent)
        item = frappe.get_doc(
            doctype="Item", item_code=f"SYNTHETIC-UNMANAGED-{uuid4().hex}", item_group=PREFIX,
            stock_uom=PREFIX, is_stock_item=1,
        ).insert(ignore_permissions=True)
        entry = frappe.get_doc(
            doctype=native.ENTRY, company=company, stock_entry_type=native.PURPOSE, purpose=native.PURPOSE,
            naming_series="MAT-STE-.YYYY.-", items=[{
                "item_code": item.name, "t_warehouse": location.name, "uom": PREFIX,
                "stock_uom": PREFIX, "qty": 1, "conversion_factor": 1, "allow_zero_valuation_rate": 1,
            }],
        ).insert(ignore_permissions=True)
        require(entry.docstatus == 0, "native guard blocked an entirely unmanaged Stock Entry draft")
        database.rollback()
    require(fresh() == before, "unmanaged draft probe persisted changes")


def verify_native_precision(payload, actor):
    with actor_session(actor, payload.company), service.synthetic_candidate(payload.company, limits=LIMITS):
        draft = service.create_draft(payload)
    frappe.db.set_default("float_precision", "2")
    frappe.db.commit()
    try:
        before = fresh()
        with actor_session(actor, payload.company), service.synthetic_candidate(payload.company, limits=LIMITS):
            expect("RECEIPT_NATIVE_PRECISION_UNCONFIGURED", lambda: service.submit(
                SubmitReceipt(draft, uuid4().hex),
            ))
        require(fresh() == before, "insufficient native precision changed stock or requests")
    finally:
        frappe.db.set_default("float_precision", "3")
        frappe.db.commit()


def ensure_fiscal_year(date):
    year = date.year
    fiscal = f"Synthetic Receipt {year}"
    if not frappe.db.exists("Fiscal Year", fiscal):
        frappe.get_doc(doctype="Fiscal Year", year=fiscal, year_start_date=f"{year}-01-01",
                       year_end_date=f"{year}-12-31").insert()


def run(site):
    ensure_fiscal_year(getdate())
    if not frappe.db.exists("Stock Entry Type", native.PURPOSE):
        frappe.get_doc(doctype="Stock Entry Type", purpose=native.PURPOSE, is_standard=1).insert(
            set_name=native.PURPOSE,
        )
    settings = frappe.get_single("System Settings")
    settings.float_precision = "3"
    settings.language = settings.language or "en"
    settings.time_zone = settings.time_zone or "UTC"
    settings.save()
    frappe.db.commit()
    require(str(frappe.db.get_default("float_precision")) == "3", "ERPNext quantity default is not three decimals")
    company, parent = _company_and_parent()
    unmanaged_draft(company, parent)
    warehouses = warehouse.list_warehouses(company=company)
    a, b = [warehouse.list_locations(record.id)[0].id for record in warehouses[:2]]
    first, second = _sku_id("A"), _sku_id("B")
    ungranted = warehouse.list_locations(warehouses[2].id)[0].id
    transit_warehouse = frappe.db.get_value("HP V1 Warehouse", {"company": company, "code": "TRANSIT"})
    transit = frappe.db.get_value(LOCATION, {"warehouse": transit_warehouse})
    require(transit, "synthetic system transit is missing")
    pieces = whole_unit_sku(first)
    actor, _ = synthetic_user(f"v108-{uuid4().hex[:10]}", "receipt", Operation.RECEIVE)
    set_scope(actor, company, Mode.SELECTED, [row.id for row in warehouses[:2]])
    frappe.db.commit()
    lines = (
        ReceiptLineInput(first, a, STD61, "1.125", PREFIX),
        ReceiptLineInput(first, a, STD61, "0.125", PREFIX),
        ReceiptLineInput(first, a, STD62, "2.375", PREFIX),
        ReceiptLineInput(first, a, NO_BATCH, "1", PREFIX),
        ReceiptLineInput(second, b, STD61, "3.25", PREFIX),
        ReceiptLineInput(second, b, NO_BATCH, "2.5", PREFIX),
    )
    payload = ReceiptDraftInput(company, lines, reason="Synthetic mixed receipt")
    verify_native_precision(payload, actor)
    with actor_session(actor, company):
        expect("RECEIPT_NOT_ENABLED", lambda: service.create_draft(payload))
        with service.synthetic_candidate(company, limits=LIMITS):
            invalid_drafts(payload, ungranted, transit)
            recovered_result, recovered_draft, initial = faults(payload)
            checked_result(recovered_result, recovered_draft, initial, actor)
            draft = service.create_draft(payload)
            command = SubmitReceipt(draft, uuid4().hex)
            before = fresh()
            result = service.submit(command)
            equivalent_lines = tuple(replace(line, quantity=line.quantity + (
                "0" if "." in line.quantity else ".0"
            )) for line in draft.lines)
            require(service.submit(replace(command, draft=replace(draft, lines=equivalent_lines))) == result,
                    "equivalent Decimal idempotent replay differs")
            expect("STOCK_IDEMPOTENCY_CONFLICT", lambda: service.submit(
                replace(command, draft=replace(draft, reason="Changed reason"))))
            expect("STOCK_DOCUMENT_ALREADY_EFFECTIVE", lambda: service.submit(
                replace(command, idempotency_key=uuid4().hex)))
    posted = checked_result(result, draft, before, actor)
    verify_guards(payload, actor, command, posted, warehouses)
    compete(site, actor, payload, same_key=False)
    compete(site, actor, payload, same_key=True)
    with actor_session(actor, company):
        pc_line = ReceiptLineInput(pieces, a, NO_BATCH, "0.5", PIECES)
        pc_receipt = ReceiptDraftInput(company, (pc_line,))
        with service.synthetic_candidate(company, limits={PIECES: "50"}):
            initial = fresh()
            expect("STOCK_QUANTITY_PRECISION", lambda: service.create_draft(pc_receipt))
            require(fresh() == initial, "fractional PC wrote receipt data")
            pc_draft = service.create_draft(replace(pc_receipt, lines=(replace(pc_line, quantity="2"),)))
            initial = fresh()
            pc_result = service.submit(SubmitReceipt(pc_draft, uuid4().hex))
        with service.synthetic_candidate(company, limits={}):
            expect("STOCK_QUANTITY_LIMIT_UNCONFIGURED", lambda: service.create_draft(
                replace(pc_receipt, lines=(replace(pc_line, quantity="2"),))))
    checked_result(pc_result, pc_draft, initial, actor)
    accounting_candidate(payload)
    print("Native Material Receipt: six lines, five buckets, same-bucket duplicates, two SKUs and two leaves. "
          "ERPNext Batch, Bundle, effective SLE and Bin agree. Zero candidate: zero GL rows.", flush=True)


def whole_unit_sku(first):
    from haxpower_v1.adapters.erpnext import item
    from haxpower_v1.catalog import sku
    from haxpower_v1.catalog.sku_dto import BrandData, SKUData
    from haxpower_v1.catalog.test_sku import make_uom

    if existing := frappe.db.get_value(SKU, {"code": PIECES}, "name"):
        return existing
    reference = frappe.get_doc(SKU, first)
    unit = make_uom(PIECES, whole=True)
    frappe.conf[item.SETTINGS] = {"item_group": reference.native_item_group, "uom_precision": {unit: 0}}
    brand = sku.save_brand(BrandData(code=PIECES, display_name="Synthetic Receipt PC"))
    result = sku.create_sku(SKUData(code=PIECES, specification=reference.specification, brand=brand.id,
                                   display_name="Synthetic Receipt Whole Item", stock_uom=unit, quantity_precision=0))
    frappe.db.commit()
    return result.id


def accounting_candidate(payload):
    from datetime import timedelta

    from erpnext.accounts.doctype.accounting_period import accounting_period
    from erpnext.stock import stock_ledger
    from erpnext.stock.doctype.stock_ledger_entry.stock_ledger_entry import StockFreezeError
    from haxpower_v1.warehouse.test_warehouse import make_company
    from frappe.utils import now_datetime

    run_id = uuid4().hex[:5].upper()
    company, parent = make_company(f"R{run_id}")
    master = frappe.get_doc("Company", company)
    master.enable_perpetual_inventory = 1
    master.default_inventory_account = frappe.db.get_value(
        "Account", {"company": company, "account_type": "Stock", "is_group": 0},
    )
    master.stock_adjustment_account = frappe.db.get_value(
        "Account", {"company": company, "account_type": "Stock Adjustment", "is_group": 0},
    )
    master.save(ignore_permissions=True)
    require(master.default_inventory_account and master.stock_adjustment_account and master.cost_center,
            "native perpetual inventory defaults are missing")
    logical = warehouse.create_warehouse(company=company, native_parent=parent, code="W1", display_name="Synthetic GL")
    leaf = warehouse.create_location(logical.id, code="A1", display_name="Synthetic GL A1")
    actor, _ = synthetic_user(f"v108-gl-{run_id.lower()}", "receipt", Operation.RECEIVE)
    set_scope(actor, company, Mode.SELECTED, [logical.id])
    frappe.db.commit()

    old_line = payload.lines[0]
    line = replace(old_line, location_id=leaf.id, quantity="2")
    payload = ReceiptDraftInput(company, (line,))
    rate = Decimal("4.25")
    verify_posting = native._verify_posting
    replayed = []

    def revalue(scope, *args, **kwargs):
        fields = ("name", "item_code", "warehouse", "voucher_type", "voucher_no", "actual_qty",
                  "qty_after_transaction", "serial_and_batch_bundle", "posting_date", "posting_time", "creation")
        sle, = frappe.db.get_values(native.SLE, {"voucher_type": native.ENTRY, "voucher_no": scope.entry.name},
                                    fields, as_dict=True, for_update=True)
        pair = (sle.item_code, sle.warehouse)
        before = native._balances(company, {pair})
        batches = native._batch_totals({leg.batch for leg in scope.legs})
        stock_ledger.update_entries_after({
            "item_code": sle.item_code, "warehouse": sle.warehouse, "posting_date": sle.posting_date,
            "posting_time": sle.posting_time, "voucher_type": sle.voucher_type, "voucher_no": sle.voucher_no,
            "sle_id": sle.name, "creation": sle.creation, "reserved_stock": 0,
        }, allow_negative_stock=False)
        require(
            frappe.db.get_value(native.SLE, sle.name, fields, as_dict=True, for_update=True) == sle
            and native._balances(company, {pair}) == before and native._batch_totals(batches) == batches,
            "native valuation replay changed quantity facts",
        )
        replayed.append(True)
        return verify_posting(scope, *args, **kwargs)

    with actor_session(actor, company):
        with service.synthetic_candidate(company, limits=LIMITS, rate=rate):
            initial = fresh()
            expect("denied", lambda: service.create_draft(replace(payload, lines=(old_line,))))
            require(fresh() == initial, "cross-company target changed persisted state")
            draft = service.create_draft(payload)
            command = SubmitReceipt(draft, uuid4().hex)
            initial = fresh()
            expect("RECEIPT_GL_NOT_ALLOWED", lambda: service.submit(command))
            require(fresh() == initial, "rejected GL left stock or accounting entries")
        with (service.synthetic_candidate(company, limits=LIMITS, rate=rate, allow_gl=True),
              patch.object(native, "_verify_posting", revalue)):
            result = service.submit(command)
    posted = checked_result(result, draft, initial, actor, expected_gl=2)
    sle, = posted[native.SLE]
    gl = posted["GL Entry"]
    require(replayed == [True] and Decimal(str(sle.incoming_rate)) == rate
            and Decimal(str(sle.stock_value_difference)) == Decimal("8.50")
            and {row.account for row in gl} == {master.default_inventory_account, master.stock_adjustment_account}
            and sum(Decimal(str(row.debit)) for row in gl) == Decimal("8.50")
            and sum(Decimal(str(row.credit)) for row in gl) == Decimal("8.50"),
            "native valuation and GL amounts differ")

    today = now_datetime()
    period = frappe.get_doc(
        doctype="Accounting Period", period_name=f"Synthetic Receipt Close {run_id}", company=company,
        start_date=today.date(), end_date=today.date(),
        closed_documents=[{"document_type": native.ENTRY, "closed": 1}],
    ).insert()
    frappe.db.commit()
    seen = []
    original_check = accounting_period.validate_accounting_period_on_doc_save

    def confirm_period(doc, method=None):
        try:
            return original_check(doc, method)
        except accounting_period.ClosedAccountingPeriod:
            seen.append("closed")
            raise

    with actor_session(actor, company), service.synthetic_candidate(company, limits=LIMITS, rate=rate, allow_gl=True):
        closed = SubmitReceipt(service.create_draft(payload), uuid4().hex)
        initial = fresh()
        with patch.object(accounting_period, "validate_accounting_period_on_doc_save", confirm_period):
            expect("STOCK_PERSISTENCE_FAILED", lambda: service.submit(closed))
        require(seen == ["closed"] and fresh() == initial, "native accounting closure failed to roll back")
    frappe.delete_doc(period.doctype, period.name)
    historical_date = today - timedelta(days=1)
    ensure_fiscal_year(historical_date.date())

    def frozen_setting():
        return frappe.db.sql(
            "SELECT value FROM `tabSingles` WHERE doctype=%s AND field=%s",
            ("Stock Settings", "stock_frozen_upto"),
        )

    # The Date getter coerces an unset value. Preserve the exact row so
    # a restored site does not acquire a synthetic historical cutoff.
    previous_frozen = frozen_setting()
    frappe.db.set_single_value("Stock Settings", "stock_frozen_upto", historical_date.date().isoformat(),
                               update_modified=False)
    frappe.clear_document_cache("Stock Settings", "Stock Settings")
    frappe.db.commit()
    from erpnext.stock.doctype.stock_ledger_entry import stock_ledger_entry

    original_freeze = stock_ledger_entry.check_stock_frozen_date

    def confirm_frozen(posting_date):
        try:
            return original_freeze(posting_date)
        except StockFreezeError:
            seen.append("frozen")
            raise

    try:
        with actor_session(actor, company), service.synthetic_candidate(company, limits=LIMITS, rate=rate, allow_gl=True):
            historical = SubmitReceipt(service.create_draft(payload), uuid4().hex)
            initial = fresh()
            with (patch.object(native, "now_datetime", return_value=historical_date),
                  patch.object(stock_ledger_entry, "check_stock_frozen_date", confirm_frozen)):
                expect("STOCK_PERSISTENCE_FAILED", lambda: service.submit(historical))
            require(seen == ["closed", "frozen"] and fresh() == initial, "native stock freeze failed to roll back")
    finally:
        if previous_frozen:
            frappe.db.set_single_value("Stock Settings", "stock_frozen_upto", previous_frozen[0][0],
                                       update_modified=False)
        else:
            frappe.db.delete("Singles", {"doctype": "Stock Settings", "field": "stock_frozen_upto"})
        frappe.clear_document_cache("Stock Settings", "Stock Settings")
        frappe.db.commit()
    require(frozen_setting() == previous_frozen, "historical probe did not restore the exact Stock Settings field")
    print("Perpetual valuation: 2 units at 4.25, voucher GL debit and credit each 8.50. "
          "Unexpected GL, closed period and frozen history rolled back. Native valuation replay preserved quantity facts.",
          flush=True)
