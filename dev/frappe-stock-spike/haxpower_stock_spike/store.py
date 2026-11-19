"""Persistence for the isolated stock spike. DML uses bound decimal strings."""

import json
from decimal import Decimal
from hashlib import sha256

import frappe
from frappe.utils import get_datetime

SITE = "haxpower-frappe-only.localhost"
DATABASE = "haxpower_frappe_only_lab"
SKU = "HP Spike SKU"
WAREHOUSE = "HP Spike Warehouse"
LOCATION = "HP Spike Location"
SCOPE = "HP Spike Scope"
BALANCE = "HP Spike Balance"
OPERATION = "HP Spike Operation"
EVENT = "HP Spike Event"
TRANSFER = "HP Spike Transfer"
TASK = "HP Spike Count Task"
TRANSIT = "IN-TRANSIT"


def require_site(*args, **kwargs):
    if (frappe.local.site != SITE or frappe.conf.db_name != DATABASE
            or frappe.conf.db_host != "mariadb" or frappe.conf.db_type != "mariadb"):
        frappe.throw("SPIKE_ISOLATED_SITE_REQUIRED", frappe.PermissionError)


def refuse_document_write(doc, method=None, *args, **kwargs):
    frappe.throw("STOCK_SERVICE_REQUIRED", frappe.PermissionError)


def install_constraints():
    require_site()
    # A true NULL is retained in all business data. MariaDB calculates the separate non-null index key.
    frappe.db.sql_ddl("""
        alter table `tabHP Spike Balance`
        add column batch_slot varbinary(321) generated always as (
            case when batch_id is null then X'00' else concat(X'01', convert(batch_id using binary)) end
        ) stored,
        add unique key hp_stock_bucket (sku, location, batch_slot),
        add constraint hp_balance_valid check (
            sku is not null and sku <> '' and location is not null and location <> ''
            and quantity is not null and quantity >= 0
            and bucket_version is not null and bucket_version >= 0
            and (batch_id is null or (char_length(batch_id) between 1 and 80))
        )
    """)
    frappe.db.sql_ddl("""
        alter table `tabHP Spike Event`
        add unique key hp_event_version (bucket, bucket_version),
        add unique key hp_unique_reversal (reversal_of),
        add constraint hp_event_valid check (
            bucket is not null and bucket <> '' and operation is not null and operation <> ''
            and bucket_version is not null and bucket_version > 0 and delta is not null and delta <> 0
        )
    """)
    frappe.db.sql_ddl("""
        alter table `tabHP Spike Transfer`
        add constraint hp_transfer_valid check (
            dispatched is not null and dispatched > 0 and received is not null
            and received >= 0 and received <= dispatched
        )
    """)


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def identity(*parts):
    return sha256(encode(parts).encode()).hexdigest()


def insert_row(doctype, name, fields, *, reserve=False):
    # DocTypes and field names are server constants. Values are always bound parameters.
    actor, now = frappe.session.user, get_datetime()
    row = {"name": name, "owner": actor, "modified_by": actor, "creation": now, "modified": now, **fields}
    columns = ", ".join(f"`{field}`" for field in row)
    placeholders = ", ".join(["%s"] * len(row))
    suffix = " on duplicate key update name = name" if reserve else ""
    frappe.db.sql(f"insert into `tab{doctype}` ({columns}) values ({placeholders}){suffix}", tuple(row.values()))


def current(doctype, name, fields):
    result = frappe.db.sql(f"select {fields} from `tab{doctype}` where name = %s for update", (name,), as_dict=True)
    return result[0] if result else None


def reserve_operation(key, fingerprint, action):
    insert_row(OPERATION, key, {"payload_hash": fingerprint, "operation_type": action, "actor": frappe.session.user},
               reserve=True)
    return current(OPERATION, key, "payload_hash, outcome")


def finish_operation(key, outcome):
    frappe.db.sql("update `tabHP Spike Operation` set outcome = %s where name = %s", (encode(outcome), key))


def lock_buckets(dimensions):
    scoped = {identity(*dim): dim for dim in dimensions}
    buckets = {}
    # Insertion of missing buckets acquires the same record locks, in the same order.
    for key in sorted(scoped):
        sku, location, batch = scoped[key]
        insert_row(BALANCE, key, {"sku": sku, "location": location, "batch_id": batch, "quantity": "0",
                                  "bucket_version": 0}, reserve=True)
        row = current(BALANCE, key, "name, sku, location, batch_id, cast(quantity as char) as quantity, bucket_version")
        if not row or (row.sku, row.location, row.batch_id) != (sku, location, batch):
            frappe.throw("STOCK_BUCKET_KEY_CONFLICT")
        row.quantity = Decimal(row.quantity)
        buckets[sku, location, batch] = row
    return buckets


def append_event(event_id, fields):
    insert_row(EVENT, event_id, fields)


def post_leg(operation, action, line, bucket, delta, *, reversal_of=None):
    if not delta:
        frappe.throw("ZERO_STOCK_EVENT")
    quantity = bucket.quantity + delta
    if quantity < 0:
        frappe.throw("INSUFFICIENT_STOCK")
    version = bucket.bucket_version + 1
    frappe.db.sql("""
        update `tabHP Spike Balance`
        set quantity = %s, bucket_version = %s, modified = %s, modified_by = %s
        where name = %s
    """, (str(quantity), version, get_datetime(), frappe.session.user, bucket.name))
    event_id = identity(operation, line, bucket.name)
    append_event(event_id, {
        "operation": operation, "operation_type": action, "bucket": bucket.name, "sku": bucket.sku,
        "location": bucket.location, "batch_id": bucket.batch_id, "delta": str(delta), "bucket_version": version,
        "reversal_of": reversal_of,
    })
    bucket.quantity, bucket.bucket_version = quantity, version
    return event_id
