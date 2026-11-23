"""The only adapter that converts a domain batch to an ERPNext Batch."""

import frappe
from frappe.utils import cint

from ...access.execution import allows_native_write, native_write
from ...catalog.dto import ACTIVE
from ...catalog.sku_persistence import SKU
from ...catalog.validation import IDENTIFIER
from ...inventory.batch.authorization import require_batch_operator, require_candidate_writer
from ...inventory.batch.dto import BatchError, BatchKind, MappingDiagnostic, MappingStatus
from ...inventory.batch.normalization import reserved_name, selection_key
from ...inventory.batch.persistence import MAPPING, mappings, validated_identity
from . import item

BATCH = "Batch"
BATCH_SERVICE = "Batch mapping"
PREFIXES = {BatchKind.BATCH: "HP-RB-", BatchKind.NO_BATCH: "HP-NB-"}
NATIVE_FIELDS = ("name", "batch_id", "item", "disabled", "docstatus", "parent_batch", "expiry_date")


def _native_name(batch_id, kind):
    return PREFIXES[BatchKind(kind)] + batch_id


def managed_batch(name, *, locked=False):
    if not isinstance(name, str) or not name:
        return False
    if reserved_name(name):
        return True
    if frappe.db.table_exists(MAPPING) and frappe.db.get_value(
        MAPPING, {"native_batch": name}, "name", for_update=locked,
    ):
        return True
    native_item = frappe.db.get_value(BATCH, name, "item", for_update=locked)
    return item.managed_item(native_item, locked=locked)


def allows_batch_insert(doc, event):
    if event not in ("before_insert", "before_validate", "before_save") or not doc.is_new():
        return False
    native_name = doc.batch_id or ""
    if not native_name.startswith(tuple(PREFIXES.values())) or doc.name not in (None, native_name):
        return False
    if not doc.item or doc.disabled or doc.parent_batch or doc.expiry_date or doc.docstatus != 0:
        return False
    sku_id = frappe.db.get_value("Item", doc.item, item.SKU_MARKER, for_update=True)
    batch_id = native_name.rsplit("-", 1)[-1]
    return bool(sku_id and IDENTIFIER.fullmatch(batch_id) and allows_native_write(
        BATCH_SERVICE, native_name, batch_id, existing=False, sku_id=sku_id,
    ) and frappe.db.get_value(SKU, sku_id, "native_item", for_update=True) == doc.item)


def allows_mapping_insert(doc):
    return (
        doc.is_new() and doc.name and IDENTIFIER.fullmatch(doc.name)
        and doc.kind in PREFIXES and doc.native_batch == _native_name(doc.name, doc.kind)
        and doc.status == ACTIVE
        and allows_native_write(BATCH_SERVICE, doc.native_batch, doc.name, existing=False, sku_id=doc.sku)
    )


def create_mapping(sku, batch_id, selection, policy_version):
    """Insert both records under the same actor and caller-owned savepoint."""
    require_candidate_writer(sku)
    native_name = _native_name(batch_id, selection.kind)
    if frappe.db.get_value(BATCH, native_name, for_update=True):
        raise BatchError("BATCH_RESERVED_NAME_CONFLICT")

    with native_write(
        lambda: require_candidate_writer(sku), service=BATCH_SERVICE, native_name=native_name,
        domain_id=batch_id, existing=False, sku_id=sku.name,
    ):
        native = frappe.get_doc(doctype=BATCH, item=sku.native_item, batch_id=native_name, disabled=0)
        native.flags.notify_update = False
        native.insert(ignore_permissions=True)
        row = frappe.get_doc(
            doctype=MAPPING, name=batch_id, sku=sku.name, kind=selection.kind,
            original_text=selection.normalization.original if selection.normalization else None,
            normalized_key=selection_key(selection), policy_version=policy_version,
            native_batch=native_name, status=ACTIVE,
        )
        row.insert(ignore_permissions=True, set_name=batch_id)
    return row


