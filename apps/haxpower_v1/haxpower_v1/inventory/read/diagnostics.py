"""Server-only native reconciliation. Diagnostics do not write inventory."""

from decimal import localcontext

import frappe

from ...access import policy
from ...catalog.sku_persistence import SKU
from ...warehouse.persistence import LOCATION, WAREHOUSE
from .. import versions
from ..operations.dto import StockError
from ..operations.transaction import connection
from .facts import Facts


def reconcile(company, sku_id=None, location_id=None):
    """Bench execute with an explicitly authorized System Maintain identity."""
    if getattr(frappe.local, "request", None) is not None:
        policy.deny()
    with connection(snapshot=True), localcontext() as context:
        context.prec = 64
        policy.require_operation(policy.Operation.SYSTEM_MAINTAIN)
        if not frappe.db.exists("Company", company):
            policy.deny()
        if sku_id and not frappe.db.exists(SKU, policy._record_id(sku_id)):
            policy.deny()
        if location_id:
            warehouse = frappe.db.get_value(LOCATION, policy._record_id(location_id), "warehouse")
            if not warehouse or frappe.db.get_value(WAREHOUSE, warehouse, "company") != company:
                policy.deny()
        try:
            facts = Facts(company)
            movements = facts.movements
            transfers = facts.transfers
        except StockError as error:
            return dict(company=company, status="Inconsistent", code=error.code, evidence=error.evidence)
        matched = {bucket: amount for bucket, amount in facts.balances.items()
                   if (sku_id is None or bucket.sku_id == sku_id)
                   and (location_id is None or bucket.location_id == location_id)}
        return dict(company=company, status="Verified", bucket_count=len(matched),
                    movement_count=sum(row.bucket in matched for row in movements),
                    transfer_line_count=sum((sku_id is None or row["sku_id"] == sku_id) and
                                            (location_id is None or location_id in (
                                                row["source_location_id"], row["target_location_id"],
                                            )) for row in transfers),
                    fact_digest=versions.digest(sorted(
                        (bucket.sku_id, bucket.location_id, bucket.batch_kind, bucket.batch_id or "",
                         versions.digest(sorted(facts.entries[bucket])), str(amount))
                        for bucket, amount in matched.items()
                    )))
