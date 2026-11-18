app_name = "haxpower_platform"
app_title = "Haxpower Platform"
app_publisher = "HaxpowerPlatform"
app_description = "Stage 1B lab only"
app_email = "experiment@example.invalid"
required_apps = ["erpnext"]

extend_doctype_class = {
    "Serial and Batch Bundle": ["haxpower_platform.stock.BundlePermissionMixin"],
}

doc_events = {
    "Batch": dict.fromkeys(
        ("before_insert", "before_validate", "before_rename", "on_trash"),
        "haxpower_platform.stock.protect_batch",
    ),
    "Stock Entry": dict.fromkeys(
        ("before_validate", "before_submit", "before_cancel", "on_trash"),
        "haxpower_platform.stock.protect_inventory",
    ),
    "Serial and Batch Bundle": dict.fromkeys(
        ("before_validate", "before_submit", "before_cancel", "on_trash"),
        "haxpower_platform.stock.protect_inventory",
    ),
    "Stock Ledger Entry": {
        "before_insert": "haxpower_platform.stock.protect_inventory",
    },
}
