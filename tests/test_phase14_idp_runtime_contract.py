"""Offline production-composition contracts for the Phase 14 IDP runtime.

The worker, authoritative reader, production processor, real pipeline and
runtime adapters are exercised together.  Only provider transports (S3,
Bedrock and Textract) and DynamoDB are in-memory fakes; no processor result is
stubbed and no AWS/network call is made.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sys
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest import mock
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parents[1] / "backend" / "src"))

from legaldesk.domain.models import DocumentStatus, MalwareScanStatus  # noqa: E402
from legaldesk.idp import (  # noqa: E402
    Boto3DynamoIDPRepository,
    DocumentForIDP,
    IDPConfig,
    IDPContractError,
    IDPDeliveryAmbiguous,
    IDPJobStatus,
    IDPExtractionRun,
    IDPFieldResult,
    DocumentType,
    FieldAcceptance,
    FieldOrigin,
    FieldPresence,
    InMemoryIDPArtifactStore,
    InMemoryIDPRepository,
    create_verified_clean_job,
)
from legaldesk.idp.ocr import (  # noqa: E402
    Boto3DynamoOCRJobStore,
    Boto3TextractProvider,
    OCRCoordinator,
)
from legaldesk.idp.providers import IDPModelConfig  # noqa: E402
from legaldesk.idp import providers as _providers  # noqa: E402
from legaldesk.idp.runtime import (  # noqa: E402
    Boto3IDPDocumentReader,
    IDPProductionConfig,
    ProductionIDPProcessor,
)
from legaldesk.idp.worker import IDPWorker, build_verified_clean_job  # noqa: E402
from legaldesk.idp_ocr_lambda import lambda_handler  # noqa: E402
from legaldesk.idp.trigger import VerifiedCleanIDPTrigger  # noqa: E402


ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "idp" / "pdfs"
QUEUE_ARN = "arn:aws:sqs:eu-west-1:111122223333:legaldesk-idp-ocr"
TOPIC_ARN = "arn:aws:sns:eu-west-1:111122223333:legaldesk-ocr"


class _ConditionAwareTable:
    """Small Dynamo seam retaining conditional/transaction boundaries."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, object]] = {}
        self.name = "synthetic-idp-table"
        self.meta = SimpleNamespace(client=self)

    @staticmethod
    def _key(item: dict[str, object]) -> tuple[str, str]:
        return str(item["pk"]), str(item["sk"])

    @staticmethod
    def _split_top(expression: str, operator: str) -> list[str]:
        parts: list[str] = []
        start = 0
        depth = 0
        marker = f" {operator} "
        index = 0
        while index <= len(expression) - len(marker):
            char = expression[index]
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            if depth == 0 and expression.startswith(marker, index):
                parts.append(expression[start:index])
                index += len(marker)
                start = index
                continue
            index += 1
        if parts:
            parts.append(expression[start:])
            return parts
        return [expression]

    @classmethod
    def _condition_value(cls, token: str, names: dict[str, str], values: dict[str, object], item: dict[str, object]) -> object:
        token = token.strip()
        if token.startswith("#"):
            return item.get(names[token])
        if token.startswith(":"):
            return values.get(token)
        return token

    @classmethod
    def _condition_holds(cls, expression: str | None, names: dict[str, str], values: dict[str, object], item: dict[str, object] | None) -> bool:
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
        ors = cls._split_top(text, "OR")
        if len(ors) > 1:
            return any(cls._condition_holds(part, names, values, current) for part in ors)
        ands = cls._split_top(text, "AND")
        if len(ands) > 1:
            return all(cls._condition_holds(part, names, values, current) for part in ands)
        match = re.fullmatch(r"attribute_not_exists\(([^)]+)\)", text)
        if match:
            key = names.get(match.group(1).strip(), match.group(1).strip())
            return key not in current
        match = re.fullmatch(r"attribute_exists\(([^)]+)\)", text)
        if match:
            key = names.get(match.group(1).strip(), match.group(1).strip())
            return key in current
        match = re.fullmatch(r"(.+?)\s+IN\s*\(([^)]*)\)", text)
        if match:
            left = cls._condition_value(match.group(1), names, values, current)
            return any(left == cls._condition_value(value, names, values, current) for value in match.group(2).split(","))
        match = re.fullmatch(r"(.+?)\s*(<=|>=|=|<|>)\s*(.+)", text)
        if not match:
            raise AssertionError(f"unsupported synthetic Dynamo condition: {text}")
        left = cls._condition_value(match.group(1), names, values, current)
        right = cls._condition_value(match.group(3), names, values, current)
        if match.group(2) == "=":
            return left == right
        if left is None or right is None:
            return False
        return {"<": left < right, "<=": left <= right, ">": left > right, ">=": left >= right}[match.group(2)]

    def get_item(self, *, Key: dict[str, str], **_: object) -> dict[str, object]:
        item = self.items.get((Key["pk"], Key["sk"]))
        return {"Item": dict(item)} if item is not None else {}

    def put_item(self, *, Item: dict[str, object], ConditionExpression: str | None = None, ExpressionAttributeNames: dict[str, str] | None = None, ExpressionAttributeValues: dict[str, object] | None = None, **_: object) -> None:
        key = self._key(Item)
        if not self._condition_holds(ConditionExpression, ExpressionAttributeNames or {}, ExpressionAttributeValues or {}, self.items.get(key)):
            raise RuntimeError("conditional put failed")
        self.items[key] = dict(Item)

    def update_item(self, *, Key: dict[str, str], UpdateExpression: str, ConditionExpression: str | None = None, ExpressionAttributeNames: dict[str, str] | None = None, ExpressionAttributeValues: dict[str, object] | None = None, **_: object) -> None:
        key = (Key["pk"], Key["sk"])
        if key not in self.items:
            raise RuntimeError("conditional update target missing")
        item = self.items[key]
        names = ExpressionAttributeNames or {}
        values = ExpressionAttributeValues or {}
        if not self._condition_holds(ConditionExpression, names, values, item):
            raise RuntimeError("conditional update failed")
        # The production adapters use Dynamo's expression aliases.  Apply the
        # small SET/REMOVE subset used by the real persistence and OCR stores,
        # including expressions whose value alias does not mirror its field
        # name (e.g. ``#state = :committed``).
        expression = UpdateExpression.replace("REMOVE", "|REMOVE", 1)
        set_part, _, remove_part = expression.partition("|REMOVE")
        if set_part.startswith("SET "):
            for assignment in set_part[4:].split(","):
                lhs, _, rhs = assignment.strip().partition("=")
                if not lhs or not rhs:
                    continue
                lhs, rhs = lhs.strip(), rhs.strip()
                if rhs in values:
                    item[names.get(lhs, lhs)] = values[rhs]
                elif "+" in rhs:
                    base, _, increment = rhs.partition("+")
                    if base.strip() in names and increment.strip() in values:
                        attribute = names[base.strip()]
                        item[attribute] = int(item.get(attribute, 0)) + int(values[increment.strip()])
        if "#attempt" in names and ":one" in values:
            item[names["#attempt"]] = int(item.get(names["#attempt"], 0)) + int(values[":one"])
        for alias in remove_part.split(","):
            alias = alias.strip()
            if alias in names:
                item.pop(names[alias], None)
        self.items[key] = item

    def transact_write_items(self, *, TransactItems: list[dict[str, object]], **_: object) -> None:
        pending: list[dict[str, object]] = []
        snapshot = dict(self.items)
        for operation in TransactItems:
            put = operation.get("Put") if isinstance(operation, dict) else None
            if not isinstance(put, dict) or not isinstance(put.get("Item"), dict):
                raise RuntimeError("unsupported synthetic transaction")
            item = dict(put["Item"])
            key = self._key(item)
            condition = put.get("ConditionExpression")
            if not self._condition_holds(condition if isinstance(condition, str) else None, put.get("ExpressionAttributeNames", {}) or {}, put.get("ExpressionAttributeValues", {}) or {}, snapshot.get(key)):
                raise RuntimeError("conditional transaction failed")
            pending.append(item)
            snapshot[key] = item
        for item in pending:
            self.items[self._key(item)] = item


