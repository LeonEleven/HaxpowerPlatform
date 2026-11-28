"""Private records and evidence encoding for the migration domain."""

from base64 import b64decode, b64encode
from contextlib import contextmanager
from contextvars import ContextVar
import json
from uuid import uuid4
import zlib

import frappe
from frappe.model.document import Document

from ..access import policy
from .formats import digest

TASK = "HP V1 Migration Task"
VERSION = "HP V1 Migration Version"
FILE = "HP V1 Migration File"
SOURCE = "HP V1 Migration Source"
APPROVAL = "HP V1 Migration Approval"
RESULT = "HP V1 Migration Result"
IDENTITY = "HP V1 Migration Identity"

_writing = ContextVar("hp_v1_migration_records", default=False)


def pack(value):
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return b64encode(zlib.compress(encoded.encode("ascii"), level=6)).decode("ascii")


def unpack(value):
    return json.loads(zlib.decompress(b64decode(value, validate=True)))


def insert(doctype, identity=None, **values):
    token = _writing.set(True)
    try:
        return frappe.get_doc(doctype=doctype, **values).insert(
            ignore_permissions=True, set_name=identity or uuid4().hex,
        ).name
    finally:
        _writing.reset(token)


def update(doctype, identity, values):
    # The task pointer and mapping confirmation can advance only via service.
    if doctype not in (TASK, IDENTITY):
        raise ValueError("MIGRATION_APPEND_ONLY")
    frappe.db.set_value(doctype, identity, values)


def record(doctype, identity, *, lock=False):
    if not frappe.db.get_value(doctype, identity, "name", for_update=lock):
        policy.deny()
    return frappe.get_doc(doctype, identity, for_update=lock)


@contextmanager
def quiet():
    previous = frappe.flags.mute_messages
    count = len(frappe.message_log)
    frappe.flags.mute_messages = True
    try:
        yield
    finally:
        del frappe.message_log[count:]
        frappe.flags.mute_messages = previous


class MigrationDocument(Document):
    def validate(self):
        if not _writing.get() or not self.is_new():
            policy.deny()

    def before_change(self):
        policy.deny()

    def on_trash(self):
        policy.deny()

    def before_rename(self, old, new, merge=False):
        policy.deny()

    def notify_update(self):
        pass


def verify(value, checksum):
    if digest(value) != checksum:
        raise ValueError("MIGRATION_EVIDENCE_INTEGRITY")
