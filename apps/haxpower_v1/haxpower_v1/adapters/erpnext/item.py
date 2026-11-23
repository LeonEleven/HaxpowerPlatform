"""ERPNext v16 Item/UOM mapping for the trusted SKU service."""

from decimal import Decimal

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.utils import cint, getdate

from ...access.execution import allows_native_write, native_write
from ...catalog import validation as check
from ...catalog.dto import ACTIVE
from ...catalog.persistence import require_maintainer
from ...catalog.sku_dto import ItemMappingDiagnostic, MappingStatus
from ...catalog.sku_persistence import SKU

ITEM = "Item"
SKU_MARKER = "custom_hp_v1_sku_id"
SKU_MARKER_PERMLEVEL = 2  # ERPNext Item Manager has native access through level 1.
SETTINGS = "hp_v1_sku_settings"

# Protect fields that identify the item and determine its use in inventory.
FLAGS = {
    "is_stock_item": 1,
    "is_fixed_asset": 0,
    "has_variants": 0,
    "has_serial_no": 0,
    "has_batch_no": 0,
    "create_new_batch": 0,
    "is_customer_provided_item": 0,
}
MASTER_FIELDS = {
    "UOM": ("stock_uom", ("uom_name", "enabled", "must_be_whole_number")),
    "Item Group": ("native_item_group", ("item_group_name", "is_group")),
}
REFERENCE_FIELDS = {
    "Stock Entry Detail": "item_code",
    "Stock Reconciliation Item": "item_code",
    "Material Request Item": "item_code",
    "Purchase Order Item": "item_code",
    "Purchase Receipt Item": "item_code",
    "Purchase Invoice Item": "item_code",
    "Sales Order Item": "item_code",
    "Delivery Note Item": "item_code",
    "Sales Invoice Item": "item_code",
    "Stock Reservation Entry": "item_code",
    "Work Order": "production_item",
    "Work Order Item": "item_code",
}
ITEM_SERVICE = "SKU mapping"
BATCH_ITEM_SERVICE = "Batch Item enablement"
BATCH_SETTINGS = ("batch_number_series", "has_expiry_date", "shelf_life_in_days", "retain_sample")


def ensure_schema():
    create_custom_fields({ITEM: [{
        "fieldname": SKU_MARKER, "label": "Haxpower V1 SKU ID", "fieldtype": "Data",
        "length": 32, "permlevel": SKU_MARKER_PERMLEVEL,
        "hidden": 1, "read_only": 1, "no_copy": 1, "search_index": 1,
        "insert_after": "item_code",
    }]}, update=False)
    field = frappe.get_meta(ITEM).get_field(SKU_MARKER)
    if (
        not field or field.fieldtype != "Data" or cint(field.length) != 32
        or cint(field.permlevel) != SKU_MARKER_PERMLEVEL
        or not all(field.get(key) for key in ("hidden", "read_only", "no_copy", "search_index"))
        or not frappe.db.has_column(ITEM, SKU_MARKER)
    ):
        frappe.throw("Incorrect Haxpower SKU marker schema on ERPNext Item.")


def _settings():
    settings = frappe.conf.get(SETTINGS)
    if not isinstance(settings, dict) or not isinstance(settings.get("uom_precision"), dict):
        frappe.throw("SKU unit and precision configuration is required.")
    return settings


def _uom_supports(uom, precision):
    # MariaDB stores ERPNext Float fields as DECIMAL(21,9). The site can
    # choose a lower precision. Stock posting is verified in later stages.
    float_precision = cint(frappe.get_system_settings("float_precision"), default=3)
    return uom and uom.enabled and (
        0 <= precision <= min(9, float_precision) and (not uom.must_be_whole_number or precision == 0)
    )


