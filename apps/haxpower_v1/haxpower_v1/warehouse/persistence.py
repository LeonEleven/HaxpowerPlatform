"""Warehouse master data and its private transaction boundary."""

from contextlib import contextmanager
from contextvars import ContextVar
from uuid import uuid4

import frappe
from frappe.model.document import Document

from ..catalog.persistence import _index_columns

WAREHOUSE = "HP V1 Warehouse"
LOCATION = "HP V1 Warehouse Location"
MAINTAINER = "HP V1 Warehouse Maintainer"
PHYSICAL = "Physical"
TRANSIT = "Transit"

_writing = ContextVar("hp_v1_warehouse_write", default=False)


def require_maintainer():
    user = frappe.session.user
    if user != "Administrator" and (user == "Guest" or MAINTAINER not in frappe.get_roles(user)):
        frappe.throw("Warehouse maintenance permission is required.", frappe.PermissionError)


def require_system_operator():
    if frappe.session.user != "Administrator":
        frappe.throw("System transit maintenance is restricted to the site operator.", frappe.PermissionError)


@contextmanager
def warehouse_write():
    # Serialize V1 master data changes, including writes that mutate the
    # ERPNext nested set. The database lock lasts until the caller commits.
    if not frappe.db.get_value("DocType", WAREHOUSE, for_update=True):
        frappe.throw("Warehouse schema is not installed.")
    savepoint = f"hp_warehouse_{uuid4().hex}"
    frappe.db.savepoint(savepoint)
    token = _writing.set(True)
    try:
        yield
    except frappe.QueryDeadlockError:
        raise  # MariaDB has rolled back the entire transaction.
    except Exception:
        frappe.db.rollback(save_point=savepoint)
        frappe.db.release_savepoint(savepoint)
        raise
    else:
        frappe.db.release_savepoint(savepoint)
    finally:
        _writing.reset(token)


class WarehouseDocument(Document):
    def _require_write(self):
        if not _writing.get():
            frappe.throw("Use the warehouse maintenance service.", frappe.PermissionError)

    before_insert = before_validate = before_change = _require_write

    def on_trash(self):
        frappe.throw("Warehouse and location identities must be retained.", frappe.PermissionError)

    def before_rename(self, old, new, merge=False):
        self.on_trash()

    def notify_update(self):
        # These documents are only accessed through the internal service.
        pass


def domain_permission(doc, user=None):
    return (user or frappe.session.user) == "Administrator"


def domain_query(user=None):
    return "" if (user or frappe.session.user) == "Administrator" else "1 = 0"


def ensure_schema():
    for doctype, fields, index in (
        (WAREHOUSE, ("company", "code"), "hp_v1_company_warehouse_code"),
        (LOCATION, ("warehouse", "code"), "hp_v1_warehouse_location_code"),
        (LOCATION, ("native_warehouse",), "native_warehouse"),
    ):
        columns = _index_columns(doctype, index)
        if not columns:
            frappe.db.add_unique(doctype, fields, constraint_name=index)
            columns = _index_columns(doctype, index)
        if tuple((column, int(non_unique)) for column, non_unique in columns) != tuple(
            (field, 0) for field in fields
        ):
            frappe.throw(f"Incorrect warehouse uniqueness constraint: {index}.")

    if not frappe.db.exists("Role", MAINTAINER):
        frappe.get_doc(doctype="Role", role_name=MAINTAINER, desk_access=0).insert(ignore_permissions=True)
