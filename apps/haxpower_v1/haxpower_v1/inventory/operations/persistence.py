"""Persistence for immutable domain identities and operation references."""

from uuid import uuid4

import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime

from ...access.policy import deny
from ...catalog.persistence import _index_columns
from ...catalog.validation import DECIMAL_WIDTH
from ..guard import GUARD, GUARD_ID
from .dto import StockError

DOCUMENT = "HP V1 Stock Document"
LINE = "HP V1 Stock Document Line"
REQUEST = "HP V1 Stock Request"
LOG = "HP V1 Operation Log"
LINK = "HP V1 Operation Native Link"
TRANSIT_OWNERSHIP = "HP V1 Transit Ownership"
SYNTHETIC_DOCUMENT = "HP V1 Synthetic Native Document"
SYNTHETIC_DETAIL = "HP V1 Synthetic Native Detail"
TABLES = (GUARD, DOCUMENT, LINE, REQUEST, LOG, LINK, SYNTHETIC_DOCUMENT, SYNTHETIC_DETAIL, TRANSIT_OWNERSHIP)

INDEXES = (
    (TRANSIT_OWNERSHIP, "hp_v1_transit_original_line", ("line_id",)),
    (TRANSIT_OWNERSHIP, "hp_v1_transit_native_detail", ("native_detail",)),
    (TRANSIT_OWNERSHIP, "hp_v1_transit_inward_sle", ("inward_sle",)),
    (DOCUMENT, "hp_v1_stock_number", ("company", "document_number")),
    (LINE, "hp_v1_stock_line_order", ("document_id", "line_no")),
    (REQUEST, "hp_v1_stock_request_identity", ("company", "actor", "command", "key_digest")),
    (LOG, "hp_v1_stock_effect", ("document_id", "effect_key")),
    (LOG, "hp_v1_stock_operation_request", ("request_id",)),
    (LOG, "hp_v1_stock_operation_revision", ("document_id", "result_revision")),
    (LINK, "hp_v1_stock_native_reference", ("operation_id", "role", "parent_type", "parent_id", "detail_type", "detail_id")),
)


class ProtectedRecord(Document):
    def _deny_maintenance(self, *_args, **_kwargs):
        deny()

    before_insert = before_validate = before_change = before_rename = on_trash = _deny_maintenance


def insert(doctype, *, name=None, **fields):
    # Internal records only. No notifications, background enqueue, cache writes
    # or user-provided table or column names run through this repository.
    if doctype not in TABLES:
        raise ValueError("Unknown stock record type")
    name = name or uuid4().hex
    actor = frappe.session.user
    now = now_datetime()
    values = {"name": name, "owner": actor, "modified_by": actor, "creation": now, "modified": now, **fields}
    columns = ", ".join(f"`{column}`" for column in values)
    placeholders = ", ".join(["%s"] * len(values))
    frappe.db.sql(f"INSERT INTO `tab{doctype}` ({columns}) VALUES ({placeholders})", tuple(values.values()))
    return name


def current(doctype, identity, fields):
    return frappe.db.get_value(doctype, identity, fields, as_dict=True, for_update=True)


def update_document(document, result):
    frappe.db.sql(
        f"""UPDATE `tab{DOCUMENT}`
            SET status = %s, revision = %s, effective_operation = %s, modified_by = %s, modified = %s
            WHERE name = %s AND revision = %s AND status = %s AND effective_operation <=> %s""",
        (result.status, result.revision, document.effective_operation or result.operation_id, frappe.session.user,
         now_datetime(), document.name, document.revision, document.status, document.effective_operation),
    )
    _assert_updated("STOCK_DOCUMENT_CHANGED")


def complete_request(request_id, result):
    now = now_datetime()
    frappe.db.sql(
        f"""UPDATE `tab{REQUEST}`
            SET status = 'Completed', result_operation = %s, result_revision = %s, completed_at = %s, modified = %s
            WHERE name = %s AND status = 'Pending' AND result_operation IS NULL""",
        (result.operation_id, result.revision, now, now, request_id),
    )
    _assert_updated("STOCK_REQUEST_CHANGED")


def _assert_updated(code):
    if frappe.db.sql("SELECT ROW_COUNT()")[0][0] != 1:
        raise StockError(code)


def ensure_schema():
    for doctype, name, columns in INDEXES:
        if not _index_columns(doctype, name):
            frappe.db.add_unique(doctype, columns, constraint_name=name)
        if _index_columns(doctype, name) != tuple((column, 0) for column in columns):
            frappe.throw(f"Incorrect stock execution index {name}.")
    column, = frappe.db.sql(
        """SELECT DATA_TYPE, CHARACTER_MAXIMUM_LENGTH FROM information_schema.COLUMNS
           WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = 'quantity'""",
        ("tab" + LINE,),
    )
    if column != ("varchar", DECIMAL_WIDTH):
        frappe.throw("Incorrect exact stock quantity storage.")
    if not frappe.db.exists(GUARD, GUARD_ID):
        insert(GUARD, name=GUARD_ID)
