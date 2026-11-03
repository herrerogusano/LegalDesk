"""The step 14 cleanup of the live runbook: a failure of `aws` must never read as success.

Static contract (everywhere): every `aws` call of the PowerShell block is followed by its `$LASTEXITCODE` check, a
pre-check comes first, and the counters are only incremented on success.

Behaviour (Windows PowerShell 5.1, skipped elsewhere): the very block of the runbook is executed against a fake
`aws` that fails in each possible way, and the run must stop without printing a summary.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))

from evals.residual_state import ResidualState  # noqa: E402

RUNBOOK = ROOT / "docs" / "phase-15-live-runbook.md"
TENANT, MATTER, USER = "tnt_phase14_20260928", "mat_phase14_a_20260928", "user-1"


def cleanup_block() -> str:
    text = RUNBOOK.read_text(encoding="utf-8")
    start = text.index("### Mandatory cleanup")
    opening = text.index("```powershell", start)
    closing = text.index("\n```", opening + 10)
    return text[opening + len("```powershell"):closing].strip("\n")


def executable_lines(block: str) -> list[str]:
    return [line for line in block.splitlines() if line.strip() and not line.strip().startswith("#")]


class StaticContractTests(unittest.TestCase):
    def test_every_aws_call_is_followed_by_its_exit_code_check(self) -> None:
        lines = executable_lines(cleanup_block())
        calls = [i for i, line in enumerate(lines) if re.search(r"(^|[=(\s])aws\s+\w+", line)]
        self.assertEqual(len(calls), 5, "sts, describe-table, get-item, delete-item and the verification get-item: update this test if the block changes")
        for index in calls:
            following = lines[index + 1] if index + 1 < len(lines) else ""
            self.assertIn("$LASTEXITCODE -ne 0", following, f"no exit-code check after: {lines[index].strip()}")
            self.assertIn("throw", following, f"the check must throw: {lines[index].strip()}")

    def test_no_aws_output_is_piped_straight_into_a_parser(self) -> None:
        for line in executable_lines(cleanup_block()):
            self.assertFalse(re.search(r"aws\s+\w+.*\|\s*ConvertFrom-Json", line), f"parse after checking the exit code: {line.strip()}")

    def test_the_pre_check_comes_before_any_read_or_delete_and_before_any_hash_pass(self) -> None:
        block = cleanup_block()
        sts, describe = block.index("aws sts get-caller-identity"), block.index("aws dynamodb describe-table")
        for later in ("aws dynamodb get-item", "aws dynamodb delete-item"):
            self.assertLess(describe, block.index(later))
        self.assertLess(sts, describe)
        self.assertIn('"344774635844"', block)
        self.assertIn("eu-west-1", block)

    def test_every_hash_is_checked_before_the_first_delete(self) -> None:
        block = cleanup_block()
        self.assertLess(block.index("-ne $b.bindingPkSha256"), block.index("aws dynamodb delete-item"))

    def test_counters_move_only_after_a_checked_success(self) -> None:
        lines = executable_lines(cleanup_block())
        delete = next(i for i, line in enumerate(lines) if "aws dynamodb delete-item" in line)
        self.assertIn("$LASTEXITCODE", lines[delete + 1])
        self.assertEqual(lines[delete + 2].strip(), "$deleted++")
        absent = [line for line in lines if "$absent++" in line]
        self.assertEqual(len(absent), 1)
        self.assertIn("$null -eq $item", absent[0])  # absent only when the read succeeded and returned no item
        self.assertTrue(any("-not $out" in line and "throw" in line for line in lines), "empty output is a failure, never absent")

    def test_the_summary_can_only_be_reached_after_all_reads_succeeded(self) -> None:
        block = cleanup_block()
        self.assertLess(block.index("verification get-item failed"), block.index('"reports=$($reports.Count)'))
        self.assertIn("cleanup incomplete", block)
        self.assertEqual(block.count("--consistent-read"), 2)


FAKE_AWS = r'''
import json, os, sys
state_path = os.environ["FAKE_AWS_STATE"]
mode = os.environ.get("FAKE_AWS_MODE", "")
args = sys.argv[1:]
with open(state_path + ".log", "a", encoding="utf-8") as log:
    log.write(" ".join(args[:2]) + "\n")
with open(state_path, encoding="utf-8") as handle:
    state = json.load(handle)

def save():
    with open(state_path, "w", encoding="utf-8") as handle:
        json.dump(state, handle)

def key():
    path = args[args.index("--key") + 1][len("file://"):]
    with open(path, encoding="utf-8-sig") as handle:
        value = json.load(handle)
    return value["pk"]["S"] + "|" + value["sk"]["S"]

if args[0] == "sts":
    if mode == "sts_fail":
        sys.stderr.write("ExpiredToken\n"); sys.exit(255)
    print(os.environ.get("FAKE_AWS_ACCOUNT", "344774635844"))
elif args[:2] == ["dynamodb", "describe-table"]:
    if mode == "describe_fail":
        sys.stderr.write("ResourceNotFoundException\n"); sys.exit(255)
    print(os.environ.get("FAKE_AWS_TABLE_STATUS", "ACTIVE"))
elif args[:2] == ["dynamodb", "get-item"]:
    # verify_fail: the delete loop reads present items; only the verification reads items that are already deleted.
    if mode == "get_fail" or (mode == "verify_fail" and state["deleted"] > 0 and state["items"].get(key()) is None):
        sys.stderr.write("ThrottlingException\n"); sys.exit(255)
    if mode == "get_empty":
        sys.exit(0)
    found = state["items"].get(key())
    print("null" if found is None else json.dumps([key().split("|")[0], found]))
elif args[:2] == ["dynamodb", "delete-item"]:
    if mode == "delete_fail":
        sys.stderr.write("ConditionalCheckFailedException\n"); sys.exit(254)
    state["items"].pop(key(), None)
    state["deleted"] += 1
    save()
else:
    sys.exit(2)
'''


@unittest.skipUnless(sys.platform == "win32" and shutil.which("powershell"), "executes the block with Windows PowerShell 5.1")
class ExecutedBlockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        (self.dir / "bin").mkdir()
        (self.dir / "bin" / "fake_aws.py").write_text(FAKE_AWS, encoding="utf-8")
        (self.dir / "bin" / "aws.cmd").write_text(f'@"{sys.executable}" "%~dp0fake_aws.py" %*\r\n', encoding="ascii")
        (self.dir / "evals" / "results").mkdir(parents=True)
        self.state_path = self.dir / "state.json"
        self.items: dict[str, str] = {}
        self.bindings: list[dict[str, object]] = []

    def add_report(self, name: str, count: int, *, present: int | None = None, tamper_hash: bool = False) -> None:
        residual = ResidualState()
        for number in range(count):
            conversation = f"{name}-{number}-{os.urandom(4).hex()}"
            pk = f"CONVERSATION#{TENANT}#{MATTER}#{USER}#{conversation}"
            entry = residual.conversation_bound(conversation, "sel-" + conversation, pk)
            entry["invocationId"] = "00000000-0000-4000-8000-000000000001"
            if tamper_hash:
                entry["bindingPkSha256"] = "0" * 64
            if present is None or number < present:
                self.items[f"{pk}|SESSION#sel-{conversation}"] = "ConversationBinding"
        prefix = "phase15-harness-checks" if name == "checks" else "phase15-routing"
        (self.dir / "evals" / "results" / f"{prefix}-T-a.json").write_text(json.dumps({"residualState": residual.as_report()}), encoding="utf-8")

    def run_block(self, mode: str = "", **env: str) -> tuple[subprocess.CompletedProcess, dict]:
        self.state_path.write_text(json.dumps({"items": self.items, "deleted": 0}), encoding="utf-8")
        block = cleanup_block()
        block = re.sub(r"(?m)^\$user\s*=.*$", f'$user = "{USER}"', block)
        script = self.dir / "cleanup.ps1"
        script.write_text(f'$Stamp = "T"; $Root = "{self.dir}"; $env:AWS_REGION = "eu-west-1"\r\n' + block + "\r\n", encoding="utf-8")
        environment = {**os.environ, "PATH": str(self.dir / "bin") + os.pathsep + os.environ["PATH"], "FAKE_AWS_STATE": str(self.state_path),
                       "FAKE_AWS_MODE": mode, **env}
        done = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                              cwd=self.dir, env=environment, capture_output=True, text=True, timeout=240)
        return done, json.loads(self.state_path.read_text(encoding="utf-8"))

    def calls(self) -> str:
        path = Path(str(self.state_path) + ".log")
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def assert_stopped(self, done: subprocess.CompletedProcess, *, items_left: int | None = None, state: dict | None = None) -> None:
        self.assertNotEqual(done.returncode, 0, done.stdout)
        self.assertNotIn("reports=", done.stdout, "a failed run must not print the summary line")
        self.assertNotIn("remaining=0", done.stdout)
        if items_left is not None and state is not None:
            self.assertEqual(len(state["items"]), items_left)

    def test_a_clean_run_deletes_everything_and_prints_the_verified_summary(self) -> None:
        self.add_report("checks", 3)
        self.add_report("eval", 5)
        done, state = self.run_block()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("reports=2 expected=8 deleted=8 alreadyAbsent=0 remaining=0", done.stdout)
        self.assertEqual(state["items"], {})

    def test_bindings_already_gone_are_counted_as_absent_only_after_a_successful_read(self) -> None:
        self.add_report("eval", 5, present=2)
        done, state = self.run_block()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("expected=5 deleted=2 alreadyAbsent=3 remaining=0", done.stdout)

    def test_a_failing_get_item_is_not_absent(self) -> None:
        self.add_report("eval", 3)
        done, state = self.run_block("get_fail")
        self.assert_stopped(done, items_left=3, state=state)
        self.assertEqual(state["deleted"], 0)

    def test_a_get_item_that_returns_nothing_is_not_absent(self) -> None:
        self.add_report("eval", 3)
        done, state = self.run_block("get_empty")
        self.assert_stopped(done, items_left=3, state=state)

    def test_a_failing_delete_stops_without_counting_it(self) -> None:
        self.add_report("eval", 3)
        done, state = self.run_block("delete_fail")
        self.assert_stopped(done, items_left=3, state=state)
        self.assertEqual(state["deleted"], 0)
        self.assertEqual(self.calls().count("dynamodb delete-item"), 1, "it stops at the first failed delete")

    def test_a_failing_verification_read_cannot_write_remaining_zero(self) -> None:
        self.add_report("eval", 3)
        done, state = self.run_block("verify_fail")
        self.assertEqual(state["deleted"], 3)  # everything was deleted, but the verification could not read: not verified
        self.assert_stopped(done)

    def test_an_item_that_changed_type_is_not_deleted(self) -> None:
        self.add_report("eval", 2)
        first = next(iter(self.items))
        self.items[first] = "SomethingElse"
        done, state = self.run_block()
        self.assert_stopped(done)
        self.assertEqual(state["items"].get(first), "SomethingElse")

    def test_expired_credentials_stop_before_anything_else(self) -> None:
        self.add_report("eval", 2)
        done, state = self.run_block("sts_fail")
        self.assert_stopped(done, items_left=2, state=state)
        self.assertNotIn("dynamodb", self.calls())

    def test_the_wrong_account_stops_before_any_dynamodb_call(self) -> None:
        self.add_report("eval", 2)
        done, state = self.run_block(FAKE_AWS_ACCOUNT="111111111111")
        self.assert_stopped(done, items_left=2, state=state)
        self.assertNotIn("dynamodb", self.calls())

    def test_an_unreachable_table_stops_before_any_read(self) -> None:
        self.add_report("eval", 2)
        done, state = self.run_block("describe_fail")
        self.assert_stopped(done, items_left=2, state=state)
        self.assertNotIn("get-item", self.calls())

    def test_an_inactive_table_stops(self) -> None:
        self.add_report("eval", 2)
        done, state = self.run_block(FAKE_AWS_TABLE_STATUS="UPDATING")
        self.assert_stopped(done, items_left=2, state=state)

    def test_one_bad_hash_stops_before_the_first_delete(self) -> None:
        self.add_report("checks", 2)
        self.add_report("eval", 2, tamper_hash=True)
        done, state = self.run_block()
        self.assert_stopped(done, items_left=4, state=state)
        self.assertNotIn("delete-item", self.calls())
        self.assertNotIn("get-item", self.calls())

    def test_no_report_or_an_empty_one_is_an_error_not_a_clean_pass(self) -> None:
        done, _ = self.run_block()
        self.assert_stopped(done)
        self.add_report("eval", 0)
        done, _ = self.run_block()
        self.assert_stopped(done)


if __name__ == "__main__":
    unittest.main()
