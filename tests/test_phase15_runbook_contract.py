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


def logical_lines(block: str) -> list[str]:
    """Statements: physical lines joined where a trailing backtick continues them."""

    lines, current = [], ""
    for line in block.splitlines():
        if line.rstrip().endswith(chr(96)):
            current += line.rstrip()[:-1] + " "
        else:
            lines.append(current + line)
            current = ""
    if current:
        lines.append(current)
    return lines


def strip_strings(code: str) -> str:
    """Quoted text removed (a '>' inside a placeholder or a message is not a redirection); a trailing comment removed."""

    code = re.sub(r"<[^>]*>", "PLACEHOLDER", code)  # <ref>, <HarnessArn>...: placeholders of the document, not operators
    code = re.sub(r'"(?:[^"`]|`.)*"', '""', code)
    code = re.sub(r"'[^']*'", "''", code)
    return re.sub(r"\s#.*$", "", code)


def section(text: str, heading: str, level: str = "##") -> str:
    start = text.index(f"{level} {heading}")
    # Ends at the next heading of the same or a higher level ("###" stops at "##" too).
    match = re.search(rf"(?m)^#{{2,{len(level)}}} (?!#)", text[start + len(level) + 1:])  # "# " lines inside code blocks are comments
    return text[start:start + len(level) + 1 + match.start()] if match else text[start:]


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
    def test_the_order_is_temporary_role_8a_promotion_8b_step_nine_then_the_closing_step_9b(self) -> None:
        positions = [RUNBOOK.index(marker) for marker in ("### 8.0 ", "### 8a ", "### Promotion ", "### 8b ", "## Step 9 ", "## Step 9b ", "## Step 10 ")]
        self.assertEqual(positions, sorted(positions))
        # the old section that said "any promotion works" and sat before step 9 is gone
        self.assertNotIn("### Restoring the code-only role", RUNBOOK)
        self.assertNotRegex(RUNBOOK, r"(?i)any promotion works|promote any commit|next code-only CD release passes")

    def test_the_promotion_that_restores_the_role_comes_after_step_nine_and_is_made_right_away(self) -> None:
        closing = section(RUNBOOK, "Step 9b ")
        for needle in ("right after step 9", "same session", "documentation", "docs-only", "closing commit", "agentChatWorker: VERIFIED", "first real use of the new CD path"):
            self.assertIn(needle, closing)
        self.assertLess(closing.index("describe-stacks"), closing.index("delete-stack"))
        self.assertIn("**A temporary role that is still there is a blocker, not an open item:**", closing)
        self.assertIn("the temporary stack cannot be deleted", closing)
        self.assertNotIn("open item with its owner", RUNBOOK)

    def test_everything_before_step_nine_forbids_promoting_before_it(self) -> None:
        step_9 = section(RUNBOOK, "Step 9 ")
        self.assertIn("**Do not promote between 8b and this step.**", step_9)
        self.assertIn("step 9b", step_9)
        self.assertIn("comes **after** this step", step_9)
        eight_b = section(RUNBOOK, "8b ", "###")
        self.assertIn("**Do not promote until step 9 is done**", eight_b)
        self.assertIn("step 9b", eight_b)
        # the only promotion allowed before 8b is the one that publishes the frontend, and it precedes 8b
        self.assertLess(RUNBOOK.index("### Promotion "), RUNBOOK.index("### 8b "))

    def test_step_fourteen_treats_a_surviving_temporary_role_as_blocking(self) -> None:
        step_14 = section(RUNBOOK, "Step 14 ")
        self.assertIn("step 9b", step_14)
        self.assertIn("**blocked**", step_14)
        self.assertNotRegex(step_14, r"(?i)open item")

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
        closing = section(RUNBOOK, "Step 9b ")
        self.assertLess(closing.index('--query "Stacks[0].RoleARN"'), closing.index("aws cloudformation delete-stack"))
        self.assertIn("NoSuchEntity", closing)
        self.assertIn("does not count", closing)
        self.assertIn("STOP, do not delete the temporary stack", closing)

    def test_the_rollback_of_8a_uses_the_backup_and_the_temporary_role(self) -> None:
        back = section(RUNBOOK, "8a ", "###").split("**Back.**")[1]
        self.assertIn("backup template", back)
        self.assertIn("--role-arn $tempRole", back)
        self.assertIn("continue-update-rollback", back)
        self.assertIn("Never skip a resource whose state you have not read", back)


