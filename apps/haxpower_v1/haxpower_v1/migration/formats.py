"""Versioned spreadsheet adapters. Parsing has no access to the catalog database."""

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from hashlib import sha256
import json
from pathlib import Path
import re

from . import xlsx

STANDARD = "haxpower-master-1"
LEGACY = "legacy-material-1"
SOURCE_SYSTEM = {STANDARD: "haxpower-master", LEGACY: "legacy-material"}
CONFIG = json.loads((Path(__file__).parent.parent / "catalog/initial_configuration.json").read_text())
CATEGORIES = {item["code"]: item for item in CONFIG["categories"]}
ATTRIBUTES = {item["display_name"]: item for item in CONFIG["attributes"]}
LEGACY_FIELDS = {
    "t_material_spec_code_config": "id company_id config_code config_name remark parent_id status version created_user create_date modify_date",
    "t_material_spec": "id company_id material_spec_code material_spec_name material_spec_unit remark status version created_user create_date modify_date",
    "t_material_spec_attribute": "id company_id prop_name prop_value prop_type status created_user version create_date modify_date material_spec_id",
    "t_material": "id company_id material_code material_spec_id factory_brand currency discount_rate remark status created_user create_date modify_date ref_price price_update_date price_update_user max_inventory min_inventory",
}
FIELDS = {
    "Categories": "code display_name parent_category description status sort_order",
    "Attributes": "code display_name data_type unit description status",
    "Options": "attribute option_key display_name status sort_order",
    "Templates": "category attribute is_required value_mode searchable identity_relevant display_order editable_for_technician",
    "Specifications": "category display_name stock_uom description status",
    "Brands": "code display_name description status",
    "SKUs": "specification brand display_name stock_uom quantity_precision description",
}
REFERENCE = {
    "parent_category": "Categories", "category": "Categories", "attribute": "Attributes",
    "specification": "Specifications", "brand": "Brands",
}
BOOLEANS = {"is_required", "searchable", "identity_relevant", "editable_for_technician"}
INTEGERS = {"sort_order", "display_order", "quantity_precision"}
CHILDREN = {
    "Specification Values": "key specification attribute value option",
    "Historical Identifiers": "key target_type target source value",
}
HEADERS = {
    "Instructions": ["version", "usage"],
    **{sheet: ["key", "action", "target_id", "target_code", *fields.split()] for sheet, fields in FIELDS.items()},
    **{sheet: fields.split() for sheet, fields in CHILDREN.items()},
}
ORDER = ("Categories", "Attributes", "Options", "Templates", "Brands", "Specifications", "SKUs")
SOURCE_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}")
DERIVED_FIELDS = {"values", "history"}
LEGACY_SPEC, LEGACY_ATTR, LEGACY_SKU, LEGACY_CONFIG = (
    "t_material_spec", "t_material_spec_attribute", "t_material", "t_material_spec_code_config",
)


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


@dataclass
class Candidate:
    kind: str
    namespace: str
    source_id: str
    action: str
    fields: dict
    rows: list[str]
    target_id: str | None = None
    target_code: str | None = None
    legacy_code: str | None = None
    issues: list[str] = field(default_factory=list)
    raw_digest: str | None = None

    @property
    def key(self):
        return digest([self.namespace, self.kind, self.source_id])


def template(stream):
    sheets = {sheet: [fields] for sheet, fields in HEADERS.items()}
    sheets["Instructions"].append([
        STANDARD,
        "Use text for source keys and identifiers. Actions are create, reference or update. "
        "Link rows by key. Use id: or code: to reference existing catalog records. "
        "Consult the V1-16B master data migration contract for column types and review.",
    ])
    xlsx.write(stream, sheets)


def _identity(cell, *, legacy=False):
    if cell.kind == "text" and cell.value and len(cell.value) <= 140 and cell.value.strip() == cell.value:
        return cell.value
    if legacy and cell.kind == "number" and re.fullmatch(r"(?:0|[1-9][0-9]{0,14})", cell.value):
        return cell.value
    return None


def _text(row, field):
    cell = row.cells[field]
    return cell.value if cell.kind == "text" else None


