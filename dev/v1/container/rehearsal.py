"""Clean install, merged release upgrade and recovery on disposable sites."""

from hashlib import sha256
import json
from pathlib import Path
import os
import subprocess
import shutil
import sys
import tarfile
from tempfile import TemporaryDirectory

from bench import (BENCH, PROJECT, PYTHON, SITE, SITES, UPGRADE, check_bench, check_installed,
                   check_targets, create_site, deploy_app, redact, require, run, site_command, test_site)


PATCH = "haxpower_v1.patches.backfill_migration_sample"
FIXTURE_VERSION = "0.1.0"
OWNERSHIP = "HP V1 Transit Ownership"
OPERATION_LOG = "HP V1 Operation Log"
NATIVE_LINK = "HP V1 Operation Native Link"
FIXTURE = Path(__file__).resolve().parent / "fixtures/app-0.1.0"


def inspect(action, target):
    try:
        output = run(PYTHON, "/opt/hp-v1/probe.py", action, target, cwd=SITES, capture=True)
    except subprocess.CalledProcessError as error:
        print(redact((error.stdout or "") + (error.stderr or "")), file=sys.stderr, flush=True)
        raise
    if action == "snapshot":
        return json.loads(output)
    if output:
        print(output.strip(), flush=True)


def install(target):
    create_site(target)
    site_command("install-app", "erpnext", target=target)
    site_command("install-app", "haxpower_v1", target=target)
    check_installed(target)


def record(label, snapshot):
    digest = sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
    catalog_rows = sum(len(table["rows"]) for table in snapshot["catalog"].values())
    sku_tables = snapshot["sku"].get("tables", {})
    sku_rows = sum(len(table["rows"]) for table in sku_tables.values())
    warehouse_rows = sum(len(table["rows"]) for table in snapshot["warehouse"].get("tables", {}).values())
    access_rows = sum(len(table["rows"]) for table in snapshot["access"].get("tables", {}).values() if table)
    batch_rows = len(snapshot["batch"].get("rows", ()))
    operation_rows = len(snapshot["operations"].get("HP V1 Operation Log", {}).get("rows", ()))
    print(f"{label}: sha256={digest} rows={len(snapshot['rows'])} "
          f"patch_records={len(snapshot['patches'])} catalog_rows={catalog_rows} sku_rows={sku_rows} "
          f"warehouse_rows={warehouse_rows} access_rows={access_rows} batch_rows={batch_rows} "
          f"operation_rows={operation_rows}", flush=True)


def verify_catalog_schema(state):
    require(len(state["catalog"]) == 7 and all(table and not table["rows"] for table in state["catalog"].values()),
            "catalog schema missing or automatically seeded")


def verify_sku_schema(state):
    sku = state["sku"]
    require(sku.get("marker") and len(sku["tables"]) == 4
            and all(table and not table["rows"] for table in sku["tables"].values())
            and not sku["native_items"], "SKU schema missing or automatically seeded")


def verify_warehouse_schema(state):
    warehouse = state["warehouse"]
    require(warehouse.get("marker") and len(warehouse["tables"]) == 2
            and all(table and not table["rows"] for table in warehouse["tables"].values())
            and not warehouse["native_leaves"], "warehouse schema missing or automatically seeded")


def verify_access_schema(state, *, role_count=13):
    access = state["access"]
    require(access and len(access["tables"]) == 2 and all(table and not table["rows"] for table in
            access["tables"].values()), "access schema missing or automatically seeded")
    require(len(access["roles"]) == role_count and all(not role["desk_access"] and not role["disabled"]
            for role in access["roles"]) and not access["assignments"], "employee capability roles were auto-assigned")


def verify_batch_schema(state):
    batch = state["batch"]
    require(batch and not batch["rows"] and not batch["native"]
            and all(name in batch["schema"] for name in (
                "hp_v1_batch_identity", "hp_v1_batch_native", "hp_v1_batch_required_identity",
            )), "Batch schema or constraints missing, or data automatically seeded")


