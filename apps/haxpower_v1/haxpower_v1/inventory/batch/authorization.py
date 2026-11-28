"""V1-06 exposes batch operations only to isolated synthetic fixtures."""

import re

import frappe

from ...access.policy import active_actor, deny
from .dto import BatchError
from .normalization import CANDIDATE_VERSION, FORMAL_VERSION

POLICY_SETTING = "hp_v1_batch_policy"
SYNTHETIC_SITE = re.compile(r"haxpower-v1-(?:dev|ci-[1-9][0-9]{0,19}-[1-9][0-9]{0,4}(?:-upgrade)?)\.localhost")


def require_batch_operator():
    if active_actor() != "Administrator" or getattr(frappe.local, "request", None) is not None:
        deny()


def is_synthetic_sku(sku):
    if sku.code.startswith("SYNTHETIC-"):
        return True
    from ...adapters.erpnext.item import INTERNAL_PREFIX
    from ...catalog.numbers import sku_serial

    return (
        SYNTHETIC_SITE.fullmatch(frappe.local.site or "") is not None
        and frappe.conf.get("db_host") == "mariadb"
        and getattr(frappe.local, "request", None) is None
        and sku.native_item_group.startswith("SYNTHETIC-")
        and sku.native_item == f"{INTERNAL_PREFIX}{sku.name}"
        and sku_serial(sku.code, sku.code.rsplit("-", 1)[0]) is not None
    )


def require_candidate_writer(sku):
    require_batch_operator()
    if (
        frappe.conf.get(POLICY_SETTING) not in (CANDIDATE_VERSION, FORMAL_VERSION)
        or not SYNTHETIC_SITE.fullmatch(frappe.local.site or "")
        or not is_synthetic_sku(sku)
    ):
        raise BatchError("BATCH_POLICY_NOT_APPROVED")
