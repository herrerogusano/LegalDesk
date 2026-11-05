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

PRICING_VERSION = "estimate-2026-11-eu-regional"
# USD per million tokens for the pinned Harness model (eu.anthropic.claude-sonnet-4-6).
# ``eu.`` is a regional (geographic cross-region) inference profile. Anthropic's list price for
# Sonnet 4.6 is $3 input / $15 output / $0.30 cache read / $3.75 cache write (5 min) per million,
# and Bedrock regional endpoints carry a 10 % premium over the global endpoint (Anthropic
# pricing page and "Claude on Amazon Bedrock", read 2026-11-02; the AWS pricing page renders its
# Anthropic table with JavaScript and could not be read, so confirm against the bill).
# This is an ESTIMATE kept in code so every report states which table it used; it covers only
# the Harness loop. The tool-internal resolver and writer calls of ``search_documents`` and the
# direct RAG fallback are not part of the Harness usage.
REGIONAL_ENDPOINT_PREMIUM = 1.10
_BASE_PRICE_PER_MILLION_TOKENS = {"input": 3.0, "output": 15.0, "cache_read": 0.30, "cache_write": 3.75}
PRICE_PER_MILLION_TOKENS = {name: round(price * REGIONAL_ENDPOINT_PREMIUM, 6) for name, price in _BASE_PRICE_PER_MILLION_TOKENS.items()}

# A model call of the loop that reported no usage is priced at this upper bound instead of at zero, so that an unknown cost can make a
# stop rule trip but never hide spending: the whole context a call can carry (history, prompts, tool results) and the output cap.
UNKNOWN_CALL_INPUT_TOKENS = 16_000
UNKNOWN_CALL_OUTPUT_TOKENS = 2_048

KNOWN_TOOLS = frozenset({contracts.SEARCH_DOCUMENTS, contracts.GET_EXTRACTED_FIELDS, contracts.LIST_MATTER_DOCUMENTS})

SAFE_META_KEYS = frozenset({
    # job state machine
    "claim", "failure", "quotaReleased", "attempt", "reason", "queueWaitMs",
    # agent loop
    "searchQueryComparisons", "planChars", "planHasJsonStart", "planHasJsonEnd", "planParseStage",
    "loopOutcome", "stopReason", "iterations", "tools", "toolCalls", "toolFailures", "invalidToolResults",
    # output tree
    "outputPath", "fallbackReason", "planError", "omittedBlocks", "blockCount", "citationCount", "memoryWritten",
    # history
    "historyUnavailable", "historyTrimmed", "historyMessages",
    # latency, usage and cost
    "latencyMs", "segmentsMs", "usageAvailable", "tokens", "inputTokens", "outputTokens", "cacheReadTokens",
    "cacheWriteTokens", "estimatedLoopCostUsd", "pricingVersion", "usageComplete", "modelCalls", "costBasis",
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
    if key == "searchQueryComparisons":
        return [v if isinstance(v, str) and v in {"same", "different", "unavailable"} else "unavailable" for v in value[:_MAX_LIST]] if isinstance(value, (list, tuple)) else None
    if key == "stopReason":
        return contracts.closed_stop_reason(value)
    if key == "planChars":
        return value if type(value) is int and 0 <= value <= _MAX_INT else None
    if key in {"planHasJsonStart", "planHasJsonEnd"}:
        return value if type(value) is bool else None
    if key == "planParseStage":
        return value if isinstance(value, str) and value in contracts.PLAN_PARSE_STAGES else None
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


def unknown_call_cost_usd() -> float:
    """Upper bound of one model call that reported no usage."""

    price = PRICE_PER_MILLION_TOKENS
    return (UNKNOWN_CALL_INPUT_TOKENS * price["input"] + UNKNOWN_CALL_OUTPUT_TOKENS * price["output"]) / 1_000_000


def usage_metadata(usage: Any, *, model_calls: int | None = None, usage_records: int | None = None) -> dict[str, object]:
    """Token counts and estimated cost of a loop.

    ``usage`` is the TOTAL of the loop (the sum of its model calls' blocks). ``model_calls`` and ``usage_records`` say how many model
    calls the stream showed and how many of them reported usage: every call that did not is priced at an upper bound, never at zero, and
    the result says so (``usageComplete: False``, ``costBasis: "upper_bound"``) so a stop rule can fail closed. Without ``model_calls``
    (a caller that cannot know) a present usage is taken as complete and an absent one as unavailable, as before.
    """

    if model_calls is None:
        if usage is None:
            return {"usageAvailable": False}
        calls, recorded = None, None
    else:
        calls = max(0, int(model_calls))
        recorded = (0 if usage is None else (calls if usage_records is None else max(0, int(usage_records))))
    missing = 0 if calls is None else max(0, calls - (recorded or 0))
    reported = estimate_loop_cost_usd(usage) or 0.0
    complete = missing == 0
    result: dict[str, object] = {"usageAvailable": usage is not None, "usageComplete": complete, "costBasis": "reported" if complete else "upper_bound"}
    if calls is not None:
        result["modelCalls"] = calls
    if usage is not None:
        result.update(
            tokens=usage.total_tokens, inputTokens=usage.input_tokens, outputTokens=usage.output_tokens,
            cacheReadTokens=usage.cache_read_input_tokens, cacheWriteTokens=usage.cache_write_input_tokens,
        )
    if usage is not None or calls:
        result["estimatedLoopCostUsd"] = round(reported + missing * unknown_call_cost_usd(), 6)
        result["pricingVersion"] = PRICING_VERSION
    return result


def loop_usage_metadata(loop: Any) -> dict[str, object]:
    """``usage_metadata`` of an ``AgentChatResult`` (or any object with the same fields)."""

    return usage_metadata(
        getattr(loop, "usage", None),
        model_calls=getattr(loop, "model_calls", None) if getattr(loop, "model_calls", 0) else None,
        usage_records=getattr(loop, "usage_records_count", None) if getattr(loop, "model_calls", 0) else None,
    )


__all__ = [
    "KNOWN_TOOLS", "PRICE_PER_MILLION_TOKENS", "PRICING_VERSION", "SAFE_META_KEYS", "SEGMENTS", "SegmentTimer",
    "UNKNOWN_CALL_INPUT_TOKENS", "UNKNOWN_CALL_OUTPUT_TOKENS", "estimate_loop_cost_usd", "loop_usage_metadata", "sanitize_job_metadata",
    "unknown_call_cost_usd", "usage_metadata",
]
