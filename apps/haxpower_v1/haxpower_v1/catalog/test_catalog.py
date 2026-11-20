"""Real Frappe/MariaDB integration tests using only synthetic product data."""

from dataclasses import replace
from datetime import date
from decimal import Decimal, localcontext
from unittest.mock import patch
from uuid import uuid4

import frappe
from frappe import client
from frappe.model.base_document import get_controller
from frappe.tests import IntegrationTestCase
from frappe.utils import now_datetime

from . import service
from .dto import (
    BOOLEAN, DATE, DECIMAL, INACTIVE, MULTI, MULTI_SELECT, SINGLE, SINGLE_SELECT, TEXT,
    AttributeData, CategoryData, OptionData, OptionSelection, SpecificationData, TemplateData,
)
from .persistence import ATTRIBUTE, AUDIT, CATEGORY, MAINTAINER, OPTION, SPECIFICATION, TEMPLATE, VALUE


class TestCatalog(IntegrationTestCase):
    def setUp(self):
        super().setUp()
        frappe.set_user("Administrator")
        point = f"test_catalog_{uuid4().hex}"
        frappe.db.savepoint(point)
        self.addCleanup(frappe.db.rollback, save_point=point)
        self.addCleanup(frappe.set_user, "Administrator")
        self.marker = f"SYNTHETIC-{uuid4().hex[:12]}"
        self.category = service.save_category(CategoryData(code=self.marker, display_name="Synthetic category"))
        self.spec = SpecificationData(code=self.marker, category=self.category.id, display_name="Synthetic specification")

    def attribute(self, data_type, *, required=False, mode=SINGLE):
        definition = service.save_attribute(AttributeData(
            code=f"{self.marker}-{data_type}", data_type=data_type, display_name=f"Synthetic {data_type}",
        ))
        rule = service.save_template(TemplateData(
            category=self.category.id, attribute=definition.id, is_required=required, value_mode=mode,
        ))
        return definition, rule

    def option(self, definition, index=0):
        return service.save_option(OptionData(
            attribute=definition.id, option_key=f"OPTION-{index:03}", display_name=f"Synthetic option {index}",
            sort_order=index,
        ))

    def user(self, role=None):
        user = frappe.get_doc(
            doctype="User", email=f"{self.marker}-{uuid4().hex[:6]}@example.invalid",
            first_name="Synthetic catalog test", send_welcome_email=0, user_type="System User",
        )
        if role:
            user.append("roles", {"role": role})
        return user.insert(ignore_permissions=True).name

    def test_config_creation_read_edit_and_category_cycle(self):
        child = service.save_category(CategoryData(
            code=f"{self.marker}-CHILD", display_name="Synthetic child", parent_category=self.category.id,
            description="Synthetic description", sort_order=8,
        ))
        self.assertEqual(service.get_category(child.id), child)
        with self.assertRaisesRegex(frappe.ValidationError, "own ancestor"):
            service.save_category(replace(self.category.data, parent_category=child.id), record_id=self.category.id)

        attribute, rule = self.attribute(MULTI_SELECT)
        option = self.option(attribute)
        updated = service.save_option(replace(option.data, display_name="Synthetic label 2"), record_id=option.id)
        self.assertEqual(service.get_option(option.id), updated)
        self.assertEqual(service.get_attribute(attribute.id), attribute)
        updated_rule = service.save_template(
            replace(rule.data, searchable=True, identity_relevant=True, display_order=3, editable_for_technician=True),
            record_id=rule.id,
        )
        self.assertEqual(service.get_category_template(self.category.id), (updated_rule,))
        self.assertTrue(updated_rule.data.editable_for_technician)

    def test_all_six_types_round_trip(self):
        definitions = {kind: self.attribute(kind, required=True, mode=MULTI if kind == MULTI_SELECT else SINGLE)[0]
                       for kind in (TEXT, DECIMAL, BOOLEAN, DATE, SINGLE_SELECT, MULTI_SELECT)}
        single = self.option(definitions[SINGLE_SELECT])
        multi = [self.option(definitions[MULTI_SELECT], index) for index in (1, 2)]
        precise = "123456789012345678901234567890.012345678901234567890123456789"
        with localcontext() as context:
            context.prec = 5
            created = service.create_specification(self.spec, attributes={
                definitions[TEXT].id: ["  合成参数 <Tolerance> & ≥ 2  "],
                definitions[DECIMAL].id: [precise + "00"],
                definitions[BOOLEAN].id: [False],
                definitions[DATE].id: ["2024-02-29"],
                definitions[SINGLE_SELECT].id: [single.id],
                definitions[MULTI_SELECT].id: [multi[1].id, multi[0].id],
            })
        persisted = service.get_specification(created.id)
        self.assertEqual(created, persisted)
        self.assertEqual(persisted.revision, 1)
        values = {attribute.data_type: attribute.values for attribute in persisted.attributes}
        self.assertEqual(values, {
            TEXT: ("合成参数 <Tolerance> & ≥ 2",), DECIMAL: (Decimal(precise),),
            BOOLEAN: (False,), DATE: (date(2024, 2, 29),),
            SINGLE_SELECT: (OptionSelection(single.id, single.data.option_key),),
            MULTI_SELECT: tuple(sorted(OptionSelection(option.id, option.data.option_key) for option in multi)),
        })
        rows = frappe.get_doc(SPECIFICATION, created.id).attribute_values
        decimal_row = next(row for row in rows if row.data_type == DECIMAL)
        self.assertEqual(decimal_row.value_decimal, precise)
        self.assertEqual(service.get_specification_audit(created.id), ())

    def test_invalid_types_required_and_unassigned_attributes(self):
        definitions = {kind: self.attribute(kind, mode=MULTI if kind == MULTI_SELECT else SINGLE)[0]
                       for kind in (TEXT, DECIMAL, BOOLEAN, DATE, SINGLE_SELECT, MULTI_SELECT)}
        for kind, invalid in (
            (TEXT, (7, "  ")),
            (DECIMAL, (0.1, True, "1_000", Decimal("NaN"), "1e500000", "1e-500000")),
            (BOOLEAN, (0, "false")), (DATE, ("2025-02-29", "20240101", now_datetime())),
            (SINGLE_SELECT, (True, "not-an-option")), (MULTI_SELECT, (9, "not-an-option")),
        ):
            for value in invalid:
                with self.subTest(kind=kind, value=value), self.assertRaises(frappe.ValidationError):
                    service.create_specification(self.spec, attributes={definitions[kind].id: [value]})
        with self.assertRaisesRegex(frappe.ValidationError, "must be a sequence"):
            service.create_specification(self.spec, attributes={definitions[TEXT].id: "synthetic"})
        with self.assertRaisesRegex(frappe.ValidationError, "not assigned"):
            service.create_specification(self.spec, attributes={uuid4().hex: ["synthetic"]})
        rule = next(item for item in service.get_category_template(self.category.id)
                    if item.data.attribute == definitions[TEXT].id)
        service.save_template(replace(rule.data, is_required=True), record_id=rule.id)
        with self.assertRaisesRegex(frappe.ValidationError, "is required"):
            service.create_specification(self.spec, attributes={})
        self.assertFalse(frappe.db.exists(SPECIFICATION, {"code": self.spec.code}))

    def test_one_value_limit_cross_attribute_and_scoped_db_keys(self):
        single, _ = self.attribute(SINGLE_SELECT)
        multi, _ = self.attribute(MULTI_SELECT)
        first, second, other = self.option(single), self.option(single, 1), self.option(multi)
        other_second = self.option(multi, 1)
        self.assertEqual(first.data.option_key, other.data.option_key)
        for values, message in (((first.id, second.id), "at most one"), ((other.id,), "does not belong")):
            with self.assertRaisesRegex(frappe.ValidationError, message):
                service.create_specification(self.spec, attributes={single.id: values})
        with self.assertRaisesRegex(frappe.ValidationError, "at most one"):
            service.create_specification(self.spec, attributes={multi.id: [other.id, other_second.id]})

        # Probe the actual database constraint independently of the service's preflight validation.
        duplicate = frappe.get_doc(OPTION, first.id).as_dict()
        duplicate["name"] = uuid4().hex
        row = frappe.get_doc(duplicate)
        with self.assertRaises(frappe.UniqueValidationError):
            row.db_insert()

        rule = service.get_category_template(self.category.id)[0]
        duplicate = frappe.get_doc(TEMPLATE, rule.id).as_dict()
        duplicate["name"] = uuid4().hex
        with self.assertRaises(frappe.UniqueValidationError):
            frappe.get_doc(duplicate).db_insert()

        for doctype, record_id in ((CATEGORY, self.category.id), (ATTRIBUTE, single.id)):
            duplicate = frappe.get_doc(doctype, record_id).as_dict()
            duplicate["name"] = uuid4().hex
            with self.assertRaises(frappe.UniqueValidationError):
                frappe.get_doc(duplicate).db_insert()

    def test_over_100_options_ordering_and_duplicate_rejection(self):
        definition, _ = self.attribute(MULTI_SELECT, mode=MULTI)
        options = [self.option(definition, index) for index in range(105)]
        self.assertEqual(service.list_options(definition.id), tuple(options))
        first, last = options[0], options[-1]
        with self.assertRaisesRegex(frappe.ValidationError, "duplicate values"):
            service.create_specification(self.spec, attributes={definition.id: [last.id, last.id]})
        created = service.create_specification(self.spec, attributes={definition.id: [last.id, first.id]})
        self.assertEqual(service.get_specification(created.id), created)
        reordered = service.edit_specification(
            created.id, self.spec, expected_revision=1, attributes={definition.id: [first.id, last.id]},
        )
        self.assertEqual(reordered, created)
        self.assertEqual(service.get_specification_audit(created.id), ())

    def test_inactive_options_can_only_be_retained_on_original_attribute(self):
        definition, _ = self.attribute(MULTI_SELECT, mode=MULTI)
        old, active, never_selected = (self.option(definition, index) for index in range(3))
        original = service.create_specification(self.spec, attributes={definition.id: [old.id]})
        for option in (old, never_selected):
            service.save_option(replace(option.data, status=INACTIVE), record_id=option.id)
        self.assertEqual(service.get_specification(original.id), original)
        self.assertEqual(service.list_options(definition.id), (active,))
        self.assertEqual(len(service.list_options(definition.id, include_inactive=True)), 3)
        retained = service.edit_specification(
            original.id, self.spec, expected_revision=1, attributes={definition.id: [old.id]},
        )
        self.assertEqual(retained, original)
        with self.assertRaisesRegex(frappe.ValidationError, "is inactive"):
            service.create_specification(replace(self.spec, code=self.spec.code + "-2"), attributes={definition.id: [old.id]})
        with self.assertRaisesRegex(frappe.ValidationError, "is inactive"):
            service.edit_specification(
                original.id, self.spec, expected_revision=1, attributes={definition.id: [old.id, never_selected.id]},
            )
        changed = service.edit_specification(
            original.id, self.spec, expected_revision=1, attributes={definition.id: [old.id, active.id]},
        )
        removed = service.edit_specification(
            original.id, self.spec, expected_revision=changed.revision, attributes={definition.id: [active.id]},
        )
        with self.assertRaisesRegex(frappe.ValidationError, "is inactive"):
            service.edit_specification(
                original.id, self.spec, expected_revision=removed.revision, attributes={definition.id: [old.id]},
            )

    def test_definition_and_template_changes_preserve_existing_records(self):
        multi, rule = self.attribute(MULTI_SELECT, mode=MULTI)
        one, two = self.option(multi), self.option(multi, 1)
        original = service.create_specification(self.spec, attributes={multi.id: [one.id, two.id]})
        other_category = service.save_category(CategoryData(code=f"{self.marker}-OTHER", display_name="Synthetic other"))
        other_attribute, other_rule = self.attribute(TEXT)

        for change, message in (
            (lambda: service.save_attribute(replace(multi.data, data_type=TEXT), record_id=multi.id), "attribute in use"),
            (lambda: service.save_option(replace(one.data, option_key="CHANGED"), record_id=one.id), "immutable"),
            (lambda: service.save_template(replace(rule.data, category=other_category.id), record_id=rule.id), "immutable"),
            (lambda: service.save_template(replace(rule.data, value_mode=SINGLE), record_id=rule.id), "multiple values"),
            (lambda: service.edit_specification(
                original.id, replace(self.spec, category=other_category.id), expected_revision=1,
            ), "category .* immutable"),
        ):
            with self.assertRaisesRegex(frappe.ValidationError, message):
                change()

        self.option(multi, 2)
        service.save_template(replace(other_rule.data, is_required=True), record_id=other_rule.id)
        self.assertEqual(service.get_specification(original.id), original)
        self.assertEqual(service.get_specification_audit(original.id), ())
        edited = service.edit_specification(
            original.id, replace(self.spec, display_name="Synthetic metadata edit"), expected_revision=1,
        )
        self.assertEqual(edited.attributes, original.attributes)
        with self.assertRaisesRegex(frappe.ValidationError, "is required"):
            service.edit_specification(
                original.id, edited.data, expected_revision=edited.revision, attributes={multi.id: [one.id]},
            )
        service.save_attribute(replace(other_attribute.data, status=INACTIVE), record_id=other_attribute.id)
        service.edit_specification(
            original.id, edited.data, expected_revision=edited.revision, attributes={multi.id: [one.id]},
        )

    def test_explicit_edit_audits_before_after_actor_time_and_operation(self):
        text, _ = self.attribute(TEXT)
        multi, _ = self.attribute(MULTI_SELECT, mode=MULTI)
        one, two = self.option(multi), self.option(multi, 1)
        before_text = "Before <Tolerance> & ≥ 2"
        original = service.create_specification(self.spec, attributes={text.id: [before_text], multi.id: [one.id]})
        actor = self.user(MAINTAINER)
        frappe.set_user(actor)
        earliest = now_datetime()
        updated = service.edit_specification(
            original.id, self.spec, expected_revision=1, attributes={multi.id: [two.id, one.id]},
        )
        latest = now_datetime()
        audit = service.get_specification_audit(original.id)
        self.assertEqual(len(audit), 2)
        self.assertEqual(len({event.operation_id for event in audit}), 1)
        self.assertEqual({event.actor for event in audit}, {actor})
        self.assertTrue(all(earliest <= event.occurred_at <= latest for event in audit))
        self.assertTrue(all(event.specification_revision == 2 and event.specification_id == original.id for event in audit))
        events = {event.before.attribute_id: event for event in audit}
        self.assertEqual(events[text.id].before.values, (before_text,))
        self.assertEqual(events[text.id].after.values, ())
        self.assertEqual(events[multi.id].before.values, (OptionSelection(one.id, one.data.option_key),))
        self.assertEqual(events[multi.id].after, updated.attributes[0])

        with self.assertRaisesRegex(frappe.ValidationError, "revision has changed"):
            service.edit_specification(original.id, self.spec, expected_revision=1, attributes={})
        service.edit_specification(original.id, self.spec, expected_revision=2, attributes={text.id: ["Later"]})
        self.assertEqual(service.get_specification_audit(original.id)[:2], audit)
        with self.assertRaises(frappe.PermissionError):
            frappe.get_doc(AUDIT, audit[0].id).save(ignore_permissions=True)
        with self.assertRaises(frappe.PermissionError):
            frappe.delete_doc(AUDIT, audit[0].id, ignore_permissions=True)

    def test_validation_and_audit_failure_roll_back_values_metadata_and_history(self):
        text, _ = self.attribute(TEXT)
        boolean, _ = self.attribute(BOOLEAN)
        original = service.create_specification(self.spec, attributes={text.id: ["Before"], boolean.id: [False]})
        existing_rows = [row.name for row in frappe.get_doc(SPECIFICATION, original.id).attribute_values]
        with self.assertRaises(frappe.ValidationError):
            service.edit_specification(
                original.id, self.spec, expected_revision=1, attributes={text.id: ["After"], boolean.id: ["true"]},
            )
        self.assertEqual(service.get_specification(original.id), original)
        audit_class = get_controller(AUDIT)
        real_insert = audit_class.insert
        writes = []

        def fail_on_second_row(record, *args, **kwargs):
            if writes:
                raise RuntimeError("Synthetic audit failure")
            writes.append(real_insert(record, *args, **kwargs).name)
            return record

        with patch.object(audit_class, "insert", fail_on_second_row):
            with self.assertRaisesRegex(RuntimeError, "Synthetic audit failure"):
                service.edit_specification(
                    original.id, replace(self.spec, display_name="Synthetic edit"), expected_revision=1,
                    attributes={text.id: ["After"], boolean.id: [True]},
                )
        self.assertEqual(len(writes), 1)
        self.assertEqual(service.get_specification(original.id), original)
        self.assertEqual([row.name for row in frappe.get_doc(SPECIFICATION, original.id).attribute_values], existing_rows)
        self.assertEqual(service.get_specification_audit(original.id), ())

    def test_identical_specs_keep_separate_ids_and_duplicate_codes_never_replace(self):
        text, _ = self.attribute(TEXT)
        values = {text.id: ["Synthetic value"]}
        first = service.create_specification(self.spec, attributes=values)
        second = service.create_specification(replace(self.spec, code=self.spec.code + "-2"), attributes=values)
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(first.attributes, second.attributes)
        with self.assertRaises(frappe.UniqueValidationError):
            service.create_specification(self.spec, attributes={text.id: ["Different value"]})
        self.assertEqual(service.get_specification(first.id), first)

    def test_non_maintenance_user_and_native_doc_writes_are_rejected(self):
        text, _ = self.attribute(TEXT)
        original = service.create_specification(self.spec, attributes={text.id: ["Synthetic value"]})
        child_name = frappe.get_doc(SPECIFICATION, original.id).attribute_values[0].name
        for user in (self.user(), self.user(MAINTAINER)):
            with self.subTest(user=user):
                frappe.set_user(user)
                if MAINTAINER not in frappe.get_roles():
                    with self.assertRaises(frappe.PermissionError):
                        service.create_specification(self.spec, attributes={})
                    with self.assertRaises(frappe.PermissionError):
                        service.edit_specification(original.id, self.spec, expected_revision=1)
                    with self.assertRaises(frappe.PermissionError):
                        service.get_specification(original.id)
                for native_write in (
                    lambda: client.insert({"doctype": SPECIFICATION, "code": "SYNTHETIC-NATIVE"}),
                    lambda: client.set_value(SPECIFICATION, original.id, "display_name", "Synthetic native edit"),
                    lambda: client.set_value(VALUE, child_name, "value_text", "Synthetic child edit"),
                    lambda: client.delete(SPECIFICATION, original.id),
                ):
                    with self.assertRaises(frappe.PermissionError):
                        native_write()
        frappe.set_user("Administrator")
        with self.assertRaises(frappe.PermissionError):
            frappe.get_doc(SPECIFICATION, original.id).save()
        self.assertEqual(service.get_specification(original.id), original)
        self.assertEqual(service.get_specification_audit(original.id), ())
        self.assertFalse(frappe.db.exists(SPECIFICATION, {"code": "SYNTHETIC-NATIVE"}))
