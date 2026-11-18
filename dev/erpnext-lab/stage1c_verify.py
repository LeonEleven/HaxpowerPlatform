"""Exercise the Stage 1C permission matrix against the isolated, existing stock fixtures."""

import json
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from urllib.parse import quote
from zipfile import ZipFile

import frappe
import requests
from frappe.permissions import get_role_permissions

from haxpower_platform.stock import COMPANY, ITEMS, LOCATIONS, QUERY_ROLE, ROLE, batch_key, post_stock
from stage1b_verify import (
    API, NO_BATCH, OPERATOR, ROOT, SITE, STOCK_USER, accepted, committed_snapshot,
    new_connection, payload, reconcile, report_filters, require, send, session,
)


RESTRICTED = "Haxpower Stage 1C Restricted"
NONE = "Haxpower Stage 1C None"
PROFILES = {
    "operator": (OPERATOR, [ROLE], tuple(LOCATIONS)),
    "query": ("stage1c-query@example.invalid", [QUERY_ROLE], tuple(LOCATIONS)),
    "restricted": ("stage1c-restricted@example.invalid", [QUERY_ROLE, RESTRICTED], ("W1/L1",)),
    "none": ("stage1c-none@example.invalid", [QUERY_ROLE, NONE], ()),
    "native": (STOCK_USER, [ROLE, "Stock User", "Stock Manager", "Item Manager"], ("W1/L1",)),
    "overlay": ("stage1c-overlay@example.invalid", [ROLE, QUERY_ROLE, RESTRICTED], ("W1/L1",)),
}
REPORTS = ("Stock Ledger", "Stock Balance", "Batch-Wise Balance History")
SNAPSHOT_TABLES = (
    "Stock Entry", "Stock Entry Detail", "Stock Reconciliation", "Stock Reconciliation Item",
    "Stock Ledger Entry", "Serial and Batch Bundle", "Serial and Batch Entry", "Batch", "Bin",
    "GL Entry", "Repost Item Valuation", "Item", "Warehouse", "Data Import",
)


def fingerprint(*, fresh=True):
    if fresh:
        frappe.db.rollback()
    result = {}
    for doctype in SNAPSHOT_TABLES:
        rows = frappe.get_all(doctype, fields=["*"], order_by="name")
        value = json.dumps(rows, sort_keys=True, ensure_ascii=False, default=str)
        result[doctype] = {"rows": len(rows), "sha256": sha256(value.encode()).hexdigest()}
    return result


def grants_snapshot():
    users = [user for user, _, _ in PROFILES.values()]
    return frappe.get_all("User Permission", filters={"user": ["in", users]},
                          fields=["*"], order_by="name")


def method(name):
    return "/api/method/" + name


def resource(doctype, name=None):
    path = "/api/resource/" + quote(doctype, safe="")
    return path if name is None else path + "/" + quote(name, safe="")


def internal_id(response):
    blobs = [response.content]
    if response.content.startswith(b"PK\x03\x04"):
        with ZipFile(BytesIO(response.content)) as archive:
            blobs.extend(archive.read(name) for name in archive.namelist() if name.endswith(".xml"))
    return any(b"HP-NB-" in blob or b"HP-RB-" in blob for blob in blobs)


def evidence(response, route, verb):
    information = {"method": verb, "route": route, "status": response.status_code,
                   "internal_id": response.ok and internal_id(response)}
    if not response.ok:
        try:
            information["exception"] = response.json().get("exc_type")
        except ValueError:
            pass
    information["hook"] = next(
        (marker for marker in ("STAGE1B_BATCH_PROTECTED", "STAGE1B_USE_ADAPTER") if marker in response.text),
        None,
    )
    return information


def prepare_profiles():
    for role in (QUERY_ROLE, RESTRICTED, NONE):
        if not frappe.db.exists("Role", role):
            frappe.get_doc({"doctype": "Role", "role_name": role, "desk_access": 1}).insert()
    clients = {}
    details = {}
    for name, (user, roles, locations) in PROFILES.items():
        clients[name] = session(user, roles, locations)
        permissions = {}
        for doctype in ("Batch", "Serial and Batch Bundle", "Stock Entry", "Stock Reconciliation",
                        "Stock Ledger Entry", "Bin", "Data Import", "User Permission"):
            values = get_role_permissions(doctype, user=user)
            permissions[doctype] = [key for key in
                                    ("read", "select", "create", "write", "submit", "cancel", "delete",
                                     "report", "export", "import", "print") if values.get(key)]
        details[name] = {"roles": roles, "grants": list(locations), "doctype": permissions}
        if name != "native":
            require(not any(permissions.values()), f"{name} has native DocType permissions")
    return clients, details