def validate_configuration(uom_name: str, precision: int) -> str:
    settings = _settings()
    configured = settings["uom_precision"].get(uom_name)
    if type(precision) is not int or type(configured) is not int or precision != configured:
        frappe.throw("SKU quantity precision must match the configured precision of its stock UOM.")
    uom = frappe.db.get_value("UOM", uom_name, ("name", "enabled", "must_be_whole_number"),
                              as_dict=True, for_update=True)
    if not _uom_supports(uom, precision) or uom.name != uom_name:
        frappe.throw("The stock UOM is missing, disabled, or incompatible with the quantity precision.")

    name = check.text(settings.get("item_group"), "Configured Item Group", 140, required=True)
    group = frappe.db.get_value("Item Group", name, ("name", "is_group"), as_dict=True, for_update=True)
    if not group or group.name != name or group.is_group:
        frappe.throw("SKU mapping requires a configured leaf ERPNext Item Group.")
    return group.name


def _write_item(name, sku_id, *, existing):
    return native_write(
        require_maintainer, service=ITEM_SERVICE, native_name=name, domain_id=sku_id, existing=existing,
    )


def _conversion_factors(item):
    return sorted((row.uom or "", Decimal(str(row.conversion_factor or 0))) for row in item.uoms)


def _matches(item, sku, *, locked=False, batch_enabled=False):
    uom = frappe.db.get_value(
        "UOM", sku.stock_uom, ("name", "enabled", "must_be_whole_number"), as_dict=True, for_update=locked,
    )
    group = frappe.db.get_value(
        "Item Group", sku.native_item_group, ("name", "is_group"), as_dict=True, for_update=locked,
    )
    return (
        item.name == sku.native_item == item.item_code
        and item.item_name == sku.display_name
        and item.stock_uom == sku.stock_uom
        and item.item_group == sku.native_item_group
        and group and group.name == sku.native_item_group and not group.is_group
        and _uom_supports(uom, sku.quantity_precision) and uom.name == sku.stock_uom
        and cint(item.disabled) == int(sku.status != ACTIVE)
        and not item.variant_of
        and not item.opening_stock
        and item.end_of_life and getdate(item.end_of_life) >= getdate()
        and cint(item.has_batch_no) == int(batch_enabled)
        and all(cint(item.get(field)) == value for field, value in FLAGS.items() if field != "has_batch_no")
        and (not batch_enabled or not any(item.get(field) for field in BATCH_SETTINGS))
        and _conversion_factors(item) == [(sku.stock_uom, Decimal(1))]
    )


def bind_item(sku, *, adopt: bool):
    """Create or explicitly adopt an unused Item in the caller's savepoint."""
    name = sku.native_item
    if not adopt:
        if frappe.db.get_value(ITEM, name, for_update=True):
            frappe.throw("An ERPNext Item already uses this code. Explicit association is required.")
        with _write_item(name, sku.name, existing=False):
            item = frappe.get_doc(
                doctype=ITEM, item_code=name, item_name=sku.display_name, item_group=sku.native_item_group,
                stock_uom=sku.stock_uom, disabled=0, standard_rate=0, opening_stock=0,
                **FLAGS, **{SKU_MARKER: sku.name},
            )
            item.flags.notify_update = False
            item.insert(ignore_permissions=True, set_name=name)
    else:
        item = frappe.get_doc(ITEM, name, for_update=True)
        if item.get(SKU_MARKER) or frappe.db.get_value(SKU, {"native_item": item.name}, for_update=True):
            frappe.throw("This ERPNext Item is already mapped to a SKU.")
        if not _matches(item, sku, locked=True):
            frappe.throw("The ERPNext Item does not match the requested SKU identity or unit.")
        _check_references(item.name)
        item.set(SKU_MARKER, sku.name)
        with _write_item(name, sku.name, existing=True):
            item.flags.notify_update = False
            item.save(ignore_permissions=True)


def _item_doc(name, *, locked):
    if not frappe.db.get_value(ITEM, name, for_update=locked):
        return None
    return frappe.get_doc(ITEM, name, for_update=locked)