def verify_stock_schema(state, *, latest=True):
    tables = state["operations"]
    require(len(tables) == 9 and all(table and "ENGINE=InnoDB" in table["schema"] for table in tables.values())
            and [row["name"] for row in tables["HP V1 Stock Write Guard"]["rows"]] == ["stock"]
            and all(not table["rows"] for name, table in tables.items() if name != "HP V1 Stock Write Guard")
            and "hp_v1_stock_request_identity" in tables["HP V1 Stock Request"]["schema"]
            and "hp_v1_stock_effect" in tables["HP V1 Operation Log"]["schema"],
            "stock execution schema or constraints missing, or business data automatically seeded")
    require(all(key in tables[OWNERSHIP]["schema"] for key in (
        "hp_v1_transit_original_line", "hp_v1_transit_native_detail", "hp_v1_transit_inward_sle",
    )), "transit ownership constraints missing")
    require(("hp_v1_stock_operation_revision" in tables[OPERATION_LOG]["schema"]) == latest,
            "unexpected document/revision uniqueness")


def repeat_migration(target, snapshot):
    site_command("migrate", target=target)
    require(snapshot == inspect("snapshot", target), f"migration replay changed schema, data or patch history on {target}")
    record(f"migration replay identical for {target}", snapshot)


def verify_upgraded(state, before):
    old, = before["rows"]
    new, = state["rows"]
    require(new == dict(old, baseline_revision=1), "record identity or content changed during migration")
    require("baseline_revision" in state["schema"], "new column missing after migration")
    require([(patch["patch"], patch["skipped"]) for patch in state["patches"]] == [(PATCH, 0)],
            "unexpected completed patch history")
    verify_catalog_schema(state)


def verify_approval_install(state):
    settings = "HP V1 Approval Settings"
    tables = state["approval"]
    require(set(tables) == {"HP V1 Approval " + name for name in (
        "Settings", "Setting Change", "Submission", "Event", "Request",
    )}, "approval schema missing")
    row, = tables[settings]["rows"]
    require((row["name"], row["revision"], row["inventory_approval_enabled"]) == ("inventory", 1, 0),
            "approval configuration did not initialize disabled")
    require(all(not table["rows"] for name, table in tables.items() if name != settings),
            "migration inferred historical approval facts")


def verify_master_data_upgrade(state, baseline, *, failed=False):
    current_spec = state["catalog"]["HP V1 Product Specification"]
    old_spec = baseline["catalog"]["HP V1 Product Specification"]
    old_units = {}
    for row in baseline["sku"]["tables"]["HP V1 Stock Item"]["rows"]:
        old_units[row["specification"]] = row["stock_uom"], row["quantity_precision"]
    expected_specs = []
    for row in old_spec["rows"]:
        unit, precision = old_units.get(row["name"], (None, 0)) if not failed else (None, 0)
        expected_specs.append(dict(row, stock_uom=unit, quantity_precision=precision))
    require(current_spec["rows"] == expected_specs, "unexpected historical specification unit or identity after upgrade")
    roles = state["access"]["roles"]
    added = [role for role in roles if role["name"] == "HP V1 Parameter Maintain"]
    require(len(added) == (0 if failed else 1), "unexpected parameter role migration state")
    comparable_access = dict(state["access"], roles=[role for role in roles if role not in added])
    comparable_catalog = dict(state["catalog"], **{"HP V1 Product Specification": old_spec})
    table = "HP V1 Product Identifier"
    previous_ids = baseline["sku"]["tables"][table]
    current_ids = state["sku"]["tables"][table]
    require("legacy_v1" in current_ids["schema"]
            and current_ids["rows"] == [dict(row, legacy_v1=None) for row in previous_ids["rows"]],
            "legacy SKU identifier facts changed during schema upgrade")
    comparable_sku = dict(state["sku"], tables=dict(state["sku"]["tables"], **{table: previous_ids}))
    require(dict(state, access=comparable_access, catalog=comparable_catalog, sku=comparable_sku,
                 formal_catalog=baseline["formal_catalog"]) == baseline,
            "V1-16A migration changed native stock, other domain histories or assigned grants")
    require(set(state["formal_catalog"]) == {"HP V1 Specification Identifier", "HP V1 Catalog Number",
                                            "HP V1 Catalog Configuration"}, "formal catalog schema missing")
    require(not state["formal_catalog"]["HP V1 Specification Identifier"]
            and not state["formal_catalog"]["HP V1 Catalog Configuration"], "migration seeded unrequested data")
    require(bool(state["formal_catalog"]["HP V1 Catalog Number"]) != failed,
            "unexpected formal number initialization state")


