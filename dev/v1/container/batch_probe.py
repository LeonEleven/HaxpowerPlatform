"""Persistent synthetic Batch evidence with independent MariaDB transactions."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import frappe

from haxpower_v1.adapters.erpnext import batch as native
from haxpower_v1.catalog.sku_persistence import SKU
from haxpower_v1.inventory.batch.authorization import POLICY_SETTING
from haxpower_v1.inventory.batch.normalization import CANDIDATE_VERSION, normalize_selection, selection_key
from haxpower_v1.inventory.batch.persistence import MAPPING
from haxpower_v1.inventory.batch.service import get_batch, resolve_or_create

from identity import require
from sku_probe import PREFIX, stock_counts

NO_BATCH = {"mode": "NO_BATCH"}
STD61 = {"mode": "BATCH", "batchNo": "STD61"}
STD62 = {"mode": "BATCH", "batchNo": "STD62"}


def snapshot():
    return {
        "schema": frappe.db.sql(f"SHOW CREATE TABLE `tab{MAPPING}`")[0][1],
        "rows": frappe.get_all(MAPPING, fields=["*"], order_by="name"),
        "native": frappe.get_all("Batch", fields=["*"], order_by="name"),
    }


def _sku_id(suffix):
    identity = frappe.db.get_value(SKU, {"code": f"{PREFIX}-{suffix}"}, "name")
    require(identity, "missing persistent synthetic SKU")
    return identity


def _stock_counts():
    return {key: value for key, value in stock_counts().items() if key != "Batch"}


def _selection_fields(sku_id, selection):
    parsed = normalize_selection(selection)
    return {"sku": sku_id, "kind": parsed.kind, "normalized_key": selection_key(parsed)}


def _race(site, sku_id, selection):
    barrier = Barrier(2, timeout=20)
    filters = _selection_fields(sku_id, selection)

    def attempt():
        frappe.init(site=site, sites_path=str(Path.cwd()))
        frappe.connect()
        try:
            frappe.conf[POLICY_SETTING] = CANDIDATE_VERSION
            connection, = frappe.db.sql("SELECT CONNECTION_ID()")[0]
            # Both workers establish an old read view before entering the service.
            require(not frappe.db.exists(MAPPING, filters), "mapping unexpectedly exists before batch race")
            barrier.wait()
            result = resolve_or_create(sku_id, selection)
            if result.created:
                frappe.db.commit()
            else:
                frappe.db.rollback()
            return connection, result
        finally:
            frappe.destroy()

    with ThreadPoolExecutor(max_workers=2) as workers:
        outcomes = [job.result(timeout=60) for job in (workers.submit(attempt), workers.submit(attempt))]

    frappe.db.rollback()
    verifier, = frappe.db.sql("SELECT CONNECTION_ID()")[0]
    require(len({verifier, *(connection for connection, _ in outcomes)}) == 3,
            "batch race did not use three independent database connections")
    results = [result for _, result in outcomes]
    require(sum(result.created for result in results) == 1 and results[0].batch == results[1].batch,
            "concurrent batch calls did not produce one creator and one replay")
    winner = results[0].batch
    require(frappe.get_all(MAPPING, filters=filters, pluck="name") == [winner.id]
            and get_batch(sku_id, winner.id) == winner,
            "third transaction did not observe exactly one verified batch mapping")
    native_name = native.resolve_native_batch(sku_id, winner.id)
    require(frappe.db.count("Batch", {"name": native_name}) == 1, "native Batch did not persist exactly once")
    frappe.db.rollback()
    print(f"{filters['kind']}: two independent MariaDB writers, one creation commit, "
          "one read-only replay and a third verification.", flush=True)


def seed_and_verify(site):
    original_stock = _stock_counts()
    frappe.conf[POLICY_SETTING] = CANDIDATE_VERSION
    frappe.db.set_single_value("Stock Settings", "enable_serial_and_batch_no_for_item", 1)
    first, second, race_sku = [_sku_id(suffix) for suffix in ("A", "B", "RACE")]
    bindings = ((first, STD61), (first, STD62), (first, NO_BATCH), (second, STD61), (second, NO_BATCH))
    native_names = set()
    for sku_id, selection in bindings:
        outcome = resolve_or_create(sku_id, selection)
        require(outcome.created and outcome.batch.batch == selection.get("batchNo"),
                "synthetic batch mapping did not round-trip into the domain")
        native_names.add(native.resolve_native_batch(sku_id, outcome.batch.id))
    require(len(native_names) == len(bindings), "different synthetic batches share a native identity")
    frappe.db.commit()
    _race(site, race_sku, NO_BATCH)
    _race(site, first, {"mode": "BATCH", "batchNo": "RACE-STD"})
    require(len(snapshot()["rows"]) == len(snapshot()["native"]) == 7, "unexpected persisted Batch counts")
    require(_stock_counts() == original_stock, "batch fixtures unexpectedly created stock or accounting records")
    print("Synthetic real/NO_BATCH mappings verified. Stock, Bundle, Bin and GL counts remained stable.", flush=True)
