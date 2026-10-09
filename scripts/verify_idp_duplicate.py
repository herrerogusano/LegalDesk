"""Bounded duplicate-delivery proof for one already-terminal IDP job.

The default command is read-free: it performs no AWS call and reports the
missing deployed proof.  ``--execute`` is an explicit operator action.  It
uses only exact, server-scoped DynamoDB point reads, the existing IDP SQS
queue adapter, and bounded CloudWatch log filtering.  It never invokes a
Lambda, Gateway tool, model, OCR API, or DynamoDB Scan.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
RESULT_ROOT = (ROOT / "evals" / "results").resolve()
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
MAX_WAIT_SECONDS = 300
MAX_POLL_SECONDS = 60
MAX_LOG_EVENTS = 50
TERMINAL_STATUSES = {"COMPLETED", "IDP_REVIEW_REQUIRED", "IDP_FAILED", "IDP_SKIPPED"}


class DuplicateProofError(ValueError):
    """Missing or unsafe proof input; never fall through to broad discovery."""


@dataclass(frozen=True, slots=True)
class Target:
    tenant_id: str
    matter_id: str
    document_id: str
    run_id: str
    document_sha256: str


def _safe_id(value: object, label: str) -> str:
    if not isinstance(value, str) or SAFE_ID.fullmatch(value) is None:
        raise DuplicateProofError(f"{label}_invalid")
    return value


def _sha(value: object, label: str) -> str:
    if not isinstance(value, str) or SHA256.fullmatch(value.lower()) is None:
        raise DuplicateProofError(f"{label}_invalid")
    return value.lower()


def _partition(tenant_id: str, matter_id: str) -> str:
    return f"TENANT#{tenant_id}#MATTER#{matter_id}"


def _document_key(tenant_id: str, matter_id: str, document_id: str) -> dict[str, str]:
    return {"pk": _partition(tenant_id, matter_id), "sk": f"DOCUMENT#{document_id}"}


def _job_key(tenant_id: str, matter_id: str, job_id: str) -> dict[str, str]:
    return {"pk": _partition(tenant_id, matter_id), "sk": f"IDP#JOB#{job_id}"}


def _run_key(tenant_id: str, matter_id: str, document_id: str, run_id: str) -> dict[str, str]:
    return {"pk": _partition(tenant_id, matter_id), "sk": f"IDP#RUN#{document_id}#{run_id}"}


def _stage_key(tenant_id: str, matter_id: str, run_id: str, stage: str) -> dict[str, str]:
    if stage not in {"CLASSIFIER", "EXTRACTOR"}:
        raise DuplicateProofError("stage_invalid")
    return {"pk": _partition(tenant_id, matter_id), "sk": f"IDP#STAGE#{run_id}#{stage}"}


def load_target(report_path: Path, *, tenant_id: str, matter_id: str) -> Target:
    path = report_path.resolve()
    try:
        path.relative_to(RESULT_ROOT)
    except ValueError as exc:
        raise DuplicateProofError("report_path_out_of_scope") from exc
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DuplicateProofError("smoke_report_unavailable") from exc
    if not isinstance(report, Mapping) or report.get("result") != "PASS":
        raise DuplicateProofError("smoke_report_not_passed")
    return Target(
        tenant_id=_safe_id(tenant_id, "tenant_id"),
        matter_id=_safe_id(matter_id, "matter_id"),
        document_id=_safe_id(report.get("documentId"), "document_id"),
        run_id=_safe_id(report.get("runId"), "run_id"),
        document_sha256=_sha(report.get("documentSha256"), "document_sha256"),
    )


def _point(table: Any, key: Mapping[str, str]) -> dict[str, object]:
    response = table.get_item(Key=dict(key), ConsistentRead=True)
    item = response.get("Item") if isinstance(response, Mapping) else None
    if not isinstance(item, Mapping):
        raise DuplicateProofError("authoritative_item_missing")
    return dict(item)


def read_snapshot(table: Any, target: Target) -> dict[str, object]:
    """Read only the document pointer, its job, run, and two paid stages."""

    document = _point(table, _document_key(target.tenant_id, target.matter_id, target.document_id))
    if any(document.get(name) != expected for name, expected in (("tenantId", target.tenant_id), ("matterId", target.matter_id), ("documentId", target.document_id), ("idpRunId", target.run_id), ("idpDocumentSha256", target.document_sha256))):
        raise DuplicateProofError("document_pointer_scope_or_hash_mismatch")
    job_id = _safe_id(document.get("idpJobId"), "job_id")
    job = _point(table, _job_key(target.tenant_id, target.matter_id, job_id))
    if any(job.get(name) != expected for name, expected in (("tenantId", target.tenant_id), ("matterId", target.matter_id), ("documentId", target.document_id), ("documentSha256", target.document_sha256), ("jobId", job_id))):
        raise DuplicateProofError("job_scope_or_hash_mismatch")
    run = _point(table, _run_key(target.tenant_id, target.matter_id, target.document_id, target.run_id))
    if any(run.get(name) != expected for name, expected in (("tenantId", target.tenant_id), ("matterId", target.matter_id), ("documentId", target.document_id), ("runId", target.run_id), ("documentSha256", target.document_sha256))):
        raise DuplicateProofError("run_scope_or_hash_mismatch")
    stages: dict[str, dict[str, object]] = {}
    for stage in ("CLASSIFIER", "EXTRACTOR"):
        item = _point(table, _stage_key(target.tenant_id, target.matter_id, target.run_id, stage))
        if item.get("runId") != target.run_id or item.get("stage") != stage or item.get("state") != "COMMITTED" or not isinstance(item.get("artifactRef"), str) or not item["artifactRef"]:
            raise DuplicateProofError(f"{stage.lower()}_stage_not_committed_with_artifact")
        stages[stage] = {key: item.get(key) for key in ("runId", "stage", "state", "requestHash", "artifactRef")}
    return {
        "jobId": job_id,
        "job": {key: job.get(key) for key in ("jobId", "status", "documentSha256", "schemaVersion", "modelId", "promptVersion", "correlationId")},
        "document": {key: document.get(key) for key in ("documentId", "idpStatus", "idpRunId", "idpDocumentSha256", "idpGenerationAt", "idpSourceKey", "s3Key")},
        "run": {key: run.get(key) for key in ("runId", "status", "documentSha256", "schemaVersion", "modelId", "promptVersion", "createdAt", "sourceKey")},
        "stages": stages,
    }


def validate_unchanged(before: Mapping[str, object], after: Mapping[str, object]) -> None:
    if before != after:
        raise DuplicateProofError("authoritative_job_run_stage_or_document_pointer_changed")
    job = before.get("job") if isinstance(before.get("job"), Mapping) else {}
    if job.get("status") not in {"COMPLETED", "IDP_REVIEW_REQUIRED"}:
        raise DuplicateProofError("duplicate_target_is_not_a_successful_terminal_job")
    document = before.get("document") if isinstance(before.get("document"), Mapping) else {}
    run = before.get("run") if isinstance(before.get("run"), Mapping) else {}
    if document.get("idpStatus") not in {"IDP_COMPLETED", "IDP_REVIEW_REQUIRED"} or run.get("status") not in {"COMPLETED", "IDP_REVIEW_REQUIRED"}:
        raise DuplicateProofError("duplicate_target_pointer_or_run_is_not_terminal")


def structured_consumption_events(events: list[Mapping[str, object]], *, job_id: str, message_id: str, started_ms: int) -> list[dict[str, int]]:
    """Accept only an explicit worker-consumed telemetry contract.

    Ordinary CloudWatch text or an event containing only a job ID is
    intentionally insufficient proof.  The SQS MessageId must match the
    actual SendMessage response, and the worker timestamp must be valid.
    """

    accepted: list[dict[str, int]] = []
    for event in events:
        message = event.get("message")
        timestamp = event.get("timestamp")
        if not isinstance(message, str) or not isinstance(timestamp, int) or timestamp < started_ms:
            continue
        try:
            payload = json.loads(message)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(payload, Mapping):
            continue
        if set(payload) != {"event", "jobId", "messageId", "outcome", "status", "timestamp"}:
            continue
        worker_timestamp = payload.get("timestamp")
        if (
            payload.get("event") == "idp_worker_consumed"
            and payload.get("jobId") == job_id
            and payload.get("messageId") == message_id
            and payload.get("outcome") == "acknowledged"
            and isinstance(payload.get("status"), str)
            and bool(payload.get("status"))
            and isinstance(worker_timestamp, int)
            and not isinstance(worker_timestamp, bool)
            and worker_timestamp >= started_ms
        ):
            accepted.append({"timestamp": timestamp, "workerTimestamp": worker_timestamp})
    return accepted


def _not_executed(reason: str) -> dict[str, object]:
    return {"result": "NOT_EXECUTED", "reason": reason}


def execute(*, report_path: Path, tenant_id: str, matter_id: str, table_name: str, queue_url: str, log_group: str, wait_seconds: int = 60, poll_seconds: int = 10) -> dict[str, object]:
    if not 1 <= wait_seconds <= MAX_WAIT_SECONDS or not 1 <= poll_seconds <= MAX_POLL_SECONDS:
        raise DuplicateProofError("observation_bounds_invalid")
    target = load_target(report_path, tenant_id=tenant_id, matter_id=matter_id)
    import boto3
    from botocore.config import Config

    config = Config(retries={"total_max_attempts": 1, "mode": "standard"}, connect_timeout=10, read_timeout=20)
    session = boto3.Session(region_name=os.environ.get("AWS_REGION", "eu-west-1"))
    table = session.resource("dynamodb", config=config).Table(table_name)
    before = read_snapshot(table, target)
    started_ms = int(time.time() * 1000)
    sqs = session.client("sqs", config=config)
    # Use the production adapter so the queue body remains exactly {jobId}.
    sys.path.insert(0, str(ROOT / "backend" / "src"))
    from legaldesk.idp_lambda import Boto3IDPSQSQueue
    message_id = Boto3IDPSQSQueue(sqs, queue_url=queue_url).publish(job_id=str(before["jobId"]))

    logs = session.client("logs", config=config)
    deadline = time.monotonic() + wait_seconds
    consumed: list[dict[str, int]] = []
    while time.monotonic() < deadline and not consumed:
        response = logs.filter_log_events(logGroupName=log_group, startTime=started_ms, limit=MAX_LOG_EVENTS, filterPattern='"idp_worker_consumed"')
        events = response.get("events", []) if isinstance(response, Mapping) else []
        consumed = structured_consumption_events([event for event in events if isinstance(event, Mapping)], job_id=str(before["jobId"]), message_id=message_id, started_ms=started_ms)
        if not consumed:
            time.sleep(min(poll_seconds, max(0.0, deadline - time.monotonic())))
    if not consumed:
        return {"result": "NOT_EXECUTED", "reason": "worker_consumed_telemetry_contract_unavailable", "messageSent": True, "messageId": message_id, "jobId": before["jobId"]}
    after = read_snapshot(table, target)
    validate_unchanged(before, after)
    return {"result": "PASS", "jobId": before["jobId"], "messageId": message_id, "consumedEvidence": {"count": len(consumed), "timestamps": [item["timestamp"] for item in consumed], "workerTimestamps": [item["workerTimestamp"] for item in consumed]}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--tenant-id", default=os.environ.get("LEGALDESK_IDP_BETA_TENANT_ID", ""))
    parser.add_argument("--matter-id", default=os.environ.get("LEGALDESK_IDP_BETA_MATTER_ID", ""))
    parser.add_argument("--table-name", default=os.environ.get("LEGALDESK_METADATA_TABLE_NAME", ""))
    parser.add_argument("--queue-url", default=os.environ.get("LEGALDESK_IDP_QUEUE_URL", ""))
    parser.add_argument("--log-group", default=os.environ.get("LEGALDESK_IDP_WORKER_LOG_GROUP", ""))
    parser.add_argument("--wait-seconds", type=int, default=60)
    parser.add_argument("--poll-seconds", type=int, default=10)
    args = parser.parse_args(argv)
    if not args.execute:
        print(json.dumps(_not_executed("explicit --execute is required; no AWS call was made"), sort_keys=True))
        return 0
    try:
        if args.report is None:
            raise DuplicateProofError("report_required")
        expected_tenant = os.environ.get("LEGALDESK_IDP_BETA_TENANT_ID", "").strip()
        expected_matter = os.environ.get("LEGALDESK_IDP_BETA_MATTER_ID", "").strip()
        if not expected_tenant or not expected_matter:
            raise DuplicateProofError("beta_scope_allowlist_missing")
        if args.tenant_id != expected_tenant or args.matter_id != expected_matter:
            raise DuplicateProofError("beta_scope_mismatch")
        for value, label in ((args.tenant_id, "tenant_id"), (args.matter_id, "matter_id"), (args.table_name, "table_name"), (args.queue_url, "queue_url"), (args.log_group, "log_group")):
            if not isinstance(value, str) or not value.strip():
                raise DuplicateProofError(f"{label}_required")
        report = execute(report_path=args.report, tenant_id=args.tenant_id, matter_id=args.matter_id, table_name=args.table_name, queue_url=args.queue_url, log_group=args.log_group, wait_seconds=args.wait_seconds, poll_seconds=args.poll_seconds)
        print(json.dumps(report, sort_keys=True))
        return 0 if report.get("result") == "PASS" else 3
    except DuplicateProofError as exc:
        print(json.dumps({"result": "BLOCKED", "reason": str(exc)}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["DuplicateProofError", "Target", "load_target", "read_snapshot", "structured_consumption_events", "validate_unchanged", "main"]