def _values(row, field, *, typed=False):
    cell = row.cells[field]
    if cell.kind == "blank":
        return None
    if typed and cell.kind == "boolean":
        return cell.value == "1"
    if cell.kind in ({"text", "number", "date"} if typed else {"text"}):
        return cell.value
    raise xlsx.MigrationError("INVALID_CELL_TYPE")


def _parse_field(row, name):
    value = _values(row, name, typed=name in BOOLEANS | INTEGERS)
    if value is None:
        return None
    if name in INTEGERS:
        if isinstance(value, str) and re.fullmatch(r"[0-9]{1,10}", value):
            return int(value)
        raise xlsx.MigrationError("INVALID_CELL_TYPE")
    if name in BOOLEANS:
        if type(value) is bool:
            return value
        if value in ("TRUE", "FALSE"):
            return value == "TRUE"
        raise xlsx.MigrationError("INVALID_CELL_TYPE")
    return value


def parse(source, namespace, files):
    if not isinstance(namespace, str) or not SOURCE_KEY.fullmatch(namespace):
        raise xlsx.MigrationError("INVALID_DATASET")
    if source not in (STANDARD, LEGACY):
        raise xlsx.MigrationError("SOURCE_VERSION")
    if not 1 <= len(files) <= xlsx.MAX_FILES or (source == STANDARD and len(files) != 1):
        raise xlsx.MigrationError("WORKBOOK_COUNT")
    schema = HEADERS if source == STANDARD else {sheet: cols.split() for sheet, cols in LEGACY_FIELDS.items()}
    sheets = {}
    for identity, name, payload in files:
        for sheet, data in xlsx.read(name, payload).items():
            if sheet in sheets or sheet not in schema:
                raise xlsx.MigrationError("WORKBOOK_SHEETS")
            sheets[sheet] = list(xlsx.rows(identity, sheet, data, schema[sheet]))
    if source == STANDARD:
        instructions = sheets.pop("Instructions", ())
        if len(instructions) != 1 or _text(instructions[0], "version") != STANDARD:
            raise xlsx.MigrationError("TEMPLATE_VERSION")
        candidates = _standard(namespace, sheets)
    else:
        if set(sheets) != set(schema):
            raise xlsx.MigrationError("WORKBOOK_SHEETS")
        candidates = _legacy(namespace, sheets)
    rows = [row for entries in sheets.values() for row in entries]
    if (len(rows) > 6500 or not candidates or
            sum(len(cell.value or "") for row in rows for cell in row.cells.values()) > 8_000_000):
        raise xlsx.MigrationError("MIGRATION_ROW_LIMIT")
    raw = {row.key: row for row in rows}
    for candidate in candidates:
        _review_text(candidate)
        if candidate.kind not in ("Options", "Brands", "Evidence") or source == STANDARD:
            candidate.raw_digest = digest(sorted(
                [digest({key: [cell.kind, cell.value] for key, cell in raw[row].cells.items()})
                 for row in candidate.rows]
            ))
    return rows, candidates


def _review_text(candidate):
    for name, limit in (("display_name", 140), ("description", 4000)):
        raw = candidate.fields.get(name)
        if not isinstance(raw, str):
            continue
        if len(raw) > limit or "\0" in raw:
            candidate.issues.append("TEXT_LIMIT")
        elif raw.strip() != raw or re.search(r"<[^>]*>", raw):
            candidate.issues.append("TEXT_TRANSFORMATION")


def _invalid(row, namespace, issue):
    return Candidate("Evidence", namespace, row.key, "evidence", {}, [row.key], issues=[issue])


