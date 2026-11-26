"""Collect narrowly scoped, credential-free integration evidence."""

import json
from pathlib import Path
import sys

import frappe
import haxpower_v1

from bench import LOCK, PROJECT, TARGETS, check_targets, require


DOCTYPE = "HP V1 Migration Sample"
PATCH = "haxpower_v1.patches.backfill_migration_sample"
FAULT_PATCH = "haxpower_v1.patches.rehearsal_failure"
SAMPLE_ID = "v1-synthetic-001"
SAMPLE_TEXT = "Synthetic V1 migration sample"


def identity_evidence(site):
    schema, user, version = frappe.db.sql("SELECT DATABASE(), CURRENT_USER(), VERSION()")[0]
    require(schema == user.split("@")[0] == TARGETS[site], "incorrect live database identity")
    require(version.split("-", 1)[0] == LOCK["tools"]["mariadb"], "unexpected server version")
    require(set(frappe.get_installed_apps()) == {"frappe", "erpnext", "haxpower_v1"}, "unexpected installed apps")
    require(frappe.get_hooks("app_name", app_name="haxpower_v1") == ["haxpower_v1"]
            and frappe.get_hooks("app_title", app_name="haxpower_v1") == ["HaxpowerPlatform"],
            "unexpected App hooks")
    require(frappe.db.get_value("Module Def", "HP V1", "app_name") == "haxpower_v1"
            and frappe.get_meta(DOCTYPE).module == "HP V1", "unexpected module or DocType owner")
    return {"site": site, "database": schema, "database_user": user.split("@")[0],
            "mariadb": version, "apps": frappe.get_installed_apps(),
            "module": "HP V1", "doctype": DOCTYPE, "app_version": haxpower_v1.__version__}


def snapshot():
    schema = frappe.db.sql("SHOW CREATE TABLE `tabHP V1 Migration Sample`")[0][1]
    columns = frappe.db.get_table_columns(DOCTYPE)
    fields = ["name", "sample_id", "sample_text", "creation", "modified"]
    if "baseline_revision" in columns:
        fields.append("baseline_revision")
    rows = frappe.get_all(DOCTYPE, fields=fields, order_by="name")
    logs = frappe.get_all("Patch Log", filters={"patch": ["in", [PATCH, FAULT_PATCH]]},
                          fields=["name", "patch", "skipped", "creation"], order_by="name")
    catalog = {}
    if frappe.db.table_exists("HP V1 Product Specification"):
        from catalog_probe import snapshot as catalog_snapshot

        catalog = catalog_snapshot()
    sku = {}
    if frappe.db.table_exists("HP V1 Stock Item"):
        from sku_probe import snapshot as sku_snapshot

        sku = sku_snapshot()
    warehouse = {}
    if frappe.db.table_exists("HP V1 Warehouse"):
        from warehouse_probe import snapshot as warehouse_snapshot

        warehouse = warehouse_snapshot()
    access = {}
    if frappe.db.table_exists("HP V1 Warehouse Access"):
        from access_probe import snapshot as access_snapshot

        access = access_snapshot()
    batch = {}
    if frappe.db.table_exists("HP V1 Batch Mapping"):
        from batch_probe import snapshot as batch_snapshot

        batch = batch_snapshot()
    operations = {}
    if frappe.db.table_exists("HP V1 Stock Document"):
        from stock_probe import snapshot as stock_snapshot

        operations = stock_snapshot()
    inventory = {}
    for table in ('HP V1 Inventory Epoch','HP V1 Inventory Version','HP V1 Stock Count','HP V1 Stock Count Line'):
        if frappe.db.table_exists(table):
            inventory[table] = dict(schema=frappe.db.sql(f'SHOW CREATE TABLE `tab{table}`')[0][1],
                                    rows=frappe.get_all(table,fields=['*'],order_by='name'))
    approval = {}
    for table in ("Approval Settings", "Approval Setting Change", "Approval Submission", "Approval Event", "Approval Request"):
        doctype = f"HP V1 {table}"
        if frappe.db.table_exists(doctype):
            approval[doctype] = {
                "schema": frappe.db.sql(f"SHOW CREATE TABLE `tab{doctype}`")[0][1],
                "rows": frappe.get_all(doctype, fields=["*"], order_by="name"),
            }
    posted = {table: frappe.get_all(table, fields=["*"], order_by="name") for table in (
        "Stock Entry", "Stock Entry Detail", "Serial and Batch Bundle", "Serial and Batch Entry",
        "Stock Ledger Entry", "Bin", "GL Entry", "Payment Ledger Entry", "Repost Item Valuation",
        "Stock Reconciliation", "Stock Reconciliation Item",
    )}
    return {"access": access, "approval": approval, "batch": batch, "operations": operations, "schema": schema, "rows": rows,
            "patches": logs, "catalog": catalog, "sku": sku,
            "warehouse": warehouse, "posted": posted, "inventory": inventory}


