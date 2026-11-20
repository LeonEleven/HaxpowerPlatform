"""Persistent synthetic catalog data and native HTTP checks on disposable CI sites."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from secrets import token_urlsafe
from urllib.parse import quote

import frappe
from frappe.utils import get_test_client

from haxpower_v1.catalog import service
from haxpower_v1.catalog.dto import (
    INACTIVE, MULTI, MULTI_SELECT, AttributeData, CategoryData, OptionData, SpecificationData, TemplateData,
)
from haxpower_v1.catalog.persistence import ATTRIBUTE, AUDIT, CATEGORY, OPTION, SPECIFICATION, TEMPLATE, VALUE

from identity import require


DOCTYPES = (CATEGORY, ATTRIBUTE, OPTION, TEMPLATE, SPECIFICATION, VALUE, AUDIT)
SAMPLE_CODE = "SYNTHETIC-V1-CATALOG-MIGRATION"


def snapshot():
    return {
        doctype: {
            "schema": frappe.db.sql(f"SHOW CREATE TABLE `tab{doctype}`")[0][1],
            "rows": frappe.get_all(doctype, fields=["*"], order_by="name"),
        } if frappe.db.table_exists(doctype) else None
        for doctype in DOCTYPES
    }


def seed():
    frappe.set_user("Administrator")
    require(not frappe.db.exists(SPECIFICATION, {"code": SAMPLE_CODE}), "synthetic specification already exists")
    category = service.save_category(CategoryData(code=SAMPLE_CODE, display_name="Synthetic migration category"))
    attribute = service.save_attribute(AttributeData(
        code=SAMPLE_CODE, display_name="Synthetic migration attribute", data_type=MULTI_SELECT,
    ))
    service.save_template(TemplateData(category=category.id, attribute=attribute.id, value_mode=MULTI))
    old = service.save_option(OptionData(attribute=attribute.id, option_key="OLD", display_name="Synthetic old"))
    new = service.save_option(OptionData(attribute=attribute.id, option_key="NEW", display_name="Synthetic new"))
    data = SpecificationData(code=SAMPLE_CODE, category=category.id, display_name="Synthetic migration specification")
    original = service.create_specification(data, attributes={attribute.id: [old.id]})
    service.save_option(replace(old.data, status=INACTIVE), record_id=old.id)
    saved = service.edit_specification(original.id, data, expected_revision=1, attributes={attribute.id: [new.id, old.id]})
    require(service.get_specification(saved.id) == saved and len(service.get_specification_audit(saved.id)) == 1,
            "failed to round-trip synthetic catalog references and audit")
    frappe.db.commit()


def verify_native_denials(site):
    frappe.set_user("Administrator")
    spec_id = frappe.db.get_value(SPECIFICATION, {"code": SAMPLE_CODE}, "name")
    require(spec_id, "missing synthetic specification")
    child_id = frappe.get_doc(SPECIFICATION, spec_id).attribute_values[0].name
    audit_id = frappe.db.get_value(AUDIT, {"specification": spec_id}, "name")
    user_id = "synthetic-catalog-http@example.invalid"
    password = "Aa9!" + token_urlsafe(24)
    user = frappe.get_doc(
        doctype="User", email=user_id, first_name="Synthetic catalog HTTP", user_type="System User",
        send_welcome_email=0, new_password=password, roles=[{"role": "Employee"}, {"role": "Item Manager"}],
    )
    user.insert(ignore_permissions=True)
    frappe.db.commit()
    from haxpower_v1.adapters.erpnext.item import SKU_MARKER
    from sku_probe import PREFIX, snapshot as sku_snapshot

    sku_name = f"{PREFIX}-A"
    sku_doctype = "HP V1 Stock Item"
    sku_id = frappe.db.get_value(sku_doctype, {"code": sku_name}, "name")
    require(sku_id, "missing synthetic SKU")
    before = snapshot(), sku_snapshot()

    def exercise():
        client = get_test_client()
        base_url = f"http://{site}"

        def request(method, path, data=None):
            return client.open(
                path, method=method, base_url=base_url, json=data,
                headers={"Accept": "application/json"}, buffered=True,
            )

        login = request("POST", "/api/method/login", {"usr": user_id, "pwd": password})
        require(login.status_code == 200, f"synthetic catalog HTTP login failed ({login.status_code})")
        identity = request("GET", "/api/method/frappe.auth.get_logged_user")
        require(identity.status_code == 200 and identity.json.get("message") == user_id,
                "HTTP requests are not authenticated as the non-maintenance test user")
        spec = quote(SPECIFICATION, safe="")
        audit = quote(AUDIT, safe="")
        denied = (
            ("POST", f"/api/resource/{spec}", {"code": "SYNTHETIC-NATIVE", "display_name": "Synthetic HTTP"}),
            ("PUT", f"/api/resource/{spec}/{spec_id}", {"display_name": "Synthetic native edit"}),
            ("POST", f"/api/v2/document/{spec}", {"code": "SYNTHETIC-NATIVE", "display_name": "Synthetic HTTP"}),
            ("PATCH", f"/api/v2/document/{spec}/{spec_id}", {"display_name": "Synthetic native edit"}),
            ("DELETE", f"/api/v2/document/{spec}/{spec_id}", None),
            ("PUT", f"/api/resource/{audit}/{audit_id}", {"after_values": "{}"}),
            ("DELETE", f"/api/resource/{audit}/{audit_id}", None),
            ("POST", "/api/method/frappe.client.set_value",
             {"doctype": VALUE, "name": child_id, "fieldname": "option_key", "value": "SYNTHETIC-CHANGED"}),
            ("POST", "/api/method/haxpower_v1.catalog.sku.create_sku", {}),
            ("GET", f"/api/resource/{quote(sku_doctype, safe='')}/{sku_id}", None),
            ("POST", f"/api/resource/{quote(sku_doctype, safe='')}", {"code": "SYNTHETIC-DENIED"}),
            ("PATCH", f"/api/v2/document/{quote(sku_doctype, safe='')}/{sku_id}", {"display_name": "Denied"}),
            ("DELETE", f"/api/v2/document/{quote(sku_doctype, safe='')}/{sku_id}", None),
            ("PUT", f"/api/resource/Item/{sku_name}", {"disabled": 1}),
            ("PATCH", f"/api/v2/document/Item/{sku_name}", {"item_name": "Denied"}),
            ("DELETE", f"/api/v2/document/Item/{sku_name}", None),
            ("POST", "/api/method/frappe.client.set_value",
             {"doctype": "Item", "name": sku_name, "fieldname": "disabled", "value": 1}),
        )
        for endpoint in (f"/api/resource/Item/{sku_name}", f"/api/v2/document/Item/{sku_name}"):
            response = request("GET", endpoint)
            require(response.status_code == 200 and not response.json["data"].get(SKU_MARKER),
                    "native Item read exposed the internal mapping marker")

        for method, path, data in denied:
            response = request(method, path, data)
            require(response.status_code == 403, f"expected native HTTP denial for {method} {path}, got {response.status_code}")
        return len(denied)

    # The WSGI app owns a different Frappe local and DB transaction per request.
    with ThreadPoolExecutor(max_workers=1) as executor:
        checked = executor.submit(exercise).result()
    frappe.db.rollback()
    require(before == (snapshot(), sku_snapshot()), "denied HTTP requests changed catalog or Item data")
    print(f"Authenticated non-maintenance user: {checked} HTTP requests denied, "
          "two native reads hid the mapping marker, catalog and Item data unchanged.")
