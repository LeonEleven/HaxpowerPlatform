app_name = "haxpower_v1"
app_title = "HaxpowerPlatform"
app_publisher = "HaxpowerPlatform"
app_description = "HaxpowerPlatform V1"
required_apps = ["erpnext"]

after_install = [
    "haxpower_v1.catalog.persistence.ensure_schema",
    "haxpower_v1.adapters.erpnext.item.ensure_schema",
]
after_migrate = after_install

_item_adapter = "haxpower_v1.adapters.erpnext.item"
_master_events = {
    "before_validate": f"{_item_adapter}.guard_master_change",
    "before_change": f"{_item_adapter}.guard_master_change",
    "before_rename": f"{_item_adapter}.guard_master_rename",
    "on_trash": f"{_item_adapter}.guard_master_trash",
}

doc_events = {
    "Item": {
        "before_validate": f"{_item_adapter}.guard_item_change",
        "before_save": f"{_item_adapter}.guard_item_change",
        "before_change": f"{_item_adapter}.guard_item_change",
        "before_rename": f"{_item_adapter}.guard_item_rename",
        "on_trash": f"{_item_adapter}.guard_item_trash",
    },
    "UOM": _master_events,
    "Item Group": _master_events,
}

_warehouse_adapter = "haxpower_v1.adapters.erpnext.warehouse"
after_install.extend([
    "haxpower_v1.warehouse.persistence.ensure_schema",
    f"{_warehouse_adapter}.ensure_schema",
])

doc_events.update({
    "Warehouse": {
        "before_validate": f"{_warehouse_adapter}.guard_change",
        "before_change": f"{_warehouse_adapter}.guard_change",
        "before_rename": f"{_warehouse_adapter}.guard_rename",
    },
    "Company": {
        "before_validate": f"{_warehouse_adapter}.guard_company_change",
        "before_change": f"{_warehouse_adapter}.guard_company_change",
        "before_rename": f"{_warehouse_adapter}.guard_company_identity",
    },
    "Warehouse Type": {
        "before_rename": f"{_warehouse_adapter}.guard_warehouse_type_identity",
        "on_trash": f"{_warehouse_adapter}.guard_warehouse_type_trash",
    },
    "DocShare": {
        "before_validate": f"{_warehouse_adapter}.guard_sharing",
        "before_change": f"{_warehouse_adapter}.guard_sharing",
    },
    "Stock Entry": {"before_validate": f"{_warehouse_adapter}.guard_stock_entry"},
    "Stock Ledger Entry": {"before_insert": f"{_warehouse_adapter}.guard_transit_posting"},
})

extend_doctype_class = {
    "Warehouse": [f"{_warehouse_adapter}.WarehouseLifecycle"],
    "Company": [f"{_warehouse_adapter}.CompanyLifecycle"],
}

_warehouse_persistence = "haxpower_v1.warehouse.persistence"
has_permission = {
    "Warehouse": f"{_warehouse_adapter}.native_warehouse_permission",
    "HP V1 Warehouse": f"{_warehouse_persistence}.domain_permission",
    "HP V1 Warehouse Location": f"{_warehouse_persistence}.domain_permission",
}
permission_query_conditions = {
    "Warehouse": f"{_warehouse_adapter}.native_warehouse_query",
    "HP V1 Warehouse": f"{_warehouse_persistence}.domain_query",
    "HP V1 Warehouse Location": f"{_warehouse_persistence}.domain_query",
}
