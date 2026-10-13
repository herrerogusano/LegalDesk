"""Security-focused, provider-free adversarial tests for the Phase 14 IDP boundary.

These tests exercise the public parser/coordinator/worker contracts.  They use
synthetic page text and fake providers only; no Bedrock, Textract, OCR service,
AWS SDK, or network call is made.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "backend" / "src"))

from legaldesk.idp import (  # noqa: E402
    DocumentForIDP,
    IDPConfig,
    IDPSourceArnError,
    IDPWorker,
    InMemoryIDPRepository,
    enqueue_verified_clean_job,
    create_verified_clean_job,
)
from legaldesk.idp.ocr import (  # noqa: E402
    InMemoryOCRJobStore,
    OCRContractError,
    OCRCoordinator,
    OCRStartRequest,
    OCRStatus,
)
from legaldesk.idp.models import DocumentType  # noqa: E402
from legaldesk.idp.registry import IDPFieldSpec, IDPSchema, IDPSchemaRegistry  # noqa: E402
from legaldesk.idp.processing import IDPOutputError, parse_extractor_output  # noqa: E402


SHA = "a" * 64
EXPECTED_QUEUE_ARN = "arn:aws:sqs:eu-west-1:111122223333:legaldesk-idp"
EXPECTED_TOPIC_ARN = "arn:aws:sns:eu-west-1:111122223333:legaldesk-ocr"


def _page_text() -> dict[int, str]:
    return {1: "Effective date: 15 January 2026. Amount: USD 1000. Date token: 01/02/2026."}


def _field_output(**fields: object) -> str:
    return json.dumps({"schema_version": "1.0.0", "fields": fields})


def _present(value: object, quote: str, *, start: int | None = None, end: int | None = None) -> dict[str, object]:
    anchor: dict[str, object] = {"page": 1, "quote": quote, "content_sha256": SHA}
    if start is not None:
        anchor["start"] = start
    if end is not None:
        anchor["end"] = end
    return {"value": value, "presence": "PRESENT", "evidence": [anchor]}


class Queue:
    def publish(self, *, job_id: str) -> None:
        return None


def _document() -> DocumentForIDP:
    return DocumentForIDP(
        tenant_id="tenant-a", matter_id="matter-a", document_id="doc-a",
        media_type="application/pdf", file_size_bytes=1024,
        malware_scan_clean=True, page_count=1, content_sha256=SHA,
    )


def _ocr_request(run_id: str, token: str) -> OCRStartRequest:
    return OCRStartRequest(
        run_id, SHA, "synthetic-bucket", "synthetic/key.pdf", token,
        expected_page_count=1, expected_sqs_source_arn=EXPECTED_QUEUE_ARN,
        expected_sns_topic_arn=EXPECTED_TOPIC_ARN,
    )


def _completion_event(job_id: str, status: str, *, topic_arn: str = EXPECTED_TOPIC_ARN) -> dict[str, object]:
    """Build the SNS-in-SQS shape consumed by the coordinator."""
    return {"Records": [{
        "eventSourceARN": EXPECTED_QUEUE_ARN,
        "body": json.dumps({
            "TopicArn": topic_arn,
            "Message": json.dumps({"JobId": job_id, "Status": status}),
        }),
    }]}


class TextProvider:
    def __init__(self, responses: list[dict[str, object]] | None = None) -> None:
        self.responses = responses or []
        self.calls: list[str | None] = []

    def start_document_text_detection(self, request: OCRStartRequest) -> str:
        return "textract-job-1"

    def get_document_text_detection(self, *, textract_job_id: str, next_token: str | None = None) -> dict[str, object]:
        self.calls.append(next_token)
        if self.responses:
            return self.responses.pop(0)
        return {"JobId": textract_job_id, "Blocks": []}


class TestPhase14IDPAdversarial(unittest.TestCase):
    def test_real_quote_with_wrong_date_or_amount_cannot_be_auto_accepted(self) -> None:
        cases = (
            ("effective_date", "2099-12-31", "Effective date: 15 January 2026."),
            ("amount", 999999, "Amount: USD 1000."),
        )
        schema = IDPSchemaRegistry().get("CONTRACT")
        for field_name, wrong_value, quote in cases:
            raw = _field_output(**{field_name: _present(wrong_value, quote)})
            try:
                result = parse_extractor_output(raw, schema=schema, page_text=_page_text(), content_sha256=SHA)
            except IDPOutputError:
                continue  # Rejecting the whole untrusted response is safe.
            self.assertNotEqual(result.fields[field_name].acceptance.value, "AUTO_ACCEPTED", field_name)

    def test_ambiguous_numeric_date_cannot_be_auto_accepted(self) -> None:
        schema = IDPSchemaRegistry().get("CONTRACT")
        raw = _field_output(effective_date=_present("2026-02-01", "Date token: 01/02/2026."))
        try:
            result = parse_extractor_output(raw, schema=schema, page_text=_page_text(), content_sha256=SHA)
        except IDPOutputError:
            return
        self.assertNotEqual(result.fields["effective_date"].acceptance.value, "AUTO_ACCEPTED")

    def test_malformed_one_field_does_not_erase_valid_neighbor(self) -> None:
        schema = IDPSchemaRegistry().get("CONTRACT")
        raw = _field_output(
            effective_date=_present("2026-01-15", "Effective date: 15 January 2026."),
            amount={"value": "not-a-number", "presence": "PRESENT", "evidence": []},
        )
        try:
            result = parse_extractor_output(raw, schema=schema, page_text=_page_text(), content_sha256=SHA)
        except IDPOutputError as exc:
            self.fail(f"malformed amount erased valid effective_date: {exc}")
        self.assertEqual(result.fields["effective_date"].value, "2026-01-15")
        self.assertEqual(result.fields["effective_date"].presence.value, "PRESENT")
        self.assertNotEqual(result.fields["amount"].acceptance.value, "AUTO_ACCEPTED")

    def test_invented_evidence_offsets_are_rejected(self) -> None:
        schema = IDPSchemaRegistry().get("CONTRACT")
        raw = _field_output(effective_date=_present("2026-01-15", "Effective date: 15 January 2026.", start=9999, end=10010))
        try:
            result = parse_extractor_output(raw, schema=schema, page_text=_page_text(), content_sha256=SHA)
        except IDPOutputError:
            return  # Rejecting fabricated offsets is safe.
        field = result.fields["effective_date"]
        self.assertNotEqual(field.acceptance.value, "AUTO_ACCEPTED")
        self.assertEqual(field.evidence, ())

    def test_wrong_sqs_arn_cannot_be_overridden_by_inner_payload_source(self) -> None:
        repository = InMemoryIDPRepository()
        job = create_verified_clean_job(repository=repository, document=_document())
        enqueue_verified_clean_job(repository=repository, queue=Queue(), job=job)
        worker = IDPWorker(
            repository=repository, document_lookup=lambda **_: _document(), config=IDPConfig(),
            expected_source_arn=EXPECTED_QUEUE_ARN, worker_id="adversarial-test",
        )
        event = {"Records": [{
            "messageId": "message-1", "eventSourceARN": "arn:aws:sqs:eu-west-1:attacker:other-queue",
            "body": json.dumps({"jobId": job.job_id, "sourceArn": EXPECTED_QUEUE_ARN}),
        }]}
        with self.assertRaises(IDPSourceArnError):
            worker.handle_sqs_event(event)

    def test_ocr_outer_wrong_queue_cannot_be_overridden_by_inner_message_source(self) -> None:
        provider = TextProvider()
        store = InMemoryOCRJobStore()
        coordinator = OCRCoordinator(provider, store)
        record = coordinator.start(_ocr_request("run-outer-arn", "token-outer-arn"))
        forged_event = {"Records": [{
            "eventSourceARN": "arn:aws:sqs:eu-west-1:attacker:wrong-queue",
            "body": json.dumps({
                "TopicArn": EXPECTED_TOPIC_ARN,
                "Message": json.dumps({
                    "JobId": record.textract_job_id,
                    "Status": "SUCCEEDED",
                    "sourceArn": EXPECTED_QUEUE_ARN,
                }),
            }),
        }]}
        with self.assertRaises(OCRContractError):
            coordinator.accept_completion(forged_event, source_arn=EXPECTED_QUEUE_ARN)

    def test_wrong_sns_topic_arn_is_rejected_even_with_outer_source_marker(self) -> None:
        provider = TextProvider()
        store = InMemoryOCRJobStore()
        coordinator = OCRCoordinator(provider, store)
        coordinator.start(_ocr_request("run-1", "token-1"))
        event = _completion_event("textract-job-1", "SUCCEEDED", topic_arn="arn:aws:sns:eu-west-1:attacker:wrong-topic")
        with self.assertRaises(OCRContractError):
            coordinator.accept_completion(event, source_arn=EXPECTED_QUEUE_ARN)

    def test_out_of_order_or_duplicate_ocr_completion_cannot_regress_success(self) -> None:
        provider = TextProvider()
        store = InMemoryOCRJobStore()
        coordinator = OCRCoordinator(provider, store)
        record = coordinator.start(_ocr_request("run-2", "token-2"))
        success = _completion_event(record.textract_job_id, "SUCCEEDED")
        failed = _completion_event(record.textract_job_id, "FAILED")
        coordinator.accept_completion(success, source_arn=EXPECTED_QUEUE_ARN)
        try:
            coordinator.accept_completion(failed, source_arn=EXPECTED_QUEUE_ARN)
        except OCRContractError:
            pass
        self.assertEqual(store.get_by_run("run-2").status, OCRStatus.SUCCEEDED)  # type: ignore[union-attr]
        coordinator.accept_completion(success, source_arn=EXPECTED_QUEUE_ARN)
        self.assertEqual(store.get_by_run("run-2").status, OCRStatus.SUCCEEDED)  # type: ignore[union-attr]

    def test_repeated_ocr_next_token_is_bounded(self) -> None:
        class RepeatingProvider(TextProvider):
            def get_document_text_detection(self, *, textract_job_id: str, next_token: str | None = None) -> dict[str, object]:
                self.calls.append(next_token)
                if len(self.calls) > 2:
                    raise AssertionError("pagination did not terminate after repeated token")
                return {"JobId": textract_job_id, "JobStatus": "SUCCEEDED", "DocumentMetadata": {"Pages": 1}, "Blocks": [], "NextToken": "same-token"}

        provider = RepeatingProvider()
        store = InMemoryOCRJobStore()
        coordinator = OCRCoordinator(provider, store)
        record = coordinator.start(_ocr_request("run-3", "token-3"))
        coordinator.accept_completion(_completion_event(record.textract_job_id, "SUCCEEDED"), source_arn=EXPECTED_QUEUE_ARN)
        with self.assertRaises(OCRContractError):
            coordinator.read_detection_pages(store.get_by_run("run-3"))  # type: ignore[arg-type]
        self.assertLessEqual(len(provider.calls), 2)

    def test_returned_page_beyond_expected_bound_is_not_silent_partial_success(self) -> None:
        provider = TextProvider([{"JobId": "textract-job-1", "JobStatus": "SUCCEEDED", "DocumentMetadata": {"Pages": 1}, "Blocks": [{"BlockType": "LINE", "Page": 3, "Text": "unexpected page"}]}])
        store = InMemoryOCRJobStore()
        coordinator = OCRCoordinator(provider, store)
        record = coordinator.start(_ocr_request("run-4", "token-4"))
        coordinator.accept_completion(_completion_event(record.textract_job_id, "SUCCEEDED"), source_arn=EXPECTED_QUEUE_ARN)
        with self.assertRaises(OCRContractError):
            coordinator.read_detection_pages(store.get_by_run("run-4"), max_pages=2)  # type: ignore[arg-type]

    def test_new_numeric_registry_field_cannot_accept_unrelated_quote(self) -> None:
        custom_schema = IDPSchema(
            DocumentType.CONTRACT,
            "1.1.0",
            {"new_numeric_field": IDPFieldSpec("new_numeric_field", "number", "Synthetic schema evolution field")},
        )
        registry = IDPSchemaRegistry({(DocumentType.CONTRACT, "1.1.0"): custom_schema})
        raw = json.dumps({
            "schema_version": "1.1.0",
            "fields": {"new_numeric_field": _present(42, "Effective date: 15 January 2026.")},
        })
        result = parse_extractor_output(raw, schema=registry.get(DocumentType.CONTRACT, "1.1.0"), page_text=_page_text(), content_sha256=SHA)
        self.assertNotEqual(result.fields["new_numeric_field"].acceptance.value, "AUTO_ACCEPTED")

    def test_model_cannot_inject_acceptance_or_origin(self) -> None:
        schema = IDPSchemaRegistry().get("CONTRACT")
        raw = json.dumps({
            "schema_version": "1.0.0",
            "fields": {"effective_date": {
                "value": "2026-01-15", "presence": "PRESENT",
                "acceptance": "HUMAN_CONFIRMED", "origin": "DERIVED",
                "evidence": [{"page": 1, "quote": "Effective date: 15 January 2026.", "content_sha256": SHA}],
            }},
        })
        try:
            result = parse_extractor_output(raw, schema=schema, page_text=_page_text(), content_sha256=SHA)
        except IDPOutputError:
            return  # Unknown control fields are rejected at the schema boundary.
        field = result.fields["effective_date"]
        self.assertNotEqual(field.acceptance.value, "HUMAN_CONFIRMED")
        self.assertNotEqual(field.origin.value, "DERIVED")

    def test_model_cannot_replace_server_content_hash_or_page(self) -> None:
        schema = IDPSchemaRegistry().get("CONTRACT")
        raw = json.dumps({
            "schema_version": "1.0.0",
            "fields": {"effective_date": {
                "value": "2026-01-15", "presence": "PRESENT",
                "evidence": [{"page": 99, "quote": "Effective date: 15 January 2026.", "content_sha256": "b" * 64}],
            }},
        })
        result = parse_extractor_output(raw, schema=schema, page_text=_page_text(), content_sha256=SHA)
        field = result.fields["effective_date"]
        self.assertNotEqual(field.acceptance.value, "AUTO_ACCEPTED")
        self.assertEqual(field.evidence, ())


if __name__ == "__main__":
    unittest.main()
