"""Mark pre-existing synthetic samples after the revision field is synced."""

import frappe


def execute():
    sample = frappe.qb.DocType("HP V1 Migration Sample")
    (
        frappe.qb.update(sample)
        .set(sample.baseline_revision, 1)
        .where(sample.baseline_revision.isnull() | (sample.baseline_revision == 0))
    ).run()