def recovery_point(backup):
    files = {name: backup / filename for name, filename in {
        "db": "site.sql.gz",
        "files": "site-public.tar",
        "private-files": "site-private.tar",
        "conf": "site-config.json",
    }.items()}
    args = [part for name, path in files.items() for part in (f"--backup-path-{name}", str(path))]
    site_command("backup", "--with-files", *args, target=UPGRADE)
    require(all(path.is_file() and path.stat().st_size for path in files.values()), "incomplete backup set")
    return files


def rehearse_failure(fixture, previous, check_failure):
    site_path = SITES / UPGRADE
    samples = {site_path / scope / "files/v1-recovery-sample.txt": f"Synthetic {scope} file\n"
               for scope in ("public", "private")}
    for path, content in samples.items():
        path.write_text(content)

    with TemporaryDirectory(prefix="hp-v1-recovery-", dir=BENCH) as backup:
        files = recovery_point(Path(backup))
        storage = site_path / "private" / "haxpower_v1"
        stored = Path(backup) / "haxpower_v1"
        if storage.exists():
            shutil.copytree(storage, stored)
        try:
            deploy_app(fault=True)
            failure = site_command("migrate", target=UPGRADE, expect_failure=True)
            require("RuntimeError: HP_V1_REHEARSAL_PATCH_FAILURE" in failure
                    and "haxpower_v1.patches.rehearsal_failure" in failure, "missing injected migration diagnostics")
            check_failure(inspect("snapshot", UPGRADE))
            print("Injected migration failure verified after schema sync.", flush=True)
        finally:
            deploy_app(fixture=fixture)
            config = site_path / "site_config.json"
            config.write_bytes(files["conf"].read_bytes())
            require(check_targets(UPGRADE), "restore target validation failed")
            for path in samples:
                path.unlink()
            site_command("restore", str(files["db"]), "--with-public-files", str(files["files"]),
                         "--with-private-files", str(files["private-files"]), "--db-root-password", "@db-root@",
                         target=UPGRADE)
            if stored.exists():
                shutil.rmtree(storage)
                shutil.copytree(stored, storage)
            require(inspect("snapshot", UPGRADE) == previous, "restored database differs from recovery point")
            require(json.loads(config.read_text()) == json.loads(files["conf"].read_text()),
                    "restored site configuration differs from recovery point")
            require(all(path.read_text() == content for path, content in samples.items()), "site files were not restored")
            record("consistent DB, site configuration and public/private files restored", previous)