def _standard(namespace, sheets):
    indexed = {}
    candidates = []
    for sheet in ORDER:
        records = sheets.get(sheet, ())
        counts = Counter(_text(row, "key") for row in records)
        for row in records:
            key = _text(row, "key")
            if not key or not SOURCE_KEY.fullmatch(key) or counts[key] != 1:
                candidates.append(_invalid(row, namespace, "DUPLICATE_OR_INVALID_KEY"))
                continue
            action = _text(row, "action")
            item = Candidate(sheet, namespace, key, action, {}, [row.key],
                             target_id=_text(row, "target_id"), target_code=_text(row, "target_code"))
            indexed[sheet, key] = item
            candidates.append(item)
            if action not in ("create", "reference", "update"):
                item.issues.append("INVALID_ACTION")
            for name in FIELDS[sheet].split():
                try:
                    if (value := _parse_field(row, name)) is not None:
                        item.fields[name] = value
                except xlsx.MigrationError as error:
                    item.issues.append(str(error))
            if action == "reference" and item.fields:
                item.issues.append("REFERENCE_HAS_CHANGES")
            if any(row.cells[key].kind not in ("text", "blank") for key in ("target_id", "target_code")):
                item.issues.append("INVALID_TARGET")
    for sheet in CHILDREN:
        rows = sheets.get(sheet, ())
        counts = Counter(_text(row, "key") for row in rows)
        for row in rows:
            key = _text(row, "key")
            if not key or not SOURCE_KEY.fullmatch(key) or counts[key] != 1:
                candidates.append(_invalid(row, namespace, "DUPLICATE_OR_INVALID_KEY"))
                continue
            if sheet == "Specification Values":
                kind, target = "Specifications", _text(row, "specification")
            else:
                kind, target = _text(row, "target_type"), _text(row, "target")
            if kind not in ("Specifications", "SKUs") or (item := indexed.get((kind, target))) is None:
                candidates.append(_invalid(row, namespace, "MISSING_PRODUCT_REFERENCE"))
                continue
            item.rows.append(row.key)
            if item.action == "reference":
                item.issues.append("REFERENCE_HAS_CHANGES")
            try:
                if sheet == "Specification Values":
                    _value(item, _text(row, "attribute"), _values(row, "value", typed=True),
                           _values(row, "option"), row)
                else:
                    source, value = _values(row, "source"), _values(row, "value")
                    if not source or not value:
                        raise xlsx.MigrationError("INVALID_HISTORICAL_IDENTIFIER")
                    item.fields.setdefault("history", []).append([source, value])
            except xlsx.MigrationError as error:
                item.issues.append(str(error))
    return candidates


def _value(item, attribute, value, option, row, *, value_kind=None):
    if not attribute or (value is None) == (option is None):
        raise xlsx.MigrationError("INVALID_ATTRIBUTE_VALUE")
    item.fields.setdefault("values", []).append({
        "attribute": attribute, "value": value, "option": option, "row": row.key,
        "type": value_kind or row.cells["value"].kind,
    })


