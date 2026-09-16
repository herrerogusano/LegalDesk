"""Deliberate Knowledge Base sync with conservative document lifecycle updates.

This is an operator workflow, not an HTTP endpoint. Callers select documents
from one stored tenant/matter scope before starting a data-source-wide sync.
Documents become INDEXED only after Bedrock reports COMPLETE with no failed
documents. Partial or timed-out jobs remain PENDING_INGESTION for reconciliation.
"""

from __future__ import annotations

from dataclasses import dataclass
from time import monotonic, sleep
from typing import Any, Callable, Mapping, Protocol, Sequence
from uuid import uuid4

from .documents import DocumentMetadataRepository
from .domain.models import Document, DocumentStatus


class KnowledgeBaseIngestionClient(Protocol):
    def start_ingestion_job(
        self,
        *,
        knowledgeBaseId: str,
        dataSourceId: str,
        clientToken: str,
    ) -> Mapping[str, Any]: ...

    def get_ingestion_job(
        self,
        *,
        knowledgeBaseId: str,
        dataSourceId: str,
        ingestionJobId: str,
    ) -> Mapping[str, Any]: ...


class S3ObjectVerifier(Protocol):
    def head_object(self, *, key: str) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class DocumentScopeRef:
    tenant_id: str
    matter_id: str
    document_id: str


@dataclass(frozen=True, slots=True)
class KnowledgeBaseSyncResult:
    ingestion_job_id: str
    status: str
    documents_updated: int
    failed_document_count: int | None


def run_knowledge_base_sync(
    *,
    client: KnowledgeBaseIngestionClient,
    object_verifier: S3ObjectVerifier,
    metadata_repository: DocumentMetadataRepository,
    knowledge_base_id: str,
    data_source_id: str,
    document_refs: Sequence[DocumentScopeRef],
    timeout_seconds: int = 1_800,
    poll_interval_seconds: int = 10,
    sleep_fn: Callable[[float], None] = sleep,
    monotonic_fn: Callable[[], float] = monotonic,
) -> KnowledgeBaseSyncResult:
    """Start one explicit full data-source sync and update selected documents.

    The operator must provide explicit tenant/matter/document references. No
    table scan occurs. The KB sync itself is global to its S3 data source; a
    successful, failure-free job confirms the selected uploaded documents have
    passed the ingestion run.
    """

    if not knowledge_base_id or not data_source_id:
        raise ValueError("knowledge base and data source IDs are required")
    if not document_refs:
        raise ValueError("at least one explicit document reference is required")
    if any(
        not ref.tenant_id or not ref.matter_id or not ref.document_id
        for ref in document_refs
    ):
        raise ValueError("document references must include tenant, matter, and document IDs")
    if len({(ref.tenant_id, ref.matter_id, ref.document_id) for ref in document_refs}) != len(document_refs):
        raise ValueError("document references must be unique")
    if timeout_seconds <= 0 or poll_interval_seconds <= 0:
        raise ValueError("timeout and poll interval must be positive")

    documents: list[Document] = []
    for ref in document_refs:
        document = metadata_repository.get_for_scope(
            tenant_id=ref.tenant_id,
            matter_id=ref.matter_id,
            document_id=ref.document_id,
        )
        if document is None:
            raise ValueError("a selected document is not present in the requested scope")
        if document.status not in {DocumentStatus.UPLOADED, DocumentStatus.FAILED}:
            raise ValueError("a selected document is not ready for a new ingestion attempt")
        documents.append(document)

    # A previous upload status is not proof the source still exists: S3
    # lifecycle or an approved removal may have deleted it since confirmation.
    for document in documents:
        try:
            original = object_verifier.head_object(key=document.s3_key)
            sidecar = object_verifier.head_object(
                key=f"{document.s3_key}.metadata.json"
            )
        except Exception as exc:
            raise ValueError("a selected source object or sidecar is unavailable") from exc
        original_size = original.get("ContentLength")
        sidecar_size = sidecar.get("ContentLength")
        original_metadata = original.get("Metadata")
        sidecar_metadata = sidecar.get("Metadata")
        expected_original_metadata = {
            "tenant-id": document.tenant_id,
            "matter-id": document.matter_id,
            "document-id": document.document_id,
        }
        if (
            not isinstance(original_size, int)
            or isinstance(original_size, bool)
            or original_size <= 0
            or original_size != document.file_size_bytes
            or original.get("ContentType") != document.media_type
            or not isinstance(original_metadata, Mapping)
            or any(
                original_metadata.get(name) != value
                for name, value in expected_original_metadata.items()
            )
            or not isinstance(sidecar_size, int)
            or isinstance(sidecar_size, bool)
            or sidecar_size <= 0
            or sidecar_size > 10 * 1024
            or sidecar.get("ContentType") != "application/json"
            or not isinstance(sidecar_metadata, Mapping)
            or sidecar_metadata.get("document-id") != document.document_id
        ):
            raise ValueError("a selected source object or sidecar is invalid")

    started = client.start_ingestion_job(
        knowledgeBaseId=knowledge_base_id,
        dataSourceId=data_source_id,
        clientToken=str(uuid4()),
    )
    job = started.get("ingestionJob")
    job_id = job.get("ingestionJobId") if isinstance(job, Mapping) else None
    if not isinstance(job_id, str) or not job_id:
        raise RuntimeError("Bedrock did not return an ingestion job ID")

    for document in documents:
        metadata_repository.update_status(
            tenant_id=document.tenant_id,
            matter_id=document.matter_id,
            document_id=document.document_id,
            status=DocumentStatus.PENDING_INGESTION,
        )

    deadline = monotonic_fn() + timeout_seconds
    terminal_status: str | None = None
    final_job: Mapping[str, Any] = {}
    while monotonic_fn() < deadline:
        response = client.get_ingestion_job(
            knowledgeBaseId=knowledge_base_id,
            dataSourceId=data_source_id,
            ingestionJobId=job_id,
        )
        current_job = response.get("ingestionJob")
        if not isinstance(current_job, Mapping):
            raise RuntimeError("Bedrock returned an invalid ingestion job response")
        final_job = current_job
        status = current_job.get("status")
        if status in {"COMPLETE", "FAILED", "STOPPED"}:
            terminal_status = str(status)
            break
        sleep_fn(poll_interval_seconds)

    if terminal_status is None:
        return KnowledgeBaseSyncResult(job_id, "TIMED_OUT", 0, None)

    statistics = final_job.get("statistics")
    failed_count = None
    if isinstance(statistics, Mapping):
        raw_failed_count = statistics.get("numberOfDocumentsFailed")
        if isinstance(raw_failed_count, int) and not isinstance(raw_failed_count, bool):
            failed_count = raw_failed_count

    if terminal_status in {"FAILED", "STOPPED"}:
        for document in documents:
            metadata_repository.update_status(
                tenant_id=document.tenant_id,
                matter_id=document.matter_id,
                document_id=document.document_id,
                status=DocumentStatus.FAILED,
            )
        return KnowledgeBaseSyncResult(job_id, terminal_status, len(documents), failed_count)

    if failed_count == 0:
        for document in documents:
            metadata_repository.update_status(
                tenant_id=document.tenant_id,
                matter_id=document.matter_id,
                document_id=document.document_id,
                status=DocumentStatus.INDEXED,
            )
        return KnowledgeBaseSyncResult(job_id, terminal_status, len(documents), failed_count)

    # A completed job with partial failures does not identify which selected
    # documents failed. Leave them pending rather than claim an unproven state.
    return KnowledgeBaseSyncResult(job_id, terminal_status, 0, failed_count)
