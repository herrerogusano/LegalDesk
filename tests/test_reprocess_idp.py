"""Offline contracts for the bounded synthetic-IDP reprocess operator."""

from __future__ import annotations

import hashlib
import sys
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from legaldesk.domain.models import DocumentStatus, MalwareScanStatus  # noqa: E402
from legaldesk.idp.persistence import InMemoryIDPRepository  # noqa: E402
from reprocess_idp import (  # noqa: E402
    ReprocessError,
    load_fixture,
    request_reprocess,
    validate_caller_identity,
    validate_operator_scope,
)


TENANT = "synthetic-beta-tenant"
MATTER = "synthetic-beta-matter"
DOCUMENT = "synthetic-document"


class _Document:
    tenant_id = TENANT
    matter_id = MATTER
    document_id = DOCUMENT
    media_type = "application/pdf"
    status = DocumentStatus.INDEXED
    malware_scan_status = MalwareScanStatus.CLEAN
    s3_key = "tenants/synthetic/document.pdf"
    idp_run_id = None
    idp_document_sha256 = None
    file_size_bytes = 0


class _Metadata:
    def __init__(self, document: _Document) -> None:
        self.document = document

    def get_for_scope(self, *, tenant_id: str, matter_id: str, document_id: str):
        if (tenant_id, matter_id, document_id) != (TENANT, MATTER, DOCUMENT):
            return None
        return self.document


class _Storage:
    def __init__(self, key: str, body: bytes) -> None:
        self.key, self.body = key, body

    def head_object(self, *, key: str):
        if key != self.key:
            raise KeyError(key)
        return {"ContentLength": len(self.body), "Metadata": {}}

    def read_object_bytes(self, *, key: str, max_bytes: int) -> bytes:
        if key != self.key or len(self.body) > max_bytes:
            raise KeyError(key)
        return self.body


class _Queue:
    def __init__(self) -> None:
        self.job_ids: list[str] = []

    def publish(self, *, job_id: str) -> str:
        self.job_ids.append(job_id)
        return f"message-{len(self.job_ids)}"


def _fixture_and_dependencies():
    fixture = load_fixture("contract-01-en-digital-monthend")
    body = fixture.path.read_bytes()
    document = _Document()
    document.file_size_bytes = len(body)
    return fixture, _Metadata(document), _Storage(document.s3_key, body), document


