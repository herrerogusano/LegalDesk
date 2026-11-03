"""Asynchronous layer of the Phase 15 golden set (ADR-021 D4, D8).

What the answer layer cannot see: a question becomes a *job* (quota reserved, record written, message queued),
a worker processes it at least once, and the browser only ever reads the job. This module drives that whole chain
with the **production code** and doubles only for the cloud services:

========================  ======================================================================
real code                 ``submit_agent_chat``, ``handle_sqs_event`` (the SQS Lambda handler),
                          ``AgentChatWorker.process``, ``InMemoryChatJobStore`` (same conditional
                          semantics as the DynamoDB store), ``InMemoryQuotaLedger``, the poll rule of
                          ``http_app`` (``expire_if_due`` + quota release)
double                    SQS (``SimQueue``: visibility timeout, redelivery, ``maxReceiveCount``, DLQ),
                          the clock (``SimClock``: nothing sleeps), Harness (``ScriptedAgent``)
========================  ======================================================================

``SCENARIOS`` is the table of expected behaviour per kind of failure (idempotency, retries by failure type,
jobs left half done, DLQ, closed session, exhausted quota, foreign job). Offline it is a CI gate; in real mode
(``DeployedAsyncTransport``) the same observations are read from the deployed stack for a small subset of jobs.

Reports hold ids, closed codes and counts only.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Mapping

ROOT = Path(__file__).resolve().parents[1]
for _path in ("backend/src", "agent/src", "tests", "."):
    sys.path.insert(0, str(ROOT / _path))

from evals.phase15_routing_eval import MATTER, SUBJECT, T0, TENANT, _ALL_TOOL_NAMES, _unsigned_token  # noqa: E402
from fixture_loader import load_authorization_store, test_identity  # noqa: E402
from legaldesk import agent_jobs  # noqa: E402
from legaldesk.agent_chat import (  # noqa: E402
    AgentChatRuntime, AgentChatWorker, InputDecision, PermanentAgentError, SubmissionError, TransientAgentError,
    release_job_quota, submit_agent_chat,
)
from legaldesk.agent_chat_lambda import handle_sqs_event  # noqa: E402
from legaldesk.agent_jobs import FailureCode, InMemoryChatJobStore, JobKey, JobStatus  # noqa: E402
from legaldesk.authorization import build_request_context  # noqa: E402
from legaldesk.gateway_interceptor import InMemoryGatewayGrantRepository  # noqa: E402
from legaldesk.memory import InMemoryConversationBindingStore, InMemoryShortTermMemory  # noqa: E402
from legaldesk.quota import InMemoryQuotaLedger, QuotaExceededError, QuotaLimits, utc_month  # noqa: E402
from legaldesk.state import InMemoryEphemeralStateStore, SessionRecord  # noqa: E402
from legaldesk_agent import AgentChatResult, AgentToolCall  # noqa: E402

QUEUE_ARN = "arn:aws:sqs:eu-west-1:000000000000:legaldesk-agent-chat-sim"
SESSION_KEY, CONVERSATION, SELECTOR = "sim-session", "conv-sim", "sel-sim"
DEFAULT_VISIBILITY_SECONDS = 900   # queue visibility timeout of the deployed stack (ADR-021 D4)
MAX_RECEIVE_COUNT = 2              # redrive policy of the deployed stack
RETRY_VISIBILITY_SECONDS = agent_jobs.RETRY_VISIBILITY_SECONDS  # 30


class SimulatedCrash(BaseException):
    """The Lambda died without returning (timeout, OOM, kill): nothing after this line runs, the message stays in flight."""


class SimClock:
    def __init__(self, start: float = T0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def set(self, value: float) -> None:
        self.now = max(self.now, float(value))  # never backwards


@dataclass(slots=True)
class _Message:
    message_id: str
    body: str
    visible_at: float
    receive_count: int = 0


class SimQueue:
    """SQS standard queue with a redrive policy, driven by a simulated clock."""

    def __init__(self, clock: SimClock, *, max_receive: int = MAX_RECEIVE_COUNT, visibility: int = DEFAULT_VISIBILITY_SECONDS,
                 process_seconds: float = 1.0) -> None:
        self.clock, self.max_receive, self.visibility, self.process_seconds = clock, max_receive, visibility, process_seconds
        self.messages: list[_Message] = []
        self.dlq: list[_Message] = []
        self.deliveries = self.deleted = self.crashes = self.handler_errors = 0
        self.visibility_changes: list[int] = []
        self.fail_publish = False
        self._counter = 0

    # JobQueue protocol used by submit_agent_chat -----------------------------------
    def publish(self, key: JobKey) -> None:
        if self.fail_publish:
            raise RuntimeError("queue unavailable")
        self.send_raw(json.dumps({"jobId": key.job_id, "tenantId": key.tenant_id, "matterId": key.matter_id}, separators=(",", ":")))

    def send_raw(self, body: str) -> None:
        self._counter += 1
        self.messages.append(_Message(f"m-{self._counter}", body, self.clock()))

    def depth(self) -> int:
        return len(self.messages)

    def pump(self, worker: Any, *, max_steps: int = 200, stop_at: float | None = None) -> None:
        """Deliver until the queue is empty (or ``stop_at``): receive, run the real handler, delete or keep."""

        steps = 0
        while steps < max_steps:
            now = self.clock()
            ready = [m for m in self.messages if m.visible_at <= now]
            if not ready:
                if not self.messages:
                    return
                upcoming = min(m.visible_at for m in self.messages)
                if stop_at is not None and upcoming > stop_at:
                    return
                self.clock.set(upcoming)
                continue
            message = ready[0]
            steps += 1
            message.receive_count += 1
            if message.receive_count > self.max_receive:  # redrive: the third receive moves it to the DLQ
                self.messages.remove(message)
                self.dlq.append(message)
                continue
            message.visible_at = now + self.visibility
            self.deliveries += 1

            def change_visibility(_receipt: str, seconds: int, _m: _Message = message) -> None:
                _m.visible_at = self.clock() + seconds
                self.visibility_changes.append(seconds)

            event = {"Records": [{"messageId": message.message_id, "receiptHandle": f"rh-{message.message_id}-{message.receive_count}",
                                  "body": message.body, "eventSourceARN": QUEUE_ARN}]}
            try:
                response = handle_sqs_event(event, worker, queue_arn=QUEUE_ARN, change_visibility=change_visibility)
            except SimulatedCrash:
                self.crashes += 1
                self.clock.advance(self.process_seconds)
                continue  # the invocation never returned: the message stays hidden until its visibility expires
            except Exception:  # noqa: BLE001 - a malformed event is a handler error: the message is not deleted
                self.handler_errors += 1
                continue
            self.clock.advance(self.process_seconds)
            if any(item["itemIdentifier"] == message.message_id for item in response["batchItemFailures"]):
                continue
            self.messages.remove(message)
            self.deleted += 1


# ------------------------------------------------------------------ the scripted agent

class ThrottlingError(Exception):
    """Shaped like a botocore ClientError for throttling (classified transient by the worker)."""

    def __init__(self) -> None:
        super().__init__("throttled")
        self.response = {"Error": {"Code": "ThrottlingException"}, "ResponseMetadata": {"HTTPStatusCode": 400}}


class ProviderFiveHundred(Exception):
    def __init__(self) -> None:
        super().__init__("provider error")
        self.response = {"Error": {"Code": "InternalServerException"}, "ResponseMetadata": {"HTTPStatusCode": 500}}


class ReadTimeoutError(Exception):
    """Class name matches botocore's, which is how the worker recognises it."""


