"""Data-only contract and workbook limits exercised without a site."""

from io import BytesIO
from pathlib import Path
import sys
import unittest
from xml.sax.saxutils import escape, quoteattr
from zipfile import ZIP_DEFLATED, ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps/haxpower_v1"))

from haxpower_v1.migration import demo, formats, xlsx


def workbook(sheets, *, extra=None):
    """Build deterministic test cells and optionally substitute an OOXML part."""
    rel, package, main = xlsx.REL, xlsx.PACKAGE, xlsx.MAIN
    parts = {
        "[Content_Types].xml": f'<Types xmlns="{xlsx.TYPES}"><Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/></Types>',
        "_rels/.rels": f'<Relationships xmlns="{package}"><Relationship Id="R" '
        f'Type="{rel}/officeDocument" Target="xl/workbook.xml"/></Relationships>',
    }
    sheet_links = []
    workbook_links = []
    for index, (sheet, lines) in enumerate(sheets.items(), 1):
        path = f"worksheets/sheet{index}.xml"
        sheet_links.append(f'<sheet name={quoteattr(sheet)} sheetId="{index}" r:id="R{index}"/>')
        workbook_links.append(f'<Relationship Id="R{index}" Type="{rel}/worksheet" Target="{path}"/>')
        data = []
        for row, values in enumerate(lines, 1):
            cells = []
            for column, value in enumerate(values):
                if value is None:
                    continue
                coord = chr(65 + column) + str(row)
                if isinstance(value, str):
                    xml = f'<c r="{coord}" t="inlineStr"><is><t>{escape(value)}</t></is></c>'
                else:
                    kind, text = value
                    xml = f'<c r="{coord}" t="{kind}">{text}</c>'
                cells.append(xml)
            data.append(f'<row r="{row}">' + "".join(cells) + "</row>")
        parts["xl/" + path] = f'<worksheet xmlns="{main}"><sheetData>' + "".join(data) + "</sheetData></worksheet>"
    parts["xl/workbook.xml"] = (f'<workbook xmlns="{main}" xmlns:r="{rel}"><sheets>'
                                + "".join(sheet_links) + "</sheets></workbook>")
    parts["xl/_rels/workbook.xml.rels"] = (f'<Relationships xmlns="{package}">'
                                           + "".join(workbook_links) + "</Relationships>")
    parts.update(extra or {})
    stream = BytesIO()
    with ZipFile(stream, "w", ZIP_DEFLATED) as archive:
        for path, content in parts.items():
            archive.writestr(path, content)
    return stream.getvalue()


