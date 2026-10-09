from __future__ import annotations

import io
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from threading import Barrier, Thread
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "backend" / "src"))

from legaldesk.idp import (  # noqa: E402
    DocumentType,
    EvidenceValidationError,
    FieldAcceptance,
    IDPOutputError,
    IDPSchemaRegistry,
    OCRCoordinator,
    OCRStartRequest,
    OCRStatus,
    PaidStage,
    PDFLimitExceeded,
    StageCallLedger,
    acquire_pdf,
    derive_anniversary,
    ocr_client_request_token,
    parse_classifier_output,
    parse_extractor_output,
    parse_strict_json,
)
from legaldesk.prompts import FileSystemSystemPromptProvider  # noqa: E402
from legaldesk.idp.providers import idp_prompt_identity  # noqa: E402


class _FakeOCR:
    def __init__(self) -> None:
        self.calls = 0

    def start_document_text_detection(self, request: OCRStartRequest) -> str:
        self.calls += 1
        return "textract-job"


class ProcessingTests(unittest.TestCase):
    def test_strict_json_rejects_duplicate_nonfinite_and_unknown(self) -> None:
        with self.assertRaises(IDPOutputError):
            parse_strict_json('{"a": 1, "a": 2}')
        with self.assertRaises(IDPOutputError):
            parse_strict_json('{"a": NaN}')
        with self.assertRaises(IDPOutputError):
            parse_classifier_output('{"document_type":"CONTRACT","unexpected":1}', page_text={}, content_sha256="a" * 64, prompt_version="1.0.0", model_id="m")

    def test_pdf_acquisition_enforces_signature_and_page_limit_without_truncation(self) -> None:
        with self.assertRaises(PDFLimitExceeded) as context:
            acquire_pdf(b"%PDF-1.7", max_bytes=4)
        self.assertEqual(context.exception.reason, "SIZE_LIMIT")

    def test_pdf_mixed_page_with_raster_body_requires_ocr_even_with_digital_header(self) -> None:
        class FakePage:
            def get(self, key):
                if key == "/Resources":
                    return {"/XObject": {"/image": {"/Subtype": "/Image", "/Width": 100, "/Height": 100}}}
                return None

            def extract_text(self):
                return "Digital header only"

        class FakeReader:
            is_encrypted = False
            pages = [FakePage()]

        with patch("legaldesk.idp.acquisition.PdfReader", return_value=FakeReader()):
            document = acquire_pdf(b"%PDF-1.7 synthetic")
        self.assertEqual(document.ocr_required_pages, (1,))
        self.assertFalse(document.coverage_complete)
        self.assertTrue(document.pages[0].has_raster_content)

    def test_classifier_and_extractor_validate_server_owned_evidence_and_acceptance(self) -> None:
        pages = {1: "This is a contract between ACME and Beta effective 2026-01-15."}
        classification = parse_classifier_output(
            '{"document_type":"CONTRACT","subtype":"NDA","evidence":[{"page":1,"quote":"contract between ACME and Beta"}]}',
            page_text=pages,
            content_sha256="a" * 64,
            prompt_version="1.0.0",
            model_id="m",
        )
        self.assertEqual(classification.document_type, DocumentType.CONTRACT)
        schema = IDPSchemaRegistry().get(DocumentType.CONTRACT)
        extracted = parse_extractor_output(
            '{"schema_version":"1.0.0","fields":{"effective_date":{"presence":"PRESENT","value":"2026-01-15","evidence":[{"page":1,"quote":"effective 2026-01-15"}]}}}',
            schema=schema,
            page_text=pages,
            content_sha256="a" * 64,
        )
        self.assertEqual(extracted.fields["effective_date"].acceptance, FieldAcceptance.AUTO_ACCEPTED)
        invalid = parse_extractor_output(
            '{"fields":{"effective_date":{"presence":"PRESENT","value":"2026-01-15","evidence":[{"page":1,"quote":"not on this page"}]}}}',
            schema=schema,
            page_text=pages,
            content_sha256="a" * 64,
        )
        self.assertEqual(invalid.fields["effective_date"].acceptance, FieldAcceptance.UNAVAILABLE)

    def test_invalid_field_does_not_discard_valid_field_and_unrelated_quote_stays_provisional(self) -> None:
        schema = IDPSchemaRegistry().get(DocumentType.CONTRACT)
        result = parse_extractor_output(
            '{"fields": {"effective_date": {"presence":"PRESENT","value":"2027-01-15","evidence":[{"page":1,"quote":"effective 2026-01-15"}]}, "amount": {"presence":"PRESENT","value":9999,"evidence":[{"page":1,"quote":"effective 2026-01-15"}]}}}',
            schema=schema,
            page_text={1: "The agreement is effective 2026-01-15."},
            content_sha256="a" * 64,
        )
        self.assertEqual(result.fields["effective_date"].acceptance, FieldAcceptance.PROVISIONAL)
        self.assertEqual(result.fields["effective_date"].origin.value, "INTERPRETIVE")
        self.assertEqual(result.fields["amount"].acceptance, FieldAcceptance.PROVISIONAL)

    def test_stage_ledger_has_distinct_atomic_paid_stages(self) -> None:
        ledger = StageCallLedger()
        barrier = Barrier(2)
        results: list[object] = []

        def begin() -> None:
            barrier.wait()
            try:
                results.append(ledger.begin(run_id="run", stage=PaidStage.CLASSIFIER, request_hash="cfg"))
            except Exception as exc:  # noqa: BLE001 - race outcome is the assertion
                results.append(exc)

        threads = [Thread(target=begin) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(not isinstance(item, Exception) for item in results), 1)

    def test_ocr_start_is_idempotent_and_callback_is_source_bound(self) -> None:
        provider = _FakeOCR()
        from legaldesk.idp import InMemoryOCRJobStore

        store = InMemoryOCRJobStore()
        coordinator = OCRCoordinator(provider, store)
        request = OCRStartRequest("run", "a" * 64, "bucket", "key", ocr_client_request_token(run_id="run", document_sha256="a" * 64), expected_page_count=1, expected_sqs_source_arn="arn:sqs", expected_sns_topic_arn="arn:sns")
        first = coordinator.start(request)
        self.assertIs(coordinator.start(request), first)
        self.assertEqual(provider.calls, 1)
        completion_event = {"Records": [{"eventSourceARN": "arn:sqs", "body": '{"TopicArn":"arn:sns","Message":"{\\"runId\\":\\"run\\",\\"JobId\\":\\"textract-job\\",\\"Status\\":\\"SUCCEEDED\\"}"}'}]}
        completion = coordinator.accept_completion(completion_event)
        self.assertEqual(completion.status, OCRStatus.SUCCEEDED)
        with self.assertRaises(Exception):
            coordinator.accept_completion({"Records": [{"eventSourceARN": "wrong", "body": '{"TopicArn":"arn:sns","Message":"{\\"JobId\\":\\"textract-job\\",\\"Status\\":\\"SUCCEEDED\\"}"}'}]})

    def test_calendar_rules_are_explicit_and_provisional(self) -> None:
        result = derive_anniversary(effective_date="2024-02-29", duration_value=1, duration_unit="years")
        self.assertEqual(result.value, "2025-02-28")
        self.assertEqual(result.acceptance, FieldAcceptance.PROVISIONAL)

    def test_idp_prompt_artifacts_are_versioned_and_separate(self) -> None:
        root = Path(__file__).parents[1] / "prompts"
        classifier = FileSystemSystemPromptProvider(root / "idp-classifier.md").load()
        extractor = FileSystemSystemPromptProvider(root / "idp-extractor.md").load()
        self.assertEqual(classifier.prompt_id, "legaldesk-idp-classifier")
        self.assertEqual(extractor.prompt_id, "legaldesk-idp-extractor")
        self.assertEqual(classifier.version, "1.0.1")
        self.assertIn("CONTRACT", classifier.content)
        self.assertIn("DEMAND", classifier.content)
        self.assertIn("JUDGMENT", classifier.content)
        self.assertIn("preserve accents, dates, punctuation", classifier.content)
        self.assertIn("fictional disclaimer does not change", classifier.content)
        identity = idp_prompt_identity()
        self.assertIn("legaldesk-idp-classifier:1.0.1:", identity)


if __name__ == "__main__":
    unittest.main()
