from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from evaluate_idp import EvaluationError, evaluate, load_manifest  # noqa: E402


def _manifest() -> dict:
    value = {
        "manifest_version": "test",
        "dataset_id": "offline-eval",
        "fixtures": [
            {
                "id": "case-one",
                "expected_type": "CONTRACT",
                "sha256": "a" * 64,
                "page_count": 2,
                "expected": {
                    "document_type": "CONTRACT",
                    "fields": {
                        "effective_date": {
                            "value": "2024-01-31",
                            "presence": "PRESENT",
                            "origin": "INTERPRETIVE",
                            "acceptance": "REVIEW_REQUIRED",
                            "evidence": [{"page": 1, "quote": "effective 31 January 2024"}],
                        },
                        "optional": {"value": None, "presence": "ABSENT", "evidence": []},
                    },
                    "derived": {"rule_id": "ADD_CALENDAR_MONTHS_V1", "conflict": False},
                },
            },
            {
                "id": "case-two",
                "expected_type": "UNKNOWN",
                "sha256": "b" * 64,
                "page_count": 1,
                "expected": {"document_type": "UNKNOWN", "fields": {}},
            },
        ],
    }
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "manifest.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return load_manifest(path)


class Phase14IDPEvaluationTests(unittest.TestCase):
    def test_absent_results_are_not_executed_and_missing_cost_is_unknown(self) -> None:
        report = evaluate(_manifest(), {"provenance": {"kind": "local"}, "results": []})
        self.assertEqual(report["counts"], {"fixtures": 2, "executed": 0, "not_executed": 2, "review_required": 0})
        self.assertEqual(report["cases"][0]["status"], "NOT_EXECUTED")
        self.assertEqual(report["classification_confusion"], {})
        self.assertIsNone(report["review_rate"]["rate"])
        self.assertEqual(report["cost"]["status"], "UNKNOWN")
        self.assertNotIn("0.0", json.dumps(report))

    def test_wrong_hash_and_fabricated_anchor_are_reported_without_source_pages(self) -> None:
        export = {
            "provenance": {"kind": "mock", "model_id": "offline-fixture"},
            "results": [
                {
                    "fixtureId": "case-one",
                    "documentType": "DEMAND",
                    "documentSha256": "c" * 64,
                    "status": "FAILED",
                    # This must never be treated as OCR evidence or emitted.
                    "source_pages": [{"text": "SYNTHETIC_SECRET_DOCUMENT_TEXT"}],
                    "fields": {
                        "effective_date": {
                            "presence": "PRESENT",
                            "value": "wrong",
                            "origin": "INTERPRETIVE",
                            "acceptance": "AUTO_ACCEPTED",
                            "evidence": [{"page": 9, "quote": "invented anchor", "contentSha256": "b" * 64}],
                        }
                    },
                    "observed": {"tokens": {"input": 10}},
                }
            ],
        }
        report = evaluate(_manifest(), export)
        case = report["cases"][0]
        self.assertEqual(case["classification"]["match"], "FAIL")
        self.assertEqual(case["document_hash"]["match"], "FAIL")
        self.assertIn("EVIDENCE_PAGE_OUT_OF_RANGE", case["fields"]["effective_date"]["evidence"]["errors"])
        self.assertIn("ORACLE_ANCHOR_MISMATCH", case["fields"]["effective_date"]["evidence"]["errors"])
        self.assertIn("EVIDENCE_CONTENT_HASH_MISMATCH", case["fields"]["effective_date"]["evidence"]["errors"])
        self.assertIn("EVIDENCE_CONTENT_HASH_NOT_FIXTURE", case["fields"]["effective_date"]["evidence"]["errors"])
        self.assertIn("ACCEPTANCE_UNSAFE_AUTO_ACCEPTED", case["fields"]["effective_date"]["evidence"]["errors"])
        self.assertIn("FIELD_MISSING", case["fields"]["optional"]["evidence"]["errors"])
        rendered = json.dumps(report, sort_keys=True)
        self.assertNotIn("SYNTHETIC_SECRET_DOCUMENT_TEXT", rendered)
        self.assertNotIn("source_pages", rendered)
        self.assertFalse(report["provenance"]["counts_as_real_model"])

    def test_complete_explicit_metrics_can_produce_a_cost_estimate(self) -> None:
        export = {
            "provenance": {"kind": "real_model", "model_id": "approved-profile", "prompt_version": "idp-v1"},
            "results": [
                {
                    "fixtureId": "case-one",
                    "documentType": "CONTRACT",
                    "documentSha256": "a" * 64,
                    "status": "COMPLETED",
                    "fields": {
                        "effective_date": {"presence": "PRESENT", "value": "2024-01-31", "origin": "INTERPRETIVE", "acceptance": "AUTO_ACCEPTED", "evidence": [{"page": 1, "quote": "effective 31 January 2024", "contentSha256": "a" * 64}]},
                        "optional": {"presence": "ABSENT", "evidence": []},
                    },
                    "derived": {"rule_id": "ADD_CALENDAR_MONTHS_V1", "conflict": False},
                    "observed": {"tokens": {"input": 1000, "output": 500}, "ocr": {"pages": 2, "api_calls": 1}, "latency_ms": 2500},
                }
            ],
        }
        pricing = {"region": "eu-west-1", "currency": "USD", "bedrock": {"input_per_1k_tokens": 1, "output_per_1k_tokens": 2}, "textract": {"per_page": 0.01}}
        report = evaluate(_manifest(), export, pricing)
        self.assertTrue(report["provenance"]["counts_as_real_model"])
        self.assertEqual(report["cost"]["status"], "ESTIMATED")
        self.assertEqual(report["cost"]["estimate"], 2.02)
        self.assertEqual(report["cases"][0]["derived"]["status"], "PASS")
        self.assertEqual(report["observed_metrics"]["latency_ms"]["value"], 2500)

    def test_alternate_quote_is_oracle_mismatch_not_proven_fabrication(self) -> None:
        report = evaluate(_manifest(), {"provenance": {"kind": "mock"}, "results": [{
            "fixtureId": "case-one", "documentType": "CONTRACT", "documentSha256": "a" * 64,
            "fields": {"effective_date": {"presence": "PRESENT", "value": "2024-01-31", "origin": "INTERPRETIVE", "acceptance": "REVIEW_REQUIRED", "evidence": [{"page": 1, "quote": "an alternate observed quote", "contentSha256": "a" * 64}]}},
        }]})
        evidence = report["cases"][0]["fields"]["effective_date"]["evidence"]
        self.assertEqual(evidence["valid"], "UNKNOWN")
        self.assertIn("ORACLE_ANCHOR_MISMATCH", evidence["errors"])

    def test_unknown_ids_and_duplicates_fail_closed(self) -> None:
        with self.assertRaises(EvaluationError):
            evaluate(_manifest(), {"provenance": {"kind": "local"}, "results": [{"fixtureId": "not-in-manifest"}]})
        duplicate = {"provenance": {"kind": "local"}, "results": [{"fixtureId": "case-one"}, {"fixtureId": "case-one"}]}
        with self.assertRaises(EvaluationError):
            evaluate(_manifest(), duplicate)

    def test_real_model_provenance_is_not_inferred_from_present_records(self) -> None:
        report = evaluate(_manifest(), {"results": [{"fixtureId": "case-one"}]})
        self.assertEqual(report["provenance"]["kind"], "unknown")
        self.assertFalse(report["provenance"]["counts_as_real_model"])

    def test_mixed_provenance_and_non_finite_pricing_fail_closed(self) -> None:
        mixed = {"provenance": {"kind": "real_model"}, "results": [{"fixtureId": "case-one", "provenance": {"kind": "mock"}}]}
        with self.assertRaises(EvaluationError):
            evaluate(_manifest(), mixed)
        result = {"provenance": {"kind": "real_model"}, "results": [{"fixtureId": "case-one"}]}
        report = evaluate(_manifest(), result, {"region": "eu-west-1", "currency": "USD", "bedrock": {"input_per_1k_tokens": float("nan"), "output_per_1k_tokens": 1}, "textract": {"per_page": 1}})
        self.assertEqual(report["cost"]["status"], "UNKNOWN")
        self.assertTrue(report["provenance"]["counts_as_real_model"])
        empty_real = evaluate(_manifest(), {"provenance": {"kind": "real_model"}, "results": []})
        self.assertEqual(empty_real["provenance"]["observed_real_results"], 0)
        self.assertFalse(empty_real["provenance"]["counts_as_real_model"])

    def test_committed_manifest_dry_report_has_no_confusion_or_observed_records(self) -> None:
        manifest = load_manifest(ROOT / "tests" / "fixtures" / "idp" / "manifest.json")
        report = evaluate(manifest)
        self.assertEqual(report["counts"], {"fixtures": 18, "executed": 0, "not_executed": 18, "review_required": 0})
        self.assertEqual(report["classification_confusion"], {})
        self.assertEqual(report["provenance"]["observed_real_results"], 0)


if __name__ == "__main__":
    unittest.main()
