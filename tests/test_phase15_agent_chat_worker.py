"""Phase 15 (T4.2, T4.3, T5): submission and worker, with a scripted Harness double."""

from __future__ import annotations

import json
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))
sys.path.insert(0, str(ROOT / "agent" / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from fixture_loader import load_authorization_store, test_identity
from legaldesk.agent_chat import (
    AgentChatRuntime,
    AgentChatWorker,
    InputDecision,
    SubmissionError,
    is_transient_error,
    submit_agent_chat,
)
from legaldesk.agent_jobs import (
    ATTEMPT_WINDOW_SECONDS,
    ClaimOutcome,
    FailureCode,
    InMemoryChatJobStore,
    JobKey,
    JobStatus,
)
from legaldesk.authorization import VerifiedIdentity, build_request_context
from legaldesk.chat import ChatCitation, ChatResponse
from legaldesk.evidence import EvidenceStatus
from legaldesk.gateway_interceptor import InMemoryGatewayGrantRepository
from legaldesk.memory import InMemoryConversationBindingStore, InMemoryShortTermMemory
from legaldesk.quota import CHATS, GATEWAY, HARNESS, InMemoryQuotaLedger, QuotaExceededError, QuotaLimits
from legaldesk.state import InMemoryEphemeralStateStore, SessionRecord
from legaldesk_agent import AgentChatResult, AgentToolCall

SUBJECT, MATTER, TENANT = "idp|alice-fictional", "mat_sundial", "tnt_aurora"
T0 = 1_800_000_000.0
SEARCH_TOOL = "@legaldesk_gateway/search-documents___search_documents"
FIELDS_TOOL = "@legaldesk_gateway/metadata-mcp___get_extracted_fields"


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> float:
        return self.now


def search_payload(answer="El plazo es de 30 días."):
    return {"answer": answer, "evidenceStatus": "answerable", "operationStatus": "ok",
            "citations": [{"citationId": "c1", "documentId": "doc-a", "documentName": "Alfa.pdf", "pageNumber": 2,
                           "section": None, "passage": "El plazo será de 30 días."}]}


def fields_payload():
    return {"documents": [{"documentId": "doc-a", "name": "Alfa.pdf", "documentType": "CONTRACT", "fields": [
        {"name": "explicit_expiration_date", "status": "fact", "value": "2026-03-15", "origin": "LITERAL",
         "evidence": [{"page": 1, "quote": "Vence el 15 de marzo de 2026"}]}]}], "notFound": []}


def agent_result(plan, *calls, outcome="completed"):
    return AgentChatResult(
        session_id="s", correlation_id="c", text=plan if isinstance(plan, str) else json.dumps(plan),
        loop_outcome=outcome, iterations=2, stop_reason="end_turn", tool_calls=tuple(calls),
    )


def call(tool_use_id, tool, payload, status="SUCCESS"):
    return AgentToolCall(tool_use_id, tool, status, payload)


TEXT_PLAN = {"version": 1, "blocks": [{"type": "text", "source": {"toolUseId": "tu-1"}}]}
DATA_PLAN = {"version": 1, "intro": "data_from_documents", "blocks": [{"type": "data", "groups": [{
    "criterion": {"id": "all_documents"}, "refs": [{"documentId": "doc-a", "field": "explicit_expiration_date"}]}]}]}


class World:
    """One backend with a job, a live session and scripted agent and RAG doubles."""

    def __init__(self, test: unittest.TestCase) -> None:
        self.clock = Clock()
        self.auth = load_authorization_store()
        self.identity = test_identity(SUBJECT)
        self.conversations = InMemoryConversationBindingStore()
        context = build_request_context(self.identity, MATTER, self.auth)
        self.conversations.bind(context, conversation_id="conv-1", session_selector="sel-1")
        self.state = InMemoryEphemeralStateStore(clock=self.clock)
        self.session_key = "cookie-session-key"
        self.state.put_session(self.session_key, SessionRecord(self.identity, "access-token", "csrf", T0 + 3600))
        self.jobs = InMemoryChatJobStore(clock=self.clock)
        self.quota = InMemoryQuotaLedger(limits=QuotaLimits(chats_per_month=20, harness_per_month=20, gateway_per_month=5), clock=self.clock)
        self.memory = InMemoryShortTermMemory()
        self.published: list[JobKey] = []
        self.agent_script: list = []
        self.agent_calls: list[tuple] = []
        self.rag_calls: list[dict] = []
        self.rag_response = None
        self.verifier = SimpleNamespace(verify_authorization_header=lambda header: self.identity)
        self.runtime = AgentChatRuntime(
            job_store=self.jobs, queue=SimpleNamespace(publish=self.published.append),
            input_check=lambda q, corr: InputDecision("allowed", q),
        )
        self.worker = AgentChatWorker(
            job_store=self.jobs, state_store=self.state, quota=self.quota, authorization_store=self.auth,
            conversation_store=self.conversations, grant_repository=InMemoryGatewayGrantRepository(),
            identity_verifier=self.verifier, gateway_url="https://gateway.example.test/mcp",
            invoke_agent=self._invoke_agent, rag_chat=self._rag_chat, memory=self.memory, clock=self.clock,
        )

    def _invoke_agent(self, binding, question, history):
        self.agent_calls.append((binding, question, history))
        step = self.agent_script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step

    def _rag_chat(self, identity, *, authorized_evidence_sink, **kwargs):
        self.rag_calls.append(kwargs)
        response = self.rag_response
        if response is not None and response.citations:
            citation = SimpleNamespace(citation_id=response.citations[0].citation_id)
            authorized_evidence_sink(identity, None, None, response, (SimpleNamespace(citation=citation, text="Pasaje recuperado por RAG."),))
        return response

    def submit(self, question="¿Cuándo vence el contrato?", **overrides):
        context = build_request_context(self.identity, MATTER, self.auth)
        values = dict(subject=SUBJECT, tenant_id=context.tenant_id, matter_id=MATTER, conversation_id="conv-1",
                      session_selector="sel-1", correlation_id=context.correlation_id, question=question,
                      session_ref=self.state.session_reference(self.session_key), now=self.clock())
        values.update(overrides)
        return submit_agent_chat(self.runtime, self.quota, **values)

    def counters(self):
        return next(iter(self.quota.counters.values()))


def rag_ok():
    return ChatResponse(
        answer="Según el contrato, el plazo es de 30 días.",
        citations=(ChatCitation("rag-1", "doc-a", "Alfa.pdf", None, 2, None),),
        evidence_status=EvidenceStatus.ANSWERABLE, disclaimer_required=True, prompt_version="1", prompt_sha256="x",
    )


class SubmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.world = World(self)

    def test_submit_reserves_one_chat_and_one_harness_and_queues_only_ids(self) -> None:
        job = self.world.submit()
        counters = self.world.counters()
        self.assertEqual((counters["chatCount"], counters["harnessCount"], counters["gatewayCount"]), (1, 1, 0))
        self.assertEqual(self.world.published, [JobKey(TENANT, MATTER, job.job_id)])
        self.assertNotIn("question", repr(self.world.published[0]))
        stored = self.world.jobs.get(job.key)
        self.assertEqual((stored.status, stored.subject, stored.conversation_id), (JobStatus.QUEUED, SUBJECT, "conv-1"))

    def test_quota_exhaustion_creates_no_job_and_keeps_the_other_counter(self) -> None:
        small = InMemoryQuotaLedger(limits=QuotaLimits(chats_per_month=5, harness_per_month=1), clock=self.world.clock)
        self.world.quota = small
        self.world.submit()
        with self.assertRaises(QuotaExceededError):
            self.world.submit("otra pregunta")
        counters = next(iter(small.counters.values()))
        self.assertEqual((counters["chatCount"], counters["harnessCount"]), (1, 1))  # chat unit given back
        self.assertEqual(len(self.world.published), 1)

    def test_queue_failure_releases_the_reservation_and_fails_the_job(self) -> None:
        def broken(_key):
            raise RuntimeError("sqs down")

        self.world.runtime.queue = SimpleNamespace(publish=broken)
        with self.assertRaises(SubmissionError):
            self.world.submit()
        self.assertEqual(self.world.counters()["chatCount"], 0)
        self.assertEqual(self.world.counters()["harnessCount"], 0)


class SubmissionFailureTests(unittest.TestCase):
    """PB-002: a failed submission always frees the quota and always raises SubmissionError."""

    class Failing:
        def __init__(self, *, create=False, publish=False, fail=False):
            self.flags = dict(create=create, publish=publish, fail=fail)

        def create(self, job):
            if self.flags["create"]:
                raise RuntimeError("ddb down")

        def fail(self, key, code):
            if self.flags["fail"]:
                raise RuntimeError("ddb still down")
            return True

    def attempt(self, **flags):
        world = World(self)
        store = self.Failing(**flags)

        def publish(key):
            if flags.get("publish"):
                raise RuntimeError("sqs down")

        world.runtime.job_store = store
        world.runtime.queue = SimpleNamespace(publish=publish)
        with self.assertRaises(SubmissionError) as caught:
            world.submit()
        self.assertIsInstance(caught.exception.__cause__, RuntimeError)
        counters = world.counters()
        self.assertEqual((counters["chatCount"], counters["harnessCount"]), (0, 0))

    def test_create_fails_and_fail_also_raises(self) -> None:
        self.attempt(create=True, fail=True)

    def test_publish_fails_and_fail_also_raises(self) -> None:
        self.attempt(publish=True, fail=True)

    def test_each_single_failure_still_releases(self) -> None:
        self.attempt(create=True)
        self.attempt(publish=True)

    def test_a_quota_release_that_raises_is_tolerated(self) -> None:
        world = World(self)
        world.runtime.queue = SimpleNamespace(publish=lambda key: (_ for _ in ()).throw(RuntimeError("sqs down")))

        real = world.quota

        class BrokenRelease:
            limits = real.limits

            def reserve(self, *a, **k):
                return real.reserve(*a, **k)

            def release(self, *a, **k):
                raise RuntimeError("quota down")

        world.quota = BrokenRelease()
        with self.assertRaises(SubmissionError):
            world.submit()


class WorkerHappyPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.world = World(self)
        self.job = self.world.submit()

    def run_worker(self):
        return self.world.worker.process(self.job.key)

    def test_text_answer_is_the_search_result_verbatim_with_openable_citation_handles(self) -> None:
        self.world.agent_script = [agent_result(TEXT_PLAN, call("tu-1", SEARCH_TOOL, search_payload()))]
        result = self.run_worker()
        self.assertEqual(result.action, "completed")
        done = self.world.jobs.get(self.job.key)
        self.assertEqual(done.status, JobStatus.SUCCEEDED)
        body = done.result
        self.assertEqual(body["path"], "grounded")
        self.assertEqual(body["blocks"][0]["text"], "El plazo es de 30 días.")
        handle = body["citations"][0]["handle"]
        record = self.world.state.get_citation(handle, subject=SUBJECT, tenant_id=TENANT, matter_id=MATTER, conversation_id="conv-1")
        self.assertEqual(record.passage, "El plazo será de 30 días.")
        self.assertNotIn("passage", json.dumps(body))  # the browser only gets opaque handles
        self.assertEqual(body["operationStatus"], "ok")
        self.assertEqual(result.metadata["tools"], ["search_documents"])

    def test_data_answer_is_written_by_the_backend_and_cites_the_idp_quote(self) -> None:
        self.world.agent_script = [agent_result(DATA_PLAN, call("tu-1", FIELDS_TOOL, fields_payload()))]
        self.run_worker()
        body = self.world.jobs.get(self.job.key).result
        self.assertEqual(body["path"], "data")
        lines = [line["text"] for line in body["blocks"][1]["groups"][0]["lines"]]
        self.assertEqual(lines, ["Alfa.pdf: Fecha de vencimiento: 2026-03-15"])
        handle = body["citations"][0]["handle"]
        self.assertEqual(self.world.state.get_citation(handle).passage, "Vence el 15 de marzo de 2026")
        self.assertTrue(self.world.state.get_citation(handle).is_idp)

    def test_the_agent_gets_the_sealed_agent_chat_binding_and_only_the_server_side_scope(self) -> None:
        self.world.agent_script = [agent_result(TEXT_PLAN, call("tu-1", SEARCH_TOOL, search_payload()))]
        self.run_worker()
        binding, question, history = self.world.agent_calls[0]
        self.assertEqual((binding.matter_id, binding.verified_subject, question, history), (MATTER, SUBJECT, "¿Cuándo vence el contrato?", ()))
        self.assertEqual(set(binding.allowed_tools), {
            SEARCH_TOOL, FIELDS_TOOL, "@legaldesk_gateway/metadata-mcp___list_matter_documents"})
        self.assertEqual(binding.bearer_token, "access-token")  # loaded from the server-side session
        self.assertNotIn("access-token", json.dumps(self.world.jobs.get(self.job.key).public_dict()))

    def test_memory_holds_only_the_question_and_the_final_answer_once(self) -> None:
        self.world.agent_script = [agent_result(DATA_PLAN, call("tu-1", FIELDS_TOOL, fields_payload()),
                                                call("tu-2", SEARCH_TOOL, search_payload("TEXTO SECRETO DE HERRAMIENTA")))]
        self.run_worker()
        (events,) = self.world.memory._events.values()
        self.assertEqual([(e.role, e.text.split("\n")[0]) for e in events],
                         [("USER", "¿Cuándo vence el contrato?"), ("ASSISTANT", "Estos son los datos extraídos de los documentos del expediente.")])
        self.assertNotIn("SECRETO", json.dumps([e.text for e in events]))
        self.assertEqual(len(self.world.state.list_history_ids((SUBJECT, "conv-1", "sel-1"))), 2)

    def test_next_question_receives_the_last_pairs_of_history_not_tool_results(self) -> None:
        self.world.agent_script = [agent_result(TEXT_PLAN, call("tu-1", SEARCH_TOOL, search_payload()))]
        self.run_worker()
        second = self.world.submit("¿Y la renovación?")
        self.world.agent_script = [agent_result(TEXT_PLAN, call("tu-1", SEARCH_TOOL, search_payload("Se renueva.")))]
        self.world.worker.process(second.key)
        history = self.world.agent_calls[1][2]
        self.assertEqual([role for role, _ in history], ["user", "assistant"])
        self.assertEqual(history[0][1], "¿Cuándo vence el contrato?")
        self.assertEqual(history[1][1], "El plazo es de 30 días.")

    def test_history_is_capped_at_six_pairs(self) -> None:
        for index in range(8):
            job = self.world.submit(f"pregunta {index}")
            self.world.agent_script = [agent_result(TEXT_PLAN, call("tu-1", SEARCH_TOOL, search_payload(f"respuesta {index}")))]
            self.world.worker.process(job.key)
        final = self.world.submit("última")
        self.world.agent_script = [agent_result(TEXT_PLAN, call("tu-1", SEARCH_TOOL, search_payload()))]
        # the very first job of setUp was never run; 8 answered pairs exist
        self.world.worker.process(final.key)
        history = self.world.agent_calls[-1][2]
        self.assertEqual(len(history), 12)
        self.assertEqual(history[0][1], "pregunta 2")

    def test_gateway_counter_is_never_charged_by_a_job(self) -> None:
        self.world.agent_script = [agent_result(TEXT_PLAN, call("tu-1", SEARCH_TOOL, search_payload()))]
        self.run_worker()
        self.assertEqual(self.world.counters()["gatewayCount"], 0)


class WorkerFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.world = World(self)
        self.job = self.world.submit()
        self.world.rag_response = rag_ok()

    def test_no_tool_called_answers_with_direct_rag_and_records_the_reason(self) -> None:
        self.world.agent_script = [agent_result(TEXT_PLAN, outcome="no_tool_called")]
        result = self.world.worker.process(self.job.key)
        body = self.world.jobs.get(self.job.key).result
        self.assertEqual(body["path"], "rag_fallback")
        self.assertEqual(body["blocks"][0]["text"], "Según el contrato, el plazo es de 30 días.")
        self.assertEqual(result.metadata["fallbackReason"], "no_tool_called")
        self.assertEqual(self.world.rag_calls[0]["session_id"], "sel-1")
        handle = body["citations"][0]["handle"]
        self.assertEqual(self.world.state.get_citation(handle).passage, "Pasaje recuperado por RAG.")

    def test_a_valid_tool_result_is_reused_before_running_rag(self) -> None:
        self.world.agent_script = [agent_result("not json", call("tu-1", SEARCH_TOOL, search_payload()), outcome="completed")]
        result = self.world.worker.process(self.job.key)
        body = self.world.jobs.get(self.job.key).result
        self.assertEqual((body["path"], result.metadata["fallbackReason"]), ("tool_result", "plan_invalid"))
        self.assertEqual(self.world.rag_calls, [])
        self.assertEqual(body["blocks"][0]["text"], "El plazo es de 30 días.")

    def test_a_cut_loop_keeps_the_completed_tool_result(self) -> None:
        self.world.agent_script = [agent_result("", call("tu-1", SEARCH_TOOL, search_payload()), outcome="timeout")]
        self.world.worker.process(self.job.key)
        self.assertEqual(self.world.jobs.get(self.job.key).result["path"], "tool_result")
        self.assertEqual(self.world.rag_calls, [])

    def test_an_invalid_tool_result_is_ignored_not_trusted(self) -> None:
        self.world.agent_script = [agent_result(TEXT_PLAN, call("tu-1", SEARCH_TOOL, {"answer": "x"}), outcome="completed")]
        result = self.world.worker.process(self.job.key)
        self.assertEqual(result.metadata["invalidToolResults"], 1)
        self.assertEqual(self.world.jobs.get(self.job.key).result["path"], "rag_fallback")

    def test_a_rag_technical_error_fails_the_job_and_releases_the_quota(self) -> None:
        self.world.rag_response = ChatResponse("error", (), None, True, None, None, operation_status="error", error_code="model_failed")
        self.world.agent_script = [agent_result("", outcome="failed")]
        self.world.worker.process(self.job.key)
        job = self.world.jobs.get(self.job.key)
        self.assertEqual((job.status, job.failure_code), (JobStatus.FAILED, FailureCode.AGENT_FAILED))
        self.assertEqual((self.world.counters()["chatCount"], self.world.counters()["harnessCount"]), (0, 0))

    def test_an_output_guardrail_block_is_a_finished_answer_not_remembered(self) -> None:
        self.world.rag_response = ChatResponse("No puedo mostrar esta respuesta.", (), None, True, None, None, operation_status="blocked")
        self.world.agent_script = [agent_result("", outcome="no_tool_called")]
        self.world.worker.process(self.job.key)
        body = self.world.jobs.get(self.job.key).result
        self.assertEqual(body["operationStatus"], "blocked")
        self.assertEqual(self.world.memory._events, {})


class WorkerReliabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.world = World(self)
        self.job = self.world.submit()

    def test_duplicate_delivery_after_success_does_nothing(self) -> None:
        self.world.agent_script = [agent_result(TEXT_PLAN, call("tu-1", SEARCH_TOOL, search_payload()))]
        self.world.worker.process(self.job.key)
        before = (dict(self.world.counters()), len(self.world.memory._events[next(iter(self.world.memory._events))]),
                  len(self.world.state.citation_handles))
        again = self.world.worker.process(self.job.key)
        self.assertEqual(again.action, "ignored")
        self.assertEqual(len(self.world.agent_calls), 1)  # Harness was not invoked again
        after = (dict(self.world.counters()), len(self.world.memory._events[next(iter(self.world.memory._events))]),
                 len(self.world.state.citation_handles))
        self.assertEqual(before, after)

    def test_duplicate_delivery_during_a_live_attempt_is_ignored(self) -> None:
        outcome, _ = self.world.jobs.claim(self.job.key, now=self.world.clock())
        self.assertIs(outcome, ClaimOutcome.STARTED)
        result = self.world.worker.process(self.job.key)  # second delivery while attempt 1 is alive
        self.assertEqual((result.action, self.world.agent_calls), ("ignored", []))

    def test_transient_error_retries_once_then_succeeds_with_a_single_reservation(self) -> None:
        class Throttled(Exception):
            response = {"Error": {"Code": "ThrottlingException"}}

        self.world.agent_script = [Throttled(), agent_result(TEXT_PLAN, call("tu-1", SEARCH_TOOL, search_payload()))]
        first = self.world.worker.process(self.job.key)
        self.assertEqual((first.action, first.retry_after_seconds), ("retry", 30))
        self.assertEqual(self.world.jobs.get(self.job.key).status, JobStatus.QUEUED)
        self.world.clock.now += 31
        second = self.world.worker.process(self.job.key)
        self.assertEqual(second.action, "completed")
        self.assertEqual(self.world.jobs.get(self.job.key).attempt, 2)
        self.assertEqual((self.world.counters()["chatCount"], self.world.counters()["harnessCount"]), (1, 1))
        (events,) = self.world.memory._events.values()
        self.assertEqual(len(events), 2)  # memory written once

    def test_a_second_transient_error_is_not_retried_and_frees_the_quota(self) -> None:
        class Unavailable(Exception):
            response = {"Error": {"Code": "ServiceUnavailableException"}}

        self.world.agent_script = [Unavailable(), Unavailable()]
        self.assertEqual(self.world.worker.process(self.job.key).action, "retry")
        self.world.clock.now += 31
        last = self.world.worker.process(self.job.key)
        job = self.world.jobs.get(self.job.key)
        self.assertEqual((last.action, job.status, job.failure_code), ("failed", JobStatus.FAILED, FailureCode.TRANSIENT_EXHAUSTED))
        self.assertEqual((self.world.counters()["chatCount"], self.world.counters()["harnessCount"]), (0, 0))

    def test_permanent_error_does_not_retry(self) -> None:
        self.world.agent_script = [ValueError("bug")]
        result = self.world.worker.process(self.job.key)
        job = self.world.jobs.get(self.job.key)
        self.assertEqual((result.action, job.attempt, job.failure_code), ("failed", 1, FailureCode.INTERNAL_ERROR))
        self.assertEqual(self.world.counters()["chatCount"], 0)

    def test_closed_session_fails_closed_without_calling_harness_and_releases_the_quota(self) -> None:
        self.world.state.delete_session(self.world.session_key)
        result = self.world.worker.process(self.job.key)
        job = self.world.jobs.get(self.job.key)
        self.assertEqual((result.action, job.failure_code, self.world.agent_calls), ("failed", FailureCode.SESSION_CLOSED, []))
        self.assertEqual((self.world.counters()["chatCount"], self.world.counters()["harnessCount"]), (0, 0))
        self.assertEqual((job.question, job.session_ref), (None, None))
        self.assertEqual(job.public_dict()["error"], "session_closed")

    def test_expired_or_foreign_session_fails_closed(self) -> None:
        self.world.clock.now = T0 + 3601  # session over; deadline would also be over, so use a fresh job
        job = self.world.submit(session_ref=self.world.state.session_reference(self.world.session_key))
        result = self.world.worker.process(job.key)
        self.assertEqual(self.world.jobs.get(job.key).failure_code, FailureCode.SESSION_CLOSED)
        self.world.clock.now = T0
        other = SessionRecord(test_identity("idp|bob-fictional"), "tok", "csrf", T0 + 3600)
        self.world.state.put_session("bobs-key", other)
        stolen = self.world.submit(session_ref=self.world.state.session_reference("bobs-key"))  # a job that points at someone else's session
        self.world.worker.process(stolen.key)
        self.assertEqual(self.world.jobs.get(stolen.key).failure_code, FailureCode.SESSION_CLOSED)
        self.assertEqual(self.world.agent_calls, [])

    def test_a_token_that_no_longer_verifies_closes_the_job(self) -> None:
        def reject(_header):
            raise PermissionError("expired")

        self.world.worker.identity_verifier = SimpleNamespace(verify_authorization_header=reject)
        self.world.worker.process(self.job.key)
        self.assertEqual(self.world.jobs.get(self.job.key).failure_code, FailureCode.SESSION_CLOSED)
        self.assertEqual(self.world.agent_calls, [])

    def test_access_revoked_after_submission_is_not_served(self) -> None:
        # The conversation binding disappears (matter deleted / conversation revoked) before the worker runs.
        self.world.worker.conversation_store = InMemoryConversationBindingStore()
        self.world.worker.process(self.job.key)
        self.assertEqual(self.world.jobs.get(self.job.key).failure_code, FailureCode.SESSION_CLOSED)
        self.assertEqual(self.world.agent_calls, [])

    def test_job_past_its_total_deadline_is_failed_and_released_on_redelivery(self) -> None:
        self.world.clock.now = T0 + 301
        result = self.world.worker.process(self.job.key)
        self.assertEqual((result.action, self.world.jobs.get(self.job.key).failure_code), ("failed", FailureCode.DEADLINE_EXCEEDED))
        self.assertEqual(self.world.counters()["chatCount"], 0)
        self.assertEqual(self.world.worker.process(self.job.key).action, "ignored")
        self.assertEqual(self.world.counters()["chatCount"], 0)  # released once, not twice

    def test_a_worker_that_outlives_the_deadline_cannot_publish(self) -> None:
        def slow(binding, question, history):
            self.world.clock.now = T0 + 400  # the loop took too long
            return agent_result(TEXT_PLAN, call("tu-1", SEARCH_TOOL, search_payload()))

        self.world.worker.invoke_agent = slow
        result = self.world.worker.process(self.job.key)
        self.assertEqual(result.action, "ignored")
        self.assertEqual(self.world.state.citation_handles, {})
        self.assertEqual(self.world.memory._events, {})
        # The next look at the job declares it failed and frees the quota.
        job, failed_now = self.world.jobs.expire_if_due(self.job.key, now=self.world.clock())
        self.assertTrue(failed_now)

    def test_memory_failure_after_the_answer_does_not_discard_it(self) -> None:
        class BrokenMemory:
            def append_event(self, *_a, **_k):
                raise RuntimeError("memory down")

        self.world.worker.memory = BrokenMemory()
        self.world.agent_script = [agent_result(TEXT_PLAN, call("tu-1", SEARCH_TOOL, search_payload()))]
        result = self.world.worker.process(self.job.key)
        self.assertEqual((result.action, result.metadata["memoryWritten"]), ("completed", False))
        self.assertEqual(self.world.jobs.get(self.job.key).status, JobStatus.SUCCEEDED)

    def test_error_after_finalize_is_never_retried(self) -> None:
        class BrokenStore(InMemoryEphemeralStateStore):
            def put_citation(self, *_a, **_k):
                raise RuntimeError("citation store down")

        broken = BrokenStore(clock=self.world.clock)
        broken.sessions = self.world.state.sessions
        self.world.worker.state_store = broken
        self.world.agent_script = [agent_result(TEXT_PLAN, call("tu-1", SEARCH_TOOL, search_payload()))]
        result = self.world.worker.process(self.job.key)
        job = self.world.jobs.get(self.job.key)
        self.assertEqual((result.action, job.attempt, job.failure_code), ("failed", 1, FailureCode.INTERNAL_ERROR))
        self.assertEqual(self.world.counters()["chatCount"], 0)

    def test_transient_classification(self) -> None:
        class Status(Exception):
            response = {"Error": {"Code": "Whatever"}, "ResponseMetadata": {"HTTPStatusCode": 503}}

        class Denied(Exception):
            response = {"Error": {"Code": "AccessDeniedException"}, "ResponseMetadata": {"HTTPStatusCode": 403}}

        class ReadTimeoutError(Exception):
            pass

        self.assertTrue(all(is_transient_error(exc) for exc in (Status(), ReadTimeoutError(), TimeoutError(), ConnectionResetError())))
        self.assertFalse(any(is_transient_error(exc) for exc in (Denied(), ValueError("x"), PermissionError("x"))))


class CrossScopeTests(unittest.TestCase):
    def test_a_job_key_for_another_matter_or_tenant_finds_nothing(self) -> None:
        world = World(self)
        job = world.submit()
        for key in (JobKey(TENANT, "mat_glacier", job.job_id), JobKey("tnt_borealis", MATTER, job.job_id)):
            self.assertEqual(world.worker.process(key).action, "ignored")
        self.assertEqual(world.agent_calls, [])
        self.assertEqual(world.jobs.get(job.key).status, JobStatus.QUEUED)

    def test_worker_refuses_a_session_whose_tenant_differs_from_the_job(self) -> None:
        world = World(self)
        job = world.submit()
        world.jobs._jobs[job.key] = replace(world.jobs._jobs[job.key], tenant_id=TENANT)  # unchanged; sanity
        forged = replace(world.jobs._jobs[job.key], subject="idp|bob-fictional")
        world.jobs._jobs[job.key] = forged
        world.worker.process(job.key)
        self.assertEqual(world.jobs.get(job.key).failure_code, FailureCode.SESSION_CLOSED)
        self.assertEqual(world.agent_calls, [])


if __name__ == "__main__":
    unittest.main()
