"""Normalize explicit input before it reaches Frappe field coercion."""

from datetime import date
from decimal import Decimal, InvalidOperation
import re

import frappe

from .dto import ACTIVE, BOOLEAN, DATE, DECIMAL, INACTIVE, TEXT


KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", re.ASCII)
IDENTIFIER = re.compile(r"[0-9a-f]{32}", re.ASCII)
DECIMAL_LITERAL = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", re.ASCII)
ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", re.ASCII)
DECIMAL_WIDTH = 140


def key(value, label):
    if not isinstance(value, str) or not KEY.fullmatch(value):
        frappe.throw(f"{label} must contain 1–100 ASCII letters, digits, dots, underscores or hyphens, starting with a letter or digit.")
    return value.upper()


def identifier(value, label):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        frappe.throw(f"{label} must be a catalog identifier.")
    return value


def text(value, label, limit, *, required=False):
    if not isinstance(value, str) or len(value) > limit or "\0" in value:
        frappe.throw(f"{label} must be text of up to {limit} characters.")
    value = value.strip()
    if required and not value:
        frappe.throw(f"{label} is required.")
    return value


def choice(value, choices, label):
    if not isinstance(value, str) or value not in choices:
        frappe.throw(f"Invalid {label}. Expected one of: {', '.join(sorted(choices))}.")
    return value


def status(value):
    return choice(value, (ACTIVE, INACTIVE), "status")


def order(value, label):
    if type(value) is not int or not 0 <= value <= 2_147_483_647:
        frappe.throw(f"{label} must be a non-negative 32-bit integer.")
    return value


def boolean(value, label):
    if type(value) is not bool:
        frappe.throw(f"{label} must be a boolean.")
    return value


def exact_decimal(value, label):
    if not (isinstance(value, Decimal) or type(value) is int
            or (isinstance(value, str) and len(value) <= 512 and DECIMAL_LITERAL.fullmatch(value))):
        frappe.throw(f"{label} must be an exact decimal supplied as Decimal, integer or decimal text.")

    try:
        number = Decimal(value)
    except InvalidOperation:
        frappe.throw(f"{label} is not a valid decimal.")
    if not number.is_finite():
        frappe.throw(f"{label} must be a finite decimal.")
    if not number:
        return Decimal(0)

    # Strip insignificant zeros without Decimal.normalize(), which can round
    # against the caller's Decimal context.
    sign, digits, exponent = number.as_tuple()
    end = len(digits)
    while digits[end - 1] == 0:
        end -= 1
    exponent += len(digits) - end
    decimal_point = end + exponent
    integer_length = max(1, decimal_point)
    fraction_length = max(0, -exponent)
    if sign + integer_length + bool(fraction_length) + fraction_length > DECIMAL_WIDTH:
        frappe.throw(f"{label} requires more than {DECIMAL_WIDTH} characters in exact decimal notation.")
    return Decimal((sign, digits[:end], exponent))


def calendar_date(value, label):
    if type(value) is date:
        return value
    if isinstance(value, str) and ISO_DATE.fullmatch(value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    frappe.throw(f"{label} must be a valid calendar date in YYYY-MM-DD format.")


def scalar(value, data_type, label):
    if data_type == TEXT:
        return text(value, label, 4000, required=True)
    if data_type == DECIMAL:
        return exact_decimal(value, label)
    if data_type == BOOLEAN:
        return boolean(value, label)
    if data_type == DATE:
        return calendar_date(value, label)
    frappe.throw(f"{label} has an unsupported data type.")
