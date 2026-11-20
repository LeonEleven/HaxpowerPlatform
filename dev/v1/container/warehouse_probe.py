"""Persistent warehouse evidence on the disposable V1 upgrade site."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Thread
from urllib.parse import quote

import frappe
import requests
from frappe.app import application
from frappe.core.doctype.user.user import generate_keys
from werkzeug.serving import make_server

from haxpower_v1.adapters.erpnext import warehouse as native
from haxpower_v1.warehouse import service
from haxpower_v1.warehouse.persistence import LOCATION, WAREHOUSE
from haxpower_v1.warehouse.test_warehouse import make_company, make_native, stock_counts

from identity import require


COMPANY_ABBR = "HPV1W"


def snapshot():
    tables = {
        doctype: {
            "schema": frappe.db.sql(f"SHOW CREATE TABLE `tab{doctype}`")[0][1],
            "rows": frappe.get_all(doctype, fields=["*"], order_by="name"),
        } if frappe.db.table_exists(doctype) else None
        for doctype in (WAREHOUSE, LOCATION)
    }
    marker = frappe.db.has_column(native.NATIVE, native.MARKER)
    leaves = []
    if marker:
        leaves = frappe.get_all(native.NATIVE, filters={native.MARKER: ("is", "set")},
                               fields=["*"], order_by="name")
    return {"tables": tables, "marker": marker, "native_leaves": leaves}


def _company_and_parent():
    company = frappe.db.get_value("Company", {"abbr": COMPANY_ABBR}, "name")
    parent = frappe.db.get_value(WAREHOUSE, {"company": company, "code": "W1"}, "native_parent")
    require(company and parent, "missing persistent synthetic warehouse data")
    return company, parent


def seed():
    frappe.set_user("Administrator")
    before = stock_counts()
    company, parent = make_company(COMPANY_ABBR)
    for warehouse_code in ("W1", "W2", "W3", "W4"):
        warehouse = service.create_warehouse(company=company, native_parent=parent, code=warehouse_code,
                                             display_name=f"Synthetic {warehouse_code}")
        for code in ("A01", "A02"):
            location = service.create_location(warehouse.id, code=code, display_name=f"Synthetic {code}")
            require(service.resolve_location(location.id) == location, "synthetic location did not round-trip")
    require([row.code for row in service.list_warehouses(company=company)] == ["W1", "W2", "W3", "W4"],
            "the fourth warehouse was not created dynamically")
    _, transit = service.provision_system_transit(company=company, native_parent=parent,
                                                  warehouse_code="TRANSIT", location_code="T1",
                                                  display_name="Synthetic system transit")
    require(service.diagnose_mapping(transit.id).status == "Verified", "synthetic transit mapping could not be verified")
    require(stock_counts() == before, "warehouse master data created stock or accounting records")
    frappe.db.commit()
    print("W1–W4, eight independent physical leaves and one protected Transit leaf persisted without stock or GL.")


def verify_concurrency(site):
    frappe.set_user("Administrator")
    company, parent = _company_and_parent()
    warehouse_ids = [row.id for row in service.list_warehouses(company=company)][:2]
    candidate = make_native(company, parent).name
    before = stock_counts()
    frappe.db.commit()
    ready = Barrier(2, timeout=30)

    def attempt(warehouse_id):
        frappe.init(site=site, sites_path=str(Path.cwd()))
        frappe.connect()
        try:
            frappe.set_user("Administrator")
            connection, = frappe.db.sql("SELECT CONNECTION_ID()")[0]
            # Establish a read view before either writer enters the service.
            require(not frappe.db.exists(LOCATION, {"native_warehouse": candidate}), "candidate already bound")
            ready.wait()
            try:
                result = service.create_location(warehouse_id, code="RACE", display_name="Synthetic race",
                                                 existing_native=candidate)
                frappe.db.commit()
                return connection, result.id
            except frappe.ValidationError as error:
                frappe.db.rollback()
                require("already mapped" in str(error), "unexpected location race failure")
                return connection, None
        finally:
            frappe.destroy()

    with ThreadPoolExecutor(max_workers=2) as executor:
        races = [executor.submit(attempt, warehouse_id) for warehouse_id in warehouse_ids]
        outcomes = [race.result(timeout=60) for race in races]
    require(len({connection for connection, _ in outcomes}) == 2, "the race reused a database connection")
    winners = [winner for _, winner in outcomes if winner]
    require(len(winners) == 1, "expected exactly one committed location")
    frappe.db.rollback()
    diagnostic = service.diagnose_native_mapping(candidate)
    require(diagnostic.location_id == winners[0] and diagnostic.status == "Verified",
            "a third transaction could not verify the winning mapping")
    require(frappe.db.count(LOCATION, {"code": "RACE"}) == 1 and stock_counts() == before,
            "the rejected concurrent write left a location or stock records")
    print("Two independent MariaDB connections raced to bind one leaf; a third transaction verified the sole winner.")


def verify_http(site):
    frappe.set_user("Administrator")
    company, parent = _company_and_parent()
    warehouse = service.list_warehouses(company=company)[0]
    location = service.list_locations(warehouse.id)[0]
    leaf = service.diagnose_mapping(location.id).native_warehouse
    transit_id = frappe.db.get_value(WAREHOUSE, {"company": company, "kind": "Transit"}, "name")
    transit_location = frappe.db.get_value(LOCATION, {"warehouse": transit_id}, "name")
    transit_leaf = service.diagnose_mapping(transit_location).native_warehouse
    unmanaged = make_native(company, parent).name
    user_id = "synthetic-warehouse-http@example.invalid"
    user = frappe.get_doc(
        doctype="User", email=user_id, first_name="Synthetic warehouse HTTP", user_type="System User",
        send_welcome_email=0, roles=[{"role": "Employee"}, {"role": "Item Manager"}],
    ).insert(ignore_permissions=True)
    api_secret = generate_keys(user.name)["api_secret"]
    api_key = frappe.db.get_value("User", user.name, "api_key")
    frappe.db.commit()
    before = snapshot(), stock_counts()

    server = make_server("127.0.0.1", 0, application)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with requests.Session() as session:
            session.trust_env = False
            session.headers.update({"Host": site, "Accept": "application/json",
                                    "Authorization": f"token {api_key}:{api_secret}"})

            def request(method, path, data=None, params=None):
                return session.request(method, f"http://127.0.0.1:{server.server_port}{path}",
                                       json=data, params=params, timeout=20)

            identity = request("GET", "/api/method/frappe.auth.get_logged_user")
            require(identity.status_code == 200 and identity.json().get("message") == user_id,
                    "loopback requests were not authenticated as the warehouse test user")
            warehouse_type = quote(WAREHOUSE, safe="")
            location_type = quote(LOCATION, safe="")
            native_leaf = quote(leaf, safe="")
            native_transit = quote(transit_leaf, safe="")
            denied = (
                ("POST", f"/api/resource/{warehouse_type}", {"code": "ILLEGAL"}),
                ("PUT", f"/api/resource/{warehouse_type}/{warehouse.id}", {"company": "Other"}),
                ("POST", f"/api/v2/document/{location_type}", {"warehouse": warehouse.id, "code": "ILLEGAL"}),
                ("PATCH", f"/api/v2/document/{location_type}/{location.id}", {"native_warehouse": unmanaged}),
                ("DELETE", f"/api/v2/document/{location_type}/{location.id}", None),
                ("GET", f"/api/resource/{location_type}/{location.id}", None),
                ("PUT", f"/api/resource/Warehouse/{native_leaf}", {"disabled": 1}),
                ("PATCH", f"/api/v2/document/Warehouse/{native_leaf}", {"warehouse_name": "Denied"}),
                ("DELETE", f"/api/v2/document/Warehouse/{native_leaf}", None),
                ("GET", f"/api/resource/Warehouse/{native_transit}", None),
                ("POST", "/api/method/frappe.client.set_value",
                 {"doctype": "Warehouse", "name": leaf, "fieldname": "parent_warehouse", "value": ""}),
                ("POST", "/api/method/haxpower_v1.warehouse.service.create_location", {}),
            )
            for method, path, payload in denied:
                response = request(method, path, payload)
                require(response.status_code == 403,
                        f"expected native HTTP denial for {method} {path}, got {response.status_code}")
            results = request("GET", "/api/resource/Warehouse", params={
                "filters": frappe.as_json({"name": ["in", [leaf, transit_leaf, unmanaged]]}),
            })
            require(results.status_code == 200 and results.json().get("data") == [{"name": unmanaged}],
                    "native Warehouse listing did not isolate managed leaves from regular warehouses")
    finally:
        server.shutdown()
        thread.join(timeout=20)
        server.server_close()
    frappe.db.rollback()
    require((snapshot(), stock_counts()) == before, "denied HTTP operations changed mapping or stock records")
    print(f"Authenticated loopback HTTP: {len(denied)} requests denied; managed leaves hidden and unmanaged listing intact.")
