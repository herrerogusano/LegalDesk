"""F-36/37/38: production adapters, provider-shaped doubles, no cloud calls."""
from __future__ import annotations

import contextlib
import io
import json
import unittest
from dataclasses import replace
from unittest.mock import patch

import test_grounding_scores as scores
import test_model_output_reasons as reasons
import test_phase15_agent_chat_worker as worker
from test_phase13_grounding import FakeGuardrail, contextual_filters
from test_phase15_agent_chat_client import HARNESS_ARN, PROMPT, StreamClient, final_message, make_scope, tool_call, SEARCH
from legaldesk.agent_chat_telemetry import sanitize_job_metadata
from legaldesk.agent_contracts import parse_answer_plan, AgentContractError, plan_diagnostics
from legaldesk.guardrails import GuardrailGroundingValidator, GroundingGuardrailBlocked
from legaldesk_agent import HarnessInvoker, invoke_agent_chat
from legaldesk_agent import client as stream_client


class PolicyDiagnostics(unittest.TestCase):
    run_assessment = scores.GroundingScores.run_assessment

    def test_documented_masked_and_blocked_outputs_still_block_through_real_chat(self):
        # AWS ApplyGuardrail docs: outputs[].text, piiEntities[].type/action/match,
        # topicPolicy.topics[].type/name/action, contextualGroundingPolicy.filters[].
        for action, topic in (("ANONYMIZED", False), ("BLOCKED", False), ("ANONYMIZED", True)):
            with self.subTest(action=action, topic=topic):
                assessments = [contextual_filters(grounding_score=1.0, relevance_score=1.0),
                    {"sensitiveInformationPolicy": {"piiEntities": [
                        {"type": "NAME", "action": action, "match": "DO_NOT_EMIT", "detected": True}], "regexes": []}}]
                if topic:
                    assessments.append({"topicPolicy": {"topics": [{"name": "DO_NOT_EMIT", "type": "DENY", "action": "BLOCKED"}]}})
                provider = FakeGuardrail({"action": "GUARDRAIL_INTERVENED", "outputs": [{"text": "DO_NOT_EMIT"}] * (3 if action == "ANONYMIZED" and not topic else 1), "assessments": assessments})
                grounder = GuardrailGroundingValidator(provider, reasons.GUARDRAIL_CONFIG)
                writer_adapter = reasons.ThroughTheRealWriterAdapter().adapter({"answer": "California law governs."})
                with patch.object(reasons, "FakeGroundingValidator", return_value=grounder):
                    response, sink = reasons.run(reasons.OPTIONAL_PASSAGE, writer=writer_adapter)
                self.assertEqual(response.operation_status, "blocked")
                events = [e.to_dict() for e in sink.events]
                terminal = next(e for e in events if e.get("operation") == "grounding_validate" and e["outcome"] == "blocked")
                labels = terminal["grounding_diagnostics"]["policyInterventions"]
                self.assertIn(f"sensitiveInformationPolicy:NAME:{action}", labels)
                if topic:
                    self.assertIn("topicPolicy:DENY:BLOCKED", labels)
                self.assertEqual((terminal["grounding_score"], terminal["relevance_score"]), (1.0, 1.0))
                request = provider.calls[0]
                self.assertEqual(request["outputScope"], "FULL")
                sources = [b["text"]["text"] for b in request["content"] if b["text"].get("qualifiers") == ["grounding_source"]]
                self.assertEqual(terminal["grounding_diagnostics"]["sourceChars"], sum(map(len, sources)))
                self.assertEqual(terminal["grounding_diagnostics"]["sourceCount"], len(sources))
                self.assertNotIn("DO_NOT_EMIT", json.dumps(events))

    def test_unknown_provider_values_cannot_become_diagnostic_text(self):
        _, event, _, _ = self.run_assessment([
            contextual_filters(), {"sensitiveInformationPolicy": {"piiEntities": [
                {"type": "DO_NOT_EMIT", "action": "ANONYMIZED", "match": "DO_NOT_EMIT"}]}}], "GUARDRAIL_INTERVENED")
        self.assertEqual(event.grounding_diagnostics["policyInterventions"], ["sensitiveInformationPolicy:OTHER:ANONYMIZED"])
        for bad in ({"policyInterventions": ["DO_NOT_EMIT"]}, dict(event.grounding_diagnostics, sourceChars=True),
                    dict(event.grounding_diagnostics, answerLanguageHint="DO_NOT_EMIT")):
            with self.assertRaises(ValueError):
                replace(event, grounding_diagnostics=bad)


