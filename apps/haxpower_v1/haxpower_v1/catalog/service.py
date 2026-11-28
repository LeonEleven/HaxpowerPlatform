"""Trusted, transactional product catalog operations. No methods are whitelisted."""

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, fields, replace
from datetime import date
from decimal import Decimal
import json
from uuid import uuid4

import frappe
from frappe.utils import get_datetime, now_datetime

from . import numbers, validation as check
from .dto import (
    ACTIVE, BOOLEAN, DATA_TYPES, DATE, DECIMAL, MULTI, MULTI_SELECT, SELECT_TYPES, SINGLE, TEXT,
    AttributeData, AttributeValues, AuditEntry, CatalogRecord, CategoryData, InputValue, OptionData,
    HistoricalIdentifier, OptionSelection, Specification, SpecificationData, TemplateData,
)
from .persistence import (
    ATTRIBUTE, AUDIT, CATEGORY, OPTION, SPECIFICATION, TEMPLATE, VALUE,
    catalog_doc, catalog_rows, catalog_write, require_catalog_permission, require_maintainer, require_site_operator,
)


def _load(doctype, record_id):
    return catalog_doc(doctype, check.identifier(record_id, doctype))


def _save(doctype, record, changes):
    if record is None:
        return frappe.get_doc(doctype=doctype, **changes).insert(
            ignore_permissions=True, set_name=uuid4().hex,
        )
    record.update(changes)
    return record.save(ignore_permissions=True)


def _data(dto, record):
    values = {}
    for field in fields(dto):
        value = record.get(field.name)
        if field.type is bool:
            value = bool(value)
        elif field.name == "description":
            value = value or ""
        elif field.name in ("parent_category", "unit", "stock_uom"):
            value = value or None
        values[field.name] = value
    return dto(**values)


def _read(doctype, dto, record_id):
    require_catalog_permission(inputs=True)
    record = _load(doctype, record_id)
    if record.status != ACTIVE:
        require_catalog_permission(parameters=True)
    return CatalogRecord(record.name, _data(dto, record))


def _metadata(data):
    return {
        "code": check.key(data.code, "Code"),
        "display_name": check.text(data.display_name, "Display name", 140, required=True),
        "description": check.text(data.description, "Description", 4000),
        "status": check.status(data.status),
    }


def save_category(data: CategoryData, *, record_id: str | None = None) -> CatalogRecord[CategoryData]:
    with catalog_write(parameters=True):
        record = _load(CATEGORY, record_id) if record_id is not None else None
        changes = _metadata(data)
        changes["sort_order"] = check.order(data.sort_order, "Sort order")
        ancestor_id = data.parent_category
        visited = {record.name} if record else set()
        while ancestor_id is not None:
            ancestor = _load(CATEGORY, ancestor_id)
            if ancestor.name in visited:
                frappe.throw("A product category cannot be its own ancestor.")
            visited.add(ancestor.name)
            ancestor_id = ancestor.parent_category or None
        changes["parent_category"] = data.parent_category
        record = _save(CATEGORY, record, changes)
        return CatalogRecord(record.name, _data(CategoryData, record))


def list_categories() -> tuple[CatalogRecord[CategoryData], ...]:
    require_catalog_permission(inputs=True)
    rows = catalog_rows(CATEGORY, {"status": ACTIVE}, ["name", *(f.name for f in fields(CategoryData))],
                       order_by="sort_order asc, code asc")
    return tuple(CatalogRecord(row.name, _data(CategoryData, row)) for row in rows)


def get_category(record_id: str) -> CatalogRecord[CategoryData]:
    return _read(CATEGORY, CategoryData, record_id)


def save_attribute(data: AttributeData, *, record_id: str | None = None) -> CatalogRecord[AttributeData]:
    with catalog_write(parameters=True):
        record = _load(ATTRIBUTE, record_id) if record_id is not None else None
        changes = _metadata(data)
        data_type = check.choice(data.data_type, DATA_TYPES, "attribute data type")
        unit = None if data.unit is None else check.text(data.unit, "Unit", 140) or None
        if unit and data_type != DECIMAL:
            frappe.throw("Only a decimal attribute can have a unit.")
        if record and (record.data_type, record.unit or None) != (data_type, unit):
            for doctype in (TEMPLATE, OPTION, VALUE, AUDIT):
                if frappe.db.get_value(doctype, {"attribute": record.name}, for_update=True):
                    frappe.throw("Cannot change the data type or unit of an attribute in use.")
        record = _save(ATTRIBUTE, record, dict(changes, data_type=data_type, unit=unit))
        return CatalogRecord(record.name, _data(AttributeData, record))


