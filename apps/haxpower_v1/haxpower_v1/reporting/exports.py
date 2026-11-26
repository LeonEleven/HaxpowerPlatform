"""Generate bounded private exports and reauthorize their recorded coverage."""

from contextlib import contextmanager
from datetime import timedelta
from hashlib import sha256
import json
import os
from time import monotonic, time
from uuid import uuid4

import frappe
from frappe.utils import now_datetime

from ..access import policy
from ..inventory.operations.dto import StockError
from ..inventory.operations.transaction import connection
from ..inventory.read.query import (
    DOCUMENT_FILTERS, INVENTORY_FILTERS, MOVEMENT_FILTERS, PERIOD_FILTERS, PRODUCT_FILTERS, Page, Scope, filters,
)
from ..inventory.read.service import inventory_data, product_data
from . import private_files, workbook

EXPORT = "HP V1 Export"
DOWNLOAD = "HP V1 Export Download"
MAX_ROWS = 5000
MAX_SECONDS = 30
DEFAULT_TTL_HOURS = 24
CATALOG = "Site Catalog"
# Bump the applicable value when nested DTO fields or their access rules change.
PROJECTION_VERSION = dict.fromkeys(workbook.COLUMNS, 1)
FIELDS = ("name", "company", "requested_by", "report", "columns_json", "warehouses_json",
          "snapshot_at", "sha256", "file_size", "row_count", "status", "failure_code", "expires_at")


@contextmanager
def execution_slot(db):
    key = "haxpower_export_" + sha256(frappe.local.site.encode()).hexdigest()[:32]
    if db.sql("SELECT GET_LOCK(%s, 0)", (key,))[0][0] != 1:
        raise StockError("EXPORT_BUSY")
    try:
        yield
    finally:
        db.sql("SELECT RELEASE_LOCK(%s)", (key,))


def _insert(table, identity, values):
    actor = frappe.session.user
    values = dict(name=identity, owner=actor, modified_by=actor, creation=now_datetime(),
                  modified=now_datetime(), **values)
    fields = ", ".join("`" + item + "`" for item in values)
    slots = ", ".join(["%s"] * len(values))
    frappe.db.sql(f"INSERT INTO `tab{table}` ({fields}) VALUES ({slots})", tuple(values.values()))


def _hours():
    hours = frappe.conf.get("hp_v1_export_ttl_hours", DEFAULT_TTL_HOURS)
    if type(hours) is not int or not 1 <= hours <= 168:
        raise StockError("EXPORT_TTL_CONFIGURATION")
    return hours


def _coverage(report, company=None):
    policy.require_operation(policy.Operation.EXPORT)
    if report == "products":
        policy.require_operation(policy.Operation.PRODUCT_READ)
        return CATALOG, ()
    scope = Scope.current(company)
    return scope.company, tuple(scope.warehouses)


def _projection(report):
    if report in workbook.COLUMNS:
        return {"columns": list(workbook.COLUMNS[report]), "version": PROJECTION_VERSION[report]}


def _current(identity):
    identity = policy._record_id(identity)
    row = frappe.db.get_value(EXPORT, identity, FIELDS, as_dict=True)
    if not row or row.name != identity or row.requested_by != policy.active_actor():
        policy.deny()
    projection = _projection(row.report)
    if not projection or projection != json.loads(row.columns_json):
        policy.deny()
    company, current = _coverage(row.report, row.company)
    if company != row.company or not set(json.loads(row.warehouses_json)) <= set(current):
        policy.deny()
    return row


def _status(row):
    return dict(export_id=row.name, report=row.report, status=row.status if row.expires_at > now_datetime() else "Expired",
                generated_at=row.snapshot_at, expires_at=row.expires_at, rows=row.row_count, size=row.file_size,
                failure_code=row.failure_code)


