"""Generation and public-projection contracts for the Phase 14 IDP store.

These tests use the production Dynamo repository against a tiny conditional
table seam.  No AWS SDK client, network, model, or document provider is used.
"""

from __future__ import annotations

import re
import sys
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "backend" / "src"))

from legaldesk.idp import (  # noqa: E402
    Boto3DynamoIDPRepository,
    DocumentType,
    FieldAcceptance,
    FieldOrigin,
    FieldPresence,
    IDPContractError,
    IDPConcurrencyError,
    IDPExtractionRun,
    IDPFieldResult,
    IDPJob,
    IDPJobStatus,
    matter_partition_key,
)


class _ExpressionTable:
    """Dynamo-shaped seam for the repository's bounded SET/condition subset."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, object]] = {}
        self.updates: list[dict[str, object]] = []

    @staticmethod
    def _key(item: dict[str, object]) -> tuple[str, str]:
        return str(item["pk"]), str(item["sk"])

    @staticmethod
    def _split(expression: str, operator: str) -> list[str]:
        marker = f" {operator} "
        parts: list[str] = []
        start = 0
        depth = 0
        index = 0
        while index < len(expression):
            if expression[index] == "(":
                depth += 1
            elif expression[index] == ")":
                depth -= 1
            if depth == 0 and expression.startswith(marker, index):
                parts.append(expression[start:index])
                index += len(marker)
                start = index
                continue
            index += 1
        return parts + [expression[start:]] if parts else [expression]

    @classmethod
    def _value(cls, token: str, names: dict[str, str], values: dict[str, object], item: dict[str, object]) -> object:
        token = token.strip()
        if token.startswith("#"):
            return item.get(names[token])
        if token.startswith(":"):
            return values[token]
        return token

    @classmethod
    def _holds(cls, expression: str | None, names: dict[str, str], values: dict[str, object], item: dict[str, object] | None) -> bool:
        if not expression:
            return True
        current = item or {}
        text = expression.strip()
        while text.startswith("(") and text.endswith(")"):
            depth = 0
            wrapped = True
            for index, char in enumerate(text):
                depth += char == "("
                depth -= char == ")"
                if depth == 0 and index != len(text) - 1:
                    wrapped = False
                    break
            if not wrapped:
                break
            text = text[1:-1].strip()
        ors = cls._split(text, "OR")
        if len(ors) > 1:
            return any(cls._holds(part, names, values, current) for part in ors)
        ands = cls._split(text, "AND")
        if len(ands) > 1:
            return all(cls._holds(part, names, values, current) for part in ands)
        match = re.fullmatch(r"attribute_not_exists\(([^)]+)\)", text)
        if match:
            return names.get(match.group(1).strip(), match.group(1).strip()) not in current
        match = re.fullmatch(r"attribute_exists\(([^)]+)\)", text)
        if match:
            return names.get(match.group(1).strip(), match.group(1).strip()) in current
        match = re.fullmatch(r"(.+?)\s+NOT IN\s*\(([^)]*)\)", text)
        if match:
            left = cls._value(match.group(1), names, values, current)
            return all(left != cls._value(value, names, values, current) for value in match.group(2).split(","))
        match = re.fullmatch(r"(.+?)\s+IN\s*\(([^)]*)\)", text)
        if match:
            left = cls._value(match.group(1), names, values, current)
            return any(left == cls._value(value, names, values, current) for value in match.group(2).split(","))
        match = re.fullmatch(r"(.+?)\s*(<=|>=|=|<|>)\s*(.+)", text)
        if not match:
            raise AssertionError(f"unsupported condition in test seam: {text}")
        left = cls._value(match.group(1), names, values, current)
        right = cls._value(match.group(3), names, values, current)
        if match.group(2) == "=":
            return left == right
        if left is None or right is None:
            return False
        return {"<": left < right, "<=": left <= right, ">": left > right, ">=": left >= right}[match.group(2)]

    def get_item(self, *, Key: dict[str, str], **_: object) -> dict[str, object]:
        item = self.items.get((Key["pk"], Key["sk"]))
        return {"Item": dict(item)} if item is not None else {}

    def put_item(self, *, Item: dict[str, object], ConditionExpression: str | None = None, ExpressionAttributeNames=None, ExpressionAttributeValues=None, **_: object) -> None:
        key = self._key(Item)
        if not self._holds(ConditionExpression, ExpressionAttributeNames or {}, ExpressionAttributeValues or {}, self.items.get(key)):
            raise RuntimeError("conditional put failed")
        self.items[key] = dict(Item)

    def update_item(self, *, Key: dict[str, str], UpdateExpression: str, ConditionExpression: str | None = None, ExpressionAttributeNames=None, ExpressionAttributeValues=None, **_: object) -> None:
        key = (Key["pk"], Key["sk"])
        if key not in self.items:
            raise RuntimeError("update target missing")
        names = ExpressionAttributeNames or {}
        values = ExpressionAttributeValues or {}
        item = self.items[key]
        self.updates.append({"UpdateExpression": UpdateExpression, "ConditionExpression": ConditionExpression, "ExpressionAttributeValues": values})
        if not self._holds(ConditionExpression, names, values, item):
            raise RuntimeError("conditional update failed")
        set_part = UpdateExpression.split("REMOVE", 1)[0].removeprefix("SET ")
        for assignment in set_part.split(","):
            lhs, _, rhs = assignment.strip().partition("=")
            if rhs.strip() in values:
                item[names.get(lhs.strip(), lhs.strip())] = values[rhs.strip()]


def _run(*, run_id: str, status: IDPJobStatus, created_at: datetime, model: str = "model-a", prompt: str = "prompt-a", schema: str = "1.0.0") -> IDPExtractionRun:
    return IDPExtractionRun(
        run_id=run_id, tenant_id="tenant-a", matter_id="matter-a", document_id="document-a",
        document_sha256="a" * 64, document_type=DocumentType.CONTRACT, schema_version=schema,
        model_id=model, prompt_version=prompt, status=status, created_at=created_at, source_key="canonical.pdf",
        fields={"amount": IDPFieldResult(field="amount", value=12.5, presence=FieldPresence.PRESENT, origin=FieldOrigin.LITERAL, acceptance=FieldAcceptance.PROVISIONAL, schema_version=schema)},
    )


def _job(*, job_id: str, status: IDPJobStatus, created_at: datetime) -> IDPJob:
    return IDPJob(
        job_id=job_id, tenant_id="tenant-a", matter_id="matter-a", document_id="document-a",
        document_sha256="a" * 64, idempotency_key=f"idempotency-{job_id}", schema_version="1.0.0",
        model_id="model-a", prompt_version="prompt-a", status=status, created_at=created_at, updated_at=created_at,
    )


class GenerationContractTests(unittest.TestCase):
    def _pointer_table(self) -> _ExpressionTable:
        table = _ExpressionTable()
        table.items[(matter_partition_key("tenant-a", "matter-a"), "DOCUMENT#document-a")] = {
            "pk": matter_partition_key("tenant-a", "matter-a"), "sk": "DOCUMENT#document-a", "documentId": "document-a", "s3Key": "canonical.pdf",
        }
        return table

    def test_document_projection_serializes_public_idp_prefixes(self) -> None:
        expected = {
            IDPJobStatus.COMPLETED: "IDP_COMPLETED", IDPJobStatus.FAILED: "IDP_FAILED",
            IDPJobStatus.REVIEW_REQUIRED: "IDP_REVIEW_REQUIRED", IDPJobStatus.SKIPPED: "IDP_SKIPPED",
        }
        for status, public_status in expected.items():
            with self.subTest(status=status):
                table = self._pointer_table()
                repository = Boto3DynamoIDPRepository("synthetic", table=table)
                repository.project_document_status(run=_run(run_id=f"run-{status}", status=status, created_at=datetime(2026, 1, 1, tzinfo=timezone.utc)))
                pointer = table.items[(matter_partition_key("tenant-a", "matter-a"), "DOCUMENT#document-a")]
                self.assertEqual(pointer["idpStatus"], public_status)

    def test_job_projection_serializes_pending_and_processing_prefixes(self) -> None:
        expected = {IDPJobStatus.QUEUED: "PENDING_IDP", IDPJobStatus.PROCESSING: "PROCESSING_IDP"}
        for status, public_status in expected.items():
            with self.subTest(status=status):
                table = self._pointer_table()
                repository = Boto3DynamoIDPRepository("synthetic", table=table)
                repository.project_document_job_status(job=_job(job_id=f"job-{status}", status=status, created_at=datetime(2026, 1, 1, tzinfo=timezone.utc)), source_key="canonical.pdf")
                pointer = table.items[(matter_partition_key("tenant-a", "matter-a"), "DOCUMENT#document-a")]
                self.assertEqual(pointer["idpStatus"], public_status)

    def test_newer_run_fences_older_job_terminal_projection(self) -> None:
        table = self._pointer_table()
        repository = Boto3DynamoIDPRepository("synthetic", table=table)
        repository.project_document_status(run=_run(run_id="newer-run", status=IDPJobStatus.COMPLETED, created_at=datetime(2026, 2, 1, tzinfo=timezone.utc)))
        with self.assertRaises(IDPConcurrencyError):
            repository.project_document_job_status(job=_job(job_id="older-job", status=IDPJobStatus.FAILED, created_at=datetime(2026, 1, 1, tzinfo=timezone.utc)), source_key="canonical.pdf")
        self.assertEqual(table.items[(matter_partition_key("tenant-a", "matter-a"), "DOCUMENT#document-a")]["idpStatus"], "IDP_COMPLETED")

    def test_newer_job_fences_older_run_terminal_projection(self) -> None:
        table = self._pointer_table()
        repository = Boto3DynamoIDPRepository("synthetic", table=table)
        repository.project_document_job_status(job=_job(job_id="newer-job", status=IDPJobStatus.FAILED, created_at=datetime(2026, 2, 1, tzinfo=timezone.utc)), source_key="canonical.pdf")
        with self.assertRaises(IDPConcurrencyError):
            repository.project_document_status(run=_run(run_id="older-run", status=IDPJobStatus.COMPLETED, created_at=datetime(2026, 1, 1, tzinfo=timezone.utc)))
        self.assertEqual(table.items[(matter_partition_key("tenant-a", "matter-a"), "DOCUMENT#document-a")]["idpStatus"], "IDP_FAILED")

    def test_older_failure_cannot_replace_newer_completed_projection(self) -> None:
        table = self._pointer_table()
        repository = Boto3DynamoIDPRepository("synthetic", table=table)
        newer = _run(run_id="newer", status=IDPJobStatus.COMPLETED, created_at=datetime(2026, 2, 1, tzinfo=timezone.utc))
        older = _run(run_id="older", status=IDPJobStatus.FAILED, created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
        repository.project_document_status(run=newer)
        with self.assertRaises(IDPConcurrencyError):
            repository.project_document_status(run=older)
        pointer = table.items[(matter_partition_key("tenant-a", "matter-a"), "DOCUMENT#document-a")]
        self.assertEqual(pointer["idpStatus"], "IDP_COMPLETED")
        self.assertEqual(pointer["idpRunId"], "newer")

    def test_older_success_cannot_overwrite_newer_terminal_generation(self) -> None:
        table = self._pointer_table()
        repository = Boto3DynamoIDPRepository("synthetic", table=table)
        newer = _run(run_id="newer-failure", status=IDPJobStatus.FAILED, created_at=datetime(2026, 2, 1, tzinfo=timezone.utc))
        older = _run(run_id="older-success", status=IDPJobStatus.COMPLETED, created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
        repository.project_document_status(run=newer)
        with self.assertRaises(IDPConcurrencyError):
            repository.project_document_status(run=older)
        pointer = table.items[(matter_partition_key("tenant-a", "matter-a"), "DOCUMENT#document-a")]
        self.assertEqual(pointer["idpStatus"], "IDP_FAILED")
        self.assertEqual(pointer["idpRunId"], "newer-failure")

    def test_repository_immutable_run_projection_rejects_config_collision(self) -> None:
        for changed in ("model_id", "prompt_version", "schema_version"):
            with self.subTest(changed=changed):
                table = _ExpressionTable()
                repository = Boto3DynamoIDPRepository("synthetic", table=table)
                base = _run(run_id="stable-run", status=IDPJobStatus.COMPLETED, created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
                repository.save_run(base)
                replacement = replace(base, **{changed: {"model_id": "model-b", "prompt_version": "prompt-b", "schema_version": "2.0.0"}[changed]})
                with self.assertRaises(IDPConcurrencyError):
                    repository.save_run(replacement)

    def test_production_recovery_rejects_existing_run_identity_mismatch_without_promotion(self) -> None:
        # Reuse only the already-authenticated offline composition fixture;
        # provider transports remain the local deterministic fakes.
        from unittest.mock import patch
        from tests.test_phase14_idp_runtime_contract import RuntimeContractTests

        fixture = RuntimeContractTests()
        prompt_path = lambda kind: Path(__file__).parents[1] / "prompts" / f"idp-{kind}.md"
        for changed in ("model_id", "prompt_version", "schema_version"):
            with self.subTest(changed=changed), patch("legaldesk.idp.providers.default_idp_prompt_path", prompt_path):
                repository, _metadata, _reader, _processor, worker, bedrock, _textract, _table, job = fixture._context("contract-01-en-digital-monthend.pdf")
                wrong = _run(run_id=job.job_id, status=IDPJobStatus.COMPLETED, created_at=job.created_at)
                wrong = replace(wrong, **{changed: {"model_id": "wrong-model", "prompt_version": "wrong-prompt", "schema_version": "9.9.9"}[changed]})
                repository.save_run(wrong)
                with self.assertRaises(IDPContractError):
                    worker.process_message({"jobId": job.job_id})
                persisted = repository.get_run(tenant_id="tenant-a", matter_id="matter-a", document_id="document-a", run_id=job.job_id)
                self.assertEqual(persisted, wrong)
                self.assertEqual(len(bedrock.calls), 2)


if __name__ == "__main__":
    unittest.main()