class SectionHelperAndStep9bGuardTests(unittest.TestCase):
    def test_section_stops_at_a_same_or_higher_level_heading(self) -> None:
        doc = "## A\n### B\nb text\n## C\nc text\n"
        self.assertNotIn("c text", section(doc, "B", "###"))
        self.assertTrue(section(doc, "A").startswith("## A"))
        self.assertNotIn("c text", section(doc, "A"))

    def test_step_9b_enforces_the_restored_role_before_deleting_the_temporary_stack(self) -> None:
        block = "\n".join(powershell_blocks(section(RUNBOOK, "Step 9b ")))
        guard = block.index("-notmatch '/LegalDeskProductionCloudFormation$'")
        self.assertLess(guard, block.index("delete-stack"))
        self.assertIn("throw", block[guard:block.index("delete-stack")])

    def test_step_9b_requires_no_such_entity_for_the_temporary_role(self) -> None:
        block = "\n".join(powershell_blocks(section(RUNBOOK, "Step 9b ")))
        self.assertIn('-notmatch "NoSuchEntity"', block)
        self.assertNotIn("get-role --role-name (Split-Path $tempRole -Leaf) 2>$null", block)


class GeneratedNamesInTheRunbookTests(unittest.TestCase):
    """Step 8b failed on 2026-11-03 because the pattern of the worker role assumed the wrong name; the runbook now carries the rule and checks it."""

    def test_the_rule_its_provenance_and_both_observed_names_are_documented(self) -> None:
        eight_zero = " ".join(section(RUNBOOK, "8.0 ", "###").split())
        for needle in (
            "inferred from two names observed on 2026-11-03", "not AWS documentation",
            "LegalDeskPhase14PublicEdge-ApplicationExecutionRole-X3hgVtmlHWXr", "LegalDeskPhase14PublicEdg-AgentChatWorkerExecutionR-bzFuZq7zur6f",
            "**both cut**", "UPDATE_ROLLBACK_COMPLETE", "iam:DeleteRolePolicy", "cfn_generated_name",
            "LegalDeskPhase14PublicEdg-AgentChatWorkerExecutionR-*", "LegalDeskPhase14PublicEd*-AgentChat*",
        ):
            self.assertIn(needle, eight_zero)

    def test_the_old_pattern_survives_only_as_the_explanation_of_the_failure(self) -> None:
        old = "LegalDeskPhase14PublicEdge-AgentChatWorker*"
        self.assertEqual(RUNBOOK.count(old), 1)
        self.assertIn("assumed that only the logical id is cut", RUNBOOK[RUNBOOK.index(old) - 200:RUNBOOK.index(old) + 100])
        template = (ROOT / "infra" / "cloudformation" / "phase-15-edge-agentchat-bootstrap.yaml").read_text(encoding="utf-8")
        cd_iam = (ROOT / "infra" / "cloudformation" / "cd-iam.yaml").read_text(encoding="utf-8")
        for text in (template, cd_iam):
            self.assertNotIn("LegalDeskPhase14PublicEdge-AgentChatWorker", text.replace("LegalDeskPhase14PublicEdge-AgentChatWorker.", ""))

    def test_8b_simulates_with_the_name_cloudformation_will_generate_not_an_example(self) -> None:
        eight_b = section(RUNBOOK, "8b ", "###")
        self.assertIn("phase15_changeset_preview.py generated-name --stack LegalDeskPhase14PublicEdge --logical-id AgentChatWorkerExecutionRole", eight_b)
        self.assertIn("--logical-id ApplicationExecutionRole", eight_b)  # and the application role's name must be denied
        self.assertIn("--logical-id AgentChatWorkerErrorsAlarm --limit 255", eight_b)
        for action in ("iam:CreateRole", "iam:TagRole", "iam:PutRolePolicy", "iam:DeleteRolePolicy", "iam:DeleteRole", "iam:PassRole"):
            self.assertIn(action, eight_b)
        self.assertNotIn("AgentChatWorkerExecution-X", RUNBOOK)  # the old example name is gone
        self.assertIn("If the worker-role line above is not all **allowed**, stop", eight_b)

    def test_the_tagging_access_denied_of_create_role_is_named_as_this_case(self) -> None:
        eight_b = " ".join(section(RUNBOOK, "8b ", "###").split())
        self.assertIn("tagging operation of `iam:CreateRole`", eight_b)
        self.assertIn("compare the role name in the event with `generated-name`", eight_b)