class _CrashAfterPersistRepository(InMemoryIDPRepository):
    """Inject the durable crash window after save_run, before PERSISTED."""

    def __init__(self) -> None:
        super().__init__()
        self.crash_after_save = True

    def checkpoint_job(self, *, claim, checkpoint, status=None, skip_reason=None, now=None):
        if checkpoint.value == "PERSISTED" and self.crash_after_save:
            self.crash_after_save = False
            raise RuntimeError("synthetic crash after durable run save")
        return super().checkpoint_job(claim=claim, checkpoint=checkpoint, status=status, skip_reason=skip_reason, now=now)

    def expire_claim(self, job_id: str) -> None:
        job = self.get_job(job_id)
        self._jobs[job_id] = replace(job, claimed_until=datetime.now(timezone.utc) - timedelta(seconds=1))  # type: ignore[arg-type]


class _ReconcileRepository(InMemoryIDPRepository):
    def __init__(self) -> None:
        super().__init__()
        self.intents = []
        self.limits: list[int] = []

    def record_clean_intent(self, *, job) -> None:
        self.intents.append(job)

    def list_clean_intents(self, *, tenant_id: str, matter_id: str, limit: int):
        self.limits.append(limit)
        return tuple(job for job in self.intents if job.tenant_id == tenant_id and job.matter_id == matter_id)[:limit]


