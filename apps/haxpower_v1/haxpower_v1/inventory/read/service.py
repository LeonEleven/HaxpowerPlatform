"""Employee inventory summaries and reports using a single authorization plan."""

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, localcontext
from functools import cached_property

import frappe
from frappe.utils import get_system_timezone, now_datetime

from ...access import policy
from ...approval import policy as rules
from ...catalog import read as catalog
from ..operations import persistence as stock
from ..operations.dto import StockError
from ..operations.transaction import connection
from .facts import Facts, quantity
from .query import Query, Scope

REPORTS = frozenset({"stock", "distribution", "batches", "transit", "ledger", "period", "documents", "corrections"})


@dataclass(frozen=True)
class InventoryCoverage:
    company: str
    warehouses: tuple[str, ...]
    includes_specification: bool
    uses_product_search: bool


class Inventory:
    def __init__(self, options):
        self.scope = Scope.current(options.get("company"))
        product_read = policy.Operation.PRODUCT_READ in policy.granted_operations()
        self.query = Query(options, self.scope, product_read=product_read)
        self.included_warehouses = set()

    @cached_property
    def facts(self):
        return Facts(self.scope.company)

    def _physical(self):
        # Quantity and operation-line provenance are verified from the same snapshot.
        _ = self.facts.movements
        return [(bucket, amount) for bucket, amount in self.facts.balances.items()
                if self.query.location_matches(bucket.location_id)
                and self.query.batch_matches(self.facts.batch(bucket.batch_id))]

    def _transit(self):
        for row in self.facts.transfers:
            source, target = row["source_location_id"], row["target_location_id"]
            if (source in self.scope.locations and target in self.scope.locations
                    and (self.query.location_matches(source) or self.query.location_matches(target))
                    and self.query.batch_matches(row["batch"])):
                source_warehouse = self.scope.locations[source]["warehouse_id"]
                target_warehouse = self.scope.locations[target]["warehouse_id"]
                yield dict(row, source_warehouse_id=source_warehouse, target_warehouse_id=target_warehouse,
                           source_warehouse=self.scope.warehouses[source_warehouse],
                           target_warehouse=self.scope.warehouses[target_warehouse],
                           source_location=self.scope.locations[source], target_location=self.scope.locations[target])

    def stock(self):
        physical, transit = defaultdict(Decimal), defaultdict(Decimal)
        for bucket, amount in self._physical():
            physical[bucket.sku_id] += amount
        transit_rows = list(self._transit())
        for row in transit_rows:
            transit[row["sku_id"]] += Decimal(row["remaining"])
        visible = {sku for sku in physical.keys() | transit.keys() if physical[sku] or transit[sku]}
        # When a batch is selected, use only its actual authorized buckets,
        # including historical zeroes; never create a missing batch candidate.
        candidates = visible
        if self.query.include_zero:
            candidates = physical.keys() | transit.keys() if self.query.batch_selected else None
        ids = self.query.product_ids(candidates=candidates)
        products = catalog.by_ids(ids, include_specification=self.query.product_read)
        result = []
        for sku in ids:
            item = products[sku]
            row = dict(
                id=item["id"], code=item["code"], display_name=item["display_name"],
                brand=item["brand"], stock_uom=item["stock_uom"], physical=quantity(physical[sku]),
                in_transit=quantity(transit[sku]),
            )
            if self.query.product_read:
                spec = item["specification"]
                row["specification"] = {key: spec[key] for key in (
                    "id", "code", "display_name", "category_path", "attributes", "historical_identifiers",
                )}
            result.append(row)
        valid = set(ids)
        self.included_warehouses.update(self.scope.locations[b.location_id]["warehouse_id"]
                                        for b, _ in self._physical() if b.sku_id in valid)
        for row in transit_rows:
            if row["sku_id"] in valid:
                self.included_warehouses.update((row["source_warehouse_id"], row["target_warehouse_id"]))
        return result

    def distribution(self):
        rows = self._physical()
        products = catalog.by_ids(self.query.product_ids(candidates={bucket.sku_id for bucket, _ in rows}, active=False))
        result = []
        for bucket, amount in rows:
            if bucket.sku_id not in products or (not amount and not self.query.include_zero):
                continue
            product = products[bucket.sku_id]
            location = self.scope.locations[bucket.location_id]
            result.append(dict(
                sku_id=bucket.sku_id, sku_code=product["code"], sku_name=product["display_name"],
                warehouse=self.scope.warehouses[location["warehouse_id"]], location=location,
                batch=self.facts.batch(bucket.batch_id), batch_id=bucket.batch_id, physical=quantity(amount),
                available=quantity(amount), uom=product["stock_uom"],
            ))
        self.included_warehouses.update(row["warehouse"]["id"] for row in result)
        return sorted(result, key=lambda row: (row["sku_code"], row["sku_id"], row["warehouse"]["code"],
                                               row["location"]["code"], row["location"]["id"],
                                               row["batch"] or "", row["batch_id"] or ""))

    def batches(self):
        matches = {(row["sku_id"], row["batch"], row["batch_id"]) for row in self.distribution()}
        matches.update((row["sku_id"], row["batch"], row["batch_id"]) for row in self.transit())
        return [dict(sku_id=sku, batch=batch, batch_id=batch_id) for sku, batch, batch_id in sorted(
            matches, key=lambda item: (item[0], item[1] or "", item[2] or ""),
        )]

    def transit(self):
        matches = list(self._transit())
        ids = set(self.query.product_ids(candidates={row["sku_id"] for row in matches}, active=False))
        matches = [row for row in matches if row["sku_id"] in ids
                   and (self.query.include_zero or Decimal(row["remaining"]))]
        for row in matches:
            self.included_warehouses.update((row["source_warehouse_id"], row["target_warehouse_id"]))
        return sorted(matches,
                      key=lambda row: (row["dispatched_at"], row["document_id"], row["line_id"]), reverse=True)

    def _movements(self):
        rows = [row for row in self.facts.movements if self.query.location_matches(row.bucket.location_id)
                and self.query.batch_matches(self.facts.batch(row.bucket.batch_id))]
        ids = self.query.product_ids(candidates={row.bucket.sku_id for row in rows}, active=False)
        products = catalog.by_ids(ids)
        return [row for row in rows if row.bucket.sku_id in products], products

    def ledger(self):
        movements, products = self._movements()
        result = []
        numbers = dict(frappe.db.get_values(stock.DOCUMENT, {"company": self.scope.company},
                                            ("name", "document_number")))
        for row in reversed(movements):
            if ((self.query.start and row.date < self.query.start) or (self.query.end and row.date >= self.query.end)
                    or (self.query.action and row.action != self.query.action)
                    or (self.query.number and self.query.number != numbers.get(row.document))
                    or (self.query.direction == "INCREASE" and row.quantity < 0)
                    or (self.query.direction == "DECREASE" and row.quantity > 0)):
                continue
            location = self.scope.locations[row.bucket.location_id]
            product = products[row.bucket.sku_id]
            result.append(dict(
                sku_id=row.bucket.sku_id, sku_code=product["code"], sku_name=product["display_name"],
                warehouse=self.scope.warehouses[location["warehouse_id"]], location=location,
                batch=self.facts.batch(row.bucket.batch_id), batch_id=row.bucket.batch_id,
                operation_id=row.operation, line_id=row.line, document_id=row.document,
                document_number=numbers.get(row.document), action=row.action, posting_at=row.date,
                quantity=quantity(row.quantity), uom=product["stock_uom"],
            ))
        self.included_warehouses.update(row["warehouse"]["id"] for row in result)
        return result

    def period(self):
        if not self.query.start or not self.query.end or any((self.query.action, self.query.number, self.query.direction)):
            frappe.throw("READ_INVALID_PERIOD")
        movements, products = self._movements()
        origins = {row["line_id"]: row for row in self._transit()}
        transit_location = self.facts.transit_location
        transit_ids = set(self.query.product_ids(candidates={row["sku_id"] for row in origins.values()}, active=False))
        products.update(catalog.by_ids(transit_ids - products.keys()))
        relevant = [(row, None) for row in movements]
        relevant.extend((row, origins[row.line]) for row in self.facts.movements
                        if row.bucket.location_id == transit_location and row.line in origins
                        and row.bucket.sku_id in transit_ids)
        grouped = {}
        for row, origin in relevant:
            if row.date >= self.query.end:
                continue
            bucket = row.bucket
            key = (bucket.sku_id, None if origin else bucket.location_id, bucket.batch_id,
                   row.line if origin else None)
            aggregate = grouped.setdefault(key, dict(opening=Decimal(0), increase=Decimal(0), decrease=Decimal(0),
                                                     actions=defaultdict(lambda: [Decimal(0), Decimal(0)])))
            if row.date < self.query.start:
                aggregate["opening"] += row.quantity
            else:
                position = int(row.quantity < 0)
                aggregate["actions"][row.action][position] += abs(row.quantity)
                aggregate["increase" if row.quantity > 0 else "decrease"] += abs(row.quantity)
        result = []
        for (sku, location, batch, transfer_line), aggregate in sorted(grouped.items(), key=lambda item: (
            products[item[0][0]]["code"], item[0][0], item[0][1] or "", item[0][2] or "", item[0][3] or "",
        )):
            opening, increase, decrease = (aggregate[key] for key in ("opening", "increase", "decrease"))
            if not self.query.include_zero and not (opening or increase or decrease):
                continue
            location = self.scope.locations.get(location)
            warehouse = self.scope.warehouses[location["warehouse_id"]] if location else None
            if warehouse:
                self.included_warehouses.add(warehouse["id"])
            else:
                origin = origins[transfer_line]
                self.included_warehouses.update((origin["source_warehouse_id"], origin["target_warehouse_id"]))
            result.append(dict(
                sku_id=sku, sku_code=products[sku]["code"], uom=products[sku]["stock_uom"],
                warehouse=warehouse, location=location, bucket_type="TRANSIT" if transfer_line else "PHYSICAL",
                transfer_line_id=transfer_line, batch=self.facts.batch(batch), batch_id=batch,
                opening=quantity(opening), increase=quantity(increase), decrease=quantity(decrease),
                closing=quantity(opening + increase - decrease),
                actions={action: dict(increase=quantity(amounts[0]), decrease=quantity(amounts[1]))
                         for action, amounts in sorted(aggregate["actions"].items())},
            ))
        return result

    def documents(self):
        from .documents import list_documents

        return list_documents(self)

    def corrections(self):
        from .documents import list_documents

        return list_documents(self, kind=rules.CORRECTION)


