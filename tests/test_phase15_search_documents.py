"""Phase 15 T2: search_documents Lambda target (grounded pipeline behind a grant)."""

from __future__ import annotations

import copy
import dataclasses
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))
sys.path.insert(0, str(ROOT / "agent" / "src"))

from fixture_loader import load_authorization_store
from legaldesk import agent_contracts as contracts
from legaldesk import search_documents_lambda as lambda_module
from legaldesk.documents import InMemoryDocumentMetadataRepository
from legaldesk.domain.models import Document, DocumentStatus, MalwareScanStatus
from legaldesk.gateway_interceptor import GatewayAuthorizationGrant, InMemoryGatewayGrantRepository
from legaldesk.guardrails import GuardrailConfig
from legaldesk.prompts import FileSystemSystemPromptProvider
from test_chat import (
    FakeAnswerWriter,
    FakeEvidenceResolver,
    FakeGroundingValidator,
    FakeGuardrailClient,
    FakeKnowledgeBaseClient,
    result,
)

SUBJECT = "idp|alice-fictional"
CORRELATION = "8ec5d1c5-7b58-4bc2-a183-8fd48a3bd279"
GRANT_ID = "9ec5d1c5-7b58-4bc2-a183-8fd48a3bd279"
PASSAGE = "The deadline is 17 days."


class SearchDocumentsLambdaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.grants = InMemoryGatewayGrantRepository()
        self.put_grant()
        self.retrieval = FakeKnowledgeBaseClient([result("tnt_aurora", "mat_sundial", "doc-one", PASSAGE, document_name="A.pdf")])
        self.guardrail = FakeGuardrailClient()
        self.writer = FakeAnswerWriter("El plazo documentado es de 17 días.")
        self.metadata = InMemoryDocumentMetadataRepository()
        self.metadata.save(Document(
            document_id="doc-one", matter_id="mat_sundial", tenant_id="tnt_aurora", name="A.pdf",
            s3_key="tnt_aurora/mat_sundial/doc-one.pdf", media_type="application/pdf", jurisdiction="synthetic",
            document_date="2024-01-01", confidentiality="internal", status=DocumentStatus.INDEXED,
            file_size_bytes=10, malware_scan_status=MalwareScanStatus.CLEAN,
        ))
        self.service = lambda_module.SearchDocumentsService(
            authorization_store=load_authorization_store(),
            retrieval_client=self.retrieval,
            knowledge_base_id="kb-fictional",
            guardrail_client=self.guardrail,
            guardrail_config=GuardrailConfig("guardrail-fictional", "1"),
            evidence_resolver=FakeEvidenceResolver(
                {"coverage": "complete", "conflict": False, "supportingCitationIds": ["citation-1"]}
            ),
            answer_writer=self.writer,
            grounding_validator=FakeGroundingValidator(
                {"grounded": True, "score": 0.95, "matchedCitationIds": ["citation-1"]}
            ),
            prompt_provider=FileSystemSystemPromptProvider(),
            metadata_repository=self.metadata,
            object_storage=None,
        )

    def put_grant(self, *, grant_id=GRANT_ID, subject=SUBJECT, matter="mat_sundial", tool="search_documents",
                  expires_at=None) -> None:
        self.grants.put(GatewayAuthorizationGrant(
            grant_id=grant_id, verified_subject=subject, requested_matter_id=matter, correlation_id=CORRELATION,
            tool_name=tool, expires_at=int(time.time()) + 120 if expires_at is None else expires_at,
        ))

    def call(self, event: dict[str, object]):
        with patch.object(lambda_module, "_service_from_environment", return_value=(self.service, self.grants)):
            return lambda_module.search_documents_lambda_handler(event, None)

    def event(self, **overrides) -> dict[str, object]:
        base = {"question": "¿Cuál es el plazo?", "matterId": "mat_sundial", "_legaldeskGrantId": GRANT_ID}
        base.update(overrides)
        return base

    def test_returns_the_grounded_answer_with_citations_and_passages(self) -> None:
        output = self.call(self.event())
        parsed = contracts.parse_search_documents_result(output)
        self.assertEqual(parsed.answer, "El plazo documentado es de 17 días.")
        self.assertEqual(parsed.evidence_status, "answerable")
        self.assertEqual(
            [(c.citation_id, c.document_id, c.document_name, c.page_number, c.passage) for c in parsed.citations],
            [("citation-1", "doc-one", "A.pdf", 4, PASSAGE)],
        )
        # The pipeline ran as in the direct path: input and grounding guardrails, one retrieval.
        self.assertEqual(len(self.retrieval.calls), 1)
        self.assertGreaterEqual(len(self.guardrail.calls), 1)

    def test_writes_no_memory_state_or_grants(self) -> None:
        def snapshot():
            return copy.deepcopy({name: getattr(self.grants, name) for name in self.grants.__slots__})

        before = snapshot()
        self.call(self.event())
        self.assertEqual(snapshot(), before)
        service_fields = {name for name in lambda_module.SearchDocumentsService.__slots__}
        self.assertFalse({name for name in service_fields if "memory" in name or "state" in name or "conversation" in name})

    def test_scope_is_the_grants_not_the_models(self) -> None:
        self.call(self.event(matterId="mat_glacier"))
        retrieval_filter = str(self.retrieval.calls[0])
        self.assertIn("mat_sundial", retrieval_filter)
        self.assertNotIn("mat_glacier", retrieval_filter)
        self.assertNotIn("tnt_borealis", retrieval_filter)

    def test_passages_of_another_matter_never_reach_the_result(self) -> None:
        self.retrieval.results = [result("tnt_borealis", "mat_glacier", "doc-foreign", "SECRET-OTHER-MATTER")]
        output = self.call(self.event())
        self.assertNotIn("SECRET-OTHER-MATTER", str(output))
        self.assertEqual(output["citations"], [])
        self.assertEqual(output["evidenceStatus"], "insufficient_evidence")

    def test_grant_for_a_matter_the_subject_cannot_access_is_denied_before_any_retrieval(self) -> None:
        self.put_grant(grant_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", matter="mat_glacier")
        output = self.call(self.event(**{"_legaldeskGrantId": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"}))
        self.assertEqual(output, {"error": "access_denied"})
        self.assertEqual(self.retrieval.calls, [])

    def test_missing_forged_expired_and_foreign_tool_grants_are_denied(self) -> None:
        self.put_grant(grant_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", expires_at=int(time.time()) - 1)
        self.put_grant(grant_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc", tool="get_extracted_fields")
        for grant in ("dddddddd-dddd-4ddd-8ddd-dddddddddddd", "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                      "cccccccc-cccc-4ccc-8ccc-cccccccccccc"):
            with self.subTest(grant=grant):
                self.assertEqual(self.call(self.event(**{"_legaldeskGrantId": grant})), {"error": "access_denied"})
        self.assertEqual(self.call({"question": "q", "matterId": "mat_sundial"}), {"error": "access_denied"})
        self.assertEqual(self.retrieval.calls, [])

    def test_unexpected_arguments_and_bad_questions_are_rejected(self) -> None:
        for event in (
            self.event(tenantId="tnt_borealis"), self.event(subject="idp|bob-fictional"),
            self.event(question=""), self.event(question="x" * (contracts.MAX_SEARCH_QUESTION_LENGTH + 1)),
            self.event(question=7), self.event(**{"_legaldeskGrantId": "not-a-uuid"}),
            {**self.event(), "authorizedContext": {"matter": "mat_glacier"}},
        ):
            with self.subTest(event=str(event)[:60]):
                self.assertIn(self.call(event), ({"error": "invalid_request"}, {"error": "access_denied"}))
        self.assertEqual(self.retrieval.calls, [])

    def test_input_guardrail_block_is_not_a_valid_tool_result(self) -> None:
        self.guardrail.responses["INPUT"] = {"action": "GUARDRAIL_INTERVENED", "outputs": [{"text": "x"}], "assessments": []}
        output = self.call(self.event())
        self.assertEqual((output["operationStatus"], output["citations"], output["evidenceStatus"]), ("blocked", [], None))
        self.assertEqual(self.retrieval.calls, [])
        with self.assertRaises(contracts.AgentContractError):
            contracts.parse_search_documents_result(output)

    def test_stale_vector_of_a_deleted_document_fails_closed(self) -> None:
        self.metadata.documents.clear()  # vector still in the KB, document record gone
        output = self.call(self.event())
        self.assertEqual((output["operationStatus"], output["citations"]), ("error", []))
        self.assertNotIn(PASSAGE, str(output))

    def test_unconfigured_environment_fails_closed(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(lambda_module._service_from_environment())
            self.assertEqual(lambda_module.search_documents_lambda_handler(self.event(), None), {"error": "service_unavailable"})


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


class Context:
    def __init__(self, remaining_ms: int) -> None:
        self.remaining_ms = remaining_ms

    def get_remaining_time_in_millis(self) -> int:
        return self.remaining_ms


class Recorder:
    def __init__(self) -> None:
        self.events = []

    def record(self, event) -> None:
        self.events.append(event)


class SlowStage:
    """Wrap a fake stage so that each call consumes simulated time."""

    def __init__(self, inner, clock, seconds, method):
        self.inner, self.clock, self.seconds, self.method = inner, clock, seconds, method
        self.calls = 0

    def __getattr__(self, name):
        attribute = getattr(self.inner, name)
        if name != self.method:
            return attribute

        def call(*args, **kwargs):
            self.calls += 1
            self.clock.now += self.seconds
            return attribute(*args, **kwargs)

        return call


class PipelineDeadlineTests(unittest.TestCase):
    """PB-002: the whole pipeline has a deadline and ends with a clean error."""

    put_grant = SearchDocumentsLambdaTests.put_grant
    event = SearchDocumentsLambdaTests.event

    def setUp(self) -> None:
        SearchDocumentsLambdaTests.setUp(self)
        self.clock = FakeClock()
        self.sink = Recorder()
        self.service = dataclasses.replace(self.service, telemetry_sink=self.sink)

    def slow(self, **seconds):
        stages = {"retrieval_client": "retrieve", "guardrail_client": "apply_guardrail",
                  "evidence_resolver": "resolve", "answer_writer": "write", "grounding_validator": "validate"}
        wrapped = {name: SlowStage(getattr(self.service, name), self.clock, seconds[name], method)
                   for name, method in stages.items() if name in seconds}
        self.service = dataclasses.replace(self.service, **wrapped)
        return wrapped

    def call_with(self, remaining_ms=None, event=None):
        context = Context(remaining_ms) if remaining_ms is not None else None
        with patch.object(lambda_module, "_service_from_environment", return_value=(self.service, self.grants)):
            return lambda_module.search_documents_lambda_handler(event or self.event(), context, clock=self.clock)

    def final_tool_event(self):
        events = [item for item in self.sink.events if item.event_type.value == "tool"]
        return events[0], events[-1]

    def test_budget_constants_are_consistent_with_the_template_and_the_stage_timeouts(self) -> None:
        template = (ROOT / "infra" / "cloudformation" / "phase-08-gateway-mcp.yaml").read_text(encoding="utf-8")
        function = template.split("\n  SearchDocumentsFunction:\n", 1)[1].split("\n  SearchDocumentsGatewayTarget:", 1)[0]
        self.assertIn(f"Timeout: {lambda_module.SEARCH_TOOL_BUDGET_SECONDS}", function)
        usable = lambda_module.SEARCH_TOOL_BUDGET_SECONDS - lambda_module.SAFETY_MARGIN_SECONDS
        # One model call must always be startable on a fresh deadline, and a
        # single stage can never outlast the usable budget.
        for stage in (lambda_module.MODEL_CALL_SECONDS, lambda_module.GUARDRAIL_CALL_SECONDS,
                      lambda_module.RETRIEVE_CALL_SECONDS):
            self.assertLess(stage, usable)
        self.assertGreater(lambda_module.SAFETY_MARGIN_SECONDS, 0)

    def test_a_pipeline_within_budget_is_unchanged(self) -> None:
        self.slow(retrieval_client=1, evidence_resolver=2, answer_writer=2)
        output = self.call_with(remaining_ms=25_000)
        self.assertEqual(output["operationStatus"], "ok")
        first, last = self.final_tool_event()
        self.assertEqual((first.outcome.value, last.outcome.value), ("started", "succeeded"))

    def test_the_deadline_is_our_budget_even_when_the_platform_leaves_more(self) -> None:
        deadline = lambda_module.SearchDeadline.for_invocation(Context(900_000), clock=self.clock)
        self.assertEqual(deadline.remaining(), lambda_module.SEARCH_TOOL_BUDGET_SECONDS - lambda_module.SAFETY_MARGIN_SECONDS)
        self.assertEqual(
            lambda_module.SearchDeadline.for_invocation(None, clock=self.clock).remaining(),
            lambda_module.SEARCH_TOOL_BUDGET_SECONDS - lambda_module.SAFETY_MARGIN_SECONDS,
        )

    def test_the_deadline_follows_what_the_platform_says_is_left(self) -> None:
        deadline = lambda_module.SearchDeadline.for_invocation(Context(9_000), clock=self.clock)
        self.assertEqual(deadline.remaining(), 9 - lambda_module.SAFETY_MARGIN_SECONDS)

    def test_a_late_stage_that_no_longer_fits_returns_a_clean_timeout_and_is_not_started(self) -> None:
        # Usable budget 22 s: retrieval 1 s, resolver 9 s, writer 9 s -> 3 s left,
        # but the grounding Guardrail needs 5 s to be started.
        stages = self.slow(retrieval_client=1, evidence_resolver=9, answer_writer=9, grounding_validator=1)
        output = self.call_with(remaining_ms=25_000)
        self.assertEqual(output, {"error": "timeout"})
        self.assertEqual(stages["grounding_validator"].calls, 0)
        first, last = self.final_tool_event()
        self.assertEqual((first.outcome.value, last.outcome.value), ("started", "error"))
        self.assertEqual(last.error_code.value, "timeout")

    def test_a_stage_is_refused_when_the_platform_has_little_time_left(self) -> None:
        stages = self.slow(retrieval_client=1)
        output = self.call_with(remaining_ms=6_000)  # 3 s usable: the input Guardrail needs 5 s
        self.assertEqual(output, {"error": "timeout"})
        self.assertEqual(stages["retrieval_client"].calls, 0)
        self.assertEqual(self.guardrail.calls, [])
        self.assertEqual(self.final_tool_event()[1].error_code.value, "timeout")

    def test_no_guarded_stage_ever_starts_without_room_for_its_own_timeout(self) -> None:
        needs = {"retrieve": lambda_module.RETRIEVE_CALL_SECONDS, "resolve": lambda_module.MODEL_CALL_SECONDS,
                 "write": lambda_module.MODEL_CALL_SECONDS, "validate": lambda_module.GUARDRAIL_CALL_SECONDS}
        seen = []

        class Probe:
            def __init__(self, inner, method, deadline_holder):
                self.inner, self.method, self.holder = inner, method, deadline_holder

            def __getattr__(self, name):
                attribute = getattr(self.inner, name)
                if name != self.method:
                    return attribute

                def call(*args, **kwargs):
                    seen.append((self.method, self.holder["deadline"].remaining()))
                    self.holder["clock"].now += 4.5
                    return attribute(*args, **kwargs)

                return call

        holder = {"clock": self.clock}
        original = lambda_module._with_deadline

        def capture(service, deadline):
            holder["deadline"] = deadline
            # The probe sits inside the guard: it only sees calls the guard let start.
            probed = dataclasses.replace(service, **{
                name: Probe(getattr(service, name), method, holder)
                for name, method in (("retrieval_client", "retrieve"), ("evidence_resolver", "resolve"),
                                     ("answer_writer", "write"), ("grounding_validator", "validate"))
            })
            return original(probed, deadline)

        with patch.object(lambda_module, "_with_deadline", capture):
            self.call_with(remaining_ms=25_000)
        self.assertTrue(seen)
        for method, remaining in seen:
            self.assertGreaterEqual(remaining, needs[method], method)

    def test_a_deadline_hit_in_the_first_stage_is_a_timeout_not_a_service_error(self) -> None:
        # The input Guardrail runs inside answer_question; a refusal there must
        # not escape as a generic failure.
        output = self.call_with(remaining_ms=3_500)
        self.assertEqual(output, {"error": "timeout"})
        self.assertEqual(self.retrieval.calls, [])

    def test_a_real_failure_closes_the_telemetry_span_and_is_not_reported_as_a_timeout(self) -> None:
        class Broken:
            def retrieve(self, **kwargs):
                raise RuntimeError("synthetic outage")

        self.service = dataclasses.replace(self.service, retrieval_client=Broken())
        output = self.call_with(remaining_ms=25_000)
        self.assertEqual((output["operationStatus"], output["citations"]), ("error", []))
        self.assertEqual(self.final_tool_event()[1].outcome.value, "error")
        self.assertNotIn("timeout", [getattr(item.error_code, "value", None) for item in self.sink.events])

    def test_sdk_clients_are_built_with_per_call_timeouts_that_fit_the_deadline(self) -> None:
        configs = {}

        def client(service_name, **kwargs):
            configs.setdefault(service_name, []).append(kwargs["config"])
            return object()

        class Resource:
            def __init__(self, service_name, **kwargs):
                configs.setdefault(service_name, []).append(kwargs["config"])

            def Table(self, name):
                return object()

        environment = {
            "DOCUMENT_METADATA_TABLE_NAME": "table", "LEGALDESK_SOURCE_BUCKET": "bucket",
            "LEGALDESK_KNOWLEDGE_BASE_ID": "KBID", "LEGALDESK_GUARDRAIL_ID": "guardrail",
            "LEGALDESK_GUARDRAIL_VERSION": "1", "LEGALDESK_RESOLVER_MODEL_ID": "arn:resolver",
            "LEGALDESK_WRITER_MODEL_ID": "arn:writer",
        }
        with patch.dict("os.environ", environment, clear=True), \
                patch("boto3.client", side_effect=client), patch("boto3.resource", side_effect=Resource):
            built = lambda_module._service_from_environment()
        self.assertIsNotNone(built)
        usable = lambda_module.SEARCH_TOOL_BUDGET_SECONDS - lambda_module.SAFETY_MARGIN_SECONDS
        all_configs = [config for items in configs.values() for config in items]
        self.assertEqual(len(all_configs), 5)  # model, guardrail, retrieve, dynamodb, s3
        self.assertEqual(
            sorted(config.read_timeout for config in configs["bedrock-runtime"]),
            sorted([lambda_module.GUARDRAIL_CALL_SECONDS, lambda_module.MODEL_CALL_SECONDS]),
        )
        for config in all_configs:
            self.assertLess(config.read_timeout, usable)
            self.assertEqual(config.retries["total_max_attempts"], 1)
            self.assertEqual(config.connect_timeout, lambda_module.CONNECT_SECONDS)


if __name__ == "__main__":
    unittest.main()
