"""Lazy production composition for the Phase 14 IDP workers.

The module has no import-time AWS clients.  Every document is re-read through
the existing authoritative metadata table and canonical S3 key before model
or Textract work, while IDP failures never mutate the RAG lifecycle.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from ..documents import Boto3DynamoDocumentMetadataRepository
from ..domain.models import DocumentStatus, MalwareScanStatus
from .artifacts import Boto3S3IDPArtifactStore, idp_artifact_key
from .models import DocumentForIDP, DocumentType, FieldAcceptance, FieldOrigin, FieldPresence, IDPCheckpoint, IDPClaim, IDPConfig, IDPContractError, IDPFieldResult, IDPJob, IDPJobStatus, IDPSkipReason
from .ocr import Boto3DynamoOCRJobStore, Boto3TextractProvider, OCRCoordinator, OCRStartRequest, OCRStatus, ocr_client_request_token
from .persistence import Boto3DynamoIDPRepository, Boto3DynamoStageLedger
from .acquisition import PDFAcquisitionError, PDFLimitExceeded
from .pipeline import IDPProcessingPipeline
from .processing import IDPOutputError
from .providers import IDPConverseClassifier, IDPConverseExtractor, IDPModelConfig, boto3_idp_runtime_client, idp_prompt_identity
from .registry import IDPSchemaRegistry
from .rules import DerivedMetadataEngine
from .worker import IDPDocumentLookup, IDPProcessor, IDPWorker


class IDPRuntimeConfigurationError(IDPContractError):
    pass


@dataclass(frozen=True, slots=True)
class IDPProductionConfig:
    region: str
    table_name: str
    source_bucket: str
    artifact_bucket: str
    work_queue_url: str
    work_queue_arn: str
    ocr_queue_arn: str
    ocr_topic_arn: str
    ocr_role_arn: str
    model_id: str
    prompt_version: str
    max_bytes: int = 20 * 1024 * 1024
    max_pages: int = 100
    max_calls_per_run: int = 2
    # Data-plane reads are deliberately short; Bedrock has its own longer
    # timeout because structured-output compilation can be cold on first use.
    read_timeout_seconds: int = 15
    max_ocr_api_calls: int = 4
    model_read_timeout_seconds: int = 120
    global_deadline_seconds: int = 330
    claim_lease_seconds: int = 480

    @classmethod
    def from_environment(cls, *, require_work_queue: bool = True) -> "IDPProductionConfig":
        def required(name: str) -> str:
            value = os.environ.get(name, "").strip()
            if not value:
                raise IDPRuntimeConfigurationError(f"{name} is required")
            return value

        def bounded_int(name: str, default: int, upper: int, lower: int = 1) -> int:
            raw = os.environ.get(name, str(default)).strip()
            if not raw.isdigit():
                raise IDPRuntimeConfigurationError(f"{name} is invalid")
            value = int(raw)
            if not lower <= value <= upper:
                raise IDPRuntimeConfigurationError(f"{name} is invalid")
            return value

        work_queue_url = os.environ.get("LEGALDESK_IDP_QUEUE_URL", "").strip()
        work_queue_arn = os.environ.get("LEGALDESK_IDP_QUEUE_ARN", "").strip()
        if require_work_queue and (not work_queue_url or not work_queue_arn):
            raise IDPRuntimeConfigurationError("IDP work queue configuration is required")
        return cls(
            region=os.environ.get("AWS_REGION", "eu-west-1").strip(),
            table_name=required("LEGALDESK_METADATA_TABLE_NAME"),
            source_bucket=required("LEGALDESK_SOURCE_BUCKET"),
            artifact_bucket=(os.environ.get("LEGALDESK_IDP_ARTIFACT_BUCKET", "").strip() or required("LEGALDESK_SOURCE_BUCKET")),
            work_queue_url=work_queue_url,
            work_queue_arn=work_queue_arn,
            ocr_queue_arn=required("LEGALDESK_IDP_OCR_SQS_SOURCE_ARN"),
            ocr_topic_arn=required("LEGALDESK_IDP_OCR_SNS_TOPIC_ARN"),
            ocr_role_arn=required("LEGALDESK_IDP_OCR_ROLE_ARN"),
            model_id=required("LEGALDESK_IDP_MODEL_ID"),
            prompt_version=idp_prompt_identity(os.environ.get("LEGALDESK_IDP_PROMPT_VERSION", "1.0.0")),
            max_bytes=bounded_int("LEGALDESK_IDP_MAX_BYTES", 20 * 1024 * 1024, 20 * 1024 * 1024),
            max_pages=bounded_int("LEGALDESK_IDP_MAX_PAGES", 100, 1000),
            max_calls_per_run=bounded_int("LEGALDESK_IDP_MAX_CALLS_PER_RUN", 2, 8),
            read_timeout_seconds=bounded_int("LEGALDESK_IDP_READ_TIMEOUT_SECONDS", 15, 15),
            max_ocr_api_calls=bounded_int("LEGALDESK_IDP_MAX_OCR_API_CALLS", 4, 4),
            model_read_timeout_seconds=bounded_int("LEGALDESK_IDP_MODEL_READ_TIMEOUT_SECONDS", 120, 120, 90),
            global_deadline_seconds=bounded_int("LEGALDESK_IDP_GLOBAL_DEADLINE_SECONDS", 330, 330, 60),
            claim_lease_seconds=bounded_int("LEGALDESK_IDP_CLAIM_LEASE_SECONDS", 480, 900, 361),
        )


class Boto3IDPDocumentReader:
    """Resolve metadata and canonical bytes from server-owned scope only."""

    def __init__(self, metadata: Any, s3_client: Any, *, bucket_name: str, max_bytes: int) -> None:
        self.metadata, self.s3, self.bucket_name, self.max_bytes = metadata, s3_client, bucket_name, max_bytes

    def __call__(self, *, tenant_id: str, matter_id: str, document_id: str) -> DocumentForIDP | None:
        document = self.metadata.get_for_scope(tenant_id=tenant_id, matter_id=matter_id, document_id=document_id)
        if document is None:
            return None
        clean = document.malware_scan_status is MalwareScanStatus.CLEAN and document.status in {DocumentStatus.UPLOADED, DocumentStatus.PENDING_INGESTION, DocumentStatus.INDEXED}
        return DocumentForIDP(tenant_id=document.tenant_id, matter_id=document.matter_id, document_id=document.document_id, media_type=document.media_type, file_size_bytes=document.file_size_bytes, malware_scan_clean=clean, content_sha256=None, source_key=document.s3_key)

    def read(self, *, job: IDPJob, document: DocumentForIDP) -> tuple[DocumentForIDP, bytes]:
        current = self(tenant_id=job.tenant_id, matter_id=job.matter_id, document_id=job.document_id)
        if current is None or current.tenant_id != job.tenant_id or current.matter_id != job.matter_id or current.document_id != job.document_id or current.source_key != document.source_key or not current.malware_scan_clean:
            raise IDPContractError("authoritative clean document scope changed")
        if current.source_key is None:
            raise IDPContractError("canonical source key is missing")
        head = self.s3.head_object(Bucket=self.bucket_name, Key=current.source_key)
        length = head.get("ContentLength") if isinstance(head, Mapping) else None
        if isinstance(length, bool) or not isinstance(length, int) or length < 1 or length > self.max_bytes or length != current.file_size_bytes:
            raise IDPContractError("canonical source size is invalid")
        response = self.s3.get_object(Bucket=self.bucket_name, Key=current.source_key, Range=f"bytes=0-{self.max_bytes - 1}")
        stream = response.get("Body") if isinstance(response, Mapping) else None
        body = stream.read(self.max_bytes + 1) if stream is not None and hasattr(stream, "read") else None
        if not isinstance(body, bytes) or len(body) != length:
            raise IDPContractError("canonical source read is incomplete")
        digest = hashlib.sha256(body).hexdigest()
        metadata = head.get("Metadata", {}) if isinstance(head, Mapping) else {}
        declared = metadata.get("legaldesk-sha256") if isinstance(metadata, Mapping) else None
        if declared is not None and declared != digest:
            raise IDPContractError("canonical source metadata hash mismatch")
        if digest != job.document_sha256:
            raise IDPContractError("canonical source hash changed")
        return replace(current, content_sha256=digest), body


class ProductionIDPProcessor(IDPProcessor):
    def __init__(self, *, repository: Any, reader: Boto3IDPDocumentReader, artifacts: Any, model_config: IDPModelConfig, bedrock: Any, textract: Any, config: IDPProductionConfig, dynamo_table: Any, review_dispatcher: Any | None = None) -> None:
        self.repository, self.reader, self.artifacts = repository, reader, artifacts
        self.model_config, self.bedrock, self.textract, self.config, self.table = model_config, bedrock, textract, config, dynamo_table
        self.review_dispatcher = review_dispatcher
        self._lambda_deadline: datetime | None = None
        self.registry = IDPSchemaRegistry()
        self.derived = DerivedMetadataEngine()
        self._pipelines: dict[tuple[str, str], IDPProcessingPipeline] = {}

    def _deadline(self, job: IDPJob, claim: IDPClaim, *, invocation_started: datetime | None = None) -> datetime:
        # Global budget is per Lambda invocation, never job age: OCR may wait
        # hours between callbacks and must not inherit the original deadline.
        started = invocation_started or datetime.now(timezone.utc)
        configured = started + timedelta(seconds=self.config.global_deadline_seconds)
        values = [configured, claim.claimed_until]
        if self._lambda_deadline is not None:
            values.append(self._lambda_deadline)
        return min(values)

    def set_lambda_context(self, context: Any) -> None:
        """Bind the actual remaining Lambda budget before processing starts."""
        getter = getattr(context, "get_remaining_time_in_millis", None)
        if not callable(getter):
            self._lambda_deadline = None
            return
        remaining = getter()
        if isinstance(remaining, bool) or not isinstance(remaining, (int, float)) or remaining <= 0:
            self._lambda_deadline = datetime.now(timezone.utc)
            return
        self._lambda_deadline = datetime.now(timezone.utc) + timedelta(seconds=max(0.0, float(remaining) / 1000.0 - 5.0))

    @staticmethod
    def _ensure_deadline(deadline: datetime, *, reserve_seconds: float = 0.0) -> None:
        if (deadline - datetime.now(timezone.utc)).total_seconds() <= reserve_seconds:
            raise IDPContractError("IDP global deadline exceeded")

    def _pipeline(self, job: IDPJob) -> IDPProcessingPipeline:
        key = (job.tenant_id, job.matter_id)
        pipeline = self._pipelines.get(key)
        if pipeline is None:
            classifier = IDPConverseClassifier(self.bedrock, config=self.model_config)
            extractor = IDPConverseExtractor(self.bedrock, config=self.model_config)
            pipeline = IDPProcessingPipeline(classifier=classifier, extractor=extractor, registry=self.registry, artifact_store=self.artifacts, stage_ledger=Boto3DynamoStageLedger(self.table, tenant_id=job.tenant_id, matter_id=job.matter_id), max_bytes=self.config.max_bytes, max_pages=self.config.max_pages, max_calls_per_run=self.config.max_calls_per_run)
            self._pipelines[key] = pipeline
        return pipeline

    def process(self, *, job: IDPJob, document: DocumentForIDP, claim: IDPClaim) -> IDPJob:
        if job.model_id != self.model_config.model_id or job.prompt_version != self.config.prompt_version or job.schema_version != self.registry.get(DocumentType.CONTRACT).version:
            return self._fail_ambiguous(job=job, claim=claim)
        try:
            authoritative, body = self.reader.read(job=job, document=document)
        except IDPContractError:
            # Scope/hash/configuration failures are terminal for this immutable
            # job identity; retries must not re-enter a paid stage.
            return self._fail_ambiguous(job=job, claim=claim)
        pipeline = self._pipeline(job)
        invocation_started = datetime.now(timezone.utc)
        deadline = self._deadline(job, claim, invocation_started=invocation_started)
        try:
            model_reserve = self.config.model_read_timeout_seconds + 20
            self._ensure_deadline(deadline, reserve_seconds=model_reserve)
            result = pipeline.process(tenant_id=job.tenant_id, matter_id=job.matter_id, document_id=job.document_id, run_id=job.job_id, content=body, content_sha256=job.document_sha256, model_id=self.model_config.model_id, prompt_version=self.config.prompt_version, deadline_at=deadline, deadline_reserve_seconds=model_reserve)
            if result.status is IDPJobStatus.WAITING_FOR_OCR:
                source_key = result.source_artifact_key or ""
                request = OCRStartRequest(job.job_id, job.document_sha256, self.config.artifact_bucket, source_key, ocr_client_request_token(run_id=job.job_id, document_sha256=job.document_sha256), expected_page_count=result.document.page_count if result.document else authoritative.page_count, expected_sqs_source_arn=self.config.ocr_queue_arn, expected_sns_topic_arn=self.config.ocr_topic_arn, canonical_source_key=authoritative.source_key)
                OCRCoordinator(Boto3TextractProvider(self.textract, notification_role_arn=self.config.ocr_role_arn, notification_topic_arn=self.config.ocr_topic_arn), Boto3DynamoOCRJobStore(self.table, tenant_id=job.tenant_id, matter_id=job.matter_id)).start(request)
                self._ensure_deadline(deadline, reserve_seconds=20)
                return self.repository.checkpoint_job(claim=claim, checkpoint=IDPCheckpoint.VALIDATED, status=IDPJobStatus.WAITING_FOR_OCR)
            return self._persist_completed(job=job, claim=claim, result=result)
        except PDFLimitExceeded as exc:
            reason = IDPSkipReason.PAGE_LIMIT if exc.reason == "PAGE_LIMIT" else IDPSkipReason.SIZE_LIMIT
            skipped = self.repository.checkpoint_job(claim=claim, checkpoint=IDPCheckpoint.VALIDATED, status=IDPJobStatus.SKIPPED, skip_reason=reason)
            self._project_job_state(skipped, source_key=authoritative.source_key)
            return skipped
        except (IDPOutputError, PDFAcquisitionError):
            return self._fail_ambiguous(job=job, claim=claim)

    def _persist_completed(self, *, job: IDPJob, claim: IDPClaim, result: Any) -> IDPJob:
        if result.run is None:
            raise IDPContractError("completed IDP result has no run")
        run = result.run
        fields = dict(run.fields)
        effective = fields.get("effective_date")
        duration = fields.get("initial_duration_value")
        unit = fields.get("initial_duration_unit")
        if all(item is not None and item.presence is FieldPresence.PRESENT for item in (effective, duration, unit)):
            try:
                derived = self.derived.derive("ADD_CALENDAR_MONTHS_V1" if str(unit.value).strip().lower() in {"month", "months", "mes", "meses"} else "ADD_CALENDAR_YEARS_V1", effective_date=effective.value, duration_value=duration.value, duration_unit=unit.value)
                if derived.field not in fields:
                    explicit = fields.get("explicit_expiration_date")
                    conflict = explicit is not None and explicit.presence is FieldPresence.PRESENT and explicit.value != derived.value
                    fields[derived.field] = IDPFieldResult(field=derived.field, value=derived.value, presence=FieldPresence.PRESENT, origin=FieldOrigin.DERIVED, acceptance=FieldAcceptance.REVIEW_REQUIRED if conflict else FieldAcceptance.PROVISIONAL, reason=derived.reason, provenance={"ruleId": derived.rule_id, "ruleVersion": derived.rule_version, "inputs": derived.inputs, "parameters": dict(derived.parameters or {}), "inputEvidence": {name: [{"page": anchor.page, "quote": anchor.quote, "contentSha256": anchor.content_sha256, "start": anchor.start, "end": anchor.end} for anchor in fields[name].evidence] for name in derived.inputs if name in fields}, "conflict": conflict, "conflictWith": "explicit_expiration_date" if conflict else None})
            except IDPContractError:
                # An unprovable/overflowing rule is a bounded provisional gap,
                # not permission to guess or fail the source document.
                pass
        if fields != dict(run.fields):
            from dataclasses import replace as _replace
            run = _replace(run, fields=fields)
        if any(field.acceptance is FieldAcceptance.REVIEW_REQUIRED for field in fields.values()) and run.status is IDPJobStatus.COMPLETED:
            from dataclasses import replace as _replace
            run = _replace(run, status=IDPJobStatus.REVIEW_REQUIRED)
        # A crash can occur after the immutable run is saved but before the
        # job checkpoint advances.  On replay the paid-stage artifacts are
        # reconstructed, but the durable run is authoritative: reuse it
        # byte-for-byte (including created_at, derived provenance, and review
        # status) instead of attempting a second immutable write with a new
        # timestamp or a pre-derivation field set.
        existing = self.repository.get_run(
            tenant_id=job.tenant_id,
            matter_id=job.matter_id,
            document_id=job.document_id,
            run_id=job.job_id,
        )
        if existing is not None:
            if (
                existing.tenant_id != job.tenant_id
                or existing.matter_id != job.matter_id
                or existing.document_id != job.document_id
                or existing.document_sha256 != job.document_sha256
                or existing.schema_version != job.schema_version
                or existing.model_id != job.model_id
                or existing.prompt_version != job.prompt_version
                or existing.run_id != job.job_id
            ):
                raise IDPContractError("durable IDP run identity changed")
            run = existing
        # Re-authorize the canonical source immediately before promoting the
        # document pointer.  A paid result may be retained as an immutable
        # artifact, but a changed/deleted source must never become current.
        current_document = self.reader(
            tenant_id=job.tenant_id,
            matter_id=job.matter_id,
            document_id=job.document_id,
        )
        if current_document is None:
            return self._fail_ambiguous(job=job, claim=claim)
        try:
            authoritative, _ = self.reader.read(job=job, document=current_document)
        except IDPContractError:
            return self._fail_ambiguous(job=job, claim=claim)
        if existing is not None and existing.source_key not in {None, authoritative.source_key}:
            return self._fail_ambiguous(job=job, claim=claim)
        run = replace(run, created_at=job.created_at, source_key=authoritative.source_key)

        # The pipeline's early run artifact is useful for stage recovery.  A
        # separate immutable final artifact includes derived fields, review
        # status, provenance, and the authoritative source identity.
        final_payload = json.dumps(
            {
                "runId": run.run_id,
                "tenantId": run.tenant_id,
                "matterId": run.matter_id,
                "documentId": run.document_id,
                "documentSha256": run.document_sha256,
                "sourceKey": run.source_key,
                "documentType": run.document_type.value,
                "schemaVersion": run.schema_version,
                "modelId": run.model_id,
                "promptVersion": run.prompt_version,
                "status": run.status.value,
                "createdAt": run.created_at.isoformat(),
                "fields": {
                    name: {
                        "field": field.field,
                        "value": field.value,
                        "presence": field.presence.value,
                        "origin": field.origin.value,
                        "acceptance": field.acceptance.value,
                        "reason": field.reason,
                        "validation": dict(field.validation),
                        "schemaVersion": field.schema_version,
                        "provenance": dict(field.provenance),
                        "evidence": [
                            {"page": anchor.page, "quote": anchor.quote, "contentSha256": anchor.content_sha256, "start": anchor.start, "end": anchor.end}
                            for anchor in field.evidence
                        ],
                    }
                    for name, field in run.fields.items()
                },
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        final_sha = hashlib.sha256(final_payload).hexdigest()
        self.artifacts.put_immutable(
            key=idp_artifact_key(
                tenant_id=job.tenant_id,
                matter_id=job.matter_id,
                document_id=job.document_id,
                run_id=job.job_id,
                kind="run-final",
                sha256=final_sha,
            ),
            body=final_payload,
            media_type="application/json",
            sha256=final_sha,
        )
        self.repository.save_run(run)
        project = getattr(self.repository, "project_document_status", None)
        if callable(project):
            project(run=run)
        if self.review_dispatcher is not None and run.status is IDPJobStatus.REVIEW_REQUIRED:
            try:
                self.review_dispatcher(run=run, job=job)
            except Exception:
                # The invocation record is durable and recoverable; review
                # dispatch ambiguity must not roll back the immutable run or
                # alter the RAG lifecycle.
                pass
        self.repository.checkpoint_job(claim=claim, checkpoint=IDPCheckpoint.PERSISTED)
        return self.repository.checkpoint_job(claim=claim, checkpoint=IDPCheckpoint.DONE, status=run.status)

    def _fail_ambiguous(self, *, job: IDPJob, claim: IDPClaim) -> IDPJob:
        failed = self.repository.checkpoint_job(claim=claim, checkpoint=IDPCheckpoint.AMBIGUOUS, status=IDPJobStatus.FAILED)
        self._project_job_state(failed)
        return failed

    def _project_job_state(self, job: IDPJob, *, source_key: str | None = None) -> None:
        project = getattr(self.repository, "project_document_job_status", None)
        if callable(project):
            project(job=job, source_key=source_key)

    def continue_ocr(self, completion: Any) -> IDPJob | None:
        locator = Boto3DynamoOCRJobStore.locate_scope(self.table, completion.textract_job_id)
        if locator is None:
            raise IDPContractError("OCR completion locator is missing")
        tenant_id, matter_id = locator
        store = Boto3DynamoOCRJobStore(self.table, tenant_id=tenant_id, matter_id=matter_id)
        record = store.get_by_textract_job_id(completion.textract_job_id)
        if record is None or record.status is not OCRStatus.SUCCEEDED:
            return None
        job = self.repository.get_job(record.run_id)
        if job is None or job.tenant_id != tenant_id or job.matter_id != matter_id or job.document_sha256 != record.document_sha256:
            raise IDPContractError("OCR continuation scope or hash mismatch")
        if job.status in {IDPJobStatus.COMPLETED, IDPJobStatus.REVIEW_REQUIRED, IDPJobStatus.FAILED, IDPJobStatus.SKIPPED}:
            return job
        document = self.reader(tenant_id=tenant_id, matter_id=matter_id, document_id=job.document_id)
        if document is None:
            raise IDPContractError("OCR continuation document is missing")
        if not record.canonical_source_key or document.source_key != record.canonical_source_key:
            raise IDPContractError("OCR continuation canonical source changed")
        # Re-authorize and hash the current canonical object before acquiring
        # the continuation claim, so a changed/deleted source cannot strand a
        # valid WAITING_FOR_OCR claim.
        authoritative, _ = self.reader.read(job=job, document=document)
        # Callback continuation always fences the producer claim with an
        # atomic WAITING_FOR_OCR -> CLAIMED transition.  It never borrows the
        # claim that started Textract, so duplicate callbacks have one winner.
        claim = self.repository.claim_job(job_id=job.job_id, worker_id="ocr-callback", lease_seconds=self.config.claim_lease_seconds, allow_waiting_for_ocr=True)
        source_key = idp_artifact_key(tenant_id=tenant_id, matter_id=matter_id, document_id=job.document_id, run_id=job.job_id, kind="source", sha256=job.document_sha256)
        self.artifacts.read_verified(key=source_key)
        try:
            deadline = self._deadline(job, claim, invocation_started=datetime.now(timezone.utc))
            self._ensure_deadline(deadline, reserve_seconds=1)
            pages = OCRCoordinator(Boto3TextractProvider(self.textract, notification_role_arn=self.config.ocr_role_arn, notification_topic_arn=self.config.ocr_topic_arn), store).read_detection_pages(record, max_pages=self.config.max_pages, max_api_calls=self.config.max_ocr_api_calls, deadline_at=deadline, deadline_reserve_seconds=20)
            self._ensure_deadline(deadline, reserve_seconds=self.config.model_read_timeout_seconds + 20)
            result = self._pipeline(job).process_pages(tenant_id=tenant_id, matter_id=matter_id, document_id=job.document_id, run_id=job.job_id, pages={page.page: page.text for page in pages}, content_sha256=job.document_sha256, model_id=self.model_config.model_id, prompt_version=self.config.prompt_version, deadline_at=deadline, deadline_reserve_seconds=self.config.model_read_timeout_seconds + 20)
            return self._persist_completed(job=job, claim=claim, result=result)
        except (IDPContractError, IDPOutputError) as exc:
            # A terminal callback with malformed/incomplete OCR must not leave
            # a claimed job PROCESSING forever.  Transport failures outside
            # this deterministic contract remain recoverable by lease expiry.
            del exc
            failed = self.repository.checkpoint_job(claim=claim, checkpoint=IDPCheckpoint.AMBIGUOUS, status=IDPJobStatus.FAILED)
            self._project_job_state(failed, source_key=record.canonical_source_key)
            return failed

    def fail_ocr(self, completion: Any) -> IDPJob | None:
        locator = Boto3DynamoOCRJobStore.locate_scope(self.table, completion.textract_job_id)
        if locator is None:
            raise IDPContractError("OCR completion locator is missing")
        store = Boto3DynamoOCRJobStore(self.table, tenant_id=locator[0], matter_id=locator[1])
        record = store.get_by_textract_job_id(completion.textract_job_id)
        if record is None:
            raise IDPContractError("OCR completion run is missing")
        job = self.repository.get_job(record.run_id)
        if job is None:
            raise IDPContractError("OCR completion job is missing")
        if job.status in {IDPJobStatus.COMPLETED, IDPJobStatus.REVIEW_REQUIRED, IDPJobStatus.FAILED, IDPJobStatus.SKIPPED}:
            return job
        claim = self.repository.claim_job(job_id=job.job_id, worker_id="ocr-callback", lease_seconds=self.config.claim_lease_seconds, allow_waiting_for_ocr=True)
        failed = self.repository.checkpoint_job(claim=claim, checkpoint=IDPCheckpoint.AMBIGUOUS, status=IDPJobStatus.FAILED)
        self._project_job_state(failed)
        return failed


@dataclass(frozen=True, slots=True)
class IDPRuntime:
    worker: IDPWorker | None
    processor: ProductionIDPProcessor


def build_runtime(*, require_worker: bool = True) -> IDPRuntime:
    """Build all AWS adapters only when an enabled Lambda invokes the handler."""

    import boto3
    from botocore.config import Config
    config = IDPProductionConfig.from_environment(require_work_queue=require_worker)
    sdk_config = Config(connect_timeout=5, read_timeout=config.read_timeout_seconds, retries={"total_max_attempts": 1, "mode": "standard"})
    resource = boto3.resource("dynamodb", region_name=config.region, config=sdk_config)
    table = resource.Table(config.table_name)
    s3 = boto3.client("s3", region_name=config.region, config=sdk_config)
    bedrock = boto3_idp_runtime_client(region=config.region, read_timeout=config.model_read_timeout_seconds)
    textract = boto3.client("textract", region_name=config.region, config=sdk_config)
    metadata = Boto3DynamoDocumentMetadataRepository(config.table_name, table=table, boto3_backed=True)
    reader = Boto3IDPDocumentReader(metadata, s3, bucket_name=config.source_bucket, max_bytes=config.max_bytes)
    repository = Boto3DynamoIDPRepository(config.table_name, table=table)
    artifacts = Boto3S3IDPArtifactStore(config.artifact_bucket, client=s3)
    model_config = IDPModelConfig(config.model_id)
    review_dispatcher = None
    if os.environ.get("LEGALDESK_IDP_REVIEW_ENABLED", "false").strip().lower() == "true":
        from ..gateway_interceptor import Boto3DynamoGatewayGrantRepository
        from .review import CognitoM2MTokenProvider, IDPMachineGatewayClient, dispatch_review_after_persist
        gateway_url = os.environ.get("LEGALDESK_IDP_GATEWAY_URL", "")
        token_endpoint = os.environ.get("LEGALDESK_IDP_TOKEN_ENDPOINT", "")
        client_id = os.environ.get("LEGALDESK_IDP_M2M_CLIENT_ID", "")
        secret_name = os.environ.get("LEGALDESK_IDP_M2M_SECRET_PARAMETER_NAME", "")
        review_scope = os.environ.get("LEGALDESK_IDP_REVIEW_SCOPE", "legaldesk-idp/review-create")
        if not all((gateway_url, token_endpoint, client_id, secret_name)):
            raise IDPRuntimeConfigurationError("IDP review Gateway configuration is required when review is enabled")
        ssm = boto3.client("ssm", region_name=config.region, config=sdk_config)
        token_provider = CognitoM2MTokenProvider(ssm_client=ssm, parameter_name=secret_name, token_endpoint=token_endpoint, client_id=client_id, scope=review_scope, timeout_seconds=15.0)
        gateway = IDPMachineGatewayClient(gateway_url=gateway_url, token_provider=token_provider, timeout_seconds=15.0)
        invocation_repository = Boto3DynamoGatewayGrantRepository(config.table_name, table=table)
        review_dispatcher = lambda *, run, job: dispatch_review_after_persist(run=run, job=job, invocation_repository=invocation_repository, gateway=gateway, machine_client_id=client_id, scope=review_scope, job_repository=repository)
    processor = ProductionIDPProcessor(repository=repository, reader=reader, artifacts=artifacts, model_config=model_config, bedrock=bedrock, textract=textract, config=config, dynamo_table=table, review_dispatcher=review_dispatcher)
    worker = IDPWorker(repository=repository, document_lookup=reader, config=IDPConfig(max_bytes=config.max_bytes, max_pages=config.max_pages, visibility_timeout_seconds=config.claim_lease_seconds, max_calls_per_run=config.max_calls_per_run, global_deadline_seconds=config.global_deadline_seconds), expected_source_arn=config.work_queue_arn, worker_id=os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "idp-worker"), processor=processor) if require_worker else None
    return IDPRuntime(worker=worker, processor=processor)


__all__ = ["Boto3IDPDocumentReader", "IDPProductionConfig", "IDPRuntime", "IDPRuntimeConfigurationError", "ProductionIDPProcessor", "build_runtime"]
