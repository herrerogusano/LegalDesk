"""Asynchronous, read-only agentic chat: submission and worker (ADR-021 D4, D5, D7, D8).

``submit_agent_chat`` is what ``POST /api/chat`` runs: it reserves the quota
once per job, writes the job and publishes ``{tenantId, matterId, jobId}``. The
queue message carries no question, no token and no session key.

``AgentChatWorker.process`` is what the SQS Lambda runs for one message:

1. ``claim`` the job (idempotency gate; duplicates, finished and expired jobs
   are ignored);
2. load the server-side session and build the sealed ``agent_chat`` binding;
   a closed or expired session fails closed and gives the quota back;
3. run the Harness loop with the last pairs of history, validate its tool
   results and plan with ``agent_output.resolve_output`` and, when nothing is
   usable, run the direct RAG path (D5);
4. ``finalize`` (point of no return), then write citation handles and the
   conversation memory exactly once, and ``complete`` the job.

Only transient errors before ``finalize`` are retried, and at most once.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol
from uuid import uuid4

from . import agent_contracts as contracts
from .agent_citations import build_citation_handles
from .agent_integration import bind_harness_invocation
from .agent_jobs import (
    MIN_SESSION_REMAINING_SECONDS,
    RETRY_VISIBILITY_SECONDS,
    ChatJob,
    ChatJobStore,
    ClaimOutcome,
    FailureCode,
    JobKey,
    new_job,
)
from .agent_output import (
    AnswerOutput,
    LoopEvidence,
    LoopOutcome,
    OutputBlock,
    OutputPath,
    RenderedCitation,
    resolve_output,
)
from .authorization import AuthorizationDenied
from .chat import GUARDRAIL_INPUT_BLOCKED_ANSWER, TECHNICAL_ERROR_ANSWER, ChatResponse
from .conversation_history import last_pairs, load_accepted_events
from .memory import MAX_EVENT_TEXT_LENGTH
from .quota import CHATS, HARNESS, QuotaLedger

TRANSIENT_ERROR_CODES = frozenset({
    "throttlingexception", "throttledexception", "toomanyrequestsexception", "serviceunavailableexception",
    "internalserverexception", "internalfailure", "serviceexception", "modeltimeoutexception",
    "requesttimeout", "requesttimeoutexception", "provisionedthroughputexceededexception",
})
TRANSIENT_ERROR_CLASSES = frozenset({
    "readtimeouterror", "connecttimeouterror", "endpointconnectionerror", "connectionclosederror",
    "connectionerror", "timeouterror", "proxyconnectionerror",
})


# The direct RAG path (resolver, writer, Guardrails) needs about this long; with less Lambda
# time left the job fails cleanly instead of being killed half way.
MIN_FALLBACK_SECONDS = 40


class SubmissionError(RuntimeError):
    """The job could not be created or queued; the quota has been given back."""


@dataclass(frozen=True, slots=True)
class InputDecision:
    """Result of the input Guardrail, run before any quota or job exists."""

    kind: str  # allowed | blocked | error
    question: str = ""


class JobQueue(Protocol):
    def publish(self, key: JobKey) -> None: ...


@dataclass(slots=True)
class AgentChatRuntime:
    """Everything ``POST /api/chat`` needs for the agentic path; absent = feature off."""

    job_store: ChatJobStore
    queue: JobQueue
    input_check: Callable[[str, str], InputDecision]


def input_not_allowed_response(kind: str, correlation_id: str) -> ChatResponse:
    """The same envelope the synchronous chat returns when the input Guardrail stops a question."""

    if kind == "blocked":
        return ChatResponse(
            answer=GUARDRAIL_INPUT_BLOCKED_ANSWER, citations=(), evidence_status=None, disclaimer_required=True,
            prompt_version=None, prompt_sha256=None, correlation_id=correlation_id, operation_status="blocked",
        )
    return ChatResponse(
        answer=TECHNICAL_ERROR_ANSWER, citations=(), evidence_status=None, disclaimer_required=True,
        prompt_version=None, prompt_sha256=None, correlation_id=correlation_id, error_code="guardrail_error",
        operation_status="error",
    )


# ----------------------------------------------------------------- quota

def reserve_job_quota(quota: QuotaLedger, tenant_id: str, job_id: str, *, now: float) -> None:
    """One ``chats`` and one ``harness`` unit per job, keyed by the job id (D8).

    The Gateway counter of the UI buttons is deliberately not touched: the tool
    calls of the agent inside a job are part of this reservation.
    """

    quota.reserve(tenant_id, CHATS, idempotency_key=job_id, now=now)
    try:
        quota.reserve(tenant_id, HARNESS, idempotency_key=job_id, now=now)
    except Exception:
        _release(quota, tenant_id, CHATS, job_id, now)
        raise


def _release(quota: QuotaLedger, tenant_id: str, operation: str, job_id: str, created_at: float) -> bool:
    try:
        quota.release(tenant_id, operation, idempotency_key=job_id, now=created_at)
        return True
    except Exception:
        return False


def release_job_quota(quota: QuotaLedger, job: ChatJob) -> bool:
    """Give both units back; ``False`` if any release failed (reported, never raised)."""

    results = [_release(quota, job.tenant_id, operation, job.job_id, job.created_at) for operation in (CHATS, HARNESS)]
    return all(results)


# ------------------------------------------------------------ submission

def submit_agent_chat(
    runtime: AgentChatRuntime,
    quota: QuotaLedger,
    *,
    subject: str,
    tenant_id: str,
    matter_id: str,
    conversation_id: str,
    session_selector: str,
    correlation_id: str,
    question: str,
    session_ref: str,
    now: float | None = None,
) -> ChatJob:
    """Reserve quota, create the job and queue it. Scope values must be server-derived."""

    moment = time.time() if now is None else now
    job = new_job(
        job_id=str(uuid4()), subject=subject, tenant_id=tenant_id, matter_id=matter_id,
        conversation_id=conversation_id, session_selector=session_selector, correlation_id=correlation_id,
        question=question, session_ref=session_ref, now=moment,
    )
    reserve_job_quota(quota, tenant_id, job.job_id, now=moment)
    try:
        runtime.job_store.create(job)
        runtime.queue.publish(job.key)
    except Exception as exc:
        try:
            # Best effort: mark the job dead so a stray message finds nothing to do.
            runtime.job_store.fail(job.key, FailureCode.INTERNAL_ERROR)
        except Exception:
            pass
        finally:
            release_job_quota(quota, job)  # never leaves the reservation behind
        raise SubmissionError("chat job could not be queued") from exc
    return job


# ---------------------------------------------------------------- worker

class TransientAgentError(Exception):
    """A provider error worth one retry (throttling, 5xx, timeout)."""


class PermanentAgentError(Exception):
    def __init__(self, code: FailureCode) -> None:
        self.code = code
        super().__init__(code.value)


class _Superseded(Exception):
    """The job reached a terminal state while this attempt ran (deadline); drop the work."""


def is_transient_error(exc: BaseException) -> bool:
    response = getattr(exc, "response", None)
    if isinstance(response, Mapping):
        error = response.get("Error")
        code = str(error.get("Code", "")).lower() if isinstance(error, Mapping) else ""
        meta = response.get("ResponseMetadata")
        status = meta.get("HTTPStatusCode") if isinstance(meta, Mapping) else None
        if code in TRANSIENT_ERROR_CODES or (isinstance(status, int) and (status >= 500 or status == 429)):
            return True
    return type(exc).__name__.lower() in TRANSIENT_ERROR_CLASSES or isinstance(exc, (TimeoutError, ConnectionError))


@dataclass(frozen=True, slots=True)
class WorkerResult:
    action: str  # completed | failed | ignored | retry
    job_id: str
    retry_after_seconds: int = 0
    metadata: Mapping[str, object] | None = None


@dataclass(frozen=True, slots=True)
class _Computed:
    output: AnswerOutput
    operation_status: str
    meta: dict[str, object]
    memory_scope: Any


class AgentChatWorker:
    def __init__(
        self,
        *,
        job_store: ChatJobStore,
        state_store: Any,
        quota: QuotaLedger,
        authorization_store: Any,
        conversation_store: Any,
        grant_repository: Any,
        identity_verifier: Any,
        gateway_url: str,
        invoke_agent: Callable[[Any, str, tuple[tuple[str, str], ...]], Any],
        rag_chat: Callable[..., Any],
        memory: Any = None,
        clock: Callable[[], float] = time.time,
        time_left: Callable[[], float] | None = None,
    ) -> None:
        self.job_store, self.state_store, self.quota = job_store, state_store, quota
        self.authorization_store, self.conversation_store = authorization_store, conversation_store
        self.grant_repository, self.identity_verifier, self.gateway_url = grant_repository, identity_verifier, gateway_url
        self.invoke_agent, self.rag_chat, self.memory, self.clock = invoke_agent, rag_chat, memory, clock
        self.time_left = time_left  # seconds of Lambda time left; set per invocation by the handler

    # -- public --------------------------------------------------------
    def process(self, key: JobKey) -> WorkerResult:
        started = time.perf_counter()
        outcome, job = self.job_store.claim(key, now=self.clock())
        if outcome is ClaimOutcome.FAILED_NOW and job is not None:
            released = release_job_quota(self.quota, job)
            return WorkerResult("failed", key.job_id, metadata={"failure": job.failure_code.value if job.failure_code else None, "quotaReleased": released})
        if outcome is not ClaimOutcome.STARTED or job is None:
            return WorkerResult("ignored", key.job_id, metadata={"claim": outcome.value})
        attempt, finalized = job.attempt, False
        try:
            computed = self._compute(job)
            if not self.job_store.finalize(key, attempt=attempt, now=self.clock()):
                raise _Superseded()
            finalized = True
            result = self._publish(job, computed)
            if not self.job_store.complete(key, attempt=attempt, result=result):
                raise _Superseded()
        except _Superseded:
            return WorkerResult("ignored", key.job_id, metadata={"claim": "superseded"})
        except Exception as exc:
            return self._handle_error(job, attempt, exc, finalized=finalized)
        meta = dict(computed.meta, attempt=attempt, latencyMs=int((time.perf_counter() - started) * 1000))
        return WorkerResult("completed", key.job_id, metadata=meta)

    # -- failure handling ---------------------------------------------
    def _handle_error(self, job: ChatJob, attempt: int, exc: Exception, *, finalized: bool) -> WorkerResult:
        if isinstance(exc, PermanentAgentError):
            code: FailureCode | None = exc.code
        elif not finalized and (isinstance(exc, TransientAgentError) or is_transient_error(exc)):
            code = None
        else:
            code = FailureCode.INTERNAL_ERROR
        if code is None:
            # At most one retry, and only before finalize (nothing was written yet).
            if self.job_store.retry_later(job.key, attempt=attempt):
                return WorkerResult("retry", job.job_id, RETRY_VISIBILITY_SECONDS, {"attempt": attempt, "reason": "transient"})
            code = FailureCode.TRANSIENT_EXHAUSTED
        failed_now = self.job_store.fail(job.key, code)
        released = release_job_quota(self.quota, job) if failed_now else None
        return WorkerResult("failed", job.job_id, metadata={"failure": code.value, "quotaReleased": released, "attempt": attempt})

    # -- compute -------------------------------------------------------
    def _session(self, job: ChatJob):
        session = self.state_store.get_session_by_reference(job.session_ref) if job.session_ref else None
        now = self.clock()
        if session is None or session.expires_at <= now or session.identity.subject != job.subject:
            raise PermanentAgentError(FailureCode.SESSION_CLOSED)
        return session

    def _bind(self, job: ChatJob, token: str):
        try:
            binding = bind_harness_invocation(
                bearer_token=token, gateway_url=self.gateway_url, identity_verifier=self.identity_verifier,
                requested_matter_id=job.matter_id, conversation_id=job.conversation_id,
                session_selector=job.session_selector, authorization_store=self.authorization_store,
                conversation_store=self.conversation_store, invocation_repository=self.grant_repository,
                correlation_id=job.correlation_id, application_action="agent_chat",
            )
        except AuthorizationDenied as exc:
            # The token no longer verifies, or access was revoked since the job was created.
            raise PermanentAgentError(FailureCode.SESSION_CLOSED) from exc
        if binding.context.tenant_id != job.tenant_id or binding.verified_subject != job.subject:
            raise PermanentAgentError(FailureCode.AGENT_FAILED)
        return binding

    def _history(self, job: ChatJob, binding: Any) -> tuple[tuple[str, str], ...] | None:
        try:
            accepted = self.state_store.list_history_ids((job.subject, job.conversation_id, job.session_selector))
            return last_pairs(load_accepted_events(self.memory, binding.memory_scope, accepted))
        except Exception:
            return None  # degraded: answer without context rather than fail the question

    def _compute(self, job: ChatJob) -> _Computed:
        session = self._session(job)
        binding = self._bind(job, session.access_token)
        history = self._history(job, binding)
        loop = self.invoke_agent(binding, job.question or "", history or ())
        search, fields, inventories, invalid = [], [], [], 0
        for call in loop.tool_calls:
            if call.status != "SUCCESS":
                continue
            try:
                if call.short_name == contracts.SEARCH_DOCUMENTS:
                    search.append((call.tool_use_id, contracts.parse_search_documents_result(call.payload)))
                elif call.short_name == contracts.GET_EXTRACTED_FIELDS:
                    fields.append(contracts.parse_extracted_fields_result(call.payload))
                elif call.short_name == contracts.LIST_MATTER_DOCUMENTS:
                    inventories.append((call.tool_use_id, contracts.parse_list_matter_documents_result(call.payload)))
            except contracts.AgentContractError:
                invalid += 1
        evidence = LoopEvidence.from_results(search, fields, inventories)
        outcome = LoopOutcome(loop.loop_outcome)
        output = resolve_output(loop=outcome, plan=loop.text if outcome is LoopOutcome.COMPLETED else None, evidence=evidence)
        meta: dict[str, object] = {
            "loopOutcome": outcome.value, "iterations": loop.iterations,
            "tools": [call.short_name for call in loop.tool_calls], "invalidToolResults": invalid,
            "tokens": loop.usage.total_tokens if loop.usage else None,
            "historyUnavailable": history is None, **output.metadata(),
        }
        operation_status = "ok"
        if output.path is OutputPath.RAG_FALLBACK:
            output, operation_status = self._rag_fallback(job, binding, output)
            meta["outputPath"] = output.path.value
            meta["citationCount"] = len(output.citations)
        return _Computed(output, operation_status, meta, binding.memory_scope)

    def _rag_fallback(self, job: ChatJob, binding: Any, reasoned: AnswerOutput) -> tuple[AnswerOutput, str]:
        """D5: nothing in the loop was usable; answer with the direct RAG path."""

        if self.time_left is not None and self.time_left() < MIN_FALLBACK_SECONDS:
            raise PermanentAgentError(FailureCode.DEADLINE_EXCEEDED)
        passages: list[Any] = []

        def collect(_identity: Any, _context: Any, _request: Any, _response: Any, selected: tuple[Any, ...]) -> None:
            passages.extend(selected)  # handles are created after finalize, never here

        response = self.rag_chat(
            binding.identity, matter_id=job.matter_id, conversation_id=job.conversation_id,
            session_id=job.session_selector, question=job.question, correlation_id=job.correlation_id,
            authorized_evidence_sink=collect,
        )
        status = getattr(response, "operation_status", "error")
        if status == "error" or not isinstance(getattr(response, "answer", None), str):
            raise PermanentAgentError(FailureCode.AGENT_FAILED)
        texts = {item.citation.citation_id: item.text for item in passages}
        citations = tuple(
            RenderedCitation(c.citation_id, c.document_id, c.document_name, c.page_number, c.section, texts[c.citation_id], False)
            for c in response.citations if c.citation_id in texts and str(texts[c.citation_id]).strip()
        )
        evidence_status = getattr(response.evidence_status, "value", None)
        block = OutputBlock("text", text=response.answer, evidence_status=evidence_status,
                            citation_ids=tuple(c.citation_id for c in citations))
        output = AnswerOutput(
            OutputPath.RAG_FALLBACK, blocks=(block,), citations=citations,
            fallback_reason=reasoned.fallback_reason, plan_error=reasoned.plan_error, omitted=reasoned.omitted,
        )
        return output, status

    # -- publish (after finalize: runs at most once per job) -----------
    def _publish(self, job: ChatJob, computed: _Computed) -> dict[str, object]:
        output = computed.output
        handles = build_citation_handles(
            subject=job.subject, tenant_id=job.tenant_id, matter_id=job.matter_id,
            conversation_id=job.conversation_id, correlation_id=job.correlation_id, citations=output.citations, now=self.clock,
        )
        by_citation: dict[str, str] = {}
        for handle, citation_key in handles:
            self.state_store.put_citation(handle, citation_key=citation_key)
            by_citation[citation_key[3]] = handle.handle
        result = output.public_dict()
        for citation in result["citations"]:  # type: ignore[union-attr]
            citation["handle"] = by_citation.get(citation["citationId"])
        result.update(operationStatus=computed.operation_status, correlationId=job.correlation_id, disclaimerRequired=True)
        if self.memory is not None and computed.operation_status == "ok":
            computed.meta["memoryWritten"] = self._remember(job, output, computed.memory_scope)
        self.state_store.append_audit({
            "subject": job.subject, "tenantId": job.tenant_id, "matterId": job.matter_id,
            "correlationId": job.correlation_id, "operation": "chat_agent", "timestampMs": int(self.clock() * 1000),
        })
        return result

    def _remember(self, job: ChatJob, output: AnswerOutput, scope: Any) -> bool:
        """Question and final answer only, once; never intermediate tool results (D7)."""

        answer = output.plain_text().strip()
        if not answer or not job.question:
            return False
        try:
            for role, text in (("USER", job.question), ("ASSISTANT", answer[:MAX_EVENT_TEXT_LENGTH])):
                event = self.memory.append_event(scope, role=role, text=text)
                event_id = getattr(event, "event_id", None)
                if not isinstance(event_id, str) and isinstance(event, Mapping):
                    nested = event.get("event")
                    event_id = event.get("eventId") or (nested.get("eventId") if isinstance(nested, Mapping) else None)
                if isinstance(event_id, str) and 0 < len(event_id) <= 256:
                    self.state_store.add_history_event((job.subject, job.conversation_id, job.session_selector), event_id)
            return True
        except Exception:
            # The answer is already final; a memory hiccup must not discard it.
            return False


__all__ = [
    "AgentChatRuntime", "AgentChatWorker", "InputDecision", "input_not_allowed_response", "JobQueue", "PermanentAgentError", "SubmissionError",
    "TransientAgentError", "WorkerResult", "is_transient_error", "release_job_quota", "reserve_job_quota",
    "submit_agent_chat",
]