def _legacy(namespace, sheets):
    output = []
    unique = {}
    for sheet, rows in sheets.items():
        identities = [(_identity(row.cells["company_id"], legacy=True),
                        _identity(row.cells["id"], legacy=True)) for row in rows]
        counts = Counter(identities)
        indexed = {}
        for row, (company, identity) in zip(rows, identities):
            if not company or not identity or counts[company, identity] != 1:
                output.append(_invalid(row, namespace, "DUPLICATE_OR_INVALID_LEGACY_ID"))
            else:
                indexed[company, identity] = row
        unique[sheet] = indexed
    # The caller provides a stable dataset id. Each company has a separate
    # identity within it. Cross-company references are never inferred.
    def source(company):
        return f"{namespace}/{company}"

    def config_reference(company, row):
        code = _text(row, "config_code")
        issues = []
        if code in CATEGORIES and _text(row, "config_name") not in (None, CATEGORIES[code]["display_name"]):
            issues.append("LEGACY_CATEGORY_CONFIG_CONFLICT")
        output.append(Candidate("Evidence", source(company), row.key, "evidence", {}, [row.key], issues=issues))
        return code if code in CATEGORIES and not issues else None

    approved = defaultdict(set)
    for (company, _), row in unique[LEGACY_CONFIG].items():
        if code := config_reference(company, row):
            approved[company].add(code)
    source_spec = {}
    for (company, identity), row in unique[LEGACY_SPEC].items():
        code = _text(row, "material_spec_code")
        fields = {
            "display_name": _text(row, "material_spec_name"),
            "stock_uom": _text(row, "material_spec_unit"),
            "description": _text(row, "remark"),
        }
        item = Candidate("Specifications", source(company), identity, "upsert", fields, [row.key],
                         legacy_code=code)
        tokens = set(re.split(r"[^A-Za-z0-9]+", code or "")) & CATEGORIES.keys()
        if len(tokens) == 1 and tokens <= approved[company]:
            fields["category"] = "code:" + next(iter(tokens))
        else:
            item.issues.append("UNVERIFIED_LEGACY_CATEGORY")
        if not code:
            item.issues.append("INVALID_LEGACY_CODE")
        if row.cells["remark"].kind not in ("text", "blank"):
            item.issues.append("INVALID_REMARK_TYPE")
        source_spec[company, identity] = item
        output.append(item)
    dictionary = {}
    for (company, _), row in unique[LEGACY_ATTR].items():
        item = source_spec.get((company, _identity(row.cells["material_spec_id"], legacy=True)))
        if not item:
            output.append(_invalid(row, source(company), "MISSING_SPECIFICATION"))
            continue
        item.rows.append(row.key)
        definition = ATTRIBUTES.get(_text(row, "prop_name"))
        if not definition:
            item.issues.append("UNMAPPED_LEGACY_ATTRIBUTE")
            continue
        code, kind = definition["code"], definition["data_type"]
        cell = row.cells["prop_value"]
        value = cell.value
        option = None
        if kind.endswith("_SELECT") and cell.kind == "text" and value:
            key = "O-" + digest([code, value])
            identity = "legacy:" + key
            entry = dictionary.setdefault((company, "Options", identity), Candidate(
                "Options", source(company), identity, "upsert",
                {"attribute": "code:" + code, "option_key": key, "display_name": value}, [],
            ))
            entry.rows.append(row.key)
            option, value = "key:" + identity, None
        elif kind == "BOOLEAN":
            translations = {"TRUE": True, "FALSE": False, "是": True, "否": False}
            value = (value == "1") if cell.kind == "boolean" else (
                translations.get(value) if cell.kind == "text" else None
            )
        elif kind != "DECIMAL" or cell.kind not in ("number", "text"):
            value = None
        try:
            _value(item, "code:" + code, value, option, row, value_kind=cell.kind)
        except xlsx.MigrationError:
            item.issues.append("INVALID_LEGACY_ATTRIBUTE_VALUE")
    source_sku = []
    for (company, identity), row in unique[LEGACY_SKU].items():
        spec_id = _identity(row.cells["material_spec_id"], legacy=True)
        spec = source_spec.get((company, spec_id))
        code = _text(row, "material_code")
        fields = {"specification": "key:" + spec_id if spec_id else None,
                  "display_name": spec.fields["display_name"] if spec else None,
                  "description": _text(row, "remark")}
        item = Candidate("SKUs", source(company), identity, "upsert", fields, [row.key], legacy_code=code)
        if not spec:
            item.issues.append("MISSING_SPECIFICATION")
        elif not code or not spec.legacy_code or not (
            code == spec.legacy_code or any(code.startswith(spec.legacy_code + mark) for mark in "-_./")
        ):
            item.issues.append("LEGACY_SKU_CODE_RELATION")
        if not code:
            item.issues.append("INVALID_LEGACY_CODE")
        if row.cells["remark"].kind not in ("text", "blank"):
            item.issues.append("INVALID_REMARK_TYPE")
        if brand := _text(row, "factory_brand"):
            key = "B-" + digest(brand)
            identity = "legacy:" + key
            entry = dictionary.setdefault((company, "Brands", identity), Candidate(
                "Brands", source(company), identity, "upsert", {"code": key, "display_name": brand}, [],
            ))
            entry.rows.append(row.key)
            fields["brand"] = "key:" + identity
        else:
            item.issues.append("EXPLICIT_BRAND_REQUIRED")
        source_sku.append(item)
    for entities in (list(source_spec.values()), source_sku):
        counts = Counter((row.namespace, row.legacy_code) for row in entities if row.legacy_code)
        for row in entities:
            if counts[row.namespace, row.legacy_code] > 1:
                row.issues.append("LEGACY_CODE_CONFLICT")
    return [*output, *dictionary.values(), *source_sku]
