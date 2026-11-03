"""Phase 15 T7: job alarms use native CloudWatch metrics; the only custom metrics match the log lines the worker really prints."""

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
FILTERS = ("AgentChatJobFailureMetric", "AgentChatFallbackMetric")
ALARMS = (
    "AgentChatJobDurationAlarm", "AgentChatWorkerErrorsAlarm", "AgentChatJobFailureAlarm", "AgentChatFallbackRateAlarm",
    "AgentChatQueueAgeAlarm", "AgentChatDeadLetterQueueAlarm",
)
REMOVED = (
    "AgentChatJobDurationMetric", "AgentChatJobCompletedMetric", "AgentChatJobFailureByCodeMetric", "AgentChatFallbackByReasonMetric",
    "AgentChatJobDurationMs", "AgentChatJobsCompleted", "AgentChatJobFailuresByCode", "AgentChatFallbacksByReason",
)
# Native, free metrics the alarms read. Anything else must be a custom metric listed (and justified) in FILTERS.
NATIVE = {
    "AWS/SQS": {"ApproximateAgeOfOldestMessage", "ApproximateNumberOfMessagesVisible"},
    "AWS/Lambda": {"Duration", "Errors", "Invocations"},
}
CUSTOM_METRICS = {"AgentChatFallbacks", "AgentChatJobFailures"}


