"""This patch is copied into the disposable Bench only during recovery testing."""

import frappe


def execute():
    frappe.db.set_value("HP V1 Migration Sample", "v1-synthetic-001",
                        "sample_text", "Attempted write inside failing patch")
    raise RuntimeError("HP_V1_REHEARSAL_PATCH_FAILURE")
