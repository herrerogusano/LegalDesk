"""Phase 15 (T4.3, T4.6): SQS handler, queue publisher and composition wiring."""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))
sys.path.insert(0, str(ROOT / "agent" / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from legaldesk import agent_chat_lambda as lam
from legaldesk.agent_chat import AgentChatWorker, WorkerResult
from legaldesk.agent_jobs import JobKey, JobStatus
from legaldesk.guardrails import GuardrailOutcome, GuardrailResult
from test_phase15_agent_chat_worker import (  # noqa: E402 - shared fixtures
    SEARCH_TOOL, TEXT_PLAN, World, agent_result, call, search_payload,
)

QUEUE_ARN = "arn:aws:sqs:eu-west-1:123456789012:legaldesk-agent-chat"


def record(key: JobKey, *, message_id="m-1", receipt="rh-1", source=QUEUE_ARN, body=None):
    body = body if body is not None else json.dumps({"jobId": key.job_id, "tenantId": key.tenant_id, "matterId": key.matter_id})
    return {"messageId": message_id, "receiptHandle": receipt, "body": body, "eventSourceARN": source}


class Recorder:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def __call__(self, receipt, seconds):
        self.calls.append((receipt, seconds))
        if self.fail:
            raise RuntimeError("sqs down")


class MessageTests(unittest.TestCase):
    def test_queue_message_carries_only_ids(self) -> None:
        sent = []
        client = SimpleNamespace(send_message=lambda **kw: sent.append(kw) or {"MessageId": "m"})
        lam.Boto3AgentChatQueue(client, queue_url="https://sqs/q").publish(JobKey("t", "m", "j"))
        self.assertEqual(json.loads(sent[0]["MessageBody"]), {"jobId": "j", "tenantId": "t", "matterId": "m"})
        with self.assertRaises(lam.AgentChatLambdaConfigurationError):
            lam.Boto3AgentChatQueue(SimpleNamespace(send_message=lambda **kw: {}), queue_url="u").publish(JobKey("t", "m", "j"))

    def test_parse_message_is_strict(self) -> None:
        self.assertEqual(lam.parse_message('{"jobId":"j","tenantId":"t","matterId":"m"}'), JobKey("t", "m", "j"))
        for bad in (None, "[]", "{}", '{"jobId":"j","tenantId":"t"}', '{"jobId":"j","tenantId":"t","matterId":"m","question":"x"}',
                    '{"jobId":1,"tenantId":"t","matterId":"m"}', '{"jobId":"j","tenantId":"t","matterId":"' + "m" * 200 + '"}',
                    "x" * 2000, "not json"):
            with self.assertRaises((ValueError, TypeError), msg=str(bad)[:40]):
                lam.parse_message(bad)


class HandlerTests(unittest.TestCase):
    def worker(self, result=None, *, raises=None):
        calls = []

        def process(key):
            calls.append(key)
            if raises:
                raise raises
            return result

        return SimpleNamespace(process=process, calls=calls)

    def run_event(self, worker, *records, visibility=None):
        visibility = visibility or Recorder()
        out = lam.handle_sqs_event({"Records": list(records)}, worker, queue_arn=QUEUE_ARN, change_visibility=visibility)
        return out, visibility

    def test_a_finished_job_acknowledges_the_message(self) -> None:
        key = JobKey("t", "m", "j")
        out, visibility = self.run_event(self.worker(WorkerResult("completed", "j")), record(key))
        self.assertEqual((out, visibility.calls), ({"batchItemFailures": []}, []))

    def test_ignored_duplicates_acknowledge_without_work(self) -> None:
        out, visibility = self.run_event(self.worker(WorkerResult("ignored", "j")), record(JobKey("t", "m", "j")))
        self.assertEqual((out["batchItemFailures"], visibility.calls), ([], []))

    def test_transient_error_shortens_visibility_to_30_seconds_before_returning_the_message(self) -> None:
        out, visibility = self.run_event(self.worker(WorkerResult("retry", "j", 30)), record(JobKey("t", "m", "j")))
        self.assertEqual(out["batchItemFailures"], [{"itemIdentifier": "m-1"}])
        self.assertEqual(visibility.calls, [("rh-1", 30)])

    def test_unexpected_error_gets_one_quick_retry_then_the_dlq(self) -> None:
        out, visibility = self.run_event(self.worker(raises=RuntimeError("ddb down")), record(JobKey("t", "m", "j")))
        self.assertEqual((out["batchItemFailures"], visibility.calls), ([{"itemIdentifier": "m-1"}], [("rh-1", 30)]))

    def test_poison_and_foreign_messages_never_reach_the_worker(self) -> None:
        worker = self.worker(WorkerResult("completed", "j"))
        key = JobKey("t", "m", "j")
        out, _ = self.run_event(worker, record(key, body="{}"), record(key, message_id="m-2", source="arn:aws:sqs:eu-west-1:123456789012:other"))
        self.assertEqual([item["itemIdentifier"] for item in out["batchItemFailures"]], ["m-1", "m-2"])
        self.assertEqual(worker.calls, [])

    def test_a_failing_visibility_call_does_not_break_the_batch_response(self) -> None:
        out, visibility = self.run_event(self.worker(WorkerResult("retry", "j", 30)), record(JobKey("t", "m", "j")), visibility=Recorder(fail=True))
        self.assertEqual(out["batchItemFailures"], [{"itemIdentifier": "m-1"}])

    def test_malformed_event_is_an_error(self) -> None:
        with self.assertRaises(ValueError):
            lam.handle_sqs_event({"Records": []}, self.worker(), queue_arn=QUEUE_ARN, change_visibility=Recorder())

    def test_disabled_by_default_builds_nothing(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LEGALDESK_AGENT_CHAT_ENABLED", None)
            with patch.object(lam, "build_worker_from_environment", side_effect=AssertionError("must not compose")):
                self.assertEqual(lam.lambda_handler({"Records": []}, None), {"status": "disabled", "batchItemFailures": []})

    def test_log_line_has_ids_and_codes_but_no_content(self) -> None:
        world = World(self)
        job = world.submit("pregunta confidencial")
        world.agent_script = [agent_result(TEXT_PLAN, call("tu-1", SEARCH_TOOL, search_payload("RESPUESTA CONFIDENCIAL")))]
        printed = []
        with patch("builtins.print", side_effect=lambda text, **_: printed.append(text)):
            lam.handle_sqs_event({"Records": [record(job.key)]}, world.worker, queue_arn=QUEUE_ARN, change_visibility=Recorder())
        line = json.loads(printed[0])
        self.assertEqual((line["event"], line["action"], line["outputPath"], line["tools"]), ("agent_chat_job", "completed", "grounded", ["search_documents"]))
        self.assertNotIn("CONFIDENCIAL", printed[0])
        self.assertNotIn("confidencial", printed[0])


class EndToEndTests(unittest.TestCase):
    def test_redelivering_the_same_message_does_not_duplicate_answer_memory_or_quota(self) -> None:
        world = World(self)
        job = world.submit()
        world.agent_script = [agent_result(TEXT_PLAN, call("tu-1", SEARCH_TOOL, search_payload()))]
        visibility = Recorder()
        for _ in range(3):  # at-least-once delivery
            lam.handle_sqs_event({"Records": [record(job.key)]}, world.worker, queue_arn=QUEUE_ARN, change_visibility=visibility)
        self.assertEqual(len(world.agent_calls), 1)
        self.assertEqual(world.jobs.get(job.key).status, JobStatus.SUCCEEDED)
        (events,) = world.memory._events.values()
        self.assertEqual(len(events), 2)
        self.assertEqual((world.counters()["chatCount"], world.counters()["harnessCount"]), (1, 1))
        self.assertEqual(len(world.state.citation_handles), 1)
        self.assertEqual(visibility.calls, [])

    def test_transient_failure_then_redelivery_after_30_seconds_succeeds(self) -> None:
        class Throttled(Exception):
            response = {"Error": {"Code": "ThrottlingException"}}

        world = World(self)
        job = world.submit()
        world.agent_script = [Throttled(), agent_result(TEXT_PLAN, call("tu-1", SEARCH_TOOL, search_payload()))]
        visibility = Recorder()
        out = lam.handle_sqs_event({"Records": [record(job.key)]}, world.worker, queue_arn=QUEUE_ARN, change_visibility=visibility)
        self.assertEqual((len(out["batchItemFailures"]), visibility.calls), (1, [("rh-1", 30)]))
        world.clock.now += 31
        out = lam.handle_sqs_event({"Records": [record(job.key)]}, world.worker, queue_arn=QUEUE_ARN, change_visibility=visibility)
        self.assertEqual(out["batchItemFailures"], [])
        self.assertEqual(world.jobs.get(job.key).status, JobStatus.SUCCEEDED)


class CompositionWiringTests(unittest.TestCase):
    def runtime(self, outcome, content=("hola",), env=None):
        from legaldesk import application

        class FakeProcessor:
            def __init__(self, *_a, **_k):
                pass

            def check_input(self, text, *, correlation_id):
                return GuardrailResult(outcome, content)

        created = []
        factory = lambda service, **kw: created.append(service) or SimpleNamespace(service=service)  # noqa: E731
        environment = {"LEGALDESK_AGENT_CHAT_QUEUE_URL": "https://sqs.eu-west-1.amazonaws.com/1/q", **(env or {})}
        with patch.object(application, "GuardrailProcessor", FakeProcessor), patch.dict(os.environ, environment):
            config = SimpleNamespace(metadata_table_name="metadata", region="eu-west-1")
            runtime = application._agent_chat_runtime(factory, None, config, object(), object(), None, SimpleNamespace())
        return runtime, created

    def test_guardrail_outcomes_map_to_submission_decisions(self) -> None:
        for outcome, expected in ((GuardrailOutcome.ALLOWED, "allowed"), (GuardrailOutcome.ANONYMIZED, "allowed"),
                                  (GuardrailOutcome.BLOCKED, "blocked"), (GuardrailOutcome.ERROR, "error")):
            runtime, created = self.runtime(outcome)
            self.assertEqual(runtime.input_check("hola", "corr").kind, expected)
            self.assertEqual(created, ["sqs"])
        runtime, _ = self.runtime(GuardrailOutcome.ALLOWED, content=("x" * 5000,))
        self.assertEqual(runtime.input_check("hola", "corr").kind, "blocked")

    def test_the_sanitized_question_is_what_gets_queued(self) -> None:
        runtime, _ = self.runtime(GuardrailOutcome.ANONYMIZED, content=("pregunta {NAME}",))
        self.assertEqual(runtime.input_check("pregunta Ana", "corr").question, "pregunta {NAME}")

    def test_queue_url_is_required_when_enabled(self) -> None:
        with self.assertRaises(ValueError):
            self.runtime(GuardrailOutcome.ALLOWED, env={"LEGALDESK_AGENT_CHAT_QUEUE_URL": " "})


if __name__ == "__main__":
    unittest.main()
