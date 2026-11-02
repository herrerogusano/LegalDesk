"""Unique-marker probe of managed ADOT content logging (PLAN_15 T9.3, ADR-021 D9).

Repeats the Phase 11 test (docs/phase-11-acceptance.md) once ADOT has been re-enabled with
``AWS_GENAI_CONTENT_EXTRACTION_OPT_OUT=true``: send one synthetic question that carries a
unique marker, then look for the marker in the Harness runtime log groups. If it appears,
the opt-out did not keep content out of the provider telemetry.

Safety properties:

* the default command is a dry run: it prints the plan and makes zero network calls, and
  imports neither boto3 nor the agent package;
* execution needs ``--execute``, an approval reference and the operator's attestation that
  ADOT is enabled; it makes exactly ONE Harness invocation and no retries;
* log reads are bounded (``--polls`` x one ``filter_log_events`` page per group and marker);
* once the paid call is made, the report is ALWAYS written (a log-read failure ends in the
  ``log_read_failed`` verdict with the counts seen so far, never in a lost run);
* the report is create-only and holds counts, closed codes and the SHA-256 of the marker.
  It never stores the marker, the question, the answer or any log event.

This script is NOT run by CI or by T7/T8. T9 runs it with cost approval; see
``docs/phase-15-adot-probe.md``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
PROBE_VERSION = "1.0.0"
SCHEMA_VERSION = "phase15-adot-probe-1"
REGION = "eu-west-1"
MAX_INVOCATIONS = 1
RUNTIME_LOG_PREFIX = "/aws/bedrock-agentcore/runtimes/"
REPORT_PREFIX = "phase15-adot-probe-"
RESULTS_ROOT = ROOT / "evals" / "results"
MAX_LOG_GROUPS = 10
_RUNTIME_ID = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
_APPROVAL_REF = re.compile(r"^[A-Za-z0-9_.:/#-]{3,120}$")
_HARNESS_ARN = re.compile(r"^arn:aws:bedrock-agentcore:[a-z0-9-]+:\d{12}:harness/[A-Za-z0-9_-]+$")

VERDICT_NO_CONTENT = "no_content_observed"
VERDICT_CONTENT = "content_observed"
VERDICT_INVOKE_FAILED = "invoke_failed"
VERDICT_NO_LOG_GROUPS = "no_log_groups_found"
VERDICT_LOG_READ_FAILED = "log_read_failed"


def new_marker() -> str:
    return "LDADOTPROBE" + uuid4().hex


def question_token(marker: str) -> str:
    """Appears only in the prompt."""

    return f"{marker}-Q"


def answer_token(marker: str) -> str:
    """Appears only in the model's answer: the prompt carries its two halves apart, never together."""

    return f"{marker}-ANSWER"


def probe_message(marker: str) -> str:
    # Distinct tokens make every hit attributable: a log event containing the question token held the
    # prompt; one containing the answer token held the model output, because the prompt never spells it.
    head = answer_token(marker)[: -len("WER")]
    return (
        f"Synthetic probe {question_token(marker)}. Write the single token obtained by joining "
        f"the two parts {head} and WER with nothing between them, and nothing else."
    )


def build_plan(*, runtime_id: str, polls: int, wait_seconds: int) -> dict[str, object]:
    return {
        "probeVersion": PROBE_VERSION, "harnessInvocations": MAX_INVOCATIONS, "retries": 0,
        "logGroupPrefix": f"{RUNTIME_LOG_PREFIX}{runtime_id}", "maxLogGroups": MAX_LOG_GROUPS,
        "polls": polls, "waitSecondsBetweenPolls": wait_seconds,
        "filterLogEventsCallsUpperBound": polls * MAX_LOG_GROUPS * 2 + 1,
        "reportStoresMarker": False,
    }


