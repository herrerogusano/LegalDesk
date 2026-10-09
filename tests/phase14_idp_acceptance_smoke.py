"""Offline acceptance-envelope preflight for the remaining Phase 14 proofs.

This module never starts the live runner and never calls AWS or a model.  It
selects the bounded smoke cases and validates metadata-only evidence if an
operator supplies a report produced by a separately approved deployed run.
Absent deployed evidence, every state remains ``NOT_EXECUTED``; offline seed
data is deliberately not promoted to AWS E2E evidence.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "tests" / "fixtures" / "idp" / "manifest.json"
RESULT_ROOT = (ROOT / "evals" / "results").resolve()
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
SMOKE_FIXTURE_IDS = (
    "contract-01-en-digital-monthend",
    "demand-02-es-scanned",
    "judgment-03-en-mixed-interpretive",
    "unknown-01-invoice",
    "negative-01-ambiguous-notice",
    "contract-01-en-digital-monthend",  # duplicate-delivery observation
)


class AcceptanceEnvelopeError(ValueError):
    """Malformed or unsafe metadata-only acceptance evidence."""


def _safe_id(value: object, label: str) -> str:
    if not isinstance(value, str) or SAFE_ID.fullmatch(value) is None:
        raise AcceptanceEnvelopeError(f"{label}_invalid")
    return value


def _sha(value: object, label: str) -> str:
    if not isinstance(value, str) or SHA256.fullmatch(value.lower()) is None:
        raise AcceptanceEnvelopeError(f"{label}_invalid")
    return value.lower()


def _manifest() -> dict[str, dict[str, object]]:
    try:
        payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AcceptanceEnvelopeError("fixture_manifest_unavailable") from exc
    fixtures = payload.get("fixtures") if isinstance(payload, Mapping) else None
    if not isinstance(fixtures, list) or not fixtures:
        raise AcceptanceEnvelopeError("fixture_manifest_invalid")
    result: dict[str, dict[str, object]] = {}
    for item in fixtures:
        if not isinstance(item, Mapping):
            raise AcceptanceEnvelopeError("fixture_manifest_invalid")
        fixture_id = item.get("id")
        digest = item.get("sha256")
        page_count = item.get("page_count")
        if not isinstance(fixture_id, str) or SAFE_ID.fullmatch(fixture_id) is None or fixture_id in result:
            raise AcceptanceEnvelopeError("fixture_manifest_invalid")
        if not isinstance(digest, str) or SHA256.fullmatch(digest.lower()) is None or type(page_count) is not int or page_count < 1:
            raise AcceptanceEnvelopeError("fixture_manifest_invalid")
        result[fixture_id] = {"id": fixture_id, "sha256": digest.lower(), "page_count": page_count, "expected_type": item.get("expected_type")}
    return result


def _not_executed(reason: str) -> dict[str, object]:
    return {"status": "NOT_EXECUTED", "reason": reason}


def _validate_deployed_evidence(raw: Mapping[str, object], *, fixture: Mapping[str, object]) -> dict[str, object]:
    """Validate only evidence that could have come from the deployed contracts."""

    if raw.get("executionClass") != "deployed_aws_e2e":
        reason = "offline_seed_or_local_fake_evidence_is_not_deployed_e2e"
        return {
            "duplicateDelivery": _not_executed(reason),
            "history": _not_executed(reason),
            "ragIsolation": _not_executed(reason),
        }
    _safe_id(raw.get("documentId"), "document_id")
    document_sha = _sha(raw.get("documentSha256"), "document_sha256")
    if document_sha != fixture["sha256"]:
        raise AcceptanceEnvelopeError("document_fixture_hash_mismatch")

    duplicate = raw.get("duplicateDelivery")
    duplicate_result = _not_executed("deployed_duplicate_delivery_evidence_missing")
    if isinstance(duplicate, Mapping):
        before = _safe_id(duplicate.get("jobIdBefore"), "duplicate_job_before")
        after = _safe_id(duplicate.get("jobIdAfter"), "duplicate_job_after")
        paid_before = duplicate.get("paidCallsBefore")
        paid_after = duplicate.get("paidCallsAfter")
        if before != after or type(paid_before) is not int or type(paid_after) is not int or paid_before != paid_after:
            raise AcceptanceEnvelopeError("duplicate_delivery_replayed_or_job_changed")
        duplicate_result = {"status": "EXECUTED", "jobId": after, "paidCallsDelta": 0}

    history = raw.get("history")
    history_result = _not_executed("three_authoritative_persisted_runs_missing")
    if isinstance(history, Mapping) and history.get("executionClass") == "deployed_aws_e2e":
        runs = history.get("runs")
        if not isinstance(runs, list) or len(runs) < 3:
            raise AcceptanceEnvelopeError("history_requires_three_runs")
        parsed: list[tuple[str, str]] = []
        for run in runs[:3]:
            if not isinstance(run, Mapping):
                raise AcceptanceEnvelopeError("history_run_invalid")
            parsed.append((_safe_id(run.get("runId"), "history_run_id"), _sha(run.get("documentSha256"), "history_document_sha256")))
        if any(item[1] != document_sha for item in parsed) or len({item[0] for item in parsed}) != 3:
            raise AcceptanceEnvelopeError("history_source_identity_invalid")
        history_result = {"status": "EXECUTED", "runCount": len(runs), "sourceSha256": document_sha}

    isolation = raw.get("ragIsolation")
    rag_result = _not_executed("deployed_failed_or_skipped_rag_evidence_missing")
    if isinstance(isolation, Mapping):
        status = isolation.get("idpStatus")
        if status not in {"IDP_FAILED", "IDP_SKIPPED"}:
            raise AcceptanceEnvelopeError("rag_isolation_requires_failed_or_skipped_idp")
        before = isolation.get("ragPointerBefore")
        after = isolation.get("ragPointerAfter")
        if not isinstance(before, str) or not isinstance(after, str) or before != after:
            raise AcceptanceEnvelopeError("rag_pointer_changed_on_idp_failure")
        rag_result = {"status": "EXECUTED", "idpStatus": status, "ragPointerStable": True}

    return {"duplicateDelivery": duplicate_result, "history": history_result, "ragIsolation": rag_result}


def build_acceptance_report(*, evidence_path: Path | None = None) -> dict[str, object]:
    manifest = _manifest()
    selected: list[dict[str, object]] = []
    seen: set[str] = set()
    for fixture_id in SMOKE_FIXTURE_IDS:
        if fixture_id not in manifest:
            raise AcceptanceEnvelopeError("smoke_fixture_not_allowlisted")
        fixture = manifest[fixture_id]
        selected.append({"fixtureId": fixture_id, "documentSha256": fixture["sha256"], "documentType": fixture["expected_type"], "status": "NOT_EXECUTED"})
        if fixture_id in seen:
            selected[-1]["purpose"] = "duplicate_delivery"
        elif fixture_id == "negative-01-ambiguous-notice":
            selected[-1]["purpose"] = "failed_or_skipped_idp_rag_isolation"
        seen.add(fixture_id)

    if evidence_path is None:
        proofs = {
            "duplicateDelivery": _not_executed("requires deployed SQS replay with same durable job and unchanged paid ledger"),
            "history": _not_executed("requires three authoritative persisted runs; no offline reprocess is claimed"),
            "ragIsolation": _not_executed("requires deployed IDP_FAILED/IDP_SKIPPED plus unchanged RAG pointer"),
        }
        evidence_source = "none"
    else:
        path = evidence_path.resolve()
        try:
            path.relative_to(RESULT_ROOT)
        except ValueError as exc:
            raise AcceptanceEnvelopeError("evidence_path_out_of_scope") from exc
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AcceptanceEnvelopeError("evidence_unavailable") from exc
        if not isinstance(raw, Mapping):
            raise AcceptanceEnvelopeError("evidence_invalid")
        proofs = _validate_deployed_evidence(raw, fixture=manifest[SMOKE_FIXTURE_IDS[0]])
        evidence_source = "metadata_only_file"

    return {
        "runner": "legaldesk-phase14-idp-acceptance-smoke",
        "mode": "offline_acceptance_preflight",
        "awsCalls": 0,
        "paidCalls": 0,
        "fixtureCorpus": {"selected": len(selected), "allowlisted": len(manifest), "syntheticOnly": True},
        "evidenceSource": evidence_source,
        "proofs": proofs,
        "cases": selected,
        "limitations": [
            "No SQS receive, Dynamo read, Bedrock/Textract call, or RAG query is performed.",
            "Offline seeds and local fake-provider results cannot become deployed E2E evidence.",
            "A real duplicate proof must compare the same persisted job ID and paid-call ledger before/after replay.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(build_acceptance_report(evidence_path=args.evidence), sort_keys=True))
        return 0
    except AcceptanceEnvelopeError as exc:
        print(json.dumps({"runner": "legaldesk-phase14-idp-acceptance-smoke", "status": "BLOCKED", "reason": str(exc)}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["AcceptanceEnvelopeError", "build_acceptance_report", "main"]