def endpoints(state):
    item, warehouse = ITEMS["SKU-A"], LOCATIONS["W1/L1"]
    batch = batch_key(item, None)
    entry = next(row for row in state["ledger"] if row.voucher_no.endswith("00008"))
    bundle, stock = entry.serial_and_batch_bundle, entry.voucher_no
    dates = report_filters(state)
    # All mutations use ordinary supported document operations and known fictitious records.
    issue = {"doctype": "Stock Entry", "company": COMPANY, "stock_entry_type": "Material Issue",
             "items": [{"item_code": item, "qty": 1, "uom": "PC", "s_warehouse": warehouse}]}
    reconciliation = {
        "doctype": "Stock Reconciliation", "company": COMPANY, "purpose": "Stock Reconciliation",
        "posting_date": dates["to_date"],
        "items": [{"item_code": item, "warehouse": warehouse, "qty": 11, "valuation_rate": 0,
                   "use_serial_batch_fields": 1, "batch_no": batch}],
    }
    bundle_doc = {
        "company": COMPANY, "item_code": item, "warehouse": warehouse,
        "voucher_type": "Stock Entry", "type_of_transaction": "Outward",
        "posting_datetime": frappe.utils.now(),
        "entries": [{"batch_no": batch, "qty": -1}],
    }
    writes = {
        "batch_create": ("POST", resource("Batch"), {"batch_id": "HP-NB-1C-DENIED", "item": item}),
        "batch_edit": ("PUT", resource("Batch", batch), {"description": "Stage 1C permission probe"}),
        "batch_disable": ("POST", method("frappe.client.set_value"),
                          {"doctype": "Batch", "name": batch, "fieldname": "disabled", "value": 1}),
        "batch_rename": ("POST", method("frappe.client.rename_doc"),
                         {"doctype": "Batch", "old_name": batch, "new_name": "HP-NB-1C-RENAME"}),
        "batch_delete": ("DELETE", resource("Batch", batch), None),
        "batch_split": ("POST", method("erpnext.stock.doctype.batch.batch.split_batch"),
                        {"batch_no": batch, "item_code": item, "warehouse": warehouse,
                         "qty": 1, "new_batch_id": "HP-NB-1C-SPLIT"}),
        "bundle_create": ("POST", resource("Serial and Batch Bundle"), bundle_doc),
        "bundle_edit": ("PUT", resource("Serial and Batch Bundle", bundle), {"is_rejected": 1}),
        "stock_create": ("POST", resource("Stock Entry"), issue),
        "stock_edit": ("PUT", resource("Stock Entry", stock), {"remarks": "Stage 1C permission probe"}),
        "stock_submit": ("POST", method("frappe.desk.form.save.savedocs"),
                         {"doc": json.dumps(issue), "action": "Submit"}),
        "stock_cancel": ("POST", method("frappe.client.cancel"), {"doctype": "Stock Entry", "name": stock}),
        "reconcile_create": ("POST", resource("Stock Reconciliation"), reconciliation),
        "reconcile_submit": ("POST", method("frappe.client.submit"), {"doc": reconciliation}),
        "import_create": ("POST", resource("Data Import"),
                          {"reference_doctype": "Batch", "import_type": "Insert New Records"}),
        "self_grant_warehouse": ("POST", resource("User Permission"),
                                 {"allow": "Warehouse", "for_value": LOCATIONS["W1/L2"],
                                  "apply_to_all_doctypes": 1}),
    }
    reads = {
        "batch_list": (resource("Batch"), {}),
        "batch_detail": (resource("Batch", batch), {}),
        "bundle_list": (resource("Serial and Batch Bundle"), {}),
        "bundle_detail": (resource("Serial and Batch Bundle", bundle), {}),
        "stock_detail": (resource("Stock Entry", stock), {}),
        "ledger_list": (resource("Stock Ledger Entry"), {}),
        "bin_list": (resource("Bin"), {}),
        "rest_v2_batch_detail": ("/api/v2/document/Batch/" + quote(batch, safe=""), {}),
        "client_get": (method("frappe.client.get"), {"doctype": "Batch", "name": batch}),
        "client_list": (method("frappe.client.get_list"), {"doctype": "Batch", "fields": '["name"]'}),
        "client_count": (method("frappe.client.get_count"), {"doctype": "Batch"}),
        "client_value": (method("frappe.client.get_value"),
                         {"doctype": "Batch", "fieldname": "name", "filters": json.dumps({"name": batch})}),
        "desk_list": (method("frappe.desk.reportview.get"), {"doctype": "Batch", "fields": '["name"]'}),
        "desk_detail": (method("frappe.desk.form.load.getdoc"), {"doctype": "Batch", "name": batch}),
        "native_batch_stock": (method("erpnext.stock.doctype.batch.batch.get_batch_qty"),
                               {"batch_no": batch, "warehouse": warehouse}),
        "import_list": (resource("Data Import"), {}),
        "import_template": (method("frappe.core.doctype.data_import.data_import.download_template"),
                            {"doctype": "Batch", "export_records": "all",
                             "export_fields": '{"Batch":["name","batch_id","item"]}'}),
        "print_view": ("/printview", {"doctype": "Batch", "name": batch, "format": "Standard"}),
        "print_pdf": (method("frappe.utils.print_format.download_pdf"),
                      {"doctype": "Batch", "name": batch, "format": "Standard"}),
    }
    report_filters_json = json.dumps({**dates, "warehouse": warehouse})
    for report in REPORTS:
        reads[report] = (method("frappe.desk.query_report.run"),
                         {"report_name": report, "filters": report_filters_json,
                          "ignore_prepared_report": "true"})
    for format_type in ("CSV", "Excel"):
        reads[f"export_{format_type}"] = (method("frappe.desk.query_report.export_query"),
                                          {"report_name": REPORTS[-1], "filters": report_filters_json,
                                           "file_format_type": format_type, "ignore_visible_idx": "true",
                                           "export_in_background": "0"})
    return writes, reads


