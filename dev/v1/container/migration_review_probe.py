"""V1-16B supplementary review and scale checks on disposable CI sites."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from time import monotonic
from unittest.mock import patch
from uuid import uuid4

import frappe

from access_probe import synthetic_user
from formal_catalog_probe import _ensure_native_units, denied
from identity import require
from migration_probe import _all_groups, _approved, _drain, _explicit_synthetic_status, _group, book
from probe import snapshot


def _upload(source, dataset, sheets, **kwargs):
    from haxpower_v1.migration import service

    return service.upload(source, dataset, [("synthetic.xlsx", book(sheets, source))], **kwargs)


def _approve(task):
    from haxpower_v1.migration import service

    service.approve(task["task"], task["version"], task["digest"], "Verified synthetic migration review")


def _target(task, group):
    from haxpower_v1.migration import service

    return service.execute(task["task"], task["version"], group=group["key"])[0]["target"]


def _legacy(name, desc, *, core="11.125", rpm="1500", version="1", company="SYN32", status="ACTIVE"):
    from haxpower_v1.migration import formats

    return {
        formats.LEGACY_CONFIG: [
            dict(id=key, company_id=company, config_code=key)
            for key in ("SF", "0017", "D28")
        ],
        formats.LEGACY_SPEC: [dict(id="S1", company_id=company, material_spec_code="SF0017D28B006",
                                   material_spec_name=name, material_spec_unit="PC", remark=desc,
                                   status=status, version=version, modify_date="synthetic-" + version)],
        formats.LEGACY_ATTR: [
            dict(id=key, company_id=company, prop_name=label, prop_value=value, material_spec_id="S1")
            for key, label, value in (("A1", "芯径φ(mm)", core), ("A2", "转速(rpm)", rpm))
        ],
        formats.LEGACY_SKU: [],
    }


def _reconcile(dataset):
    from haxpower_v1.catalog import service as products
    from haxpower_v1.migration import formats, plan, service, store

    source = formats.LEGACY
    def upload(name, desc, **kwargs):
        return _upload(source, dataset, _legacy(name, desc, **kwargs))

    first = upload("Synthetic A", "1")
    spec_group = _group(first, "Specifications", "S1")
    require(spec_group["issues"] == ["LEGACY_STATUS_REVIEW_REQUIRED"], "explicit ACTIVE needs disposition")
    generic = service.revise(first["task"], first["version"], [{"keys": [spec_group["key"]], "reason": "Only a note"}])
    require(generic["blocked"], "generic reason waived source status")
    first = _explicit_synthetic_status(generic)
    _approve(first)
    _drain(first)
    identity = _target(first, _group(first, "Specifications", "S1"))
    core = frappe.db.get_value("HP V1 Attribute Definition", {"code": "CORE_DIAMETER"}, "name")

    frappe.db.rollback()
    manual = products.get_specification(identity)
    values = {row.attribute_id: list(row.values) for row in manual.attributes}
    values[core] = ["77.125"]
    products.edit_specification(identity, replace(manual.data, display_name="Synthetic B"),
                                expected_revision=manual.revision, attributes=values)
    frappe.db.commit()

    second = upload("Synthetic A", "2", rpm="1600")
    _approve(second)
    _drain(second)
    metadata = upload("Synthetic A", "2", rpm="1600", version="2")
    _approve(metadata)
    _drain(metadata)
    frappe.db.rollback()
    current = products.get_specification(identity)
    require(current.data.display_name == "Synthetic B" and current.data.description == "2" and
            any(row.attribute_id == core and str(row.values[0]) == "77.125" for row in current.attributes),
            "independent edits were not preserved through another update and metadata replay")

    third = upload("Synthetic C", "3", core="13.125", rpm="1600", version="3")
    group = _group(third, "Specifications", "S1")
    require(set(group["manual_drift"]) == {"display_name", "values:" + core}, "manual baselines were absorbed")
    chosen = service.revise(third["task"], third["version"], [
        {"keys": [group["key"]], "reason": "Keep live name, accept source core diameter",
         "keep_local": ["display_name"], "accept_source": ["values:" + core]},
    ])
    _approve(chosen)
    _drain(chosen)
    frappe.db.rollback()
    result = products.get_specification(identity)
    require(result.data.display_name == "Synthetic B" and result.data.description == "3" and
            any(row.attribute_id == core and str(row.values[0]) == "13.125" for row in result.attributes),
            "mixed reviewed field resolutions did not persist atomically")
    replay = upload("Synthetic C", "3", core="13.125", rpm="1600", version="3")
    _approve(replay)
    _drain(replay)
    frappe.db.rollback()
    require(products.get_specification(identity).revision == result.revision, "same source replay changed revision")

    fourth = upload("Synthetic D", "3", core="13.125", rpm="1600")
    _approve(fourth)
    _drain(fourth)
    frappe.db.rollback()
    require(products.get_specification(identity).data.display_name == "Synthetic D", "explicit keep did not anchor live value")

    # Emulate a mapping written by the audited predecessor. Its complete target
    # snapshot cannot prove which individual fields the reviewer confirmed.
    key = plan.mapping_key(source, {"kind": "Specifications", "namespace": dataset + "/SYN32", "source_id": "S1"})
    old = store.unpack(frappe.db.get_value(store.IDENTITY, key, "evidence"))
    for name in ("baseline", "baseline_complete", "business_source", "reviewed_legacy_status"):
        old.pop(name)
    frappe.db.set_value(store.IDENTITY, key, "evidence", store.pack(old))
    frappe.db.commit()
    fifth = upload("Synthetic D", "4", core="13.125", rpm="1600")
    require("LEGACY_STATUS_REVIEW_REQUIRED" in _group(fifth, "Specifications", "S1")["issues"],
            "pre-fix mapping had no evidence of historical status disposition")
    fifth = _explicit_synthetic_status(fifth)
    require(_group(fifth, "Specifications", "S1")["manual_drift"] == ["description"],
            "pre-fix mapping silently treated the full post-write snapshot as trusted")
    return identity


def _proof(tag):
    from haxpower_v1.migration import formats, service, store, xlsx
    from haxpower_v1.reporting import private_files

    source = formats.STANDARD
    def task_for(suffix):
        return _upload(source, tag + suffix, {"Instructions": [{"version": source}],
            "Brands": [{"key": index, "action": "create", "code": tag + suffix + index,
                        "display_name": "Synthetic evidence brand " + index} for index in ("A", "B")]})

    def path_for(task):
        entry = service.preview(task["task"])["manifest"][0]
        return entry, private_files.filename(entry["id"], area=service.AREA)

    task = task_for("pre")
    _, path = path_for(task)
    original = path.read_bytes()
    path.write_bytes(b"X" + original[1:])
    denied(xlsx.MigrationError, lambda: _approve(task), "MIGRATION_FILE_INTEGRITY")
    path.write_bytes(original)
    _approve(task)
    path.unlink()
    result = service.execute(task["task"], task["version"])[0]
    require(result["state"] == "Invalidated" and result["code"] == "STALE_MIGRATION_FILE_INTEGRITY",
            "approved task executed without source workbook")
    path.write_bytes(original)

    task = task_for("partial")
    _approve(task)
    committed = service.execute(task["task"], task["version"])[0]
    _, path = path_for(task)
    original = path.read_bytes()
    path.write_bytes(b"X" + original[1:])
    blocked = service.execute(task["task"], task["version"])[0]
    require(blocked["code"] == "STALE_MIGRATION_FILE_INTEGRITY" and
            service.execute(task["task"], task["version"], group=committed["group"])[0] == committed,
            "partial run lost its committed receipt or ignored tampering")
    path.write_bytes(original)

    task = task_for("meta")
    entry, _ = path_for(task)
    frappe.db.set_value(store.FILE, entry["id"], "byte_count", entry["size"] + 1)
    frappe.db.commit()
    denied(xlsx.MigrationError, lambda: _approve(task), "MIGRATION_FILE_INTEGRITY")
    frappe.db.set_value(store.FILE, entry["id"], "byte_count", entry["size"])
    frappe.db.commit()
    _approve(task)
    chunk = frappe.db.get_value(store.SOURCE, {"version_id": task["version"]}, "name")
    raw = [{"synthetic": "modified evidence"}]
    frappe.db.set_value(store.SOURCE, chunk, {"evidence": store.pack(raw), "sha256": formats.digest(raw)})
    frappe.db.commit()
    invalid = service.execute(task["task"], task["version"])[0]
    require(invalid["code"] == "STALE_MIGRATION_SOURCE_INTEGRITY", "source chunks were trusted without version proof")

    task = task_for("approval")
    _approve(task)
    frappe.db.set_value(store.APPROVAL, task["version"], "version_id", "0" * 32)
    frappe.db.commit()
    denied(xlsx.MigrationError, lambda: service.execute(task["task"], task["version"]), "MIGRATION_EVIDENCE_INTEGRITY")


def _conflicts(site, actor, settings, tag, spec):
    from haxpower_v1.adapters.erpnext import item
    from haxpower_v1.catalog.sku_persistence import SKU
    from haxpower_v1.migration import formats, plan, service

    source = formats.STANDARD
    header = {"Instructions": [{"version": source}]}
    brand = frappe.db.get_value("HP V1 Brand", {"code": "UNBRANDED"})
    alternate = frappe.db.get_value("HP V1 Brand", {"code": "UNKNOWN-BRAND"})

    def create(kind, index, values):
        return _upload(source, tag + index, {**header, kind: [{"key": "K", "action": "create", **values}]})

    def pair(kind, prefix, payload, *, concurrent=False):
        tasks = [create(kind, prefix + suffix, payload) for suffix in ("A", "B")]
        for task in tasks:
            _approve(task)
        def execute(task):
            return service.execute(task["task"], task["version"])[0]
        if concurrent:
            gate = Barrier(2)
            def worker(task):
                frappe.init(site=site, sites_path=str(Path.cwd()))
                frappe.connect()
                frappe.set_user(actor)
                frappe.conf[item.SETTINGS] = settings
                try:
                    gate.wait(timeout=20)
                    return execute(task)
                finally:
                    frappe.destroy()
            with ThreadPoolExecutor(2) as pool:
                outputs = list(pool.map(worker, tasks))
        else:
            outputs = list(map(execute, tasks))
        require({row["state"] for row in outputs} == {"Invalidated", "Completed"}, f"{kind} target conflict escaped")
        for task, outcome in zip(tasks, outputs):
            group = _group(task, kind, "K")
            frappe.db.rollback()
            identity, _ = plan._map(source, group)
            if outcome["state"] == "Completed":
                require(identity == outcome["target"] and
                        service.execute(task["task"], task["version"], group=group["key"])[0] == outcome,
                        "winning receipt or mapping did not survive retry")
            else:
                require(outcome["code"] == "STALE_TARGET_KEY" and identity is None,
                        "losing plan remapped or created a product")

    payload = {"specification": "id:" + spec, "brand": "id:" + brand, "display_name": "Synthetic unique SKU " + tag}
    duplicate = _upload(source, tag + "dual", {**header, "SKUs": [
        {"key": key, "action": "create", **payload} for key in ("K", "L")
    ]})
    require("DUPLICATE_TARGET_KEY" in _group(duplicate, "SKUs", "L")["issues"], "duplicate target within one plan")
    pair("SKUs", "same", payload)
    alternate_payload = {**payload, "brand": "id:" + alternate, "display_name": payload["display_name"] + " 2"}
    pair("SKUs", "race", alternate_payload, concurrent=True)
    frappe.db.rollback()
    skus = frappe.get_all(SKU, filters={"specification": spec}, fields=["native_item", "brand", "code"])
    require(len(skus) == 2 and {row.brand for row in skus} == {brand, alternate} and len({row.code for row in skus}) == 2
            and all(frappe.db.exists("Item", row.native_item) for row in skus)
            and frappe.db.count("Item", {"item_name": ["in", [payload["display_name"],
                                                             alternate_payload["display_name"]]]}) == 2,
            "SKU pair conflict left wrong native facts")

    complete = _upload(source, tag + "sameA", {**header, "SKUs": []},
                       complete_snapshot={"company": None, "entities": ["SKUs"]})
    missing = [group for group in _all_groups(complete) if group["kind"] == "Missing"]
    require(len(missing) == 1 and missing[0]["source_id"] == "K", "missing SKU not reported")
    reviewed = service.revise(complete["task"], complete["version"], [
        {"keys": [missing[0]["key"]], "reason": "Keep the prior mapped SKU", "ack_missing": True},
    ])
    _approve(reviewed)
    require(service.preview(reviewed["task"])["state"] == "Completed", "missing-only review must complete")

    pair("Brands", "brand", {"code": "R-" + tag, "display_name": "Synthetic conflict brand"}, concurrent=True)
    pair("Options", "option", {"attribute": "code:MODEL", "option_key": "R-" + tag,
                             "display_name": "Synthetic conflict option"}, concurrent=True)
    category = create("Categories", "category", {"code": "R-" + tag, "display_name": "Synthetic conflict category"})
    _approve(category)
    _drain(category)
    pair("Templates", "template", {"category": "code:R-" + tag.upper(), "attribute": "code:MODEL",
                                   "value_mode": "MULTI"}, concurrent=True)


def _snapshot_cases(tag):
    from haxpower_v1.migration import formats, service, store

    source = formats.LEGACY
    company_a, company_b = "SNAP-A", "SNAP-B"
    def dataset(company):
        return _legacy("Synthetic snapshot " + company, "1", company=company)
    both = dataset(company_a)
    for name, rows in dataset(company_b).items():
        both[name] += rows
    for suffix in ("", "alt"):
        task = _explicit_synthetic_status(_upload(source, tag + suffix, both))
        _approve(task)
        _drain(task)
    frappe.db.rollback()
    ids = frappe.db.count(store.IDENTITY)
    empty = dataset(company_b)
    safe = _upload(source, tag, empty)
    require(not any(group["kind"] == "Missing" for group in _all_groups(safe)), "partial file was treated as complete")
    scope = {"company": company_a, "entities": ["Specifications", "SKUs"]}
    complete = _upload(source, tag, empty, complete_snapshot=scope)
    missing = [group for group in _all_groups(complete) if group["kind"] == "Missing"]
    require(len(missing) == 1 and missing[0]["namespace"] == tag + "/" + company_a and
            missing[0]["source_id"] == "S1", "missing diff escaped declared dataset / company / entity")
    review = service.revise(complete["task"], complete["version"], [
        {"keys": [missing[0]["key"]], "reason": "Preserve missing source identity", "ack_missing": True},
    ])
    _approve(review)
    _drain(review)
    frappe.db.rollback()
    require(frappe.db.count(store.IDENTITY) == ids, "missing report modified a historical mapping")
    invalid = dataset(company_a)
    invalid[formats.LEGACY_SPEC].append(dict(invalid[formats.LEGACY_SPEC][0]))
    ambiguous = _upload(source, tag, invalid, complete_snapshot=scope)
    require("SNAPSHOT_IDENTITY_INCOMPLETE" in {issue for group in _all_groups(ambiguous) for issue in group["issues"]}
            and not any(group["kind"] == "Missing" for group in _all_groups(ambiguous)),
            "duplicate ID was interpreted as a missing source identity")


def _run(site, action):
    from haxpower_v1.access.policy import Operation as OP
    from haxpower_v1.adapters.erpnext import item
    from haxpower_v1.catalog.initial_configuration import initialize

    _ensure_native_units()
    tag = uuid4().hex[:8]
    settings = {"item_group": frappe.db.get_value("Item Group", {"is_group": 0}, "name"),
                "uom_precision": {"PC": 0, "SET": 0}}
    actor, _ = synthetic_user(tag, "review-regression", OP.PRODUCT_READ, OP.PRODUCT_MAINTAIN,
                              OP.PARAMETER_MAINTAIN, OP.MIGRATION_OPERATE, OP.MIGRATION_REVIEW,
                              OP.MIGRATION_EXECUTE, OP.EXPORT)
    with patch.dict(frappe.conf, {item.SETTINGS: settings}):
        initialize()
        frappe.db.commit()
        before = snapshot()
        frappe.set_user(actor)
        try:
            action(site, actor, settings, tag)
        finally:
            frappe.set_user("Administrator")
        frappe.db.rollback()
        after = snapshot()
        for key in ("batch", "posted", "operations", "inventory", "approval", "corrections", "warehouse"):
            require(before[key] == after[key], f"migration supplementary test changed stock facts: {key}")


def _review(site, actor, settings, tag):
    spec = _reconcile(tag)
    _proof(tag)
    _conflicts(site, actor, settings, tag, spec)
    _snapshot_cases(tag + "snapshot")
    print("V1-16B architecture review: field anchors, conflicts, integrity, legacy status and snapshot scope verified.")


def run(site):
    _run(site, _review)


def _scale(site, actor, settings, tag):
    from haxpower_v1.catalog.sku_persistence import SKU
    from haxpower_v1.migration import demo, formats, service, store

    with TemporaryDirectory(prefix="hp32-scale-") as path:
        start = monotonic()
        demo.generate(path)
        generated = monotonic() - start
        initial, delta = [(Path(path) / name).read_bytes() for name in ("legacy.xlsx", "legacy-v2.xlsx")]
        dataset = "scale-" + tag
        metrics = []
        snapshots = []
        for label, content in (("initial", initial), ("replay", initial), ("incremental", delta)):
            started = monotonic()
            task = service.upload(formats.LEGACY, dataset, [(label + ".xlsx", content)])
            blocked = task["blocked"]
            task = _explicit_synthetic_status(task)
            require(not task["blocked"], f"scale {label} has unresolved candidates")
            reviewed = monotonic()
            _approve(task)
            _drain(task)
            finished = monotonic()
            groups = _all_groups(task)
            require(service.preview(task["task"])["state"] == "Completed", f"scale {label} missed a group")
            frappe.db.rollback()
            mappings = frappe.get_all(store.IDENTITY, filters={"source_namespace": store.pack(dataset + "/DEMO")},
                                      fields=["name", "entity_type", "target_id"])
            snapshots.append({row.name: row.target_id for row in mappings})
            counts = {kind: sum(row.entity_type == kind for row in mappings)
                      for kind in ("Specifications", "SKUs", "Brands", "Options")}
            require(counts["Specifications"] == (261 if label == "incremental" else 260) and
                    counts["SKUs"] == (550 if label == "incremental" else 548) and counts["Brands"] == 3,
                    f"scale unexpected identities: {counts}")
            metrics.append((label, blocked, len(groups), counts, round(reviewed-started, 2), round(finished-reviewed, 2)))
            print(f"V1-16B scale phase: {metrics[-1]}", flush=True)
        require(snapshots[0] == snapshots[1] and all(snapshots[2][key] == target for key, target in snapshots[0].items()),
                "scale replay or delta replaced stable identities")
        sku_ids = frappe.get_all(store.IDENTITY, filters={"source_namespace": store.pack(dataset + "/DEMO"),
                                                         "entity_type": "SKUs"}, pluck="target_id")
        items = frappe.get_all(SKU, filters={"name": ["in", sku_ids]}, fields=["native_item", "name"])
        native_ids = [row.native_item for row in items]
        require(len(native_ids) == 550 and len(set(native_ids)) == 550 and
                frappe.db.count("Item", {"name": ["in", native_ids]}) == 550 and
                not frappe.db.count("Stock Ledger Entry", {"item_code": ["in", native_ids]}) and
                not frappe.db.count("Bin", {"item_code": ["in", native_ids]}), "scaled SKU native and zero stock evidence failed")
        spec_ids = frappe.get_all(store.IDENTITY, filters={"source_namespace": store.pack(dataset + "/DEMO"),
                                                          "entity_type": "Specifications"}, pluck="target_id")
        values = frappe.db.count("HP V1 Product Attribute Value", {"parent": ["in", spec_ids]})
        history = frappe.db.count("HP V1 Specification Identifier", {"parent": ["in", spec_ids]})
        require(values >= 2080 and history == 261 and frappe.db.count("HP V1 Product Identifier", {
            "parent": ["in", sku_ids], "kind": "LEGACY",
        }) == 550, "technical attributes or lossless legacy rows missing")
        print(f"V1-16B scale generated in {generated:.2f}s, technical={values}, spec_history={history}; "
              f"(phase, initial blocks, groups, mappings, preview/review seconds, execute seconds): {metrics}")


def scale(site):
    _run(site, _scale)
