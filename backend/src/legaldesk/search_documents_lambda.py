"""``search_documents`` Gateway Lambda target for the agentic chat (ADR-021 D2).

It runs the grounded pipeline that ``chat.answer_question`` already runs for the
direct RAG path (Guardrail input -> Knowledge Base Retrieve -> evidence
resolver -> answer writer -> grounding Guardrail -> validated citations) and
returns the structured result, citations included.

Boundaries:

* Scope comes only from the short-lived grant the Gateway interceptor wrote for
  this exact call. ``matterId`` and any other model-supplied selector are
  discarded; subject and matter are reauthorized against the authorization
  store inside ``answer_question``.
* It owns a separate Lambda so the Knowledge Base, model and Guardrail IAM does
  not widen the metadata MCP Lambda.
* It writes nothing: no memory, no conversation state, no citation handles. The
  worker that owns the conversation creates handles from the returned citations.
* Its time budget is the Lambda timeout (25 s); SDK retries are disabled.
"""

from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping
from uuid import UUID

from . import agent_contracts
from .authorization import (
    AuthorizationDenied,
    AuthorizationStore,
    RequestContext,
    _gateway_identity_from_verified_subject,
)
from .chat import (
    ChatRequest,
    ChatResponse,
    answer_question,
)
from .gateway_interceptor import (
    GATEWAY_GRANT_ENTITY,
    GATEWAY_GRANT_SORT_KEY,
    Boto3DynamoGatewayGrantRepository,
    GatewayGrantRepository,
    gateway_grant_partition_key,
)
from .observability import (
    TelemetryEventType,
    TelemetryOutcome,
    TelemetrySink,
    emit_telemetry,
)
from .prompts import FileSystemSystemPromptProvider, SystemPromptProvider

LEGALDESK_GRANT_ARGUMENT = "_legaldeskGrantId"
SEARCH_TOOL_BUDGET_SECONDS = 25
_ALLOWED_ARGUMENTS = frozenset({"question", "matterId", LEGALDESK_GRANT_ARGUMENT})
_GRANT_KEYS = frozenset({
    "pk", "sk", "entityType", "verifiedSubject", "requestedMatterId",
    "correlationId", "toolName", "expiresAt",
})


class _NoLegacyGenerator:
    def generate(self, _request: Any) -> Mapping[str, object]:
        raise RuntimeError("the separated resolver/writer pipeline is required")


class _GrantScopedConversation:
    """Conversation binding derived from the grant, not from the browser.

    ``answer_question`` insists on a conversation binding. This call has no
    browser conversation: the only scope is the grant, so the "conversation" is
    a synthetic id derived from the grant's correlation id and nothing is
    stored. It still refuses any context for a different matter.
    """

    def __init__(self, *, matter_id: str, conversation_id: str) -> None:
        self._matter_id = matter_id
        self._conversation_id = conversation_id

    def is_bound(self, *, context: RequestContext, conversation_id: str, session_selector: str) -> bool:
        return (
            context.matter_id == self._matter_id
            and conversation_id == self._conversation_id
            and session_selector == self._conversation_id
        )


@dataclass(frozen=True, slots=True)
class SearchDocumentsService:
    """Provider seams of the grounded pipeline (all injectable for tests)."""

    authorization_store: AuthorizationStore
    retrieval_client: Any
    knowledge_base_id: str
    guardrail_client: Any
    guardrail_config: Any
    evidence_resolver: Any
    answer_writer: Any
    grounding_validator: Any
    prompt_provider: SystemPromptProvider
    metadata_repository: Any
    object_storage: Any
    telemetry_sink: TelemetrySink | None = None


def _result(response: ChatResponse, passages: Mapping[str, str]) -> dict[str, object]:
    """Map a pipeline response to the ``search_documents`` result contract."""

    if response.operation_status != "ok" or response.evidence_status is None:
        # Blocked and technical outcomes carry only the fixed backend sentence,
        # never citations; the validator will not treat them as a valid result.
        return {
            "answer": response.answer, "evidenceStatus": None,
            "operationStatus": "blocked" if response.operation_status == "blocked" else "error",
            "citations": [],
        }
    citations: list[dict[str, object]] = []
    for citation in response.citations:
        passage = passages.get(citation.citation_id)
        if passage is None:
            return {
                "answer": response.answer, "evidenceStatus": None,
                "operationStatus": "error", "citations": [],
            }
        citations.append({
            "citationId": citation.citation_id, "documentId": citation.document_id,
            "documentName": citation.document_name, "pageNumber": citation.page_number,
            "section": citation.section,
            "passage": passage[: agent_contracts.MAX_RESULT_PASSAGE_CHARS],
        })
    return {
        "answer": response.answer, "evidenceStatus": response.evidence_status.value,
        "operationStatus": "ok", "citations": citations,
    }


