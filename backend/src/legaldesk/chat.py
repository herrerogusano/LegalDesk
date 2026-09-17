"""Provider-neutral retrieve-then-generate orchestration for Phase 05.

The generation boundary receives a server-loaded versioned system prompt, the
user's question, and bounded passage text. It has no authorization scope,
browser selectors, retrieval filters, or provider credentials.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Mapping, Protocol

from .authorization import (
    AuthorizationDenied,
    AuthorizationStore,
    VerifiedIdentity,
    build_request_context,
)
from .memory import ConversationBindingStore
from .guardrails import (
    BedrockGuardrailClient,
    GuardrailAuditSink,
    GuardrailConfig,
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


class EvidenceStatus(StrEnum):
    ANSWERABLE = "answerable"
    AMBIGUOUS = "ambiguous"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


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
            "sourceUri": self.source_uri,
            "pageNumber": self.page_number,
            "section": self.section,
        }


@dataclass(frozen=True, slots=True)
class ChatResponse:
    answer: str
    citations: tuple[ChatCitation, ...]
    evidence_status: EvidenceStatus
    disclaimer_required: bool
    prompt_version: str | None
    prompt_sha256: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "answer": self.answer,
            "citations": [citation.to_dict() for citation in self.citations],
            "evidenceStatus": self.evidence_status.value,
            "disclaimerRequired": self.disclaimer_required,
            "promptVersion": self.prompt_version,
            "promptSha256": self.prompt_sha256,
        }


def insufficient_evidence_response(
    prompt_artifact: SystemPromptArtifact | None = None,
) -> ChatResponse:
    return ChatResponse(
        answer=INSUFFICIENT_EVIDENCE_ANSWER,
        citations=(),
        evidence_status=EvidenceStatus.INSUFFICIENT_EVIDENCE,
        disclaimer_required=True,
        prompt_version=prompt_artifact.version if prompt_artifact else None,
        prompt_sha256=prompt_artifact.sha256 if prompt_artifact else None,
    )


def guardrail_blocked_response(
    stage: GuardrailStage,
    prompt_artifact: SystemPromptArtifact,
) -> ChatResponse:
    answer = (
        GUARDRAIL_INPUT_BLOCKED_ANSWER
        if stage is GuardrailStage.INPUT
        else GUARDRAIL_OUTPUT_BLOCKED_ANSWER
    )
    return ChatResponse(
        answer=answer,
        citations=(),
        evidence_status=EvidenceStatus.INSUFFICIENT_EVIDENCE,
        disclaimer_required=True,
        prompt_version=prompt_artifact.version,
        prompt_sha256=prompt_artifact.sha256,
    )


def _validate_generation_response(
    result: Mapping[str, object],
    passages: tuple[RetrievedPassage, ...],
    prompt_artifact: SystemPromptArtifact,
) -> ChatResponse:
    if set(result) != set(GENERATION_RESPONSE_FIELDS):
        return insufficient_evidence_response(prompt_artifact)
    answer = result.get("answer")
    raw_citation_ids = result.get("citationIds")
    raw_status = result.get("evidenceStatus")
    if (
        not isinstance(answer, str)
        or not answer.strip()
        or len(answer) > MAX_ANSWER_LENGTH
        or not isinstance(raw_citation_ids, (list, tuple))
        or any(not isinstance(item, str) for item in raw_citation_ids)
    ):
        return insufficient_evidence_response(prompt_artifact)
    try:
        status = EvidenceStatus(raw_status)
    except (ValueError, TypeError):
        return insufficient_evidence_response(prompt_artifact)

    retrieved = {passage.citation.citation_id: passage.citation for passage in passages}
    citation_ids = tuple(raw_citation_ids)
    if (
        len(citation_ids) != len(set(citation_ids))
        or any(citation_id not in retrieved for citation_id in citation_ids)
    ):
        return insufficient_evidence_response(prompt_artifact)
    if not citation_ids:
        return insufficient_evidence_response(prompt_artifact)

    return ChatResponse(
        answer=answer.strip(),
        citations=tuple(
            ChatCitation.from_retrieved(retrieved[citation_id])
            for citation_id in citation_ids
        ),
        evidence_status=status,
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

    try:
        prompt_artifact = prompt_provider.load()
    except PromptConfigurationError:
        # A missing or malformed server prompt must never fall back to generation.
        return insufficient_evidence_response()

    guardrails = GuardrailProcessor(
        guardrail_client,
        guardrail_config,
        audit_sink=guardrail_audit_sink,
    )
    input_result = guardrails.check_input(
        request.question.strip(), correlation_id=context.correlation_id
    )
    if not input_result.can_proceed:
        return guardrail_blocked_response(GuardrailStage.INPUT, prompt_artifact)
    question = input_result.content[0]
    if len(question) > MAX_QUERY_LENGTH:
        return guardrail_blocked_response(GuardrailStage.INPUT, prompt_artifact)

    passages = _retrieve_with_context(
        context,
        question,
        bedrock_client=retrieval_client,
        knowledge_base_id=knowledge_base_id,
    )
    if not passages:
        return insufficient_evidence_response(prompt_artifact)

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
    generated = generator.generate(generation_request)
    if not isinstance(generated, Mapping):
        return insufficient_evidence_response(prompt_artifact)
    response = _validate_generation_response(generated, passages, prompt_artifact)
    if not response.citations:
        return response

    output_result = guardrails.check_output(
        question,
        generation_request.evidence,
        response.answer,
        correlation_id=context.correlation_id,
    )
    if not output_result.can_proceed:
        return guardrail_blocked_response(GuardrailStage.OUTPUT, prompt_artifact)
    safe_answer = output_result.content[0]
    if len(safe_answer) > MAX_ANSWER_LENGTH:
        return guardrail_blocked_response(GuardrailStage.OUTPUT, prompt_artifact)
    return replace(response, answer=safe_answer)
