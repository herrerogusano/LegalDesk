"""Agent side of the T9 step 12 failure (toolAccuracy 0.667, 29 of 30 cases fell back to RAG).

Offline findings pinned here:

* the answer-plan grammar of prompt 1.3.0 is exactly what ``resolve_loop`` validates (so the matter-selector instruction did not shift the
  format), with real-looking Harness tool-use ids;
* the ``get_extracted_fields`` call the prompt prescribes satisfies the tool's schema (and the documented limits are the schema's);
* a loop that dies after its first tool, a plan that copies the wrong tool-use id and a listing that fails its contract are now told apart in the
  report by closed labels, which is the evidence only a live run can produce.
"""

from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))
sys.path.insert(0, str(ROOT / "agent" / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import jsonschema  # noqa: E402
import test_phase15_agent_chat_client as chat  # noqa: E402
from evals import phase15_routing_eval as ev  # noqa: E402
from legaldesk import agent_contracts as contracts  # noqa: E402
from legaldesk.agent_chat import AgentChatWorker  # noqa: E402
from legaldesk_agent import AgentChatResult, AgentToolCall, HarnessInvoker, invoke_agent_chat  # noqa: E402
from legaldesk_agent.client import _unanswered_tool_names  # noqa: E402

LIST = "@legaldesk_gateway/metadata-mcp___list_matter_documents"
FIELDS = "@legaldesk_gateway/metadata-mcp___get_extracted_fields"
SEARCH = "@legaldesk_gateway/search-documents___search_documents"
# Bedrock/Strands tool-use ids look like this: a prefix, an underscore and base64url characters (which include - and _)
TID_SEARCH, TID_LIST, TID_FIELDS = "tooluse_Zk9-a_7QdLw3pNfXy2RhVg", "tooluse_Qm1_bT4-cRZ8xYvJ0sKuEA", "tooluse_h3-PqW_n6DfLcGt9Vo2MzA"


def document(index: int, **overrides):
    base = {
        "documentId": f"6f1d0c1e-0000-4000-8000-{index:012d}", "name": f"Contrato {index}.pdf", "mediaType": "application/pdf",
        "jurisdiction": "ES", "documentDate": "2026-01-01", "confidentiality": "internal", "status": "INDEXED",
        "fileSizeBytes": 2048, "uploadedAt": "2026-09-28T10:00:00+00:00",
    }
    base.update(overrides)
    return base


def fields_payload(documents):
    return {"documents": [{"documentId": d["documentId"], "name": d["name"], "documentType": "CONTRACT", "fields": [
        {"name": "explicit_expiration_date", "status": "fact", "value": "2027-03-15", "origin": "LITERAL",
         "evidence": [{"page": 1, "quote": "Vence el 15 de marzo de 2027"}]},
        {"name": "automatic_renewal", "status": "fact", "value": True, "origin": "LITERAL",
         "evidence": [{"page": 2, "quote": "Se renueva automáticamente"}]}]} for d in documents], "notFound": []}


SEARCH_OK = {"answer": "El plazo es de 17 días.", "evidenceStatus": "answerable", "operationStatus": "ok",
             "citations": [{"citationId": "citation-1", "documentId": "6f1d0c1e-0000-4000-8000-000000000001", "documentName": "Contrato 1.pdf",
                            "pageNumber": 4, "section": None, "passage": "El plazo documentado es de 17 días."}]}


def loop(plan, calls, *, stop="end_turn", outcome="completed"):
    return AgentChatResult(session_id="s", correlation_id="c", text=plan if isinstance(plan, str) else json.dumps(plan), loop_outcome=outcome,
                           iterations=3, stop_reason=stop, tool_calls=tuple(calls))


class ThePlanGrammarOfThePromptIsWhatTheValidatorAcceptsTests(unittest.TestCase):
    docs = [document(1), document(2), document(3)]

    def calls(self):
        return [AgentToolCall(TID_LIST, LIST, "SUCCESS", {"documents": self.docs}),
                AgentToolCall(TID_FIELDS, FIELDS, "SUCCESS", fields_payload(self.docs)),
                AgentToolCall(TID_SEARCH, SEARCH, "SUCCESS", SEARCH_OK)]

    def resolve(self, plan):
        output, outcome, invalid = AgentChatWorker.resolve_loop(loop(plan, self.calls()))
        return output, invalid

    def test_each_block_type_of_the_prompt_renders_with_realistic_tool_use_ids(self) -> None:
        refs = [{"documentId": d["documentId"], "field": "explicit_expiration_date"} for d in self.docs]
        plans = {
            "text": {"version": 1, "intro": "answer_from_documents", "blocks": [{"type": "text", "source": {"toolUseId": TID_SEARCH}}]},
            "inventory": {"version": 1, "intro": "inventory_of_documents", "blocks": [
                {"type": "inventory", "source": {"toolUseId": TID_LIST}, "statusIs": ["INDEXED"], "order": {"by": "document_name", "direction": "asc"}}]},
            "data": {"version": 1, "intro": "data_from_documents", "blocks": [{"type": "data", "groups": [
                {"criterion": {"id": "expires_in_period", "params": {"from": "2027-01-01", "to": "2027-12-31"}}, "refs": refs}],
                "order": {"by": "field_value", "field": "explicit_expiration_date", "direction": "asc"}, "aggregates": [{"type": "count"}, {"type": "min", "field": "explicit_expiration_date"}]}]},
            "mixed": {"version": 1, "intro": "data_and_text_from_documents", "blocks": [
                {"type": "data", "groups": [{"criterion": {"id": "all_documents"}, "refs": refs}]},
                {"type": "text", "source": {"toolUseId": TID_SEARCH}, "transitionBefore": "continue_with_text"}]},
        }
        for name, plan in plans.items():
            with self.subTest(name):
                output, invalid = self.resolve(plan)
                self.assertEqual((output.fallback_reason, output.omitted, invalid), (None, (), 0))
                self.assertNotEqual(output.path.value, "rag_fallback")

    def test_one_markdown_fence_is_tolerated_and_prose_around_the_json_is_not(self) -> None:
        plan = json.dumps({"version": 1, "blocks": [{"type": "text", "source": {"toolUseId": TID_SEARCH}}]})
        self.assertIsNone(self.resolve("```json\n" + plan + "\n```")[0].fallback_reason)
        for text in ("Aquí está el plan: " + plan, plan + "\nEspero que ayude."):
            output, _ = self.resolve(text)
            self.assertEqual((output.fallback_reason.value, output.plan_error), ("plan_invalid", "invalid_json"))

    def test_a_plan_that_copies_the_wrong_tool_use_id_omits_the_block_with_a_closed_reason(self) -> None:
        for wrong in (TID_SEARCH[:-3], TID_SEARCH.upper(), "tooluse_" + "x" * 22, "call_1"):
            output, _ = self.resolve({"version": 1, "blocks": [{"type": "inventory", "source": {"toolUseId": wrong}}]})
            self.assertEqual(output.fallback_reason.value, "no_valid_blocks")
            self.assertEqual([item.error.value for item in output.omitted], ["unknown_tool_use_id"])

    def test_a_failed_search_leaves_no_text_to_print_so_a_text_block_is_omitted(self) -> None:
        """Why every text/mixed case fell back while the search tool was failing: the block points at a call with no usable result."""

        failed = AgentToolCall(TID_SEARCH, SEARCH, "ERROR", {}, "tool_reported_error")
        output, _, _ = AgentChatWorker.resolve_loop(loop({"version": 1, "blocks": [{"type": "text", "source": {"toolUseId": TID_SEARCH}}]}, [failed]))
        self.assertEqual((output.path.value, output.fallback_reason.value, [i.error.value for i in output.omitted]), ("rag_fallback", "no_valid_blocks", ["unknown_tool_use_id"]))


class TheToolCallThePromptPrescribesFitsTheSchemaTests(unittest.TestCase):
    schema = contracts.GET_EXTRACTED_FIELDS_SCHEMA

    def valid(self, instance) -> bool:
        return jsonschema.Draft202012Validator(self.schema).is_valid(instance)

    def test_matter_selector_ids_from_the_listing_and_registry_names_validate(self) -> None:
        ids = [document(i)["documentId"] for i in range(1, 4)]
        self.assertTrue(self.valid({"matterId": "mat_phase14_a_20260928", "documentIds": ids, "fieldNames": ["automatic_renewal", "amount"]}))
        self.assertTrue(self.valid({"matterId": "mat_phase14_a_20260928", "documentIds": ids}))  # fieldNames omitted: every field
        self.assertTrue(self.valid({"matterId": "mat_phase14_a_20260928", "documentIds": [f"tooluse_{i}" for i in range(20)]}))

    def test_what_the_prompt_says_is_exactly_what_the_schema_enforces(self) -> None:
        ids = [document(i)["documentId"] for i in range(1, 22)]
        self.assertFalse(self.valid({"matterId": "m", "documentIds": ids}))  # 21 documents: the limit the prompt states (20 per call)
        self.assertFalse(self.valid({"documentIds": ids[:2]}))                # no matterId: the failure the instruction fixes
        self.assertFalse(self.valid({"matterId": "m", "documentIds": ids[:2], "fieldNames": ["expiration_date"]}))  # not a registry name
        prompt = " ".join((ROOT / "prompts" / "legaldesk-agent.md").read_text(encoding="utf-8").split())
        self.assertIn("for up to 20 documents per call", prompt)
        self.assertEqual(contracts.MAX_DOCUMENTS_PER_CALL, 20)

    def test_every_field_the_prompt_names_in_a_criterion_is_in_the_registry_enum(self) -> None:
        prompt = (ROOT / "prompts" / "legaldesk-agent.md").read_text(encoding="utf-8")
        named = set(re.findall(r"on `([a-z_]+)`", prompt))
        self.assertTrue(named)
        self.assertTrue(named <= set(contracts.IDP_FIELD_NAMES), named - set(contracts.IDP_FIELD_NAMES))


class ALoopThatDiesAfterItsFirstToolSaysWhyTests(unittest.TestCase):
    def events(self, *, with_second_result: bool):
        events = chat.tool_call(TID_LIST, "legaldesk_gateway_metadata-mcp___list_matter_documents", {"documents": [document(1)]})
        second = chat.tool_call(TID_FIELDS, "legaldesk_gateway_metadata-mcp___get_extracted_fields", fields_payload([document(1)]))
        return events + (second if with_second_result else second[:4])  # the cut variant has the use, never the result

    def run_loop(self, events, **kwargs):
        client = chat.StreamClient(events, **kwargs)
        return invoke_agent_chat(HarnessInvoker(client, chat.HARNESS_ARN), "¿Qué contratos se renuevan?", invocation_scope=chat.make_scope(), system_prompt=chat.PROMPT)

    def test_a_requested_tool_without_a_result_is_named_and_the_stream_error_gets_a_label(self) -> None:
        class Boom(Exception):
            response = {"Error": {"Code": "InternalServerException"}}

        result = self.run_loop(self.events(with_second_result=False), error_after=(len(self.events(with_second_result=False)), Boom("The request timed out in the tool")))
        self.assertEqual([c.short_name for c in result.tool_calls], ["list_matter_documents"])
        self.assertEqual(result.unanswered_tools, ("get_extracted_fields",))
        self.assertEqual((result.loop_outcome, result.stream_error_hint), ("timeout", "timeout"))

    def test_a_clean_loop_has_nothing_unanswered_and_no_error_hint(self) -> None:
        result = self.run_loop(self.events(with_second_result=True) + chat.final_message("{}"))
        self.assertEqual((result.unanswered_tools, result.stream_error_hint), ((), None))

    def test_unknown_names_are_reported_as_other_and_never_by_text(self) -> None:
        events = chat.tool_call("tu-1", "legaldesk_gateway_search-documents___search_documents", {"x": 1})[:3]
        self.assertEqual(_unanswered_tool_names(events, (chat.SEARCH,)), ("search_documents",))
        weird = [{"contentBlockStart": {"contentBlockIndex": 0, "start": {"toolUse": {"toolUseId": "tu-2", "name": "SECRET document name"}}}}]
        self.assertEqual(_unanswered_tool_names(weird, (chat.SEARCH,)), ("other",))


class TheReportCarriesTheClosedDiagnosticsTests(unittest.TestCase):
    def observe(self, plan_text, calls, outcome="completed", **extra):
        obs = ev.Observation()
        result = AgentChatResult(session_id="s", correlation_id="c", text=plan_text, loop_outcome=outcome, iterations=2, stop_reason=extra.pop("stop", "tool_use"),
                                 tool_calls=tuple(calls), **extra)
        output, _outcome, invalid = AgentChatWorker.resolve_loop(result)
        ev._diagnose(obs, result, output, invalid)
        return obs

    def test_the_labels_for_a_loop_that_died_after_the_listing(self) -> None:
        call = AgentToolCall(TID_LIST, LIST, "SUCCESS", {"documents": [document(1)]})
        obs = self.observe("", [call], "failed", unanswered_tools=("get_extracted_fields",), stream_error_hint="timeout", stop="max_iterations_exceeded")
        self.assertEqual((obs.stop_reason, obs.unanswered_tools, obs.stream_error_hint, obs.invalid_tool_results),
                         ("max_iterations_exceeded", ["get_extracted_fields"], "timeout", 0))

    def test_an_unparsable_listing_is_counted_apart_from_a_wrong_id(self) -> None:
        broken = AgentToolCall(TID_LIST, LIST, "SUCCESS", {"documents": [{"documentId": "x"}]})
        plan = json.dumps({"version": 1, "blocks": [{"type": "inventory", "source": {"toolUseId": TID_LIST}}]})
        obs = self.observe(plan, [broken])
        self.assertEqual((obs.invalid_tool_results, obs.omitted_errors, obs.source_id_matches), (1, ["unknown_tool_use_id"], ["exact"]))
        good = AgentToolCall(TID_LIST, LIST, "SUCCESS", {"documents": [document(1)]})
        copied = json.dumps({"version": 1, "blocks": [{"type": "inventory", "source": {"toolUseId": TID_LIST[:-2] + "zz"}}]})
        obs = self.observe(copied, [good])
        self.assertEqual((obs.invalid_tool_results, obs.omitted_errors, obs.source_id_matches), (0, ["unknown_tool_use_id"], ["similar"]))
        invented = json.dumps({"version": 1, "blocks": [{"type": "inventory", "source": {"toolUseId": "call_1"}}]})
        self.assertEqual(self.observe(invented, [good]).source_id_matches, ["none"])

    def test_the_row_of_the_report_has_only_closed_values(self) -> None:
        obs = self.observe("Aquí está mi respuesta con SECRET text", [], stop="weird stop reason with spaces")
        row = ev.score_case({"id": "x", "category": "c", "expected": {"toolSets": [[]], "paths": ["rag_fallback"]}}, obs, offline=False)
        diagnostics = row["diagnostics"]
        self.assertEqual(set(diagnostics), {"planError", "omittedErrors", "stopReason", "toolStatuses", "toolErrorHints", "unansweredTools",
                                            "streamErrorHint", "invalidToolResults", "sourceIdMatches"})
        self.assertEqual((diagnostics["planError"], diagnostics["stopReason"]), ("invalid_json", "other"))
        self.assertNotIn("SECRET", json.dumps(row))


if __name__ == "__main__":
    unittest.main()
