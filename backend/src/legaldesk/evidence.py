"""Deterministic evidence resolution and answer-writing boundaries.

The model may propose a small evidence-resolution object, but it never chooses
the product-facing ``EvidenceStatus`` and it never receives authorization
scope.  The backend validates the object against the citation IDs issued by
retrieval and derives the status locally.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping, Protocol, Sequence


class EvidenceStatus(StrEnum):
    ANSWERABLE = "answerable"
    AMBIGUOUS = "ambiguous"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class EvidenceCoverage(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    NONE = "none"


EVIDENCE_RESOLUTION_FIELDS = (
    "coverage",
    "conflict",
    "supportingCitationIds",
)

ANSWER_WRITER_INSTRUCTION = (
    "Write only the answer text. Evidence status and citation IDs are fixed by "
    "the validated resolver/backend contract; do not choose, add, remove, or "
    "rewrite them, and do not return JSON metadata."
)

# Bedrock Converse ``outputConfig.textFormat`` accepts this JSON-Schema subset.
# It is intentionally limited to the resolver contract; the server-side
# validator below remains mandatory because provider schema enforcement does not
# prove citation authorization or semantic entailment.
EVIDENCE_RESOLUTION_JSON_SCHEMA: dict[str, object] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["coverage", "conflict", "supportingCitationIds"],
    "properties": {
        "coverage": {"type": "string", "enum": ["complete", "partial", "none"]},
        "conflict": {"type": "boolean"},
        "supportingCitationIds": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
}

ANSWER_WRITER_JSON_SCHEMA: dict[str, object] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["answer"],
    "properties": {"answer": {"type": "string"}},
}


def evidence_resolver_output_config() -> dict[str, object]:
    """Return the local Converse payload fragment for schema-constrained output."""

    return {
        "textFormat": {
            "type": "json_schema",
            "structure": {
                "jsonSchema": {
                    "schema": json.dumps(
                        EVIDENCE_RESOLUTION_JSON_SCHEMA,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    "name": "legaldesk_evidence_resolution",
                }
            },
        }
    }


def answer_writer_output_config() -> dict[str, object]:
    """Return the strict Converse output shape for the separated writer."""

    return {
        "textFormat": {
            "type": "json_schema",
            "structure": {
                "jsonSchema": {
                    "schema": json.dumps(ANSWER_WRITER_JSON_SCHEMA, separators=(",", ":"), sort_keys=True),
                    "name": "legaldesk_answer_writer",
                }
            },
        }
    }


class EvidenceContractError(ValueError):
    """Raised when a model resolution cannot be safely accepted."""


class GroundingContractError(ValueError):
    """Raised when a writer answer cannot be grounded by an approved check."""


@dataclass(frozen=True, slots=True)
class EvidenceResolution:
    """Validated model proposal, scoped to retrieval-issued citations only."""

    coverage: EvidenceCoverage
    conflict: bool
    supporting_citation_ids: tuple[str, ...]

    @property
    def evidence_status(self) -> EvidenceStatus:
        """Derive the user-visible status without consulting model labels."""

        if self.conflict:
            return EvidenceStatus.AMBIGUOUS
        if self.coverage is EvidenceCoverage.COMPLETE:
            return EvidenceStatus.ANSWERABLE
        return EvidenceStatus.INSUFFICIENT_EVIDENCE

    def to_dict(self) -> dict[str, object]:
        return {
            "coverage": self.coverage.value,
            "conflict": self.conflict,
            "supportingCitationIds": list(self.supporting_citation_ids),
        }


@dataclass(frozen=True, slots=True)
class EvidenceResolutionRequest:
    question: str
    evidence: tuple[object, ...]


@dataclass(frozen=True, slots=True)
class AnswerWriterRequest:
    question: str
    evidence: tuple[object, ...]
    resolution: EvidenceResolution

    @property
    def instruction(self) -> str:
        return ANSWER_WRITER_INSTRUCTION


class EvidenceResolver(Protocol):
    def resolve(self, request: EvidenceResolutionRequest) -> Mapping[str, object]: ...


class ConverseClient(Protocol):
    """Minimal boto-compatible client seam; never creates a client or calls AWS."""

    def converse(self, **kwargs: object) -> Mapping[str, object]: ...


def build_converse_evidence_resolver_request(
    request: EvidenceResolutionRequest,
    *,
    model_id: str,
    system_prompt: str,
    max_tokens: int = 256,
    temperature: float = 0.0,
) -> dict[str, object]:
    """Build a concrete, schema-constrained Converse request for a resolver.

    The adapter sends only the question and already-authorized evidence. It
    deliberately does not enable provider-native citations: structured output
    and Anthropic citations are incompatible, while our IDs are application
    citation IDs validated server-side.
    """

    if not isinstance(model_id, str) or not model_id.strip():
        raise ValueError("model_id is required")
    if not isinstance(system_prompt, str) or not system_prompt.strip():
        raise ValueError("system_prompt is required")
    if type(max_tokens) is not int or not 1 <= max_tokens <= 4096:
        raise ValueError("max_tokens is invalid")
    if isinstance(temperature, bool) or not isinstance(temperature, (int, float)) or not 0 <= temperature <= 1:
        raise ValueError("temperature is invalid")
    evidence_lines = []
    for item in request.evidence:
        if isinstance(item, Mapping):
            citation_id = item.get("citation_id", item.get("citationId"))
            text = item.get("text")
        else:
            citation_id = getattr(item, "citation_id", getattr(item, "citationId", None))
            text = getattr(item, "text", None)
        if not isinstance(citation_id, str) or not citation_id.strip() or not isinstance(text, str):
            raise ValueError("evidence citation mappings are invalid")
        evidence_lines.append(f"[{citation_id}] {text}")
    user_text = f"Question: {request.question}\n\nAuthorized evidence:\n" + "\n".join(evidence_lines)
    return {
        "modelId": model_id,
        "system": [{"text": system_prompt}],
        "messages": [{"role": "user", "content": [{"text": user_text}]}],
        "inferenceConfig": {"maxTokens": max_tokens, "temperature": float(temperature)},
        "outputConfig": evidence_resolver_output_config(),
    }


class ConverseEvidenceResolver:
    """Concrete provider adapter with server-side contract validation."""

    def __init__(self, client: ConverseClient, *, model_id: str, system_prompt: str) -> None:
        self.client = client
        self.model_id = model_id
        self.system_prompt = system_prompt

    def resolve(self, request: EvidenceResolutionRequest) -> Mapping[str, object]:
        response = self.client.converse(
            **build_converse_evidence_resolver_request(
                request, model_id=self.model_id, system_prompt=self.system_prompt
            )
        )
        try:
            content = response["output"]["message"]["content"]  # type: ignore[index]
            text = next(item["text"] for item in content if isinstance(item, Mapping) and isinstance(item.get("text"), str))
            parsed = json.loads(text)
        except (KeyError, IndexError, StopIteration, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise EvidenceContractError("Converse resolver returned invalid JSON") from exc
        if not isinstance(parsed, Mapping):
            raise EvidenceContractError("Converse resolver returned a non-object")
        return parsed


def build_converse_answer_writer_request(
    request: AnswerWriterRequest,
    *,
    model_id: str,
    system_prompt: str,
    max_tokens: int = 512,
    temperature: float = 0.0,
) -> dict[str, object]:
    """Build a writer request with fixed status/IDs and selected evidence only."""

    if not isinstance(model_id, str) or not model_id.strip() or not isinstance(system_prompt, str) or not system_prompt.strip():
        raise ValueError("model_id and system_prompt are required")
    if type(max_tokens) is not int or not 1 <= max_tokens <= 4096:
        raise ValueError("max_tokens is invalid")
    if isinstance(temperature, bool) or not isinstance(temperature, (int, float)) or not 0 <= temperature <= 1:
        raise ValueError("temperature is invalid")
    selected = []
    allowed = set(request.resolution.supporting_citation_ids)
    for item in request.evidence:
        citation_id = item.get("citation_id", item.get("citationId")) if isinstance(item, Mapping) else getattr(item, "citation_id", getattr(item, "citationId", None))
        text = item.get("text") if isinstance(item, Mapping) else getattr(item, "text", None)
        if citation_id in allowed and isinstance(citation_id, str) and isinstance(text, str):
            selected.append(f"[{citation_id}] {text}")
    if len(selected) != len(allowed):
        raise ValueError("writer evidence does not contain every supporting citation")
    user_text = (
        "mode: separated_answer_writer\n"
        f"question: {request.question}\n"
        f"fixedEvidenceStatus: {request.resolution.evidence_status.value}\n"
        f"allowedCitationIds: {json.dumps(list(request.resolution.supporting_citation_ids))}\n"
        "selectedEvidence:\n" + "\n".join(selected) + "\n\n" + ANSWER_WRITER_INSTRUCTION
    )
    return {
        "modelId": model_id,
        "system": [{"text": system_prompt}],
        "messages": [{"role": "user", "content": [{"text": user_text}]}],
        "inferenceConfig": {"maxTokens": max_tokens, "temperature": float(temperature)},
        "outputConfig": answer_writer_output_config(),
    }


class ConverseAnswerWriter:
    """Concrete local adapter for the answer-only Converse response shape."""

    def __init__(self, client: ConverseClient, *, model_id: str, system_prompt: str) -> None:
        self.client = client
        self.model_id = model_id
        self.system_prompt = system_prompt

    def write(self, request: AnswerWriterRequest) -> Mapping[str, object]:
        response = self.client.converse(
            **build_converse_answer_writer_request(
                request, model_id=self.model_id, system_prompt=self.system_prompt
            )
        )
        try:
            content = response["output"]["message"]["content"]  # type: ignore[index]
            text = next(item["text"] for item in content if isinstance(item, Mapping) and isinstance(item.get("text"), str))
            parsed = json.loads(text)
        except (KeyError, IndexError, StopIteration, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise EvidenceContractError("Converse writer returned invalid JSON") from exc
        if not isinstance(parsed, Mapping) or set(parsed) != {"answer"} or not isinstance(parsed.get("answer"), str) or not parsed["answer"].strip():
            raise EvidenceContractError("Converse writer returned an unsupported shape")
        return {"answer": parsed["answer"]}


class AnswerWriter(Protocol):
    def write(self, request: AnswerWriterRequest) -> Mapping[str, object] | str: ...


class GroundingValidator(Protocol):
    def validate(self, request: "GroundingRequest") -> Mapping[str, object]: ...


@dataclass(frozen=True, slots=True)
class GroundingRequest:
    question: str
    answer: str
    evidence: tuple[object, ...]
    supporting_citation_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GroundingResult:
    grounded: bool
    score: float
    matched_citation_ids: tuple[str, ...]


def validate_grounding_result(
    result: Mapping[str, object],
    *,
    supporting_citation_ids: Sequence[str],
    minimum_score: float = 0.7,
) -> GroundingResult:
    """Validate the post-writer grounding decision and fail closed.

    This boundary accepts only a bounded validator result. It does not infer
    grounding from the presence of a citation, which prevents a writer from
    attaching an otherwise valid citation to an unsupported claim.
    """

    fields = {"grounded", "score", "matchedCitationIds"}
    if not isinstance(result, Mapping) or set(result) != fields:
        raise GroundingContractError("grounding result has an unsupported shape")
    grounded = result["grounded"]
    score = result["score"]
    matched = result["matchedCitationIds"]
    if type(grounded) is not bool or isinstance(score, bool) or not isinstance(score, (int, float)):
        raise GroundingContractError("grounding result types are invalid")
    if not 0.0 <= float(score) <= 1.0 or not isinstance(matched, (list, tuple)):
        raise GroundingContractError("grounding result values are invalid")
    matched_ids = tuple(matched)
    if any(type(item) is not str or not item.strip() for item in matched_ids):
        raise GroundingContractError("matched citations are invalid")
    if len(matched_ids) != len(set(matched_ids)):
        raise GroundingContractError("matched citations must be unique")
    supporting = tuple(supporting_citation_ids)
    if any(item not in supporting for item in matched_ids):
        raise GroundingContractError("grounding cites an unavailable supporting passage")
    if not grounded or float(score) < minimum_score or not matched_ids:
        raise GroundingContractError("writer answer failed grounding validation")
    return GroundingResult(True, float(score), matched_ids)


def validate_evidence_resolution(
    result: Mapping[str, object],
    supplied_citation_ids: Sequence[str],
) -> EvidenceResolution:
    """Validate the strict resolver contract against retrieval output.

    The resolver cannot mint citation IDs.  ``none`` is intentionally strict:
    it must carry no supporting citation because it means no passage materially
    supports the requested answer.  Any malformed result raises and callers
    must fail closed.
    """

    if not isinstance(result, Mapping) or set(result) != set(EVIDENCE_RESOLUTION_FIELDS):
        raise EvidenceContractError("evidence resolution has an unsupported shape")
    try:
        coverage = EvidenceCoverage(result["coverage"])
    except (TypeError, ValueError) as exc:
        raise EvidenceContractError("coverage is invalid") from exc
    conflict = result["conflict"]
    if type(conflict) is not bool:
        raise EvidenceContractError("conflict must be a boolean")
    raw_ids = result["supportingCitationIds"]
    if not isinstance(raw_ids, (list, tuple)) or any(
        type(item) is not str or not item.strip() for item in raw_ids
    ):
        raise EvidenceContractError("supportingCitationIds must be non-empty strings")
    citation_ids = tuple(raw_ids)
    if len(citation_ids) != len(set(citation_ids)):
        raise EvidenceContractError("supportingCitationIds must be unique")
    supplied = tuple(supplied_citation_ids)
    if len(supplied) != len(set(supplied)) or any(
        type(item) is not str or not item.strip() for item in supplied
    ):
        raise EvidenceContractError("supplied citation IDs are invalid")
    if any(item not in supplied for item in citation_ids):
        raise EvidenceContractError("resolution cites an unavailable passage")
    if coverage is EvidenceCoverage.NONE and citation_ids:
        raise EvidenceContractError("none coverage cannot cite supporting passages")
    if coverage is EvidenceCoverage.NONE and conflict:
        raise EvidenceContractError("none coverage cannot declare a conflict")
    if coverage is not EvidenceCoverage.NONE and not citation_ids:
        raise EvidenceContractError("complete or partial coverage requires support")
    return EvidenceResolution(coverage, conflict, citation_ids)


def legacy_resolution_from_generation(
    result: Mapping[str, object],
    supplied_citation_ids: Sequence[str],
) -> EvidenceResolution:
    """Adapt the Phase 05 combined contract during the migration window.

    New integrations should call :func:`validate_evidence_resolution` directly.
    This adapter intentionally maps the old model label only at the compatibility
    boundary; the resulting status is still derived by ``EvidenceResolution``.
    """

    if not isinstance(result, Mapping):
        raise EvidenceContractError("legacy generation result must be an object")
    raw_status = result.get("evidenceStatus")
    raw_ids = result.get("citationIds")
    if not isinstance(raw_ids, (list, tuple)):
        raise EvidenceContractError("legacy citationIds are invalid")
    if raw_status == EvidenceStatus.ANSWERABLE.value:
        coverage, conflict = EvidenceCoverage.COMPLETE, False
    elif raw_status == EvidenceStatus.AMBIGUOUS.value:
        coverage, conflict = EvidenceCoverage.COMPLETE, True
    elif raw_status == EvidenceStatus.INSUFFICIENT_EVIDENCE.value:
        coverage, conflict = (
            (EvidenceCoverage.PARTIAL, False)
            if raw_ids
            else (EvidenceCoverage.NONE, False)
        )
    else:
        raise EvidenceContractError("legacy evidence status is invalid")
    return validate_evidence_resolution(
        {
            "coverage": coverage.value,
            "conflict": conflict,
            "supportingCitationIds": list(raw_ids),
        },
        supplied_citation_ids,
    )


__all__ = [
    "AnswerWriter",
    "AnswerWriterRequest",
    "ANSWER_WRITER_INSTRUCTION",
    "ANSWER_WRITER_JSON_SCHEMA",
    "ConverseAnswerWriter",
    "ConverseClient",
    "ConverseEvidenceResolver",
    "EVIDENCE_RESOLUTION_FIELDS",
    "EVIDENCE_RESOLUTION_JSON_SCHEMA",
    "EvidenceContractError",
    "GroundingContractError",
    "EvidenceCoverage",
    "EvidenceResolution",
    "EvidenceResolutionRequest",
    "EvidenceResolver",
    "EvidenceStatus",
    "GroundingRequest",
    "GroundingResult",
    "GroundingValidator",
    "legacy_resolution_from_generation",
    "build_converse_evidence_resolver_request",
    "build_converse_answer_writer_request",
    "answer_writer_output_config",
    "evidence_resolver_output_config",
    "validate_evidence_resolution",
    "validate_grounding_result",
]
