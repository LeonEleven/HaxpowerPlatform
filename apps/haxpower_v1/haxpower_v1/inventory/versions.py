"""Quantity epochs and monotone range counters; never a second quantity ledger.

Writers hold the shared stock guard. Snapshot readers use nonlocking reads. Native fingerprints detect
out-of-band quantity edits while ignoring valuation/modified-only maintenance.
"""
from collections import defaultdict
from dataclasses import asdict
from decimal import Decimal, localcontext
from hashlib import sha256
import json
from uuid import uuid4

import frappe
from frappe.utils import now_datetime

from .operations.dto import Bucket, StockError
from .operations import persistence as stock
from .guard import lock_stock_writes

EPOCH = 'HP V1 Inventory Epoch'
VERSION = 'HP V1 Inventory Version'
TASK = 'HP V1 Stock Count'
LINE = 'HP V1 Stock Count Line'
TABLES = (EPOCH, VERSION, TASK, LINE)
EMPTY = sha256(b'[]').hexdigest()


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()


def insert(doctype, *, name=None, **fields):
    if doctype not in TABLES:
        raise ValueError('Unknown inventory version/count record')
    name = name or uuid4().hex
    now = now_datetime()
    values = dict(name=name, owner=frappe.session.user, modified_by=frappe.session.user,
                  creation=now, modified=now, **fields)
    frappe.db.sql('INSERT INTO `tab' + doctype + '` (' + ','.join('`'+f+'`' for f in values)
                  + ') VALUES (' + ','.join(['%s']*len(values)) + ')', tuple(values.values()))
    return name


def epoch(*, locked=True):
    row = stock.current(EPOCH, 'inventory', ('generation', 'state'), locked=locked)
    if not row or row.state != 'Ready':
        raise StockError('INVENTORY_VERSION_NOT_READY')
    return row.generation


def identity(company, location, scope, sku=None, batch_key=None):
    return digest((company, location, scope, sku or '', batch_key or ''))


def batch_key(kind, batch_id):
    if kind == 'NO_BATCH' and batch_id is None:
        return 'NO_BATCH'
    if kind == 'BATCH' and batch_id:
        return 'BATCH:' + batch_id
    raise StockError('STOCK_EXPLICIT_BATCH_REQUIRED')


def keys(bucket):
    return ((bucket.company, bucket.location_id, 'LOCATION', '', ''),
            (bucket.company, bucket.location_id, 'SKU', bucket.sku_id, ''),
            (bucket.company, bucket.location_id, 'BUCKET', bucket.sku_id,
             batch_key(bucket.batch_kind, bucket.batch_id)))


def read(company, location, scope, sku=None, kind=None, batch_id=None):
    generation = epoch()
    key = batch_key(kind, batch_id) if scope == 'BUCKET' else ''
    row = stock.current(VERSION, identity(company, location, scope, sku, key),
                        ('generation', 'counter', 'fact_digest'))
    if row and row.generation != generation:
        raise StockError('INVENTORY_VERSION_NOT_READY')
    return int(row.counter) if row else 0


