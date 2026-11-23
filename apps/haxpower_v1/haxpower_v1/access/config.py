"""Trusted site configuration for warehouse scope. This module exposes no RPC."""

from uuid import uuid4

import frappe
from frappe.model.document import Document

from ..catalog import validation
from ..warehouse.persistence import PHYSICAL, WAREHOUSE
from .policy import Mode, SCOPE, active_actor, deny


def require_access_operator():
    if active_actor() != "Administrator":
        deny()


class WarehouseAccess(Document):
    def validate(self):
        require_access_operator()
        company = frappe.db.get_value("Company", self.company, ("name", "is_group"), as_dict=True)
        if not company or company.name != self.company or company.is_group:
            frappe.throw("Select an operational company.")
        validation.choice(self.mode, tuple(Mode), "Warehouse scope")
        if self.mode != Mode.SELECTED and self.warehouses:
            frappe.throw("A warehouse selection requires SELECTED mode.")
        selected = set()
        for row in self.warehouses:
            warehouse_id = validation.identifier(row.warehouse, "Warehouse ID")
            warehouse = frappe.db.get_value(WAREHOUSE, warehouse_id, ("name", "company", "kind"), as_dict=True)
            if (
                warehouse_id in selected or not warehouse or warehouse.name != warehouse_id
                or warehouse.company != self.company or warehouse.kind != PHYSICAL
            ):
                frappe.throw("Select distinct physical warehouse IDs in the same company.")
            selected.add(warehouse_id)

    def notify_update(self):
        # Scope and its changes are private to the Bench operator.
        pass


def set_scope(user, company, mode: Mode, warehouse_ids=()):
    require_access_operator()
    # Lock the user so two operators cannot race to create its first policy.
    if not frappe.db.get_value("User", user, for_update=True):
        frappe.throw("Unknown employee.")
    name = frappe.db.get_value(SCOPE, {"user": user}, for_update=True)
    doc = frappe.get_doc(SCOPE, name, for_update=True) if name else frappe.new_doc(SCOPE)
    doc.update({
        "user": user, "company": company, "mode": mode,
        "warehouses": [{"warehouse": warehouse_id} for warehouse_id in warehouse_ids],
    })
    savepoint = f"hp_scope_{uuid4().hex}"
    frappe.db.savepoint(savepoint)
    try:
        doc.save(ignore_permissions=True)
    except frappe.QueryDeadlockError:
        raise  # MariaDB has already rolled back the transaction.
    except Exception:
        frappe.db.rollback(save_point=savepoint)
        frappe.db.release_savepoint(savepoint)
        raise
    else:
        frappe.db.release_savepoint(savepoint)
