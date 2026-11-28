"""Controlled initialization of the approved first product configuration."""

from decimal import Decimal
import json
from pathlib import Path

import frappe

from .persistence import CONFIGURATION

_CONFIG = json.loads(Path(__file__).with_suffix(".json").read_text(encoding="utf-8"))
FORMAL_UNITS = _CONFIG["units"]
FORMAL_UNIT_LIMITS = {unit: Decimal(data["line_limit"]) for unit, data in FORMAL_UNITS.items()}
SPECIAL_BRAND_CODES = frozenset(row["code"] for row in _CONFIG["brands"])


def initialize():
    from ..adapters.erpnext.item import validate_configuration
    from . import service, sku
    from .dto import MULTI, MULTI_SELECT, SINGLE, AttributeData, CategoryData, TemplateData
    from .persistence import ATTRIBUTE, CATEGORY, TEMPLATE, catalog_write
    from .sku_dto import BrandData
    from .sku_persistence import BRAND

    with catalog_write(parameters=True):
        version = _CONFIG["version"]
        for unit, data in FORMAL_UNITS.items():
            validate_configuration(unit, data["precision"])

        if entry := frappe.db.get_value(CONFIGURATION, version, "record_ids", for_update=True):
            records = json.loads(entry)
            for doctype, refs in records.items():
                for key, record_id in refs.items():
                    if not frappe.db.get_value(doctype, record_id, "name", for_update=True):
                        frappe.throw(f"Approved configuration requires review: {doctype} {key} is missing.")
            return records

        records = {doctype: {} for doctype in (ATTRIBUTE, CATEGORY, BRAND, TEMPLATE)}
        for doctype, key, saver, row_type in (
            (ATTRIBUTE, "attributes", service.save_attribute, AttributeData),
            (CATEGORY, "categories", service.save_category, CategoryData),
            (BRAND, "brands", sku.save_brand, BrandData),
        ):
            for row in _CONFIG[key]:
                data = {k: v for k, v in row.items() if k != "attributes"}
                if frappe.db.get_value(doctype, {"code": row["code"]}, for_update=True):
                    frappe.throw(f"Approved configuration conflicts with existing {doctype} code {row['code']}.")
                if frappe.db.get_value(doctype, {"display_name": row["display_name"]}, for_update=True):
                    frappe.throw(f"Approved configuration conflicts with existing {doctype} name {row['display_name']}.")
                records[doctype][row["code"]] = saver(row_type(**data)).id

        multi = {row["code"] for row in _CONFIG["attributes"] if row["data_type"] == MULTI_SELECT}
        for entry in _CONFIG["categories"]:
            category = entry["code"]
            for order, attribute in enumerate(_CONFIG["common_attributes"] + entry["attributes"], start=1):
                record = service.save_template(TemplateData(
                    category=records[CATEGORY][category], attribute=records[ATTRIBUTE][attribute],
                    display_order=order, value_mode=MULTI if attribute in multi else SINGLE, searchable=True,
                ))
                records[TEMPLATE][f"{category}/{attribute}"] = record.id

        frappe.get_doc(doctype=CONFIGURATION, record_ids=json.dumps(records, sort_keys=True)).insert(
            ignore_permissions=True, set_name=version,
        )
        return records
