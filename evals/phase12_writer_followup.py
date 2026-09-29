"""Bounded writer-only follow-up for the validated Phase 12 resolver result.

The immutable resolver-v3 report proves that the historical resolver prompt
1.1.0 matched all nine fixture resolutions. This runner reuses those
server-validated expected resolutions while the current runtime calls writer
prompt 1.3.0. Execution requires both
``--execute`` and ``--preflight``; reports contain metadata only.
"""

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
    EVIDENCE_RESOLVER_PROMPT_SHA256,
    EVIDENCE_RESOLVER_PROMPT_VERSION,
    GroundingContractError,
    validate_evidence_resolution,
    validate_grounding_result,
)

try:
    from .phase12_remediation_runner import (  # noqa: E402
        MODEL_ID,
        REGION,
        RESULTS_ROOT,
        _deterministic_grounding_detailed,
    )
    from .synthetic_debug import SYNTHETIC_CASES  # noqa: E402
except ImportError:  # Direct script execution.
    from phase12_remediation_runner import (  # type: ignore[no-redef]  # noqa: E402
        MODEL_ID,
        REGION,
        RESULTS_ROOT,
        _deterministic_grounding_detailed,
    )
    from synthetic_debug import SYNTHETIC_CASES  # type: ignore[no-redef]  # noqa: E402


RUNNER_VERSION = "1.1.0"
MAX_WRITER_INVOCATIONS = 9
EXPECTED_RESOLVER_PROMPT_VERSION = "1.2.0"
EXPECTED_RESOLVER_PROMPT_SHA256 = "da65f6b0efa70e728d9c6c5b85c036a7fb3b71b1b24c1cde33e9caedabe8127c"
EXPECTED_WRITER_PROMPT_VERSION = "1.4.0"
EXPECTED_WRITER_PROMPT_SHA256 = "04d46e6d66bd9b75d3be7ddedd0dd1734dd712a2285a7a5017ed11e6bff2c90f"
HISTORICAL_RESOLVER_PROMPT_VERSION = "1.1.0"
HISTORICAL_RESOLVER_PROMPT_SHA256 = "ae9fba28e300f69656e4bdd53ea288fb1139c5f448d7857519f28fadb1dee672"
SOURCE_RESOLVER_REPORT = RESULTS_ROOT / "phase12-remediation-resolver-v3-report.json"
SOURCE_RESOLVER_REPORT_SHA256 = "9e2ac4fb1bf87336a25fd261d800b7de7688722fc2f5698327071ebc8c2709f3"
DEFAULT_OUTPUT = RESULTS_ROOT / "phase12-remediation-writer-v1-report.json"
HISTORICAL_REPORT_NAMES = {
    "phase12-remediation-synthetic-report.json",
    "phase12-direct-bedrock-report.json",
    "phase12-direct-bedrock-followup-report.json",
    "phase12-direct-bedrock-final-report.json",
    "phase12-remediation-real-report.json",
    "phase12-remediation-resolver-v2-report.json",
    "phase12-remediation-resolver-v3-report.json",
    "phase12-remediation-writer-v2-report.json",
}


class RealConverseClient(Protocol):
    def converse(self, **kwargs: object) -> Mapping[str, object]: ...


class _BoundedWriterClient:
    def __init__(self, client: RealConverseClient) -> None:
        self._client = client
        self.calls = 0

    def converse(self, **kwargs: object) -> Mapping[str, object]:
        if self.calls >= MAX_WRITER_INVOCATIONS:
            raise RuntimeError("writer invocation cap exceeded")
        self.calls += 1
        return self._client.converse(**kwargs)


def assert_output_path(path: Path) -> Path:
    if not isinstance(path, Path):
        raise TypeError("output path must be a Path")
    resolved = path.resolve()
    try:
        resolved.relative_to(RESULTS_ROOT.resolve())
    except ValueError as exc:
        raise ValueError("writer follow-up reports must remain under evals/results") from exc
    if resolved.name in HISTORICAL_REPORT_NAMES:
        raise ValueError("historical evaluation reports are immutable")
    return resolved


