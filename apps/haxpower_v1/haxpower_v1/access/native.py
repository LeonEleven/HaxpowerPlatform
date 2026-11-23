"""Native record guards for the V1 resource registry."""

import frappe

from ..adapters.erpnext import item, warehouse
from ..catalog.sku_persistence import SKU
from ..warehouse.persistence import LOCATION
from .policy import deny
from .registry import STOCK_DOCUMENTS

# V1-06 can extend this predicate with its persisted forward/reverse mapping.
BATCH_PREFIXES = ("HP-NB-", "HP-RB-")
MASTER_MAPPINGS = {
    "Item": (item.managed_item, item.SKU_MARKER, SKU, "native_item"),
    "Warehouse": (warehouse.managed_warehouse, warehouse.MARKER, LOCATION, "native_warehouse"),
}


def managed_batch(name, *, locked=False):
    return bool(isinstance(name, str) and name and (
        name.upper().startswith(BATCH_PREFIXES)
        or item.managed_item(frappe.db.get_value("Batch", name, "item", for_update=locked), locked=locked)
    ))


def _references(doc, *, locked):
    # ERPNext uses typed Links in both headers and detail rows. Inspect the
    # before-image too so clearing a managed reference cannot bypass a guard.
    predicates = {"Item": item.managed_item, "Warehouse": warehouse.managed_warehouse, "Batch": managed_batch}
    for row in (doc, *doc.get_all_children()):
        for field in row.meta.get_link_fields():
            if (predicate := predicates.get(field.options)) and predicate(row.get(field.fieldname), locked=locked):
                return True
    return False


def guard_stock_document(doc, method=None, *, target=None, merge=False):
    previous = doc.get_doc_before_save()
    if doc.doctype == "Batch" and any(
        managed_batch(name, locked=True) for name in (doc.name, doc.get("batch_id"), target)
    ):
        deny()
    if _references(doc, locked=True) or (previous and _references(previous, locked=True)):
        deny()
    if merge and target and frappe.db.exists(doc.doctype, target):
        if _references(frappe.get_doc(doc.doctype, target, for_update=True), locked=True):
            deny()


def guard_repost(doc, method=None):
    # These operations may sweep data not identified by a Link field.
    # A V1 installation with native resources requires separately governed maintenance.
    if frappe.db.table_exists(SKU) and frappe.db.get_value(SKU, {}, "name", for_update=True):
        deny()
    if frappe.db.table_exists(LOCATION) and frappe.db.get_value(LOCATION, {}, "name", for_update=True):
        deny()


class StockLifecycle:
    def on_trash(self):
        guard_stock_document(self)
        if parent := getattr(super(), "on_trash", None):
            return parent()

    def before_rename(self, old, new, merge=False):
        guard_stock_document(self, target=new, merge=merge)
        if parent := getattr(super(), "before_rename", None):
            return parent(old, new, merge=merge)


class MaintenanceLifecycle:
    def on_trash(self):
        guard_repost(self)
        if parent := getattr(super(), "on_trash", None):
            return parent()

    def before_rename(self, old, new, merge=False):
        guard_repost(self)
        if parent := getattr(super(), "before_rename", None):
            return parent(old, new, merge=merge)


def individual_master_access(doctype, name):
    if name and not isinstance(name, str):
        deny()
    if not name or not (mapping := MASTER_MAPPINGS.get(doctype)):
        return
    if mapping[0](name):
        deny()
    if frappe.db.exists(doctype, name) and _references(frappe.get_doc(doctype, name), locked=False):
        deny()


def native_item_permission(doc, user=None, permission_type=None):
    return not item.managed_item(doc.name)


def native_item_query(user=None):
    return unmanaged_condition("Item")


def unmanaged_condition(doctype):
    mapping = MASTER_MAPPINGS.get(doctype)
    if not mapping:
        return "1 = 1"
    _, marker, domain, forward_field = mapping
    return (
        f"(coalesce(`tab{doctype}`.`{marker}`, '') = '' and not exists "
        f"(select 1 from `tab{domain}` as hp_mapping "
        f"where hp_mapping.`{forward_field}` = `tab{doctype}`.name))"
    )


def guard_master_list(doctype):
    if doctype not in MASTER_MAPPINGS:
        return
    # Stock Frappe shares can override permission_query_conditions. New shares
    # and adoption of shared records are blocked. Fail closed on legacy drift.
    if frappe.db.sql(
        f"""
        select 1 from `tabDocShare` as share
        join `tab{doctype}` on `tab{doctype}`.name = share.share_name
        where share.share_doctype = %s and not {unmanaged_condition(doctype)}
        limit 1
        """,
        (doctype,),
    ):
        deny()


def guard_sharing(doc, method=None):
    for share in (doc, doc.get_doc_before_save()):
        if not share:
            continue
        doctype = share.share_doctype or ""
        if doctype.startswith("HP V1 ") or doctype in STOCK_DOCUMENTS:
            deny()
        if (mapping := MASTER_MAPPINGS.get(doctype)) and mapping[0](share.share_name, locked=True):
            deny()


def guard_master_references(doc, method=None):
    if _references(doc, locked=True):
        deny()