def assert_native_ownership(sku, rows):
    """Before creating a new identity, refuse to adopt unmapped native batches."""
    mapped_names = {row.native_batch for row in rows}
    existing = frappe.db.get_values(BATCH, {"item": sku.native_item}, "name", for_update=True)
    if any(name not in mapped_names for (name,) in existing):
        raise BatchError("BATCH_MAPPING_UNAVAILABLE")


def mapping_status(row, sku, *, locked=False):
    if not sku or row.sku != sku.name:
        return MappingStatus.MISSING_SKU
    if (selection := validated_identity(row)) is None:
        return MappingStatus.NORMALIZATION_CONFLICT
    same_key = mappings(
        {"sku": row.sku, "kind": row.kind, "normalized_key": row.normalized_key}, locked=locked,
    )
    same_native = mappings({"native_batch": row.native_batch}, locked=locked)
    if [match.name for match in same_key] != [row.name] or [match.name for match in same_native] != [row.name]:
        return MappingStatus.DUPLICATE_MAPPING
    if not IDENTIFIER.fullmatch(row.name) or row.native_batch != _native_name(row.name, selection.kind):
        return MappingStatus.RESERVED_NAME_CONFLICT
    native = frappe.db.get_value(BATCH, row.native_batch, NATIVE_FIELDS, as_dict=True, for_update=locked)
    if not native:
        return MappingStatus.MISSING_BATCH
    if native.name != row.native_batch or native.batch_id != row.native_batch:
        return MappingStatus.RESERVED_NAME_CONFLICT
    if native.item != sku.native_item:
        return MappingStatus.WRONG_ITEM
    if row.status != ACTIVE or sku.status != ACTIVE or cint(native.disabled):
        return MappingStatus.INACTIVE
    if cint(native.docstatus) != 0 or native.parent_batch or native.expiry_date:
        return MappingStatus.INVALID_NATIVE_STATE
    if item.mapping_status(sku, locked=locked) != "Verified":
        return MappingStatus.INCOMPATIBLE_ITEM
    return MappingStatus.VERIFIED


def resolve_native_batch(sku_id: str, batch_id: str) -> str:
    """For future native posting adapters inside their authorized transaction."""
    require_batch_operator()
    from ...inventory.batch.service import _load_batch, _load_sku

    sku = _load_sku(sku_id, locked=True)
    row = _load_batch(sku, batch_id, locked=True)
    if mapping_status(row, sku, locked=True) != MappingStatus.VERIFIED:
        raise BatchError("BATCH_MAPPING_UNAVAILABLE")
    return row.native_batch


def diagnose_mappings() -> tuple[MappingDiagnostic, ...]:
    require_batch_operator()
    rows = mappings({})
    skus = {row.name: row for row in frappe.get_all(
        SKU, fields=["name", "native_item", "display_name", "native_item_group", "stock_uom",
                     "quantity_precision", "status"],
    )}
    by_item = {sku.native_item: sku.name for sku in skus.values()}
    mapped_native_names = {row.native_batch for row in rows}
    diagnostics = [
        MappingDiagnostic(row.sku, row.name, row.native_batch, mapping_status(row, skus.get(row.sku)))
        for row in rows
    ]
    for native in frappe.get_all(BATCH, fields=["name", "batch_id", "item"], order_by="name"):
        if native.name not in mapped_native_names and (
            native.item in by_item or reserved_name(native.name) or reserved_name(native.batch_id)
        ):
            diagnostics.append(MappingDiagnostic(
                by_item.get(native.item), None, native.name, MappingStatus.ORPHAN_BATCH,
            ))
            if reserved_name(native.name) or reserved_name(native.batch_id):
                diagnostics.append(MappingDiagnostic(
                    by_item.get(native.item), None, native.name, MappingStatus.RESERVED_NAME_CONFLICT,
                ))
    mapped = {row.sku for row in rows}
    for sku in skus.values():
        if sku.name not in mapped and (
            item.mapping_status(sku) != "Verified" or not item.batch_settings_ready()
        ):
            diagnostics.append(MappingDiagnostic(sku.name, None, None, MappingStatus.INCOMPATIBLE_ITEM))
    return tuple(diagnostics)