def _entries(*, locked=True, company=None):
    """Reconcile all managed native quantity history, including zeroed buckets."""
    from ..catalog.sku_persistence import SKU
    from ..warehouse.persistence import LOCATION, WAREHOUSE
    from .batch.persistence import MAPPING
    from .batch.service import _load_sku
    from .batch.dto import BatchError
    from .availability import read_source

    lock = " FOR UPDATE" if locked else ""
    skus = {r.native_item: r for r in frappe.db.get_values(SKU, {}, ('name', 'native_item'), as_dict=True, for_update=locked)}
    locations = {r.native_warehouse: r for r in frappe.db.sql(
        f'SELECT l.name,l.native_warehouse,w.company FROM `tab{LOCATION}` l '
        f'JOIN `tab{WAREHOUSE}` w ON w.name=l.warehouse' + lock, as_dict=True)}
    mappings = {r.native_batch: r for r in frappe.db.get_values(
        MAPPING, {}, ('name','sku','kind','native_batch'), as_dict=True, for_update=locked)}
    if company is not None:
        locations = {name: row for name, row in locations.items() if row.company == company}
    result, pairs = defaultdict(list), defaultdict(set)
    # Include cancelled rows in the scan so corruption cannot disappear silently.
    where, params = "", ()
    if company is not None:
        where = " WHERE s.company=%s OR s.warehouse IN %s"
        params = (company, tuple(locations) or ("",))
        if frappe.db.sql("SELECT COUNT(*) FROM `tabStock Ledger Entry` s" + where, params)[0][0] > 200_000:
            raise StockError("INVENTORY_READ_HISTORY_LIMIT")

    rows = frappe.db.sql('''SELECT s.name,s.company,s.item_code,s.warehouse,
        CAST(s.actual_qty AS CHAR) quantity,s.serial_and_batch_bundle bundle,s.voucher_type,s.voucher_no,
        s.voucher_detail_no,s.docstatus,s.is_cancelled FROM `tabStock Ledger Entry` s''' + where + lock,
                         params, as_dict=True)
    link_counts = defaultdict(int)
    company_filter = " AND d.company=%s" if company is not None else ""
    company_params = (company,) if company is not None else ()
    for parent_type,parent_id,detail_id,link_company in frappe.db.sql(f'''SELECT l.parent_type,l.parent_id,l.detail_id,d.company
        FROM `tab{stock.LINK}` l JOIN `tab{stock.LOG}` o ON o.name=l.operation_id
        JOIN `tab{stock.DOCUMENT}` d ON d.name=o.document_id WHERE o.status='Effective'
        ''' + company_filter + lock, company_params):
        link_counts[parent_type,parent_id,detail_id,link_company] += 1
    parent_filters = {"company": company} if company is not None else {}
    parent_status = {(table,name):status for table in ('Stock Entry','Stock Reconciliation')
                     for name,status in frappe.db.get_values(table,parent_filters,('name','docstatus'),for_update=locked)}
    parts_by_bundle = defaultdict(list)
    bundle_filter = ""
    bundle_params = ()
    if company is not None:
        bundle_filter = " WHERE parent IN %s"
        bundle_params = (tuple({row.bundle for row in rows if row.bundle}) or ("",),)
    for parent,name,batch,qty,status,cancelled in frappe.db.sql('''SELECT parent,name,batch_no,CAST(qty AS CHAR),
        docstatus,is_cancelled FROM `tabSerial and Batch Entry`''' + bundle_filter + ' ORDER BY name' + lock,
        bundle_params,
    ):
        parts_by_bundle[parent].append((name,batch,qty,status,cancelled))
    seen_native_links = set()
    for row in rows:
        if row.item_code not in skus and row.warehouse not in locations:
            continue
        if (row.item_code not in skus or row.warehouse not in locations or row.docstatus != 1 or row.is_cancelled
                or row.company != locations[row.warehouse].company):
            raise StockError('INVENTORY_NATIVE_HISTORY_INVALID', evidence=dict(sle=row.name, check="SLE_SCOPE_OR_STATUS"))
        sku, location = skus[row.item_code], locations[row.warehouse]
        if (link_counts[row.voucher_type,row.voucher_no,row.voucher_detail_no,row.company] != 1
                or parent_status.get((row.voucher_type,row.voucher_no)) != 1):
            raise StockError('INVENTORY_NATIVE_HISTORY_INVALID', evidence=dict(sle=row.name, check="OPERATION_LINK"))
        seen_native_links.add((row.voucher_type,row.voucher_no,row.voucher_detail_no,row.company))
        parts = parts_by_bundle.get(row.bundle, ())
        if not parts:
            raise StockError('INVENTORY_NATIVE_HISTORY_INVALID', evidence=dict(sle=row.name, bundle=row.bundle,
                                                                            check="BUNDLE_ENTRIES"))
        for name, batch, qty, status, cancelled in parts:
            mapping = mappings.get(batch)
            if not mapping or mapping.sku != sku.name or status != 1 or cancelled:
                raise StockError('INVENTORY_NATIVE_HISTORY_INVALID', evidence=dict(sle=row.name, part=name, batch=batch,
                                                                                check="BATCH_MAPPING_OR_ENTRY_STATUS"))
            bucket = Bucket(row.company, sku.name, location.name, mapping.kind,
                            mapping.name if mapping.kind == 'BATCH' else None)
            result[bucket].append((row.name, row.quantity, row.bundle, row.voucher_type,row.voucher_no,
                                   row.voucher_detail_no,name,batch,qty))
        pairs[row.company].add((row.item_code, row.warehouse))
    managed_links = {key for key in link_counts if key[0] in ('Stock Entry', 'Stock Reconciliation')}
    if missing := managed_links - seen_native_links:
        raise StockError('INVENTORY_NATIVE_HISTORY_INVALID',
                         evidence=dict(check="LINK_WITHOUT_SLE", links=sorted(missing)[:100], total=len(missing)))
    # A stray Bin or stale Batch is checked even in the absence of a live SLE.
    bin_scope = (' LEFT JOIN `tabWarehouse` w ON w.name=b.warehouse WHERE w.company=%s OR b.warehouse IN %s'
                 if company is not None else "")
    for item, leaf in frappe.db.sql('SELECT b.item_code,b.warehouse FROM `tabBin` b' + bin_scope + lock, params):
        if item in skus or leaf in locations:
            if item not in skus or leaf not in locations:
                raise StockError('INVENTORY_NATIVE_HISTORY_INVALID')
            pairs[locations[leaf].company].add((item,leaf))
    # Reconciliation sums fixed-point database quantities. A caller's Decimal
    # context must not round those totals or affect the inventory transaction.
    with localcontext() as context:
        context.prec = 64
        for pair_company, selected in pairs.items():
            sku_ids = sorted({item for item, _ in selected})
            try:
                managed_skus = tuple(_load_sku(skus[item].name, locked=locked) for item in sku_ids)
            except BatchError as error:
                raise StockError(error.code, evidence=dict(native_items=sku_ids[:100])) from None
            read_source(pair_company, selected, managed_skus, locked=locked)
    return result


