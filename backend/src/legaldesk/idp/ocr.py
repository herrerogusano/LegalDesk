"""Asynchronous OCR continuation contracts.

Only the narrow lifecycle is defined here.  A production adapter may call
Textract, but this module never polls or performs an AWS call itself.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Mapping, Protocol

from .models import IDPContractError


class OCRStatus(StrEnum):
    WAITING_FOR_OCR = "WAITING_FOR_OCR"
    SUCCEEDED = "SUCCEEDED"
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
    FAILED = "FAILED"


class OCRContractError(IDPContractError):
    pass


@dataclass(frozen=True, slots=True)
class OCRStartRequest:
    run_id: str
    document_sha256: str
    source_bucket: str
    source_key: str
    client_request_token: str
    feature_version: str = "TEXT_DETECTION_V1"
    expected_page_count: int | None = None
    expected_sqs_source_arn: str | None = None
    expected_sns_topic_arn: str | None = None
    # The canonical document key is retained separately from source_key,
    # which is the immutable IDP snapshot sent to Textract.  Continuation
    # must re-authorize the canonical metadata against this value.
    canonical_source_key: str | None = None

    def __post_init__(self) -> None:
        for name in ("run_id", "document_sha256", "source_bucket", "source_key", "client_request_token", "feature_version"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise OCRContractError(f"{name} is required")
        if re.fullmatch(r"[0-9a-fA-F]{64}", self.document_sha256) is None:
            raise OCRContractError("document_sha256 is invalid")
        if len(self.client_request_token) > 64:
            raise OCRContractError("OCR client request token is too long")
        if self.expected_page_count is not None and (isinstance(self.expected_page_count, bool) or not isinstance(self.expected_page_count, int) or not 1 <= self.expected_page_count <= 1000):
            raise OCRContractError("expected_page_count is invalid")
        for name in ("expected_sqs_source_arn", "expected_sns_topic_arn"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise OCRContractError(f"{name} is invalid")
        if self.canonical_source_key is not None and (not isinstance(self.canonical_source_key, str) or not self.canonical_source_key.strip()):
            raise OCRContractError("canonical_source_key is invalid")


@dataclass(frozen=True, slots=True)
class OCRJobRecord:
    run_id: str
    document_sha256: str
    client_request_token: str
    textract_job_id: str
    status: OCRStatus = OCRStatus.WAITING_FOR_OCR
    expected_sqs_source_arn: str | None = None
    expected_sns_topic_arn: str | None = None
    expected_page_count: int | None = None
    next_token: str | None = None
    canonical_source_key: str | None = None

    @property
    def expected_source_arn(self) -> str | None:
        """Compatibility view; new code distinguishes SQS and SNS sources."""

        return self.expected_sqs_source_arn


def ocr_client_request_token(*, run_id: str, document_sha256: str) -> str:
    """Stable, bounded token preventing duplicate paid Start calls."""

    return hashlib.sha256(f"{run_id}:{document_sha256}".encode("ascii")).hexdigest()[:64]


class OCRProvider(Protocol):
    def start_document_text_detection(self, request: OCRStartRequest) -> str: ...

    def get_document_text_detection(self, *, textract_job_id: str, next_token: str | None = None) -> Mapping[str, Any]: ...


class Boto3TextractProvider:
    """Small async Textract adapter; retries are disabled by its composition."""

    def __init__(self, client: Any, *, notification_role_arn: str, notification_topic_arn: str) -> None:
        if client is None or not all(isinstance(value, str) and value.strip() for value in (notification_role_arn, notification_topic_arn)):
            raise OCRContractError("Textract configuration is invalid")
        self.client = client
        self.notification_role_arn = notification_role_arn
        self.notification_topic_arn = notification_topic_arn

    def start_document_text_detection(self, request: OCRStartRequest) -> str:
        if request.expected_sns_topic_arn != self.notification_topic_arn:
            raise OCRContractError("Textract notification topic is not server-configured")
        response = self.client.start_document_text_detection(
            DocumentLocation={"S3Object": {"Bucket": request.source_bucket, "Name": request.source_key}},
            ClientRequestToken=request.client_request_token,
            JobTag=request.run_id[:64],
            NotificationChannel={"SNSTopicArn": self.notification_topic_arn, "RoleArn": self.notification_role_arn},
        )
        job_id = response.get("JobId") if isinstance(response, Mapping) else None
        if not isinstance(job_id, str) or not job_id.strip():
            raise OCRContractError("Textract returned no JobId")
        return job_id

    def get_document_text_detection(self, *, textract_job_id: str, next_token: str | None = None) -> Mapping[str, Any]:
        kwargs: dict[str, Any] = {"JobId": textract_job_id, "MaxResults": 1000}
        if next_token is not None:
            kwargs["NextToken"] = next_token
        response = self.client.get_document_text_detection(**kwargs)
        if not isinstance(response, Mapping):
            raise OCRContractError("Textract response is invalid")
        return response


class OCRJobStore(Protocol):
    def get_by_run(self, run_id: str) -> OCRJobRecord | None: ...

    def get_by_textract_job_id(self, textract_job_id: str) -> OCRJobRecord | None: ...

    def put(self, record: OCRJobRecord) -> None: ...


class InMemoryOCRJobStore:
    def __init__(self) -> None:
        self.records: dict[str, OCRJobRecord] = {}

    def get_by_run(self, run_id: str) -> OCRJobRecord | None:
        return self.records.get(run_id)

    def get_by_textract_job_id(self, textract_job_id: str) -> OCRJobRecord | None:
        return next((record for record in self.records.values() if record.textract_job_id == textract_job_id), None)

    def put(self, record: OCRJobRecord) -> None:
        old = self.records.get(record.run_id)
        if old is not None and (old.document_sha256 != record.document_sha256 or old.client_request_token != record.client_request_token):
            raise OCRContractError("OCR run identity cannot be changed")
        if old is not None and old.status in {OCRStatus.SUCCEEDED, OCRStatus.PARTIAL_SUCCESS, OCRStatus.FAILED}:
            if record.status is not old.status:
                raise OCRContractError("terminal OCR state cannot regress")
            return
        if old is not None and old.textract_job_id != "PENDING" and record.textract_job_id == "PENDING":
            raise OCRContractError("OCR job mapping cannot be cleared")
        self.records[record.run_id] = record


class Boto3DynamoOCRJobStore:
    """Scoped Option-B OCR mapping plus direct JobId locator; never scans."""

    def __init__(self, table: Any, *, tenant_id: str, matter_id: str, transaction_client: Any | None = None) -> None:
        if table is None or not all(isinstance(value, str) and value.strip() for value in (tenant_id, matter_id)):
            raise OCRContractError("OCR store scope is invalid")
        self.table, self.tenant_id, self.matter_id = table, tenant_id, matter_id
        self.transaction_client = transaction_client or getattr(getattr(table, "meta", None), "client", None)

    @property
    def _pk(self) -> str:
        return f"TENANT#{self.tenant_id}#MATTER#{self.matter_id}"

    def _run_key(self, run_id: str) -> dict[str, str]:
        return {"pk": self._pk, "sk": f"IDP#OCR#{run_id}"}

    def _job_key(self, job_id: str) -> dict[str, str]:
        return {"pk": f"IDP#OCRJOB#{job_id}", "sk": "LOCATOR"}

    @staticmethod
    def locate_scope(table: Any, textract_job_id: str) -> tuple[str, str] | None:
        """Resolve callback scope from the direct JobId locator, never Scan."""

        if table is None or not isinstance(textract_job_id, str) or not textract_job_id.strip():
            raise OCRContractError("OCR callback locator is invalid")
        item = table.get_item(Key={"pk": f"IDP#OCRJOB#{textract_job_id}", "sk": "LOCATOR"}, ConsistentRead=True).get("Item")
        if not isinstance(item, Mapping) or not all(isinstance(item.get(name), str) and item[name].strip() for name in ("tenantId", "matterId", "runId", "jobId")) or item.get("jobId") != textract_job_id:
            return None
        return str(item["tenantId"]), str(item["matterId"])

    def _from_item(self, item: Mapping[str, Any]) -> OCRJobRecord:
        return OCRJobRecord(
            run_id=str(item["runId"]), document_sha256=str(item["documentSha256"]), client_request_token=str(item["clientRequestToken"]),
            textract_job_id=str(item["textractJobId"]), status=OCRStatus(item["status"]),
            expected_sqs_source_arn=item.get("expectedSqsSourceArn"), expected_sns_topic_arn=item.get("expectedSnsTopicArn"),
            expected_page_count=int(item["expectedPageCount"]) if item.get("expectedPageCount") is not None else None,
            next_token=item.get("nextToken"), canonical_source_key=item.get("canonicalSourceKey"),
        )

    def get_by_run(self, run_id: str) -> OCRJobRecord | None:
        item = self.table.get_item(Key=self._run_key(run_id), ConsistentRead=True).get("Item")
        return self._from_item(item) if item else None

    def get_by_textract_job_id(self, textract_job_id: str) -> OCRJobRecord | None:
        locator = self.table.get_item(Key=self._job_key(textract_job_id), ConsistentRead=True).get("Item")
        if not locator or locator.get("tenantId") != self.tenant_id or locator.get("matterId") != self.matter_id:
            return None
        return self.get_by_run(str(locator["runId"]))

    def put(self, record: OCRJobRecord) -> None:
        item = {
            "entityType": "IDPOCRJob", "pk": self._pk, "sk": f"IDP#OCR#{record.run_id}", "runId": record.run_id,
            "tenantId": self.tenant_id, "matterId": self.matter_id, "documentSha256": record.document_sha256,
            "clientRequestToken": record.client_request_token, "textractJobId": record.textract_job_id, "status": record.status.value,
            "expectedSqsSourceArn": record.expected_sqs_source_arn, "expectedSnsTopicArn": record.expected_sns_topic_arn,
            "expectedPageCount": record.expected_page_count, "nextToken": record.next_token,
            "canonicalSourceKey": record.canonical_source_key,
        }
        item = {key: value for key, value in item.items() if value is not None}
        current = self.get_by_run(record.run_id)
        if current is not None and (current.document_sha256 != record.document_sha256 or current.client_request_token != record.client_request_token):
            raise OCRContractError("OCR run identity cannot be changed")
        if current is not None and current.status in {OCRStatus.SUCCEEDED, OCRStatus.PARTIAL_SUCCESS, OCRStatus.FAILED} and current.status is not record.status:
            raise OCRContractError("terminal OCR state cannot regress")
        names = {"#run": "runId", "#hash": "clientRequestToken", "#job": "textractJobId", "#status": "status"}
        values = {":run": record.run_id, ":hash": record.client_request_token, ":pending": "PENDING", ":waiting": OCRStatus.WAITING_FOR_OCR.value}
        if record.textract_job_id == "PENDING":
            # Repeated persist-before-Start is idempotent, but a real mapping
            # or terminal callback can never be cleared by a stale writer.
            condition = "attribute_not_exists(pk) OR (#run = :run AND #hash = :hash AND #job = :pending AND #status = :waiting)"
            self.table.put_item(Item=item, ConditionExpression=condition, ExpressionAttributeNames=names, ExpressionAttributeValues=values)
            return
        if current is not None and current.textract_job_id == record.textract_job_id:
            # Callback completion is a one-way atomic transition.  The
            # pre-read is only an optimization; this condition fences a
            # concurrent stale STARTED/PENDING or terminal rewrite.
            if current.status in {OCRStatus.SUCCEEDED, OCRStatus.PARTIAL_SUCCESS, OCRStatus.FAILED}:
                if current.status is record.status:
                    return
                raise OCRContractError("terminal OCR state cannot regress")
            try:
                update = "SET #status = :status"
                update_values = {**values, ":job_id": record.textract_job_id, ":status": record.status.value}
                if record.next_token is not None:
                    update += ", #next = :next"
                    names["#next"] = "nextToken"
                    update_values[":next"] = record.next_token
                self.table.update_item(
                    Key=self._run_key(record.run_id), UpdateExpression=update,
                    ConditionExpression="#run = :run AND #hash = :hash AND #job = :job_id AND #status = :waiting",
                    ExpressionAttributeNames=names, ExpressionAttributeValues=update_values,
                )
            except Exception as exc:
                raise OCRContractError("atomic OCR completion transition failed") from exc
            return
        locator = {"entityType": "IDPOCRJobLocator", "pk": self._job_key(record.textract_job_id)["pk"], "sk": "LOCATOR", "jobId": record.textract_job_id, "runId": record.run_id, "tenantId": self.tenant_id, "matterId": self.matter_id}
        if not callable(getattr(self.transaction_client, "transact_write_items", None)):
            raise OCRContractError("atomic OCR mapping transaction client is required")
        try:
            self.transaction_client.transact_write_items(TransactItems=[
                {"Put": {"TableName": getattr(self.table, "name", ""), "Item": item, "ConditionExpression": "#run = :run AND #hash = :hash AND #job = :pending AND #status = :waiting", "ExpressionAttributeNames": names, "ExpressionAttributeValues": values}},
                {"Put": {"TableName": getattr(self.table, "name", ""), "Item": locator, "ConditionExpression": "attribute_not_exists(pk) OR #run = :run", "ExpressionAttributeNames": {"#run": "runId"}, "ExpressionAttributeValues": {":run": record.run_id}}},
            ])
        except Exception as exc:
            raise OCRContractError("atomic OCR mapping transaction failed") from exc


@dataclass(frozen=True, slots=True)
class OCRCompletion:
    textract_job_id: str
    status: OCRStatus
    run_id: str
    next_token: str | None = None


@dataclass(frozen=True, slots=True)
class OCRPageText:
    page: int
    text: str


class OCRCoordinator:
    """Persist-before/after boundary for asynchronous Start and callbacks."""

    def __init__(self, provider: OCRProvider, store: OCRJobStore) -> None:
        self.provider = provider
        self.store = store

    def start(self, request: OCRStartRequest, *, expected_source_arn: str | None = None) -> OCRJobRecord:
        existing = self.store.get_by_run(request.run_id)
        if existing is not None:
            if existing.document_sha256 != request.document_sha256 or existing.client_request_token != request.client_request_token:
                raise OCRContractError("OCR request does not match persisted run configuration")
            if existing.textract_job_id != "PENDING":
                return existing
        expected_sqs = request.expected_sqs_source_arn or expected_source_arn
        if existing is not None and existing.canonical_source_key != request.canonical_source_key:
            raise OCRContractError("OCR canonical source identity cannot be changed")
        pending = existing or OCRJobRecord(request.run_id, request.document_sha256, request.client_request_token, "PENDING", expected_sqs_source_arn=expected_sqs, expected_sns_topic_arn=request.expected_sns_topic_arn, expected_page_count=request.expected_page_count, canonical_source_key=request.canonical_source_key)
        # Persist the expected identity/config before Start.  A crash after
        # Start is recovered with the same idempotent ClientRequestToken.
        self.store.put(pending)
        textract_job_id = self.provider.start_document_text_detection(request)
        if not isinstance(textract_job_id, str) or not textract_job_id.strip():
            raise OCRContractError("OCR provider returned an invalid job id")
        record = OCRJobRecord(request.run_id, request.document_sha256, request.client_request_token, textract_job_id, expected_sqs_source_arn=pending.expected_sqs_source_arn, expected_sns_topic_arn=pending.expected_sns_topic_arn, expected_page_count=pending.expected_page_count, canonical_source_key=pending.canonical_source_key)
        self.store.put(record)
        return record

    def accept_completion(self, event: Mapping[str, Any] | str, *, source_arn: str | None = None) -> OCRCompletion:
        payload = _decode_event(event)
        job_id = payload.get("JobId", payload.get("jobId"))
        status_raw = payload.get("Status", payload.get("status"))
        if not all(isinstance(item, str) and item.strip() for item in (job_id, status_raw)):
            raise OCRContractError("OCR callback is incomplete")
        try:
            status = OCRStatus.SUCCEEDED if status_raw.upper() == "SUCCEEDED" else OCRStatus.PARTIAL_SUCCESS if status_raw.upper() == "PARTIAL_SUCCESS" else OCRStatus.FAILED if status_raw.upper() == "FAILED" else None
        except AttributeError:
            status = None
        if status is None:
            raise OCRContractError("OCR callback status is invalid")
        record = self.store.get_by_textract_job_id(job_id)
        if record is None or record.textract_job_id != job_id:
            raise OCRContractError("OCR callback does not match a persisted run")
        outer_sqs = payload.get("_outer_sqs_source_arn")
        outer_topic = payload.get("_outer_sns_topic_arn")
        if source_arn is not None and outer_sqs != source_arn:
            raise OCRContractError("OCR callback outer SQS ARN is invalid")
        if record.expected_sqs_source_arn is None or outer_sqs != record.expected_sqs_source_arn:
            raise OCRContractError("OCR callback outer SQS ARN does not match persisted configuration")
        if record.expected_sns_topic_arn is None or outer_topic != record.expected_sns_topic_arn:
            raise OCRContractError("OCR callback SNS TopicArn does not match persisted configuration")
        if record.status in {OCRStatus.SUCCEEDED, OCRStatus.PARTIAL_SUCCESS, OCRStatus.FAILED}:
            return OCRCompletion(record.textract_job_id, record.status, record.run_id, record.next_token)
        updated = OCRJobRecord(record.run_id, record.document_sha256, record.client_request_token, record.textract_job_id, status, record.expected_sqs_source_arn, record.expected_sns_topic_arn, record.expected_page_count, payload.get("NextToken"), record.canonical_source_key)
        self.store.put(updated)
        return OCRCompletion(record.textract_job_id, status, record.run_id, updated.next_token)

    def read_detection_pages(self, record: OCRJobRecord, *, max_pages: int = 100, expected_page_count: int | None = None, max_api_calls: int = 100, max_blocks: int = 10_000, max_text_chars: int = 4_000_000, deadline_at: datetime | None = None, deadline_reserve_seconds: float = 0.0) -> tuple[OCRPageText, ...]:
        """Read a completed job's paginated LINE blocks; never polls for completion."""

        if record.status is not OCRStatus.SUCCEEDED:
            raise OCRContractError("OCR pages are available only after a completion callback")
        if not isinstance(max_pages, int) or isinstance(max_pages, bool) or not 1 <= max_pages <= 1000:
            raise OCRContractError("OCR page bound is invalid")
        expected_page_count = expected_page_count or record.expected_page_count
        if expected_page_count is None:
            raise OCRContractError("OCR page coverage count is not persisted")
        if expected_page_count > max_pages:
            raise OCRContractError("OCR page count exceeds configured bound")
        if not isinstance(max_api_calls, int) or max_api_calls < 1 or max_api_calls > 1000:
            raise OCRContractError("OCR API call bound is invalid")
        pages: dict[int, list[str]] = {}
        token: str | None = None
        seen_tokens: set[str] = set()
        block_count = 0
        text_chars = 0
        for _ in range(max_api_calls):
            if deadline_at is not None and (deadline_at - datetime.now(timezone.utc)).total_seconds() <= deadline_reserve_seconds:
                raise OCRContractError("IDP global deadline exceeded")
            response = self.provider.get_document_text_detection(textract_job_id=record.textract_job_id, next_token=token)
            if not isinstance(response, Mapping) or response.get("JobId") not in {None, record.textract_job_id}:
                raise OCRContractError("OCR page response does not match the persisted job")
            if response.get("JobStatus") != "SUCCEEDED":
                raise OCRContractError("OCR result is not complete")
            metadata = response.get("DocumentMetadata")
            if not isinstance(metadata, Mapping) or metadata.get("Pages") != expected_page_count:
                raise OCRContractError("OCR page metadata does not prove complete coverage")
            for block in response.get("Blocks", ()):
                block_count += 1
                if block_count > max_blocks:
                    raise OCRContractError("OCR block bound exceeded")
                if not isinstance(block, Mapping) or block.get("BlockType") != "LINE":
                    continue
                page = block.get("Page")
                text = block.get("Text")
                if not isinstance(page, int) or not 1 <= page <= max_pages or not isinstance(text, str) or not text:
                    raise OCRContractError("OCR response contains an invalid or out-of-range page block")
                text_chars += len(text)
                if text_chars > max_text_chars:
                    raise OCRContractError("OCR text bound exceeded")
                pages.setdefault(page, []).append(text)
            next_token = response.get("NextToken")
            if next_token is None:
                break
            if not isinstance(next_token, str) or not next_token:
                raise OCRContractError("OCR pagination token is invalid")
            if next_token in seen_tokens:
                raise OCRContractError("OCR pagination token repeated")
            seen_tokens.add(next_token)
            token = next_token
        else:
            raise OCRContractError("OCR pagination call bound exceeded")
        if set(pages) != set(range(1, expected_page_count + 1)):
            raise OCRContractError("OCR result has incomplete page coverage")
        return tuple(OCRPageText(page, " ".join(pages[page])) for page in range(1, expected_page_count + 1))