def integration():
    require(PROJECT.startswith("haxpower-v1-ci-"), "integration requires a disposable CI project")
    require(not check_targets() and not check_targets(UPGRADE), "integration requires two empty sites")
    os.umask(0o077)
    install(SITE)
    test_site(SITE)
    site_command("migrate", target=SITE)
    clean = inspect("snapshot", SITE)
    require(not clean["rows"] and len(clean["patches"]) == 1 and "baseline_revision" in clean["schema"],
            "unexpected clean baseline")
    verify_catalog_schema(clean)
    verify_sku_schema(clean)
    verify_warehouse_schema(clean)
    verify_access_schema(clean, role_count=14)
    verify_batch_schema(clean)
    verify_stock_schema(clean)
    verify_approval_install(clean)
    record("clean install and first migration", clean)
    repeat_migration(SITE, clean)
    inspect("receipt-seed", SITE)
    inspect("receipt-execution", SITE)
    inspect("issue-execution", SITE)
    inspect("transfer-dispatch", SITE)
    inspect("transfer-followup", SITE)
    inspect("stock-count", SITE)
    inspect("stock-count-extended", SITE)
    inspect("stock-count-native", SITE)
    inspect("approval", SITE)
    inspect("stock-correction", SITE)
    inspect("formal-catalog", SITE)
    inspect("inventory-reporting", SITE)
    inspect("inventory-history-scale", SITE)
    clean_with_stock = inspect("snapshot", SITE)
    repeat_migration(SITE, clean_with_stock)
    rehearse_upgrade(clean_with_stock)


def rehearse_upgrade(clean):
    require(PROJECT.startswith("haxpower-v1-ci-") and not check_targets(UPGRADE),
            "upgrade rehearsal requires an empty disposable site")
    try:
        deploy_app(fixture=FIXTURE)
        version = run(PYTHON, "-c", "import haxpower_v1; print(haxpower_v1.__version__)", capture=True)
        require(version.strip() == FIXTURE_VERSION, "previous fixture was not installed")
        install(UPGRADE)
        inspect("seed", UPGRADE)
        previous = inspect("snapshot", UPGRADE)
        require(len(previous["rows"]) == 1 and not previous["patches"] and not previous["catalog"]
                and not previous["sku"] and not previous["warehouse"] and not previous["access"] and not previous["batch"]
                and "baseline_revision" not in previous["schema"],
                "unexpected previous fixture state")
        record(f"synthetic previous release {FIXTURE_VERSION}", previous)
        rehearse_failure(FIXTURE, previous, lambda state: verify_upgraded(state, previous))
    finally:
        deploy_app()

    with TemporaryDirectory(prefix="hp-v1-merged-baseline-", dir=BENCH) as path:
        try:
            # The host streams only the App directory from the pinned merged commit.
            with tarfile.open(fileobj=sys.stdin.buffer, mode="r|") as archive:
                archive.extractall(path, filter="data")
            baseline = Path(path) / "apps/haxpower_v1"
            deploy_app(fixture=baseline)
            version = run(PYTHON, "-c", "import haxpower_v1; print(haxpower_v1.__version__)", capture=True)
            require(version.strip() == "0.16.0", "expected the merged V1-15 app")
            site_command("migrate", target=UPGRADE)
            baseline_empty = inspect("snapshot", UPGRADE)
            verify_upgraded(baseline_empty, previous)
            verify_sku_schema(baseline_empty)
            verify_warehouse_schema(baseline_empty)
            verify_access_schema(baseline_empty)
            verify_batch_schema(baseline_empty)
            verify_stock_schema(baseline_empty)
            inspect("seed-catalog", UPGRADE)
            inspect("seed-skus", UPGRADE)
            inspect("verify-skus", UPGRADE)
            inspect("seed-warehouses", UPGRADE)
            inspect("seed-access", UPGRADE)
            inspect("seed-batches", UPGRADE)
            seeded = inspect("snapshot", UPGRADE)
            require(sum(len(table["rows"]) for table in seeded["catalog"].values()) == 9
                    and len(seeded["sku"]["native_items"]) == 2
                    and len(seeded["warehouse"]["native_leaves"]) == 9
                    and len(seeded["access"]["assignments"]) == 2
                    and len(seeded["batch"]["rows"]) == len(seeded["batch"]["native"]) == 5,
                    "unexpected merged V1-15 synthetic state")
            inspect("sku-concurrency", UPGRADE)
            inspect("stock-execution", UPGRADE)
            inspect("receipt-execution", UPGRADE)
            inspect("issue-execution", UPGRADE)
            inspect("seed-transfer-history", UPGRADE)
            inspect("settle-transfer-history", UPGRADE)
            inspect("stock-count", UPGRADE)
            inspect("count-recovery-prepare", UPGRADE)
            inspect("correction-baseline", UPGRADE)
            inspect("correction-recovery-prepare", UPGRADE)
            # Ordinary approval recovery requires the final recorded switch to be on.
            inspect("approval-recovery-prepare", UPGRADE)
            inspect("seed-formal-upgrade", UPGRADE)
            baseline_state = inspect("snapshot", UPGRADE)
            record("merged V1-15 with native movements, counts, approvals and corrections", baseline_state)
            def failed_upgrade(state):
                verify_master_data_upgrade(state, baseline_state, failed=True)
            rehearse_failure(baseline, baseline_state, failed_upgrade)
        finally:
            deploy_app()

    run_upgraded_rehearsal(clean, baseline_state)


