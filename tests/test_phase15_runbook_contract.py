"""Contract of the T9 live runbook after the findings of 2026-11-03 (order of step 8, temporary role, BOM, PowerShell syntax)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
RUNBOOK = (ROOT / "docs" / "phase-15-live-runbook.md").read_text(encoding="utf-8")
EXPECTATIONS = json.loads((ROOT / "scripts" / "phase15_t9_expectations.json").read_text(encoding="utf-8"))


def section(text: str, heading: str, level: str = "##") -> str:
    start = text.index(f"{level} {heading}")
    match = re.search(rf"(?m)^{level} (?!#)", text[start + 5:])
    return text[start:start + 5 + match.start()] if match else text[start:]


def powershell_blocks(text: str) -> list[str]:
    """The fenced PowerShell blocks, also the indented ones inside list items (dedented)."""

    blocks = []
    fence = chr(96) * 3
    pattern = r"(?m)^([ ]*)" + fence + r"powershell\n(.*?)\n\1" + fence + r"[ ]*$"
    for match in re.finditer(pattern, text, flags=re.S):
        indent = match.group(1)
        block_lines = match.group(2).split(chr(10))
        blocks.append(chr(10).join(line[len(indent):] if line.startswith(indent) else line for line in block_lines))
    return blocks



class OrderOfStepEightTests(unittest.TestCase):
    def test_the_order_is_temporary_role_8a_promotion_8b_restore_then_step_nine(self) -> None:
        positions = [RUNBOOK.index(marker) for marker in ("### 8.0 ", "### 8a ", "### Promotion ", "### 8b ", "### Restoring the code-only role", "## Step 9 ")]
        self.assertEqual(positions, sorted(positions))

    def test_8a_runs_with_the_agent_off_the_new_code_the_quota_and_the_temporary_role(self) -> None:
        eight_a = section(RUNBOOK, "8a ", "###")
        self.assertIn('"EnableAgentChat=false"', eight_a)
        self.assertIn('"QuotaHarnessPerMonth=50"', eight_a)
        self.assertIn('"ApplicationCodeVersion=$version"', eight_a)
        self.assertIn("--role-arn $tempRole", eight_a)
        self.assertIn("public-edge-code-and-quota", eight_a)
        self.assertIn("cd-template-match", eight_a)
        self.assertIn("**`true`**", eight_a)
        self.assertNotIn("EnableAgentChat=true", eight_a)

    def test_the_promotion_is_verified_with_the_flag_off_and_without_the_step_nine_role(self) -> None:
        promotion = section(RUNBOOK, "Promotion ", "###")
        for needle in ("agentChatWorker: DISABLED", "LegalDeskProductionCloudFormation", "did not need the step 9 role", "cd-template-match", "frontend"):
            self.assertIn(needle, promotion)

    def test_8b_switches_only_the_flag_on_and_keeps_the_code_the_cd_deployed(self) -> None:
        eight_b = section(RUNBOOK, "8b ", "###")
        self.assertIn("--set EnableAgentChat=true --output", eight_b)
        self.assertNotIn("ApplicationCodeKey=", eight_b)
        self.assertNotIn("QuotaHarnessPerMonth=", eight_b.split("**After.**")[0])
        self.assertIn("--role-arn $tempRole", eight_b)
        self.assertIn("public-edge-agent-chat-on", eight_b)
        self.assertIn("stage `agent-chat`", eight_b.lower().replace("Stage", "stage"))
        self.assertIn("never widen the role on the fly", eight_b)

    def test_no_promotion_between_8b_and_step_nine(self) -> None:
        self.assertIn("**Do not promote between 8b and this step.**", section(RUNBOOK, "Step 9 "))

    def test_the_stored_role_is_restored_and_proven_before_the_temporary_stack_is_deleted(self) -> None:
        restore = section(RUNBOOK, "Restoring the code-only role", "###")
        self.assertLess(restore.index('query "Stacks[0].RoleARN"'), restore.index("delete-stack"))
        self.assertIn("NoSuchEntity", restore)
        self.assertIn("does not count", restore)
        step_14 = section(RUNBOOK, "Step 14 ")
        self.assertIn("LegalDeskPhase15EdgeBootstrap", step_14)
        self.assertIn("not closed", step_14)

    def test_the_rollback_of_8a_uses_the_backup_and_the_temporary_role(self) -> None:
        back = section(RUNBOOK, "8a ", "###").split("**Back.**")[1]
        self.assertIn("backup template", back)
        self.assertIn("--role-arn $tempRole", back)
        self.assertIn("continue-update-rollback", back)
        self.assertIn("Never skip a resource whose state you have not read", back)


class ProfilesAndCommandsExistTests(unittest.TestCase):
    def test_every_profile_the_runbook_names_exists(self) -> None:
        names = set(re.findall(r"--profile ([a-z0-9-]+)", RUNBOOK)) | set(re.findall(r"profile `([a-z0-9-]+)`", RUNBOOK))
        self.assertTrue(names)
        for name in names:
            self.assertIn(name, EXPECTATIONS["profiles"], name)

    def test_every_subcommand_the_runbook_uses_exists(self) -> None:
        sys.path.insert(0, str(ROOT))
        from scripts import phase15_changeset_preview as preview

        used = set(re.findall(r"phase15_changeset_preview\.py (\w[\w-]*)", RUNBOOK))
        self.assertTrue({"bootstrap-params", "params", "preview", "check-change-set", "cd-template-match"} <= used)
        parser = preview._parser()
        choices = next(action.choices for action in parser._actions if getattr(action, "choices", None) and "preview" in action.choices)
        self.assertLessEqual(used, set(choices))


class ByteOrderMarkAndChecksTests(unittest.TestCase):
    def test_no_json_or_yaml_is_written_with_a_bom_producing_encoding(self) -> None:
        for block in powershell_blocks(RUNBOOK):
            for line in block.splitlines():
                if line.lstrip().startswith("#"):
                    continue
                if "Out-File" in line and "utf8" in line.lower():
                    self.fail(f"BOM-producing write: {line.strip()}")
                if ".json" in line and "Set-Content" in line:
                    self.assertIn("ascii", line, line)  # Set-Content -Encoding ascii is BOM-free; utf8 is not on 5.1

    def test_the_helper_writes_without_a_bom_and_files_go_through_it(self) -> None:
        self.assertIn("New-Object System.Text.UTF8Encoding($false)", RUNBOOK)
        self.assertIn("[IO.File]::WriteAllText", RUNBOOK)
        step_1 = section(RUNBOOK, "Step 1 ")
        self.assertGreaterEqual(step_1.count("Save-Text"), 5)

    def test_the_checksum_file_does_not_hash_itself(self) -> None:
        step_1 = section(RUNBOOK, "Step 1 ")
        sums = step_1[step_1.index("$sums = "):]
        self.assertIn('-notlike "*SHA256SUMS.txt"', sums)
        self.assertLess(sums.index("$sums = "), sums.index('Save-Text "$BK\\SHA256SUMS.txt"'))
        self.assertNotIn("| Out-File", sums.split("\n")[0])

    def test_every_aws_call_of_step_one_is_checked(self) -> None:
        block = powershell_blocks(section(RUNBOOK, "Step 1 "))[0]
        for line in block.splitlines():
            if re.search(r"(^|[=(\s])aws\s+\w+", line) and not line.lstrip().startswith("#"):
                self.assertIn("Assert-Aws", line, line)

    def test_the_probe_scope_and_its_limits_are_documented(self) -> None:
        step_5 = section(RUNBOOK, "Step 5 ")
        for needle in ("log groups of the Harness runtime", "does **not** read spans", "answerTokenWritten", "no conclusion", "not changed"):
            self.assertIn(needle, step_5)
        probe = (ROOT / "docs" / "phase-15-adot-probe.md").read_text(encoding="utf-8")
        for needle in ("aws/spans", "answerTokenWritten", "X-Ray"):
            self.assertIn(needle, probe)


@unittest.skipUnless(sys.platform == "win32" and shutil.which("powershell"), "parses with Windows PowerShell 5.1")
class PowerShellSyntaxTests(unittest.TestCase):
    """The block that did not even parse on 2026-11-03 (`$Stamp:` read as a drive) must never come back: parse every block."""

    def test_every_powershell_block_of_the_runbook_parses(self) -> None:
        blocks = powershell_blocks(RUNBOOK)
        self.assertGreater(len(blocks), 15)
        with tempfile.TemporaryDirectory() as temp:
            files = []
            for number, block in enumerate(blocks):
                text = re.sub(r"<[^>\n]*>", "PLACEHOLDER", block)
                path = Path(temp) / f"block{number:02d}.ps1"
                path.write_text(text, encoding="utf-8")
                files.append(path)
            script = (
                "$bad = 0; foreach ($f in Get-ChildItem '" + temp + "' -Filter block*.ps1) { $errors = $null; "
                "[void][System.Management.Automation.Language.Parser]::ParseFile($f.FullName, [ref]$null, [ref]$errors); "
                "foreach ($e in $errors) { $bad++; Write-Output ($f.Name + ':' + $e.Extent.StartLineNumber + ': ' + $e.Message) } }; exit $bad"
            )
            done = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)


if __name__ == "__main__":
    unittest.main()