def get_attribute(record_id: str) -> CatalogRecord[AttributeData]:
    return _read(ATTRIBUTE, AttributeData, record_id)


def save_option(data: OptionData, *, record_id: str | None = None) -> CatalogRecord[OptionData]:
    with catalog_write(parameters=True):
        record = _load(OPTION, record_id) if record_id is not None else None
        attribute = _load(ATTRIBUTE, data.attribute)
        if attribute.data_type not in SELECT_TYPES:
            frappe.throw("Only select attributes can have options.")
        option_key = check.key(data.option_key, "Option key")
        if record and (record.attribute, record.option_key) != (attribute.name, option_key):
            frappe.throw("An option's attribute and key are immutable. Create a new option and deactivate the old one.")
        if not record and frappe.db.get_value(
            OPTION, {"attribute": attribute.name, "option_key": option_key}, for_update=True,
        ):
            frappe.throw("This option key already exists for the attribute.")
        changes = {
            "attribute": attribute.name,
            "option_key": option_key,
            "display_name": check.text(data.display_name, "Display name", 140, required=True),
            "status": check.status(data.status),
            "sort_order": check.order(data.sort_order, "Sort order"),
        }
        record = _save(OPTION, record, changes)
        return CatalogRecord(record.name, _data(OptionData, record))


def get_option(record_id: str) -> CatalogRecord[OptionData]:
    return _read(OPTION, OptionData, record_id)


def list_options(attribute_id: str, *, include_inactive: bool = False) -> tuple[CatalogRecord[OptionData], ...]:
    require_catalog_permission(inputs=True)
    attribute = _load(ATTRIBUTE, attribute_id)
    if include_inactive or attribute.status != ACTIVE:
        require_catalog_permission(parameters=True)
    filters = {"attribute": attribute.name}
    if not include_inactive:
        filters["status"] = ACTIVE
    rows = catalog_rows(
        OPTION, filters, ["name", *(f.name for f in fields(OptionData))],
        order_by="sort_order asc, option_key asc",
    )
    return tuple(CatalogRecord(row.name, _data(OptionData, row)) for row in rows)


def _has_multiple_values(category_id, attribute_id):
    # A template mode change may not invalidate the cardinality of existing data.
    spec_ids = [row.name for row in catalog_rows(SPECIFICATION, {"category": category_id}, ["name"])]
    if not spec_ids:
        return False
    rows = catalog_rows(
        VALUE,
        {"attribute": attribute_id, "parent": ("in", spec_ids),
         "parenttype": SPECIFICATION, "parentfield": "attribute_values"},
        ["parent"],
    )
    return any(count > 1 for count in Counter(row.parent for row in rows).values())


def save_template(data: TemplateData, *, record_id: str | None = None) -> CatalogRecord[TemplateData]:
    with catalog_write(parameters=True):
        record = _load(TEMPLATE, record_id) if record_id is not None else None
        category = _load(CATEGORY, data.category)
        attribute = _load(ATTRIBUTE, data.attribute)
        if record and (record.category, record.attribute) != (category.name, attribute.name):
            frappe.throw("A category attribute's category and attribute are immutable.")
        if not record and frappe.db.get_value(
            TEMPLATE, {"category": category.name, "attribute": attribute.name}, for_update=True,
        ):
            frappe.throw("This attribute already belongs to the category template.")
        mode = check.choice(data.value_mode, (SINGLE, MULTI), "value mode")
        if mode == MULTI and attribute.data_type != MULTI_SELECT:
            frappe.throw("Only a multi-select attribute can allow multiple values.")
        if record and record.value_mode == MULTI and mode == SINGLE and _has_multiple_values(category.name, attribute.name):
            frappe.throw("Cannot restrict this template to a single value while specifications use multiple values.")
        changes = asdict(data)
        changes["display_order"] = check.order(data.display_order, "Display order")
        for field in fields(TemplateData):
            if field.type is bool:
                changes[field.name] = check.boolean(changes[field.name], field.name)
        record = _save(TEMPLATE, record, changes)
        return CatalogRecord(record.name, _data(TemplateData, record))


