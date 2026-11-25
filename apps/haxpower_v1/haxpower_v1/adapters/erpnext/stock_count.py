"""Pinned ERPNext Stock Reconciliation, including per-bucket multi-SLE netting."""
from contextvars import ContextVar
from dataclasses import dataclass
from decimal import Decimal

import frappe
from frappe.utils import now_datetime

from ...inventory.operations.dto import NativeLink, StockError
from ...inventory.operations.transaction import _native_orm_active
from ...inventory.batch.persistence import MAPPING
from . import stock_entry as stock

ENTRY = 'Stock Reconciliation'
DETAIL = 'Stock Reconciliation Item'
DETAIL_FIELDS = {(ENTRY,DETAIL):'items'}
_scope = ContextVar('hp_v1_native_count',default=None)


@dataclass(frozen=True)
class CountRow:
    line_id: str
    item: str
    leaf: str
    batch: str
    book: Decimal
    actual: Decimal
    uom: str


def _row(row, expected):
    return (row.item_code,row.warehouse,row.batch_no)==(expected.item,expected.leaf,expected.batch) and (
        stock._number(row.qty)==expected.actual and row.use_serial_batch_fields==1
        and not row.serial_no and not row.reconcile_all_serial_batch)


def allows_write(doc,event):
    scope = _scope.get()
    if not scope or not _native_orm_active.get() or scope['actor']!=frappe.session.user:
        return False
    entry, rows = scope['entry'],scope['rows']
    if doc is entry:
        return (event in ('before_insert','before_validate','before_save','before_submit')
                and doc.company==scope['company'] and doc.purpose==ENTRY
                and len(doc.items)==len(rows)
                and all(_row(r,e) for r,e in zip(doc.items,rows,strict=True)))
    if entry.docstatus!=1:
        return False
    if doc.doctype=='Bin':
        return (event in ('before_insert','before_validate','before_save') and doc.is_new()
                and any((doc.item_code,doc.warehouse)==(r.item,r.leaf) for r in rows))
    if doc.doctype not in (stock.BUNDLE,stock.SLE):
        return False
    if (doc.voucher_type,doc.voucher_no,doc.company)!=(ENTRY,entry.name,scope['company']):
        return False
    expected = next((e for r,e in zip(entry.items,rows,strict=True) if r.name==doc.voucher_detail_no),None)
    if not expected or (doc.item_code,doc.warehouse)!=(expected.item,expected.leaf):
        return False
    if doc.doctype==stock.BUNDLE:
        if event not in ('before_insert','before_validate','before_save','before_submit','before_change'):
            return False
        if len(doc.entries)!=1 or doc.is_cancelled or doc.has_serial_no or doc.docstatus not in (0,1):
            return False
        part,=doc.entries
        quantity = -expected.book if doc.type_of_transaction=='Outward' else expected.actual
        if ((not quantity and (doc.type_of_transaction!='Inward' or doc.docstatus!=0))
                or part.batch_no!=expected.batch or part.serial_no
                or stock._number(part.qty)!=quantity):
            return False
        key = doc.voucher_detail_no,doc.type_of_transaction
        if doc.is_new() and event=='before_validate' and doc.name:
            scope['bundles'].setdefault(key,doc.name)
        return (event=='before_insert' and doc.is_new() and key not in scope['bundles']
                or scope['bundles'].get(key)==doc.name)
    quantity = stock._number(doc.actual_qty)
    direction = 'Outward' if quantity<0 else 'Inward'
    return (event in ('before_insert','before_validate','before_submit','before_change')
        and doc.docstatus==1 and not doc.is_cancelled and quantity!=0
        and quantity==(-expected.book if quantity<0 else expected.actual)
        and doc.serial_and_batch_bundle==scope['bundles'].get((doc.voucher_detail_no,direction)))


def _native_number(value):
    # ERPNext's in-memory reconciliation controller compares qty directly with
    # numbers before field coercion. Persisted domain quantities stay exact text;
    # native quantity columns are DECIMAL and checked again after posting.
    number = float(value)
    if Decimal(str(number)) != value:
        raise StockError('COUNT_NATIVE_PRECISION_UNREPRESENTABLE')
    return number


