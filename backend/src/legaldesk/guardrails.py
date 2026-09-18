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


class GuardrailStage(StrEnum):
    INPUT = "input"
    OUTPUT = "output"


class GuardrailOutcome(StrEnum):
    ALLOWED = "allowed"
    ANONYMIZED = "anonymized"
    BLOCKED = "blocked"
    ERROR = "error"


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
