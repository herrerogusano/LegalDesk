"""The tool-name format Harness uses in its stream (T9 step 11, 2026-11-03).

``allowedTools`` holds ``@legaldesk_gateway/search-documents___search_documents`` and the stream names the same call
``legaldesk_gateway_search-documents___search_documents``. The parser used to reject the stream (``unknown or invalid Harness tool use``)
although Harness had run the tool. It now accepts exactly the image of each permitted entry and normalises it to the entry, so that
the call log, the output tree and the telemetry see one name.
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

import test_phase15_agent_chat_client as base  # noqa: E402 - the stream double and the sealed agent_chat scope
from legaldesk import agent_contracts as contracts  # noqa: E402
from legaldesk.agent_chat import AgentChatWorker  # noqa: E402
from legaldesk_agent import HarnessInvocationError, HarnessInvoker, invoke_agent_chat  # noqa: E402
from legaldesk_agent.client import (  # noqa: E402
    ClientStreamParserError,
    _canonical_tool_name,
    _harness_stream_form,
    _structured_tool_results,
    ERROR_HINTS,
)

FIXTURE = ROOT / "tests" / "fixtures" / "phase15_live" / "harness-stream-gateway-names.json"
SEARCH, FIELDS = base.SEARCH, base.FIELDS
LIST = "@legaldesk_gateway/metadata-mcp___list_matter_documents"
REVIEW = base.REVIEW
# What the live stream called them
SEARCH_STREAM = "legaldesk_gateway_search-documents___search_documents"
FIELDS_STREAM = "legaldesk_gateway_metadata-mcp___get_extracted_fields"
LIST_STREAM = "legaldesk_gateway_metadata-mcp___list_matter_documents"
AGENT_SCOPE_TOOLS = (SEARCH, FIELDS, LIST)  # the allowlist of an agent_chat scope


def fixture_events() -> list[dict]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["events"]


class TheNameHarnessReallyUsesTests(unittest.TestCase):
    def test_the_fixture_carries_the_observed_format_and_no_allowlist_entry(self) -> None:
        names = [e["contentBlockStart"]["start"]["toolUse"]["name"] for e in fixture_events() if "toolUse" in e.get("contentBlockStart", {}).get("start", {})]
        self.assertEqual(names, [SEARCH_STREAM, FIELDS_STREAM, LIST_STREAM])
        self.assertTrue(all(not name.startswith("@") and "/" not in name for name in names))

    def test_the_stream_is_accepted_and_every_call_is_normalised_to_its_allowlist_entry(self) -> None:
        calls = _structured_tool_results(fixture_events(), allowed_tools=AGENT_SCOPE_TOOLS, raw=True, allow_incomplete=True)
        self.assertEqual([(c.tool_use_id, c.name, c.status) for c in calls],
                         [("tooluse_search", SEARCH, "SUCCESS"), ("tooluse_fields", FIELDS, "SUCCESS"), ("tooluse_list", LIST, "SUCCESS")])

    def test_through_the_real_adapter_the_call_log_and_the_output_tree_recognise_the_three_tools(self) -> None:
        result = invoke_agent_chat(HarnessInvoker(base.StreamClient(fixture_events()), base.HARNESS_ARN), "¿Qué dice?",
                                   invocation_scope=base.make_scope(), system_prompt=base.PROMPT)
        self.assertEqual(result.loop_outcome, "completed")
        self.assertEqual([c.short_name for c in result.tool_calls], [contracts.SEARCH_DOCUMENTS, contracts.GET_EXTRACTED_FIELDS, contracts.LIST_MATTER_DOCUMENTS])
        self.assertEqual([c.name for c in result.tool_calls], [SEARCH, FIELDS, LIST])
        output, outcome, invalid = AgentChatWorker.resolve_loop(result)
        self.assertEqual(invalid, 0)  # the three results were parsed by their contracts: none was skipped as an unknown tool
        self.assertEqual(outcome.value, "completed")
        self.assertIsNone(output.fallback_reason)

    def test_the_review_tool_pair_of_the_non_agentic_path_is_accepted_the_same_way(self) -> None:
        events = base.tool_call("tu-1", "legaldesk_gateway_review-task-lambda___create_review_task", {"reviewTaskId": "review-1", "status": "pending"})
        (call,) = _structured_tool_results(events, allowed_tools=(REVIEW,))
        self.assertEqual((call.name, call.payload["reviewTaskId"]), (REVIEW, "review-1"))
        # and with the default allowlist (no scope), which is what the review button path uses
        (call,) = _structured_tool_results(events)
        self.assertEqual(call.name, REVIEW)


class OnlyTheExactImageOfAnAllowedEntryIsAcceptedTests(unittest.TestCase):
    def test_the_mapping_is_group_slash_rest_to_group_underscore_rest(self) -> None:
        self.assertEqual(_harness_stream_form(SEARCH), SEARCH_STREAM)
        self.assertEqual(_harness_stream_form(FIELDS), FIELDS_STREAM)
        for malformed in ("search_documents", "@nogroup", "@/rest", "@group/", "@a/b/c", "a/b___c", ""):
            self.assertIsNone(_harness_stream_form(malformed), malformed)

    def test_names_that_look_alike_but_are_not_the_image_of_an_allowed_entry_are_rejected(self) -> None:
        for name in (
            "other_gateway_search-documents___search_documents",           # another group
            "legaldesk_gateway_review-task-lambda___create_review_task",   # another target: not in the agent_chat scope
            "legaldesk_gateway_search-documents___get_extracted_fields",   # the right target with another target's tool
            "legaldesk_gateway_metadata-mcp___get_document_metadata",      # a real tool, outside this scope
            SEARCH_STREAM + "x", " " + SEARCH_STREAM, SEARCH_STREAM + " ", SEARCH_STREAM.upper(),
            "legaldesk_gateway/search-documents___search_documents",       # slash form without the @
            "@" + SEARCH_STREAM,                                           # the @ with the stream separators
            "legaldesk_gateway_search-documents", "legaldesk_gateway_search_documents", "legaldesk_gateway_",
            "_search-documents___search_documents", "legaldesk_gateway_search-documents___search_documents___x",
        ):
            with self.subTest(name=name):
                self.assertIsNone(_canonical_tool_name(name, AGENT_SCOPE_TOOLS))
                with self.assertRaises(ValueError):
                    _structured_tool_results(base.tool_call("tu-1", name, {"x": 1}), allowed_tools=AGENT_SCOPE_TOOLS, raw=True)

    def test_the_form_is_accepted_only_for_the_entries_of_the_scope(self) -> None:
        self.assertEqual(_canonical_tool_name(SEARCH_STREAM, (SEARCH,)), SEARCH)
        self.assertIsNone(_canonical_tool_name(FIELDS_STREAM, (SEARCH,)))  # permitted in general, not in this scope
        self.assertEqual(_canonical_tool_name(FIELDS_STREAM, None), FIELDS)  # no scope: the five tools of the integration

    def test_what_was_accepted_before_is_unchanged(self) -> None:
        for name in (SEARCH, "search_documents"):
            self.assertEqual(_canonical_tool_name(name, AGENT_SCOPE_TOOLS), name)
        self.assertIsNone(_canonical_tool_name(REVIEW, AGENT_SCOPE_TOOLS))
        self.assertIsNone(_canonical_tool_name("", AGENT_SCOPE_TOOLS))
        self.assertIsNone(_canonical_tool_name(None, AGENT_SCOPE_TOOLS))
        self.assertIsNone(_canonical_tool_name("x" * 257, AGENT_SCOPE_TOOLS))

    def test_two_entries_with_the_same_stream_form_are_never_resolved(self) -> None:
        ambiguous = ("@a_b/c___t", "@a/b_c___t")
        self.assertEqual({_harness_stream_form(entry) for entry in ambiguous}, {"a_b_c___t"})
        self.assertIsNone(_canonical_tool_name("a_b_c___t", ambiguous))

    def test_the_normalisation_does_not_widen_the_strict_parts_of_the_parser(self) -> None:
        # a result still needs its use, a use still cannot repeat, and an unknown name still fails the whole stream
        events = base.tool_call("tu-1", SEARCH_STREAM, {"answer": "x"})
        with self.assertRaises(ValueError):
            _structured_tool_results(events + base.tool_call("tu-1", SEARCH_STREAM, {"answer": "x"}), allowed_tools=AGENT_SCOPE_TOOLS, raw=True)
        with self.assertRaises(ValueError):
            _structured_tool_results(events + base.tool_call("tu-2", "legaldesk_gateway_unknown___tool", {}), allowed_tools=AGENT_SCOPE_TOOLS, raw=True)


class AFailedToolResultSaysWhyWithAClosedLabelTests(unittest.TestCase):
    """T9 step 11 (second report): a failed search_documents call left only status=ERROR; the reason is now a closed label, never the text."""

    def parse(self, status: str, text: str | None):
        events = base.tool_call("tu-1", SEARCH, text, status=status)
        (call,) = _structured_tool_results(events, allowed_tools=AGENT_SCOPE_TOOLS, raw=True, allow_incomplete=True)
        return call

    def test_labels_for_the_reasons_a_gateway_or_harness_would_give(self) -> None:
        cases = {
            "Input validation error: 'matterId' is a required property": "missing_required_argument",
            '{"jsonrpc":"2.0","id":1,"error":{"code":-32001,"message":"access denied"}}': "access_denied",
            "Tool search_documents is not allowed": "tool_not_allowed",
            "Arguments failed schema validation": "invalid_arguments",
            "The request timed out": "timeout",
            "ThrottlingException: Rate exceeded": "throttled",
            "something we have never seen": "unclassified",
        }
        for text, expected in cases.items():
            with self.subTest(text):
                call = self.parse("error", text)
                self.assertEqual((call.status, call.error_hint), ("ERROR", expected))
                self.assertIn(call.error_hint, ERROR_HINTS)

    def test_a_successful_call_has_no_hint_and_an_empty_success_is_a_no_text_error(self) -> None:
        ok = self.parse("success", json.dumps({"content": [{"type": "text", "text": json.dumps({"answer": "x"})}]}))
        self.assertEqual((ok.status, ok.error_hint), ("SUCCESS", None))
        empty = self.parse("success", "")
        self.assertEqual((empty.status, empty.error_hint), ("ERROR", "no_text"))

    def test_the_hint_reaches_the_call_log_of_the_worker(self) -> None:
        events = base.tool_call("tu-1", SEARCH_STREAM, "Input validation error: 'matterId' is a required property") + base.final_message("{}")
        result = invoke_agent_chat(HarnessInvoker(base.StreamClient(events), base.HARNESS_ARN), "q", invocation_scope=base.make_scope(), system_prompt=base.PROMPT)
        (call,) = result.tool_calls
        self.assertEqual((call.short_name, call.status, call.error_hint), ("search_documents", "ERROR", "missing_required_argument"))


class TheParserRejectionHasItsOwnExceptionTests(unittest.TestCase):
    def test_an_unknown_name_raises_the_parser_error_which_is_still_a_harness_invocation_error(self) -> None:
        events = base.tool_call("tu-1", "other_gateway_search-documents___search_documents", {"answer": "x"}) + base.final_message("{}")
        with self.assertRaises(ClientStreamParserError) as caught:
            invoke_agent_chat(HarnessInvoker(base.StreamClient(events), base.HARNESS_ARN), "q", invocation_scope=base.make_scope(), system_prompt=base.PROMPT)
        self.assertIsInstance(caught.exception, HarnessInvocationError)
        self.assertEqual(str(caught.exception), "AgentCore returned an invalid event stream")  # unchanged for existing callers

    def test_what_harness_itself_refuses_is_not_the_parser_error(self) -> None:
        class Validation(Exception):
            response = {"Error": {"Code": "ValidationException"}}

        with self.assertRaises(Validation):  # at request time: propagates untouched for the caller to classify
            invoke_agent_chat(HarnessInvoker(base.StreamClient(raises=Validation("tools not allowed")), base.HARNESS_ARN), "q",
                              invocation_scope=base.make_scope(), system_prompt=base.PROMPT)
        # in the stream: an error event of Harness ends the loop as a failed one, it is not a parser rejection
        result = invoke_agent_chat(HarnessInvoker(base.StreamClient([{"runtimeClientError": {"message": "tool not allowed"}}]), base.HARNESS_ARN), "q",
                                   invocation_scope=base.make_scope(), system_prompt=base.PROMPT)
        self.assertEqual(result.loop_outcome, "failed")


if __name__ == "__main__":
    unittest.main()
