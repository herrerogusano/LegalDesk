"""Run the exact two-case direct-Bedrock Phase 12 follow-up.

This follow-up is bounded to one attempt per case. It retries only the two
model cases that were not accepted by the historical three-call smoke, after
the shared validator policy was corrected.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Callable

try:
    from .direct_bedrock_smoke import (
        CASES,
        MAX_OUTPUT_TOKENS,
        MODEL_ID,
        REGION,
        TEMPERATURE,
        BedrockConverseClient,
        DirectCase,
        FileSystemSystemPromptProvider,
        _model_payload,
        _parse_and_validate,
        _response_text,
    )
except ImportError:  # Direct script execution.
    from direct_bedrock_smoke import (  # type: ignore[no-redef]
        CASES,
        MAX_OUTPUT_TOKENS,
        MODEL_ID,
        REGION,
        TEMPERATURE,
        BedrockConverseClient,
        DirectCase,
        FileSystemSystemPromptProvider,
        _model_payload,
        _parse_and_validate,
        _response_text,
    )


FOLLOW_UP_CASE_IDS = ("direct-answerable-citation", "direct-untrusted-injection")
FOLLOW_UP_CASES: tuple[DirectCase, ...] = tuple(
    case for case in CASES if case.case_id in FOLLOW_UP_CASE_IDS
)
MAX_MODEL_INVOCATIONS = 2
TOTAL_CASES = 2
# Frozen artifact contract for this historical runner. The current filesystem
# prompt is 1.3.0 and must be rejected; future remediation uses a new runner
# and report path instead of mutating this historical path.
CURRENT_PROMPT_VERSION = "1.2.0"
CURRENT_PROMPT_SHA256 = "d87c5f6469de95979800097858b27f0eb66d96e8618f8808cffc4c2430bdb2e2"
ROOT = Path(__file__).resolve().parents[1]
HISTORICAL_REPORT_PATH = (ROOT / "evals" / "results" / "phase12-direct-bedrock-followup-report.json").resolve()
DEFAULT_OUTPUT = ROOT / "evals" / "results" / "phase12-direct-bedrock-followup-rerun-report.json"


def _assert_output_path_is_not_historical(output_path: Path | None) -> None:
    """Never allow a follow-up execution to overwrite its one-time evidence."""

    if output_path is None:
        return
    resolved_output = output_path.resolve()
    if os.path.normcase(str(resolved_output)) == os.path.normcase(str(HISTORICAL_REPORT_PATH)):
        raise RuntimeError(
            "historical Phase 12 follow-up report is immutable; use the final runner's distinct output path"
        )


def run_follow_up(
    *,
    client_factory: Callable[[str], BedrockConverseClient] | None = None,
    region: str = REGION,
    output_path: Path | None = None,
    prompt_provider: FileSystemSystemPromptProvider | None = None,
    cases: tuple[DirectCase, ...] = FOLLOW_UP_CASES,
) -> dict[str, object]:
    """Run exactly two model calls when separately authorized by an operator."""

    if region != REGION:
        raise ValueError("direct Bedrock follow-up is restricted to eu-west-1")
    if len(cases) != TOTAL_CASES or tuple(case.case_id for case in cases) != FOLLOW_UP_CASE_IDS:
        raise RuntimeError("follow-up must contain the two approved case IDs in order")
    # Validate the cap before loading the prompt or creating a client.
    if sum(bool(case.evidence) for case in cases) != MAX_MODEL_INVOCATIONS:
        raise RuntimeError("follow-up must contain exactly two model cases")
    _assert_output_path_is_not_historical(output_path)
    prompt = (prompt_provider or FileSystemSystemPromptProvider()).load()
    if prompt.version != CURRENT_PROMPT_VERSION:
        raise RuntimeError("follow-up requires the current prompt version")
    if prompt.sha256 != CURRENT_PROMPT_SHA256:
        raise RuntimeError("follow-up requires the exact approved prompt artifact")
    factory = client_factory or (lambda selected_region: __import__("boto3").client("bedrock-runtime", region_name=selected_region))
    client = factory(region)
    results: list[dict[str, object]] = []
    model_invocations = 0
    for case in cases:
        if model_invocations >= MAX_MODEL_INVOCATIONS:
            raise RuntimeError("follow-up model invocation cap exceeded")
        started = time.perf_counter()
        model_invocations += 1
        try:
            response = client.converse(**_model_payload(prompt, case))
            parsed = _parse_and_validate(_response_text(response), case, prompt)
        except Exception:
            parsed = {
                "validation": "error",
                "errorCode": "provider_error",
                "evidenceStatus": None,
                "citationIds": [],
                "canaryLeakDetected": False,
                "promptLiteralDisclosureDetected": False,
                "groundingCheck": False,
            }
        expected_match = (
            parsed["evidenceStatus"] == case.expected_status
            and parsed["citationIds"] == list(case.expected_citations)
        )
        accepted = expected_match and parsed["validation"] == "valid" and bool(parsed["groundingCheck"])
        results.append(
            {
                "caseId": case.case_id,
                "evidenceStatus": parsed["evidenceStatus"],
                "citationIds": parsed["citationIds"],
                "validation": parsed["validation"],
                "errorCode": parsed["errorCode"],
                "passed": expected_match,
                "accepted": accepted,
                "groundingCheck": parsed["groundingCheck"],
                "canaryLeakDetected": parsed["canaryLeakDetected"],
                "promptLiteralDisclosureDetected": parsed["promptLiteralDisclosureDetected"],
                "latencyMs": round((time.perf_counter() - started) * 1000, 3),
            }
        )
    report: dict[str, object] = {
        "runner": "legaldesk-phase12-direct-bedrock-followup",
        "runnerVersion": "1.0.0",
        "mode": "direct-bedrock-followup-bounded",
        "modelId": MODEL_ID,
        "region": region,
        "promptId": prompt.prompt_id,
        "promptVersion": prompt.version,
        "promptSha256": prompt.sha256,
        "maxOutputTokens": MAX_OUTPUT_TOKENS,
        "temperature": TEMPERATURE,
        "maxModelInvocations": MAX_MODEL_INVOCATIONS,
        "invocationsAttempted": model_invocations,
        "retryCount": 0,
        "passedCases": sum(bool(item["passed"]) for item in results),
        "acceptedModelCases": sum(bool(item["accepted"]) for item in results),
        "acceptedCases": sum(bool(item["accepted"]) for item in results),
        "totalCases": len(results),
        "cases": results,
    }
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", default=REGION)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    report = run_follow_up(region=args.region, output_path=args.output)
    print(json.dumps({"mode": report["mode"], "acceptedCases": report["acceptedCases"], "totalCases": report["totalCases"]}, sort_keys=True))
    return 0 if report["acceptedCases"] == report["totalCases"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
