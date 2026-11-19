from frappe.model.document import Document

from haxpower_platform.audit import require_append, refuse_change


class Stage1FCountTask(Document):
    before_insert = require_append
    before_save = require_append
    on_trash = refuse_change
    before_rename = refuse_change
