"""Batched business projections shared by catalog and inventory queries."""

from collections import defaultdict
from datetime import date
from decimal import Decimal

import frappe

from ..access import policy
from . import service, validation
from .dto import ACTIVE, BOOLEAN, DATE, DECIMAL, SELECT_TYPES, TEXT
from .persistence import ATTRIBUTE, CATEGORY, OPTION, SPEC_IDENTIFIER, SPECIFICATION, TEMPLATE, VALUE
from .sku_persistence import BRAND, SKU


def rows(table, fields, filters=None, *, order_by="name asc"):
    return frappe.get_all(table, fields=fields.split(), filters=filters, order_by=order_by)


def label(row):
    return dict(id=row.name, code=row.code, display_name=row.display_name)


def labels(table, *, active=False):
    return {row.name: row for row in rows(
        table, "name code display_name description" + (" parent_category" if table == CATEGORY else ""),
        {"status": ACTIVE} if active else None, order_by="code asc, name asc",
    )}


def children(table, parenttype, parentfield, fields, parents):
    result = defaultdict(list)
    if parents:
        for row in rows(table, "parent " + fields, {
            "parenttype": parenttype, "parentfield": parentfield, "parent": ("in", tuple(parents)),
        }, order_by="parent asc, idx asc, name asc"):
            result[row.parent].append(row)
    return result


def specification_rows(filters):
    specs = rows(SPECIFICATION, "name code display_name description category", filters)
    if not specs:
        return {}
    categories = labels(CATEGORY)
    definitions = {row.name: row for row in rows(ATTRIBUTE, "name code display_name data_type unit")}
    options = {row.name: row for row in rows(OPTION, "name attribute option_key display_name status")}
    templates = {(row.category, row.attribute): bool(row.searchable)
                 for row in rows(TEMPLATE, "category attribute searchable")}
    values = children(VALUE, SPECIFICATION, "attribute_values",
                      "attribute data_type option_id option_key value_text value_decimal value_boolean value_date",
                      [spec.name for spec in specs])
    history = children(SPEC_IDENTIFIER, SPECIFICATION, "historical_identifiers", "source value search_text",
                       [spec.name for spec in specs])
    result = {}
    for spec in specs:
        path, current = {}, spec.category
        while current:
            if current in path or current not in categories:
                frappe.throw("PRODUCT_CATALOG_INCONSISTENT")
            category = categories[current]
            path[current] = label(category)
            current = category.parent_category
        attributes = {}
        for row in values[spec.name]:
            definition = definitions.get(row.attribute)
            if not definition or definition.data_type != row.data_type:
                frappe.throw("PRODUCT_CATALOG_INCONSISTENT")
            value = service._row_value(row)
            if row.data_type in SELECT_TYPES:
                option = options.get(value.id)
                if not option or (option.attribute, option.option_key) != (row.attribute, value.key):
                    frappe.throw("PRODUCT_CATALOG_INCONSISTENT")
                value = dict(id=option.name, key=option.option_key, display_name=option.display_name)
            elif isinstance(value, Decimal):
                value = format(value, "f")
            elif isinstance(value, date):
                value = value.isoformat()
            attribute = attributes.setdefault(row.attribute, dict(
                label(definition), type=row.data_type, unit=definition.unit or None, values=[],
                searchable=templates.get((spec.category, row.attribute), False),
            ))
            attribute["values"].append(value)
        result[spec.name] = dict(label(spec), description=spec.description or "",
                                 category_path=list(reversed(path.values())), attributes=list(attributes.values()),
                                 historical_identifiers=[dict(source=row.source, value=row.value)
                                                         for row in history[spec.name]],
                                 historical_search=[row.search_text for row in history[spec.name]])
    return result


def searchable_text(spec):
    from .sku import _search_text

    fields = [spec["code"], spec["display_name"], spec["description"], *spec["historical_search"]]
    for category in spec["category_path"]:
        fields.extend((category["code"], category["display_name"]))
    for attribute in spec["attributes"]:
        if attribute["searchable"]:
            for value in attribute["values"]:
                if isinstance(value, dict):
                    fields.extend((value["key"], value["display_name"]))
                else:
                    fields.append(str(value))
    return _search_text("\n".join(fields))


