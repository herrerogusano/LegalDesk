"""A plan names the source of a text or inventory block by tool and position, not by tool-use id (T9 step 12, second real run).

14 of 14 plans of the second run named an id that matched no call of the loop (``sourceIdMatches: none``), while the data blocks, which point at
document ids visible in the tool results, worked every time. The model does not reliably copy the tool-use id, so a contract built on copying it
cannot work. The source is now ``{"tool": "<tool of the block>", "call": n}``, resolved on the server against the call log of the same loop.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))
sys.path.insert(0, str(ROOT / "agent" / "src"))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT))

import test_phase15_agent_chat_client as chat  # noqa: E402
from evals import phase15_routing_eval as ev  # noqa: E402
from legaldesk import agent_contracts as contracts  # noqa: E402
from legaldesk.agent_chat import AgentChatWorker  # noqa: E402
from legaldesk.agent_output import BlockError  # noqa: E402
from legaldesk_agent import AgentChatResult, AgentToolCall, HarnessInvoker, invoke_agent_chat  # noqa: E402

LIST = "@legaldesk_gateway/metadata-mcp___list_matter_documents"
FIELDS = "@legaldesk_gateway/metadata-mcp___get_extracted_fields"
SEARCH = "@legaldesk_gateway/search-documents___search_documents"
# ids as Harness generates them; no model copies these reliably
ID_S1, ID_S2, ID_L1, ID_F1 = "tooluse_Zk9-a_7QdLw3pNfXy2RhVg", "tooluse_Qm1_bT4-cRZ8xYvJ0sKuEA", "tooluse_h3-PqW_n6DfLcGt9Vo2MzA", "tooluse_Xp0_RtY5-uQ2cBnLw8MaHg"
DOC = "6f1d0c1e-0000-4000-8000-000000000001"
SEARCH_OK = {"answer": "El plazo documentado es de 17 días.", "evidenceStatus": "answerable", "operationStatus": "ok",
             "citations": [{"citationId": "citation-1", "documentId": DOC, "documentName": "Contrato 1.pdf", "pageNumber": 4, "section": None,
                            "passage": "El plazo será de 17 días."}]}
SEARCH_OK_2 = {**SEARCH_OK, "answer": "Segunda respuesta.", "citations": [{**SEARCH_OK["citations"][0], "citationId": "citation-2"}]}
LISTING = {"documents": [{"documentId": DOC, "name": "Contrato 1.pdf", "mediaType": "application/pdf", "status": "INDEXED", "uploadedAt": "2026-09-28T10:00:00+00:00"}]}


def text(call, **extra):
    return {"type": "text", "source": {"tool": "search_documents", "call": call}, **extra}


def inventory(call, **extra):
    return {"type": "inventory", "source": {"tool": "list_matter_documents", "call": call}, **extra}


def plan(*blocks):
    return {"version": 1, "blocks": list(blocks)}


def resolve(plan_value, calls):
    loop = AgentChatResult(session_id="s", correlation_id="c", text=plan_value if isinstance(plan_value, str) else json.dumps(plan_value),
                           loop_outcome="completed", iterations=3, stop_reason="end_turn", tool_calls=tuple(calls))
    output, _outcome, invalid = AgentChatWorker.resolve_loop(loop)
    return output, invalid


def ok(tool_use_id, name, payload):
    return AgentToolCall(tool_use_id, name, "SUCCESS", payload)


class TheDocumentedFormParsesAndTheRestIsRefusedTests(unittest.TestCase):
    def test_both_block_types_accept_tool_and_call(self) -> None:
        parsed = contracts.parse_answer_plan(plan(text(1), inventory(2, statusIs=["INDEXED"])))
        self.assertEqual([(type(b).__name__, b.tool_use_id, b.call) for b in parsed.blocks], [("TextBlock", "", 1), ("InventoryBlock", "", 2)])

    def test_the_exact_id_form_is_still_accepted_but_no_longer_advertised(self) -> None:
        parsed = contracts.parse_answer_plan(plan({"type": "text", "source": {"toolUseId": "tu-1"}}))
        self.assertEqual((parsed.blocks[0].tool_use_id, parsed.blocks[0].call), ("tu-1", None))
        prompt = (ROOT / "prompts" / "legaldesk-agent.md").read_text(encoding="utf-8")
        self.assertNotIn("toolUseId", prompt)
        flat = " ".join(prompt.split())
        for needle in ('"source": {"tool": "search_documents", "call": <n>}', '"source": {"tool": "list_matter_documents", "call": <n>}', "counted from 1"):
            self.assertIn(needle, flat)

    def test_anything_else_in_a_source_is_an_invalid_plan(self) -> None:
        wrong_tool = {"type": "text", "source": {"tool": "list_matter_documents", "call": 1}}
        for source in (
            {"tool": "list_matter_documents", "call": 1},          # the tool of the other block type
            {"tool": "search_documents", "call": 0}, {"tool": "search_documents", "call": -1}, {"tool": "search_documents", "call": contracts.MAX_SOURCE_CALL + 1},
            {"tool": "search_documents", "call": True}, {"tool": "search_documents", "call": "1"}, {"tool": "search_documents", "call": 1.0},
            {"tool": "search_documents"}, {"call": 1}, {},                                  # one half
            {"tool": "search_documents", "call": 1, "toolUseId": "x"},                      # both forms at once
            {"tool": "search_documents", "call": 1, "extra": 1},
            "search_documents", 1, None,
        ):
            with self.subTest(source=source):
                with self.assertRaises(contracts.AgentContractError):
                    contracts.parse_answer_plan(plan({"type": "text", "source": source}))
        with self.assertRaises(contracts.AgentContractError):
            contracts.parse_answer_plan(plan(wrong_tool))
        with self.assertRaises(contracts.AgentContractError):
            contracts.parse_answer_plan(plan({"type": "inventory", "source": {"tool": "search_documents", "call": 1}}))

    def test_the_bound_is_what_a_loop_can_hold(self) -> None:
        self.assertGreaterEqual(contracts.MAX_SOURCE_CALL, 3 * 4)  # 3 iterations, several parallel tool uses each


class ThePositionIsResolvedAgainstTheCallLogOfTheSameLoopTests(unittest.TestCase):
    def test_a_plan_that_never_writes_an_id_renders_the_verbatim_tool_result(self) -> None:
        calls = [ok(ID_L1, LIST, LISTING), ok(ID_S1, SEARCH, SEARCH_OK)]
        output, invalid = resolve(plan(text(1)), calls)
        self.assertEqual((output.path.value, output.fallback_reason, output.omitted, invalid), ("grounded", None, (), 0))
        self.assertEqual(output.blocks[0].text, SEARCH_OK["answer"])  # verbatim: the model supplied no content
        inventory_output, _ = resolve(plan(inventory(1)), calls)
        self.assertEqual(inventory_output.path.value, "inventory")

    def test_the_position_counts_the_calls_of_that_tool_only_and_in_stream_order(self) -> None:
        calls = [ok(ID_S1, SEARCH, SEARCH_OK), ok(ID_L1, LIST, LISTING), ok(ID_S2, SEARCH, SEARCH_OK_2)]
        first, _ = resolve(plan(text(1)), calls)
        second, _ = resolve(plan(text(2)), calls)
        self.assertEqual((first.blocks[0].text, second.blocks[0].text), (SEARCH_OK["answer"], "Segunda respuesta."))
        both, _ = resolve(plan(text(1), text(2, transitionBefore="continue_with_text")), calls)
        self.assertEqual([b.kind for b in both.blocks], ["text", "transition", "text"])  # two different calls, two blocks

    def test_a_failed_call_still_counts_but_cannot_be_printed(self) -> None:
        calls = [AgentToolCall(ID_S1, SEARCH, "ERROR", {}, "tool_reported_error"), ok(ID_S2, SEARCH, SEARCH_OK)]
        good, _ = resolve(plan(text(2)), calls)
        self.assertEqual((good.path.value, good.blocks[0].text), ("grounded", SEARCH_OK["answer"]))
        bad, _ = resolve(plan(text(1)), calls)
        self.assertEqual([item.error for item in bad.omitted], [BlockError.SOURCE_CALL_UNUSABLE])
        self.assertEqual(bad.fallback_reason.value, "no_valid_blocks")

    def test_a_position_past_the_last_call_is_unknown_not_guessed(self) -> None:
        output, _ = resolve(plan(text(3)), [ok(ID_S1, SEARCH, SEARCH_OK)])
        self.assertEqual([item.error for item in output.omitted], [BlockError.UNKNOWN_SOURCE_CALL])
        none_made, _ = resolve(plan(inventory(1)), [ok(ID_S1, SEARCH, SEARCH_OK)])
        self.assertEqual([item.error for item in none_made.omitted], [BlockError.UNKNOWN_SOURCE_CALL])

    def test_a_listing_that_fails_its_contract_is_an_unusable_call_not_a_missing_one(self) -> None:
        broken = ok(ID_L1, LIST, {"documents": [{"documentId": "x"}]})
        output, invalid = resolve(plan(inventory(1)), [broken])
        self.assertEqual(([item.error for item in output.omitted], invalid), ([BlockError.SOURCE_CALL_UNUSABLE], 1))

    def test_the_same_call_cannot_be_printed_twice_by_either_form(self) -> None:
        calls = [ok(ID_S1, SEARCH, SEARCH_OK)]
        twice, _ = resolve(plan(text(1), text(1)), calls)
        self.assertEqual([item.error for item in twice.omitted], [BlockError.DUPLICATE_TEXT_BLOCK])
        mixed_forms, _ = resolve(plan(text(1), {"type": "text", "source": {"toolUseId": ID_S1}}), calls)
        self.assertEqual([item.error for item in mixed_forms.omitted], [BlockError.DUPLICATE_TEXT_BLOCK])

    def test_a_copied_id_that_is_wrong_is_still_refused_as_before(self) -> None:
        """What the live model did: an id that matches nothing. The old contract could not render this; the new one does not need the id."""

        output, _ = resolve(plan({"type": "text", "source": {"toolUseId": "tooluse_made_up_by_the_model"}}), [ok(ID_S1, SEARCH, SEARCH_OK)])
        self.assertEqual([item.error for item in output.omitted], [BlockError.UNKNOWN_TOOL_USE_ID])
        self.assertEqual(output.path.value, "tool_result")  # the search result is salvaged, as the D5 tree always did

    def test_data_blocks_are_unchanged_and_mix_with_positions(self) -> None:
        fields = {"documents": [{"documentId": DOC, "name": "Contrato 1.pdf", "documentType": "CONTRACT", "fields": [
            {"name": "explicit_expiration_date", "status": "fact", "value": "2027-03-15", "origin": "LITERAL",
             "evidence": [{"page": 1, "quote": "Vence el 15 de marzo de 2027"}]}]}], "notFound": []}
        data = {"type": "data", "groups": [{"criterion": {"id": "all_documents"}, "refs": [{"documentId": DOC, "field": "explicit_expiration_date"}]}]}
        output, invalid = resolve(plan(data, text(1, transitionBefore="continue_with_text")),
                                  [ok(ID_L1, LIST, LISTING), ok(ID_F1, FIELDS, fields), ok(ID_S1, SEARCH, SEARCH_OK)])
        self.assertEqual((output.path.value, output.omitted, invalid), ("mixed", (), 0))


class EndToEndThroughTheAdapterTests(unittest.TestCase):
    def test_a_stream_with_harness_names_and_a_plan_by_position_renders(self) -> None:
        events = (
            chat.tool_call(ID_L1, "legaldesk_gateway_metadata-mcp___list_matter_documents", LISTING)
            + chat.tool_call(ID_S1, "legaldesk_gateway_search-documents___search_documents", SEARCH_OK)
            + chat.final_message(json.dumps(plan(text(1))))
        )
        result = invoke_agent_chat(HarnessInvoker(chat.StreamClient(events), chat.HARNESS_ARN), "¿Qué dice?", invocation_scope=chat.make_scope(), system_prompt=chat.PROMPT)
        output, _outcome, invalid = AgentChatWorker.resolve_loop(result)
        self.assertEqual((output.path.value, invalid, output.blocks[0].text), ("grounded", 0, SEARCH_OK["answer"]))
        self.assertNotIn(ID_S1, result.text)  # the plan carries no id at all


class TheReportSaysWhetherThePositionWasInRangeTests(unittest.TestCase):
    def observe(self, source, calls):
        obs = ev.Observation()
        plan_text = json.dumps(plan({"type": "text", "source": source}))
        result = AgentChatResult(session_id="s", correlation_id="c", text=plan_text, loop_outcome="completed", iterations=2, stop_reason="end_turn", tool_calls=tuple(calls))
        output, _o, invalid = AgentChatWorker.resolve_loop(result)
        ev._diagnose(obs, result, output, invalid)
        return obs.source_id_matches

    def test_positions_and_ids_get_closed_labels(self) -> None:
        calls = [ok(ID_S1, SEARCH, SEARCH_OK)]
        self.assertEqual(self.observe({"tool": "search_documents", "call": 1}, calls), ["ordinal_in_range"])
        self.assertEqual(self.observe({"tool": "search_documents", "call": 2}, calls), ["ordinal_out_of_range"])
        self.assertEqual(self.observe({"toolUseId": ID_S1}, calls), ["exact"])
        self.assertEqual(self.observe({"toolUseId": "call_1"}, calls), ["none"])


if __name__ == "__main__":
    unittest.main()