def post(plan, *, rate, allow_gl):
    if not _native_orm_active.get():
        raise StockError('STOCK_ADAPTER_SIDE_EFFECT')
    rows=[]
    for line in sorted(plan.command.lines,key=lambda r:r.line_no):
        baseline = plan.context[line.line_id]
        if not Decimal(baseline.difference_quantity):
            continue
        sku=plan.skus[line.sku_id]
        from ...inventory.batch.service import lock_execution_mapping
        mapping=lock_execution_mapping(sku,line.batch_kind,line.batch_id)
        batch=frappe.db.get_value(MAPPING,mapping.id,'native_batch',for_update=True)
        rows.append(CountRow(line.line_id,sku.native_item,plan.locations[line.target].native_warehouse,
                             batch,Decimal(baseline.snapshot_quantity),Decimal(line.quantity),line.uom))
    if not rows:
        return ()
    from .issue import check_negative_stock
    check_negative_stock(rows,'COUNT_NEGATIVE_STOCK_CONFIGURATION')
    company=plan.command.company
    before=stock._balances(company,{(r.item,r.leaf) for r in rows})
    batches=stock._batch_totals({r.batch for r in rows})
    now=now_datetime()
    entry=frappe.get_doc(doctype=ENTRY,purpose=ENTRY,company=company,naming_series='MAT-RECO-.YYYY.-',
        posting_date=now.date(),posting_time=now.time(),set_posting_time=1)
    for row in rows:
        entry.append('items',dict(item_code=row.item,warehouse=row.leaf,batch_no=row.batch,
            qty=_native_number(row.actual),stock_uom=row.uom,use_serial_batch_fields=1,
            valuation_rate=_native_number(rate),allow_zero_valuation_rate=int(rate==0)))
    scope=dict(actor=frappe.session.user,entry=entry,company=company,rows=rows,bundles={})
    token=_scope.set(scope)
    try:
        entry.flags.ignore_version=True
        entry.insert(ignore_permissions=True)
        entry.submit()
        return _verify(scope,before,batches,allow_gl)
    finally:
        _scope.reset(token)


