from haxpower_v1.access.registry import MAINTENANCE_DOCUMENTS as _MAINTENANCE_DOCUMENTS
from haxpower_v1.access.registry import STOCK_DOCUMENTS as _STOCK_DOCUMENTS

app_name = "haxpower_v1"
app_title = "HaxpowerPlatform"
app_publisher = "HaxpowerPlatform"
app_description = "HaxpowerPlatform V1"
required_apps = ["erpnext"]

auth_hooks = ["haxpower_v1.access.request.enforce_http_boundary"]
before_job = ["haxpower_v1.access.background.guard_queued_native_job"]

after_install = [
    "haxpower_v1.catalog.persistence.ensure_schema",
    "haxpower_v1.access.policy.ensure_schema",
    "haxpower_v1.access.background.stop_native_schedules",
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
    },
    "UOM": _master_events,
    "Item Group": _master_events,
}

_warehouse_adapter = "haxpower_v1.adapters.erpnext.warehouse"
after_install.extend([
    "haxpower_v1.warehouse.persistence.ensure_schema",
    "haxpower_v1.inventory.batch.persistence.ensure_schema",
    "haxpower_v1.inventory.operations.persistence.ensure_schema",
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
})

extend_doctype_class = {
    "Item": [f"{_item_adapter}.ItemLifecycle"],
    "Warehouse": [f"{_warehouse_adapter}.WarehouseLifecycle"],
    "Company": [f"{_warehouse_adapter}.CompanyLifecycle"],
}

_warehouse_persistence = "haxpower_v1.warehouse.persistence"
has_permission = {
    "Item": "haxpower_v1.access.native.native_item_permission",
    "Warehouse": f"{_warehouse_adapter}.native_warehouse_permission",
    "HP V1 Warehouse": f"{_warehouse_persistence}.domain_permission",
    "HP V1 Warehouse Location": f"{_warehouse_persistence}.domain_permission",
}
permission_query_conditions = {
    "Item": "haxpower_v1.access.native.native_item_query",
    "Warehouse": f"{_warehouse_adapter}.native_warehouse_query",
    "HP V1 Warehouse": f"{_warehouse_persistence}.domain_query",
    "HP V1 Warehouse Location": f"{_warehouse_persistence}.domain_query",
}


_access = "haxpower_v1.access.native"
_stock_guard = f"{_access}.guard_stock_document"
for _doctype in _STOCK_DOCUMENTS:
    doc_events[_doctype] = dict.fromkeys((
        "before_insert", "before_validate", "before_submit", "before_cancel",
        "before_update_after_submit", "before_change", "before_discard",
    ), _stock_guard)
    extend_doctype_class[_doctype] = [f"{_access}.StockLifecycle"]
doc_events["Stock Entry"]["before_insert"] = [f"{_warehouse_adapter}.guard_stock_entry", _stock_guard]
doc_events["Stock Ledger Entry"]["before_insert"] = [f"{_warehouse_adapter}.guard_transit_posting", _stock_guard]
doc_events["Batch"]["before_save"] = _stock_guard
extend_doctype_class["Serial and Batch Bundle"].append(
    "haxpower_v1.adapters.erpnext.receipt.ReceiptBundlePermissions"
)

for _doctype in _MAINTENANCE_DOCUMENTS:
    doc_events[_doctype] = dict.fromkeys((
        "before_insert", "before_submit", "before_cancel", "before_change", "before_discard",
    ), f"{_access}.guard_repost")
    extend_doctype_class[_doctype] = [f"{_access}.MaintenanceLifecycle"]

_item_reference_guard = f"{_access}.guard_master_references"
doc_events["Item"]["before_validate"] = [f"{_item_adapter}.guard_item_change", _item_reference_guard]
doc_events["Item"]["before_change"] = [f"{_item_adapter}.guard_item_change", _item_reference_guard]
doc_events["DocShare"] = dict.fromkeys(("before_insert", "before_validate", "before_change"),
                                      f"{_access}.guard_sharing")
