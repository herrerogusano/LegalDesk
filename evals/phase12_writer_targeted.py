"""One-call Phase 12 follow-up for the sole writer-v1 failure."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Mapping, Protocol

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

from legaldesk.evidence import (  # noqa: E402
    ANSWER_WRITER_PROMPT_SHA256,
    ANSWER_WRITER_PROMPT_VERSION,
    AnswerWriterRequest,
    ConverseAnswerWriter,
    EvidenceContractError,
    GroundingContractError,
    validate_evidence_resolution,
    validate_grounding_result,
)

try:
    from .phase12_remediation_runner import MODEL_ID, REGION, RESULTS_ROOT, _deterministic_grounding_detailed  # noqa: E402
    from .phase12_writer_followup import _source_report  # noqa: E402
    from .synthetic_debug import SYNTHETIC_CASES  # noqa: E402
except ImportError:  # Direct script execution.
    from phase12_remediation_runner import MODEL_ID, REGION, RESULTS_ROOT, _deterministic_grounding_detailed  # type: ignore[no-redef]  # noqa: E402
    from phase12_writer_followup import _source_report  # type: ignore[no-redef]  # noqa: E402
    from synthetic_debug import SYNTHETIC_CASES  # type: ignore[no-redef]  # noqa: E402


RUNNER_VERSION = "1.1.0"
TARGET_CASE_ID = "synthetic-partial-03"
MAX_MODEL_INVOCATIONS = 1
EXPECTED_WRITER_PROMPT_VERSION = "1.4.0"
EXPECTED_WRITER_PROMPT_SHA256 = "b4b54b39e7d016af94534ff8078b43d2aac556f608c33a8c7bee8697430294e7"
SOURCE_WRITER_REPORT = RESULTS_ROOT / "phase12-remediation-writer-v1-report.json"
SOURCE_WRITER_REPORT_SHA256 = "3bab7aaa56921c4fe7b49ebbc9e19041535de3041b236621c3d0379bb718f3d9"
DEFAULT_OUTPUT = RESULTS_ROOT / "phase12-remediation-writer-v2-report.json"


class RealConverseClient(Protocol):
    def converse(self, **kwargs: object) -> Mapping[str, object]: ...


class _OneCallClient:
    def __init__(self, client: RealConverseClient) -> None:
        self._client = client
        self.calls = 0

    def converse(self, **kwargs: object) -> Mapping[str, object]:
        if self.calls >= MAX_MODEL_INVOCATIONS:
            raise RuntimeError("targeted writer invocation cap exceeded")
        self.calls += 1
        return self._client.converse(**kwargs)


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _source_writer_report() -> Mapping[str, object]:
    report = json.loads(SOURCE_WRITER_REPORT.read_text(encoding="utf-8"))
    if not isinstance(report, Mapping) or _canonical_hash(report) != SOURCE_WRITER_REPORT_SHA256:
        raise RuntimeError("source writer report differs from the approved artifact")
    cases = report.get("cases")
    if (
        report.get("runnerVersion") != "1.0.0"
        or report.get("acceptedCases") != 8
        or report.get("totalCases") != 9
        or report.get("writerCalls") != 9
        or not isinstance(cases, list)
    ):
        raise RuntimeError("source writer report does not prove the approved 8/9 result")
    failed = [item for item in cases if isinstance(item, Mapping) and item.get("accepted") is False]
    if (
        len(failed) != 1
        or failed[0].get("caseId") != TARGET_CASE_ID
        or failed[0].get("groundingDiagnosticCode") != "UNSUPPORTED_LEXICAL_CLAIM"
    ):
        raise RuntimeError("source writer report does not isolate the approved target")
    return report


def assert_output_path(path: Path) -> Path:
    if not isinstance(path, Path):
        raise TypeError("output path must be a Path")
    resolved = path.resolve()
    try:
        resolved.relative_to(RESULTS_ROOT.resolve())
    except ValueError as exc:
        raise ValueError("targeted writer report must remain under evals/results") from exc
    if resolved == SOURCE_WRITER_REPORT.resolve():
        raise ValueError("historical writer report is immutable")
    return resolved


def _target_case() -> Mapping[str, object]:
    matches = [case for case in SYNTHETIC_CASES if case.get("caseId") == TARGET_CASE_ID]
    if len(matches) != 1:
        raise RuntimeError("target fixture is missing or duplicated")
    return matches[0]


def preflight_targeted(output_path: Path | None = None) -> dict[str, object]:
    if (
        ANSWER_WRITER_PROMPT_VERSION != EXPECTED_WRITER_PROMPT_VERSION
        or ANSWER_WRITER_PROMPT_SHA256 != EXPECTED_WRITER_PROMPT_SHA256
    ):
        raise RuntimeError("targeted follow-up requires the exact approved writer prompt")
    _source_report()
    _source_writer_report()
    _target_case()
    if output_path is not None:
        assert_output_path(output_path)
    return {
        "runner": "legaldesk-phase12-writer-targeted",
        "runnerVersion": RUNNER_VERSION,
        "mode": "preflight",
        "region": REGION,
        "model": MODEL_ID,
        "targetCaseId": TARGET_CASE_ID,
        "writerPromptVersion": ANSWER_WRITER_PROMPT_VERSION,
        "writerPromptHash": ANSWER_WRITER_PROMPT_SHA256,
        "sourceWriterReportHash": SOURCE_WRITER_REPORT_SHA256,
        "maxModelInvocations": MAX_MODEL_INVOCATIONS,
        "retryCount": 0,
        "metadataOnly": True,
    }


def run_targeted(
    *,
    client: RealConverseClient | None = None,
    output_path: Path | None = None,
    execute: bool = False,
    preflight: bool = False,
) -> dict[str, object]:
    if not execute:
        raise RuntimeError("real execution requires --execute")
    if not preflight:
        raise RuntimeError("real execution requires explicit --preflight")
    destination = assert_output_path(output_path or DEFAULT_OUTPUT)
    if destination.exists():
        raise RuntimeError("targeted writer report already exists; never overwrite evaluation evidence")
    preflight_targeted(destination)
    if client is None:
        try:
            import boto3  # type: ignore[import-not-found]
            from botocore.config import Config  # type: ignore[import-not-found]

            client = boto3.client(
                "bedrock-runtime",
                region_name=REGION,
                config=Config(retries={"total_max_attempts": 1, "mode": "standard"}),
            )
        except Exception as exc:
            raise RuntimeError("unable to create the fixed Bedrock client") from exc

    case = _target_case()
    passages = tuple(case["passages"])  # type: ignore[arg-type]
    resolution = validate_evidence_resolution(
        case["resolver"],  # type: ignore[arg-type]
        [str(passage["citationId"]) for passage in passages],  # type: ignore[index]
    )
    metadata: dict[str, object] = {
        "caseId": TARGET_CASE_ID,
        "category": str(case["category"]),
        "writerCalled": False,
        "citationIds": list(resolution.supporting_citation_ids),
        "evidenceStatus": resolution.evidence_status.value,
        "validationCodes": ["SOURCE_RESOLUTION_VALID"],
        "errorCodes": [],
        "accepted": False,
    }
    bounded = _OneCallClient(client)
    writer = ConverseAnswerWriter(bounded, model_id=MODEL_ID)
    try:
        metadata["writerCalled"] = True
        written = writer.write(AnswerWriterRequest(str(case["question"]), passages, resolution))
        answer = written.get("answer") if isinstance(written, Mapping) else None
        if not isinstance(answer, str) or not answer.strip():
            raise EvidenceContractError("writer answer was not text")
        grounding_raw, reason = _deterministic_grounding_detailed(
            answer,
            case,
            resolution.supporting_citation_ids,
        )
        metadata["groundingDiagnosticCode"] = reason
        grounding = validate_grounding_result(
            grounding_raw,
            supporting_citation_ids=resolution.supporting_citation_ids,
        )
        metadata["validationCodes"] = [
            "SOURCE_RESOLUTION_VALID",
            "WRITER_VALID",
            "GROUNDING_VALID",
        ]
        metadata["groundingScore"] = grounding.score
        metadata["accepted"] = True
    except GroundingContractError:
        metadata["errorCodes"] = ["GROUNDING_INVALID"]
    except EvidenceContractError:
        metadata["errorCodes"] = ["WRITER_CONTRACT_INVALID"]
    except Exception:
        metadata["errorCodes"] = ["WRITER_PROVIDER_FAILURE"]

    report: dict[str, object] = {
        "runner": "legaldesk-phase12-writer-targeted",
        "runnerVersion": RUNNER_VERSION,
        "mode": "real-bedrock-bounded",
        "region": REGION,
        "model": MODEL_ID,
        "writerPromptVersion": ANSWER_WRITER_PROMPT_VERSION,
        "writerPromptHash": ANSWER_WRITER_PROMPT_SHA256,
        "sourceWriterReportHash": SOURCE_WRITER_REPORT_SHA256,
        "totalCases": 1,
        "acceptedCases": int(bool(metadata["accepted"])),
        "writerCalls": bounded.calls,
        "inferenceCalls": bounded.calls,
        "retryCount": 0,
        "maxModelInvocations": MAX_MODEL_INVOCATIONS,
        "metadataOnly": True,
        "historicalReportsImmutable": True,
        "cases": [metadata],
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    if not args.execute:
        print(json.dumps(preflight_targeted(args.output), indent=2, sort_keys=True))
        return 0
    if not args.preflight:
        parser.error("--execute requires explicit --preflight")
    report = run_targeted(output_path=args.output, execute=True, preflight=True)
    print(json.dumps({key: report[key] for key in ("acceptedCases", "totalCases", "inferenceCalls")}, sort_keys=True))
    return 0 if report["acceptedCases"] == 1 else 1


if __name__ == "__main__":
    raise SystemExit(main())
