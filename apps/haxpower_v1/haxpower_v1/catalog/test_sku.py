"""SKU integration tests on the locked Frappe/ERPNext site."""

from dataclasses import asdict, replace
from unittest.mock import patch
from uuid import uuid4

import frappe
from frappe import client
from frappe.tests import IntegrationTestCase

from ..adapters.erpnext import item
from . import service, sku
from .dto import INACTIVE, CategoryData, SpecificationData
from .persistence import MAINTAINER
from .sku_dto import Alias, BrandData, Identifier, SKUData
from .sku_persistence import ALIAS, IDENTIFIER, SKU

NO_STOCK_WRITES = (
    "Stock Ledger Entry", "Bin", "Stock Entry", "Stock Reconciliation", "Batch",
    "Serial and Batch Bundle", "GL Entry", "Payment Ledger Entry", "Item Price",
)


def make_uom(name, *, whole=False, enabled=True):
    return frappe.get_doc(
        doctype="UOM", uom_name=name, must_be_whole_number=int(whole), enabled=int(enabled),
    ).insert(ignore_permissions=True).name


def make_item_group(name):
    root = frappe.db.get_value("Item Group", {"is_group": 1, "parent_item_group": ("is", "not set")})
    if not root:
        root = frappe.get_doc(
            doctype="Item Group", item_group_name=f"{name}-ROOT", is_group=1,
        ).insert().name
    return frappe.get_doc(doctype="Item Group", item_group_name=name, parent_item_group=root).insert().name


