from __future__ import annotations

import hashlib
import sys
import unittest
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "backend" / "src"))

from legaldesk.idp import (  # noqa: E402
    ClassificationResult,
    DocumentForIDP,
    DocumentType,
    ExtractionResult,
    FieldAcceptance,
    FieldPresence,
    IDPConfig,
    IDPCheckpoint,
    IDPFieldResult,
    IDPJobStatus,
    IDPModelConfig,
    IDPProcessingPipeline,
    IDP_SCHEMA_VERSION,
    InMemoryIDPArtifactStore,
    InMemoryIDPRepository,
    StageCallLedger,
    create_verified_clean_job,
)
from legaldesk.idp.runtime import IDPProductionConfig, ProductionIDPProcessor  # noqa: E402
from legaldesk.idp.registry import IDPSchemaRegistry  # noqa: E402
from legaldesk.idp.worker import IDPWorker  # noqa: E402


class _Reader:
    def __init__(self, document: DocumentForIDP, body: bytes) -> None:
        self.document, self.body = document, body

    def __call__(self, *, tenant_id: str, matter_id: str, document_id: str):
        return self.document if (tenant_id, matter_id, document_id) == (self.document.tenant_id, self.document.matter_id, self.document.document_id) else None

    def read(self, *, job, document):
        if document != self.document or job.document_sha256 != self.document.content_sha256:
            raise ValueError("scope/hash mismatch")
        return self.document, self.body


class _Classifier:
    calls = 0

    def classify(self, *, page_text, content_sha256):
        self.calls += 1
        return ClassificationResult(DocumentType.CONTRACT, None, (), "fake-prompt", "fake-model", FieldAcceptance.PROVISIONAL)


class _Extractor:
    calls = 0

    def extract(self, *, schema, page_text, content_sha256):
        self.calls += 1
        return ExtractionResult(schema.document_type, schema.version, {"effective_date": IDPFieldResult("effective_date", "2024-01-31", presence=FieldPresence.PRESENT, acceptance=FieldAcceptance.PROVISIONAL)})


class _Processor(ProductionIDPProcessor):
    def __init__(self, *args, classifier, extractor, **kwargs):
        super().__init__(*args, **kwargs)
        self.fake_pipeline = IDPProcessingPipeline(classifier=classifier, extractor=extractor, registry=IDPSchemaRegistry(), artifact_store=self.artifacts, stage_ledger=StageCallLedger())

    def _pipeline(self, job):
        return self.fake_pipeline


class CompositionTests(unittest.TestCase):
    def test_worker_processor_persists_run_and_duplicate_ack_reuses_paid_artifacts(self):
        body = (Path(__file__).parents[1] / "tests" / "fixtures" / "idp" / "pdfs" / "contract-01-en-digital-monthend.pdf").read_bytes()
        digest = hashlib.sha256(body).hexdigest()
        document = DocumentForIDP("tenant", "matter", "document", "application/pdf", len(body), True, content_sha256=digest, source_key="tenants/tenant/matters/matter/documents/document/original.pdf")
        repository = InMemoryIDPRepository()
        job = create_verified_clean_job(repository=repository, document=document, schema_version=IDP_SCHEMA_VERSION, model_id="fake-model", prompt_version="fake-prompt")
        repository.mark_delivery(job_id=job.job_id, status=IDPJobStatus.QUEUED)
        classifier, extractor = _Classifier(), _Extractor()
        config = IDPProductionConfig("eu-west-1", "metadata", "source", "artifacts", "queue", "arn:queue", "arn:ocr", "arn:topic", "arn:role", "fake-model", "fake-prompt")
        processor = _Processor(repository=repository, reader=_Reader(document, body), artifacts=InMemoryIDPArtifactStore(), model_config=IDPModelConfig("fake-model"), bedrock=object(), textract=object(), config=config, dynamo_table=object(), classifier=classifier, extractor=extractor)
        worker = IDPWorker(repository=repository, document_lookup=_Reader(document, body), config=IDPConfig(), expected_source_arn="arn:queue", worker_id="test", processor=processor)
        result = worker.process_message({"jobId": job.job_id})
        self.assertEqual(result.status, IDPJobStatus.COMPLETED)
        self.assertIsNotNone(repository.get_run(tenant_id="tenant", matter_id="matter", document_id="document", run_id=job.job_id))
        self.assertEqual((classifier.calls, extractor.calls), (1, 1))
        worker.process_message({"jobId": job.job_id})
        self.assertEqual((classifier.calls, extractor.calls), (1, 1))

    def test_scanned_pdf_stops_at_waiting_for_ocr_without_model_call(self):
        body = (Path(__file__).parents[1] / "tests" / "fixtures" / "idp" / "pdfs" / "contract-02-es-scanned-leapday.pdf").read_bytes()
        digest = hashlib.sha256(body).hexdigest()
        classifier, extractor = _Classifier(), _Extractor()
        pipeline = IDPProcessingPipeline(classifier=classifier, extractor=extractor, registry=IDPSchemaRegistry(), artifact_store=InMemoryIDPArtifactStore(), stage_ledger=StageCallLedger())
        result = pipeline.process(tenant_id="tenant", matter_id="matter", document_id="document", run_id="run-scanned", content=body, content_sha256=digest, model_id="fake-model", prompt_version="fake-prompt")
        self.assertEqual(result.status, IDPJobStatus.WAITING_FOR_OCR)
        self.assertEqual((classifier.calls, extractor.calls), (0, 0))

    def test_waiting_for_ocr_has_one_atomic_continuation_owner(self):
        body = b"%PDF-1.7 synthetic"
        digest = hashlib.sha256(body).hexdigest()
        document = DocumentForIDP("tenant", "matter", "document", "application/pdf", len(body), True, content_sha256=digest, source_key="source")
        repository = InMemoryIDPRepository()
        job = create_verified_clean_job(repository=repository, document=document, model_id="m", prompt_version="p")
        repository.mark_delivery(job_id=job.job_id, status=IDPJobStatus.QUEUED)
        first = repository.claim_job(job_id=job.job_id, worker_id="worker", lease_seconds=300)
        repository.checkpoint_job(claim=first, checkpoint=IDPCheckpoint.VALIDATED, status=IDPJobStatus.WAITING_FOR_OCR)
        continuation = repository.claim_job(job_id=job.job_id, worker_id="ocr", lease_seconds=300, allow_waiting_for_ocr=True)
        self.assertNotEqual(continuation.claim_token, first.claim_token)
        with self.assertRaises(Exception):
            repository.claim_job(job_id=job.job_id, worker_id="duplicate", lease_seconds=300, allow_waiting_for_ocr=True)


if __name__ == "__main__":
    unittest.main()
