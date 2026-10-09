"""Scheduled, bounded reconciliation Lambda for the authenticated beta.

The schedule receives no browser-controlled selectors.  Its small list of
tenant/matter/subject partitions is deployment configuration, validated here
against the single approved beta tenant.  Upload and ingestion discovery use
bounded partition queries; Gateway expiry discovery uses the repository-owned
day-bucket index and point-read revalidation.  No DynamoDB scan is permitted.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from threading import Lock
from typing import Any, Mapping

from .documents import Boto3DynamoDocumentMetadataRepository, Boto3S3ObjectStorage
from .gateway_interceptor import Boto3DynamoGatewayGrantRepository
from .ingestion import AsyncKnowledgeBaseIngestionService
from .reconciliation import (
    IngestionReconciliationScope,
    ReconciliationScope,
    ReconciliationService,
    ReconciliationReport,
)
from .state import DynamoDBEphemeralStateStore
from .idp.models import IDPConfig, IDPJobStatus
from .idp.persistence import Boto3DynamoIDPRepository
from .idp.trigger import VerifiedCleanIDPTrigger
from .idp.providers import idp_prompt_identity
from .idp_lambda import Boto3IDPSQSQueue


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,127}$")
_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_MAX_SCOPES = 32
_MAX_LIMIT = 100
_logger = logging.getLogger(__name__)
_service: ReconciliationService | None = None
_service_lock = Lock()


class ReconciliationLambdaError(RuntimeError):
    """Closed boundary error; details never contain provider/config values."""


@dataclass(frozen=True, slots=True)
class ReconciliationConfig:
    region: str
    table_name: str
    source_bucket: str
    beta_tenant_id: str
    upload_scopes: tuple[ReconciliationScope, ...]
    ingestion_scopes: tuple[IngestionReconciliationScope, ...]
    upload_stale_seconds: float
    ingestion_stale_seconds: float
    limit_per_scope: int
    max_batch: int = _MAX_LIMIT
    idp_enabled: bool = False
    idp_recovery_limit_per_scope: int = 25

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> "ReconciliationConfig":
        values = dict(os.environ if environ is None else environ)

        def required(name: str, pattern: re.Pattern[str] | None = None) -> str:
            value = values.get(name, "").strip()
            if not value or (pattern is not None and pattern.fullmatch(value) is None):
                raise ReconciliationLambdaError("reconciliation configuration is invalid")
            return value

        def bounded_number(name: str, *, minimum: float, maximum: float) -> float:
            raw = values.get(name, "")
            try:
                value = float(raw)
            except (TypeError, ValueError):
                raise ReconciliationLambdaError("reconciliation configuration is invalid") from None
            if not minimum <= value <= maximum:
                raise ReconciliationLambdaError("reconciliation configuration is invalid")
            return value

        def bounded_int(name: str, *, minimum: int, maximum: int, default: int) -> int:
            raw = values.get(name, str(default))
            try:
                value = int(raw)
            except (TypeError, ValueError):
                raise ReconciliationLambdaError("reconciliation configuration is invalid") from None
            if not minimum <= value <= maximum:
                raise ReconciliationLambdaError("reconciliation configuration is invalid")
            return value

        beta_tenant = required("LEGALDESK_RECONCILIATION_BETA_TENANT_ID", _IDENTIFIER)
        upload_scopes = _parse_json_scopes(
            values.get("LEGALDESK_RECONCILIATION_UPLOAD_SCOPES", ""),
            expected_keys={"tenantId", "matterId"},
            beta_tenant=beta_tenant,
            ingestion=False,
        )
        ingestion_scopes = _parse_json_scopes(
            values.get("LEGALDESK_RECONCILIATION_INGESTION_SCOPES", ""),
            expected_keys={"subject", "tenantId", "matterId"},
            beta_tenant=beta_tenant,
            ingestion=True,
        )
        if not upload_scopes and not ingestion_scopes:
            raise ReconciliationLambdaError("reconciliation configuration is invalid")
        raw_idp = values.get("LEGALDESK_IDP_ENABLED", "false").strip().lower()
        if raw_idp not in {"true", "false"}:
            raise ReconciliationLambdaError("reconciliation configuration is invalid")
        idp_enabled = raw_idp == "true"
        return cls(
            region=required("AWS_REGION", re.compile(r"^[a-z0-9-]{1,32}$")),
            table_name=required("LEGALDESK_METADATA_TABLE_NAME"),
            source_bucket=required("LEGALDESK_SOURCE_BUCKET", _BUCKET),
            beta_tenant_id=beta_tenant,
            upload_scopes=upload_scopes,
            ingestion_scopes=ingestion_scopes,
            upload_stale_seconds=bounded_number(
                "LEGALDESK_RECONCILIATION_UPLOAD_STALE_SECONDS", minimum=60, maximum=7 * 24 * 60 * 60
            ),
            ingestion_stale_seconds=bounded_number(
                "LEGALDESK_RECONCILIATION_INGESTION_STALE_SECONDS", minimum=60, maximum=24 * 60 * 60
            ),
            limit_per_scope=bounded_int("LEGALDESK_RECONCILIATION_LIMIT_PER_SCOPE", minimum=1, maximum=_MAX_LIMIT, default=25),
            idp_enabled=idp_enabled,
            idp_recovery_limit_per_scope=bounded_int("LEGALDESK_IDP_RECOVERY_LIMIT_PER_SCOPE", minimum=1, maximum=_MAX_LIMIT, default=25),
        )


def _parse_json_scopes(
    raw: str,
    *,
    expected_keys: set[str],
    beta_tenant: str,
    ingestion: bool,
) -> tuple[ReconciliationScope, ...] | tuple[IngestionReconciliationScope, ...]:
    if not isinstance(raw, str) or not raw.strip():
        raise ReconciliationLambdaError("reconciliation configuration is invalid")
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        raise ReconciliationLambdaError("reconciliation configuration is invalid") from None
    if not isinstance(payload, list) or len(payload) > _MAX_SCOPES:
        raise ReconciliationLambdaError("reconciliation configuration is invalid")
    result: list[object] = []
    seen: set[tuple[str, ...]] = set()
    for item in payload:
        if not isinstance(item, Mapping) or set(item) != expected_keys:
            raise ReconciliationLambdaError("reconciliation configuration is invalid")
        values = {key: item.get(key).strip() if isinstance(item.get(key), str) else item.get(key) for key in expected_keys}
        if any(not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None for value in values.values()):
            raise ReconciliationLambdaError("reconciliation configuration is invalid")
        if values["tenantId"] != beta_tenant:
            raise ReconciliationLambdaError("reconciliation configuration is invalid")
        marker = tuple(str(values[key]) for key in sorted(expected_keys))
        if marker in seen:
            raise ReconciliationLambdaError("reconciliation configuration is invalid")
        seen.add(marker)
        if ingestion:
            result.append(
                IngestionReconciliationScope(
                    subject=values["subject"], tenant_id=values["tenantId"], matter_id=values["matterId"]
                )
            )
        else:
            result.append(ReconciliationScope(tenant_id=values["tenantId"], matter_id=values["matterId"]))
    return tuple(result)  # type: ignore[return-value]


def _build_service(config: ReconciliationConfig) -> ReconciliationService:
    try:
        import boto3
        from botocore.config import Config

        sdk_config = Config(retries={"total_max_attempts": 1, "mode": "standard"})
        table = boto3.resource("dynamodb", region_name=config.region, config=sdk_config).Table(config.table_name)
        s3 = boto3.client("s3", region_name=config.region, config=sdk_config)
        bedrock_agent = boto3.client("bedrock-agent", region_name=config.region, config=sdk_config)
        sqs = boto3.client("sqs", region_name=config.region, config=sdk_config) if config.idp_enabled else None
    except ReconciliationLambdaError:
        raise
    except Exception:
        raise ReconciliationLambdaError("reconciliation composition is unavailable") from None
    metadata = Boto3DynamoDocumentMetadataRepository(config.table_name, table=table, boto3_backed=True)
    storage = Boto3S3ObjectStorage(config.source_bucket, client=s3)
    state_store = DynamoDBEphemeralStateStore(config.table_name, table=table)
    ingestion = AsyncKnowledgeBaseIngestionService(
        client=bedrock_agent,
        object_verifier=storage,
        metadata_repository=metadata,
        state_store=state_store,
        knowledge_base_id=_required_env("LEGALDESK_KNOWLEDGE_BASE_ID"),
        data_source_id=_required_env("LEGALDESK_DATA_SOURCE_ID"),
    )
    idp_recovery = None
    if config.idp_enabled:
        queue_url = _required_env("LEGALDESK_IDP_QUEUE_URL")
        queue_arn = _required_env("LEGALDESK_IDP_QUEUE_ARN")
        model_id = _required_env("LEGALDESK_IDP_MODEL_ID")
        idp_repository = Boto3DynamoIDPRepository(config.table_name, table=table)
        review_recovery = None
        if os.environ.get("LEGALDESK_IDP_REVIEW_ENABLED", "false").strip().lower() == "true":
            from .idp.review import CognitoM2MTokenProvider, IDPMachineGatewayClient, dispatch_review_after_persist
            review_url = os.environ.get("LEGALDESK_IDP_GATEWAY_URL", "").strip()
            token_endpoint = os.environ.get("LEGALDESK_IDP_TOKEN_ENDPOINT", "").strip()
            client_id = os.environ.get("LEGALDESK_IDP_M2M_CLIENT_ID", "").strip()
            secret_name = os.environ.get("LEGALDESK_IDP_M2M_SECRET_PARAMETER_NAME", "").strip()
            review_scope = os.environ.get("LEGALDESK_IDP_REVIEW_SCOPE", "legaldesk-idp/review-create").strip()
            if not all((review_url, token_endpoint, client_id, secret_name)):
                raise ReconciliationLambdaError("review recovery configuration is invalid")
            ssm = boto3.client("ssm", region_name=config.region, config=sdk_config)
            token_provider = CognitoM2MTokenProvider(ssm_client=ssm, parameter_name=secret_name, token_endpoint=token_endpoint, client_id=client_id, scope=review_scope, timeout_seconds=15.0)
            gateway = IDPMachineGatewayClient(gateway_url=review_url, token_provider=token_provider, timeout_seconds=15.0)
            invocation_repository = Boto3DynamoGatewayGrantRepository(config.table_name, table=table)

            def recover_review_dispatch(*, tenant_id: str, matter_id: str, limit: int, _page: tuple[tuple[object, ...], str | None] | None = None) -> tuple[object, ...]:
                recovered: list[object] = []
                cursor = idp_repository.get_review_recovery_cursor(tenant_id=tenant_id, matter_id=matter_id)
                if _page is not None:
                    documents, next_cursor = _page
                elif callable(getattr(metadata, "list_for_scope_page", None)):
                    documents, next_cursor = metadata.list_for_scope_page(tenant_id=tenant_id, matter_id=matter_id, limit=min(limit, 100), cursor=cursor)
                else:
                    documents, next_cursor = metadata.list_for_scope(tenant_id=tenant_id, matter_id=matter_id, limit=min(limit, 100)), None
                try:
                    for document in documents:
                        if len(recovered) >= limit:
                            break
                        # Review recovery must inspect the chronological
                        # history page, not an arbitrary UUID-key prefix
                        # limited to the first two runs.  The repository
                        # marker cursor keeps this bounded and recoverable;
                        # pending runs in the same document are therefore not
                        # starved behind newer terminal rows.
                        # Dispatch work remains capped by ``limit``; history
                        # inspection has its own small bounded page so a
                        # pending older run cannot be hidden behind several
                        # already-SENT generations of the same document.
                        runs = idp_repository.list_run_history(
                            tenant_id=tenant_id, matter_id=matter_id,
                            document_id=document.document_id, limit=20,
                        ).runs
                        for run in runs:
                            if run.status is not IDPJobStatus.REVIEW_REQUIRED or len(recovered) >= limit:
                                continue
                            job = idp_repository.get_job(run.run_id)
                            # A run ID normally equals the producer job ID,
                            # but the durable contracts do not require that
                            # identity.  Resolve the review job by the same
                            # scoped document/hash when a restart finds a
                            # separately named job; this remains a bounded
                            # partition query, never a table scan.
                            if job is None:
                                list_jobs = getattr(idp_repository, "list_review_jobs", None)
                                candidates = list_jobs(
                                    tenant_id=tenant_id,
                                    matter_id=matter_id,
                                    document_id=document.document_id,
                                    limit=20,
                                ) if callable(list_jobs) else ()
                                matching = tuple(
                                    candidate for candidate in candidates
                                    if candidate.document_sha256 == run.document_sha256
                                    and candidate.created_at == run.created_at
                                )
                                job = matching[0] if len(matching) == 1 else None
                            if job is None or job.review_delivery_state == "SENT":
                                continue
                            try:
                                dispatch_review_after_persist(run=run, job=job, invocation_repository=invocation_repository, gateway=gateway, machine_client_id=client_id, scope=review_scope, job_repository=idp_repository)
                                recovered.append(job)
                            except Exception:
                                # Leave the run REVIEW_REQUIRED; the next bounded
                                # schedule retries the Gateway capability only.
                                continue
                finally:
                    # Advance past terminal/non-review rows even when one
                    # malformed candidate fails; no page can starve later docs.
                    idp_repository.set_review_recovery_cursor(tenant_id=tenant_id, matter_id=matter_id, cursor=next_cursor)
                return tuple(recovered)

            review_recovery = recover_review_dispatch
        idp_config = IDPConfig.from_mapping({
            "max_bytes": os.environ.get("LEGALDESK_IDP_MAX_BYTES", str(20 * 1024 * 1024)),
            "max_pages": os.environ.get("LEGALDESK_IDP_MAX_PAGES", "100"),
            "visibility_timeout_seconds": os.environ.get("LEGALDESK_IDP_CLAIM_LEASE_SECONDS", "480"),
            "max_attempts": os.environ.get("LEGALDESK_IDP_MAX_ATTEMPTS", "3"),
            "max_calls_per_run": os.environ.get("LEGALDESK_IDP_MAX_CALLS_PER_RUN", "2"),
            "global_deadline_seconds": os.environ.get("LEGALDESK_IDP_GLOBAL_DEADLINE_SECONDS", "330"),
        })
        trigger = VerifiedCleanIDPTrigger(
            repository=idp_repository,
            queue=Boto3IDPSQSQueue(sqs, queue_url=queue_url),
            config=idp_config,
            enabled=True,
            model_id=model_id,
            prompt_version=idp_prompt_identity(os.environ.get("LEGALDESK_IDP_PROMPT_VERSION", "1.0.0")),
            review_recovery=review_recovery,
        )
        if not queue_arn.startswith("arn:"):
            raise ReconciliationLambdaError("reconciliation configuration is invalid")
        idp_recovery = lambda **scope: trigger.recover_delivery(
            **scope,
            metadata_repository=metadata,
            object_storage=storage,
        )
    return ReconciliationService(
        metadata_repository=metadata,
        object_storage=storage,
        state_store=state_store,
        ingestion_service=ingestion,
        gateway_repository=Boto3DynamoGatewayGrantRepository(config.table_name, table=table),
        idp_recovery=idp_recovery,
        max_batch=config.max_batch,
    )


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value or _IDENTIFIER.fullmatch(value) is None:
        raise ReconciliationLambdaError("reconciliation configuration is invalid")
    return value


def _report(report: ReconciliationReport) -> dict[str, int]:
    return {
        "examined": report.examined,
        "changed": report.changed,
        "skipped": report.skipped,
        "ambiguous": report.ambiguous,
        "failed": report.failed,
    }


def _get_service(config: ReconciliationConfig) -> ReconciliationService:
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                _service = _build_service(config)
    return _service


def lambda_handler(event: Mapping[str, object], _lambda_context: object) -> dict[str, object]:
    """Run one bounded scheduled pass and return only aggregate evidence."""

    if not isinstance(event, Mapping):
        raise ReconciliationLambdaError("reconciliation event is invalid")
    try:
        config = ReconciliationConfig.from_environment()
        service = _get_service(config)
        upload = service.reconcile_pending_uploads(
            scopes=config.upload_scopes,
            stale_after_seconds=config.upload_stale_seconds,
            limit_per_scope=config.limit_per_scope,
        ) if config.upload_scopes else ReconciliationReport()
        ingestion = service.reconcile_ingestion_scopes(
            scopes=config.ingestion_scopes,
            stale_after_seconds=config.ingestion_stale_seconds,
            limit_per_scope=config.limit_per_scope,
        ) if config.ingestion_scopes else ReconciliationReport()
        allowed_matters = tuple(sorted({
            *(scope.matter_id for scope in config.upload_scopes),
            *(scope.matter_id for scope in config.ingestion_scopes),
        }))
        gateway = service.reconcile_gateway_indexed(
            limit=config.limit_per_scope,
            allowed_matter_ids=allowed_matters,
        )
    except ReconciliationLambdaError:
        _logger.error("reconciliation_unavailable", extra={"error_code": "configuration_or_composition"})
        raise
    except Exception:
        _logger.error("reconciliation_failed", extra={"error_code": "processing_failed"})
        raise ReconciliationLambdaError("reconciliation failed") from None
    result = {"status": "completed", "uploads": _report(upload), "ingestion": _report(ingestion), "gateway": _report(gateway)}
    if config.idp_enabled:
        recover = getattr(service, "reconcile_idp_scopes", None)
        if not callable(recover):
            raise ReconciliationLambdaError("IDP reconciliation composition is unavailable")
        idp = recover(scopes=config.upload_scopes, limit_per_scope=config.idp_recovery_limit_per_scope)
        result["idp"] = _report(idp)
    _logger.info(
        "reconciliation_completed",
        extra={"upload_changed": upload.changed, "ingestion_changed": ingestion.changed, "gateway_changed": gateway.changed},
    )
    return result


def reset_service_for_tests() -> None:
    global _service
    with _service_lock:
        _service = None


__all__ = ["ReconciliationConfig", "ReconciliationLambdaError", "lambda_handler", "reset_service_for_tests"]
