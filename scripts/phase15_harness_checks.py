"""Live checks of the Harness behaviour that the agentic chat depends on (PLAN_15 T9.2, T9.5, ADR-021).

Three checks, one report, a fixed budget of **5 Harness invocations and no retries**:

``allowed_tools`` (pre-check, 1 invocation)
    The agent chat sends ``tools`` and ``allowedTools`` on every invocation, naming
    ``search_documents`` and ``get_extracted_fields``, which the Harness template's own
    ``AllowedTools`` does not list. This asks Harness for a trivial reply with the real sealed
    binding and reports whether it *rejects* that request. The cut-loop check then reports whether
    it rejects the tools when the model *uses* them. Run it before the paid eval.

``session`` (2 invocations, no tools, no memory)
    Without memory, the same ``runtimeSessionId`` must not keep a conversation of its own (D7).
    Turn 1 carries a word that turn 2 never spells; if turn 2 repeats it, Harness kept the history.
    The word, the two prompts' own tokens and the acknowledgement token are all different, so a hit
    can only come from history.

``tool_results`` (2 invocations, tools on, ``maxIterations`` forced to 1 in this process only)
    The loop ends right after the first tool call. The check asks whether the completed tool result
    is still in the stream (D5 relies on it) and what the production output tree does with it.

Safety properties (same pattern as ``phase15_adot_marker_probe.py``):

* the default command is a dry run: it prints the plan and imports neither boto3 nor the agent or
  backend packages;
* execution needs ``--execute``, an approval reference, the release identity and the token in
  ``LEGALDESK_EVAL_ACCESS_TOKEN``; the Harness must be the worker's own (``LEGALDESK_HARNESS_ARN``);
* the budget is spent *before* each call, so a failed call counts; there is no retry; two failures in a
  row stop the run;
* once the first paid call is attempted, the report is ALWAYS written (also if a later step raises);
* the report is create-only and holds closed verdicts, booleans, counts and SHA-256 of the markers. It
  never stores a marker, a question, an answer or a tool payload.

Not run by CI. T9 runs it with cost approval; see ``docs/phase-15-live-runbook.md``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
CHECKS_VERSION = "1.2.0"
SCHEMA_VERSION = "phase15-harness-checks-2"
REPORT_PREFIX = "phase15-harness-checks-"
RESULTS_ROOT = ROOT / "evals" / "results"
REGION = "eu-west-1"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))  # run as `python scripts/phase15_harness_checks.py`: make `evals` importable
from evals.residual_state import GRANT_TTL_SECONDS, ResidualState  # noqa: E402,F401 - shared with the routing eval

# Tools the agent chat sends per invocation that the Harness template's AllowedTools does not list.
NEW_TOOLS = frozenset({"search_documents", "get_extracted_fields"})
KNOWN_TOOL_NAMES = NEW_TOOLS | {"list_matter_documents", "get_document_metadata"}
INVOCATIONS = {"allowed_tools": 1, "session": 2, "tool_results": 2}
MAX_INVOCATIONS = sum(INVOCATIONS.values())
MAX_CONSECUTIVE_FAILURES = 2
ALLOWED_TOOLS_QUESTION = "Synthetic availability check. Reply with the single word OK and do not call any tool."
DEFAULT_CUT_QUESTION = "What does the notice clause of the lease say?"

# Closed vocabularies of the report.
ALLOWED_TOOLS = (
    "accepted", "request_rejected", "tool_use_rejected", "client_parser_rejected", "request_accepted_tool_use_not_exercised", "inconclusive", "not_run",
)
# The worker (invoke_agent_chat) uses a NEW runtimeSessionId on every call, so Harness keeping the history of one session id is a
# known, handled behaviour of Harness (found by this check on 2026-11-03), not a defect of the worker. A test pins the claim to the
# adapter (tests/test_phase15_agent_gateway_selector_and_session.py); if it ever stops being true that test fails first.
WORKER_USES_A_FRESH_SESSION_PER_INVOCATION = True
# Closed reasons of a failed tool result; identical to legaldesk_agent.client.ERROR_HINTS (a test compares them).
ERROR_HINTS = (
    "missing_required_argument", "invalid_arguments", "access_denied", "tool_not_allowed", "timeout", "throttled", "no_text", "unclassified",
)
# Verdicts after which the cut-loop check is not run (it would fail the same way and spend two paid calls).
REJECTED_VERDICTS = frozenset({"request_rejected", "tool_use_rejected", "client_parser_rejected"})
# What the report may say about the acknowledgement of turn 1, so that an inconclusive session says WHY without storing any text.
ACK_DIAGNOSES = ("whole_word", "inside_longer_token", "different_case", "empty_answer", "absent")
SESSION = ("history_not_retained", "history_retained", "inconclusive", "invoke_failed", "not_run")
TOOL_RESULTS = ("retained", "lost", "inconclusive", "invoke_failed", "skipped_tools_rejected", "not_run")
OVERALL = ("all_passed", "findings", "inconclusive", "aborted_after_errors")
INTERPRETATION = {
    "allowed_tools": {
        "accepted": "Harness accepted the per-invocation tools and a new tool completed: the template AllowedTools is not an obstacle",
        "request_rejected": "Harness rejected the per-invocation tools at request time: add search_documents and get_extracted_fields to the Harness AllowedTools (owner decision) before the eval",
        "tool_use_rejected": "Harness itself refused the new tools when the model used them (an error from Harness that points at the tools): same fix as request_rejected",
        "client_parser_rejected": "OUR stream parser (legaldesk_agent.client) rejected the stream; Harness did not refuse anything. Do not change the Harness AllowedTools: fix the parser (live 2026-11-03: it did not know the name format Harness uses for Gateway tools)",
        "request_accepted_tool_use_not_exercised": "the request was accepted but no new tool completed: not conclusive; the eval will show it",
        "inconclusive": "the pre-check failed for another reason (errorCode/errorClass; a ValidationException that does not point at the tools lands here): no conclusion",
        "not_run": "not run",
    },
    "session": {
        "history_not_retained": "without memory the session keeps no history of its own (bounded evidence): the worker's session scheme is fine",
        "history_retained": "Harness keeps the history of a runtimeSessionId even with memory disabled. The check reuses one id on purpose, so this verdict is expected; the worker uses a new runtimeSessionId on every call (see sessionPolicy), so it is a handled finding, not a defect",
        "inconclusive": "the model did not acknowledge turn 1 as a whole word (see ackDiagnosis for why, without any text): no conclusion; a rerun needs a new approval",
        "invoke_failed": "an invocation failed: no conclusion; a rerun needs a new approval",
        "not_run": "not run",
    },
    "tool_results": {
        "retained": "the completed tool result is in the stream after the loop is cut: ADR-021 D5 holds as written",
        "lost": "the tool result is lost when the loop is cut: the output tree must go straight to the direct RAG fallback (code change)",
        "inconclusive": "the model did not call a tool, or its tool calls failed or were conclusive in only one call (see toolErrorHints for why a call failed, as a closed label): no conclusion",
        "invoke_failed": "an invocation failed: no conclusion; a rerun needs a new approval",
        "skipped_tools_rejected": "not run because the allowed_tools pre-check was rejected",
        "not_run": "not run",
    },
}
_APPROVAL_REF = re.compile(r"^[A-Za-z0-9_.:/#-]{3,120}$")
_GIT_SHA = re.compile(r"^[0-9a-f]{40,64}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_HARNESS_ARN = re.compile(r"^arn:aws:bedrock-agentcore:[a-z0-9-]+:\d{12}:harness/[A-Za-z0-9_-]+$")
_CLOSED_TEXT = re.compile(r"^[a-z0-9_.-]{1,40}$")


class BudgetExceeded(RuntimeError):
    """A call beyond the fixed budget was requested; it was not made."""


class CallBudget:
    """Counts a paid call *before* it is made, so a failed call is spent too."""

    def __init__(self, limit: int = MAX_INVOCATIONS) -> None:
        self.limit, self.used = limit, 0

    def spend(self) -> None:
        if self.used >= self.limit:
            raise BudgetExceeded("invocation budget exhausted")
        self.used += 1


@dataclass(frozen=True)
class Backends:
    """What the checks need from the outside; injected so the logic is testable without AWS."""

    plain: Callable[[str, str], str]  # (message, runtimeSessionId) -> text; no memory, no tools
    agent: Callable[[str], Any]  # question -> AgentChatResult-like; sealed binding, maxIterations forced to 1
    resolve: Callable[[Any], str | None]  # the production output tree: loop -> output path
    new_session_id: Callable[[], str]
    residual: ResidualState = field(default_factory=ResidualState)


def build_plan() -> dict[str, object]:
    return {
        "checksVersion": CHECKS_VERSION, "harnessInvocations": MAX_INVOCATIONS, "perCheck": dict(INVOCATIONS), "retries": 0,
        "stopAfterConsecutiveFailures": MAX_CONSECUTIVE_FAILURES, "toolResultsMaxIterations": 1,
        "newToolsNotInTheHarnessTemplate": sorted(NEW_TOOLS), "reportStoresMarkersQuestionsOrAnswers": False,
        "residualState": "one conversation binding (no TTL) and one 5-minute invocation grant per agent call, ids recorded in the report",
    }


def _error_code(exc: BaseException) -> str:
    """A closed-shape code: the AWS error code if it has a safe shape, else the class name."""

    response = getattr(exc, "response", None)
    error = response.get("Error") if isinstance(response, Mapping) else None
    code = error.get("Code") if isinstance(error, Mapping) else None
    for candidate in (code, type(exc).__name__):
        if isinstance(candidate, str) and re.fullmatch(r"[A-Za-z0-9_.]{1,64}", candidate):
            return candidate
    return "unknown"


def _closed(value: object) -> str | None:
    """A Harness-provided label (stop reason, loop outcome) only if it has a safe shape."""

    return value if isinstance(value, str) and _CLOSED_TEXT.fullmatch(value.lower()) else ("other" if value else None)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _whole_token(token: str, text: str) -> bool:
    """``token`` appears in ``text`` as a whole word, not inside a longer run of letters or digits."""

    return re.search(r"(?<![A-Za-z0-9])" + re.escape(token) + r"(?![A-Za-z0-9])", text) is not None


# A closed hint read from a ValidationException's message to attribute it to the tools list. The message itself
# is matched in memory and never stored, logged or copied into the report.
_ALLOWED_TOOLS_HINT = re.compile(r"allowed[\s_-]*tools?|tools?\b.{0,60}\bnot\b.{0,30}\b(?:allowed|permitted|configured|listed)\b")


def _is_client_parser_error(exc: BaseException) -> bool:
    """OUR parser refused the stream. By type when the agent package raised it, by its fixed message for older callers."""

    return any(cls.__name__ == "ClientStreamParserError" for cls in type(exc).__mro__) or "invalid event stream" in str(exc).lower()


def classify_agent_error(exc: BaseException) -> str:
    """Attribute a failed invocation, with a closed answer.

    ``request_rejected`` only for a ``ValidationException`` whose message points at the allowed tools;
    ``validation_unattributed`` for any other ``ValidationException`` (a bad token limit, a malformed header...);
    ``client_parser_rejected`` when OUR stream parser refused the stream (not a Harness refusal: it used to be reported as one);
    ``harness_tool_rejected`` when Harness itself failed the invocation with a message that points at the allowed tools;
    ``other`` otherwise.
    """

    if _error_code(exc) == "ValidationException":
        return "request_rejected" if _ALLOWED_TOOLS_HINT.search(str(exc).lower()) else "validation_unattributed"
    if _is_client_parser_error(exc):
        return "client_parser_rejected"
    if any(cls.__name__ in {"HarnessInvocationError", "EventStreamError"} for cls in type(exc).__mro__) and _ALLOWED_TOOLS_HINT.search(str(exc).lower()):
        return "harness_tool_rejected"  # an error Harness itself put in the stream, pointing at the tools (never a client-side or throttling error)
    return "other"


class _State:
    def __init__(self, budget: CallBudget) -> None:
        self.budget, self.consecutive_failures, self.aborted = budget, 0, False

    def failed(self) -> None:
        self.consecutive_failures += 1
        if self.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            self.aborted = True

    def ok(self) -> None:
        self.consecutive_failures = 0


def check_allowed_tools(backends: Backends, state: _State) -> dict[str, object]:
    record: dict[str, object] = {"verdict": "not_run", "invocations": 0, "errorCode": None, "errorClass": None}
    state.budget.spend()
    record["invocations"] = 1
    try:
        backends.agent(ALLOWED_TOOLS_QUESTION)
    except BudgetExceeded:
        raise
    except Exception as exc:  # noqa: BLE001 - recorded, never retried
        kind = classify_agent_error(exc)
        record["errorCode"], record["errorClass"] = _error_code(exc), kind
        record["verdict"] = {
            "request_rejected": "request_rejected", "harness_tool_rejected": "tool_use_rejected", "client_parser_rejected": "client_parser_rejected",
        }.get(kind, "inconclusive")
        state.failed()
        return record
    state.ok()
    record["verdict"] = "request_accepted_tool_use_not_exercised"  # upgraded by the tool_results check
    return record


def ack_diagnosis(ack: str, text: str) -> str:
    """Why an acknowledgement did or did not count, as a closed label. Computed in memory; the text is never kept."""

    if _whole_token(ack, text):
        return "whole_word"
    if not text.strip():
        return "empty_answer"
    if ack in text:
        return "inside_longer_token"
    if ack.lower() in text.lower():
        return "different_case"
    return "absent"


def _recalled(core: str, text: str) -> bool:
    """The random core of the word, in any case, anywhere in the answer. Deliberately loose: a false hit is impossible (64 random bits
    that only turn 1 carried) and a false miss would be the dangerous error, a wrong 'history not retained'."""

    return core.lower() in text.lower()


def check_session(backends: Backends, state: _State) -> dict[str, object]:
    record: dict[str, object] = {
        "verdict": "not_run", "invocations": 0, "errorCode": None, "acknowledged": None, "ackDiagnosis": None, "recalled": None,
        "turn1Chars": None, "turn2Chars": None,
    }
    # Short random cores (the earlier 32-hex-digit tokens are easy for a model to miscopy). The acknowledgement still has to come back as a
    # whole word with the exact case; only the word of turn 1 is matched loosely (see _recalled).
    word_core, token_1, token_2, ack = uuid4().hex[:16], "LDCHKA" + uuid4().hex[:12], "LDCHKB" + uuid4().hex[:12], "LDACK" + uuid4().hex[:12]
    word = "LDWORD" + word_core
    record["markerSha256"] = {"word": _sha(word), "turn1": _sha(token_1), "turn2": _sha(token_2), "ack": _sha(ack)}
    session_id = backends.new_session_id()
    record["sessionIdSha256"] = _sha(session_id)
    framing = "This is an automated LegalDesk operations check, not a legal question. Call no tool."
    turn_1 = f"{framing} Check id {token_1}. Remember this word for later: {word}. Now reply with this token and nothing else: {ack}"
    turn_2 = (
        f"{framing} Check id {token_2}. What was the word I asked you to remember earlier in this conversation? "
        "Reply with that word and nothing else. If you do not know it, reply exactly: UNKNOWN"
    )
    answers: list[str] = []
    for message in (turn_1, turn_2):
        state.budget.spend()
        record["invocations"] = int(record["invocations"]) + 1
        try:
            text = backends.plain(message, session_id)
        except BudgetExceeded:
            raise
        except Exception as exc:  # noqa: BLE001
            record["errorCode"], record["verdict"] = _error_code(exc), "invoke_failed"
            state.failed()
            return record
        state.ok()
        answers.append(text if isinstance(text, str) else "")
    # Computed in memory, never stored. The acknowledgement is a random, unique token matched as a whole word
    # (so "back" or "acknowledge" can never count, and neither can a partial echo of the token).
    acknowledged, recalled = _whole_token(ack, answers[0]), _recalled(word_core, answers[1])
    record["acknowledged"], record["recalled"] = acknowledged, recalled
    record["ackDiagnosis"] = ack_diagnosis(ack, answers[0])
    record["turn1Chars"], record["turn2Chars"] = min(len(answers[0]), 100_000), min(len(answers[1]), 100_000)
    if recalled:
        record["verdict"] = "history_retained"
    elif acknowledged:
        record["verdict"] = "history_not_retained"
    else:
        record["verdict"] = "inconclusive"
    return record


def check_tool_results(backends: Backends, state: _State, question: str) -> dict[str, object]:
    record: dict[str, object] = {"verdict": "not_run", "invocations": 0, "calls": [], "errorCode": None, "newToolCompleted": False, "clientParserRejected": False, "harnessToolRejected": False}
    outcomes: list[str] = []
    for _ in range(INVOCATIONS["tool_results"]):
        state.budget.spend()
        record["invocations"] = int(record["invocations"]) + 1
        call: dict[str, object] = {"outcome": "invoke_failed"}
        try:
            loop = backends.agent(question)
        except BudgetExceeded:
            raise
        except Exception as exc:  # noqa: BLE001
            call["errorCode"], call["errorClass"] = _error_code(exc), classify_agent_error(exc)
            record["errorCode"] = call["errorCode"]
            record["clientParserRejected"] = bool(record["clientParserRejected"]) or call["errorClass"] == "client_parser_rejected"
            record["harnessToolRejected"] = bool(record["harnessToolRejected"]) or call["errorClass"] in {"harness_tool_rejected", "request_rejected"}
            state.failed()
            outcomes.append("invoke_failed")
            record["calls"].append(call)  # type: ignore[union-attr]
            if state.aborted:
                break
            continue
        state.ok()
        outcome, retained_new = classify_cut_loop(loop)
        calls = tuple(getattr(loop, "tool_calls", ()))
        call.update({
            "loopOutcome": _closed(getattr(loop, "loop_outcome", None)), "stopReason": _closed(getattr(loop, "stop_reason", None)),
            "iterations": getattr(loop, "iterations", None), "toolCalls": len(calls),
            "toolNames": [name if name in KNOWN_TOOL_NAMES else "other" for name in (getattr(item, "short_name", None) for item in calls)],
            "toolStatuses": [status if status in {"SUCCESS", "ERROR"} else "other" for status in (getattr(item, "status", None) for item in calls)],
            "toolErrorHints": [
                (hint if hint in ERROR_HINTS else "other") for hint in (getattr(item, "error_hint", None) for item in calls if getattr(item, "status", None) != "SUCCESS")
            ],
            "completedNewToolResults": retained_new, "outputPath": None, "outcome": outcome,
        })
        if outcome == "retained":
            try:
                call["outputPath"] = _closed(backends.resolve(loop))
            except Exception as exc:  # noqa: BLE001 - the output tree is not the question asked here
                call["resolveErrorCode"] = _error_code(exc)
        if retained_new:
            record["newToolCompleted"] = True
        outcomes.append(outcome)
        record["calls"].append(call)  # type: ignore[union-attr]
    if "lost" in outcomes:
        record["verdict"] = "lost"
    elif "retained" in outcomes:
        record["verdict"] = "retained"
    elif outcomes and all(item == "invoke_failed" for item in outcomes):
        record["verdict"] = "invoke_failed"
    else:
        record["verdict"] = "inconclusive"
    return record


def classify_cut_loop(loop: Any) -> tuple[str, int]:
    """(outcome, completed results of the tools outside the template) for one loop cut after its first tool.

    * ``retained``: any tool that completed with ``SUCCESS`` still has its structured result in the stream;
    * ``lost``: the evidence says a result should be there and is not: a ``SUCCESS`` result whose content is
      empty, or the model asked for a tool (stop reason ``tool_use``) and the stream holds no tool result at all;
    * ``inconclusive``: no tool was called, or only ``ERROR`` results (a denial or a tool failure says nothing
      about whether results survive a cut), or the loop ended for another reason.
    """

    calls = tuple(getattr(loop, "tool_calls", ()))
    complete = [item for item in calls if getattr(item, "status", None) == "SUCCESS" and getattr(item, "payload", None)]
    new_tools = sum(1 for item in complete if getattr(item, "short_name", None) in NEW_TOOLS)
    if complete:
        return "retained", new_tools
    if any(getattr(item, "status", None) == "SUCCESS" for item in calls):
        return "lost", 0  # completed successfully, yet no content survived
    if not calls and getattr(loop, "stop_reason", None) == "tool_use":
        return "lost", 0  # requested, and the stream has no result for it
    return "inconclusive", 0


def run_checks(backends: Backends, budget: CallBudget, results: dict[str, dict[str, object]], *, cut_question: str = DEFAULT_CUT_QUESTION) -> None:
    """Runs the three checks in order, writing each result into ``results`` as soon as it exists."""

    state = _State(budget)
    results["allowed_tools"] = check_allowed_tools(backends, state)
    if not state.aborted:
        results["session"] = check_session(backends, state)
    if results["allowed_tools"]["verdict"] in REJECTED_VERDICTS:
        results["tool_results"] = {"verdict": "skipped_tools_rejected", "invocations": 0}
    elif not state.aborted:
        results["tool_results"] = check_tool_results(backends, state, cut_question)
    # "Aborted" means the failures actually stopped the run before every check had its two/one calls.
    unfinished = any(name not in results for name in ("session", "tool_results")) or int(results.get("tool_results", {}).get("invocations", INVOCATIONS["tool_results"])) < INVOCATIONS["tool_results"]
    results["_aborted"] = {"verdict": "aborted_after_errors" if state.aborted and unfinished else "no"}


def _final(results: Mapping[str, Mapping[str, object]]) -> tuple[str, str]:
    """(allowed-tools verdict, overall verdict) from the three checks."""

    allowed = str(results.get("allowed_tools", {}).get("verdict", "not_run"))
    tools = results.get("tool_results", {})
    if allowed not in REJECTED_VERDICTS:
        if tools.get("clientParserRejected"):
            allowed = "client_parser_rejected"
        elif tools.get("harnessToolRejected"):
            allowed = "tool_use_rejected"
        elif allowed == "request_accepted_tool_use_not_exercised" and tools.get("newToolCompleted"):
            allowed = "accepted"
    session = str(results.get("session", {}).get("verdict", "not_run"))
    cut = str(tools.get("verdict", "not_run"))
    if results.get("_aborted", {}).get("verdict") == "aborted_after_errors":
        return allowed, "aborted_after_errors"
    session_ok = session == "history_not_retained" or (session == "history_retained" and WORKER_USES_A_FRESH_SESSION_PER_INVOCATION)
    if allowed == "accepted" and session_ok and cut == "retained":
        return allowed, "all_passed"
    findings = allowed in REJECTED_VERDICTS or (session == "history_retained" and not WORKER_USES_A_FRESH_SESSION_PER_INVOCATION) or cut == "lost"
    return allowed, "findings" if findings else "inconclusive"


def build_report(results: Mapping[str, Mapping[str, object]], budget: CallBudget, *, started: str, approval_ref: str, release_commit: str,
                 artifact_sha256: str, harness_arn: str, matter_id: str, internal_error: str | None,
                 residual: ResidualState | None = None) -> dict[str, object]:
    allowed, overall = _final(results)
    if internal_error and overall == "all_passed":
        overall = "inconclusive"
    checks = {name: dict(value) for name, value in results.items() if not name.startswith("_")}
    for name in ("allowed_tools", "session", "tool_results"):
        checks.setdefault(name, {"verdict": "not_run", "invocations": 0})
    checks["allowed_tools"]["verdict"] = allowed
    return {
        "schemaVersion": SCHEMA_VERSION, "checksVersion": CHECKS_VERSION, "startedAt": started,
        "approvalRef": approval_ref, "releaseCommit": release_commit, "artifactSha256": artifact_sha256,
        "harnessArnSha256": _sha(harness_arn), "matterIdSha256": _sha(matter_id),
        "budget": {"harnessInvocationsLimit": budget.limit, "harnessInvocationsUsed": budget.used, "retries": 0},
        "sessionPolicy": "fresh runtimeSessionId on every agent call" if WORKER_USES_A_FRESH_SESSION_PER_INVOCATION else "reuses the binding session id",
        "checks": checks, "residualState": (residual or ResidualState()).as_report(), "internalErrorCode": internal_error, "verdict": overall,
        "interpretation": {name: INTERPRETATION[name].get(str(checks[name]["verdict"]), "") for name in INTERPRETATION},
    }


def assert_report_path(path: Path) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(RESULTS_ROOT.resolve())
    except ValueError as exc:
        raise ValueError("the report must stay under evals/results") from exc
    if not resolved.name.startswith(REPORT_PREFIX) or resolved.suffix != ".json":
        raise ValueError(f"the report name must start with {REPORT_PREFIX!r} and end in .json")
    if path.is_symlink() or resolved.exists():
        raise FileExistsError("reports are immutable and cannot be overwritten")
    return resolved


def write_create_only(path: Path, payload: Mapping[str, object]) -> None:
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(encoded)


def execute_checks(
    backends: Backends, *, output: Path, approval_ref: str, release_commit: str, artifact_sha256: str, harness_arn: str,
    matter_id: str, cut_question: str = DEFAULT_CUT_QUESTION, budget: CallBudget | None = None,
) -> dict[str, object]:
    """Runs the checks and ALWAYS writes the report once a paid call has been attempted."""

    path = assert_report_path(output)  # fail before spending anything
    budget = budget or CallBudget()
    results: dict[str, dict[str, object]] = {}
    started, internal_error = datetime.now(timezone.utc).isoformat(timespec="seconds"), None
    report: dict[str, object] | None = None
    try:
        run_checks(backends, budget, results, cut_question=cut_question)
    except BaseException as exc:  # noqa: BLE001 - whatever happens, the spent calls must leave evidence
        internal_error = _error_code(exc)
        raise
    finally:
        if budget.used:
            report = build_report(results, budget, started=started, approval_ref=approval_ref, release_commit=release_commit,
                                  artifact_sha256=artifact_sha256, harness_arn=harness_arn, matter_id=matter_id, internal_error=internal_error,
                                  residual=backends.residual)
            try:
                write_create_only(path, report)
            except Exception:
                print(json.dumps(report, sort_keys=True), file=sys.stderr)  # no content in it: safe to print
                raise
    if report is None:  # unreachable: run_checks spends before its first call
        raise RuntimeError("no report was produced")
    return report


# ------------------------------------------------------------------------- real backends


@contextmanager
def _max_iterations_one(agent_client: Any) -> Iterator[None]:
    previous = agent_client.AGENT_CHAT_MAX_ITERATIONS
    agent_client.AGENT_CHAT_MAX_ITERATIONS = 1  # this process only; production keeps 3
    try:
        yield
    finally:
        agent_client.AGENT_CHAT_MAX_ITERATIONS = previous


def build_real_backends(harness_arn: str, matter_id: str, access_token: str, region: str = REGION) -> Backends:
    """The production worker's own pieces (binding, Harness client, output tree). Imported only on the approved path."""

    for relative in ("backend/src", "agent/src"):
        sys.path.insert(0, str(ROOT / relative))
    import boto3  # noqa: PLC0415
    from botocore.config import Config  # noqa: PLC0415
    import legaldesk_agent.client as agent_client  # noqa: PLC0415
    from legaldesk.agent_chat_lambda import build_worker_from_environment  # noqa: PLC0415
    from legaldesk.agent_jobs import new_job  # noqa: PLC0415
    from legaldesk.authorization import build_request_context  # noqa: PLC0415
    from legaldesk.memory import conversation_binding_partition_key  # noqa: PLC0415
    from legaldesk_agent import HarnessInvoker, new_session_id  # noqa: PLC0415

    if os.environ.get("LEGALDESK_HARNESS_ARN") != harness_arn:
        raise SystemExit("--harness-arn must be the Harness of the worker environment (LEGALDESK_HARNESS_ARN)")
    worker, _queue_arn, _visibility = build_worker_from_environment()
    invoker = HarnessInvoker(
        boto3.client("bedrock-agentcore", region_name=region, config=Config(read_timeout=120, connect_timeout=5, retries={"total_max_attempts": 1, "mode": "standard"})),
        harness_arn,
    )

    residual = ResidualState()

    def agent(question: str) -> Any:
        identity = worker.identity_verifier.verify_authorization_header(f"Bearer {access_token}")
        context = build_request_context(identity, matter_id, worker.authorization_store)
        conversation_id, selector = f"checks-{uuid4()}", f"checks-{uuid4()}"
        worker.conversation_store.bind(context, conversation_id=conversation_id, session_selector=selector)
        entry = residual.conversation_bound(conversation_id, selector, conversation_binding_partition_key(context, conversation_id))
        job = new_job(
            job_id=str(uuid4()), subject=identity.subject, tenant_id=context.tenant_id, matter_id=matter_id,
            conversation_id=conversation_id, session_selector=selector, correlation_id=context.correlation_id,
            question=question, session_ref="checks", now=time.time(),
        )
        binding = worker._bind(job, access_token)  # noqa: SLF001 - the same sealed binding as production
        entry["invocationId"] = binding.invocation_id  # the grant _bind wrote (expires in 5 minutes)
        with _max_iterations_one(agent_client):
            return worker.invoke_agent(binding, question, ())

    def resolve(loop: Any) -> str | None:
        output, _outcome, _invalid = worker.resolve_loop(loop)
        return output.path.value

    def plain(message: str, session_id: str) -> str:
        result = invoker.invoke(message, session_id=session_id)  # no memory scope, no tools
        return result.text if isinstance(getattr(result, "text", None), str) else ""

    return Backends(plain=plain, agent=agent, resolve=resolve, new_session_id=new_session_id, residual=residual)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--harness-arn", required=True)
    parser.add_argument("--matter-id", required=True, help="synthetic matter the test user may access")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--approval-ref", default="")
    parser.add_argument("--release-commit", default="")
    parser.add_argument("--artifact-sha256", default="")
    parser.add_argument("--cut-question", default=DEFAULT_CUT_QUESTION, help="a synthetic question that needs search_documents")
    parser.add_argument("--execute", action="store_true", help="required explicit execution acknowledgement")
    args = parser.parse_args(argv)
    if _HARNESS_ARN.fullmatch(args.harness_arn) is None:
        raise SystemExit("--harness-arn must be an exact Harness ARN")
    if not args.execute:
        print(json.dumps({"mode": "dry_run", "plan": build_plan()}, indent=2, sort_keys=True))  # zero network calls
        return 0
    token = os.environ.get("LEGALDESK_EVAL_ACCESS_TOKEN", "")
    if not (token and _APPROVAL_REF.fullmatch(args.approval_ref) and _GIT_SHA.fullmatch(args.release_commit) and _HEX64.fullmatch(args.artifact_sha256)):
        raise SystemExit("--execute needs --approval-ref, --release-commit, --artifact-sha256 and LEGALDESK_EVAL_ACCESS_TOKEN")
    assert_report_path(args.output)  # fail before building anything or spending
    backends = build_real_backends(args.harness_arn, args.matter_id, token)
    report = execute_checks(
        backends, output=args.output, approval_ref=args.approval_ref, release_commit=args.release_commit,
        artifact_sha256=args.artifact_sha256, harness_arn=args.harness_arn, matter_id=args.matter_id, cut_question=args.cut_question,
    )
    print(json.dumps({"verdict": report["verdict"], "checks": {name: value["verdict"] for name, value in report["checks"].items()}}, indent=2, sort_keys=True))  # type: ignore[union-attr]
    return 0 if report["verdict"] == "all_passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
