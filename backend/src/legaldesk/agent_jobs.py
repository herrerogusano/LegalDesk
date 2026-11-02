"""Chat jobs of the asynchronous agentic chat (ADR-021 D4).

A job is created by ``POST /api/chat`` and processed once by the SQS worker. The
record lives in the metadata table under a key made of tenant and matter, so a
read can only ever hit the matter the caller was authorized for, and the matter
delete of the data-governance runbook can remove every job of the matter with
one bounded query.

State machine (every arrow is one conditional write)::

    queued --claim--> running(attempt n) --finalize--> finalizing --complete--> succeeded
       ^                  |  \\                              \\
       +--retry_later-----+   +-----------fail--------------> failed  (also from queued/finalizing)

* ``claim`` is the idempotency gate: a duplicate SQS delivery during a live
  attempt is ignored, a dead attempt is taken over only while attempts and total
  time remain.
* ``finalize`` is the point of no return. Citation handles and conversation
  memory are written only after it, so they are written at most once per job;
  an error after it is permanent and never retried.
* ``fail`` and the expiry check are the only writers of ``failed``; the caller
  that wins the transition is the one that gives the quota back.

The record keeps the question and a *derived reference* to the server-side session
(never the cookie value) only while the job is not terminal; both are removed on
success and on failure.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Callable, Mapping, Protocol

JOB_TTL_SECONDS = 60 * 60
JOB_TOTAL_DEADLINE_SECONDS = 5 * 60
MAX_ATTEMPTS = 2  # first try plus at most one retry
# A live attempt owns the job this long: the worker Lambda timeout (150 s) plus margin.
ATTEMPT_WINDOW_SECONDS = 165
FINALIZE_WINDOW_SECONDS = 60
RETRY_VISIBILITY_SECONDS = 30
MIN_SESSION_REMAINING_SECONDS = 5 * 60
MAX_JOBS_PER_MATTER_DELETE = 100
MAX_RESULT_BYTES = 200 * 1024

JOB_ENTITY = "P15ChatJob"
JOB_KEY_PREFIX = "LEGALDESK#P15#CHATJOB#"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    FINALIZING = "finalizing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


TERMINAL = frozenset({JobStatus.SUCCEEDED, JobStatus.FAILED})
ACTIVE = frozenset({JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.FINALIZING})


class FailureCode(StrEnum):
    """Closed, content-free reasons; also what the browser is told."""

    SESSION_CLOSED = "session_closed"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    TRANSIENT_EXHAUSTED = "transient_exhausted"
    ATTEMPTS_EXHAUSTED = "attempts_exhausted"
    AGENT_FAILED = "agent_failed"
    INTERNAL_ERROR = "internal_error"


class ClaimOutcome(StrEnum):
    STARTED = "started"
    DUPLICATE = "duplicate"      # another delivery owns a live attempt: ignore the message
    TERMINAL = "terminal"        # already succeeded or failed: ignore the message
    MISSING = "missing"          # expired or deleted: ignore the message
    FAILED_NOW = "failed_now"    # this call moved the job to failed (deadline or attempts): release the quota


@dataclass(frozen=True, slots=True)
class JobKey:
    tenant_id: str
    matter_id: str
    job_id: str

    def __post_init__(self) -> None:
        for name in ("tenant_id", "matter_id", "job_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or len(value) > 128:
                raise ValueError(f"{name} is invalid")


@dataclass(frozen=True, slots=True)
class ChatJob:
    job_id: str
    subject: str
    tenant_id: str
    matter_id: str
    conversation_id: str
    session_selector: str
    correlation_id: str
    status: JobStatus
    attempt: int
    deadline: int
    created_at: int
    expires_at: int
    question: str | None = None
    session_ref: str | None = None
    attempt_deadline: int | None = None
    result: Mapping[str, object] | None = None
    failure_code: FailureCode | None = None

    @property
    def key(self) -> JobKey:
        return JobKey(self.tenant_id, self.matter_id, self.job_id)

    def public_dict(self) -> dict[str, object]:
        """Browser-facing view: no subject, tenant, question or session key."""

        body: dict[str, object] = {
            "jobId": self.job_id,
            "status": self.status.value,
            "conversationId": self.conversation_id,
            "correlationId": self.correlation_id,
        }
        if self.status is JobStatus.SUCCEEDED and self.result is not None:
            body["result"] = dict(self.result)
        if self.status is JobStatus.FAILED:
            body["error"] = (self.failure_code or FailureCode.INTERNAL_ERROR).value
        return body


def new_job(
    *, job_id: str, subject: str, tenant_id: str, matter_id: str, conversation_id: str, session_selector: str,
    correlation_id: str, question: str, session_ref: str, now: float,
) -> ChatJob:
    created = int(now)
    return ChatJob(
        job_id=job_id, subject=subject, tenant_id=tenant_id, matter_id=matter_id,
        conversation_id=conversation_id, session_selector=session_selector, correlation_id=correlation_id,
        status=JobStatus.QUEUED, attempt=0, deadline=created + JOB_TOTAL_DEADLINE_SECONDS, created_at=created,
        expires_at=created + JOB_TTL_SECONDS, question=question, session_ref=session_ref,
    )


@dataclass(frozen=True, slots=True)
class JobDeletion:
    """One bounded deletion batch. ``complete`` is false while jobs of the matter remain.

    A partial delete is never reported as complete: the operator repeats the same call
    until ``complete`` is true.
    """

    removed: int
    complete: bool


class ChatJobStore(Protocol):
    def create(self, job: ChatJob) -> None: ...

    def get(self, key: JobKey) -> ChatJob | None: ...

    def claim(self, key: JobKey, *, now: float) -> tuple[ClaimOutcome, ChatJob | None]: ...

    def finalize(self, key: JobKey, *, attempt: int, now: float) -> bool: ...

    def complete(self, key: JobKey, *, attempt: int, result: Mapping[str, object]) -> bool: ...

    def retry_later(self, key: JobKey, *, attempt: int) -> bool: ...

    def fail(self, key: JobKey, code: FailureCode) -> bool: ...

    def expire_if_due(self, key: JobKey, *, now: float) -> tuple[ChatJob | None, bool]: ...

    def delete_for_matter(self, *, tenant_id: str, matter_id: str, limit: int) -> JobDeletion: ...


def _encode_result(result: Mapping[str, object]) -> str:
    text = json.dumps(result, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    if len(text.encode("utf-8")) > MAX_RESULT_BYTES:
        raise ValueError("job result is too large")
    return text


def _due_failure(job: ChatJob, now: float) -> FailureCode | None:
    """Why an active job must be failed right now, if it must (pure)."""

    if job.status not in ACTIVE:
        return None
    if job.deadline <= now:
        return FailureCode.DEADLINE_EXCEEDED
    stale = job.attempt_deadline is not None and job.attempt_deadline <= now
    if job.status is JobStatus.FINALIZING and stale:
        return FailureCode.INTERNAL_ERROR  # side effects may be half done: never retried
    if job.status is JobStatus.RUNNING and stale and job.attempt >= MAX_ATTEMPTS:
        return FailureCode.ATTEMPTS_EXHAUSTED
    return None


class InMemoryChatJobStore:
    """Process-local store with the exact semantics of the DynamoDB one (tests, loopback)."""

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        self._jobs: dict[JobKey, ChatJob] = {}
        self._lock = threading.Lock()

    def _live(self, key: JobKey) -> ChatJob | None:
        job = self._jobs.get(key)
        if job is not None and job.expires_at <= self.clock():
            del self._jobs[key]
            return None
        return job

    def create(self, job: ChatJob) -> None:
        with self._lock:
            if job.key in self._jobs:
                raise ValueError("job already exists")
            self._jobs[job.key] = job

    def get(self, key: JobKey) -> ChatJob | None:
        with self._lock:
            return self._live(key)

    def _fail_locked(self, key: JobKey, code: FailureCode) -> bool:
        job = self._live(key)
        if job is None or job.status not in ACTIVE:
            return False
        self._jobs[key] = replace(job, status=JobStatus.FAILED, failure_code=code, question=None,
                                  session_ref=None, attempt_deadline=None)
        return True

    def claim(self, key: JobKey, *, now: float) -> tuple[ClaimOutcome, ChatJob | None]:
        with self._lock:
            job = self._live(key)
            if job is None:
                return ClaimOutcome.MISSING, None
            if job.status in TERMINAL:
                return ClaimOutcome.TERMINAL, job
            due = _due_failure(job, now)
            if due is not None:
                return (ClaimOutcome.FAILED_NOW if self._fail_locked(key, due) else ClaimOutcome.TERMINAL), self._jobs[key]
            stale = job.attempt_deadline is not None and job.attempt_deadline <= now
            takeable = job.status is JobStatus.QUEUED or (job.status is JobStatus.RUNNING and stale)
            if not takeable:
                return ClaimOutcome.DUPLICATE, job
            claimed = replace(job, status=JobStatus.RUNNING, attempt=job.attempt + 1,
                              attempt_deadline=int(now) + ATTEMPT_WINDOW_SECONDS)
            self._jobs[key] = claimed
            return ClaimOutcome.STARTED, claimed

    def finalize(self, key: JobKey, *, attempt: int, now: float) -> bool:
        with self._lock:
            job = self._live(key)
            if job is None or job.status is not JobStatus.RUNNING or job.attempt != attempt or job.deadline <= now:
                return False
            self._jobs[key] = replace(job, status=JobStatus.FINALIZING, attempt_deadline=int(now) + FINALIZE_WINDOW_SECONDS)
            return True

    def complete(self, key: JobKey, *, attempt: int, result: Mapping[str, object]) -> bool:
        encoded = _encode_result(result)
        with self._lock:
            job = self._live(key)
            if job is None or job.status is not JobStatus.FINALIZING or job.attempt != attempt:
                return False
            self._jobs[key] = replace(job, status=JobStatus.SUCCEEDED, result=json.loads(encoded), question=None,
                                      session_ref=None, attempt_deadline=None)
            return True

    def retry_later(self, key: JobKey, *, attempt: int) -> bool:
        with self._lock:
            job = self._live(key)
            if job is None or job.status is not JobStatus.RUNNING or job.attempt != attempt or attempt >= MAX_ATTEMPTS:
                return False
            self._jobs[key] = replace(job, status=JobStatus.QUEUED, attempt_deadline=None)
            return True

    def fail(self, key: JobKey, code: FailureCode) -> bool:
        with self._lock:
            return self._fail_locked(key, code)

    def expire_if_due(self, key: JobKey, *, now: float) -> tuple[ChatJob | None, bool]:
        with self._lock:
            job = self._live(key)
            if job is None:
                return None, False
            due = _due_failure(job, now)
            failed_now = due is not None and self._fail_locked(key, due)
            return self._jobs[key], failed_now

    def delete_for_matter(self, *, tenant_id: str, matter_id: str, limit: int) -> JobDeletion:
        _check_limit(limit)
        with self._lock:
            keys = sorted((key for key in self._jobs if (key.tenant_id, key.matter_id) == (tenant_id, matter_id)),
                          key=lambda key: key.job_id)
            batch = keys[:limit]
            for key in batch:
                del self._jobs[key]
            return JobDeletion(len(batch), len(keys) <= limit)


def _check_limit(limit: int) -> None:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_JOBS_PER_MATTER_DELETE:
        raise ValueError("job deletion limit is invalid")


class DynamoDBChatJobStore:
    """Jobs in the metadata table; point reads and one bounded query, never a scan."""

    def __init__(self, table_name: str, *, table: Any | None = None) -> None:
        if not isinstance(table_name, str) or not table_name.strip():
            raise ValueError("table_name is required")
        self.table_name = table_name
        if table is None:
            from .aws_cache import dynamodb_table

            table = dynamodb_table(table_name)
        self.table = table

    # ---------------------------------------------------------------- keys
    @staticmethod
    def partition(tenant_id: str, matter_id: str) -> str:
        return f"{JOB_KEY_PREFIX}{tenant_id}#MATTER#{matter_id}"

    @classmethod
    def _key(cls, key: JobKey) -> dict[str, str]:
        return {"pk": cls.partition(key.tenant_id, key.matter_id), "sk": f"JOB#{key.job_id}"}

    @staticmethod
    def _conditional_failure(exc: Exception) -> bool:
        response = getattr(exc, "response", None)
        code = response.get("Error", {}).get("Code") if isinstance(response, Mapping) else None
        return code == "ConditionalCheckFailedException" or "conditional check failed" in str(exc).lower()

    # ---------------------------------------------------------- (de)serialize
    @staticmethod
    def _int(value: object) -> int | None:
        if isinstance(value, bool) or value is None:
            return None
        try:
            return int(value)  # type: ignore[call-overload]
        except (TypeError, ValueError):
            return None

    @classmethod
    def _job(cls, item: object) -> ChatJob | None:
        if not isinstance(item, Mapping) or item.get("entityType") != JOB_ENTITY:
            return None
        strings = {name: item.get(name) for name in (
            "jobId", "subject", "tenantId", "matterId", "conversationId", "sessionSelector", "correlationId")}
        numbers = {name: cls._int(item.get(name)) for name in ("attempt", "deadline", "createdAt", "expiresAt")}
        try:
            status = JobStatus(item.get("status"))
        except ValueError:
            return None
        if any(not isinstance(v, str) or not v for v in strings.values()) or any(v is None for v in numbers.values()):
            return None
        result = None
        if item.get("result") is not None:
            try:
                result = json.loads(str(item["result"]))
            except ValueError:
                return None
            if not isinstance(result, dict):
                return None
        code = item.get("failureCode")
        try:
            failure = FailureCode(code) if code is not None else None
        except ValueError:
            failure = FailureCode.INTERNAL_ERROR
        question, session_ref = item.get("question"), item.get("sessionRef")
        return ChatJob(
            job_id=strings["jobId"], subject=strings["subject"], tenant_id=strings["tenantId"],  # type: ignore[arg-type]
            matter_id=strings["matterId"], conversation_id=strings["conversationId"],  # type: ignore[arg-type]
            session_selector=strings["sessionSelector"], correlation_id=strings["correlationId"],  # type: ignore[arg-type]
            status=status, attempt=numbers["attempt"], deadline=numbers["deadline"],  # type: ignore[arg-type]
            created_at=numbers["createdAt"], expires_at=numbers["expiresAt"],  # type: ignore[arg-type]
            question=question if isinstance(question, str) else None,
            session_ref=session_ref if isinstance(session_ref, str) else None,
            attempt_deadline=cls._int(item.get("attemptDeadline")), result=result, failure_code=failure,
        )

    def _write(self, key: JobKey, *, update: str, condition: str, names: dict[str, str], values: dict[str, object]) -> ChatJob | None:
        """One conditional update; ``None`` means the condition did not hold."""

        names = {**names, "#entity": "entityType"}
        values = {**values, ":entity": JOB_ENTITY}
        try:
            response = self.table.update_item(
                Key=self._key(key), UpdateExpression=update,
                ConditionExpression=f"#entity = :entity AND ({condition})",
                ExpressionAttributeNames=names, ExpressionAttributeValues=values, ReturnValues="ALL_NEW",
            )
        except Exception as exc:
            if self._conditional_failure(exc):
                return None
            raise
        job = self._job(response.get("Attributes")) if isinstance(response, Mapping) else None
        if job is None:
            raise RuntimeError("job update returned no record")
        return job

    # --------------------------------------------------------------- store
    def create(self, job: ChatJob) -> None:
        item: dict[str, object] = {
            **self._key(job.key), "entityType": JOB_ENTITY, "jobId": job.job_id, "subject": job.subject,
            "tenantId": job.tenant_id, "matterId": job.matter_id, "conversationId": job.conversation_id,
            "sessionSelector": job.session_selector, "correlationId": job.correlation_id,
            "status": job.status.value, "attempt": job.attempt, "deadline": job.deadline,
            "createdAt": job.created_at, "expiresAt": job.expires_at, "ttl": job.expires_at,
        }
        if job.question is not None:
            item["question"] = job.question
        if job.session_ref is not None:
            item["sessionRef"] = job.session_ref
        self.table.put_item(Item=item, ConditionExpression="attribute_not_exists(pk)")

    def get(self, key: JobKey, *, now: float | None = None) -> ChatJob | None:
        response = self.table.get_item(Key=self._key(key), ConsistentRead=True)
        job = self._job(response.get("Item") if isinstance(response, Mapping) else None)
        # TTL deletion is lazy: an expired record must never be served.
        if job is None or job.expires_at <= (time.time() if now is None else now):
            return None
        return job

    _TERMINAL_CLEANUP = "REMOVE #question, #sessionRef, #attemptDeadline"

    def fail(self, key: JobKey, code: FailureCode) -> bool:
        return self._write(
            key,
            update=f"SET #status = :failed, #failureCode = :code {self._TERMINAL_CLEANUP}",
            condition="#status IN (:queued, :running, :finalizing)",
            names={"#status": "status", "#failureCode": "failureCode", "#question": "question",
                   "#sessionRef": "sessionRef", "#attemptDeadline": "attemptDeadline"},
            values={":failed": JobStatus.FAILED.value, ":code": code.value, ":queued": JobStatus.QUEUED.value,
                    ":running": JobStatus.RUNNING.value, ":finalizing": JobStatus.FINALIZING.value},
        ) is not None

    def claim(self, key: JobKey, *, now: float) -> tuple[ClaimOutcome, ChatJob | None]:
        moment = int(now)
        common_names = {"#status": "status", "#attempt": "attempt", "#attemptDeadline": "attemptDeadline", "#deadline": "deadline"}
        common_values = {":running": JobStatus.RUNNING.value, ":now": moment, ":max": MAX_ATTEMPTS, ":one": 1,
                         ":until": moment + ATTEMPT_WINDOW_SECONDS}
        update = "SET #status = :running, #attempt = #attempt + :one, #attemptDeadline = :until"
        claimed = self._write(
            key, update=update,
            condition="#status = :queued AND #deadline > :now AND #attempt < :max",
            names=common_names, values={**common_values, ":queued": JobStatus.QUEUED.value},
        ) or self._write(
            key, update=update,
            condition="#status = :running AND #attemptDeadline <= :now AND #deadline > :now AND #attempt < :max",
            names=common_names, values=common_values,
        )
        if claimed is not None:
            return ClaimOutcome.STARTED, claimed
        job = self.get(key, now=now)
        if job is None:
            return ClaimOutcome.MISSING, None
        if job.status in TERMINAL:
            return ClaimOutcome.TERMINAL, job
        due = _due_failure(job, now)
        if due is not None:
            failed_now = self.fail(key, due)
            return (ClaimOutcome.FAILED_NOW if failed_now else ClaimOutcome.TERMINAL), self.get(key, now=now)
        return ClaimOutcome.DUPLICATE, job

    def finalize(self, key: JobKey, *, attempt: int, now: float) -> bool:
        moment = int(now)
        return self._write(
            key,
            update="SET #status = :finalizing, #attemptDeadline = :until",
            condition="#status = :running AND #attempt = :attempt AND #deadline > :now",
            names={"#status": "status", "#attempt": "attempt", "#attemptDeadline": "attemptDeadline", "#deadline": "deadline"},
            values={":finalizing": JobStatus.FINALIZING.value, ":running": JobStatus.RUNNING.value, ":attempt": attempt,
                    ":now": moment, ":until": moment + FINALIZE_WINDOW_SECONDS},
        ) is not None

    def complete(self, key: JobKey, *, attempt: int, result: Mapping[str, object]) -> bool:
        return self._write(
            key,
            update=f"SET #status = :succeeded, #result = :result {self._TERMINAL_CLEANUP}",
            condition="#status = :finalizing AND #attempt = :attempt",
            names={"#status": "status", "#attempt": "attempt", "#result": "result", "#question": "question",
                   "#sessionRef": "sessionRef", "#attemptDeadline": "attemptDeadline"},
            values={":succeeded": JobStatus.SUCCEEDED.value, ":finalizing": JobStatus.FINALIZING.value,
                    ":attempt": attempt, ":result": _encode_result(result)},
        ) is not None

    def retry_later(self, key: JobKey, *, attempt: int) -> bool:
        return self._write(
            key,
            update="SET #status = :queued REMOVE #attemptDeadline",
            condition="#status = :running AND #attempt = :attempt AND #attempt < :max",
            names={"#status": "status", "#attempt": "attempt", "#attemptDeadline": "attemptDeadline"},
            values={":queued": JobStatus.QUEUED.value, ":running": JobStatus.RUNNING.value, ":attempt": attempt,
                    ":max": MAX_ATTEMPTS},
        ) is not None

    def expire_if_due(self, key: JobKey, *, now: float) -> tuple[ChatJob | None, bool]:
        job = self.get(key, now=now)
        if job is None:
            return None, False
        due = _due_failure(job, now)
        if due is None:
            return job, False
        failed_now = self.fail(key, due)
        return self.get(key, now=now), failed_now

    def delete_for_matter(self, *, tenant_id: str, matter_id: str, limit: int) -> JobDeletion:
        """Delete at most ``limit`` jobs of the matter and say whether any remain.

        A page ends at 1 MB of *evaluated* data (a succeeded job can hold ~200 KB), so a
        short page proves nothing: pages are followed until ``limit + 1`` keys are seen
        (more remain) or ``LastEvaluatedKey`` is absent (that was all of them). A matter
        with more than ``limit`` jobs is therefore removed over several calls, each
        bounded, and only the call that leaves nothing behind reports ``complete``.
        """

        _check_limit(limit)
        from boto3.dynamodb.conditions import Key

        keys: list[dict[str, str]] = []
        start: dict[str, object] | None = None
        while True:
            request: dict[str, Any] = {
                "KeyConditionExpression": Key("pk").eq(self.partition(tenant_id, matter_id)) & Key("sk").begins_with("JOB#"),
                "ProjectionExpression": "#pk, #sk",
                "ExpressionAttributeNames": {"#pk": "pk", "#sk": "sk"},
                "ConsistentRead": True,
            }
            if start is not None:
                request["ExclusiveStartKey"] = start
            response = self.table.query(**request)
            if not isinstance(response, Mapping):
                raise RuntimeError("job query returned an invalid response")
            for item in response.get("Items", ()):
                if isinstance(item, Mapping) and isinstance(item.get("pk"), str) and isinstance(item.get("sk"), str):
                    keys.append({"pk": item["pk"], "sk": item["sk"]})
            start = response.get("LastEvaluatedKey")
            if len(keys) > limit or not start:
                break
        batch = keys[:limit]
        for key in batch:
            self.table.delete_item(Key=key)
        return JobDeletion(len(batch), len(keys) <= limit)


__all__ = [
    "ACTIVE", "ATTEMPT_WINDOW_SECONDS", "ChatJob", "ChatJobStore", "ClaimOutcome", "DynamoDBChatJobStore",
    "FailureCode", "InMemoryChatJobStore", "JOB_TOTAL_DEADLINE_SECONDS", "JOB_TTL_SECONDS", "JobDeletion", "JobKey", "JobStatus",
    "MAX_ATTEMPTS", "MIN_SESSION_REMAINING_SECONDS", "RETRY_VISIBILITY_SECONDS", "TERMINAL", "new_job",
]