class ValidationFailure(Exception):
    def __init__(self) -> None:
        super().__init__("bad request")
        self.response = {"Error": {"Code": "ValidationException"}, "ResponseMetadata": {"HTTPStatusCode": 400}}


def canned_result(binding: Any) -> AgentChatResult:
    """A valid grounded loop (one search_documents result and a one-block plan)."""

    payload = {"answer": "Respuesta sintética.", "evidenceStatus": "answerable", "operationStatus": "ok", "citations": [{
        "citationId": "s-1", "documentId": "doc-sim", "documentName": "sim.pdf", "pageNumber": 1, "section": None,
        "passage": "Pasaje sintético."}]}
    plan = {"version": 1, "blocks": [{"type": "text", "source": {"toolUseId": "tu-1"}}]}
    return AgentChatResult(
        session_id="sim", correlation_id=binding.correlation_id, text=json.dumps(plan), loop_outcome="completed", iterations=1,
        stop_reason="end_turn", tool_calls=(AgentToolCall("tu-1", _ALL_TOOL_NAMES["search_documents"], "SUCCESS", payload),))


class ScriptedAgent:
    """Behaviour per call: ``"ok"``, an exception instance to raise, or a callable ``(binding) -> AgentChatResult``."""

    def __init__(self, behaviours: list[Any], result: Callable[[Any], AgentChatResult] = canned_result) -> None:
        self.behaviours, self.result, self.calls = list(behaviours), result, 0

    def __call__(self, binding: Any, question: str, history: tuple[tuple[str, str], ...]) -> AgentChatResult:
        index = min(self.calls, len(self.behaviours) - 1)
        self.calls += 1
        behaviour = self.behaviours[index]
        if behaviour == "ok":
            return self.result(binding)
        if isinstance(behaviour, BaseException):
            raise behaviour
        return behaviour(binding)


def failing_rag(*_args: Any, **_kwargs: Any) -> Any:
    return SimpleNamespace(operation_status="error", answer="x", citations=(), evidence_status=None)