def _decode_event(event: Mapping[str, Any] | str) -> dict[str, Any]:
    if isinstance(event, str):
        try:
            event = json.loads(event)
        except json.JSONDecodeError as exc:
            raise OCRContractError("OCR callback is not JSON") from exc
    if not isinstance(event, Mapping):
        raise OCRContractError("OCR callback is invalid")
    records = event.get("Records")
    if isinstance(records, list) and len(records) == 1 and isinstance(records[0], Mapping):
        record = records[0]
        if record.get("eventSourceARN"):
            body = record.get("body")
            nested = _decode_event(body) if isinstance(body, (str, Mapping)) else {}
            nested["_outer_sqs_source_arn"] = record["eventSourceARN"]
            return nested
    # Textract notifications commonly arrive as an SNS Message inside SQS.
    message = event.get("Message")
    if isinstance(message, (str, Mapping)):
        nested = _decode_event(message) if isinstance(message, (str, Mapping)) else {}
        if event.get("TopicArn"):
            nested["_outer_sns_topic_arn"] = event["TopicArn"]
        for key in ("_outer_sqs_source_arn", "_outer_sns_topic_arn"):
            if key in event:
                nested.setdefault(key, event[key])
        return nested
    return dict(event)


__all__ = [
    "Boto3DynamoOCRJobStore",
    "Boto3TextractProvider",
    "InMemoryOCRJobStore",
    "OCRCompletion",
    "OCRCoordinator",
    "OCRContractError",
    "OCRJobRecord",
    "OCRJobStore",
    "OCRPageText",
    "OCRProvider",
    "OCRStartRequest",
    "OCRStatus",
    "ocr_client_request_token",
]
