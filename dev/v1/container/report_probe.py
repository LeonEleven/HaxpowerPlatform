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
    set_scope(product[0], fixture.company, Mode.SELECTED, [first])
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
        verify_product_permissions(fixture, request, client, authorized, product_reader)
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


def verify_product_permissions(fixture, request, client, stock_reader, product_reader):
    """Exercise catalog fields, result selection and workbook revocation on the same cookies."""
    from unittest.mock import patch

    from haxpower_v1.adapters.erpnext import item
    from haxpower_v1.catalog import service, sku
    from haxpower_v1.catalog.dto import TEXT, AttributeData, CategoryData, SpecificationData, TemplateData
    from haxpower_v1.catalog.sku_dto import Alias, Identifier, SKUData
    from haxpower_v1.catalog.sku_persistence import SKU

    prefix = uuid4().hex[:10].upper()
    code = f"SYNTHETIC-FIELD-{prefix}"
    term = f"ATTR-{prefix}"
    category = service.save_category(CategoryData(code=code, display_name=f"CATEGORY-{prefix}"))
    attribute = service.save_attribute(AttributeData(code=code, display_name="Synthetic field", data_type=TEXT))
    service.save_template(TemplateData(category=category.id, attribute=attribute.id, searchable=True))
    base = frappe.db.get_value(SKU, fixture.sku, ("brand", "native_item_group", "stock_uom", "quantity_precision"),
                               as_dict=True)
    formal = hasattr(service, "create_fixture_specification")
    legacy_spec, legacy_sku = f"OLDSPEC-{prefix}", f"OLDSKU-{prefix}"
    with patch.dict(frappe.conf, {item.SETTINGS: {
        "item_group": base.native_item_group, "uom_precision": {base.stock_uom: base.quantity_precision},
    }}):
        if formal:
            from haxpower_v1.catalog.dto import HistoricalIdentifier

            spec = service.create_specification(
                SpecificationData(stock_uom=base.stock_uom, category=category.id, display_name=f"SPEC-{prefix}"),
                attributes={attribute.id: [term]},
                historical_identifiers=[HistoricalIdentifier(source="Synthetic", value=legacy_spec)],
            )
            identifiers = [Identifier(kind="LEGACY", value=legacy_sku, source="Synthetic")]
            sku_fields = {}
        else:
            spec = service.create_specification(
                SpecificationData(code=code, category=category.id, display_name=f"SPEC-{prefix}"),
                attributes={attribute.id: [term]},
            )
            identifiers = []
            sku_fields = {"code": code}
        product = sku.create_sku(
            SKUData(specification=spec.id, brand=base.brand, display_name="合成可读",
                    description=f"DESCRIPTION-{prefix}", stock_uom=base.stock_uom,
                    quantity_precision=base.quantity_precision, **sku_fields),
            identifiers=[Identifier(kind="OEM", value=f"IDENTIFIER-{prefix}"), *identifiers],
            aliases=[Alias(value=f"ALIAS-{prefix}")],
        )
    native_id = frappe.db.get_value(SKU, product.id, "native_item")
    dual_identity = synthetic_user(prefix, "read-combined", Operation.INVENTORY_READ,
                                   Operation.PRODUCT_READ, Operation.EXPORT)
    set_scope(dual_identity[0], fixture.company, Mode.SELECTED, [fixture.warehouses[0]])
    frappe.db.commit()
    with fixture.operate():
        receipt = receipts.submit(fixture.receipt_command("2", sku=product.id))

    dual = client(dual_identity)
    criteria = {"sku_id": product.id, "location_id": fixture.source}
    date = frappe.utils.nowdate()

    def report(session, name, selection, expected=200):
        response = request(session, f"inventory.read.api.{name}", params={"criteria": json.dumps(selection)},
                           expected=expected)
        return response.json().get("message")

    def generate(session, name, selection):
        issued = result(request, session, "reporting.api.generate", body={"report": name, "criteria": selection})
        require(issued["status"] == "Ready" and issued["rows"] >= 1, "field coverage export did not generate")
        return issued["export_id"]

    def download(session, identity, expected=200):
        return request(session, "reporting.api.download", body={"export_id": identity}, expected=expected)

    directory = result(request, product_reader, "access.api.get_sku", params={"sku_id": product.id})
    require(term in json.dumps(directory) and not ({"physical", "available", "in_transit"} & directory.keys()),
            "product-only detail lost technical fields or exposed inventory")
    if formal:
        require(native_id != product.data.code and native_id not in json.dumps(directory)
                and legacy_spec in json.dumps(directory) and legacy_sku in json.dumps(directory),
                "employee product DTO lost historical identifiers or leaked native Item identity")
        for query in (spec.data.code, legacy_spec, legacy_sku, product.data.code, f"合成可读 {term} {legacy_spec}"):
            found = result(request, product_reader, "access.api.search_products", params={"criteria": json.dumps({"query": query})})
            require(any(row["id"] == product.id for row in found["rows"]), "formal or historical search lost a match")
    report(product_reader, "stock", criteria, expected=403)
    request(stock_reader, "access.api.product_filters", expected=403)
    detail = result(request, stock_reader, "inventory.read.api.document", params={"document_id": receipt.document_id})
    require(term not in json.dumps(detail) and "specification" not in json.dumps(detail),
            "stock document leaked product fields")

    basic = report(stock_reader, "stock", {**criteria, "query": "合成可读"})
    basic_row, = basic["rows"]
    require(basic["total"] == 1 and Decimal(basic_row["physical"]) == 2 and "specification" not in basic_row,
            "basic keyword search lost quantity or returned technical fields")
    secrets = (term, f"SPEC-{prefix}", f"CATEGORY-{prefix}", f"DESCRIPTION-{prefix}",
               f"IDENTIFIER-{prefix}", f"ALIAS-{prefix}")
    if formal:
        secrets += (legacy_spec, legacy_sku, native_id)
    for secret in secrets:
        require(report(stock_reader, "stock", {**criteria, "query": secret})["total"] == 0,
                "stock-only keyword matched a restricted catalog field")

    exact = {**criteria, "attributes": {attribute.id: term}}
    for name in ("stock", "distribution", "batches", "transit", "ledger", "period", "documents", "corrections"):
        selection = {**criteria, "start": date, "end": date} if name == "period" else criteria
        report(stock_reader, name, {**selection, "attributes": exact["attributes"]}, expected=403)
        require(report(stock_reader, name, {**selection, "query": term})["total"] == 0,
                f"{name} used restricted keyword matches for an inventory-only employee")
    report(stock_reader, "stock", {**criteria, "category": category.id}, expected=403)
    request(stock_reader, "reporting.api.generate", body={"report": "stock", "criteria": exact}, expected=403)

    combined = {**exact, "query": f"合成可读　{term.lower()}" + (f" {legacy_spec}" if formal else "")}
    full = report(dual, "stock", combined)
    full_row, = full["rows"]
    require(full["total"] == 1 and term in json.dumps(full_row["specification"]),
            "combined permission search lost its AND semantics or technical projection")
    report_id = generate(dual, "stock", combined)
    basic_id = generate(stock_reader, "stock", criteria)
    plain_id = generate(dual, "distribution", criteria)
    filtered_id = generate(dual, "distribution", combined)

    full_sheet = xlsx_values(download(dual, report_id))
    basic_sheet = xlsx_values(download(stock_reader, basic_id))
    filtered_sheet = xlsx_values(download(dual, filtered_id))
    require("specification" not in filtered_sheet[0], "distribution unexpectedly exported technical columns")
    frappe.db.rollback()
    require("specification" in full_sheet[0] and term in str(full_sheet[1])
            and "specification" not in basic_sheet[0] and term not in str(basic_sheet[1]),
            "Excel field visibility differs from the corresponding employee page")
    if formal:
        require(legacy_spec in str(full_sheet[1]), "authorized stock workbook omitted specification history")
        require(native_id not in str(full_sheet) and native_id not in str(basic_sheet)
                and "HP-NB-" not in str(filtered_sheet), "employee workbook exposed a native Item or Batch")
    manifest = json.loads(frappe.db.get_value(exports.EXPORT, report_id, "columns_json"))
    coverage = json.loads(frappe.db.get_value(exports.EXPORT, filtered_id, "columns_json"))
    require(manifest["columns"] == list(full_sheet[0]) and manifest["product_columns"] == ["specification"]
            and coverage["product_columns"] == [] and coverage["uses_product_search"],
            "export did not record actual columns and technical search dependency")

    # An owner with both read roles still must regenerate an unclassified workbook.
    original = frappe.db.get_value(exports.EXPORT, report_id, "columns_json")
    try:
        frappe.db.set_value(exports.EXPORT, report_id, "columns_json",
                            json.dumps({"columns": list(full_sheet[0]), "version": 1}), update_modified=False)
        frappe.db.commit()
        request(dual, "reporting.api.status", params={"export_id": report_id}, expected=403)
        download(dual, report_id, expected=403)
    finally:
        frappe.db.set_value(exports.EXPORT, report_id, "columns_json", original, update_modified=False)
        frappe.db.commit()

    roles = {"parent": dual_identity[0], "role": Operation.PRODUCT_READ.role}
    require(frappe.db.exists("Has Role", roles), "missing product role before revocation")
    frappe.db.delete("Has Role", roles)
    frappe.db.commit()
    for identity in (report_id, filtered_id):
        request(dual, "reporting.api.status", params={"export_id": identity}, expected=403)
        download(dual, identity, expected=403)
    result(request, dual, "reporting.api.status", params={"export_id": plain_id})
    download(dual, plain_id)
    require("specification" not in report(dual, "stock", criteria)["rows"][0],
            "the same cookie retained technical fields after revocation")

    checksum = frappe.db.get_value(exports.EXPORT, plain_id, "sha256")
    try:
        frappe.db.set_value(exports.EXPORT, plain_id, "sha256", "0" * 64, update_modified=False)
        frappe.db.commit()
        rejected = download(dual, plain_id, expected=417)
        require("PRIVATE_FILE_UNAVAILABLE" in rejected.text, "download failed without its integrity error")
    finally:
        frappe.db.set_value(exports.EXPORT, plain_id, "sha256", checksum, update_modified=False)
        frappe.db.commit()

    if formal:
        verify_formal_native_flow(product.id)

    print("V1-15 Cookie HTTP field isolation, AND search, workbook provenance, revocation and integrity passed.",
          flush=True)


