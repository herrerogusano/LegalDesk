"""Phase 14.4 human review, history and selected-document query contracts.

These tests use the real Gateway interceptor, MCP server, Review Lambda
adapter, IDP repository and review service.  Only persistence/provider
transports are in-memory; authorization is a real bilateral user/matter
membership check.
"""

from __future__ import annotations

import base64
from contextlib import ExitStack
import hashlib
import io
import json
import sys
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).parents[1] / "backend" / "src"))

from legaldesk.authorization import (  # noqa: E402
    InMemoryAuthorizationStore,
    _gateway_identity_from_verified_subject,
    build_request_context,
)
from legaldesk.documents import InMemoryDocumentMetadataRepository  # noqa: E402
from legaldesk.documents import DocumentPipeline, InMemoryObjectStorage  # noqa: E402
from legaldesk.domain.models import (  # noqa: E402
    Document,
    DocumentStatus,
    Matter,
    MatterStatus,
    MalwareScanStatus,
    ReviewTaskStatus,
    User,
)
from legaldesk.gateway_interceptor import (  # noqa: E402
    GatewayTarget,
    IDP_REVIEW_SCOPE,
    InMemoryGatewayGrantRepository,
    gateway_request_interceptor,
)
from legaldesk.idp.models import (  # noqa: E402
    DocumentType,
    EvidenceAnchor,
    FieldAcceptance,
    FieldOrigin,
    FieldPresence,
    IDPCheckpoint,
    IDPContractError,
    IDPExtractionRun,
    IDPFieldResult,
    IDPJob,
    IDPJobStatus,
)
from legaldesk.idp.persistence import InMemoryIDPRepository  # noqa: E402
from legaldesk.idp.runtime import Boto3IDPDocumentReader  # noqa: E402
from legaldesk.idp.artifacts import Boto3S3IDPArtifactStore, idp_artifact_key  # noqa: E402
from legaldesk.idp.review import (  # noqa: E402
    IDPDecisionAction,
    IDPReviewInvocation,
    IDPFieldDecision,
    IDPReviewService,
    InMemoryIDPDecisionRepository,
    create_machine_review_task,
    query_selected_document,
)
from legaldesk.mcp_server import MCPServer, mcp_lambda_handler  # noqa: E402
from legaldesk.gateway_client import GatewayHttpResponse  # noqa: E402
from legaldesk.http_app import ApplicationComposition, create_http_app  # noqa: E402
from legaldesk.memory import InMemoryConversationBindingStore  # noqa: E402
from legaldesk.state import SessionRecord  # noqa: E402
from legaldesk.chat import ChatRequest, answer_question  # noqa: E402
from legaldesk.guardrails import GuardrailConfig  # noqa: E402
from legaldesk.reconciliation import ReconciliationScope  # noqa: E402
from legaldesk.reconciliation_lambda import ReconciliationConfig, _build_service  # noqa: E402
from legaldesk.review_tasks import (  # noqa: E402
    Boto3DynamoReviewTaskRepository,
    InMemoryReviewTaskRepository,
    ReviewTaskPersistenceError,
    gateway_lambda_handler,
)


TENANT = "tenant-a"
MATTER = "matter-a"
OTHER_MATTER = "matter-b"
DOCUMENT = "document-a"
SUBJECT = "human-subject"
USER_ID = "human-user"
SOURCE_BYTES = b"synthetic LegalDesk IDP source bytes\nEffective date: 31 January 2024.\n"
SOURCE_SHA = hashlib.sha256(SOURCE_BYTES).hexdigest()
CHANGED_SHA = hashlib.sha256(SOURCE_BYTES + b"changed").hexdigest()
# Evidence anchors are verified against the immutable run content hash by the
# human-decision boundary; synthetic page text is therefore bound to SOURCE_SHA.
PAGE_SHA = SOURCE_SHA
CORRELATION = "11111111-1111-4111-8111-111111111111"


def _segment(value: object) -> str:
    return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode().rstrip("=")


def _token(*, subject: str = SUBJECT, client: str | None = None, scope: str = "legaldesk/use", token_use: str = "access") -> str:
    claims: dict[str, object] = {"sub": subject, "scope": scope, "token_use": token_use}
    if client is not None:
        claims["client_id"] = client
    return f"{_segment({'alg': 'none'})}.{_segment(claims)}.unsigned"


def _document(*, matter: str = MATTER) -> Document:
    return Document(
        document_id=DOCUMENT,
        matter_id=matter,
        tenant_id=TENANT,
        name="contract.pdf",
        s3_key="tenant-a/matter-a/contract.pdf",
        media_type="application/pdf",
        jurisdiction="synthetic",
        document_date="2024-01-01",
        confidentiality="internal",
        status=DocumentStatus.UPLOADED,
        file_size_bytes=len(SOURCE_BYTES),
        malware_scan_status=MalwareScanStatus.CLEAN,
    )


def _field(name: str, *, acceptance: FieldAcceptance = FieldAcceptance.REVIEW_REQUIRED, value: object = "2024-01-31") -> IDPFieldResult:
    return IDPFieldResult(
        field=name,
        value=value,
        presence=FieldPresence.PRESENT,
        origin=FieldOrigin.LITERAL,
        acceptance=acceptance,
        evidence=(EvidenceAnchor(page=1, quote="Effective date: 31 January 2024.", content_sha256=PAGE_SHA),),
    )


def _run(run_id: str, *, created_at: datetime, acceptance: FieldAcceptance = FieldAcceptance.REVIEW_REQUIRED, sha: str = SOURCE_SHA, schema: str = "1.0.0", document_id: str = DOCUMENT) -> IDPExtractionRun:
    return IDPExtractionRun(
        run_id=run_id,
        tenant_id=TENANT,
        matter_id=MATTER,
        document_id=document_id,
        document_sha256=sha,
        document_type=DocumentType.CONTRACT,
        schema_version=schema,
        model_id="synthetic-model",
        prompt_version="synthetic-prompt",
        status=IDPJobStatus.REVIEW_REQUIRED,
        fields={"effective_date": _field("effective_date", acceptance=acceptance)},
        created_at=created_at,
        source_key="tenant-a/matter-a/contract.pdf",
    )


class _RecoveryMetadata:
    """Bounded document-page seam used by the real reconciliation closure."""

    def __init__(self, documents: tuple[Document, ...]) -> None:
        self.documents = documents
        self.page_calls = 0

    def list_for_scope_page(self, *, tenant_id: str, matter_id: str, limit: int, cursor: str | None = None):
        self.page_calls += 1
        selected = tuple(item for item in self.documents if item.tenant_id == tenant_id and item.matter_id == matter_id)
        return selected[:limit], None


class _ReviewTransport:
    def __init__(self) -> None:
        self.calls = 0

    def post(self, _url: str, body: bytes, _headers: dict[str, str], _timeout: float) -> GatewayHttpResponse:
        self.calls += 1
        request = json.loads(body.decode("utf-8"))
        return GatewayHttpResponse(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "jsonrpc": "2.0", "id": request["id"],
                "result": {"reviewTaskId": f"review-recovered-{self.calls}"},
            }).encode("utf-8"),
        )


class _StaticIdentityVerifier:
    def __init__(self, identity: object) -> None:
        self.identity = identity

    def verify_authorization_header(self, _header: str) -> object:
        return self.identity


