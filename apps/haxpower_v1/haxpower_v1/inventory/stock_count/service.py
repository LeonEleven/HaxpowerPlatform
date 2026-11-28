"""Non-freezing stock counts, trusted snapshots and an approval execution seam.

Not whitelisted: employee stock writes remain disabled pending B01/B03/B12.
Completion submits the confirmed snapshot through the inventory approval policy.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from decimal import Decimal, localcontext
from uuid import uuid4

import frappe

from ..batch.authorization import is_synthetic_sku
from frappe.utils import now_datetime

from ...access import policy
from ...adapters.erpnext import stock_count as native
from .. import availability, versions
from ..batch import service as batches
from ..batch.dto import BatchError, BatchKind
from ..batch.persistence import MAPPING
from ..guard import lock_stock_writes
from ..operations import persistence as store
from ..operations.dto import Command, Line, QuantitySign, StockError
from ..operations.quantity import quantity_text
from ..operations.service import Action, _commit, _execute, _id, _text
from ..operations.transaction import connection
from ..posting import require_fixture, require_native_precision, selection
from .dto import CountLine, CountTask, CreateCount, Scope

ACTION = 'Stock Count'
_candidate = ContextVar('hp_v1_count_candidate', default=None)


@dataclass(frozen=True)
class Candidate:
    actor: str
    company: str
    limits: dict
    rate: Decimal
    allow_gl: bool


@contextmanager
def synthetic_candidate(company, *, limits, rate=Decimal(0), allow_gl=False):
    if not isinstance(rate, Decimal) or not rate.is_finite() or rate < 0 or type(allow_gl) is not bool:
        raise StockError('COUNT_VALUATION_INVALID')
    token = _candidate.set(Candidate(frappe.session.user,company,dict(limits),rate,allow_gl))
    try:
        _authorize(company)
        yield
    finally:
        _candidate.reset(token)


def _authorize(company):
    return require_fixture(_candidate.get(),company,'COUNT_NOT_ENABLED')


def _location(company, location_id):
    policy.require_operation(policy.Operation.COUNT,locked=True)
    _authorize(company)
    _id(location_id)
    return policy.lock_execution_locations(company,(location_id,))[location_id]


def _selection(sku_id, payload):
    try:
        return _select_locked(sku_id, payload)
    except BatchError as error:
        raise StockError(error.code) from None


def _select_locked(sku_id, payload):
    sku = batches._load_sku(sku_id,locked=True)
    if not is_synthetic_sku(sku):
        policy.deny()
    def authorize(current):
        policy.require_operation(policy.Operation.COUNT,locked=True)
        _authorize(_candidate.get().company)
        if not is_synthetic_sku(current):
            policy.deny()
    # V1-06 controlled provisioning, in this same transaction; no SKU creation.
    parsed = selection(payload)
    resolved = batches._resolve_locked(sku,parsed,authorize).batch
    return sku, frappe.db.get_value(MAPPING,resolved.id,('name','kind','original_text','native_batch'),
                                     as_dict=True,for_update=True)


def _scope_input(payload):
    _text(payload.company,140,required=True)
    _text(payload.notes,4000)
    _id(payload.location_id)
    if payload.scope not in tuple(Scope):
        raise StockError('COUNT_SCOPE_INVALID')
    if payload.scope == Scope.LOCATION:
        if payload.sku_id is not None or payload.batch is not None:
            raise StockError('COUNT_SCOPE_INVALID')
    else:
        _id(payload.sku_id)
        if payload.scope == Scope.SKU and payload.batch is not None:
            raise StockError('COUNT_SCOPE_INVALID')
        if payload.scope == Scope.BUCKET:
            selection(payload.batch)


def _rows(task_id):
    return frappe.db.get_values(versions.LINE,{'task_id':task_id},'*',as_dict=True,
                                for_update=True,order_by='line_no asc')


def _head(company, task_id):
    _id(task_id)
    document = store.current(store.DOCUMENT,task_id,
        ('name','company','document_type','status','revision','effective_operation','notes',
         'reason','owner','approval_submission'))
    task = store.current(versions.TASK,task_id,('*',))
    if not document or not task or (document.company,document.document_type,task.company) != (company,ACTION,company):
        policy.deny()
    _location(company,task.location_id)
    return document,task


def _stale(task, rows):
    if task.generation != versions.epoch():
        return 'COUNT_GENERATION_CHANGED'
    if str(versions.read(task.company,task.location_id,task.scope,task.sku_id or None,
                         task.batch_kind or None,task.batch_id or None)) != task.scope_version:
        return 'COUNT_SCOPE_CHANGED'
    for row in rows:
        if str(versions.read(task.company,row.location_id,'BUCKET',row.sku_id,row.batch_kind,row.batch_id or None)) != row.bucket_version:
            return 'COUNT_BUCKET_CHANGED'
    return None


def _view(document, task, rows):
    lines = tuple(CountLine(r.name,r.line_no,r.sku_id,r.location_id,r.batch_kind,r.batch_id or None,
        r.batch_no or None,r.uom,r.snapshot_quantity,r.actual_quantity,r.difference_quantity,r.bucket_version,
        bool(r.confirmed),bool(r.discovered),r.counted_by,str(r.counted_at) if r.counted_at else None,r.notes or '') for r in rows)
    stale = _stale(task,rows) if document.status in ('Draft', 'Rejected', 'Pending Approval') else None
    return CountTask(task.company,task.name,task.location_id,task.scope,task.sku_id or None,
        task.batch_kind or None,task.batch_id or None,task.snapshot_id,str(task.snapshot_at),task.generation,
        task.scope_version,document.revision,document.status,stale,task.original_task or None,
        task.recount_task or None,document.effective_operation,lines)


def _trusted(task, rows):
    if reason := _stale(task,rows):
        raise StockError(reason)


def _bucket_line(task, bucket, quantity, number, *, discovered=False):
    sku = batches._load_sku(bucket.sku_id,locked=True)
    mapping = batches.lock_execution_mapping(sku,bucket.batch_kind,bucket.batch_id)
    if not mapping:
        raise StockError('BATCH_MAPPING_UNAVAILABLE')
    return versions.insert(versions.LINE,task_id=task.name,line_no=number,sku_id=sku.name,
        location_id=bucket.location_id,batch_kind=bucket.batch_kind,batch_id=bucket.batch_id,
        bucket_key=versions.digest((sku.name,bucket.batch_kind,bucket.batch_id)),
        batch_no=mapping.batch,uom=sku.stock_uom,snapshot_quantity=format(quantity,'f'),
        bucket_version=str(versions.read(task.company,bucket.location_id,'BUCKET',sku.name,bucket.batch_kind,bucket.batch_id)),
        confirmed=0,discovered=int(discovered))


def _create(payload, original=None):
    from ..operations.dto import Bucket
    _scope_input(payload)
    location = _location(payload.company,payload.location_id)
    current = versions.verify()
    explicit = None
    if payload.scope == Scope.BUCKET:
        sku,mapping = _selection(payload.sku_id,payload.batch)
        explicit = Bucket(payload.company,sku.name,location.name,mapping.kind,
                          mapping.name if mapping.kind=='BATCH' else None)
    elif payload.scope == Scope.SKU:
        batches._load_sku(payload.sku_id,locked=True)
    buckets = {b for b in current if b.company==payload.company and b.location_id==payload.location_id
               and (payload.scope==Scope.LOCATION or b.sku_id==payload.sku_id)
               and (payload.scope!=Scope.BUCKET or b==explicit)}
    if explicit:
        buckets.add(explicit)
    skus = {sid:batches._load_sku(sid,locked=True) for sid in sorted({b.sku_id for b in buckets})}
    require_native_precision(skus.values(),'COUNT_NATIVE_PRECISION_UNCONFIGURED')
    cutoff = now_datetime()
    balances = availability.read_source(payload.company,
        {(s.native_item,location.native_warehouse) for s in skus.values()},tuple(skus.values()),posting_at=cutoff)[0][0] if skus else {}
    task_id, snapshot_id, now = uuid4().hex,uuid4().hex,cutoff
    store.insert(store.DOCUMENT,name=task_id,company=payload.company,document_type=ACTION,
                 document_number='COUNT-'+task_id,status='Draft',revision=1,notes=payload.notes)
    kind = explicit.batch_kind if explicit else None
    bid = explicit.batch_id if explicit else None
    versions.insert(versions.TASK,name=task_id,company=payload.company,location_id=payload.location_id,
        scope=payload.scope,sku_id=payload.sku_id,batch_kind=kind,batch_id=bid,
        batch_no=payload.batch.get('batchNo') if payload.batch else None,snapshot_id=snapshot_id,
        snapshot_at=now,generation=versions.epoch(),
        scope_version=str(versions.read(payload.company,payload.location_id,payload.scope,payload.sku_id,kind,bid)),
        original_task=original)
    _,task = _head(payload.company,task_id)
    for number,b in enumerate(sorted(buckets,key=lambda b:(b.sku_id,b.batch_kind,b.batch_id or '')),1):
        mapping = batches.lock_execution_mapping(skus[b.sku_id],b.batch_kind,b.batch_id)
        native_batch = frappe.db.get_value(MAPPING,mapping.id,'native_batch',for_update=True)
        qty = balances.get((skus[b.sku_id].native_item,location.native_warehouse,native_batch),Decimal(0))
        _bucket_line(task,b,qty,number)
    return _view(*_head(payload.company,task_id),_rows(task_id))


def create(payload):
    try:
        with connection(read_committed=True) as db:
            lock_stock_writes(payload.company)
            result = _create(payload)
            _commit(db)
            return result
    except BatchError as error:
        raise StockError(error.code) from None


def get(company, task_id):
    with connection(read_committed=True):
        lock_stock_writes(company)
        document,task = _head(company,task_id)
        versions.verify()
        return _view(document,task,_rows(task_id))


def _editable(company, task_id, revision):
    document,task = _head(company,task_id)
    if document.status not in ('Draft', 'Rejected') or task.recount_task:
        raise StockError('COUNT_NOT_EDITABLE')
    if type(revision) is not int or document.revision != revision:
        raise StockError('STOCK_REVISION_CONFLICT')
    rows = _rows(task_id)
    versions.verify()
    _trusted(task,rows)
    return document,task,rows


def _revision(document):
    frappe.db.sql(f'UPDATE `tab{store.DOCUMENT}` SET revision=revision+1,modified=%s,modified_by=%s WHERE name=%s AND revision=%s',
                  (now_datetime(),frappe.session.user,document.name,document.revision))
    store._assert_updated('STOCK_REVISION_CONFLICT')


def record(company, task_id, revision, actuals):
    if not isinstance(actuals,tuple) or not actuals:
        raise StockError('COUNT_ACTUAL_REQUIRED')
    with connection(read_committed=True) as db:
        lock_stock_writes(company)
        document,task,rows = _editable(company,task_id,revision)
        indexed,seen = {r.name:r for r in rows},set()
        now = now_datetime()
        for actual in actuals:
            _text(actual.notes,4000)
            if actual.line_id not in indexed or actual.line_id in seen:
                raise StockError('COUNT_LINE_INVALID')
            seen.add(actual.line_id)
            row = indexed[actual.line_id]
            sku = batches._load_sku(row.sku_id,locked=True)
            qty = quantity_text(actual.quantity,row.uom,sku=sku,sign=QuantitySign.NONNEGATIVE,limits=_authorize(company).limits)
            with localcontext() as context:
                context.prec=300
                difference = format(Decimal(qty)-Decimal(row.snapshot_quantity),'f')
            frappe.db.sql(f'''UPDATE `tab{versions.LINE}` SET actual_quantity=%s,difference_quantity=%s,
                confirmed=1,counted_by=%s,counted_at=%s,notes=%s WHERE name=%s''',
                (qty,difference,frappe.session.user,now,actual.notes,row.name))
        frappe.db.sql(f'UPDATE `tab{versions.TASK}` SET counted_by=%s,counted_at=%s WHERE name=%s',
                      (frappe.session.user,now,task_id))
        _revision(document)
        result = _view(*_head(company,task_id),_rows(task_id))
        _commit(db)
        return result


def discover(company, task_id, revision, sku_id, batch):
    from ..operations.dto import Bucket
    with connection(read_committed=True) as db:
        lock_stock_writes(company)
        document,task,rows = _editable(company,task_id,revision)
        if task.scope == Scope.BUCKET or (task.scope==Scope.SKU and task.sku_id != sku_id):
            raise StockError('COUNT_DISCOVERY_OUT_OF_SCOPE')
        sku,mapping = _selection(sku_id,batch)
        bucket = Bucket(company,sku_id,task.location_id,mapping.kind,mapping.name if mapping.kind=='BATCH' else None)
        if any((r.sku_id,r.batch_kind,r.batch_id or None)==(sku_id,bucket.batch_kind,bucket.batch_id) for r in rows):
            raise StockError('COUNT_BUCKET_ALREADY_INCLUDED')
        # Unchanged enclosing version proves no quantity action since baseline.
        # Every historical bucket (even zero) was included; an absent identity
        # with no ledger history is therefore provably new and originally zero.
        if bucket in versions.facts() or versions.read(company,task.location_id,'BUCKET',sku_id,bucket.batch_kind,bucket.batch_id):
            raise StockError('COUNT_BASELINE_UNPROVEN')
        _bucket_line(task,bucket,Decimal(0),len(rows)+1,discovered=True)
        _revision(document)
        result = _view(*_head(company,task_id),_rows(task_id))
        _commit(db)
        return result


def recount(company, task_id, revision, reason):
    _text(reason,4000,required=True)
    if not reason.strip():
        raise StockError('STOCK_INVALID_TEXT')
    with connection(read_committed=True) as db:
        lock_stock_writes(company)
        document,task = _head(company,task_id)
        if document.status not in ('Draft', 'Rejected') or task.recount_task or document.revision!=revision:
            raise StockError('COUNT_NOT_EDITABLE')
        batch = ({'mode':task.batch_kind,'batchNo':task.batch_no} if task.batch_kind=='BATCH'
                 else {'mode':'NO_BATCH'}) if task.scope==Scope.BUCKET else None
        result = _create(CreateCount(company,task.location_id,task.scope,task.sku_id,batch,reason),task_id)
        frappe.db.sql(f'UPDATE `tab{versions.TASK}` SET recount_task=%s WHERE name=%s',(result.task_id,task_id))
        frappe.db.sql(f'UPDATE `tab{store.DOCUMENT}` SET status=%s,revision=revision+1,reason=%s WHERE name=%s',
                      ('Superseded',reason,task_id))
        _commit(db)
        return result


def _kernel_lines(rows):
    return tuple(sorted((Line(r.name,r.line_no,r.sku_id,r.actual_quantity or '0',r.uom,r.batch_kind,
        r.batch_id or None,target=r.location_id,notes=r.notes or '') for r in rows),key=lambda r:r.line_id))


def _load(company, task_id, document_type):
    document,task = _head(company,task_id)
    return document,_kernel_lines(_rows(task_id))


def _snapshot_identity(task):
    return versions.digest((task.snapshot_id,task.generation,task.scope_version,task.scope,task.location_id,
        task.sku_id,task.batch_kind,task.batch_id,
        [(r.line_id,r.snapshot_quantity,r.bucket_version,r.discovered,r.confirmed) for r in sorted(task.lines,key=lambda r:r.line_id)]))


def _approval_snapshot(task_id):
    fields = ('snapshot_id', 'generation', 'scope_version', 'scope', 'location_id', 'sku_id',
              'batch_kind', 'batch_id', 'snapshot_at')
    task = store.current(versions.TASK, task_id, fields)
    if not task:
        raise StockError('COUNT_SNAPSHOT_MISMATCH')
    line_fields = ('name', 'sku_id', 'location_id', 'batch_kind', 'batch_id', 'snapshot_quantity',
                   'bucket_version', 'confirmed', 'discovered', 'actual_quantity', 'difference_quantity')
    return {**task, 'snapshot_at': str(task.snapshot_at),
            'lines': [{field: row[field] for field in line_fields} for row in _rows(task_id)]}


def _prepare(command, document, stored_lines, locations):
    _,task = _head(command.company,document.name)
    rows = _rows(document.name)
    _trusted(task,rows)
    if any(not r.confirmed or r.actual_quantity is None for r in rows):
        raise StockError('COUNT_ACTUAL_INCOMPLETE')
    current = _view(document,task,rows)
    if command.snapshot_id != _snapshot_identity(current):
        raise StockError('COUNT_SNAPSHOT_MISMATCH')
    # Quantity facts are checked independently of the counters under the guard.
    location = _location(command.company,task.location_id)
    skus = {r.sku_id:batches._load_sku(r.sku_id,locked=True) for r in rows}
    require_native_precision(skus.values(),'COUNT_NATIVE_PRECISION_UNCONFIGURED')
    balances = availability.read_source(command.company,
        {(s.native_item,location.native_warehouse) for s in skus.values()},tuple(skus.values()))[0][0] if skus else {}
    for row in rows:
        mapping = batches.lock_execution_mapping(skus[row.sku_id],row.batch_kind,row.batch_id or None)
        batch = frappe.db.get_value(MAPPING,mapping.id,'native_batch',for_update=True)
        if balances.get((skus[row.sku_id].native_item,location.native_warehouse,batch),Decimal(0)) != Decimal(row.snapshot_quantity):
            raise StockError('COUNT_SNAPSHOT_UNTRUSTED')
    return {r.name:r for r in rows}


def _history(document, stored_lines):
    # Historical completion is immutable; later inventory versions are valid.
    # Reconcile the retained native identities under today's authorization.
    versions.verify()


def _action(candidate):
    return Action(ACTION,policy.Operation.COUNT,QuantitySign.NONNEGATIVE,candidate.limits,
        native.DETAIL_FIELDS,_authorize,lambda plan:native.post(plan,rate=candidate.rate,allow_gl=candidate.allow_gl),
        native.verify_link,native_orm=True,read_committed=True,
        final_status='Completed',require_reason=True,load_document=_load,prepare_plan=_prepare,
        native_line_ids=lambda plan:{rid for rid,r in plan.context.items() if Decimal(r.difference_quantity)},
        allow_empty_lines=True,verify_history=_history)


def complete(payload):
    return _complete(payload)


def _complete(payload, *, before_guard=None, commit=None):
    task = payload.task
    candidate = _authorize(task.company)
    lines = tuple(Line(r.line_id,r.line_no,r.sku_id,r.actual_quantity if r.actual_quantity is not None else '0',
        r.uom,r.batch_kind,r.batch_id,target=r.location_id,notes=r.notes) for r in task.lines)
    command = Command(task.company,task.task_id,task.revision,payload.idempotency_key,lines,payload.reason,
                      snapshot_id=_snapshot_identity(task))
    return _execute(command,_action(candidate),before_guard=before_guard,commit=commit)
