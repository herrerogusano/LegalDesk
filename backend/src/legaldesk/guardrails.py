"""Provider-neutral Bedrock Guardrails boundary for chat safety checks."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping, Protocol, Sequence

from .observability import (
    TelemetryEventType,
    TelemetryOutcome,
    TelemetrySink,
    emit_telemetry,
)


_LOGGER = logging.getLogger("legaldesk.guardrails")
_VALID_ACTIONS = {"NONE", "GUARDRAIL_INTERVENED"}
_BLOCKING_ASSESSMENT_ACTIONS = {"BLOCKED", "BLOCK", "GUARDRAIL_INTERVENED"}
_NON_BLOCKING_ASSESSMENT_ACTIONS = {"NONE", "ANONYMIZED"}
_KNOWN_ASSESSMENT_POLICIES = {
    "automatedReasoningPolicy",
    "contentPolicy",
    "contextualGroundingPolicy",
    "sensitiveInformationPolicy",
    "topicPolicy",
    "wordPolicy",
}
_ASSESSMENT_METADATA = {"invocationMetrics", "appliedGuardrailDetails"}


class GuardrailStage(StrEnum):
    INPUT = "input"
    OUTPUT = "output"


class GuardrailOutcome(StrEnum):
    ALLOWED = "allowed"
    ANONYMIZED = "anonymized"
    BLOCKED = "blocked"
    ERROR = "error"


class GroundingGuardrailError(RuntimeError):
    """Provider or schema failure while evaluating contextual grounding."""


class GroundingGuardrailBlocked(PermissionError):
    """Provider policy blocked the generated answer."""


@dataclass(frozen=True, slots=True)
class GuardrailConfig:
    """Server-owned Bedrock Guardrail selection; never accepted from a client."""

    identifier: str
    version: str

    def __post_init__(self) -> None:
        if not isinstance(self.identifier, str) or not self.identifier.strip():
            raise ValueError("guardrail identifier is not configured")
        if not isinstance(self.version, str) or not self.version.strip():
            raise ValueError("guardrail version is not configured")


class BedrockGuardrailClient(Protocol):
    """Subset of the Bedrock Runtime client used by this integration."""

    def apply_guardrail(self, **kwargs: object) -> Mapping[str, object]: ...


@dataclass(frozen=True, slots=True)
class GuardrailAuditEvent:
    """Safe audit metadata: stage/outcome only, with no content or assessment body."""

    correlation_id: str
    stage: GuardrailStage
    action: str
    outcome: GuardrailOutcome


class GuardrailAuditSink(Protocol):
    def record(self, event: GuardrailAuditEvent) -> None: ...


class EvidenceText(Protocol):
    text: str


class LoggingGuardrailAuditSink:
    """Emit structured outcome metadata without logging prompts, PII, or passages."""

    def record(self, event: GuardrailAuditEvent) -> None:
        _LOGGER.info(
            "guardrail_outcome",
            extra={
                "correlation_id": event.correlation_id,
                "guardrail_stage": event.stage.value,
                "guardrail_action": event.action,
                "guardrail_outcome": event.outcome.value,
            },
        )


@dataclass(frozen=True, slots=True)
class GuardrailResult:
    outcome: GuardrailOutcome
    content: tuple[str, ...]

    @property
    def can_proceed(self) -> bool:
        return self.outcome in {GuardrailOutcome.ALLOWED, GuardrailOutcome.ANONYMIZED}


class GuardrailProcessor:
    """Apply input checks and contextual output checks with fail-closed parsing."""

    def __init__(
        self,
        client: BedrockGuardrailClient,
        config: GuardrailConfig,
        audit_sink: GuardrailAuditSink | None = None,
        telemetry_sink: TelemetrySink | None = None,
    ) -> None:
        self._client = client
        self._config = config
        self._audit_sink = audit_sink or LoggingGuardrailAuditSink()
        self._telemetry_sink = telemetry_sink

    def check_input(self, text: str, *, correlation_id: str) -> GuardrailResult:
        content = ({"text": {"text": text}},)
        return self._apply(
            GuardrailStage.INPUT,
            content,
            (text,),
            selected_index=0,
            correlation_id=correlation_id,
        )

    def check_output(
        self,
        question: str,
        evidence: Sequence[EvidenceText],
        answer: str,
        *,
        correlation_id: str,
    ) -> GuardrailResult:
        content: list[dict[str, object]] = [
            {"text": {"text": question, "qualifiers": ["query"]}}
        ]
        content.extend(
            {
                "text": {
                    "text": passage.text,
                    "qualifiers": ["grounding_source"],
                }
            }
            for passage in evidence
        )
        content.append({"text": {"text": answer}})
        originals = (question, *(passage.text for passage in evidence), answer)
        return self._apply(
            GuardrailStage.OUTPUT,
            tuple(content),
            originals,
            selected_index=len(content) - 1,
            correlation_id=correlation_id,
        )

    def _apply(
        self,
        stage: GuardrailStage,
        content: Sequence[Mapping[str, object]],
        originals: tuple[str, ...],
        *,
        selected_index: int,
        correlation_id: str,
    ) -> GuardrailResult:
        source = "INPUT" if stage is GuardrailStage.INPUT else "OUTPUT"
        try:
            response = self._client.apply_guardrail(
                guardrailIdentifier=self._config.identifier,
                guardrailVersion=self._config.version,
                source=source,
                content=list(content),
                outputScope="FULL",
            )
        except Exception:
            return self._finish(
                correlation_id, stage, "ERROR", GuardrailOutcome.ERROR, ()
            )

        if not isinstance(response, Mapping):
            return self._finish(
                correlation_id, stage, "ERROR", GuardrailOutcome.ERROR, ()
            )
        action = response.get("action")
        if not isinstance(action, str) or action not in _VALID_ACTIONS:
            return self._finish(
                correlation_id, stage, "ERROR", GuardrailOutcome.ERROR, ()
            )

        assessments = response.get("assessments")
        if not isinstance(assessments, (list, tuple)) or any(
            not isinstance(assessment, Mapping) for assessment in assessments
        ):
            return self._finish(
                correlation_id, stage, "ERROR", GuardrailOutcome.ERROR, ()
            )
        assessment_actions = _assessment_actions(assessments)
        if assessment_actions is None:
            return self._finish(
                correlation_id, stage, "ERROR", GuardrailOutcome.ERROR, ()
            )
        known_assessment_actions = (
            _NON_BLOCKING_ASSESSMENT_ACTIONS | _BLOCKING_ASSESSMENT_ACTIONS
        )
        if any(value not in known_assessment_actions for value in assessment_actions):
            return self._finish(
                correlation_id, stage, "ERROR", GuardrailOutcome.ERROR, ()
            )
        if action == "NONE" and any(value != "NONE" for value in assessment_actions):
            # The response action and policy assessments disagree; do not trust it.
            return self._finish(
                correlation_id, stage, "ERROR", GuardrailOutcome.ERROR, ()
            )
        if action == "GUARDRAIL_INTERVENED":
            if any(value in _BLOCKING_ASSESSMENT_ACTIONS for value in assessment_actions):
                return self._finish(
                    correlation_id, stage, action, GuardrailOutcome.BLOCKED, ()
                )
            if (
                not assessment_actions
                or "ANONYMIZED" not in assessment_actions
                or any(
                    value not in _NON_BLOCKING_ASSESSMENT_ACTIONS
                    for value in assessment_actions
                )
            ):
                return self._finish(
                    correlation_id, stage, action, GuardrailOutcome.BLOCKED, ()
                )

        returned = _returned_texts(response.get("outputs", ()))
        if returned is None:
            return self._finish(
                correlation_id, stage, "ERROR", GuardrailOutcome.ERROR, ()
            )
        if returned and len(returned) != len(originals):
            # FULL scope must retain content ordering so redaction can be applied safely.
            return self._finish(
                correlation_id, stage, "ERROR", GuardrailOutcome.ERROR, ()
            )
        if action == "GUARDRAIL_INTERVENED" and not returned:
            return self._finish(
                correlation_id, stage, "ERROR", GuardrailOutcome.ERROR, ()
            )
        if "ANONYMIZED" in assessment_actions and not returned:
            # Never substitute original text when an anonymized output is absent.
            return self._finish(
                correlation_id, stage, "ERROR", GuardrailOutcome.ERROR, ()
            )

        safe_content = returned or originals
        if any(not text.strip() for text in safe_content):
            return self._finish(
                correlation_id, stage, "ERROR", GuardrailOutcome.ERROR, ()
            )
        outcome = (
            GuardrailOutcome.ANONYMIZED
            if "ANONYMIZED" in assessment_actions
            else GuardrailOutcome.ALLOWED
        )
        return self._finish(
            correlation_id,
            stage,
            action,
            outcome,
            (safe_content[selected_index],),
        )

    def _finish(
        self,
        correlation_id: str,
        stage: GuardrailStage,
        action: str,
        outcome: GuardrailOutcome,
        content: tuple[str, ...],
    ) -> GuardrailResult:
        safe_action = action if action in _VALID_ACTIONS else "ERROR"
        self._audit_sink.record(
            GuardrailAuditEvent(correlation_id, stage, safe_action, outcome)
        )
        telemetry_outcome = {
            GuardrailOutcome.ALLOWED: TelemetryOutcome.SUCCEEDED,
            GuardrailOutcome.ANONYMIZED: TelemetryOutcome.SUCCEEDED,
            GuardrailOutcome.BLOCKED: TelemetryOutcome.BLOCKED,
            GuardrailOutcome.ERROR: TelemetryOutcome.ERROR,
        }[outcome]
        emit_telemetry(
            self._telemetry_sink,
            TelemetryEventType.GUARDRAIL,
            correlation_id,
            telemetry_outcome,
            operation=stage.value,
            error_code=("guardrail_error" if outcome is GuardrailOutcome.ERROR else None),
        )
        return GuardrailResult(outcome, content)


class GuardrailGroundingValidator:
    """Concrete grounding adapter backed by Bedrock contextual assessments.

    This is intentionally separate from the lexical test oracle. A provider
    response must contain a well-formed contextual-grounding filter, score,
    threshold, and non-blocking action; otherwise the adapter fails closed.
    """

    performs_output_guardrail = True

    def __init__(
        self,
        client: BedrockGuardrailClient,
        config: GuardrailConfig,
        *,
        minimum_score: float = 0.75,
        audit_sink: GuardrailAuditSink | None = None,
        telemetry_sink: TelemetrySink | None = None,
    ) -> None:
        if isinstance(minimum_score, bool) or not isinstance(minimum_score, (int, float)):
            raise ValueError("minimum_score is invalid")
        if not 0.0 <= float(minimum_score) <= 1.0:
            raise ValueError("minimum_score is invalid")
        self._client = client
        self._config = config
        self._minimum_score = float(minimum_score)
        self._audit_sink = audit_sink or LoggingGuardrailAuditSink()
        self._telemetry_sink = telemetry_sink

    def validate(self, request: object) -> Mapping[str, object]:
        question = getattr(request, "question", None)
        answer = getattr(request, "answer", None)
        evidence = getattr(request, "evidence", None)
        supporting = getattr(request, "supporting_citation_ids", None)
        if (
            not isinstance(question, str)
            or not question.strip()
            or not isinstance(answer, str)
            or not answer.strip()
            or not isinstance(evidence, (list, tuple))
            or not isinstance(supporting, (list, tuple))
            or not supporting
        ):
            raise GroundingGuardrailError("grounding request is invalid")
        if len(question) > 1_000 or len(answer) > 5_000:
            raise GroundingGuardrailError("grounding content exceeds provider limits")
        evidence_texts: list[str] = []
        for passage in evidence:
            text = getattr(passage, "text", None)
            if not isinstance(text, str) or not text.strip():
                raise GroundingGuardrailError("grounding evidence is invalid")
            evidence_texts.append(text)
        if sum(len(text) for text in evidence_texts) > 100_000:
            raise GroundingGuardrailError("grounding sources exceed provider limits")
        content: list[dict[str, object]] = [
            {"text": {"text": question, "qualifiers": ["query"]}}
        ]
        for text in evidence_texts:
            content.append({"text": {"text": text, "qualifiers": ["grounding_source"]}})
        content.append({"text": {"text": answer}})
        correlation_id = getattr(request, "correlation_id", None)
        if not isinstance(correlation_id, str):
            correlation_id = "00000000-0000-4000-8000-000000000000"
        try:
            response = self._client.apply_guardrail(
                guardrailIdentifier=self._config.identifier,
                guardrailVersion=self._config.version,
                source="OUTPUT",
                content=content,
                outputScope="FULL",
            )
        except Exception as exc:
            self._record(correlation_id, GuardrailOutcome.ERROR)
            raise GroundingGuardrailError("grounding provider failed") from exc
        try:
            (
                top_action,
                grounding_score,
                grounding_threshold,
                relevance_score,
                relevance_threshold,
                grounding_action,
                relevance_action,
            ) = self._contextual_assessment(response)
        except Exception as exc:
            self._record(correlation_id, GuardrailOutcome.ERROR)
            raise GroundingGuardrailError("grounding assessment is malformed") from exc
        if top_action == "GUARDRAIL_INTERVENED" or (
            grounding_action in _BLOCKING_ASSESSMENT_ACTIONS
            or relevance_action in _BLOCKING_ASSESSMENT_ACTIONS
        ):
            self._record(correlation_id, GuardrailOutcome.BLOCKED, action=top_action)
            raise GroundingGuardrailBlocked("grounding policy blocked output")
        outputs = _returned_texts(response.get("outputs", ())) if isinstance(response, Mapping) else None
        if outputs is None or outputs:
            self._record(correlation_id, GuardrailOutcome.ERROR)
            raise GroundingGuardrailError("guardrail output is inconsistent with NONE action")
        if grounding_action != "NONE" or relevance_action != "NONE":
            self._record(correlation_id, GuardrailOutcome.ERROR)
            raise GroundingGuardrailError("grounding assessment action is invalid")
        effective_grounding_threshold = max(self._minimum_score, grounding_threshold)
        effective_relevance_threshold = max(0.5, relevance_threshold)
        # The downstream contract's score is grounding confidence. Relevance
        # has its own threshold and must not be reinterpreted as grounding.
        score = grounding_score
        self._record(
            correlation_id,
            GuardrailOutcome.ALLOWED
            if grounding_score >= effective_grounding_threshold
            and relevance_score >= effective_relevance_threshold
            else GuardrailOutcome.BLOCKED,
        )
        if (
            grounding_score < effective_grounding_threshold
            or relevance_score < effective_relevance_threshold
        ):
            return {
                "grounded": False,
                "score": score,
                "matchedCitationIds": [],
            }
        # Bedrock reports one aggregate score for the tagged source set; it
        # does not attest entailment independently for each passage. These
        # IDs therefore preserve the server-selected support set for the
        # existing contract, rather than claiming per-citation findings.
        return {
            "grounded": True,
            "score": score,
            "matchedCitationIds": list(supporting),
        }

    @staticmethod
    def _contextual_assessment(
        response: object,
    ) -> tuple[str, float, float, float, float, str, str]:
        if not isinstance(response, Mapping) or response.get("action") not in _VALID_ACTIONS:
            raise ValueError("guardrail response is malformed")
        top_action = response["action"]
        assessments = response.get("assessments")
        if not isinstance(assessments, (list, tuple)):
            raise ValueError("assessments are missing")
        for assessment in assessments:
            if not isinstance(assessment, Mapping):
                raise ValueError("assessment is malformed")
            unknown_policies = set(assessment) - (_KNOWN_ASSESSMENT_POLICIES | _ASSESSMENT_METADATA)
            if unknown_policies:
                raise ValueError("unknown policy assessment")
            for policy_name, policy_value in assessment.items():
                if not isinstance(policy_value, Mapping):
                    raise ValueError("policy assessment is malformed")
                if policy_name in _ASSESSMENT_METADATA:
                    continue
                if policy_name != "contextualGroundingPolicy":
                    policy_actions = _assessment_actions(policy_value)
                    if policy_actions is None or (
                        not policy_actions and _has_nonempty_policy_payload(policy_value)
                    ):
                        raise ValueError("policy assessment action is missing")
        all_actions = _assessment_actions(assessments)
        if all_actions is None or any(
            action not in _NON_BLOCKING_ASSESSMENT_ACTIONS | _BLOCKING_ASSESSMENT_ACTIONS
            for action in all_actions
        ):
            raise ValueError("policy assessment is malformed")
        if any(action != "NONE" for action in all_actions):
            # PII/topic/relevance intervention must never be ignored merely
            # because a contextual grounding filter also returned NONE.
            return top_action, 0.0, 1.0, 0.0, 1.0, "BLOCKED", "BLOCKED"
        filters: list[Mapping[str, object]] = []
        for assessment in assessments:
            if not isinstance(assessment, Mapping):
                raise ValueError("assessment is malformed")
            policy = assessment.get("contextualGroundingPolicy")
            if policy is None:
                continue
            if not isinstance(policy, Mapping) or not isinstance(policy.get("filters"), (list, tuple)):
                raise ValueError("contextual grounding policy is malformed")
            for item in policy["filters"]:
                if not isinstance(item, Mapping) or item.get("type") not in {"GROUNDING", "RELEVANCE"}:
                    raise ValueError("contextual grounding filter is malformed")
                filters.append(item)
        if len(filters) != 2 or {item.get("type") for item in filters} != {"GROUNDING", "RELEVANCE"}:
            raise ValueError("exactly one grounding and relevance filter are required")

        parsed: dict[str, tuple[float, float, str]] = {}
        for item in filters:
            filter_type = item["type"]
            score = item.get("score")
            threshold = item.get("threshold")
            action = item.get("action")
            if (
                isinstance(score, bool)
                or not isinstance(score, (int, float))
                or isinstance(threshold, bool)
                or not isinstance(threshold, (int, float))
                or not isinstance(action, str)
                or not 0.0 <= float(score) <= 1.0
                or not 0.0 <= float(threshold) <= 1.0
            ):
                raise ValueError("grounding filter values are malformed")
            parsed[str(filter_type)] = (float(score), float(threshold), action.upper())
        grounding = parsed["GROUNDING"]
        relevance = parsed["RELEVANCE"]
        return top_action, grounding[0], grounding[1], relevance[0], relevance[1], grounding[2], relevance[2]

    def _record(self, correlation_id: str, outcome: GuardrailOutcome, *, action: str | None = None) -> None:
        safe_action = action or ("ERROR" if outcome is GuardrailOutcome.ERROR else "NONE")
        self._audit_sink.record(
            GuardrailAuditEvent(correlation_id, GuardrailStage.OUTPUT, safe_action, outcome)
        )
        emit_telemetry(
            self._telemetry_sink,
            TelemetryEventType.GUARDRAIL,
            correlation_id,
            {
                GuardrailOutcome.ALLOWED: TelemetryOutcome.SUCCEEDED,
                GuardrailOutcome.BLOCKED: TelemetryOutcome.BLOCKED,
                GuardrailOutcome.ERROR: TelemetryOutcome.ERROR,
                GuardrailOutcome.ANONYMIZED: TelemetryOutcome.SUCCEEDED,
            }[outcome],
            operation=GuardrailStage.OUTPUT.value,
            error_code=("guardrail_error" if outcome is GuardrailOutcome.ERROR else None),
        )


def _assessment_actions(value: object) -> tuple[str, ...] | None:
    """Collect action fields from Bedrock's nested assessment unions."""

    actions: list[str] = []

    def visit(node: object) -> bool:
        if isinstance(node, Mapping):
            for key, nested in node.items():
                if key == "action":
                    if not isinstance(nested, str):
                        return False
                    actions.append(nested.upper())
                elif isinstance(nested, (Mapping, list, tuple)):
                    if not visit(nested):
                        return False
                elif nested is not None and not isinstance(
                    nested, (str, int, float, bool)
                ):
                    return False
            return True
        if isinstance(node, (list, tuple)):
            return all(visit(nested) for nested in node)
        return node is None or isinstance(node, (str, int, float, bool))

    if not visit(value):
        return None
    return tuple(actions)


def _has_nonempty_policy_payload(value: object) -> bool:
    """Distinguish a valid empty policy result from malformed populated data."""

    if isinstance(value, Mapping):
        return any(_has_nonempty_policy_payload(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return bool(value) and any(_has_nonempty_policy_payload(item) for item in value)
    return value not in (None, "")


def _returned_texts(value: object) -> tuple[str, ...] | None:
    if value in (None, ()):
        return ()
    if not isinstance(value, (list, tuple)):
        return None
    texts: list[str] = []
    for block in value:
        if not isinstance(block, Mapping):
            return None
        raw_text = block.get("text")
        if isinstance(raw_text, Mapping):
            raw_text = raw_text.get("text")
        if not isinstance(raw_text, str):
            return None
        texts.append(raw_text)
    return tuple(texts)
