"""Authorized synchronous backend entry points for master data migration."""

from dataclasses import asdict
from pathlib import Path
from time import sleep
from uuid import uuid4

import frappe
from frappe.utils import now_datetime

from ..access import policy
from ..catalog.persistence import SPECIFICATION
from ..inventory.operations.transaction import connection
from ..reporting import private_files
from . import catalog, formats, plan, store, xlsx

OP = policy.Operation
AREA = private_files.Area.MIGRATION
MAX_GROUP_BATCH = 25
DEADLOCK_RETRIES = 3
MAX_PAGE = 100


def _permission(operation, *, lock=False):
    policy.require_operation(operation, locked=lock)


def _view():
    grants = policy.granted_operations()
    if OP.PRODUCT_READ not in grants or not grants.intersection({
        OP.MIGRATION_OPERATE, OP.MIGRATION_REVIEW, OP.MIGRATION_EXECUTE,
    }):
        policy.deny()


def _authorize(operation, *, lock=False):
    if not {operation, OP.PRODUCT_READ} <= policy.granted_operations(locked=lock):
        policy.deny()


def _catalog_lock():
    if not frappe.db.get_value("DocType", SPECIFICATION, "name", for_update=True):
        raise xlsx.MigrationError("CATALOG_SCHEMA_MISSING")


def _valid_id(value):
    return policy._record_id(value)


def _save_file(task_id, source, name, content, pending, *, purpose="source", identity=None):
    identity = identity or uuid4().hex
    pending.append(identity)
    size, digest = private_files.store(identity, lambda stream: stream.write(content), area=AREA)
    store.insert(store.FILE, identity, task_id=task_id, source_version=source, original_name=store.pack(name),
                 sha256=digest, byte_count=size, purpose=purpose)
    return {"id": identity, "name": name, "sha256": digest, "size": size}


def _file(task_id, file_id, *, expected=None, for_download=False):
    entry = store.record(store.FILE, _valid_id(file_id))
    if entry.task_id != task_id:
        policy.deny()
    if for_download and entry.purpose == "report":
        _permission(OP.EXPORT)
    name = store.unpack(entry.original_name)
    if expected and (name, entry.sha256, entry.byte_count) != (
            expected["name"], expected["sha256"], expected["size"]):
        raise xlsx.MigrationError("MIGRATION_FILE_INTEGRITY")
    return name, private_files.read(entry.name, entry.byte_count, entry.sha256, area=AREA)


def _remove_uncommitted(pending):
    for identity in pending:
        # After a connection failure at commit, preserve any committed evidence.
        with connection(read_committed=True):
            if not frappe.db.get_value(store.FILE, identity, "name"):
                private_files.filename(identity, area=AREA).unlink(missing_ok=True)


def _read_uploads(uploads):
    if not isinstance(uploads, (list, tuple)) or not 1 <= len(uploads) <= xlsx.MAX_FILES:
        raise xlsx.MigrationError("WORKBOOK_COUNT")
    supplied = []
    for entry in uploads:
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            raise xlsx.MigrationError("WORKBOOK_FORMAT")
        filename, content = entry
        if type(content) is not bytes or not 0 < len(content) <= xlsx.MAX_FILE:
            raise xlsx.MigrationError("WORKBOOK_FILE_LIMIT")
        supplied.append((uuid4().hex, filename, content))
    return supplied