class PassRoleSimulationTests(unittest.TestCase):
    def test_passrole_is_simulated_with_the_passed_to_service_context(self) -> None:
        for line in RUNBOOK.splitlines():
            if "simulate-principal-policy" in line and "iam:PassRole" in line:
                self.assertIn("ContextKeyName=iam:PassedToService", line, line)
                self.assertIn("ContextKeyValues=lambda.amazonaws.com", line, line)


class Step9SimulatesTheAttributeReadsTests(unittest.TestCase):
    """The CD of 2026-11-03 failed on sqs:GetQueueAttributes, an attribute read that step 9 did not simulate."""

    def test_step_9_simulates_the_reads_with_real_arns_from_the_deployed_stack(self) -> None:
        step_9 = section(RUNBOOK, "Step 9 ")
        for needle in (
            'AgentChatQueueArn', 'AgentChatDeadLetterQueueArn', 'describe-stacks --stack-name LegalDeskPhase14PublicEdge',
            "--action-names sqs:GetQueueAttributes sqs:GetQueueUrl --resource-arns $queueArn $dlqArn",
            "--action-names iam:GetRole --resource-arns $workerRole",
            "sqs:SendMessage sqs:ReceiveMessage sqs:SetQueueAttributes sqs:PurgeQueue --resource-arns $queueArn",
        ):
            self.assertIn(needle, step_9)
        # the queue reads are simulated for the code-only role, and denied for a queue outside the two
        self.assertEqual(step_9.count("--policy-source-arn $cfnRole --action-names sqs:"), 3)

    def test_the_stop_if_names_the_unable_to_retrieve_attribute_error_as_this_case(self) -> None:
        stop = " ".join(section(RUNBOOK, "Step 9 ").split())
        for needle in ("Unable to retrieve ... attribute", "stack_update_failed", "is **this case**", "only that read", "tests/test_cd_iam_attribute_reads.py", "do not widen with a wildcard"):
            self.assertIn(needle, stop)

    def test_an_already_executed_step_9_is_rerun_with_the_current_template(self) -> None:
        step_9 = " ".join(section(RUNBOOK, "Step 9 ").split())
        self.assertIn("run **this step again**", step_9)
        self.assertIn("ReadOnlyAgentChatQueueAttributes", step_9)


class Step11ParserVersusHarnessTests(unittest.TestCase):
    """Live 2026-11-03: our parser's rejection was reported as a Harness rejection and sent the owner towards the wrong fix."""

    def test_the_closed_code_is_documented_with_its_meaning_and_the_wrong_fix_to_avoid(self) -> None:
        step_11 = " ".join(section(RUNBOOK, "Step 11 ").split())
        for needle in (
            "client_parser_rejected", "ClientStreamParserError", "**our** stream parser", "not** an `AllowedTools` change",
            "legaldesk_gateway_search-documents___search_documents", "@legaldesk_gateway/search-documents___search_documents",
            "`@<group>/<rest>` → `<group>_<rest>`", "**Harness itself**", "ackDiagnosis", "inside_longer_token", "never their text",
        ):
            self.assertIn(needle, step_11)

    def test_every_verdict_of_the_script_for_allowed_tools_is_in_the_runbook_table(self) -> None:
        from scripts import phase15_harness_checks as checks

        row = next(line for line in section(RUNBOOK, "Step 11 ").splitlines() if line.startswith("| `allowed_tools` (pre-check"))
        for verdict in checks.ALLOWED_TOOLS:
            if verdict != "not_run":
                self.assertIn(f"`{verdict}`", row)


