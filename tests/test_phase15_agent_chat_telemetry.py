"""Phase 15 T7: content-free job telemetry (ADR-021 D9): allowlist, value shapes, cost and what the worker emits."""

from __future__ import annotations

import dataclasses
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))
sys.path.insert(0, str(ROOT / "agent" / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from legaldesk import agent_chat_lambda as lam
from legaldesk import agent_chat_telemetry as telemetry
from legaldesk_agent import HarnessUsage
from test_phase15_agent_chat_lambda import QUEUE_ARN, Recorder, record
from test_phase15_agent_chat_worker import (  # noqa: E402 - shared fixtures
    DATA_PLAN, FIELDS_TOOL, SEARCH_TOOL, TEXT_PLAN, World, agent_result, call, fields_payload, rag_ok, search_payload,
)

PROMPT = ("1.2.0", "a" * 64)
SEGMENT_NAMES = {"session", "bind", "history", "agent", "resolve", "publish"}


class SanitizerTests(unittest.TestCase):
    def test_only_allowlisted_keys_with_closed_shaped_values_survive(self) -> None:
        hostile = {
            "claim": "started",
            "outputPath": "El plazo de pago es de 30 días",            # free text in an allowlisted key
            "fallbackReason": "plan_invalid\nINJECTED",                  # control characters
            "failure": "Session_Closed",                                 # not the closed lowercase shape
            "question": "¿Cuál es el importe?",                          # not allowlisted
            "answer": "30 días", "token": "eyJhbGciOi", "subject": "idp|alice",
            "tokens": float("nan"), "attempt": 2,
            "tools": ["search_documents", "create_review_task", "x y z", 7],
            "omittedBlocks": [{"index": 1, "error": "unknown_ref"}, {"index": 2, "error": "Texto libre"},
                              {"index": 3, "error": "unknown_ref", "extra": "x"}],
            "segmentsMs": {"agent": 1200, "resolve": 3, "secret_stage": 9, "bind": "slow"},
            "planError": {"nested": "value"},
        }
        safe = telemetry.sanitize_job_metadata(hostile)
        self.assertEqual(safe, {
            "claim": "started", "attempt": 2,
            "tools": ["search_documents", "other", "other"],   # a non-LegalDesk tool is never named
            "omittedBlocks": [{"index": 1, "error": "unknown_ref"}],
            "segmentsMs": {"resolve": 3, "agent": 1200},
        })
        rendered = json.dumps(safe)
        for fragment in ("plazo", "INJECTED", "importe", "idp|alice", "eyJ", "create_review_task"):
            self.assertNotIn(fragment, rendered)

    def test_nulls_are_dropped_so_a_metric_filter_can_test_presence(self) -> None:
        self.assertEqual(telemetry.sanitize_job_metadata({"fallbackReason": None, "planError": None, "iterations": 0}),
                         {"iterations": 0})
        self.assertEqual(telemetry.sanitize_job_metadata(None), {})

    def test_every_value_a_real_job_emits_passes_the_sanitizer_unchanged(self) -> None:
        # Guards the allowlist against drift: the worker's own vocabulary must not be silently dropped.
        world = World(self)
        job = world.submit()
        world.agent_script = [dataclasses.replace(
            agent_result(TEXT_PLAN, call("tu-1", SEARCH_TOOL, search_payload())),
            usage=HarnessUsage(input_tokens=1000, output_tokens=200, total_tokens=1200), history_messages=2)]
        meta = world.worker.process(job.key).metadata
        self.assertEqual(telemetry.sanitize_job_metadata(meta), {k: v for k, v in meta.items() if v is not None})


class CostAndTimerTests(unittest.TestCase):
    def test_cost_is_the_priced_sum_of_the_four_token_kinds(self) -> None:
        usage = HarnessUsage(input_tokens=1_000_000, output_tokens=100_000, total_tokens=1_100_000,
                             cache_read_input_tokens=2_000_000, cache_write_input_tokens=400_000)
        prices = telemetry.PRICE_PER_MILLION_TOKENS
        expected = prices["input"] + 0.1 * prices["output"] + 2 * prices["cache_read"] + 0.4 * prices["cache_write"]
        self.assertAlmostEqual(telemetry.estimate_loop_cost_usd(usage), expected, places=6)
        self.assertIsNone(telemetry.estimate_loop_cost_usd(None))

    def test_price_table_is_the_published_sonnet_46_price_plus_the_regional_premium(self) -> None:
        # Anthropic list price of Sonnet 4.6 (platform.claude.com pricing, read 2026-11-02) and the
        # 10 % premium of Bedrock regional endpoints, which the `eu.` inference profile is. Changing
        # this table means re-reading both pages and the cost estimate document.
        self.assertEqual(telemetry.PRICE_PER_MILLION_TOKENS, {"input": 3.30, "output": 16.50, "cache_read": 0.33, "cache_write": 4.125})
        self.assertEqual(telemetry.PRICING_VERSION, "estimate-2026-11-eu-regional")
        self.assertEqual(telemetry.usage_metadata(None), {"usageAvailable": False})

    def test_a_segment_that_raises_is_still_timed_and_unknown_segments_are_refused(self) -> None:
        timer = telemetry.SegmentTimer()
        with self.assertRaises(RuntimeError), timer.segment("agent"):
            raise RuntimeError("boom")
        with timer.segment("agent"), timer.segment("resolve"):
            pass
        self.assertEqual(list(timer.as_dict()), ["agent", "resolve"])  # execution order, one entry per stage
        with self.assertRaises(ValueError), timer.segment("free text stage"):
            pass


class WorkerEmitsTheMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.world = World(self)
        self.world.worker.prompt_identity = PROMPT
        self.job = self.world.submit()

    def run_worker(self):
        return self.world.worker.process(self.job.key)

    def test_a_grounded_job_reports_tools_iterations_path_latency_tokens_cost_and_ids(self) -> None:
        usage = HarnessUsage(input_tokens=1500, output_tokens=300, total_tokens=1800)
        self.world.agent_script = [dataclasses.replace(
            agent_result(TEXT_PLAN, call("tu-1", SEARCH_TOOL, search_payload())), usage=usage, history_messages=0)]
        self.world.clock.now += 4  # four seconds between submission and the worker starting
        meta = self.run_worker().metadata
        self.assertEqual((meta["tools"], meta["toolCalls"], meta["toolFailures"], meta["iterations"]), (["search_documents"], 1, 0, 2))
        self.assertEqual((meta["outputPath"], meta["loopOutcome"], meta["stopReason"]), ("grounded", "completed", "end_turn"))
        self.assertEqual((meta["tokens"], meta["inputTokens"], meta["outputTokens"], meta["usageAvailable"]), (1800, 1500, 300, True))
        self.assertAlmostEqual(meta["estimatedLoopCostUsd"], (1500 * 3.30 + 300 * 16.50) / 1_000_000, places=6)
        self.assertEqual(meta["pricingVersion"], telemetry.PRICING_VERSION)
        self.assertEqual(set(meta["segmentsMs"]), SEGMENT_NAMES)  # no fallback stage on this path
        self.assertEqual(meta["queueWaitMs"], 4000)
        self.assertEqual((meta["correlationId"], meta["agentPromptVersion"], meta["agentPromptSha256"]),
                         (self.job.correlation_id, *PROMPT))
        self.assertGreaterEqual(meta["latencyMs"], 0)

    def test_the_fallback_path_adds_its_own_stage_and_keeps_the_reason(self) -> None:
        self.world.rag_response = rag_ok()
        self.world.agent_script = [agent_result("no es un plan", outcome="no_tool_called")]
        meta = self.run_worker().metadata
        self.assertEqual((meta["outputPath"], meta["fallbackReason"], meta["tools"], meta["toolCalls"]),
                         ("rag_fallback", "no_tool_called", [], 0))
        self.assertEqual(set(meta["segmentsMs"]), SEGMENT_NAMES | {"fallback"})
        self.assertFalse(meta["usageAvailable"])
        self.assertNotIn("tokens", telemetry.sanitize_job_metadata(meta))

    def test_failed_tool_calls_history_and_omitted_blocks_are_counted_not_described(self) -> None:
        history = (("user", "pregunta anterior"), ("assistant", "respuesta anterior"))
        self.world.state.add_history_event(("idp|alice-fictional", "conv-1", "sel-1"), "evt-1")
        failing = call("tu-2", FIELDS_TOOL, {"message": "denied"}, status="ERROR")
        self.world.agent_script = [dataclasses.replace(
            agent_result(DATA_PLAN, call("tu-1", FIELDS_TOOL, fields_payload()), failing), history_messages=0)]
        with patch("legaldesk.agent_chat.last_pairs", return_value=history):
            meta = self.run_worker().metadata
        self.assertEqual((meta["toolCalls"], meta["toolFailures"]), (2, 1))
        self.assertEqual((meta["historyMessages"], meta["historyUnavailable"], meta["historyTrimmed"]), (0, True, False))
        self.assertEqual(meta["omittedBlocks"], [])

    def test_the_lambda_log_line_carries_the_telemetry_without_nulls_or_content(self) -> None:
        self.world.agent_script = [dataclasses.replace(
            agent_result(TEXT_PLAN, call("tu-1", SEARCH_TOOL, search_payload("RESPUESTA CONFIDENCIAL"))),
            usage=HarnessUsage(input_tokens=10, output_tokens=5, total_tokens=15))]
        printed: list[str] = []
        with patch("builtins.print", side_effect=lambda text, **_: printed.append(text)):
            lam.handle_sqs_event({"Records": [record(self.job.key)]}, self.world.worker, queue_arn=QUEUE_ARN,
                                 change_visibility=Recorder())
        line = json.loads(printed[0])
        self.assertEqual((line["event"], line["action"], line["jobId"]), ("agent_chat_job", "completed", self.job.job_id))
        self.assertEqual((line["tokens"], line["outputPath"], line["correlationId"]), (15, "grounded", self.job.correlation_id))
        self.assertNotIn("fallbackReason", line)  # absent, not null: the metric filter tests presence
        self.assertNotIn("null", printed[0])
        for fragment in ("CONFIDENCIAL", "confidencial", "access-token", "idp|alice", "Alfa.pdf"):
            self.assertNotIn(fragment, printed[0])

    def test_a_failed_job_logs_its_closed_failure_code_only(self) -> None:
        self.world.state.sessions.clear()  # the user logged out
        printed: list[str] = []
        with patch("builtins.print", side_effect=lambda text, **_: printed.append(text)):
            lam.handle_sqs_event({"Records": [record(self.job.key)]}, self.world.worker, queue_arn=QUEUE_ARN,
                                 change_visibility=Recorder())
        line = json.loads(printed[0])
        self.assertEqual((line["action"], line["failure"], line["quotaReleased"]), ("failed", "session_closed", True))


if __name__ == "__main__":
    unittest.main()