# ------------------------------------------------------------------ the harness

class AsyncHarness:
    """One deployment in memory: API submit, queue, SQS handler, worker, job store, quota, session, memory."""

    def __init__(self, agent: Callable[..., Any], *, rag_chat: Callable[..., Any] = failing_rag,
                 input_check: Callable[[str, str], InputDecision] | None = None,
                 limits: QuotaLimits | None = None, session_ttl: float = 3600, process_seconds: float = 1.0,
                 time_left: Callable[[], float] | None = None) -> None:
        self.clock = SimClock()
        self.auth = load_authorization_store()
        self.identity = test_identity(SUBJECT)
        self.conversations = InMemoryConversationBindingStore()
        self.conversations.bind(build_request_context(self.identity, MATTER, self.auth), conversation_id=CONVERSATION, session_selector=SELECTOR)
        self.state = InMemoryEphemeralStateStore(clock=self.clock)
        self.state.put_session(SESSION_KEY, SessionRecord(self.identity, _unsigned_token(SUBJECT), "csrf", T0 + session_ttl))
        self.jobs = InMemoryChatJobStore(clock=self.clock)
        self.quota = InMemoryQuotaLedger(limits=limits or QuotaLimits(chats_per_month=50, harness_per_month=50), clock=self.clock)
        self.grants = InMemoryGatewayGrantRepository()
        self.queue = SimQueue(self.clock, process_seconds=process_seconds)
        self.memory = InMemoryShortTermMemory()
        self.agent = agent
        self.runtime = AgentChatRuntime(self.jobs, self.queue, input_check or (lambda question, _corr: InputDecision("allowed", question)))
        self.worker = AgentChatWorker(
            job_store=self.jobs, state_store=self.state, quota=self.quota, authorization_store=self.auth,
            conversation_store=self.conversations, grant_repository=self.grants,
            identity_verifier=SimpleNamespace(verify_authorization_header=lambda header: self.identity),
            gateway_url="https://gateway.example.test/mcp", invoke_agent=agent, rag_chat=rag_chat, memory=self.memory,
            clock=self.clock, time_left=time_left,
        )
        self.context = build_request_context(self.identity, MATTER, self.auth)

    def submit(self, question: str = "¿Pregunta sintética?") -> agent_jobs.ChatJob:
        return submit_agent_chat(
            self.runtime, self.quota, subject=SUBJECT, tenant_id=self.context.tenant_id, matter_id=MATTER,
            conversation_id=CONVERSATION, session_selector=SELECTOR, correlation_id=self.context.correlation_id,
            question=question, session_ref=self.state.session_reference(SESSION_KEY), now=self.clock(),
        )

    def pump(self, **kwargs: Any) -> None:
        self.queue.pump(self.worker, **kwargs)

    def poll(self, key: JobKey) -> dict[str, Any]:
        """What ``GET /api/chat/jobs/{id}`` does (http_app): expire if due, give the quota back, return the public view."""

        job, failed_now = self.jobs.expire_if_due(key, now=self.clock())
        if job is None:
            return {"status": "missing"}
        if failed_now:
            release_job_quota(self.quota, job)
        return job.public_dict()

    def counters(self) -> tuple[int, int]:
        counter = self.quota.counters.get((self.context.tenant_id, utc_month(self.clock())), {})
        return counter.get("chatCount", 0), counter.get("harnessCount", 0)

    def memory_events(self) -> int:
        return sum(len(events) for events in self.memory._events.values())  # noqa: SLF001 - test double introspection

    def active_jobs(self) -> int:
        return sum(1 for job in self.jobs._jobs.values() if job.status in agent_jobs.ACTIVE)  # noqa: SLF001


# ------------------------------------------------------------------ observations and scenarios

@dataclass(slots=True)
class AsyncObservation:
    status: str | None = None
    failure: str | None = None
    attempts: int | None = None
    agent_calls: int = 0
    quota: tuple[int, int] = (0, 0)
    memory_events: int = 0
    deliveries: int = 0
    dlq: int = 0
    deleted: int = 0
    crashes: int = 0
    visibility_changes: tuple[int, ...] = ()
    queue_left: int = 0
    active_jobs: int = 0
    elapsed_s: float = 0.0
    timeline: tuple[tuple[int, str], ...] = ()   # (seconds since submit, what the UI would show)
    extra: dict[str, Any] = field(default_factory=dict)


