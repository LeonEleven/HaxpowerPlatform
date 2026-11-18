app_name = "haxpower_platform"
app_title = "Haxpower Platform"
app_publisher = "HaxpowerPlatform"
app_description = "Haxpower isolated inventory lab"
app_email = "experiment@example.invalid"
required_apps = ["erpnext"]

extend_doctype_class = {
    "Serial and Batch Bundle": ["haxpower_platform.stock.BundlePermissionMixin"],
}

inventory_events = ("before_validate", "before_submit", "before_update_after_submit", "before_cancel", "on_trash")

doc_events = {
    "Batch": dict.fromkeys(
        ("before_insert", "before_validate", "before_rename", "on_trash"),
        "haxpower_platform.stock.protect_batch",
    ),
    "Stock Entry": dict.fromkeys(
        inventory_events,
        "haxpower_platform.stock.protect_inventory",
    ),
    "Stock Reconciliation": dict.fromkeys(
        inventory_events,
        "haxpower_platform.stock.protect_inventory",
    ),
    "Serial and Batch Bundle": dict.fromkeys(
        inventory_events,
        "haxpower_platform.stock.protect_inventory",
    ),
    "Stock Ledger Entry": dict.fromkeys(
        ("before_insert", *inventory_events), "haxpower_platform.stock.protect_inventory",
    ),
}
