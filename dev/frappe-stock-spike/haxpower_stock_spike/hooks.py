app_name = "haxpower_stock_spike"
app_title = "Haxpower Stock Spike"
app_publisher = "HaxpowerPlatform"
app_description = "Stage 2A inventory experiment"
app_email = "experiment@example.invalid"
required_apps = ["frappe"]
before_install = "haxpower_stock_spike.store.require_site"
after_install = "haxpower_stock_spike.store.install_constraints"

# Business tables can only be written through the inventory service.
doc_events = {
    doctype: dict.fromkeys(
        ("before_insert", "before_save", "before_rename", "on_trash"),
        "haxpower_stock_spike.store.refuse_document_write",
    )
    for doctype in ("HP Spike Balance", "HP Spike Operation", "HP Spike Event", "HP Spike Transfer", "HP Spike Count Task")
}