def _observe(h: AsyncHarness, key: JobKey | None, agent: Any, started: float) -> AsyncObservation:
    job = h.jobs.get(key) if key else None
    return AsyncObservation(
        status=job.status.value if job else None, failure=job.failure_code.value if job and job.failure_code else None,
        attempts=job.attempt if job else None, agent_calls=getattr(agent, "calls", 0), quota=h.counters(),
        memory_events=h.memory_events(), deliveries=h.queue.deliveries, dlq=len(h.queue.dlq), deleted=h.queue.deleted,
        crashes=h.queue.crashes, visibility_changes=tuple(h.queue.visibility_changes), queue_left=h.queue.depth(),
        active_jobs=h.active_jobs(), elapsed_s=round(h.clock() - started, 1),
    )


def _run(agent: Any, *, rag_chat: Callable[..., Any] = failing_rag, **harness_kwargs: Any) -> tuple[AsyncHarness, agent_jobs.ChatJob, float]:
    harness = AsyncHarness(agent, rag_chat=rag_chat, **harness_kwargs)
    started = harness.clock()
    return harness, harness.submit(), started


def s_happy() -> AsyncObservation:
    agent = ScriptedAgent(["ok"])
    h, job, t0 = _run(agent)
    h.pump()
    return _observe(h, job.key, agent, t0)


def s_duplicate_after_success() -> AsyncObservation:
    agent = ScriptedAgent(["ok"])
    h, job, t0 = _run(agent)
    h.pump()
    before = h.jobs.get(job.key).result
    h.queue.publish(job.key)   # SQS delivers the same message again (at-least-once)
    h.pump()
    obs = _observe(h, job.key, agent, t0)
    obs.extra["resultUnchanged"] = h.jobs.get(job.key).result == before
    return obs


def s_duplicate_during_live_attempt() -> AsyncObservation:
    seen: list[str] = []
    holder: dict[str, Any] = {}

    def concurrent_delivery(binding: Any) -> AgentChatResult:
        holder["h"].clock.advance(10)
        seen.append(holder["h"].worker.process(holder["key"]).action)   # a second delivery while this attempt is alive
        return canned_result(binding)

    agent = ScriptedAgent([concurrent_delivery])
    h, job, t0 = _run(agent)
    holder.update(h=h, key=job.key)
    h.pump()
    obs = _observe(h, job.key, agent, t0)
    obs.extra["concurrentDelivery"] = seen[0] if seen else None
    return obs


def s_concurrent_contenders() -> AsyncObservation:
    """Two real threads claim the same job at the same moment: exactly one wins (barrier + gate, not sequential)."""

    gate, entered = threading.Event(), threading.Event()

    def slow(binding: Any) -> AgentChatResult:
        entered.set()
        gate.wait(5)
        return canned_result(binding)

    agent = ScriptedAgent([slow])
    h, job, t0 = _run(agent)
    h.queue.messages.clear()   # this scenario delivers by hand, from two threads
    results: list[str] = []
    first = threading.Thread(target=lambda: results.append(h.worker.process(job.key).action))
    first.start()
    entered.wait(5)
    second = threading.Thread(target=lambda: results.append(h.worker.process(job.key).action))
    second.start()
    second.join(5)
    gate.set()
    first.join(5)
    obs = _observe(h, job.key, agent, t0)
    obs.extra["actions"] = tuple(sorted(results))
    return obs


def _transient(exc_factory: Callable[[], BaseException], times: int) -> AsyncObservation:
    agent = ScriptedAgent([exc_factory() for _ in range(times)] + ["ok"])
    h, job, t0 = _run(agent)
    h.pump()
    return _observe(h, job.key, agent, t0)


def _permanent(exc: BaseException) -> AsyncObservation:
    agent = ScriptedAgent([exc, "ok"])
    h, job, t0 = _run(agent)
    h.pump()
    return _observe(h, job.key, agent, t0)


def s_session_closed() -> AsyncObservation:
    agent = ScriptedAgent(["ok"])
    h, job, t0 = _run(agent)
    h.state.delete_session(SESSION_KEY)   # the user logs out while the job waits in the queue
    h.pump()
    return _observe(h, job.key, agent, t0)


def s_session_expired_in_queue() -> AsyncObservation:
    agent = ScriptedAgent(["ok"])
    h, job, t0 = _run(agent, session_ttl=60)   # a session about to expire; the message waits longer than that
    h.clock.advance(120)
    h.pump()
    return _observe(h, job.key, agent, t0)


def s_logout_mid_job() -> AsyncObservation:
    holder: dict[str, Any] = {}

    def logout_then_answer(binding: Any) -> AgentChatResult:
        holder["h"].state.delete_session(SESSION_KEY)   # logout while the Harness loop runs: the answer is already final
        return canned_result(binding)

    agent = ScriptedAgent([logout_then_answer])
    h, job, t0 = _run(agent)
    holder["h"] = h
    h.pump()
    return _observe(h, job.key, agent, t0)