def check_custom(clients, original):
    results = {}
    for name, client in clients.items():
        _, roles, locations = PROFILES[name]
        observed = {}
        for loc in LOCATIONS:
            params = {"stockItemId": "SKU-A", "warehouseLocationId": loc}
            response = send(client, "GET", API + "balances", params=params)
            expected = 200 if loc in locations else 403
            require(response.status_code == expected, f"{name} {loc} custom read HTTP {response.status_code}")
            require(not internal_id(response), "Internal ID in custom read")
            if expected == 200:
                rows = response.json()["message"]["items"]
                require(all(row["location"] == loc for row in rows), f"{name} received another location")
                quantities = {(row["batch"], row["quantity"]) for row in rows}
                require(quantities == ({("STD61", "7"), ("STD62", "20"), (None, "11")}
                                       if loc == "W1/L1" else {(None, "12")}), f"{name} wrong quantities")
            else:
                require("WAREHOUSE_ACCESS_DENIED" in response.text, "Expected a warehouse denial")
            observed[f"read_{loc}"] = evidence(response, API + "balances", "GET")
        forged = send(client, "GET", API + "balances", params={
            "stockItemId": "SKU-A", "warehouseLocationId": "HP1B W1 - H1B",
        })
        require(forged.status_code == 403 and "WAREHOUSE_ACCESS_DENIED" in forged.text,
                f"{name} accepted a parent/unknown warehouse ID")
        observed["read_parent_or_unknown"] = evidence(forged, API + "balances", "GET")
        for action in ("RECEIPT", "ISSUE"):
            if ROLE in roles and "W1/L2" in locations:
                continue
            target = locations[0] if ROLE not in roles and locations else "W1/L2"
            response = send(client, "POST", API + "post_stock",
                            json=payload(action, f"1c-deny-{name}-{action}", "1", NO_BATCH, loc=target))
            reason = "WAREHOUSE_ACCESS_DENIED" if ROLE in roles else "INVENTORY_OPERATION_DENIED"
            require(response.status_code == 403 and reason in response.text,
                    f"{name} {action} did not deny {reason}")
            require(fingerprint() == original, f"{name} denied write changed inventory")
            observed[f"write_{action}"] = evidence(response, API + "post_stock", "POST")
            observed[f"write_{action}"]["location"] = target
        results[name] = observed
    response = accepted(clients["operator"], payload("ISSUE", "out-no-batch", "4", NO_BATCH))
    require(response == {"documentId": "MAT-STE-2026-00008", "replayed": True}, "Authorized replay failed")
    results["operator"]["replay"] = response
    require(fingerprint() == original, "Authorized replay changed inventory")
    guest = requests.Session()
    guest.trust_env = False
    guest.headers["Host"] = SITE
    response = send(guest, "GET", API + "balances", params={"stockItemId": "SKU-A", "warehouseLocationId": "W1/L1"})
    require(response.status_code in (401, 403) and not internal_id(response), "Unauthenticated access")
    results["guest"] = {"read": evidence(response, API + "balances", "GET")}
    return results