class _RetrievalTransport:
    def __init__(self, *, document_id: str) -> None:
        self.document_id = document_id
        self.calls: list[dict[str, object]] = []
        self.return_wrong_document = False

    def retrieve(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        document_id = "document-foreign" if self.return_wrong_document else self.document_id
        return {
            "retrievalResults": [{
                "content": {"text": "Effective date: 31 January 2024."},
                "location": {"s3Location": {"uri": f"s3://synthetic/{document_id}.pdf"}},
                "metadata": {
                    "tenantId": TENANT,
                    "matterId": MATTER,
                    "documentId": document_id,
                    "documentName": "contract.pdf",
                    "x-amz-bedrock-kb-document-page-number": 1,
                },
            }],
        }


class _NoopGuardrail:
    def apply_guardrail(self, **_kwargs: object) -> dict[str, object]:
        return {"action": "NONE", "outputs": [], "assessments": []}


class _GroundedGenerator:
    def generate(self, _request: object) -> dict[str, object]:
        return {
            "answer": "The effective date is 31 January 2024.",
            "citationIds": ["citation-1"],
            "evidenceStatus": "answerable",
        }


class _ProofS3Transport:
    """One fake S3 transport for canonical source and page artifacts."""

    def __init__(self) -> None:
        self.source_body = SOURCE_BYTES
        self.artifacts: dict[str, tuple[bytes, str]] = {}

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        del Bucket
        if Key != "tenant-a/matter-a/contract.pdf":
            raise KeyError(Key)
        digest = hashlib.sha256(self.source_body).hexdigest()
        return {"ContentLength": len(self.source_body), "Metadata": {"legaldesk-sha256": digest}}

    def get_object(self, *, Bucket: str, Key: str, Range: str) -> dict[str, object]:
        del Bucket, Range
        if Key == "tenant-a/matter-a/contract.pdf":
            return {"Body": io.BytesIO(self.source_body)}
        body, digest = self.artifacts[Key]
        return {"Body": io.BytesIO(body), "Metadata": {"legaldesk-sha256": digest}}


class _InterleavingReviewRepository(InMemoryReviewTaskRepository):
    """Simulate a conditional task-close race after decision preflight."""

    def update(self, **_kwargs: object):
        raise ReviewTaskPersistenceError("synthetic conditional close race")

    def update_with_idp_decision(self, **_kwargs: object):
        raise ReviewTaskPersistenceError("synthetic conditional close race")


class HumanQueryContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.auth = InMemoryAuthorizationStore(
            users_by_subject={SUBJECT: User(USER_ID, SUBJECT, frozenset({TENANT}), frozenset({"attorney"}))},
            matters_by_id={
                MATTER: Matter(MATTER, TENANT, "Matter A", frozenset({USER_ID}), MatterStatus.ACTIVE),
                OTHER_MATTER: Matter(OTHER_MATTER, TENANT, "Matter B", frozenset(), MatterStatus.ACTIVE),
            },
        )
        self.metadata = InMemoryDocumentMetadataRepository()
        self.metadata.save(_document())
        self.runs = InMemoryIDPRepository()
        self.decisions = InMemoryIDPDecisionRepository()
        self.review_service = IDPReviewService(self.decisions)
        self.reviews = InMemoryReviewTaskRepository()
        self.grants = InMemoryGatewayGrantRepository()

    def _http_chat_app(
        self,
        *,
        document: Document,
        runs: InMemoryIDPRepository,
        chat_service: object,
    ) -> tuple[object, list[object]]:
        """Build the real WSGI composition with provider seams only faked."""

        self.metadata.save(document)
        identity = _gateway_identity_from_verified_subject(SUBJECT)
        conversations = InMemoryConversationBindingStore()
        context = build_request_context(identity, MATTER, self.auth)
        conversations.bind(context, conversation_id="conversation-a", session_selector="session-a")
        composition = ApplicationComposition(
            identity_verifier=_StaticIdentityVerifier(identity),
            token_exchange=None,
            authorization_store=self.auth,
            conversation_store=conversations,
            document_pipeline=DocumentPipeline(self.auth, InMemoryObjectStorage(), self.metadata),
            object_storage=InMemoryObjectStorage(),
            metadata_repository=self.metadata,
            mcp_server=MCPServer(self.metadata, idp_repository=runs, idp_review_service=self.review_service),
            chat_service=chat_service,
            matter_catalog=(MATTER,),
        )
        app = create_http_app(composition)
        # Mirror the durable server-side record created by POST
        # /api/conversations; citation inspection must not rely on the
        # browser-supplied conversation/session pair alone.
        app.state_store.bind_conversation(
            "conversation-a",
            (SUBJECT, TENANT, MATTER, "session-a"),
            context.correlation_id,
        )
        app.state_store.put_session(
            "session-a",
            SessionRecord(identity, "synthetic-access-token", "csrf-a", time.time() + 3_600),
        )
        return app, [identity]

    @staticmethod
    def _http_call(app: object, *, method: str, path: str, payload: dict[str, object] | None = None) -> tuple[int, dict[str, object]]:
        raw = json.dumps(payload or {}).encode("utf-8")
        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path.split("?", 1)[0],
            "QUERY_STRING": path.split("?", 1)[1] if "?" in path else "",
            "HTTP_HOST": "localhost",
            "HTTP_COOKIE": "legaldesk_session=session-a",
            "HTTP_X_CSRF_TOKEN": "csrf-a" if method not in {"GET", "HEAD"} else "",
            "CONTENT_LENGTH": str(len(raw)) if method not in {"GET", "HEAD"} else "0",
            "wsgi.input": io.BytesIO(raw),
        }
        result: list[str] = []
        body = b"".join(app(environ, lambda status, _headers: result.append(status)))  # type: ignore[operator]
        return int(result[0].split(" ", 1)[0]), json.loads(body.decode("utf-8"))

    @classmethod
    def _http_post(cls, app: object, payload: dict[str, object]) -> tuple[int, dict[str, object]]:
        return cls._http_call(app, method="POST", path="/api/chat", payload=payload)

    def _event(self, *, tool: str, arguments: dict[str, object], matter: str = MATTER, token: str | None = None) -> dict[str, object]:
        body = {
            "jsonrpc": "2.0", "id": "human-1", "method": "tools/call",
            "params": {"name": tool, "arguments": dict(arguments)},
        }
        return {"mcp": {"gatewayRequest": {"headers": {
            "Authorization": f"Bearer {token or _token()}",
            "x-legaldesk-requested-matter-id": matter,
        }, "body": body}, "rawGatewayRequest": {"body": json.dumps(body)}}}

    def _intercept(self, event: dict[str, object]) -> dict[str, object]:
        with patch.dict("os.environ", {"LEGALDESK_IDP_M2M_CLIENT_ID": "synthetic-idp-machine"}), patch(
            "legaldesk.gateway_interceptor._authorization_store_from_environment", return_value=self.auth
        ), patch("legaldesk.gateway_interceptor._grant_repository_from_environment", return_value=self.grants):
            return gateway_request_interceptor(event, None)

    def _metadata_gateway(self, *, history_limit: int = 1, cursor: str | None = None, matter: str = MATTER, proof_bundle: tuple[object, object] | None = None) -> dict[str, object]:
        arguments: dict[str, object] = {"matterId": matter, "documentId": DOCUMENT, "historyLimit": history_limit}
        if cursor is not None:
            arguments["historyCursor"] = cursor
        transformed = self._intercept(self._event(
            tool="metadata-mcp___get_document_metadata", arguments=arguments, matter=matter,
        ))
        if "transformedGatewayRequest" not in transformed.get("mcp", {}):
            return transformed
        target = transformed["mcp"]["transformedGatewayRequest"]
        target_body = dict(target["body"])
        params = dict(target_body["params"])
        params["name"] = "get_document_metadata"
        target_body["params"] = params
        # Exercise the actual MCP Lambda target: it consumes the opaque grant,
        # reloads the subject/matter, and reauthorizes before metadata access.
        env = {"DOCUMENT_METADATA_TABLE_NAME": "synthetic-metadata"}
        if proof_bundle is not None:
            env["LEGALDESK_IDP_ARTIFACT_BUCKET"] = "synthetic-artifacts"
        else:
            env["LEGALDESK_IDP_ARTIFACT_BUCKET"] = ""
            env["LEGALDESK_SOURCE_BUCKET"] = ""
        with ExitStack() as stack:
            stack.enter_context(patch.dict("os.environ", env))
            stack.enter_context(patch(
                "legaldesk.mcp_server._mcp_repositories_from_environment",
                return_value=(self.auth, self.metadata),
            ))
            stack.enter_context(patch("legaldesk.mcp_server._mcp_grant_repository_from_environment", return_value=self.grants))
            stack.enter_context(patch(
                "legaldesk.idp.persistence.Boto3DynamoIDPRepository", return_value=self.runs
            ))
            stack.enter_context(patch("legaldesk.idp.review.Boto3DynamoIDPDecisionRepository", return_value=self.decisions))
            if proof_bundle is not None:
                stack.enter_context(patch(
                    "legaldesk.mcp_server._idp_artifact_store_from_environment",
                    return_value=proof_bundle,
                ))
            target_response = mcp_lambda_handler(
                {"headers": target["headers"], "body": json.dumps(target_body), "isBase64Encoded": False}, None
            )
        if target_response.get("statusCode") != 200:
            return target_response
        return json.loads(str(target_response["body"]))

    def _review_gateway(self, *, task_id: str, decision: dict[str, object], matter: str = MATTER, token: str | None = None, idp_repository: object | None = None, proof_bundle: tuple[object, object] | None = None) -> dict[str, object]:
        event = self._event(
            tool="review-task-lambda___update_review_task",
            matter=matter,
            token=token,
            arguments={"matterId": matter, "reviewTaskId": task_id, "status": ReviewTaskStatus.CLOSED.value, "idpDecision": decision},
        )
        transformed = self._intercept(event)
        if "transformedGatewayRequest" not in transformed.get("mcp", {}):
            return transformed
        args = transformed["mcp"]["transformedGatewayRequest"]["body"]["params"]["arguments"]
        grant_id = args["_legaldeskGrantId"]
        env = {"REVIEW_TASK_TABLE_NAME": "synthetic-review", "IDP_TABLE_NAME": "synthetic-review"}
        if proof_bundle is not None:
            env["LEGALDESK_IDP_ARTIFACT_BUCKET"] = "synthetic-artifacts"
        else:
            env["LEGALDESK_IDP_ARTIFACT_BUCKET"] = ""
            env["LEGALDESK_SOURCE_BUCKET"] = ""
        with ExitStack() as stack:
            stack.enter_context(patch.dict("os.environ", env))
            stack.enter_context(patch(
                "legaldesk.review_tasks._repositories_from_environment", return_value=(self.reviews, self.auth)
            ))
            stack.enter_context(patch("legaldesk.review_tasks._gateway_grant_repository_from_environment", return_value=self.grants))
            stack.enter_context(patch(
                "legaldesk.idp.persistence.Boto3DynamoIDPRepository", return_value=idp_repository or self.runs
            ))
            stack.enter_context(patch("legaldesk.idp.review.Boto3DynamoIDPDecisionRepository", return_value=self.decisions))
            if proof_bundle is not None:
                stack.enter_context(patch(
                    "legaldesk.review_tasks._idp_artifact_store_from_environment",
                    return_value=proof_bundle,
                ))
            return gateway_lambda_handler({"_legaldeskGrantId": grant_id, **args}, None)

    def _save_review_task(self, run: IDPExtractionRun) -> str:
        self.runs.save_run(run)
        invocation = IDPReviewInvocation(
            tenant_id=TENANT, matter_id=MATTER, document_id=DOCUMENT, run_id=run.run_id,
            document_sha256=run.document_sha256, correlation_id=CORRELATION,
            machine_client_id="synthetic-idp-machine", field_names=("effective_date",),
        )
        result = create_machine_review_task(
            invocation=invocation, run=run, review_repository=self.reviews, service_actor="service:idp-review",
        )
        return str(result["reviewTaskId"])

    def _proof_run(self, *, run_id: str = "proof-run", page_text: str = "Effective date: 31 January 2024.") -> tuple[IDPExtractionRun, tuple[object, object], _ProofS3Transport]:
        s3 = _ProofS3Transport()
        payload = json.dumps({
            "tenantId": TENANT,
            "matterId": MATTER,
            "documentId": DOCUMENT,
            "runId": run_id,
            "documentSha256": SOURCE_SHA,
            "pages": {"1": page_text},
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        artifact_sha = hashlib.sha256(payload).hexdigest()
        key = idp_artifact_key(
            tenant_id=TENANT, matter_id=MATTER, document_id=DOCUMENT,
            run_id=run_id, kind="pages", sha256=artifact_sha,
        )
        s3.artifacts[key] = (payload, artifact_sha)
        artifact_store = Boto3S3IDPArtifactStore("synthetic-artifacts", client=s3)
        reader = Boto3IDPDocumentReader(
            self.metadata, s3, bucket_name="synthetic-source", max_bytes=20 * 1024 * 1024,
        )
        run = replace(_run(run_id, created_at=datetime(2024, 1, 1, tzinfo=timezone.utc)), page_text_artifact_key=key)
        return run, (artifact_store, reader), s3

    def test_human_gateway_metadata_exposes_current_fields_and_bounded_history_pages(self) -> None:
        base = datetime(2024, 1, 1, tzinfo=timezone.utc)
        # UUID-like identifiers are intentionally ordered opposite to time;
        # history must use the server timestamp, not lexical run IDs.
        for run_id, offset in (("run-0003", 0), ("run-0001", 1), ("run-0002", 2)):
            self.runs.save_run(_run(run_id, created_at=base + timedelta(days=offset)))

        first = self._metadata_gateway(history_limit=1)
        payload = json.loads(first["result"]["content"][0]["text"])
        self.assertEqual(payload["document"]["documentId"], DOCUMENT)
        self.assertEqual(payload["idp"]["status"], "IDP_REVIEW_REQUIRED")
        self.assertEqual(payload["idp"]["documentSha256"], SOURCE_SHA)
        self.assertEqual(payload["idp"]["schemaVersion"], "1.0.0")
        self.assertEqual(payload["idp"]["fields"][0]["name"], "effective_date")
        self.assertEqual(payload["idp"]["fields"][0]["evidence"][0]["page"], 1)
        self.assertEqual(payload["idp"]["history"][0]["runId"], "run-0002")
        cursor = payload["idp"]["nextCursor"]
        self.assertIsInstance(cursor, str)

        seen = [payload["idp"]["history"][0]["runId"]]
        while cursor:
            page = json.loads(self._metadata_gateway(history_limit=1, cursor=cursor)["result"]["content"][0]["text"])
            seen.append(page["idp"]["history"][0]["runId"])
            cursor = page["idp"]["nextCursor"]
        self.assertEqual(seen, ["run-0002", "run-0001", "run-0003"])

        cross = self._metadata_gateway(matter=OTHER_MATTER)
        self.assertEqual(cross["mcp"]["transformedGatewayResponse"]["statusCode"], 403)

    def test_human_gateway_review_actions_are_scoped_and_evidence_bound(self) -> None:
        base = datetime(2024, 1, 1, tzinfo=timezone.utc)
        task_ids: dict[str, str] = {}
        for index, action in enumerate(("APPROVE", "CORRECT", "REJECT")):
            run = _run(f"run-action-{index}", created_at=base + timedelta(days=index))
            task_ids[action] = self._save_review_task(run)
            decision: dict[str, object] = {
                "fieldName": "effective_date", "action": action, "reason": "human reviewed",
                "evidence": [{"page": 1, "quote": "Effective date: 31 January 2024.", "contentSha256": PAGE_SHA}],
            }
            if action == "CORRECT":
                # The reviewer supplies only the raw replacement value.  The
                # server preserves/derives presence, origin and acceptance.
                decision["proposedValue"] = "2024-02-01"
            response = self._review_gateway(task_id=task_ids[action], decision=decision)
            self.assertIn("idpDecision", response, response)
            self.assertEqual(response["idpDecision"]["action"], action)
            effective_after_action = self.review_service.effective_field(
                run=self.runs.get_run(tenant_id=TENANT, matter_id=MATTER, document_id=DOCUMENT, run_id=f"run-action-{index}"),
                field_name="effective_date",
            )
            self.assertIsNotNone(effective_after_action)
            if action == "APPROVE":
                self.assertEqual(effective_after_action["acceptance"], FieldAcceptance.HUMAN_CONFIRMED.value)
            elif action == "CORRECT":
                self.assertEqual(effective_after_action["value"], "2024-02-01")
            else:
                self.assertEqual(effective_after_action["acceptance"], FieldAcceptance.REJECTED.value)

        effective = self.review_service.effective_field(run=self.runs.get_run(tenant_id=TENANT, matter_id=MATTER, document_id=DOCUMENT, run_id="run-action-1"), field_name="effective_date")
        # The newest compatible human decision is authoritative across runs;
        # the later REJECT therefore supersedes the earlier CORRECT.
        self.assertEqual(effective["acceptance"], FieldAcceptance.REJECTED.value)

        for invalid_index, invalid in enumerate((
            {"fieldName": "missing_field", "action": "APPROVE", "reason": "no such schema field", "evidence": []},
            {"fieldName": "effective_date", "action": "APPROVE", "reason": "unknown source", "documentSha256": CHANGED_SHA, "evidence": []},
            {"fieldName": "effective_date", "action": "APPROVE", "reason": "unknown evidence key", "evidence": [{"page": 1, "quote": "x", "contentSha256": PAGE_SHA, "unexpected": True}]},
            {"fieldName": "effective_date", "action": "CORRECT", "reason": "wrong schema type", "proposedValue": 123, "evidence": []},
            {"fieldName": "effective_date", "action": "CORRECT", "reason": "client cannot set origin", "proposedValue": {"value": "2024-02-02", "origin": "DERIVED"}, "evidence": []},
            {"fieldName": "effective_date", "action": "CORRECT", "reason": "client cannot set acceptance", "proposedValue": {"value": "2024-02-02", "acceptance": "AUTO_ACCEPTED"}, "evidence": []},
        )):
            invalid_task = self._save_review_task(_run(f"run-invalid-{invalid_index}", created_at=base + timedelta(days=10 + invalid_index)))
            response = self._review_gateway(task_id=invalid_task, decision=invalid)
            self.assertIn(response.get("error"), {"invalid_request", "review_task_error"}, response)

        cross = self._review_gateway(task_id=task_ids["APPROVE"], matter=OTHER_MATTER, decision={"fieldName": "effective_date", "action": "APPROVE", "reason": "cross matter", "evidence": []})
        self.assertEqual(cross["mcp"]["transformedGatewayResponse"]["statusCode"], 403)

        m2m_task_id = self._save_review_task(_run("run-m2m", created_at=base + timedelta(days=4)))
        m2m = self._review_gateway(
            task_id=m2m_task_id,
            token=_token(subject=SUBJECT, client="synthetic-idp-machine", scope=IDP_REVIEW_SCOPE),
            decision={"fieldName": "effective_date", "action": "APPROVE", "reason": "machine cannot confirm", "evidence": []},
        )
        self.assertEqual(
            m2m.get("mcp", {}).get("transformedGatewayResponse", {}).get("statusCode"),
            403,
            m2m,
        )

        closed_task = self._save_review_task(_run("run-closed", created_at=base + timedelta(days=5)))
        human_context = build_request_context(
            _gateway_identity_from_verified_subject(SUBJECT), MATTER, self.auth, correlation_id=CORRELATION
        )
        self.reviews.update(
            context=human_context,
            review_task_id=closed_task,
            status=ReviewTaskStatus.CLOSED,
            resolution_note="already resolved",
        )
        decisions_before = len(self.decisions.list_decisions(
            tenant_id=TENANT, matter_id=MATTER, document_id=DOCUMENT, field_name="effective_date"
        ))
        closed_response = self._review_gateway(
            task_id=closed_task,
            decision={
                "fieldName": "effective_date", "action": "APPROVE", "reason": "must not mutate closed task",
                "evidence": [{"page": 1, "quote": "Effective date: 31 January 2024.", "contentSha256": PAGE_SHA}],
            },
        )
        self.assertIn(closed_response.get("error"), {"invalid_request", "review_task_error"}, closed_response)
        decisions_after = len(self.decisions.list_decisions(
            tenant_id=TENANT, matter_id=MATTER, document_id=DOCUMENT, field_name="effective_date"
        ))
        self.assertEqual(decisions_after, decisions_before)

    def test_real_review_handler_accepts_new_artifact_anchor_but_rejects_forged_or_scoped_anchor(self) -> None:
        run, proof_bundle, _s3 = self._proof_run(run_id="proof-valid")
        task_id = self._save_review_task(run)
        accepted = self._review_gateway(
            task_id=task_id,
            decision={
                "fieldName": "effective_date", "action": "APPROVE", "reason": "artifact-backed anchor",
                "evidence": [{"page": 1, "quote": "31 January 2024", "contentSha256": SOURCE_SHA}],
            },
            proof_bundle=proof_bundle,
        )
        self.assertIn("idpDecision", accepted, accepted)
        self.assertEqual(accepted["idpDecision"]["action"], IDPDecisionAction.APPROVE.value)

        forged, forged_bundle, _ = self._proof_run(run_id="proof-forged")
        forged_task = self._save_review_task(forged)
        rejected_quote = self._review_gateway(
            task_id=forged_task,
            decision={
                "fieldName": "effective_date", "action": "APPROVE", "reason": "must not invent page text",
                "evidence": [{"page": 1, "quote": "31 February 2024", "contentSha256": SOURCE_SHA}],
            },
            proof_bundle=forged_bundle,
        )
        self.assertIn(rejected_quote.get("error"), {"invalid_request", "review_task_error"}, rejected_quote)

        scoped, scoped_bundle, _ = self._proof_run(run_id="proof-scoped")
        # The bytes are valid, but the artifact key belongs to another run;
        # the review Lambda must reject this scope substitution.
        scoped = replace(scoped, page_text_artifact_key=forged.page_text_artifact_key)
        scoped_task = self._save_review_task(scoped)
        rejected_scope = self._review_gateway(
            task_id=scoped_task,
            decision={
                "fieldName": "effective_date", "action": "APPROVE", "reason": "must reject foreign artifact scope",
                "evidence": [{"page": 1, "quote": "31 January 2024", "contentSha256": SOURCE_SHA}],
            },
            proof_bundle=scoped_bundle,
        )
        self.assertIn(rejected_scope.get("error"), {"invalid_request", "review_task_error"}, rejected_scope)

        digest_run, digest_bundle, _ = self._proof_run(run_id="proof-digest")
        original_key = str(digest_run.page_text_artifact_key)
        replacement = "0" if original_key[-1] != "0" else "1"
        digest_run = replace(digest_run, page_text_artifact_key=original_key[:-1] + replacement)
        digest_task = self._save_review_task(digest_run)
        rejected_digest = self._review_gateway(
            task_id=digest_task,
            decision={
                "fieldName": "effective_date", "action": "APPROVE", "reason": "must reject altered artifact digest",
                "evidence": [{"page": 1, "quote": "31 January 2024", "contentSha256": SOURCE_SHA}],
            },
            proof_bundle=digest_bundle,
        )
        self.assertIn(rejected_digest.get("error"), {"invalid_request", "review_task_error"}, rejected_digest)

    def test_real_review_handler_rechecks_canonical_bytes_before_decision(self) -> None:
        run, proof_bundle, s3 = self._proof_run(run_id="proof-mutated")
        task_id = self._save_review_task(run)
        s3.source_body = b"x" * len(SOURCE_BYTES)
        rejected = self._review_gateway(
            task_id=task_id,
            decision={
                "fieldName": "effective_date", "action": "APPROVE", "reason": "canonical source changed",
                "evidence": [{"page": 1, "quote": "Effective date: 31 January 2024.", "contentSha256": SOURCE_SHA}],
            },
            proof_bundle=proof_bundle,
        )
        self.assertIn(rejected.get("error"), {"invalid_request", "review_task_error"}, rejected)
        self.assertEqual(self.decisions.list_decisions(
            tenant_id=TENANT, matter_id=MATTER, document_id=DOCUMENT, field_name="effective_date",
        ), ())

    def test_real_metadata_handler_rechecks_canonical_bytes_before_current_projection(self) -> None:
        run, proof_bundle, s3 = self._proof_run(run_id="proof-metadata-mutated")
        self.runs.save_run(run)
        before = self._metadata_gateway(history_limit=1, proof_bundle=proof_bundle)
        before_payload = json.loads(before["result"]["content"][0]["text"])
        self.assertEqual(before_payload["idp"]["runId"], run.run_id)
        s3.source_body = b"z" * len(SOURCE_BYTES)
        after = self._metadata_gateway(history_limit=1, proof_bundle=proof_bundle)
        after_payload = json.loads(after["body"]) if "body" in after else after
        self.assertEqual(after_payload["error"]["code"], -32000, after)

    def test_task_close_race_does_not_leave_an_orphan_human_decision(self) -> None:
        original_reviews = self.reviews
        self.reviews = _InterleavingReviewRepository()
        try:
            run = _run("race-run", created_at=datetime(2024, 1, 1, tzinfo=timezone.utc))
            task_id = self._save_review_task(run)
            response = self._review_gateway(
                task_id=task_id,
                decision={
                    "fieldName": "effective_date", "action": "APPROVE", "reason": "conditional close race",
                    "evidence": [{"page": 1, "quote": "Effective date: 31 January 2024.", "contentSha256": SOURCE_SHA}],
                },
            )
            self.assertIn(response.get("error"), {"service_unavailable", "review_task_error"}, response)
            self.assertEqual(self.decisions.list_decisions(
                tenant_id=TENANT, matter_id=MATTER, document_id=DOCUMENT, field_name="effective_date",
            ), ())
        finally:
            self.reviews = original_reviews

    def test_boto_review_decision_transaction_serializes_one_native_dynamo_shape(self) -> None:
        """Capture the actual SDK wire body without contacting DynamoDB.

        The repository must use one shared table transaction: the task put is
        conditional on its current status and the append-only decision put is
        conditional on both key attributes being absent.  This deliberately
        exercises botocore's serializer rather than a hand-written fake table,
        including a Decimal nested in the decision result.
        """
        import boto3
        from legaldesk.idp.review import Boto3DynamoIDPDecisionRepository

        run = _run("boto-wire-run", created_at=datetime(2024, 1, 1, tzinfo=timezone.utc))
        task_id = self._save_review_task(run)
        context = build_request_context(
            _gateway_identity_from_verified_subject(SUBJECT), MATTER, self.auth,
            correlation_id=CORRELATION,
        )
        task = self.reviews.get(context=context, review_task_id=task_id)
        self.assertIsNotNone(task)
        decision = IDPFieldDecision(
            decision_id="decision-boto-wire", tenant_id=TENANT, matter_id=MATTER,
            document_id=DOCUMENT, run_id=run.run_id, document_sha256=SOURCE_SHA,
            schema_version=run.schema_version, field_name="effective_date",
            action=IDPDecisionAction.CORRECT, reviewer_user_id=USER_ID,
            correlation_id=CORRELATION, previous={"value": "2024-01-31"},
            proposed={"value": Decimal("123.45"), "presence": FieldPresence.PRESENT.value},
            result={
                "value": Decimal("123.45"),
                "presence": FieldPresence.PRESENT.value,
                "origin": FieldOrigin.LITERAL.value,
            }, evidence=(run.fields["effective_date"].evidence[0],),
            reason="wire serialization", review_task_id=task_id,
        )

        session = boto3.Session(
            aws_access_key_id="synthetic-access-key",
            aws_secret_access_key="synthetic-secret-key",
            region_name="eu-west-1",
        )
        resource = session.resource("dynamodb", endpoint_url="http://127.0.0.1:9")
        table = resource.Table("synthetic-review")
        repository = Boto3DynamoReviewTaskRepository("synthetic-review", table=table)
        decision_repository = Boto3DynamoIDPDecisionRepository("synthetic-review", table=table)
        captured: dict[str, object] = {}

        class NetworkMustNotBeReached(Exception):
            pass

        def capture_before_call(**kwargs: object) -> None:
            captured.update(kwargs)
            raise NetworkMustNotBeReached()

        table.meta.client.meta.events.register(
            "before-call.dynamodb.TransactWriteItems", capture_before_call,
        )
        try:
            with patch.object(repository, "get", return_value=task), self.assertRaises(ReviewTaskPersistenceError):
                repository.update_with_idp_decision(
                    context=context, review_task_id=task_id, status=ReviewTaskStatus.CLOSED,
                    resolution_note="closed from captured wire test", decision=decision,
                    decision_repository=decision_repository,
                )
        finally:
            table.meta.client.meta.events.unregister(
                "before-call.dynamodb.TransactWriteItems", capture_before_call,
            )

        wire = captured.get("params")
        self.assertIsInstance(wire, dict)
        body = wire.get("body")
        self.assertIsInstance(body, bytes)
        payload = json.loads(body.decode("utf-8"))
        writes = payload["TransactItems"]
        self.assertEqual(len(writes), 2)
        puts = [entry["Put"] for entry in writes]
        self.assertEqual({put["TableName"] for put in puts}, {"synthetic-review"})

        task_put, decision_put = puts
        self.assertIn("#status = :expectedStatus", task_put["ConditionExpression"])
        self.assertEqual(
            task_put["ExpressionAttributeValues"][":expectedStatus"],
            {"S": ReviewTaskStatus.OPEN.value},
        )
        self.assertIn("#matterId = :matterId", task_put["ConditionExpression"])
        self.assertEqual(task_put["ExpressionAttributeValues"][":matterId"], {"S": MATTER})
        self.assertEqual(
            decision_put["ConditionExpression"],
            "attribute_not_exists(pk) AND attribute_not_exists(sk)",
        )

        encoded_result = decision_put["Item"]["result"]
        self.assertEqual(encoded_result["M"]["value"], {"N": "123.45"})
        self.assertNotIn("S", encoded_result["M"]["value"])
        self.assertNotIn("M", encoded_result["M"]["value"])

    def test_newer_compatible_human_decision_wins_current_projection(self) -> None:
        run = _run("projection-order", created_at=datetime(2024, 1, 1, tzinfo=timezone.utc))
        anchor = run.fields["effective_date"].evidence[0]
        base = datetime(2024, 1, 1, tzinfo=timezone.utc)
        old = IDPFieldDecision(
            decision_id="decision-old", tenant_id=TENANT, matter_id=MATTER, document_id=DOCUMENT,
            run_id=run.run_id, document_sha256=SOURCE_SHA, schema_version=run.schema_version,
            field_name="effective_date", action=IDPDecisionAction.APPROVE, reviewer_user_id=USER_ID,
            correlation_id=CORRELATION, previous={"value": "2024-01-31"}, proposed=None,
            result={"value": "2024-01-31", "presence": FieldPresence.PRESENT.value, "origin": FieldOrigin.LITERAL.value},
            evidence=(anchor,), reason="older", created_at=base,
        )
        new = replace(
            old, decision_id="decision-new", action=IDPDecisionAction.CORRECT,
            proposed={"value": "2024-02-01", "presence": FieldPresence.PRESENT.value, "origin": FieldOrigin.LITERAL.value},
            result={"value": "2024-02-01", "presence": FieldPresence.PRESENT.value, "origin": FieldOrigin.LITERAL.value},
            reason="newer", created_at=base + timedelta(days=1),
        )
        self.decisions.save_decision(old)
        self.decisions.save_decision(new)
        effective = self.review_service.effective_field(run=run, field_name="effective_date")
        self.assertEqual(effective["value"], "2024-02-01")
        self.assertEqual(effective["acceptance"], FieldAcceptance.HUMAN_CONFIRMED.value)

    def test_human_confirmation_wins_same_content_rerun_but_not_changed_hash_or_schema(self) -> None:
        original = _run("run-confirmed", created_at=datetime(2024, 1, 1, tzinfo=timezone.utc))
        context = build_request_context(_gateway_identity_from_verified_subject(SUBJECT), MATTER, self.auth, correlation_id=CORRELATION)
        decision = self.review_service.record_human_decision(
            context=context, run=original, field_name="effective_date", action=IDPDecisionAction.APPROVE,
            proposed=None, result=None, evidence=original.fields["effective_date"].evidence, reason="confirmed",
        )
        same_content = replace(original, run_id="run-rerun-same", created_at=datetime(2024, 1, 2, tzinfo=timezone.utc))
        self.runs.save_run(original)
        self.runs.save_run(same_content)
        # The immutable decision is keyed to the content/schema identity; a
        # same-content rerun retains the human-confirmed effective value.
        self.assertEqual(self.review_service.effective_field(run=same_content, field_name="effective_date")["acceptance"], FieldAcceptance.HUMAN_CONFIRMED.value)
        metadata = self._metadata_gateway(history_limit=2)
        metadata_payload = json.loads(metadata["result"]["content"][0]["text"])
        self.assertEqual(metadata_payload["idp"]["runId"], "run-rerun-same")
        self.assertEqual(metadata_payload["idp"]["fields"][0]["acceptance"], FieldAcceptance.HUMAN_CONFIRMED.value)
        changed_content = replace(original, run_id="run-rerun-changed", document_sha256=CHANGED_SHA)
        changed_schema = replace(original, run_id="run-rerun-schema", schema_version="2.0.0")
        self.assertNotEqual(self.review_service.effective_field(run=changed_content, field_name="effective_date")["acceptance"], FieldAcceptance.HUMAN_CONFIRMED.value)
        self.assertNotEqual(self.review_service.effective_field(run=changed_schema, field_name="effective_date")["acceptance"], FieldAcceptance.HUMAN_CONFIRMED.value)
        with self.assertRaises(Exception):
            self.decisions.save_decision(replace(decision, action=IDPDecisionAction.REJECT))

    def test_selected_document_query_is_idp_first_then_filtered_rag_or_insufficient(self) -> None:
        run = _run("run-query", created_at=datetime(2024, 1, 1, tzinfo=timezone.utc), acceptance=FieldAcceptance.PROVISIONAL)
        self.runs.save_run(run)
        context = build_request_context(_gateway_identity_from_verified_subject(SUBJECT), MATTER, self.auth, correlation_id=CORRELATION)
        rag_calls: list[tuple[str, str]] = []

        def rag_fallback(_context, document_id: str, field_name: str):
            rag_calls.append((document_id, field_name))
            return {"answer": "RAG answer", "citations": ["citation-document-a"]}

        idp = query_selected_document(context=context, document_id=DOCUMENT, field_name="effective_date", repository=self.runs, review_service=self.review_service, rag_fallback=rag_fallback)
        self.assertEqual(idp.source, "IDP")
        self.assertEqual(idp.status, "AVAILABLE")
        self.assertEqual(idp.value["acceptance"], FieldAcceptance.PROVISIONAL.value)
        self.assertTrue(idp.value["evidence"])
        self.assertEqual(rag_calls, [])

        unavailable = replace(
            run, run_id="run-unavailable", status=IDPJobStatus.FAILED,
            fields={"effective_date": IDPFieldResult(field="effective_date", value=None, presence=FieldPresence.UNKNOWN, origin=FieldOrigin.LITERAL, acceptance=FieldAcceptance.UNAVAILABLE)},
        )
        self.runs.save_run(unavailable)
        fallback = query_selected_document(context=context, document_id=DOCUMENT, field_name="effective_date", repository=self.runs, review_service=self.review_service, rag_fallback=rag_fallback)
        self.assertEqual(fallback.source, "RAG")
        self.assertEqual(rag_calls[-1], (DOCUMENT, "effective_date"))
        insufficient = query_selected_document(context=context, document_id=DOCUMENT, field_name="effective_date", repository=self.runs, review_service=self.review_service)
        self.assertEqual(insufficient.status, "INSUFFICIENT_EVIDENCE")
        self.assertIsNone(insufficient.fallback)

    def test_http_selected_document_idp_answer_precedes_chat_and_paid_rag(self) -> None:
        run = _run("run-http-idp", created_at=datetime(2024, 1, 1, tzinfo=timezone.utc), acceptance=FieldAcceptance.PROVISIONAL)
        self.runs.save_run(run)
        chat_calls: list[object] = []

        def unexpected_chat(*_args: object, **_kwargs: object) -> object:
            chat_calls.append(True)
            raise AssertionError("usable IDP must not invoke chat or retrieval")

        app, _ = self._http_chat_app(document=replace(_document(), status=DocumentStatus.INDEXED), runs=self.runs, chat_service=unexpected_chat)
        status, payload = self._http_post(app, {
            "question": "What is the effective date?",
            "matterId": MATTER,
            "conversationId": "conversation-a",
            "sessionId": "session-a",
            "selectedDocumentId": DOCUMENT,
            "selectedFieldName": "effective_date",
        })
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["idp"]["field"], "effective_date")
        self.assertEqual(payload["idp"]["acceptance"], FieldAcceptance.PROVISIONAL.value)
        self.assertEqual(payload["evidenceStatus"], "ambiguous")
        self.assertTrue(payload["citations"])
        self.assertEqual(chat_calls, [])
        # The citation handle is server-created and can only be inspected
        # through the real authenticated HTTP target, not from model output.
        handles = tuple(app.citation_handles)  # type: ignore[attr-defined]
        self.assertEqual(len(handles), 1)
        citation_status, citation = self._http_call(app, method="GET", path=f"/api/citations?handle={handles[0]}")
        self.assertEqual(citation_status, 200, citation)
        self.assertEqual(citation["documentId"], DOCUMENT)
        self.assertEqual(citation["passage"], "Effective date: 31 January 2024.")

    def test_http_selected_document_fallback_uses_real_answer_question_filter_and_rejects_wrong_result(self) -> None:
        document = replace(_document(), status=DocumentStatus.INDEXED)
        runs = InMemoryIDPRepository()
        retrieval = _RetrievalTransport(document_id=DOCUMENT)
        binding_holder: dict[str, InMemoryConversationBindingStore] = {}

        def chat_service(identity: object, *, matter_id: str, conversation_id: str, session_id: str, question: str, correlation_id: str, selected_document_id: str | None = None, selected_field_name: str | None = None, **_kwargs: object) -> object:
            request = ChatRequest(
                conversation_id,
                session_id,
                matter_id,
                question,
                selected_document_id=selected_document_id,
                selected_field_name=selected_field_name,
            )
            return answer_question(
                identity,  # type: ignore[arg-type]
                request,
                authorization_store=self.auth,
                retrieval_client=retrieval,
                knowledge_base_id="synthetic-kb",
                generator=_GroundedGenerator(),
                guardrail_client=_NoopGuardrail(),
                guardrail_config=GuardrailConfig("synthetic-guardrail", "1"),
                conversation_binding_store=binding_holder["store"],
                correlation_id=correlation_id,
                metadata_repository=self.metadata,
            )

        app, _ = self._http_chat_app(document=document, runs=runs, chat_service=chat_service)
        # The app owns the authoritative binding store; answer_question must
        # receive that same dependency rather than a test-created projection.
        binding_holder["store"] = app.composition.conversation_store

        status, payload = self._http_post(app, {
            "question": "What is the effective date?",
            "matterId": MATTER,
            "conversationId": "conversation-a",
            "sessionId": "session-a",
            "selectedDocumentId": DOCUMENT,
            "selectedFieldName": "effective_date",
        })
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["operationStatus"], "ok")
        self.assertEqual(payload["evidenceStatus"], "answerable")
        retrieval_filter = retrieval.calls[-1]["retrievalConfiguration"]["vectorSearchConfiguration"]["filter"]  # type: ignore[index]
        filter_text = json.dumps(retrieval_filter, sort_keys=True)
        self.assertIn('"key": "documentId"', filter_text)
        self.assertIn('"value": "document-a"', filter_text)
        self.assertIn('"key": "matterId"', filter_text)
        self.assertIn('"value": "matter-a"', filter_text)

        retrieval.return_wrong_document = True
        status, rejected = self._http_post(app, {
            "question": "What is the effective date?",
            "matterId": MATTER,
            "conversationId": "conversation-a",
            "sessionId": "session-a",
            "selectedDocumentId": DOCUMENT,
            "selectedFieldName": "effective_date",
        })
        self.assertEqual(status, 200, rejected)
        self.assertEqual(rejected["operationStatus"], "error")
        self.assertEqual(rejected["citations"], [])

    def test_production_reader_binds_authoritative_bytes_and_rejects_same_key_mutation(self) -> None:
        class MetadataTransport:
            def get_for_scope(self, *, tenant_id: str, matter_id: str, document_id: str):
                return _document() if (tenant_id, matter_id, document_id) == (TENANT, MATTER, DOCUMENT) else None

        class S3Transport:
            def __init__(self) -> None:
                self.body = SOURCE_BYTES

            def head_object(self, *, Bucket: str, Key: str):
                return {"ContentLength": len(self.body), "Metadata": {"legaldesk-sha256": hashlib.sha256(self.body).hexdigest()}}

            def get_object(self, *, Bucket: str, Key: str, Range: str):
                return {"Body": io.BytesIO(self.body)}

        job = IDPJob(
            job_id="reader-job", tenant_id=TENANT, matter_id=MATTER, document_id=DOCUMENT,
            document_sha256=SOURCE_SHA, idempotency_key="reader-idempotency", status=IDPJobStatus.REVIEW_REQUIRED,
            checkpoint=IDPCheckpoint.DONE, model_id="synthetic-model", prompt_version="synthetic-prompt",
        )
        s3 = S3Transport()
        reader = Boto3IDPDocumentReader(MetadataTransport(), s3, bucket_name="synthetic-source", max_bytes=20 * 1024 * 1024)
        authoritative = reader(tenant_id=TENANT, matter_id=MATTER, document_id=DOCUMENT)
        self.assertIsNotNone(authoritative)
        resolved, body = reader.read(job=job, document=authoritative)
        self.assertEqual(body, SOURCE_BYTES)
        self.assertEqual(resolved.content_sha256, SOURCE_SHA)

        # The source key and byte length remain unchanged, but the server-owned
        # hash changes.  A current lookup must not reuse the old run identity.
        s3.body = b"x" * len(SOURCE_BYTES)
        with self.assertRaises(IDPContractError):
            reader.read(job=job, document=authoritative)

    def test_boto_decision_adapter_uses_scoped_query_and_chronological_resolution(self) -> None:
        class DecisionTable:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []
                self.items: list[dict[str, object]] = []

            def query(self, **kwargs: object):
                self.calls.append(kwargs)
                self.assert_query_shape(kwargs)
                ordered = sorted(self.items, key=lambda item: str(item["sk"]), reverse=not kwargs.get("ScanIndexForward", True))
                start = 0
                cursor = kwargs.get("ExclusiveStartKey")
                if isinstance(cursor, dict):
                    matching = [index for index, item in enumerate(ordered) if item["pk"] == cursor.get("pk") and item["sk"] == cursor.get("sk")]
                    if len(matching) != 1:
                        raise AssertionError("pagination cursor did not identify one item")
                    start = matching[0] + 1
                limit = int(kwargs["Limit"])
                page = ordered[start:start + limit]
                response: dict[str, object] = {"Items": page}
                if start + limit < len(ordered):
                    last = page[-1]
                    response["LastEvaluatedKey"] = {"pk": last["pk"], "sk": last["sk"]}
                return response

            @staticmethod
            def assert_query_shape(kwargs: dict[str, object]) -> None:
                if kwargs.get("ScanIndexForward") is not False:
                    raise AssertionError("decision history must query newest-first")
                if kwargs.get("ConsistentRead") is not True:
                    raise AssertionError("decision history must be strongly consistent")

        base = datetime(2024, 3, 1, tzinfo=timezone.utc)
        run = _run("decision-run", created_at=base)
        context = build_request_context(_gateway_identity_from_verified_subject(SUBJECT), MATTER, self.auth, correlation_id=CORRELATION)
        first = self.review_service.record_human_decision(
            context=context, run=run, field_name="effective_date", action=IDPDecisionAction.APPROVE,
            proposed=None, result=None, evidence=run.fields["effective_date"].evidence, reason="first",
        )
        first = replace(first, created_at=base)
        second = replace(first, decision_id="decision-later", created_at=base + timedelta(days=1), reason="later")
        third = replace(first, decision_id="decision-newest", created_at=base + timedelta(days=2), reason="newest")
        fourth = replace(first, decision_id="decision-middle", created_at=base - timedelta(days=1), reason="middle")
        table = DecisionTable()
        from legaldesk.idp.review import Boto3DynamoIDPDecisionRepository
        # Deliberately scramble fixture storage order; the fake applies the
        # same key-order/pagination semantics as Dynamo rather than masking an
        # adapter that merely trusts preordered test data.
        table.items = [
            Boto3DynamoIDPDecisionRepository._item(first),
            Boto3DynamoIDPDecisionRepository._item(third),
            Boto3DynamoIDPDecisionRepository._item(fourth),
            Boto3DynamoIDPDecisionRepository._item(second),
        ]
        adapter = Boto3DynamoIDPDecisionRepository("synthetic-decisions", table=table)
        decisions = adapter.list_decisions(tenant_id=TENANT, matter_id=MATTER, document_id=DOCUMENT, field_name="effective_date", limit=2)
        self.assertEqual([item.decision_id for item in decisions], ["decision-newest", "decision-later"])
        self.assertEqual(len(table.calls), 2)
        self.assertEqual(table.calls[0]["Limit"], 2)
        self.assertEqual(table.calls[0]["ScanIndexForward"], False)
        self.assertIn("ExclusiveStartKey", table.calls[1])
        expression = table.calls[0]["KeyConditionExpression"].get_expression()
        self.assertEqual(expression["operator"], "AND")
        self.assertEqual(expression["values"][0].get_expression()["values"][1], "TENANT#tenant-a#MATTER#matter-a")
        self.assertEqual(expression["values"][1].get_expression()["values"][1], "IDP#DECISION#document-a#effective_date#")

    def test_real_reconciliation_closure_recovers_oldest_same_document_run_and_fair_next_document(self) -> None:
        class PagedMetadata(_RecoveryMetadata):
            def list_for_scope_page(self, *, tenant_id: str, matter_id: str, limit: int, cursor: str | None = None):
                self.page_calls += 1
                start = int(cursor) if cursor is not None else 0
                selected = tuple(item for item in self.documents if item.tenant_id == tenant_id and item.matter_id == matter_id)
                page = selected[start:start + limit]
                next_cursor = str(start + limit) if start + limit < len(selected) else None
                return page, next_cursor

        # One document has three chronological runs: the newest two are
        # already delivered, while the oldest remains pending.  A second
        # document proves that bounded document pagination remains fair.
        documents = tuple(replace(_document(), document_id=f"document-{index}") for index in range(2))
        metadata = PagedMetadata(documents)
        repository = InMemoryIDPRepository()
        base = datetime(2024, 2, 1, tzinfo=timezone.utc)
        run_specs = (
            ("recovery-oldest", documents[0], base, "PENDING"),
            ("recovery-middle", documents[0], base + timedelta(days=1), "SENT"),
            ("recovery-newest", documents[0], base + timedelta(days=2), "SENT"),
            ("recovery-other", documents[1], base + timedelta(days=3), "PENDING"),
        )
        for run_id, document, created_at, delivery_state in run_specs:
            run = _run(
                run_id, created_at=created_at, document_id=document.document_id,
            )
            status = IDPJobStatus.REVIEW_REQUIRED
            run = replace(run, status=status)
            job = IDPJob(
                job_id=run_id, tenant_id=TENANT, matter_id=MATTER,
                document_id=document.document_id, document_sha256=SOURCE_SHA,
                idempotency_key=f"{run_id}-idempotency", schema_version="1.0.0",
                model_id="synthetic-model", prompt_version="synthetic-prompt", status=status,
                checkpoint=IDPCheckpoint.DONE, review_delivery_state=delivery_state,
                created_at=run.created_at, updated_at=run.created_at,
            )
            repository.create_job(job)
            repository.save_run(run)

        class TokenProvider:
            def token(self) -> str:
                return _token(client="synthetic-idp-machine", scope=IDP_REVIEW_SCOPE)

        transport = _ReviewTransport()
        class RecoveryStorage:
            def head_object(self, *, key: str):
                return {"ContentLength": -1, "Metadata": {}}

        recovery_storage = RecoveryStorage()
        config = ReconciliationConfig(
            region="eu-west-1", table_name="synthetic-table", source_bucket="synthetic-source",
            beta_tenant_id=TENANT, upload_scopes=(ReconciliationScope(TENANT, MATTER),), ingestion_scopes=(),
            upload_stale_seconds=3600, ingestion_stale_seconds=3600, limit_per_scope=1,
            idp_enabled=True, idp_recovery_limit_per_scope=1,
        )
        sdk_resource = type("Resource", (), {"Table": lambda _self, _name: object()})()

        def sdk_client(_name: str, **_kwargs: object) -> object:
            return object()

        with patch.dict("os.environ", {
            "LEGALDESK_IDP_REVIEW_ENABLED": "true", "LEGALDESK_IDP_GATEWAY_URL": "https://gateway.example.com",
            "LEGALDESK_IDP_TOKEN_ENDPOINT": "https://token.example.com", "LEGALDESK_IDP_M2M_CLIENT_ID": "synthetic-idp-machine",
            "LEGALDESK_IDP_M2M_SECRET_PARAMETER_NAME": "synthetic-secret", "LEGALDESK_IDP_REVIEW_SCOPE": IDP_REVIEW_SCOPE,
            "LEGALDESK_IDP_QUEUE_URL": "https://sqs.example.com/queue", "LEGALDESK_IDP_QUEUE_ARN": "arn:aws:sqs:eu-west-1:111122223333:idp",
            "LEGALDESK_IDP_MODEL_ID": "synthetic-model", "LEGALDESK_IDP_PROMPT_VERSION": "1.0.0",
            "LEGALDESK_KNOWLEDGE_BASE_ID": "synthetic-kb", "LEGALDESK_DATA_SOURCE_ID": "synthetic-ds",
        }), patch("boto3.resource", return_value=sdk_resource, create=True), patch("boto3.client", side_effect=sdk_client, create=True), patch(
            "legaldesk.reconciliation_lambda.Boto3DynamoDocumentMetadataRepository", return_value=metadata
        ), patch("legaldesk.reconciliation_lambda.Boto3S3ObjectStorage", return_value=recovery_storage), patch(
            "legaldesk.reconciliation_lambda.DynamoDBEphemeralStateStore", return_value=object()
        ), patch("legaldesk.reconciliation_lambda.AsyncKnowledgeBaseIngestionService", return_value=object()), patch(
            "legaldesk.reconciliation_lambda.Boto3DynamoIDPRepository", return_value=repository
        ), patch("legaldesk.reconciliation_lambda.Boto3IDPSQSQueue", return_value=object()), patch(
            "legaldesk.reconciliation_lambda.Boto3DynamoGatewayGrantRepository", return_value=self.grants
        ), patch("legaldesk.idp.review.CognitoM2MTokenProvider", return_value=TokenProvider()), patch(
            "legaldesk.gateway_client.UrllibGatewayTransport", return_value=transport
        ):
            service = _build_service(config)
            reports = [service.reconcile_idp_scopes(scopes=config.upload_scopes, limit_per_scope=1) for _ in range(4)]

        self.assertEqual(
            [report.changed for report in reports], [1, 1, 0, 0],
            {"reports": reports, "metadataPages": metadata.page_calls, "transportCalls": transport.calls,
             "invocations": self.grants.idp_invocations,
             "jobs": [repository.get_job(run_id) for run_id, *_ in run_specs]},
        )
        self.assertEqual(transport.calls, 2)
        self.assertEqual(metadata.page_calls, 4)
        self.assertEqual(repository.get_job("recovery-oldest").review_delivery_state, "SENT")
        self.assertEqual(repository.get_job("recovery-middle").review_delivery_state, "SENT")
        self.assertEqual(repository.get_job("recovery-newest").review_delivery_state, "SENT")
        self.assertEqual(repository.get_job("recovery-other").review_delivery_state, "SENT")


if __name__ == "__main__":
    unittest.main()
