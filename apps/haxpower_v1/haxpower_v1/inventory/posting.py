"""Shared stock input validation and the isolated execution boundary."""

import os

import frappe

from .batch.authorization import SYNTHETIC_SITE
from .batch.dto import BatchError, BatchKind
from .batch.normalization import normalize_selection
from .operations.dto import StockError


def require_fixture(candidate, company, error):
    site = frappe.local.site or ""
    project = os.environ.get("HP_V1_PROJECT", "")
    actor = frappe.session.user
    if (
        not candidate or candidate.actor != actor or candidate.company != company
        or not isinstance(actor, str) or not actor.endswith("@example.invalid")
        or not isinstance(company, str) or not company.startswith("Synthetic V1 Warehouse ")
        or getattr(frappe.local, "request", None) is not None
        or not project.startswith("haxpower-v1-ci-") or not SYNTHETIC_SITE.fullmatch(site)
        or site not in (f"{project}.localhost", f"{project}-upgrade.localhost")
        or frappe.conf.get("db_host") != "mariadb"
    ):
        raise StockError(error)
    return candidate


def selection(payload):
    try:
        selection = normalize_selection(payload)
    except BatchError as error:
        raise StockError(error.code) from None
    if selection.normalization and selection.normalization.original != selection.normalization.candidate:
        raise StockError("BATCH_NORMALIZATION_CONFIRMATION_REQUIRED")
    return selection


def batch_input(line):
    if line.batch_kind == BatchKind.BATCH:
        return {"mode": "BATCH", "batchNo": line.batch_no}
    return {"mode": "NO_BATCH"}


def require_native_precision(skus, error):
    from frappe.utils import cint

    # ERPNext's SLE accumulator defaults to two decimals in the absence of
    # an explicit default, even when native quantity fields accept three.
    precision = cint(frappe.db.get_default("float_precision"))
    if not 1 <= precision <= 9 or any(sku.quantity_precision > precision for sku in skus):
        raise StockError(error)
