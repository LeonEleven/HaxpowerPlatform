"""Minimal adapter for the isolated inventory experiments."""

import json
import re
from contextvars import ContextVar
from decimal import Decimal
from hashlib import sha256
from uuid import NAMESPACE_URL, uuid5

import frappe
from erpnext.stock.doctype.serial_and_batch_bundle.serial_and_batch_bundle import get_batch_qty_for_stock


SITE = "haxpower-stage1b.localhost"
COMPANY = "HP Stage 1B Lab"
ROLE = "Haxpower Stage 1B Operator"
QUERY_ROLE = "Haxpower Stage 1C Query"
ITEMS = {"SKU-A": "HP1B-SKU-A", "SKU-B": "HP1B-SKU-B"}
LOCATIONS = {"W1/L1": "HP1B L1 - H1B", "W1/L2": "HP1B L2 - H1B"}
SYSTEM_DESCRIPTION = "Haxpower internal no-batch bucket"
INTERNAL_PREFIX = "HP-NB-"
REAL_PREFIX = "HP-RB-"
SERVICE_WRITE = ContextVar("stage1b_service_write", default=False)


class BundlePermissionMixin:
    def has_permission(self, permtype="read", *, debug=False, user=None):
        # ERPNext creates and submits a separate Bundle document during posting.
        if (frappe.local.site == SITE and self.item_code in ITEMS.values()
                and permtype in ("create", "write", "submit", "cancel", "delete", "amend")):
            return (SERVICE_WRITE.get() and self.company == COMPANY and self.warehouse in LOCATIONS.values()
                    and permtype in ("create", "write", "submit"))
        return super().has_permission(permtype, debug=debug, user=user)


def reserved(value):
    return isinstance(value, str) and value.upper().startswith((INTERNAL_PREFIX, REAL_PREFIX))


def protect_batch(doc, method=None, *args, **kwargs):
    if frappe.local.site != SITE or SERVICE_WRITE.get():
        return
    before = doc.get_doc_before_save() if method == "before_validate" else None
    if doc.item in ITEMS.values() or (before and before.item in ITEMS.values()) or any(
        reserved(value) for value in (doc.name, doc.batch_id, *args)
    ):
        frappe.throw("STAGE1B_BATCH_PROTECTED", frappe.PermissionError)


def protect_inventory(doc, method=None):
    if frappe.local.site != SITE or SERVICE_WRITE.get():
        return
    for version in (doc, doc.get_doc_before_save()):
        if version and (version.get("item_code") in ITEMS.values()
                        or any(row.item_code in ITEMS.values() for row in version.get("items", []))):
            frappe.throw("STAGE1B_USE_ADAPTER", frappe.PermissionError)


def fixture_scope(stock_item_id, warehouse_location_id, *, write=False):
    if frappe.local.site != SITE or frappe.conf.db_name != "haxpower_stage1b_lab":
        frappe.throw("STAGE1B_SITE_REQUIRED", frappe.PermissionError)
    allowed_roles = {ROLE} if write else {ROLE, QUERY_ROLE}
    if allowed_roles.isdisjoint(frappe.get_roles()):
        frappe.throw("INVENTORY_OPERATION_DENIED", frappe.PermissionError)
    warehouse = LOCATIONS.get(warehouse_location_id) if isinstance(warehouse_location_id, str) else None
    if not warehouse or not frappe.db.exists(
        "Warehouse", {"name": warehouse, "company": COMPANY, "is_group": 0, "disabled": 0}
    ) or not frappe.db.exists(
        "User Permission", {"user": frappe.session.user, "allow": "Warehouse",
                            "for_value": warehouse, "apply_to_all_doctypes": 1}
    ):
        frappe.throw("WAREHOUSE_ACCESS_DENIED", frappe.PermissionError)
    item = ITEMS.get(stock_item_id) if isinstance(stock_item_id, str) else None
    if not item:
        frappe.throw("PRODUCT_NOT_FOUND")
    return item, warehouse


def parse_selection(selection):
    if not isinstance(selection, dict) or "mode" not in selection:
        frappe.throw("BATCH_SELECTION_REQUIRED")
    if selection["mode"] == "NO_BATCH" and set(selection) == {"mode"}:
        return None
    if selection["mode"] == "BATCH" and set(selection) == {"mode", "batchNo"}:
        number = selection["batchNo"]
        if (isinstance(number, str) and 0 < len(number) <= 80 and number == number.strip()
                and not reserved(number) and all(c.isprintable() and c not in "<>" for c in number)):
            return number
    frappe.throw("BATCH_NOT_FOUND")


def batch_key(item, number):
    payload = json.dumps([SITE, item, number], ensure_ascii=False, separators=(",", ":"))
    key = uuid5(NAMESPACE_URL, payload).hex
    return (INTERNAL_PREFIX if number is None else REAL_PREFIX) + key


def find_batch(item, number, *, create=False):
    key = batch_key(item, number)
    description = SYSTEM_DESCRIPTION if number is None else number
    batch = frappe.db.get_value("Batch", key, ["item", "description", "disabled"], as_dict=True,
                                for_update=create)
    if batch:
        if batch.item != item or batch.description != description or batch.disabled:
            frappe.throw("STAGE1B_BATCH_MAPPING_CONFLICT")
    elif create:
        frappe.get_doc({
            "doctype": "Batch", "batch_id": key, "item": item, "description": description,
        }).insert(ignore_permissions=True)
    else:
        frappe.throw("BATCH_NOT_FOUND")
    return key


