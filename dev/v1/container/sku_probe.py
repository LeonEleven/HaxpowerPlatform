"""Persistent synthetic SKU evidence for the disposable V1 upgrade site."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier

import frappe

from haxpower_v1.adapters.erpnext import item
from haxpower_v1.catalog import service, sku
from haxpower_v1.catalog.sku_dto import Alias, BrandData, Identifier, SKUData
from haxpower_v1.catalog.sku_persistence import ALIAS, BRAND, IDENTIFIER, SKU
from haxpower_v1.catalog.test_sku import NO_STOCK_WRITES, make_item_group, make_uom

from catalog_probe import SAMPLE_CODE, SPECIFICATION
from identity import require

DOCTYPES = (BRAND, SKU, ALIAS, IDENTIFIER)
PREFIX = "SYNTHETIC-V1-SKU"
NATIVE = f"{PREFIX}-NATIVE"


def snapshot():
    tables = {
        doctype: {
            "schema": frappe.db.sql(f"SHOW CREATE TABLE `tab{doctype}`")[0][1],
            "rows": frappe.get_all(doctype, fields=["*"], order_by="name"),
        } if frappe.db.table_exists(doctype) else None
        for doctype in DOCTYPES
    }
    items = []
    marker = frappe.db.has_column("Item", item.SKU_MARKER)
    if marker and tables[SKU]:
        items = [
            frappe.get_doc("Item", name).as_dict()
            for name in frappe.get_all(SKU, pluck="native_item", order_by="native_item")
        ]
    return {"tables": tables, "native_items": items, "marker": marker}


def stock_counts():
    return {doctype: frappe.db.count(doctype) for doctype in NO_STOCK_WRITES}


def seed():
    frappe.set_user("Administrator")
    before = stock_counts()
    spec_id = frappe.db.get_value(SPECIFICATION, {"code": SAMPLE_CODE}, "name")
    require(spec_id, "missing specification from the V1-02 upgrade fixture")
    spec = service.get_specification(spec_id)
    require(spec.revision == 2 and len(service.get_specification_audit(spec.id)) == 1,
            "V1-02 specification history is missing")
    uom = make_uom(PREFIX)
    group = make_item_group(PREFIX)
    frappe.conf[item.SETTINGS] = {"item_group": group, "uom_precision": {uom: 3}}
    for suffix in ("A", "B"):
        code = f"{PREFIX}-{suffix}"
        brand = sku.save_brand(BrandData(code=code, display_name=f"合成 Synthetic Brand {suffix}"))
        result = sku.create_sku(
            SKUData(code=code, specification=spec.id, brand=brand.id, display_name="合成 Synthetic valve",
                    stock_uom=uom, quantity_precision=3),
            identifiers=[Identifier(kind="OEM", value=f"{PREFIX}-SHARED")],
            aliases=[Alias(value=f"合成别名 {suffix}", language="zh")],
        )
        require(sku.diagnose_item_mapping(code).sku_id == result.id, "synthetic mapping failed")
    require(len(sku.search_skus(f"{PREFIX}-SHARED")) == 2, "shared synthetic OEM search did not match both brands")
    require(stock_counts() == before, "SKU seeding created stock or accounting records")
    frappe.db.commit()
    print("Two synthetic brands, stable SKU IDs and independent ERPNext Items verified.")


def verify_concurrency(site):
    frappe.set_user("Administrator")
    before = stock_counts()
    template, = sku.search_skus(f"{PREFIX}-A", limit=1)
    settings = {"item_group": PREFIX, "uom_precision": {PREFIX: 3}}
    frappe.get_doc(
        doctype="Item", item_code=NATIVE, item_name=template.data.display_name,
        stock_uom=PREFIX, item_group=PREFIX, is_stock_item=1, standard_rate=0, opening_stock=0,
    ).insert()
    frappe.db.commit()

    def race(first, second, *, native=None, expected_error):
        ready = Barrier(2, timeout=30)

        def attempt(code):
            frappe.init(site=site, sites_path=str(Path.cwd()))
            frappe.connect()
            try:
                frappe.set_user("Administrator")
                frappe.conf[item.SETTINGS] = settings
                connection, = frappe.db.sql("SELECT CONNECTION_ID()")[0]
                ready.wait()
                try:
                    result = sku.create_sku(replace(template.data, code=code), existing_item=native)
                    frappe.db.commit()
                    return connection, result.id
                except (frappe.ValidationError, frappe.UniqueValidationError) as error:
                    frappe.db.rollback()
                    require(expected_error in str(error), f"unexpected SKU race failure: {error}")
                    return connection, None
            finally:
                frappe.destroy()

        with ThreadPoolExecutor(max_workers=2) as executor:
            attempts = [executor.submit(attempt, code) for code in (first, second)]
            outcomes = [attempt.result(timeout=60) for attempt in attempts]
        require(len({connection for connection, _ in outcomes}) == 2, "SKU race reused a database connection")
        winners = [result for _, result in outcomes if result]
        require(len(winners) == 1, "SKU race must have exactly one committed winner")
        frappe.db.rollback()
        target = native or first.upper()
        result = sku.diagnose_item_mapping(target)
        require(result.sku_id == result.marker == winners[0] and result.status == "Verified",
                "concurrent mapping could not be verified from a third transaction")
        require(frappe.db.count(SKU, {"code": ("in", [first.upper(), second.upper()])}) == 1,
                "rejected SKU race left a domain row")
        require(frappe.db.count("Item", {item.SKU_MARKER: winners[0]}) == 1,
                "SKU race left an extra Item marker")
        print(f"Two independent MariaDB connections verified one committed winner for {expected_error}.")

    code = f"{PREFIX}-RACE"
    race(code, code.lower(), expected_error="SKU code already exists")
    race(f"{PREFIX}-MAP-A", f"{PREFIX}-MAP-B", native=NATIVE, expected_error="Item is already mapped")
    require(stock_counts() == before, "concurrent SKU writes created stock or accounting records")
