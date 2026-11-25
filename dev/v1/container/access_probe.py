"""Exercise access with real cookies against the isolated Frappe web stack."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
import re
from secrets import token_urlsafe
from threading import Barrier, Thread
from urllib.parse import quote
from uuid import uuid4

import frappe
import requests
from frappe.app import application
from werkzeug.serving import WSGIRequestHandler, make_server

from haxpower_v1.access.config import set_scope
from haxpower_v1.access.policy import Mode, Operation, SCOPE, SCOPE_ROW, authorized_warehouse_ids
from haxpower_v1.warehouse import service
from haxpower_v1.warehouse.test_warehouse import make_company, make_native, stock_counts

from identity import require
from sku_probe import PREFIX
from warehouse_probe import _company_and_parent


BUSINESS = "/api/method/haxpower_v1.access.api."
STOCK_ROLES = ("Stock User", "Stock Manager", "Item Manager", "Purchase Manager", "Sales Manager")
BASELINE_USER = "v1-access-v105-baseline-existing@example.invalid"


def snapshot():
    if not frappe.db.table_exists(SCOPE):
        return {}
    tables = {
        doctype: {
            "schema": frappe.db.sql(f"SHOW CREATE TABLE `tab{doctype}`")[0][1],
            "rows": frappe.get_all(doctype, fields=["*"], order_by="name"),
        } if frappe.db.table_exists(doctype) else None
        for doctype in (SCOPE, SCOPE_ROW)
    }
    roles = [operation.role for operation in Operation]
    return {
        "tables": tables,
        "roles": frappe.get_all("Role", filters={"name": ("in", roles)}, fields=["name", "desk_access", "disabled"],
                                order_by="name"),
        "assignments": frappe.get_all("Has Role", filters={"parenttype": "User", "role": ("in", roles)},
                                      fields=["parent", "role"], order_by="parent, role"),
    }


class QuietHandler(WSGIRequestHandler):
    def log_request(self, code="-", size="-"):
        # Matrix assertions print identifiers and status codes only.
        pass


def synthetic_user(run_id, label, *operations, roles=(), enabled=1):
    email = f"v1-access-{run_id.lower()}-{label}@example.invalid"
    password = token_urlsafe(24) + "_1@aA"
    frappe.get_doc(
        doctype="User", email=email, first_name="Synthetic access", new_password=password, enabled=enabled,
        user_type="System User", send_welcome_email=0,
        roles=[{"role": role} for role in ("Employee", *roles, *(operation.role for operation in operations))],
    ).insert(ignore_permissions=True)
    return email, password


def seed_existing_grant():
    company, _ = _company_and_parent()
    warehouse = service.list_warehouses(company=company)[0]
    user, _ = synthetic_user("v105-baseline", "existing", Operation.WAREHOUSE_READ, Operation.INVENTORY_READ)
    set_scope(user, company, Mode.SELECTED, [warehouse.id])
    frappe.db.commit()
    print("Merged V1-05 synthetic employee capability roles and selected warehouse grant persisted.")


def verify_existing_grant():
    company, _ = _company_and_parent()
    warehouse = service.list_warehouses(company=company)[0]
    frappe.set_user(BASELINE_USER)
    try:
        require(authorized_warehouse_ids(Operation.INVENTORY_READ) == (warehouse.id,),
                "pre-upgrade authorization no longer resolves the same warehouse")
    finally:
        frappe.set_user("Administrator")
    print("Pre-upgrade employee capability and warehouse selection verified after upgrade.")


def verify_scope_concurrency(site, user, company, warehouse_id):
    barrier = Barrier(2)

    def save(mode, ids):
        frappe.init(site=site, sites_path=str(Path.cwd()))
        frappe.connect()
        try:
            # Both connections observe a missing grant before the first write.
            require(not frappe.db.exists(SCOPE, {"user": user}), "unexpected preexisting grant")
            barrier.wait(timeout=20)
            set_scope(user, company, mode, ids)
            frappe.db.commit()
        finally:
            frappe.destroy()

    with ThreadPoolExecutor(max_workers=2) as workers:
        pending = (
            workers.submit(save, Mode.ALL, ()),
            workers.submit(save, Mode.SELECTED, (warehouse_id,)),
        )
        for job in pending:
            job.result(timeout=30)

    frappe.db.rollback()
    matches = frappe.get_all(SCOPE, filters={"user": user}, pluck="name")
    require(len(matches) == 1, "concurrent configuration persisted duplicate grants")
    doc = frappe.get_doc(SCOPE, matches[0])
    expected = [warehouse_id] if doc.mode == Mode.SELECTED else []
    require(doc.mode in (Mode.SELECTED, Mode.ALL) and [row.warehouse for row in doc.warehouses] == expected,
            "concurrent configuration mixed mode and warehouse rows")
    print("Two independent MariaDB connections wrote one user scope. A third verified one complete configuration.")


def verify_http(site):
    from probe import snapshot as domain_snapshot

    run_id = uuid4().hex[:8].upper()
    company, parent = make_company(f"AC{run_id[:3]}")
    other_company, other_parent = make_company(f"OT{run_id[:3]}")

    def new_warehouse(code, *, different_company=False):
        return service.create_warehouse(
            company=other_company if different_company else company,
            native_parent=other_parent if different_company else parent,
            code=code, display_name=f"Synthetic {code}",
        )

    w1, w2, w3 = [new_warehouse(code) for code in ("W1", "W2", "W3")]
    other = new_warehouse("OTHER", different_company=True)
    first = service.create_location(w1.id, code="L1", display_name="Visible")
    second = service.create_location(w1.id, code="L2", display_name="Visible 2")
    hidden = service.create_location(w2.id, code="HIDDEN", display_name="Hidden")
    _, transit = service.provision_system_transit(
        company=company, native_parent=parent, warehouse_code="TRANSIT", location_code="T1", display_name="Transit",
    )
    leaf = service.diagnose_mapping(first.id).native_warehouse
    unmanaged = make_native(company, parent).name
    managed_item = f"{PREFIX}-A"
    sku = frappe.db.get_value("HP V1 Stock Item", {"native_item": managed_item}, ("name", "specification"), as_dict=True)
    require(sku, "missing baseline SKU")
    from haxpower_v1.inventory.batch.persistence import MAPPING
    from haxpower_v1.inventory.operations.persistence import DOCUMENT, LOG, REQUEST
    from stock_probe import snapshot as stock_snapshot

    batch_mapping = frappe.db.get_value(
        MAPPING, {"kind": "NO_BATCH", "sku": sku.name}, ("name", "native_batch"), as_dict=True,
    )
    require(batch_mapping, "missing synthetic batch for HTTP boundary verification")
    stock_document = frappe.db.get_value(DOCUMENT, {"status": "Effective"})
    require(stock_document, "missing committed synthetic stock document for HTTP verification")
    all_user = synthetic_user(run_id, "all", Operation.WAREHOUSE_READ, Operation.INVENTORY_READ)
    selected = synthetic_user(run_id, "selected", Operation.WAREHOUSE_READ, Operation.TRANSFER, Operation.PRODUCT_READ)
    operator = synthetic_user(run_id, "operator", Operation.WAREHOUSE_MAINTAIN, Operation.RECEIVE, Operation.ISSUE, Operation.COUNT)
    approver = synthetic_user(run_id, "approver", Operation.APPROVE, Operation.WAREHOUSE_READ)
    empty = synthetic_user(run_id, "empty", Operation.WAREHOUSE_READ)
    absent = synthetic_user(run_id, "absent", Operation.WAREHOUSE_READ)
    privileged = synthetic_user(run_id, "native-roles", Operation.WAREHOUSE_READ, roles=STOCK_ROLES)
    disabled = synthetic_user(run_id, "disabled", Operation.WAREHOUSE_READ, enabled=0)
    set_scope(all_user[0], company, Mode.ALL)
    set_scope(selected[0], company, Mode.SELECTED, [w1.id])
    set_scope(operator[0], company, Mode.ALL)
    set_scope(approver[0], company, Mode.NONE)
    set_scope(empty[0], company, Mode.SELECTED)
    set_scope(privileged[0], company, Mode.SELECTED, [w1.id])
    set_scope(disabled[0], company, Mode.ALL)
    frappe.db.commit()

    server = make_server("127.0.0.1", 0, application, request_handler=QuietHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    outcomes = []

    def call(session, label, method, path, *, body=None, params=None, status=200):
        result = session.request(method, f"http://127.0.0.1:{server.server_port}{path}", json=body,
                                 params=params, timeout=30, allow_redirects=False)
        allowed = status if isinstance(status, tuple) else (status,)
        require(result.status_code in allowed, f"HTTP {label}: expected {allowed}, got {result.status_code}"
                + (f" ({result.json().get('exc_type', '')})" if result.headers.get("Content-Type", "").startswith(
                    "application/json") else ""))
        outcomes.append((label, result.status_code))
        return result

    def make_session(stack):
        session = stack.enter_context(requests.Session())
        session.trust_env = False
        session.headers.update({"Host": site, "Accept": "application/json"})
        return session

    def login(stack, identity, label, *, desk=False):
        session = make_session(stack)
        call(session, f"{label} login", "POST", "/api/method/login", body={"usr": identity[0], "pwd": identity[1]})
        response = call(session, f"{label} session", "GET", "/api/method/frappe.auth.get_logged_user")
        require(response.json().get("message") == identity[0], "HTTP call did not resume authenticated cookie")
        if desk:
            page = call(session, f"{label} Desk boot", "GET", "/desk")
            token = re.search(r'frappe\.csrf_token = "([a-zA-Z0-9]+)";', page.text)
            require(token, "Desk did not return a session CSRF token")
            session.headers["X-Frappe-CSRF-Token"] = token[1]
        return session

    def business(session, label, command, status=200, **params):
        return call(session, label, "GET", BUSINESS + command, params=params, status=status).json().get("message")

    def listed(session, expected, label, **params):
        response = business(session, label, "list_warehouses", **params)
        require([row["id"] for row in response["rows"]] == [warehouse.id for warehouse in expected],
                f"unexpected warehouse IDs for {label}")
        require(all(set(row) == {"id", "code", "display_name"} for row in response["rows"]),
                "warehouse projection leaked internal fields")
        return response["total"]

    def inventory_snapshot():
        return {table:frappe.get_all(table,fields=['*'],order_by='name') for table in (
            'HP V1 Inventory Epoch','HP V1 Inventory Version','HP V1 Stock Count','HP V1 Stock Count Line')
            if frappe.db.table_exists(table)}

    def denial_matrix(session, selected_session, operator_session):
        escaped_leaf = quote(leaf, safe="")
        native_item = quote(managed_item, safe="")
        scope_type = quote(SCOPE, safe="")
        native_batch = batch_mapping.native_batch
        batch_url = quote(native_batch, safe="")
        batch_doc = {"doctype": "Batch", "name": native_batch, "disabled": 1}
        batch_input = {"item": managed_item, "batch_id": "HP-RB-synthetic-denied"}
        batch_cases = (
            ("REST v1 Batch create", "POST", "/api/resource/Batch", batch_input),
            ("REST v1 Batch read", "GET", f"/api/resource/Batch/{batch_url}", None),
            ("REST v1 Batch update", "PUT", f"/api/resource/Batch/{batch_url}", {"disabled": 1}),
            ("REST v2 Batch read", "GET", f"/api/v2/document/Batch/{batch_url}", None),
            ("REST v2 Batch update", "PATCH", f"/api/v2/document/Batch/{batch_url}", {"disabled": 1}),
            ("REST v2 Batch delete", "DELETE", f"/api/v2/document/Batch/{batch_url}", None),
            ("REST mapping detail", "GET", f"/api/v2/document/{quote(MAPPING, safe='')}/{batch_mapping.name}", None),
            ("client Batch set value", "POST", "/api/method/frappe.client.set_value",
             {"doctype": "Batch", "name": native_batch, "fieldname": "disabled", "value": 1}),
            ("client Batch rename", "POST", "/api/method/frappe.client.rename_doc",
             {"doctype": "Batch", "old_name": native_batch, "new_name": "synthetic-denied"}),
            ("client Batch bulk insert", "POST", "/api/method/frappe.client.insert_many",
             {"docs": [dict(batch_input, doctype="Batch")]}),
            ("client Batch bulk update", "POST", "/api/method/frappe.client.bulk_update", {"docs": [batch_doc]}),
            ("Desk Batch detail", "POST", "/api/method/frappe.desk.form.load.getdoc",
             {"doctype": "Batch", "name": native_batch}),
            ("Desk Batch save", "POST", "/api/method/frappe.desk.form.save.savedocs",
             {"doc": frappe.as_json(batch_doc), "action": "Save"}),
            ("internal Batch service", "POST", "/api/method/haxpower_v1.inventory.batch.service.resolve_or_create",
             {"sku_id": sku.name, "selection": {"mode": "NO_BATCH"}}),
        )
        for label, method, path, body in batch_cases:
            response = call(session, label, method, path, body=body, status=403)
            require(native_batch not in response.text and "HP-RB-synthetic-denied" not in response.text
                    and batch_mapping.name not in response.text, "native Batch or mapping leaked into an error")

        cases = (
            ("REST v1 managed Item detail", "GET", f"/api/resource/Item/{native_item}", None),
            ("REST v2 managed Item detail", "GET", f"/api/v2/document/Item/{native_item}", None),
            ("REST v1 managed Item update", "PUT", f"/api/v1/resource/Item/{native_item}", {"description": "Denied"}),
            ("REST v2 managed Warehouse update", "PATCH", f"/api/v2/document/Warehouse/{escaped_leaf}",
             {"disabled": 1}),
            ("REST v2 managed Item delete", "DELETE", f"/api/v2/document/Item/{native_item}", None),
            ("REST v2 managed Warehouse delete", "DELETE", f"/api/v2/document/Warehouse/{escaped_leaf}", None),
            ("REST Company stock defaults", "GET", f"/api/resource/Company/{quote(company, safe='')}", None),
            ("REST scope list", "GET", f"/api/resource/{scope_type}", None),
            ("REST scope write", "POST", f"/api/v2/document/{scope_type}", {"mode": "ALL"}),
            ("REST Batch create", "POST", "/api/v2/document/Batch", {"item": managed_item, "batch_id": "HP-NB-TEST"}),
            ("REST Stock Entry create", "POST", "/api/resource/Stock%20Entry",
             {"items": [{"item_code": managed_item, "t_warehouse": leaf}]}),
            ("REST Reconciliation create", "POST", "/api/v2/document/Stock%20Reconciliation",
             {"items": [{"item_code": managed_item, "warehouse": leaf}]}),
            ("REST SLE list", "GET", "/api/resource/Stock%20Ledger%20Entry", None),
            ("REST Bin count", "GET", "/api/v2/doctype/Bin/count", None),
            ("REST Bundle list", "GET", "/api/v2/document/Serial%20and%20Batch%20Bundle", None),
            ("REST Purchase Receipt write", "POST", "/api/v2/document/Purchase%20Receipt", {"set_warehouse": leaf}),
            ("REST Purchase Invoice write", "POST", "/api/resource/Purchase%20Invoice", {"update_stock": 1}),
            ("REST Delivery Note write", "POST", "/api/resource/Delivery%20Note", {"set_warehouse": leaf}),
            ("REST Sales Invoice write", "POST", "/api/v2/document/Sales%20Invoice", {"update_stock": 1}),
            ("client get Item", "POST", "/api/method/frappe.client.get", {"doctype": "Item", "name": managed_item}),
            ("client set Warehouse", "POST", "/api/v2/method/frappe.client.set_value",
             {"doctype": "Warehouse", "name": leaf, "fieldname": "disabled", "value": 1}),
            ("client delete Item", "POST", "/api/method/frappe.client.delete", {"doctype": "Item", "name": managed_item}),
            ("client rename Item", "POST", "/api/method/frappe.client.rename_doc",
             {"doctype": "Item", "old_name": managed_item, "new_name": "DENIED-RENAME"}),
            ("client bulk insert", "POST", "/api/method/frappe.client.insert_many",
             {"docs": [{"doctype": "Stock Entry", "to_warehouse": leaf}]}),
            ("client bulk update", "POST", "/api/method/frappe.client.bulk_update",
             {"docs": [{"doctype": "Item", "name": managed_item, "description": "Denied"}]}),
            ("Desk Item form", "POST", "/api/method/frappe.desk.form.load.getdoc",
             {"doctype": "Item", "name": managed_item}),
            ("Desk Item save", "POST", "/api/method/frappe.desk.form.save.savedocs",
             {"doc": frappe.as_json({"doctype": "Item", "name": managed_item, "description": "Denied"}), "action": "Save"}),
            ("Desk Stock Entry submit", "POST", "/api/method/frappe.desk.form.save.savedocs",
             {"doc": frappe.as_json({"doctype": "Stock Entry", "to_warehouse": leaf}), "action": "Submit"}),
            ("Desk Stock Entry cancel", "POST", "/api/method/frappe.desk.form.save.cancel",
             {"doctype": "Stock Entry", "name": "SYNTHETIC-DRAFT"}),
            ("Desk bulk delete", "POST", "/api/method/frappe.desk.reportview.delete_items",
             {"doctype": "Item", "items": frappe.as_json([managed_item])}),
            ("Desk Stock Ledger report", "POST", "/api/method/frappe.desk.query_report.run",
             {"report_name": "Stock Ledger", "filters": frappe.as_json({"company": company})}),
            ("Desk Stock Balance report", "POST", "/api/method/frappe.desk.query_report.run",
             {"report_name": "Stock Balance", "filters": frappe.as_json({"company": company})}),
            ("Prepared Report list", "GET", "/api/v2/document/Prepared%20Report", None),
            ("Prepared Report create", "POST", "/api/method/frappe.core.doctype.prepared_report.prepared_report.make_prepared_report",
             {"report_name": "Stock Balance", "filters": "{}"}),
            ("Prepared Report attachment", "GET",
             "/api/method/frappe.core.doctype.prepared_report.prepared_report.download_attachment?dn=SYNTHETIC", None),
            ("Print view", "GET", f"/printview?doctype=Item&name={native_item}", None),
            ("File list", "GET", "/api/resource/File", None),
            ("Private file download", "GET", "/private/files/v1-recovery-sample.txt", None),
            ("Report view export", "POST", "/api/method/frappe.desk.reportview.export_query", {"doctype": "Item"}),
            ("Query Report export", "POST", "/api/method/frappe.desk.query_report.export_query",
             {"report_name": "Stock Ledger"}),
            ("Data Import", "POST", "/api/resource/Data%20Import", {"reference_doctype": "Item"}),
            ("Repost queue", "POST", "/api/resource/Repost%20Item%20Valuation", {"item_code": managed_item}),
            ("Company cleanup", "POST", "/api/resource/Transaction%20Deletion%20Record", {"company": company}),
        )
        for label, method, path, body in cases:
            call(session, label, method, path, body=body, status=403)
        call(selected_session, "query employee Stock Entry write", "POST", "/api/resource/Stock%20Entry",
             body={"to_warehouse": leaf}, status=403)
        stock_type = quote(DOCUMENT, safe="")
        for label, method, path, body in (
            ("stock document REST read", "GET", f"/api/resource/{stock_type}/{stock_document}", None),
            ("stock document REST update", "PATCH", f"/api/v2/document/{stock_type}/{stock_document}", {"status": "Draft"}),
            ("stock document REST delete", "DELETE", f"/api/v2/document/{stock_type}/{stock_document}", None),
            ("stock request list", "GET", f"/api/resource/{quote(REQUEST, safe='')}", None),
            ("operation log list", "GET", f"/api/v2/document/{quote(LOG, safe='')}", None),
            ("internal stock executor", "POST", "/api/method/haxpower_v1.inventory.operations.synthetic.execute",
             {"command": {"document_id": stock_document}}),
            ("internal receipt draft", "POST", "/api/method/haxpower_v1.inventory.receipt.service.create_draft", {}),
            ("internal receipt submit", "POST", "/api/method/haxpower_v1.inventory.receipt.service.submit", {}),
            ("internal issue draft", "POST", "/api/method/haxpower_v1.inventory.issue.service.create_draft", {}),
            ("internal issue submit", "POST", "/api/method/haxpower_v1.inventory.issue.service.submit", {}),
            ("internal issue availability", "POST", "/api/method/haxpower_v1.inventory.issue.service.get_availability", {}),
            ("internal transfer draft", "POST", "/api/method/haxpower_v1.inventory.transfer.service.create_draft", {}),
            ("internal transfer dispatch", "POST", "/api/method/haxpower_v1.inventory.transfer.service.dispatch", {}),
            ("internal transfer receive", "POST", "/api/method/haxpower_v1.inventory.transfer.service.receive", {}),
            ("internal transit return", "POST", "/api/method/haxpower_v1.inventory.transfer.service.return_remaining", {}),
            ("internal transfer detail", "POST", "/api/method/haxpower_v1.inventory.transfer.service.get_dispatch", {}),
            ("count task list", "GET", "/api/resource/HP%20V1%20Stock%20Count", None),
            ("count line create", "POST", "/api/resource/HP%20V1%20Stock%20Count%20Line", {}),
            ("inventory epoch create", "POST", "/api/resource/HP%20V1%20Inventory%20Epoch", {}),
            ("inventory version create", "POST", "/api/v2/document/HP%20V1%20Inventory%20Version", {}),
            *((f"internal count {method}", "POST", f"/api/method/haxpower_v1.inventory.stock_count.service.{method}", {})
              for method in ('create','get','record','discover','recount','complete')),
            ("native count helper", "POST", "/api/method/erpnext.stock.doctype.stock_reconciliation.stock_reconciliation.get_stock_balance_for", {}),
            ("native count REST create", "POST", "/api/resource/Stock%20Reconciliation", {}),
            ("native count Desk submit", "POST", "/api/method/frappe.desk.form.save.savedocs",
             {"doc": frappe.as_json({"doctype":"Stock Reconciliation","company":company}), "action":"Submit"}),
            ("internal transit ownership", "GET", "/api/resource/HP%20V1%20Transit%20Ownership", {}),
            ("native End Transit mapper", "POST",
             "/api/method/erpnext.stock.doctype.stock_entry.stock_entry.make_stock_in_entry", {}),
            ("REST Material Issue create", "POST", "/api/resource/Stock%20Entry",
             {"purpose": "Material Issue", "items": [{"item_code": managed_item, "s_warehouse": leaf}]}),
            ("Desk Material Issue submit", "POST", "/api/method/frappe.desk.form.save.savedocs",
             {"doc": frappe.as_json({"doctype": "Stock Entry", "purpose": "Material Issue",
                                     "items": [{"item_code": managed_item, "s_warehouse": leaf}]}), "action": "Submit"}),
        ):
            call(operator_session, label, method, path, body=body, status=403)

    try:
        with ExitStack() as stack:
            all_session = login(stack, all_user, "all")
            selected_session = login(stack, selected, "selected")
            operator_session = login(stack, operator, "operator")
            approver_session = login(stack, approver, "approver")
            empty_session = login(stack, empty, "empty")
            absent_session = login(stack, absent, "absent")
            native_session = login(stack, privileged, "native roles", desk=True)
            anonymous = make_session(stack)
            call(anonymous, "disabled account login", "POST", "/api/method/login",
                 body={"usr": disabled[0], "pwd": disabled[1]}, status=(401, 403))
            business(anonymous, "anonymous warehouse query", "list_warehouses", status=403)
            call(anonymous, "anonymous native write", "POST", "/api/resource/Item", body={}, status=403)
            require(listed(all_session, [w1, w2, w3], "ALL before W4") == 3, "incorrect initial ALL total")
            require(listed(selected_session, [w1], "SELECTED before W4") == 1, "incorrect initial SELECTED total")
            w4 = new_warehouse("W4")
            frappe.db.commit()
            require(listed(all_session, [w4], "ALL after W4 with offset", limit=1, offset=3) == 4,
                    "ALL did not apply scope before count")
            require(listed(selected_session, [], "SELECTED search for W4", query="W4") == 0,
                    "SELECTED search counted inaccessible W4")
            for session, label in ((approver_session, "NONE"), (empty_session, "empty SELECTED"), (absent_session, "missing")):
                require(listed(session, [], label) == 0, "ungranted scope returned rows")
            business(operator_session, "maintenance role alone cannot query", "list_warehouses", status=403)
            for key, row in (("other company", other), ("unselected", w2)):
                business(all_session if row == other else selected_session, key, "get_warehouse",
                         warehouse_id=row.id, status=403)
            for key, row in (("unselected location", hidden), ("system Transit", transit)):
                business(selected_session, key, "get_location", location_id=row.id, status=403)
            business(selected_session, "unknown location", "get_location", location_id="0" * 32, status=403)
            locations = business(selected_session, "scoped Location count and offset", "list_locations",
                                 warehouse_id=w1.id, offset=1, limit=1)
            require(locations["total"] == 2 and locations["rows"] == [
                {"id": second.id, "warehouse_id": w1.id, "code": "L2", "display_name": "Visible 2"}
            ], "location projection leaked a hidden mapping or incorrect count")
            product = business(selected_session, "product summary", "get_sku", sku_id=sku.name)
            require(set(product) == {"id", "code", "display_name"}, "unapproved product fields leaked")
            require(batch_mapping.native_batch not in str(product), "product API exposed a native Batch identity")
            business(selected_session, "specification summary", "get_specification", specification_id=sku.specification)
            business(all_session, "warehouse role cannot view product", "get_sku", sku_id=sku.name, status=403)

            # Compare all mapped domain/native rows and raw stock counts across denied requests.
            before = domain_snapshot(), stock_counts(), frappe.db.count("Version"), stock_snapshot(), inventory_snapshot()
            denial_matrix(native_session, all_session, operator_session)
            frappe.db.rollback()
            require((domain_snapshot(), stock_counts(), frappe.db.count("Version"), stock_snapshot(), inventory_snapshot()) == before,
                    "denied requests left a persistent side effect")

            for prefix in ("/api/resource", "/api/v2/document"):
                warehouses = call(native_session, f"{prefix} unmanaged Warehouse list", "GET", f"{prefix}/Warehouse",
                                  params={"filters": frappe.as_json({"name": ["in", [leaf, unmanaged]]})}).json()["data"]
                require(warehouses == [{"name": unmanaged}], "native listing returned a managed Warehouse")
                items = call(native_session, f"{prefix} managed Item filtering", "GET", f"{prefix}/Item",
                             params={"filters": frappe.as_json({"name": managed_item})}).json()["data"]
                require(not items, "native listing returned a managed Item")
            desk_list = call(native_session, "Desk filtered Warehouse list", "POST",
                             "/api/method/frappe.desk.reportview.get_list",
                             body={"doctype": "Warehouse", "view": "List", "fields": ["name"],
                                   "filters": {"name": ["in", [leaf, unmanaged]]}}).json()["message"]
            require(desk_list == [{"name": unmanaged}], "Desk list returned a managed Warehouse")
            client_list = call(native_session, "frappe.client filtered Item list", "GET",
                               "/api/method/frappe.client.get_list",
                               params={"doctype": "Item", "filters": frappe.as_json({"name": managed_item})})
            require(client_list.json()["message"] == [], "frappe.client returned a managed Item")
            item_count = call(native_session, "native Item count", "GET", "/api/v2/doctype/Item/count",
                              params={"filters": frappe.as_json({"name": managed_item})}).json()["data"]
            require(item_count == 0, "native count included a managed Item")
            call(native_session, "update unmanaged Warehouse", "PATCH",
                 f"/api/v2/document/Warehouse/{quote(unmanaged, safe='')}", body={"city": "Synthetic maintained city"})
            unmanaged_item = f"SYNTHETIC-V1-ACCESS-{run_id}-UNMANAGED"
            call(native_session, "create unmanaged Item", "POST", "/api/resource/Item", body={
                "item_code": unmanaged_item, "item_name": "Synthetic unmanaged", "stock_uom": PREFIX,
                "item_group": PREFIX, "is_stock_item": 0,
            })
            call(native_session, "edit unmanaged Item from client", "POST", "/api/method/frappe.client.set_value",
                 body={"doctype": "Item", "name": unmanaged_item, "fieldname": "description", "value": "Synthetic edit"})

            set_scope(selected[0], company, Mode.NONE)
            frappe.db.commit()
            business(selected_session, "same session after scope revoked", "get_warehouse", warehouse_id=w1.id, status=403)
            user = frappe.get_doc("User", all_user[0])
            user.set("roles", [row for row in user.roles if row.role != Operation.WAREHOUSE_READ.role])
            user.save(ignore_permissions=True)
            frappe.db.commit()
            business(all_session, "old session after role removal", "list_warehouses", status=(401, 403))
            relogged = login(stack, all_user, "role removed")
            business(relogged, "fresh session after role removal", "list_warehouses", status=403)
    finally:
        server.shutdown()
        thread.join(timeout=20)
        server.server_close()
        frappe.db.rollback()
    verify_scope_concurrency(site, absent[0], company, w1.id)
    print(f"V1 cookie-session HTTP matrix: {len(outcomes)} checks", flush=True)
    for label, code in outcomes:
        print(f"  {code} {label}", flush=True)
