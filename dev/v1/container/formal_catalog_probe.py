"""V1-16A formal identities, grants and stock compatibility on disposable sites."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

import frappe

from identity import require

UPGRADE_CODE = "P999999"
PENDING_CODE = "P999998"
BASELINE_IDENTIFIERS = ("OLD-SKU-001", r'"\u003c"')
LEGACY_READER = "v1-legacy-catalog-reader@example.invalid"


def seed_upgrade():
    """Run against V1-15 to record formal-looking legacy codes without rewriting identities."""
    from haxpower_v1.adapters.erpnext import item
    from haxpower_v1.catalog import service, sku
    from haxpower_v1.catalog.dto import SpecificationData
    from haxpower_v1.catalog.sku_dto import Identifier, SKUData
    from haxpower_v1.catalog.sku_persistence import SKU
    from sku_probe import PREFIX

    base = frappe.get_doc(SKU, frappe.db.get_value(SKU, {"code": f"{PREFIX}-A"}))
    category = service.get_specification(base.specification).data.category
    for code in (UPGRADE_CODE, PENDING_CODE):
        spec = service.create_specification(
            SpecificationData(code=code, category=category, display_name=f"Synthetic upgrade {code}"), attributes={},
        )
        if code == UPGRADE_CODE:
            with patch.dict(frappe.conf, {item.SETTINGS: {
                "item_group": base.native_item_group, "uom_precision": {base.stock_uom: base.quantity_precision},
            }}):
                sku.create_sku(SKUData(code=f"{code}-999", specification=spec.id, brand=base.brand,
                                       display_name="Synthetic reserved suffix", stock_uom=base.stock_uom,
                                       quantity_precision=base.quantity_precision),
                               identifiers=[Identifier(kind="LEGACY", value=value, source="Synthetic V1-15")
                                            for value in BASELINE_IDENTIFIERS])
    actor = frappe.get_doc(
        doctype="User", email="v1-legacy-catalog@example.invalid", first_name="Synthetic legacy maintenance",
        send_welcome_email=0, roles=[{"role": "HP V1 Catalog Maintainer"}],
    ).insert(ignore_permissions=True)
    require(actor.name == "v1-legacy-catalog@example.invalid", "historical role fixture missing")
    from haxpower_v1.access.policy import Operation
    from haxpower_v1.reporting import exports

    frappe.get_doc(
        doctype="User", email=LEGACY_READER, first_name="Synthetic legacy workbook reader",
        send_welcome_email=0, roles=[{"role": Operation.PRODUCT_READ.role}, {"role": Operation.EXPORT.role}],
    ).insert(ignore_permissions=True)
    frappe.db.commit()
    frappe.set_user(LEGACY_READER)
    try:
        workbook = exports.generate("products", {"sku_code": UPGRADE_CODE + "-999"})
        require(workbook["status"] == "Ready" and workbook["rows"] == 1, "V1-15 workbook fixture did not generate")
    finally:
        frappe.set_user("Administrator")


def denied(exception, operation, message=""):
    try:
        operation()
    except exception as error:
        require(not message or message in str(error), f"missing diagnostic: {message}")
    else:
        raise RuntimeError(f"expected {exception.__name__}: {message}")


def verify_upgrade():
    from haxpower_v1.adapters.erpnext import item
    from haxpower_v1.catalog import migration, service, sku
    from haxpower_v1.catalog.dto import HistoricalIdentifier, SpecificationData
    from haxpower_v1.catalog.persistence import SPECIFICATION
    from haxpower_v1.catalog.sku_dto import Identifier, SKUData
    from haxpower_v1.catalog.sku_persistence import SKU
    from haxpower_v1.access.policy import Operation
    from haxpower_v1.reporting import exports

    original_file = frappe.db.get_value(exports.EXPORT, {"requested_by": LEGACY_READER})
    require(original_file, "V1-15 workbook metadata was not preserved")
    frappe.set_user(LEGACY_READER)
    try:
        require(exports.download(original_file)[1].startswith(b"PK"),
                "V1-15 workbook was unreadable after V1-16A upgrade")
    finally:
        frappe.set_user("Administrator")
    frappe.db.delete("Has Role", {"parent": LEGACY_READER, "role": Operation.PRODUCT_READ.role})
    frappe.db.commit()
    frappe.set_user(LEGACY_READER)
    try:
        denied(frappe.PermissionError, lambda: exports.status(original_file))
        denied(frappe.PermissionError, lambda: exports.download(original_file))
    finally:
        frappe.set_user("Administrator")

    existing = frappe.get_doc(SKU, frappe.db.get_value(SKU, {"code": UPGRADE_CODE + "-999"}))
    require(tuple(row.value for row in sku.get_sku(existing.name).identifiers) == BASELINE_IDENTIFIERS,
            "V1-15 LEGACY identifiers changed during schema upgrade")
    for value in BASELINE_IDENTIFIERS:
        require(sku.search_sku_ids(value) == ((existing.name,), 1), "V1-15 LEGACY search changed during upgrade")
    pending = service.get_specification(frappe.db.get_value(SPECIFICATION, {"code": PENDING_CODE}))
    active = service.get_specification(existing.specification)
    require(active.data.stock_uom == existing.stock_uom and pending.data.stock_uom in (None, ""),
            "migration did not backfill exactly the known UOM")
    require(migration.legacy_maintenance_assignments() == ("v1-legacy-catalog@example.invalid",),
            "legacy assignment not available for review")
    frappe.set_user("v1-legacy-catalog@example.invalid")
    try:
        denied(frappe.PermissionError, lambda: service.get_specification(active.id))
    finally:
        frappe.set_user("Administrator")

    with patch.dict(frappe.conf, {item.SETTINGS: {
        "item_group": existing.native_item_group, "uom_precision": {existing.stock_uom: existing.quantity_precision},
    }}):
        data = SKUData(specification=pending.id, brand=existing.brand, display_name="Synthetic pending unit")
        denied(frappe.ValidationError, lambda: sku.create_sku(data), "pending stock UOM")
        proposed = service.create_specification(
            SpecificationData(category=active.data.category, display_name="Synthetic expanded number",
                              stock_uom=existing.stock_uom), attributes={},
            historical_identifiers=[HistoricalIdentifier(source="Synthetic V1-16A", value="001<spec>")],
        )
        require(proposed.data.code == "P1000000", "global numbering did not cross the six-digit boundary")
        new = sku.create_sku(
            replace(data, specification=active.id),
            identifiers=[Identifier(kind="LEGACY", value='001<legacy>\\-"', source="Synthetic V1-16A")],
        )
        require(new.data.code == UPGRADE_CODE + "-1000", "SKU numbering did not cross the three-digit boundary")
        fixed = service.configure_pending_specification_unit(
            pending.id, existing.stock_uom, expected_revision=pending.revision,
        )
        require(sku.create_sku(replace(data, specification=fixed.id)).data.code == PENDING_CODE + "-001",
                "configured historical formal code did not receive its own sequence")

    frappe.db.savepoint("formal_unit_conflict")
    try:
        frappe.db.set_value(SKU, new.id, "quantity_precision", existing.quantity_precision + 1)
        denied(frappe.ValidationError, migration.reconcile, "conflicting SKU stock units or precision")
    finally:
        frappe.db.rollback(save_point="formal_unit_conflict")
    migration.reconcile()
    frappe.db.commit()
    print("V1-16A upgrade reserved six/three-digit boundaries, preserved legacy grants and recovered from unit conflict.",
          flush=True)


def _ensure_native_units():
    from haxpower_v1.catalog.test_sku import make_uom

    for unit in ("PC", "SET"):
        native = frappe.db.get_value("UOM", unit, "name")
        if native and native != unit:
            temporary = f"SYNTHETIC-UNIT-{unit}"
            frappe.rename_doc("UOM", native, temporary, force=True)
            frappe.rename_doc("UOM", temporary, unit, force=True)
        if native:
            doc = frappe.get_doc("UOM", unit)
            doc.update(enabled=1, must_be_whole_number=1)
            doc.save()
        else:
            make_uom(unit, whole=True)


def _number_race(site, name, data, settings):
    from haxpower_v1.adapters.erpnext import item
    from haxpower_v1.catalog import service, sku

    barrier = Barrier(2, timeout=30)

    def create():
        frappe.init(site=site, sites_path=str(Path.cwd()))
        frappe.connect()
        try:
            frappe.conf[item.SETTINGS] = settings
            connection_id, = frappe.db.sql("SELECT CONNECTION_ID()")[0]
            frappe.db.get_value("HP V1 Product Category", {}, "name")
            barrier.wait()
            spec = service.create_specification(name, attributes={})
            product = sku.create_sku(replace(data, specification=spec.id))
            shared = sku.create_sku(data)
            frappe.db.commit()
            return connection_id, spec.data.code, product.data.code, shared.data.code
        finally:
            frappe.destroy()

    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = [pool.submit(create) for _ in range(2)]
        first, second = [entry.result(timeout=60) for entry in pending]
    require(first[0] != second[0] and first[1] != second[1] and first[3] != second[3],
            "parallel catalog allocation reused a connection or code")
    require(all(outcome[2] == f"{outcome[1]}-001" for outcome in (first, second)),
            "SKU sequence crossed independent specifications")


def run(site):
    from haxpower_v1.access.policy import Operation
    from haxpower_v1.adapters.erpnext import item
    from haxpower_v1.catalog import initial_configuration, numbers, service, sku
    from haxpower_v1.catalog.dto import CategoryData, SpecificationData
    from haxpower_v1.catalog.persistence import NUMBER, SPECIFICATION
    from haxpower_v1.catalog.sku_dto import SKUData
    from haxpower_v1.catalog.sku_persistence import SKU
    from haxpower_v1.inventory.batch import service as batches
    from haxpower_v1.inventory.batch.authorization import POLICY_SETTING
    from haxpower_v1.inventory.batch.dto import BatchError
    from haxpower_v1.inventory.batch.normalization import CANDIDATE_VERSION, FORMAL_VERSION, normalize_selection
    from haxpower_v1.inventory.batch.persistence import MAPPING
    from access_probe import synthetic_user
    from sku_probe import PREFIX, stock_counts

    _ensure_native_units()
    settings = {"item_group": PREFIX, "uom_precision": {"PC": 0, "SET": 0}}
    frappe.conf[item.SETTINGS] = settings
    before = stock_counts()
    initialized = initial_configuration.initialize()
    categories = initialized["HP V1 Product Category"]
    attributes = initialized["HP V1 Attribute Definition"]
    brands = initialized["HP V1 Brand"]
    templates = initialized["HP V1 Category Attribute"]
    require((len(categories), len(attributes), len(brands), len(templates)) == (13, 23, 2, 90),
            "initial approved configuration is incomplete")
    category = service.get_category(categories["0011"])
    service.save_category(replace(category.data, description="Synthetic manual change"), record_id=category.id)
    require(initial_configuration.initialize() == initialized
            and service.get_category(category.id).data.description == "Synthetic manual change",
            "configuration replay replaced a manual edit")

    token = uuid4().hex[:10]
    product_role = synthetic_user(token, "product-maintenance", Operation.PRODUCT_MAINTAIN)[0]
    parameter_role = synthetic_user(token, "parameter-maintenance", Operation.PARAMETER_MAINTAIN)[0]
    dual = synthetic_user(token, "combined-maintenance", Operation.PRODUCT_MAINTAIN, Operation.PARAMETER_MAINTAIN)[0]
    data = SpecificationData(category=category.id, display_name="Synthetic formal specification", stock_uom="PC")
    frappe.set_user(product_role)
    try:
        require(service.get_category_template(category.id), "product role cannot read enrollment inputs")
        denied(frappe.PermissionError, lambda: service.save_category(category.data, record_id=category.id))
        denied(frappe.PermissionError,
               lambda: service.create_fixture_specification(replace(data, code="SYNTHETIC-BYPASS"), attributes={}))
        denied(frappe.ValidationError, lambda: service.create_specification(
            data, attributes={attributes["ALIAS"]: [{"create": "Synthetic bypass"}]},
        ))
        first = service.create_specification(data, attributes={})
        denied(frappe.ValidationError,
               lambda: service.edit_specification(first.id, replace(first.data, code="P800000"), expected_revision=1),
               "immutable")
    finally:
        frappe.set_user("Administrator")
    frappe.set_user(parameter_role)
    try:
        denied(frappe.PermissionError, lambda: service.create_specification(data, attributes={}))
        denied(frappe.PermissionError, lambda: service.get_specification(first.id))
        require(initial_configuration.initialize() == initialized, "parameter role cannot replay approved configuration")
    finally:
        frappe.set_user("Administrator")
    frappe.set_user(dual)
    try:
        service.save_category(CategoryData(code=f"SYNTHETIC-{token}", display_name="Synthetic dual grant"))
        second = service.create_specification(replace(data, stock_uom="SET"), attributes={})
    finally:
        frappe.set_user("Administrator")
    frappe.db.delete("Has Role", {"parent": product_role, "role": Operation.PRODUCT_MAINTAIN.role})
    frappe.set_user(product_role)
    try:
        denied(frappe.PermissionError, lambda: service.create_specification(data, attributes={}))
    finally:
        frappe.set_user("Administrator")

    sku_data = SKUData(specification=first.id, brand=brands["UNBRANDED"], display_name="Synthetic formal SKU")
    denied(frappe.ValidationError, lambda: sku.create_sku(replace(sku_data, stock_uom="SET")), "specification")
    with patch.object(item, "bind_item", side_effect=frappe.ValidationError("Synthetic native creation failure")):
        denied(frappe.ValidationError, lambda: sku.create_sku(sku_data), "Synthetic native creation failure")
    saved = sku.create_sku(sku_data)
    other = sku.create_sku(replace(sku_data, brand=brands["UNKNOWN-BRAND"]))
    set_sku = sku.create_sku(replace(sku_data, specification=second.id))
    require((saved.data.code, other.data.code, set_sku.data.code) ==
            (f"{first.data.code}-001", f"{first.data.code}-002", f"{second.data.code}-001"),
            "rollback or per-specification sequence changed the issued number")
    mappings = [frappe.db.get_value(SKU, value.id, "native_item") for value in (saved, other, set_sku)]
    require(mappings == [f"{item.INTERNAL_PREFIX}{value.id}" for value in (saved, other, set_sku)],
            "native identities were coupled to business codes")
    frappe.db.savepoint("formal_number_collision")
    try:
        frappe.db.set_value(NUMBER, numbers.SPEC_SEQUENCE, "last_value", str(numbers.spec_serial(first.data.code) - 1))
        denied(frappe.UniqueValidationError, lambda: service.create_specification(data, attributes={}), "conflicts")
    finally:
        frappe.db.rollback(save_point="formal_number_collision")

    legacy = service.create_fixture_specification(
        replace(data, code="SYNTHETIC-FORMAL-PENDING", stock_uom=None), attributes={},
    )
    denied(frappe.ValidationError, lambda: sku.create_sku(replace(sku_data, specification=legacy.id)), "pending")
    legacy = service.configure_pending_specification_unit(legacy.id, "PC", expected_revision=legacy.revision)
    denied(frappe.ValidationError, lambda: sku.create_sku(replace(sku_data, specification=legacy.id)), "disposition")
    require(frappe.db.get_value(SPECIFICATION, first.id, "quantity_precision") == 0 and stock_counts() == before,
            "catalog maintenance created inventory or changed its declared precision")
    frappe.db.commit()
    _number_race(site, data, sku_data, settings)

    # Exercise both stored policy versions on one exact identity and across SKUs.
    selection = {"mode": "BATCH", "batchNo": "001 Ab Ｃ"}
    with patch.dict(frappe.conf, {POLICY_SETTING: CANDIDATE_VERSION}):
        candidate = batches.resolve_or_create(saved.id, selection)
    with patch.dict(frappe.conf, {POLICY_SETTING: FORMAL_VERSION}):
        same = batches.resolve_or_create(saved.id, selection)
        distinct = batches.resolve_or_create(other.id, selection)
        raw = {"mode": "BATCH", "batchNo": " 001 Ab Ｃ "}
        denied(BatchError, lambda: batches.resolve_or_create(saved.id, raw), "BATCH_NORMALIZATION_COLLISION")
        preview = batches.preview_selection(other.id, {"mode": "BATCH", "batchNo": " Plain "})
        confirmed = batches.resolve_or_create(
            other.id, {"mode": "BATCH", "batchNo": " Plain "}, confirmation=preview.normalization,
        )
        require(batches.resolve_or_create(other.id, {"mode": "BATCH", "batchNo": "Plain"}).batch == confirmed.batch,
                "formal canonical input could not reuse a confirmed batch")
    require(same.batch == candidate.batch and not same.created and distinct.batch != same.batch
            and frappe.db.get_value(MAPPING, candidate.batch.id, "policy_version") == CANDIDATE_VERSION
            and frappe.db.get_value(MAPPING, distinct.batch.id, "policy_version") == FORMAL_VERSION,
            "formal batch compatibility rewrote or duplicated a candidate mapping")
    equivalents = ("001 Ab Ｃ", "001 ab Ｃ", "001 Ab C", "01 Ab Ｃ", "001  Ab Ｃ")
    candidates = [normalize_selection({"mode": "BATCH", "batchNo": text}).normalization.candidate
                  for text in equivalents]
    require(tuple(candidates) == equivalents, "formal policy folded batch distinctions")
    boundary = normalize_selection({"mode": "BATCH", "batchNo": " " * 176 + "Z" * 80})
    require(len(boundary.normalization.candidate) == 80, "valid formal batch length boundary rejected")
    for invalid in ("", "''", "hp-NB-ab", "\tx", "<x", "Z" * 81, " " * 256 + "X"):
        denied(BatchError, lambda: normalize_selection({"mode": "BATCH", "batchNo": invalid}))
    frappe.db.commit()

    _native_boundaries(saved.id, set_sku.id)
    print("V1-16A formal configuration, permissions, concurrent numbering, Batch identity and native quantity passed.",
          flush=True)


def _native_boundaries(pc_sku, set_sku):
    from haxpower_v1.inventory.receipt import service as receipts
    from haxpower_v1.inventory.receipt.dto import ReceiptDraftInput, ReceiptLineInput, SubmitReceipt
    from approval_probe import Fixture
    from receipt_probe import NO_BATCH, checked_result, fresh
    from stock_probe import actor_session, expect

    fixture = Fixture()
    fixture.toggle(False)
    for unit, sku in (("PC", pc_sku), ("SET", set_sku)):
        line = ReceiptLineInput(sku, fixture.source, NO_BATCH, "100000", unit)
        payload = ReceiptDraftInput(fixture.company, (line,), "Synthetic formal UOM boundary")
        before = fresh()
        with actor_session(fixture.operator, fixture.company), receipts.synthetic_candidate(
            fixture.company, limits={unit: "999999"},
        ):
            for quantity, error in (("100000.001", "STOCK_QUANTITY_PRECISION"), ("100001", "STOCK_QUANTITY_LIMIT")):
                expect(error, lambda: receipts.create_draft(replace(payload, lines=(replace(line, quantity=quantity),))))
            draft = receipts.create_draft(payload)
            result = receipts.submit(SubmitReceipt(draft, uuid4().hex))
        checked_result(result, draft, before, fixture.operator)