class _FlakyQueue:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fail_once = True

    def publish(self, *, job_id: str) -> None:
        self.calls.append(job_id)
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("synthetic unknown queue outcome")


class _MetadataDocument:
    def __init__(self, *, tenant_id: str, matter_id: str, document_id: str, source_key: str, size: int) -> None:
        self.tenant_id = tenant_id
        self.matter_id = matter_id
        self.document_id = document_id
        self.s3_key = source_key
        self.file_size_bytes = size
        self.media_type = "application/pdf"
        self.malware_scan_status = MalwareScanStatus.CLEAN
        self.status = DocumentStatus.INDEXED
        self.rag_state = "INDEXED_AND_AVAILABLE"


class _Metadata:
    def __init__(self, document: _MetadataDocument) -> None:
        self.document = document

    def get_for_scope(self, *, tenant_id: str, matter_id: str, document_id: str):
        document = self.document
        if document is None or (tenant_id, matter_id, document_id) != (document.tenant_id, document.matter_id, document.document_id):
            return None
        return document


class _S3:
    def __init__(self, *, key: str, body: bytes) -> None:
        self.key, self.body = key, body
        self.head_calls = 0
        self.get_calls = 0

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        self.head_calls += 1
        if Key != self.key:
            raise RuntimeError("object not found")
        return {"ContentLength": len(self.body), "Metadata": {"legaldesk-sha256": hashlib.sha256(self.body).hexdigest()}}

    def get_object(self, *, Bucket: str, Key: str, Range: str) -> dict[str, object]:
        self.get_calls += 1
        if Key != self.key:
            raise RuntimeError("object not found")
        return {"Body": io.BytesIO(self.body)}