class ReprocessOperatorTests(unittest.TestCase):
    def test_scope_and_caller_allowlists_are_exact(self) -> None:
        environ = {
            "LEGALDESK_IDP_BETA_TENANT_ID": TENANT,
            "LEGALDESK_IDP_BETA_MATTER_ID": MATTER,
            "LEGALDESK_IDP_REVIEW_MATTER_IDS": MATTER,
        }
        validate_operator_scope(tenant_id=TENANT, matter_id=MATTER, environ=environ)
        with self.assertRaises(ReprocessError):
            validate_operator_scope(tenant_id="other", matter_id=MATTER, environ=environ)
        validate_caller_identity({"Arn": "arn:aws:iam::123456789012:role/idp-operator", "Account": "123456789012"}, expected_arn="arn:aws:iam::123456789012:role/idp-operator")
        with self.assertRaises(ReprocessError):
            validate_caller_identity({"Arn": "arn:aws:iam::123456789012:role/other", "Account": "123456789012"}, expected_arn="arn:aws:iam::123456789012:role/idp-operator")

    def test_reprocess_reads_current_bytes_and_enqueues_normal_job(self) -> None:
        fixture, metadata, storage, document = _fixture_and_dependencies()
        queue = _Queue()
        repository = InMemoryIDPRepository()
        job = request_reprocess(
            repository=repository, metadata=metadata, storage=storage, queue=queue,
            tenant_id=TENANT, matter_id=MATTER, document_id=DOCUMENT,
            fixture=fixture, model_id="synthetic-model", prompt_version="synthetic-prompt",
            generation_factory=lambda: "server-generation-1",
        )
        self.assertEqual(job.status.value, "QUEUED")
        self.assertEqual(job.document_sha256, fixture.sha256)
        self.assertEqual(queue.job_ids, [job.job_id])

    def test_changed_source_or_non_clean_document_fails_closed(self) -> None:
        fixture, metadata, storage, document = _fixture_and_dependencies()
        with self.assertRaisesRegex(ReprocessError, "canonical_source_hash_mismatch"):
            request_reprocess(
                repository=InMemoryIDPRepository(), metadata=metadata,
                storage=_Storage(document.s3_key, b"x" * document.file_size_bytes), queue=_Queue(),
                tenant_id=TENANT, matter_id=MATTER, document_id=DOCUMENT,
                fixture=fixture, model_id="m", prompt_version="p",
            )
        document.malware_scan_status = MalwareScanStatus.PENDING
        with self.assertRaisesRegex(ReprocessError, "document_not_verified_clean"):
            request_reprocess(
                repository=InMemoryIDPRepository(), metadata=metadata, storage=storage, queue=_Queue(),
                tenant_id=TENANT, matter_id=MATTER, document_id=DOCUMENT,
                fixture=fixture, model_id="m", prompt_version="p",
            )

    def test_cross_scope_document_reference_is_denied_before_storage_read(self) -> None:
        fixture, metadata, storage, _ = _fixture_and_dependencies()
        with self.assertRaisesRegex(ReprocessError, "document_not_found_in_scope"):
            request_reprocess(
                repository=InMemoryIDPRepository(), metadata=metadata, storage=storage, queue=_Queue(),
                tenant_id="other-tenant", matter_id=MATTER, document_id=DOCUMENT,
                fixture=fixture, model_id="m", prompt_version="p",
            )

    def test_same_generation_is_idempotent_and_preserves_existing_pointer(self) -> None:
        fixture, metadata, storage, document = _fixture_and_dependencies()
        document.idp_run_id = "old-run"
        document.idp_document_sha256 = fixture.sha256
        queue = _Queue()
        repository = InMemoryIDPRepository()
        factory = lambda: "same-generation"
        first = request_reprocess(
            repository=repository, metadata=metadata, storage=storage, queue=queue,
            tenant_id=TENANT, matter_id=MATTER, document_id=DOCUMENT,
            fixture=fixture, model_id="m", prompt_version="p", generation_factory=factory,
        )
        second = request_reprocess(
            repository=repository, metadata=metadata, storage=storage, queue=queue,
            tenant_id=TENANT, matter_id=MATTER, document_id=DOCUMENT,
            fixture=fixture, model_id="m", prompt_version="p", generation_factory=factory,
        )
        self.assertEqual(first.job_id, second.job_id)
        self.assertEqual(queue.job_ids, [first.job_id])
        self.assertEqual(document.idp_run_id, "old-run")
        self.assertEqual(document.idp_document_sha256, fixture.sha256)

    def test_current_non_terminal_generation_blocks_second_paid_generation(self) -> None:
        fixture, metadata, storage, document = _fixture_and_dependencies()
        repository = InMemoryIDPRepository()
        # A queued job is represented by the current pointer only in the
        # server-owned document metadata; the operator must not create another
        # paid generation while it is still active.
        initial = request_reprocess(
            repository=repository, metadata=metadata, storage=storage, queue=_Queue(),
            tenant_id=TENANT, matter_id=MATTER, document_id=DOCUMENT,
            fixture=fixture, model_id="m", prompt_version="p", generation_factory=lambda: "initial-op",
        )
        document.idp_run_id = initial.job_id
        with self.assertRaisesRegex(ReprocessError, "current_generation_not_terminal"):
            request_reprocess(
                repository=repository, metadata=metadata, storage=storage, queue=_Queue(),
                tenant_id=TENANT, matter_id=MATTER, document_id=DOCUMENT,
                fixture=fixture, model_id="m", prompt_version="p", generation_factory=lambda: "second-op",
            )


if __name__ == "__main__":
    unittest.main()
