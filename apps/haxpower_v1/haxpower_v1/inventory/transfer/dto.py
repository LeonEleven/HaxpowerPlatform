"""Internal transfer commands and the authorized original-dispatch projection."""

from dataclasses import dataclass
from datetime import datetime

from ..batch.dto import BatchInput


@dataclass(frozen=True)
class TransferLineInput:
    sku_id: str
    source_location_id: str
    target_location_id: str
    batch: BatchInput
    quantity: str
    uom: str
    reason: str = ""
    notes: str = ""


@dataclass(frozen=True)
class TransferDraftInput:
    company: str
    lines: tuple[TransferLineInput, ...]
    reason: str = ""
    notes: str = ""


@dataclass(frozen=True)
class TransferLine(TransferLineInput):
    transfer_line_id: str = ""


@dataclass(frozen=True)
class TransferDraft:
    company: str
    transfer_id: str
    revision: int
    lines: tuple[TransferLine, ...]
    reason: str
    notes: str


@dataclass(frozen=True)
class DispatchTransfer:
    draft: TransferDraft
    idempotency_key: str


@dataclass(frozen=True)
class DispatchResult:
    transfer_id: str
    operation_id: str
    status: str
    revision: int


@dataclass(frozen=True)
class TransferQuantity:
    transfer_line_id: str
    quantity: str


@dataclass(frozen=True)
class ReceiveTransfer:
    company: str
    transfer_id: str
    expected_revision: int
    idempotency_key: str
    lines: tuple[TransferQuantity, ...]
    reason: str = ""
    notes: str = ""


@dataclass(frozen=True)
class ReturnTransfer:
    company: str
    transfer_id: str
    expected_revision: int
    idempotency_key: str
    lines: tuple[TransferQuantity, ...]
    reason: str
    notes: str = ""


@dataclass(frozen=True)
class TransferActionResult:
    transfer_id: str
    operation_id: str
    status: str
    revision: int


@dataclass(frozen=True)
class DispatchLine:
    transfer_line_id: str
    sku_id: str
    source_location_id: str
    target_location_id: str
    batch: BatchInput
    batch_id: str | None
    dispatched_quantity: str
    uom: str
    transit_location_id: str
    ownership_id: str
    native_entry: str
    native_detail: str
    native_inward_sle: str
    received_quantity: str
    returned_quantity: str
    remaining_quantity: str


@dataclass(frozen=True)
class Dispatch:
    company: str
    transfer_id: str
    operation_id: str
    status: str
    revision: int
    dispatched_by: str
    dispatched_at: datetime
    reason: str
    notes: str
    lines: tuple[DispatchLine, ...]