class TestSKU(IntegrationTestCase):
    def setUp(self):
        super().setUp()
        frappe.set_user("Administrator")
        point = f"test_sku_{uuid4().hex}"
        frappe.db.savepoint(point)
        self.addCleanup(frappe.db.rollback, save_point=point)
        self.addCleanup(frappe.set_user, "Administrator")
        self.code = f"SYNTHETIC-SKU-{uuid4().hex[:12].upper()}"
        self.uom = make_uom(self.code)
        self.group = make_item_group(self.code)
        self.config = {"item_group": self.group, "uom_precision": {self.uom: 3}}
        self.enterContext(patch.dict(frappe.conf, {item.SETTINGS: self.config}))
        category = service.save_category(CategoryData(code=self.code, display_name="Synthetic category"))
        self.spec = service.create_specification(
            SpecificationData(code=self.code, category=category.id, display_name="Synthetic specification"),
            attributes={},
        )
        self.brand = sku.save_brand(BrandData(code=self.code, display_name="合成 Alpha"))
        self.data = SKUData(
            code=self.code, specification=self.spec.id, brand=self.brand.id, display_name="合成阀 Synthetic Valve",
            stock_uom=self.uom, quantity_precision=3,
        )

    def native_item(self, *, code=None, **changes):
        fields = dict(item_code=code or self.code, item_name=self.data.display_name, item_group=self.group,
                      stock_uom=self.uom, is_stock_item=1, opening_stock=0, standard_rate=0)
        return frappe.get_doc(doctype="Item", **(fields | changes)).insert()

    def user(self, role):
        return frappe.get_doc(
            doctype="User", email=f"{uuid4().hex[:12]}@example.invalid",
            first_name="Synthetic SKU tester", user_type="System User", send_welcome_email=0,
            roles=[{"role": role}],
        ).insert(ignore_permissions=True).name

    def test_two_brands_search_and_identity_without_stock_changes(self):
        baseline = {doctype: frappe.db.count(doctype) for doctype in NO_STOCK_WRITES}
        other = sku.save_brand(BrandData(code=f"{self.code}-B", display_name="合成 Beta"))
        oem = Identifier(kind="OEM", value=f"ＯＥＭ-{self.code}", source="Synthetic sample")
        engrave = Identifier(kind="ENGRAVING", value=f"刻印-{self.code}", notes="Synthetic engraving")
        a = sku.create_sku(
            replace(self.data, code=self.code.lower()), identifiers=[oem, engrave],
            aliases=[Alias(value=f"合成油嘴 {self.code}", language="zh"), Alias(value="Hidden synonym", searchable=False)],
        )
        b = sku.create_sku(replace(self.data, code=f"{self.code}-B", brand=other.id), identifiers=[oem])
        self.assertEqual(a.data.code, self.code)
        self.assertNotEqual(a.id, b.id)
        self.assertEqual(a.data.specification, b.data.specification)
        for result in (a, b):
            self.assertEqual(sku.get_sku(result.id), result)
            self.assertEqual(result.mapping_status, "Verified")
            self.assertNotIn("native_item", asdict(result))
            diagnostic = sku.diagnose_item_mapping(result.data.code)
            self.assertEqual((diagnostic.status, diagnostic.sku_id, diagnostic.marker),
                             ("Verified", result.id, result.id))
        for query in (f"合成阀 {self.code}", f"VALVE {self.code}", f"oem-{self.code}"):
            self.assertEqual(tuple(row.id for row in sku.search_skus(query)), (a.id, b.id))
        for query in (f"合成油嘴 alpha {self.code}", f"刻印-{self.code}"):
            self.assertEqual(tuple(row.id for row in sku.search_skus(query)), (a.id,))
        self.assertEqual(sku.search_skus("Hidden synonym"), ())
        self.assertEqual(sku.search_skus("%"), ())
        self.assertEqual(sku.search_skus(f"valve {self.code}", limit=1, offset=1), (b,))
        self.assertEqual({doctype: frappe.db.count(doctype) for doctype in NO_STOCK_WRITES}, baseline)
        with self.assertRaises(frappe.UniqueValidationError):
            sku.create_sku(replace(self.data, code=self.code.lower(), brand=other.id))

    def test_explicit_mapping_and_database_uniqueness(self):
        native = self.native_item(code=f"{self.code}-NATIVE")
        a = sku.create_sku(self.data, existing_item=native.name)
        self.assertEqual(sku.diagnose_item_mapping(native.name).sku_id, a.id)
        with self.assertRaisesRegex(frappe.ValidationError, "already mapped"):
            sku.create_sku(replace(self.data, code=f"{self.code}-B"), existing_item=native.name)
        b = sku.create_sku(replace(self.data, code=f"{self.code}-B"))
        # This fixture checks the actual database index beneath the ORM/service checks.
        row = frappe.get_doc(SKU, b.id).as_dict()
        for changes in ({"native_item": native.name, "code": f"{self.code}-C"},
                        {"native_item": f"{self.code}-C", "code": self.code.lower()}):
            with self.assertRaises(frappe.UniqueValidationError):
                frappe.get_doc(dict(row, **changes, name=uuid4().hex)).db_insert()
        self.assertEqual(frappe.db.get_value(SKU, b.id, "native_item"), b.data.code)

    def test_missing_or_invalid_master_data(self):
        for changes in ({"specification": uuid4().hex}, {"brand": None}, {"stock_uom": "MISSING"},
                        {"quantity_precision": True}, {"quantity_precision": -1}, {"quantity_precision": 2}):
            with self.subTest(changes=changes), self.assertRaises(frappe.ValidationError):
                sku.create_sku(replace(self.data, **changes))
        with patch.dict(frappe.conf, {item.SETTINGS: None}), self.assertRaisesRegex(
            frappe.ValidationError, "configuration is required",
        ):
            sku.create_sku(self.data)
        for name, precision, whole, enabled in (
            ("MISSING", 0, False, True), (f"{self.code}-DISABLED", 0, False, False),
            (f"{self.code}-WHOLE", 3, True, True), (self.uom, 10, False, True),
        ):
            if name not in ("MISSING", self.uom):
                make_uom(name, whole=whole, enabled=enabled)
            self.config["uom_precision"][name] = precision
            with self.subTest(uom=name), self.assertRaisesRegex(frappe.ValidationError, "stock UOM"):
                sku.create_sku(replace(self.data, stock_uom=name, quantity_precision=precision))
        disabled = sku.save_brand(replace(self.brand.data, status=INACTIVE), record_id=self.brand.id)
        with self.assertRaisesRegex(frappe.ValidationError, "active brand"):
            sku.create_sku(replace(self.data, brand=disabled.id))
        service.edit_specification(
            self.spec.id, replace(self.spec.data, status=INACTIVE), expected_revision=self.spec.revision,
        )
        with self.assertRaisesRegex(frappe.ValidationError, "active specification"):
            sku.create_sku(self.data)

    def test_integer_unit_and_metadata_edit(self):
        whole = make_uom(f"{self.code}-WHOLE", whole=True)
        self.config["uom_precision"][whole] = 0
        data = replace(self.data, stock_uom=whole, quantity_precision=0)
        saved = sku.create_sku(data, aliases=[Alias(value="Original alias")])
        edited = sku.edit_sku(
            saved.id, replace(data, display_name="合成编辑 Updated", description="Synthetic edit"),
            expected_revision=saved.revision, aliases=[],
        )
        self.assertEqual(sku.get_sku(saved.id), edited)
        self.assertEqual(edited.aliases, ())
        self.assertEqual(edited.revision, 2)
        self.assertEqual(edited.data.quantity_precision, 0)
        self.assertEqual(frappe.db.get_value("Item", self.code, "item_name"), "合成编辑 Updated")
        with self.assertRaisesRegex(frappe.ValidationError, "revision changed"):
            sku.edit_sku(saved.id, data, expected_revision=saved.revision)
        for changes in ({"code": f"{self.code}-B"}, {"stock_uom": self.uom}, {"quantity_precision": 1},
                        {"specification": uuid4().hex}, {"brand": uuid4().hex}):
            with self.subTest(changes=changes), self.assertRaisesRegex(frappe.ValidationError, "immutable"):
                sku.edit_sku(saved.id, replace(edited.data, **changes), expected_revision=edited.revision)
        with self.assertRaisesRegex(frappe.ValidationError, "active SKUs"):
            sku.save_brand(replace(self.brand.data, status=INACTIVE), record_id=self.brand.id)
        self.assertEqual(sku.get_sku(saved.id), edited)

    def test_native_failure_after_insert_and_domain_failure_roll_back(self):
        insert = frappe.get_doc(doctype="Item").__class__.after_insert

        def fail_after_native_write(native):
            insert(native)
            self.assertTrue(frappe.db.exists("Item", self.code))
            frappe.throw("Synthetic failure after native Item insertion.")

        controller = frappe.get_doc(doctype="Item").__class__
        with patch.object(controller, "after_insert", fail_after_native_write), self.assertRaisesRegex(
            frappe.ValidationError, "after native Item insertion",
        ):
            sku.create_sku(self.data, aliases=[Alias(value="Synthetic alias")])
        self.assertFalse(frappe.db.exists("Item", self.code))
        self.assertFalse(frappe.db.exists(SKU, {"code": self.code}))
        # Fail after both tables and children have been written to exercise the
        # outer savepoint, including when the caller catches the error.
        with patch.object(sku, "_stock_item", side_effect=frappe.ValidationError("Synthetic DTO failure")):
            with self.assertRaisesRegex(frappe.ValidationError, "Synthetic DTO failure"):
                sku.create_sku(self.data, aliases=[Alias(value="Synthetic rolled-back alias")],
                               identifiers=[Identifier(kind="OEM", value=self.code)])
        self.assertFalse(frappe.db.exists("Item", self.code))
        self.assertFalse(frappe.db.exists(SKU, {"code": self.code}))
        self.assertFalse(frappe.db.exists(ALIAS, {"value": "Synthetic rolled-back alias"}))
        self.assertFalse(frappe.db.exists(IDENTIFIER, {"value": self.code}))
        native = self.native_item(code=f"{self.code}-ADOPT")
        with patch.object(sku, "_stock_item", side_effect=frappe.ValidationError("Synthetic adoption failure")):
            with self.assertRaisesRegex(frappe.ValidationError, "Synthetic adoption failure"):
                sku.create_sku(self.data, existing_item=native.name)
        self.assertFalse(frappe.db.get_value("Item", native.name, item.SKU_MARKER))
        self.assertEqual(sku.diagnose_item_mapping(native.name).status, "Unmanaged")
        self.assertEqual(sku.create_sku(self.data).mapping_status, "Verified")

    def test_deactivation_rejects_stock_open_lines_and_incomplete_proof(self):
        saved = sku.create_sku(self.data)
        with self.assertRaisesRegex(frappe.ValidationError, "write coordination"):
            sku.deactivate_sku(saved.id)
        # Synthetic reference rows isolate the check from stock posting, which
        # is outside V1-03. The surrounding savepoint removes these fixtures.
        frappe.get_doc(doctype="Bin", name=uuid4().hex, item_code=self.code, actual_qty=1).db_insert()
        with self.assertRaisesRegex(frappe.ValidationError, "native stock"):
            sku.deactivate_sku(saved.id)
        frappe.db.delete("Bin", {"item_code": self.code})
        frappe.get_doc(
            doctype="Stock Entry Detail", name=uuid4().hex, parent=uuid4().hex, parenttype="Stock Entry",
            parentfield="items", item_code=self.code, docstatus=0,
        ).db_insert()
        with self.assertRaisesRegex(frappe.ValidationError, "active native reference in Stock Entry Detail"):
            sku.deactivate_sku(saved.id)
        self.assertEqual(sku.get_sku(saved.id), saved)
        self.assertFalse(frappe.db.get_value("Item", self.code, "disabled"))

    def test_role_and_document_guards(self):
        employee = self.user("Item Manager")
        maintainer = self.user(MAINTAINER)
        frappe.set_user(maintainer)
        saved = sku.create_sku(self.data)
        self.assertEqual(sku.get_sku(saved.id), saved)
        with self.assertRaises(frappe.PermissionError):
            sku.diagnose_item_mapping(self.code)
        frappe.set_user(employee)
        for call in (lambda: sku.create_sku(self.data), lambda: sku.get_sku(saved.id),
                     lambda: sku.search_skus(self.code), lambda: sku.deactivate_sku(saved.id),
                     lambda: sku.save_brand(self.brand.data), lambda: sku.get_brand(self.brand.id),
                     lambda: sku.edit_sku(saved.id, self.data, expected_revision=saved.revision)):
            with self.assertRaises(frappe.PermissionError):
                call()
        with self.assertRaises(frappe.PermissionError):
            client.set_value("Item", self.code, "disabled", 1)
        with self.assertRaises(frappe.PermissionError):
            client.set_value(SKU, saved.id, "display_name", "Denied")
        frappe.set_user("Administrator")
        for field, value in (("item_code", f"{self.code}-OTHER"), (item.SKU_MARKER, ""), ("disabled", 1)):
            native = frappe.get_doc("Item", self.code)
            with self.subTest(field=field), self.assertRaises(frappe.PermissionError):
                native.db_set(field, value)
        managed_uom = frappe.get_doc("UOM", self.uom)
        with self.assertRaises(frappe.PermissionError):
            managed_uom.db_set("must_be_whole_number", 1)
        domain = frappe.get_doc(SKU, saved.id)
        with self.assertRaises(frappe.PermissionError):
            domain.db_set("quantity_precision", 0)
        with self.assertRaises(frappe.PermissionError):
            frappe.delete_doc(SKU, saved.id, ignore_permissions=True, force=True)
        with self.assertRaises(frappe.PermissionError):
            frappe.delete_doc("Item", self.code, ignore_permissions=True, force=True)
        with self.assertRaises(frappe.PermissionError):
            frappe.rename_doc("Item", self.code, f"{self.code}-RENAMED", force=True)
        managed = frappe.get_doc("Item", self.code)
        managed.end_of_life = str(managed.end_of_life)
        managed.description = "Synthetic non-identity maintenance"
        managed.save()
        unmanaged = self.native_item(code=f"{self.code}-UNMANAGED")
        unmanaged.item_name = "Unmanaged native maintenance"
        unmanaged.save()
        self.assertEqual(sku.diagnose_item_mapping(unmanaged.name).status, "Unmanaged")
        self.assertEqual(sku.get_sku(saved.id), saved)

    def test_mapping_diagnostics_detect_corruption(self):
        saved = sku.create_sku(self.data)
        other = self.native_item(code=f"{self.code}-OTHER")
        frappe.db.set_value("Item", other.name, item.SKU_MARKER, saved.id)
        self.assertEqual(sku.get_sku(saved.id).mapping_status, "Duplicate Marker")
        self.assertEqual(sku.diagnose_item_mapping(other.name).status, "Duplicate Marker")
        frappe.db.set_value("Item", other.name, item.SKU_MARKER, uuid4().hex)
        self.assertEqual(sku.diagnose_item_mapping(other.name).status, "Orphan Marker")
        frappe.db.set_value("Item", self.code, "item_name", "Synthetic unexpected edit")
        self.assertEqual(sku.get_sku(saved.id).mapping_status, "Item Drift")
        with self.assertRaisesRegex(frappe.ValidationError, "needs review"):
            sku.edit_sku(saved.id, self.data, expected_revision=saved.revision)
        frappe.db.delete("Item", {"name": self.code})
        self.assertEqual(sku.get_sku(saved.id).mapping_status, "Missing Item")
        self.assertEqual(sku.diagnose_item_mapping(self.code).sku_id, saved.id)
