"""Second real routing run (2026-11-03): the Harness token cap, the gate for an inherited createOnly property, the token script and the runbook gaps."""

from __future__ import annotations

import copy
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))

from scripts import phase15_changeset_preview as preview  # noqa: E402

TEMPLATE = ROOT / "infra" / "cloudformation" / "phase-01-harness.yaml"
BEFORE = ROOT / "tests" / "fixtures" / "phase15_live" / "phase-01-harness.before-max-tokens.yaml"
RUNBOOK = (ROOT / "docs" / "phase-15-live-runbook.md").read_text(encoding="utf-8")


class _Loader(yaml.SafeLoader):
    pass


_Loader.add_multi_constructor("!", lambda loader, suffix, node: (
    {"Ref": loader.construct_scalar(node)} if suffix == "Ref" and isinstance(node, yaml.ScalarNode)
    else loader.construct_scalar(node) if isinstance(node, yaml.ScalarNode)
    else loader.construct_sequence(node, deep=True) if isinstance(node, yaml.SequenceNode) else loader.construct_mapping(node, deep=True)))


class TheHarnessTokenCapIsOneParameterInBothPlacesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.template = yaml.load(TEMPLATE.read_text(encoding="utf-8"), Loader=_Loader)

    def test_both_caps_read_the_same_parameter_and_no_literal_256_is_left(self) -> None:
        harness = self.template["Resources"]["MinimalHarness"]["Properties"]
        self.assertEqual(harness["MaxTokens"], {"Ref": "MaxTokensPerIteration"})
        self.assertEqual(harness["Model"]["BedrockModelConfig"]["MaxTokens"], {"Ref": "MaxTokensPerIteration"})
        self.assertNotRegex(TEMPLATE.read_text(encoding="utf-8"), r"MaxTokens:\s*256")

    def test_the_default_is_what_the_worker_sends_and_the_range_is_bounded(self) -> None:
        sys.path.insert(0, str(ROOT / "agent" / "src"))
        from legaldesk_agent.client import AGENT_CHAT_MAX_TOKENS

        parameter = self.template["Parameters"]["MaxTokensPerIteration"]
        self.assertEqual((parameter["Type"], parameter["Default"], parameter["MinValue"], parameter["MaxValue"]), ("Number", AGENT_CHAT_MAX_TOKENS, 256, 4096))
        self.assertEqual(AGENT_CHAT_MAX_TOKENS, 2048)

    def test_the_change_set_is_exactly_the_profile_and_in_place(self) -> None:
        deployed = {"EnablePhase08Gateway": "true", "EnablePhase09Memory": "false", "EnableManagedAdot": "false"}
        result = preview.compare(dict(preview.load_template(BEFORE.read_text(encoding="utf-8"))), dict(preview.load_template(TEMPLATE.read_text(encoding="utf-8"))), deployed, deployed)
        self.assertEqual({item["id"]: item["properties"] for item in result["modified"]}, {"MinimalHarness": ["MaxTokens", "Model"]})
        self.assertEqual((result["added"], result["removed"], result["iam"]), ([], [], []))
        self.assertEqual([item["replacement"] for item in result["modified"]], ["no"])
        self.assertEqual(preview.check_preview(result, preview.load_profile("harness-max-tokens")), [])

    def test_neither_property_is_createonly_and_a_replacement_stops_the_gate(self) -> None:
        paths = preview.load_create_only()["AWS::BedrockAgentCore::Harness"]
        self.assertEqual(sorted(paths), ["HarnessName", "Memory/ManagedMemoryConfiguration/EncryptionKeyArn"])
        profile = preview.load_profile("harness-max-tokens")
        live = {"Status": "CREATE_COMPLETE", "Changes": [{"ResourceChange": {
            "Action": "Modify", "LogicalResourceId": "MinimalHarness", "ResourceType": "AWS::BedrockAgentCore::Harness", "Replacement": "False",
            "Details": [{"Target": {"Name": "MaxTokens", "Attribute": "Properties", "RequiresRecreation": "Never"}, "Evaluation": "Static", "ChangeSource": "DirectModification"},
                        {"Target": {"Name": "Model", "Attribute": "Properties", "RequiresRecreation": "Never"}, "Evaluation": "Static", "ChangeSource": "DirectModification"}]}}]}
        self.assertEqual(preview.analyze_change_set(live, profile)["violations"], [])
        replaced = copy.deepcopy(live)
        replaced["Changes"][0]["ResourceChange"]["Replacement"] = "True"
        self.assertTrue(preview.analyze_change_set(replaced, profile)["violations"])

    def test_the_cost_statement_and_the_runbook_say_what_it_costs_and_when_to_stop(self) -> None:
        cost = " ".join((ROOT / "docs" / "phase-15-t9-cost-estimate.md").read_text(encoding="utf-8").split())
        for needle in ("Raising the per-call output cap from 256 to 2048", "Typical cost does not move", "+0.1 to +0.2 USD per 30-case run", "2.20 USD"):
            self.assertIn(needle, cost)
        runbook = " ".join(RUNBOOK.split())
        for needle in ("STOP if `Replacement` is anything but `False`", "not documented behaviour", "MaxTokensPerIteration", "profile `harness-max-tokens`"):
            self.assertIn(needle, runbook)


