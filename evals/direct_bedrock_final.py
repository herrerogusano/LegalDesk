"""Prepare the historical bounded two-case Bedrock check for prompt version 1.2.0.

This runner is intentionally not executed by the local suite. It preserves the
historical follow-up report while providing a distinct report path for a future
explicitly authorized check with the corrected prompt.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Callable

try:
    from .direct_bedrock_followup import (
        FOLLOW_UP_CASES,
        FOLLOW_UP_CASE_IDS,
        CURRENT_PROMPT_SHA256,
        HISTORICAL_REPORT_PATH,
        MAX_MODEL_INVOCATIONS,
        REGION,
        BedrockConverseClient,
        FileSystemSystemPromptProvider,
        _assert_output_path_is_not_historical,
        run_follow_up,
    )
except ImportError:  # Direct script execution.
    from direct_bedrock_followup import (  # type: ignore[no-redef]
        FOLLOW_UP_CASES,
        FOLLOW_UP_CASE_IDS,
        CURRENT_PROMPT_SHA256,
        HISTORICAL_REPORT_PATH,
        MAX_MODEL_INVOCATIONS,
        REGION,
        BedrockConverseClient,
        FileSystemSystemPromptProvider,
        _assert_output_path_is_not_historical,
        run_follow_up,
    )


PROMPT_VERSION = "1.2.0"
RUNNER_VERSION = "1.0.0"
TOTAL_CASES = 2
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "evals" / "results" / "phase12-direct-bedrock-final-report.json"


class CurrentPromptProvider:
    """Allow the final runner to use only the currently approved prompt."""

    def __init__(self, provider: FileSystemSystemPromptProvider) -> None:
        self._provider = provider

    def load(self):
        artifact = self._provider.load()
        if artifact.version != PROMPT_VERSION:
            raise RuntimeError("historical final runner requires prompt version 1.2.0")
        if artifact.sha256 != CURRENT_PROMPT_SHA256:
            raise RuntimeError("final Bedrock runner requires the exact approved prompt artifact")
        return artifact


def run_final(
    *,
    client_factory: Callable[[str], BedrockConverseClient] | None = None,
    region: str = REGION,
    output_path: Path | None = None,
    prompt_provider: FileSystemSystemPromptProvider | None = None,
    cases=FOLLOW_UP_CASES,
) -> dict[str, object]:
    """Prepare exactly two calls, with cap validation before client creation."""

    if len(cases) != TOTAL_CASES or tuple(case.case_id for case in cases) != FOLLOW_UP_CASE_IDS:
        raise RuntimeError("final runner must contain the two approved case IDs in order")
    _assert_output_path_is_not_historical(output_path)
    report = run_follow_up(
        client_factory=client_factory,
        region=region,
        output_path=None,
        prompt_provider=CurrentPromptProvider(prompt_provider or FileSystemSystemPromptProvider()),
        cases=cases,
    )
    final_report = dict(report)
    final_report.update(
        {
            "runner": "legaldesk-phase12-direct-bedrock-final",
            "runnerVersion": RUNNER_VERSION,
            "mode": "direct-bedrock-final-bounded",
        }
    )
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(final_report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return final_report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", default=REGION)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    report = run_final(region=args.region, output_path=args.output)
    print(json.dumps({"mode": report["mode"], "acceptedCases": report["acceptedCases"], "totalCases": report["totalCases"]}, sort_keys=True))
    return 0 if report["acceptedCases"] == report["totalCases"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
