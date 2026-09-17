"""Narrow, testable client for invoking an AgentCore Harness.

Only the user message and a server-managed session ID are variable. Model,
prompt, tools, skills, and other security-sensitive overrides are deliberately
not exposed through this adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Iterable, Mapping, Protocol
from uuid import UUID, uuid4


MIN_SESSION_ID_LENGTH = 33
_DERIVED_ACTOR = re.compile(r"^ldactor-[0-9a-f]{48}$")
_DERIVED_SESSION = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


class HarnessInvocationError(RuntimeError):
    """Raised when AgentCore returns a runtime client error."""


class HarnessDataPlane(Protocol):
    def invoke_harness(self, **kwargs: Any) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class InvokeResult:
    session_id: str
    text: str


@dataclass(frozen=True, slots=True, init=False)
class HarnessMemoryScope:
    """Server-derived opaque scope accepted by the Harness adapter.

    There is intentionally no CLI flag or free-form ``actor_id`` parameter on
    :meth:`HarnessInvoker.invoke`.  The trusted backend creates this value
    after authorization and passes it as one object.
    """

    actor_id: str
    session_id: str

    @classmethod
    def from_derived(cls, scope: object) -> "HarnessMemoryScope":
        """Adapt the backend's derived scope without accepting raw selectors."""

        if (
            type(scope).__module__ != "legaldesk.memory"
            or type(scope).__name__ != "MemoryScope"
        ):
            raise TypeError("scope must be produced by the backend memory adapter")
        is_sealed = getattr(scope, "_is_sealed", None)
        if not callable(is_sealed) or not is_sealed():
            raise TypeError("scope must be produced by the backend memory adapter")
        instance = object.__new__(cls)
        object.__setattr__(instance, "actor_id", getattr(scope, "actor_id", None))
        object.__setattr__(instance, "session_id", getattr(scope, "session_id", None))
        instance.__post_init__()
        return instance

    def __post_init__(self) -> None:
        if (
            not isinstance(self.actor_id, str)
            or not self.actor_id
            or not isinstance(self.session_id, str)
            or not self.session_id
        ):
            raise ValueError("memory scope must contain opaque actor/session IDs")
        if not _DERIVED_ACTOR.fullmatch(self.actor_id) or not _DERIVED_SESSION.fullmatch(
            self.session_id
        ):
            raise ValueError("memory scope IDs must be server-derived opaque identifiers")


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

    def invoke(
        self,
        message: str,
        *,
        session_id: str | None = None,
        memory_scope: HarnessMemoryScope | None = None,
    ) -> InvokeResult:
        if not message or not message.strip():
            raise ValueError("message must not be empty")
        if memory_scope is not None and not isinstance(memory_scope, HarnessMemoryScope):
            raise TypeError("memory_scope must be a server-derived HarnessMemoryScope")
        if memory_scope is not None and session_id is not None:
            if validate_session_id(session_id) != memory_scope.session_id:
                raise ValueError("session_id conflicts with memory scope")
        effective_session_id = validate_session_id(
            memory_scope.session_id if memory_scope is not None else (session_id or new_session_id())
        )
        request: dict[str, Any] = {
            "harnessArn": self.harness_arn,
            "runtimeSessionId": effective_session_id,
            "messages": [{"role": "user", "content": [{"text": message.strip()}]}],
        }
        if memory_scope is not None:
            request["actorId"] = memory_scope.actor_id
        response = self.client.invoke_harness(**request)
        try:
            text = _text_from_events(response["stream"])
        except HarnessInvocationError:
            raise
        except Exception as exc:
            if exc.__class__.__name__ != "EventStreamError":
                raise
            raise HarnessInvocationError(str(exc)) from exc
        return InvokeResult(session_id=effective_session_id, text=text)
