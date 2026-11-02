"""Content-free telemetry of one agentic chat job (ADR-021 D9, PLAN_15 T7).

The worker already computes the facts (tools, iterations, output path, fallback
reason, usage); this module only decides **what may leave the process**:

* ``SAFE_META_KEYS`` is the allowlist of job attributes;
* ``sanitize_job_metadata`` keeps a value only if it has a closed, content-free
  shape (identifier-like strings, numbers, booleans, short lists of those). A
  free-text value can therefore never reach CloudWatch, even if a future change
  puts one into the metadata by mistake;
* ``SegmentTimer`` and ``estimate_loop_cost_usd`` produce the latency per stage
  and the estimated cost.

Nothing here sees a question, an answer, a passage or a token.
"""

from __future__ import annotations

import re
import time
from contextlib import contextmanager
from typing import Any, Iterator, Mapping

from . import agent_contracts as contracts

# Stages of one job, in execution order. The names are a closed vocabulary.
SEGMENTS = ("session", "bind", "history", "agent", "resolve", "fallback", "publish")

PRICING_VERSION = "estimate-2026-11"
# USD per million tokens for the pinned Harness model (eu.anthropic.claude-sonnet-4-6).
# This is an ESTIMATE kept in code so every report states which table it used; it
# covers only the Harness loop. The tool-internal resolver and writer calls of
# ``search_documents`` and the direct RAG fallback are not part of the Harness usage.
PRICE_PER_MILLION_TOKENS = {"input": 3.0, "output": 15.0, "cache_read": 0.30, "cache_write": 3.75}

KNOWN_TOOLS = frozenset({contracts.SEARCH_DOCUMENTS, contracts.GET_EXTRACTED_FIELDS, contracts.LIST_MATTER_DOCUMENTS})

SAFE_META_KEYS = frozenset({
    # job state machine
    "claim", "failure", "quotaReleased", "attempt", "reason", "queueWaitMs",
    # agent loop
    "loopOutcome", "stopReason", "iterations", "tools", "toolCalls", "toolFailures", "invalidToolResults",
    # output tree
    "outputPath", "fallbackReason", "planError", "omittedBlocks", "blockCount", "citationCount", "memoryWritten",
    # history
    "historyUnavailable", "historyTrimmed", "historyMessages",
    # latency, usage and cost
    "latencyMs", "segmentsMs", "usageAvailable", "tokens", "inputTokens", "outputTokens", "cacheReadTokens",
    "cacheWriteTokens", "estimatedLoopCostUsd", "pricingVersion",
    # ids and hashes (never the subject, the question or the answer)
    "correlationId", "agentPromptVersion", "agentPromptSha256",
})

# Identifier-like: lowercase, digits and the separators of ids, versions and hashes.
_SAFE_TEXT = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,63}$")
_MAX_LIST = 16
_MAX_INT = 10**12


def _safe_text(value: object) -> str | None:
    return value if isinstance(value, str) and _SAFE_TEXT.fullmatch(value) else None


def _safe_number(value: object) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        return None
    if isinstance(value, int):
        return value if abs(value) <= _MAX_INT else None
    return round(value, 6) if abs(value) <= _MAX_INT else None


def _safe_value(key: str, value: object) -> object | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return _safe_number(value)
    if isinstance(value, str):
        return _safe_text(value)
    if key == "tools" and isinstance(value, (list, tuple)):
        # A tool name that is not one of ours is reported as "other", never verbatim.
        return [name if name in KNOWN_TOOLS else "other" for name in value[:_MAX_LIST] if isinstance(name, str)]
    if key == "omittedBlocks" and isinstance(value, (list, tuple)):
        items = []
        for item in value[:_MAX_LIST]:
            if isinstance(item, Mapping) and set(item) == {"index", "error"}:
                index, error = _safe_number(item["index"]), _safe_text(item["error"])
                if isinstance(index, int) and error is not None:
                    items.append({"index": index, "error": error})
        return items
    if key == "segmentsMs" and isinstance(value, Mapping):
        return {name: ms for name in SEGMENTS if isinstance(ms := _safe_number(value.get(name)), int)}
    return None


def sanitize_job_metadata(metadata: Mapping[str, object] | None) -> dict[str, object]:
    """The subset of ``metadata`` that is allowlisted and has a closed shape.

    ``None`` values are dropped so a CloudWatch metric filter can test presence
    with ``$.field = *``.
    """

    safe: dict[str, object] = {}
    for key, value in (metadata or {}).items():
        if key not in SAFE_META_KEYS:
            continue
        cleaned = _safe_value(key, value)
        if cleaned is not None:
            safe[key] = cleaned
    return safe


class SegmentTimer:
    """Milliseconds spent in each named stage of a job (a stage may repeat; times add up)."""

    def __init__(self) -> None:
        self._ms: dict[str, int] = {}

    @contextmanager
    def segment(self, name: str) -> Iterator[None]:
        if name not in SEGMENTS:
            raise ValueError("segment is not in the closed vocabulary")
        started = time.perf_counter()
        try:
            yield
        finally:  # a stage that raises still shows how long it ran
            self._ms[name] = self._ms.get(name, 0) + max(0, int((time.perf_counter() - started) * 1000))

    def as_dict(self) -> dict[str, int]:
        return {name: self._ms[name] for name in SEGMENTS if name in self._ms}


def estimate_loop_cost_usd(usage: Any) -> float | None:
    """Estimated USD of the Harness loop from its token usage; ``None`` without usage."""

    if usage is None:
        return None
    price = PRICE_PER_MILLION_TOKENS
    # ``input_tokens`` is the non-cached input; cache reads and writes are priced apart.
    cost = (
        usage.input_tokens * price["input"] + usage.output_tokens * price["output"]
        + usage.cache_read_input_tokens * price["cache_read"] + usage.cache_write_input_tokens * price["cache_write"]
    ) / 1_000_000
    return round(cost, 6)


def usage_metadata(usage: Any) -> dict[str, object]:
    """Token counts and estimated cost of a loop, or just ``usageAvailable: False``."""

    if usage is None:
        return {"usageAvailable": False}
    return {
        "usageAvailable": True, "tokens": usage.total_tokens, "inputTokens": usage.input_tokens,
        "outputTokens": usage.output_tokens, "cacheReadTokens": usage.cache_read_input_tokens,
        "cacheWriteTokens": usage.cache_write_input_tokens,
        "estimatedLoopCostUsd": estimate_loop_cost_usd(usage), "pricingVersion": PRICING_VERSION,
    }


__all__ = [
    "KNOWN_TOOLS", "PRICE_PER_MILLION_TOKENS", "PRICING_VERSION", "SAFE_META_KEYS", "SEGMENTS", "SegmentTimer",
    "estimate_loop_cost_usd", "sanitize_job_metadata", "usage_metadata",
]
