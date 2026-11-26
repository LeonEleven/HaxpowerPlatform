"""Write an XLSX with fixed columns, explicit text cells, and exact quantities."""

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
import json
import re
from time import monotonic

from ..inventory.operations.dto import StockError

COLUMNS = {
    "products": ("id", "code", "display_name", "description", "specification", "brand", "stock_uom",
                 "quantity_precision", "identifiers", "aliases", "images", "attachments"),
    "stock": ("id", "code", "display_name", "specification", "brand", "stock_uom", "physical", "in_transit"),
    "distribution": ("sku_id", "sku_code", "sku_name", "warehouse", "location", "batch", "batch_id",
                     "physical", "available", "uom"),
    "batches": ("sku_id", "batch", "batch_id"),
    "transit": ("document_id", "document_number", "operation_id", "line_id", "sku_id", "batch", "batch_id", "uom",
                "source_warehouse_id", "source_location_id", "source_warehouse", "source_location",
                "target_warehouse_id", "target_location_id", "target_warehouse", "target_location",
                "dispatched", "received", "returned", "remaining", "dispatched_at", "status"),
    "ledger": ("sku_id", "sku_code", "sku_name", "warehouse", "location", "batch", "batch_id", "operation_id",
               "line_id", "document_id", "document_number", "action", "posting_at", "quantity", "uom"),
    "period": ("sku_id", "sku_code", "uom", "warehouse", "location", "bucket_type", "transfer_line_id",
               "batch", "batch_id", "opening", "increase",
               "decrease", "closing", "actions"),
    "documents": ("id", "number", "type", "status", "created_at", "effective_operation_id"),
    "corrections": ("id", "number", "type", "status", "created_at", "effective_operation_id"),
}
MAX_UNCOMPRESSED_TEXT = 32 * 1024 * 1024
INVALID_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


def text(value):
    if isinstance(value, (date, datetime)):
        value = value.isoformat()
    elif isinstance(value, (dict, list, tuple)):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    elif isinstance(value, Decimal):
        value = format(value, "f")
    elif value is None:
        value = ""
    value = INVALID_XML.sub("\ufffd", str(value))
    if len(value) > 32767:
        raise StockError("EXPORT_CELL_LIMIT")
    return value


def write(stream, report, rows, *, deadline):
    # Delay loading the writer for APIs that only return JSON.
    from openpyxl import Workbook
    from openpyxl.cell import WriteOnlyCell

    book = Workbook(write_only=True)
    sheet = book.create_sheet(title=report)
    columns = COLUMNS[report]

    total_bytes = 0

    def append(values):
        nonlocal total_bytes
        cells = []
        for value in values:
            value = text(value)
            total_bytes += len(value.encode("utf-8"))
            if total_bytes > MAX_UNCOMPRESSED_TEXT:
                raise StockError("EXPORT_FILE_LIMIT")
            cell = WriteOnlyCell(sheet, value=value)
            cell.data_type = "s"
            cells.append(cell)
        sheet.append(cells)

    try:
        append(columns)
        for row in rows:
            if monotonic() > deadline:
                raise StockError("EXPORT_TIMEOUT")
            append("无批次" if key == "batch" and row[key] is None else row[key] for key in columns)
        book.save(stream)
        if monotonic() > deadline:
            raise StockError("EXPORT_TIMEOUT")
    finally:
        # Write-only worksheets spool into temporary files before ZIP creation.
        # openpyxl cleans successful saves itself. Close/clean aborted sheets.
        if not sheet.closed:
            sheet.close()
        writer = sheet._writer
        if writer is not None and Path(writer.out).exists():
            writer.cleanup()
        book.close()
