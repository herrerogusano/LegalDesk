"""Bounded, explicitly enabled real-model runner for Phase 12 remediation.

The normal invocation is a no-cost preflight. A real run requires both
``--execute`` and ``--preflight``; the provider client is imported and created
only after those checks pass. Reports are metadata-only.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Mapping, Protocol, TypedDict

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

from legaldesk.evidence import (  # noqa: E402
    ANSWER_WRITER_PROMPT_SHA256,
    ANSWER_WRITER_PROMPT_VERSION,
    AnswerWriterRequest,
    ConverseAnswerWriter,
    ConverseEvidenceResolver,
    EvidenceResolutionRequest,
    EVIDENCE_RESOLVER_PROMPT_SHA256,
    EVIDENCE_RESOLVER_PROMPT_VERSION,
    GroundingContractError,
    validate_evidence_resolution,
    validate_grounding_result,
)
from legaldesk.prompts import FileSystemSystemPromptProvider  # noqa: E402
try:
    from .grounding_oracle import GroundingSpec, evaluate_grounding_detailed  # noqa: E402
except ImportError:  # Direct script execution.
    from grounding_oracle import GroundingSpec, evaluate_grounding_detailed  # type: ignore[no-redef]  # noqa: E402
try:
    from .synthetic_debug import SYNTHETIC_CASES  # noqa: E402
except ImportError:  # Direct script execution.
    from synthetic_debug import SYNTHETIC_CASES  # type: ignore[no-redef]  # noqa: E402

REGION = "eu-west-1"
MODEL_ID = "eu.anthropic.claude-sonnet-4-6"
MAX_SAMPLE_COUNT = 9
MAX_RESOLVER_INVOCATIONS = 9
MAX_WRITER_INVOCATIONS = 9
MAX_REAL_MODEL_INVOCATIONS = MAX_RESOLVER_INVOCATIONS + MAX_WRITER_INVOCATIONS
MAX_PER_GROUP = 3
RUNNER_VERSION = "6.2.0"
EXPECTED_PROMPT_VERSION = "1.3.0"
EXPECTED_PROMPT_SHA256 = "de28c6e7d7b3a9284cfac505e4f4d099e8854da9ce8ecebb7911c0adefe8af56"
EXPECTED_RESOLVER_PROMPT_VERSION = "1.2.0"
EXPECTED_RESOLVER_PROMPT_SHA256 = "da65f6b0efa70e728d9c6c5b85c036a7fb3b71b1b24c1cde33e9caedabe8127c"
EXPECTED_WRITER_PROMPT_VERSION = "1.4.0"
EXPECTED_WRITER_PROMPT_SHA256 = "b4b54b39e7d016af94534ff8078b43d2aac556f608c33a8c7bee8697430294e7"
RESULTS_ROOT = ROOT / "evals" / "results"
DEFAULT_OUTPUT = RESULTS_ROOT / "phase12-remediation-resolver-v4-report.json"
HISTORICAL_REPORTS = {
    "phase12-remediation-synthetic-report.json",
    "phase12-direct-bedrock-report.json",
    "phase12-direct-bedrock-followup-report.json",
    "phase12-direct-bedrock-final-report.json",
    "phase12-remediation-real-report.json",
    "phase12-remediation-resolver-v2-report.json",
    "phase12-remediation-resolver-v3-report.json",
    "phase12-remediation-writer-v1-report.json",
    "phase12-remediation-writer-v2-report.json",
}
CASE_GROUPS = {
    "factual": tuple(f"synthetic-factual-{index:02d}" for index in range(1, 4)),
    "partial": tuple(f"synthetic-partial-{index:02d}" for index in range(1, 4)),
    "injection": tuple(f"synthetic-injection-{index:02d}" for index in range(1, 4)),
}


class PlannedCall(TypedDict):
    caseId: str
    category: str
    stage: str
    maxAttempts: int
    retries: int


class RealConverseClient(Protocol):
    def converse(self, **kwargs: object) -> Mapping[str, object]: ...


def build_real_run_plan() -> tuple[PlannedCall, ...]:
    """Return the fixed nine-case, eighteen-call upper-bound plan."""
    plan: list[PlannedCall] = []
    for category, case_ids in CASE_GROUPS.items():
        for case_id in case_ids:
            plan.append({"caseId": case_id, "category": category, "stage": "resolver", "maxAttempts": 1, "retries": 0})
            plan.append({"caseId": case_id, "category": category, "stage": "writer", "maxAttempts": 1, "retries": 0})
    if len(plan) != MAX_REAL_MODEL_INVOCATIONS:
        raise AssertionError("bounded remediation plan must contain exactly eighteen invocations")
    return tuple(plan)


def assert_output_path(path: Path) -> Path:
    """Allow only reports inside evals/results and never historical names."""
    if not isinstance(path, Path):
        raise TypeError("output path must be a Path")
    resolved = path.resolve()
    try:
        resolved.relative_to(RESULTS_ROOT.resolve())
    except ValueError as exc:
        raise ValueError("real remediation reports must remain under evals/results") from exc
    if resolved.name in HISTORICAL_REPORTS:
        raise ValueError("historical evaluation reports are immutable")
    return resolved


def _cases() -> tuple[Mapping[str, object], ...]:
    cases = tuple(SYNTHETIC_CASES)
    if len(cases) != MAX_SAMPLE_COUNT:
        raise RuntimeError("real remediation runner requires exactly nine fixtures")
    counts = {category: 0 for category in CASE_GROUPS}
    ids: list[str] = []
    for case in cases:
        case_id = case.get("caseId")
        category = case.get("category")
        if not isinstance(case_id, str) or category not in counts or case_id in ids:
            raise RuntimeError("fixture set is not the approved nine-case set")
        ids.append(case_id)
        counts[category] += 1  # type: ignore[index]
    if counts != {"factual": 3, "partial": 3, "injection": 3}:
        raise RuntimeError("fixture categories must contain exactly three cases each")
    expected_ids = tuple(
        case_id
        for category_case_ids in CASE_GROUPS.values()
        for case_id in category_case_ids
    )
    if tuple(ids) != expected_ids:
        raise RuntimeError("fixture IDs or ordering differ from the approved nine-case set")
    return cases


def preflight_real_run(output_path: Path | None = None) -> dict[str, object]:
    """Validate limits and prompt metadata without importing boto3."""
    artifact = FileSystemSystemPromptProvider().load()
    if artifact.version != EXPECTED_PROMPT_VERSION or artifact.sha256 != EXPECTED_PROMPT_SHA256:
        raise RuntimeError("real runner requires the exact approved prompt 1.3.0 artifact")
    if (
        EVIDENCE_RESOLVER_PROMPT_VERSION != EXPECTED_RESOLVER_PROMPT_VERSION
        or EVIDENCE_RESOLVER_PROMPT_SHA256 != EXPECTED_RESOLVER_PROMPT_SHA256
        or ANSWER_WRITER_PROMPT_VERSION != EXPECTED_WRITER_PROMPT_VERSION
        or ANSWER_WRITER_PROMPT_SHA256 != EXPECTED_WRITER_PROMPT_SHA256
    ):
        raise RuntimeError("real runner requires the exact approved resolver/writer prompts")
    cases = _cases()
    if output_path is not None:
        assert_output_path(output_path)
    return {
        "runner": "legaldesk-phase12-remediation-real",
        "runnerVersion": RUNNER_VERSION,
        "mode": "preflight",
        "region": REGION,
        "model": MODEL_ID,
        "promptVersion": artifact.version,
        "promptHash": artifact.sha256,
        "resolverPromptVersion": EVIDENCE_RESOLVER_PROMPT_VERSION,
        "resolverPromptHash": EVIDENCE_RESOLVER_PROMPT_SHA256,
        "writerPromptVersion": ANSWER_WRITER_PROMPT_VERSION,
        "writerPromptHash": ANSWER_WRITER_PROMPT_SHA256,
        "totalCases": len(cases),
        "maxResolverInvocations": MAX_RESOLVER_INVOCATIONS,
        "maxWriterInvocations": MAX_WRITER_INVOCATIONS,
        "maxModelInvocations": MAX_REAL_MODEL_INVOCATIONS,
        "retryCount": 0,
        "fixtureCategories": {category: 3 for category in CASE_GROUPS},
        "structuredOutput": True,
        "metadataOnly": True,
    }


class _BoundedClient:
    """Count calls by stage and fail closed if a cap is exceeded."""
    def __init__(self, client: RealConverseClient, stage: str, limit: int) -> None:
        self._client, self._stage, self._limit, self.calls = client, stage, limit, 0

    def converse(self, **kwargs: object) -> Mapping[str, object]:
        if self.calls >= self._limit:
            raise RuntimeError(f"{self._stage} invocation cap exceeded")
        self.calls += 1
        return self._client.converse(**kwargs)


def _deterministic_grounding_detailed(
    answer: str,
    case: Mapping[str, object],
    citation_ids: tuple[str, ...],
) -> tuple[Mapping[str, object], str]:
    """Use the fixture-declared claim oracle; no third model call is made."""
    spec = case.get("groundingSpec")
    passages = case.get("passages")
    if not isinstance(spec, GroundingSpec) or not isinstance(passages, (list, tuple)):
        return ({"grounded": False, "score": 0.0, "matchedCitationIds": []}, "FIXTURE_INVALID")
    available_ids = tuple(
        item.get("citationId")
        for item in passages
        if isinstance(item, Mapping) and isinstance(item.get("citationId"), str)
    )
    evidence_by_citation_id = {
        str(item["citationId"]): str(item["text"])
        for item in passages
        if isinstance(item, Mapping)
        and isinstance(item.get("citationId"), str)
        and isinstance(item.get("text"), str)
    }
    return evaluate_grounding_detailed(
        answer,
        spec,
        citation_ids,
        available_ids,
        evidence_by_citation_id,
    )


def _deterministic_grounding(
    answer: str,
    case: Mapping[str, object],
    citation_ids: tuple[str, ...],
) -> Mapping[str, object]:
    """Compatibility wrapper exposing only the production validator shape."""

    result, _reason_code = _deterministic_grounding_detailed(answer, case, citation_ids)
    return result


def _error_code(stage: str, exc: Exception) -> str:
    """Return a bounded code, never provider exception text."""
    name = type(exc).__name__
    if stage == "resolver" and name == "EvidenceContractError":
        return "RESOLVER_CONTRACT_INVALID"
    if stage == "writer" and name == "EvidenceContractError":
        return "WRITER_CONTRACT_INVALID"
    if isinstance(exc, GroundingContractError):
        return "GROUNDING_INVALID"
    return f"{stage.upper()}_PROVIDER_FAILURE"


def _resolution_mismatch_codes(
    actual: Mapping[str, object],
    expected: Mapping[str, object],
) -> list[str]:
    """Describe only mismatched dimensions; never persist resolver content."""

    codes: list[str] = []
    if actual.get("coverage") != expected.get("coverage"):
        codes.append("RESOLUTION_COVERAGE_MISMATCH")
    if actual.get("conflict") != expected.get("conflict"):
        codes.append("RESOLUTION_CONFLICT_MISMATCH")
    if actual.get("supportingCitationIds") != expected.get("supportingCitationIds"):
        codes.append("RESOLUTION_CITATIONS_MISMATCH")
    return codes or ["RESOLUTION_ORACLE_MISMATCH"]


def run_real_evaluation(*, client: RealConverseClient | None = None, output_path: Path | None = None, execute: bool = False, preflight: bool = False) -> dict[str, object]:
    """Run the separated resolver/writer pipeline with one attempt per stage."""
    if not execute:
        raise RuntimeError("real execution requires --execute")
    if not preflight:
        raise RuntimeError("real execution requires explicit --preflight")
    destination = assert_output_path(output_path or DEFAULT_OUTPUT)
    if destination.exists():
        raise RuntimeError("real remediation report already exists; never overwrite evaluation evidence")
    preflight_real_run(destination)
    artifact = FileSystemSystemPromptProvider().load()
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

    resolver_client = _BoundedClient(client, "resolver", MAX_RESOLVER_INVOCATIONS)
    writer_client = _BoundedClient(client, "writer", MAX_WRITER_INVOCATIONS)
    resolver = ConverseEvidenceResolver(resolver_client, model_id=MODEL_ID)
    writer = ConverseAnswerWriter(writer_client, model_id=MODEL_ID)
    results: list[dict[str, object]] = []
    for case in _cases():
        metadata: dict[str, object] = {
            "caseId": str(case["caseId"]), "category": str(case["category"]),
            "resolverCalled": False, "writerCalled": False, "citationIds": [],
            "validationCodes": [], "errorCodes": [], "accepted": False,
        }
        passages = tuple(case["passages"])  # type: ignore[arg-type]
        supplied_ids = tuple(str(item["citationId"]) for item in passages)  # type: ignore[index]
        try:
            metadata["resolverCalled"] = True
            raw_resolution = resolver.resolve(EvidenceResolutionRequest(str(case["question"]), passages))
            normalized = validate_evidence_resolution(raw_resolution, supplied_ids)
            metadata["evidenceStatus"] = normalized.evidence_status.value
            metadata["citationIds"] = list(normalized.supporting_citation_ids)
            metadata["validationCodes"] = ["RESOLUTION_VALID"]
            expected_resolution = case.get("resolver")
            if not isinstance(expected_resolution, Mapping) or normalized.to_dict() != dict(expected_resolution):
                metadata["errorCodes"] = (
                    ["RESOLUTION_ORACLE_INVALID"]
                    if not isinstance(expected_resolution, Mapping)
                    else _resolution_mismatch_codes(normalized.to_dict(), expected_resolution)
                )
                results.append(metadata)
                continue
            if normalized.coverage.value == "none":
                metadata["validationCodes"] = ["RESOLUTION_VALID", "CANONICAL_NO_EVIDENCE"]
                metadata["accepted"] = True
                results.append(metadata)
                continue
            metadata["writerCalled"] = True
            written = writer.write(AnswerWriterRequest(str(case["question"]), passages, normalized))
            answer = written.get("answer") if isinstance(written, Mapping) else None
            if not isinstance(answer, str) or not answer.strip():
                raise ValueError("writer answer was not text")
            grounding_raw, grounding_reason = _deterministic_grounding_detailed(
                answer,
                case,
                normalized.supporting_citation_ids,
            )
            metadata["groundingDiagnosticCode"] = grounding_reason
            grounding = validate_grounding_result(
                grounding_raw,
                supporting_citation_ids=normalized.supporting_citation_ids,
            )
            metadata["validationCodes"] = ["RESOLUTION_VALID", "WRITER_VALID", "GROUNDING_VALID"]
            metadata["groundingScore"] = grounding.score
            metadata["accepted"] = True
        except Exception as exc:
            stage = "writer" if metadata["writerCalled"] else "resolver"
            metadata["errorCodes"] = [_error_code(stage, exc)]
        results.append(metadata)

    report: dict[str, object] = {
        "runner": "legaldesk-phase12-remediation-real", "runnerVersion": RUNNER_VERSION,
        "mode": "real-bedrock-bounded", "region": REGION, "model": MODEL_ID,
        "promptVersion": artifact.version, "promptHash": artifact.sha256,
        "resolverPromptVersion": EVIDENCE_RESOLVER_PROMPT_VERSION,
        "resolverPromptHash": EVIDENCE_RESOLVER_PROMPT_SHA256,
        "writerPromptVersion": ANSWER_WRITER_PROMPT_VERSION,
        "writerPromptHash": ANSWER_WRITER_PROMPT_SHA256,
        "totalCases": len(results), "acceptedCases": sum(bool(item["accepted"]) for item in results),
        "resolverCalls": resolver_client.calls, "writerCalls": writer_client.calls,
        "inferenceCalls": resolver_client.calls + writer_client.calls, "retryCount": 0,
        "maxModelInvocations": MAX_REAL_MODEL_INVOCATIONS, "metadataOnly": True,
        "historicalReportsImmutable": True, "cases": results,
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="enable the explicitly bounded Bedrock run")
    parser.add_argument("--preflight", action="store_true", help="confirm fixed scope and call limits")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    if not args.execute:
        print(json.dumps(preflight_real_run(args.output), indent=2, sort_keys=True))
        return 0
    if not args.preflight:
        parser.error("--execute requires explicit --preflight")
    report = run_real_evaluation(output_path=args.output, execute=True, preflight=True)
    print(json.dumps({"mode": report["mode"], "acceptedCases": report["acceptedCases"], "totalCases": report["totalCases"], "inferenceCalls": report["inferenceCalls"]}, sort_keys=True))
    return 0 if report["acceptedCases"] == report["totalCases"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
