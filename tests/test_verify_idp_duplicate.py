from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from verify_idp_duplicate import (  # noqa: E402
    DuplicateProofError,
    Target,
    main,
    read_snapshot,
    structured_consumption_events,
    validate_unchanged,
)


class _PointTable:
    def __init__(self, items: dict[tuple[str, str], dict[str, object]]) -> None:
        self.items = items
        self.calls: list[dict[str, object]] = []

    def get_item(self, *, Key: dict[str, str], **kwargs: object) -> dict[str, object]:
        self.calls.append({"Key": Key, **kwargs})
        item = self.items.get((Key["pk"], Key["sk"]))
        return {"Item": dict(item)} if item is not None else {}


class VerifyIDPDuplicateTests(unittest.TestCase):
    def _snapshot_table(self) -> tuple[_PointTable, Target]:
        target = Target("tenant-beta", "matter-beta", "doc-1", "run-1", "a" * 64)
        pk = "TENANT#tenant-beta#MATTER#matter-beta"
        items: dict[tuple[str, str], dict[str, object]] = {
            (pk, "DOCUMENT#doc-1"): {"tenantId": target.tenant_id, "matterId": target.matter_id, "documentId": target.document_id, "idpJobId": "job-1", "idpRunId": target.run_id, "idpDocumentSha256": target.document_sha256, "idpStatus": "IDP_COMPLETED", "idpGenerationAt": "2026-10-09T00:00:00Z", "idpSourceKey": "source/doc-1", "s3Key": "source/doc-1"},
            (pk, "IDP#JOB#job-1"): {"jobId": "job-1", "tenantId": target.tenant_id, "matterId": target.matter_id, "documentId": target.document_id, "documentSha256": target.document_sha256, "status": "COMPLETED", "schemaVersion": "1.0.0", "modelId": "model", "promptVersion": "prompt", "correlationId": "corr-1"},
            (pk, "IDP#RUN#doc-1#run-1"): {"runId": target.run_id, "tenantId": target.tenant_id, "matterId": target.matter_id, "documentId": target.document_id, "documentSha256": target.document_sha256, "status": "COMPLETED", "schemaVersion": "1.0.0", "modelId": "model", "promptVersion": "prompt", "createdAt": "2026-10-09T00:00:00Z", "sourceKey": "source/doc-1"},
            (pk, "IDP#STAGE#run-1#CLASSIFIER"): {"runId": target.run_id, "stage": "CLASSIFIER", "state": "COMMITTED", "requestHash": "b" * 64, "artifactRef": "idp-artifacts/classifier"},
            (pk, "IDP#STAGE#run-1#EXTRACTOR"): {"runId": target.run_id, "stage": "EXTRACTOR", "state": "COMMITTED", "requestHash": "c" * 64, "artifactRef": "idp-artifacts/extractor"},
        }
        return _PointTable(items), target

    def test_snapshot_uses_only_exact_point_reads_and_accepts_terminal_committed_job(self) -> None:
        table, target = self._snapshot_table()
        snapshot = read_snapshot(table, target)
        self.assertEqual(snapshot["jobId"], "job-1")
        self.assertEqual(len(table.calls), 5)
        self.assertEqual(snapshot["stages"]["CLASSIFIER"]["state"], "COMMITTED")
        validate_unchanged(snapshot, snapshot)

    def test_changed_pointer_or_nonterminal_job_fails_closed(self) -> None:
        table, target = self._snapshot_table()
        before = read_snapshot(table, target)
        after = dict(before)
        after["document"] = dict(before["document"], idpRunId="run-other")
        with self.assertRaises(DuplicateProofError):
            validate_unchanged(before, after)
        nonterminal = dict(before, job=dict(before["job"], status="PROCESSING"))
        with self.assertRaises(DuplicateProofError):
            validate_unchanged(nonterminal, nonterminal)

    def test_consumption_requires_structured_worker_event(self) -> None:
        self.assertEqual(structured_consumption_events([{"timestamp": 10, "message": "job-1 completed"}], job_id="job-1", message_id="message-1", started_ms=0), [])
        events = [{"timestamp": 11, "message": json.dumps({"event": "idp_worker_consumed", "jobId": "job-1", "messageId": "wrong-message", "outcome": "acknowledged", "status": "COMPLETED", "timestamp": 11})}]
        self.assertEqual(structured_consumption_events(events, job_id="job-1", message_id="message-1", started_ms=10), [])
        events[0]["message"] = json.dumps({"event": "idp_worker_consumed", "jobId": "job-1", "messageId": "message-1", "outcome": "acknowledged", "status": "COMPLETED", "timestamp": 11})
        self.assertEqual(structured_consumption_events(events, job_id="job-1", message_id="message-1", started_ms=10), [{"timestamp": 11, "workerTimestamp": 11}])

    def test_default_command_makes_no_aws_call(self) -> None:
        self.assertEqual(main([]), 0)


if __name__ == "__main__":
    unittest.main()
