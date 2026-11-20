from frappe.model.document import Document

from haxpower_platform.audit import refuse_change, require_append


class Stage2BStockOperation(Document):
    before_insert = require_append
    before_save = require_append
    on_trash = refuse_change
    before_rename = refuse_change