def s_crash_mid_attempt() -> AsyncObservation:
    """The Lambda dies after claiming: the job stays ``running`` and nobody tells the UI. The poll rule must."""

    agent = ScriptedAgent([SimulatedCrash()])
    h, job, t0 = _run(agent)
    h.pump(stop_at=h.clock() + 1)                    # the crashed delivery; the message stays hidden for 900 s
    timeline = [(0, h.poll(job.key)["status"])]
    for at in (100, 200, 299, 301):                  # what the UI sees while it polls
        h.clock.set(T0 + at)
        timeline.append((at, h.poll(job.key)["status"]))
    quota_after_deadline = h.counters()
    h.pump()                                         # the message returns after its visibility: terminal job, ignored
    obs = _observe(h, job.key, agent, t0)
    obs.timeline = tuple(timeline)
    obs.extra["quotaAtDeadline"] = quota_after_deadline
    return obs


def s_crash_then_redelivery_after_deadline() -> AsyncObservation:
    agent = ScriptedAgent([SimulatedCrash(), "ok"])
    h, job, t0 = _run(agent)
    h.pump()   # no poll: only the queue redelivers (after 900 s, past the 5-minute job deadline)
    return _observe(h, job.key, agent, t0)


def s_crash_after_finalize() -> AsyncObservation:
    agent = ScriptedAgent(["ok", "ok"])
    h, job, t0 = _run(agent)

    def die(*_a: Any, **_k: Any) -> None:
        raise SimulatedCrash()

    h.state.put_citation = die   # type: ignore[method-assign]  # _publish runs after finalize
    h.pump(stop_at=h.clock() + 1)
    seen_finalizing = h.jobs.get(job.key).status.value
    h.clock.set(T0 + agent_jobs.FINALIZE_WINDOW_SECONDS + 5)
    view = h.poll(job.key)
    h.pump()
    obs = _observe(h, job.key, agent, t0)
    obs.timeline = ((1, seen_finalizing), (agent_jobs.FINALIZE_WINDOW_SECONDS + 5, view["status"]))
    return obs


def s_error_after_finalize() -> AsyncObservation:
    agent = ScriptedAgent(["ok", "ok"])
    h, job, t0 = _run(agent)

    def boom(*_a: Any, **_k: Any) -> None:
        raise ThrottlingError()   # even a transient-looking error is never retried once finalized

    h.state.put_citation = boom   # type: ignore[method-assign]
    h.pump()
    return _observe(h, job.key, agent, t0)


def s_poison_message() -> AsyncObservation:
    agent = ScriptedAgent(["ok"])
    h, job, t0 = _run(agent)
    h.queue.messages.clear()
    h.queue.send_raw("not json")
    h.queue.send_raw(json.dumps({"jobId": "j", "tenantId": "t", "matterId": "m", "question": "extra key"}))
    h.pump()
    return _observe(h, None, agent, t0)


def s_unclassified_error_goes_to_dlq() -> AsyncObservation:
    """The worker itself raises (store down): the handler cannot classify it, so one quick retry, then the DLQ.

    The job stays ``queued``; the poll rule must still fail it at the deadline and give the quota back."""

    agent = ScriptedAgent(["ok"])
    h, job, t0 = _run(agent)
    real_process = h.worker.process

    def broken(_key: JobKey) -> Any:
        raise RuntimeError("job store unavailable")

    h.worker.process = broken   # type: ignore[method-assign]
    h.pump()
    status_when_dlq = h.jobs.get(job.key).status.value
    h.worker.process = real_process   # type: ignore[method-assign]
    h.clock.set(T0 + agent_jobs.JOB_TOTAL_DEADLINE_SECONDS + 1)
    view = h.poll(job.key)
    obs = _observe(h, job.key, agent, t0)
    obs.timeline = ((1, status_when_dlq), (agent_jobs.JOB_TOTAL_DEADLINE_SECONDS + 1, view["status"]))
    return obs


def s_quota_exhausted() -> AsyncObservation:
    agent = ScriptedAgent(["ok"])
    h = AsyncHarness(agent, limits=QuotaLimits(chats_per_month=50, harness_per_month=1))
    t0 = h.clock()
    first = h.submit()
    rejected = False
    try:
        h.submit()
    except QuotaExceededError:
        rejected = True
    h.pump()
    obs = _observe(h, first.key, agent, t0)
    obs.extra["secondRejected"] = rejected
    obs.extra["jobsCreated"] = len(h.jobs._jobs)   # noqa: SLF001
    return obs


def s_publish_failure() -> AsyncObservation:
    agent = ScriptedAgent(["ok"])
    h = AsyncHarness(agent)
    h.queue.fail_publish = True
    t0, raised = h.clock(), False
    try:
        h.submit()
    except SubmissionError:
        raised = True
    obs = _observe(h, None, agent, t0)
    obs.extra["submissionError"] = raised
    obs.extra["jobsCreated"] = len(h.jobs._jobs)   # noqa: SLF001
    return obs


