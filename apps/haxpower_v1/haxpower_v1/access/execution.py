"""Private native write scope bound to the authenticated actor and one mapping."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

import frappe


@dataclass(frozen=True)
class NativeExecution:
    actor: str
    service: str
    native_name: str
    domain_id: str
    existing: bool


_native_execution = ContextVar("hp_v1_native_execution", default=None)


@contextmanager
def native_write(authorize, *, service, native_name, domain_id, existing):
    """Run the adapter as the current actor in the caller's transaction."""
    from .policy import active_actor

    actor = active_actor()
    authorize()
    token = _native_execution.set(NativeExecution(actor, service, native_name, domain_id, existing))
    had_search_setting = "disable_global_search" in frappe.conf
    previous = frappe.conf.get("disable_global_search")
    frappe.conf["disable_global_search"] = True
    try:
        yield
    finally:
        if had_search_setting:
            frappe.conf["disable_global_search"] = previous
        else:
            frappe.conf.pop("disable_global_search", None)
        _native_execution.reset(token)


def allows_native_write(service, native_name, domain_id, *, existing):
    return _native_execution.get() == NativeExecution(
        frappe.session.user, service, native_name, domain_id, existing,
    )