def check_native(clients, state, original):
    writes, reads = endpoints(state)
    results = {}
    for profile in ("operator", "query", "restricted", "none", "native"):
        client = clients[profile]
        examined = {}
        for label, (verb, route, body) in writes.items():
            # Keep the original posted vouchers intact. The native comparison uses creation
            # guards and ordinary edits, with cancellation/deletion covered by business profiles.
            if profile == "native" and label in ("batch_delete", "stock_cancel", "import_create",
                                                  "self_grant_warehouse"):
                continue
            if label == "self_grant_warehouse":
                body = {**body, "user": PROFILES[profile][0]}
            response = send(client, verb, route, **({"json": body} if body is not None else {}))
            outcome = evidence(response, route, verb)
            require(fingerprint() == original, f"{profile} {label} changed the stock snapshot")
            require(response.status_code == 403, f"{profile} {label} did not deny: {outcome}")
            if profile == "native":
                if label.startswith("batch_"):
                    require(outcome["hook"] == "STAGE1B_BATCH_PROTECTED", f"{label} bypassed Batch hook")
                elif label.startswith(("stock_", "reconcile_")):
                    require(outcome["hook"] == "STAGE1B_USE_ADAPTER", f"{label} bypassed inventory hook")
            examined[label] = outcome
        for label, (route, params) in reads.items():
            if profile == "native" and label == "print_pdf":
                continue
            response = send(client, "GET", route, params=params)
            outcome = evidence(response, route, "GET")
            if profile != "native":
                require(response.status_code == 403,
                        f"{profile} {label} did not deny access: {outcome}")
                if not any(prefix in route + json.dumps(params) for prefix in ("HP-NB-", "HP-RB-")):
                    require(not internal_id(response), f"{profile} {label} leaked an ID in an error")
            examined[label] = outcome
        results[profile] = examined
    for label in ("batch_list", "bundle_detail", "Batch-Wise Balance History", "export_CSV", "export_Excel"):
        require(results["native"][label]["internal_id"], f"Native exposure control did not work: {label}")
    return results


def successful_rollback(original):
    frappe.db.rollback()
    frappe.set_user(OPERATOR)
    try:
        posted = {}
        for action in ("RECEIPT", "ISSUE"):
            result = post_stock(
                action, "SKU-A", "W1/L1", "1", f"stage1c-transaction-{action}", NO_BATCH,
            )
            require(not result["replayed"], "Unexpected persistent probe")
            posted[action] = result["documentId"]
        require(frappe.db.count("Stock Ledger Entry") == original["Stock Ledger Entry"]["rows"] + 2,
                "Successful workflow did not write both SLE rows")
    finally:
        frappe.db.rollback()
        new_connection()
    require(fingerprint() == original, "Successful rollback probe left persistent data")
    return posted


def main(emit=print):
    require(Path.cwd() == ROOT, "Wrong Bench directory")
    new_connection()
    state = committed_snapshot()
    reconcile(state)
    original = fingerprint()
    started_at = frappe.utils.now()
    try:
        clients, profiles = prepare_profiles()
        initial_grants = grants_snapshot()
        custom = check_custom(clients, original)
        native = check_native(clients, state, original)
        rolled_back = successful_rollback(original)
        new_connection()
        after = fingerprint()
        require(after == original, "Stage 1B state changed after the permission matrix")
        require(grants_snapshot() == initial_grants, "Denied requests changed grants")
        require(committed_snapshot() == state, "Committed inventory changed")
        emit(json.dumps({
            "site": SITE, "started_at": started_at, "profiles": profiles, "before": original, "after": after,
            "custom": custom, "native": native, "rolled_back_documents": rolled_back,
            "warehouse_grants_unchanged": True,
            "retained_documents": [row.name for row in state["documents"]],
        }, ensure_ascii=False, indent=2))
    finally:
        new_connection()
        for user, _, _ in PROFILES.values():
            if not frappe.db.exists("User", user):
                continue
            doc = frappe.get_doc("User", user)
            doc.api_key = None
            doc.api_secret = None
            doc.save()
        frappe.db.commit()
        frappe.destroy()


if __name__ == "__main__":
    main()
