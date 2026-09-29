"""Provider-neutral retrieve-then-generate orchestration for Phase 05.

The generation boundary receives a server-loaded versioned system prompt, the
user's question, and bounded passage text. It has no authorization scope,
browser selectors, retrieval filters, or provider credentials.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, replace
from typing import Callable, Mapping, Protocol

from .authorization import (
    AuthorizationDenied,
    AuthorizationStore,
    RequestContext,
    VerifiedIdentity,
    build_request_context,
)
from .memory import ConversationBindingStore
from .documents import DocumentMetadataRepository
from .observability import (
    TelemetryEventType,
    TelemetryOutcome,
    TelemetrySink,
    emit_telemetry,
)
from .guardrails import (
    BedrockGuardrailClient,
    GuardrailAuditSink,
    GuardrailConfig,
    GroundingGuardrailBlocked,
    GroundingGuardrailError,
    GuardrailProcessor,
    GuardrailStage,
)
from .prompts import (
    DEFAULT_SYSTEM_PROMPT_PROVIDER,
    PromptConfigurationError,
    SystemPromptArtifact,
    SystemPromptProvider,
)
from .retrieval import (
    MAX_QUERY_LENGTH,
    BedrockKnowledgeBaseClient,
    Citation,
    RetrievedPassage,
    _retrieve_with_context,
    ObjectHeadStorage,
)
from .evidence import (
    AnswerWriter,
    AnswerWriterRequest,
    EvidenceContractError,
    GroundingContractError,
    EvidenceResolutionRequest,
    EvidenceResolver,
    EvidenceStatus,
    EVIDENCE_RESOLVER_PROMPT_SHA256,
    EVIDENCE_RESOLVER_PROMPT_VERSION,
    GroundingRequest,
    GroundingValidator,
    legacy_resolution_from_generation,
    validate_evidence_resolution,
    validate_grounding_result,
    validate_answer_writer_result,
    evidence_requires_relationship_projection,
    validate_writer_relationships_against_evidence,
    render_writer_relationships,
    ANSWER_WRITER_PROMPT_SHA256,
    ANSWER_WRITER_PROMPT_VERSION,
)


MAX_OPAQUE_ID_LENGTH = 128
MAX_ANSWER_LENGTH = 8_000
GENERATION_RESPONSE_FIELDS = ("answer", "citationIds", "evidenceStatus")
_OPAQUE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
INSUFFICIENT_EVIDENCE_ANSWER = (
    "No se encontró evidencia suficiente en los documentos autorizados para responder."
)
GUARDRAIL_INPUT_BLOCKED_ANSWER = (
    "No puedo procesar esta solicitud porque infringe una política de seguridad."
)
GUARDRAIL_OUTPUT_BLOCKED_ANSWER = (
    "No puedo mostrar esta respuesta porque infringe una política de seguridad."
)
TECHNICAL_ERROR_ANSWER = (
    "No se pudo completar la solicitud por un problema operativo. Inténtalo de nuevo más tarde."
)


def _valid_selector(value: object, *, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_OPAQUE_ID_LENGTH
        or _OPAQUE_ID.fullmatch(value) is None
    ):
        raise ValueError(f"{field_name} must be a valid opaque selector")
    return value


@dataclass(frozen=True, slots=True)
class ChatRequest:
    """Validated browser input. Matter remains an untrusted selector."""

    conversation_id: str
    session_id: str
    matter_id: str
    question: str

    def __post_init__(self) -> None:
        _valid_selector(self.conversation_id, field_name="conversationId")
        _valid_selector(self.session_id, field_name="sessionId")
        _valid_selector(self.matter_id, field_name="matterId")
        if (
            not isinstance(self.question, str)
            or not self.question.strip()
            or len(self.question) > MAX_QUERY_LENGTH
        ):
            raise ValueError("question is empty or outside the allowed length")


def parse_chat_request(payload: Mapping[str, object]) -> ChatRequest:
    """Parse JSON-shaped browser input and reject unrecognized scope fields."""

    if not isinstance(payload, Mapping):
        raise ValueError("chat request must be an object")
    expected = {"conversationId", "sessionId", "matterId", "question"}
    if set(payload) != expected:
        raise ValueError("chat request has missing or unsupported fields")
    return ChatRequest(
        conversation_id=payload["conversationId"],  # type: ignore[arg-type]
        session_id=payload["sessionId"],  # type: ignore[arg-type]
        matter_id=payload["matterId"],  # type: ignore[arg-type]
        question=payload["question"],  # type: ignore[arg-type]
    )


@dataclass(frozen=True, slots=True)
class GenerationEvidence:
    """Minimum generation input: a retrieval-issued citation ID and raw text."""

    citation_id: str
    text: str


@dataclass(frozen=True, slots=True)
class GenerationRequest:
    question: str
    evidence: tuple[GenerationEvidence, ...]
    system_prompt: SystemPromptArtifact


class TextGenerator(Protocol):
    def generate(self, request: GenerationRequest) -> Mapping[str, object]: ...


@dataclass(frozen=True, slots=True)
class ChatCitation:
    citation_id: str
    document_id: str
    document_name: str | None
    source_uri: str | None
    page_number: int | None
    section: str | None

    @classmethod
    def from_retrieved(cls, citation: Citation) -> ChatCitation:
        return cls(
            citation_id=citation.citation_id,
            document_id=citation.document_id,
            document_name=citation.document_name,
            source_uri=citation.source_uri,
            page_number=citation.page_number,
            section=citation.section,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "citationId": self.citation_id,
            "documentId": self.document_id,
            "documentName": self.document_name,
            "pageNumber": self.page_number,
            "section": self.section,
        }


@dataclass(frozen=True, slots=True)
class ChatResponse:
    answer: str
    citations: tuple[ChatCitation, ...]
    evidence_status: EvidenceStatus | None
    disclaimer_required: bool
    prompt_version: str | None
    prompt_sha256: str | None
    correlation_id: str | None = None
    resolver_prompt_version: str | None = None
    resolver_prompt_sha256: str | None = None
    writer_prompt_version: str | None = None
    writer_prompt_sha256: str | None = None
    error_code: str | None = None
    operation_status: str = "ok"

    def to_dict(self) -> dict[str, object]:
        return {
            "answer": self.answer,
            "citations": [citation.to_dict() for citation in self.citations],
            "evidenceStatus": self.evidence_status.value if self.evidence_status else None,
            "disclaimerRequired": self.disclaimer_required,
            "promptVersion": self.prompt_version,
            "promptSha256": self.prompt_sha256,
            "correlationId": self.correlation_id,
            "resolverPromptVersion": self.resolver_prompt_version,
            "resolverPromptSha256": self.resolver_prompt_sha256,
            "writerPromptVersion": self.writer_prompt_version,
            "writerPromptSha256": self.writer_prompt_sha256,
            "errorCode": self.error_code,
            "operationStatus": self.operation_status,
        }


def insufficient_evidence_response(
    prompt_artifact: SystemPromptArtifact | None = None,
    *,
    correlation_id: str | None = None,
) -> ChatResponse:
    return ChatResponse(
        answer=INSUFFICIENT_EVIDENCE_ANSWER,
        citations=(),
        evidence_status=EvidenceStatus.INSUFFICIENT_EVIDENCE,
        disclaimer_required=True,
        prompt_version=prompt_artifact.version if prompt_artifact else None,
        prompt_sha256=prompt_artifact.sha256 if prompt_artifact else None,
        correlation_id=correlation_id,
    )


def guardrail_blocked_response(
    stage: GuardrailStage,
    prompt_artifact: SystemPromptArtifact,
    *,
    correlation_id: str | None = None,
) -> ChatResponse:
    answer = (
        GUARDRAIL_INPUT_BLOCKED_ANSWER
        if stage is GuardrailStage.INPUT
        else GUARDRAIL_OUTPUT_BLOCKED_ANSWER
    )
    return ChatResponse(
        answer=answer,
        citations=(),
        # A policy block is an operational/security outcome, not a claim
        # about documentary coverage. Keep it distinct from the three-value
        # evidence contract consumed by clients.
        evidence_status=None,
        disclaimer_required=True,
        prompt_version=prompt_artifact.version,
        prompt_sha256=prompt_artifact.sha256,
        correlation_id=correlation_id,
        operation_status="blocked",
    )


def technical_error_response(
    prompt_artifact: SystemPromptArtifact | None = None,
    *,
    correlation_id: str | None = None,
    error_code: str = "service_unavailable",
    separated_pipeline: bool = False,
) -> ChatResponse:
    return ChatResponse(
        answer=TECHNICAL_ERROR_ANSWER,
        citations=(),
        evidence_status=None,
        disclaimer_required=True,
        prompt_version=prompt_artifact.version if prompt_artifact else None,
        prompt_sha256=prompt_artifact.sha256 if prompt_artifact else None,
        correlation_id=correlation_id,
        resolver_prompt_version=(EVIDENCE_RESOLVER_PROMPT_VERSION if separated_pipeline else None),
        resolver_prompt_sha256=(EVIDENCE_RESOLVER_PROMPT_SHA256 if separated_pipeline else None),
        writer_prompt_version=(ANSWER_WRITER_PROMPT_VERSION if separated_pipeline else None),
        writer_prompt_sha256=(ANSWER_WRITER_PROMPT_SHA256 if separated_pipeline else None),
        error_code=error_code,
        operation_status="error",
    )


def _validate_generation_response(
    result: Mapping[str, object],
    passages: tuple[RetrievedPassage, ...],
    prompt_artifact: SystemPromptArtifact,
) -> ChatResponse:
    if set(result) != set(GENERATION_RESPONSE_FIELDS):
        return technical_error_response(prompt_artifact, error_code="model_failed")
    answer = result.get("answer")
    raw_citation_ids = result.get("citationIds")
    if (
        not isinstance(answer, str)
        or not answer.strip()
        or len(answer) > MAX_ANSWER_LENGTH
        or not isinstance(raw_citation_ids, (list, tuple))
        or any(not isinstance(item, str) for item in raw_citation_ids)
    ):
        return technical_error_response(prompt_artifact, error_code="model_failed")
    retrieved = {passage.citation.citation_id: passage.citation for passage in passages}
    citation_ids = tuple(raw_citation_ids)
    if (
        len(citation_ids) != len(set(citation_ids))
        or any(citation_id not in retrieved for citation_id in citation_ids)
    ):
        return technical_error_response(prompt_artifact, error_code="model_failed")
    if not citation_ids:
        try:
            resolution = legacy_resolution_from_generation(
                result,
                tuple(retrieved),
            )
        except (EvidenceContractError, GroundingContractError):
            return technical_error_response(prompt_artifact, error_code="model_failed")
        if resolution.evidence_status is not EvidenceStatus.INSUFFICIENT_EVIDENCE:
            return technical_error_response(prompt_artifact, error_code="model_failed")
        return insufficient_evidence_response(prompt_artifact)

    try:
        resolution = legacy_resolution_from_generation(
            result,
            tuple(retrieved),
        )
    except (EvidenceContractError, GroundingContractError):
        return technical_error_response(prompt_artifact, error_code="model_failed")

    return ChatResponse(
        answer=answer.strip(),
        citations=tuple(
            ChatCitation.from_retrieved(retrieved[citation_id])
            for citation_id in citation_ids
        ),
        evidence_status=resolution.evidence_status,
        # Every LegalDesk answer needs the product's legal-advice disclaimer.
        disclaimer_required=True,
        prompt_version=prompt_artifact.version,
        prompt_sha256=prompt_artifact.sha256,
    )


def answer_question(
    identity: VerifiedIdentity,
    request: ChatRequest,
    *,
    authorization_store: AuthorizationStore,
    retrieval_client: BedrockKnowledgeBaseClient,
    knowledge_base_id: str,
    generator: TextGenerator,
    guardrail_client: BedrockGuardrailClient,
    guardrail_config: GuardrailConfig,
    conversation_binding_store: ConversationBindingStore,
    correlation_id: str | None = None,
    prompt_provider: SystemPromptProvider = DEFAULT_SYSTEM_PROMPT_PROVIDER,
    guardrail_audit_sink: GuardrailAuditSink | None = None,
    telemetry_sink: TelemetrySink | None = None,
    evidence_resolver: EvidenceResolver | None = None,
    answer_writer: AnswerWriter | None = None,
    grounding_validator: GroundingValidator | None = None,
    authorized_evidence_sink: Callable[[VerifiedIdentity, RequestContext, ChatRequest, ChatResponse, tuple[RetrievedPassage, ...]], None] | None = None,
    metadata_repository: DocumentMetadataRepository | None = None,
    object_storage: ObjectHeadStorage | None = None,
) -> ChatResponse:
    """Load the server prompt, authorize and retrieve, then generate from evidence."""

    if (
        not isinstance(identity, VerifiedIdentity)
        or not isinstance(identity.subject, str)
        or not identity.subject.strip()
    ):
        raise ValueError("identity must be verified")
    if not isinstance(request, ChatRequest):
        raise TypeError("request must be a validated ChatRequest")
    separated = (evidence_resolver, answer_writer, grounding_validator)
    if any(item is not None for item in separated) and not all(item is not None for item in separated):
        raise TypeError("evidence_resolver, answer_writer and grounding_validator must be supplied together")

    # This deterministic check must run before any Guardrail sees caller input.
    context = build_request_context(
        identity,
        request.matter_id,
        authorization_store,
        correlation_id=correlation_id,
    )
    if not conversation_binding_store.is_bound(
        context=context,
        conversation_id=request.conversation_id,
        session_selector=request.session_id,
    ):
        raise AuthorizationDenied("conversation access denied")

    request_started_at = time.perf_counter()
    retrieved_passages: tuple[RetrievedPassage, ...] = ()
    evidence_sink_called = False
    emit_telemetry(
        telemetry_sink,
        TelemetryEventType.AGENT,
        context.correlation_id,
        TelemetryOutcome.STARTED,
        operation="answer_question",
    )

    def finish(response: ChatResponse) -> ChatResponse:
        nonlocal evidence_sink_called
        if (
            authorized_evidence_sink is not None
            and not evidence_sink_called
            and response.operation_status == "ok"
            and response.citations
            and retrieved_passages
        ):
            evidence_sink_called = True
            selected_ids = {citation.citation_id for citation in response.citations}
            selected_passages = tuple(
                passage for passage in retrieved_passages
                if passage.citation.citation_id in selected_ids
            )
            try:
                authorized_evidence_sink(
                    identity,
                    context,
                    request,
                    response,
                    selected_passages,
                )
            except Exception:
                response = technical_error_response(
                    prompt_artifact if "prompt_artifact" in locals() else None,
                    correlation_id=context.correlation_id,
                    error_code="citation_store_failed",
                    separated_pipeline=all(item is not None for item in (evidence_resolver, answer_writer, grounding_validator)),
                )
        response = replace(response, correlation_id=context.correlation_id)
        final_outcome = (
            TelemetryOutcome.BLOCKED
            if response.answer in {GUARDRAIL_INPUT_BLOCKED_ANSWER, GUARDRAIL_OUTPUT_BLOCKED_ANSWER}
            else TelemetryOutcome.ERROR
            if response.operation_status == "error"
            else TelemetryOutcome.BLOCKED
            if response.operation_status == "blocked"
            else TelemetryOutcome.NOT_FOUND
            if response.evidence_status is EvidenceStatus.INSUFFICIENT_EVIDENCE and not response.citations
            else TelemetryOutcome.SUCCEEDED
        )
        emit_telemetry(
            telemetry_sink,
            TelemetryEventType.AGENT,
            context.correlation_id,
            final_outcome,
            started_at=request_started_at,
            operation="answer_question",
        )
        emit_telemetry(
            telemetry_sink,
            TelemetryEventType.FINAL,
            context.correlation_id,
            final_outcome,
            started_at=request_started_at,
            operation="answer_question",
            count=len(response.citations),
            prompt_version=response.prompt_version,
            prompt_sha256=response.prompt_sha256,
            resolver_prompt_version=response.resolver_prompt_version,
            resolver_prompt_sha256=response.resolver_prompt_sha256,
            writer_prompt_version=response.writer_prompt_version,
            writer_prompt_sha256=response.writer_prompt_sha256,
            error_code=response.error_code,
        )
        return response

    try:
        prompt_artifact = prompt_provider.load()
    except PromptConfigurationError:
        # A missing or malformed server prompt must never fall back to generation.
        return finish(
            technical_error_response(
                correlation_id=context.correlation_id,
                error_code="service_unavailable",
            )
        )

    guardrails = GuardrailProcessor(
        guardrail_client,
        guardrail_config,
        audit_sink=guardrail_audit_sink,
        telemetry_sink=telemetry_sink,
    )
    input_result = guardrails.check_input(
        request.question.strip(), correlation_id=context.correlation_id
    )
    if not input_result.can_proceed:
        if input_result.outcome.value == "error":
            return finish(
                technical_error_response(
                    prompt_artifact,
                    correlation_id=context.correlation_id,
                    error_code="guardrail_error",
                )
            )
        return finish(guardrail_blocked_response(GuardrailStage.INPUT, prompt_artifact, correlation_id=context.correlation_id))
    question = input_result.content[0]
    if len(question) > MAX_QUERY_LENGTH:
        return finish(guardrail_blocked_response(GuardrailStage.INPUT, prompt_artifact, correlation_id=context.correlation_id))

    try:
        passages = _retrieve_with_context(
            context,
            question,
            bedrock_client=retrieval_client,
            knowledge_base_id=knowledge_base_id,
            telemetry_sink=telemetry_sink,
            metadata_repository=metadata_repository,
            object_storage=object_storage,
        )
    except Exception:
        emit_telemetry(
            telemetry_sink,
            TelemetryEventType.AGENT,
            context.correlation_id,
            TelemetryOutcome.ERROR,
            started_at=request_started_at,
            operation="answer_question",
            error_code="retrieval_failed",
        )
        return finish(
            technical_error_response(
                prompt_artifact,
                correlation_id=context.correlation_id,
                error_code="retrieval_failed",
            )
        )
    retrieved_passages = passages
    if not passages:
        return finish(insufficient_evidence_response(prompt_artifact, correlation_id=context.correlation_id))

    generation_request = GenerationRequest(
        question=question,
        system_prompt=prompt_artifact,
        evidence=tuple(
            GenerationEvidence(
                citation_id=passage.citation.citation_id,
                text=passage.text,
            )
            for passage in passages
        ),
    )
    model_started_at = time.perf_counter()
    emit_telemetry(
        telemetry_sink,
        TelemetryEventType.MODEL,
        context.correlation_id,
        TelemetryOutcome.STARTED,
        operation="generate_answer",
    )
    resolved_evidence = None
    stage_in_progress: str | None = None
    try:
        if all(item is not None for item in (evidence_resolver, answer_writer, grounding_validator)):
            stage_in_progress = "resolve_evidence"
            emit_telemetry(
                telemetry_sink,
                TelemetryEventType.MODEL,
                context.correlation_id,
                TelemetryOutcome.STARTED,
                operation=stage_in_progress,
                resolver_prompt_version=EVIDENCE_RESOLVER_PROMPT_VERSION,
                resolver_prompt_sha256=EVIDENCE_RESOLVER_PROMPT_SHA256,
            )
            resolution_raw = evidence_resolver.resolve(
                EvidenceResolutionRequest(
                    question=question,
                    evidence=generation_request.evidence,
                )
            )
            resolution = validate_evidence_resolution(
                resolution_raw,
                tuple(item.citation.citation_id for item in passages),
            )
            resolved_evidence = resolution
            emit_telemetry(
                telemetry_sink,
                TelemetryEventType.MODEL,
                context.correlation_id,
                TelemetryOutcome.SUCCEEDED,
                operation=stage_in_progress,
                count=len(resolution.supporting_citation_ids),
                resolver_prompt_version=EVIDENCE_RESOLVER_PROMPT_VERSION,
                resolver_prompt_sha256=EVIDENCE_RESOLVER_PROMPT_SHA256,
            )
            stage_in_progress = None
            if not resolution.supporting_citation_ids:
                # No passage supports the answer.  Do not spend a writer call
                # and do not let a writer manufacture an answer for none.
                generated = {
                    "answer": INSUFFICIENT_EVIDENCE_ANSWER,
                }
            else:
                stage_in_progress = "write_answer"
                emit_telemetry(
                    telemetry_sink,
                    TelemetryEventType.MODEL,
                    context.correlation_id,
                    TelemetryOutcome.STARTED,
                    operation=stage_in_progress,
                    writer_prompt_version=ANSWER_WRITER_PROMPT_VERSION,
                    writer_prompt_sha256=ANSWER_WRITER_PROMPT_SHA256,
                )
                written = answer_writer.write(
                    AnswerWriterRequest(
                        question=question,
                        evidence=generation_request.evidence,
                        resolution=resolution,
                    )
                )
                relationships: tuple[Mapping[str, str], ...] = ()
                if isinstance(written, Mapping):
                    normalized_written = validate_answer_writer_result(written)
                    answer = normalized_written.get("answer")
                    raw_relationships = normalized_written.get("relationships", ())
                    if not isinstance(raw_relationships, (list, tuple)):
                        raise EvidenceContractError("answer writer relationships are invalid")
                    relationships = tuple(raw_relationships)  # type: ignore[arg-type]
                else:
                    raise EvidenceContractError("answer writer returned an unsupported shape")
                if (
                    not isinstance(answer, str)
                    or not answer.strip()
                    or len(answer) > MAX_ANSWER_LENGTH
                ):
                    raise EvidenceContractError("answer writer returned invalid answer text")
                selected_evidence = tuple(
                    item
                    for item in generation_request.evidence
                    if item.citation_id in resolution.supporting_citation_ids
                )
                if evidence_requires_relationship_projection(selected_evidence) and not relationships:
                    raise EvidenceContractError(
                        "answer writer omitted a required directed relationship"
                    )
                if relationships:
                    relationships = validate_writer_relationships_against_evidence(
                        relationships,
                        selected_evidence,
                    )
                    # Structured relationships are the authoritative public
                    # rendering for directional claims; this prevents prose
                    # from reversing a validated actor/action/recipient.
                    answer = render_writer_relationships(relationships)
                emit_telemetry(
                    telemetry_sink,
                    TelemetryEventType.MODEL,
                    context.correlation_id,
                    TelemetryOutcome.SUCCEEDED,
                    operation=stage_in_progress,
                    count=1,
                    writer_prompt_version=ANSWER_WRITER_PROMPT_VERSION,
                    writer_prompt_sha256=ANSWER_WRITER_PROMPT_SHA256,
                )
                stage_in_progress = None
                grounding_started_at = time.perf_counter()
                emit_telemetry(
                    telemetry_sink,
                    TelemetryEventType.GUARDRAIL,
                    context.correlation_id,
                    TelemetryOutcome.STARTED,
                    operation="grounding_validate",
                    writer_prompt_version=ANSWER_WRITER_PROMPT_VERSION,
                    writer_prompt_sha256=ANSWER_WRITER_PROMPT_SHA256,
                )
                try:
                    grounding_result = grounding_validator.validate(
                        GroundingRequest(
                            question=question,
                            answer=answer.strip(),
                            evidence=selected_evidence,
                            supporting_citation_ids=resolution.supporting_citation_ids,
                            correlation_id=context.correlation_id,
                            relationships=relationships,
                        )
                    )
                    validate_grounding_result(
                        grounding_result,
                        supporting_citation_ids=resolution.supporting_citation_ids,
                    )
                except GroundingGuardrailBlocked:
                    emit_telemetry(
                        telemetry_sink,
                        TelemetryEventType.GUARDRAIL,
                        context.correlation_id,
                        TelemetryOutcome.BLOCKED,
                        started_at=grounding_started_at,
                        operation="grounding_validate",
                        writer_prompt_version=ANSWER_WRITER_PROMPT_VERSION,
                        writer_prompt_sha256=ANSWER_WRITER_PROMPT_SHA256,
                    )
                    raise
                except GroundingGuardrailError:
                    emit_telemetry(
                        telemetry_sink,
                        TelemetryEventType.GUARDRAIL,
                        context.correlation_id,
                        TelemetryOutcome.ERROR,
                        started_at=grounding_started_at,
                        operation="grounding_validate",
                        error_code="guardrail_error",
                        writer_prompt_version=ANSWER_WRITER_PROMPT_VERSION,
                        writer_prompt_sha256=ANSWER_WRITER_PROMPT_SHA256,
                    )
                    raise
                except GroundingContractError:
                    emit_telemetry(
                        telemetry_sink,
                        TelemetryEventType.GUARDRAIL,
                        context.correlation_id,
                        TelemetryOutcome.ERROR,
                        started_at=grounding_started_at,
                        operation="grounding_validate",
                        error_code="model_failed",
                        writer_prompt_version=ANSWER_WRITER_PROMPT_VERSION,
                        writer_prompt_sha256=ANSWER_WRITER_PROMPT_SHA256,
                    )
                    raise
                emit_telemetry(
                    telemetry_sink,
                    TelemetryEventType.GUARDRAIL,
                    context.correlation_id,
                    TelemetryOutcome.SUCCEEDED,
                    started_at=grounding_started_at,
                    operation="grounding_validate",
                    count=len(resolution.supporting_citation_ids),
                    writer_prompt_version=ANSWER_WRITER_PROMPT_VERSION,
                    writer_prompt_sha256=ANSWER_WRITER_PROMPT_SHA256,
                )
                generated = {
                    "answer": answer,
                }
        else:
            # Compatibility boundary for existing Phase 05 generators. New
            # providers should use the two explicit boundaries above.
            generated = generator.generate(generation_request)
    except GroundingGuardrailBlocked:
        emit_telemetry(
            telemetry_sink,
            TelemetryEventType.MODEL,
            context.correlation_id,
            TelemetryOutcome.BLOCKED,
            started_at=model_started_at,
            operation="generate_answer",
        )
        return finish(
            guardrail_blocked_response(
                GuardrailStage.OUTPUT,
                prompt_artifact,
                correlation_id=context.correlation_id,
            )
        )
    except GroundingGuardrailError:
        emit_telemetry(
            telemetry_sink,
            TelemetryEventType.MODEL,
            context.correlation_id,
            TelemetryOutcome.ERROR,
            started_at=model_started_at,
            operation="generate_answer",
            error_code="guardrail_error",
        )
        return finish(
            technical_error_response(
                prompt_artifact,
                correlation_id=context.correlation_id,
                error_code="guardrail_error",
                separated_pipeline=all(item is not None for item in (evidence_resolver, answer_writer, grounding_validator)),
            )
        )
    except EvidenceContractError:
        # Resolver/writer contract violations are untrusted model output, not
        # documentary absence. Return an operational error without exposing
        # malformed content as a canonical no-evidence answer.
        emit_telemetry(
            telemetry_sink,
            TelemetryEventType.MODEL,
            context.correlation_id,
            TelemetryOutcome.ERROR,
            started_at=model_started_at,
            operation="generate_answer",
            error_code="model_failed",
        )
        if stage_in_progress is not None:
            emit_telemetry(
                telemetry_sink,
                TelemetryEventType.MODEL,
                context.correlation_id,
                TelemetryOutcome.ERROR,
                started_at=model_started_at,
                operation=stage_in_progress,
                error_code="model_failed",
                resolver_prompt_version=(
                    EVIDENCE_RESOLVER_PROMPT_VERSION
                    if stage_in_progress == "resolve_evidence"
                    else None
                ),
                resolver_prompt_sha256=(
                    EVIDENCE_RESOLVER_PROMPT_SHA256
                    if stage_in_progress == "resolve_evidence"
                    else None
                ),
                writer_prompt_version=(
                    ANSWER_WRITER_PROMPT_VERSION
                    if stage_in_progress == "write_answer"
                    else None
                ),
                writer_prompt_sha256=(
                    ANSWER_WRITER_PROMPT_SHA256
                    if stage_in_progress == "write_answer"
                    else None
                ),
            )
        return finish(
            technical_error_response(
                prompt_artifact,
                correlation_id=context.correlation_id,
                error_code="model_failed",
                separated_pipeline=all(
                    item is not None
                    for item in (evidence_resolver, answer_writer, grounding_validator)
                ),
            )
        )
    except GroundingContractError:
        # A malformed or rejected grounding contract is an operational
        # validation failure, not evidence that the corpus lacks an answer.
        emit_telemetry(
            telemetry_sink,
            TelemetryEventType.MODEL,
            context.correlation_id,
            TelemetryOutcome.ERROR,
            started_at=model_started_at,
            operation="generate_answer",
            error_code="model_failed",
        )
        if all(item is not None for item in (evidence_resolver, answer_writer, grounding_validator)):
            return finish(
                technical_error_response(
                    prompt_artifact,
                    correlation_id=context.correlation_id,
                    error_code="model_failed",
                    separated_pipeline=True,
                )
            )
        raise
    except Exception:
        emit_telemetry(
            telemetry_sink,
            TelemetryEventType.MODEL,
            context.correlation_id,
            TelemetryOutcome.ERROR,
            started_at=model_started_at,
            operation="generate_answer",
            error_code="model_failed",
        )
        if stage_in_progress is not None:
            emit_telemetry(
                telemetry_sink,
                TelemetryEventType.MODEL,
                context.correlation_id,
                TelemetryOutcome.ERROR,
                started_at=model_started_at,
                operation=stage_in_progress,
                error_code="model_failed",
            )
        emit_telemetry(
            telemetry_sink,
            TelemetryEventType.ERROR,
            context.correlation_id,
            TelemetryOutcome.ERROR,
            started_at=model_started_at,
            operation="generate_answer",
            error_code="model_failed",
        )
        emit_telemetry(
            telemetry_sink,
            TelemetryEventType.AGENT,
            context.correlation_id,
            TelemetryOutcome.ERROR,
            started_at=request_started_at,
            operation="answer_question",
            error_code="model_failed",
        )
        return finish(
            technical_error_response(
                prompt_artifact,
                correlation_id=context.correlation_id,
                error_code="model_failed",
                separated_pipeline=all(item is not None for item in (evidence_resolver, answer_writer, grounding_validator)),
            )
        )
    emit_telemetry(
        telemetry_sink,
        TelemetryEventType.MODEL,
        context.correlation_id,
        TelemetryOutcome.SUCCEEDED,
        started_at=model_started_at,
        operation="generate_answer",
    )
    if not isinstance(generated, Mapping):
        return finish(
            technical_error_response(
                prompt_artifact,
                correlation_id=context.correlation_id,
                error_code="model_failed",
            )
        )
    if all(item is not None for item in (evidence_resolver, answer_writer, grounding_validator)):
        try:
            if resolved_evidence is None:
                raise EvidenceContractError("missing validated evidence resolution")
            answer = generated.get("answer")
            if not isinstance(answer, str) or not answer.strip() or len(answer) > MAX_ANSWER_LENGTH:
                raise EvidenceContractError("answer writer returned invalid answer text")
            response = ChatResponse(
                answer=answer.strip(),
                citations=tuple(
                    ChatCitation.from_retrieved(
                        next(
                            passage.citation
                            for passage in passages
                            if passage.citation.citation_id == citation_id
                        )
                    )
                    for citation_id in resolved_evidence.supporting_citation_ids
                ),
                evidence_status=resolved_evidence.evidence_status,
                disclaimer_required=True,
                prompt_version=prompt_artifact.version,
                prompt_sha256=prompt_artifact.sha256,
                resolver_prompt_version=EVIDENCE_RESOLVER_PROMPT_VERSION,
                resolver_prompt_sha256=EVIDENCE_RESOLVER_PROMPT_SHA256,
                writer_prompt_version=ANSWER_WRITER_PROMPT_VERSION,
                writer_prompt_sha256=ANSWER_WRITER_PROMPT_SHA256,
            )
        except (EvidenceContractError, KeyError, StopIteration, TypeError):
            return finish(
                technical_error_response(
                    prompt_artifact,
                    correlation_id=context.correlation_id,
                    error_code="model_failed",
                    separated_pipeline=True,
                )
            )
    else:
        response = _validate_generation_response(generated, passages, prompt_artifact)
    if not response.citations:
        return finish(response)

    if getattr(grounding_validator, "performs_output_guardrail", False):
        # The productive grounding adapter already performed the single
        # contextual OUTPUT ApplyGuardrail call above.
        return finish(response)

    output_result = guardrails.check_output(
        question,
        generation_request.evidence,
        response.answer,
        correlation_id=context.correlation_id,
    )
    if not output_result.can_proceed:
        if output_result.outcome.value == "error":
            return finish(
                technical_error_response(
                    prompt_artifact,
                    correlation_id=context.correlation_id,
                    error_code="guardrail_error",
                )
            )
        return finish(guardrail_blocked_response(GuardrailStage.OUTPUT, prompt_artifact, correlation_id=context.correlation_id))
    safe_answer = output_result.content[0]
    if len(safe_answer) > MAX_ANSWER_LENGTH:
        return finish(guardrail_blocked_response(GuardrailStage.OUTPUT, prompt_artifact, correlation_id=context.correlation_id))
    return finish(replace(response, answer=safe_answer))
