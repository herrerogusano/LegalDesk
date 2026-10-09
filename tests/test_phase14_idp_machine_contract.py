"""Black-box machine-IDP review Gateway contracts.

The request interceptor and Review Lambda are real production handlers.  Only
the IDP locator and Dynamo transport are in-memory seams; no human identity or
fallback answer is supplied.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import sys
import time
import types
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "backend" / "src"))

from legaldesk.gateway_interceptor import (  # noqa: E402
    IDP_REVIEW_SCOPE,
    IDP_REVIEW_PURPOSE,
    IDP_REVIEW_CREATE_TOOL,
    IDPReviewInvocationRecord,
    InMemoryGatewayGrantRepository,
    gateway_request_interceptor,
)
from legaldesk.idp.models import (  # noqa: E402
    FieldAcceptance,
    FieldOrigin,
    FieldPresence,
    IDPCheckpoint,
    IDPFieldResult,
    IDPJob,
    IDPConfig,
    IDPJobStatus,
    IDPExtractionRun,
    DocumentType,
)
from legaldesk.domain.models import Document, DocumentStatus, MalwareScanStatus  # noqa: E402
from legaldesk.domain.models import Matter, MatterStatus, User  # noqa: E402
from legaldesk.authorization import InMemoryAuthorizationStore  # noqa: E402
from legaldesk.review_tasks import gateway_lambda_handler  # noqa: E402
from legaldesk.review_tasks import Boto3DynamoReviewTaskRepository  # noqa: E402
from legaldesk.idp.review import IDPMachineGatewayClient, IDPReviewError, dispatch_review_after_persist  # noqa: E402
from legaldesk.idp.persistence import InMemoryIDPRepository  # noqa: E402
from legaldesk.idp.trigger import VerifiedCleanIDPTrigger  # noqa: E402
from legaldesk.reconciliation import ReconciliationScope  # noqa: E402
from legaldesk.reconciliation_lambda import ReconciliationConfig, _build_service  # noqa: E402


MACHINE_CLIENT = "synthetic-idp-machine"
MATTER = "matter-a"
ALLOWED_MATTERS = "matter-a,matter-b"
DOCUMENT = "document-a"
RUN = "run-a"
SOURCE_BYTES = b"synthetic canonical legal document bytes\n"
DIGEST = hashlib.sha256(SOURCE_BYTES).hexdigest()
CORRELATION = "11111111-1111-4111-8111-111111111111"
INVOCATION = "22222222-2222-4222-8222-222222222222"


def _segment(value: object) -> str:
    return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode().rstrip("=")


def _machine_token(*, client: str = MACHINE_CLIENT, scope: str = IDP_REVIEW_SCOPE, token_use: str = "access", subject: str | None = None) -> str:
    claims: dict[str, object] = {"client_id": client, "token_use": token_use, "scope": scope}
    if subject is not None:
        claims["sub"] = subject
    return f"{_segment({'alg': 'none'})}.{_segment(claims)}.unsigned"


class _ReviewTable:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, object]] = {}
        self.put_calls = 0

    def put_item(self, *, Item: dict[str, object], ConditionExpression: str | None = None, **_: object) -> None:
        self.put_calls += 1
        key = (str(Item["pk"]), str(Item["sk"]))
        if ConditionExpression and key in self.items:
            raise RuntimeError("conditional put failed")
        self.items[key] = dict(Item)

    def get_item(self, *, Key: dict[str, str], **_: object) -> dict[str, object]:
        item = self.items.get((Key["pk"], Key["sk"]))
        return {"Item": dict(item)} if item is not None else {}


class _IDPLocator:
    """Durable job/run lookup seam used by the real Review Lambda branch."""

    def __init__(self) -> None:
        when = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
        self.job = IDPJob(
            job_id=RUN, tenant_id="tenant-a", matter_id=MATTER, document_id=DOCUMENT,
            document_sha256=DIGEST, idempotency_key="idempotency-run-a", correlation_id=CORRELATION,
            schema_version="1.0.0", model_id="model-a", prompt_version="prompt-a",
            status=IDPJobStatus.REVIEW_REQUIRED, checkpoint=IDPCheckpoint.DONE, created_at=when, updated_at=when,
        )
        self.run = IDPExtractionRun(
            run_id=RUN, tenant_id="tenant-a", matter_id=MATTER, document_id=DOCUMENT,
            document_sha256=DIGEST, document_type=DocumentType.CONTRACT, schema_version="1.0.0",
            model_id="model-a", prompt_version="prompt-a", status=IDPJobStatus.REVIEW_REQUIRED,
            source_key="canonical.pdf", fields={
                "effective_date": IDPFieldResult(
                    field="effective_date", value="2024-01-31", presence=FieldPresence.PRESENT,
                    origin=FieldOrigin.LITERAL, acceptance=FieldAcceptance.REVIEW_REQUIRED,
                    schema_version="1.0.0",
                )
            }, created_at=when,
        )

    def get_job(self, run_id: str):
        return self.job if self.job is not None and run_id == RUN else None

    def get_run(self, *, tenant_id: str, matter_id: str, document_id: str, run_id: str):
        expected_tenant = self.job.tenant_id if self.job is not None else "tenant-a"
        if self.run is None or (tenant_id, matter_id, document_id, run_id) != (expected_tenant, MATTER, DOCUMENT, RUN):
            return None
        return self.run


class _Metadata:
    def __init__(self) -> None:
        self.document = Document(
            document_id=DOCUMENT, matter_id=MATTER, tenant_id="tenant-a",
            name="canonical.pdf", s3_key="canonical.pdf", media_type="application/pdf",
            jurisdiction="synthetic", document_date="2024-01-01", confidentiality="internal",
            status=DocumentStatus.UPLOADED, file_size_bytes=len(SOURCE_BYTES),
            malware_scan_status=MalwareScanStatus.CLEAN,
        )

    @property
    def s3_key(self):
        return self.document.s3_key

    def get_for_scope(self, *, tenant_id: str, matter_id: str, document_id: str):
        return self.document if (tenant_id, matter_id, document_id) == ("tenant-a", MATTER, DOCUMENT) else None


class _S3:
    def __init__(self) -> None:
        self.body = SOURCE_BYTES
        self.metadata: dict[str, str] = {}

    def head_object(self, *, Bucket: str, Key: str):
        return {"ContentLength": len(self.body), "Metadata": dict(self.metadata)}

    def get_object(self, *, Bucket: str, Key: str, Range: str):
        return {"Body": io.BytesIO(self.body)}


def _event(*, matter: str = MATTER, invocation_id: str = INVOCATION, tool: str = "review-task-lambda___create_review_task", token: str | None = None, extra: dict[str, object] | None = None) -> dict[str, object]:
    arguments: dict[str, object] = {
        "matterId": matter, "invocationId": invocation_id,
    }
    if extra:
        arguments.update(extra)
    body = {"jsonrpc": "2.0", "id": "machine-1", "method": "tools/call", "params": {"name": tool, "arguments": arguments}}
    return {
        "mcp": {
            "gatewayRequest": {"headers": {"Authorization": f"Bearer {token or _machine_token()}", "x-legaldesk-requested-matter-id": matter}, "body": body},
            "rawGatewayRequest": {"body": json.dumps(body)},
        }
    }


class MachineReviewContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.grants = InMemoryGatewayGrantRepository()
        self.locator = _IDPLocator()
        self.review_table = _ReviewTable()
        self.review_repository = Boto3DynamoReviewTaskRepository("synthetic-review", table=self.review_table)
        self.s3 = _S3()
        self.grants.put_idp_review_invocation(IDPReviewInvocationRecord(
            invocation_id=INVOCATION, machine_client_id=MACHINE_CLIENT,
            scope=IDP_REVIEW_SCOPE, purpose=IDP_REVIEW_PURPOSE,
            tool_name=IDP_REVIEW_CREATE_TOOL, requested_matter_id=MATTER,
            document_id=DOCUMENT, run_id=RUN, document_sha256=DIGEST,
            correlation_id=CORRELATION, field_names=("effective_date",),
            expires_at=int(time.time()) + 300,
        ))

    def _patches(self, *, real_resolver: bool = False):
        import legaldesk.gateway_interceptor as gateway_module
        resolver_patch = patch(
            "legaldesk.gateway_interceptor._idp_review_invocation_resolver",
            wraps=gateway_module._idp_review_invocation_resolver,
        ) if real_resolver else patch("legaldesk.gateway_interceptor._idp_review_invocation_resolver", side_effect=lambda record, client: {
            "tenant_id": "tenant-a", "matter_id": MATTER, "document_id": DOCUMENT, "run_id": RUN,
            "document_sha256": DIGEST, "correlation_id": CORRELATION, "field_names": ("effective_date",),
        } if (record.get("runId"), record.get("requestedMatterId"), client) == (RUN, MATTER, MACHINE_CLIENT) else (_ for _ in ()).throw(PermissionError("scope denied")))
        return (
            patch.dict("os.environ", {
                "LEGALDESK_IDP_M2M_CLIENT_ID": MACHINE_CLIENT,
                "LEGALDESK_IDP_REVIEW_SCOPE": IDP_REVIEW_SCOPE,
                # The table name is only a transport seam.  Authorization is
                # deployment-owned and must be explicit even in synthetic
                # tests; the production code must not infer scope from a
                # synthetic table name.
                "LEGALDESK_IDP_REVIEW_TENANT_ID": "tenant-a",
                "LEGALDESK_IDP_REVIEW_MATTER_IDS": ALLOWED_MATTERS,
                "IDP_TABLE_NAME": "synthetic-idp",
            }),
            patch("legaldesk.gateway_interceptor._grant_repository_from_environment", return_value=self.grants),
            resolver_patch,
            patch("legaldesk.review_tasks._gateway_grant_repository_from_environment", return_value=self.grants),
            patch("legaldesk.review_tasks._repositories_from_environment", return_value=(self.review_repository, None)),
            patch("legaldesk.idp.persistence.Boto3DynamoIDPRepository", return_value=self.locator),
            patch("legaldesk.documents.Boto3DynamoDocumentMetadataRepository", return_value=_Metadata()),
            patch("boto3.client", return_value=self.s3, create=True),
        )

    def _invoke(self, event: dict[str, object], *, real_resolver: bool = False, scope_overrides: dict[str, str] | None = None):
        patches = self._patches(real_resolver=real_resolver)
        for item in patches:
            item.start()
        active = list(patches)
        if scope_overrides is not None:
            override = patch.dict("os.environ", scope_overrides)
            override.start()
            active.append(override)
        self.addCleanup(lambda: [item.stop() for item in reversed(active)])
        transformed = gateway_request_interceptor(event, None)
        return transformed

    @staticmethod
    def _grant_id(transformed: dict[str, object]) -> str:
        # A valid M2M call must reach the dedicated transformed request.  Keep
        # this assertion explicit so a regression is reported at the gateway
        # boundary instead of becoming an unrelated KeyError in the target.
        if "transformedGatewayRequest" not in transformed.get("mcp", {}):
            raise AssertionError(f"valid machine request denied by gateway: {transformed!r}")
        return str(transformed["mcp"]["transformedGatewayRequest"]["body"]["params"]["arguments"]["_legaldeskGrantId"])

    def test_valid_machine_grant_reaches_real_review_repository_with_exact_actor_and_correlation(self) -> None:
        transformed = self._invoke(_event())
        grant_id = self._grant_id(transformed)
        self.assertEqual(
            set(transformed["mcp"]["transformedGatewayRequest"]["body"]["params"]["arguments"]),
            {"_legaldeskGrantId"},
        )
        with self._patches()[0], self._patches()[3], self._patches()[4], self._patches()[5], self._patches()[6], self._patches()[7]:
            result = gateway_lambda_handler({"_legaldeskGrantId": grant_id}, None)
        self.assertEqual(result.get("runId"), RUN, result)
        self.assertEqual(result.get("correlationId"), CORRELATION, result)
        self.assertEqual(len(self.review_table.items), 1)
        task = next(iter(self.review_table.items.values()))
        self.assertEqual(task["createdByUserId"], "service:idp-review")
        self.assertEqual(task["correlationId"], CORRELATION)

    def test_forged_and_expired_machine_grants_are_denied(self) -> None:
        transformed = self._invoke(_event())
        grant_id = self._grant_id(transformed)
        self.grants.idp_reviews[grant_id] = {**self.grants.idp_reviews[grant_id], "unexpected": "forged"}
        with self._patches()[0], self._patches()[3], self._patches()[4], self._patches()[5], self._patches()[6], self._patches()[7]:
            self.assertEqual(gateway_lambda_handler({"_legaldeskGrantId": grant_id}, None)["error"], "access_denied")
        self.grants.idp_reviews[grant_id] = {key: value for key, value in self.grants.idp_reviews[grant_id].items() if key != "unexpected"}
        self.grants.idp_reviews[grant_id]["expiresAt"] = int(time.time()) - 1
        with self._patches()[0], self._patches()[3], self._patches()[4], self._patches()[5], self._patches()[6], self._patches()[7]:
            self.assertEqual(gateway_lambda_handler({"_legaldeskGrantId": grant_id}, None)["error"], "access_denied")

    def test_machine_wrong_client_scope_tool_and_extra_body_are_denied_before_target(self) -> None:
        cases = (
            _event(token=_machine_token(client="wrong-client")),
            _event(token=_machine_token(scope="wrong/scope")),
            _event(tool="review-task-lambda___list_review_tasks"),
            _event(extra={"unexpected": "caller-data"}),
        )
        for candidate in cases:
            with self.subTest(candidate=candidate["mcp"]["gatewayRequest"]["body"]):
                transformed = self._invoke(candidate)
                self.assertEqual(transformed["mcp"]["transformedGatewayResponse"]["statusCode"], 403)
                self.assertEqual(len(self.grants.idp_reviews), 0)
                self.assertEqual(len(self.review_table.items), 0)

    def test_machine_claim_mismatch_cannot_fall_through_to_authorized_human(self) -> None:
        """M2M client identity is a strict boundary, not a human fallback."""
        human_store = InMemoryAuthorizationStore(
            users_by_subject={
                "human-sub": User(
                    user_id="human-user", verified_subject="human-sub",
                    tenant_ids=frozenset({"tenant-a"}), roles=frozenset({"attorney"}),
                )
            },
            matters_by_id={
                MATTER: Matter(
                    matter_id=MATTER, tenant_id="tenant-a", name="Synthetic matter",
                    authorized_user_ids=frozenset({"human-user"}), status=MatterStatus.ACTIVE,
                )
            },
        )
        cases = (
            _event(token=_machine_token(scope="legaldesk/use", subject="human-sub")),
            _event(token=_machine_token(token_use="id", subject="human-sub")),
        )
        for candidate in cases:
            with self.subTest(token=candidate["mcp"]["gatewayRequest"]["headers"]["Authorization"]):
                with patch("legaldesk.gateway_interceptor._authorization_store_from_environment", return_value=human_store):
                    transformed = self._invoke(candidate)
                self.assertEqual(transformed["mcp"]["transformedGatewayResponse"]["statusCode"], 403)
                self.assertEqual(len(self.grants.grants), 0)
                self.assertEqual(len(self.grants.idp_reviews), 0)

    def test_cross_matter_and_changed_or_deleted_scope_are_denied_before_target(self) -> None:
        candidate = _event(matter="matter-other")
        transformed = self._invoke(candidate)
        self.assertEqual(transformed["mcp"]["transformedGatewayResponse"]["statusCode"], 403)
        self.assertEqual(len(self.grants.idp_reviews), 0)
        patches = self._patches()
        for item in patches:
            item.start()
        self.addCleanup(lambda: [item.stop() for item in reversed(patches)])
        with patch("legaldesk.gateway_interceptor._idp_review_invocation_resolver", side_effect=PermissionError("document changed or deleted")):
            transformed = gateway_request_interceptor(_event(), None)
        self.assertEqual(transformed["mcp"]["transformedGatewayResponse"]["statusCode"], 403)
        self.assertEqual(len(self.grants.idp_reviews), 0)

    def test_target_rechecks_durable_run_and_duplicate_grant_is_idempotent(self) -> None:
        transformed = self._invoke(_event())
        grant_id = self._grant_id(transformed)
        target_event = {"_legaldeskGrantId": grant_id}
        with self._patches()[0], self._patches()[3], self._patches()[4], self._patches()[5], self._patches()[6], self._patches()[7]:
            first = gateway_lambda_handler(target_event, None)
            second = gateway_lambda_handler(target_event, None)
        self.assertEqual(first, second)
        self.assertEqual(len(self.review_table.items), 1)
        self.locator.run = replace(self.locator.run, document_sha256="b" * 64)
        with self._patches()[0], self._patches()[3], self._patches()[4], self._patches()[5], self._patches()[6], self._patches()[7]:
            changed = gateway_lambda_handler(target_event, None)
        self.assertEqual(changed["error"], "access_denied")
        self.locator.run = None
        with self._patches()[0], self._patches()[3], self._patches()[4], self._patches()[5]:
            denied = gateway_lambda_handler(target_event, None)
        self.assertEqual(denied["error"], "access_denied")

    def test_dispatch_replay_after_lost_response_creates_one_task_and_marks_latest_invocation_sent(self) -> None:
        class _Dispatch:
            def __init__(inner) -> None:
                inner.calls = 0

            def dispatch(inner, record):
                inner.calls += 1
                transformed = gateway_request_interceptor(_event(invocation_id=record.invocation_id), None)
                grant_id = self._grant_id(transformed)
                result = gateway_lambda_handler({"_legaldeskGrantId": grant_id}, None)
                if inner.calls == 1:
                    # The target has committed the task, but the caller lost
                    # the response.  The dispatch layer must make this
                    # recoverable rather than creating a second task.
                    raise TimeoutError("response lost after target commit")
                return result

        dispatch = _Dispatch()
        patches = self._patches()
        for item in patches:
            item.start()
        self.addCleanup(lambda: [item.stop() for item in reversed(patches)])
        with self.assertRaises(TimeoutError):
            dispatch_review_after_persist(
                run=self.locator.run,
                job=self.locator.job,
                invocation_repository=self.grants,
                gateway=dispatch,
                machine_client_id=MACHINE_CLIENT,
            )
        scheduled_callbacks = []
        scheduled_repository = InMemoryIDPRepository()
        scheduled_repository.create_job(self.locator.job)

        def recover_review(*, tenant_id: str, matter_id: str, limit: int):
            scheduled_callbacks.append((tenant_id, matter_id, limit))
            return (dispatch_review_after_persist(
                run=self.locator.run,
                job=self.locator.job,
                invocation_repository=self.grants,
                gateway=dispatch,
                machine_client_id=MACHINE_CLIENT,
            ),)

        trigger = VerifiedCleanIDPTrigger(
            repository=scheduled_repository,
            queue=object(),
            config=IDPConfig(),
            enabled=True,
            review_recovery=recover_review,
        )
        trigger.recover_delivery(tenant_id="tenant-a", matter_id=MATTER, limit=1)
        self.assertEqual(dispatch.calls, 2)
        self.assertEqual(scheduled_callbacks, [("tenant-a", MATTER, 1)])
        self.assertEqual(len(self.review_table.items), 1)
        states = [item.get("deliveryState") for item in self.grants.idp_invocations.values()]
        self.assertEqual(states.count("AMBIGUOUS"), 1)
        self.assertEqual(states.count("SENT"), 1)

    def test_actual_reconciliation_composition_recovers_terminal_review_once_across_bounded_pages(self) -> None:
        from legaldesk.gateway_client import GatewayHttpResponse

        class _PagedMetadata(_Metadata):
            def __init__(inner) -> None:
                super().__init__()
                inner.calls = 0
                inner.review_calls = []
                inner.completed = replace(inner.document, document_id="completed-doc", status=DocumentStatus.FAILED)
                inner.review_pages = ((inner.completed,), (inner.document,))

            def list_for_scope(inner, *, tenant_id: str, matter_id: str, limit: int):
                inner.calls += 1
                return (inner.completed,)

            def list_for_scope_page(inner, *, tenant_id: str, matter_id: str, limit: int, cursor: str | None = None):
                inner.review_calls.append(cursor)
                index = int(cursor) if cursor is not None else 0
                page = inner.review_pages[index] if index < len(inner.review_pages) else ()
                next_cursor = str(index + 1) if index + 1 < len(inner.review_pages) else None
                return page[:limit], next_cursor

        class _Storage:
            @staticmethod
            def head_object(*, key: str):
                return {"ContentLength": 0, "Metadata": {}}

        class _Token:
            @staticmethod
            def token() -> str:
                return _machine_token()

        class _HTTP:
            def __init__(inner) -> None:
                inner.calls = 0
                inner.fail_once = True

            def post(inner, url, body, headers, timeout):
                inner.calls += 1
                request = json.loads(body.decode("utf-8"))
                arguments = request["params"]["arguments"]
                event = _event(invocation_id=arguments["invocationId"], token=headers["Authorization"])
                transformed = gateway_request_interceptor(event, None)
                if "transformedGatewayRequest" not in transformed.get("mcp", {}):
                    error = transformed["mcp"]["transformedGatewayResponse"]["body"]["error"]
                    payload = {"jsonrpc": "2.0", "id": request["id"], "error": error}
                else:
                    grant_id = self._grant_id(transformed)
                    result = gateway_lambda_handler({"_legaldeskGrantId": grant_id}, None)
                    if "error" in result:
                        payload = {"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32001, "message": "denied"}}
                    else:
                        payload = {"jsonrpc": "2.0", "id": request["id"], "result": result}
                if inner.fail_once:
                    inner.fail_once = False
                    raise TimeoutError("response lost after review task commit")
                return GatewayHttpResponse(200, "application/json", json.dumps(payload).encode("utf-8"))

        metadata = _PagedMetadata()
        idp_repository = InMemoryIDPRepository()
        idp_repository.create_job(self.locator.job)
        idp_repository.save_run(self.locator.run)
        storage = _Storage()
        sdk_table = self.review_table
        sdk_resource = type("_Resource", (), {"Table": lambda _self, _name: sdk_table})()
        http = _HTTP()
        # The bundled offline runtime exposes boto3 as a namespace without
        # botocore.config.  Inject only that narrow import seam; replacing the
        # whole botocore package breaks boto3's own imports in a fresh process.
        fake_botocore_config = types.ModuleType("botocore.config")
        fake_botocore_config.Config = lambda **_kwargs: object()
        scope = ReconciliationScope("tenant-a", MATTER)
        config = ReconciliationConfig(
            region="eu-west-1", table_name="synthetic-table", source_bucket="synthetic-source",
            beta_tenant_id="tenant-a", upload_scopes=(scope,), ingestion_scopes=(),
            upload_stale_seconds=3600, ingestion_stale_seconds=3600, limit_per_scope=1,
            idp_enabled=True, idp_recovery_limit_per_scope=1,
        )

        def sdk_client(name, **_kwargs):
            return self.s3 if name == "s3" else object()

        with patch.dict("os.environ", {
            "AWS_REGION": "eu-west-1", "LEGALDESK_METADATA_TABLE_NAME": "synthetic-table", "LEGALDESK_SOURCE_BUCKET": "synthetic-source",
            "LEGALDESK_IDP_ENABLED": "true",
            "LEGALDESK_IDP_REVIEW_ENABLED": "true", "LEGALDESK_IDP_GATEWAY_URL": "https://gateway.example.com",
            "LEGALDESK_IDP_TOKEN_ENDPOINT": "https://token.example.com", "LEGALDESK_IDP_M2M_CLIENT_ID": MACHINE_CLIENT,
            "LEGALDESK_IDP_M2M_SECRET_PARAMETER_NAME": "synthetic-secret", "LEGALDESK_IDP_REVIEW_SCOPE": IDP_REVIEW_SCOPE,
            "LEGALDESK_IDP_REVIEW_TENANT_ID": "tenant-a", "LEGALDESK_IDP_REVIEW_MATTER_IDS": ALLOWED_MATTERS,
            "LEGALDESK_IDP_QUEUE_URL": "https://sqs.example.com/queue", "LEGALDESK_IDP_QUEUE_ARN": "arn:aws:sqs:eu-west-1:111122223333:idp",
            "LEGALDESK_IDP_MODEL_ID": "synthetic-model", "LEGALDESK_IDP_PROMPT_VERSION": "1.0.0",
            "LEGALDESK_KNOWLEDGE_BASE_ID": "synthetic-kb", "LEGALDESK_DATA_SOURCE_ID": "synthetic-ds",
            "IDP_TABLE_NAME": "synthetic-table",
            "LEGALDESK_RECONCILIATION_BETA_TENANT_ID": "tenant-a",
            "LEGALDESK_RECONCILIATION_UPLOAD_SCOPES": json.dumps([{"tenantId": "tenant-a", "matterId": MATTER}]),
            "LEGALDESK_RECONCILIATION_INGESTION_SCOPES": "[]",
            "LEGALDESK_RECONCILIATION_UPLOAD_STALE_SECONDS": "3600",
            "LEGALDESK_RECONCILIATION_INGESTION_STALE_SECONDS": "3600",
            "LEGALDESK_RECONCILIATION_LIMIT_PER_SCOPE": "1",
            "LEGALDESK_IDP_RECOVERY_LIMIT_PER_SCOPE": "1",
        }), patch.dict("sys.modules", {"botocore.config": fake_botocore_config}), patch("boto3.resource", return_value=sdk_resource, create=True), patch("boto3.client", side_effect=sdk_client, create=True), \
            patch("legaldesk.reconciliation_lambda.Boto3DynamoDocumentMetadataRepository", return_value=metadata), \
            patch("legaldesk.reconciliation_lambda.Boto3S3ObjectStorage", return_value=storage), \
            patch("legaldesk.reconciliation_lambda.DynamoDBEphemeralStateStore", return_value=object()), \
            patch("legaldesk.reconciliation_lambda.AsyncKnowledgeBaseIngestionService", return_value=object()), \
            patch("legaldesk.reconciliation_lambda.Boto3DynamoIDPRepository", return_value=idp_repository), \
            patch("legaldesk.reconciliation_lambda.Boto3IDPSQSQueue", return_value=object()), \
            patch("legaldesk.reconciliation_lambda.Boto3DynamoGatewayGrantRepository", return_value=self.grants), \
            patch("legaldesk.idp.review.CognitoM2MTokenProvider", return_value=_Token()), \
            patch("legaldesk.gateway_client.UrllibGatewayTransport", return_value=http), \
            patch("legaldesk.gateway_interceptor._grant_repository_from_environment", return_value=self.grants), \
            patch("legaldesk.review_tasks._gateway_grant_repository_from_environment", return_value=self.grants), \
            patch("legaldesk.review_tasks._repositories_from_environment", return_value=(self.review_repository, None)), \
            patch("legaldesk.idp.persistence.Boto3DynamoIDPRepository", return_value=idp_repository), \
            patch("legaldesk.documents.Boto3DynamoDocumentMetadataRepository", return_value=metadata):
            service = _build_service(config)
            gateway = IDPMachineGatewayClient(gateway_url="https://gateway.example.com", token_provider=_Token(), transport=http)
            with self.assertRaises(TimeoutError):
                dispatch_review_after_persist(
                    run=self.locator.run, job=self.locator.job,
                    invocation_repository=self.grants, gateway=gateway,
                    machine_client_id=MACHINE_CLIENT,
                )
            with patch("legaldesk.reconciliation_lambda._get_service", return_value=service):
                first = __import__("legaldesk.reconciliation_lambda", fromlist=["lambda_handler"]).lambda_handler({}, None)
                first_http_calls = http.calls
                for _ in range(4):
                    __import__("legaldesk.reconciliation_lambda", fromlist=["lambda_handler"]).lambda_handler({}, None)
                    if http.calls == 2:
                        break
                recovered_http_calls = http.calls
                __import__("legaldesk.reconciliation_lambda", fromlist=["lambda_handler"]).lambda_handler({}, None)
                terminal_http_calls = http.calls

        self.assertEqual(first_http_calls, 1)
        self.assertEqual(recovered_http_calls, 2, {"metadataListCalls": metadata.calls, "reviewCalls": metadata.review_calls, "httpCalls": http.calls, "invocationStates": [item.get("deliveryState") for item in self.grants.idp_invocations.values()]})
        self.assertEqual(terminal_http_calls, 2)
        self.assertEqual(len(self.review_table.items), 1)
        states = [item.get("deliveryState") for item in self.grants.idp_invocations.values()]
        self.assertEqual(states.count("AMBIGUOUS"), 1)
        self.assertEqual(states.count("SENT"), 1)

    def test_real_resolver_binds_empty_metadata_to_canonical_bytes_and_rejects_same_key_mutation(self) -> None:
        transformed = self._invoke(_event(), real_resolver=True)
        grant_id = self._grant_id(transformed)
        with self._patches(real_resolver=True)[0], self._patches(real_resolver=True)[3], self._patches(real_resolver=True)[4], self._patches(real_resolver=True)[5], self._patches(real_resolver=True)[6], self._patches(real_resolver=True)[7]:
            result = gateway_lambda_handler({"_legaldeskGrantId": grant_id}, None)
        self.assertEqual(result.get("runId"), RUN, result)
        self.assertEqual(len(self.review_table.items), 1)

        transformed_after = self._invoke(_event(), real_resolver=True)
        grant_after = self._grant_id(transformed_after)
        # Same canonical key and byte length are not sufficient: a content
        # mutation must fail the resolver's hash binding before another grant.
        self.s3.body = b"different canonical bytes with same length"[: len(SOURCE_BYTES)]
        self.assertEqual(len(self.s3.body), len(SOURCE_BYTES))
        denied = self._invoke(_event(), real_resolver=True)
        self.assertEqual(denied["mcp"]["transformedGatewayResponse"]["statusCode"], 403)
        self.assertEqual(len(self.grants.idp_reviews), 2)
        with self._patches(real_resolver=True)[0], self._patches(real_resolver=True)[3], self._patches(real_resolver=True)[4], self._patches(real_resolver=True)[5], self._patches(real_resolver=True)[6], self._patches(real_resolver=True)[7]:
            target_denied = gateway_lambda_handler({"_legaldeskGrantId": grant_after}, None)
        self.assertEqual(target_denied["error"], "access_denied")
        self.assertEqual(len(self.review_table.items), 1)

    def test_real_resolver_rejects_valid_run_from_tenant_b_when_client_is_bound_to_tenant_a(self) -> None:
        self.locator.job = replace(self.locator.job, tenant_id="tenant-b")
        self.locator.run = replace(self.locator.run, tenant_id="tenant-b")
        with patch.dict("os.environ", {"LEGALDESK_IDP_REVIEW_TENANT_ID": "tenant-a", "LEGALDESK_IDP_REVIEW_MATTER_IDS": ALLOWED_MATTERS}):
            denied = self._invoke(_event(), real_resolver=True)
        self.assertEqual(denied["mcp"]["transformedGatewayResponse"]["statusCode"], 403)
        self.assertEqual(len(self.grants.idp_reviews), 0)

    def test_real_resolver_requires_both_explicit_tenant_and_matter_allowlist(self) -> None:
        """A synthetic table must not activate machine review authorization."""
        for missing in ("LEGALDESK_IDP_REVIEW_TENANT_ID", "LEGALDESK_IDP_REVIEW_MATTER_IDS"):
            with self.subTest(missing=missing):
                denied = self._invoke(_event(), real_resolver=True, scope_overrides={missing: ""})
            self.assertEqual(denied["mcp"]["transformedGatewayResponse"]["statusCode"], 403)
            self.assertEqual(len(self.grants.idp_reviews), 0)

    def test_http_200_mcp_error_leaves_dispatch_ambiguous_not_sent(self) -> None:
        from legaldesk.gateway_client import GatewayHttpResponse

        class _Token:
            @staticmethod
            def token() -> str:
                return "synthetic-machine-token"

        class _ErrorTransport:
            def post(inner, url, body, headers, timeout):
                request = json.loads(body.decode("utf-8"))
                payload = {"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32001, "message": "denied"}}
                return GatewayHttpResponse(200, "application/json", json.dumps(payload).encode("utf-8"))

        gateway = IDPMachineGatewayClient(
            gateway_url="https://gateway.example.com",
            token_provider=_Token(),
            transport=_ErrorTransport(),
        )
        with self.assertRaises(IDPReviewError):
            dispatch_review_after_persist(
                run=self.locator.run,
                job=self.locator.job,
                invocation_repository=self.grants,
                gateway=gateway,
                machine_client_id=MACHINE_CLIENT,
            )
        self.assertEqual(len(self.review_table.items), 0)
        states = [item.get("deliveryState") for item in self.grants.idp_invocations.values()]
        self.assertEqual(states.count("AMBIGUOUS"), 1)
        self.assertNotIn("SENT", states)


if __name__ == "__main__":
    unittest.main()
