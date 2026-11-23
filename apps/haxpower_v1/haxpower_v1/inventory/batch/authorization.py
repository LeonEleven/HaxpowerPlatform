"""V1-06 exposes batch operations only to isolated synthetic fixtures."""

import re

import frappe

from ...access.policy import active_actor, deny
from .dto import BatchError
from .normalization import CANDIDATE_VERSION

POLICY_SETTING = "hp_v1_batch_policy"
SYNTHETIC_SITE = re.compile(r"haxpower-v1-(?:dev|ci-[1-9][0-9]{0,19}-[1-9][0-9]{0,4}(?:-upgrade)?)\.localhost")


def require_batch_operator():
    if active_actor() != "Administrator" or getattr(frappe.local, "request", None) is not None:
        deny()


def require_candidate_writer(sku):
    require_batch_operator()
    if (
        frappe.conf.get(POLICY_SETTING) != CANDIDATE_VERSION
        or not SYNTHETIC_SITE.fullmatch(frappe.local.site or "")
        or not sku.code.startswith("SYNTHETIC-")
    ):
        raise BatchError("BATCH_POLICY_NOT_APPROVED")
