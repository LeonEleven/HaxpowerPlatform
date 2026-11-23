"""Integration tests for warehouse master data in the locked ERPNext site."""

from unittest.mock import patch
from uuid import uuid4

import frappe
from frappe.tests import IntegrationTestCase

from ..adapters.erpnext import warehouse as native
from ..catalog.test_sku import NO_STOCK_WRITES, make_item_group, make_uom
from . import service
from .persistence import LOCATION, MAINTAINER, WAREHOUSE


def stock_counts():
    return {doctype: frappe.db.count(doctype) for doctype in NO_STOCK_WRITES}


def make_company(suffix):
    # A bare ERPNext install has not run the setup wizard's native fixtures.
    if not frappe.db.exists("Warehouse Type", "Transit"):
        frappe.get_doc(doctype="Warehouse Type").insert(set_name="Transit")
    name = f"Synthetic V1 Warehouse {suffix}"
    company = frappe.get_doc(
        doctype="Company", company_name=name, abbr=suffix,
        default_currency="USD", country="United States", enable_perpetual_inventory=0,
    ).insert(ignore_permissions=True)
    parent = frappe.db.get_value("Warehouse", {
        "company": company.name, "is_group": 1, "parent_warehouse": ("is", "not set"),
    })
    return company.name, parent


def make_native(company, parent, *, group=False, disabled=False):
    return frappe.get_doc(
        doctype="Warehouse", warehouse_name=f"Synthetic {uuid4().hex}", company=company,
        parent_warehouse=parent, is_group=int(group), disabled=int(disabled),
    ).insert()


def mapping_records():
    records = {}
    for doctype in (WAREHOUSE, LOCATION, native.NATIVE):
        records[doctype] = frappe.get_all(doctype, fields=["*"], order_by="name")
    return records