def s_foreign_job_and_scope() -> AsyncObservation:
    agent = ScriptedAgent(["ok"])
    h, job, t0 = _run(agent)
    h.pump()
    obs = _observe(h, job.key, agent, t0)
    other_tenant = JobKey("tnt_borealis", MATTER, job.job_id)
    other_matter = JobKey(job.tenant_id, "mat_glacier", job.job_id)
    obs.extra["otherTenantVisible"] = h.jobs.get(other_tenant) is not None
    obs.extra["otherMatterVisible"] = h.jobs.get(other_matter) is not None
    obs.extra["publicViewKeys"] = tuple(sorted(h.jobs.get(job.key).public_dict()))
    obs.extra["questionKept"] = h.jobs.get(job.key).question is not None
    return obs


def s_rag_fallback_error() -> AsyncObservation:
    def no_tool(binding: Any) -> AgentChatResult:
        return AgentChatResult("sim", binding.correlation_id, "", "no_tool_called", 1, "end_turn", ())

    agent = ScriptedAgent([no_tool])
    h, job, t0 = _run(agent, rag_chat=failing_rag)
    h.pump()
    return _observe(h, job.key, agent, t0)


def s_fallback_without_time() -> AsyncObservation:
    def no_tool(binding: Any) -> AgentChatResult:
        return AgentChatResult("sim", binding.correlation_id, "", "no_tool_called", 1, "end_turn", ())

    agent = ScriptedAgent([no_tool])
    h, job, t0 = _run(agent, time_left=lambda: 5.0)   # the Lambda has no time left for the direct RAG path
    h.pump()
    return _observe(h, job.key, agent, t0)


def s_expired_job_message() -> AsyncObservation:
    agent = ScriptedAgent(["ok"])
    h, job, t0 = _run(agent)
    h.queue.messages.clear()
    h.clock.advance(agent_jobs.JOB_TTL_SECONDS + 10)   # the record has expired (TTL); a stray message arrives
    h.queue.publish(job.key)
    h.pump()
    obs = _observe(h, job.key, agent, t0)
    obs.extra["jobStillStored"] = h.jobs.get(job.key) is not None
    return obs


def s_deadline_at_claim() -> AsyncObservation:
    agent = ScriptedAgent(["ok"])
    h, job, t0 = _run(agent)
    h.queue.messages.clear()
    h.clock.advance(agent_jobs.JOB_TOTAL_DEADLINE_SECONDS + 1)   # the message was stuck in the queue longer than the deadline
    h.queue.publish(job.key)
    h.pump()
    return _observe(h, job.key, agent, t0)


@dataclass(frozen=True, slots=True)
class Scenario:
    id: str
    topic: str            # idempotency | retry | half_done | dlq | session | quota | isolation | fallback
    run: Callable[[], AsyncObservation]
    expect: Mapping[str, Any]   # every key must equal the observation (``extra`` keys are looked up in ``obs.extra``)


_OK = dict(status="succeeded", failure=None, quota=(1, 1), memory_events=2, dlq=0, queue_left=0, active_jobs=0)
_FAILED_RELEASED = dict(status="failed", quota=(0, 0), memory_events=0, dlq=0, queue_left=0, active_jobs=0)

