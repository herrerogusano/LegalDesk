"""Deterministic local seam for health and transport smoke tests.

This is not a local LLM and does not pretend to validate model quality. The
managed Harness owns the real orchestration loop.
"""

from __future__ import annotations

from dataclasses import dataclass

from .client import InvokeResult, new_session_id, validate_session_id


@dataclass(frozen=True, slots=True)
class LocalAgent:
    name: str = "legaldesk_phase_01"

    def health(self) -> dict[str, str]:
        return {"status": "ok", "agent": self.name, "mode": "local-test-double"}

    def invoke(self, message: str, *, session_id: str | None = None) -> InvokeResult:
        if not message or not message.strip():
            raise ValueError("message must not be empty")
        effective_session_id = validate_session_id(session_id or new_session_id())
        return InvokeResult(
            session_id=effective_session_id,
            text="LegalDesk Phase 01 local invocation is healthy.",
        )
