"""Types shared by batch preview, resolution and diagnostics."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, TypedDict


class BatchKind(StrEnum):
    BATCH = "BATCH"
    NO_BATCH = "NO_BATCH"


class BatchNumberInput(TypedDict):
    mode: Literal["BATCH"]
    batchNo: str


class NoBatchInput(TypedDict):
    mode: Literal["NO_BATCH"]


BatchInput = BatchNumberInput | NoBatchInput


class NormalizationStatus(StrEnum):
    EXACT = "EXACT"
    CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"
    COLLISION = "COLLISION"


class MappingStatus(StrEnum):
    VERIFIED = "VERIFIED"
    MISSING_BATCH = "MISSING_BATCH"
    WRONG_ITEM = "WRONG_ITEM"
    DUPLICATE_MAPPING = "DUPLICATE_MAPPING"
    ORPHAN_BATCH = "ORPHAN_BATCH"
    RESERVED_NAME_CONFLICT = "RESERVED_NAME_CONFLICT"
    INACTIVE = "INACTIVE"
    INVALID_NATIVE_STATE = "INVALID_NATIVE_STATE"
    NORMALIZATION_CONFLICT = "NORMALIZATION_CONFLICT"
    INCOMPATIBLE_ITEM = "INCOMPATIBLE_ITEM"
    MISSING_SKU = "MISSING_SKU"


@dataclass(frozen=True)
class Normalization:
    original: str
    candidate: str
    policy_version: str
    status: NormalizationStatus


@dataclass(frozen=True)
class BatchSelection:
    kind: BatchKind
    normalization: Normalization | None


@dataclass(frozen=True)
class Batch:
    id: str
    sku_id: str
    kind: BatchKind
    batch: str | None
    status: Literal["Active", "Inactive"]
    mapping_status: MappingStatus


@dataclass(frozen=True)
class BatchResolution:
    batch: Batch
    normalization: Normalization | None
    created: bool


@dataclass(frozen=True)
class MappingDiagnostic:
    sku_id: str | None
    batch_id: str | None
    native_batch: str | None
    status: MappingStatus


class BatchError(ValueError):
    """Domain failure with a safe code and optional input-only preview."""

    def __init__(self, code: str, normalization: Normalization | None = None):
        super().__init__(code)
        self.code = code
        self.normalization = normalization
