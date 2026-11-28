"""Migration projections and writes through the formal catalog services."""

from dataclasses import MISSING, asdict, fields
from datetime import date
from decimal import Decimal
import re

import frappe

from ..adapters.erpnext import item
from ..catalog import service, sku, validation
from ..catalog.dto import (
    ACTIVE, DATA_TYPES, DECIMAL, MULTI, SELECT_TYPES, AttributeData, CategoryData, HistoricalIdentifier,
    OptionData, OptionSelection, SpecificationData, TemplateData,
)
from ..catalog.persistence import ATTRIBUTE, CATEGORY, OPTION, SPECIFICATION, TEMPLATE
from ..catalog.sku_dto import BrandData, Identifier, SKUData
from ..catalog.sku_persistence import BRAND, SKU
from .formats import ORDER, digest

TYPES = dict(zip(ORDER, (CATEGORY, ATTRIBUTE, OPTION, TEMPLATE, BRAND, SPECIFICATION, SKU)))
DTOS = dict(zip(ORDER, (CategoryData, AttributeData, OptionData, TemplateData, BrandData, SpecificationData, SKUData)))
SAVERS = {
    "Categories": service.save_category, "Attributes": service.save_attribute, "Options": service.save_option,
    "Templates": service.save_template, "Brands": sku.save_brand,
}
IMMUTABLE = {
    "Categories": {"code"}, "Attributes": {"code"}, "Options": {"attribute", "option_key"},
    "Templates": {"category", "attribute"}, "Brands": {"code"},
    "Specifications": {"code", "category", "stock_uom"}, "SKUs": set(sku.IMMUTABLE_FIELDS),
}
DISPLAY = {"display_name": 140, "description": 4000, "unit": 140, "stock_uom": 140}
REQUIRED = {
    "Categories": ("code", "display_name"), "Attributes": ("code", "display_name", "data_type"),
    "Options": ("attribute", "option_key", "display_name"), "Templates": ("category", "attribute"),
    "Brands": ("code", "display_name"), "Specifications": ("category", "display_name", "stock_uom"),
    "SKUs": ("specification", "brand", "display_name"),
}
CODE_KINDS = set(ORDER) - {"Options", "Templates"}


class Blocked(ValueError):
    pass


def _serialize(value):
    if isinstance(value, OptionSelection):
        return value.id
    if isinstance(value, (Decimal, date)):
        return str(value)
    return value


def snapshot(kind, identity):
    doctype = TYPES[kind]
    if not isinstance(identity, str) or not validation.IDENTIFIER.fullmatch(identity):
        raise Blocked("INVALID_TARGET_ID")
    if not frappe.db.get_value(doctype, identity, "name"):
        raise Blocked("TARGET_MISSING")
    doc = frappe.get_doc(doctype, identity)
    if kind == "Specifications":
        data = service._specification(doc)
        return {
            **asdict(data.data), "revision": data.revision, "quantity_precision": doc.quantity_precision,
            "values": {entry.attribute_id: [_serialize(value) for value in entry.values] for entry in data.attributes},
            "history": [list((row.source, row.value)) for row in data.historical_identifiers],
        }
    if kind == "SKUs":
        data = sku._stock_item(doc)
        return {
            **asdict(data.data), "revision": data.revision, "status": data.status,
            "native_item": doc.native_item, "mapping_status": data.mapping_status,
            "history": [[row.source, row.value] for row in data.identifiers if row.kind == "LEGACY"],
            "other_identifiers": [asdict(row) for row in data.identifiers if row.kind != "LEGACY"],
            "aliases": [asdict(row) for row in data.aliases],
        }
    return asdict(service._data(DTOS[kind], doc))


def lookup(kind, identity, code):
    if code is not None:
        if kind not in CODE_KINDS or not isinstance(code, str) or not validation.KEY.fullmatch(code):
            raise Blocked("INVALID_TARGET_CODE")
        row = frappe.db.get_value(TYPES[kind], {"code": code}, ["name", "code"], as_dict=True)
        if not row or row.code != code or identity and identity != row.name:
            raise Blocked("TARGET_CODE_MISMATCH")
        identity = row.name
    if not identity:
        raise Blocked("EXPLICIT_TARGET_REQUIRED")
    return identity, snapshot(kind, identity)


def matching(kind, payload):
    if kind in CODE_KINDS and "code" in payload:
        query = {"code": payload["code"]}
    elif kind == "Options" and isinstance(payload["attribute"], str):
        query = {"attribute": payload["attribute"], "option_key": payload["option_key"]}
    elif kind == "Templates" and all(isinstance(payload[key], str) for key in ("category", "attribute")):
        query = {name: payload[name] for name in ("category", "attribute")}
    else:
        return None
    return frappe.db.get_value(TYPES[kind], query, "name")


