"""Bounded, data-only XLSX reader and plain-text workbook writer."""

from dataclasses import dataclass
from io import BytesIO
import re
from pathlib import PurePosixPath
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZIP_STORED, BadZipFile, ZipFile

MAX_FILES = 4
MAX_FILE = 4 * 1024 * 1024
MAX_EXPANDED = 24 * 1024 * 1024
MAX_ROWS = 5001
MAX_COLUMNS = 32
MAX_CELL = 8192
MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE = "http://schemas.openxmlformats.org/package/2006/relationships"
TYPES = "http://schemas.openxmlformats.org/package/2006/content-types"

PART = re.compile(
    r"\[Content_Types\]\.xml|_rels/\.rels|docProps/(core|app)\.xml|"
    r"xl/(workbook\.xml|_rels/workbook\.xml\.rels|styles\.xml|sharedStrings\.xml|"
    r"theme/theme[1-9][0-9]*\.xml|worksheets/sheet[1-9][0-9]*\.xml)"
)
COORDINATE = re.compile(r"([A-Z]{1,2})([1-9][0-9]{0,4})")
PASSIVE = {
    "workbook": {"fileVersion", "workbookPr", "bookViews", "sheets", "calcPr"},
    "worksheet": {"sheetPr", "dimension", "sheetViews", "sheetFormatPr", "cols", "sheetData",
                  "pageMargins", "pageSetup", "printOptions", "autoFilter"},
}


class MigrationError(ValueError):
    """Stable errors safe to expose without workbook data."""


@dataclass(frozen=True)
class Cell:
    kind: str
    value: str | None


@dataclass(frozen=True)
class SourceRow:
    file: str
    sheet: str
    number: int
    cells: dict[str, Cell]

    @property
    def key(self):
        return f"{self.file}/{self.sheet}/{self.number}"


def _require(condition, code="WORKBOOK_STRUCTURE"):
    if not condition:
        raise MigrationError(code)


def _xml(payload):
    # Use UTF-8 only and reject declarations before handing bytes to the parser.
    value = payload.decode("utf-8-sig")
    _require(not re.search(r"<\s*!\s*(DOCTYPE|ENTITY)", value, re.I), "WORKBOOK_ACTIVE_CONTENT")
    _require("\0" not in value)
    return ET.fromstring(value)


def _tag(name):
    return f"{{{MAIN}}}{name}"


def _strings(node):
    # An inline/shared string consists of plain text or styled text runs.
    values = []
    for child in node:
        if child.tag == _tag("t"):
            values.append(child.text or "")
        elif child.tag == _tag("r"):
            _require(all(part.tag in {_tag("rPr"), _tag("t")} for part in child))
            values.extend(part.text or "" for part in child.findall(_tag("t")))
        else:
            raise MigrationError("WORKBOOK_STRUCTURE")
    value = "".join(values)
    _require(len(value) <= MAX_CELL, "WORKBOOK_CELL_LIMIT")
    return value


def _members(payload):
    with ZipFile(BytesIO(payload)) as archive:
        entries = archive.infolist()
        _require(0 < len(entries) <= 24, "WORKBOOK_MEMBER_LIMIT")
        _require(sum(entry.file_size for entry in entries) <= MAX_EXPANDED, "WORKBOOK_EXPANDED_LIMIT")
        names = set()
        for entry in entries:
            name = entry.filename
            _require(name == entry.orig_filename and PART.fullmatch(name) and name not in names,
                     "WORKBOOK_UNSUPPORTED_MEMBER")
            _require(entry.compress_type in (ZIP_STORED, ZIP_DEFLATED) and not entry.flag_bits & 1
                     and (entry.external_attr >> 28) in (0, 8), "WORKBOOK_UNSUPPORTED_MEMBER")
            _require(entry.file_size <= max(65536, 180 * entry.compress_size), "WORKBOOK_COMPRESSION_LIMIT")
            names.add(name)
        return {entry.filename: _xml(archive.read(entry)) for entry in entries}


def _relations(node, base, allowed):
    _require(node.tag == f"{{{PACKAGE}}}Relationships")
    result = {}
    for child in node:
        identity, kind, path = (child.get(key, "") for key in ("Id", "Type", "Target"))
        _require(child.tag == f"{{{PACKAGE}}}Relationship" and identity and identity not in result
                 and child.get("TargetMode", "Internal") == "Internal" and kind in allowed and path,
                 "WORKBOOK_ACTIVE_CONTENT")
        target = path[1:] if path.startswith("/") else base + path
        _require(PART.fullmatch(target) and target.startswith(allowed[kind]))
        result[identity] = target
    return result


def _read_cell(node, shared):
    _require(node.find(_tag("f")) is None, "WORKBOOK_FORMULA")
    _require(all(child.tag in {_tag("v"), _tag("is")} for child in node))
    kind = node.get("t", "n")
    raw = node.findtext(_tag("v"))
    if kind == "inlineStr":
        inline = node.find(_tag("is"))
        return Cell("text", _strings(inline) if inline is not None else "")
    if kind == "s":
        _require(raw and re.fullmatch(r"[0-9]{1,6}", raw) and int(raw) < len(shared))
        return Cell("text", shared[int(raw)])
    kinds = {"n": "number", "b": "boolean", "d": "date", "e": "error"}
    _require(kind in kinds and node.find(_tag("is")) is None)
    _require(raw is None or len(raw) <= MAX_CELL, "WORKBOOK_CELL_LIMIT")
    _require(kind != "b" or raw in ("0", "1"))
    if raw is None:
        return Cell("blank", None)
    # Styled numeric cells retain their exact serialized token, never a float.
    return Cell(kinds[kind], raw)