def _decisions(records, candidates, actor):
    if not isinstance(records, list) or len(records) > len(candidates):
        raise xlsx.MigrationError("INVALID_REVIEW_DECISION")
    known = {candidate.key: candidate for candidate in candidates}
    output = {}
    allowed = {"keys", "reason", "skip", "target_id", "target_code", "fields", "drop_rows", "action"}
    for entry in records:
        if not isinstance(entry, dict) or set(entry) - allowed:
            raise xlsx.MigrationError("INVALID_REVIEW_DECISION")
        keys, reason = entry.get("keys"), entry.get("reason")
        if (type(keys) is not list or not keys or not isinstance(reason, str) or
                not reason.strip() or len(reason) > 1000):
            raise xlsx.MigrationError("INVALID_REVIEW_DECISION")
        patch = entry.get("fields", {})
        drops = entry.get("drop_rows", [])
        if (not isinstance(patch, dict) or type(drops) is not list or
                any(not isinstance(row, str) for row in drops) or
                type(entry.get("skip", False)) is not bool or
                entry.get("action", "reference") not in ("reference", "update") or
                any(value is not None and (not isinstance(value, str) or len(value) > 140)
                    for value in (entry.get("target_id"), entry.get("target_code")))):
            raise xlsx.MigrationError("INVALID_REVIEW_DECISION")
        for key in keys:
            if not isinstance(key, str) or key not in known or key in output:
                raise xlsx.MigrationError("INVALID_REVIEW_DECISION")
            candidate = known[key]
            valid_fields = set(formats.FIELDS.get(candidate.kind, "").split())
            if candidate.kind == "Specifications":
                valid_fields.add("values")
            if set(patch) - valid_fields:
                raise xlsx.MigrationError("INVALID_REVIEW_DECISION")
            for name, value in patch.items():
                if name == "values":
                    if (not isinstance(value, list) or len(value) > 256 or any(
                        not isinstance(row, dict) or set(row) != {"attribute", "value", "option", "row", "type"}
                        or row["row"] not in candidate.rows or row["type"] not in ("text", "number", "boolean", "date")
                        or not isinstance(row["attribute"], str)
                        or any(type(data) not in (str, bool, type(None)) or
                               isinstance(data, str) and len(data) > xlsx.MAX_CELL for data in row.values())
                        for row in value
                    )):
                        raise xlsx.MigrationError("INVALID_REVIEW_DECISION")
                elif name in formats.BOOLEANS:
                    if type(value) is not bool:
                        raise xlsx.MigrationError("INVALID_REVIEW_DECISION")
                elif name in formats.INTEGERS:
                    if type(value) is not int or not 0 <= value <= 2_147_483_647:
                        raise xlsx.MigrationError("INVALID_REVIEW_DECISION")
                elif not isinstance(value, str) or len(value) > xlsx.MAX_CELL:
                    raise xlsx.MigrationError("INVALID_REVIEW_DECISION")
            output[key] = {**{name: value for name, value in entry.items() if name != "keys"},
                           "actor": actor, "at": now_datetime().isoformat(), "rows": candidate.rows}
    return output


def _preview(task, parent, manifest, rows, entries, decisions):
    with store.quiet():
        groups = plan.Preview(task.source_version, entries, decisions).run()
    evidence = {
        "task": task.name, "parent": parent, "manifest": manifest, "decisions": decisions,
        "groups": groups, "source": {"format": task.source_version, "dataset": task.dataset},
    }
    identity = store.insert(store.VERSION, task_id=task.name, parent_version=parent,
                            manifest=store.pack(manifest), decisions=store.pack(decisions),
                            plan=store.pack(groups), plan_digest=formats.digest(evidence))
    for ordinal in range(0, len(rows), 64):
        raw = [asdict(row) for row in rows[ordinal:ordinal + 64]]
        store.insert(store.SOURCE, version_id=identity, ordinal=ordinal, evidence=store.pack(raw),
                     sha256=formats.digest(raw))
    store.update(store.TASK, task.name, {"state": "Pending Review", "current_version": identity})
    return {"task": task.name, "version": identity, "digest": formats.digest(evidence),
            "groups": len(groups), "blocked": sum(group["action"] == "blocked" for group in groups)}


def _version(task, version):
    entry = store.record(store.VERSION, _valid_id(version))
    if entry.task_id != task.name:
        policy.deny()
    data = {
        "task": task.name, "parent": entry.parent_version or None, "manifest": store.unpack(entry.manifest),
        "decisions": store.unpack(entry.decisions), "groups": store.unpack(entry.plan),
        "source": {"format": task.source_version, "dataset": task.dataset},
    }
    store.verify(data, entry.plan_digest)
    return entry, data


