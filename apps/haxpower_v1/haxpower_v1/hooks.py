app_name = "haxpower_v1"
app_title = "HaxpowerPlatform"
app_publisher = "HaxpowerPlatform"
app_description = "HaxpowerPlatform V1"
required_apps = ["erpnext"]

after_install = "haxpower_v1.catalog.persistence.ensure_schema"
after_migrate = "haxpower_v1.catalog.persistence.ensure_schema"
