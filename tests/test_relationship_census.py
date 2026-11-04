"""Independent corpus labels and real-adapter availability impact, offline."""
from __future__ import annotations
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from phase15_relationship_census import census


class SpanishCorpusCensus(unittest.TestCase):
    def test_spanish_obligations_and_controlled_fail_closed_impact(self):
        measured = census()
        self.assertEqual(measured["corpusDocuments"], 27)
        self.assertEqual(measured["spanishFilenameDocuments"], 10)
        self.assertEqual(measured["spanishLanguageDocuments"], 12)
        self.assertEqual(measured["nonemptySpanishDocuments"], 11)
        self.assertEqual(measured["spanishFixtureDocuments"], 7)
        self.assertEqual(measured["spanishFixtureDetected"], 1)
        self.assertEqual(measured["baselineDetected"], 0)
        self.assertEqual(measured["detected"], 3)
        self.assertEqual(measured["falsePositives"], 0)
        self.assertEqual(measured["falseNegatives"], 0)
        self.assertEqual(measured["missingProjectionAnswersFailClosed"], 3)
        self.assertEqual(measured["malformedProjectionAnswersFailClosed"], 3)
        self.assertEqual(measured["unexpectedErrors"], 0)
        self.assertEqual(measured["liveModelAnswersMeasured"], 0)


if __name__ == "__main__":
    unittest.main()