def run_upgraded_rehearsal(clean, baseline_state):
    check_bench()
    site_command("migrate", target=UPGRADE)
    upgraded = inspect("snapshot", UPGRADE)
    verify_master_data_upgrade(upgraded, baseline_state)
    inspect("verify-formal-upgrade", UPGRADE)
    record("V1-16A upgraded from merged V1-15; native inventory and domain histories preserved", upgraded)
    verify_upgraded_workflows(clean)


def verify_upgraded_workflows(clean):
    inspect("approval-recovery-finish", UPGRADE)
    inspect("verify-inventory-versions", UPGRADE)
    inspect("verify-skus", UPGRADE)
    inspect("verify-access", UPGRADE)
    test_site(UPGRADE)
    inspect("batch-verify", UPGRADE)
    inspect("stock-execution", UPGRADE)
    inspect("catalog-permissions", UPGRADE)
    inspect("warehouse-concurrency", UPGRADE)
    inspect("warehouse-http", UPGRADE)
    inspect("access-http", UPGRADE)
    inspect("verify-stock", UPGRADE)
    inspect("issue-reconcile-history", UPGRADE)
    inspect("receipt-execution", UPGRADE)
    inspect("issue-execution", UPGRADE)
    inspect("transfer-dispatch", UPGRADE)
    inspect("continue-v112-history", UPGRADE)
    inspect("stock-count", UPGRADE)
    inspect("stock-count-native", UPGRADE)
    # The extended probe rotates the site's generation. Run it after the
    # restored V1-12 Draft has proved continuity with its original snapshot.
    inspect("inventory-reporting", UPGRADE)
    populated = inspect("snapshot", UPGRADE)
    record("upgraded site with synthetic domain data and warehouse grants", populated)
    repeat_migration(UPGRADE, populated)
    repeat_migration(UPGRADE, populated)

    def check_populated_failure(state):
        require(state == populated, "failing migration changed the populated stock execution snapshot")

    rehearse_failure(None, populated, check_populated_failure)
    repeat_migration(UPGRADE, populated)
    test_site(UPGRADE)
    verify_recovered_stock(clean)


def verify_recovered_stock(clean):
    inspect("inventory-reporting-recovered", UPGRADE)
    inspect("correction-recovery-finish", UPGRADE)
    inspect("count-recovery-finish", UPGRADE)
    inspect("verify-skus", UPGRADE)
    inspect("verify-access", UPGRADE)
    inspect("verify-stock", UPGRADE)
    inspect("stock-execution", UPGRADE)
    inspect("issue-reconcile-history", UPGRADE)
    inspect("receipt-execution", UPGRADE)
    inspect("issue-execution", UPGRADE)
    inspect("transfer-reconcile-history", UPGRADE)
    inspect("resume-v112-history", UPGRADE)
    inspect("verify-inventory-versions", UPGRADE)
    inspect("stock-count", UPGRADE)
    inspect("stock-count-extended", UPGRADE)
    inspect("stock-count-native", UPGRADE)
    inspect("transfer-dispatch", UPGRADE)
    check_installed(UPGRADE)
    require(inspect("snapshot", SITE) == clean, "upgrade rehearsal changed the clean-install site")