def by_ids(ids, *, details=False, include_specification=False):
    if not ids:
        return {}
    include_specification = include_specification or details
    fields = "name code display_name brand stock_uom quantity_precision"
    if include_specification:
        fields += " specification"
    if details:
        fields += " description"
    skus = rows(SKU, fields, {"name": ("in", tuple(ids))})
    specs = specification_rows({"name": ("in", tuple({row.specification for row in skus}))}) if include_specification else {}
    brands = {row.name: label(row) for row in rows(BRAND, "name code display_name")}
    identifiers = children("HP V1 Product Identifier", SKU, "identifiers", "kind value source notes", ids) if details else {}
    aliases = children("HP V1 Product Alias", SKU, "aliases", "value language source searchable", ids) if details else {}
    result = {}
    for row in skus:
        result[row.name] = dict(
            label(row), brand=brands[row.brand], stock_uom=row.stock_uom, quantity_precision=row.quantity_precision,
        )
        if include_specification:
            result[row.name]["specification"] = public_spec(specs[row.specification])
        if details:
            result[row.name].update(
                description=row.description or "",
                identifiers=[{k: v for k, v in item.items() if k != "parent"} for item in identifiers[row.name]],
                aliases=[{k: v for k, v in item.items() if k not in ("parent", "searchable")} for item in aliases[row.name]],
                images=[], attachments=[],
            )
    return result


def public_spec(spec):
    return dict(
        {k: v for k, v in spec.items() if k != "historical_search"},
        attributes=[{k: v for k, v in attribute.items() if k != "searchable"} for attribute in spec["attributes"]],
    )


def get_sku(sku_id):
    policy.require_operation(policy.Operation.PRODUCT_READ)
    sku_id = policy._record_id(sku_id)
    if not frappe.db.exists(SKU, {"name": sku_id, "status": ACTIVE}):
        policy.deny()
    return by_ids((sku_id,), details=True)[sku_id]


def get_specification(specification_id):
    policy.require_operation(policy.Operation.PRODUCT_READ)
    spec = specification_rows({"name": policy._record_id(specification_id), "status": ACTIVE})
    if not spec:
        policy.deny()
    return public_spec(next(iter(spec.values())))


def attribute_filters(values):
    if not isinstance(values, dict) or len(values) > 10:
        frappe.throw("PRODUCT_INVALID_ATTRIBUTE_FILTER")
    if not values:
        return {}
    for key in values:
        validation.identifier(key, "Attribute")
    types = {row.name: row.data_type for row in rows(ATTRIBUTE, "name data_type", {"name": ("in", tuple(values))})}
    if values.keys() != types.keys():
        frappe.throw("PRODUCT_INVALID_ATTRIBUTE_FILTER")
    normalized = {}
    for key, value in values.items():
        kind = types[key]
        if kind in SELECT_TYPES:
            value = validation.identifier(value, "Option")
        elif kind == DECIMAL:
            value = validation.exact_decimal(value, "Attribute")
        elif kind == DATE:
            value = validation.calendar_date(value, "Attribute").isoformat()
        elif kind == BOOLEAN:
            value = validation.boolean(value, "Attribute")
        elif kind == TEXT:
            value = validation.text(value, "Attribute", 4000, required=True)
        normalized[key] = value
    return normalized


def matches(spec, category, filters):
    if category and category not in {row["id"] for row in spec["category_path"]}:
        return False
    attributes = {row["id"]: row for row in spec["attributes"]}
    for key, value in filters.items():
        if key not in attributes:
            return False
        row = attributes[key]
        stored = row["values"]
        if row["type"] == DECIMAL:
            stored = [Decimal(item) for item in stored]
        elif row["type"] in SELECT_TYPES:
            stored = [item["id"] for item in stored]
        if value not in stored:
            return False
    return True


def filter_options():
    policy.require_operation(policy.Operation.PRODUCT_READ)
    attributes = rows(ATTRIBUTE, "name code display_name unit data_type", {"status": ACTIVE})
    options = defaultdict(list)
    for row in rows(OPTION, "name option_key display_name attribute status", order_by="sort_order asc, name asc"):
        options[row.attribute].append(dict(id=row.name, key=row.option_key, display_name=row.display_name,
                                           active=row.status == ACTIVE))
    return dict(
        categories=[dict(label(row), parent_id=row.parent_category or None)
                    for row in labels(CATEGORY, active=True).values()],
        brands=[label(row) for row in labels(BRAND, active=True).values()],
        attributes=[dict(label(row), type=row.data_type, unit=row.unit or None, options=options[row.name])
                    for row in attributes],
    )
