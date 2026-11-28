"""Lossless storage for historical codes that may look like markup."""

import json


def encode(value):
    # Keep markup delimiters out of stored fields without disabling Frappe's
    # sanitization. Employee projections decode to JSON values or XLSX text cells.
    return json.dumps(value, ensure_ascii=False).replace("<", r"\u003c").replace(">", r"\u003e")


def decode(value):
    return json.loads(value)
