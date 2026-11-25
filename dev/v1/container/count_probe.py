"""Real MariaDB acceptance for non-freezing counts and quantity versions."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import replace
from decimal import Decimal
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

import frappe

from haxpower_v1.access import policy
from haxpower_v1.access.config import set_scope
from haxpower_v1.access.policy import Mode, Operation
from haxpower_v1.inventory import versions
from haxpower_v1.inventory.stock_count import service as counts
from haxpower_v1.inventory.stock_count.dto import Actual, CompleteCount, CreateCount, Scope
from haxpower_v1.inventory.operations import persistence as store
from haxpower_v1.inventory.operations.dto import StockError
from haxpower_v1.inventory.operations.service import _commit
from haxpower_v1.inventory.operations.transaction import connection
from haxpower_v1.inventory.receipt import service as receipts
from haxpower_v1.inventory.receipt.dto import ReceiptDraftInput, ReceiptLineInput, SubmitReceipt
from haxpower_v1.inventory.issue import service as issues
from haxpower_v1.inventory.issue.dto import IssueDraftInput, IssueLineInput, SubmitIssue
from haxpower_v1.warehouse import service as warehouse
from haxpower_v1.warehouse.persistence import LOCATION

from access_probe import synthetic_user
from batch_probe import _sku_id
from identity import require
from receipt_probe import LIMITS, NO_BATCH, STD61, STD62, snapshot as native_snapshot
from sku_probe import PREFIX
from stock_probe import actor_session, session
from warehouse_probe import _company_and_parent


def fresh():
    with connection():
        state = native_snapshot()
        for table in (*versions.TABLES,'Stock Reconciliation','Stock Reconciliation Item'):
            state[table]=frappe.get_all(table,fields=['*'],order_by='name')
        return state


def reject(code, call):
    before=fresh()
    try:
        call()
    except StockError as error:
        require(error.code==code,f'expected {code}, got {error.code}')
    else:
        raise RuntimeError('expected count rejection '+code)
    require(fresh()==before,'rejected/faulted count left native, version, task, request or log changes')


def create(company,location,scope=Scope.BUCKET,sku=None,batch=NO_BATCH):
    return counts.create(CreateCount(company,location,scope,sku,batch if scope==Scope.BUCKET else None))


def actual(task, values=None):
    values=values or {r.line_id:r.snapshot_quantity for r in task.lines}
    return counts.record(task.company,task.task_id,task.revision,
                         tuple(Actual(r.line_id,values[r.line_id]) for r in task.lines if r.line_id in values))


def finish(task, *, key=None, **kwargs):
    return counts._complete(CompleteCount(task,key or uuid4().hex,'Synthetic physical count'),**kwargs)


def receipt(company,sku,location,batch,qty='1'):
    draft=receipts.create_draft(ReceiptDraftInput(company,(ReceiptLineInput(sku,location,batch,qty,PREFIX),)))
    return receipts.submit(SubmitReceipt(draft,uuid4().hex))


def issue(company,sku,location,batch,qty='1'):
    draft=issues.create_draft(IssueDraftInput(company,(IssueLineInput(sku,location,batch,qty,PREFIX),)))
    return issues.submit(SubmitIssue(draft,uuid4().hex))


def counters(company,location,sku,kind='NO_BATCH',bid=None):
    from haxpower_v1.inventory.guard import lock_stock_writes
    with connection():
        lock_stock_writes(company)
        return tuple(versions.read(company,location,scope,sku if scope!='LOCATION' else None,kind,bid)
                     for scope in ('LOCATION','SKU','BUCKET'))


def verify_effect(task, result, before):
    after=fresh()
    expected={r.line_id:Decimal(r.actual_quantity)-Decimal(r.snapshot_quantity) for r in task.lines}
    logs=[r for r in after[store.LOG] if r.name==result.operation_id]
    require(len(logs)==1 and logs[0].action==counts.ACTION and logs[0].actor==frappe.session.user,
            'count operation ownership/semantics lost')
    added={table:[r for r in records if r.name not in {x.name for x in before[table]}] for table,records in after.items()}
    links=added[store.LINK]
    require({r.line_id for r in links}=={rid for rid,delta in expected.items() if delta},'native links cover wrong count rows')
    require(logs[0].quantity_effect==('Native' if links else 'None'),'missing explicit no-quantity effect type')
    for link in links:
        sle=[r for r in added['Stock Ledger Entry'] if r.voucher_no==link.parent_id and r.voucher_detail_no==link.detail_id]
        require(sum((Decimal(str(r.actual_qty)) for r in sle),Decimal(0))==expected[link.line_id],
                'native reconciliation did not net its multi-SLE batch difference')
    if not links:
        for table in ('Stock Entry','Stock Reconciliation','Stock Ledger Entry','Serial and Batch Bundle','Bin','Batch',versions.VERSION):
            require(after[table]==before[table],'no difference fabricated native stock or advanced quantity versions')
    with connection():
        from haxpower_v1.inventory.guard import lock_stock_writes
        lock_stock_writes()
        versions.verify()
    return after


def compete(site,actor,company,first,second):
    barrier=Barrier(2,timeout=60)
    ids=[]
    def before_guard(db):
        # Deliberately establish a pre-lock database view.
        db.sql('SELECT COUNT(*) FROM `tabStock Ledger Entry`')
        ids.append(db.sql('SELECT CONNECTION_ID()')[0][0])
        barrier.wait()
    def run(task):
        with session(site,actor,company),counts.synthetic_candidate(company,limits=LIMITS):
            try:
                return finish(task,before_guard=before_guard)
            except StockError as error:
                return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=tuple(pool.map(run,(first,second)))
    require(results.count('COUNT_SCOPE_CHANGED')==1,'same-version counts did not serialize to one winner')
    with connection() as third:
        ids.append(third.sql('SELECT CONNECTION_ID()')[0][0])
        from haxpower_v1.inventory.guard import lock_stock_writes
        lock_stock_writes(company)
        versions.verify()
    require(len(set(ids))==3,'count race did not use independent writers and verifier')
    print('COUNT race: independent writers with pre-lock views, one adjustment, one stale refusal; third connection verified.',flush=True)


def run(site):
    frappe.db.set_default("float_precision", "3")
    for purpose in ('Material Receipt','Material Issue'):
        if not frappe.db.exists('Stock Entry Type',purpose):
            frappe.get_doc(doctype='Stock Entry Type',purpose=purpose,is_standard=1).insert(set_name=purpose)
    company,_=_company_and_parent()
    warehouses=warehouse.list_warehouses(company=company)
    code='C'+uuid4().hex[:10].upper()
    locations=[warehouse.create_location(w.id,code=code,display_name='Synthetic count '+code).id for w in warehouses[:2]]
    source,other=locations
    empty_source=warehouse.create_location(warehouses[0].id,code=code+'E',display_name='Synthetic empty count').id
    actor,_=synthetic_user(uuid4().hex[:10],'count',Operation.COUNT,Operation.RECEIVE,Operation.ISSUE,Operation.TRANSFER)
    set_scope(actor,company,Mode.SELECTED,[w.id for w in warehouses[:2]])
    frappe.db.commit()
    sku,brand=_sku_id('A'),_sku_id('B')
    with (actor_session(actor,company),counts.synthetic_candidate(company,limits=LIMITS),
          receipts.synthetic_candidate(company,limits=LIMITS),issues.synthetic_candidate(company,limits=LIMITS)):
        for sid,batch,qty in ((sku,NO_BATCH,'8'),(sku,STD61,'10'),(sku,STD62,'5'),(brand,NO_BATCH,'3')):
            receipt(company,sid,source,batch,qty)
        empty=create(company,empty_source,Scope.LOCATION)
        before=fresh();verify_effect(empty,finish(empty),before)
        empty=actual(create(company,empty_source,sku=sku))
        before=fresh();verify_effect(empty,finish(empty),before)
        task=create(company,source,Scope.LOCATION)
        require(len(task.lines)==4 and not any(r.confirmed for r in task.lines),'LOCATION snapshot omitted batch/brand buckets')
        reject('COUNT_ACTUAL_INCOMPLETE',lambda:finish(task))
        for value,code in ((None,'STOCK_QUANTITY_INVALID'),(0.0,'STOCK_QUANTITY_INVALID'),
                           ('-1','STOCK_QUANTITY_SIGN'),('0.0001','STOCK_QUANTITY_PRECISION')):
            reject(code,lambda:counts.record(company,task.task_id,task.revision,(Actual(task.lines[0].line_id,value),)))
        values={r.line_id:('5' if r.sku_id==sku and r.batch_kind=='NO_BATCH' else
                          '0' if r.batch_no=='STD62' else
                          '12' if r.batch_no=='STD61' else r.snapshot_quantity) for r in task.lines}
        task=actual(task,values)
        before=fresh(); key=uuid4().hex
        result=finish(task,key=key); verify_effect(task,result,before)
        after=fresh()
        require(finish(task,key=key)==result and fresh()==after,'count idempotent replay wrote inventory/version')
        equivalent=replace(task,lines=tuple(replace(r,actual_quantity=r.actual_quantity+'.000' if '.' not in r.actual_quantity else r.actual_quantity+'0') for r in task.lines))
        require(finish(equivalent,key=key)==result and fresh()==after,'equivalent decimal actuals changed the canonical replay')
        reject('STOCK_IDEMPOTENCY_CONFLICT',lambda:finish(replace(task,lines=(replace(task.lines[0],actual_quantity='99'),*task.lines[1:])),key=key))
        reject('STOCK_DOCUMENT_ALREADY_EFFECTIVE',lambda:finish(task))
        noop=actual(create(company,source,Scope.SKU,sku))
        before=fresh(); verify_effect(noop,finish(noop),before)
        # Zero-book and explicit actual zero are both valid; no fabricated SLE for 0->0.
        zero=create(company,source,sku=sku,batch=STD62)
        gain=actual(zero,{zero.lines[0].line_id:'1.125'})
        before=fresh();verify_effect(gain,finish(gain),before)
        explicit=create(company,source,sku=sku,batch=STD62)
        # use its own stable line ID
        explicit=counts.record(company,explicit.task_id,explicit.revision,(Actual(explicit.lines[0].line_id,'0'),))
        before=fresh();verify_effect(explicit,finish(explicit),before)
        new=create(company,other,Scope.SKU,sku)
        new=counts.discover(company,new.task_id,new.revision,sku,{'mode':'BATCH','batchNo':code+'FOUND'})
        require(len(new.lines)==1 and new.lines[0].discovered and new.lines[0].snapshot_quantity=='0','discovery baseline lost')
        reject('COUNT_DISCOVERY_OUT_OF_SCOPE',lambda:counts.discover(company,new.task_id,new.revision,brand,NO_BATCH))
        invalid=create(company,other,Scope.LOCATION)
        reject('BATCH_SKU_UNAVAILABLE',lambda:counts.discover(company,invalid.task_id,invalid.revision,'f'*32,NO_BATCH))
        reject('INVALID_BATCH_SELECTION',lambda:counts.discover(company,new.task_id,new.revision,sku,{'mode':'UNKNOWN'}))
        new=actual(new,{new.lines[0].line_id:'2.125'})
        before=fresh();verify_effect(new,finish(new),before)
        print('COUNT native shapes: mixed reconciliation, explicit zero, zero-book gain, discovery and no-op verified.',flush=True)
        # Version ranges: unrelated SKU, batch and location must not invalidate a bucket.
        isolated=actual(create(company,source,sku=sku))
        receipt(company,sku,source,STD61)
        receipt(company,brand,source,NO_BATCH)
        receipt(company,sku,other,NO_BATCH)
        before=fresh();verify_effect(isolated,finish(isolated),before)
        sku_task=actual(create(company,source,Scope.SKU,sku))
        receipt(company,brand,source,NO_BATCH)
        receipt(company,sku,other,NO_BATCH)
        before=fresh();verify_effect(sku_task,finish(sku_task),before)
        old=actual(create(company,source,sku=sku))
        baseline=counters(company,source,sku)
        issue(company,sku,source,NO_BATCH)
        receipt(company,sku,source,NO_BATCH)
        require(counters(company,source,sku)==tuple(x+2 for x in baseline),'round-trip quantity actions did not advance all versions')
        reject('COUNT_SCOPE_CHANGED',lambda:finish(old))
        replacement=counts.recount(company,old.task_id,old.revision,'Synthetic count became stale')
        require(replacement.original_task==old.task_id and not any(r.confirmed for r in replacement.lines)
                and counts.get(company,old.task_id).status=='Superseded','recount rewrote/copied old actuals')
        # New bucket in LOCATION or SKU range invalidates a snapshot with no initial line for it.
        for scope in (Scope.LOCATION,Scope.SKU):
            old=actual(create(company,source,scope,sku if scope==Scope.SKU else None))
            receipt(company,sku,source,{'mode':'BATCH','batchNo':uuid4().hex})
            reject('COUNT_SCOPE_CHANGED',lambda:finish(old))
        print('COUNT ranges: unrelated changes, round-trip stale and new-bucket invalidation verified.',flush=True)
        first=create(company,source,sku=sku);second=create(company,source,sku=sku)
        first=actual(first,{first.lines[0].line_id:'4'});second=actual(second,{second.lines[0].line_id:'3'})
        compete(site,actor,company,first,second)
        fault_task=create(company,source,Scope.SKU,sku)
        fault_task=actual(fault_task,{r.line_id:format(Decimal(r.snapshot_quantity)+1,'f') for r in fault_task.lines})
        same_task_races(site,actor,company,source,sku)
        # Real native posting runs; inject failure only at each persistence boundary.
        original_insert=store.insert
        original_write=versions._write
        original_update=store.update_document
        original_complete=store.complete_request
        def fail_version(*args,**kwargs):
            original_write(*args,**kwargs)
            raise RuntimeError('Synthetic version SQL completed then failure')
        def fail_state(*args):
            original_update(*args)
            raise RuntimeError('Synthetic task state SQL completed then failure')
        def fail_result(*args):
            original_complete(*args)
            raise RuntimeError('Synthetic request SQL completed then failure')
        def fail_log(doctype,**kwargs):
            if doctype==store.LOG: raise RuntimeError('synthetic post-native log failure')
            return original_insert(doctype,**kwargs)
        for target,method,replacement in (
            (store,'insert',fail_log),
            (versions,'_write',fail_version),
            (store,'update_document',fail_state),
            (store,'complete_request',fail_result),
        ):
            with patch.object(target,method,replacement):
                reject('STOCK_PERSISTENCE_FAILED',lambda:finish(fault_task))
        def pre_commit_disconnect(db):
            with connection() as killer:
                connection_id=db.sql('SELECT CONNECTION_ID()')[0][0]
                killer.sql('KILL CONNECTION '+str(connection_id))
            _commit(db)
        reject('STOCK_RETRY_REQUIRED',lambda:finish(fault_task,commit=pre_commit_disconnect))
        def lost_response(db):
            _commit(db)
            raise OSError('Synthetic committed response lost')
        before=fresh();result=finish(fault_task,commit=lost_response);verify_effect(fault_task,result,before)
        # History replay after subsequent quantity changes uses current authorization.
        receipt(company,sku,source,NO_BATCH)
        before=fresh()
        original_result=finish(task,key=key)
        require(original_result.document_id==task.task_id and fresh()==before,'historical count replay mutated after new quantities')
    set_scope(actor,company,Mode.NONE);frappe.db.commit()
    with actor_session(actor,company),counts.synthetic_candidate(company,limits=LIMITS):
        before=fresh()
        try: finish(task,key=key)
        except frappe.PermissionError: pass
        else: raise RuntimeError('revoked count replay was authorized')
        require(fresh()==before,'revoked request left changes')
    print('COUNT native matrix: LOCATION/SKU/BUCKET, Batch/NO_BATCH/brand, explicit zero, 0->gain, multi-SLE netting, '
          'discovery, no-op, range isolation, round-trip stale/recount, replay/revocation, faults and disconnect recovery passed.',flush=True)


def extended(site):
    """Quantity-write coverage, races, generation and native maintenance."""
    from haxpower_v1.inventory.transfer import service as transfers, dto as transfer_dto, transit
    from haxpower_v1.inventory.guard import lock_stock_writes
    from haxpower_v1.adapters.erpnext import stock_count as native
    from haxpower_v1.adapters.erpnext import stock_entry
    from erpnext.stock import stock_ledger
    from datetime import timedelta
    from frappe.utils import now_datetime
    company,_=_company_and_parent()
    warehouses=warehouse.list_warehouses(company=company)
    code='X'+uuid4().hex[:10].upper()
    source,target=[warehouse.create_location(w.id,code=code,display_name=code).id for w in warehouses[:2]]
    actor,_=synthetic_user(uuid4().hex[:10],'count-extra',Operation.COUNT,Operation.RECEIVE,Operation.ISSUE,
                           Operation.TRANSFER,Operation.SYSTEM_MAINTAIN)
    set_scope(actor,company,Mode.SELECTED,[w.id for w in warehouses[:2]])
    for purpose in ('Material Receipt','Material Issue','Material Transfer'):
        if not frappe.db.exists('Stock Entry Type',purpose):
            frappe.get_doc(doctype='Stock Entry Type',purpose=purpose,is_standard=1).insert(set_name=purpose)
    if not frappe.db.exists('Stock Entry Type',stock_entry.DISPATCH_TYPE):
        frappe.get_doc(doctype='Stock Entry Type',purpose='Material Transfer',is_standard=0,add_to_transit=1).insert(set_name=stock_entry.DISPATCH_TYPE)
    frappe.db.commit()
    sku=_sku_id('A')
    with (actor_session(actor,company),counts.synthetic_candidate(company,limits=LIMITS),
          receipts.synthetic_candidate(company,limits=LIMITS),issues.synthetic_candidate(company,limits=LIMITS),
          transfers.synthetic_candidate(company,limits=LIMITS)):
        def changed(before,locations):
            for location,old in before.items():
                require(counters(company,location,sku)==tuple(v+int(location in locations) for v in old),
                        'quantity command advanced wrong source/Transit/target versions')
        receipt(company,sku,source,NO_BATCH,'30')
        with connection():
            lock_stock_writes(company)
            transit_id=transit.resolve(company).name
        def state():return {l:counters(company,l,sku) for l in (source,target,transit_id)}
        old=actual(create(company,source,sku=sku))
        before=state()
        draft=transfers.create_draft(transfer_dto.TransferDraftInput(company,(
            transfer_dto.TransferLineInput(sku,source,target,NO_BATCH,'10',PREFIX),),'Synthetic count version transfer'))
        transfers.dispatch(transfer_dto.DispatchTransfer(draft,uuid4().hex))
        changed(before,{source,transit_id});reject('COUNT_SCOPE_CHANGED',lambda:finish(old))
        dispatch=transfers.get_dispatch(company,draft.transfer_id)
        old=actual(create(company,target,sku=sku))
        before=state()
        transfers.receive(transfer_dto.ReceiveTransfer(company,dispatch.transfer_id,dispatch.revision,uuid4().hex,
            (transfer_dto.TransferQuantity(dispatch.lines[0].transfer_line_id,'6'),),'Synthetic receipt'))
        changed(before,{transit_id,target});reject('COUNT_SCOPE_CHANGED',lambda:finish(old))
        dispatch=transfers.get_dispatch(company,draft.transfer_id)
        old=actual(create(company,source,sku=sku))
        before=state()
        transfers.return_remaining(transfer_dto.ReturnTransfer(company,dispatch.transfer_id,dispatch.revision,uuid4().hex,
            (transfer_dto.TransferQuantity(dispatch.lines[0].transfer_line_id,'4'),),'Synthetic return'))
        changed(before,{transit_id,source});reject('COUNT_SCOPE_CHANGED',lambda:finish(old))
        print('Version coverage: receipt, issue, dispatch, receive and return; actual physical/Transit legs checked.',flush=True)
        # The approval seam has no stock effect until actual execution; a delayed
        # executor must reject once intervening quantities have changed.
        pending=actual(create(company,source,sku=sku))
        receipt(company,sku,source,NO_BATCH)
        reject('COUNT_SCOPE_CHANGED',lambda:finish(pending))
        # Task age alone has no effect when the generation and range are stable.
        timeless=actual(create(company,source,sku=sku))
        with patch.object(counts,'now_datetime',lambda:now_datetime()+timedelta(days=365)):
            before=fresh();verify_effect(timeless,finish(timeless),before)
        # Normal native valuation replay must retain the quantity epoch/counters.
        stable=actual(create(company,source,sku=sku))
        with connection(read_committed=True) as db:
            lock_stock_writes(company)
            oldfacts=versions.verify()
            oldversions=frappe.get_all(versions.VERSION,fields=['*'],order_by='name')
            item=frappe.db.get_value('HP V1 Stock Item',sku,'native_item')
            leaf=frappe.db.get_value(LOCATION,source,'native_warehouse')
            sle=frappe.db.get_value('Stock Ledger Entry',{'item_code':item,'warehouse':leaf},
                ('name','posting_date','posting_time','voucher_type','voucher_no','voucher_detail_no','serial_and_batch_bundle','actual_qty','creation'),as_dict=True,order_by='creation desc')
            # Use the existing native adapter's exact voucher/Bundle scope for
            # this synthetic maintenance proof; background maintenance stays closed.
            from haxpower_v1.inventory.operations.dto import Bucket
            from haxpower_v1.inventory.operations.transaction import adapter_transaction
            from haxpower_v1.inventory.batch.persistence import MAPPING
            require(sle.voucher_type==stock_entry.ENTRY and Decimal(str(sle.actual_qty))>0,
                    'revaluation fixture must target the latest material receipt')
            batch=frappe.db.get_value(MAPPING,{'sku':sku,'kind':'NO_BATCH'},'native_batch',for_update=True)
            leg=stock_entry.Leg('synthetic-revaluation',Bucket(company,sku,source,'NO_BATCH',None),
                                item,leaf,batch,PREFIX,Decimal(str(sle.actual_qty)))
            entry=frappe.get_doc(stock_entry.ENTRY,sle.voucher_no,for_update=True)
            scope=stock_entry.Posting(actor,'synthetic-revaluation',company,entry,(leg,),
                {sle.voucher_detail_no:leg},{(sle.voucher_detail_no,leaf):sle.serial_and_batch_bundle})
            token=stock_entry._scope.set(scope)
            try:
                with adapter_transaction(db,native_orm=True):
                    stock_ledger.update_entries_after(dict(item_code=item,warehouse=leaf,posting_date=sle.posting_date,
                        posting_time=sle.posting_time,voucher_type=sle.voucher_type,voucher_no=sle.voucher_no,
                        sle_id=sle.name,creation=sle.creation,reserved_stock=0),allow_negative_stock=False)
            finally:
                stock_entry._scope.reset(token)
            require(versions.verify()==oldfacts and frappe.get_all(versions.VERSION,fields=['*'],order_by='name')==oldversions,
                    'normal valuation replay advanced quantity versions')
            _commit(db)
        before=fresh();verify_effect(stable,finish(stable),before)
        # Epoch switch preserves counters but invalidates even unchanged quantities.
        old=actual(create(company,source,sku=sku))
        with connection(read_committed=True) as db:
            lock_stock_writes(company)
            versions._rotate_generation_locked('Synthetic recovery/upgrade continuity rehearsal')
            _commit(db)
        reject('COUNT_GENERATION_CHANGED',lambda:finish(old))
        reopened=counts.recount(company,old.task_id,old.revision,'Synthetic generation changed')
        reopened=actual(reopened)
        before=fresh();verify_effect(reopened,finish(reopened),before)
        # Concurrent count versus each write uses two independent MariaDB
        # transactions and a third verifier. Both legal serial orders are checked.
        for kind in ('receipt','issue','dispatch','receive','return','new-bucket'):
            selected=source if kind not in ('receive',) else target
            if kind in ('receive','return'):
                draft=transfers.create_draft(transfer_dto.TransferDraftInput(company,(
                    transfer_dto.TransferLineInput(sku,source,target,NO_BATCH,'2',PREFIX),),'Synthetic race'))
                transfers.dispatch(transfer_dto.DispatchTransfer(draft,uuid4().hex))
                dispatch=transfers.get_dispatch(company,draft.transfer_id)
                cls=transfer_dto.ReceiveTransfer if kind=='receive' else transfer_dto.ReturnTransfer
                write=cls(company,dispatch.transfer_id,dispatch.revision,uuid4().hex,
                    (transfer_dto.TransferQuantity(dispatch.lines[0].transfer_line_id,'1'),),'Synthetic race')
            elif kind=='dispatch':
                draft=transfers.create_draft(transfer_dto.TransferDraftInput(company,(
                    transfer_dto.TransferLineInput(sku,source,target,NO_BATCH,'1',PREFIX),),'Synthetic race'))
                write=transfer_dto.DispatchTransfer(draft,uuid4().hex)
            elif kind=='issue':
                write=SubmitIssue(issues.create_draft(IssueDraftInput(company,(
                    IssueLineInput(sku,source,NO_BATCH,'1',PREFIX),))),uuid4().hex)
            else:
                selection=NO_BATCH if kind=='receipt' else {'mode':'BATCH','batchNo':uuid4().hex}
                write=SubmitReceipt(receipts.create_draft(ReceiptDraftInput(company,(
                    ReceiptLineInput(sku,selected,selection,'1',PREFIX),))),uuid4().hex)
            task=create(company,selected,Scope.LOCATION if kind=='new-bucket' else Scope.BUCKET,
                        None if kind=='new-bucket' else sku)
            task=actual(task,{r.line_id:format(Decimal(r.snapshot_quantity)+1,'f') for r in task.lines})
            barrier=Barrier(2,timeout=60);ids=[]
            def before_guard(db):
                db.sql('SELECT COUNT(*) FROM `tabStock Ledger Entry`')
                ids.append(db.sql('SELECT CONNECTION_ID()')[0][0]);barrier.wait()
            def compete_writer(which):
                with (session(site,actor,company),counts.synthetic_candidate(company,limits=LIMITS),
                      receipts.synthetic_candidate(company,limits=LIMITS),issues.synthetic_candidate(company,limits=LIMITS),
                      transfers.synthetic_candidate(company,limits=LIMITS)):
                    try:
                        if which==0:return finish(task,before_guard=before_guard)
                        if kind in ('receipt','new-bucket'):return receipts._submit(write,before_guard=before_guard)
                        if kind=='issue':return issues._submit(write,before_guard=before_guard)
                        if kind=='dispatch':return transfers._dispatch(write,before_guard=before_guard)
                        return transfers._followup(write,'Receive' if kind=='receive' else 'Return',before_guard=before_guard)
                    except StockError as error:return error.code
            original_action=counts._action
            with patch.object(counts,'_action',lambda c:replace(original_action(c),read_committed=False)),ThreadPoolExecutor(max_workers=2) as pool:
                results=tuple(pool.map(compete_writer,(0,1)))
            require(not isinstance(results[1],str) and (not isinstance(results[0],str) or results[0]=='COUNT_SCOPE_CHANGED'),
                    f'count/{kind} race failed valid serialized result: {results}')
            with connection() as third:
                ids.append(third.sql('SELECT CONNECTION_ID()')[0][0]);lock_stock_writes(company);versions.verify()
            require(len(set(ids))==3,'quantity race reused database connections')
        print('COUNT extended matrix: all five write paths, delayed execution stale refusal, no time limit, '
              'normal native valuation, generation invalidation and six independent-connection races passed.',flush=True)


def recovery_prepare():
    company,_=_company_and_parent()
    w=warehouse.list_warehouses(company=company)[0]
    code='R'+uuid4().hex[:10].upper()
    location=warehouse.create_location(w.id,code=code,display_name='Synthetic recovery count').id
    actor,_=synthetic_user(uuid4().hex[:10],'count-recovery',Operation.COUNT)
    set_scope(actor,company,Mode.SELECTED,[w.id]);frappe.db.commit()
    with actor_session(actor,company),counts.synthetic_candidate(company,limits=LIMITS):
        task=counts.create(CreateCount(company,location,Scope.BUCKET,_sku_id('A'),NO_BATCH,'Synthetic V1-12 recovery marker'))
        actual(task,{task.lines[0].line_id:'1.125'})
    print('Prepared confirmed zero-book count at the consistent backup recovery point.',flush=True)


def recovery_finish():
    row=frappe.db.get_value(store.DOCUMENT,{'notes':'Synthetic V1-12 recovery marker'},('name','company','owner'),as_dict=True)
    require(bool(row),'restored count task missing')
    with actor_session(row.owner,row.company),counts.synthetic_candidate(row.company,limits=LIMITS):
        task=counts.get(row.company,row.name)
        require(task.status=='Draft' and not task.stale_reason and task.lines[0].actual_quantity=='1.125',
                'restore lost count/version/snapshot consistency')
        before=fresh();verify_effect(task,finish(task),before)
    print('Restored confirmed task completed using its restored native stock and quantity epoch.',flush=True)


def native_boundaries(site):
    """Native close/freeze/GL, second-row failure and employee CRUD denials."""
    from haxpower_v1.adapters.erpnext import stock_count as native
    from frappe.utils import nowdate
    company,_=_company_and_parent()
    w=warehouse.list_warehouses(company=company)[0]
    code='F'+uuid4().hex[:10].upper()
    location=warehouse.create_location(w.id,code=code,display_name=code).id
    actor,_=synthetic_user(uuid4().hex[:10],'count-native',Operation.COUNT,Operation.RECEIVE)
    set_scope(actor,company,Mode.SELECTED,[w.id]);frappe.db.commit()
    sku=_sku_id('A')
    with (actor_session(actor,company),counts.synthetic_candidate(company,limits=LIMITS),
          receipts.synthetic_candidate(company,limits=LIMITS)):
        for batch in (NO_BATCH,STD61):receipt(company,sku,location,batch,'3')
        task=create(company,location,Scope.SKU,sku)
        task=actual(task,{r.line_id:'2' for r in task.lines})
        original=native.allows_write
        reached=[]
        def second_ledger(doc,event):
            if doc.doctype=='Stock Ledger Entry' and event=='before_insert':
                reached.append(doc.name)
                if len(reached)==3:raise RuntimeError('Synthetic second native count row failure')
            return original(doc,event)
        with patch.object(native,'allows_write',second_ledger):
            reject('STOCK_PERSISTENCE_FAILED',lambda:finish(task))
        require(len(reached)==3,'fault did not reach second native count row')
        baseline=fresh()
        # Managed tasks/versions and native records remain closed to direct CRUD.
        for table in (*versions.TABLES,store.DOCUMENT,versions.LINE):
            try:
                frappe.get_doc(doctype=table).insert(ignore_permissions=True)
            except frappe.PermissionError:pass
            else:raise RuntimeError('protected count/version CRUD was accepted')
        doc=frappe.get_doc(doctype='Stock Reconciliation',purpose='Stock Reconciliation',company=company,
            posting_date=nowdate(),items=[dict(item_code=frappe.db.get_value('HP V1 Stock Item',sku,'native_item'),
                warehouse=frappe.db.get_value(LOCATION,location,'native_warehouse'),qty=1)])
        try:doc.insert(ignore_permissions=True)
        except frappe.PermissionError:pass
        else:raise RuntimeError('native employee reconciliation bypass accepted')
        # Roll back failed direct ORM attempts before comparing on new connection.
        frappe.db.rollback()
        require(fresh()==baseline,'native/direct record denial left count side effects')
    today=nowdate()
    period=frappe.get_doc(doctype='Accounting Period',period_name='Synthetic Count '+code,company=company,
        start_date=today,end_date=today,closed_documents=[dict(document_type='Stock Reconciliation',closed=1)]).insert()
    frappe.db.commit()
    try:
        with actor_session(actor,company),counts.synthetic_candidate(company,limits=LIMITS):
            reject('STOCK_PERSISTENCE_FAILED',lambda:finish(task))
    finally:
        frappe.delete_doc(period.doctype,period.name);frappe.db.commit()
    old=frappe.db.sql("SELECT value FROM tabSingles WHERE doctype='Stock Settings' AND field='stock_frozen_upto'")
    frappe.db.set_single_value('Stock Settings','stock_frozen_upto',today,update_modified=False)
    frappe.clear_document_cache('Stock Settings','Stock Settings');frappe.db.commit()
    try:
        with actor_session(actor,company),counts.synthetic_candidate(company,limits=LIMITS):
            reject('STOCK_PERSISTENCE_FAILED',lambda:finish(task))
    finally:
        if old:
            frappe.db.set_single_value('Stock Settings','stock_frozen_upto',old[0][0],update_modified=False)
        else:
            frappe.db.delete('Singles',{'doctype':'Stock Settings','field':'stock_frozen_upto'})
        frappe.clear_document_cache('Stock Settings','Stock Settings');frappe.db.commit()
    with actor_session(actor,company),counts.synthetic_candidate(company,limits=LIMITS):
        before=fresh();result=finish(task);verify_effect(task,result,before)
        link=next(r for r in fresh()[store.LINK] if r.operation_id==result.operation_id)
        for action in ('cancel','delete','change'):
            baseline=fresh()
            doc=frappe.get_doc('Stock Reconciliation',link.parent_id)
            doc.flags.ignore_permissions=True
            try:
                if action=='cancel':doc.cancel()
                elif action=='delete':doc.delete(ignore_permissions=True)
                else:doc.db_set('purpose','Opening Stock')
            except (frappe.PermissionError,frappe.ValidationError):pass
            else:raise RuntimeError('native count lifecycle bypass accepted '+action)
            frappe.db.rollback()
            require(fresh()==baseline,'native count lifecycle refusal changed stock or versions')
    # A separate synthetic company proves actual perpetual inventory GL behavior.
    from warehouse_probe import make_company
    marker='G'+uuid4().hex[:6].upper()
    valued_company,parent=make_company(marker)
    master=frappe.get_doc('Company',valued_company)
    master.enable_perpetual_inventory=1
    master.default_inventory_account=frappe.db.get_value('Account',{'company':valued_company,'account_type':'Stock','is_group':0})
    master.stock_adjustment_account=frappe.db.get_value('Account',{'company':valued_company,'account_type':'Stock Adjustment','is_group':0})
    master.save(ignore_permissions=True)
    logical=warehouse.create_warehouse(company=valued_company,native_parent=parent,code='W1',display_name=marker)
    leaf=warehouse.create_location(logical.id,code='A01',display_name=marker)
    set_scope(actor,valued_company,Mode.SELECTED,[logical.id]);frappe.db.commit()
    with (actor_session(actor,valued_company),receipts.synthetic_candidate(valued_company,limits=LIMITS,rate=Decimal('4.25'),allow_gl=True),
          counts.synthetic_candidate(valued_company,limits=LIMITS,rate=Decimal('4.25'))):
        receipt(valued_company,sku,leaf.id,NO_BATCH,'2')
        task=create(valued_company,leaf.id,sku=sku)
        task=actual(task,{task.lines[0].line_id:'1'})
        observed=[];verify=native._verify
        def capture(scope,*args):
            observed.extend(frappe.db.get_values('GL Entry',{'voucher_type':native.ENTRY,'voucher_no':scope['entry'].name},
                                               ('debit','credit'),as_dict=True,for_update=True))
            return verify(scope,*args)
        with patch.object(native,'_verify',capture):reject('COUNT_GL_NOT_ALLOWED',lambda:finish(task))
        require(sum(Decimal(str(r.debit)) for r in observed)==Decimal('4.25')
                and sum(Decimal(str(r.credit)) for r in observed)==Decimal('4.25'),'count did not exercise real balanced 4.25 GL')
    print('COUNT native boundaries: second row rollback, direct native/system CRUD denial, closed period, freeze and 4.25 GL rejection passed.',flush=True)


def backfill_fault():
    """Invoke real initialization with a second-counter fault on the V1-11 upgrade."""
    require(not frappe.db.get_value(versions.EPOCH,'inventory'),'backfill fault needs the uninitialized upgrade state')
    before=fresh()
    original=versions.insert
    writes=[]
    def fail_second(doctype,**fields):
        if doctype==versions.VERSION:
            writes.append(fields)
            if len(writes)==2:raise RuntimeError('Synthetic second inventory baseline counter failure')
        return original(doctype,**fields)
    try:
        with connection(read_committed=True),patch.object(versions,'insert',fail_second):
            versions.ensure_schema()
    except RuntimeError as error:
        require(str(error)=='Synthetic second inventory baseline counter failure','unexpected backfill failure')
    else:raise RuntimeError('backfill did not reach injected baseline failure')
    require(len(writes)==2 and fresh()==before,'failed baseline left Ready epoch, partial counters or native changes')
    with connection():
        from haxpower_v1.inventory.guard import lock_stock_writes
        lock_stock_writes()
        try:versions.epoch()
        except StockError as error:require(error.code=='INVENTORY_VERSION_NOT_READY','wrong missing epoch refusal')
        else:raise RuntimeError('failed backfill left quantity execution open')
    print('V1-11 upgrade baseline failure: actual initializer rolled back epoch and partial counters; quantity execution remains closed.',flush=True)


def same_task_races(site,actor,company,location,sku):
    for same_key in (True,False):
        task=create(company,location,sku=sku)
        task=actual(task,{task.lines[0].line_id:format(Decimal(task.lines[0].snapshot_quantity)+1,'f')})
        old=counters(company,location,sku)
        barrier=Barrier(2,timeout=60);ids=[];key=uuid4().hex
        def before_guard(db):
            ids.append(db.sql('SELECT CONNECTION_ID()')[0][0]);barrier.wait()
        def run(index):
            with session(site,actor,company),counts.synthetic_candidate(company,limits=LIMITS):
                try:return finish(task,key=key if same_key else key+str(index),before_guard=before_guard)
                except StockError as error:return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:results=tuple(pool.map(run,(0,1)))
        if same_key:require(results[0]==results[1] and not isinstance(results[0],str),'concurrent count retry was not idempotent')
        else:require(results.count('STOCK_DOCUMENT_ALREADY_EFFECTIVE')==1,'different-key same-task count completed twice')
        with connection() as third:
            from haxpower_v1.inventory.guard import lock_stock_writes
            lock_stock_writes(company);ids.append(third.sql('SELECT CONNECTION_ID()')[0][0]);versions.verify()
        require(len(set(ids))==3 and counters(company,location,sku)==tuple(x+1 for x in old),'same-task retry advanced inventory twice')
    print('COUNT same-task races: same-key replay and different-key uniqueness verified by third connection.',flush=True)
