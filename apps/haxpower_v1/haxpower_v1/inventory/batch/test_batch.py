"""Integration coverage using the locked ERPNext and MariaDB site."""

from dataclasses import asdict, replace
import re
from unittest.mock import patch
from uuid import uuid4

import frappe
from frappe.model.base_document import get_controller
from frappe.tests import IntegrationTestCase

from ...access.execution import allows_native_write
from ...adapters.erpnext import batch as native, item
from ...catalog import service as catalog, sku
from ...catalog.dto import CategoryData, SpecificationData
from ...catalog.sku_dto import BrandData, SKUData
from ...catalog.test_sku import NO_STOCK_WRITES, make_item_group, make_uom
from .authorization import POLICY_SETTING
from .dto import BatchError, MappingStatus, NormalizationStatus
from .normalization import CANDIDATE_VERSION
from .persistence import MAPPING
from .service import get_batch, preview_selection, resolve_existing, resolve_or_create

NO_BATCH = {"mode": "NO_BATCH"}


def real(number):
    return {"mode": "BATCH", "batchNo": number}


class TestBatch(IntegrationTestCase):
    def setUp(self):
        super().setUp()
        point = f"test_batch_{uuid4().hex}"
        frappe.db.savepoint(point)
        self.addCleanup(frappe.db.rollback, save_point=point)
        self.addCleanup(frappe.set_user, frappe.session.user)
        frappe.set_user("Administrator")
        frappe.db.set_single_value("Stock Settings", "enable_serial_and_batch_no_for_item", 1)
        code = f"SYNTHETIC-BATCH-{uuid4().hex[:12].upper()}"
        uom, group = make_uom(code), make_item_group(code)
        self.enterContext(patch.dict(frappe.conf, {
            POLICY_SETTING: CANDIDATE_VERSION,
            item.SETTINGS: {"item_group": group, "uom_precision": {uom: 3}},
        }))
        category = catalog.save_category(CategoryData(code=code, display_name="Synthetic category"))
        spec = catalog.create_fixture_specification(
            SpecificationData(code=code, category=category.id, display_name="Synthetic specification"),
            attributes={},
        )
        brand = sku.save_brand(BrandData(code=code, display_name="Synthetic brand"))
        data = SKUData(code=code, specification=spec.id, brand=brand.id,
                       display_name="Synthetic SKU", stock_uom=uom, quantity_precision=3)
        self.first = sku.create_fixture_sku(data)
        self.second = sku.create_fixture_sku(replace(data, code=f"{code}-2"))
        self.native_items = (self.first.data.code, self.second.data.code)

    def stock_tables(self):
        return {table: frappe.db.count(table) for table in NO_STOCK_WRITES if table != "Batch"}

    def test_real_and_no_batch_are_independent_and_reused(self):
        before = self.stock_tables()
        self.assertEqual(frappe.db.get_value("Item", self.native_items[0], "has_batch_no"), 0)
        with self.assertRaisesRegex(BatchError, "^BATCH_MAPPING_UNAVAILABLE$"):
            resolve_existing(self.first.id, NO_BATCH)
        bindings = [(self.first, real("STD61")), (self.first, real("STD62")), (self.first, NO_BATCH),
                    (self.second, real("STD61")), (self.second, NO_BATCH)]
        seen = set()
        for stock_item, selection in bindings:
            result = resolve_or_create(stock_item.id, selection)
            self.assertTrue(result.created)
            native_name = native.resolve_native_batch(stock_item.id, result.batch.id)
            seen.add(native_name)
            self.assertEqual(result.batch.batch, selection.get("batchNo"))
            self.assertEqual(result.batch, get_batch(stock_item.id, result.batch.id))
            self.assertNotIn(native_name, str(asdict(result)))
            self.assertEqual(frappe.db.get_value("Batch", native_name, "item"), stock_item.data.code)
            self.assertEqual(frappe.db.get_value(MAPPING, result.batch.id, "owner"), "Administrator")
            modified = frappe.db.get_value("Item", stock_item.data.code, "modified")
            replay = resolve_or_create(stock_item.id, selection)
            self.assertEqual(replay, replace(result, created=False))
            self.assertEqual(resolve_existing(stock_item.id, selection), replay)
            self.assertEqual(frappe.db.get_value("Item", stock_item.data.code, "modified"), modified)
            other = self.second if stock_item == self.first else self.first
            with self.assertRaisesRegex(BatchError, "^BATCH_MAPPING_UNAVAILABLE$"):
                get_batch(other.id, result.batch.id)
            with self.assertRaisesRegex(BatchError, "^BATCH_MAPPING_UNAVAILABLE$"):
                get_batch(stock_item.id, native_name)
        self.assertEqual(len(seen), len(bindings))
        self.assertEqual(sku.get_sku(self.first.id), self.first)
        self.assertEqual(sku.get_sku(self.second.id), self.second)
        for name in self.native_items:
            self.assertEqual(frappe.db.get_value("Item", name, ("has_batch_no", "create_new_batch")), (1, 0))
        self.assertEqual(self.stock_tables(), before)

    def test_db_collation_and_exact_identity(self):
        collation, = frappe.db.sql("""
            SELECT COLLATION_NAME FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = 'normalized_key'
        """, ("tab" + MAPPING,))[0]
        self.assertTrue(re.fullmatch(r"utf8mb4_[a-z0-9_]+", collation))
        sql_comparison, = frappe.db.sql(
            f"SELECT CONVERT('STD61' USING utf8mb4) COLLATE {collation} = 'Std61'",
        )[0]
        self.assertEqual(sql_comparison, 1)
        samples = ("STD61", "Std61", "ＳＴＤ６１", "STD 61", "STD  61", "STD\u300061", '"STD61"',
                   "É", "E", "E\u0301", "A" * 80)
        results = [resolve_or_create(self.first.id, real(text)).batch for text in samples]
        self.assertEqual([result.batch for result in results], list(samples))
        self.assertEqual(len({result.id for result in results}), len(samples))
        first = frappe.get_doc(MAPPING, results[0].id).as_dict()
        no_batch = resolve_or_create(self.first.id, NO_BATCH).batch
        none = frappe.get_doc(MAPPING, no_batch.id).as_dict()
        duplicates = (
            dict(first, name=uuid4().hex, native_batch="synthetic-other-native"),
            dict(first, name=uuid4().hex, sku=self.second.id),
            dict(none, name=uuid4().hex, native_batch="synthetic-other-native"),
        )
        for row in duplicates:
            with self.assertRaises(frappe.UniqueValidationError):
                frappe.get_doc(row).db_insert()
        for key in (None, "", "alternate"):
            with self.assertRaises(Exception) as error:
                frappe.get_doc(dict(none, name=uuid4().hex, normalized_key=key,
                                    native_batch="synthetic-other-native")).db_insert()
            self.assertEqual(error.exception.args[0], 4025)

    def test_normalization_preview_confirmation_and_collision(self):
        original = "\u3000STD61\u00a0"
        selection = real(original)
        preview = preview_selection(self.first.id, selection).normalization
        self.assertEqual((preview.original, preview.candidate, preview.status),
                         (original, "STD61", NormalizationStatus.CONFIRMATION_REQUIRED))
        with self.assertRaisesRegex(BatchError, "^BATCH_NORMALIZATION_CONFIRMATION_REQUIRED$") as error:
            resolve_or_create(self.first.id, selection, confirmation=replace(preview, policy_version="old"))
        self.assertEqual(error.exception.normalization, preview)
        self.assertEqual(frappe.db.count(MAPPING, {"sku": self.first.id}), 0)
        bound = resolve_or_create(self.first.id, selection, confirmation=preview)
        self.assertEqual(bound.batch.batch, original)
        self.assertEqual(resolve_or_create(self.first.id, selection, confirmation=preview).batch, bound.batch)
        for text in ("STD61", " STD61"):
            alternate = preview_selection(self.first.id, real(text)).normalization
            self.assertEqual(alternate.status, NormalizationStatus.COLLISION)
            with self.assertRaisesRegex(BatchError, "^BATCH_NORMALIZATION_COLLISION$") as error:
                resolve_or_create(self.first.id, real(text), confirmation=alternate)
            self.assertEqual(error.exception.normalization, alternate)
        self.assertEqual(frappe.db.count(MAPPING, {"sku": self.first.id}), 1)

    def test_bad_selection_and_policy_gate_leave_item_and_mapping_unchanged(self):
        invalid = (None, "", {}, {"batchNo": "STD61"}, {"mode": "UNRECOGNIZED"}, {"mode": "NO_BATCH", "batchNo": None},
                   {"mode": "NO_BATCH", "label": "STD61"}, real(""), real(None), real("\u3000\u00a0"),
                   real("''"), real("‘ ’"), real("＂　＂"), real("「 」"), real("HP-NB-one"), real("hP-rB-two"),
                   real("ＨＰ－ＮＢ－three"), real("A\x00"), real("A\u200b"), real("A\t"), real("<A>"), real("A" * 81))
        for selection in invalid:
            with self.subTest(selection=selection), self.assertRaises(BatchError):
                resolve_or_create(self.first.id, selection)
        with patch.dict(frappe.conf, {POLICY_SETTING: "approved"}):
            for selection in (NO_BATCH, real("STD61")):
                with self.assertRaisesRegex(BatchError, "^BATCH_POLICY_NOT_APPROVED$"):
                    resolve_or_create(self.first.id, selection)
        self.assertEqual(frappe.db.get_value("Item", self.native_items[0], "has_batch_no"), 0)
        self.assertEqual(frappe.db.count(MAPPING, {"sku": self.first.id}), 0)

    def test_item_configuration_and_existing_stock_block_enablement(self):
        item_name = self.native_items[0]
        frappe.db.set_single_value("Stock Settings", "enable_serial_and_batch_no_for_item", 0)
        with self.assertRaisesRegex(BatchError, "^BATCH_ITEM_INCOMPATIBLE$"):
            resolve_or_create(self.first.id, NO_BATCH)
        frappe.db.set_single_value("Stock Settings", "enable_serial_and_batch_no_for_item", 1)
        record = frappe.get_doc(doctype="Bin", name=uuid4().hex, item_code=item_name, actual_qty=1)
        # Synthetic database fixture for preexisting stock. The production write path stays closed.
        record.db_insert()
        with self.assertRaisesRegex(BatchError, "^BATCH_ITEM_INCOMPATIBLE$"):
            resolve_or_create(self.first.id, real("STD61"))
        frappe.db.delete("Bin", {"name": record.name})
        frappe.db.set_value("Item", item_name, "create_new_batch", 1)
        with self.assertRaisesRegex(BatchError, "^BATCH_ITEM_INCOMPATIBLE$"):
            resolve_or_create(self.first.id, NO_BATCH)
        self.assertEqual(frappe.db.get_value("Item", item_name, "has_batch_no"), 0)
        self.assertEqual(frappe.db.count(MAPPING, {"sku": self.first.id}), 0)

    def test_native_or_domain_failure_rolls_back_and_restores_context(self):
        for doctype, method in (("Batch", "before_save"), (MAPPING, "before_insert")):
            marker = object()
            previous_search = frappe.conf.get("disable_global_search", marker)
            touched = []

            def inject(doc):
                native_name = doc.native_batch if doctype == MAPPING else doc.batch_id
                touched.append(native_name)
                self.assertEqual(bool(frappe.db.exists("Batch", native_name)), doctype == MAPPING)
                raise RuntimeError(f"synthetic failure {native_name}")

            with patch.object(get_controller(doctype), method, inject):
                with self.assertRaisesRegex(BatchError, "^BATCH_PERSISTENCE_FAILED$"):
                    resolve_or_create(self.first.id, NO_BATCH)
            native_name, = touched
            self.assertIs(frappe.conf.get("disable_global_search", marker), previous_search)
            self.assertFalse(allows_native_write(native.BATCH_SERVICE, native_name, native_name[6:],
                                                existing=False, sku_id=self.first.id))
            self.assertEqual(frappe.db.count(MAPPING, {"sku": self.first.id}), 0)
            self.assertFalse(frappe.db.exists("Batch", native_name))
            self.assertEqual(frappe.db.get_value("Item", self.native_items[0], "has_batch_no"), 0)
            self.assertNotIn(native_name, str(frappe.local.message_log))
        self.assertTrue(resolve_or_create(self.first.id, NO_BATCH).created)

    def test_diagnostics_reject_native_and_domain_drift(self):
        result = resolve_or_create(self.first.id, real("STD61"))
        native_name = native.resolve_native_batch(self.first.id, result.batch.id)

        def verify(status):
            self.assertIn(status, {row.status for row in native.diagnose_mappings()
                                   if row.batch_id == result.batch.id})
            with self.assertRaisesRegex(BatchError, "^BATCH_MAPPING_UNAVAILABLE$"):
                resolve_or_create(self.first.id, real("STD61"))
            with self.assertRaisesRegex(BatchError, "^BATCH_MAPPING_UNAVAILABLE$"):
                get_batch(self.first.id, result.batch.id)

        frappe.db.set_value("Batch", native_name, "item", self.native_items[1])
        verify(MappingStatus.WRONG_ITEM)
        frappe.db.set_value("Batch", native_name, {"item": self.native_items[0], "disabled": 1})
        verify(MappingStatus.INACTIVE)
        frappe.db.set_value("Batch", native_name, {"disabled": 0, "batch_id": "unexpected"})
        verify(MappingStatus.RESERVED_NAME_CONFLICT)
        frappe.db.set_value("Batch", native_name, "batch_id", native_name)
        frappe.db.set_value("Item", self.native_items[0], "has_batch_no", 0)
        verify(MappingStatus.INCOMPATIBLE_ITEM)
        frappe.db.set_value("Item", self.native_items[0], "has_batch_no", 1)
        frappe.db.set_value(MAPPING, result.batch.id, "original_text", "inconsistent")
        verify(MappingStatus.NORMALIZATION_CONFLICT)
        frappe.db.set_value(MAPPING, result.batch.id, "original_text", "STD61")
        orphan = frappe.get_doc(doctype="Batch", name="HP-RB-synthetic-orphan",
                                batch_id="HP-RB-synthetic-orphan", item=self.native_items[0])
        orphan.db_insert()
        statuses = {row.status for row in native.diagnose_mappings() if row.native_batch == orphan.name}
        self.assertEqual(statuses, {MappingStatus.ORPHAN_BATCH, MappingStatus.RESERVED_NAME_CONFLICT})
        frappe.db.delete("Batch", {"name": native_name})
        verify(MappingStatus.MISSING_BATCH)
        self.assertEqual(frappe.db.count(MAPPING, {"sku": self.first.id}), 1)

    def test_orm_denies_direct_batch_item_and_mapping_changes(self):
        result = resolve_or_create(self.first.id, NO_BATCH)
        native_name = native.resolve_native_batch(self.first.id, result.batch.id)
        for batch_id in ("synthetic-unreserved", "hp-rb-synthetic"):
            with self.assertRaises(frappe.PermissionError):
                frappe.get_doc(doctype="Batch", batch_id=batch_id, item=self.native_items[0]).insert()
        unmanaged = frappe.get_doc(
            doctype="Item", item_code=f"{self.native_items[0]}-UNMANAGED",
            item_group=frappe.db.get_value("Item", self.native_items[0], "item_group"),
            stock_uom=self.first.data.stock_uom, has_batch_no=1, create_new_batch=0, is_stock_item=1,
        ).insert().name
        with self.assertRaises(frappe.PermissionError):
            frappe.get_doc(doctype="Batch", batch_id="hP-Nb-synthetic-unmanaged", item=unmanaged).insert()
        doc = frappe.get_doc("Batch", native_name)
        doc.item = self.native_items[1]
        with self.assertRaises(frappe.PermissionError):
            doc.save()
        with self.assertRaises(frappe.PermissionError):
            frappe.get_doc("Batch", native_name).db_set("disabled", 1)
        with self.assertRaises(frappe.PermissionError):
            frappe.delete_doc("Batch", native_name)
        with self.assertRaises(frappe.PermissionError):
            frappe.rename_doc("Batch", native_name, "synthetic-rename", force=True)
        with self.assertRaises(frappe.PermissionError):
            frappe.get_doc(MAPPING, result.batch.id).db_set("status", "Inactive")
        with self.assertRaises(frappe.PermissionError):
            frappe.get_doc("Item", self.native_items[0]).update({"has_batch_no": 0}).save()
        user = frappe.get_doc(
            doctype="User", email=f"batch-{uuid4().hex}@example.invalid", first_name="Synthetic employee",
            send_welcome_email=0, roles=[{"role": "Stock Manager"}],
        ).insert().name
        frappe.set_user(user)
        for operation, args in (
            (get_batch, (self.first.id, result.batch.id)), (preview_selection, (self.first.id, NO_BATCH)),
            (resolve_or_create, (self.first.id, NO_BATCH)), (resolve_existing, (self.first.id, NO_BATCH)),
            (native.diagnose_mappings, ()),
        ):
            with self.assertRaises(frappe.PermissionError):
                operation(*args)
