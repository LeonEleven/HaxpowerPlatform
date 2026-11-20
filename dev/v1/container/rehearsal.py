"""Real clean install, historic fixture upgrade and recovery on CI-only sites."""

from hashlib import sha256
import json
import os
import subprocess
import sys

from bench import (BENCH, PROJECT, PYTHON, SITE, SITES, UPGRADE, check_bench, check_installed,
                   check_targets, create_site, deploy_app, redact, require, run, site_command, test_site)


PATCH = "haxpower_v1.patches.backfill_migration_sample"
FIXTURE_VERSION = "0.1.0"


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
    print(f"{label}: sha256={digest} rows={len(snapshot['rows'])} "
          f"patch_records={len(snapshot['patches'])} catalog_rows={catalog_rows}", flush=True)


def verify_catalog_schema(state):
    require(len(state["catalog"]) == 7 and all(table and not table["rows"] for table in state["catalog"].values()),
            "catalog schema missing or automatically seeded")


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


def recovery_point():
    backup = BENCH / "v1-recovery"
    backup.mkdir(mode=0o700)
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
    record("clean install and first migration", clean)
    repeat_migration(SITE, clean)

    try:
        deploy_app(previous=True)
        version = run(PYTHON, "-c", "import haxpower_v1; print(haxpower_v1.__version__)", capture=True)
        require(version.strip() == FIXTURE_VERSION, "previous fixture was not installed")
        install(UPGRADE)
        inspect("seed", UPGRADE)
        previous = inspect("snapshot", UPGRADE)
        require(len(previous["rows"]) == 1 and not previous["patches"] and not previous["catalog"]
                and "baseline_revision" not in previous["schema"], "unexpected previous fixture state")
        record(f"synthetic previous release {FIXTURE_VERSION}", previous)

        site_path = SITES / UPGRADE
        samples = {site_path / scope / "files/v1-recovery-sample.txt": f"Synthetic {scope} file\n"
                   for scope in ("public", "private")}
        for path, content in samples.items():
            path.write_text(content)
        files = recovery_point()

        deploy_app(fault=True)
        failure = site_command("migrate", target=UPGRADE, expect_failure=True)
        require("RuntimeError: HP_V1_REHEARSAL_PATCH_FAILURE" in failure
                and "haxpower_v1.patches.rehearsal_failure" in failure, "missing injected migration diagnostics")
        verify_upgraded(inspect("snapshot", UPGRADE), previous)
        print("Injected migrate failed in haxpower_v1.patches.rehearsal_failure "
              "(HP_V1_REHEARSAL_PATCH_FAILURE). Schema and earlier patch persisted.", flush=True)

        deploy_app(previous=True)
        config = site_path / "site_config.json"
        config.write_bytes(files["conf"].read_bytes())
        require(check_targets(UPGRADE), "restore target validation failed")
        for path in samples:
            path.unlink()
        site_command("restore", str(files["db"]), "--with-public-files", str(files["files"]),
                     "--with-private-files", str(files["private-files"]), "--db-root-password", "@db-root@",
                     target=UPGRADE)
        require(inspect("snapshot", UPGRADE) == previous, "restored database differs from recovery point")
        require(json.loads(config.read_text()) == json.loads(files["conf"].read_text()),
                "restored site configuration differs from recovery point")
        require(all(path.read_text() == content for path, content in samples.items()), "site files were not restored")
        record("consistent DB, site configuration and public/private files restored", previous)
    finally:
        deploy_app()

    check_bench()
    site_command("migrate", target=UPGRADE)
    upgraded = inspect("snapshot", UPGRADE)
    verify_upgraded(upgraded, previous)
    record("upgrade recovered and synthetic record preserved", upgraded)
    inspect("seed-catalog", UPGRADE)
    inspect("catalog-permissions", UPGRADE)
    populated = inspect("snapshot", UPGRADE)
    require(sum(len(table["rows"]) for table in populated["catalog"].values()) == 9,
            "unexpected synthetic catalog migration fixture")
    record("upgraded site with synthetic specification, inactive option and audit", populated)
    repeat_migration(UPGRADE, populated)
    repeat_migration(UPGRADE, populated)
    check_installed(UPGRADE)
    require(inspect("snapshot", SITE) == clean, "upgrade rehearsal changed the clean-install site")
