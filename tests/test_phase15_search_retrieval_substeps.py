"""T9 step 12 (2026-11-03): eighteen ``search_documents`` runs failed with ``retrieval_invalid_response`` and reported success.

* The post-retrieval checks (normalisation, document record, object HEAD) were collapsed into one error code. Each is now a closed code, with
  the same fail-closed behaviour and the same exceptions as before.
* The search tool answered with a failure payload that Gateway reports as a SUCCESSFUL tool call. It now raises, so Gateway reports ``isError``;
  and the call log of the worker marks a result that reports its own failure as ERROR whichever target produced it.
"""

from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path
from typing import Any, Mapping
from unittest import mock

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))
sys.path.insert(0, str(ROOT / "agent" / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import test_phase15_agent_chat_client as chat  # noqa: E402
import test_retrieval as base  # noqa: E402
from legaldesk import search_documents_lambda as lambda_module  # noqa: E402
from legaldesk.domain.models import Document, DocumentStatus, MalwareScanStatus  # noqa: E402
from legaldesk.documents import InMemoryDocumentMetadataRepository  # noqa: E402
from legaldesk.observability import TelemetryErrorCode, TelemetryEventType, TelemetryOutcome  # noqa: E402
from legaldesk.retrieval import RetrievalSubstepError, _object_error_code  # noqa: E402
from legaldesk_agent.client import _structured_tool_results  # noqa: E402

KEY = "tenants/tnt_aurora/matters/mat_sundial/doc-sundial.pdf"
SUBSTEP_CODES = {
    "retrieval_results_malformed", "retrieval_document_lookup_failed", "retrieval_document_missing", "retrieval_document_not_indexed",
    "retrieval_document_not_clean", "retrieval_object_denied", "retrieval_object_missing", "retrieval_object_failed",
}


def real_shaped_result(document_id: str = "doc-sundial", text: str = "El plazo es de 17 días.") -> dict[str, Any]:
    """What Bedrock Knowledge Bases really returns: extra x-amz metadata, a FLOAT page number, a typed location, a float score."""

    return {
        "content": {"type": "TEXT", "text": text},
        "location": {"type": "S3", "s3Location": {"uri": f"s3://source-bucket/tenants/tnt_aurora/matters/mat_sundial/documents/{document_id}/original.pdf"}},
        "metadata": {
            "tenantId": "tnt_aurora", "matterId": "mat_sundial", "documentId": document_id, "documentName": "Contrato.pdf",
            "mediaType": "application/pdf", "jurisdiction": "ES", "confidentiality": "internal",
            "x-amz-bedrock-kb-source-uri": f"s3://source-bucket/tenants/tnt_aurora/matters/mat_sundial/documents/{document_id}/original.pdf",
            "x-amz-bedrock-kb-chunk-id": "1%3A0%3AyGzAbCdEf", "x-amz-bedrock-kb-data-source-id": "XIA81RBIPG",
            "x-amz-bedrock-kb-document-page-number": 3.0,
        },
        "score": 0.5234,
    }


class Sink:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def record(self, event: Any) -> None:
        self.events.append(event)

    def retrieval_errors(self) -> list[str]:
        return [e.error_code.value for e in self.events if e.event_type is TelemetryEventType.RETRIEVAL and e.outcome is TelemetryOutcome.ERROR and e.error_code]


class RaisesWithCode(Exception):
    def __init__(self, code: str) -> None:
        super().__init__("provider message that must never be copied anywhere")
        self.response = {"Error": {"Code": code, "Message": "SECRET provider message"}}


class Substeps(unittest.TestCase):
    """Reuses the retrieval fixtures (authorization store, KB double) of the existing tests without re-running them."""

    setUp = base.RetrievalTests.setUp

    def run_search(self, *, results=None, repository=None, storage=None, sink=None):
        from legaldesk.retrieval import search_legal_documents

        return search_legal_documents(
            base.ALICE, "mat_sundial", "¿Cuál es el plazo?", authorization_store=self.auth,
            bedrock_client=base.FakeKnowledgeBaseClient(results if results is not None else [real_shaped_result()]),
            knowledge_base_id="kb-fictional", correlation_id="8ec5d1c5-7b58-4bc2-a183-8fd48a3bd279", telemetry_sink=sink,
            metadata_repository=repository, object_storage=storage,
        )

    def repository(self, **overrides):
        repository = InMemoryDocumentMetadataRepository()
        fields = dict(
            document_id="doc-sundial", matter_id="mat_sundial", tenant_id="tnt_aurora", name="sundial.pdf", s3_key=KEY,
            media_type="application/pdf", jurisdiction="fictional", document_date="2099-01-01", confidentiality="fictional-internal",
            status=DocumentStatus.INDEXED, malware_scan_status=MalwareScanStatus.CLEAN,
        )
        fields.update(overrides)
        repository.save(Document(**fields))
        return repository

    def failing(self, *, code: str, **kwargs) -> list[str]:
        sink = Sink()
        with self.assertRaises(ValueError) as caught:  # the same exception family as before: every caller still fails closed
            self.run_search(sink=sink, **kwargs)
        self.assertIsInstance(caught.exception, RetrievalSubstepError)
        self.assertEqual(caught.exception.code, code)
        return sink.retrieval_errors()

    def test_the_real_knowledge_base_response_shape_is_accepted_end_to_end(self) -> None:
        storage = base.HeadRecordingStorage(existing_keys={KEY})
        (passage,) = self.run_search(repository=self.repository(), storage=storage)
        self.assertEqual(passage.text, "El plazo es de 17 días.")
        self.assertEqual((passage.citation.document_id, passage.citation.document_name), ("doc-sundial", "Contrato.pdf"))
        self.assertIsNone(passage.citation.page_number)  # a float page number is not trusted as a page (and is no error)
        self.assertEqual(storage.calls, [KEY])  # HEAD of the key of the LIVE record, never the provider URI

    def test_each_post_retrieval_step_has_its_own_closed_code(self) -> None:
        storage = base.HeadRecordingStorage(existing_keys={KEY})
        malformed = real_shaped_result()
        malformed["content"] = {"type": "TEXT", "text": "   "}
        cases = {
            "retrieval_results_malformed": dict(results=[malformed]),
            "retrieval_document_missing": dict(repository=InMemoryDocumentMetadataRepository(), storage=storage),
            "retrieval_document_not_indexed": dict(repository=self.repository(status=DocumentStatus.PENDING_INGESTION), storage=storage),
            "retrieval_document_not_clean": dict(repository=self.repository(malware_scan_status=MalwareScanStatus.PENDING), storage=storage),
            "retrieval_object_denied": dict(repository=self.repository(), storage=base.HeadRecordingStorage(error=RaisesWithCode("AccessDenied"))),
            "retrieval_object_missing": dict(repository=self.repository(), storage=base.HeadRecordingStorage(error=RaisesWithCode("404"))),
            "retrieval_object_failed": dict(repository=self.repository(), storage=base.HeadRecordingStorage(error=RuntimeError("timed out"))),
        }
        for code, kwargs in cases.items():
            with self.subTest(code):
                self.assertEqual(self.failing(code=code, **kwargs), [code])
        broken = mock.Mock()
        broken.get_for_scope.side_effect = RuntimeError("dynamodb timed out")
        self.assertEqual(self.failing(code="retrieval_document_lookup_failed", repository=broken, storage=storage), ["retrieval_document_lookup_failed"])

    def test_the_codes_are_in_the_closed_telemetry_vocabulary_and_carry_no_provider_text(self) -> None:
        vocabulary = {code.value for code in TelemetryErrorCode}
        self.assertTrue(SUBSTEP_CODES <= vocabulary)
        sink = Sink()
        with self.assertRaises(ValueError):
            self.run_search(sink=sink, repository=self.repository(), storage=base.HeadRecordingStorage(error=RaisesWithCode("AccessDenied")))
        self.assertNotIn("SECRET", json.dumps([e.to_dict() for e in sink.events], default=str))

    def test_one_unavailable_document_still_blocks_the_whole_response_as_before(self) -> None:
        """Fail closed is unchanged: a stale vector among the results never reaches the resolver (it is just reported precisely now)."""

        repository = self.repository()
        results = [real_shaped_result("doc-sundial"), real_shaped_result("doc-stale", "Texto de un documento borrado.")]
        storage = base.HeadRecordingStorage(existing_keys={KEY})
        self.assertEqual(self.failing(code="retrieval_document_missing", results=results, repository=repository, storage=storage), ["retrieval_document_missing"])

    def test_the_object_error_class_reads_only_the_provider_code(self) -> None:
        for code, expected in (("403", "retrieval_object_denied"), ("AccessDenied", "retrieval_object_denied"), ("Forbidden", "retrieval_object_denied"),
                               ("404", "retrieval_object_missing"), ("NoSuchKey", "retrieval_object_missing"), ("NotFound", "retrieval_object_missing"),
                               ("SlowDown", "retrieval_object_failed"), ("InternalError", "retrieval_object_failed")):
            self.assertEqual(_object_error_code(RaisesWithCode(code)), expected, code)
        for exc in (RuntimeError("AccessDenied"), KeyError("404"), TimeoutError("403")):
            self.assertEqual(_object_error_code(exc), "retrieval_object_failed")  # a message is never parsed


class TheSearchToolFailsAsAnErrorTests(unittest.TestCase):
    def handler(self, result):
        with mock.patch.object(lambda_module, "search_documents_lambda_handler", return_value=result):
            return lambda_module.search_documents_gateway_handler({}, None)

    def test_each_failure_is_raised_with_a_closed_code(self) -> None:
        cases = [
            ({"error": "timeout"}, "timeout"), ({"error": "service_unavailable"}, "service_unavailable"),
            ({"error": "access_denied"}, "access_denied"), ({"error": "invalid_request"}, "invalid_request"),
            ({"error": "something unexpected with text"}, "service_unavailable"),
            ({"answer": "El servicio no está disponible.", "evidenceStatus": None, "operationStatus": "error", "citations": []}, "pipeline_error"),
            ("not a mapping", "invalid_result"),
        ]
        for result, code in cases:
            with self.subTest(code=code):
                with self.assertLogs(lambda_module._LOGGER, level="WARNING") as logs, self.assertRaises(lambda_module.SearchToolError) as caught:
                    self.handler(result)
                self.assertEqual((caught.exception.code, str(caught.exception)), (code, code))
                self.assertEqual(json.loads(logs.output[0].split("search_documents failed ", 1)[1]), {"code": code})

    def test_a_usable_result_and_a_blocked_one_are_still_returned(self) -> None:
        ok = {"answer": "El plazo es de 17 días.", "evidenceStatus": "answerable", "operationStatus": "ok", "citations": []}
        blocked = {"answer": "No puedo ayudar con eso.", "evidenceStatus": None, "operationStatus": "blocked", "citations": []}
        self.assertEqual(self.handler(ok), ok)
        self.assertEqual(self.handler(blocked), blocked)  # a fixed backend sentence, not a failure

    def test_the_failure_log_and_message_never_contain_the_question_or_the_document(self) -> None:
        question = "SECRET-QUESTION about Contrato.pdf"
        with mock.patch.object(lambda_module, "_service_from_environment", return_value=None), \
                self.assertLogs(lambda_module._LOGGER, level="WARNING") as logs, self.assertRaises(lambda_module.SearchToolError) as caught:
            lambda_module.search_documents_gateway_handler({"question": question, "_legaldeskGrantId": "11111111-1111-4111-8111-111111111111"}, None)
        self.assertEqual(caught.exception.code, "service_unavailable")
        self.assertNotIn("SECRET", " ".join(logs.output) + str(caught.exception))

    def test_the_template_points_gateway_at_the_raising_entry_point(self) -> None:
        template = (ROOT / "infra" / "cloudformation" / "phase-08-gateway-mcp.yaml").read_text(encoding="utf-8")
        self.assertIn("Handler: legaldesk.search_documents_lambda.search_documents_gateway_handler", template)
        self.assertNotIn("Handler: legaldesk.search_documents_lambda.search_documents_lambda_handler", template)
        self.assertTrue(callable(lambda_module.search_documents_gateway_handler))


class AResultThatReportsItsOwnFailureIsAnErrorForTheWorkerTests(unittest.TestCase):
    SEARCH = chat.SEARCH

    def parse(self, payload: Mapping[str, Any], allowed=(chat.SEARCH,)):
        events = chat.tool_call("tu-1", self.SEARCH, payload)
        (call,) = _structured_tool_results(events, allowed_tools=allowed, raw=True, allow_incomplete=True)
        return call

    def test_failure_shapes_are_errors_with_no_payload(self) -> None:
        for payload in (
            {"error": "timeout"},
            {"answer": "El servicio no está disponible.", "evidenceStatus": None, "operationStatus": "error", "citations": []},
        ):
            with self.subTest(payload=payload):
                call = self.parse(payload)
                self.assertEqual((call.status, dict(call.payload), call.error_hint), ("ERROR", {}, "tool_reported_error"))

    def test_real_results_are_untouched(self) -> None:
        ok = {"answer": "El plazo es de 17 días.", "evidenceStatus": "answerable", "operationStatus": "ok", "citations": []}
        blocked = {"answer": "No puedo ayudar con eso.", "evidenceStatus": None, "operationStatus": "blocked", "citations": []}
        inventory = {"documents": []}
        for payload in (ok, blocked, inventory):
            call = self.parse(copy.deepcopy(payload), allowed=(chat.SEARCH,))
            self.assertEqual((call.status, dict(call.payload)), ("SUCCESS", payload))

    def test_the_worker_then_treats_it_as_no_evidence_not_as_an_invalid_result(self) -> None:
        from legaldesk.agent_chat import AgentChatWorker
        from legaldesk_agent import AgentChatResult, AgentToolCall

        failed = self.parse({"error": "timeout"})
        loop = AgentChatResult(session_id="s", correlation_id="c", text="", loop_outcome="completed", iterations=2, stop_reason="end_turn",
                               tool_calls=(AgentToolCall(failed.tool_use_id, failed.name, failed.status, failed.payload, failed.error_hint),))
        output, _outcome, invalid = AgentChatWorker.resolve_loop(loop)
        self.assertEqual(invalid, 0)  # not counted as a malformed search result: it is a failed call
        self.assertEqual(loop.tool_calls[0].error_hint, "tool_reported_error")


if __name__ == "__main__":
    unittest.main()