def get_category_template(category_id: str) -> tuple[CatalogRecord[TemplateData], ...]:
    require_catalog_permission(inputs=True)
    category = _load(CATEGORY, category_id)
    return tuple(CatalogRecord(row.name, _data(TemplateData, row)) for row in _template_rows(category.name))


def _template_rows(category_id):
    return catalog_rows(
        TEMPLATE, {"category": category_id}, ["name", *(f.name for f in fields(TemplateData))],
        order_by="display_order asc, attribute asc",
    )


def _row_value(row):
    if row.data_type in SELECT_TYPES:
        return OptionSelection(row.option_id, row.option_key)
    if row.data_type == TEXT:
        return row.value_text
    if row.data_type == DECIMAL:
        return Decimal(row.value_decimal)
    if row.data_type == BOOLEAN:
        if row.value_boolean in ("TRUE", "FALSE"):
            return row.value_boolean == "TRUE"
        frappe.throw("Invalid stored boolean attribute value.")
    if row.data_type == DATE:
        return check.calendar_date(row.value_date, "Stored date")
    frappe.throw("Unsupported stored attribute type.")


def _saved_values(record):
    values = defaultdict(list)
    for row in record.attribute_values:
        values[row.attribute, row.data_type].append(_row_value(row))
    return {
        attribute: AttributeValues(attribute, data_type, tuple(attribute_values))
        for (attribute, data_type), attribute_values in sorted(values.items())
    }


def _specification(record):
    return Specification(
        record.name, record.revision, _data(SpecificationData, record), tuple(_saved_values(record).values()),
        tuple(HistoricalIdentifier(source=row.source, value=row.value) for row in record.historical_identifiers),
    )


def get_specification(record_id: str) -> Specification:
    require_maintainer()
    return _specification(_load(SPECIFICATION, record_id))


def _option_values(raw_values, definition, previous):
    option_ids = [check.identifier(raw, "Option") for raw in raw_values]
    options = {
        option.name: option for option in catalog_rows(
            OPTION, {"name": ("in", option_ids)}, ["name", "attribute", "option_key", "status"],
        )
    }
    values = []
    for option_id in option_ids:
        option = options.get(option_id)
        if not option or option.attribute != definition.name:
            frappe.throw(f"Option does not belong to attribute {definition.code}.")
        value = OptionSelection(option.name, option.option_key)
        if option.status != ACTIVE and value not in previous:
            frappe.throw(f"Option {option.option_key} is inactive.")
        values.append(value)
    return values


def _normalize_values(category_id, inputs, previous):
    if not isinstance(inputs, Mapping):
        frappe.throw("Specification attributes must map attribute identifiers to value sequences.")
    template = {row.attribute: row for row in _template_rows(category_id)}
    for attribute_id in inputs:
        check.identifier(attribute_id, "Attribute")
        if attribute_id not in template:
            frappe.throw(f"Attribute {attribute_id} is not assigned to the category template.")

    if not template:
        return {}
    definitions = catalog_rows(
        ATTRIBUTE, {"name": ("in", list(template))}, ["name", "code", "data_type", "status"],
    )
    result = {}
    for definition in sorted(definitions, key=lambda row: row.name):
        rule = template[definition.name]
        raw_values = inputs.get(definition.name, ())
        if not isinstance(raw_values, Sequence) or isinstance(raw_values, (str, bytes, bytearray)):
            frappe.throw(f"Values for attribute {definition.code} must be a sequence.")
        if not raw_values:
            if rule.is_required and definition.status == ACTIVE:
                frappe.throw(f"Attribute {definition.code} is required.")
            continue
        if len(raw_values) > 1 and rule.value_mode != MULTI:
            frappe.throw(f"Attribute {definition.code} accepts at most one value.")
        old_values = previous[definition.name].values if definition.name in previous else ()
        if definition.data_type in SELECT_TYPES:
            values = _option_values(raw_values, definition, old_values)
        else:
            values = [check.scalar(raw, definition.data_type, definition.code) for raw in raw_values]
        unique = set(values)
        if len(unique) != len(values):
            frappe.throw(f"Attribute {definition.code} contains duplicate values.")
        if definition.status != ACTIVE and not unique.issubset(old_values):
            frappe.throw(f"Attribute {definition.code} is inactive.")
        result[definition.name] = AttributeValues(definition.name, definition.data_type, tuple(sorted(values)))
    return result