def mapping_status(sku, *, locked=False) -> MappingStatus:
    from ...inventory.batch.persistence import has_mapping

    item = _item_doc(sku.native_item, locked=locked)
    if item is None:
        return "Missing Item"
    markers = frappe.db.get_values(ITEM, {SKU_MARKER: sku.name}, "name", for_update=locked)
    if len(markers) > 1:
        return "Duplicate Marker"
    marker = item.get(SKU_MARKER)
    if marker and marker != sku.name and not frappe.db.get_value(SKU, marker, for_update=locked):
        return "Orphan Marker"
    batch_enabled = bool(has_mapping(sku.name, locked=locked))
    if marker != sku.name or not _matches(item, sku, locked=locked, batch_enabled=batch_enabled):
        return "Item Drift"
    return "Verified"


def mapping_for_item(name: str) -> ItemMappingDiagnostic:
    """Called by the authorized catalog diagnostic."""
    sku_id = frappe.db.get_value(SKU, {"native_item": name}, "name")
    item = _item_doc(name, locked=False)
    marker = (item.get(SKU_MARKER) or None) if item else None
    if sku_id:
        status = mapping_status(frappe.get_doc(SKU, sku_id))
    elif item is None:
        status = "Missing Item"
    elif not marker:
        status = "Unmanaged"
    elif not frappe.db.exists(SKU, marker):
        status = "Orphan Marker"
    elif frappe.db.count(ITEM, {SKU_MARKER: marker}) > 1:
        status = "Duplicate Marker"
    else:
        status = "Item Drift"
    return ItemMappingDiagnostic(sku_id, name, marker, status)


def update_item_name(sku, display_name):
    item = frappe.get_doc(ITEM, sku.native_item, for_update=True)
    item.item_name = display_name
    with _write_item(item.name, sku.name, existing=True):
        item.flags.notify_update = False
        item.save(ignore_permissions=True)


def _check_references(name):
    if frappe.db.get_value("Bin", {"item_code": name, "actual_qty": ("!=", 0)}, for_update=True):
        frappe.throw("The ERPNext Item has native stock.")
    if frappe.db.get_value("DocShare", {"share_doctype": ITEM, "share_name": name}, for_update=True):
        frappe.throw("The ERPNext Item has a native sharing grant.")
    if frappe.db.get_value("Stock Ledger Entry", {"item_code": name}, for_update=True):
        frappe.throw("The ERPNext Item has native stock ledger history.")
    for doctype, item_field in REFERENCE_FIELDS.items():
        if frappe.db.get_value(doctype, {item_field: name, "docstatus": ("<", 2)}, for_update=True):
            frappe.throw(f"The ERPNext Item has an active native reference in {doctype}.")


def batch_settings_ready():
    return bool(cint(frappe.db.get_single_value(
        "Stock Settings", "enable_serial_and_batch_no_for_item", cache=False,
    )))


def _enable_batch(sku, batch_id):
    """Enable the mapped Item in the Batch service's transaction."""
    from ...inventory.batch.authorization import require_candidate_writer
    from ...inventory.batch.dto import BatchError
    from ...inventory.batch.persistence import has_mapping

    require_candidate_writer(sku)
    if mapping_status(sku, locked=True) != "Verified":
        raise BatchError("BATCH_ITEM_INCOMPATIBLE")
    doc = _item_doc(sku.native_item, locked=True)
    if doc.has_batch_no:
        return
    if (not batch_settings_ready() or has_mapping(sku.name, locked=True)
            or any(doc.get(field) for field in BATCH_SETTINGS)):
        raise BatchError("BATCH_ITEM_INCOMPATIBLE")
    try:
        _check_references(doc.name)
    except frappe.ValidationError:
        raise BatchError("BATCH_ITEM_INCOMPATIBLE") from None
    for doctype, field in (("Batch", "item"), ("Serial No", "item_code"), ("Serial and Batch Bundle", "item_code")):
        if frappe.db.get_value(doctype, {field: doc.name}, for_update=True):
            raise BatchError("BATCH_ITEM_INCOMPATIBLE")
    doc.has_batch_no = 1
    with native_write(
        lambda: require_candidate_writer(sku), service=BATCH_ITEM_SERVICE, native_name=doc.name,
        domain_id=batch_id, existing=True, sku_id=sku.name,
    ):
        # Scope contains the concrete mapping ID that the Item guard checks.
        doc.flags.hp_v1_enable_batch_id = batch_id
        doc.flags.notify_update = False
        doc.save(ignore_permissions=True)


