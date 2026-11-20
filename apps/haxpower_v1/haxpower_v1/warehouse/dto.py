"""Server-side warehouse DTOs with stable domain IDs."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Warehouse:
    id: str
    company: str
    code: str
    display_name: str
    description: str
    status: str
    kind: str
    revision: int

    @property
    def system_managed(self) -> bool:
        return self.kind == "Transit"


@dataclass(frozen=True)
class Location:
    id: str
    warehouse_id: str
    code: str
    display_name: str
    description: str
    status: str
    revision: int


@dataclass(frozen=True)
class MappingDiagnostic:
    location_id: str | None
    native_warehouse: str | None
    status: str
