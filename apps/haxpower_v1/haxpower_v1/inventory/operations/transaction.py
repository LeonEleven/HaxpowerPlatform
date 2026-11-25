"""Dedicated MariaDB transaction and private adapter restrictions."""

from collections import defaultdict
from contextlib import contextmanager, ExitStack
from contextvars import ContextVar
from functools import wraps
import re
from threading import Lock

import frappe

from .dto import StockError

_adapter_active = ContextVar("hp_v1_stock_adapter_active", default=False)
_native_orm_active = ContextVar("hp_v1_stock_native_orm_active", default=False)
_cache_lock = Lock()
_cache_users = 0
_cache_patches = None


@contextmanager
def connection(*, read_committed=False):
    if _adapter_active.get():
        raise StockError("STOCK_ADAPTER_SIDE_EFFECT")
    previous = frappe.local.db
    frappe.connect(set_admin_as_user=False)
    database = frappe.local.db
    if database.db_type != "mariadb":
        frappe.local.db = previous
        raise StockError("STOCK_DATABASE_UNSUPPORTED")
    try:
        if read_committed:
            # ERPNext uses ordinary reads for availability and valuation. Refresh
            # those reads after waiting on the shared stock-write lock.
            database.sql("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
        database.sql("START TRANSACTION")
        yield database
    finally:
        try:
            # Closing also performs a full rollback if the attempt never committed.
            database.close()
        finally:
            frappe.local.db = previous
            for (doctype, _), callback in getattr(database, "hp_stock_cache_invalidations", {}).items():
                previous.value_cache.pop(doctype, None)
                callback()


@contextmanager
def _native_orm(reject):
    """Keep transaction-local documents out of shared cache during native ORM writes."""
    from frappe import realtime
    from frappe.utils import background_jobs

    global _cache_users, _cache_patches

    def cache_method(method, skipped_value):
        @wraps(method)
        def guarded(key, *args, **kwargs):
            if _native_orm_active.get() and isinstance(key, str) and key.startswith("document_cache::"):
                return skipped_value
            return method(key, *args, **kwargs)
        return guarded

    def external_effect(method):
        @wraps(method)
        def guarded(*args, **kwargs):
            if fail := _native_orm_active.get():
                fail()
            return method(*args, **kwargs)
        return guarded

    def replace(stack, obj, attr, wrapper):
        original = getattr(obj, attr)
        setattr(obj, attr, wrapper(original))
        stack.callback(setattr, obj, attr, original)

    with _cache_lock:
        if not _cache_users:
            patches = ExitStack()
            for method, skipped in (("get_value", None), ("set_value", 0)):
                replace(patches, frappe.cache, method, lambda original: cache_method(original, skipped))
            for obj, attr in (
                (frappe, "enqueue"), (background_jobs, "enqueue"), (background_jobs, "get_queue"),
                (realtime, "emit_via_redis"),
            ):
                replace(patches, obj, attr, external_effect)
            _cache_patches = patches
        _cache_users += 1
    token = _native_orm_active.set(reject)
    had_search = "disable_global_search" in frappe.conf
    previous_search = frappe.conf.get("disable_global_search")
    previous_flags = frappe.local.flags
    previous_requests = frappe.local.request_cache
    previous_dimensions = getattr(frappe.local, "pending_dimension_checks", None)
    previous_futures = getattr(frappe.local, "future_sle", None)
    previous_perpetual = getattr(frappe.local, "enable_perpetual_inventory", None)
    previous_realtime = getattr(frappe.local, "_realtime_log", None)
    if hasattr(frappe.local, "_realtime_log"):
        del frappe.local._realtime_log
    frappe.conf.disable_global_search = True
    frappe.local.flags = frappe._dict(previous_flags, mute_messages=True, currently_saving=[], warehouse_account_map={})
    frappe.local.request_cache = defaultdict(dict)
    frappe.local.pending_dimension_checks = {}
    frappe.local.future_sle = {}
    # ERPNext memoizes the company's accounting switch outside Frappe's cache.
    # A Bench process can outlive a committed settings change.
    frappe.local.enable_perpetual_inventory = {}
    try:
        yield
    finally:
        if had_search:
            frappe.conf.disable_global_search = previous_search
        else:
            frappe.conf.pop("disable_global_search", None)
        frappe.local.flags = previous_flags
        frappe.local.request_cache = previous_requests
        frappe.local.pending_dimension_checks = previous_dimensions
        if previous_futures is None:
            del frappe.local.future_sle
        else:
            frappe.local.future_sle = previous_futures
        if previous_perpetual is None:
            del frappe.local.enable_perpetual_inventory
        else:
            frappe.local.enable_perpetual_inventory = previous_perpetual
        if hasattr(frappe.local, "_realtime_log"):
            del frappe.local._realtime_log
        if previous_realtime is not None:
            frappe.local._realtime_log = previous_realtime
        _native_orm_active.reset(token)
        with _cache_lock:
            _cache_users -= 1
            if not _cache_users:
                _cache_patches.close()
                _cache_patches = None


@contextmanager
def adapter_transaction(database, *, native_orm=False):
    """Reject nested transaction boundaries and deferred Frappe callbacks."""
    violated = False

    def reject(*_args, **_kwargs):
        nonlocal violated
        violated = True
        raise StockError("STOCK_ADAPTER_SIDE_EFFECT")

    original_sql = database.sql
    callbacks = ("before_commit", "after_commit", "before_rollback", "after_rollback")

    class RejectCallbacks:
        add = __call__ = run = reset = staticmethod(reject)

    class CacheCallbacks(RejectCallbacks):
        @staticmethod
        def add(callback):
            # Frappe already invalidated its cache synchronously. Repeat the
            # same invalidation once the independent connection is closed.
            code = getattr(callback, "__code__", None)
            if (
                getattr(callback, "__module__", None) != "frappe.model.document"
                or not code or code.co_qualname != "clear_document_cache.<locals>.clear_in_redis"
            ):
                reject()
            doctype, name = (cell.cell_contents for cell in callback.__closure__)
            database.hp_stock_cache_invalidations[doctype, name] = callback

    def guarded_sql(query, *args, **kwargs):
        # SQL templates must contain a single transactional DML statement.
        # The database driver binds values separately.
        text = str(query)
        if native_orm and text.lower() == "savepoint create_bin" and not args and not kwargs:
            # ERPNext uses this savepoint when creating a first Bin. A
            # collision must fail the whole command under the site lock.
            return original_sql(query)
        if kwargs.get("auto_commit") or ";" in text or not re.match(
            r"\s*(?:SELECT|INSERT|UPDATE|DELETE)\b", text, re.IGNORECASE,
        ) or re.search(r"\bINTO\s+(?:OUTFILE|DUMPFILE)\b", text, re.IGNORECASE):
            reject()
        return original_sql(query, *args, **kwargs)

    replace = dict.fromkeys(("commit", "rollback", "begin", "close", "connect", "sql_ddl"), reject)
    replace.update({name: RejectCallbacks() for name in callbacks})
    if native_orm:
        replace.update(after_commit=CacheCallbacks(), after_rollback=CacheCallbacks())
        database.hp_stock_cache_invalidations = {}
    replace["sql"] = guarded_sql
    originals = {name: getattr(database, name) for name in replace}
    token = _adapter_active.set(True)
    try:
        for name, method in replace.items():
            setattr(database, name, method)
        with ExitStack() as stack:
            if native_orm:
                stack.enter_context(_native_orm(reject))
            yield
        if violated or not original_sql("SELECT @@in_transaction")[0][0]:
            reject()
    finally:
        _adapter_active.reset(token)
        for name, method in originals.items():
            setattr(database, name, method)
