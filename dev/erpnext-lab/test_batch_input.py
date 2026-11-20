"""Batch normalization and import preview tests independent of Frappe."""

import unittest

from haxpower_platform.haxpower_platform.batch_input import BatchInputError, normalize_selection, preview_legacy


class BatchInputTests(unittest.TestCase):
    def test_explicit_no_batch_is_distinct_from_missing(self):
        self.assertIsNone(normalize_selection({"mode": "NO_BATCH"}))
        for selection in (None, {}, {"batchNo": None}):
            with self.subTest(selection=selection), self.assertRaisesRegex(BatchInputError, "BATCH_SELECTION_REQUIRED"):
                normalize_selection(selection)

    def test_no_batch_cannot_carry_a_text_alias(self):
        with self.assertRaisesRegex(BatchInputError, "INVALID_BATCH_SELECTION"):
            normalize_selection({"mode": "NO_BATCH", "batchNo": ""})

    def test_whitespace_and_paired_or_single_quotes_require_review(self):
        for value, code in (("", "BATCH_TEXT_EMPTY"), (" \u3000\t", "BATCH_TEXT_EMPTY"),
                            ("''", "BATCH_QUOTES_REQUIRE_CONFIRMATION"), ("’", "BATCH_QUOTES_REQUIRE_CONFIRMATION"),
                            ("“ ”", "BATCH_QUOTES_REQUIRE_CONFIRMATION"), ("『』", "BATCH_QUOTES_REQUIRE_CONFIRMATION")):
            with self.subTest(value=value), self.assertRaisesRegex(BatchInputError, code):
                normalize_selection({"mode": "BATCH", "batchNo": value})
            self.assertEqual(preview_legacy(value), {"status": code, "requires_review": True})

    def test_trim_edges_and_preserve_meaningful_text(self):
        value = 'Std ６１-"A"'
        self.assertEqual(normalize_selection({"mode": "BATCH", "batchNo": "\u00a0" + value + "  "}), value)
        self.assertEqual(normalize_selection({"mode": "BATCH", "batchNo": '"STD61"'}), '"STD61"')
        self.assertEqual(preview_legacy("NULL"), {"status": "UNCHANGED", "proposed_batch": "NULL"})
        self.assertEqual(preview_legacy(" STD61 "), {"status": "TRIMMED", "proposed_batch": "STD61"})
        self.assertEqual(preview_legacy(None), {"status": "SOURCE_NULL", "proposed_batch": None})

    def test_internal_batch_markers_and_controls_are_rejected(self):
        for value, code in ((" hp-NB-key ", "INTERNAL_BATCH_ID_FORBIDDEN"), ("HP-RB-key", "INTERNAL_BATCH_ID_FORBIDDEN"),
                            ("STD\u200b61", "INVALID_BATCH_TEXT"), (None, "INVALID_BATCH_TEXT"), ("A" * 81, "INVALID_BATCH_TEXT")):
            with self.subTest(value=value), self.assertRaisesRegex(BatchInputError, code):
                normalize_selection({"mode": "BATCH", "batchNo": value})


if __name__ == "__main__":
    unittest.main()
