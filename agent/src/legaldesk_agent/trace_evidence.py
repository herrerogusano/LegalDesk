"""Local, metadata-only normalization for Harness/tool evidence.

AgentCore's managed trace is provider-owned.  This module only consumes
allowlisted metadata already available to the application (or test doubles);
it never parses prompts, answers, passages, tokens, or secrets.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping


_DECISION_KEYS = {"authorizationdecision", "decision", "authdecision"}
_CODE_KEYS = {"authorizationcode", "errorcode", "policycode", "denycode"}
_TOOL_KEYS = {"tool", "toolname", "targettool"}
_TARGET_KEYS = {"targetinvoked", "target_invoked"}
_TRACE_KEYS = {"requestid", "request_id", "traceid", "trace_id", "correlationid"}
_DENY_CODES = {"CROSS_MATTER", "ACCESS_DENIED", "DENY", "UNAUTHORIZED"}
_GUARDRAIL_DECISIONS = {"ALLOW", "BLOCK", "ANONYMIZE", "ERROR", "UNAVAILABLE"}
_STOP_REASONS = {"END_TURN", "TOOL_USE", "MAX_TOKENS", "ERROR", "UNAVAILABLE"}
_FINAL_VALIDATIONS = {"VALID", "INVALID", "UNAVAILABLE"}
_TOOL_RESULTS = {"SUCCEEDED", "BLOCKED", "ERROR", "UNAVAILABLE"}


@dataclass(frozen=True, slots=True)
class StructuredHarnessEvidence:
    """Safe decision metadata used by a local acceptance overlay."""

    authorization_decision: str | None
    authorization_code: str | None
    target_invoked: bool | None
    tool_name: str | None
    tool_call_id: str | None
    tool_result: str | None
    guardrail_decision: str | None
    stop_reason: str | None
    final_validation: str | None
    trace_id_present: bool
    source: str
    structured: bool

    @property
    def accepted_cross_matter_deny(self) -> bool:
        return (
            self.authorization_decision == "DENY"
            and self.authorization_code == "CROSS_MATTER"
            and self.target_invoked is False
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "authorizationDecision": self.authorization_decision,
            "authorizationCode": self.authorization_code,
            "targetInvoked": self.target_invoked,
            "toolName": self.tool_name,
            "toolCallId": self.tool_call_id,
            "toolResult": self.tool_result,
            "guardrailDecision": self.guardrail_decision,
            "stopReason": self.stop_reason,
            "finalValidation": self.final_validation,
            "traceIdPresent": self.trace_id_present,
            "source": self.source,
            "structured": self.structured,
            "acceptedCrossMatterDeny": self.accepted_cross_matter_deny,
        }


def _safe_items(value: object) -> Iterable[tuple[str, object]]:
    if not isinstance(value, Mapping):
        return ()
    return ((str(key).casefold(), item) for key, item in value.items())


def normalize_harness_evidence(
    *sources: object,
    source_name: str = "local",
) -> StructuredHarnessEvidence:
    """Extract only known metadata keys from Harness/interceptor events.

    Unknown keys are ignored.  This deliberately returns ``structured=False``
    when no allowlisted decision fields are present; text keyword matching is
    never upgraded into security evidence.
    """

    decision: str | None = None
    code: str | None = None
    tool: str | None = None
    tool_call_id: str | None = None
    tool_result: str | None = None
    guardrail: str | None = None
    stop_reason: str | None = None
    final_validation: str | None = None
    target: bool | None = None
    trace_present = False
    found_structured = False

    def visit(value: object) -> None:
        nonlocal decision, code, tool, tool_call_id, tool_result, guardrail, stop_reason, final_validation, target, trace_present, found_structured
        if isinstance(value, (list, tuple)):
            for nested in value:
                visit(nested)
            return
        for key, item in _safe_items(value):
            if key in _DECISION_KEYS and isinstance(item, str):
                normalized = item.upper()
                if normalized in {"ALLOW", "DENY", "UNKNOWN"}:
                    decision = normalized
                    found_structured = True
            elif key in _CODE_KEYS and isinstance(item, str):
                normalized = item.upper()
                if normalized in _DENY_CODES or normalized in {"ALLOW", "OK", "SUCCESS"}:
                    code = normalized
                    found_structured = True
            elif key in _TOOL_KEYS and isinstance(item, str) and item:
                # Keep names, not payloads.  Tool names are bounded metadata.
                tool = item[:128]
                found_structured = True
            elif key in {"toolcallid", "tool_call_id"} and isinstance(item, str) and item and len(item) <= 128:
                tool_call_id = item
                found_structured = True
            elif key in {"toolresult", "tool_result"} and isinstance(item, str) and item.upper() in _TOOL_RESULTS:
                tool_result = item.upper()
                found_structured = True
            elif key in {"guardraildecision", "guardrail_decision"} and isinstance(item, str) and item.upper() in _GUARDRAIL_DECISIONS:
                guardrail = item.upper()
                found_structured = True
            elif key in {"stopreason", "stop_reason"} and isinstance(item, str) and item.upper() in _STOP_REASONS:
                stop_reason = item.upper()
                found_structured = True
            elif key in {"finalvalidation", "final_validation"} and isinstance(item, str) and item.upper() in _FINAL_VALIDATIONS:
                final_validation = item.upper()
                found_structured = True
            elif key in _TARGET_KEYS and type(item) is bool:
                target = item
                found_structured = True
            elif key in _TRACE_KEYS and isinstance(item, str) and item:
                trace_present = True
            if isinstance(item, Mapping):
                visit(item)
            elif isinstance(item, (list, tuple)):
                for nested in item:
                    visit(nested)

    for source in sources:
        visit(source)

    # A denied cross-matter decision is accepted only when all three explicit
    # fields are available; never infer it from a textual error message.
    return StructuredHarnessEvidence(
        authorization_decision=decision,
        authorization_code=code,
        target_invoked=target,
        tool_name=tool,
        tool_call_id=tool_call_id,
        tool_result=tool_result,
        guardrail_decision=guardrail,
        stop_reason=stop_reason,
        final_validation=final_validation,
        trace_id_present=trace_present,
        source=source_name,
        structured=found_structured,
    )


class HarnessEvidenceCollector:
    """Bounded local collector for one Harness attempt."""

    def __init__(self, *, source_name: str = "local") -> None:
        self.source_name = source_name
        self._sources: list[object] = []

    def add(self, value: object) -> None:
        if isinstance(value, Mapping):
            self._sources.append(value)

    def normalize(self) -> StructuredHarnessEvidence:
        return normalize_harness_evidence(*self._sources, source_name=self.source_name)


__all__ = [
    "HarnessEvidenceCollector",
    "StructuredHarnessEvidence",
    "normalize_harness_evidence",
]
