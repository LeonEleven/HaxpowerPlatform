"""Prepare the isolated Stage 1B site and prove explicit native Batch allocation."""

import json
import sys
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import frappe
from frappe.desk.page.setup_wizard.setup_wizard import setup_complete
from frappe.utils import today
from frappe.utils.scheduler import disable_scheduler


SITE = "haxpower-stage1b.localhost"
BENCH_SITES = Path("/workspace/development/frappe-bench/sites")
COMPANY = "HP Stage 1B Lab"
ROLE = "Haxpower Stage 1B Operator"


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def insert(doctype, **values):
    return frappe.get_doc({"doctype": doctype, **values}).insert()


def configure():
    if not frappe.is_setup_complete():
        year = today()[:4]
        result = setup_complete({
            "language": "English", "country": "China", "currency": "CNY",
            "timezone": "Asia/Shanghai", "company_name": COMPANY, "company_abbr": "H1B",
            "fy_start_date": f"{year}-01-01", "fy_end_date": f"{year}-12-31",
            "chart_of_accounts": "Standard", "domain": "Distribution", "enable_telemetry": 0,
        })
        require(result.get("status") == "ok" and frappe.is_setup_complete(), "Setup incomplete")
    disable_scheduler()
    require(frappe.db.exists("Company", COMPANY) and frappe.db.count("Company") == 1,
            "Unexpected company baseline")
    require(not frappe.db.count("Stock Ledger Entry"), "Existing stock data, stop fixture setup")
    company = frappe.get_doc("Company", COMPANY)
    company.enable_perpetual_inventory = 0
    company.save()
    settings = frappe.get_doc("Stock Settings")
    settings.update({
        "enable_serial_and_batch_no_for_item": 1, "allow_negative_stock": 0,
        "allow_negative_stock_for_batch": 0, "auto_create_serial_and_batch_bundle_for_outward": 0,
    })
    settings.save()
    frappe.db.value_cache.pop("Stock Settings", None)
    if not frappe.db.exists("UOM", "PC"):
        insert("UOM", uom_name="PC", must_be_whole_number=1)
    require(frappe.db.get_value("UOM", "PC", "must_be_whole_number") == 1, "Unexpected PC unit")
    root = frappe.db.get_value("Warehouse", {"company": COMPANY, "is_group": 1}, "name")
    if not frappe.db.exists("Warehouse", "HP1B W1 - H1B"):
        insert("Warehouse", warehouse_name="HP1B W1", company=COMPANY, parent_warehouse=root, is_group=1)
    for location in ("L1", "L2"):
        if not frappe.db.exists("Warehouse", f"HP1B {location} - H1B"):
            insert("Warehouse", warehouse_name=f"HP1B {location}", company=COMPANY,
                   parent_warehouse="HP1B W1 - H1B")
    if not frappe.db.exists("Brand", "HP1B 品牌甲"):
        insert("Brand", brand="HP1B 品牌甲")
    for suffix in ("A", "B"):
        item = f"HP1B-SKU-{suffix}"
        if not frappe.db.exists("Item", item):
            insert("Item", item_code=item, item_name=f"HP1B 虚构喷油嘴 {suffix}",
                   item_group="All Item Groups", stock_uom="PC", brand="HP1B 品牌甲",
                   is_stock_item=1, has_batch_no=1, create_new_batch=0)
    if not frappe.db.exists("Role", ROLE):
        insert("Role", role_name=ROLE, desk_access=1)
    frappe.db.commit()


def native_probe():
    item, warehouse = "HP1B-SKU-A", "HP1B L1 - H1B"
    run = uuid4().hex[:12]
    internal = f"HP-NB-NATIVE-{run}"
    real = f"HP-REAL-NATIVE-{run}"
    require(not frappe.db.count("Stock Ledger Entry"), "Run before the first durable experiment")
    require(not frappe.db.get_single_value("Stock Settings", "auto_create_serial_and_batch_bundle_for_outward"),
            "Automatic allocation must be off")
    try:
        for batch in (internal, real):
            insert("Batch", item=item, batch_id=batch)
        evidence = []
        for purpose, batch, quantity in (
            ("Material Receipt", real, "12"), ("Material Receipt", internal, "15"),
            ("Material Issue", internal, "4"), ("Material Issue", real, "5"),
        ):
            receipt = purpose == "Material Receipt"
            row = {
                "item_code": item, "qty": Decimal(quantity), "uom": "PC",
                "use_serial_batch_fields": 1, "batch_no": batch, "allow_zero_valuation_rate": 1,
                ("t_warehouse" if receipt else "s_warehouse"): warehouse,
            }
            doc = insert("Stock Entry", stock_entry_type=purpose, company=COMPANY, items=[row])
            doc.submit()
            sle = frappe.get_doc("Stock Ledger Entry", {"voucher_no": doc.name})
            bundle = frappe.get_doc("Serial and Batch Bundle", sle.serial_and_batch_bundle)
            signed = Decimal(quantity) * (1 if receipt else -1)
            require(bundle.docstatus == 1 and len(bundle.entries) == 1
                    and bundle.entries[0].batch_no == batch
                    and Decimal(str(bundle.entries[0].qty)) == signed,
                    "Unexpected batch allocation")
            evidence.append({"document": doc.name, "sle": sle.name, "bundle": bundle.name,
                             "batch": batch, "qty": str(signed)})
        print(json.dumps({"native_probe": evidence}))
    finally:
        frappe.db.rollback()
    require(not frappe.db.count("Stock Ledger Entry"), "Native probe did not roll back")


def main():
    require(Path.cwd() == BENCH_SITES, "Wrong Bench directory")
    frappe.init(site=SITE, sites_path=".")
    frappe.connect()
    try:
        require(frappe.conf.db_host == "mariadb" and frappe.conf.db_name == "haxpower_stage1b_lab",
                "Wrong database")
        require(set(frappe.get_installed_apps()) == {"frappe", "erpnext"},
                "This runs before installing the prototype app")
        if sys.argv[1:] == ["configure"]:
            configure()
        elif sys.argv[1:] == ["native"]:
            native_probe()
        else:
            raise SystemExit("Choose configure or native")
    finally:
        frappe.db.rollback()
        frappe.destroy()


if __name__ == "__main__":
    main()
