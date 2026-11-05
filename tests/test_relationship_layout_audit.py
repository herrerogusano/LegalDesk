"""Verify real local PDF extraction, not source_pages standing in for PDF text."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from phase15_relationship_layout_audit import audit


class ExtractedFixtureLayouts(unittest.TestCase):
    def test_all_fixture_pdfs_are_hash_checked_and_extracted_without_live_ocr(self):
        measured = audit()
        counts = measured["counts"]
        self.assertEqual(counts["fixtureDocuments"], 18)
        self.assertEqual(counts["validPDFs"], 18)
        self.assertEqual(counts["pages"], 22)
        self.assertEqual(counts["ocrRequiredPages"], 8)
        self.assertEqual(counts["unexpectedErrors"], 0)
        for representation in measured["representations"].values():
            self.assertEqual(representation["nonemptyPages"], 14)
            self.assertEqual(representation["pagesWithBlankLines"], 0)
            self.assertEqual(representation["obligationPages"], 0)
            self.assertEqual(representation["unsafeObligationSegments"], 0)
        self.assertEqual(measured["liveOCRCalls"], 0)
        self.assertEqual(measured["modelCalls"], 0)


if __name__ == "__main__":
    unittest.main()
