"""Explicit historical identities for isolated engineering fixtures."""

import frappe

from ..inventory.batch.authorization import SYNTHETIC_SITE
from .persistence import require_site_operator


def require_fixture_catalog():
    require_site_operator()
    if not SYNTHETIC_SITE.fullmatch(frappe.local.site or "") or frappe.conf.get("db_host") != "mariadb":
        frappe.throw("Explicit catalog fixture codes require an isolated site.", frappe.PermissionError)