def upload(source, dataset, uploads, *, task_id=None, expected_version=None):
    """Create a task or append replacement files and an unapproved preview."""
    _authorize(OP.MIGRATION_OPERATE)
    supplied = _read_uploads(uploads)
    rows, entries = formats.parse(source, dataset, supplied)
    pending = []
    try:
        with connection(read_committed=True) as db:
            _authorize(OP.MIGRATION_OPERATE, lock=True)
            _catalog_lock()
            if task_id:
                task = store.record(store.TASK, _valid_id(task_id), lock=True)
                if (task.source_version, task.dataset) != (source, dataset):
                    raise xlsx.MigrationError("TASK_SOURCE_MISMATCH")
                _current(task, expected_version, allow_completed=True)
            else:
                task_id = store.insert(store.TASK, source_version=source, dataset=dataset, state="Draft")
                task = store.record(store.TASK, task_id)
            manifest = [_save_file(task_id, source, filename, content, pending, identity=identity)
                        for identity, filename, content in supplied]
            response = _preview(task, task.current_version or None, manifest, rows, entries, {})
            db.commit()
            return response
    except Exception:
        if pending:
            _remove_uncommitted(pending)
        raise


def _current(task, version, *, allow_completed=False):
    if task.current_version != _valid_id(version) or (not allow_completed and task.state == "Completed"):
        raise xlsx.MigrationError("MIGRATION_VERSION_INACTIVE")


def revise(task_id, expected_version, decisions):
    """Append an edited preview. Decisions in this call replace earlier decisions."""
    with connection(read_committed=True) as db:
        _authorize(OP.MIGRATION_REVIEW, lock=True)
        _catalog_lock()
        task = store.record(store.TASK, _valid_id(task_id), lock=True)
        _current(task, expected_version)
        _, data = _version(task, expected_version)
        supplied = [(entry["id"], *_file(task.name, entry["id"], expected=entry)) for entry in data["manifest"]]
        rows, candidates = formats.parse(task.source_version, task.dataset, supplied)
        validated = _decisions(decisions, candidates, policy.active_actor(locked=True))
        response = _preview(task, expected_version, data["manifest"], rows, candidates, validated)
        db.commit()
        return response


def approve(task_id, version, digest, reason):
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
        raise xlsx.MigrationError("REVIEW_REASON_REQUIRED")
    with connection(read_committed=True) as db:
        _authorize(OP.MIGRATION_REVIEW, lock=True)
        task = store.record(store.TASK, _valid_id(task_id), lock=True)
        _current(task, version)
        entry, data = _version(task, version)
        if task.state != "Pending Review" or entry.plan_digest != digest:
            raise xlsx.MigrationError("MIGRATION_VERSION_INACTIVE")
        if any(group["action"] == "blocked" for group in data["groups"]):
            raise xlsx.MigrationError("UNRESOLVED_MIGRATION_GROUP")
        if not any(group["action"] != "skip" for group in data["groups"]):
            raise xlsx.MigrationError("EMPTY_EXECUTION_PLAN")
        evidence = {"actor": policy.active_actor(locked=True), "at": now_datetime().isoformat(), "reason": reason,
                    "digest": digest, "version": version}
        store.insert(store.APPROVAL, version, version_id=version, plan_digest=digest,
                     evidence=store.pack(evidence), sha256=formats.digest(evidence))
        store.update(store.TASK, task.name, {"state": "Approved"})
        db.commit()
        return evidence


def preview(task_id, version=None, *, offset=0, limit=50):
    with connection(read_committed=True):
        _view()
        task = store.record(store.TASK, _valid_id(task_id))
        entry, data = _version(task, version or task.current_version)
        _page(offset, limit)
        selected = data["groups"][offset:offset + limit]
        approvals = frappe.db.get_value(store.APPROVAL, entry.name, ["evidence", "sha256"], as_dict=True)
        approval = store.unpack(approvals.evidence) if approvals else None
        if approvals:
            store.verify(approval, approvals.sha256)
        return {"task": task.name, "state": task.state, "current": entry.name == task.current_version,
                "version": entry.name, "digest": entry.plan_digest, "manifest": data["manifest"],
                "source": data["source"], "parent_version": data["parent"], "approval": approval,
                "total": len(data["groups"]), "groups": selected,
                "decisions": {group["key"]: data["decisions"][group["key"]] for group in selected
                              if group["key"] in data["decisions"]},
                "results": _recent_results(entry.name, [group["key"] for group in selected])}


def _page(offset, limit):
    if type(offset) is not int or not 0 <= offset <= 10000 or type(limit) is not int or not 1 <= limit <= MAX_PAGE:
        raise xlsx.MigrationError("MIGRATION_PAGE_LIMIT")


