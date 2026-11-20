import frappe
from frappe.tests import IntegrationTestCase

from haxpower_v1.patches.backfill_migration_sample import execute


class TestHPV1MigrationSample(IntegrationTestCase):
    def test_installed_metadata(self):
        self.assertIn("erpnext", frappe.get_installed_apps())
        self.assertEqual(frappe.get_hooks("app_title", app_name="haxpower_v1"), ["HaxpowerPlatform"])
        self.assertEqual(frappe.get_meta("HP V1 Migration Sample").module, "HP V1")

    def test_backfill_keeps_identity_and_existing_revision(self):
        sample = frappe.get_doc(
            doctype="HP V1 Migration Sample",
            sample_id="unit-test-sample",
            sample_text="Synthetic unit test",
        ).insert()
        execute()
        self.assertEqual(sample.reload().baseline_revision, 1)
        frappe.db.set_value(sample.doctype, sample.name, "baseline_revision", 2)
        execute()
        self.assertEqual(sample.reload().baseline_revision, 2)
        self.assertEqual(sample.name, "unit-test-sample")
        self.assertEqual(sample.sample_text, "Synthetic unit test")