def post_entry(action, item, warehouse, qty, key, remarks):
    receipt = action == "RECEIPT"
    direction = "t_warehouse" if receipt else "s_warehouse"
    doc = frappe.get_doc({
        "doctype": "Stock Entry", "company": COMPANY,
        "stock_entry_type": "Material Receipt" if receipt else "Material Issue",
        "remarks": remarks,
        "items": [{
            "item_code": item, "qty": qty, "uom": "PC",
            "batch_no": key, "use_serial_batch_fields": 1,
            "allow_zero_valuation_rate": 1, direction: warehouse,
        }],
    }).insert(ignore_permissions=True)
    doc.submit()
    ledger = frappe.db.get_value(
        "Stock Ledger Entry", {"voucher_no": doc.name, "voucher_detail_no": doc.items[0].name,
                               "warehouse": warehouse, "item_code": item, "voucher_type": "Stock Entry"},
        ["serial_and_batch_bundle", "actual_qty"], as_dict=True,
    )
    signed = qty if receipt else -qty
    if not ledger or not ledger.serial_and_batch_bundle or Decimal(str(ledger.actual_qty)) != signed:
        frappe.throw("STAGE1B_UNEXPECTED_LEDGER")
    bundle = frappe.get_doc("Serial and Batch Bundle", ledger.serial_and_batch_bundle)
    if (bundle.docstatus != 1 or len(bundle.entries) != 1 or bundle.entries[0].batch_no != key
            or Decimal(str(bundle.entries[0].qty)) != signed):
        frappe.throw("STAGE1B_UNEXPECTED_BUNDLE")
    return doc.name


@frappe.whitelist(methods=["POST"])
def post_stock(action, stockItemId, warehouseLocationId, quantity, requestId, batchSelection=None):
    item, warehouse = fixture_scope(stockItemId, warehouseLocationId, write=True)
    number = parse_selection(batchSelection)
    if action not in ("RECEIPT", "ISSUE"):
        frappe.throw("INVALID_STOCK_ACTION")
    if not isinstance(quantity, str) or not re.fullmatch(r"[1-9][0-9]{0,5}", quantity):
        frappe.throw("INVALID_PC_QUANTITY")
    if not isinstance(requestId, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", requestId):
        frappe.throw("INVALID_REQUEST_ID")
    qty = Decimal(quantity)
    # Serialize prototype writes and look up replay evidence on the same transaction.
    frappe.db.get_value("Company", COMPANY, "name", for_update=True)
    digest = sha256(f"{frappe.session.user}\0{requestId}".encode()).hexdigest()
    fingerprint = sha256(json.dumps([action, item, warehouse, quantity, number]).encode()).hexdigest()
    prefix = f"HP-STAGE1B:{digest}:"
    remarks = prefix + fingerprint
    existing = frappe.db.get_value(
        "Stock Entry", {"company": COMPANY, "remarks": ["like", prefix + "%"]},
        ["name", "docstatus", "remarks"], as_dict=True, for_update=True,
    )
    if existing:
        if existing.remarks != remarks or existing.docstatus != 1:
            frappe.throw("IDEMPOTENCY_CONFLICT")
        return {"documentId": existing.name, "replayed": True}
    if frappe.db.get_single_value("Stock Settings", "auto_create_serial_and_batch_bundle_for_outward"):
        frappe.throw("STAGE1B_AUTO_ALLOCATION_MUST_BE_OFF")
    token = SERVICE_WRITE.set(True)
    try:
        key = find_batch(item, number, create=action == "RECEIPT")
        if action == "ISSUE":
            available = get_batch_qty_for_stock(
                batch_no=key, item_code=item, warehouse=warehouse,
                for_stock_levels=True, ignore_reserved_stock=True,
            )
            if Decimal(str(available)) < qty:
                frappe.throw("INSUFFICIENT_STOCK")
        name = post_entry(action, item, warehouse, qty, key, remarks)
    finally:
        SERVICE_WRITE.reset(token)
    return {"documentId": name, "replayed": False}


@frappe.whitelist(methods=["GET"])
def balances(stockItemId, warehouseLocationId):
    item, warehouse = fixture_scope(stockItemId, warehouseLocationId)
    allocations = get_batch_qty_for_stock(
        item_code=item, warehouse=warehouse, for_stock_levels=True, ignore_reserved_stock=True,
    )
    items = []
    for allocation in allocations:
        key = allocation.batch_no
        if key == batch_key(item, None):
            number = None
            description = frappe.db.get_value("Batch", {"name": key, "item": item}, "description")
            if description != SYSTEM_DESCRIPTION:
                frappe.throw("STAGE1B_BATCH_MAPPING_CONFLICT")
        else:
            number = frappe.db.get_value("Batch", {"name": key, "item": item}, "description")
            if not number or key != batch_key(item, number):
                frappe.throw("STAGE1B_BATCH_MAPPING_CONFLICT")
        items.append({
            "stockItemId": stockItemId, "location": warehouseLocationId, "batch": number,
            "quantity": format(Decimal(str(allocation.qty)).normalize(), "f"), "uom": "PC",
        })
    return {"items": items}