def _verify(scope,before,batches,allow_gl):
    entry,rows=scope['entry'],scope['rows']
    saved=frappe.get_doc(ENTRY,entry.name,for_update=True)
    if (saved.docstatus,saved.owner,saved.company)!=(1,scope['actor'],scope['company']) or len(saved.items)!=len(rows):
        raise StockError('COUNT_NATIVE_REFERENCE_INVALID')
    effects=frappe.db.get_values(stock.SLE,{'voucher_type':ENTRY,'voucher_no':entry.name},'*',
                                 as_dict=True,for_update=True)
    seen=set()
    links=[]
    for detail,row in zip(saved.items,rows,strict=True):
        if not _row(detail,row) or stock._number(detail.current_qty)!=row.book:
            raise StockError('COUNT_NATIVE_REFERENCE_INVALID')
        expected=sorted([q for q in (-row.book,row.actual) if q])
        ledgers=[s for s in effects if s.voucher_detail_no==detail.name]
        if sorted(stock._number(s.actual_qty) for s in ledgers)!=expected:
            raise StockError('COUNT_NATIVE_NET_MISMATCH')
        net=Decimal(0)
        for sle in ledgers:
            if ((sle.company,sle.item_code,sle.warehouse,sle.docstatus,sle.is_cancelled)
                    !=(scope['company'],row.item,row.leaf,1,0) or not sle.serial_and_batch_bundle
                    or sle.serial_and_batch_bundle in seen):
                raise StockError('COUNT_NATIVE_REFERENCE_INVALID')
            bundle=frappe.get_doc(stock.BUNDLE,sle.serial_and_batch_bundle,for_update=True)
            quantity=stock._number(sle.actual_qty)
            if (bundle.docstatus,bundle.is_cancelled,bundle.company,bundle.item_code,bundle.warehouse,
                bundle.voucher_type,bundle.voucher_no,bundle.voucher_detail_no,bundle.type_of_transaction)!= (
                1,0,scope['company'],row.item,row.leaf,ENTRY,entry.name,detail.name,'Outward' if quantity<0 else 'Inward') or len(bundle.entries)!=1:
                raise StockError('COUNT_NATIVE_REFERENCE_INVALID')
            part,=bundle.entries
            if (part.docstatus,part.is_cancelled,part.batch_no,part.item_code,part.warehouse,part.parenttype,
                part.parentfield,stock._number(part.qty),part.serial_no or None)!=(
                1,0,row.batch,row.item,row.leaf,stock.BUNDLE,'entries',quantity,None):
                raise StockError('COUNT_NATIVE_REFERENCE_INVALID')
            net+=quantity
            seen.add(bundle.name)
        if net!=row.actual-row.book:
            raise StockError('COUNT_NATIVE_NET_MISMATCH')
        before[0][row.item,row.leaf,row.batch]+=net
        before[1][row.item,row.leaf]+=net
        batches[row.batch]+=net
        links.append(NativeLink(row.line_id,'Count',ENTRY,entry.name,DETAIL,detail.name))
    # Pinned ERPNext creates a draft zero Inward Bundle for an explicit
    # actual zero. It must never produce an effective SLE or be submitted.
    all_bundles=frappe.db.get_values(stock.BUNDLE,{'voucher_type':ENTRY,'voucher_no':entry.name},
                                   ('name','voucher_detail_no','docstatus'),as_dict=True,for_update=True)
    for bundle in all_bundles:
        if bundle.name in seen:
            continue
        detail=next((d for d in saved.items if d.name==bundle.voucher_detail_no),None)
        expected_row=next((r for d,r in zip(saved.items,rows,strict=True) if d.name==bundle.voucher_detail_no),None)
        draft=frappe.get_doc(stock.BUNDLE,bundle.name,for_update=True)
        if (not detail or not expected_row or expected_row.actual!=0 or bundle.docstatus!=0
                or detail.serial_and_batch_bundle!=bundle.name or draft.type_of_transaction!='Inward'
                or len(draft.entries)!=1 or draft.entries[0].batch_no!=expected_row.batch
                or stock._number(draft.entries[0].qty)!=0 or stock._number(draft.total_qty)!=0):
            raise StockError('COUNT_NATIVE_REFERENCE_INVALID')
    if len(seen)!=len(effects) or before!=stock._balances(scope['company'],before[1]) or batches!=stock._batch_totals(batches):
        raise StockError('COUNT_NATIVE_RECONCILIATION_FAILED')
    if not allow_gl and frappe.db.get_value('GL Entry',{'voucher_type':ENTRY,'voucher_no':entry.name},for_update=True):
        raise StockError('COUNT_GL_NOT_ALLOWED')
    return tuple(links)


def verify_link(plan,link):
    return link.role=='Count' and frappe.db.get_value(ENTRY,link.parent_id,('company','purpose','docstatus'),for_update=True)==(
        plan.command.company,ENTRY,1)


def ensure_schema():
    """Use supported Custom DocPerm for the native helper's type-level check.

    There is no create/submit/cancel/delete grant. Managed document permissions,
    HTTP boundaries and lifecycle guards independently deny direct native CRUD.
    """
    from frappe.permissions import add_permission, update_permission_property
    from ...access.policy import Operation
    role = Operation.COUNT.role
    if not frappe.db.exists('Custom DocPerm', {'parent': 'Item', 'role': role}):
        add_permission('Item', role, ptype='read')
    if not frappe.db.exists('Custom DocPerm', {'parent': ENTRY, 'role': role}):
        add_permission(ENTRY, role, ptype='read')
        update_permission_property(ENTRY, role, 0, 'write', 1)


def permission(doc, user=None, permission_type=None):
    from ...access.native import _references
    scope = _scope.get()
    if (scope and scope['actor'] == frappe.session.user and user in (None,frappe.session.user)
            and doc.name == scope['entry'].name and _native_orm_active.get()):
        return True
    if _references(doc, locked=False):
        return False
    return None
