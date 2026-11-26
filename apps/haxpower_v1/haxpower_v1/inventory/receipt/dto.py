"""Domain DTOs for receipts. Quantities cross the boundary as decimal strings."""

from dataclasses import dataclass

from ..batch.dto import BatchInput


@dataclass(frozen=True)
class ReceiptLineInput:
    sku_id: str
    location_id: str
    batch: BatchInput
    quantity: str
    uom: str
    reason: str = ""
    notes: str = ""


@dataclass(frozen=True)
class ReceiptDraftInput:
    company: str
    lines: tuple[ReceiptLineInput, ...]
    reason: str = ""
    notes: str = ""


@dataclass(frozen=True)
class ReceiptLine(ReceiptLineInput):
    line_id: str = ""


@dataclass(frozen=True)
class ReceiptDraft:
    company: str
    document_id: str
    revision: int
    lines: tuple[ReceiptLine, ...]
    reason: str


@dataclass(frozen=True)
class SubmitReceipt:
    draft: ReceiptDraft
    idempotency_key: str


@dataclass(frozen=True)
class ReceiptResult:
    document_id: str
    operation_id: str | None
    status: str
    revision: int
    lines: tuple[ReceiptLine, ...]
    submission_id: str | None = None