def _sheet(node, shared):
    _require(node.tag == _tag("worksheet") and all(child.tag in {_tag(k) for k in PASSIVE["worksheet"]}
                                                  for child in node), "WORKBOOK_ACTIVE_CONTENT")
    data = node.findall(_tag("sheetData"))
    _require(len(data) == 1)
    result = {}
    used_rows = set()
    for row in data[0]:
        number = row.get("r", "")
        _require(row.tag == _tag("row") and re.fullmatch(r"[1-9][0-9]{0,4}", number))
        number = int(number)
        _require(number <= MAX_ROWS and number not in used_rows, "WORKBOOK_ROW_LIMIT")
        used_rows.add(number)
        cells = {}
        for child in row:
            match = COORDINATE.fullmatch(child.get("r", ""))
            _require(child.tag == _tag("c") and match and int(match[2]) == number)
            column = 0
            for letter in match[1]:
                column = 26 * column + ord(letter) - 64
            _require(column <= MAX_COLUMNS and column not in cells, "WORKBOOK_COLUMN_LIMIT")
            cells[column] = _read_cell(child, shared)
        if any(cell.value is not None for cell in cells.values()):
            result[number] = cells
    return dict(sorted(result.items()))


def read(name, payload):
    _require(isinstance(name, str) and len(name) <= 160 and PurePosixPath(name).name == name
             and not any(ord(char) < 32 or char in "\\:" for char in name)
             and name.lower().endswith(".xlsx"), "WORKBOOK_EXTENSION")
    _require(type(payload) is bytes and 0 < len(payload) <= MAX_FILE, "WORKBOOK_FILE_LIMIT")
    _require(payload.startswith(b"PK\x03\x04"), "WORKBOOK_STRUCTURE")
    try:
        members = _members(payload)
        content = members["[Content_Types].xml"]
        _require(content.tag == f"{{{TYPES}}}Types")
        workbook_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
        declared = [child.get("ContentType") for child in content if child.get("PartName") == "/xl/workbook.xml"]
        _require(declared == [workbook_type], "WORKBOOK_ACTIVE_CONTENT")
        _require(all(not any(word in child.get("ContentType", "").lower()
                            for word in ("macro", "activex", "oleobject", "externallink")) for child in content),
                 "WORKBOOK_ACTIVE_CONTENT")
        root = _relations(members["_rels/.rels"], "", {
            f"{REL}/officeDocument": "xl/workbook.xml",
            f"{REL}/extended-properties": "docProps/app.xml",
            f"{PACKAGE}/metadata/core-properties": "docProps/core.xml",
        })
        _require(list(root.values()).count("xl/workbook.xml") == 1)
        rels = _relations(members["xl/_rels/workbook.xml.rels"], "xl/", {
            f"{REL}/worksheet": "xl/worksheets/",
            f"{REL}/styles": "xl/styles.xml",
            f"{REL}/theme": "xl/theme/",
            f"{REL}/sharedStrings": "xl/sharedStrings.xml",
        })
        _require(set(root.values()) | set(rels.values()) | {"[Content_Types].xml", "_rels/.rels",
                  "xl/_rels/workbook.xml.rels"} == set(members))
        book = members["xl/workbook.xml"]
        _require(book.tag == _tag("workbook") and all(child.tag in {_tag(k) for k in PASSIVE["workbook"]}
                                                      for child in book), "WORKBOOK_ACTIVE_CONTENT")
        sheets = book.find(_tag("sheets"))
        _require(sheets is not None and 1 <= len(sheets) <= 10, "WORKBOOK_SHEET_LIMIT")
        shared = []
        if "xl/sharedStrings.xml" in members:
            strings = members["xl/sharedStrings.xml"]
            _require(strings.tag == _tag("sst") and len(strings) <= MAX_ROWS * MAX_COLUMNS)
            for entry in strings:
                _require(entry.tag == _tag("si"))
                shared.append(_strings(entry))
        result = {}
        paths = set()
        for sheet in sheets:
            label = sheet.get("name", "")
            target = rels[sheet.attrib[f"{{{REL}}}id"]]
            _require(sheet.tag == _tag("sheet") and 0 < len(label) <= 31 and label not in result
                     and target.startswith("xl/worksheets/") and target not in paths)
            paths.add(target)
            result[label] = _sheet(members[target], shared)
        _require(paths == {path for path in rels.values() if path.startswith("xl/worksheets/")})
        return result
    except MigrationError:
        raise
    except (BadZipFile, ET.ParseError, KeyError, ValueError, OSError, RuntimeError, EOFError, NotImplementedError):
        raise MigrationError("WORKBOOK_STRUCTURE") from None


def rows(file, sheet, data, expected):
    header = data.pop(1, {})
    values = [header.get(index) for index in range(1, max(header, default=0) + 1)]
    _require(all(cell and cell.kind == "text" for cell in values), "WORKBOOK_HEADERS")
    names = [cell.value for cell in values]
    _require(len(names) == len(set(names)) and set(names) == set(expected), "WORKBOOK_HEADERS")
    for number, entries in data.items():
        _require(max(entries, default=0) <= len(names), "WORKBOOK_HEADERS")
        yield SourceRow(file, sheet, number, {
            name: entries.get(index, Cell("blank", None)) for index, name in enumerate(names, 1)
        })


def write(stream, sheets):
    from openpyxl import Workbook
    from openpyxl.cell import WriteOnlyCell

    book = Workbook(write_only=True)
    for name, lines in sheets.items():
        sheet = book.create_sheet(name)
        for line in lines:
            cells = []
            for value in line:
                cell = WriteOnlyCell(sheet, value=value)
                if isinstance(value, str):
                    cell.data_type = "s"
                cells.append(cell)
            sheet.append(cells)
    book.save(stream)
