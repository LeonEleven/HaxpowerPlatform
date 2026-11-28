"""Build immutable catalog plans and record the exact dependencies they reviewed."""

from collections import defaultdict
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime

import frappe

from ..catalog import validation
from ..catalog.dto import ACTIVE, BOOLEAN, DATE, MULTI, MULTI_SELECT, SELECT_TYPES, SINGLE, TEXT
from . import catalog, formats
from .catalog import Blocked

MAPPING = "HP V1 Migration Identity"
ACKNOWLEDGE = {
    "DUPLICATE_ATTRIBUTE_VALUE", "LEGACY_CATEGORY_CONFIG_CONFLICT", "LEGACY_CODE_CONFLICT",
    "LEGACY_SKU_CODE_RELATION", "MISSING_OPTIONAL_TECHNICAL_VALUES",
}
PARAMETERS = set(formats.ORDER) - {"Specifications", "SKUs"}


def mapping_key(source, entry):
    return formats.digest([formats.SOURCE_SYSTEM[source], entry["namespace"], entry["kind"], entry["source_id"]])


def _map(source, entry):
    row = frappe.db.get_value(MAPPING, mapping_key(source, entry),
                              ["target_id", "evidence", "source_system", "source_namespace", "entity_type",
                               "source_id", "source_digest"], as_dict=True)
    if row:
        from .store import unpack

        previous = unpack(row.evidence)
        if ((row.source_system, unpack(row.source_namespace), row.entity_type, unpack(row.source_id)) !=
                (formats.SOURCE_SYSTEM[source], entry["namespace"], entry["kind"], entry["source_id"])
                or formats.digest(previous["source"]) != row.source_digest):
            raise Blocked("SOURCE_MAPPING_INTEGRITY")
        return row.target_id, previous
    return None, {}


def _source_content(entry):
    vals = deepcopy(entry["fields"])
    if "values" in vals:
        values = [{k: v for k, v in row.items() if k != "row"} for row in vals["values"]]
        vals["values"] = sorted(values, key=formats.digest)
    if "history" in vals:
        vals["history"] = sorted(vals["history"])
    return {"fields": vals, "legacy_code": entry["legacy_code"], "operation": entry["action"],
            "raw_digest": entry["raw_digest"]}


def _decide(entry, decisions):
    choice = decisions.get(entry["key"], {})
    fields = choice.get("fields", {})
    entry["fields"].update(fields)
    entry.update({name: choice[name] for name in ("target_id", "target_code") if name in choice})
    if "action" in choice or choice.get("target_id") or choice.get("target_code"):
        entry["action"] = choice.get("action", "reference")
    if choice.get("drop_rows"):
        drops = set(choice["drop_rows"])
        if not drops <= set(entry["rows"]):
            raise Blocked("UNRELATED_ROW_DECISION")
        entry["fields"]["values"] = [
            value for value in entry["fields"].get("values", []) if value["row"] not in drops
        ]
    resolved = set()
    for issue in entry["issues"]:
        if issue in ACKNOWLEDGE:
            resolved.add(issue)
        elif issue == "UNVERIFIED_LEGACY_CATEGORY" and "category" in fields:
            resolved.add(issue)
        elif issue in {"MISSING_SPECIFICATION", "EXPLICIT_BRAND_REQUIRED"} and (
            ("specification" if issue == "MISSING_SPECIFICATION" else "brand") in fields
        ):
            resolved.add(issue)
        elif issue in {"TEXT_LIMIT", "TEXT_TRANSFORMATION"} and set(fields) & {"display_name", "description"}:
            resolved.add(issue)
        elif issue == "INVALID_REMARK_TYPE" and "description" in fields:
            resolved.add(issue)
        elif issue in {"UNMAPPED_LEGACY_ATTRIBUTE", "INVALID_LEGACY_ATTRIBUTE_VALUE"} and "values" in fields:
            resolved.add(issue)
    entry["issues"] = [issue for issue in entry["issues"] if not choice or issue not in resolved]
    return choice


