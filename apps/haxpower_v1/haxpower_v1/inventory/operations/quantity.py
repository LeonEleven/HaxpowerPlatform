"""Exact stock quantity validation before field coercion or persistence."""

from decimal import Decimal

import frappe

from ...catalog.initial_configuration import FORMAL_UNIT_LIMITS
from ...catalog.validation import DECIMAL_WIDTH, exact_decimal
from .dto import QuantitySign, StockError


def _decimal(value, code):
    try:
        return exact_decimal(value, "Quantity")
    except frappe.ValidationError:
        raise StockError(code) from None


def canonical_quantity(value):
    if not isinstance(value, str):
        raise StockError("STOCK_QUANTITY_INVALID")
    return format(_decimal(value, "STOCK_QUANTITY_INVALID"), "f")


def quantity_text(value, uom, *, sku, sign: QuantitySign, limits):
    if uom != sku.stock_uom:
        raise StockError("STOCK_UOM_MISMATCH")
    text = canonical_quantity(value)
    number = Decimal(text)
    if (max(0, -number.as_tuple().exponent) > sku.quantity_precision
            or len(text) > DECIMAL_WIDTH):
        raise StockError("STOCK_QUANTITY_PRECISION")
    if ((sign == QuantitySign.POSITIVE and number <= 0)
            or (sign == QuantitySign.NONNEGATIVE and number < 0)
            or (sign == QuantitySign.NONZERO and not number)):
        raise StockError("STOCK_QUANTITY_SIGN")
    if (limit := _decimal(limits.get(uom), "STOCK_QUANTITY_LIMIT_UNCONFIGURED")) <= 0:
        raise StockError("STOCK_QUANTITY_LIMIT_UNCONFIGURED")
    if number.copy_abs() > min(limit, FORMAL_UNIT_LIMITS.get(uom, limit)):
        raise StockError("STOCK_QUANTITY_LIMIT")
    return text