def similar(kind, payload):
    if kind not in {"Specifications", "SKUs", "Brands", "Options"} or not payload.get("display_name"):
        return []
    filters = {"display_name": payload["display_name"]}
    if kind == "Options" and isinstance(payload["attribute"], str):
        filters["attribute"] = payload["attribute"]
    if kind == "SKUs" and isinstance(payload["specification"], str):
        filters["specification"] = payload["specification"]
    matches = frappe.get_all(TYPES[kind], filters=filters, pluck="name", order_by="name asc", limit=8)
    stem = payload["display_name"].rstrip("0123456789").rstrip()
    if len(matches) < 8 and len(stem) >= 6 and stem != payload["display_name"] and not set(stem) & {"%", "_", "\\"}:
        filters["display_name"] = ["like", stem + "%"]
        candidates = frappe.get_all(TYPES[kind], filters=filters, pluck="name", order_by="name asc", limit=8)
        matches = list(dict.fromkeys(matches + candidates))[:8]
    return matches


def template(category):
    return {row.name: digest(asdict(service._data(TemplateData, row))) for row in map(
        lambda identity: frappe.get_doc(TEMPLATE, identity),
        frappe.get_all(TEMPLATE, filters={"category": category}, pluck="name"),
    )}


def template_rules(category):
    return [service._data(TemplateData, row) for row in service._template_rows(category)]


def unit(name):
    precision = item.configured_precision(name)
    return {"precision": precision, "item_group": item.validate_configuration(name, precision)}


def defaults(kind):
    return {field.name: field.default for field in fields(DTOS[kind]) if field.default is not MISSING}


def normalize(kind, supplied, current, *, create):
    payload = {}
    for key, value in supplied.items():
        if value is None:
            continue
        if key in DISPLAY:
            limit = DISPLAY[key]
            if not isinstance(value, str) or len(value) > limit or value != value.strip() or re.search(r"<[^>]*>", value):
                raise Blocked("TEXT_REQUIRES_EXPLICIT_CLEAN_VALUE")
            if not value and key != "description":
                raise Blocked("REQUIRED_FIELD_MISSING")
        elif key in {"code", "option_key"}:
            if not isinstance(value, str) or not validation.KEY.fullmatch(value):
                raise Blocked("INVALID_CATALOG_CODE")
            value = validation.key(value, "Migration code")
        payload[key] = value
    if create and any(not payload.get(field) for field in REQUIRED[kind]):
        raise Blocked("REQUIRED_FIELD_MISSING")
    if current:
        for name in IMMUTABLE[kind] & payload.keys():
            if current.get(name) != payload[name]:
                raise Blocked("IMMUTABLE_TARGET_IDENTITY")
        if kind == "SKUs" and current["mapping_status"] != "Verified":
            raise Blocked("NATIVE_ITEM_MAPPING_CONFLICT")
    if "data_type" in payload:
        validation.choice(payload["data_type"], DATA_TYPES, "attribute data type")
        if payload.get("unit") and payload["data_type"] != DECIMAL:
            raise Blocked("NONDECIMAL_ATTRIBUTE_UNIT")
    if "status" in payload:
        validation.status(payload["status"])
    return payload


def apply(group, payload):
    kind, identity = group["kind"], group["target"]
    action = group["action"]
    if action in {"reference", "unchanged"}:
        return identity
    if kind in SAVERS:
        data = DTOS[kind](**{key: payload[key] for key in payload})
        return SAVERS[kind](data, record_id=identity).id

    values = payload.pop("values", None)
    history = payload.pop("history", None)
    if kind == "Specifications":
        history = [HistoricalIdentifier(source=source, value=value) for source, value in history or ()]
        data = SpecificationData(**payload)
        if identity:
            previous = service.get_specification(identity)
            if previous.revision != group["before"]["revision"]:
                raise Blocked("STALE_PLAN")
            return service.edit_specification(identity, data, expected_revision=previous.revision,
                                              attributes=values, historical_identifiers=history or None).id
        return service.create_specification(data, attributes=values or {}, historical_identifiers=history).id

    legacy = [Identifier(kind="LEGACY", source=source, value=value) for source, value in history or ()]
    data = SKUData(**payload)
    if identity:
        previous = sku.get_sku(identity)
        if previous.revision != group["before"]["revision"]:
            raise Blocked("STALE_PLAN")
        identifiers = list(previous.identifiers)
        existing = {(row.source, row.value) for row in identifiers if row.kind == "LEGACY"}
        identifiers.extend(row for row in legacy if (row.source, row.value) not in existing)
        return sku.edit_sku(identity, data, expected_revision=previous.revision,
                            identifiers=identifiers if legacy else None).id
    return sku.create_sku(data, identifiers=legacy).id


def merge(kind, payload, current):
    return {**{field.name: current[field.name] for field in fields(DTOS[kind]) if field.name in current}, **payload}
