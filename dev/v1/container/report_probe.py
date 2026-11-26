"""Cookie HTTP, native history, XLSX and authorization regression for V1-15."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from io import BytesIO
import json
from threading import Thread
from time import monotonic
from uuid import uuid4
from werkzeug.serving import make_server
from zipfile import ZipFile

import frappe
import requests
from frappe.app import application
from openpyxl import load_workbook

from haxpower_v1.access.config import set_scope
from haxpower_v1.adapters.erpnext import receipt as native_receipt
from haxpower_v1.access.policy import Mode, Operation
from haxpower_v1.inventory.operations.transaction import connection
from haxpower_v1.inventory.read import diagnostics
from haxpower_v1.inventory.read.query import Page
from haxpower_v1.inventory.read.service import Inventory, inventory_data
from haxpower_v1.inventory.receipt import service as receipts
from haxpower_v1.inventory.transfer import dto as transfer, service as transfers
from haxpower_v1.reporting import exports, workbook
from haxpower_v1.warehouse import service as warehouses

from access_probe import QuietHandler, synthetic_user
from approval_probe import Fixture
from identity import require
from stock_probe import actor_session, session
from count_probe import finish
from receipt_probe import ensure_fiscal_year
from warehouse_probe import _company_and_parent

RPC = "/api/method/haxpower_v1."


@contextmanager
def site_http(site):
    server = make_server("127.0.0.1", 0, application, request_handler=QuietHandler)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    with ExitStack() as resources:
        def request(session, command, *, body=None, params=None, expected=200, absolute=False, headers=None):
            path = command if absolute else RPC + command
            response = session.request("POST" if body is not None else "GET",
                                       f"http://127.0.0.1:{server.server_port}{path}",
                                       json=body, params=params, timeout=90, allow_redirects=False, headers=headers)
            expected = expected if type(expected) is tuple else (expected,)
            require(response.status_code in expected, f"{command}: {response.status_code}, expected {expected}; "
                    + (response.text[:1400] if response.status_code >= 400 else ""))
            return response

        def client(identity=None):
            session = resources.enter_context(requests.Session())
            session.trust_env = False
            session.headers.update({"Host": site, "Accept": "application/json"})
            if identity:
                request(session, "/api/method/login", body=dict(usr=identity[0], pwd=identity[1]), absolute=True)
                value = request(session, "access.api.csrf_token").json()["message"]
                session.headers["X-Frappe-CSRF-Token"] = value["csrf_token"]
            return session

        try:
            yield request, client
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)


def result(request, session, method, **kwargs):
    response = request(session, method, **kwargs)
    require("no-store" in response.headers.get("Cache-Control", ""), "private response may be cached")
    return response.json()["message"]


def xlsx_values(response):
    book = load_workbook(BytesIO(response.content), read_only=True)
    try:
        return list(book.active.values)
    finally:
        book.close()


def run(site):
    fixture = Fixture()
    fixture.toggle(False)
    with fixture.operate():
        receipts.submit(fixture.receipt_command("12"))
        receipts.submit(fixture.receipt_command("3", batch={"mode": "BATCH", "batchNo": "STD61"}))
        receipts.submit(fixture.receipt_command("8", source=fixture.target))
        command = fixture.transfer_command("4")
        ready = transfers.submit(command)
        sent = transfers.dispatch(replace(command, draft=replace(command.draft, revision=ready.revision)))
    first, second = fixture.warehouses
    prefix = uuid4().hex[:10]
    selected = synthetic_user(prefix, "read-selected", Operation.INVENTORY_READ, Operation.EXPORT)
    all_user = synthetic_user(prefix, "read-all", Operation.INVENTORY_READ)
    product = synthetic_user(prefix, "read-product", Operation.PRODUCT_READ)
    only_export = synthetic_user(prefix, "read-export-only", Operation.EXPORT)
    other = synthetic_user(prefix, "read-other", Operation.INVENTORY_READ, Operation.EXPORT)
    set_scope(selected[0], fixture.company, Mode.SELECTED, [first])
    set_scope(all_user[0], fixture.company, Mode.ALL)
    set_scope(other[0], fixture.company, Mode.SELECTED, [first])
    frappe.db.commit()

    verify_snapshot(site, fixture, selected[0])
    started = monotonic()
    with site_http(site) as (request, client):
        authorized, full, product_reader, exporter, stranger = (client(user) for user in (
            selected, all_user, product, only_export, other,
        ))
        request(client(), "inventory.read.api.stock", expected=403)
        request(product_reader, "inventory.read.api.stock", expected=403)
        request(exporter, "reporting.api.generate", body={"report": "stock"}, expected=403)
        request(full, "reporting.api.generate", body={"report": "stock"}, expected=403)
        request(authorized, "access.api.get_sku", params={"sku_id": fixture.sku}, expected=403)
        products = result(request, product_reader, "access.api.search_products",
                          params={"criteria": json.dumps({"query": "ｓｙｎｔｈｅｔｉｃ　合成"}), "page": 1, "pageSize": 1})
        require(products["total"] >= 2 and len(products["rows"]) == 1, "normalized AND/pagination failed")
        require("physical" not in products["rows"][0], "directory revealed stock")
        criteria = {"sku_id": fixture.sku, "location_id": fixture.source}
        response = result(request, authorized, "inventory.read.api.stock", params={"criteria": json.dumps(criteria)})
        row, = response["rows"]
        require(row["id"] == fixture.sku and Decimal(row["physical"]) == 11 and Decimal(row["in_transit"]) == 0,
                "one-sided summary included transit or hidden quantities")
        all_visible = result(request, full, "inventory.read.api.stock", params={"criteria": json.dumps(criteria)})
        require(Decimal(all_visible["rows"][0]["in_transit"]) == 4, "dual-scope transit missing")
        request(authorized, "inventory.read.api.document", expected=403, params={"document_id": sent.transfer_id})
        detail = result(request, full, "inventory.read.api.document", params={"document_id": sent.transfer_id})
        require(detail["transfers"][0]["remaining"] == "4", "transfer lifecycle projection incorrect")
        request(authorized, "inventory.read.api.stock", expected=403,
                params={"criteria": json.dumps({"warehouse_id": second})})
        explicit = {**criteria, "batch": None}
        page = result(request, authorized, "inventory.read.api.distribution", params={"criteria": json.dumps(explicit)})
        batch, = page["rows"]
        require(batch["batch"] is None and Decimal(batch["available"]) == 8, "NO_BATCH filter leaked another bucket")
        ledger = result(request, authorized, "inventory.read.api.ledger", params={"criteria": json.dumps(explicit)})
        physical_leg, = [entry for entry in ledger["rows"] if entry["document_id"] == sent.transfer_id]
        require(Decimal(physical_leg["quantity"]) == -4 and fixture.target not in str(ledger),
                "ledger did not isolate the local transfer leg")
        today = frappe.utils.nowdate()
        period = result(request, authorized, "inventory.read.api.period",
                        params={"criteria": json.dumps({**explicit, "start": today, "end": today})})
        rollup, = period["rows"]
        require(Decimal(rollup["closing"]) == (Decimal(rollup["opening"]) + Decimal(rollup["increase"])
                                                - Decimal(rollup["decrease"])) == 8, "period does not reconcile")
        request(authorized, "reporting.api.generate", body={"report": "distribution"}, expected=(400, 403),
                headers={"X-Frappe-CSRF-Token": None})
        receipt = result(request, authorized, "reporting.api.generate",
                         body={"report": "distribution", "criteria": explicit})
        require(receipt["status"] == "Ready", f"export generation failed: {receipt}")
        identity = receipt["export_id"]
        request(stranger, "reporting.api.download", expected=403, body={"export_id": identity})
        preserved = result(request, stranger, "reporting.api.generate",
                           body={"report": "distribution", "criteria": explicit})
        require(preserved["status"] == "Ready", "retained-permission recovery fixture did not generate")
        request(authorized, f"/private/haxpower_v1/exports/{identity}.xlsx", absolute=True, expected=(403, 404))
        request(authorized, f"/private/files/{identity}.xlsx", absolute=True, expected=(403, 404))
        request(authorized, "/api/resource/HP%20V1%20Export", absolute=True, expected=403)
        binary = request(authorized, "reporting.api.download", body={"export_id": identity})
        require("no-store" in binary.headers.get("Cache-Control", ""), "download can be cached")
        output = xlsx_values(binary)
        columns = output[0]
        require(len(output) == 2 and output[1][columns.index("batch")] == "无批次"
                and Decimal(output[1][columns.index("physical")]) == Decimal(batch["physical"]),
                "Excel quantities differ from employee distribution")
        with fixture.operate():
            dispatch = transfers.get_dispatch(fixture.company, sent.transfer_id)
            transfer_id = dispatch.lines[0].transfer_line_id
            partial = transfers.receive(transfer.ReceiveTransfer(
                fixture.company, sent.transfer_id, sent.revision, uuid4().hex,
                (transfer.TransferQuantity(transfer_id, "2"),),
            ))
            transfers.return_remaining(transfer.ReturnTransfer(
                fixture.company, sent.transfer_id, partial.revision, uuid4().hex,
                (transfer.TransferQuantity(transfer_id, "2"),), "Synthetic V1-15 return",
            ))
            # Use the same clean native Dynamic Link cache as the V1-12 probe.
            frappe.clear_cache()
            count = finish(fixture.count_task(difference=-3))
        detail = result(request, full, "inventory.read.api.document", params={"document_id": sent.transfer_id})
        transfer_row = detail["transfers"][0]
        require(transfer_row["status"] == "Closed with Return" and Decimal(transfer_row["remaining"]) == 0
                and Decimal(transfer_row["received"]) + Decimal(transfer_row["returned"]) == 4,
                "received/returned lifecycle did not close")
        ledger = result(request, authorized, "inventory.read.api.ledger", params={"criteria": json.dumps(explicit)})
        net = [Decimal(item["quantity"]) for item in ledger["rows"] if item["document_id"] == count.document_id]
        require(net == [Decimal(-3)], "native count did not collapse into a single net business movement")
        set_scope(selected[0], fixture.company, Mode.SELECTED, [second])
        frappe.db.commit()
        request(authorized, "reporting.api.download", expected=403, body={"export_id": identity})
        set_scope(selected[0], fixture.company, Mode.SELECTED, [first])
        frappe.db.commit()
        role = frappe.db.get_value("Has Role", {"parent": selected[0], "role": Operation.EXPORT.role}, "name")
        frappe.db.delete("Has Role", {"name": role})
        frappe.db.commit()
        request(authorized, "reporting.api.status", expected=403, params={"export_id": identity})
        request(authorized, "reporting.api.download", expected=403, body={"export_id": identity})

        _, native_parent = _company_and_parent()
        w4 = warehouses.create_warehouse(company=fixture.company, native_parent=native_parent,
                                         code=f"W4-{uuid4().hex[:8].upper()}", display_name="Synthetic fourth warehouse")
        fourth = warehouses.create_location(w4.id, code="PRIMARY", display_name="Synthetic fourth warehouse location")
        frappe.db.commit()
        expanded = {"sku_id": fixture.sku, "warehouse_id": w4.id, "include_zero": True}
        output = result(request, full, "inventory.read.api.stock", params={"criteria": json.dumps(expanded)})
        require(output["total"] == 1 and output["rows"][0]["physical"] == "0", "ALL did not include a new warehouse")
        request(authorized, "inventory.read.api.stock", params={"criteria": json.dumps(expanded)}, expected=403)
        expanded["location_id"] = fourth.id
        expanded["company"] = "Unauthorized synthetic company"
        request(full, "inventory.read.api.stock", params={"criteria": json.dumps(expanded)}, expected=403)

    with actor_session(fixture.manager, fixture.company):
        outcome = diagnostics.reconcile(fixture.company, fixture.sku, fixture.source)
    require(outcome["status"] == "Verified" and outcome["bucket_count"] >= 2, f"reconciliation failed: {outcome}")
    verify_reconciliation_faults(fixture)
    check_workbook()
    measure_catalog()
    print(f"V1-15 HTTP roles, warehouse scope, native snapshots, XLSX and revocation passed in "
          f"{monotonic() - started:.2f}s.", flush=True)


def verify_snapshot(site, fixture, reader):
    def write():
        with session(site, fixture.operator, fixture.company), fixture.candidates():
            receipts.submit(fixture.receipt_command("1", source=fixture.target))

    def physical():
        item, = Inventory({"sku_id": fixture.sku, "location_id": fixture.target}).stock()
        return Decimal(item["physical"])

    # Use a dual-scope employee for this independent snapshot.
    scope_id = frappe.db.get_value("HP V1 Warehouse Access", {"user": reader}, "name")
    require(bool(scope_id), "missing read fixture warehouse access")
    set_scope(reader, fixture.company, Mode.SELECTED, fixture.warehouses)
    frappe.db.commit()
    try:
        with actor_session(reader, fixture.company), ThreadPoolExecutor(max_workers=1) as workers:
            with connection(snapshot=True):
                old = physical()
                workers.submit(write).result(timeout=30)
                require(physical() == old, "writer changed totals inside a read snapshot")
            with connection(snapshot=True):
                require(physical() == old + 1, "fresh snapshot did not read committed writer")
    finally:
        set_scope(reader, fixture.company, Mode.SELECTED, [fixture.warehouses[0]])
        frappe.db.commit()


def verify_reconciliation_faults(fixture):
    leaf = frappe.db.get_value("HP V1 Warehouse Location", fixture.source, "native_warehouse")
    native_item = frappe.db.get_value("HP V1 Stock Item", fixture.sku, "native_item")
    bin_id, qty = frappe.db.get_value("Bin", {"warehouse": leaf, "item_code": native_item},
                                      ("name", "actual_qty"))
    sle, bundle = frappe.db.get_value("Stock Ledger Entry", {"warehouse": leaf, "item_code": native_item},
                                     ("name", "serial_and_batch_bundle"))
    part, bundle_qty = frappe.db.get_value("Serial and Batch Entry", {"parent": bundle}, ("name", "qty"))
    cases = (
        ("Bin", "actual_qty", bin_id, qty, qty + 1, "BIN"),
        ("Serial and Batch Entry", "qty", part, bundle_qty, bundle_qty + 1, "SLE_ALLOCATION"),
        ("Stock Ledger Entry", "serial_and_batch_bundle", sle, bundle, None, "BUNDLE_ENTRIES"),
    )
    for table, field, identity, original, corrupt, check in cases:
        try:
            frappe.db.set_value(table, identity, field, corrupt, update_modified=False)
            frappe.db.commit()
            with actor_session(fixture.manager, fixture.company):
                outcome = diagnostics.reconcile(fixture.company)
            require(outcome["status"] == "Inconsistent" and outcome["evidence"].get("check") == check,
                    f"reconciliation missed synthetic {check}: {outcome}")
        finally:
            frappe.db.set_value(table, identity, field, original, update_modified=False)
            frappe.db.commit()
    print("V1-15 synthetic Bin, Bundle allocation and detached SLE faults failed closed.", flush=True)


def measure_catalog():
    import tracemalloc
    from unittest.mock import patch
    from haxpower_v1.catalog import read as catalog
    from haxpower_v1.catalog.sku import search_sku_ids
    from haxpower_v1.catalog.sku_persistence import SKU

    name = frappe.db.get_value(SKU, {}, "name")
    fields = ("name", "code", "display_name", "description", "specification", "brand", "stock_uom",
              "quantity_precision", "search_text", "status", "native_item")
    seed = frappe.db.get_value(SKU, name, fields, as_dict=True)
    group = f"SYNTHETIC-PERF-{uuid4().hex[:8].upper()}"
    point = "catalog_performance"
    frappe.db.savepoint(point)
    try:
        values = []
        for number in range(1000):
            code = f"{group}-{number:04}"
            row = dict(seed, name=uuid4().hex, code=code, display_name="合成活塞环 320",
                       search_text=f"{code.casefold()} 合成活塞环 320", native_item=code)
            values.append(tuple(row[field] for field in fields))
        frappe.db.bulk_insert(SKU, fields, values)
        sql = frappe.db.sql
        plans, query_times = [], []

        def explain(statement, *args, **kwargs):
            compiled = str(statement)
            if compiled.startswith("SELECT sku.name"):
                plans.extend(sql("EXPLAIN " + compiled, args[0]))
            started = monotonic()
            result = sql(statement, *args, **kwargs)
            query_times.append(monotonic() - started)
            return result

        tracemalloc.start()
        started = monotonic()
        with patch.object(frappe.db, "sql", side_effect=explain):
            ids, total = search_sku_ids(f"{group} 活塞环 320", limit=100, offset=900)
            products = catalog.by_ids(ids, details=True)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        duration = monotonic() - started
        require(total == 1000 and len(products) == 100, "thousand-SKU scale page mismatch")
        print(f"V1-15 catalog scale: 1000 fixture SKUs, offset=900, size=100, wall={duration:.3f}s, "
              f"DB={sum(query_times):.3f}s/{len(query_times)} statements, Python peak={peak / 1024:.0f} KiB, "
              f"EXPLAIN={[(row[2], row[3], row[5], row[8]) for row in plans]}.", flush=True)
        ids, total = search_sku_ids(f"{group} 活塞环 320")
        products = catalog.by_ids(ids, details=True)
        stream = BytesIO()
        tracemalloc.start()
        started = monotonic()
        workbook.write(stream, "products", (products[identity] for identity in ids), deadline=started + 30)
        elapsed = monotonic() - started
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        file_size = stream.tell()
        book = load_workbook(stream, read_only=True)
        try:
            count = sum(1 for _ in book.active.values) - 1
            require(count == total == 1000, "thousand-SKU workbook lost or duplicated rows")
        finally:
            book.close()
        print(f"V1-15 workbook scale: rows={count}, XLSX={file_size} bytes, "
              f"writer={elapsed:.3f}s, Python peak={peak / 1024:.0f} KiB.", flush=True)
    finally:
        frappe.db.rollback(save_point=point)


def measure_native_history(site):
    """Cost and conservation on a multi-year slice of actual native postings."""
    from unittest.mock import patch
    import tracemalloc

    from haxpower_v1.adapters.erpnext import item
    from haxpower_v1.catalog import sku
    from haxpower_v1.catalog.sku_dto import SKUData
    from haxpower_v1.catalog.sku_persistence import SKU

    fixture = Fixture()
    fixture.toggle(False)
    base = frappe.db.get_value(SKU, fixture.sku, ("specification", "brand", "native_item_group", "stock_uom",
                                                  "quantity_precision"), as_dict=True)
    code = f"SYNTHETIC-REPORT-HISTORY-{uuid4().hex[:10].upper()}"
    with patch.dict(frappe.conf, {item.SETTINGS: {
        "item_group": base.native_item_group, "uom_precision": {base.stock_uom: base.quantity_precision},
    }}):
        fixture.sku = sku.create_sku(SKUData(code=code, specification=base.specification, brand=base.brand,
                                             display_name="Synthetic native report history", stock_uom=base.stock_uom,
                                             quantity_precision=base.quantity_precision)).id
    year = frappe.utils.getdate().year
    calendar = [datetime(yr, month, 15, 12)
                for yr in range(year - 3, year) for month in (1, 4, 7, 10)]
    for date in calendar[::4]:
        if not frappe.db.exists("Fiscal Year", {
            "year_start_date": ("<=", date.date()), "year_end_date": (">=", date.date()), "disabled": 0,
        }):
            ensure_fiscal_year(date)
    reader = synthetic_user(uuid4().hex[:10], "read-history", Operation.INVENTORY_READ)
    set_scope(reader[0], fixture.company, Mode.SELECTED, [fixture.warehouses[0]])
    frappe.db.commit()
    lines_per_posting = 16
    with fixture.operate():
        for date in calendar:
            # Change only this synthetic adapter's posting timestamp. ERPNext
            # still creates the native document, SLE, Bundle and Bin.
            with patch.object(native_receipt, "now_datetime", return_value=date):
                receipts.submit(fixture.receipt_command("1.125", lines=lines_per_posting))
    criteria = dict(sku_id=fixture.sku, location_id=fixture.source, batch=None,
                    start=calendar[0].date().isoformat(), end=calendar[-1].date().isoformat())
    expected = Decimal("1.125") * len(calendar) * lines_per_posting
    with actor_session(reader[0], fixture.company):
        tracemalloc.start()
        started = monotonic()
        ledger = inventory_data("ledger", criteria, Page(100, 0))[0]
        period = inventory_data("period", criteria, Page(100, 0))[0]
        duration = monotonic() - started
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    require(ledger["total"] == len(calendar) * lines_per_posting, "multi-year SLE page lost business rows")
    bucket, = period["rows"]
    require(Decimal(bucket["opening"]) == Decimal(bucket["decrease"]) == 0
            and Decimal(bucket["increase"]) == Decimal(bucket["closing"]) == expected,
            "multi-year report disagrees with native posting totals")
    print(f"V1-15 native history: {ledger['total']} effective SLE across three posting years, "
          f"fixed page=100 and period rollup, wall={duration:.3f}s, Python peak={peak / 1024:.0f} KiB.",
          flush=True)


def check_workbook():
    values = ("=HYPERLINK(\"https://example.invalid\")", "+SUM(1,1)", "-1+2", "@SUM(1,1)",
              "\x01https://example.invalid", "12345678901234567890.123456789")
    stream = BytesIO()
    workbook.write(stream, "batches", [
        {"sku_id": value, "batch": None, "batch_id": None} for value in values
    ], deadline=monotonic() + 10)
    with ZipFile(stream) as archive:
        require(not any("externalLink" in name or name.endswith("vbaProject.bin") for name in archive.namelist()),
                "unexpected Excel external link or macro")
    book = load_workbook(stream)
    try:
        observed = list(book.active["A"])[1:]
        require(all(cell.data_type == "s" and cell.hyperlink is None for cell in observed)
                and [cell.value for cell in observed] == [workbook.text(value) for value in values],
                "Excel mutation, hyperlink or formula found")
    finally:
        book.close()


def verify_recovered(site):
    from haxpower_v1.access.policy import granted_operations

    records = frappe.get_all(exports.EXPORT, filters={"report": "distribution", "status": "Ready"},
                             fields=["name", "company", "requested_by", "file_size", "sha256"])
    retained, revoked = [], []
    for row in records:
        data = exports.private_files.read(row.name, row.file_size, row.sha256)
        require(data.startswith(b"PK"), "recovery lost the validated Excel contents")
        with actor_session(row.requested_by, row.company):
            if Operation.EXPORT in granted_operations():
                _, recovered = exports.download(row.name)
                require(recovered == data, "restored authorized workbook differs")
                retained.append(row)
            else:
                try:
                    exports.download(row.name)
                except frappe.PermissionError:
                    revoked.append(row)
                else:
                    raise AssertionError("recovery resurrected revoked export permissions")
    require(bool(retained) and bool(revoked), "missing retained and revoked recovery fixtures")
    require(all(frappe.db.exists(exports.DOWNLOAD, {"export_id": row.name, "actor": row.requested_by})
                for row in revoked), "recovery lost download audit for revoked owners")
    with site_http(site) as (request, client):
        request(client(), "reporting.api.download", body={"export_id": retained[0].name}, expected=403)
    print("V1-15 restored Excel contents, download audit, retained access and revocation denial.", flush=True)
