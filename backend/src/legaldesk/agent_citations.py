"""Opaque citation handles for answers built from tool results (ADR-021 D2).

The browser only ever receives opaque handles. The direct RAG path and the
agentic worker create them with this one function, so they share the same
scope binding and TTL: a handle belongs to one subject, tenant, matter and
conversation, and expires after ``CITATION_HANDLE_TTL_SECONDS``.

The function is pure apart from the injectable clock and token factory; it
stores nothing. The caller persists each pair with
``state_store.put_citation(handle, citation_key=key)``.
"""

from __future__ import annotations

import secrets
import time
from typing import Callable, Iterable

from .agent_output import RenderedCitation
from .state import CitationHandle

CITATION_HANDLE_TTL_SECONDS = 300
MAX_CITATION_HANDLES = 32
MAX_HANDLE_PASSAGE_CHARS = 8_000

CitationKey = tuple[str, str, str, str]


def build_citation_handles(
    *,
    subject: str,
    tenant_id: str,
    matter_id: str,
    conversation_id: str,
    correlation_id: str,
    citations: Iterable[RenderedCitation],
    now: Callable[[], float] = time.time,
    token_factory: Callable[[], str] = lambda: secrets.token_urlsafe(24),
) -> tuple[tuple[CitationHandle, CitationKey], ...]:
    """Return ``(handle, citation_key)`` pairs, one per distinct citation id.

    ``subject``, ``tenant_id`` and ``matter_id`` must come from the server-side
    job/session, never from the browser or the agent. Passages are the text the
    tool returned for this very turn.
    """

    for name, value in (("subject", subject), ("tenant_id", tenant_id), ("matter_id", matter_id),
                        ("conversation_id", conversation_id), ("correlation_id", correlation_id)):
        if not isinstance(value, str) or not value:
            raise ValueError(f"{name} is required")
    unique: dict[str, RenderedCitation] = {}
    for citation in citations:
        if (
            not isinstance(citation.citation_id, str) or not citation.citation_id
            or not isinstance(citation.document_id, str) or not citation.document_id
            or not isinstance(citation.passage, str) or not citation.passage.strip()
        ):
            raise ValueError("citation is malformed")
        unique.setdefault(citation.citation_id, citation)
    if len(unique) > MAX_CITATION_HANDLES:
        raise ValueError("too many citations")
    expires_at = now() + CITATION_HANDLE_TTL_SECONDS
    return tuple(
        (
            CitationHandle(
                handle=token_factory(), subject=subject, tenant_id=tenant_id, matter_id=matter_id,
                conversation_id=conversation_id, document_id=citation.document_id,
                passage=citation.passage[:MAX_HANDLE_PASSAGE_CHARS], expires_at=expires_at,
                is_idp=citation.is_idp,
            ),
            (subject, conversation_id, correlation_id, citation.citation_id),
        )
        for citation in unique.values()
    )


__all__ = [
    "CITATION_HANDLE_TTL_SECONDS",
    "MAX_CITATION_HANDLES",
    "build_citation_handles",
]
