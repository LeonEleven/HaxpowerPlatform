"""Dedicated MariaDB transaction and private adapter restrictions."""

from contextlib import contextmanager
from contextvars import ContextVar
import re

import frappe

from .dto import StockError

_adapter_active = ContextVar("hp_v1_stock_adapter_active", default=False)


@contextmanager
def connection():
    if _adapter_active.get():
        raise StockError("STOCK_ADAPTER_SIDE_EFFECT")
    previous = frappe.local.db
    frappe.connect(set_admin_as_user=False)
    database = frappe.local.db
    if database.db_type != "mariadb":
        frappe.local.db = previous
        raise StockError("STOCK_DATABASE_UNSUPPORTED")
    try:
        database.sql("START TRANSACTION")
        yield database
    finally:
        try:
            # Closing also performs a full rollback if the attempt never committed.
            database.close()
        finally:
            frappe.local.db = previous


@contextmanager
def adapter_transaction(database):
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

    def guarded_sql(query, *args, **kwargs):
        # SQL templates must contain a single transactional DML statement.
        # The database driver binds values separately.
        text = str(query)
        if kwargs.get("auto_commit") or ";" in text or not re.match(
            r"\s*(?:SELECT|INSERT|UPDATE|DELETE)\b", text, re.IGNORECASE,
        ) or re.search(r"\bINTO\s+(?:OUTFILE|DUMPFILE)\b", text, re.IGNORECASE):
            reject()
        return original_sql(query, *args, **kwargs)

    replace = dict.fromkeys(("commit", "rollback", "begin", "close", "connect", "sql_ddl"), reject)
    replace.update({name: RejectCallbacks() for name in callbacks})
    replace["sql"] = guarded_sql
    originals = {name: getattr(database, name) for name in replace}
    token = _adapter_active.set(True)
    try:
        for name, method in replace.items():
            setattr(database, name, method)
        yield
        if violated or not original_sql("SELECT @@in_transaction")[0][0]:
            reject()
    finally:
        _adapter_active.reset(token)
        for name, method in originals.items():
            setattr(database, name, method)