def _upstream_values(values):
    grouped = defaultdict(list)
    for row in values:
        grouped[row["attribute"]].append({name: value for name, value in row.items() if name != "row"})
    return {key: sorted(rows, key=formats.digest) for key, rows in grouped.items()}


class Preview:
    def __init__(self, source, candidates, decisions):
        self.source = source
        self.decisions = decisions
        self.entries = {entry.key: dict(asdict(entry), key=entry.key) for entry in candidates}
        self.lookup = {(entry["namespace"], entry["kind"], entry["source_id"]): key
                       for key, entry in self.entries.items()}
        self.groups = {}
        self.visiting = set()
        self.claims = {}
        self.business_keys = {}

    def reference(self, group, kind, reference):
        if not isinstance(reference, str):
            raise Blocked("MISSING_REFERENCE")
        mode, sep, value = reference.partition(":")
        if not sep or mode == "key":
            value = value if sep else reference
            key = self.lookup.get((group["namespace"], kind, value))
            if not key:
                raise Blocked("MISSING_REFERENCE")
            dep = self.build(key)
            if dep["action"] in ("blocked", "skip"):
                raise Blocked("BLOCKED_DEPENDENCY")
            if key not in group["dependencies"]:
                group["dependencies"].append(key)
            target = dep["target"]
            return target or {"plan": key}, dep["projected"]
        if mode not in ("id", "code"):
            raise Blocked("INVALID_REFERENCE")
        target, state = catalog.lookup(kind, value if mode == "id" else None, value if mode == "code" else None)
        # If a group updates this same record, use its reviewed projection.
        key = self.claims.get((kind, target))
        if key:
            return self.reference(group, kind, "key:" + self.entries[key]["source_id"])
        group["guards"][target] = [kind, formats.digest(state)]
        return target, state

    def _rules(self, group, category):
        group["template_category"] = category
        if isinstance(category, str):
            base = catalog.template_rules(category)
            group["template_guard"] = catalog.template(category)
            rules = {row.attribute: asdict(row) for row in base}
            for row in base:
                if row.is_required:
                    _, definition = self.reference(group, "Attributes", "id:" + row.attribute)
                    rules[row.attribute]["is_required"] = definition["status"] == ACTIVE
        else:
            rules = {}
        for dep in tuple(self.groups.values()):
            if (dep["kind"] == "Templates" and dep["action"] not in ("blocked", "skip")
                    and dep["projected"].get("category") == category):
                value = dep["projected"]
                rules[formats.digest(value["attribute"]) if isinstance(value["attribute"], dict)
                      else value["attribute"]] = value
                group["dependencies"].append(dep["key"])
                group["template_groups"].append(dep["key"])
        return rules

    def _values(self, group, entries, category, previous):
        if len(entries) > 256:
            raise Blocked("ATTRIBUTE_ROW_LIMIT")
        rules = self._rules(group, category)
        supplied = defaultdict(list)
        duplicates = False
        for row in entries:
            attr, definition = self.reference(group, "Attributes", row["attribute"])
            key = formats.digest(attr) if isinstance(attr, dict) else attr
            if key not in rules:
                raise Blocked("ATTRIBUTE_OUTSIDE_CATEGORY_TEMPLATE")
            if definition["status"] != ACTIVE:
                raise Blocked("INACTIVE_ATTRIBUTE")
            kind = definition["data_type"]
            raw, option = row["value"], row["option"]
            if kind in SELECT_TYPES:
                if raw is not None:
                    raise Blocked("EXPLICIT_OPTION_REQUIRED")
                value, record = self.reference(group, "Options", option)
                if record["attribute"] != attr or record["status"] != ACTIVE:
                    raise Blocked("OPTION_ATTRIBUTE_CONFLICT")
            else:
                if option is not None or (kind == TEXT and row["type"] != "text") or (
                    kind == DATE and row["type"] not in ("date", "text")
                ):
                    raise Blocked("INVALID_TYPED_CELL")
                if kind == BOOLEAN and raw in ("TRUE", "FALSE"):
                    raw = raw == "TRUE"
                if kind == DATE and row["type"] == "date":
                    try:
                        parsed = datetime.fromisoformat(raw)
                        if parsed.time().isoformat() != "00:00:00" or parsed.tzinfo:
                            raise Blocked("INVALID_TYPED_CELL")
                        raw = parsed.date().isoformat()
                    except (TypeError, ValueError):
                        raise Blocked("INVALID_TYPED_CELL") from None
                try:
                    value = catalog._serialize(validation.scalar(raw, kind, "Migration attribute value"))
                except frappe.ValidationError:
                    raise Blocked("INVALID_ATTRIBUTE_SCALAR") from None
                if kind == TEXT and raw != value:
                    raise Blocked("TEXT_REQUIRES_EXPLICIT_CLEAN_VALUE")
            pair = [attr, value]
            if pair in supplied[key]:
                duplicates = True
            else:
                supplied[key].append(pair)
            if len(supplied[key]) > 1 and rules[key]["value_mode"] != MULTI:
                raise Blocked("SINGLE_VALUE_CONFLICT")
        if duplicates and not self.decisions.get(group["key"]):
            raise Blocked("DUPLICATE_ATTRIBUTE_VALUE")
        result = {key: [[key, val] for val in values] for key, values in previous.items()}
        result.update(supplied)
        if self.source == formats.LEGACY:
            group["missing_optional_attributes"] = sorted(
                key for key, rule in rules.items() if not rule["is_required"] and key not in result
            )
        if any(rule["is_required"] and key not in result for key, rule in rules.items()):
            raise Blocked("REQUIRED_ATTRIBUTE_MISSING")
        return [pair for key in sorted(result) for pair in sorted(result[key], key=lambda value: formats.digest(value[1]))]

    def build(self, key):
        if key in self.visiting:
            raise Blocked("CIRCULAR_REFERENCE")
        if key in self.groups:
            return self.groups[key]
        self.visiting.add(key)
        entry = deepcopy(self.entries[key])
        result = {
            "key": key, "kind": entry["kind"], "namespace": entry["namespace"], "source_id": entry["source_id"],
            "rows": entry["rows"], "action": "blocked", "target": None, "before": {}, "payload": {},
            "issues": [], "guards": {}, "dependencies": [], "similar": [], "template_groups": [],
        }
        try:
            self._build(entry, result)
        except Blocked as error:
            result["issues"].append(str(error))
        except frappe.ValidationError:
            result["issues"].append("CATALOG_VALIDATION")
        finally:
            self.visiting.remove(key)
        self.groups[key] = result
        return result

    def _build(self, entry, result):
        kind = entry["kind"]
        source_content = _source_content(entry)
        decision = _decide(entry, self.decisions)
        result["source"] = source_content
        result["candidate"] = entry["fields"]
        result["issues"] = entry["issues"]
        if decision.get("skip") or kind == "Evidence" and not entry["issues"]:
            result["action"] = "skip"
            return
        action = entry["action"]
        target, previous = _map(self.source, entry)
        same_source = target and previous.get("source") == source_content and not decision
        if entry["issues"] and not same_source:
            return
        result["prior_mapping"] = formats.digest(previous) if previous else None
        result["previous_source"] = previous.get("source")
        current = {}
        if entry["target_id"] or entry["target_code"]:
            explicit, current = catalog.lookup(kind, entry["target_id"], entry["target_code"])
            if target and target != explicit:
                raise Blocked("SOURCE_IDENTITY_ALREADY_BOUND")
            if action == "create":
                raise Blocked("CREATE_CANNOT_SPECIFY_TARGET")
            target = explicit
        elif target:
            current = catalog.snapshot(kind, target)
        elif action in ("reference", "update"):
            raise Blocked("EXPLICIT_TARGET_REQUIRED")
        result.update(target=target, before=current)
        if current and kind == "SKUs" and current["mapping_status"] != "Verified":
            raise Blocked("NATIVE_ITEM_MAPPING_CONFLICT")
        if target and (kind, target) in self.claims:
            other = self.groups.get(self.claims[kind, target])
            if kind in {"Specifications", "SKUs"} or (other and other["action"] == "update"):
                raise Blocked("MULTIPLE_SOURCE_IDENTITIES_TARGET_RECORD")
        if previous and any(current.get(field) != previous["target"].get(field) for field in catalog.IMMUTABLE[kind]):
            raise Blocked("TARGET_IDENTITY_DRIFT")
        if previous and kind == "SKUs" and current["native_item"] != previous["target"]["native_item"]:
            raise Blocked("NATIVE_ITEM_MAPPING_CONFLICT")

        if same_source:
            result.update(action="unchanged", projected=current, issues=[])
            self.claims.setdefault((kind, target), result["key"])
            return

        if action == "reference":
            if kind in {"Specifications", "SKUs"} and entry["legacy_code"]:
                raise Blocked("LEGACY_PRODUCT_LINK_REQUIRES_REVIEWED_UPDATE")
            if kind not in {"Specifications", "SKUs"} and current.get("status", ACTIVE) != ACTIVE:
                raise Blocked("INACTIVE_TARGET")
            result.update(action="reference", projected=current)
            self.claims.setdefault((kind, target), result["key"])
            return

        fields = {key: value for key, value in entry["fields"].items() if key not in formats.DERIVED_FIELDS}
        related = {}
        referenced_fields = formats.REFERENCE.keys() & (fields.keys() | current.keys())
        for key in sorted(referenced_fields):
            if fields.get(key) is not None:
                fields[key], related[key] = self.reference(result, formats.REFERENCE[key], fields[key])
            elif current.get(key):
                _, related[key] = self.reference(result, formats.REFERENCE[key], "id:" + current[key])
        if any(record.get("status", ACTIVE) != ACTIVE for record in related.values()):
            raise Blocked("INACTIVE_DEPENDENCY")

        payload = catalog.normalize(kind, fields, current, create=not target)
        projected = {**catalog.defaults(kind), **current, **payload}
        if kind == "Options":
            definition = related.get("attribute")
            if definition and definition.get("data_type") not in SELECT_TYPES:
                raise Blocked("OPTION_REQUIRES_SELECT_ATTRIBUTE")
        if kind == "Templates":
            definition = related.get("attribute")
            mode = projected.get("value_mode", SINGLE)
            if mode not in (SINGLE, MULTI) or (mode == MULTI and definition and
                                                 definition.get("data_type") != MULTI_SELECT):
                raise Blocked("INVALID_TEMPLATE_VALUE_MODE")
        if kind == "Specifications":
            uom = projected["stock_uom"]
            result["unit"] = [uom, catalog.unit(uom)]
            if current and current["quantity_precision"] != result["unit"][1]["precision"]:
                raise Blocked("IMMUTABLE_UNIT_PRECISION")
            prior_values = current.get("values", {})
            rows = entry["fields"].get("values", [])
            if previous and "values" not in decision.get("fields", {}) and not decision.get("drop_rows"):
                earlier = _upstream_values(previous["source"]["fields"].get("values", []))
                incoming = _upstream_values(source_content["fields"].get("values", []))
                rows = [row for row in rows if earlier.get(row["attribute"]) != incoming.get(row["attribute"])]
            if (self.source == formats.LEGACY and not entry["fields"].get("values") and not prior_values and
                    not decision):
                raise Blocked("MISSING_OPTIONAL_TECHNICAL_VALUES")
            values = self._values(result, rows, projected["category"], prior_values)
            if rows:
                payload["values"] = values
            if not current:
                projected["quantity_precision"] = result["unit"][1]["precision"]
        elif kind == "SKUs":
            spec = related.get("specification")
            if not spec:
                _, spec = self.reference(result, "Specifications", "id:" + projected["specification"])
            uom = spec.get("stock_uom")
            result["unit"] = [uom, catalog.unit(uom)]
            precision = result["unit"][1]["precision"]
            if (projected.get("stock_uom") not in (None, uom) or
                    projected.get("quantity_precision") not in (None, precision) or
                    spec.get("quantity_precision") not in (None, precision)):
                raise Blocked("SKU_SPECIFICATION_UNIT_CONFLICT")
            brand = projected["brand"]
            specification = projected["specification"]
            if not target and isinstance(brand, str) and isinstance(specification, str):
                if frappe.db.get_value(catalog.SKU, {"specification": specification, "brand": brand}, "name"):
                    raise Blocked("SKU_BRAND_ALREADY_EXISTS")
        if kind in {"Specifications", "SKUs"}:
            history = entry["fields"].get("history", [])
            if entry["legacy_code"]:
                history = [*history, [formats.SOURCE_SYSTEM[self.source], entry["legacy_code"]]]
            for source, code in history:
                validation.text(source, "Source system", 140, required=True)
                validation.text(code, "Historical identifier", 140, required=True)
                if kind == "Specifications" and (source != source.strip() or "<" in source):
                    raise Blocked("INVALID_SOURCE_LABEL")
            if additions := [value for value in history if value not in current.get("history", ())]:
                payload["history"] = sorted(set(map(tuple, additions)))

        upstream = previous.get("source", {}).get("fields", {})
        patches = decision.get("fields", {})
        changed = {
            name: value for name, value in payload.items()
            if current.get(name) != value and (
                not previous or name in formats.DERIVED_FIELDS or name in patches or
                upstream.get(name) != source_content["fields"].get(name)
            )
        }
        if "values" in changed and all(isinstance(pair[0], str) for pair in changed["values"]):
            proposed = defaultdict(list)
            for attr, value in changed["values"]:
                proposed[attr].append(value)
            prior = current.get("values", {})
            if proposed.keys() == prior.keys() and all(sorted(map(formats.digest, vals)) ==
                    sorted(map(formats.digest, prior[attr])) for attr, vals in proposed.items()):
                del changed["values"]
        if target:
            drift = [name for name in changed if name != "history" and previous
                     and current.get(name) != previous["target"].get(name)]
            if drift and not decision:
                result["manual_drift"] = drift
                raise Blocked("MANUAL_TARGET_EDIT")
            if changed and action == "create":
                raise Blocked("EXISTING_SOURCE_REQUIRES_UPDATE")
            result["action"] = "update" if changed else "unchanged"
            if not previous and action not in {"update"}:
                raise Blocked("EXPLICIT_TARGET_REQUIRED")
            self.claims[kind, target] = result["key"]
        else:
            business = {
                "Options": ("attribute", "option_key"), "Templates": ("category", "attribute"),
                "SKUs": ("specification", "brand"),
            }.get(kind, ("code",) if kind in PARAMETERS else ())
            if business:
                unique = formats.digest([kind, *[projected.get(field) for field in business]])
                if unique in self.business_keys:
                    raise Blocked("DUPLICATE_TARGET_KEY")
                self.business_keys[unique] = result["key"]
            if match := catalog.matching(kind, payload):
                result["similar"] = [match]
                raise Blocked("TARGET_KEY_ALREADY_EXISTS")
            result["action"] = "create"
        result.update(payload=changed if target else payload,
                      projected={**current, **changed} if target else {**projected, **payload},
                      similar=catalog.similar(kind, projected) if not target else [])

    def run(self):
        for kind in (*formats.ORDER, "Evidence"):
            for key, entry in self.entries.items():
                if entry["kind"] == kind:
                    self.build(key)
        return list(self.groups.values())
