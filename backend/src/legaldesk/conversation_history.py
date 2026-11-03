"""Accepted conversation history, shared by the history endpoint and the agentic chat.

The backend is the only owner of conversation memory (ADR-011, ADR-021 D7).
Only events the backend itself accepted (their ids are recorded in the state
store) are ever read back; provider events that are not on that list are
ignored. The agent receives the last pairs of question and final answer and
never intermediate tool results.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence

HISTORY_PAIRS = 6
HISTORY_TEXT_CHARS = 2_000   # per message sent to the agent
HISTORY_TOTAL_CHARS = 12_000  # whole history sent to the agent
MAX_HISTORY_EVENT_TEXT = 8_000


@dataclass(frozen=True, slots=True)
class HistoryEvent:
    event_id: str
    role: str
    text: str
    timestamp: datetime | None = None


def parse_provider_event(item: object) -> HistoryEvent | None:
    """Parse one AgentCore Memory event; never treat arbitrary metadata as text."""

    if not isinstance(item, Mapping):
        return None
    event_id = item.get("eventId")
    if not isinstance(event_id, str) and isinstance(item.get("event"), Mapping):
        event_id = item["event"].get("eventId")
    if not isinstance(event_id, str):
        return None
    source = item.get("event") if isinstance(item.get("event"), Mapping) else item
    role, text = source.get("role"), source.get("text")
    payload = source.get("payload")
    if isinstance(payload, Sequence) and not isinstance(payload, (str, bytes, bytearray)):
        for block in payload:
            if not isinstance(block, Mapping):
                continue
            conversational = block.get("conversational")
            if not isinstance(conversational, Mapping):
                continue
            candidate_role = conversational.get("role")
            content = conversational.get("content")
            candidate_text = content.get("text") if isinstance(content, Mapping) else None
            if isinstance(candidate_role, str) and isinstance(candidate_text, str):
                role, text = candidate_role, candidate_text
                break
    if not isinstance(role, str) or not isinstance(text, str) or len(text) > MAX_HISTORY_EVENT_TEXT:
        return None
    stamp = source.get("eventTimestamp", item.get("eventTimestamp"))
    return HistoryEvent(event_id, role, text, stamp if isinstance(stamp, datetime) else None)


def load_accepted_events(memory: Any, scope: Any, accepted_ids: Sequence[str]) -> list[HistoryEvent]:
    """Events of ``scope`` whose ids the backend accepted, oldest first when timestamps allow."""

    if memory is None:
        return []
    if memory.__class__.__name__.startswith("InMemory"):
        events = [
            HistoryEvent(item.event_id, item.role, item.text, item.event_timestamp)
            for item in memory.list_events(scope) if item.event_id in accepted_ids
        ]
    else:
        raw = memory.list_events(scope, maxResults=100, includePayloads=True)
        provider_events = raw.get("events", ()) if isinstance(raw, Mapping) else ()
        events = []
        if isinstance(provider_events, Sequence):
            for item in provider_events:
                parsed = parse_provider_event(item)
                if parsed is not None and parsed.event_id in accepted_ids:
                    events.append(parsed)
    if events and all(event.timestamp is not None for event in events):
        events.sort(key=lambda event: event.timestamp)  # type: ignore[arg-type,return-value]
    return events


def last_pairs(events: Sequence[HistoryEvent]) -> tuple[tuple[str, str], ...]:
    """The last question/answer pairs as ``(role, text)`` messages for the agent.

    A pair is a user event directly followed by an assistant event; an orphan
    (for example a question whose answer was never accepted) is dropped. Texts
    are cut and the oldest pairs are dropped until the whole fits the budget.
    """

    pairs: list[tuple[str, str]] = []
    pending: str | None = None
    for event in events:
        role = event.role.upper()
        if role == "USER":
            pending = event.text
        elif role == "ASSISTANT" and pending is not None:
            pairs.append((pending, event.text))
            pending = None
        else:
            pending = None
    kept = [(q.strip()[:HISTORY_TEXT_CHARS], a.strip()[:HISTORY_TEXT_CHARS]) for q, a in pairs[-HISTORY_PAIRS:]]
    kept = [(q, a) for q, a in kept if q and a]
    while kept and sum(len(q) + len(a) for q, a in kept) > HISTORY_TOTAL_CHARS:
        kept.pop(0)
    return tuple(message for q, a in kept for message in (("user", q), ("assistant", a)))


__all__ = ["HISTORY_PAIRS", "HistoryEvent", "last_pairs", "load_accepted_events", "parse_provider_event"]