def assert_deactivation_safe(sku):
    _check_references(sku.native_item)
    # Native document writers and future V1 workflows must share an exclusion
    # boundary before a negative stock/reference lookup can authorize disable.
    frappe.throw("SKU deactivation requires V1 inventory reference and write coordination.")


def managed_item(name, *, locked=False):
    if not name:
        return False
    return bool(
        (frappe.db.has_column(ITEM, SKU_MARKER)
         and frappe.db.get_value(ITEM, name, SKU_MARKER, for_update=locked))
        or (frappe.db.table_exists(SKU) and frappe.db.get_value(SKU, {"native_item": name}, for_update=locked))
    )


def _protected_item_fields(doc, *, exclude_batch_flag=False):
    return (
        doc.get(SKU_MARKER) or None, doc.item_code, doc.item_name, doc.item_group, doc.stock_uom,
        cint(doc.disabled), doc.variant_of or None, Decimal(str(doc.opening_stock or 0)),
        getdate(doc.end_of_life) if doc.end_of_life else None,
        *(cint(doc.get(field)) for field in FLAGS if not (exclude_batch_flag and field == "has_batch_no")),
        *(doc.get(field) or None for field in BATCH_SETTINGS), _conversion_factors(doc),
    )


def guard_item_change(doc, method=None):
    marker = doc.get(SKU_MARKER) or None
    name = doc.name or doc.item_code
    existing = not doc.is_new()
    if marker and allows_native_write(ITEM_SERVICE, name, marker, existing=existing):
        return
    if marker or (existing and managed_item(name, locked=True)):
        previous = doc.get_doc_before_save()
        batch_id = doc.flags.get("hp_v1_enable_batch_id")
        if (marker and previous and batch_id
                and allows_native_write(BATCH_ITEM_SERVICE, name, batch_id, existing=existing, sku_id=marker)
                and not previous.has_batch_no and cint(doc.has_batch_no) == 1
                and _protected_item_fields(doc, exclude_batch_flag=True)
                == _protected_item_fields(previous, exclude_batch_flag=True)):
            return
        # Preserve the existing Bench operator's non-identity maintenance.
        if (frappe.session.user == "Administrator" and not getattr(frappe.local, "request", None)
                and previous and _protected_item_fields(doc) == _protected_item_fields(previous)):
            return
        frappe.throw("Use the SKU service to change a managed Item.", frappe.PermissionError)


class ItemLifecycle:
    def on_trash(self):
        if managed_item(self.name, locked=True):
            frappe.throw("A managed ERPNext Item cannot be deleted.", frappe.PermissionError)
        super().on_trash()

    def before_rename(self, old, new, merge=False):
        if any(managed_item(name, locked=True) for name in sorted({old, new})):
            frappe.throw("A managed ERPNext Item cannot be renamed or merged.", frappe.PermissionError)
        super().before_rename(old, new, merge=merge)


def _used_master(doctype, name):
    link, _ = MASTER_FIELDS[doctype]
    return frappe.db.table_exists(SKU) and frappe.db.get_value(SKU, {link: name}, for_update=True)


def guard_master_change(doc, method=None):
    previous = doc.get_doc_before_save()
    if previous is None:
        return
    _, fields = MASTER_FIELDS[doc.doctype]
    if any(doc.get(field) != previous.get(field) for field in fields) and _used_master(doc.doctype, doc.name):
        frappe.throw("Cannot change the identity or inventory semantics of a master used by a SKU.", frappe.PermissionError)


def guard_master_trash(doc, method=None):
    if _used_master(doc.doctype, doc.name):
        frappe.throw("Cannot delete a master used by a SKU.", frappe.PermissionError)


def guard_master_rename(doc, method, old, new, merge=False):
    if any(_used_master(doc.doctype, name) for name in (old, new)):
        frappe.throw("Cannot rename or merge a master used by a SKU.", frappe.PermissionError)
