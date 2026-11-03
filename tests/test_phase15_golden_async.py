"""Phase 15 golden set, asynchronous layer: the scenarios must pass, and must FAIL when the production rule they guard is broken."""

from __future__ import annotations

import contextlib
import io
import json
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend" / "src"))
sys.path.insert(0, str(ROOT / "agent" / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from evals import phase15_golden_async as asy
from legaldesk import agent_chat, agent_chat_lambda, agent_jobs
from legaldesk.agent_jobs import JobKey

SCENARIO_IDS = {s.id: s for s in asy.SCENARIOS}
_REAL_STDOUT = sys.stdout


def setUpModule() -> None:   # the SQS handler prints one JSON log line per delivery: keep CI output readable
    sys.stdout = io.StringIO()


def tearDownModule() -> None:
    sys.stdout = _REAL_STDOUT


def run_quietly(scenarios=asy.SCENARIOS):
    with contextlib.redirect_stdout(io.StringIO()):
        return asy.run_scenarios(scenarios)


def failed_ids(**patches) -> set[str]:
    """Scenario ids that fail while the given production attributes are patched (module path -> value)."""

    with contextlib.ExitStack() as stack:
        for target, value in patches.items():
            stack.enter_context(patch(target, value))
        rows = run_quietly()
    return {row["id"] for row in rows if not row["passed"]}


class ScenarioTableTests(unittest.TestCase):
    def test_every_scenario_passes_against_the_real_worker_handler_and_stores(self) -> None:
        rows = run_quietly()
        self.assertEqual([row["id"] for row in rows if not row["passed"]], [])
        summary = asy.summarize_scenarios(rows)
        self.assertEqual((summary["scenarios"], summary["passed"]), (len(asy.SCENARIOS), len(asy.SCENARIOS)))

    def test_the_table_covers_every_topic_the_owner_asked_for(self) -> None:
        topics = {s.topic for s in asy.SCENARIOS}
        self.assertEqual(topics, {"idempotency", "retry", "half_done", "dlq", "session", "quota", "isolation", "fallback"})
        required = {
            "duplicate_after_success", "duplicate_during_live_attempt", "concurrent_contenders_one_winner",
            "transient_twice_exhausts_and_releases_quota", "permanent_validation_error_not_retried",
            "crash_mid_attempt_ui_sees_running_then_failed", "crash_after_finalize_ui_sees_finalizing_then_failed",
            "poison_messages_go_to_dlq_without_reaching_the_worker", "session_closed_before_processing",
            "session_expired_while_queued", "logout_during_the_loop_does_not_discard_the_answer", "quota_exhausted_creates_no_job",
            "job_is_scoped_to_tenant_matter_and_hides_secrets", "error_after_finalize_never_retried",
        }
        self.assertLessEqual(required, set(SCENARIO_IDS))

    def test_scenario_ids_are_unique_and_every_expectation_names_a_known_observation(self) -> None:
        self.assertEqual(len(SCENARIO_IDS), len(asy.SCENARIOS))
        extra_keys = {"resultUnchanged", "concurrentDelivery", "actions", "quotaAtDeadline", "secondRejected", "jobsCreated", "submissionError",
                      "otherTenantVisible", "otherMatterVisible", "publicViewKeys", "questionKept", "jobStillStored"}
        for scenario in asy.SCENARIOS:
            for key in scenario.expect:
                self.assertTrue(key in asy.OBSERVATION_FIELDS or key in extra_keys, f"{scenario.id}: {key}")

    def test_an_unmet_expectation_is_reported_by_name(self) -> None:
        obs = asy.AsyncObservation(status="failed", attempts=1)
        self.assertEqual(asy.check_expectation({"status": "succeeded", "attempts": 1}, obs), ["status"])
        self.assertEqual(asy.check_expectation({"nope": 1}, obs), ["nope"])   # an absent extra never matches by accident

    def test_a_scenario_that_cannot_run_is_a_failed_scenario_with_a_closed_code(self) -> None:
        def broken() -> asy.AsyncObservation:
            raise RuntimeError("secret detail")

        row = run_quietly((asy.Scenario("broken", "retry", broken, {}),))[0]
        self.assertFalse(row["passed"])
        self.assertEqual(row["error"], "runtimeerror")
        self.assertNotIn("secret", json.dumps(row))


class WhatTheScenariosPinDown(unittest.TestCase):
    """Behaviour the owner asked about, asserted directly (not only through the table)."""

    def test_a_transient_failure_retries_once_with_a_30_second_visibility_and_one_reservation(self) -> None:
        obs = SCENARIO_IDS["transient_throttling_retries_once"].run()
        self.assertEqual((obs.status, obs.attempts, obs.agent_calls, obs.quota), ("succeeded", 2, 2, (1, 1)))
        self.assertEqual(obs.visibility_changes, (30,))
        self.assertGreaterEqual(obs.elapsed_s, 30)       # the retry waited for the shortened visibility, not 900 s
        self.assertEqual(obs.memory_events, 2)           # the conversation memory was written once, after the successful attempt

    def test_a_permanent_failure_is_never_retried_and_gives_the_quota_back(self) -> None:
        obs = SCENARIO_IDS["permanent_bug_not_retried"].run()
        self.assertEqual((obs.status, obs.attempts, obs.agent_calls, obs.quota, obs.visibility_changes), ("failed", 1, 1, (0, 0), ()))

    def test_the_second_transient_failure_ends_the_job_without_the_dlq(self) -> None:
        obs = SCENARIO_IDS["transient_twice_exhausts_and_releases_quota"].run()
        self.assertEqual((obs.failure, obs.dlq, obs.deleted), ("transient_exhausted", 0, 1))   # a finished failure is acknowledged, not dead-lettered

    def test_a_crashed_job_is_visible_as_running_until_the_deadline_and_then_failed_with_the_quota_released(self) -> None:
        obs = SCENARIO_IDS["crash_mid_attempt_ui_sees_running_then_failed"].run()
        states = dict(obs.timeline)
        self.assertEqual((states[0], states[299], states[301]), ("running", "running", "failed"))
        self.assertEqual(obs.extra["quotaAtDeadline"], (0, 0))   # released by the poll, not only by the late redelivery
        self.assertEqual(obs.active_jobs, 0)                     # nothing is left active

    def test_the_message_of_a_job_the_poll_already_failed_is_ignored_when_it_returns(self) -> None:
        obs = SCENARIO_IDS["crash_mid_attempt_ui_sees_running_then_failed"].run()
        self.assertEqual((obs.agent_calls, obs.queue_left, obs.dlq), (1, 0, 0))   # no second run, nothing in the DLQ

    def test_a_job_that_died_after_finalize_is_never_run_again(self) -> None:
        obs = SCENARIO_IDS["crash_after_finalize_ui_sees_finalizing_then_failed"].run()
        self.assertEqual((obs.failure, obs.agent_calls, obs.memory_events), ("internal_error", 1, 0))

    def test_poison_messages_reach_the_dlq_after_two_receives_and_never_the_worker(self) -> None:
        obs = SCENARIO_IDS["poison_messages_go_to_dlq_without_reaching_the_worker"].run()
        self.assertEqual((obs.dlq, obs.deliveries, obs.agent_calls, obs.queue_left), (2, 4, 0, 0))

    def test_the_dlq_job_is_still_failed_by_the_poll_and_its_quota_returns(self) -> None:
        obs = SCENARIO_IDS["unclassified_error_twice_goes_to_dlq_and_the_poll_still_fails_the_job"].run()
        self.assertEqual((obs.dlq, obs.status, obs.failure, obs.quota, obs.active_jobs), (1, "failed", "deadline_exceeded", (0, 0), 0))

    def test_logout_while_the_job_runs_does_not_discard_a_finished_answer(self) -> None:
        obs = SCENARIO_IDS["logout_during_the_loop_does_not_discard_the_answer"].run()
        self.assertEqual((obs.status, obs.quota), ("succeeded", (1, 1)))

    def test_closing_the_session_before_the_worker_claims_fails_closed_without_calling_the_agent(self) -> None:
        obs = SCENARIO_IDS["session_closed_before_processing"].run()
        self.assertEqual((obs.failure, obs.agent_calls, obs.quota), ("session_closed", 0, (0, 0)))

    def test_the_job_is_scoped_to_its_tenant_and_matter_and_the_browser_view_hides_the_question(self) -> None:
        obs = SCENARIO_IDS["job_is_scoped_to_tenant_matter_and_hides_secrets"].run()
        self.assertFalse(obs.extra["otherTenantVisible"] or obs.extra["otherMatterVisible"] or obs.extra["questionKept"])
        self.assertNotIn("subject", obs.extra["publicViewKeys"])

    def test_two_real_threads_claiming_at_once_produce_one_winner(self) -> None:
        for _ in range(3):   # a race: repeat it
            obs = SCENARIO_IDS["concurrent_contenders_one_winner"].run()
            self.assertEqual((obs.extra["actions"], obs.agent_calls), (("completed", "ignored"), 1))


class MutationsOfProductionRules(unittest.TestCase):
    """The scenarios are only worth something if breaking a rule turns the right ones red."""

    def test_a_longer_retry_visibility_is_caught(self) -> None:
        failed = failed_ids(**{"evals.phase15_golden_async.RETRY_VISIBILITY_SECONDS": 60, "legaldesk.agent_chat_lambda.RETRY_VISIBILITY_SECONDS": 60})
        self.assertIn("transient_throttling_retries_once", failed)
        self.assertIn("poison_messages_go_to_dlq_without_reaching_the_worker", failed)

    def test_no_retry_budget_is_caught(self) -> None:
        self.assertIn("transient_throttling_retries_once", failed_ids(**{"legaldesk.agent_jobs.MAX_ATTEMPTS": 1}))

    def test_retrying_everything_is_caught(self) -> None:
        failed = failed_ids(**{"legaldesk.agent_chat.is_transient_error": lambda exc: True})
        self.assertTrue({"permanent_validation_error_not_retried", "permanent_bug_not_retried"} <= failed)

    def test_retrying_nothing_is_caught(self) -> None:
        failed = failed_ids(**{"legaldesk.agent_chat.is_transient_error": lambda exc: False})
        self.assertTrue({"transient_throttling_retries_once", "transient_5xx_retries_once", "transient_read_timeout_retries_once"} <= failed)

    def test_a_quota_that_is_not_given_back_is_caught(self) -> None:
        failed = failed_ids(**{"legaldesk.agent_chat.release_job_quota": lambda quota, job: True, "evals.phase15_golden_async.release_job_quota": lambda quota, job: True})
        self.assertTrue({"session_closed_before_processing", "transient_twice_exhausts_and_releases_quota", "crash_mid_attempt_ui_sees_running_then_failed",
                         "queue_publish_failure_releases_quota"} <= failed)

    def test_a_job_deadline_that_never_expires_is_caught(self) -> None:
        failed = failed_ids(**{"legaldesk.agent_jobs.JOB_TOTAL_DEADLINE_SECONDS": 10**9})
        self.assertTrue({"crash_redelivered_after_deadline_fails_cleanly", "deadline_exceeded_at_claim"} <= failed)

    def test_losing_the_idempotency_gate_is_caught(self) -> None:
        real_claim = agent_jobs.InMemoryChatJobStore.claim

        def always_started(self, key, *, now):
            outcome, job = real_claim(self, key, now=now)
            return (agent_jobs.ClaimOutcome.STARTED, job) if outcome in (agent_jobs.ClaimOutcome.DUPLICATE, agent_jobs.ClaimOutcome.TERMINAL) else (outcome, job)

        failed = failed_ids(**{"legaldesk.agent_jobs.InMemoryChatJobStore.claim": always_started})
        self.assertTrue({"duplicate_after_success", "duplicate_during_live_attempt"} & failed)

    def test_retrying_after_finalize_is_caught(self) -> None:
        real_handle = agent_chat.AgentChatWorker._handle_error

        def retry_even_if_finalized(self, job, attempt, exc, *, finalized):
            return real_handle(self, job, attempt, exc, finalized=False)

        failed = failed_ids(**{"legaldesk.agent_chat.AgentChatWorker._handle_error": retry_even_if_finalized})
        self.assertIn("error_after_finalize_never_retried", failed)

    def test_a_handler_that_acknowledges_poison_messages_is_caught(self) -> None:
        def swallow(event, worker, *, queue_arn, change_visibility):
            return {"batchItemFailures": []}

        self.assertIn("poison_messages_go_to_dlq_without_reaching_the_worker", failed_ids(**{"evals.phase15_golden_async.handle_sqs_event": swallow}))

    def test_a_poll_that_never_expires_jobs_is_caught(self) -> None:
        def never(self, key, *, now):
            return self._jobs.get(key), False

        failed = failed_ids(**{"legaldesk.agent_jobs.InMemoryChatJobStore.expire_if_due": never})
        self.assertTrue({"crash_mid_attempt_ui_sees_running_then_failed", "crash_after_finalize_ui_sees_finalizing_then_failed",
                         "unclassified_error_twice_goes_to_dlq_and_the_poll_still_fails_the_job"} <= failed)

    def test_a_queue_with_no_redrive_policy_is_caught(self) -> None:
        class NoRedrive(asy.SimQueue):
            def __init__(self, clock, **kwargs):
                super().__init__(clock, max_receive=10**6, **kwargs)

        failed = failed_ids(**{"evals.phase15_golden_async.SimQueue": NoRedrive})
        self.assertIn("poison_messages_go_to_dlq_without_reaching_the_worker", failed)

    def test_a_job_key_without_scope_is_caught(self) -> None:
        real_get = agent_jobs.InMemoryChatJobStore.get

        def ignore_scope(self, key):
            return real_get(self, JobKey("tnt_aurora", "mat_sundial", key.job_id))

        self.assertIn("job_is_scoped_to_tenant_matter_and_hides_secrets", failed_ids(**{"legaldesk.agent_jobs.InMemoryChatJobStore.get": ignore_scope}))


class SimulatedQueueTests(unittest.TestCase):
    def harness(self, *behaviours):
        agent = asy.ScriptedAgent(list(behaviours) or ["ok"])
        return asy.AsyncHarness(agent), agent

    def test_a_message_is_hidden_while_in_flight_and_deleted_after_success(self) -> None:
        h, _ = self.harness()
        job = h.submit()
        with contextlib.redirect_stdout(io.StringIO()):
            h.pump()
        self.assertEqual((h.queue.deleted, h.queue.depth(), h.queue.deliveries), (1, 0, 1))
        self.assertEqual(h.jobs.get(job.key).status.value, "succeeded")

    def test_a_crash_leaves_the_message_hidden_for_the_full_visibility_timeout(self) -> None:
        h, _ = self.harness(asy.SimulatedCrash())
        h.submit()
        h.queue.pump(h.worker, stop_at=h.clock() + 5)
        self.assertEqual((h.queue.crashes, h.queue.depth()), (1, 1))
        self.assertGreaterEqual(h.queue.messages[0].visible_at - h.clock(), asy.DEFAULT_VISIBILITY_SECONDS - 5)

    def test_the_third_receive_moves_the_message_to_the_dlq(self) -> None:
        h, _ = self.harness()
        h.queue.send_raw("garbage")
        with contextlib.redirect_stdout(io.StringIO()):
            h.pump()
        self.assertEqual((len(h.queue.dlq), h.queue.deliveries, h.queue.depth()), (1, 2, 0))
        self.assertEqual(h.queue.dlq[0].receive_count, 3)

    def test_the_clock_never_goes_backwards_and_nothing_sleeps(self) -> None:
        clock = asy.SimClock()
        clock.advance(10)
        clock.set(5)
        self.assertEqual(clock(), asy.T0 + 10)

    def test_the_poll_rule_matches_the_api(self) -> None:
        """Drift guard: the API's job-status handler must still expire a due job and release its quota."""

        source = (ROOT / "backend" / "src" / "legaldesk" / "http_app.py").read_text(encoding="utf-8")
        region = source[source.index("expire_if_due") - 1200: source.index("expire_if_due") + 400]
        self.assertIn("release_job_quota", region)
        self.assertIn("job.subject != identity.subject", region)

    def test_the_handler_constants_the_simulation_assumes_are_the_deployed_ones(self) -> None:
        template = (ROOT / "infra").rglob("*.yaml")
        text = "\n".join(p.read_text(encoding="utf-8", errors="ignore") for p in template)
        self.assertRegex(text, r"maxReceiveCount[\"']?\s*:\s*2")
        self.assertRegex(text, r"VisibilityTimeout:\s*900")
        self.assertEqual((asy.MAX_RECEIVE_COUNT, asy.DEFAULT_VISIBILITY_SECONDS, asy.RETRY_VISIBILITY_SECONDS), (2, 900, agent_chat_lambda.RETRY_VISIBILITY_SECONDS))


class PollUntilTerminalTests(unittest.TestCase):
    def test_a_job_that_never_finishes_is_reported_stuck_not_forever_running(self) -> None:
        h = asy.AsyncHarness(asy.ScriptedAgent([asy.SimulatedCrash()]))
        job = h.submit()
        h.queue.pump(h.worker, stop_at=h.clock() + 1)

        def sleep(seconds: float) -> None:
            h.clock.advance(seconds)

        seen = asy.poll_job_until_terminal(h.jobs, h.quota, job.key, clock=h.clock, sleep=sleep, timeout_s=100, interval_s=10)
        self.assertEqual(seen["status"], "stuck")           # still active after 100 s, and honest about it
        self.assertEqual(seen["timeline"][0][1], "running")

    def test_the_deadline_turns_a_crashed_job_into_a_failed_one_and_releases_the_quota(self) -> None:
        h = asy.AsyncHarness(asy.ScriptedAgent([asy.SimulatedCrash()]))
        job = h.submit()
        h.queue.pump(h.worker, stop_at=h.clock() + 1)
        seen = asy.poll_job_until_terminal(h.jobs, h.quota, job.key, clock=h.clock, sleep=lambda s: h.clock.advance(s), timeout_s=400, interval_s=20)
        self.assertEqual((seen["status"], seen["failure"], h.counters()), ("failed", "deadline_exceeded", (0, 0)))
        self.assertEqual([state for _, state in seen["timeline"]], ["running", "failed"])

    def test_a_missing_job_is_reported_missing(self) -> None:
        h = asy.AsyncHarness(asy.ScriptedAgent(["ok"]))
        seen = asy.poll_job_until_terminal(h.jobs, h.quota, JobKey("t", "m", "j"), clock=h.clock, sleep=lambda s: None)
        self.assertEqual(seen["status"], "missing")

    def test_the_observation_never_holds_content(self) -> None:
        h = asy.AsyncHarness(asy.ScriptedAgent(["ok"]))
        job = h.submit("PREGUNTA-CONFIDENCIAL")
        with contextlib.redirect_stdout(io.StringIO()):
            h.pump()
        seen = asy.poll_job_until_terminal(h.jobs, h.quota, job.key, clock=h.clock, sleep=lambda s: None)
        text = json.dumps(seen)
        self.assertNotIn("CONFIDENCIAL", text)
        self.assertNotIn("Respuesta sintética", text)


if __name__ == "__main__":
    unittest.main()
