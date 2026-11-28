"""Field-level comparison anchors for source identities."""

from copy import deepcopy

from . import formats
from .catalog import Blocked

ATTRIBUTE_PREFIX = "values:"


def business(source, prior=None):
    """Carry forward values omitted from a partial export."""
    prior = prior or {}
    incoming = source.get("fields", {})
    fields = {**prior.get("fields", {}), **{k: v for k, v in incoming.items() if v is not None}}
    if "values" in incoming:
        replaced = {row["attribute"] for row in incoming["values"]}
        fields["values"] = sorted(
            [*[row for row in prior.get("fields", {}).get("values", ()) if row["attribute"] not in replaced],
             *incoming["values"]], key=formats.digest,
        )
    return {"fields": fields, "legacy_code": source.get("legacy_code") or prior.get("legacy_code")}


def upstream(previous):
    return previous.get("business_source") or business(previous.get("source", {}))


def value(state, field):
    if field.startswith(ATTRIBUTE_PREFIX):
        values = state.get("values", {}).get(field.removeprefix(ATTRIBUTE_PREFIX), [])
        return sorted(values, key=formats.digest)
    return state.get(field)


def fields(state):
    return {**{name: content for name, content in state.items() if name != "values"},
            **{ATTRIBUTE_PREFIX + key: value(state, ATTRIBUTE_PREFIX + key) for key in state.get("values", {})}}


def resolve(current, previous, changed, proposed_attributes, decision):
    """Apply only explicitly resolved conflicts and return approved field names."""
    keep = set(decision.get("keep_local", ()))
    accept = set(decision.get("accept_source", ()))
    touched = set(changed) - {"history", "values"}
    if "values" in changed:
        touched.update(key for key, proposed in proposed_attributes.items()
                       if value(current, key) != proposed)
    if not (keep | accept) <= touched:
        raise Blocked("INVALID_FIELD_RESOLUTION")
    anchors = previous.get("baseline", {})
    # Old mapping snapshots may have absorbed independent manual edits.
    # A changed field with no trustworthy anchor requires explicit review.
    drift = sorted(field for field in touched if previous and (
        (field not in anchors and (not previous.get("baseline_complete") or not field.startswith(ATTRIBUTE_PREFIX))) or
        value(current, field) != anchors.get(field, [])
    ) and field not in keep | accept)
    return touched, keep, drift


def advance(previous, group, state, attribute_ids):
    if not previous:
        return fields(state)
    anchors = deepcopy(previous.get("baseline", {}))
    for name in group.get("confirmed_fields", ()):
        anchors[name] = value(state, name)
    for identity in attribute_ids:
        name = ATTRIBUTE_PREFIX + identity
        anchors[name] = value(state, name)
    return anchors