def generate(report, submitted):
    if report not in workbook.COLUMNS:
        frappe.throw("READ_INVALID_REPORT")
    allowed = {
        "products": PRODUCT_FILTERS, "ledger": MOVEMENT_FILTERS, "period": PERIOD_FILTERS,
        "documents": DOCUMENT_FILTERS, "corrections": DOCUMENT_FILTERS,
    }.get(report, INVENTORY_FILTERS)
    criteria = filters(submitted, allowed)
    started = now_datetime()
    deadline = monotonic() + MAX_SECONDS
    with connection(read_committed=True) as db, execution_slot(db):
        actor = policy.active_actor()
        company, _ = _coverage(report, criteria.get("company"))
        identity = uuid4().hex
        row = dict(company=company, requested_by=actor, report=report,
                   criteria=json.dumps(criteria, ensure_ascii=False, sort_keys=True),
                   columns_json=json.dumps(_projection(report)), warehouses_json="[]",
                   snapshot_at=started, expires_at=started + timedelta(hours=_hours()),
                   status="Failed", row_count=0, file_size=0, sha256=None, failure_code=None)
        try:
            if report == "products":
                response, _ = product_data(criteria, Page(MAX_ROWS + 1, 0), export=True)
            else:
                response, actual_company, coverage = inventory_data(report, criteria, Page(MAX_ROWS + 1, 0), export=True)
                row["warehouses_json"] = json.dumps(coverage)
                current_company, current = _coverage(report, company)
                if actual_company != current_company or not set(coverage) <= set(current):
                    policy.deny()
            if response["total"] > MAX_ROWS:
                raise StockError("EXPORT_ROW_LIMIT")
            rows = response["rows"]
            row["snapshot_at"] = response["as_of"]
            row["file_size"], row["sha256"] = private_files.store(
                identity, lambda stream: workbook.write(stream, report, rows, deadline=deadline),
            )
            try:
                _, current = _coverage(report, company)
                if not set(json.loads(row["warehouses_json"])) <= set(current):
                    policy.deny()
            except frappe.PermissionError:
                private_files.filename(identity).unlink(missing_ok=True)
                raise
            row.update(status="Ready", row_count=len(rows))
        except StockError as error:
            row["failure_code"] = (error.code if error.code.startswith("EXPORT_") else "EXPORT_DATA_UNAVAILABLE")
        except (OSError, frappe.QueryTimeoutError):
            row["failure_code"] = "EXPORT_GENERATION_FAILED"
        _insert(EXPORT, identity, row)
        db.commit()
        return _status(frappe._dict(row, name=identity))


def status(identity):
    with connection(snapshot=True):
        return _status(_current(identity))


def download(identity):
    with connection(read_committed=True) as db:
        row = _current(identity)
        if row.status != "Ready" or row.expires_at <= now_datetime():
            policy.deny()
        data = private_files.read(row.name, row.file_size, row.sha256)
        # Fetch current grants once more after filesystem I/O.
        _current(identity)
        _insert(DOWNLOAD, uuid4().hex, dict(export_id=row.name, actor=frappe.session.user, sha256=row.sha256))
        db.commit()
        return row.report + ".xlsx", data


def purge_expired():
    """Bench-only bounded cleanup. Preserve export records and download audit."""
    if getattr(frappe.local, "request", None) is not None:
        policy.deny()
    with connection(read_committed=True) as db, execution_slot(db):
        policy.require_operation(policy.Operation.SYSTEM_MAINTAIN)
        identities = frappe.db.get_values(EXPORT, {"expires_at": ("<=", now_datetime()), "status": ("!=", "Expired")},
                                          "name", pluck=True, limit=200, order_by="expires_at asc")
        for identity in identities:
            private_files.filename(identity).unlink(missing_ok=True)
            db.set_value(EXPORT, identity, "status", "Expired", update_modified=False)
        orphans = _purge_orphans()
        db.commit()
        return dict(expired=len(identities), orphan_files=orphans)


def _purge_orphans():
    from ..catalog.validation import IDENTIFIER

    # A crash between durable file publication and the database commit may
    # leave an old generated file with no owner record.
    removed = 0
    minimum_age = _hours() * 3600
    with os.scandir(private_files.directory()) as entries:
        for entry in entries:
            if removed == 200:
                break
            if not entry.is_file(follow_symlinks=False) or time() - entry.stat().st_mtime < minimum_age:
                continue
            identity, suffix = os.path.splitext(entry.name)
            if entry.name.startswith(".generating-") or (
                suffix == ".xlsx" and IDENTIFIER.fullmatch(identity) and not frappe.db.exists(EXPORT, identity)
            ):
                os.unlink(entry.path)
                removed += 1
    return removed