class AnInheritedUnchangedCreateOnlyPropertyDoesNotStopTheGateTests(unittest.TestCase):
    PROFILE = {"add": [], "remove": [], "modify": {"ReconciliationSchedule": ["State"]}, "iamResources": [], "iam": []}

    def change_set(self, *, source_replaced: str = "False", change_source: str = "ResourceAttribute", evaluation: str = "Dynamic", cause="ReconciliationSchedule.Arn"):
        return {"Status": "CREATE_COMPLETE", "Changes": [
            {"ResourceChange": {"Action": "Modify", "LogicalResourceId": "ReconciliationSchedule", "ResourceType": "AWS::Events::Rule", "Replacement": source_replaced,
                                "Details": [{"Target": {"Name": "State", "Attribute": "Properties", "RequiresRecreation": "Never"}, "Evaluation": "Static", "ChangeSource": "DirectModification"}]}},
            {"ResourceChange": {"Action": "Modify", "LogicalResourceId": "ReconciliationInvokePermission", "ResourceType": "AWS::Lambda::Permission", "Replacement": "Conditional",
                                "Details": [{"Target": {"Name": "SourceArn", "Attribute": "Properties", "RequiresRecreation": "Conditionally"},
                                             "Evaluation": evaluation, "ChangeSource": change_source, "CausingEntity": cause}]}},
        ]}

    def test_the_type_has_a_createonly_list_and_it_contains_source_arn(self) -> None:
        paths = preview.load_create_only()["AWS::Lambda::Permission"]
        self.assertIn("SourceArn", paths)
        self.assertEqual(list(paths), sorted(paths))

    def test_the_live_case_passes_and_is_surfaced_not_silent(self) -> None:
        result = preview.analyze_change_set(self.change_set(), self.PROFILE)
        self.assertEqual(result["violations"], [])
        self.assertEqual(len(result["inherited"]), 1)
        self.assertIn("ReconciliationInvokePermission.SourceArn <- ReconciliationSchedule.Arn", result["inherited"][0])

    def test_every_variation_that_could_really_replace_the_permission_still_stops(self) -> None:
        for label, kwargs in (
            ("the source is replaced too", {"source_replaced": "True"}),
            ("the source is replaced conditionally", {"source_replaced": "Conditional"}),
            ("the value is edited directly", {"change_source": "DirectModification", "evaluation": "Static", "cause": None}),
            ("it comes from a parameter", {"change_source": "ParameterReference", "cause": "ReconciliationCodeKey"}),
            ("it comes from a resource that is not in this change set", {"cause": "SomethingElse.Arn"}),
        ):
            with self.subTest(label):
                result = preview.analyze_change_set(self.change_set(**kwargs), self.PROFILE)
                self.assertTrue(result["violations"], label)

    def test_a_type_without_a_list_still_stops(self) -> None:
        described = self.change_set()
        described["Changes"][1]["ResourceChange"]["ResourceType"] = "AWS::Some::Other"
        self.assertTrue(any("no createOnly list" in item for item in preview.analyze_change_set(described, self.PROFILE)["violations"]))


class TheTokenScriptAndTheRunbookGapsTests(unittest.TestCase):
    SCRIPT = ROOT / "scripts" / "phase15_get_eval_token.ps1"

    def test_the_script_parses_never_prints_the_token_and_refuses_the_repository(self) -> None:
        text = self.SCRIPT.read_text(encoding="utf-8")
        self.assertNotRegex(text, r"(?i)write-(host|output)\s+\$\(?\$?(item|token|cookie)")
        self.assertNotIn("Write-Host", text)
        for needle in ("-AsSecureString", "OutFile must be outside the repository", "icacls", "/inheritance:r", "[Parameter(Mandatory = $true)][string]$OutFile",
                       "Remove-Item $keyFile", "LEGALDESK#P14#STATE#SESSION#", "--consistent-read"):
            self.assertIn(needle, text)
        self.assertNotIn("scan", text.lower().replace("nothing is scanned", ""))  # a point read, never a scan
        if shutil.which("powershell") is None:
            self.skipTest("no powershell")
        command = f"$errors=$null; $null=[System.Management.Automation.Language.Parser]::ParseFile('{self.SCRIPT}',[ref]$null,[ref]$errors); if ($errors) {{ $errors | Out-String; exit 1 }}"
        done = subprocess.run(["powershell", "-NoProfile", "-Command", command], capture_output=True, text=True, timeout=60)  # noqa: S603,S607
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def test_the_runbook_explains_the_token_the_environment_and_the_prompt_paths(self) -> None:
        step_11 = " ".join(RUNBOOK[RUNBOOK.index("## Step 11 "):RUNBOOK.index("## Step 12 ")].split())
        for needle in (
            "phase15_get_eval_token.ps1", "outside the repository", "point read", "no scan", "LEGALDESK_AGENT_PROMPT_PATH", "LEGALDESK_SYSTEM_PROMPT_PATH",
            "/var/task/prompts/legaldesk-agent.md", "legaldesk-system.md", "git -C $Repo checkout $commit", "Remove-Item $tokenFile -Force", "log out of the beta",
        ):
            self.assertIn(needle, step_11)

    def test_pytest_does_not_collect_the_identity_helper(self) -> None:
        sys.path.insert(0, str(ROOT / "backend" / "src"))
        sys.path.insert(0, str(ROOT / "tests"))
        import fixture_loader

        self.assertIs(fixture_loader.test_identity.__test__, False)
        ghost = (ROOT / "tests" / "test_phase15_ghost_documents_cleanup.py").read_text(encoding="utf-8")
        self.assertRegex(ghost, r"import test_identity as operator_identity")
        self.assertFalse(re.search(r"^from fixture_loader import test_identity\s*(#.*)?$", ghost, re.MULTILINE))


if __name__ == "__main__":
    unittest.main()
