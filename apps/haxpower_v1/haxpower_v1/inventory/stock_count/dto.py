"""Domain stock count contracts. Quantities cross boundaries as decimal text."""
from dataclasses import dataclass
from enum import StrEnum


class Scope(StrEnum):
    LOCATION = 'LOCATION'
    SKU = 'SKU'
    BUCKET = 'BUCKET'


@dataclass(frozen=True)
class CreateCount:
    company: str
    location_id: str
    scope: Scope
    sku_id: str | None = None
    batch: dict | None = None
    notes: str = ''


@dataclass(frozen=True)
class CountLine:
    line_id: str
    line_no: int
    sku_id: str
    location_id: str
    batch_kind: str
    batch_id: str | None
    batch_no: str | None
    uom: str
    snapshot_quantity: str
    actual_quantity: str | None
    difference_quantity: str | None
    bucket_version: str
    confirmed: bool
    discovered: bool
    counted_by: str | None
    counted_at: str | None
    notes: str


@dataclass(frozen=True)
class CountTask:
    company: str
    task_id: str
    location_id: str
    scope: str
    sku_id: str | None
    batch_kind: str | None
    batch_id: str | None
    snapshot_id: str
    snapshot_at: str
    generation: str
    scope_version: str
    revision: int
    status: str
    stale_reason: str | None
    original_task: str | None
    recount_task: str | None
    operation_id: str | None
    lines: tuple[CountLine, ...]


@dataclass(frozen=True)
class Actual:
    line_id: str
    quantity: str
    notes: str = ''


@dataclass(frozen=True)
class CompleteCount:
    task: CountTask
    idempotency_key: str
    reason: str
