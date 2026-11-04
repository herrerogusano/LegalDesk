"""The real eval lists the conversation bindings it creates (same ``residualState`` as the harness checks)."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parents[1]
for _path in (ROOT, ROOT / "backend" / "src", ROOT / "agent" / "src", ROOT / "tests"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from evals import phase15_routing_eval as ev
from evals import residual_state
from legaldesk.authorization import build_request_context
from legaldesk.memory import conversation_binding_partition_key
from legaldesk_agent import AgentChatResult, HarnessInvocationError

UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dataset, cls.digest = ev.load_dataset()

    def client(self, behaviour=None):
        """A WorkerLoopClient over the in-memory backend; ``behaviour(n)`` returns a loop or raises."""

        backend = ev.OfflineBackend(self.dataset, self.dataset["cases"][0])
        calls = []

        def invoke_agent(binding, question, history):
            calls.append(binding)
            if behaviour is None:
                return AgentChatResult(session_id="s", correlation_id="c", text="", loop_outcome="no_tool_called", iterations=1, stop_reason="end_turn")
            return behaviour(len(calls))

        backend.worker.invoke_agent = invoke_agent
        backend.worker.rag_chat = lambda *a, **k: self.fail("the real eval must not run the direct RAG path")
        client = ev.WorkerLoopClient(backend.worker, access_token=ev._unsigned_token(ev.SUBJECT), matter_id=ev.MATTER)
        return client, backend, calls

    def run_real(self, client, sink=None):
        return ev.run_real(self.dataset, self.digest, client, approval_ref="owner-approval:test", release_commit="a" * 40,
                           artifact_sha256="b" * 64, prompt_identity=("1.2.0", "c" * 64), sink=sink)


class ResidualRecordingTests(Base):
    def test_a_case_records_its_binding_with_the_same_format_as_the_harness_checks(self) -> None:
        client, backend, calls = self.client()
        client.run(self.dataset["cases"][0])
        (entry,) = client.residual.bindings
        self.assertEqual(set(entry), {"conversationId", "sessionSelector", "bindingPkSha256", "invocationId"})
        self.assertTrue(entry["conversationId"].startswith("eval-") and entry["sessionSelector"].startswith("eval-"))
        self.assertRegex(entry["invocationId"], UUID)
        self.assertEqual(entry["invocationId"], calls[0].invocation_id)
        # the hash is of the real partition key, built the way the runbook cleanup rebuilds it
        identity = backend.worker.identity_verifier.verify_authorization_header(f"Bearer {client.access_token}")
        context = build_request_context(identity, ev.MATTER, backend.worker.authorization_store)
        key = conversation_binding_partition_key(context, entry["conversationId"])
        self.assertEqual(key, f"CONVERSATION#{context.tenant_id}#{context.matter_id}#{context.user_id}#{entry['conversationId']}")
        self.assertEqual(entry["bindingPkSha256"], hashlib.sha256(key.encode("utf-8")).hexdigest())
        self.assertTrue(backend.worker.conversation_store.is_bound(context=context, conversation_id=entry["conversationId"], session_selector=entry["sessionSelector"]))

    def test_the_two_reports_share_one_implementation(self) -> None:
        from scripts import phase15_harness_checks as checks

        self.assertIs(checks.ResidualState, residual_state.ResidualState)
        self.assertIs(ev.ResidualState, residual_state.ResidualState)

    def test_every_case_has_its_own_random_ids(self) -> None:
        client, _, _ = self.client()
        for case in self.dataset["cases"][:3]:
            client.run(case)
        ids = [(item["conversationId"], item["sessionSelector"], item["invocationId"]) for item in client.residual.bindings]
        self.assertEqual(len(ids), 3)
        self.assertEqual(len({value for row in ids for value in row}), 9)

    def test_the_binding_is_listed_when_the_call_fails_after_it(self) -> None:
        client, _, _ = self.client(lambda n: (_ for _ in ()).throw(HarnessInvocationError("AgentCore returned an invalid event stream")))
        obs = client.run(self.dataset["cases"][0])
        self.assertEqual(obs.error, "harnessinvocationerror")
        (entry,) = client.residual.bindings
        self.assertRegex(entry["invocationId"], UUID)

    def test_the_binding_is_listed_when_the_grant_could_not_be_written(self) -> None:
        client, backend, _ = self.client()
        backend.worker._bind = mock.Mock(side_effect=RuntimeError("table unavailable"))
        client.run(self.dataset["cases"][0])
        (entry,) = client.residual.bindings
        self.assertIsNone(entry["invocationId"])  # the binding exists, no grant was written
        self.assertEqual(client.residual.as_report()["invocationGrantsWritten"], 0)

    def test_the_binding_is_listed_when_the_job_cannot_even_be_built(self) -> None:
        client, _, _ = self.client()
        with mock.patch.object(ev, "new_job", side_effect=ValueError("bad job")):
            with self.assertRaises(ValueError):
                client.run(self.dataset["cases"][0])
        self.assertEqual(len(client.residual.bindings), 1)

    def test_the_binding_is_listed_when_the_call_is_interrupted(self) -> None:
        client, _, _ = self.client(lambda n: (_ for _ in ()).throw(KeyboardInterrupt()))
        with self.assertRaises(KeyboardInterrupt):
            client.run(self.dataset["cases"][0])
        self.assertEqual(len(client.residual.bindings), 1)


class InterruptedRunTests(Base):
    def test_an_interrupted_run_still_writes_a_report_with_what_was_spent_and_created(self) -> None:
        def behaviour(n):
            if n == 3:
                raise KeyboardInterrupt()
            return AgentChatResult(session_id="s", correlation_id="c", text="", loop_outcome="no_tool_called", iterations=1, stop_reason="end_turn")

        client, _, _ = self.client(behaviour)
        written: list[dict] = []
        with self.assertRaises(KeyboardInterrupt):
            self.run_real(client, sink=written.append)
        (report,) = written
        self.assertTrue(report["aborted"])
        self.assertEqual(report["interruptedBy"], "KeyboardInterrupt")
        self.assertEqual(report["budget"]["harnessInvocationsUsed"], 3)  # the interrupted call was paid for
        self.assertEqual(len(report["residualState"]["conversationBindings"]), 3)
        self.assertEqual(report["residualState"]["invocationGrantsWritten"], 3)
        self.assertFalse(report["verdict"]["passed"], "an interrupted run can never pass")
        self.assertEqual(len(report["cases"]), len(self.dataset["cases"]))
        self.assertEqual(sum(1 for row in report["cases"] if row.get("error") == "not_run"), len(self.dataset["cases"]) - 2)

    def test_an_exception_from_the_client_also_ends_in_a_report_and_is_raised_again(self) -> None:
        client, backend, _ = self.client()
        original = backend.worker.conversation_store.bind
        count = {"n": 0}

        def flaky(*args, **kwargs):
            count["n"] += 1
            if count["n"] == 2:
                raise RuntimeError("table unavailable")
            return original(*args, **kwargs)

        backend.worker.conversation_store.bind = flaky
        written: list[dict] = []
        with self.assertRaises(RuntimeError):
            self.run_real(client, sink=written.append)
        self.assertEqual((written[0]["interruptedBy"], len(written[0]["residualState"]["conversationBindings"])), ("RuntimeError", 1))

    def test_a_complete_run_lists_one_binding_per_case(self) -> None:
        client, _, _ = self.client()
        written: list[dict] = []
        report = self.run_real(client, sink=written.append)
        self.assertIs(written[0], report)
        self.assertEqual(len(report["residualState"]["conversationBindings"]), 30)
        self.assertEqual(report["residualState"]["grantTtlSeconds"], 300)
        self.assertIsNone(report["interruptedBy"])
        self.assertIn("step 14", report["residualState"]["cleanup"])

    def test_a_report_that_cannot_be_written_is_printed_and_the_failure_raised(self) -> None:
        client, _, _ = self.client()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(OSError):
            self.run_real(client, sink=mock.Mock(side_effect=OSError("disk full")))
        printed = json.loads(stderr.getvalue())
        self.assertEqual(len(printed["residualState"]["conversationBindings"]), 30)

    def test_a_client_without_residual_state_still_produces_the_block(self) -> None:
        class Plain:
            def run(self, case):
                return ev.Observation()

        report = self.run_real(Plain())
        self.assertEqual(report["residualState"]["conversationBindings"], [])

    def test_the_report_never_holds_the_key_or_who_it_belongs_to(self) -> None:
        client, backend, _ = self.client()
        written: list[dict] = []
        self.run_real(client, sink=written.append)
        text = json.dumps(written[0])
        identity = backend.worker.identity_verifier.verify_authorization_header(f"Bearer {client.access_token}")
        context = build_request_context(identity, ev.MATTER, backend.worker.authorization_store)
        for secret in ("CONVERSATION#", context.tenant_id, context.user_id, identity.subject):
            self.assertNotIn(secret, text)
        self.assertNotIn(self.dataset["cases"][0]["question"], text)


class CommandLineTests(Base):
    def test_execute_writes_the_report_even_when_the_run_is_interrupted(self) -> None:
        client, backend, _ = self.client(lambda n: (_ for _ in ()).throw(KeyboardInterrupt()))
        fake_module = mock.Mock(build_worker_from_environment=lambda: (backend.worker, "arn:queue", 30))
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(ev, "RESULTS_ROOT", Path(temp)), \
                mock.patch.dict(sys.modules, {"legaldesk.agent_chat_lambda": fake_module}), \
                mock.patch.dict("os.environ", {"LEGALDESK_EVAL_ACCESS_TOKEN": ev._unsigned_token(ev.SUBJECT)}):
            output = Path(temp) / "phase15-routing-test-a.json"
            argv = ["real", "--execute", "--approval-ref", "ticket-1", "--question-ref", "F-01-written-question", "--release-commit", "a" * 40, "--artifact-sha256", "b" * 64,
                    "--matter-id", ev.MATTER, "--output", str(output)]
            with self.assertRaises(KeyboardInterrupt):
                ev.main(argv)
            report = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(report["interruptedBy"], "KeyboardInterrupt")
        self.assertEqual(len(report["residualState"]["conversationBindings"]), 1)
        self.assertFalse(report["verdict"]["passed"])


class RunbookAndCleanupContractTests(unittest.TestCase):
    """The runbook cleanup reads either report; this pins the contract it depends on."""

    def test_both_reports_expose_the_same_block_the_cleanup_reads(self) -> None:
        state = residual_state.ResidualState()
        state.conversation_bound("c", "s", "CONVERSATION#t#m#u#c")["invocationId"] = "00000000-0000-4000-8000-000000000001"
        block = state.as_report()
        self.assertEqual(set(block), {"conversationBindings", "invocationGrantsWritten", "grantTtlSeconds", "cleanup"})
        self.assertEqual(block["conversationBindings"][0]["bindingPkSha256"], hashlib.sha256(b"CONVERSATION#t#m#u#c").hexdigest())

    def test_the_runbook_makes_the_cleanup_mandatory_and_reads_both_reports(self) -> None:
        text = (ROOT / "docs" / "phase-15-live-runbook.md").read_text(encoding="utf-8")
        step_14 = text[text.index("## Step 14"):].replace("Mandatory", "mandatory")
        for needle in ("residualState.conversationBindings", "phase15-harness-checks-", "phase15-routing-", "mandatory", "bindingPkSha256"):
            self.assertIn(needle, step_14)
        self.assertNotIn("accepted residue", text)
        self.assertNotIn("Known gap", text)


if __name__ == "__main__":
    unittest.main()
