from unittest.mock import patch
from uuid import uuid4

import frappe
from frappe.tests import IntegrationTestCase

from ..adapters.erpnext import warehouse as native
from ..warehouse import service
from ..warehouse.test_warehouse import make_company, stock_counts
from . import api, execution
from .config import set_scope
from .policy import (
    SCOPE, Mode, Operation, authorized_warehouse_ids, granted_operations, require_location_access, require_operation,
    require_transfer_access,
)


class TestAccess(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        frappe.set_user("Administrator")
        cls.company, cls.parent = make_company(uuid4().hex[:5].upper())
        cls.other_company, cls.other_parent = make_company(uuid4().hex[:5].upper())

    def setUp(self):
        super().setUp()
        frappe.set_user("Administrator")
        point = f"test_access_{uuid4().hex}"
        frappe.db.savepoint(point)
        self.addCleanup(frappe.db.rollback, save_point=point)
        self.addCleanup(frappe.set_user, "Administrator")
        self.w1 = self.warehouse("W1")
        self.w2 = self.warehouse("W2")

    def warehouse(self, code, *, other=False):
        return service.create_warehouse(
            company=self.other_company if other else self.company,
            native_parent=self.other_parent if other else self.parent, code=code, display_name=code,
        )

    def actor(self, *operations, mode=None, selected=(), enabled=1):
        user = frappe.get_doc(
            doctype="User", email=f"access-{uuid4().hex[:12]}@example.invalid", first_name="Synthetic access",
            user_type="System User", send_welcome_email=0, enabled=enabled,
            roles=[{"role": operation.role} for operation in operations],
        ).insert(ignore_permissions=True)
        if mode:
            set_scope(user.name, self.company, mode, selected)
        return user.name

    def test_operation_union_and_scope_are_independent(self):
        user = self.actor(Operation.WAREHOUSE_READ, Operation.TRANSFER, Operation.APPROVE,
                          mode=Mode.SELECTED, selected=[self.w1.id])
        frappe.set_user(user)
        self.assertEqual(granted_operations(), {Operation.WAREHOUSE_READ, Operation.TRANSFER, Operation.APPROVE})
        for operation in (Operation.WAREHOUSE_READ, Operation.TRANSFER, Operation.APPROVE):
            self.assertEqual(authorized_warehouse_ids(operation), (self.w1.id,))
        for operation in (Operation.PRODUCT_MAINTAIN, Operation.COUNT, Operation.EXPORT):
            with self.assertRaises(frappe.PermissionError):
                require_operation(operation)
        with self.assertRaises(frappe.PermissionError):
            set_scope(user, self.company, Mode.ALL)

    def test_all_tracks_additions_within_company_and_selected_does_not(self):
        all_user = self.actor(Operation.WAREHOUSE_READ, mode=Mode.ALL)
        selected = self.actor(Operation.WAREHOUSE_READ, mode=Mode.SELECTED, selected=[self.w1.id])
        w3 = self.warehouse("W3")
        other = self.warehouse("W-OTHER", other=True)
        frappe.set_user(all_user)
        self.assertEqual(authorized_warehouse_ids(Operation.WAREHOUSE_READ), (self.w1.id, self.w2.id, w3.id))
        frappe.set_user("Administrator")
        w4 = self.warehouse("W4")
        frappe.set_user(all_user)
        self.assertEqual(authorized_warehouse_ids(Operation.WAREHOUSE_READ), (self.w1.id, self.w2.id, w3.id, w4.id))
        with self.assertRaises(frappe.PermissionError):
            api.get_warehouse(other.id)
        frappe.set_user(selected)
        self.assertEqual(api.list_warehouses(limit=1), {
            "rows": [{"id": self.w1.id, "code": "W1", "display_name": "W1"}], "total": 1,
        })
        self.assertEqual(api.list_warehouses(query="W4"), {"rows": [], "total": 0})

    def test_missing_none_and_empty_selected_deny_detail(self):
        users = [self.actor(Operation.WAREHOUSE_READ, mode=mode) for mode in (None, Mode.NONE, Mode.SELECTED)]
        for user in users:
            frappe.set_user(user)
            self.assertEqual(api.list_warehouses(), {"rows": [], "total": 0})
            with self.assertRaises(frappe.PermissionError):
                api.get_warehouse(self.w1.id)

    def test_both_transfer_sides_and_the_operation_are_required(self):
        for ids in ([], [self.w1.id], [self.w2.id], [self.w1.id, self.w2.id]):
            frappe.set_user("Administrator")
            user = self.actor(Operation.TRANSFER, mode=Mode.SELECTED, selected=ids)
            frappe.set_user(user)
            if len(ids) == 2:
                require_transfer_access(self.w1.id, self.w2.id)
                require_transfer_access(self.w2.id, self.w1.id)
                with self.assertRaises(frappe.PermissionError):
                    require_transfer_access(self.w1.id, self.w1.id)
            else:
                with self.assertRaises(frappe.PermissionError):
                    require_transfer_access(self.w1.id, self.w2.id)
        frappe.set_user("Administrator")
        user = self.actor(Operation.WAREHOUSE_READ, mode=Mode.ALL)
        frappe.set_user(user)
        with self.assertRaises(frappe.PermissionError):
            require_transfer_access(self.w1.id, self.w2.id)

    def test_location_filter_excludes_transit_and_drift_before_counting(self):
        visible = service.create_location(self.w1.id, code="A1", display_name="Physical")
        drift = service.create_location(self.w1.id, code="A2", display_name="Drift")
        leaf = service.diagnose_mapping(drift.id).native_warehouse
        _, transit = service.provision_system_transit(
            company=self.company, native_parent=self.parent, warehouse_code="T", location_code="T1", display_name="Transit",
        )
        inactive = service.create_location(self.w1.id, code="A3", display_name="Inactive")
        frappe.db.set_value("Warehouse", leaf, native.MARKER, "")
        frappe.db.set_value("HP V1 Warehouse Location", inactive.id, "status", "Inactive")
        user = self.actor(Operation.WAREHOUSE_READ, mode=Mode.ALL)
        frappe.set_user(user)
        self.assertEqual(api.list_locations(self.w1.id, offset=1), {"rows": [], "total": 1})
        self.assertEqual(api.get_location(visible.id)["warehouse_id"], self.w1.id)
        for inaccessible in (transit.id, drift.id, inactive.id, uuid4().hex):
            with self.assertRaises(frappe.PermissionError):
                require_location_access(inaccessible, Operation.WAREHOUSE_READ)

    def test_disabled_and_revoked_grants_are_checked_again(self):
        user = self.actor(Operation.INVENTORY_READ, mode=Mode.ALL)
        frappe.set_user(user)
        require_operation(Operation.INVENTORY_READ)
        frappe.set_user("Administrator")
        frappe.db.delete("Has Role", {"parenttype": "User", "parent": user, "role": Operation.INVENTORY_READ.role})
        frappe.set_user(user)
        with self.assertRaises(frappe.PermissionError):
            require_operation(Operation.INVENTORY_READ)
        frappe.set_user("Administrator")
        frappe.db.set_value("User", user, "enabled", 0)
        for actor in (user, "Guest"):
            frappe.set_user(actor)
            with self.assertRaises(frappe.PermissionError):
                granted_operations()

    def test_scope_validation_and_private_execution_rollback(self):
        other = self.warehouse("OTHER", other=True)
        user = self.actor(Operation.WAREHOUSE_READ, mode=Mode.SELECTED, selected=[self.w1.id])
        for invalid in ([other.id], [self.w1.id, self.w1.id], [self.parent]):
            with self.assertRaises(frappe.ValidationError):
                set_scope(user, self.company, Mode.SELECTED, invalid)
        scope_name = frappe.db.get_value(SCOPE, {"user": user}, "name")
        scope = frappe.get_doc(SCOPE, scope_name)
        with patch.object(type(scope), "update_children", side_effect=RuntimeError("Synthetic child failure")):
            with self.assertRaisesRegex(RuntimeError, "Synthetic child failure"):
                set_scope(user, self.company, Mode.NONE)
        restored = frappe.get_doc(SCOPE, scope_name)
        self.assertEqual((restored.mode, [row.warehouse for row in restored.warehouses]), (Mode.SELECTED, [self.w1.id]))
        before = stock_counts()
        # An exception must remove all trust state and restore configuration.
        frappe.set_user(user)
        previous = dict(frappe.conf)
        args = ("Synthetic", self.parent, self.w1.id)
        with self.assertRaisesRegex(RuntimeError, "Synthetic failure"):
            with execution.native_write(
                lambda: require_operation(Operation.WAREHOUSE_READ),
                service=args[0], native_name=args[1], domain_id=args[2], existing=True,
            ):
                self.assertTrue(execution.allows_native_write(*args, existing=True))
                self.assertEqual(frappe.session.user, user)
                raise RuntimeError("Synthetic failure")
        self.assertFalse(execution.allows_native_write(*args, existing=True))
        self.assertEqual(dict(frappe.conf), previous)
        self.assertEqual(stock_counts(), before)

    def test_managed_warehouse_rejects_stock_orm_writes_before_posting(self):
        location = service.create_location(self.w1.id, code="A1", display_name="Physical")
        leaf = service.diagnose_mapping(location.id).native_warehouse
        baseline = stock_counts()
        documents = (
            frappe.get_doc(doctype="Stock Entry", to_warehouse=leaf),
            frappe.get_doc(doctype="Stock Reconciliation", items=[{"warehouse": leaf}]),
            frappe.get_doc(doctype="Purchase Receipt", set_warehouse=leaf),
            frappe.get_doc(doctype="Sales Invoice", set_warehouse=leaf, update_stock=1),
            frappe.get_doc(doctype="Stock Ledger Entry", warehouse=leaf),
            frappe.get_doc(doctype="Bin", warehouse=leaf),
            frappe.get_doc(doctype="Batch", batch_id="HP-NB-SYNTHETIC"),
        )
        for doc in documents:
            with self.subTest(doctype=doc.doctype), self.assertRaises(frappe.PermissionError):
                doc.insert(ignore_permissions=True)
        self.assertEqual(stock_counts(), baseline)

    def test_disallowed_native_jobs_stop_before_execution(self):
        from frappe.utils.background_jobs import execute_job

        from .background import JOB, RUN_DOCUMENT, RUN_SCHEDULED
        from .registry import DISABLED_SCHEDULED_JOBS

        jobs = frappe.get_all(
            JOB, filters={"method": ("in", DISABLED_SCHEDULED_JOBS)}, fields=["name", "method", "stopped"],
        )
        self.assertEqual({job.method for job in jobs if job.stopped}, set(DISABLED_SCHEDULED_JOBS))
        previous = getattr(frappe.local, "job", None)
        try:
            for method, kwargs in (
                (RUN_SCHEDULED, {"scheduled_job_type": jobs[0].name}),
                (jobs[0].method, {}),
                (RUN_DOCUMENT, {"doctype": "Transaction Deletion Record", "name": "Synthetic", "doc_method": "execute_task"}),
            ):
                with self.subTest(method=method), self.assertRaises(frappe.PermissionError):
                    execute_job(frappe.local.site, method, None, "Synthetic V1 job", kwargs, is_async=False)
        finally:
            if previous is None:
                del frappe.local.job
            else:
                frappe.local.job = previous