def run_search_documents(
    service: SearchDocumentsService,
    *,
    subject: str,
    matter_id: str,
    correlation_id: str,
    question: str,
) -> dict[str, object]:
    """Run the grounded pipeline for one grant-scoped question."""

    started_at = time.perf_counter()
    emit_telemetry(
        service.telemetry_sink, TelemetryEventType.TOOL, correlation_id, TelemetryOutcome.STARTED,
        operation="search_documents",
    )
    conversation_id = f"search-{correlation_id}"
    passages: dict[str, str] = {}

    def collect(_identity: Any, _context: Any, _request: Any, _response: Any, selected: tuple[Any, ...]) -> None:
        # Not persisted: handed back to the caller inside the tool result.
        for passage in selected:
            passages[passage.citation.citation_id] = passage.text

    response = answer_question(
        _gateway_identity_from_verified_subject(subject),
        ChatRequest(conversation_id, conversation_id, matter_id, question),
        authorization_store=service.authorization_store,
        retrieval_client=service.retrieval_client,
        knowledge_base_id=service.knowledge_base_id,
        generator=_NoLegacyGenerator(),
        guardrail_client=service.guardrail_client,
        guardrail_config=service.guardrail_config,
        conversation_binding_store=_GrantScopedConversation(matter_id=matter_id, conversation_id=conversation_id),  # type: ignore[arg-type]
        correlation_id=correlation_id,
        evidence_resolver=service.evidence_resolver,
        answer_writer=service.answer_writer,
        grounding_validator=service.grounding_validator,
        prompt_provider=service.prompt_provider,
        telemetry_sink=service.telemetry_sink,
        authorized_evidence_sink=collect,
        metadata_repository=service.metadata_repository,
        object_storage=service.object_storage,
    )
    result = _result(response, passages)
    emit_telemetry(
        service.telemetry_sink, TelemetryEventType.TOOL,
        correlation_id,
        TelemetryOutcome.SUCCEEDED if result["operationStatus"] == "ok" else TelemetryOutcome.ERROR,
        started_at=started_at, operation="search_documents", count=len(result["citations"]),  # type: ignore[arg-type]
    )
    return result


def _validated_grant(repository: GatewayGrantRepository, grant_id: str) -> tuple[str, str, str] | None:
    """Load one exact, unexpired grant issued for ``search_documents``."""

    item = repository.get(grant_id)
    if not isinstance(item, Mapping) or set(item) not in (_GRANT_KEYS, _GRANT_KEYS | {"ttl"}):
        return None
    if (
        item.get("pk") != gateway_grant_partition_key(grant_id)
        or item.get("sk") != GATEWAY_GRANT_SORT_KEY
        or item.get("entityType") != GATEWAY_GRANT_ENTITY
        or item.get("toolName") != agent_contracts.SEARCH_DOCUMENTS
    ):
        return None
    expires_at = item.get("expiresAt")
    subject, matter_id, correlation_id = (
        item.get("verifiedSubject"), item.get("requestedMatterId"), item.get("correlationId"),
    )
    if (
        isinstance(expires_at, bool) or not isinstance(expires_at, (int, float, Decimal))
        or not math.isfinite(float(expires_at)) or float(expires_at) <= time.time()
        or not all(isinstance(value, str) and value for value in (subject, matter_id, correlation_id))
    ):
        return None
    try:
        UUID(correlation_id)  # type: ignore[arg-type]
    except ValueError:
        return None
    return subject, matter_id, correlation_id  # type: ignore[return-value]