def verify_formal_native_flow(sku_id):
    from haxpower_v1.inventory.issue import service as issues
    from correction_probe import Fixture as CorrectionFixture
    from transfer_followup_probe import payload

    fixture = CorrectionFixture()
    fixture.sku = sku_id
    fixture.toggle(False)
    with fixture.operate():
        receipt = receipts.submit(fixture.receipt_command("5.125"))
    correction = fixture.check(fixture.submit(receipt, ("0.125",)))
    require(correction.operation_id, "the independent native Item missed its correction history")
    with fixture.operate():
        issue = issues.submit(fixture.issue_command("0.375"))
        command = fixture.transfer_command("1.25")
        ready = transfers.submit(command)
        sent = transfers.dispatch(replace(command, draft=replace(command.draft, revision=ready.revision)))
        dispatched = transfers.get_dispatch(fixture.company, sent.transfer_id)
        received = transfers.receive(payload(dispatched, (0, "0.5")))
        frappe.clear_cache()
        counted = finish(fixture.count_task(difference=Decimal("0.125")))
        require(issue.operation_id and received.operation_id and counted.operation_id,
                "formal SKU did not complete issue, partial transfer and native count")
    print("Formal SKU independent Item passed fractional receipt, correction, issue, transfer and counting.", flush=True)


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
        fixture.sku = getattr(sku, "create_fixture_sku", sku.create_sku)(SKUData(code=code, specification=base.specification, brand=base.brand,
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

    records = frappe.get_all(exports.EXPORT, filters={"report": ("in", ["distribution", "stock"]), "status": "Ready"},
                             fields=["name", "company", "requested_by", "file_size", "sha256", "columns_json"])
    retained, revoked = [], []
    for row in records:
        data = exports.private_files.read(row.name, row.file_size, row.sha256)
        require(data.startswith(b"PK"), "recovery lost the validated Excel contents")
        manifest = json.loads(row.columns_json)
        required = {Operation.EXPORT, Operation.INVENTORY_READ}
        if manifest["product_columns"] or manifest["uses_product_search"]:
            required.add(Operation.PRODUCT_READ)
        with actor_session(row.requested_by, row.company):
            if required <= granted_operations():
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
