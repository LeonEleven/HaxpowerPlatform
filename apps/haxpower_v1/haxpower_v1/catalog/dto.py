"""Product catalog contracts for trusted application code."""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Generic, TypeVar


TEXT = "TEXT"
DECIMAL = "DECIMAL"
BOOLEAN = "BOOLEAN"
DATE = "DATE"
SINGLE_SELECT = "SINGLE_SELECT"
MULTI_SELECT = "MULTI_SELECT"
SELECT_TYPES = frozenset({SINGLE_SELECT, MULTI_SELECT})
DATA_TYPES = frozenset({TEXT, DECIMAL, BOOLEAN, DATE, *SELECT_TYPES})

ACTIVE = "Active"
INACTIVE = "Inactive"
SINGLE = "SINGLE"
MULTI = "MULTI"


@dataclass(frozen=True, kw_only=True)
class CategoryData:
    code: str
    display_name: str
    parent_category: str | None = None
    description: str = ""
    status: str = ACTIVE
    sort_order: int = 0


@dataclass(frozen=True, kw_only=True)
class AttributeData:
    code: str
    display_name: str
    data_type: str
    unit: str | None = None
    description: str = ""
    status: str = ACTIVE


@dataclass(frozen=True, kw_only=True)
class OptionData:
    attribute: str
    option_key: str
    display_name: str
    status: str = ACTIVE
    sort_order: int = 0


@dataclass(frozen=True, kw_only=True)
class TemplateData:
    category: str
    attribute: str
    is_required: bool = False
    value_mode: str = SINGLE
    searchable: bool = False
    identity_relevant: bool = False
    display_order: int = 0
    editable_for_technician: bool = False


@dataclass(frozen=True, kw_only=True)
class SpecificationData:
    code: str | None = None
    stock_uom: str | None = None
    display_name: str
    category: str
    description: str = ""
    status: str = ACTIVE


RecordData = TypeVar("RecordData", CategoryData, AttributeData, OptionData, TemplateData)


@dataclass(frozen=True)
class CatalogRecord(Generic[RecordData]):
    id: str
    data: RecordData


@dataclass(frozen=True, order=True)
class OptionSelection:
    id: str
    key: str


# Select input uses the option ID. Decimal input accepts Decimal, integer or
# decimal text. Date input accepts a date or ISO calendar date string.
InputValue = str | int | Decimal | bool | date
Value = str | Decimal | bool | date | OptionSelection


@dataclass(frozen=True)
class AttributeValues:
    attribute_id: str
    data_type: str
    values: tuple[Value, ...]


@dataclass(frozen=True, kw_only=True)
class HistoricalIdentifier:
    source: str
    value: str


@dataclass(frozen=True)
class Specification:
    id: str
    revision: int
    data: SpecificationData
    attributes: tuple[AttributeValues, ...]
    historical_identifiers: tuple[HistoricalIdentifier, ...] = ()


@dataclass(frozen=True)
class AuditEntry:
    id: str
    specification_id: str
    specification_revision: int
    operation_id: str
    actor: str
    occurred_at: datetime
    before: AttributeValues
    after: AttributeValues
