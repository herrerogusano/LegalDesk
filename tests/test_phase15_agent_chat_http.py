"""Phase 15 (T4.1, T4.4, T5): POST /api/chat agentic path and GET chat job, through the WSGI app."""

from __future__ import annotations

import io
import json
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))
sys.path.insert(0, str(ROOT / "agent" / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from fixture_loader import test_identity
from legaldesk.agent_chat import AgentChatRuntime, AgentChatWorker, InputDecision
from legaldesk.agent_jobs import FailureCode, InMemoryChatJobStore, JobKey, JobStatus
from legaldesk.authorization import InMemoryAuthorizationStore
from legaldesk.chat import ChatResponse
from legaldesk.gateway_interceptor import InMemoryGatewayGrantRepository
from legaldesk.documents import DocumentPipeline, InMemoryDocumentMetadataRepository, InMemoryObjectStorage
from legaldesk.domain.models import Matter, User
from legaldesk.evidence import EvidenceStatus
from legaldesk.http_app import SESSION_COOKIE, ApplicationComposition, create_http_app
from legaldesk.mcp_server import MCPServer
from legaldesk.memory import InMemoryConversationBindingStore
from legaldesk.quota import InMemoryQuotaLedger, QuotaLimits
from legaldesk.state import InMemoryEphemeralStateStore, SessionRecord
from test_phase15_agent_chat_worker import (
    DATA_PLAN, FIELDS_TOOL, SEARCH_TOOL, TEXT_PLAN, agent_result, call, fields_payload, search_payload,
)

ALICE, CAROL, BOB = "subject-alice", "subject-carol", "subject-bob"
QUESTION = "¿Qué documentos hay y cuándo vencen?"


def auth_store() -> InMemoryAuthorizationStore:
    users = [
        User("usr_alice", ALICE, frozenset({"tnt_a"}), frozenset({"member"})),
        User("usr_carol", CAROL, frozenset({"tnt_a"}), frozenset({"member"})),
        User("usr_bob", BOB, frozenset({"tnt_b"}), frozenset({"member"})),
    ]
    matters = [
        Matter("mat_1", "tnt_a", "One", frozenset({"usr_alice", "usr_carol"})),
        Matter("mat_2", "tnt_a", "Two", frozenset({"usr_alice"})),
        Matter("mat_b", "tnt_b", "Bee", frozenset({"usr_bob"})),
    ]
    return InMemoryAuthorizationStore(
        users_by_subject={user.verified_subject: user for user in users},
        matters_by_id={matter.matter_id: matter for matter in matters},
    )


class Harness:
    def __init__(self, *, with_agent: bool = True, input_kind: str = "allowed") -> None:
        self.auth = auth_store()
        self.state = InMemoryEphemeralStateStore()
        self.jobs = InMemoryChatJobStore()
        self.published: list[JobKey] = []
        self.input_calls: list[tuple[str, str]] = []
        self.sync_calls: list[dict] = []
        self.quota = InMemoryQuotaLedger(limits=QuotaLimits(chats_per_month=2, harness_per_month=2, gateway_per_month=2))
        self.input_kind = input_kind
        self.review_calls: list[dict] = []

        def gateway_invoke(binding, *, tool_name, arguments):
            self.review_calls.append({"tool": tool_name, "arguments": dict(arguments)})
            return {"status": "SUCCESS", "tool": f"@legaldesk_gateway/review-task-lambda___{tool_name}",
                    "result": {"reviewTaskId": "rt-1", "status": "open"}, "correlationId": binding.correlation_id}
        metadata = InMemoryDocumentMetadataRepository()
        storage = InMemoryObjectStorage()
        identities = {f"tok-{name}": test_identity(subject) for name, subject in (("alice", ALICE), ("carol", CAROL), ("bob", BOB))}
        verifier = SimpleNamespace(verify_authorization_header=lambda header: identities[header.removeprefix("Bearer ")])

        def check(question: str, correlation_id: str) -> InputDecision:
            self.input_calls.append((question, correlation_id))
            return InputDecision(self.input_kind, question)

        def chat_service(identity, **kwargs):
            self.sync_calls.append(kwargs)
            return ChatResponse("respuesta síncrona", (), EvidenceStatus.INSUFFICIENT_EVIDENCE, True, None, None)

        self.composition = ApplicationComposition(
            identity_verifier=verifier, token_exchange=None, authorization_store=self.auth,
            conversation_store=InMemoryConversationBindingStore(),
            document_pipeline=DocumentPipeline(self.auth, storage, metadata), object_storage=storage,
            metadata_repository=metadata, mcp_server=MCPServer(metadata),
            matter_catalog=("mat_1", "mat_2", "mat_b"), state_store=self.state, quota=self.quota,
            chat_service=chat_service, gateway_invoker=SimpleNamespace(invoke=gateway_invoke),
            gateway_grant_repository=InMemoryGatewayGrantRepository(),
            agent_chat=AgentChatRuntime(self.jobs, SimpleNamespace(publish=self.published.append), check) if with_agent else None,
        )
        self.app = create_http_app(self.composition)

    def login(self, name: str, *, lifetime: float = 3600, key: str | None = None) -> dict[str, str]:
        key = key or f"cookie-{name}"
        self.state.put_session(key, SessionRecord(test_identity({"alice": ALICE, "carol": CAROL, "bob": BOB}[name]),
                                                  f"tok-{name}", f"csrf-{name}", time.time() + lifetime))
        return {"cookie": f"{SESSION_COOKIE}={key}", "csrf": f"csrf-{name}"}

    def call(self, method: str, path: str, body: dict | None = None, *, who: dict[str, str]) -> tuple[int, dict]:
        raw = json.dumps(body or {}).encode()
        environ = {
            "REQUEST_METHOD": method, "PATH_INFO": path, "HTTP_HOST": "localhost", "HTTP_COOKIE": who["cookie"],
            "HTTP_X_CSRF_TOKEN": who["csrf"], "CONTENT_LENGTH": str(len(raw)), "wsgi.input": io.BytesIO(raw),
            "QUERY_STRING": "",
        }
        captured: dict = {}
        payload = b"".join(self.app(environ, lambda status, headers: captured.update(status=status)))
        return int(captured["status"][:3]), json.loads(payload or b"{}")

    def conversation(self, matter: str, who: dict[str, str]) -> dict:
        status, body = self.call("POST", "/api/conversations", {"matterId": matter}, who=who)
        assert status == 201, body
        return body

    def ask(self, who, matter="mat_1", question=QUESTION, **extra):
        conv = self.conversation(matter, who)
        return self.call("POST", "/api/chat", {"question": question, "matterId": matter, "conversationId": conv["conversationId"],
                                               "sessionId": conv["sessionId"], **extra}, who=who), conv

    def counters(self):
        return next(iter(self.quota.counters.values()), {"chatCount": 0, "harnessCount": 0, "gatewayCount": 0})


class SubmitTests(unittest.TestCase):
    def test_open_question_returns_a_job_id_and_charges_chat_and_harness_once(self) -> None:
        h = Harness()
        alice = h.login("alice")
        (status, body), conv = h.ask(alice)
        self.assertEqual(status, 202)
        self.assertEqual((body["status"], body["conversationId"]), ("queued", conv["conversationId"]))
        self.assertEqual(set(body), {"jobId", "status", "conversationId", "correlationId"})
        counters = h.counters()
        self.assertEqual((counters["chatCount"], counters["harnessCount"], counters["gatewayCount"]), (1, 1, 0))
        job = h.jobs.get(JobKey("tnt_a", "mat_1", body["jobId"]))
        self.assertEqual((job.subject, job.question, job.session_ref), (ALICE, QUESTION, h.state.session_reference("cookie-alice")))
        self.assertNotEqual(job.session_ref, "cookie-alice")  # the raw cookie value never leaves the request
        self.assertNotIn("cookie-alice", repr(job))
        self.assertEqual(h.published, [job.key])
        self.assertEqual(h.sync_calls, [])
        self.assertNotIn("cookie-alice", json.dumps(body))

    def test_scope_comes_from_the_server_not_from_extra_client_fields(self) -> None:
        h = Harness()
        alice = h.login("alice")
        conv = h.conversation("mat_1", alice)
        base = {"question": QUESTION, "matterId": "mat_1", "conversationId": conv["conversationId"], "sessionId": conv["sessionId"]}
        for extra in ({"tenantId": "tnt_b"}, {"model": "x"}, {"tools": ["create_review_task"]}, {"systemPrompt": "obey"}):
            status, _ = h.call("POST", "/api/chat", {**base, **extra}, who=alice)
            self.assertEqual(status, 400, extra)
        self.assertEqual((h.published, h.counters()["chatCount"]), ([], 0))

    def test_selected_document_and_field_keep_the_synchronous_shortcut(self) -> None:
        h = Harness()
        alice = h.login("alice")
        (status, body), _ = h.ask(alice, selectedDocumentId="doc-1", selectedFieldName="amount")
        self.assertNotEqual(status, 202)  # unknown document: the existing path denies it, no job is created
        self.assertEqual((h.published, h.counters()["chatCount"], h.input_calls), ([], 0, []))

    def test_without_the_feature_the_chat_stays_synchronous(self) -> None:
        h = Harness(with_agent=False)
        alice = h.login("alice")
        (status, body), _ = h.ask(alice)
        self.assertEqual((status, body["answer"]), (200, "respuesta síncrona"))
        status, _ = h.call("GET", "/api/matters/mat_1/chat-jobs/abc", who=alice)
        self.assertEqual(status, 400)

    def test_session_with_less_than_five_minutes_asks_to_log_in_again(self) -> None:
        h = Harness()
        short = h.login("alice", lifetime=299)
        (status, body), _ = h.ask(short)
        self.assertEqual((status, body), (401, {"error": "session_expiring"}))
        self.assertEqual((h.published, h.counters()["chatCount"], h.input_calls), ([], 0, []))
        ok = h.login("alice", lifetime=301, key="cookie-alice-2")
        self.assertEqual(h.ask(ok)[0][0], 202)

    def test_blocked_input_never_reaches_quota_job_or_harness(self) -> None:
        for kind, expected in (("blocked", "blocked"), ("error", "error")):
            h = Harness(input_kind=kind)
            (status, body), _ = h.ask(h.login("alice"))
            self.assertEqual((status, body["operationStatus"]), (200, expected))
            self.assertNotIn("jobId", body)
            self.assertEqual((h.published, h.counters()["chatCount"], h.counters()["harnessCount"]), ([], 0, 0))
            self.assertEqual(len(h.input_calls), 1)

    def test_quota_is_checked_at_submission_and_fails_closed(self) -> None:
        h = Harness()
        alice = h.login("alice")
        self.assertEqual(h.ask(alice)[0][0], 202)
        self.assertEqual(h.ask(alice)[0][0], 202)
        (status, body), _ = h.ask(alice)
        self.assertEqual((status, body), (429, {"error": "quota_exceeded"}))
        self.assertEqual(len(h.published), 2)

    def test_conversation_of_another_user_is_denied_before_anything_is_reserved(self) -> None:
        h = Harness()
        alice, carol = h.login("alice"), h.login("carol")
        conv = h.conversation("mat_1", alice)
        status, _ = h.call("POST", "/api/chat", {"question": QUESTION, "matterId": "mat_1", "conversationId": conv["conversationId"],
                                                 "sessionId": conv["sessionId"]}, who=carol)
        self.assertEqual(status, 403)
        self.assertEqual((h.published, h.counters()["chatCount"], h.input_calls), ([], 0, []))

    def test_matter_the_user_cannot_access_is_denied(self) -> None:
        h = Harness()
        carol = h.login("carol")
        conv = h.conversation("mat_1", carol)
        status, _ = h.call("POST", "/api/chat", {"question": QUESTION, "matterId": "mat_2", "conversationId": conv["conversationId"],
                                                 "sessionId": conv["sessionId"]}, who=carol)
        self.assertEqual((status, h.published), (403, []))


class ReviewAfterJobTests(unittest.TestCase):
    """The existing POST /reviews flow works for the text of an agent answer, and only for it."""

    def setUp(self) -> None:
        self.h = Harness()
        self.alice, self.carol = self.h.login("alice"), self.h.login("carol")
        self.worker = AgentChatWorker(
            job_store=self.h.jobs, state_store=self.h.state, quota=self.h.quota, authorization_store=self.h.auth,
            conversation_store=self.h.composition.conversation_store, grant_repository=InMemoryGatewayGrantRepository(),
            identity_verifier=self.h.composition.identity_verifier, gateway_url="https://gateway.example.test/mcp",
            invoke_agent=lambda *_args: self.script.pop(0), rag_chat=None,
        )

    def answered(self, plan, *calls, who=None):
        who = who or self.alice
        self.script = [agent_result(plan, *calls)]
        (status, body), conv = self.h.ask(who)
        self.assertEqual(status, 202)
        self.assertEqual(self.worker.process(self.h.published[-1]).action, "completed")
        return body, conv

    def review(self, body, conv, *, who=None, correlation=None):
        status, response = self.h.call("POST", "/api/matters/mat_1/reviews", {
            "conversationId": conv["conversationId"], "sessionId": conv["sessionId"], "reasonCode": "user_requested_review",
            "dueAt": time.strftime("%Y-%m-%d", time.gmtime(time.time() + 86400 * 3)), "note": "",
            "originCorrelationId": correlation or body["correlationId"], "idempotencyKey": f"{body['jobId']}:review",
        }, who=who or self.alice)
        return status, response

    def test_text_answer_can_be_saved_for_review_with_the_server_side_snapshot(self) -> None:
        body, conv = self.answered(TEXT_PLAN, call("tu-1", SEARCH_TOOL, search_payload()))
        status, created = self.review(body, conv)
        self.assertEqual((status, created["reviewTaskId"]), (201, "rt-1"))
        (gateway,) = self.h.review_calls
        snapshot = gateway["arguments"]["snapshot"]
        self.assertEqual((snapshot["question"], snapshot["answer"], snapshot["evidenceStatus"]), (QUESTION, "El plazo es de 30 días.", "answerable"))
        self.assertEqual(snapshot["citations"][0]["passage"], "El plazo será de 30 días.")

    def test_data_only_answer_cannot_be_saved_for_review(self) -> None:
        body, conv = self.answered(DATA_PLAN, call("tu-1", FIELDS_TOOL, fields_payload()))
        status, _ = self.review(body, conv)
        self.assertGreaterEqual(status, 400)
        self.assertEqual(self.h.review_calls, [])

    def test_a_job_of_another_subject_or_conversation_cannot_be_reviewed(self) -> None:
        body, conv = self.answered(TEXT_PLAN, call("tu-1", SEARCH_TOOL, search_payload()))
        # carol is a member of the same matter but neither owns the conversation nor the correlation
        self.assertEqual(self.review(body, conv, who=self.carol)[0], 403)
        carol_conv = self.h.conversation("mat_1", self.carol)
        status, _ = self.h.call("POST", "/api/matters/mat_1/reviews", {
            "conversationId": carol_conv["conversationId"], "sessionId": carol_conv["sessionId"],
            "reasonCode": "user_requested_review", "dueAt": time.strftime("%Y-%m-%d", time.gmtime(time.time() + 86400 * 3)),
            "originCorrelationId": body["correlationId"],
        }, who=self.carol)
        self.assertEqual(status, 403)
        # the same user from another conversation of the matter
        other = self.h.conversation("mat_1", self.alice)
        status, _ = self.h.call("POST", "/api/matters/mat_1/reviews", {
            "conversationId": other["conversationId"], "sessionId": other["sessionId"],
            "reasonCode": "user_requested_review", "dueAt": time.strftime("%Y-%m-%d", time.gmtime(time.time() + 86400 * 3)),
            "originCorrelationId": body["correlationId"],
        }, who=self.alice)
        self.assertEqual(status, 403)
        self.assertEqual(self.h.review_calls, [])


class JobReadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness()
        self.alice, self.carol, self.bob = self.h.login("alice"), self.h.login("carol"), self.h.login("bob")
        (_, body), self.conv = self.h.ask(self.alice)
        self.job_id = body["jobId"]
        self.path = f"/api/matters/mat_1/chat-jobs/{self.job_id}"

    def test_owner_reads_the_job_state(self) -> None:
        status, body = self.h.call("GET", self.path, who=self.alice)
        self.assertEqual((status, body["status"], body["jobId"]), (200, "queued", self.job_id))
        self.assertNotIn("result", body)

    def test_result_is_returned_once_the_job_succeeded(self) -> None:
        key = JobKey("tnt_a", "mat_1", self.job_id)
        self.h.jobs.claim(key, now=time.time())
        self.h.jobs.finalize(key, attempt=1, now=time.time())
        self.h.jobs.complete(key, attempt=1, result={"path": "grounded", "blocks": [], "citations": []})
        status, body = self.h.call("GET", self.path, who=self.alice)
        self.assertEqual((status, body["status"], body["result"]["path"]), (200, "succeeded", "grounded"))

    def test_another_subject_of_the_same_matter_cannot_read_it(self) -> None:
        status, body = self.h.call("GET", self.path, who=self.carol)  # carol is a member of mat_1
        self.assertEqual((status, body), (403, {"error": "access_denied"}))

    def test_cross_matter_and_cross_tenant_reads_are_denied(self) -> None:
        for who, matter in ((self.alice, "mat_2"), (self.alice, "mat_b"), (self.bob, "mat_b"), (self.bob, "mat_1")):
            status, _ = self.h.call("GET", f"/api/matters/{matter}/chat-jobs/{self.job_id}", who=who)
            self.assertEqual(status, 403, (who["csrf"], matter))

    def test_unknown_job_is_indistinguishable_from_a_foreign_one(self) -> None:
        status, body = self.h.call("GET", "/api/matters/mat_1/chat-jobs/00000000-0000-4000-8000-000000000000", who=self.alice)
        self.assertEqual((status, body), (403, {"error": "access_denied"}))

    def test_malformed_job_id_is_rejected(self) -> None:
        status, _ = self.h.call("GET", "/api/matters/mat_1/chat-jobs/..%2Fx", who=self.alice)
        self.assertEqual(status, 400)

    def test_job_is_not_served_if_the_conversation_is_no_longer_bound(self) -> None:
        self.h.composition.conversation_store = InMemoryConversationBindingStore()
        status, _ = self.h.call("GET", self.path, who=self.alice)
        self.assertEqual(status, 403)

    def test_a_job_past_its_deadline_is_failed_when_read_and_gives_the_quota_back_once(self) -> None:
        key = JobKey("tnt_a", "mat_1", self.job_id)
        stored = self.h.jobs._jobs[key]
        from dataclasses import replace
        self.h.jobs._jobs[key] = replace(stored, deadline=int(time.time()) - 1)
        self.assertEqual((self.h.counters()["chatCount"], self.h.counters()["harnessCount"]), (1, 1))
        for _ in range(2):
            status, body = self.h.call("GET", self.path, who=self.alice)
            self.assertEqual((status, body["status"], body["error"]), (200, "failed", "deadline_exceeded"))
            self.assertEqual((self.h.counters()["chatCount"], self.h.counters()["harnessCount"]), (0, 0))
        self.assertEqual(self.h.jobs.get(key).failure_code, FailureCode.DEADLINE_EXCEEDED)
        self.assertIsNone(self.h.jobs.get(key).question)

    def test_a_late_reader_who_is_not_the_owner_cannot_expire_the_job(self) -> None:
        key = JobKey("tnt_a", "mat_1", self.job_id)
        from dataclasses import replace
        self.h.jobs._jobs[key] = replace(self.h.jobs._jobs[key], deadline=int(time.time()) - 1)
        self.assertEqual(self.h.call("GET", self.path, who=self.carol)[0], 403)
        self.assertEqual(self.h.jobs.get(key).status, JobStatus.QUEUED)
        self.assertEqual(self.h.counters()["chatCount"], 1)


if __name__ == "__main__":
    unittest.main()