def _source_report() -> Mapping[str, object]:
    payload = SOURCE_RESOLVER_REPORT.read_bytes()
    report = json.loads(payload)
    if not isinstance(report, Mapping):
        raise RuntimeError("source resolver report is invalid")
    canonical = json.dumps(
        report,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if hashlib.sha256(canonical).hexdigest() != SOURCE_RESOLVER_REPORT_SHA256:
        raise RuntimeError("source resolver report hash differs from the approved artifact")
    cases = report.get("cases")
    if (
        report.get("runnerVersion") != "6.0.0"
        or report.get("resolverPromptVersion") != HISTORICAL_RESOLVER_PROMPT_VERSION
        or report.get("resolverPromptHash") != HISTORICAL_RESOLVER_PROMPT_SHA256
        or report.get("resolverCalls") != 9
        or not isinstance(cases, list)
        or len(cases) != 9
    ):
        raise RuntimeError("source resolver report does not prove the approved nine resolutions")
    fixture_by_id = {str(case["caseId"]): case for case in SYNTHETIC_CASES}
    seen: set[str] = set()
    for item in cases:
        if not isinstance(item, Mapping) or not isinstance(item.get("caseId"), str):
            raise RuntimeError("source resolver case metadata is invalid")
        case_id = str(item["caseId"])
        fixture = fixture_by_id.get(case_id)
        if fixture is None or case_id in seen:
            raise RuntimeError("source resolver cases differ from approved fixtures")
        seen.add(case_id)
        expected = validate_evidence_resolution(
            fixture["resolver"],  # type: ignore[arg-type]
            [str(passage["citationId"]) for passage in fixture["passages"]],  # type: ignore[index]
        )
        if (
            item.get("evidenceStatus") != expected.evidence_status.value
            or item.get("citationIds") != list(expected.supporting_citation_ids)
            or item.get("resolverCalled") is not True
            or "RESOLUTION_VALID" not in item.get("validationCodes", [])
            or any(str(code).startswith("RESOLUTION_") and code != "RESOLUTION_VALID" for code in item.get("errorCodes", []))
        ):
            raise RuntimeError("source resolver case did not match its approved resolution")
    if seen != set(fixture_by_id):
        raise RuntimeError("source resolver report is missing approved fixtures")
    return report


def preflight_writer_followup(output_path: Path | None = None) -> dict[str, object]:
    if (
        EVIDENCE_RESOLVER_PROMPT_VERSION != EXPECTED_RESOLVER_PROMPT_VERSION
        or EVIDENCE_RESOLVER_PROMPT_SHA256 != EXPECTED_RESOLVER_PROMPT_SHA256
        or ANSWER_WRITER_PROMPT_VERSION != EXPECTED_WRITER_PROMPT_VERSION
        or ANSWER_WRITER_PROMPT_SHA256 != EXPECTED_WRITER_PROMPT_SHA256
    ):
        raise RuntimeError("writer follow-up requires the exact approved stage prompts")
    _source_report()
    if output_path is not None:
        assert_output_path(output_path)
    return {
        "runner": "legaldesk-phase12-writer-followup",
        "runnerVersion": RUNNER_VERSION,
        "mode": "preflight",
        "region": REGION,
        "model": MODEL_ID,
        "resolverPromptVersion": EVIDENCE_RESOLVER_PROMPT_VERSION,
        "resolverPromptHash": EVIDENCE_RESOLVER_PROMPT_SHA256,
        "writerPromptVersion": ANSWER_WRITER_PROMPT_VERSION,
        "writerPromptHash": ANSWER_WRITER_PROMPT_SHA256,
        "sourceResolverReportHash": SOURCE_RESOLVER_REPORT_SHA256,
        "totalCases": len(SYNTHETIC_CASES),
        "maxModelInvocations": MAX_WRITER_INVOCATIONS,
        "retryCount": 0,
        "metadataOnly": True,
    }


def run_writer_followup(
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
        raise RuntimeError("writer follow-up report already exists; never overwrite evaluation evidence")
    preflight_writer_followup(destination)
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

    bounded = _BoundedWriterClient(client)
    writer = ConverseAnswerWriter(bounded, model_id=MODEL_ID)
    results: list[dict[str, object]] = []
    for case in SYNTHETIC_CASES:
        metadata: dict[str, object] = {
            "caseId": str(case["caseId"]),
            "category": str(case["category"]),
            "writerCalled": False,
            "citationIds": [],
            "validationCodes": ["SOURCE_RESOLUTION_VALID"],
            "errorCodes": [],
            "accepted": False,
        }
        passages = tuple(case["passages"])  # type: ignore[arg-type]
        resolution = validate_evidence_resolution(
            case["resolver"],  # type: ignore[arg-type]
            [str(passage["citationId"]) for passage in passages],  # type: ignore[index]
        )
        metadata["evidenceStatus"] = resolution.evidence_status.value
        metadata["citationIds"] = list(resolution.supporting_citation_ids)
        try:
            metadata["writerCalled"] = True
            written = writer.write(AnswerWriterRequest(str(case["question"]), passages, resolution))
            answer = written.get("answer") if isinstance(written, Mapping) else None
            if not isinstance(answer, str) or not answer.strip():
                raise ValueError("writer answer was not text")
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
        except Exception as exc:
            metadata["errorCodes"] = [
                "WRITER_CONTRACT_INVALID"
                if type(exc).__name__ == "EvidenceContractError"
                else "WRITER_PROVIDER_FAILURE"
            ]
        results.append(metadata)

    report: dict[str, object] = {
        "runner": "legaldesk-phase12-writer-followup",
        "runnerVersion": RUNNER_VERSION,
        "mode": "real-bedrock-bounded",
        "region": REGION,
        "model": MODEL_ID,
        "resolverPromptVersion": EVIDENCE_RESOLVER_PROMPT_VERSION,
        "resolverPromptHash": EVIDENCE_RESOLVER_PROMPT_SHA256,
        "writerPromptVersion": ANSWER_WRITER_PROMPT_VERSION,
        "writerPromptHash": ANSWER_WRITER_PROMPT_SHA256,
        "sourceResolverReportHash": SOURCE_RESOLVER_REPORT_SHA256,
        "totalCases": len(results),
        "acceptedCases": sum(bool(item["accepted"]) for item in results),
        "writerCalls": bounded.calls,
        "inferenceCalls": bounded.calls,
        "retryCount": 0,
        "maxModelInvocations": MAX_WRITER_INVOCATIONS,
        "metadataOnly": True,
        "historicalReportsImmutable": True,
        "cases": results,
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
        print(json.dumps(preflight_writer_followup(args.output), indent=2, sort_keys=True))
        return 0
    if not args.preflight:
        parser.error("--execute requires explicit --preflight")
    report = run_writer_followup(output_path=args.output, execute=True, preflight=True)
    print(json.dumps({key: report[key] for key in ("acceptedCases", "totalCases", "inferenceCalls")}, sort_keys=True))
    return 0 if report["acceptedCases"] == report["totalCases"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
