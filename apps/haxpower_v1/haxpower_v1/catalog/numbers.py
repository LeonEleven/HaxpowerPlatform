"""Transaction-scoped high-water marks for issued catalog codes."""

import re

import frappe

from .persistence import NUMBER, SPECIFICATION

SPEC_CODE = re.compile(r"P([0-9]{6,99})\Z")
SKU_CODE = re.compile(r"(P[0-9]{6,99})-([0-9]{3,98})\Z")
SPEC_SEQUENCE = "SPECIFICATION"


def _serial(text, width):
    value = int(text)
    return value if value > 0 and f"{value:0{width}d}" == text else None


def spec_serial(code):
    found = SPEC_CODE.fullmatch(code) if isinstance(code, str) else None
    return _serial(found[1], 6) if found else None


def sku_serial(code, spec_code):
    found = SKU_CODE.fullmatch(code) if isinstance(code, str) else None
    return _serial(found[2], 3) if found and found[1] == spec_code else None


def assert_fixture_code(code):
    from .validation import key

    code = key(code, "Code")
    if SPEC_CODE.fullmatch(code) or SKU_CODE.fullmatch(code):
        frappe.throw("Fixture codes cannot occupy the formal numbering namespace.")


def _last_value(namespace):
    stored = frappe.db.get_value(NUMBER, namespace, "last_value", for_update=True)
    if stored is None:
        return None
    if not re.fullmatch(r"0|[1-9][0-9]{0,98}", stored):
        frappe.throw(f"Invalid catalog number state for {namespace}.")
    return int(stored)


def reserve_through(namespace, minimum):
    current = _last_value(namespace)
    if current is None:
        frappe.get_doc(doctype=NUMBER, last_value=str(minimum)).insert(
            ignore_permissions=True, set_name=namespace,
        )
    elif current < minimum:
        frappe.db.set_value(NUMBER, namespace, "last_value", str(minimum), update_modified=False)


def allocate(spec=None):
    from .sku_persistence import SKU

    if spec and not spec_serial(spec.code):
        frappe.throw("Historical specification code requires an approved disposition before creating a SKU.")
    namespace = spec.id if spec else SPEC_SEQUENCE
    current = _last_value(namespace)
    if current is None:
        frappe.throw("Catalog numbering has not been initialized for this identity. Run site migration.")
    next_value = current + 1
    code = f"{spec.code}-{next_value:03d}" if spec else f"P{next_value:06d}"
    if len(code) > 100:
        frappe.throw("Catalog number namespace is exhausted.")
    if frappe.db.get_value(SKU if spec else SPECIFICATION, {"code": code}, for_update=True):
        frappe.throw(f"Catalog numbering conflicts with existing code {code}. Run site migration.",
                     frappe.UniqueValidationError)
    frappe.db.set_value(NUMBER, namespace, "last_value", str(next_value), update_modified=False)
    return code