def inventory_data(report, options, page, *, export=False):
    if report not in REPORTS:
        frappe.throw("READ_INVALID_REPORT")
    with connection(snapshot=True), localcontext() as context:
        as_of = now_datetime()
        context.prec = 64
        if export:
            policy.require_operation(policy.Operation.EXPORT)
        service = Inventory(options)
        result = getattr(service, report)()
        coverage = InventoryCoverage(
            service.scope.company, tuple(sorted(service.included_warehouses)),
            includes_specification=report == "stock" and service.query.product_read,
            uses_product_search=service.query.uses_product_fields,
        )
        response = dict(page.select(result), as_of=as_of)
        if report in ("ledger", "period"):
            response["timezone"] = get_system_timezone()
        return response, coverage


def product_data(options, page, *, export=False):
    from ...catalog.sku import search_sku_ids

    with connection(snapshot=True):
        as_of = now_datetime()
        actor = policy.active_actor()
        policy.require_operation(policy.Operation.PRODUCT_READ)
        if export:
            policy.require_operation(policy.Operation.EXPORT)
        ids, total = search_sku_ids(options.get("query", ""), limit=page.limit, offset=page.offset,
                                    **{k: v for k, v in options.items() if k != "query"})
        products = catalog.by_ids(ids, details=True)
        return {"rows": [products[sku] for sku in ids], "total": total, "as_of": as_of}, actor