def _value_fields(attribute, value):
    row = {"attribute": attribute.attribute_id, "data_type": attribute.data_type}
    if attribute.data_type in SELECT_TYPES:
        row.update(option_id=value.id, option_key=value.key)
    elif attribute.data_type == TEXT:
        row["value_text"] = value
    elif attribute.data_type == DECIMAL:
        row["value_decimal"] = format(value, "f")
    elif attribute.data_type == BOOLEAN:
        row["value_boolean"] = "TRUE" if value else "FALSE"
    elif attribute.data_type == DATE:
        row["value_date"] = value
    return row


def _set_values(record, attributes):
    existing_rows = {(row.attribute, _row_value(row)): row for row in record.attribute_values}
    record.set("attribute_values", [])
    for attribute in attributes.values():
        for value in attribute.values:
            row = existing_rows.get((attribute.attribute_id, value))
            record.append("attribute_values", row if row is not None else _value_fields(attribute, value))
            record.attribute_values[-1].idx = len(record.attribute_values)


def _set_history(record, identifiers):
    from .sku import _search_text

    if not isinstance(identifiers, Sequence) or len(identifiers) > 100:
        frappe.throw("Provide up to 100 typed specification historical identifiers.")
    record.set("historical_identifiers", [])
    for row in identifiers:
        if not isinstance(row, HistoricalIdentifier):
            frappe.throw("Expected a typed historical identifier.")
        source = check.text(row.source, "Source system", 140, required=True)
        check.text(row.value, "Historical identifier", 140, required=True)
        record.append("historical_identifiers", dict(
            source=source, value=row.value, search_text=check.text(_search_text(row.value), "Search text", 140),
        ))


def create_specification(
    data: SpecificationData, *, attributes: Mapping[str, Sequence[InputValue]],
    historical_identifiers: Sequence[HistoricalIdentifier] = (),
) -> Specification:
    return _create_specification(data, attributes, historical_identifiers, fixture=False)


def create_fixture_specification(data: SpecificationData, *, attributes: Mapping[str, Sequence[InputValue]]):
    from .fixtures import require_fixture_catalog

    require_fixture_catalog()
    return _create_specification(data, attributes, (), fixture=True)


def _create_specification(data, attributes, historical_identifiers, *, fixture):
    from ..adapters.erpnext import item

    with catalog_write():
        category = _load(CATEGORY, data.category)
        if category.status != ACTIVE:
            frappe.throw("New specifications require an active product category.")
        values = _normalize_values(category.name, attributes, {})
        if fixture:
            numbers.assert_fixture_code(data.code)
            precision = item.configured_precision(data.stock_uom) if data.stock_uom else 0
        else:
            if data.code is not None:
                frappe.throw("Specification code is assigned by the service.")
            precision = item.configured_precision(data.stock_uom)
            data = replace(data, code=numbers.allocate())
        record = frappe.get_doc(
            doctype=SPECIFICATION, category=category.name, revision=1, **_metadata(data),
            stock_uom=data.stock_uom, quantity_precision=precision,
        )
        _set_values(record, values)
        _set_history(record, historical_identifiers)
        record.insert(ignore_permissions=True, set_name=uuid4().hex)
        if not fixture:
            numbers.reserve_through(record.name, 0)
        return _specification(record)


def configure_pending_specification_unit(record_id: str, stock_uom: str, *, expected_revision: int) -> Specification:
    from ..adapters.erpnext import item
    from .sku_persistence import SKU

    require_site_operator()
    with catalog_write():
        record = _load(SPECIFICATION, record_id)
        if type(expected_revision) is not int or record.revision != expected_revision:
            frappe.throw("The specification revision has changed. Reload it before editing.")
        if record.stock_uom or frappe.db.get_value(SKU, {"specification": record.name}, for_update=True):
            frappe.throw("Only a pending historical specification without SKUs can be configured.")
        record.stock_uom = stock_uom
        record.quantity_precision = item.configured_precision(stock_uom)
        record.revision += 1
        record.save(ignore_permissions=True)
        return _specification(record)