def _service_from_environment() -> tuple[SearchDocumentsService, GatewayGrantRepository] | None:
    """Build the pipeline from the Lambda environment; ``None`` if unconfigured."""

    names = (
        "DOCUMENT_METADATA_TABLE_NAME", "LEGALDESK_SOURCE_BUCKET", "LEGALDESK_KNOWLEDGE_BASE_ID",
        "LEGALDESK_GUARDRAIL_ID", "LEGALDESK_GUARDRAIL_VERSION", "LEGALDESK_RESOLVER_MODEL_ID",
        "LEGALDESK_WRITER_MODEL_ID",
    )
    values = {name: os.environ.get(name, "").strip() for name in names}
    if not all(values.values()):
        return None
    import boto3
    from botocore.config import Config

    from .authorization import Boto3DynamoAuthorizationStore
    from .aws_cache import dynamodb_table
    from .documents import Boto3DynamoDocumentMetadataRepository, Boto3S3ObjectStorage
    from .evidence import ConverseAnswerWriter, ConverseEvidenceResolver
    from .guardrails import GuardrailConfig, GuardrailGroundingValidator

    region = os.environ.get("AWS_REGION", "eu-west-1")
    # No SDK retries and a read timeout inside the 25 s tool budget.
    sdk = Config(retries={"total_max_attempts": 1, "mode": "standard"}, read_timeout=20, connect_timeout=3)
    runtime = boto3.client("bedrock-runtime", region_name=region, config=sdk)
    table_name = values["DOCUMENT_METADATA_TABLE_NAME"]
    table = dynamodb_table(table_name)
    prompt_path = os.environ.get("LEGALDESK_SYSTEM_PROMPT_PATH", "").strip()
    guardrail_config = GuardrailConfig(values["LEGALDESK_GUARDRAIL_ID"], values["LEGALDESK_GUARDRAIL_VERSION"])
    service = SearchDocumentsService(
        authorization_store=Boto3DynamoAuthorizationStore(table_name, table=table),
        retrieval_client=boto3.client("bedrock-agent-runtime", region_name=region, config=sdk),
        knowledge_base_id=values["LEGALDESK_KNOWLEDGE_BASE_ID"],
        guardrail_client=runtime,
        guardrail_config=guardrail_config,
        evidence_resolver=ConverseEvidenceResolver(runtime, model_id=values["LEGALDESK_RESOLVER_MODEL_ID"]),
        answer_writer=ConverseAnswerWriter(runtime, model_id=values["LEGALDESK_WRITER_MODEL_ID"]),
        grounding_validator=GuardrailGroundingValidator(runtime, guardrail_config, minimum_score=0.75),
        prompt_provider=(
            FileSystemSystemPromptProvider(Path(prompt_path)) if prompt_path else FileSystemSystemPromptProvider()
        ),
        metadata_repository=Boto3DynamoDocumentMetadataRepository(table_name, table=table, boto3_backed=True),
        object_storage=Boto3S3ObjectStorage(
            values["LEGALDESK_SOURCE_BUCKET"], client=boto3.client("s3", region_name=region, config=sdk),
        ),
    )
    return service, Boto3DynamoGatewayGrantRepository(table_name, table=table)


def search_documents_lambda_handler(event: Mapping[str, object], _lambda_context: object) -> dict[str, object]:
    """AgentCore Lambda-target entry point; flat tool arguments plus a grant id."""

    if not isinstance(event, Mapping):
        return {"error": "invalid_request"}
    try:
        grant_id = event.get(LEGALDESK_GRANT_ARGUMENT)
        if not isinstance(grant_id, str) or "authorizedContext" in event:
            return {"error": "access_denied"}
        UUID(grant_id)
        if set(event) - _ALLOWED_ARGUMENTS:
            return {"error": "invalid_request"}
        question = event.get("question")
        if (
            not isinstance(question, str) or not question.strip()
            or len(question) > agent_contracts.MAX_SEARCH_QUESTION_LENGTH
        ):
            return {"error": "invalid_request"}
        built = _service_from_environment()
        if built is None:
            return {"error": "service_unavailable"}
        service, grants = built
        scope = _validated_grant(grants, grant_id)
        if scope is None:
            return {"error": "access_denied"}
        subject, matter_id, correlation_id = scope
        # ``matterId`` from the event is a Gateway schema selector only.
        return run_search_documents(
            service, subject=subject, matter_id=matter_id, correlation_id=correlation_id, question=question.strip(),
        )
    except (ValueError, TypeError):
        return {"error": "invalid_request"}
    except AuthorizationDenied:
        return {"error": "access_denied"}
    except Exception:
        return {"error": "service_unavailable"}


__all__ = [
    "SEARCH_TOOL_BUDGET_SECONDS",
    "SearchDocumentsService",
    "run_search_documents",
    "search_documents_lambda_handler",
]
