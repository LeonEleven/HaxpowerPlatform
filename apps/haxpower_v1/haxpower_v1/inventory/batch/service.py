"""Internal V1-06 batch preview, lookup, and atomic synthetic provisioning."""

from dataclasses import replace
from uuid import uuid4

import frappe

from ...adapters.erpnext import batch as native, item
from ...catalog.dto import ACTIVE
from ...catalog.sku_persistence import SKU
from ...catalog.validation import IDENTIFIER
from ..guard import lock_stock_writes
from .authorization import require_batch_operator, require_candidate_writer
from .dto import (
    Batch, BatchError, BatchInput, BatchKind, BatchResolution, BatchSelection,
    MappingStatus, Normalization, NormalizationStatus,
)
from .normalization import CANDIDATE_VERSION, normalize_selection, selection_key
from .persistence import batch_write, mappings, validated_identity


def _load_sku(sku_id, *, locked):
    if not isinstance(sku_id, str) or not IDENTIFIER.fullmatch(sku_id):
        raise BatchError("BATCH_SKU_UNAVAILABLE")
    if not frappe.db.get_value(SKU, sku_id, for_update=locked):
        raise BatchError("BATCH_SKU_UNAVAILABLE")
    sku = frappe.get_doc(SKU, sku_id, for_update=locked)
    if sku.name != sku_id or sku.status != ACTIVE:
        raise BatchError("BATCH_SKU_UNAVAILABLE")
    return sku


def _load_batch(sku, batch_id, *, locked):
    if not isinstance(batch_id, str) or not IDENTIFIER.fullmatch(batch_id):
        raise BatchError("BATCH_MAPPING_UNAVAILABLE")
    rows = mappings({"sku": sku.name, "name": batch_id}, locked=locked)
    if len(rows) != 1 or rows[0].name != batch_id:
        raise BatchError("BATCH_MAPPING_UNAVAILABLE")
    return rows[0]


def _matching_rows(sku, selection, *, locked):
    return mappings(
        {"sku": sku.name, "kind": selection.kind, "normalized_key": selection_key(selection)}, locked=locked,
    )


def _preview(selection, rows):
    if any(validated_identity(row) is None for row in rows):
        raise BatchError("BATCH_MAPPING_UNAVAILABLE")
    preview = selection.normalization
    if preview and any(row.original_text != preview.original for row in rows):
        preview = replace(preview, status=NormalizationStatus.COLLISION)
    return replace(selection, normalization=preview)


def _verified_batch(row, sku, *, locked):
    if native.mapping_status(row, sku, locked=locked) != MappingStatus.VERIFIED:
        raise BatchError("BATCH_MAPPING_UNAVAILABLE")
    return Batch(row.name, row.sku, BatchKind(row.kind), row.original_text, row.status, MappingStatus.VERIFIED)


def _confirmed_preview(selection, rows, confirmation):
    preview = _preview(selection, rows).normalization
    if preview and preview.status == NormalizationStatus.COLLISION:
        raise BatchError("BATCH_NORMALIZATION_COLLISION", preview)
    if preview and preview.status == NormalizationStatus.CONFIRMATION_REQUIRED and confirmation != preview:
        raise BatchError("BATCH_NORMALIZATION_CONFIRMATION_REQUIRED", preview)
    return preview


def preview_selection(sku_id: str, selection: BatchInput | None) -> BatchSelection:
    require_batch_operator()
    parsed = normalize_selection(selection)
    sku = _load_sku(sku_id, locked=False)
    return _preview(parsed, _matching_rows(sku, parsed, locked=False))


def get_batch(sku_id: str, batch_id: str) -> Batch:
    require_batch_operator()
    sku = _load_sku(sku_id, locked=False)
    return _verified_batch(_load_batch(sku, batch_id, locked=False), sku, locked=False)


def resolve_existing(
    sku_id: str, selection: BatchInput | None, *, confirmation: Normalization | None = None,
) -> BatchResolution:
    """Resolve a selection without creating a previously absent stock bucket."""
    require_batch_operator()
    parsed = normalize_selection(selection)
    sku = _load_sku(sku_id, locked=False)
    return _resolve_existing(sku, parsed, locked=False, confirmation=confirmation)


def _resolve_existing(sku, parsed, *, locked, confirmation=None):
    rows = _matching_rows(sku, parsed, locked=locked)
    preview = _confirmed_preview(parsed, rows, confirmation)
    if len(rows) != 1:
        raise BatchError("BATCH_MAPPING_UNAVAILABLE")
    return BatchResolution(_verified_batch(rows[0], sku, locked=locked), preview, created=False)


def resolve_or_create(
    sku_id: str, selection: BatchInput | None, *, confirmation: Normalization | None = None,
) -> BatchResolution:
    require_batch_operator()
    parsed = normalize_selection(selection)
    try:
        with batch_write():
            lock_stock_writes()
            # A current read under the SKU row lock serializes the first and
            # all subsequent selections, even if the caller has an old snapshot.
            sku = _load_sku(sku_id, locked=True)
            return _resolve_locked(sku, parsed, require_candidate_writer, confirmation=confirmation)
    except BatchError:
        raise
    except (frappe.QueryDeadlockError, frappe.QueryTimeoutError):
        raise BatchError("BATCH_RETRY_REQUIRED") from None
    except Exception:
        # Never relay native exception text, SQL, or Batch names to callers.
        raise BatchError("BATCH_PERSISTENCE_FAILED") from None


def _resolve_locked(sku, parsed, authorize, *, confirmation=None):
    """Resolve under the shared stock guard in the caller's transaction."""
    authorize(sku)
    rows = _matching_rows(sku, parsed, locked=True)
    preview = _confirmed_preview(parsed, rows, confirmation)
    if rows:
        if len(rows) != 1:
            raise BatchError("BATCH_MAPPING_UNAVAILABLE")
        return BatchResolution(_verified_batch(rows[0], sku, locked=True), preview, created=False)
    prior = mappings({"sku": sku.name}, locked=True)
    if any(validated_identity(row) is None for row in prior):
        raise BatchError("BATCH_MAPPING_UNAVAILABLE")
    native.assert_native_ownership(sku, prior)
    batch_id = uuid4().hex
    item._enable_batch(sku, batch_id, authorize=authorize)
    row = native.create_mapping(sku, batch_id, parsed, CANDIDATE_VERSION, authorize=authorize)
    return BatchResolution(_verified_batch(row, sku, locked=True), preview, created=True)


def lock_execution_mapping(sku, kind, batch_id):
    """Resolve an existing identity under the shared stock guard without provisioning."""
    if kind == BatchKind.NO_BATCH and batch_id is None:
        rows = _matching_rows(sku, normalize_selection({"mode": BatchKind.NO_BATCH}), locked=True)
        if not rows:
            return None
        if len(rows) != 1:
            raise BatchError("BATCH_MAPPING_UNAVAILABLE")
        row, = rows
    else:
        row = _load_batch(sku, batch_id, locked=True)
    if row.kind != kind:
        raise BatchError("BATCH_MAPPING_UNAVAILABLE")
    return _verified_batch(row, sku, locked=True)
