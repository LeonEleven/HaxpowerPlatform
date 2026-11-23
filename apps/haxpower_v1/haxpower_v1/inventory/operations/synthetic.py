"""Bench-only fixture adapter. Its writes target only synthetic DocTypes."""

from dataclasses import replace
from uuid import uuid4

import frappe

from ...access.policy import Operation, deny
from ..batch.authorization import SYNTHETIC_SITE
from . import persistence as store
from .dto import Command, NativeLink, QuantitySign
from .service import Action, _create_draft, _execute

COMMAND = "Synthetic Apply"
SETTING = "hp_v1_stock_synthetic_actors"


def _authorize(company):
    actor = frappe.session.user
    configured = frappe.conf.get(SETTING)
    if (
        getattr(frappe.local, "request", None) is not None
        or not SYNTHETIC_SITE.fullmatch(frappe.local.site or "")
        or not isinstance(configured, dict)
        or not isinstance(actor, str)
        or not actor.endswith("@example.invalid")
        or configured.get(actor) != company
        or not isinstance(company, str)
        or not company.startswith("Synthetic V1 Warehouse ")
    ):
        deny()


def _native_links(plan, *, after_detail=None):
    links = []
    # Two synthetic parents allow multiple references per business line.
    for role in ("Primary", "Secondary"):
        parent = store.insert(store.SYNTHETIC_DOCUMENT, document_id=plan.command.document_id)
        for index, line in enumerate(plan.command.lines, 1):
            detail = store.insert(
                store.SYNTHETIC_DETAIL, line_id=line.line_id, parent=parent, parenttype=store.SYNTHETIC_DOCUMENT,
                parentfield="details", idx=index,
            )
            links.append(NativeLink(line.line_id, role, store.SYNTHETIC_DOCUMENT, parent, store.SYNTHETIC_DETAIL, detail))
            if after_detail:
                after_detail(plan, len(links))
    return tuple(links)


def _verify_reference(plan, link):
    return (
        frappe.db.get_value(store.SYNTHETIC_DOCUMENT, link.parent_id, "document_id", for_update=True)
        == plan.command.document_id
        and frappe.db.get_value(store.SYNTHETIC_DETAIL, link.detail_id, "line_id", for_update=True) == link.line_id
    )


def _action(limits):
    return Action(COMMAND, Operation.RECEIVE, QuantitySign.POSITIVE, limits,
                  {(store.SYNTHETIC_DOCUMENT, store.SYNTHETIC_DETAIL): "details"},
                  _authorize, _native_links, _verify_reference)


def create_draft(company, lines, *, limits, reason="", notes=""):
    """Generate stable document and line IDs on trusted server code."""
    _authorize(company)
    identified = tuple(replace(line, line_id=uuid4().hex) for line in lines)
    command = Command(company, uuid4().hex, 1, uuid4().hex, identified, reason)
    return _create_draft(command, _action(limits), notes=notes)


def execute(command, *, limits, before_guard=None, after_detail=None, commit=None):
    _authorize(command.company)
    action = _action(limits)
    if after_detail:
        action = replace(action, apply=lambda plan: _native_links(plan, after_detail=after_detail))
    return _execute(command, action, before_guard=before_guard, commit=commit)