def main(action, site):
    require(check_targets(site), "unexpected or missing probe site")
    frappe.init(site=site, sites_path=str(Path.cwd()))
    frappe.connect()
    try:
        if PROJECT.startswith("haxpower-v1-ci-"):
            # Upgrade and recovery share a site and create distinct identities.
            # Keep their fixture budget local to this CLI process.
            frappe.conf.throttle_user_limit = 256
        if action == "identity":
            print(json.dumps(identity_evidence(site), sort_keys=True))
        elif action == "snapshot":
            print(frappe.as_json(snapshot()))
        elif action == "seed":
            require(not frappe.db.exists(DOCTYPE, SAMPLE_ID), "synthetic record already exists")
            frappe.get_doc(doctype=DOCTYPE, sample_id=SAMPLE_ID, sample_text=SAMPLE_TEXT).insert(
                ignore_permissions=True
            )
            frappe.db.commit()
        elif action == "seed-catalog":
            from catalog_probe import seed

            seed()
        elif action == "seed-skus":
            from sku_probe import seed

            seed()
        elif action == "sku-concurrency":
            from sku_probe import verify_concurrency

            verify_concurrency(site)
        elif action == "catalog-permissions":
            from catalog_probe import verify_native_denials

            verify_native_denials(site)
        elif action == "verify-skus":
            from sku_probe import verify_seed

            verify_seed()
        elif action == "seed-warehouses":
            from warehouse_probe import seed

            seed()
        elif action in {"warehouse-concurrency", "warehouse-http"}:
            from warehouse_probe import verify_concurrency, verify_http

            (verify_concurrency if action == "warehouse-concurrency" else verify_http)(site)
        elif action == "seed-access":
            from access_probe import seed_existing_grant

            seed_existing_grant()
        elif action == "verify-access":
            from access_probe import verify_existing_grant

            verify_existing_grant()
        elif action in {"seed-batches", "batch-verify"}:
            from batch_probe import seed_existing, verify_concurrency

            seed_existing() if action == "seed-batches" else verify_concurrency(site)
        elif action in {"stock-execution", "verify-stock"}:
            from stock_probe import run, verify_persisted

            run(site) if action == "stock-execution" else verify_persisted()
        elif action in {"receipt-seed", "receipt-execution"}:
            from receipt_probe import run, seed

            seed() if action == "receipt-seed" else run(site)
        elif action in {"issue-execution", "issue-reconcile-history"}:
            from issue_probe import run, verify_receipts

            run(site) if action == "issue-execution" else verify_receipts()
        elif action in {"transfer-dispatch", "transfer-reconcile-history"}:
            from transfer_probe import run, verify_history

            run(site) if action == "transfer-dispatch" else verify_history()
        elif action == "seed-transfer-history":
            from transfer_probe import seed_history

            seed_history()
        elif action in {"transfer-followup", "settle-transfer-history", "resume-transfer-history"}:
            from transfer_followup_probe import run, settle_history

            if action == "transfer-followup":
                run(site)
            else:
                settle_history(recovered=action == "resume-transfer-history")
        elif action in {"continue-v112-history", "resume-v112-history"}:
            from transfer_followup_probe import continue_v112_history

            continue_v112_history(recovered=action == 'resume-v112-history')
        elif action == "verify-inventory-versions":
            from haxpower_v1.inventory import versions
            from haxpower_v1.inventory.guard import lock_stock_writes

            lock_stock_writes()
            history=versions.verify()
            require(bool(history), 'upgrade did not backfill any native quantity history')
            print(f'Inventory epoch ready: {len(history)} historical buckets, including zero/transit; native facts verified.')
        elif action == "version-backfill-fault":
            from count_probe import backfill_fault

            backfill_fault()
        elif action in {'count-recovery-prepare','count-recovery-finish'}:
            from count_probe import recovery_prepare, recovery_finish

            (recovery_prepare if action=='count-recovery-prepare' else recovery_finish)()
        elif action in {"approval", "approval-recovery-prepare", "approval-recovery-finish"}:
            from approval_probe import recovery_finish, recovery_prepare, run

            if action == "approval":
                run(site)
            else:
                (recovery_prepare if action.endswith("prepare") else recovery_finish)()
        elif action == "stock-count-native":
            from count_probe import native_boundaries

            native_boundaries(site)
        elif action == "stock-count-extended":
            from count_probe import extended

            extended(site)
        elif action == "stock-count":
            from count_probe import run

            run(site)
        elif action == "access-http":
            from access_probe import verify_http

            verify_http(site)
        else:
            raise ValueError("unknown probe action")
    finally:
        frappe.destroy()


if __name__ == "__main__":
    main(*sys.argv[1:])