def edit_specification(
    record_id: str, data: SpecificationData, *, expected_revision: int,
    attributes: Mapping[str, Sequence[InputValue]] | None = None,
    historical_identifiers: Sequence[HistoricalIdentifier] | None = None,
) -> Specification:
    with catalog_write():
        record = _load(SPECIFICATION, record_id)
        if type(expected_revision) is not int or record.revision != expected_revision:
            frappe.throw("The specification revision has changed. Reload it before editing.")
        if data.category != record.category:
            frappe.throw("The category of an existing specification is immutable.")
        if data.code != record.code or (data.stock_uom or None) != (record.stock_uom or None):
            frappe.throw("Specification code and stock UOM are immutable.")
        changes = _metadata(data)
        previous = _saved_values(record)
        current = previous if attributes is None else _normalize_values(record.category, attributes, previous)
        original = _data(SpecificationData, record)
        unchanged = all(getattr(original, key) == value for key, value in changes.items())
        if historical_identifiers is None and previous == current and unchanged:
            return _specification(record)

        record.update(changes)
        record.revision += 1
        if previous != current:
            _set_values(record, current)
        if historical_identifiers is not None:
            previous_history = tuple(HistoricalIdentifier(source=row.source, value=row.value)
                                     for row in record.historical_identifiers)
            _set_history(record, historical_identifiers)
            retained = {HistoricalIdentifier(source=row.source, value=row.value)
                        for row in record.historical_identifiers}
            if not retained.issuperset(previous_history):
                frappe.throw("Existing historical specification identifiers must be retained.")
        record.save(ignore_permissions=True)
        if previous != current:
            _append_audit(record, previous, current)
        return _specification(record)


def _serialize(attribute):
    elements = []
    for value in attribute.values:
        if isinstance(value, OptionSelection):
            value = asdict(value)
        elif isinstance(value, Decimal):
            value = format(value, "f")
        elif isinstance(value, date):
            value = value.isoformat()
        elements.append(value)
    return json.dumps({"data_type": attribute.data_type, "values": elements}, ensure_ascii=False, sort_keys=True)


def _deserialize(attribute_id, raw):
    payload = json.loads(raw)
    data_type = payload["data_type"]
    values = payload["values"]
    if data_type in SELECT_TYPES:
        values = [OptionSelection(**value) for value in values]
    elif data_type == DECIMAL:
        values = [Decimal(value) for value in values]
    elif data_type == DATE:
        values = [date.fromisoformat(value) for value in values]
    return AttributeValues(attribute_id, data_type, tuple(values))


def _append_audit(record, previous, current):
    operation_id = uuid4().hex
    actor = frappe.session.user
    occurred_at = now_datetime()
    for attribute_id in sorted(previous.keys() | current.keys()):
        before, after = previous.get(attribute_id), current.get(attribute_id)
        if before == after:
            continue
        empty = AttributeValues(attribute_id, (before or after).data_type, ())
        frappe.get_doc(
            doctype=AUDIT, specification=record.name, attribute=attribute_id,
            specification_revision=record.revision, operation_id=operation_id,
            actor=actor, occurred_at=occurred_at,
            before_values=_serialize(before or empty), after_values=_serialize(after or empty),
        ).insert(ignore_permissions=True, set_name=uuid4().hex)


def get_specification_audit(record_id: str) -> tuple[AuditEntry, ...]:
    require_maintainer()
    record = _load(SPECIFICATION, record_id)
    rows = catalog_rows(
        AUDIT, {"specification": record.name},
        ["name", "specification_revision", "operation_id", "actor", "occurred_at",
         "attribute", "before_values", "after_values"],
        order_by="specification_revision asc, attribute asc",
    )
    return tuple(
        AuditEntry(
            row.name, record.name, row.specification_revision, row.operation_id, row.actor,
            get_datetime(row.occurred_at), _deserialize(row.attribute, row.before_values),
            _deserialize(row.attribute, row.after_values),
        )
        for row in rows
    )
