"""Export generation, status and authorized binary delivery."""

import frappe

from ..inventory.operations.dto import StockError
from . import exports


@frappe.whitelist(methods=["POST"])
def generate(report: str, criteria=None):
    try:
        return exports.generate(report, criteria)
    except StockError as error:
        frappe.throw(error.code)


@frappe.whitelist(methods=["GET"])
def status(export_id: str):
    return exports.status(export_id)


@frappe.whitelist(methods=["POST"])
def download(export_id: str):
    try:
        filename, content = exports.download(export_id)
    except StockError as error:
        frappe.throw(error.code)
    frappe.response.update(type="download", content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                           filename=filename, filecontent=content)
