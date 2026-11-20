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
