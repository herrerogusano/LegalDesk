"""Independent integrity checks for the Phase 14 synthetic IDP corpus.

The checks deliberately use the manifest's hand-authored source pages as the
ground truth.  They do not import an extractor or call Bedrock, Textract, OCR,
or any network service.
"""

from __future__ import annotations

import hashlib
import json
import re
import unittest
from pathlib import Path

from pypdf import PdfReader


ROOT = Path(__file__).parents[1]
DATASET = ROOT / "tests" / "fixtures" / "idp"
MANIFEST_PATH = DATASET / "manifest.json"
EXPECTED_FIELDS = {
    "CONTRACT": {"parties", "effective_date", "explicit_expiration_date", "initial_duration_value", "initial_duration_unit", "automatic_renewal", "renewal_period_value", "renewal_period_unit", "termination_notice_value", "termination_notice_unit", "amount", "currency", "jurisdiction", "governing_law", "subtype"},
    "DEMAND": {"claimants", "defendants", "court", "case_number", "filing_date", "claims", "claimed_amount", "currency"},
    "JUDGMENT": {"court", "case_number", "decision_date", "parties", "operative_ruling", "costs_statement", "appeal_information"},
    "UNKNOWN": {"document_type", "general_document_evidence"},
}


def _compact(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


class TestPhase14IDPDataset(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not MANIFEST_PATH.exists():
            raise AssertionError(f"missing generated IDP manifest: {MANIFEST_PATH}")
        cls.manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        cls.fixtures = cls.manifest["fixtures"]

    def test_manifest_shape_has_fifteen_supported_documents_and_negative_set(self) -> None:
        self.assertEqual(self.manifest["manifest_version"], "1.0.0")
        self.assertEqual(self.manifest["schema_version"], "1.0.0")
        self.assertEqual(self.manifest["provenance"], "worker-authored synthetic expected data; supervisor reviewed selected cases, not human/legal expert review")
        self.assertIn("byte-identical cross-platform output is not promised", self.manifest["reproduction"]["byte_determinism"])
        self.assertEqual(len(self.fixtures), 18)
        supported = [item for item in self.fixtures if item["category"] == "ground_truth"]
        self.assertEqual(len(supported), 15)
        self.assertEqual({item["expected_type"] for item in supported}, {"CONTRACT", "DEMAND", "JUDGMENT"})
        for kind in ("CONTRACT", "DEMAND", "JUDGMENT"):
            self.assertEqual(sum(item["expected_type"] == kind for item in supported), 5)
        self.assertEqual({item["category"] for item in self.fixtures} - {"ground_truth"}, {"unknown", "adversarial", "negative"})
        self.assertEqual(sum(item["category"] == "unknown" for item in self.fixtures), 1)
        self.assertEqual(sum(item["category"] == "adversarial" for item in self.fixtures), 1)
        self.assertEqual(sum(item["category"] == "negative" for item in self.fixtures), 1)

    def test_pdf_hashes_and_page_counts_are_persisted(self) -> None:
        ids: set[str] = set()
        hashes: set[str] = set()
        for item in self.fixtures:
            self.assertNotIn(item["id"], ids)
            ids.add(item["id"])
            pdf_path = DATASET / "pdfs" / item["filename"]
            self.assertTrue(pdf_path.is_file(), pdf_path)
            digest = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
            self.assertEqual(digest, item["sha256"], item["id"])
            self.assertNotIn(digest, hashes, item["id"])
            hashes.add(digest)
            self.assertEqual(len(PdfReader(str(pdf_path)).pages), item["page_count"], item["id"])
            self.assertEqual(item["page_count"], len(item["source_pages"]), item["id"])
            self.assertTrue(all(page["text"].startswith("SYNTHETIC TEST FIXTURE - NO LEGAL ADVICE") for page in item["source_pages"]))

    def test_language_and_digital_scanned_mixed_diversity(self) -> None:
        self.assertIn("en", {item["language"] for item in self.fixtures})
        self.assertIn("es", {item["language"] for item in self.fixtures})
        modes = [mode for item in self.fixtures for mode in item["content_modes"]]
        self.assertGreaterEqual(modes.count("digital"), 12)
        self.assertGreaterEqual(modes.count("scanned"), 6)
        self.assertGreaterEqual(sum(len(set(item["content_modes"])) > 1 for item in self.fixtures), 3)

    def test_expected_fields_are_complete_and_have_independent_source_anchors(self) -> None:
        for item in self.fixtures:
            kind = item["expected_type"]
            self.assertEqual(set(item["expected"]["fields"]), EXPECTED_FIELDS[kind], item["id"])
            source = {page_no: page["text"] for page_no, page in enumerate(item["source_pages"], start=1)}
            for field_name, expected in item["expected"]["fields"].items():
                self.assertIn(expected["presence"], {"PRESENT", "ABSENT", "NOT_APPLICABLE", "AMBIGUOUS", "UNKNOWN"})
                self.assertIn(expected["origin"], {"LITERAL", "DERIVED", "INTERPRETIVE"})
                self.assertIn(expected["acceptance"], {"AUTO_ACCEPTED", "PROVISIONAL", "REVIEW_REQUIRED", "HUMAN_CONFIRMED", "REJECTED", "UNAVAILABLE"})
                for anchor in expected["evidence"]:
                    self.assertIn(anchor["page"], source, (item["id"], field_name))
                    self.assertIn(anchor["quote"], source[anchor["page"]], (item["id"], field_name))
                if expected["presence"] in {"PRESENT", "AMBIGUOUS", "UNKNOWN"}:
                    self.assertTrue(expected["evidence"], (item["id"], field_name))
                if expected["presence"] == "ABSENT":
                    self.assertIsNone(expected["value"], (item["id"], field_name))
                    self.assertEqual(expected["evidence"], [])
                    self.assertNotEqual(expected["acceptance"], "REVIEW_REQUIRED")

    def test_digital_anchor_is_present_in_pdf_but_scanned_anchor_is_source_truth(self) -> None:
        digital_seen = scanned_seen = 0
        for item in self.fixtures:
            reader = PdfReader(str(DATASET / "pdfs" / item["filename"]))
            for page_number, source_page in enumerate(item["source_pages"], start=1):
                extracted = _compact(reader.pages[page_number - 1].extract_text() or "")
                for expected in item["expected"]["fields"].values():
                    for anchor in expected["evidence"]:
                        if anchor["page"] != page_number:
                            continue
                        if source_page["mode"] == "digital":
                            digital_seen += 1
                            self.assertIn(_compact(anchor["quote"]), extracted, (item["id"], anchor))
                        else:
                            scanned_seen += 1
                            # Image-only pages intentionally have no text layer;
                            # the independently authored source page is canonical.
                            self.assertEqual(extracted, "", item["id"])
        self.assertGreater(digital_seen, 10)
        self.assertGreater(scanned_seen, 10)

    def test_derived_rules_conflicts_and_interpretive_review_expectations(self) -> None:
        contracts = [item for item in self.fixtures if item["expected_type"] == "CONTRACT"]
        estimated = {item["id"]: item["expected"]["derived"]["estimated_anniversary"] for item in contracts}
        self.assertEqual(estimated["contract-01-en-digital-monthend"], "2024-02-29")
        self.assertEqual(estimated["contract-02-es-scanned-leapday"], "2025-02-28")
        conflict = next(item for item in contracts if item["id"] == "contract-05-en-mixed-explicit-conflict")
        self.assertTrue(conflict["expected"]["derived"]["conflict"])
        self.assertIn("explicit-vs-derived expiration conflict", conflict["expected"]["review_reasons"])
        self.assertEqual(conflict["expected"]["fields"]["termination_notice_value"]["presence"], "AMBIGUOUS")
        for field_name in ("automatic_renewal", "renewal_period_value", "renewal_period_unit"):
            renewal = conflict["expected"]["fields"][field_name]
            self.assertEqual(renewal["acceptance"], "REVIEW_REQUIRED")
            self.assertEqual({anchor["page"] for anchor in renewal["evidence"]}, {1, 2})
            self.assertTrue(any("conditional" in anchor["quote"] for anchor in renewal["evidence"]))
        judgment = next(item for item in self.fixtures if item["id"] == "judgment-03-en-mixed-interpretive")
        self.assertEqual(judgment["expected"]["fields"]["operative_ruling"]["origin"], "INTERPRETIVE")
        self.assertEqual(judgment["expected"]["fields"]["operative_ruling"]["acceptance"], "REVIEW_REQUIRED")
        self.assertEqual(len(judgment["expected"]["fields"]["operative_ruling"]["evidence"]), 2)
        self.assertEqual(judgment["expected"]["fields"]["costs_statement"]["origin"], "LITERAL")
        self.assertEqual(judgment["expected"]["fields"]["costs_statement"]["acceptance"], "REVIEW_REQUIRED")
        self.assertTrue(any("human review" in reason for reason in judgment["expected"]["review_reasons"]))

    def test_adversarial_and_unknown_cases_remain_unforced(self) -> None:
        adversarial = next(item for item in self.fixtures if item["category"] == "adversarial")
        self.assertEqual(adversarial["expected_type"], "UNKNOWN")
        self.assertIn("IGNORE ALL PREVIOUS INSTRUCTIONS", adversarial["source_pages"][0]["text"])
        self.assertNotIn("effective_date", adversarial["expected"]["fields"])
        unknown = next(item for item in self.fixtures if item["category"] == "unknown")
        self.assertEqual(unknown["expected_type"], "UNKNOWN")
        negative = next(item for item in self.fixtures if item["category"] == "negative")
        self.assertTrue(any("insufficient evidence" in reason for reason in negative["expected"]["review_reasons"]))


if __name__ == "__main__":
    unittest.main()