class MigrationFormatsTest(unittest.TestCase):
    def test_structural_limits_and_passive_format(self):
        bad = [
            (workbook({"X": [[("n", "<f>1+1</f><v>2</v>")]]}), "WORKBOOK_FORMULA"),
            (workbook({"X": [["x"]]}, extra={"xl/externalLinks/externalLink1.xml": "<x/>"}),
             "WORKBOOK_UNSUPPORTED_MEMBER"),
            (workbook({"X": [["x"]]}, extra={"xl/embeddings/sample.bin": b"test"}),
             "WORKBOOK_UNSUPPORTED_MEMBER"),
            (workbook({"X": [["x"]]}, extra={"../sample.xml": "<x/>"}), "WORKBOOK_UNSUPPORTED_MEMBER"),
            (workbook({"X": [["x"]]}, extra={"xl/workbook.xml": "<!DOCTYPE sample><sample/>"}),
             "WORKBOOK_ACTIVE_CONTENT"),
            (workbook({"X": [["x" * (xlsx.MAX_CELL + 1)]]}), "WORKBOOK_CELL_LIMIT"),
            (workbook({"X": [["x"]] * (xlsx.MAX_ROWS + 1)}), "WORKBOOK_ROW_LIMIT"),
            (workbook({"X": [["x"]]}, extra={"xl/worksheets/sheet1.xml": (
                f'<worksheet xmlns="{xlsx.MAIN}"><sheetData><row r="1">'
                '<c r="AG1" t="inlineStr"><is><t>x</t></is></c></row></sheetData></worksheet>'
            )}), "WORKBOOK_COLUMN_LIMIT"),
            (b"not an XLSX file", "WORKBOOK_STRUCTURE"),
        ]
        for content, code in bad:
            with self.subTest(code=code), self.assertRaisesRegex(xlsx.MigrationError, code):
                xlsx.read("synthetic.xlsx", content)
        for file in ("sample.xlsm", "../sample.xlsx", "x\\sample.xlsx"):
            with self.assertRaisesRegex(xlsx.MigrationError, "WORKBOOK_EXTENSION"):
                xlsx.read(file, workbook({"X": [["x"]]}))

    def test_cell_types_and_immutable_tokens(self):
        data = workbook({"X": [["text", "number", "bool", "injection"],
                               ["0007", ("n", "<v>1.230000009999999E-4</v>"), ("b", "<v>1</v>"), "=2+3"]]})
        rows = xlsx.rows("file", "X", xlsx.read("raw.xlsx", data)["X"], ["text", "number", "bool", "injection"])
        row = next(rows)
        self.assertEqual(row.cells["text"], xlsx.Cell("text", "0007"))
        self.assertEqual(row.cells["number"], xlsx.Cell("number", "1.230000009999999E-4"))
        self.assertEqual(row.cells["bool"], xlsx.Cell("boolean", "1"))
        self.assertEqual(row.cells["injection"], xlsx.Cell("text", "=2+3"))

    def test_standard_version_duplicate_keys_and_columns(self):
        sheets = {"Instructions": [formats.HEADERS["Instructions"], [formats.STANDARD]],
                  "Brands": [formats.HEADERS["Brands"], ["B", "create"], ["B", "create"]]}
        _, candidates = formats.parse(formats.STANDARD, "synthetic", [("file", "raw.xlsx", workbook(sheets))])
        self.assertEqual([row.issues for row in candidates], [["DUPLICATE_OR_INVALID_KEY"]] * 2)
        sheets["Instructions"][1] = ["unknown"]
        with self.assertRaisesRegex(xlsx.MigrationError, "TEMPLATE_VERSION"):
            formats.parse(formats.STANDARD, "synthetic", [("file", "raw.xlsx", workbook(sheets))])
        sheets["Brands"][0] = ["key", "action"]
        with self.assertRaisesRegex(xlsx.MigrationError, "WORKBOOK_HEADERS"):
            formats.parse(formats.STANDARD, "synthetic", [("file", "raw.xlsx", workbook(sheets))])

    def test_synthetic_four_tables_cover_approved_classification_and_types(self):
        legacy, standard = demo._synthetic()
        self.assertEqual(len(legacy[formats.LEGACY_SPEC]), 260)
        self.assertEqual(len(legacy[formats.LEGACY_SKU]), 548)
        self.assertEqual(len(legacy[formats.LEGACY_ATTR]), 2080)
        self.assertEqual(len(standard["Categories"]), 13)
        self.assertEqual(len(standard["Attributes"]), 23)
        sheets = {key: demo._table(value.split(), legacy[key]) for key, value in formats.LEGACY_FIELDS.items()}
        _, candidates = formats.parse(formats.LEGACY, "synthetic", [("file", "raw.xlsx", workbook(sheets))])
        self.assertFalse([entry.issues for entry in candidates if entry.issues])
        self.assertEqual({entry.namespace for entry in candidates}, {"synthetic/DEMO"})
        self.assertTrue(any(entry.legacy_code and "<legacy>" in entry.legacy_code for entry in candidates))
        second, _ = demo._synthetic(second=True)
        self.assertEqual(len(second[formats.LEGACY_SPEC]), 261)


if __name__ == "__main__":
    unittest.main()