def _assert_report_path(path: Path) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(RESULTS_ROOT.resolve())
    except ValueError as exc:
        raise ValueError("the report must stay under evals/results") from exc
    if not resolved.name.startswith(REPORT_PREFIX) or resolved.suffix != ".json":
        raise ValueError(f"the report name must start with {REPORT_PREFIX!r} and end in .json")
    if path.is_symlink() or resolved.exists():
        raise FileExistsError("probe reports are immutable and cannot be overwritten")
    return resolved


def _write_create_only(path: Path, payload: Mapping[str, object]) -> None:
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    descriptor = os.open(str(path), flags, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(encoded)


def _count_marker_events(logs: Any, groups: list[str], marker: str, counts: dict[str, int]) -> None:
    """Matching events per marker kind, written into ``counts`` as each kind completes.

    Only counts leave this function, never an event. If a read fails half way, ``counts`` still holds
    every kind that was completed, so the report can say what was seen before the failure.
    """

    for kind, token in (("question", question_token(marker)), ("answer", answer_token(marker))):
        total = 0
        for group in groups:
            response = logs.filter_log_events(logGroupName=group, filterPattern=f'"{token}"', limit=50)
            events = response.get("events", []) if isinstance(response, Mapping) else []
            total += len(events)
        counts[kind] = total


def _error_code(exc: BaseException) -> str:
    """A closed-shape code for the report: the AWS error code if it has a safe shape, else the class name."""

    response = getattr(exc, "response", None)
    error = response.get("Error") if isinstance(response, Mapping) else None
    code = error.get("Code") if isinstance(error, Mapping) else None
    for candidate in (code, type(exc).__name__):
        if isinstance(candidate, str) and re.fullmatch(r"[A-Za-z0-9_.]{1,64}", candidate):
            return candidate
    return "unknown"


def run_probe(
    *,
    harness_arn: str,
    runtime_id: str,
    approval_ref: str,
    output: Path,
    execute: bool,
    adot_enabled_attested: bool = False,
    polls: int = 4,
    wait_seconds: int = 30,
    region: str = REGION,
    invoke: Callable[[str], object] | None = None,
    logs_client: Any = None,
    sleep: Callable[[float], None] = time.sleep,
    marker: str | None = None,
) -> dict[str, object]:
    if _HARNESS_ARN.fullmatch(harness_arn or "") is None:
        raise ValueError("harness_arn must be an exact Harness ARN")
    if _RUNTIME_ID.fullmatch(runtime_id or "") is None:
        raise ValueError("runtime_id is invalid")
    if not 1 <= polls <= 10 or not 5 <= wait_seconds <= 120:
        raise ValueError("polls or wait_seconds is outside the bounded range")
    plan = build_plan(runtime_id=runtime_id, polls=polls, wait_seconds=wait_seconds)
    if not execute:
        return {"mode": "dry_run", "plan": plan}  # zero network calls
    if _APPROVAL_REF.fullmatch(approval_ref or "") is None:
        raise ValueError("execution needs the reference of the owner's cost approval")
    if not adot_enabled_attested:
        raise ValueError("the operator must attest that ADOT is enabled on the Harness (EnableManagedAdot=true)")
    report_path = _assert_report_path(output)  # fail before spending anything
    marker = marker or new_marker()
    if invoke is None or logs_client is None:
        import boto3  # noqa: PLC0415 - only on the approved path

        sys.path.insert(0, str(ROOT / "agent" / "src"))
        from legaldesk_agent import HarnessInvoker  # noqa: PLC0415

        invoker = HarnessInvoker.from_boto3(harness_arn, region)
        invoke = invoke or (lambda message: invoker.invoke(message))
        logs_client = logs_client or boto3.client("logs", region_name=region)

    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    verdict, counts, groups_scanned, polls_used = VERDICT_INVOKE_FAILED, {"question": 0, "answer": 0}, 0, 0
    error_code: str | None = None
    answer_complied: bool | None = None  # did the model write the answer token? computed in memory, never stored
    try:
        result = invoke(probe_message(marker))
    except Exception as exc:  # noqa: BLE001 - the paid call failed: still leave evidence
        error_code = _error_code(exc)
    else:
        text = result if isinstance(result, str) else getattr(result, "text", None)
        answer_complied = answer_token(marker) in text if isinstance(text, str) else None
        verdict = VERDICT_NO_CONTENT
        # Everything after the paid invocation is guarded: whatever fails, the single approved run
        # must end in a report with the counts seen so far.
        try:
            for polls_used in range(1, polls + 1):
                sleep(wait_seconds)  # provider telemetry is delivered with a delay
                paginator = logs_client.get_paginator("describe_log_groups")
                groups = [
                    item["logGroupName"] for page in paginator.paginate(logGroupNamePrefix=f"{RUNTIME_LOG_PREFIX}{runtime_id}")
                    for item in page.get("logGroups", [])
                ][:MAX_LOG_GROUPS]
                groups_scanned = len(groups)
                if not groups:
                    verdict = VERDICT_NO_LOG_GROUPS
                    continue
                _count_marker_events(logs_client, groups, marker, counts)
                if counts["question"] or counts["answer"]:
                    verdict = VERDICT_CONTENT
                    break
                verdict = VERDICT_NO_CONTENT
        except Exception as exc:  # noqa: BLE001 - throttling, AccessDenied, malformed responses...
            verdict, error_code = VERDICT_LOG_READ_FAILED, _error_code(exc)
    report = {
        "schemaVersion": SCHEMA_VERSION, "probeVersion": PROBE_VERSION, "startedAt": started,
        "approvalRef": approval_ref, "harnessArnSha256": hashlib.sha256(harness_arn.encode()).hexdigest(),
        "markerSha256": hashlib.sha256(marker.encode()).hexdigest(),
        "harnessInvocations": 1, "retries": 0, "operatorAttestsAdotEnabled": True,
        "logGroupsScanned": groups_scanned, "pollsUsed": polls_used, "waitSecondsBetweenPolls": wait_seconds,
        "questionMarkerEvents": counts["question"], "answerMarkerEvents": counts["answer"],
        "answerTokenWritten": answer_complied, "errorCode": error_code,
        "verdict": verdict,
        "interpretation": {
            VERDICT_NO_CONTENT: "marker not seen within the window; absence is bounded evidence, not proof",
            VERDICT_CONTENT: "content reached provider logs: apply the residual risk path of ADR-021 D9 or disable ADOT",
            VERDICT_INVOKE_FAILED: "the single invocation failed; no conclusion; a rerun needs new approval",
            VERDICT_NO_LOG_GROUPS: "no runtime log group found for the runtime id; no conclusion",
            VERDICT_LOG_READ_FAILED: (
                "the invocation was made but reading the logs failed; counts are what was seen before the failure; "
                "no conclusion unless a count is above zero; read the logs by hand under the same approval, do not reinvoke"
            ),
        }[verdict],
    }
    try:
        _write_create_only(report_path, report)
    except Exception:
        # The only paid run must not lose its evidence: put the report on stderr (it holds no content).
        print(json.dumps(report, sort_keys=True), file=sys.stderr)
        raise
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harness-arn", required=True)
    parser.add_argument("--runtime-id", required=True, help="id of the Harness runtime (the RuntimeArn output of legaldesk-phase-01)")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--approval-ref", default="")
    parser.add_argument("--adot-enabled-attested", action="store_true")
    parser.add_argument("--polls", type=int, default=4)
    parser.add_argument("--wait-seconds", type=int, default=30)
    parser.add_argument("--execute", action="store_true", help="required explicit execution acknowledgement")
    args = parser.parse_args(argv)
    report = run_probe(
        harness_arn=args.harness_arn, runtime_id=args.runtime_id, approval_ref=args.approval_ref, output=args.output,
        execute=args.execute, adot_enabled_attested=args.adot_enabled_attested, polls=args.polls,
        wait_seconds=args.wait_seconds,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report.get("verdict", VERDICT_NO_CONTENT) == VERDICT_NO_CONTENT or report.get("mode") == "dry_run" else 1


if __name__ == "__main__":
    raise SystemExit(main())
