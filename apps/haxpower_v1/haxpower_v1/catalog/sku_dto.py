"""Domain contracts for the internal SKU catalog."""

from dataclasses import dataclass
from typing import Literal

from .dto import ACTIVE

IdentifierKind = Literal["OEM", "MANUFACTURER", "ENGRAVING", "LEGACY", "OTHER"]
MappingStatus = Literal["Verified", "Unmanaged", "Missing Item", "Orphan Marker", "Duplicate Marker", "Item Drift"]


@dataclass(frozen=True, kw_only=True)
class BrandData:
    code: str
    display_name: str
    description: str = ""
    status: str = ACTIVE


@dataclass(frozen=True)
class Brand:
    id: str
    data: BrandData


@dataclass(frozen=True, kw_only=True)
class SKUData:
    code: str
    specification: str
    brand: str | None
    display_name: str
    stock_uom: str
    quantity_precision: int
    description: str = ""


@dataclass(frozen=True, kw_only=True)
class Identifier:
    kind: IdentifierKind
    value: str
    source: str = ""
    notes: str = ""


@dataclass(frozen=True, kw_only=True)
class Alias:
    value: str
    language: str = ""
    source: str = ""
    searchable: bool = True


@dataclass(frozen=True)
class StockItem:
    id: str
    revision: int
    data: SKUData
    brand: Brand | None
    status: str
    identifiers: tuple[Identifier, ...]
    aliases: tuple[Alias, ...]
    mapping_status: MappingStatus


@dataclass(frozen=True)
class ItemMappingDiagnostic:
    sku_id: str | None
    item_name: str
    marker: str | None
    status: MappingStatus
