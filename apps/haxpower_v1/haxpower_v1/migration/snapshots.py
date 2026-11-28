"""Bound missing-record reporting to an explicitly complete source slice."""

import frappe

from . import formats, store, xlsx

SHEETS = {"Specifications": formats.LEGACY_SPEC, "SKUs": formats.LEGACY_SKU}


def scope(source, supplied):
    if supplied is None:
        return None
    if not isinstance(supplied, dict) or set(supplied) != {"company", "entities"}:
        raise xlsx.MigrationError("INVALID_SNAPSHOT_SCOPE")
    company, entities = supplied["company"], supplied["entities"]
    if (not isinstance(entities, list) or not entities or
            any(not isinstance(kind, str) or kind not in SHEETS for kind in entities) or
            len(set(entities)) != len(entities) or
            source == formats.STANDARD and company is not None or
            source == formats.LEGACY and (
                not isinstance(company, str) or not 0 < len(company) <= 140 or company != company.strip()
            )):
        raise xlsx.MigrationError("INVALID_SNAPSHOT_SCOPE")
    return {"company": company, "entities": sorted(entities)}


def differences(source, dataset, complete, rows, candidates):
    if complete is None:
        return []
    legacy = source == formats.LEGACY
    company = complete["company"]
    namespace = f"{dataset}/{company}" if legacy else dataset
    results = []
    for kind in complete["entities"]:
        sheet = SHEETS[kind] if legacy else kind
        present = [row for row in rows if row.sheet == sheet]
        if legacy:
            evidence = [(row, formats._identity(row.cells["company_id"], legacy=True)) for row in present]
            if any(identity is None for _, identity in evidence):
                results.append(_incomplete(namespace, kind, [row.key for row, value in evidence if value is None]))
                continue
            present = [row for row, identity in evidence if identity == company]
        known = {candidate.source_id for candidate in candidates
                 if candidate.kind == kind and candidate.namespace == namespace}
        if len(known) != len(present):
            results.append(_incomplete(namespace, kind, [row.key for row in present]))
            continue
        mappings = frappe.get_all(store.IDENTITY, filters={
            "source_system": formats.SOURCE_SYSTEM[source], "source_namespace": store.pack(namespace),
            "entity_type": kind,
        }, fields=["name", "target_id", "source_id", "source_namespace", "latest_version"])
        for mapping in mappings:
            identity = store.unpack(mapping.source_id)
            if identity in known or store.unpack(mapping.source_namespace) != namespace:
                continue
            results.append(formats.Candidate(
                "Missing", namespace, mapping.name, "missing",
                {"entity": kind, "source_identity": identity, "target": mapping.target_id,
                 "latest_confirmation": mapping.latest_version}, [], issues=["SOURCE_RECORD_MISSING"],
            ))
            if len(results) + len(candidates) > formats.MAX_GROUPS:
                raise xlsx.MigrationError("MIGRATION_ROW_LIMIT")
    return sorted(results, key=lambda candidate: candidate.key)


def _incomplete(namespace, kind, rows):
    return formats.Candidate("Evidence", namespace, "incomplete-snapshot-" + kind, "evidence",
                             {"entity": kind}, rows[:256], issues=["SNAPSHOT_IDENTITY_INCOMPLETE"])
