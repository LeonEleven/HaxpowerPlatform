"""Quantity epochs and monotone range counters; never a second quantity ledger.

Every caller holds the shared stock guard. Native identity fingerprints detect
out-of-band quantity edits while ignoring valuation/modified-only maintenance.
"""
from collections import defaultdict
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


def epoch():
    row = stock.current(EPOCH, 'inventory', ('generation', 'state'))
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


def facts():
    """Reconcile all managed native quantity history, including zeroed buckets."""
    from ..catalog.sku_persistence import SKU
    from ..warehouse.persistence import LOCATION, WAREHOUSE
    from .batch.persistence import MAPPING
    from .batch.service import _load_sku
    from .availability import read_source

    skus = {r.native_item: r for r in frappe.db.get_values(SKU, {}, ('name', 'native_item'), as_dict=True, for_update=True)}
    locations = {r.native_warehouse: r for r in frappe.db.sql(
        f'SELECT l.name,l.native_warehouse,w.company FROM `tab{LOCATION}` l '
        f'JOIN `tab{WAREHOUSE}` w ON w.name=l.warehouse FOR UPDATE', as_dict=True)}
    mappings = {r.native_batch: r for r in frappe.db.get_values(
        MAPPING, {}, ('name','sku','kind','native_batch'), as_dict=True, for_update=True)}
    result, pairs = defaultdict(list), defaultdict(set)
    # Include cancelled rows in the scan so corruption cannot disappear silently.
    rows = frappe.db.sql('''SELECT s.name,s.company,s.item_code,s.warehouse,
        CAST(s.actual_qty AS CHAR) quantity,s.serial_and_batch_bundle bundle,s.voucher_type,s.voucher_no,
        s.voucher_detail_no,s.docstatus,s.is_cancelled FROM `tabStock Ledger Entry` s FOR UPDATE''', as_dict=True)
    link_counts = defaultdict(int)
    for parent_type,parent_id,detail_id,company in frappe.db.sql(f'''SELECT l.parent_type,l.parent_id,l.detail_id,d.company
        FROM `tab{stock.LINK}` l JOIN `tab{stock.LOG}` o ON o.name=l.operation_id
        JOIN `tab{stock.DOCUMENT}` d ON d.name=o.document_id WHERE o.status='Effective' FOR UPDATE'''):
        link_counts[parent_type,parent_id,detail_id,company] += 1
    parent_status = {(table,name):status for table in ('Stock Entry','Stock Reconciliation')
                     for name,status in frappe.db.get_values(table,{},('name','docstatus'),for_update=True)}
    parts_by_bundle = defaultdict(list)
    for parent,name,batch,qty,status,cancelled in frappe.db.sql('''SELECT parent,name,batch_no,CAST(qty AS CHAR),
        docstatus,is_cancelled FROM `tabSerial and Batch Entry` ORDER BY name FOR UPDATE'''):
        parts_by_bundle[parent].append((name,batch,qty,status,cancelled))
    for row in rows:
        if row.item_code not in skus and row.warehouse not in locations:
            continue
        if (row.item_code not in skus or row.warehouse not in locations or row.docstatus != 1 or row.is_cancelled
                or row.company != locations[row.warehouse].company):
            raise StockError('INVENTORY_NATIVE_HISTORY_INVALID')
        sku, location = skus[row.item_code], locations[row.warehouse]
        if (link_counts[row.voucher_type,row.voucher_no,row.voucher_detail_no,row.company] != 1
                or parent_status.get((row.voucher_type,row.voucher_no)) != 1):
            raise StockError('INVENTORY_NATIVE_HISTORY_INVALID')
        parts = parts_by_bundle.get(row.bundle, ())
        if not parts:
            raise StockError('INVENTORY_NATIVE_HISTORY_INVALID')
        for name, batch, qty, status, cancelled in parts:
            mapping = mappings.get(batch)
            if not mapping or mapping.sku != sku.name or status != 1 or cancelled:
                raise StockError('INVENTORY_NATIVE_HISTORY_INVALID')
            bucket = Bucket(row.company, sku.name, location.name, mapping.kind,
                            mapping.name if mapping.kind == 'BATCH' else None)
            result[bucket].append((row.name, row.quantity, row.bundle, row.voucher_type,row.voucher_no,
                                   row.voucher_detail_no,name,batch,qty))
        pairs[row.company].add((row.item_code, row.warehouse))
    # A stray Bin or stale Batch is checked even in the absence of a live SLE.
    for item, leaf in frappe.db.sql('SELECT item_code,warehouse FROM `tabBin` FOR UPDATE'):
        if item in skus or leaf in locations:
            if item not in skus or leaf not in locations:
                raise StockError('INVENTORY_NATIVE_HISTORY_INVALID')
            pairs[locations[leaf].company].add((item,leaf))
    for company, selected in pairs.items():
        read_source(company, selected, tuple(_load_sku(skus[item].name, locked=True) for item in sorted({i for i,_ in selected})))
    return {bucket:digest(sorted(entries)) for bucket,entries in result.items()}


def verify():
    generation, current = epoch(), facts()
    recorded = frappe.db.get_values(VERSION, {'scope':'BUCKET'},
                                   ('company','location_id','sku_id','batch_key','generation','fact_digest'),
                                   as_dict=True, for_update=True)
    expected = {}
    for row in recorded:
        if row.generation != generation:
            raise StockError('INVENTORY_VERSION_NOT_READY')
        kind, bid = ('NO_BATCH',None) if row.batch_key == 'NO_BATCH' else ('BATCH',row.batch_key.removeprefix('BATCH:'))
        expected[Bucket(row.company,row.sku_id,row.location_id,kind,bid)] = row.fact_digest
    if current != expected:
        raise StockError('INVENTORY_QUANTITY_HISTORY_CHANGED')
    all_rows = frappe.db.get_values(VERSION, {},
        ('name','company','location_id','scope','sku_id','batch_key','generation','counter'),
        as_dict=True, for_update=True)
    required = {identity(*key) for bucket in current for key in keys(bucket)}
    if {r.name for r in all_rows} != required:
        raise StockError('INVENTORY_VERSION_NOT_READY')
    for row in all_rows:
        if (row.generation != generation or not row.counter or not row.counter.isascii()
                or not row.counter.isdigit() or str(int(row.counter)) != row.counter
                or not 0 <= int(row.counter) < 10**38
                or row.name != identity(row.company,row.location_id,row.scope,row.sku_id,row.batch_key)):
            raise StockError('INVENTORY_VERSION_NOT_READY')
    return current


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
            from decimal import Decimal
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
    policy.require_operation(policy.Operation.SYSTEM_MAINTAIN, locked=True)
    lock_stock_writes()
    verify()
    generation = uuid4().hex
    frappe.db.sql(f'UPDATE `tab{EPOCH}` SET generation=%s,baseline_at=%s,reason=%s WHERE name=%s',
                  (generation, now_datetime(), reason, 'inventory'))
    frappe.db.sql(f'UPDATE `tab{VERSION}` SET generation=%s', (generation,))
    return generation