SCENARIOS: tuple[Scenario, ...] = (
    Scenario("happy_path", "idempotency", s_happy, {**_OK, "attempts": 1, "agent_calls": 1, "deliveries": 1, "deleted": 1}),
    Scenario("duplicate_after_success", "idempotency", s_duplicate_after_success,
             {**_OK, "attempts": 1, "agent_calls": 1, "deliveries": 2, "deleted": 2, "resultUnchanged": True}),
    Scenario("duplicate_during_live_attempt", "idempotency", s_duplicate_during_live_attempt,
             {**_OK, "attempts": 1, "agent_calls": 1, "concurrentDelivery": "ignored"}),
    Scenario("concurrent_contenders_one_winner", "idempotency", s_concurrent_contenders,
             {**_OK, "attempts": 1, "agent_calls": 1, "actions": ("completed", "ignored")}),
    Scenario("transient_throttling_retries_once", "retry", lambda: _transient(ThrottlingError, 1),
             {**_OK, "attempts": 2, "agent_calls": 2, "deliveries": 2, "visibility_changes": (RETRY_VISIBILITY_SECONDS,)}),
    Scenario("transient_5xx_retries_once", "retry", lambda: _transient(ProviderFiveHundred, 1),
             {**_OK, "attempts": 2, "agent_calls": 2, "visibility_changes": (RETRY_VISIBILITY_SECONDS,)}),
    Scenario("transient_read_timeout_retries_once", "retry", lambda: _transient(ReadTimeoutError, 1),
             {**_OK, "attempts": 2, "agent_calls": 2, "visibility_changes": (RETRY_VISIBILITY_SECONDS,)}),
    Scenario("transient_builtin_timeout_retries_once", "retry", lambda: _transient(TimeoutError, 1),
             {**_OK, "attempts": 2, "agent_calls": 2, "visibility_changes": (RETRY_VISIBILITY_SECONDS,)}),
    Scenario("transient_marked_by_worker_retries_once", "retry", lambda: _transient(TransientAgentError, 1),
             {**_OK, "attempts": 2, "agent_calls": 2}),
    Scenario("transient_twice_exhausts_and_releases_quota", "retry", lambda: _transient(ThrottlingError, 2),
             {**_FAILED_RELEASED, "failure": "transient_exhausted", "attempts": 2, "agent_calls": 2, "deliveries": 2,
              "visibility_changes": (RETRY_VISIBILITY_SECONDS,)}),
    Scenario("permanent_validation_error_not_retried", "retry", lambda: _permanent(ValidationFailure()),
             {**_FAILED_RELEASED, "failure": "internal_error", "attempts": 1, "agent_calls": 1, "deliveries": 1, "visibility_changes": ()}),
    Scenario("permanent_bug_not_retried", "retry", lambda: _permanent(KeyError("bug")),
             {**_FAILED_RELEASED, "failure": "internal_error", "attempts": 1, "agent_calls": 1, "visibility_changes": ()}),
    Scenario("permanent_agent_failure_not_retried", "retry", lambda: _permanent(PermanentAgentError(FailureCode.AGENT_FAILED)),
             {**_FAILED_RELEASED, "failure": "agent_failed", "attempts": 1, "agent_calls": 1, "visibility_changes": ()}),
    Scenario("error_after_finalize_never_retried", "retry", s_error_after_finalize,
             {**_FAILED_RELEASED, "failure": "internal_error", "attempts": 1, "agent_calls": 1, "deliveries": 1, "visibility_changes": ()}),
    Scenario("crash_mid_attempt_ui_sees_running_then_failed", "half_done", s_crash_mid_attempt,
             {**_FAILED_RELEASED, "failure": "deadline_exceeded", "attempts": 1, "agent_calls": 1, "crashes": 1,
              "timeline": ((0, "running"), (100, "running"), (200, "running"), (299, "running"), (301, "failed")),
              "quotaAtDeadline": (0, 0)}),
    Scenario("crash_redelivered_after_deadline_fails_cleanly", "half_done", s_crash_then_redelivery_after_deadline,
             {**_FAILED_RELEASED, "failure": "deadline_exceeded", "attempts": 1, "agent_calls": 1, "crashes": 1, "deliveries": 2}),
    Scenario("crash_after_finalize_ui_sees_finalizing_then_failed", "half_done", s_crash_after_finalize,
             {**_FAILED_RELEASED, "failure": "internal_error", "attempts": 1, "agent_calls": 1, "crashes": 1,
              "timeline": ((1, "finalizing"), (agent_jobs.FINALIZE_WINDOW_SECONDS + 5, "failed"))}),
    Scenario("deadline_exceeded_at_claim", "half_done", s_deadline_at_claim,
             {**_FAILED_RELEASED, "failure": "deadline_exceeded", "attempts": 0, "agent_calls": 0}),
    Scenario("expired_job_message_is_ignored", "half_done", s_expired_job_message,
             {"dlq": 0, "queue_left": 0, "agent_calls": 0, "jobStillStored": False}),
    Scenario("poison_messages_go_to_dlq_without_reaching_the_worker", "dlq", s_poison_message,
             {"dlq": 2, "queue_left": 0, "agent_calls": 0, "deliveries": 4, "visibility_changes": (RETRY_VISIBILITY_SECONDS,) * 4}),
    Scenario("unclassified_error_twice_goes_to_dlq_and_the_poll_still_fails_the_job", "dlq", s_unclassified_error_goes_to_dlq,
             {"dlq": 1, "status": "failed", "failure": "deadline_exceeded", "quota": (0, 0), "agent_calls": 0, "active_jobs": 0,
              "timeline": ((1, "queued"), (agent_jobs.JOB_TOTAL_DEADLINE_SECONDS + 1, "failed"))}),
    Scenario("session_closed_before_processing", "session", s_session_closed,
             {**_FAILED_RELEASED, "failure": "session_closed", "agent_calls": 0, "deliveries": 1}),
    Scenario("session_expired_while_queued", "session", s_session_expired_in_queue,
             {**_FAILED_RELEASED, "failure": "session_closed", "agent_calls": 0}),
    Scenario("logout_during_the_loop_does_not_discard_the_answer", "session", s_logout_mid_job,
             {**_OK, "attempts": 1, "agent_calls": 1}),
    Scenario("quota_exhausted_creates_no_job", "quota", s_quota_exhausted,
             {**_OK, "secondRejected": True, "jobsCreated": 1, "memory_events": 2}),
    Scenario("queue_publish_failure_releases_quota", "quota", s_publish_failure,
             {"quota": (0, 0), "submissionError": True, "queue_left": 0, "active_jobs": 0, "agent_calls": 0}),
    Scenario("rag_fallback_error_fails_the_job_and_releases_quota", "fallback", s_rag_fallback_error,
             {**_FAILED_RELEASED, "failure": "agent_failed", "attempts": 1}),
    Scenario("fallback_without_lambda_time_fails_cleanly", "fallback", s_fallback_without_time,
             {**_FAILED_RELEASED, "failure": "deadline_exceeded", "attempts": 1}),
    Scenario("job_is_scoped_to_tenant_matter_and_hides_secrets", "isolation", s_foreign_job_and_scope,
             {**_OK, "otherTenantVisible": False, "otherMatterVisible": False, "questionKept": False,
              "publicViewKeys": ("conversationId", "correlationId", "jobId", "result", "status")}),
)

