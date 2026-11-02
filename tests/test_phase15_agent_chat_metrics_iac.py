"""Phase 15 T7: job metric filters and alarms match the log lines the worker really prints."""

from __future__ import annotations

import dataclasses
import json
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))
sys.path.insert(0, str(ROOT / "agent" / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from legaldesk import agent_chat_lambda as lam
from legaldesk_agent import HarnessUsage
from test_phase15_agent_chat_lambda import QUEUE_ARN, Recorder, record
from test_phase15_agent_chat_worker import (  # noqa: E402 - shared fixtures
    SEARCH_TOOL, TEXT_PLAN, World, agent_result, call, rag_ok, search_payload,
)

TEMPLATE = Path(__file__).parents[1] / "infra" / "cloudformation" / "phase-14-public-edge.yaml"
NAMESPACE = "LegalDesk/Phase15AgentChat"
FILTERS = (
    "AgentChatJobDurationMetric", "AgentChatJobCompletedMetric", "AgentChatJobFailureMetric",
    "AgentChatJobFailureByCodeMetric", "AgentChatFallbackMetric", "AgentChatFallbackByReasonMetric",
)
ALARMS = (
    "AgentChatJobDurationAlarm", "AgentChatJobFailureAlarm", "AgentChatFallbackRateAlarm", "AgentChatQueueAgeAlarm",
)


def matches(pattern: str, line: dict) -> bool:
    """Evaluate the subset of the CloudWatch JSON filter syntax the template uses."""

    expression = pattern.strip()
    assert expression.startswith("{") and expression.endswith("}"), pattern
    expression = expression[1:-1]
    expression = re.sub(r'\$\.(\w+) = \*', r'has("\1")', expression)
    expression = re.sub(r'\$\.(\w+) = "([^"]*)"', r'eq("\1", "\2")', expression)
    expression = expression.replace("&&", " and ").replace("||", " or ")
    assert "$" not in expression, f"unsupported syntax in {pattern}"
    return bool(eval(expression, {"__builtins__": {}}, {  # noqa: S307 - test-only, closed grammar
        "has": lambda name: name in line, "eq": lambda name, value: line.get(name) == value}))


class AgentChatMetricsTemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.template = TEMPLATE.read_text(encoding="utf-8")

    def block(self, resource: str) -> str:
        body = self.template.split(f"\n  {resource}:", 1)[1]
        return re.split(r"(?m)^  [A-Za-z][A-Za-z0-9]*:\s*$", body, maxsplit=1)[0]

    def pattern(self, resource: str) -> str:
        return re.search(r"FilterPattern: '(.*)'", self.block(resource)).group(1)

    def test_everything_is_behind_the_agent_chat_flag_and_in_one_namespace(self) -> None:
        for resource in FILTERS + ALARMS:
            block = self.block(resource)
            self.assertIn("Condition: AgentChatEnabled", block, resource)
        for resource in FILTERS:
            self.assertIn("LogGroupName: !Ref AgentChatWorkerLogGroup", self.block(resource))
            self.assertIn(f"MetricNamespace: {NAMESPACE}", self.block(resource))
        for resource in ALARMS[:2]:
            self.assertIn(f"Namespace: {NAMESPACE}", self.block(resource))

    def test_alarms_notify_the_same_optional_topic_as_the_dlq_alarm(self) -> None:
        for resource in ALARMS:
            block = self.block(resource)
            self.assertIn("ActionsEnabled: !If [HasAgentChatAlarmTopic, true, false]", block, resource)
            self.assertIn("- - !Ref AgentChatAlarmTopicArn", block, resource)
            self.assertIn("TreatMissingData: notBreaching", block, resource)  # a quiet beta is not an incident

    def test_the_api_latency_alarm_is_not_applied_to_the_job(self) -> None:
        duration = self.block("AgentChatJobDurationAlarm")
        self.assertIn("ExtendedStatistic: p95", duration)
        self.assertIn("Threshold: 90000", duration)  # the Harness loop limit, not 3 s
        self.assertNotRegex(duration, r"Threshold: 3000\b")

    def test_queue_age_alarm_watches_the_work_queue_not_the_dlq(self) -> None:
        age = self.block("AgentChatQueueAgeAlarm")
        self.assertIn("MetricName: ApproximateAgeOfOldestMessage", age)
        self.assertIn("Value: !GetAtt AgentChatQueue.QueueName", age)
        self.assertIn("Threshold: 120", age)  # well inside the 5-minute job deadline

    def test_fallback_rate_needs_a_minimum_sample_and_references_declared_series(self) -> None:
        alarm = self.block("AgentChatFallbackRateAlarm")
        expression = re.search(r"Expression: (.*)", alarm).group(1)
        self.assertIn("IF(completed >= 5,", expression)
        ids = set(re.findall(r"- Id: (\w+)", alarm))
        self.assertEqual(ids, {"rate", "fallbacks", "completed"})
        self.assertEqual(set(re.findall(r"MetricName: (\w+)", alarm)), {"AgentChatFallbacks", "AgentChatJobsCompleted"})
        self.assertEqual(len(re.findall(r"ReturnData: true", alarm)), 1)  # exactly one series is evaluated


class FiltersMatchWhatTheWorkerPrintsTests(unittest.TestCase):
    """The contract between the Lambda log line and the template, tested with real lines."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.metrics = AgentChatMetricsTemplateTests()
        cls.metrics.setUpClass()
        cls.patterns = {name: cls.metrics.pattern(name) for name in FILTERS}

    def lines(self, world, job, *, script, rag=None, before=None):
        world.agent_script, world.rag_response = script, rag
        if before:
            before()
        printed: list[str] = []
        with patch("builtins.print", side_effect=lambda text, **_: printed.append(text)):
            lam.handle_sqs_event({"Records": [record(job.key)]}, world.worker, queue_arn=QUEUE_ARN, change_visibility=Recorder())
        return [json.loads(item) for item in printed if '"agent_chat_job"' in item]

    def fired(self, line: dict) -> set[str]:
        return {name for name, pattern in self.patterns.items() if matches(pattern, line)}

    def test_a_clean_completed_job_counts_as_completed_with_a_duration_and_no_fallback(self) -> None:
        world = World(self)
        job = world.submit()
        (line,) = self.lines(world, job, script=[agent_result(TEXT_PLAN, call("tu-1", SEARCH_TOOL, search_payload()))])
        self.assertEqual(self.fired(line), {"AgentChatJobDurationMetric", "AgentChatJobCompletedMetric"})
        self.assertIsInstance(line["latencyMs"], int)  # the value the duration filter extracts

    def test_a_fallback_counts_as_completed_and_as_fallback_by_reason(self) -> None:
        world = World(self)
        job = world.submit()
        (line,) = self.lines(world, job, script=[agent_result("sin plan", outcome="no_tool_called")], rag=rag_ok())
        self.assertEqual(line["fallbackReason"], "no_tool_called")  # the dimension value
        self.assertEqual(self.fired(line), {
            "AgentChatJobDurationMetric", "AgentChatJobCompletedMetric", "AgentChatFallbackMetric", "AgentChatFallbackByReasonMetric"})

    def test_a_reused_tool_result_is_also_a_fallback(self) -> None:
        world = World(self)
        job = world.submit()
        script = [agent_result("no es un plan", call("tu-1", SEARCH_TOOL, search_payload()))]
        (line,) = self.lines(world, job, script=script)
        self.assertEqual((line["outputPath"], line["fallbackReason"]), ("tool_result", "plan_invalid"))
        self.assertIn("AgentChatFallbackMetric", self.fired(line))

    def test_a_failed_job_counts_once_as_a_failure_with_its_code_and_not_as_completed(self) -> None:
        world = World(self)
        job = world.submit()
        (line,) = self.lines(world, job, script=[], before=world.state.sessions.clear)  # session closed
        self.assertEqual(line["failure"], "session_closed")  # the dimension value
        self.assertEqual(self.fired(line), {"AgentChatJobFailureMetric", "AgentChatJobFailureByCodeMetric"})

    def test_an_unclassified_error_is_a_failure_without_a_code(self) -> None:
        world = World(self)
        printed: list[str] = []
        with patch("builtins.print", side_effect=lambda text, **_: printed.append(text)):
            lam.handle_sqs_event({"Records": [record(world.submit().key, body="{}")]}, world.worker, queue_arn=QUEUE_ARN,
                                 change_visibility=Recorder())
        line = json.loads(printed[0])
        self.assertEqual(self.fired(line), {"AgentChatJobFailureMetric"})

    def test_retries_and_duplicates_are_not_counted_as_anything(self) -> None:
        class Throttled(Exception):
            response = {"Error": {"Code": "ThrottlingException"}}

        world = World(self)
        job = world.submit()
        (retry,) = self.lines(world, job, script=[Throttled()])
        self.assertEqual((retry["action"], self.fired(retry)), ("retry", set()))
        done = World(self)
        finished = done.submit()
        self.lines(done, finished, script=[dataclasses.replace(
            agent_result(TEXT_PLAN, call("tu-1", SEARCH_TOOL, search_payload())), usage=HarnessUsage(total_tokens=1))])
        (duplicate,) = self.lines(done, finished, script=[])
        self.assertEqual((duplicate["action"], self.fired(duplicate)), ("ignored", set()))

    def test_the_filter_grammar_helper_really_discriminates(self) -> None:
        pattern = '{ $.event = "agent_chat_job" && ($.action = "failed" || $.action = "error") }'
        self.assertTrue(matches(pattern, {"event": "agent_chat_job", "action": "error"}))
        self.assertFalse(matches(pattern, {"event": "agent_chat_job", "action": "completed"}))
        self.assertFalse(matches(pattern, {"event": "other", "action": "failed"}))
        self.assertFalse(matches('{ $.x = * }', {"y": 1}))


if __name__ == "__main__":
    unittest.main()
