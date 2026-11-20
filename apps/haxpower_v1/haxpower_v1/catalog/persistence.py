"""Frappe write boundary and installation of catalog database constraints."""

from contextlib import contextmanager
from contextvars import ContextVar
from uuid import uuid4

import frappe
from frappe.model.document import Document


CATEGORY = "HP V1 Product Category"
ATTRIBUTE = "HP V1 Attribute Definition"
OPTION = "HP V1 Attribute Option"
TEMPLATE = "HP V1 Category Attribute"
SPECIFICATION = "HP V1 Product Specification"
VALUE = "HP V1 Product Attribute Value"
AUDIT = "HP V1 Attribute Edit Audit"
MAINTAINER = "HP V1 Catalog Maintainer"

_in_write = ContextVar("hp_v1_catalog_write", default=False)


def require_maintainer():
    user = frappe.session.user
    if user != "Administrator" and (user == "Guest" or MAINTAINER not in frappe.get_roles(user)):
        frappe.throw("Catalog maintenance permission is required.", frappe.PermissionError)


@contextmanager
def catalog_write():
    require_maintainer()
    # Serialize catalog writes until the caller's transaction ends. Validate against
    # current reads after obtaining the lock, including when the caller has read before.
    if not frappe.db.get_value("DocType", SPECIFICATION, for_update=True):
        frappe.throw("Catalog schema is not installed.")
    savepoint = f"catalog_{uuid4().hex}"
    frappe.db.savepoint(savepoint)
    token = _in_write.set(True)
    try:
        yield
    except frappe.QueryDeadlockError:
        # MariaDB has already rolled back the entire transaction.
        raise
    except Exception:
        frappe.db.rollback(save_point=savepoint)
        frappe.db.release_savepoint(savepoint)
        raise
    else:
        frappe.db.release_savepoint(savepoint)
    finally:
        _in_write.reset(token)


def catalog_doc(doctype, record_id):
    return frappe.get_doc(doctype, record_id, for_update=_in_write.get())


def catalog_rows(doctype, filters, fieldnames, *, order_by=None):
    return frappe.db.get_values(
        doctype, filters, fieldnames, as_dict=True, order_by=order_by,
        for_update=_in_write.get(),
    )


class CatalogDocument(Document):
    def validate(self):
        if not _in_write.get():
            frappe.throw("Use the catalog maintenance service to write this document.", frappe.PermissionError)

    def on_trash(self):
        frappe.throw("Catalog records cannot be deleted. Use status where available.", frappe.PermissionError)

    def before_rename(self, old, new, merge=False):
        frappe.throw("Catalog record identifiers cannot be renamed or merged.", frappe.PermissionError)

    def notify_update(self):
        # Internal records have no Desk subscribers. Keep writes free of
        # realtime callbacks, which Frappe retains after a savepoint rollback.
        pass


class CatalogAudit(CatalogDocument):
    def validate(self):
        super().validate()
        if not self.is_new():
            frappe.throw("Catalog audit history is append-only.", frappe.PermissionError)


def ensure_schema():
    """Run after DocType sync on both fresh installs and upgrades."""
    for doctype, fields, index in (
        (OPTION, ("attribute", "option_key"), "hp_v1_attribute_option_key"),
        (TEMPLATE, ("category", "attribute"), "hp_v1_category_attribute_key"),
    ):
        # Check the index in this site's schema, including field order and uniqueness.
        rows = _index_columns(doctype, index)
        if not rows:
            frappe.db.add_unique(doctype, fields, constraint_name=index)
            rows = _index_columns(doctype, index)
        if tuple((column, int(non_unique)) for column, non_unique in rows) != tuple((field, 0) for field in fields):
            frappe.throw(f"Incorrect catalog uniqueness constraint: {index}.")

    if not frappe.db.exists("Role", MAINTAINER):
        frappe.get_doc(doctype="Role", role_name=MAINTAINER, desk_access=0).insert(ignore_permissions=True)


def _index_columns(doctype, index):
    return frappe.db.sql(
        """SELECT COLUMN_NAME, NON_UNIQUE
           FROM information_schema.STATISTICS
           WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND INDEX_NAME = %s
           ORDER BY SEQ_IN_INDEX""",
        ("tab" + doctype, index),
    )