def matches(pattern: str, line: dict) -> bool:
    """Evaluate the subset of the CloudWatch JSON filter syntax the template uses."""

    expression = pattern.strip()
    assert expression.startswith("{") and expression.endswith("}"), pattern
    expression = expression[1:-1]
    expression = re.sub(r'\$\.(\w+) = \*', r'has("\1")', expression)
    expression = re.sub(r'\$\.(\w+) = "([^"]*)"', r'eq("\1", "\2")', expression)
    expression = re.sub(r'\$\.(\w+) != "([^"]*)"', r'ne("\1", "\2")', expression)
    expression = expression.replace("&&", " and ").replace("||", " or ")
    assert "$" not in expression, f"unsupported syntax in {pattern}"
    return bool(eval(expression, {"__builtins__": {}}, {  # noqa: S307 - test-only, closed grammar
        "has": lambda name: name in line, "eq": lambda name, value: line.get(name) == value,
        "ne": lambda name, value: name in line and line[name] != value}))


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
            self.assertIn("Condition: AgentChatEnabled", self.block(resource), resource)
        for resource in FILTERS:
            self.assertIn("LogGroupName: !Ref AgentChatWorkerLogGroup", self.block(resource))
            self.assertIn(f"MetricNamespace: {NAMESPACE}", self.block(resource))

    def test_only_two_custom_metrics_remain_and_none_has_a_dimension(self) -> None:
        # Each dimension value is a separate billable metric; the split by reason or code stays in the logs.
        self.assertEqual(self.template.count("AWS::Logs::MetricFilter"), 2)
        self.assertEqual(set(re.findall(r"MetricName: (AgentChat\w+)", "\n".join(self.block(name) for name in FILTERS))), CUSTOM_METRICS)
        for resource in FILTERS:
            self.assertNotIn("Dimensions:", self.block(resource), resource)
        self.assertEqual(len(re.findall(r"(?m)^\s+MetricNamespace: ", self.template)), 2)

    def test_the_metrics_that_were_replaced_by_native_ones_are_gone(self) -> None:
        for name in REMOVED:
            self.assertNotIn(name, self.template, name)

    def test_every_alarm_metric_is_native_or_one_of_the_two_justified_custom_ones(self) -> None:
        for resource in ALARMS:
            block = self.block(resource)
            namespaces = re.findall(r"Namespace: ([\w/]+)", block)
            metrics = re.findall(r"MetricName: (\w+)", block)
            self.assertTrue(metrics, resource)
            for metric in metrics:
                custom = metric in CUSTOM_METRICS
                native = any(metric in names for names in NATIVE.values())
                self.assertTrue(custom or native, f"{resource}: {metric} is neither a known native metric nor an allowed custom one")
            for namespace in namespaces:
                self.assertIn(namespace, set(NATIVE) | {NAMESPACE}, resource)

    def test_the_native_alarms_watch_the_right_resource(self) -> None:
        for resource in ("AgentChatJobDurationAlarm", "AgentChatWorkerErrorsAlarm"):
            block = self.block(resource)
            self.assertIn("Namespace: AWS/Lambda", block)
            self.assertIn("Value: !Ref AgentChatWorkerFunction", block)
        self.assertIn("Value: !GetAtt AgentChatQueue.QueueName", self.block("AgentChatQueueAgeAlarm"))
        self.assertIn("Value: !GetAtt AgentChatDeadLetterQueue.QueueName", self.block("AgentChatDeadLetterQueueAlarm"))

    def test_alarms_notify_the_same_optional_topic_as_the_dlq_alarm(self) -> None:
        for resource in ALARMS:
            block = self.block(resource)
            self.assertIn("ActionsEnabled: !If [HasAgentChatAlarmTopic, true, false]", block, resource)
            self.assertIn("- - !Ref AgentChatAlarmTopicArn", block, resource)
            self.assertIn("TreatMissingData: notBreaching", block, resource)  # a quiet beta is not an incident

    def test_the_api_latency_alarm_is_not_applied_to_the_job(self) -> None:
        duration = self.block("AgentChatJobDurationAlarm")
        self.assertIn("MetricName: Duration", duration)
        self.assertIn("ExtendedStatistic: p95", duration)
        self.assertIn("Threshold: 90000", duration)  # the Harness loop limit, not 3 s
        self.assertNotRegex(duration, r"Threshold: 3000\b")
        self.assertIn("Timeout: 150", self.block("AgentChatWorkerFunction"))  # the threshold stays below the Lambda timeout

    def test_errors_alarm_counts_lambda_errors_over_fifteen_minutes(self) -> None:
        errors = self.block("AgentChatWorkerErrorsAlarm")
        for needle in ("MetricName: Errors", "Statistic: Sum", "Period: 900", "Threshold: 3"):
            self.assertIn(needle, errors)

    def test_queue_age_alarm_watches_the_work_queue_not_the_dlq(self) -> None:
        age = self.block("AgentChatQueueAgeAlarm")
        self.assertIn("MetricName: ApproximateAgeOfOldestMessage", age)
        self.assertIn("Value: !GetAtt AgentChatQueue.QueueName", age)
        self.assertIn("Threshold: 120", age)  # well inside the 5-minute job deadline

    def test_fallback_rate_uses_native_invocations_as_denominator_with_a_minimum_sample(self) -> None:
        alarm = self.block("AgentChatFallbackRateAlarm")
        expression = re.search(r"Expression: (.*)", alarm).group(1)
        self.assertIn("IF(invocations >= 5,", expression)
        self.assertIn("FILL(fallbacks, 0)", expression)  # the custom metric publishes nothing when there is no fallback
        ids = set(re.findall(r"- Id: (\w+)", alarm))
        self.assertEqual(ids, {"rate", "fallbacks", "invocations"})
        self.assertEqual(set(re.findall(r"MetricName: (\w+)", alarm)), {"AgentChatFallbacks", "Invocations"})
        self.assertIn("Namespace: AWS/Lambda", alarm)
        self.assertIn("Value: !Ref AgentChatWorkerFunction", alarm)
        self.assertEqual(len(re.findall(r"ReturnData: true", alarm)), 1)  # exactly one series is evaluated

    def test_six_alarms_seven_billed_alarm_metrics(self) -> None:
        # 1 each: DLQ, queue age, duration, errors, job failures; 2 for the fallback-rate math (fallbacks + invocations).
        self.assertEqual(self.template.count("AWS::CloudWatch::Alarm"), len(ALARMS))
        billed = {name: len(re.findall(r"MetricName: \w+", self.block(name))) for name in ALARMS}
        self.assertEqual(billed["AgentChatFallbackRateAlarm"], 2)
        self.assertTrue(all(count == 1 for name, count in billed.items() if name != "AgentChatFallbackRateAlarm"), billed)
        self.assertEqual(sum(billed.values()), 7)


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

    def test_a_clean_completed_job_publishes_no_custom_metric(self) -> None:
        # Duration, errors and the number of jobs are native Lambda metrics now.
        world = World(self)
        job = world.submit()
        (line,) = self.lines(world, job, script=[agent_result(TEXT_PLAN, call("tu-1", SEARCH_TOOL, search_payload()))])
        self.assertEqual(self.fired(line), set())

    def test_a_fallback_counts_once_as_a_fallback(self) -> None:
        world = World(self)
        job = world.submit()
        (line,) = self.lines(world, job, script=[agent_result("sin plan", outcome="no_tool_called")], rag=rag_ok())
        self.assertEqual(line["fallbackReason"], "no_tool_called")  # stays in the log line: split it in Logs Insights
        self.assertEqual(self.fired(line), {"AgentChatFallbackMetric"})

    def test_a_reused_tool_result_is_also_a_fallback(self) -> None:
        world = World(self)
        job = world.submit()
        script = [agent_result("no es un plan", call("tu-1", SEARCH_TOOL, search_payload()))]
        (line,) = self.lines(world, job, script=script)
        self.assertEqual((line["outputPath"], line["fallbackReason"]), ("tool_result", "plan_invalid"))
        self.assertEqual(self.fired(line), {"AgentChatFallbackMetric"})

    def test_a_job_marked_failed_counts_once_as_a_failure(self) -> None:
        world = World(self)
        job = world.submit()
        (line,) = self.lines(world, job, script=[ValueError("bug in the handler")])
        self.assertEqual((line["action"], line["failure"]), ("failed", "internal_error"))  # the code stays in the log line
        self.assertEqual(self.fired(line), {"AgentChatJobFailureMetric"})

    def test_a_closed_session_is_not_a_service_failure(self) -> None:
        world = World(self)
        job = world.submit()
        (line,) = self.lines(world, job, script=[], before=world.state.sessions.clear)  # the user logged out
        self.assertEqual((line["action"], line["failure"]), ("failed", "session_closed"))
        self.assertEqual(self.fired(line), set())

    def test_an_unclassified_error_is_left_to_the_dlq_and_lambda_alarms(self) -> None:
        world = World(self)
        printed: list[str] = []
        with patch("builtins.print", side_effect=lambda text, **_: printed.append(text)):
            lam.handle_sqs_event({"Records": [record(world.submit().key, body="{}")]}, world.worker, queue_arn=QUEUE_ARN,
                                 change_visibility=Recorder())
        line = json.loads(printed[0])
        self.assertEqual((line["action"], self.fired(line)), ("error", set()))  # redelivered once, then in the DLQ

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
        excluded = '{ $.failure = * && $.failure != "session_closed" }'
        self.assertTrue(matches(excluded, {"failure": "deadline_exceeded"}))
        self.assertFalse(matches(excluded, {"failure": "session_closed"}))
        self.assertFalse(matches(excluded, {}))  # a line without the field never matches


if __name__ == "__main__":
    unittest.main()