def facts():
    return {bucket:digest(sorted(entries)) for bucket,entries in _entries().items()}


def verify(*, include_entries=False, locked=True, company=None):
    generation, entries = epoch(locked=locked), _entries(locked=locked, company=company)
    filters = {"company": company} if company is not None else {}
    current = {bucket:digest(sorted(rows)) for bucket,rows in entries.items()}
    recorded = frappe.db.get_values(VERSION, dict(filters, scope='BUCKET'),
                                   ('company','location_id','sku_id','batch_key','generation','fact_digest'),
                                   as_dict=True, for_update=locked)
    expected = {}
    for row in recorded:
        if row.generation != generation:
            raise StockError('INVENTORY_VERSION_NOT_READY')
        kind, bid = ('NO_BATCH',None) if row.batch_key == 'NO_BATCH' else ('BATCH',row.batch_key.removeprefix('BATCH:'))
        expected[Bucket(row.company,row.sku_id,row.location_id,kind,bid)] = row.fact_digest
    if current != expected:
        differences = [bucket for bucket in current.keys() | expected.keys()
                       if current.get(bucket) != expected.get(bucket)]
        differences.sort(key=lambda bucket: (bucket.location_id, bucket.sku_id, bucket.batch_id or ""))
        raise StockError('INVENTORY_QUANTITY_HISTORY_CHANGED', evidence=dict(
            total=len(differences), buckets=[dict(asdict(bucket), recorded=expected.get(bucket),
                                                observed=current.get(bucket)) for bucket in differences[:100]],
        ))
    all_rows = frappe.db.get_values(VERSION, filters,
        ('name','company','location_id','scope','sku_id','batch_key','generation','counter'),
        as_dict=True, for_update=locked)
    required = {identity(*key) for bucket in current for key in keys(bucket)}
    if {r.name for r in all_rows} != required:
        raise StockError('INVENTORY_VERSION_NOT_READY')
    for row in all_rows:
        if (row.generation != generation or not row.counter or not row.counter.isascii()
                or not row.counter.isdigit() or str(int(row.counter)) != row.counter
                or not 0 <= int(row.counter) < 10**38
                or row.name != identity(row.company,row.location_id,row.scope,row.sku_id,row.batch_key)):
            raise StockError('INVENTORY_VERSION_NOT_READY')
    return (current, entries) if include_entries else current


def advance(before, links):
    """Called once by the kernel after links/log exist, before its sole COMMIT."""
    after, touched = facts(), set()
    for link in links:
        rows = frappe.db.sql('''SELECT s.company,k.name,l.name,m.kind,m.name,
            CAST(e.qty AS CHAR) FROM `tabStock Ledger Entry` s
            JOIN `tabSerial and Batch Entry` e ON e.parent=s.serial_and_batch_bundle
            JOIN `tabHP V1 Batch Mapping` m ON m.native_batch=e.batch_no
            JOIN `tabHP V1 Stock Item` k ON k.native_item=s.item_code
            JOIN `tabHP V1 Warehouse Location` l ON l.native_warehouse=s.warehouse
            WHERE s.voucher_type=%s AND s.voucher_no=%s AND s.voucher_detail_no=%s
              AND s.docstatus=1 AND s.is_cancelled=0 FOR UPDATE''',
            (link.parent_type,link.parent_id,link.detail_id))
        for company,sku,location,kind,bid,qty in rows:
            if Decimal(qty):
                touched.add(Bucket(company,sku,location,kind,bid if kind=='BATCH' else None))
    changed = {b for b in before.keys() | after.keys() if before.get(b) != after.get(b)}
    if changed != touched:
        raise StockError('INVENTORY_VERSION_EFFECT_MISMATCH')
    _write(touched, after, increment=True)


