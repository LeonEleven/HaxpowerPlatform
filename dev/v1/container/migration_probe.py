"""Exercise source evidence, review, atomic catalog groups and stock invariance."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

import frappe

from access_probe import synthetic_user
from formal_catalog_probe import _ensure_native_units, denied
from identity import require
from probe import snapshot


def book(sheets, source):
    from haxpower_v1.migration import formats, xlsx

    stream = BytesIO()
    headers = (formats.HEADERS if source == formats.STANDARD else
               {name: value.split() for name, value in formats.LEGACY_FIELDS.items()})
    xlsx.write(stream, {sheet: [headers[sheet], *([row.get(k) for k in headers[sheet]] for row in rows)]
                        for sheet, rows in sheets.items()})
    return stream.getvalue()


def _group(task, kind, identity):
    from haxpower_v1.migration import service

    offset = 0
    while True:
        page = service.preview(task["task"], task["version"], limit=100, offset=offset)
        for group in page["groups"]:
            if group["kind"] == kind and group["source_id"] == identity:
                return group
        offset += 100
        require(offset < page["total"], f"source identity missing from {kind}")


def _approved(source, dataset, content):
    from haxpower_v1.migration import service

    task = service.upload(source, dataset, [("master.xlsx", content)])
    require(not task["blocked"], "synthetic preview unexpectedly blocked")
    service.approve(task["task"], task["version"], task["digest"], "Synthetic review")
    return task


def _drain(task):
    from haxpower_v1.migration import service

    while True:
        batch = service.execute(task["task"], task["version"], max_groups=25)
        require(all(row["state"] in {"Completed", "Executing"} for row in batch), f"import results {batch}")
        if "group" not in batch[-1]:
            return


def _concurrent(site, task, key, actor, settings):
    from haxpower_v1.adapters.erpnext import item
    from haxpower_v1.migration import service

    gate = Barrier(2)

    def run_once():
        frappe.init(site=site, sites_path=str(Path.cwd()))
        frappe.connect()
        frappe.set_user(actor)
        frappe.conf[item.SETTINGS] = settings
        try:
            gate.wait(timeout=20)
            return service.execute(task["task"], task["version"], group=key)[0]
        finally:
            frappe.destroy()

    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(run_once) for _ in range(2)]
        results = [future.result(timeout=40) for future in futures]
    require(results[0] == results[1] and results[0]["state"] == "Completed",
            "concurrent retry did not return the original receipt")
    return results[0]


def _standard(code, source, brand_code, *, name="Synthetic migration rotor", action="create", sku=True):
    return {
        "Instructions": [{"version": source}],
        "Categories": [{"key": "C", "action": "reference", "target_code": "0011"}],
        "Brands": [{"key": "B", "action": "create", "code": brand_code, "display_name": "Synthetic migration brand"}],
        "Specifications": [{"key": "001", "action": action, "category": "C", "display_name": name, "stock_uom": "PC"}],
        "SKUs": [{"key": "002", "action": "create", "specification": "001", "brand": "B", "display_name": name}]
        if sku else [],
        "Historical Identifiers": [
            {"key": "H1", "target_type": "Specifications", "target": "001", "source": "SYNTHETIC", "value": code},
            *([{"key": "H2", "target_type": "SKUs", "target": "002", "source": "SYNTHETIC", "value": '00<old>\\-"'}]
              if sku else []),
        ],
    }


def _custom_template(source, tag):
    heading = "Distinct synthetic template"
    return {
        "Instructions": [{"version": source}],
        "Categories": [{"key": "C", "action": "create", "code": f"CLASS-{tag}", "display_name": heading}],
        "Attributes": [{"key": "A", "action": "create", "code": f"ATTR-{tag}", "display_name": heading,
                        "data_type": "MULTI_SELECT"}],
        "Options": [
            {"key": key, "action": "create", "attribute": "A", "option_key": key, "display_name": key}
            for key in ("0001", "0002")
        ],
        "Templates": [{"key": "T", "action": "create", "category": "C", "attribute": "A",
                       "is_required": True, "value_mode": "MULTI", "searchable": True}],
        "Specifications": [{"key": "S", "action": "create", "category": "C", "stock_uom": "SET",
                            "display_name": heading}],
        "Specification Values": [{"key": key, "specification": "S", "attribute": "A", "option": key}
                                 for key in ("0001", "0002")],
    }


def run(site):
    from haxpower_v1.access.policy import Operation as OP
    from haxpower_v1.adapters.erpnext import item
    from haxpower_v1.catalog import service as products, sku
    from haxpower_v1.catalog.initial_configuration import initialize
    from haxpower_v1.catalog.persistence import SPECIFICATION
    from haxpower_v1.catalog.sku_persistence import SKU
    from haxpower_v1.migration import demo, formats, service, store, xlsx
    from haxpower_v1.reporting import exports

    _ensure_native_units()
    tag = uuid4().hex[:8]
    source = formats.STANDARD
    actor, _ = synthetic_user(tag, "migrator", OP.PRODUCT_READ, OP.PRODUCT_MAINTAIN, OP.PARAMETER_MAINTAIN,
                              OP.MIGRATION_OPERATE, OP.MIGRATION_REVIEW, OP.MIGRATION_EXECUTE, OP.EXPORT)
    reader, _ = synthetic_user(tag, "reader", OP.PRODUCT_READ)
    reviewer, _ = synthetic_user(tag, "reviewer", OP.PRODUCT_READ, OP.MIGRATION_REVIEW)
    executor, _ = synthetic_user(tag, "executor", OP.PRODUCT_READ, OP.MIGRATION_EXECUTE, OP.PRODUCT_MAINTAIN)
    parameter, _ = synthetic_user(tag, "parameter", OP.PRODUCT_READ, OP.MIGRATION_EXECUTE, OP.PARAMETER_MAINTAIN)
    settings = {"item_group": "All Item Groups", "uom_precision": {"PC": 0, "SET": 0}}
    groups = frappe.get_all("Item Group", filters={"is_group": 0}, pluck="name", order_by="name")
    require(bool(groups), "expected a native Item Group fixture")
    settings["item_group"] = groups[0]
    with patch.dict(frappe.conf, {item.SETTINGS: settings}):
        initialize()
        frappe.db.commit()
        before = snapshot()
        before_catalog = (frappe.db.count(SPECIFICATION), frappe.db.count(SKU))
        frappe.set_user(actor)
        try:
            content = book(_standard("000<old>\\spec", source, f"DR-{tag}"), source)
            task = service.upload(source, tag, [("master.xlsx", content)])
            denied(xlsx.MigrationError, lambda: service.execute(task["task"], task["version"]), "MIGRATION_NOT_APPROVED")
            view = service.preview(task["task"])
            require(not task["blocked"] and before_catalog == (frappe.db.count(SPECIFICATION), frappe.db.count(SKU)),
                    "preview created catalog facts or failed validation")
            raw = service.source_rows(task["task"], task["version"], limit=20)
            require(any(row["cells"].get("key") == {"kind": "text", "value": "001"} for row in raw),
                    "source type or leading zeros were lost")
            original = view["manifest"][0]["id"]
            require(service.download(task["task"], original)[1] == content, "private source bytes changed")
            denied(xlsx.MigrationError, lambda: service.approve(task["task"], task["version"], "0" * 64, "wrong"),
                   "MIGRATION_VERSION_INACTIVE")
            report = service.report(task["task"], task["version"])
            require(service.download(task["task"], report["id"])[1].startswith(b"PK"), "private report unavailable")
            frappe.set_user(reader)
            denied(frappe.PermissionError, lambda: service.preview(task["task"]))
            denied(frappe.PermissionError, lambda: service.download(task["task"], original))
            frappe.set_user(reviewer)
            service.approve(task["task"], task["version"], task["digest"], "Reviewed synthetic workbook")
            denied(frappe.PermissionError, lambda: service.execute(task["task"], task["version"]))
            frappe.set_user(actor)
            keys = {group["kind"]: group["key"] for group in view["groups"]}
            execute = lambda kind: service.execute(task["task"], task["version"], group=keys[kind])[0]
            original_apply = service._apply
            attempts = iter([True, False])

            def retry_once(*args):
                if next(attempts):
                    raise frappe.QueryDeadlockError("Synthetic retry")
                return original_apply(*args)

            with patch.object(service, "_apply", side_effect=retry_once):
                require(execute("Categories")["state"] == "Completed", "group did not retry a deadlock")
            frappe.set_user(executor)
            denied(frappe.PermissionError, lambda: execute("Brands"))
            frappe.set_user(parameter)
            execute("Brands")
            denied(frappe.PermissionError, lambda: execute("Specifications"))
            frappe.set_user(actor)
            spec_id = execute("Specifications")["target"]

            with patch.object(item, "bind_item", side_effect=frappe.ValidationError("synthetic injected error")):
                require(execute("SKUs")["state"] == "Failed", "Item failure was not persisted")
            with patch.object(service, "_write_mapping", side_effect=RuntimeError("synthetic mapping failure")):
                require(execute("SKUs")["state"] == "Failed", "mapping failure was not persisted")
            frappe.db.rollback()
            require(not frappe.db.exists(SKU, {"specification": spec_id}) and
                    not frappe.db.exists("Item", {"item_name": "Synthetic migration rotor"}),
                    "failed mapping left a product or Item")
            frappe.set_user("Administrator")
            frappe.db.delete("Has Role", {"parent": actor, "role": OP.MIGRATION_EXECUTE.role})
            frappe.db.commit()
            frappe.set_user(actor)
            denied(frappe.PermissionError, lambda: execute("SKUs"))
            frappe.set_user("Administrator")
            frappe.get_doc("User", actor).append("roles", {"role": OP.MIGRATION_EXECUTE.role}).db_insert()
            frappe.db.commit()
            frappe.set_user(actor)

            imported = _concurrent(site, task, keys["SKUs"], actor, settings)
            require(execute("SKUs") == imported and service.preview(task["task"])["state"] == "Completed",
                    "lost-response retry or failure recovery changed the result")
            frappe.db.rollback()
            current_sku = sku.get_sku(imported["target"])
            require(current_sku.data.specification == spec_id and current_sku.mapping_status == "Verified" and
                    current_sku.identifiers[0].value == '00<old>\\-"', "SKU source identity or history changed")
            require(products.get_specification(spec_id).historical_identifiers[0].value == "000<old>\\spec",
                    "specification history was changed")
            found, total = sku.search_sku_ids("000<old>\\spec", limit=1, offset=0)
            require(current_sku.id in found and total >= 1, "authorized historical search failed")
            exported = exports.generate("products", {"sku_code": current_sku.data.code})
            require(exported["rows"] == 1 and exports.download(exported["export_id"])[1][:2] == b"PK",
                    "historical product export unavailable")

            replay = _approved(source, tag, content)
            require(all(group["action"] == "unchanged" for group in service.preview(replay["task"])["groups"]),
                    "repeated source identities did not resolve as unchanged")
            _drain(replay)
            increment = _standard("000<old>\\spec", source, f"DR-{tag}", name="Synthetic source revision",
                                  action="update", sku=False)
            changed = _approved(source, tag, book(increment, source))
            spec_group = _group(changed, "Specifications", "001")
            prior = _group(changed, "Categories", "C")
            service.execute(changed["task"], changed["version"], group=prior["key"])
            frappe.db.rollback()
            require(spec_group["action"] == "update", "source revision did not show a diff")
            old = products.get_specification(spec_id)
            products.edit_specification(spec_id, replace(old.data, display_name="Manual live edit"),
                                        expected_revision=old.revision)
            frappe.db.commit()
            require(service.execute(changed["task"], changed["version"], group=spec_group["key"])[0]["state"]
                    == "Invalidated", "target revision change did not invalidate approval")
            new = service.revise(changed["task"], changed["version"], [])
            require(new["blocked"] and _group(new, "Specifications", "001")["issues"] == ["MANUAL_TARGET_EDIT"],
                    "three-way conflict not reported")
            revised = service.revise(new["task"], new["version"],
                                     [{"keys": [spec_group["key"]], "reason": "Reviewed the manual edit"}])
            service.approve(revised["task"], revised["version"], revised["digest"], "Approved source revision")
            _drain(revised)
            frappe.db.rollback()
            require(products.get_specification(spec_id).data.display_name == "Synthetic source revision",
                    "reviewed update not applied")
            increment["Specifications"][0]["stock_uom"] = "SET"
            bad = service.upload(source, tag, [("master.xlsx", book(increment, source))])
            require("IMMUTABLE_TARGET_IDENTITY" in _group(bad, "Specifications", "001")["issues"],
                    "immutable unit change was not blocked")
            require(sku.get_sku(imported["target"]).data.stock_uom == "PC", "existing SKU changed units")

            custom = _approved(source, "custom-" + tag, book(_custom_template(source, tag), source))
            _drain(custom)
            custom_group = _group(custom, "Specifications", "S")
            custom_result = service.execute(custom["task"], custom["version"], group=custom_group["key"])[0]
            frappe.db.rollback()
            custom_spec = products.get_specification(custom_result["target"])
            require(custom_spec.data.stock_uom == "SET" and
                    [option.key for option in custom_spec.attributes[0].values] == ["0001", "0002"],
                    "new category template, option keys or stock unit drifted during import")

            brands = _standard("Distinct brand identities", source, f"UNUSED-{tag}", sku=False)
            brands["Brands"] = [
                {"key": code, "action": "reference", "target_code": code}
                for code in ("UNBRANDED", "UNKNOWN-BRAND")
            ]
            brands["SKUs"] = [
                {"key": code, "action": "create", "specification": "001", "brand": code,
                 "display_name": "Synthetic distinct special brand"}
                for code in ("UNBRANDED", "UNKNOWN-BRAND")
            ]
            brand_case = _approved(source, "brands-" + tag, book(brands, source))
            _drain(brand_case)
            frappe.db.rollback()
            imported_brands = []
            for code in ("UNBRANDED", "UNKNOWN-BRAND"):
                brand_group = _group(brand_case, "SKUs", code)
                outcome = service.execute(brand_case["task"], brand_case["version"], group=brand_group["key"])[0]
                imported_brands.append(sku.get_sku(outcome["target"]))
            require({row.brand.data.code for row in imported_brands} == {"UNBRANDED", "UNKNOWN-BRAND"} and
                    len({row.data.specification for row in imported_brands}) == 1,
                    "explicit no-brand and unknown brand were merged")

            legacy, _ = demo._synthetic(small=True)
            raw = book(legacy, formats.LEGACY)
            legacy_task = service.upload(formats.LEGACY, "errors-" + tag, [("old.xlsx", raw)])
            require(legacy_task["blocked"] > 0, "legacy anomalies failed to block approval")
            legacy_preview = service.preview(legacy_task["task"], limit=100)
            legacy_groups = list(legacy_preview["groups"])
            for offset in range(100, legacy_preview["total"], 100):
                legacy_groups.extend(service.preview(legacy_task["task"], offset=offset, limit=100)["groups"])
            issues = {issue for group in legacy_groups for issue in group["issues"]}
            require({"DUPLICATE_OR_INVALID_LEGACY_ID", "EXPLICIT_BRAND_REQUIRED", "MISSING_SPECIFICATION",
                     "DUPLICATE_ATTRIBUTE_VALUE", "MISSING_OPTIONAL_TECHNICAL_VALUES", "SINGLE_VALUE_CONFLICT"} <= issues,
                    f"legacy anomaly evidence incomplete: {issues}")
            denied(xlsx.MigrationError, lambda: service.approve(
                legacy_task["task"], legacy_task["version"], legacy_task["digest"], "Synthetic"), "UNRESOLVED")

            full, _ = demo._synthetic()
            spec_rows = full[formats.LEGACY_SPEC][:13]
            spec_keys = {row["id"] for row in spec_rows}
            legal = {**full, formats.LEGACY_SPEC: spec_rows}
            for sheet in (formats.LEGACY_ATTR, formats.LEGACY_SKU):
                legal[sheet] = [row for row in full[sheet] if row["material_spec_id"] in spec_keys]
            legacy_ok = _approved(formats.LEGACY, tag, book(legal, formats.LEGACY))
            _drain(legacy_ok)
            require(service.preview(legacy_ok["task"])["state"] == "Completed",
                    "valid four-table migration did not complete")
            legacy_spec = _group(legacy_ok, "Specifications", spec_rows[0]["id"])
            legacy_result = service.execute(legacy_ok["task"], legacy_ok["version"], group=legacy_spec["key"])[0]
            frappe.db.rollback()
            formal_spec = products.get_specification(legacy_result["target"])
            require(any(len(attr.values) > 1 for attr in formal_spec.attributes) and
                    formal_spec.historical_identifiers[0].value == spec_rows[0]["material_spec_code"],
                    "legacy multiselect values or identifier were lost")

            changed_decimal = next(row for row in legal[formats.LEGACY_ATTR] if row["prop_name"] == "芯径φ(mm)")
            changed_decimal["prop_value"] = "13.026"
            attr_delta = _approved(formats.LEGACY, tag, book(legal, formats.LEGACY))
            _drain(attr_delta)
            updated_group = _group(attr_delta, "Specifications", changed_decimal["material_spec_id"])
            frappe.db.rollback()
            audits = products.get_specification_audit(updated_group["target"])
            require(any(str(entry.after.values[0]) == "13.026" for entry in audits if entry.after.values),
                    "exact legacy Decimal update or native catalog attribute audit is missing")

            frappe.set_user("Administrator")
            frappe.db.delete("Has Role", {"parent": actor, "role": OP.MIGRATION_OPERATE.role})
            frappe.db.commit()
            frappe.set_user(actor)
            denied(frappe.PermissionError, lambda: service.download(task["task"], original))
            denied(frappe.PermissionError, lambda: service.download(task["task"], report["id"]))
            denied(frappe.PermissionError, lambda: service.upload(source, tag, [("master.xlsx", content)]))
        finally:
            frappe.set_user("Administrator")
        # A fresh read also compares exact rows, not just aggregate stock.
        frappe.db.rollback()
        after = snapshot()
        for key in ("batch", "posted", "operations", "inventory", "approval", "corrections", "warehouse"):
            require(before[key] == after[key], f"migration altered stock or workflow facts in {key}")
        for table, facts in before["sku"]["tables"].items():
            remaining = {row["name"]: row for row in after["sku"]["tables"][table]["rows"]}
            require(all(remaining[row["name"]] == row for row in facts["rows"]), f"preexisting {table} changed")
        native_items = {row["name"]: row for row in after["sku"]["native_items"]}
        require(all(native_items[row["name"]] == row for row in before["sku"]["native_items"]),
                "a preexisting ERPNext Item changed during migration")
        require(frappe.db.count(store.IDENTITY) > 0, "no durable migration identity recorded")
        print("V1-16B spreadsheet review, native/mapping fault rollback, concurrent retries, source deltas, "
              "permission revocation and exact stock facts verified.")

def recovered():
    from haxpower_v1.access.policy import Operation as OP
    from haxpower_v1.migration import service, store

    entry = frappe.get_all(store.FILE, filters={"purpose": "source"},
                           fields=["name", "task_id", "sha256"], order_by="creation asc", limit=1)[0]
    task = frappe.get_doc(store.TASK, entry.task_id)
    require(task.state == "Completed", "recovered task receipt state changed")
    actor, _ = synthetic_user(uuid4().hex[:8], "recovered", OP.PRODUCT_READ, OP.MIGRATION_OPERATE)
    frappe.db.commit()
    frappe.set_user(actor)
    try:
        from hashlib import sha256

        content = service.download(task.name, entry.name)[1]
        require(sha256(content).hexdigest() == entry.sha256 and
                service.preview(task.name)["state"] == "Completed", "recovered source or approval unavailable")
    finally:
        frappe.set_user("Administrator")
    print("Recovered V1-16B private source and completed task verified under current authorization.")