def source_rows(task_id, version, *, offset=0, limit=50):
    _page(offset, limit)
    with connection(read_committed=True):
        _view()
        task = store.record(store.TASK, _valid_id(task_id))
        entry, _ = _version(task, version)
        chunks = frappe.get_all(
            store.SOURCE,
            filters={"version_id": entry.name, "ordinal": ["between", [offset // 64 * 64, offset + limit - 1]]},
            fields=["evidence", "sha256"], order_by="ordinal asc",
        )
        rows = []
        for chunk in chunks:
            records = store.unpack(chunk.evidence)
            store.verify(records, chunk.sha256)
            rows.extend(records)
        start = offset % 64
        return rows[start:start + limit]


def download(task_id, file_id):
    with connection(read_committed=True):
        _authorize(OP.MIGRATION_OPERATE)
        store.record(store.TASK, _valid_id(task_id))
        result = _file(task_id, file_id, for_download=True)
        _authorize(OP.MIGRATION_OPERATE)
        if frappe.db.get_value(store.FILE, file_id, "purpose") == "report":
            _permission(OP.EXPORT)
        return result


def report(task_id, version):
    """Persist a private XLSX overview. Details remain available via paged evidence."""
    from io import BytesIO

    pending = []
    try:
        with connection(read_committed=True) as db:
            _authorize(OP.MIGRATION_OPERATE, lock=True)
            _permission(OP.EXPORT, lock=True)
            task = store.record(store.TASK, _valid_id(task_id))
            entry, data = _version(task, version)
            results = _recent_results(entry.name)
            headers = ["group", "entity", "namespace", "source identity", "disposition", "target", "issues", "result"]
            rows = ([group["key"], group["kind"], group["namespace"], group["source_id"], group["action"],
                     results.get(group["key"], {}).get("target") or group["target"], ", ".join(group["issues"]),
                     results.get(group["key"], {}).get("state")] for group in data["groups"])
            stream = BytesIO()
            from itertools import chain
            xlsx.write(stream, {"Migration": chain([headers], rows)})
            metadata = _save_file(task.name, task.source_version, f"migration-{version}.xlsx",
                                  stream.getvalue(), pending, purpose="report")
            db.commit()
            return metadata
    except Exception:
        if pending:
            _remove_uncommitted(pending)
        raise


def _recent_results(version, keys=None):
    if keys == []:
        return {}
    filters = {"version_id": version}
    if keys is not None:
        filters["group_key"] = ["in", keys]
    rows = frappe.get_all(store.RESULT, filters=filters, fields=["group_key", "evidence", "sha256"],
                          order_by="creation asc, name asc")
    recent = {}
    for row in rows:
        result = store.unpack(row.evidence)
        store.verify(result, row.sha256)
        recent[row.group_key] = result
    return recent


def _resolve(value, results):
    return results[value["plan"]]["target"] if isinstance(value, dict) else value


def _guard(group, results):
    for dependency in group["dependencies"]:
        result = results[dependency]
        if formats.digest(catalog.snapshot(result["kind"], result["target"])) != result["target_digest"]:
            raise catalog.Blocked("STALE_DEPENDENCY")
    for identity, (kind, previous) in group["guards"].items():
        if formats.digest(catalog.snapshot(kind, identity)) != previous:
            raise catalog.Blocked("STALE_DEPENDENCY")
    if group["target"] and catalog.snapshot(group["kind"], group["target"]) != group["before"]:
        raise catalog.Blocked("STALE_TARGET")
    if group.get("unit"):
        try:
            current = catalog.unit(group["unit"][0])
        except frappe.ValidationError:
            raise catalog.Blocked("STALE_UNIT_CONFIGURATION") from None
        if current != group["unit"][1]:
            raise catalog.Blocked("STALE_UNIT_CONFIGURATION")
    if (category := group.get("template_category")) is not None:
        category = _resolve(category, results)
        expected = dict(group.get("template_guard", {}))
        expected.update({results[key]["target"]: results[key]["target_digest"] for key in group["template_groups"]})
        if catalog.template(category) != expected:
            raise catalog.Blocked("STALE_CATEGORY_TEMPLATE")


def _write_mapping(source, group, target, state, version, approved):
    identity = plan.mapping_key(source, group)
    current_target, previous = plan._map(source, group)
    if current_target and current_target != target:
        raise catalog.Blocked("SOURCE_IDENTITY_ALREADY_BOUND")
    if group["action"] == "unchanged" and previous.get("source") == group["source"]:
        # Confirm the version without adopting manual local edits as source state.
        store.update(store.IDENTITY, identity, {"latest_version": version})
        return
    evidence = {"source": group["source"], "target": state, "plan_digest": approved["digest"],
                "reviewer": approved["actor"], "reviewed_at": approved["at"], "rows": group["rows"],
                "executor": policy.active_actor(locked=True)}
    values = dict(target_id=target, latest_version=version,
                  source_digest=formats.digest(group["source"]), evidence=store.pack(evidence))
    if current_target:
        store.update(store.IDENTITY, identity, values)
    else:
        store.insert(store.IDENTITY, identity, source_system=formats.SOURCE_SYSTEM[source], source_namespace=store.pack(group["namespace"]),
                     entity_type=group["kind"], source_id=store.pack(group["source_id"]),
                     first_version=version, **values)


def _apply(task, version, group, results, approved):
    _guard(group, results)
    current_target, previous = plan._map(task.source_version, group)
    if ((formats.digest(previous) if previous else None) != group["prior_mapping"] or
            current_target and current_target != group["target"]):
        raise catalog.Blocked("STALE_SOURCE_IDENTITY")
    kind = group["kind"]
    payload = {name: _resolve(value, results) for name, value in group["payload"].items()
               if name not in formats.DERIVED_FIELDS}
    if group["action"] == "create" and catalog.matching(kind, payload):
        raise catalog.Blocked("STALE_TARGET_KEY")
    if "values" in group["payload"]:
        values = {}
        for attribute, value in group["payload"]["values"]:
            values.setdefault(_resolve(attribute, results), []).append(_resolve(value, results))
        payload["values"] = values
    if "history" in group["payload"]:
        payload["history"] = group["payload"]["history"]
    try:
        target = catalog.apply(group, catalog.merge(kind, payload, group["before"]))
    except (frappe.QueryDeadlockError, frappe.PermissionError):
        raise
    except frappe.ValidationError:
        raise catalog.Blocked("CATALOG_WRITE_REJECTED") from None
    state = catalog.snapshot(kind, target)
    if group["action"] in {"create", "update"}:
        for field, expected in payload.items():
            if field == "history":
                if any(row not in state["history"] for row in expected):
                    raise catalog.Blocked("CATALOG_PERSISTENCE_DIFFERENCE")
            elif field == "values":
                if expected.keys() != state["values"].keys() or any(
                        sorted(map(formats.digest, value)) != sorted(map(formats.digest, state["values"][key]))
                        for key, value in expected.items()):
                    raise catalog.Blocked("CATALOG_PERSISTENCE_DIFFERENCE")
            elif state.get(field) != expected:
                raise catalog.Blocked("CATALOG_PERSISTENCE_DIFFERENCE")
    try:
        _write_mapping(task.source_version, group, target, state, version, approved)
    except (catalog.Blocked, frappe.QueryDeadlockError, frappe.PermissionError):
        raise
    except Exception:
        raise catalog.Blocked("SOURCE_MAPPING_WRITE_FAILED") from None
    return {"target": target, "target_digest": formats.digest(state)}


def _execute(task_id, version, requested, seen):
    with connection(read_committed=True) as db:
        _authorize(OP.MIGRATION_EXECUTE, lock=True)
        _catalog_lock()
        task = store.record(store.TASK, _valid_id(task_id), lock=True)
        _current(task, version, allow_completed=True)
        if task.state not in {"Approved", "Executing", "Failed", "Completed"}:
            raise xlsx.MigrationError("MIGRATION_NOT_APPROVED")
        preview_doc, data = _version(task, version)
        approval = store.record(store.APPROVAL, version)
        approved = store.unpack(approval.evidence)
        store.verify(approved, approval.sha256)
        if preview_doc.plan_digest != approval.plan_digest or approved["digest"] != approval.plan_digest:
            raise xlsx.MigrationError("MIGRATION_EVIDENCE_INTEGRITY")
        groups = {group["key"]: group for group in data["groups"] if group["action"] != "skip"}
        results = _recent_results(version)
        completed = {key for key, result in results.items() if result["state"] == "Completed"}
        executable = [group for key, group in groups.items() if key not in completed | seen
                      and set(group["dependencies"]) <= completed]
        if requested:
            if requested not in groups:
                raise xlsx.MigrationError("UNKNOWN_MIGRATION_GROUP")
            if requested in completed:
                return results[requested]
            executable = [group for group in executable if group["key"] == requested]
            if not executable:
                raise xlsx.MigrationError("MIGRATION_DEPENDENCY_PENDING")
        if not executable:
            return {"state": task.state, "remaining": len(groups) - len(completed)}

        group = executable[0]
        key, kind = group["key"], group["kind"]
        if group["action"] in {"create", "update"}:
            _permission(OP.PARAMETER_MAINTAIN if kind in plan.PARAMETERS else OP.PRODUCT_MAINTAIN, lock=True)
        result = {"group": key, "kind": kind, "actor": policy.active_actor(locked=True),
                  "at": now_datetime().isoformat(), "state": "Completed"}
        savepoint = f"migration_{uuid4().hex}"
        db.savepoint(savepoint)
        with store.quiet():
            try:
                result.update(_apply(task, version, group, results, approved))
            except (frappe.QueryDeadlockError, frappe.PermissionError):
                raise
            except Exception as error:
                db.rollback(save_point=savepoint)
                code = str(error) if isinstance(error, catalog.Blocked) else "CATALOG_GROUP_FAILED"
                stale = code.startswith("STALE_") or code in {"TARGET_MISSING", "SOURCE_IDENTITY_ALREADY_BOUND"}
                result.update(state="Invalidated" if stale else "Failed", code=code)
        db.release_savepoint(savepoint)
        store.insert(store.RESULT, version_id=version, group_key=key, state=result["state"],
                     evidence=store.pack(result), sha256=formats.digest(result))
        if result["state"] == "Invalidated":
            state = "Invalidated"
        elif result["state"] == "Completed" and len(completed) + 1 == len(groups):
            state = "Completed"
        elif result["state"] == "Failed" or any(row["state"] == "Failed" and other != key
                                                 for other, row in results.items()):
            state = "Failed"
        else:
            state = "Executing"
        store.update(store.TASK, task.name, {"state": state})
        db.commit()
        return result


def execute(task_id, version, *, group=None, max_groups=1):
    """Attempt up to 25 groups. Each catalog change, mapping and receipt commits together."""
    if (group is not None and (not isinstance(group, str) or len(group) != 64)
            or type(max_groups) is not int or not 1 <= max_groups <= MAX_GROUP_BATCH
            or group is not None and max_groups != 1):
        raise xlsx.MigrationError("MIGRATION_EXECUTION_LIMIT")
    seen = set()
    results = []
    for _ in range(max_groups):
        for attempt in range(DEADLOCK_RETRIES + 1):
            try:
                result = _execute(task_id, version, group, seen)
                break
            except frappe.QueryDeadlockError:
                if attempt == DEADLOCK_RETRIES:
                    raise xlsx.MigrationError("MIGRATION_DATABASE_BUSY") from None
                # A deadlock ends the transaction. Reopen it with current
                # authorization and the same approved plan before retrying.
                sleep(0.05 * 2 ** attempt)
        results.append(result)
        if "group" not in result or result["state"] == "Invalidated":
            break
        seen.add(result["group"])
    return results


def template(path):
    """Bench-only output to an operator-selected local path."""
    if getattr(frappe.local, "request", None) is not None:
        policy.deny()
    _authorize(OP.MIGRATION_OPERATE)
    with Path(path).open("xb") as stream:
        formats.template(stream)


def upload_paths(source, dataset, paths, *, task_id=None, expected_version=None):
    """Bench-only convenience. Reauthorize before opening local files."""
    if getattr(frappe.local, "request", None) is not None:
        policy.deny()
    _authorize(OP.MIGRATION_OPERATE)
    if not isinstance(paths, list) or not 1 <= len(paths) <= xlsx.MAX_FILES:
        raise xlsx.MigrationError("WORKBOOK_COUNT")
    uploads = []
    for path in map(Path, paths):
        with path.open("rb") as stream:
            uploads.append((path.name, stream.read(xlsx.MAX_FILE + 1)))
    return upload(source, dataset, uploads, task_id=task_id, expected_version=expected_version)