class Step11SecondReportTests(unittest.TestCase):
    """The findings of the second step 11 run: a hidden Harness session and a search call that never reached its target."""

    def test_the_session_and_selector_findings_are_documented_with_their_closed_labels(self) -> None:
        step_11 = " ".join(section(RUNBOOK, "Step 11 ").split())
        for needle in (
            "a new `runtimeSessionId` on every agent call", "sessionPolicy", "toolErrorHints", "missing_required_argument", "denyStage",
            "raw_matter_selector", "tool_not_in_invocation", "matter_selector_instruction", "Not proven without AWS", "It is a selector, not authority",
        ):
            self.assertIn(needle, step_11)

    def test_every_deny_stage_of_the_interceptor_is_documented(self) -> None:
        import sys

        sys.path.insert(0, str(ROOT / "backend" / "src"))
        from legaldesk import gateway_interceptor

        step_11 = section(RUNBOOK, "Step 11 ")
        for stage in gateway_interceptor._DENY_STAGES:
            self.assertIn(f"`{stage}`", step_11)


class Step12FirstRunFindingsTests(unittest.TestCase):
    """The first real routing run failed the gate; the runbook says what was found offline, what is open and what the next report will say."""

    def test_the_findings_the_change_set_and_the_open_questions_are_written_down(self) -> None:
        step_12 = " ".join(section(RUNBOOK, "Step 12 ").split())
        for needle in (
            "Not pinned without AWS", "retrieval_object_denied", "search_documents_gateway_handler", "SearchToolError", "tool_reported_error",
            "check-change-set --profile gateway-search-handler-and-code", "Replacement` `False` on all three", "sourceIdMatches", "unansweredTools",
            "streamErrorHint", "owner decision", "fail closed",
        ):
            self.assertIn(needle, step_12)

    def test_every_retrieval_code_named_in_the_runbook_exists_in_the_telemetry_vocabulary(self) -> None:
        import sys

        sys.path.insert(0, str(ROOT / "backend" / "src"))
        from legaldesk.observability import TelemetryErrorCode

        step_12 = section(RUNBOOK, "Step 12 ")
        named = set(re.findall(r"`(retrieval_[a-z_]+)`", step_12))
        self.assertGreaterEqual(len(named), 8)
        self.assertTrue(named <= {code.value for code in TelemetryErrorCode}, named)

    def test_the_profile_named_by_the_runbook_exists(self) -> None:
        from scripts import phase15_changeset_preview as preview

        self.assertEqual(preview.load_profile("gateway-search-handler-and-code")["iam"], [])


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
    def test_no_powershell_write_uses_a_bom_producing_or_default_encoding(self) -> None:
        """PowerShell 5.1: Out-File and `>` default to UTF-16LE with a BOM, Set-Content to the ANSI code page, `-Encoding utf8` writes a BOM.

        The only encodings allowed are `ascii` (no BOM) and the Save-Text helper (UTF-8 without BOM).
        """

        for block in powershell_blocks(RUNBOOK):
            for logical in logical_lines(block):
                code = strip_strings(logical)
                if code.lstrip().startswith("#"):
                    continue
                for cmdlet in ("Out-File", "Set-Content", "Add-Content"):
                    if re.search(rf"\b{cmdlet}\b", code):
                        self.assertRegex(logical, r"-Encoding\s+ascii\b", f"{cmdlet} without -Encoding ascii: {logical.strip()}")
                for cmdlet in ("Tee-Object", "Export-Csv", "Export-Clixml", "Out-Printer"):
                    self.assertNotRegex(code, rf"\b{cmdlet}\b", logical.strip())
                for redirection in re.finditer(r"(?<![-<])(\*|\d)?>{1,2}(?!=)\s*([^\s;)]*)", code):
                    # "2>&1" merges streams into the pipeline; it writes no file.
                    self.assertIn(redirection.group(2), ("$null", "&1"), f"file redirection (UTF-16 with BOM by default): {logical.strip()}")

    def test_dotnet_writes_go_only_through_the_one_helper_with_the_no_bom_encoding(self) -> None:
        writes = [line for block in powershell_blocks(RUNBOOK) for line in block.splitlines() if re.search(r"\[(System\.)?IO\.File\]::(Write|Append|Create)", line) and not line.lstrip().startswith("#")]
        self.assertEqual(len(writes), 1, writes)
        self.assertIn("WriteAllText", writes[0])
        self.assertIn("$Utf8NoBom", writes[0])
        self.assertNotRegex(RUNBOOK, r"UTF8Encoding\(\$true\)|\[Text\.Encoding\]::UTF8\)\s*\)?\s*$")

    def test_every_yaml_and_json_the_runbook_saves_goes_through_save_text(self) -> None:
        for block in powershell_blocks(RUNBOOK):
            for logical in logical_lines(block):
                if logical.lstrip().startswith("#"):
                    continue
                if re.search(r"\.(ya?ml|json)\b", logical) and re.search(r"\b(Out-File|Set-Content|Add-Content)\b", strip_strings(logical)):
                    # the two small ASCII-only helper files of the cleanup (key and values of a conditional delete) may use Set-Content -Encoding ascii
                    self.assertRegex(logical, r"-Encoding\s+ascii\b", logical.strip())
                    self.assertTrue("key.json" in logical or "values.json" in logical, f"save this with Save-Text: {logical.strip()}")

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
    def _parse_all(self, ast_script: str) -> subprocess.CompletedProcess:
        blocks = powershell_blocks(RUNBOOK)
        with tempfile.TemporaryDirectory() as temp:
            for number, block in enumerate(blocks):
                (Path(temp) / f"block{number:02d}.ps1").write_text(re.sub(r"<[^>\n]*>", "PLACEHOLDER", block), encoding="utf-8")
            script = "$bad = 0; foreach ($f in Get-ChildItem '" + temp + "' -Filter block*.ps1) { $ast = [System.Management.Automation.Language.Parser]::ParseFile($f.FullName, [ref]$null, [ref]$null); " + ast_script + " }; exit $bad"
            return subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)

    def test_the_syntax_tree_has_no_write_with_the_default_encoding_or_a_file_redirection(self) -> None:
        """Same rule as the static test, on the parsed commands (it also sees what a regex on text cannot, such as splatting or odd continuations)."""

        ast_script = (
            "foreach ($c in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true)) { "
            "$name = $c.GetCommandName(); "
            "if ($name -in 'Out-File','Set-Content','Add-Content') { $enc = $null; $els = $c.CommandElements; "
            "for ($i = 0; $i -lt $els.Count - 1; $i++) { if ($els[$i] -is [System.Management.Automation.Language.CommandParameterAst] -and $els[$i].ParameterName -like 'Enc*') { $enc = $els[$i + 1].ToString() } } "
            "if ($enc -ne 'ascii') { $bad++; Write-Output ($f.Name + ': ' + $name + ' without -Encoding ascii: ' + $c.Extent.Text) } } "
            "if ($name -in 'Tee-Object','Export-Csv','Export-Clixml') { $bad++; Write-Output ($f.Name + ': ' + $name) } } "
            "foreach ($r in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FileRedirectionAst] }, $true)) { "
            "if ($r.Location.Extent.Text -ne '$null') { $bad++; Write-Output ($f.Name + ': redirection to ' + $r.Location.Extent.Text) } }"
        )
        done = self._parse_all(ast_script)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

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