class StreamLimits(unittest.TestCase):
    def invoke(self, events):
        with contextlib.redirect_stdout(io.StringIO()):
            return invoke_agent_chat(HarnessInvoker(StreamClient(events), HARNESS_ARN), "synthetic",
                                     invocation_scope=make_scope(), system_prompt=PROMPT)

    def test_more_than_256_provider_shaped_deltas_complete(self):
        events = tool_call("t1", SEARCH, worker.search_payload())
        events += [{"messageStart": {"role": "assistant"}}]
        events += [{"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": " "}}}] * 300
        events += final_message("{}")[1:]
        result = self.invoke(events)
        self.assertEqual(result.loop_outcome, "completed")
        self.assertIsNone(result.stream_error_hint)

    def test_event_limit_at_and_over_with_provider_shaped_stream(self):
        events = tool_call("t1", SEARCH, worker.search_payload()) + final_message("{}")
        # Isolate the counter at a small bound; at the production bound any real
        # Converse frame has already exceeded the byte cap. No giant fixture.
        with patch.object(stream_client, "_MAX_STREAM_EVENTS", len(events)):
            self.assertEqual(self.invoke(events).loop_outcome, "completed")
            cut = self.invoke(events + [{"metadata": {}}])
        self.assertEqual((cut.loop_outcome, cut.stream_error_hint), ("failed", "stream_limit_exceeded"))
        self.assertEqual(len(cut.tool_calls), 1)  # completed result available for deterministic salvage
        self.assertEqual(stream_client._MAX_STREAM_EVENTS * len(json.dumps({}).encode()), stream_client._MAX_STREAM_BYTES)

    def test_actual_byte_limit_at_and_over_and_partial_result_recovery(self):
        events = tool_call("t1", SEARCH, worker.search_payload()) + final_message("{}")
        pad = {"metadata": {"padding": ""}}
        used = sum(len(json.dumps(e, ensure_ascii=False, default=str).encode()) for e in events + [pad])
        pad["metadata"]["padding"] = "x" * (stream_client._MAX_STREAM_BYTES - used)
        events.append(pad)
        self.assertEqual(self.invoke(events).loop_outcome, "completed")
        cut = self.invoke(events + [{"metadata": {}}])
        self.assertEqual((cut.loop_outcome, cut.stream_error_hint), ("failed", "stream_limit_exceeded"))
        self.assertEqual(len(cut.tool_calls), 1)

    def test_search_questions_are_compared_without_retaining_arguments(self):
        for argument, expected in ((json.dumps({"question": "synthetic"}), "same"),
                                   (json.dumps({"question": "rewritten synthetic"}), "different"),
                                   ('{"question":"a","question":"b"}', "unavailable"),
                                   ('{"question":', "unavailable")):
            events = tool_call("t1", SEARCH, worker.search_payload())
            events.insert(2, {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"toolUse": {"input": argument}}}})
            result = self.invoke(events + final_message("{}"))
            self.assertEqual(result.search_query_comparisons, (expected,))
        result = self.invoke(tool_call("t1", SEARCH, worker.search_payload()) + final_message("{}"))
        self.assertEqual(result.search_query_comparisons, ("unavailable",))