def _write(buckets, facts_by_bucket, *, increment):
    generation, updates = epoch(), {}
    for bucket in buckets:
        for key in keys(bucket):
            updates[key] = facts_by_bucket[bucket] if key[2]=='BUCKET' else None
    for key, fingerprint in sorted(updates.items()):
        company,location,scope,sku,batch = key
        name = identity(*key)
        row = stock.current(VERSION,name,('counter','generation'))
        counter = (int(row.counter) if row else 0) + int(increment)
        if counter >= 10**38:
            raise StockError('INVENTORY_VERSION_EXHAUSTED')
        if row:
            if row.generation != generation:
                raise StockError('INVENTORY_VERSION_NOT_READY')
            frappe.db.sql(f'UPDATE `tab{VERSION}` SET counter=%s,fact_digest=%s WHERE name=%s',
                          (str(counter),fingerprint,name))
        else:
            insert(VERSION,name=name,company=company,location_id=location,scope=scope,sku_id=sku,
                   batch_key=batch,generation=generation,counter=str(counter),fact_digest=fingerprint)


def ensure_schema():
    # DDL precedes the protected DML transaction: MariaDB DDL commits implicitly.
    from ..catalog.persistence import _index_columns
    for table,name,columns in (
        (VERSION,'hp_v1_inventory_scope',('company','location_id','scope','sku_id','batch_key')),
        (LINE,'hp_v1_count_bucket',('task_id','bucket_key')),
    ):
        if not _index_columns(table,name):
            frappe.db.add_unique(table,columns,constraint_name=name)
    frappe.db.commit()
    lock_stock_writes()
    row = stock.current(EPOCH,'inventory',('generation','state'))
    if row:
        verify()
        return
    baseline = facts()
    insert(EPOCH,name='inventory',generation=uuid4().hex,state='Ready',baseline_at=now_datetime(),
           reason='V1-12 protected native history and operation-link reconciliation')
    _write(baseline,baseline,increment=False)
    # Hook caller commits; any failure rolls the entire initialization back.


def _rotate_generation_locked(reason):
    """Maintenance seam, caller owns guard/transaction and release authorization.

    This preserves monotone counters and the old task generations. It is not an
    employee API or a disaster-recovery release policy (B11/B12 remain pending).
    """
    if getattr(frappe.local, 'request', None) is not None or not isinstance(reason, str) or not reason.strip():
        raise StockError('INVENTORY_REBASE_UNAUTHORIZED')
    from ..access import policy
    lock_stock_writes()
    policy.require_operation(policy.Operation.SYSTEM_MAINTAIN, locked=True)
    verify()
    generation = uuid4().hex
    frappe.db.sql(f'UPDATE `tab{EPOCH}` SET generation=%s,baseline_at=%s,reason=%s,modified=%s,modified_by=%s WHERE name=%s',
                  (generation, now_datetime(), reason, now_datetime(), frappe.session.user, 'inventory'))
    frappe.db.sql(f'UPDATE `tab{VERSION}` SET generation=%s', (generation,))
    return generation


def _rebaseline_generation_locked(reason):
    """Explicit repair seam for lost version continuity, never called by migrate.

    Requires separately authorized maintenance. Reconcile native facts and
    bidirectional operation links before replacing the version baseline in a
    new generation; old count snapshots can never become valid in this epoch.
    The caller owns the single transaction/COMMIT and operational release.
    """
    from ..access import policy
    if getattr(frappe.local, 'request', None) is not None or not isinstance(reason, str) or not reason.strip():
        raise StockError('INVENTORY_REBASE_UNAUTHORIZED')
    lock_stock_writes()
    policy.require_operation(policy.Operation.SYSTEM_MAINTAIN, locked=True)
    baseline = facts()
    prior = stock.current(EPOCH, 'inventory', ('generation', 'state'))
    if not prior:
        raise StockError('INVENTORY_VERSION_NOT_READY')
    # Historical quantity buckets are in baseline even when currently zero.
    # Counters restart only in this explicit new generation, never on migrate.
    frappe.db.sql(f'DELETE FROM `tab{VERSION}`')
    generation = uuid4().hex
    frappe.db.sql(f'UPDATE `tab{EPOCH}` SET generation=%s,state=%s,baseline_at=%s,reason=%s,modified=%s,modified_by=%s WHERE name=%s',
                  (generation, 'Ready', now_datetime(), reason, now_datetime(), frappe.session.user, 'inventory'))
    _write(baseline, baseline, increment=False)
    verify()
    return generation