OBSERVATION_FIELDS = {
    "status", "failure", "attempts", "agent_calls", "quota", "memory_events", "deliveries", "dlq", "deleted", "crashes",
    "visibility_changes", "queue_left", "active_jobs", "elapsed_s", "timeline",
}


def check_expectation(expect: Mapping[str, Any], obs: AsyncObservation) -> list[str]:
    """Names of the expectations the observation does not meet (empty = scenario passed)."""

    wrong = []
    for key, wanted in expect.items():
        got = getattr(obs, key) if key in OBSERVATION_FIELDS else obs.extra.get(key, "<absent>")
        if got != wanted:
            wrong.append(key)
    return wrong


def run_scenarios(scenarios: tuple[Scenario, ...] = SCENARIOS) -> list[dict[str, Any]]:
    rows = []
    for scenario in scenarios:
        started = time.perf_counter()
        try:
            obs = scenario.run()
            wrong, error = check_expectation(scenario.expect, obs), None
        except Exception as exc:  # noqa: BLE001 - a scenario that cannot run is a failed scenario, with a closed code
            obs, wrong, error = AsyncObservation(), [], type(exc).__name__.lower()[:64]
        rows.append({
            "id": scenario.id, "topic": scenario.topic, "passed": error is None and not wrong, "unmet": wrong, "error": error,
            "status": obs.status, "failure": obs.failure, "attempts": obs.attempts, "agentCalls": obs.agent_calls,
            "deliveries": obs.deliveries, "dlq": obs.dlq, "quota": list(obs.quota), "memoryEvents": obs.memory_events,
            "simulatedSeconds": obs.elapsed_s, "wallMs": int((time.perf_counter() - started) * 1000),
        })
    return rows


def summarize_scenarios(rows: list[dict[str, Any]]) -> dict[str, Any]:
    topics = sorted({row["topic"] for row in rows})
    return {
        "scenarios": len(rows), "passed": sum(1 for row in rows if row["passed"]),
        "byTopic": {topic: {"scenarios": sum(1 for r in rows if r["topic"] == topic),
                            "passed": sum(1 for r in rows if r["topic"] == topic and r["passed"])} for topic in topics},
        "failed": [row["id"] for row in rows if not row["passed"]],
    }


# ------------------------------------------------------------------ real transport (prepared, not executed)

def poll_job_until_terminal(job_store: Any, quota: Any, key: JobKey, *, clock: Callable[[], float] = time.time,
                            sleep: Callable[[float], None] = time.sleep, timeout_s: float = 330.0, interval_s: float = 3.0) -> dict[str, Any]:
    """Poll like the browser does, with the same expiry rule as the API (``expire_if_due`` + quota release).

    Returns the closed observation of the job: status timeline, failure code, attempts, wall latency. It never holds the question
    or the answer. A job still active after ``timeout_s`` (the deadline is 300 s) is reported ``stuck``: that is a finding.
    """

    started, timeline, last = clock(), [], None
    while clock() - started <= timeout_s:
        job, failed_now = job_store.expire_if_due(key, now=clock())
        if job is None:
            return {"status": "missing", "timeline": timeline, "latencyS": round(clock() - started, 1)}
        if failed_now:
            release_job_quota(quota, job)
        if job.status.value != last:
            last = job.status.value
            timeline.append((round(clock() - started, 1), last))
        if job.status in agent_jobs.TERMINAL:
            return {"status": job.status.value, "failure": job.failure_code.value if job.failure_code else None, "attempts": job.attempt,
                    "timeline": timeline, "latencyS": round(clock() - started, 1)}
        sleep(interval_s)
    return {"status": "stuck", "timeline": timeline, "latencyS": round(clock() - started, 1)}
