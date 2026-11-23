"""Storage invariants for batch identity. Application writes are append-only."""

from contextlib import contextmanager
from uuid import uuid4

import frappe
from frappe.model.document import Document

from ...access.policy import deny
from ...catalog.persistence import _index_columns
from .dto import BatchError, BatchKind
from .normalization import normalize_selection, selection_key

MAPPING = "HP V1 Batch Mapping"
IDENTITY_INDEX = "hp_v1_batch_identity"
NATIVE_INDEX = "hp_v1_batch_native"
CHECK_NAME = "hp_v1_batch_required_identity"
FIELDS = ("name", "sku", "kind", "original_text", "normalized_key", "policy_version", "native_batch", "status")


def mappings(filters, *, locked=False):
    return frappe.db.get_values(MAPPING, filters, FIELDS, as_dict=True, order_by="name", for_update=locked)


def has_mapping(sku_id, *, locked=False):
    # Item hooks can run before the app's DocType exists, e.g. installing ERPNext.
    return frappe.db.table_exists(MAPPING) and frappe.db.get_value(
        MAPPING, {"sku": sku_id}, "name", for_update=locked,
    )


def stored_selection(row):
    if row.kind == BatchKind.NO_BATCH and row.original_text is None:
        payload = {"mode": BatchKind.NO_BATCH}
    else:
        payload = {"mode": row.kind, "batchNo": row.original_text}
    return normalize_selection(payload, policy_version=row.policy_version)


def validated_identity(row):
    try:
        selection = stored_selection(row)
    except BatchError:
        return None
    return selection if selection_key(selection) == row.normalized_key else None


@contextmanager
def batch_write():
    savepoint = f"hp_batch_{uuid4().hex}"
    frappe.db.savepoint(savepoint)
    had_mute = "mute_messages" in frappe.flags
    previous = frappe.flags.get("mute_messages")
    frappe.flags.mute_messages = True  # Native validation may include internal identifiers.
    try:
        yield
    except frappe.QueryDeadlockError:
        raise  # InnoDB already rolled back this transaction.
    except Exception:
        frappe.db.rollback(save_point=savepoint)
        frappe.db.release_savepoint(savepoint)
        raise
    else:
        frappe.db.release_savepoint(savepoint)
    finally:
        if had_mute:
            frappe.flags.mute_messages = previous
        else:
            frappe.flags.pop("mute_messages", None)


class BatchMappingDocument(Document):
    def before_insert(self):
        from ...adapters.erpnext.batch import allows_mapping_insert

        if not allows_mapping_insert(self):
            deny()

    def validate(self):
        self.before_insert()
        if validated_identity(self) is None:
            raise BatchError("BATCH_MAPPING_INVALID")

    def before_change(self):
        deny()

    def on_trash(self):
        deny()

    def before_rename(self, old, new, merge=False):
        deny()

    def notify_update(self):
        pass


def ensure_schema():
    for name, columns in (
        (IDENTITY_INDEX, ("sku", "kind", "normalized_key")),
        (NATIVE_INDEX, ("native_batch",)),
    ):
        if not _index_columns(MAPPING, name):
            frappe.db.add_unique(MAPPING, columns, constraint_name=name)
        if _index_columns(MAPPING, name) != tuple((column, 0) for column in columns):
            frappe.throw("Incorrect batch mapping unique index.")

    # Frappe reqd fields are still nullable at the SQL level. The CHECK makes
    # missing identity fields and alternate NO_BATCH keys impossible.
    if not frappe.db.sql(
        """SELECT 1 FROM information_schema.TABLE_CONSTRAINTS
           WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = %s
             AND CONSTRAINT_NAME = %s AND CONSTRAINT_TYPE = 'CHECK'""",
        ("tab" + MAPPING, CHECK_NAME),
    ):
        frappe.db.sql_ddl(f"""
            ALTER TABLE `tab{MAPPING}` ADD CONSTRAINT `{CHECK_NAME}` CHECK (
                sku IS NOT NULL AND sku <> '' AND native_batch IS NOT NULL AND native_batch <> ''
                AND policy_version IS NOT NULL AND policy_version <> ''
                AND status IS NOT NULL AND BINARY status IN ('Active', 'Inactive')
                AND kind IS NOT NULL AND normalized_key IS NOT NULL
                AND (
                    (BINARY kind = 'NO_BATCH' AND BINARY normalized_key = '-' AND original_text IS NULL)
                    OR (BINARY kind = 'BATCH' AND original_text IS NOT NULL AND BINARY original_text <> ''
                        AND BINARY normalized_key REGEXP '^([0-9a-f]{{6}}){{1,80}}$')
                )
            )
        """)