class TestWarehouse(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        frappe.set_user("Administrator")
        cls.company, cls.parent = make_company(uuid4().hex[:5].upper())
        cls.second_company, cls.second_parent = make_company(uuid4().hex[:5].upper())

    def setUp(self):
        super().setUp()
        frappe.set_user("Administrator")
        point = f"test_warehouse_{uuid4().hex}"
        frappe.db.savepoint(point)
        self.addCleanup(frappe.db.rollback, save_point=point)
        self.addCleanup(frappe.set_user, "Administrator")

    def warehouse(self, code="W1"):
        return service.create_warehouse(company=self.company, native_parent=self.parent, code=code,
                                        display_name=f"合成仓库 {code}")

    def location(self, warehouse, code="A01", **kwargs):
        return service.create_location(warehouse.id, code=code, display_name=f"合成库位 {code}", **kwargs)

    def test_dynamic_warehouses_and_independent_leaf_locations(self):
        stock_before = stock_counts()
        for code in ("W1", "W2", "W3"):
            warehouse = self.warehouse(code)
            for local_code in ("A01", "A02"):
                self.location(warehouse, local_code)
        self.assertEqual([row.code for row in service.list_warehouses(company=self.company)], ["W1", "W2", "W3"])
        fourth = self.warehouse("W4")
        self.location(fourth, "A01")
        self.location(fourth, "A02")
        warehouses = service.list_warehouses(company=self.company)
        self.assertEqual([row.code for row in warehouses], ["W1", "W2", "W3", "W4"])

        leaves = set()
        for warehouse in warehouses:
            self.assertEqual(service.get_warehouse(warehouse.id), warehouse)
            self.assertEqual(warehouse.display_name, f"合成仓库 {warehouse.code}")
            locations = service.list_locations(warehouse.id)
            self.assertEqual([location.code for location in locations], ["A01", "A02"])
            for location in locations:
                self.assertEqual(service.get_location(location.id), location)
                self.assertEqual(service.resolve_location(location.id), location)
                self.assertEqual(location.display_name, f"合成库位 {location.code}")
                self.assertEqual(location.warehouse_id, warehouse.id)
                mapping = service.diagnose_mapping(location.id)
                leaf = frappe.get_doc(native.NATIVE, mapping.native_warehouse)
                self.assertEqual(mapping.status, "Verified")
                self.assertEqual((leaf.company, leaf.parent_warehouse, leaf.is_group, leaf.get(native.MARKER)),
                                 (self.company, self.parent, 0, location.id))
                self.assertNotEqual(location.id, leaf.name)
                leaves.add(leaf.name)
        self.assertEqual(len(leaves), 8)
        self.assertEqual(stock_counts(), stock_before)

    def test_duplicate_codes_and_native_bindings_are_rejected_by_database(self):
        warehouse = self.warehouse()
        existing = make_native(self.company, self.parent)
        first = self.location(warehouse, existing_native=existing.name)
        second = self.location(warehouse, "A02")
        before = mapping_records()
        with self.assertRaisesRegex(frappe.ValidationError, "Warehouse code already"):
            self.warehouse("w1")
        with self.assertRaisesRegex(frappe.ValidationError, "Location code already"):
            self.location(warehouse, "a01")
        with self.assertRaisesRegex(frappe.ValidationError, "already mapped"):
            self.location(self.warehouse("W2"), existing_native=existing.name)
        self.assertEqual(service.diagnose_native_mapping(existing.name).location_id, first.id)
        row = frappe.get_doc(LOCATION, second.id).as_dict()
        for changes in ({"code": "A03", "native_warehouse": existing.name},
                        {"code": "a01", "native_warehouse": "Unused Warehouse"}):
            with self.assertRaises(frappe.UniqueValidationError):
                frappe.get_doc(dict(row, name=uuid4().hex, **changes)).db_insert()
        self.assertEqual(mapping_records()[LOCATION], before[LOCATION])
        self.assertEqual(mapping_records()[native.NATIVE], before[native.NATIVE])

    def test_invalid_company_and_non_leaf_or_disabled_mapping(self):
        warehouse = self.warehouse()
        with self.assertRaisesRegex(frappe.ValidationError, "operational ERPNext Company"):
            service.create_warehouse(company="Missing synthetic company", native_parent=self.parent, code="W2",
                                     display_name="Invalid")
        with self.assertRaisesRegex(frappe.ValidationError, "same Company"):
            service.create_warehouse(company=self.second_company, native_parent=self.parent, code="W2",
                                     display_name="Invalid")
        cross_company = make_native(self.second_company, self.second_parent)
        group = make_native(self.company, self.parent, group=True)
        disabled = make_native(self.company, self.parent, disabled=True)
        before = mapping_records()
        for name in (cross_company.name, group.name, disabled.name):
            with self.subTest(name=name), self.assertRaisesRegex(frappe.ValidationError, "enabled leaf"):
                self.location(warehouse, existing_native=name)
        with self.assertRaises(frappe.DoesNotExistError):
            service.create_location(uuid4().hex, code="A01", display_name="Orphan")
        self.assertEqual(mapping_records(), before)

    def test_metadata_edits_keep_identity_and_protect_native_lifecycle(self):
        warehouse = self.warehouse()
        location = self.location(warehouse)
        updated = service.edit_warehouse(warehouse.id, display_name="更新仓库", expected_revision=1)
        updated_location = service.edit_location(location.id, display_name="更新库位", expected_revision=1)
        self.assertEqual((updated.id, updated.code, updated.company, updated.revision),
                         (warehouse.id, warehouse.code, self.company, 2))
        self.assertEqual((updated_location.id, updated_location.code, updated_location.revision),
                         (location.id, location.code, 2))
        with self.assertRaises(frappe.TimestampMismatchError):
            service.edit_location(location.id, display_name="Outdated", expected_revision=1)
        name = service.diagnose_mapping(location.id).native_warehouse
        unmanaged = make_native(self.company, self.parent)
        before = mapping_records()
        for field, value in (("warehouse_name", "Renamed"), ("company", self.second_company),
                             ("is_group", 1), ("disabled", 1), (native.MARKER, "")):
            with self.subTest(field=field), self.assertRaises(frappe.PermissionError):
                frappe.get_doc(native.NATIVE, name).db_set(field, value)
        for old, new, merge in ((name, "Renamed leaf", False), (name, unmanaged.name, True),
                                (unmanaged.name, name, True), (self.parent, "Renamed group", False)):
            with self.assertRaises(frappe.PermissionError):
                frappe.rename_doc(native.NATIVE, old, new, merge=merge, force=True)
        with self.assertRaises(frappe.PermissionError):
            frappe.delete_doc(native.NATIVE, name, force=True)
        with self.assertRaises(frappe.PermissionError):
            frappe.get_doc(native.NATIVE, self.parent).db_set("disabled", 1)
        with self.assertRaises(frappe.PermissionError):
            frappe.rename_doc("Company", self.company, "Renamed company", force=True)
        with self.assertRaises(frappe.PermissionError):
            frappe.get_doc(LOCATION, location.id).db_set("native_warehouse", unmanaged.name)
        with self.assertRaises(frappe.PermissionError):
            frappe.delete_doc(LOCATION, location.id)
        self.assertEqual(mapping_records(), before)
        unmanaged.db_set("city", "Synthetic city")
        self.assertEqual(frappe.db.get_value(native.NATIVE, unmanaged.name, "city"), "Synthetic city")

    def test_failure_rolls_back_native_creation_and_explicit_binding(self):
        warehouse = self.warehouse()
        candidate = make_native(self.company, self.parent)
        before, stock_before = mapping_records(), stock_counts()
        original = frappe.get_doc(native.NATIVE, candidate.name).insert

        def fail_after_native_insert(doc, *args, **kwargs):
            original.__func__(doc, *args, **kwargs)
            raise RuntimeError("Synthetic native failure")

        with patch.object(type(candidate), "insert", fail_after_native_insert):
            with self.assertRaisesRegex(RuntimeError, "Synthetic native failure"):
                self.location(warehouse)
            with self.assertRaisesRegex(RuntimeError, "Synthetic native failure"):
                service.provision_system_transit(company=self.company, native_parent=self.parent,
                                                 warehouse_code="TRANSIT", location_code="T", display_name="Transit")
        self.assertEqual(mapping_records(), before)
        controller = type(frappe.get_doc(doctype=LOCATION))
        with patch.object(controller, "insert", side_effect=RuntimeError("Synthetic domain failure")):
            with self.assertRaisesRegex(RuntimeError, "Synthetic domain failure"):
                self.location(warehouse, existing_native=candidate.name)
        self.assertEqual(mapping_records(), before)
        self.assertEqual(stock_counts(), stock_before)

    def test_deactivation_reports_references_and_refuses_empty_locations(self):
        warehouse = self.warehouse()
        location = self.location(warehouse)
        name = service.diagnose_mapping(location.id).native_warehouse
        with self.assertRaisesRegex(frappe.ValidationError, "retained locations"):
            service.deactivate_warehouse(warehouse.id)
        empty_warehouse = self.warehouse("W2")
        with self.assertRaisesRegex(frappe.ValidationError, "complete V1 reference checks"):
            service.deactivate_warehouse(empty_warehouse.id)
        with self.assertRaisesRegex(frappe.ValidationError, "complete V1 reference checks"):
            service.deactivate_location(location.id)
        # Isolated data fixtures for individual reference checks. Integration
        # with all future open V1 document types belongs to V1-07–V1-11.
        for doctype, fields, reason in (
            ("Bin", {"warehouse": name, "actual_qty": 1}, "non-zero stock"),
            ("Stock Ledger Entry", {"warehouse": name, "is_cancelled": 0}, "stock ledger history"),
        ):
            with self.subTest(doctype=doctype):
                fixture = frappe.get_doc(doctype=doctype, name=uuid4().hex, **fields)
                fixture.db_insert()
                with self.assertRaisesRegex(frappe.ValidationError, reason):
                    service.deactivate_location(location.id)
                frappe.db.delete(doctype, {"name": fixture.name})
        if not frappe.db.exists("Stock Entry Type", "Material Receipt"):
            frappe.get_doc(doctype="Stock Entry Type", purpose="Material Receipt").insert(set_name="Material Receipt")
        tag = f"Synthetic-warehouse-{uuid4().hex[:12]}"
        unit = make_uom(tag)
        item = frappe.get_doc(doctype="Item", item_code=tag, item_name=tag, item_group=make_item_group(tag),
                              stock_uom=unit, is_stock_item=1).insert()
        unmanaged = make_native(self.company, self.parent)
        draft = frappe.get_doc(
            doctype="Stock Entry", company=self.company, stock_entry_type="Material Receipt",
            purpose="Material Receipt", items=[{"item_code": item.name, "qty": 1, "t_warehouse": unmanaged.name,
                                                "uom": unit, "stock_uom": unit, "conversion_factor": 1,
                                                "basic_rate": 1}],
        ).insert()
        self.assertEqual(draft.docstatus, 0)
        # Model a draft that predates the V1-05 lifecycle guard.
        frappe.db.set_value("Stock Entry Detail", draft.items[0].name, "t_warehouse", name)
        with self.assertRaisesRegex(frappe.ValidationError, "native reference in Stock Entry Detail"):
            service.deactivate_location(location.id)
        self.assertEqual(service.resolve_location(location.id), location)

    def test_transit_is_protected_in_services_and_ledger_insert(self):
        warehouse, location = service.provision_system_transit(
            company=self.company, native_parent=self.parent, warehouse_code="TRANSIT", location_code="T1",
            display_name="System transit",
        )
        self.assertTrue(warehouse.system_managed)
        self.assertNotIn(warehouse.id, [row.id for row in service.list_warehouses(company=self.company)])
        for action, args in (
            (service.get_warehouse, (warehouse.id,)), (service.list_locations, (warehouse.id,)),
            (service.get_location, (location.id,)), (service.resolve_location, (location.id,)),
            (service.deactivate_location, (location.id,)), (service.deactivate_warehouse, (warehouse.id,)),
        ):
            with self.subTest(action=action), self.assertRaises(frappe.PermissionError):
                action(*args)
        native_name = service.diagnose_mapping(location.id).native_warehouse
        self.assertEqual(frappe.db.get_value(native.NATIVE, native_name, "warehouse_type"), "Transit")
        with self.assertRaises(frappe.PermissionError):
            frappe.rename_doc("Warehouse Type", "Transit", "Renamed transit type", force=True)
        stock_before = stock_counts()
        with self.assertRaisesRegex(frappe.PermissionError, "controlled transfer service"):
            frappe.get_doc(doctype="Stock Entry", to_warehouse=native_name).insert(ignore_permissions=True)
        with self.assertRaisesRegex(frappe.PermissionError, "controlled transfer service"):
            frappe.get_doc(doctype="Stock Ledger Entry", warehouse=native_name).insert(ignore_permissions=True)
        self.assertEqual(stock_counts(), stock_before)

    def test_permissions_allow_only_the_internal_maintenance_path(self):
        warehouse = self.warehouse()
        location = self.location(warehouse)
        for role in ("Item Manager", MAINTAINER):
            user = frappe.get_doc(
                doctype="User", email=f"{uuid4().hex[:12]}@example.invalid", first_name="Synthetic maintainer",
                send_welcome_email=0, roles=[{"role": role}],
            ).insert(ignore_permissions=True).name
            with self.subTest(role=role):
                frappe.set_user(user)
                if role == "Item Manager":
                    with self.assertRaises(frappe.PermissionError):
                        service.get_location(location.id)
                    with self.assertRaises(frappe.PermissionError):
                        self.location(warehouse, "DENIED")
                else:
                    self.assertEqual(service.get_location(location.id), location)
                    self.location(warehouse, "ALLOWED")
                with self.assertRaises(frappe.PermissionError):
                    frappe.get_doc(LOCATION, location.id).db_set("code", "DIRECT")
                with self.assertRaises(frappe.PermissionError):
                    service.provision_system_transit(company=self.company, native_parent=self.parent,
                                                     warehouse_code="T", location_code="T", display_name="Denied")
                self.assertEqual(frappe.session.user, user)
                frappe.set_user("Administrator")

    def test_drift_diagnostics_reject_a_location_with_a_missing_marker(self):
        warehouse = self.warehouse()
        location = self.location(warehouse)
        native_name = service.diagnose_mapping(location.id).native_warehouse
        frappe.db.set_value(native.NATIVE, native_name, native.MARKER, "")
        self.assertEqual(service.diagnose_mapping(location.id).status, "Warehouse Drift")
        with self.assertRaisesRegex(frappe.ValidationError, "mapping is inconsistent"):
            service.resolve_location(location.id)
        with self.assertRaises(frappe.PermissionError):
            frappe.get_doc(native.NATIVE, native_name).db_set("warehouse_name", "Denied")
