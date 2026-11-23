"""Domain DTOs for issues. Quantities cross the boundary as decimal strings."""

from dataclasses import dataclass

from ..batch.dto import BatchInput


@dataclass(frozen=True)
class IssueLineInput:
    sku_id: str
    source_location_id: str
    batch: BatchInput
    quantity: str
    uom: str
    reason: str = ""
    notes: str = ""


@dataclass(frozen=True)
class IssueDraftInput:
    company: str
    lines: tuple[IssueLineInput, ...]
    reason: str = ""
    notes: str = ""


@dataclass(frozen=True)
class IssueLine(IssueLineInput):
    line_id: str = ""


@dataclass(frozen=True)
class IssueDraft:
    company: str
    document_id: str
    revision: int
    lines: tuple[IssueLine, ...]
    reason: str


@dataclass(frozen=True)
class SubmitIssue:
    draft: IssueDraft
    idempotency_key: str


@dataclass(frozen=True)
class IssueResult:
    document_id: str
    operation_id: str
    status: str
    revision: int
    lines: tuple[IssueLine, ...]
