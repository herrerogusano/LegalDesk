from __future__ import annotations

import hashlib
import io
import json
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

    def test_duration_units_canonicalize_literal_english_and_spanish_aliases(self) -> None:
        schema = IDPSchemaRegistry().get(DocumentType.CONTRACT)
        cases = (
            ("month", "The initial term is one (1) calendar month.", "MONTH"),
            ("YEARS", "The agreement remains in force for two (2) years.", "YEAR"),
            ("meses", "La duración inicial será de doce (12) meses.", "MONTH"),
            ("DÍAS", "La duración inicial será de diez (10) días.", "DAY"),
        )
        for value, quote, expected in cases:
            with self.subTest(value=value):
                result = parse_extractor_output(
                    json.dumps({"fields": {"initial_duration_unit": {"presence": "PRESENT", "value": value, "evidence": [{"page": 1, "quote": quote}]}}}),
                    schema=schema,
                    page_text={1: quote},
                    content_sha256="a" * 64,
                )
                field = result.fields["initial_duration_unit"]
                self.assertEqual(field.value, expected)
                self.assertEqual(field.acceptance, FieldAcceptance.AUTO_ACCEPTED)
                self.assertEqual(field.origin.value, "LITERAL")

    def test_unsupported_duration_unit_cannot_become_auto_accepted(self) -> None:
        schema = IDPSchemaRegistry().get(DocumentType.CONTRACT)
        quote = "The initial term is two (2) weeks."
        result = parse_extractor_output(
            json.dumps({"fields": {"initial_duration_unit": {"presence": "PRESENT", "value": "weeks", "evidence": [{"page": 1, "quote": quote}]}}}),
            schema=schema,
            page_text={1: quote},
            content_sha256="a" * 64,
        )
        field = result.fields["initial_duration_unit"]
        self.assertEqual(field.value, "weeks")
        self.assertEqual(field.acceptance, FieldAcceptance.PROVISIONAL)
        self.assertEqual(field.origin.value, "INTERPRETIVE")
        self.assertFalse(field.validation["lexical_support"])

    def test_real_model_duration_unit_spellings_canonicalize_to_fixture_oracle(self) -> None:
        # Regression for the 2026-10-09 real-model exports: these spellings
        # were AUTO_ACCEPTED raw (digests below are the exported valueDigest)
        # and therefore mismatched the fixture's canonical unit digest.
        manifest = json.loads((Path(__file__).parent / "fixtures" / "idp" / "manifest.json").read_text(encoding="utf-8"))
        fixtures = {item["id"]: item for item in manifest["fixtures"]}
        schema = IDPSchemaRegistry().get(DocumentType.CONTRACT)
        cases = (
            ("contract-01-en-digital-monthend", "month", "181d90829828f91d8d92fb4b3fbd0dcdfeb130d6a1982eb05253810d07d802c9"),
            ("contract-03-en-mixed-nda", "years", "a45cc62d46b1"),
            ("contract-05-en-mixed-explicit-conflict", "year", "2992f9abc1d6"),
        )
        for fixture_id, spelling, exported_prefix in cases:
            with self.subTest(fixture=fixture_id):
                fixture = fixtures[fixture_id]
                expected = fixture["expected"]["fields"]["initial_duration_unit"]
                self.assertTrue(hashlib.sha256(json.dumps(spelling).encode()).hexdigest().startswith(exported_prefix))
                pages = {index: page["text"] for index, page in enumerate(fixture["source_pages"], start=1)}
                evidence = [{"page": anchor["page"], "quote": anchor["quote"]} for anchor in expected["evidence"]]
                field = parse_extractor_output(
                    json.dumps({"fields": {"initial_duration_unit": {"presence": "PRESENT", "value": spelling, "evidence": evidence}}}),
                    schema=schema,
                    page_text=pages,
                    content_sha256=fixture["sha256"],
                ).fields["initial_duration_unit"]
                self.assertEqual(field.value, expected["value"])
                self.assertEqual(field.acceptance, FieldAcceptance.AUTO_ACCEPTED)
                self.assertEqual(field.origin.value, "LITERAL")

    def test_canonical_duration_unit_contradicting_its_quote_is_not_auto_accepted(self) -> None:
        schema = IDPSchemaRegistry().get(DocumentType.CONTRACT)
        quote = "The initial term is one (1) calendar month."
        for value in ("year", "DAY", "años"):
            with self.subTest(value=value):
                field = parse_extractor_output(
                    json.dumps({"fields": {"initial_duration_unit": {"presence": "PRESENT", "value": value, "evidence": [{"page": 1, "quote": quote}]}}}),
                    schema=schema,
                    page_text={1: quote},
                    content_sha256="a" * 64,
                ).fields["initial_duration_unit"]
                self.assertNotEqual(field.acceptance, FieldAcceptance.AUTO_ACCEPTED)
                self.assertEqual(field.origin.value, "INTERPRETIVE")
                self.assertFalse(field.validation["lexical_support"])

    def _subtype(self, value: str, quote: str, page_text: dict[int, str] | None = None):
        return parse_extractor_output(
            json.dumps({"fields": {"subtype": {"presence": "PRESENT", "value": value, "evidence": [{"page": 1, "quote": quote}]}}}),
            schema=IDPSchemaRegistry().get(DocumentType.CONTRACT),
            page_text=page_text or {1: quote},
            content_sha256="a" * 64,
        ).fields["subtype"]

    def test_heading_like_subtype_with_valid_anchor_is_absent_not_auto_accepted(self) -> None:
        # Regression for 2026-10-13 real-model eval: the page-1 all-caps title
        # was returned as subtype and AUTO_ACCEPTED while the oracle is absent.
        for value in ("SERVICES AGREEMENT", "Master Services Agreement", "CONTRATO DE PRESTACIÓN DE SERVICIOS", "services"):
            with self.subTest(value=value):
                field = self._subtype(value, value.upper() if value.isupper() else value)
                self.assertIsNone(field.value)
                self.assertEqual(field.presence.value, "ABSENT")
                self.assertEqual(field.acceptance, FieldAcceptance.UNAVAILABLE)
                self.assertEqual(field.evidence, ())

    def test_explicit_nda_in_english_and_spanish_canonicalizes_and_is_auto_accepted(self) -> None:
        cases = (
            ("NDA", "This is a confidentiality agreement (NDA)."),
            ("Non-Disclosure Agreement", "NON-DISCLOSURE AGREEMENT between ACME and Beta"),
            ("nda", "Mutual NDA effective 2026-01-15"),
            ("acuerdo de confidencialidad", "ACUERDO DE CONFIDENCIALIDAD entre ACME y Beta"),
            ("NDA", "Este es un acuerdo de confidencialidad."),
        )
        for value, quote in cases:
            with self.subTest(value=value, quote=quote):
                field = self._subtype(value, quote)
                self.assertEqual(field.value, "NDA")
                self.assertEqual(field.acceptance, FieldAcceptance.AUTO_ACCEPTED)
                self.assertEqual(field.origin.value, "LITERAL")

    def test_nda_value_not_supported_by_its_quote_is_not_auto_accepted(self) -> None:
        field = self._subtype("NDA", "SERVICES AGREEMENT")
        self.assertEqual(field.value, "NDA")
        self.assertNotEqual(field.acceptance, FieldAcceptance.AUTO_ACCEPTED)
        self.assertFalse(field.validation["lexical_support"])

    def test_subtype_vocabulary_is_registry_data_not_control_flow(self) -> None:
        from legaldesk.idp import processing
        from legaldesk.idp.registry import CLOSED_VOCABULARIES

        kind = IDPSchemaRegistry().get(DocumentType.CONTRACT).field("subtype").lexical_kind
        self.assertEqual(tuple(CLOSED_VOCABULARIES[kind]), ("NDA",))
        extended = {**CLOSED_VOCABULARIES, kind: {**CLOSED_VOCABULARIES[kind], "MSA": ("master services agreement",)}}
        with patch.object(processing, "CLOSED_VOCABULARIES", extended):
            field = self._subtype("Master Services Agreement", "MASTER SERVICES AGREEMENT")
        self.assertEqual(field.value, "MSA")
        self.assertEqual(field.acceptance, FieldAcceptance.AUTO_ACCEPTED)

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
        self.assertEqual(classifier.version, "1.0.2")
        self.assertIn("CONTRACT", classifier.content)
        self.assertIn("DEMAND", classifier.content)
        self.assertIn("JUDGMENT", classifier.content)
        self.assertIn("preserve accents, dates, punctuation", classifier.content)
        self.assertIn("fictional disclaimer does not change", classifier.content)
        identity = idp_prompt_identity()
        self.assertIn("legaldesk-idp-classifier:1.0.2:", identity)


if __name__ == "__main__":
    unittest.main()