class _Bedrock:
    def __init__(self, *, review_conflict: bool = False, on_extraction=None) -> None:
        self.calls: list[dict[str, object]] = []
        self.review_conflict = review_conflict
        self.on_extraction = on_extraction

    def converse(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        schema_name = kwargs["outputConfig"]["textFormat"]["structure"]["jsonSchema"]["name"]  # type: ignore[index]
        user = kwargs["messages"][0]["content"][0]["text"]  # type: ignore[index]
        pages = json.loads(user)["pages"]
        page_text = " ".join(str(item["text"]) for item in pages)
        if schema_name == "legaldesk_idp_classifier":
            quote = "OCR synthetic page" if "OCR synthetic page" in page_text else "SERVICE AGREEMENT"
            output = {"document_type": "CONTRACT", "evidence": [{"page": 1, "quote": quote}]}
        else:
            quote = "OCR synthetic page" if "OCR synthetic page" in page_text else "This Agreement is effective 31 January 2024."
            fields: dict[str, object] = {}
            if "OCR synthetic page" not in page_text:
                fields = {
                    "effective_date": {"value": "2024-01-31", "presence": "PRESENT", "evidence": [{"page": 1, "quote": quote}]},
                    "initial_duration_value": {"value": 1, "presence": "PRESENT", "evidence": [{"page": 1, "quote": "The initial term is one (1) calendar month."}]},
                    "initial_duration_unit": {"value": "month", "presence": "PRESENT", "evidence": [{"page": 1, "quote": "The initial term is one (1) calendar month."}]},
                }
                if self.review_conflict:
                    fields["explicit_expiration_date"] = {"value": "2024-01-31", "presence": "PRESENT", "evidence": [{"page": 1, "quote": "This Agreement is effective 31 January 2024."}]}
            else:
                fields = {"effective_date": {"value": "2024-02-29", "presence": "PRESENT", "evidence": [{"page": 1, "quote": quote}]}}
            # Converse's provider grammar uses the compact homogeneous array;
            # the production adapter expands it before the stable parser.  A
            # real schema has to be covered completely, including absent
            # fields, so this fake derives the rows from the server-provided
            # registry envelope rather than inventing a partial mapping.
            registry_fields = [item["name"] for item in json.loads(user)["fields"]]
            wire_fields = []
            for name in registry_fields:
                value = fields.get(name)
                if value is None:
                    wire_fields.append({"field": name, "presence": "ABSENT", "value": None, "reason": "", "evidence": []})
                else:
                    wire_fields.append({"field": name, "presence": value["presence"], "value": value["value"], "reason": "", "evidence": value["evidence"]})
            output = {"schema_version": "1.0.0", "fields": wire_fields}
            if self.on_extraction is not None:
                self.on_extraction()
        return {"output": {"message": {"content": [{"text": json.dumps(output)}]}}, "stopReason": "end_turn", "usage": {"inputTokens": 10, "outputTokens": 10}}


class _Textract:
    def __init__(self) -> None:
        self.start_calls: list[dict[str, object]] = []
        self.get_calls: list[dict[str, object]] = []
        self.job_id = "textract-runtime-job-1"
        self.failed = False

    def start_document_text_detection(self, **kwargs: object) -> dict[str, str]:
        self.start_calls.append(kwargs)
        return {"JobId": self.job_id}

    def get_document_text_detection(self, **kwargs: object) -> dict[str, object]:
        self.get_calls.append(kwargs)
        return {"JobId": self.job_id, "JobStatus": "SUCCEEDED", "DocumentMetadata": {"Pages": 1}, "Blocks": [{"BlockType": "LINE", "Page": 1, "Text": "OCR synthetic page"}]}


def _completion_event(job_id: str, status: str) -> dict[str, object]:
    return {"Records": [{
        "eventSourceARN": QUEUE_ARN,
        "body": json.dumps({"TopicArn": TOPIC_ARN, "Message": json.dumps({"JobId": job_id, "Status": status})}),
    }]}


class RuntimeContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # The source checkout keeps prompt artifacts at the repository root;
        # production packaging resolves its bundled location.  Point the
        # real provider adapters at these checked-in synthetic-test inputs.
        cls._prompt_path = _providers.default_idp_prompt_path
        _providers.default_idp_prompt_path = lambda kind: ROOT / "prompts" / f"idp-{kind}.md"

    @classmethod
    def tearDownClass(cls) -> None:
        _providers.default_idp_prompt_path = cls._prompt_path

    def _context(self, fixture: str, repository=None, *, body: bytes | None = None, review_conflict: bool = False, on_extraction=None):
        body = (FIXTURES / fixture).read_bytes() if body is None else body
        digest = hashlib.sha256(body).hexdigest()
        source_key = f"tenants/tenant-a/matters/matter-a/documents/{fixture}"
        metadata_doc = _MetadataDocument(tenant_id="tenant-a", matter_id="matter-a", document_id="document-a", source_key=source_key, size=len(body))
        metadata = _Metadata(metadata_doc)
        s3 = _S3(key=source_key, body=body)
        reader = Boto3IDPDocumentReader(metadata, s3, bucket_name="synthetic-source", max_bytes=20 * 1024 * 1024)
        repository = repository or InMemoryIDPRepository()
        document = DocumentForIDP("tenant-a", "matter-a", "document-a", "application/pdf", len(body), True, content_sha256=digest, source_key=source_key)
        job = create_verified_clean_job(repository=repository, document=document, model_id="synthetic-model", prompt_version="synthetic-prompt")
        repository.mark_delivery(job_id=job.job_id, status=IDPJobStatus.QUEUED)
        bedrock, textract, table = _Bedrock(review_conflict=review_conflict, on_extraction=on_extraction), _Textract(), _ConditionAwareTable()
        config = IDPProductionConfig("eu-west-1", "synthetic-table", "synthetic-source", "synthetic-artifacts", "synthetic-queue", "arn:queue", QUEUE_ARN, TOPIC_ARN, "arn:role", "synthetic-model", "synthetic-prompt")
        processor = ProductionIDPProcessor(repository=repository, reader=reader, artifacts=InMemoryIDPArtifactStore(), model_config=IDPModelConfig("synthetic-model"), bedrock=bedrock, textract=textract, config=config, dynamo_table=table)
        worker = IDPWorker(repository=repository, document_lookup=reader, config=IDPConfig(), expected_source_arn="arn:queue", worker_id="runtime-contract", processor=processor)
        return repository, metadata, reader, processor, worker, bedrock, textract, table, job

    def _start_scanned(self):
        context = self._context("contract-02-es-scanned-leapday.pdf")
        result = context[4].process_message({"jobId": context[-1].job_id})
        self.assertEqual(result.status, IDPJobStatus.WAITING_FOR_OCR)
        return context

    def test_digital_worker_completion_persists_run_and_preserves_rag_state(self) -> None:
        repository, metadata, _reader, _processor, worker, bedrock, _textract, _table, job = self._context("contract-01-en-digital-monthend.pdf")
        rag_before = metadata.document.rag_state
        result = worker.process_message({"jobId": job.job_id})
        self.assertEqual(result.status, IDPJobStatus.COMPLETED)
        run = repository.get_run(tenant_id="tenant-a", matter_id="matter-a", document_id="document-a", run_id=job.job_id)
        self.assertIsNotNone(run)
        self.assertEqual(run.status, IDPJobStatus.COMPLETED)  # type: ignore[union-attr]
        self.assertEqual(len(bedrock.calls), 2)
        self.assertEqual(metadata.document.rag_state, rag_before)
        duplicate = worker.process_message({"jobId": job.job_id})
        self.assertEqual(duplicate.status, IDPJobStatus.COMPLETED)
        self.assertEqual(len(bedrock.calls), 2)

    def test_old_queued_job_uses_current_invocation_deadline(self) -> None:
        repository, _metadata, _reader, _processor, worker, bedrock, _textract, _table, job = self._context(
            "contract-01-en-digital-monthend.pdf"
        )
        current = repository.get_job(job.job_id)
        repository._jobs[job.job_id] = replace(  # type: ignore[attr-defined]
            current, created_at=datetime.now(timezone.utc) - timedelta(hours=1)  # type: ignore[arg-type]
        )
        result = worker.process_message({"jobId": job.job_id})
        self.assertEqual(result.status, IDPJobStatus.COMPLETED)
        self.assertEqual(len(bedrock.calls), 2)

    def test_ocr_continuation_ten_minutes_after_job_creation_gets_fresh_budget(self) -> None:
        repository, _metadata, _reader, processor, worker, bedrock, textract, table, job = self._context(
            "contract-02-es-scanned-leapday.pdf"
        )
        current = repository.get_job(job.job_id)
        repository._jobs[job.job_id] = replace(  # type: ignore[attr-defined]
            current, created_at=datetime.now(timezone.utc) - timedelta(minutes=10)  # type: ignore[arg-type]
        )
        waiting = worker.process_message({"jobId": job.job_id})
        self.assertEqual(waiting.status, IDPJobStatus.WAITING_FOR_OCR)
        store = Boto3DynamoOCRJobStore(table, tenant_id="tenant-a", matter_id="matter-a")
        record = store.get_by_run(job.job_id)
        completion = OCRCoordinator(
            Boto3TextractProvider(textract, notification_role_arn="arn:role", notification_topic_arn=TOPIC_ARN),
            store,
        ).accept_completion(_completion_event(record.textract_job_id, "SUCCEEDED"), source_arn=QUEUE_ARN)  # type: ignore[union-attr]
        completed = processor.continue_ocr(completion)
        self.assertEqual(completed.status, IDPJobStatus.COMPLETED)  # type: ignore[union-attr]
        self.assertEqual(len(bedrock.calls), 2)

    def test_review_required_terminal_duplicate_does_not_call_providers(self) -> None:
        _repository, _metadata, _reader, _processor, worker, bedrock, _textract, _table, job = self._context(
            "contract-01-en-digital-monthend.pdf", review_conflict=True
        )
        first = worker.process_message({"jobId": job.job_id})
        self.assertEqual(first.status, IDPJobStatus.REVIEW_REQUIRED)
        self.assertEqual(len(bedrock.calls), 2)
        duplicate = worker.process_message({"jobId": job.job_id})
        self.assertEqual(duplicate.status, IDPJobStatus.REVIEW_REQUIRED)
        self.assertEqual(len(bedrock.calls), 2)

    def test_source_mutation_after_paid_extraction_never_promotes_run(self) -> None:
        for mutation in ("content", "deletion"):
            with self.subTest(mutation=mutation):
                state: dict[str, object] = {}

                def mutate() -> None:
                    metadata = state["metadata"]
                    if mutation == "content":
                        reader = state["reader"]
                        reader.s3.body = b"Z" * len(reader.s3.body)
                    else:
                        metadata.document = None

                repository, metadata, reader, _processor, worker, _bedrock, _textract, _table, job = self._context(
                    "contract-01-en-digital-monthend.pdf", on_extraction=mutate
                )
                state.update(metadata=metadata, reader=reader)
                rag_before = metadata.document.rag_state
                try:
                    result = worker.process_message({"jobId": job.job_id})
                except IDPContractError:
                    result = None
                if result is not None:
                    self.assertEqual(result.status, IDPJobStatus.FAILED)
                self.assertIsNone(repository.get_run(tenant_id="tenant-a", matter_id="matter-a", document_id="document-a", run_id=job.job_id))
                self.assertEqual(metadata.document.rag_state if metadata.document is not None else rag_before, rag_before)

    def test_corrupt_pdf_ends_failed_without_rag_mutation(self) -> None:
        corrupt = b"synthetic bytes that are not a PDF"
        repository, metadata, _reader, _processor, worker, _bedrock, _textract, _table, job = self._context(
            "contract-01-en-digital-monthend.pdf", body=corrupt
        )
        rag_before = metadata.document.rag_state
        result = worker.process_message({"jobId": job.job_id})
        self.assertEqual(result.status, IDPJobStatus.FAILED)
        self.assertIsNone(repository.get_run(tenant_id="tenant-a", matter_id="matter-a", document_id="document-a", run_id=job.job_id))
        self.assertEqual(metadata.document.rag_state, rag_before)

    def test_prompt_bundle_change_fails_old_queued_job_without_provider_call(self) -> None:
        repository, _metadata, _reader, processor, worker, bedrock, _textract, _table, job = self._context(
            "contract-01-en-digital-monthend.pdf"
        )
        processor.config = replace(processor.config, prompt_version="different-prompt-bundle")
        result = worker.process_message({"jobId": job.job_id})
        self.assertEqual(result.status, IDPJobStatus.FAILED)
        self.assertEqual(len(bedrock.calls), 0)
        self.assertIsNone(repository.get_run(tenant_id="tenant-a", matter_id="matter-a", document_id="document-a", run_id=job.job_id))

    def test_bounded_reconcile_creates_missing_job_and_recovers_ambiguous_dispatch(self) -> None:
        repository = _ReconcileRepository()
        queue = _FlakyQueue()
        trigger = VerifiedCleanIDPTrigger(repository=repository, queue=queue, config=IDPConfig(), enabled=True, model_id="synthetic-model", prompt_version="synthetic-prompt")
        document = DocumentForIDP("tenant-a", "matter-a", "document-a", "application/pdf", 10, True, content_sha256="a" * 64)
        intent = build_verified_clean_job(document=document, model_id="synthetic-model", prompt_version="synthetic-prompt")
        repository.record_clean_intent(job=intent)
        with self.assertRaises(IDPDeliveryAmbiguous):
            trigger.recover_delivery(tenant_id="tenant-a", matter_id="matter-a", limit=1)
        self.assertEqual(repository.get_job(intent.job_id).status, IDPJobStatus.DELIVERY_AMBIGUOUS)  # type: ignore[union-attr]
        recovered = trigger.recover_delivery(tenant_id="tenant-a", matter_id="matter-a", limit=1)
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].status, IDPJobStatus.QUEUED)
        self.assertEqual(repository.get_job(intent.job_id).status, IDPJobStatus.QUEUED)  # type: ignore[union-attr]
        self.assertEqual(len(queue.calls), 2)
        self.assertEqual(repository.limits, [1, 1])

    def test_scanned_start_uses_trusted_callback_continuation_and_persists_run(self) -> None:
        repository, metadata, _reader, processor, _worker, bedrock, textract, table, job = self._start_scanned()
        self.assertEqual(len(bedrock.calls), 0)
        self.assertEqual(len(textract.start_calls), 1)
        store = Boto3DynamoOCRJobStore(table, tenant_id="tenant-a", matter_id="matter-a")
        record = store.get_by_run(job.job_id)
        self.assertIsNotNone(record)
        coordinator = OCRCoordinator(Boto3TextractProvider(textract, notification_role_arn="arn:role", notification_topic_arn=TOPIC_ARN), store)
        completion = coordinator.accept_completion(_completion_event(record.textract_job_id, "SUCCEEDED"), source_arn=QUEUE_ARN)  # type: ignore[union-attr]
        completed = processor.continue_ocr(completion)
        self.assertIsNotNone(completed)
        self.assertEqual(completed.status, IDPJobStatus.COMPLETED)  # type: ignore[union-attr]
        self.assertIsNotNone(repository.get_run(tenant_id="tenant-a", matter_id="matter-a", document_id="document-a", run_id=job.job_id))
        self.assertEqual(len(textract.get_calls), 1)
        self.assertEqual(len(bedrock.calls), 2)
        self.assertEqual(metadata.document.rag_state, "INDEXED_AND_AVAILABLE")

    def test_failed_ocr_is_terminal_without_model_or_rag_mutation(self) -> None:
        _repository, metadata, reader, processor, _worker, bedrock, _textract, table, job = self._start_scanned()
        store = Boto3DynamoOCRJobStore(table, tenant_id="tenant-a", matter_id="matter-a")
        record = store.get_by_run(job.job_id)
        coordinator = OCRCoordinator(Boto3TextractProvider(_Textract(), notification_role_arn="arn:role", notification_topic_arn=TOPIC_ARN), store)
        completion = coordinator.accept_completion(_completion_event(record.textract_job_id, "FAILED"), source_arn=QUEUE_ARN)  # type: ignore[union-attr]
        failed = processor.fail_ocr(completion)
        self.assertIsNotNone(failed)
        self.assertEqual(failed.status, IDPJobStatus.FAILED)  # type: ignore[union-attr]
        self.assertEqual(len(bedrock.calls), 0)
        self.assertEqual(metadata.document.rag_state, "INDEXED_AND_AVAILABLE")

    def test_real_ocr_lambda_duplicate_completion_does_not_reprocess(self) -> None:
        repository, _metadata, _reader, processor, _worker, bedrock, textract, table, job = self._start_scanned()
        store = Boto3DynamoOCRJobStore(table, tenant_id="tenant-a", matter_id="matter-a")
        record = store.get_by_run(job.job_id)
        coordinator = OCRCoordinator(Boto3TextractProvider(textract, notification_role_arn="arn:role", notification_topic_arn=TOPIC_ARN), store)
        event = _completion_event(record.textract_job_id, "SUCCEEDED")  # type: ignore[union-attr]
        with mock.patch.dict(os.environ, {"LEGALDESK_IDP_ENABLED": "true"}):
            first = lambda_handler(event, None, coordinator=coordinator, processor=processor)
            second = lambda_handler(event, None, coordinator=coordinator, processor=processor)
        self.assertEqual(first["status"], "SUCCEEDED")
        self.assertEqual(second["status"], "SUCCEEDED")
        self.assertEqual(repository.get_job(job.job_id).status, IDPJobStatus.COMPLETED)  # type: ignore[union-attr]
        self.assertEqual(len(textract.get_calls), 1)
        self.assertEqual(len(bedrock.calls), 2)

    def test_real_ocr_lambda_failed_and_partial_success_are_terminal(self) -> None:
        for callback_status in ("FAILED", "PARTIAL_SUCCESS"):
            with self.subTest(callback_status=callback_status):
                repository, _metadata, _reader, processor, _worker, bedrock, textract, table, job = self._start_scanned()
                store = Boto3DynamoOCRJobStore(table, tenant_id="tenant-a", matter_id="matter-a")
                record = store.get_by_run(job.job_id)
                coordinator = OCRCoordinator(Boto3TextractProvider(textract, notification_role_arn="arn:role", notification_topic_arn=TOPIC_ARN), store)
                with mock.patch.dict(os.environ, {"LEGALDESK_IDP_ENABLED": "true"}):
                    response = lambda_handler(_completion_event(record.textract_job_id, callback_status), None, coordinator=coordinator, processor=processor)  # type: ignore[union-attr]
                self.assertEqual(response["status"], callback_status)
                self.assertEqual(repository.get_job(job.job_id).status, IDPJobStatus.FAILED)  # type: ignore[union-attr]
                self.assertEqual(len(bedrock.calls), 0)

    def test_boto_run_serializes_fractional_money_as_decimal(self) -> None:
        table = _ConditionAwareTable()
        repository = Boto3DynamoIDPRepository("synthetic-table", table=table)
        run = IDPExtractionRun(
            run_id="run-fractional-money", tenant_id="tenant-a", matter_id="matter-a", document_id="document-a",
            document_sha256="a" * 64, document_type=DocumentType.CONTRACT, schema_version="1.0.0",
            model_id="synthetic-model", prompt_version="synthetic-prompt", status=IDPJobStatus.COMPLETED,
            fields={"amount": IDPFieldResult(field="amount", value=12.50, presence=FieldPresence.PRESENT, origin=FieldOrigin.LITERAL, acceptance=FieldAcceptance.PROVISIONAL, schema_version="1.0.0")},
            created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        )
        repository.save_run(run)
        item = next(value for value in table.items.values() if value.get("entityType") == "IDPExtractionRun")
        self.assertEqual(item["fields"]["amount"]["value"], Decimal("12.5"))  # type: ignore[index]
        loaded = repository.get_run(tenant_id="tenant-a", matter_id="matter-a", document_id="document-a", run_id=run.run_id)
        self.assertEqual(loaded.fields["amount"].value, Decimal("12.5"))  # type: ignore[union-attr]

    def test_replay_after_save_run_crash_reuses_committed_stages_and_created_at(self) -> None:
        repository = _CrashAfterPersistRepository()
        repository, _metadata, reader, processor, worker, bedrock, textract, table, job = self._context(
            "contract-01-en-digital-monthend.pdf", repository=repository
        )
        with self.assertRaisesRegex(RuntimeError, "after durable run save"):
            worker.process_message({"jobId": job.job_id})
        saved = repository.get_run(tenant_id="tenant-a", matter_id="matter-a", document_id="document-a", run_id=job.job_id)
        self.assertIsNotNone(saved)
        calls_before_replay = len(bedrock.calls)
        repository.expire_claim(job.job_id)

        # A fresh processor reconstructs the COMMITTED classifier/extractor
        # artifacts from the durable stage ledger; it is not a terminal-job
        # early return and therefore exercises replay after a real crash gap.
        fresh_processor = ProductionIDPProcessor(
            repository=repository,
            reader=reader,
            artifacts=processor.artifacts,
            model_config=processor.model_config,
            bedrock=bedrock,
            textract=textract,
            config=processor.config,
            dynamo_table=table,
        )
        fresh_worker = IDPWorker(
            repository=repository,
            document_lookup=reader,
            config=IDPConfig(),
            expected_source_arn="arn:queue",
            worker_id="runtime-replay",
            processor=fresh_processor,
        )
        replayed = fresh_worker.process_message({"jobId": job.job_id})
        self.assertEqual(replayed.status, IDPJobStatus.COMPLETED)
        recovered = repository.get_run(tenant_id="tenant-a", matter_id="matter-a", document_id="document-a", run_id=job.job_id)
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered.created_at, saved.created_at)  # type: ignore[union-attr]
        self.assertEqual(len(bedrock.calls), calls_before_replay)

    def test_deleted_document_scope_before_ocr_continuation_is_rejected(self) -> None:
        _repository, metadata, reader, processor, _worker, bedrock, _textract, table, job = self._start_scanned()
        store = Boto3DynamoOCRJobStore(table, tenant_id="tenant-a", matter_id="matter-a")
        record = store.get_by_run(job.job_id)
        completion = OCRCoordinator(Boto3TextractProvider(_Textract(), notification_role_arn="arn:role", notification_topic_arn=TOPIC_ARN), store).accept_completion(_completion_event(record.textract_job_id, "SUCCEEDED"), source_arn=QUEUE_ARN)  # type: ignore[union-attr]
        metadata.document = None
        with self.assertRaises(IDPContractError):
            processor.continue_ocr(completion)
        self.assertEqual(len(bedrock.calls), 0)

    def test_changed_document_source_scope_before_ocr_continuation_is_rejected(self) -> None:
        _repository, metadata, reader, processor, _worker, bedrock, _textract, table, job = self._start_scanned()
        store = Boto3DynamoOCRJobStore(table, tenant_id="tenant-a", matter_id="matter-a")
        record = store.get_by_run(job.job_id)
        completion = OCRCoordinator(Boto3TextractProvider(_Textract(), notification_role_arn="arn:role", notification_topic_arn=TOPIC_ARN), store).accept_completion(_completion_event(record.textract_job_id, "SUCCEEDED"), source_arn=QUEUE_ARN)  # type: ignore[union-attr]
        metadata.document.s3_key += ".replaced"
        # Keep the replacement object readable so this isolates scope binding
        # from a provider's not-found exception.
        reader.s3.key = metadata.document.s3_key
        with self.assertRaises(IDPContractError):
            processor.continue_ocr(completion)
        self.assertEqual(len(bedrock.calls), 0)


if __name__ == "__main__":
    unittest.main()
