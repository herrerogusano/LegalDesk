"""Narrow, testable client for invoking an AgentCore Harness.

Only the user message and a server-managed session ID are variable. Model,
prompt, tools, skills, and other security-sensitive overrides are deliberately
not exposed through this adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Protocol
from uuid import UUID, uuid4


MIN_SESSION_ID_LENGTH = 33


class HarnessInvocationError(RuntimeError):
    """Raised when AgentCore returns a runtime client error."""


class HarnessDataPlane(Protocol):
    def invoke_harness(self, **kwargs: Any) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class InvokeResult:
    session_id: str
    text: str


def new_session_id() -> str:
    """Return an opaque UUID suitable for AgentCore Runtime session isolation."""

    return str(uuid4())


def validate_session_id(session_id: str) -> str:
    if len(session_id) < MIN_SESSION_ID_LENGTH:
        raise ValueError("session_id must contain at least 33 characters")
    try:
        UUID(session_id)
    except (ValueError, AttributeError) as exc:
        raise ValueError("session_id must be a UUID") from exc
    return session_id


def _text_from_events(events: Iterable[Mapping[str, Any]]) -> str:
    chunks: list[str] = []
    for event in events:
        if "runtimeClientError" in event:
            error = event["runtimeClientError"]
            message = error.get("message", "AgentCore runtime client error")
            raise HarnessInvocationError(str(message))
        delta = event.get("contentBlockDelta", {}).get("delta", {})
        if "text" in delta:
            chunks.append(str(delta["text"]))
    return "".join(chunks)


@dataclass(slots=True)
class HarnessInvoker:
    client: HarnessDataPlane
    harness_arn: str

    @classmethod
    def from_boto3(cls, harness_arn: str, region: str = "eu-west-1") -> "HarnessInvoker":
        import boto3

        return cls(
            client=boto3.client("bedrock-agentcore", region_name=region),
            harness_arn=harness_arn,
        )

    def invoke(self, message: str, *, session_id: str | None = None) -> InvokeResult:
        if not message or not message.strip():
            raise ValueError("message must not be empty")
        effective_session_id = validate_session_id(session_id or new_session_id())
        response = self.client.invoke_harness(
            harnessArn=self.harness_arn,
            runtimeSessionId=effective_session_id,
            messages=[{"role": "user", "content": [{"text": message.strip()}]}],
        )
        return InvokeResult(
            session_id=effective_session_id,
            text=_text_from_events(response["stream"]),
        )
