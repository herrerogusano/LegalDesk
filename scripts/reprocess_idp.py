"""Request one bounded IDP reprocessing generation for the beta synthetic scope.

This is an operator-only escape hatch for the Phase 14 smoke envelope.  The
default command is a local preflight and makes no AWS calls.  ``--execute``
requires the exact deployed beta tenant/matter and operator role allowlists,
the canonical synthetic fixture hash, and then uses the existing metadata,
IDP repository, and SQS adapters.  It never invokes Lambda, OCR, Bedrock, or
an HTTP/Gateway route and never accepts a caller-supplied generation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = (ROOT / "tests" / "fixtures" / "idp" / "pdfs").resolve()
MANIFEST_PATH = (ROOT / "tests" / "fixtures" / "idp" / "manifest.json").resolve()
MAX_BYTES = 20 * 1024 * 1024
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
TERMINAL_JOB_STATUSES = {"COMPLETED", "REVIEW_REQUIRED", "FAILED", "SKIPPED"}

sys.path.insert(0, str(ROOT / "backend" / "src"))

from legaldesk.documents import Boto3DynamoDocumentMetadataRepository, Boto3S3ObjectStorage  # noqa: E402
from legaldesk.domain.models import DocumentStatus, MalwareScanStatus  # noqa: E402
from legaldesk.idp.models import DocumentForIDP, IDPConfig, IDPContractError, IDPJob, IDPJobStatus  # noqa: E402
from legaldesk.idp.persistence import Boto3DynamoIDPRepository, IDPRepository  # noqa: E402
from legaldesk.idp_lambda import Boto3IDPSQSQueue  # noqa: E402
from legaldesk.idp.worker import IDPQueue, build_verified_clean_job, enqueue_verified_clean_job  # noqa: E402


class ReprocessError(ValueError):
    """Closed, operator-facing validation category."""


@dataclass(frozen=True, slots=True)
class Fixture:
    fixture_id: str
    path: Path
    sha256: str


def _safe_id(value: object, label: str) -> str:
    if not isinstance(value, str) or SAFE_ID.fullmatch(value.strip()) is None:
        raise ReprocessError(f"{label}_invalid")
    return value.strip()


def _sha(value: object, label: str) -> str:
    if not isinstance(value, str) or SHA256.fullmatch(value.lower()) is None:
        raise ReprocessError(f"{label}_invalid")
    return value.lower()


def load_fixture(fixture_id: str, *, manifest_path: Path = MANIFEST_PATH) -> Fixture:
    """Resolve only a repository-owned synthetic PDF and verify its bytes."""

    fixture_id = _safe_id(fixture_id, "fixture_id")
    if manifest_path.resolve() != MANIFEST_PATH:
        raise ReprocessError("manifest_path_must_be_canonical")
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReprocessError("fixture_manifest_unavailable") from exc
    fixtures = payload.get("fixtures") if isinstance(payload, Mapping) else None
    if not isinstance(fixtures, list):
        raise ReprocessError("fixture_manifest_invalid")
    record = next((item for item in fixtures if isinstance(item, Mapping) and item.get("id") == fixture_id), None)
    if record is None:
        raise ReprocessError("fixture_id_not_allowlisted")
    filename = record.get("filename")
    digest = record.get("sha256")
    if not isinstance(filename, str) or Path(filename).name != filename or not filename.lower().endswith(".pdf"):
        raise ReprocessError("fixture_manifest_invalid")
    digest = _sha(digest, "fixture_sha256")
    path = (FIXTURE_ROOT / filename).resolve()
    try:
        path.relative_to(FIXTURE_ROOT)
    except ValueError as exc:
        raise ReprocessError("fixture_path_out_of_scope") from exc
    if path.is_symlink() or not path.is_file():
        raise ReprocessError("fixture_file_unavailable")
    body = path.read_bytes()
    if len(body) > MAX_BYTES or hashlib.sha256(body).hexdigest() != digest:
        raise ReprocessError("fixture_hash_mismatch")
    return Fixture(fixture_id, path, digest)


def validate_operator_scope(*, tenant_id: str, matter_id: str, environ: Mapping[str, str]) -> None:
    tenant_id = _safe_id(tenant_id, "tenant_id")
    matter_id = _safe_id(matter_id, "matter_id")
    expected_tenant = environ.get("LEGALDESK_IDP_BETA_TENANT_ID", "").strip()
    expected_matter = environ.get("LEGALDESK_IDP_BETA_MATTER_ID", "").strip()
    if not expected_tenant or not expected_matter:
        raise ReprocessError("beta_scope_allowlist_missing")
    if tenant_id != expected_tenant or matter_id != expected_matter:
        raise ReprocessError("beta_scope_mismatch")
    configured_matters = tuple(item.strip() for item in environ.get("LEGALDESK_IDP_REVIEW_MATTER_IDS", "").split(",") if item.strip())
    if configured_matters and matter_id not in configured_matters:
        raise ReprocessError("matter_not_in_review_allowlist")


def validate_caller_identity(identity: Mapping[str, object], *, expected_arn: str) -> None:
    """Require the exact pre-approved operator role; never infer authority."""

    if not isinstance(expected_arn, str) or not expected_arn.strip():
        raise ReprocessError("operator_role_allowlist_missing")
    if identity.get("Arn") != expected_arn:
        raise ReprocessError("operator_role_mismatch")
    if not isinstance(identity.get("Account"), str) or not identity["Account"]:
        raise ReprocessError("operator_account_unavailable")


def _validate_current_job(repository: IDPRepository, document: Any, *, tenant_id: str, matter_id: str, document_id: str, digest: str) -> None:
    pointer_hash = getattr(document, "idp_document_sha256", None)
    if pointer_hash is not None and pointer_hash != digest:
        raise ReprocessError("current_pointer_hash_mismatch")
    run_id = getattr(document, "idp_run_id", None)
    public_status = getattr(document, "idp_status", None)
    if public_status in {"PENDING_IDP", "PROCESSING_IDP"}:
        raise ReprocessError("current_generation_not_terminal")
    if not isinstance(run_id, str) or not run_id:
        return
    current = repository.get_job(run_id)
    if current is not None:
        if (current.tenant_id, current.matter_id, current.document_id, current.document_sha256) != (tenant_id, matter_id, document_id, digest):
            raise ReprocessError("current_job_scope_or_hash_mismatch")
        if current.status.value not in TERMINAL_JOB_STATUSES:
            raise ReprocessError("current_generation_not_terminal")
    get_run = getattr(repository, "get_run", None)
    if callable(get_run):
        run = get_run(tenant_id=tenant_id, matter_id=matter_id, document_id=document_id, run_id=run_id)
        if run is not None:
            if run.document_sha256 != digest or run.source_key != getattr(document, "idp_source_key", None):
                raise ReprocessError("current_run_scope_or_source_mismatch")
            if run.status.value not in TERMINAL_JOB_STATUSES:
                raise ReprocessError("current_generation_not_terminal")


def request_reprocess(
    *,
    repository: IDPRepository,
    metadata: Any,
    storage: Any,
    queue: IDPQueue,
    tenant_id: str,
    matter_id: str,
    document_id: str,
    fixture: Fixture,
    model_id: str,
    prompt_version: str,
    schema_version: str = "1.0.0",
    config: IDPConfig | None = None,
    generation_factory: Callable[[], str] = lambda: str(uuid4()),
) -> IDPJob:
    """Validate one current clean source, create, and enqueue one generation.

    ``generation_factory`` is dependency injection for offline conditional
    tests only.  Production callers use the default server-generated UUID.
    """

    tenant_id, matter_id, document_id = (_safe_id(tenant_id, "tenant_id"), _safe_id(matter_id, "matter_id"), _safe_id(document_id, "document_id"))
    if not isinstance(fixture, Fixture):
        raise ReprocessError("fixture_invalid")
    document = metadata.get_for_scope(tenant_id=tenant_id, matter_id=matter_id, document_id=document_id)
    if document is None:
        raise ReprocessError("document_not_found_in_scope")
    if (document.tenant_id, document.matter_id, document.document_id) != (tenant_id, matter_id, document_id):
        raise ReprocessError("document_scope_mismatch")
    if document.media_type.lower() != "application/pdf":
        raise ReprocessError("document_not_pdf")
    if document.status not in {DocumentStatus.UPLOADED, DocumentStatus.PENDING_INGESTION, DocumentStatus.INDEXED} or document.malware_scan_status is not MalwareScanStatus.CLEAN:
        raise ReprocessError("document_not_verified_clean")
    source_key = document.s3_key
    if not isinstance(source_key, str) or not source_key.strip() or not isinstance(document.file_size_bytes, int) or isinstance(document.file_size_bytes, bool) or not 1 <= document.file_size_bytes <= MAX_BYTES:
        raise ReprocessError("document_source_metadata_invalid")
    _validate_current_job(repository, document, tenant_id=tenant_id, matter_id=matter_id, document_id=document_id, digest=fixture.sha256)
    try:
        head = storage.head_object(key=source_key)
        if not isinstance(head, Mapping) or head.get("ContentLength") != document.file_size_bytes:
            raise ReprocessError("canonical_size_changed")
        body = storage.read_object_bytes(key=source_key, max_bytes=MAX_BYTES)
    except ReprocessError:
        raise
    except Exception as exc:
        raise ReprocessError("canonical_source_unavailable") from exc
    if not isinstance(body, bytes) or len(body) != document.file_size_bytes or hashlib.sha256(body).hexdigest() != fixture.sha256:
        raise ReprocessError("canonical_source_hash_mismatch")
    generation = generation_factory()
    if not isinstance(generation, str) or not SAFE_ID.fullmatch(generation):
        raise ReprocessError("server_generation_invalid")
    bounded = config or IDPConfig()
    trusted = DocumentForIDP(
        tenant_id=tenant_id, matter_id=matter_id, document_id=document_id,
        media_type=document.media_type, file_size_bytes=document.file_size_bytes,
        malware_scan_clean=True, content_sha256=fixture.sha256, source_key=source_key,
    )
    job = build_verified_clean_job(document=trusted, schema_version=schema_version, model_id=model_id, prompt_version=prompt_version, generation=f"reprocess:{generation}", max_attempts=bounded.max_attempts)
    # The clean-intent row is the existing durable outbox seam.  A second
    # operator invocation must not create a fresh generation while a prior
    # dispatch is still pending/ambiguous: that could result in duplicate paid
    # stages.  An identical idempotency identity is a safe replay and returns
    # the existing conditional winner.
    list_intents = getattr(repository, "list_clean_intents", None)
    if callable(list_intents):
        try:
            intents = list_intents(tenant_id=tenant_id, matter_id=matter_id, limit=20)
        except Exception as exc:
            raise ReprocessError("existing_reprocess_state_unavailable") from exc
        for intent in intents:
            if (intent.document_id, intent.document_sha256, intent.model_id, intent.prompt_version) != (document_id, fixture.sha256, model_id, prompt_version):
                continue
            existing = repository.get_job(intent.job_id)
            if existing is None:
                raise ReprocessError("pending_reprocess_intent")
            if existing.idempotency_key == job.idempotency_key:
                return existing
            if existing.status.value not in TERMINAL_JOB_STATUSES:
                raise ReprocessError("reprocess_generation_already_active")
    record_intent = getattr(repository, "record_clean_intent", None)
    if callable(record_intent):
        record_intent(job=job)
    durable = repository.create_job(job)
    # A retry that presents the same idempotency identity returns the durable
    # winner and must not send a second message once delivery is terminal or
    # already in progress.
    if durable.status is IDPJobStatus.ENQUEUE_PENDING:
        durable = enqueue_verified_clean_job(repository=repository, queue=queue, job=durable)
    return durable


def _not_executed(reason: str) -> dict[str, object]:
    return {"result": "NOT_EXECUTED", "reason": reason, "awsCalls": 0}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--fixture-id", required=True)
    parser.add_argument("--document-id", required=True)
    parser.add_argument("--tenant-id", default=os.environ.get("LEGALDESK_IDP_BETA_TENANT_ID", ""))
    parser.add_argument("--matter-id", default=os.environ.get("LEGALDESK_IDP_BETA_MATTER_ID", ""))
    parser.add_argument("--table-name", default=os.environ.get("LEGALDESK_METADATA_TABLE_NAME", ""))
    parser.add_argument("--source-bucket", default=os.environ.get("LEGALDESK_SOURCE_BUCKET", ""))
    parser.add_argument("--queue-url", default=os.environ.get("LEGALDESK_IDP_QUEUE_URL", ""))
    parser.add_argument("--model-id", default=os.environ.get("LEGALDESK_IDP_MODEL_ID", ""))
    parser.add_argument("--prompt-version", default=os.environ.get("LEGALDESK_IDP_PROMPT_VERSION", "1.0.0"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        fixture = load_fixture(args.fixture_id)
        if not args.execute:
            print(json.dumps(_not_executed("explicit --execute is required; no AWS call was made"), sort_keys=True))
            return 0
        validate_operator_scope(tenant_id=args.tenant_id, matter_id=args.matter_id, environ=os.environ)
        if os.environ.get("LEGALDESK_IDP_ENABLED", "false").strip().lower() != "true":
            raise ReprocessError("idp_processing_disabled")
        expected_role = os.environ.get("LEGALDESK_IDP_OPERATOR_ROLE_ARN", "").strip()
        required = {"table_name": args.table_name, "source_bucket": args.source_bucket, "queue_url": args.queue_url, "model_id": args.model_id}
        if any(not isinstance(value, str) or not value.strip() for value in required.values()):
            raise ReprocessError("deployed_idp_configuration_missing")
        import boto3
        from botocore.config import Config
        sdk_config = Config(retries={"total_max_attempts": 1, "mode": "standard"}, connect_timeout=10, read_timeout=20)
        session = boto3.Session(region_name=os.environ.get("AWS_REGION", "eu-west-1"))
        validate_caller_identity(session.client("sts", config=sdk_config).get_caller_identity(), expected_arn=expected_role)
        table = session.resource("dynamodb", config=sdk_config).Table(args.table_name)
        metadata = Boto3DynamoDocumentMetadataRepository(args.table_name, table=table, boto3_backed=True)
        repository = Boto3DynamoIDPRepository(args.table_name, table=table)
        storage = Boto3S3ObjectStorage(args.source_bucket, client=session.client("s3", config=sdk_config))
        queue = Boto3IDPSQSQueue(session.client("sqs", config=sdk_config), queue_url=args.queue_url)
        generation_id = str(uuid4())
        job = request_reprocess(repository=repository, metadata=metadata, storage=storage, queue=queue, tenant_id=args.tenant_id, matter_id=args.matter_id, document_id=args.document_id, fixture=fixture, model_id=args.model_id, prompt_version=args.prompt_version, generation_factory=lambda: generation_id)
        print(json.dumps({"result": "PASS", "jobId": job.job_id, "generationId": f"reprocess:{generation_id}", "documentId": job.document_id, "documentSha256": job.document_sha256, "status": job.status.value}, sort_keys=True))
        return 0
    except ReprocessError as exc:
        print(json.dumps({"result": "BLOCKED", "reason": str(exc)}, sort_keys=True))
        return 2
    except Exception:
        print(json.dumps({"result": "BLOCKED", "reason": "reprocess_failed"}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["Fixture", "ReprocessError", "load_fixture", "validate_operator_scope", "validate_caller_identity", "request_reprocess", "main"]
