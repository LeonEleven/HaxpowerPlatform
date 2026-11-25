"""Domain-only input and result types for the stock execution kernel."""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from ..batch.dto import BatchKind


class StockError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class QuantitySign(StrEnum):
    POSITIVE = "positive"
    NONNEGATIVE = "nonnegative"
    NONZERO = "nonzero"


@dataclass(frozen=True)
class Line:
    line_id: str
    line_no: int
    sku_id: str
    quantity: str
    uom: str
    batch_kind: BatchKind
    batch_id: str | None = None
    source: str | None = None
    target: str | None = None
    reason: str = ""
    notes: str = ""
    batch_no: str | None = None


@dataclass(frozen=True)
class LineQuantity:
    line_id: str
    quantity: str


@dataclass(frozen=True)
class Command:
    company: str
    document_id: str
    expected_revision: int
    idempotency_key: str
    lines: tuple[Line | LineQuantity, ...]
    reason: str = ""
    original_operation: str | None = None
    notes: str | None = None
    snapshot_id: str | None = None


@dataclass(frozen=True)
class Bucket:
    company: str
    sku_id: str
    location_id: str
    batch_kind: BatchKind
    batch_id: str | None


@dataclass(frozen=True)
class NativeLink:
    line_id: str
    role: str
    parent_type: str
    parent_id: str
    detail_type: str
    detail_id: str
    ownership_id: str | None = None


@dataclass(frozen=True)
class Result:
    document_id: str
    operation_id: str | None
    revision: int
    status: str = "Effective"
    submission_id: str | None = None


def sum_demands(company: str, lines: tuple[Line, ...], *, location_field: str) -> dict[Bucket, Decimal]:
    """Sum already validated canonical lines for one leg of an action."""
    from decimal import localcontext

    from ...catalog.validation import DECIMAL_WIDTH

    if location_field not in ("source", "target"):
        raise ValueError("Unknown bucket location field")
    totals = {}
    with localcontext() as context:
        # One line may have a large integral part and another a tiny fraction.
        context.prec = 2 * DECIMAL_WIDTH + len(str(len(lines)))
        for line in lines:
            if location := getattr(line, location_field):
                batch_id = line.batch_id if line.batch_kind == BatchKind.BATCH else None
                bucket = Bucket(company, line.sku_id, location, line.batch_kind, batch_id)
                totals[bucket] = totals.get(bucket, Decimal(0)) + Decimal(line.quantity)
    return totals
