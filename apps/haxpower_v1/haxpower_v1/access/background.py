"""Keep stock maintenance schedules and previously queued entry points closed."""

import frappe

from .policy import deny
from .registry import DISABLED_SCHEDULED_JOBS, DISABLED_WORKER_METHODS, MAINTENANCE_DOCUMENTS, STOCK_DOCUMENTS

JOB = "Scheduled Job Type"
RUN_SCHEDULED = "frappe.core.doctype.scheduled_job_type.scheduled_job_type.run_scheduled_job"
RUN_DOCUMENT = "frappe.utils.background_jobs.run_doc_method"


def stop_native_schedules():
    for name in frappe.get_all(
        JOB, filters={"method": ("in", DISABLED_SCHEDULED_JOBS), "stopped": 0}, pluck="name",
    ):
        frappe.get_doc(JOB, name).db_set("stopped", 1)


def guard_queued_native_job(method, kwargs):
    if method in DISABLED_WORKER_METHODS:
        deny()
    if method == RUN_SCHEDULED:
        name = kwargs.get("scheduled_job_type")
        if name and frappe.db.get_value(JOB, name, "method") in DISABLED_SCHEDULED_JOBS:
            deny()
    if method == RUN_DOCUMENT and kwargs.get("doctype") in (*MAINTENANCE_DOCUMENTS, *STOCK_DOCUMENTS):
        deny()