class PlanDiagnostics(unittest.TestCase):
    def test_real_parser_stage_and_worker_metadata_without_plan(self):
        for text, expected in (("prefix {}", "json_decode"), ('{"version":', "json_decode"),
                               ('{"version":1,"blocks":[{}]}', "schema")):
            with self.subTest(expected=expected):
                with self.assertRaises(AgentContractError) as caught:
                    parse_answer_plan(text)
                world = worker.World(self)
                world.agent_script = [worker.agent_result(text, worker.call("tu-1", worker.SEARCH_TOOL, worker.search_payload()))]
                job = world.submit()
                result = world.worker.process(job.key)
                meta = sanitize_job_metadata(result.metadata)
                self.assertEqual(meta["planParseStage"], expected)
                self.assertEqual(meta["planChars"], len(text))
                self.assertEqual(meta["planHasJsonStart"], True)
                self.assertEqual(meta["planHasJsonEnd"], "}" in text)
                self.assertEqual(meta["stopReason"], "end_turn")
                self.assertNotIn(text, json.dumps(meta))
                self.assertEqual(meta["planError"], caught.exception.code)

    def test_prose_around_a_fenced_plan_reaches_the_user_as_the_plan_and_never_as_text(self):
        prose = "PROSE-THAT-MUST-NOT-BE-SHOWN I have all the data I need."
        text = f"{prose}\n\n```json\n{json.dumps(worker.TEXT_PLAN, indent=2)}\n```"
        world = worker.World(self)
        world.agent_script = [worker.agent_result(text, worker.call("tu-1", worker.SEARCH_TOOL, worker.search_payload()))]
        job = world.submit()
        result = world.worker.process(job.key)
        meta = sanitize_job_metadata(result.metadata)
        self.assertEqual(meta["planParseStage"], "accepted")
        self.assertIsNone(meta.get("fallbackReason"))
        body = world.jobs.get(job.key).result
        self.assertEqual(body["path"], "grounded")
        self.assertNotIn("PROSE-THAT-MUST-NOT-BE-SHOWN", json.dumps(body, default=str))
        self.assertNotIn("PROSE-THAT-MUST-NOT-BE-SHOWN", json.dumps(meta, default=str))

    def test_diagnostic_input_is_closed_and_does_not_relax_the_parser(self):
        self.assertEqual(plan_diagnostics("", None, attempted=False)["planParseStage"], "not_attempted")
        self.assertEqual(sanitize_job_metadata({"planParseStage": "DO_NOT_EMIT", "planChars": True,
            "planHasJsonStart": "DO_NOT_EMIT", "stopReason": "DO_NOT_EMIT"}), {"stopReason": "other"})
        valid = "```json\n" + json.dumps(worker.TEXT_PLAN) + "\n```"
        parse_answer_plan(valid)  # one fence, with or without prose around it (F-37)
        parse_answer_plan("prose\n" + valid)
        with self.assertRaises(AgentContractError):
            parse_answer_plan("prose\n" + json.dumps(worker.TEXT_PLAN))
        with self.assertRaises(AgentContractError):
            parse_answer_plan(valid + "\n" + valid)

    def test_report_carries_the_actual_parser_diagnostics(self):
        from evals import phase15_routing_eval as ev
        case = ev.load_dataset()[0]["cases"][0]
        loop = worker.agent_result('{"version":', worker.call("tu-1", worker.SEARCH_TOOL, worker.search_payload()))
        output, _, invalid = worker.AgentChatWorker.resolve_loop(loop)
        obs = ev.Observation()
        ev._diagnose(obs, loop, output, invalid)
        report = ev.score_case(case, obs, offline=True)
        self.assertEqual(report["diagnostics"]["planParseStage"], "json_decode")
        self.assertEqual(report["diagnostics"]["planChars"], len(loop.text))
        self.assertFalse(report["diagnostics"]["planHasJsonEnd"])


if __name__ == "__main__":
    unittest.main()
