"""check-change-set against the real change sets of the T9 live block (2026-11-03) and the rules they taught.

Fixtures: ``tests/fixtures/phase15_live/*.json``, trimmed and anonymised ``describe-change-set`` output.
Before the fix, the Gateway and the Harness change sets stopped on changes that were not edits:
``RoleArn`` / ``InterceptorConfigurations`` of the Gateway (inherited from the role and the Lambda of the same change
set, none of them replaced) and ``ExecutionRoleArn`` of the Harness, and ``Memory`` was treated as a replacement risk
although only ``HarnessName`` and ``Memory/ManagedMemoryConfiguration/EncryptionKeyArn`` are createOnly.
"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from scripts import phase15_changeset_preview as preview

ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "phase15_live"
CASES = (
    ("harness-memory-off-adot-on", "harness-memory-off-adot-on"),
    ("harness-adot-off", "harness-adot-off"),
    ("gateway-search-documents", "gateway-search-documents"),
    ("edge-8a-code-and-quota", "public-edge-code-and-quota"),
)


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def change(described: dict[str, Any], logical_id: str) -> dict[str, Any]:
    return next(item["ResourceChange"] for item in described["Changes"] if item["ResourceChange"]["LogicalResourceId"] == logical_id)


def detail(described: dict[str, Any], logical_id: str, name: str) -> dict[str, Any]:
    return next(d for d in change(described, logical_id)["Details"] if d["Target"]["Name"] == name)


def analysis(described: dict[str, Any], profile_name: str, **profile_overrides: Any) -> dict[str, Any]:
    profile = {**preview.load_profile(profile_name), **profile_overrides}
    return preview.analyze_change_set(described, profile)


class RealChangeSetsTests(unittest.TestCase):
    def test_the_fixtures_are_anonymised(self) -> None:
        for path in FIXTURES.glob("*.json"):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("344774635844", text, path.name)
            self.assertNotIn("arn:", text, path.name)
            self.assertNotIn("﻿", text)

    def test_every_real_change_set_of_the_live_block_passes_its_profile(self) -> None:
        for name, profile in CASES:
            with self.subTest(name):
                self.assertEqual(analysis(fixture(name), profile)["violations"], [])

    def test_the_gateway_inherited_changes_are_reported_as_accepted_by_rule(self) -> None:
        result = analysis(fixture("gateway-search-documents"), "gateway-search-documents")
        self.assertEqual(result["violations"], [])
        self.assertIn("LegalDeskGateway.RoleArn <- GatewayExecutionRole.Arn", result["inherited"])
        self.assertIn("LegalDeskGateway.InterceptorConfigurations <- GatewayRequestInterceptorFunction.Arn", result["inherited"])
        # LegalDeskGateway is in no profile list: the rule alone accepts it
        self.assertNotIn("LegalDeskGateway", preview.load_profile("gateway-search-documents")["modify"])

    def test_the_harness_inherited_role_and_conditional_memory_are_reported_as_accepted_by_rule(self) -> None:
        result = analysis(fixture("harness-memory-off-adot-on"), "harness-memory-off-adot-on")
        self.assertEqual(result["inherited"], ["MinimalHarness.ExecutionRoleArn <- HarnessExecutionRole.Arn"])
        self.assertEqual(len(result["conditionalAccepted"]), 1)
        self.assertTrue(result["conditionalAccepted"][0].startswith("MinimalHarness.Memory:"))

    def test_the_8a_change_set_is_only_code_environment_and_the_inherited_integration(self) -> None:
        described = fixture("edge-8a-code-and-quota")
        self.assertEqual({item["ResourceChange"]["Action"] for item in described["Changes"]}, {"Modify"})
        result = analysis(described, "public-edge-code-and-quota")
        self.assertEqual(result["inherited"], ["PublicApiIntegration.IntegrationUri <- ApplicationFunction.Arn"])
        self.assertEqual(preview.load_profile("public-edge-code-and-quota")["add"], [])

    def test_the_command_prints_what_it_accepted_by_rule(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "cs.json"
            path.write_text(json.dumps(fixture("gateway-search-documents")), encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = preview.main(["check-change-set", "--change-set-json", str(path), "--profile", "gateway-search-documents"])
        report = json.loads(out.getvalue())
        self.assertEqual((code, report["ok"]), (0, True))
        self.assertEqual(len(report["acceptedAsInherited"]), 4)


class InheritedChangeRuleTests(unittest.TestCase):
    """A dynamic change is inherited only from a resource of the same change set that is modified and not replaced."""

    def gateway(self) -> dict[str, Any]:
        return copy.deepcopy(fixture("gateway-search-documents"))

    def problems(self, described: dict[str, Any]) -> list[str]:
        return analysis(described, "gateway-search-documents")["violations"]

    def test_a_cause_that_is_replaced_does_not_make_it_inherited(self) -> None:
        described = self.gateway()
        change(described, "GatewayExecutionRole")["Replacement"] = "True"
        problems = self.problems(described)
        self.assertTrue(any("replacement risk (True): GatewayExecutionRole" in p for p in problems), problems)
        self.assertTrue(any("LegalDeskGateway" in p and "RoleArn" in p for p in problems), problems)

    def test_a_cause_outside_the_change_set_does_not_make_it_inherited(self) -> None:
        described = self.gateway()
        described["Changes"] = [item for item in described["Changes"] if item["ResourceChange"]["LogicalResourceId"] != "GatewayExecutionRole"]
        problems = self.problems(described)
        self.assertTrue(any("LegalDeskGateway" in p and "RoleArn" in p for p in problems), problems)

    def test_a_cause_that_is_only_added_does_not_make_it_inherited(self) -> None:
        described = self.gateway()
        detail(described, "LegalDeskGateway", "RoleArn")["CausingEntity"] = "SearchDocumentsExecutionRole.Arn"
        self.assertTrue(any("LegalDeskGateway" in p for p in self.problems(described)))

    def test_only_dynamic_resource_attribute_details_can_be_inherited(self) -> None:
        for field, value in (("Evaluation", "Static"), ("ChangeSource", "DirectModification")):
            with self.subTest(field):
                described = self.gateway()
                detail(described, "LegalDeskGateway", "RoleArn")[field] = value
                self.assertTrue(any("LegalDeskGateway" in p for p in self.problems(described)))

    def test_a_resource_cannot_inherit_from_itself(self) -> None:
        described = self.gateway()
        detail(described, "LegalDeskGateway", "RoleArn")["CausingEntity"] = "LegalDeskGateway.Arn"
        self.assertTrue(any("LegalDeskGateway" in p for p in self.problems(described)))

    def test_a_parameter_cause_is_never_inherited(self) -> None:
        described = self.gateway()
        detail(described, "LegalDeskGateway", "RoleArn")["CausingEntity"] = "GatewayJwtClientId"
        self.assertTrue(any("LegalDeskGateway" in p for p in self.problems(described)))

    def test_a_direct_edit_next_to_an_inherited_one_still_has_to_be_listed(self) -> None:
        described = self.gateway()
        change(described, "LegalDeskGateway")["Details"].append(
            {"Target": {"Attribute": "Properties", "Name": "Description", "RequiresRecreation": "Never"}, "Evaluation": "Static", "ChangeSource": "DirectModification"})
        self.assertTrue(any("unexpected modification: LegalDeskGateway [Description]" in p for p in self.problems(described)))

    def test_inheritance_does_not_hide_iam_changes_outside_the_profile(self) -> None:
        described = self.gateway()
        extra = copy.deepcopy(change(described, "MetadataMcpFunction"))
        extra.update(LogicalResourceId="MetadataMcpExecutionRole", ResourceType="AWS::IAM::Role")
        described["Changes"].append({"Type": "Resource", "ResourceChange": extra})
        self.assertTrue(any("IAM resource changed outside the profile: MetadataMcpExecutionRole" in p for p in self.problems(described)))


class ConditionalReplacementTests(unittest.TestCase):
    """Replacement Conditional is decided by the createOnly list of the type, not by stopping blindly."""

    def harness(self) -> dict[str, Any]:
        return copy.deepcopy(fixture("harness-memory-off-adot-on"))

    def problems(self, described: dict[str, Any], **overrides: Any) -> list[str]:
        return analysis(described, "harness-memory-off-adot-on", **overrides)["violations"]

    def test_the_createOnly_list_of_the_harness_is_the_one_from_describe_type(self) -> None:
        self.assertEqual(preview.load_create_only()["AWS::BedrockAgentCore::Harness"], ("HarnessName", "Memory/ManagedMemoryConfiguration/EncryptionKeyArn"))

    def test_memory_is_accepted_only_when_the_profile_accepts_the_nested_path_explicitly(self) -> None:
        self.assertEqual(self.problems(self.harness()), [])
        problems = self.problems(self.harness(), conditionalNestedAllowed={})
        self.assertEqual(len(problems), 1)
        self.assertIn("MinimalHarness.Memory has createOnly paths below it (Memory/ManagedMemoryConfiguration/EncryptionKeyArn)", problems[0])

    def test_a_createOnly_property_that_changes_stops(self) -> None:
        described = self.harness()
        change(described, "MinimalHarness")["Details"].append(
            {"Target": {"Attribute": "Properties", "Name": "HarnessName", "RequiresRecreation": "Conditionally"}, "Evaluation": "Static", "ChangeSource": "DirectModification"})
        problems = self.problems(described, modify={"MinimalHarness": ["Memory", "EnvironmentVariables", "HarnessName"], "HarnessExecutionRole": ["Policies"]})
        self.assertIn("replacement risk (Conditional): MinimalHarness.HarnessName is createOnly", problems)

    def test_a_property_that_cloudformation_calls_conditional_but_the_list_does_not_explain_stops(self) -> None:
        described = self.harness()
        detail(described, "MinimalHarness", "EnvironmentVariables")["Target"]["RequiresRecreation"] = "Conditionally"
        problems = self.problems(described)
        self.assertTrue(any("EnvironmentVariables has no createOnly path" in p and "stale or incomplete" in p for p in problems), problems)

    def test_a_type_without_a_list_stops(self) -> None:
        described = self.harness()
        change(described, "MinimalHarness")["ResourceType"] = "AWS::Made::Up"
        self.assertTrue(any("no createOnly list for AWS::Made::Up" in p for p in self.problems(described)))

    def test_always_and_true_are_never_accepted(self) -> None:
        described = self.harness()
        change(described, "MinimalHarness")["Replacement"] = "True"
        self.assertTrue(any("replacement risk (True): MinimalHarness" in p for p in self.problems(described)))
        described = self.harness()
        detail(described, "MinimalHarness", "Memory")["Target"]["RequiresRecreation"] = "Always"
        self.assertTrue(self.problems(described))

    def test_a_conditional_without_any_property_that_explains_it_stops(self) -> None:
        described = self.harness()
        for item in change(described, "MinimalHarness")["Details"]:
            item["Target"]["RequiresRecreation"] = "Never"
        self.assertTrue(any("no property explains it" in p for p in self.problems(described)))

    def test_the_preview_reads_the_same_list(self) -> None:
        table = preview.load_create_only()
        self.assertEqual(preview.replacement_of("AWS::BedrockAgentCore::Harness", ["EnvironmentVariables"], table), ("no", []))
        self.assertEqual(preview.replacement_of("AWS::BedrockAgentCore::Harness", ["Memory", "EnvironmentVariables"], table), ("CONDITIONAL", ["Memory"]))
        self.assertEqual(preview.replacement_of("AWS::BedrockAgentCore::Harness", ["HarnessName"], table), ("REPLACEMENT", []))
        self.assertEqual(preview.replacement_of("AWS::Made::Up", ["X"], table), ("UNKNOWN", []))

    def test_the_preview_accepts_a_conditional_only_when_named_by_the_profile(self) -> None:
        template = preview.load_template((ROOT / "infra" / "cloudformation" / "phase-01-harness.yaml").read_text(encoding="utf-8"))
        base = {"EnablePhase08Gateway": "true"}
        result = preview.compare(template, template, {**base, "EnablePhase09Memory": "true", "EnableManagedAdot": "false"},
                                 {**base, "EnablePhase09Memory": "false", "EnableManagedAdot": "true"})
        harness = next(item for item in result["modified"] if item["id"] == "MinimalHarness")
        self.assertEqual((harness["replacement"], harness["conditionalProperties"]), ("CONDITIONAL", ["Memory"]))
        profile = dict(preview.load_profile("harness-memory-off-adot-on"))
        self.assertEqual(preview.check_preview(result, profile), [])
        profile["conditionalNestedAllowed"] = {}
        self.assertTrue(any("replacement risk (CONDITIONAL): MinimalHarness" in p for p in preview.check_preview(result, profile)))


class ByteOrderMarkTests(unittest.TestCase):
    """Windows PowerShell 5.1 `Out-File -Encoding utf8` writes a BOM: every operator-facing input must survive it."""

    BOM = "﻿"

    def write(self, directory: Path, name: str, text: str) -> Path:
        path = directory / name
        path.write_bytes((self.BOM + text).encode("utf-8"))
        return path

    def test_params_reads_a_stack_backup_and_a_template_with_a_bom(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            stack = self.write(directory, "stack.json", json.dumps({"Stacks": [{"StackName": "S", "Parameters": [{"ParameterKey": "A", "ParameterValue": "1"}]}]}))
            template = self.write(directory, "t.yaml", "Parameters: {A: {Type: String}}\nResources: {R: {Type: AWS::SQS::Queue}}\n")
            output = directory / "params.json"
            code = preview.main(["params", "--stack-json", str(stack), "--template", str(template), "--set", "A=2", "--output", str(output)])
            self.assertEqual(code, 0)
            raw = output.read_bytes()
            self.assertFalse(raw.startswith(self.BOM.encode("utf-8")), "the tool itself must write without a BOM")
            self.assertEqual(json.loads(raw), [{"ParameterKey": "A", "ParameterValue": "2"}])

    def test_check_change_set_and_cd_template_match_read_a_bom(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            cs = self.write(directory, "cs.json", json.dumps(fixture("harness-adot-off")))
            deployed = self.write(directory, "deployed.yaml", (ROOT / "infra" / "cloudformation" / "phase-14-public-edge.yaml").read_text(encoding="utf-8"))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(preview.main(["check-change-set", "--change-set-json", str(cs), "--profile", "harness-adot-off"]), 0)
                self.assertEqual(preview.main(["cd-template-match", "--deployed", str(deployed)]), 0)

    def test_preview_reads_a_backup_with_a_bom(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            deployed = self.write(Path(temp), "deployed.yaml", (ROOT / "infra" / "cloudformation" / "phase-14-public-edge.yaml").read_text(encoding="utf-8"))
            local = ROOT / "infra" / "cloudformation" / "phase-14-public-edge.yaml"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(preview.main(["preview", "--deployed", str(deployed), "--local", str(local)]), 0)



class BootstrapParametersTests(unittest.TestCase):
    """The parameters of the temporary role stack are derived from the deployed stack, not typed."""

    STACK = {
        "StackName": "LegalDeskPhase14PublicEdge",
        "StackId": "arn:aws:cloudformation:eu-west-1:123456789012:stack/LegalDeskPhase14PublicEdge/2a915a00-bb2c-11f1-a20b-02292202827b",
        "Parameters": [{"ParameterKey": "ApplicationCodeBucket", "ParameterValue": "artifacts-bucket"}],
    }
    RESOURCES = {"StackResources": [
        {"LogicalResourceId": "ApplicationExecutionRole", "PhysicalResourceId": "LegalDeskPhase14PublicEdge-ApplicationExecutionRole-AbC123"},
        {"LogicalResourceId": "ApplicationFunction", "PhysicalResourceId": "LegalDeskPhase14PublicEdge-application"},
        {"LogicalResourceId": "PublicDistribution", "PhysicalResourceId": "EABC123"},
        {"LogicalResourceId": "FrontendOriginAccessControl", "PhysicalResourceId": "EOAC123"},
        {"LogicalResourceId": "EdgeResponseHeadersPolicy", "PhysicalResourceId": "449093f1-2ce6-45e7-a9a1-f97a0d731fdc"},
        {"LogicalResourceId": "PublicApi", "PhysicalResourceId": "abc123"},
        {"LogicalResourceId": "PublicApiIntegration", "PhysicalResourceId": "xyz789"},
    ]}

    def build(self, stage: str = "code-and-quota") -> dict[str, str]:
        return {item["ParameterKey"]: item["ParameterValue"] for item in preview.bootstrap_parameters(self.RESOURCES, self.STACK, stage)}

    def test_every_arn_is_rebuilt_from_a_physical_id_and_matches_the_template_patterns(self) -> None:
        import re

        values = self.build()
        template = preview.load_template((ROOT / "infra" / "cloudformation" / "phase-15-edge-agentchat-bootstrap.yaml").read_text(encoding="utf-8"))
        self.assertEqual(set(values), set(template["Parameters"]))
        for name, spec in template["Parameters"].items():
            if "AllowedPattern" in spec:
                self.assertRegex(values[name], re.compile(spec["AllowedPattern"]), name)
        self.assertEqual(values["ApplicationFunctionArn"], "arn:aws:lambda:eu-west-1:123456789012:function:LegalDeskPhase14PublicEdge-application")
        self.assertEqual(values["PublicApiIntegrationArn"], "arn:aws:apigateway:eu-west-1::/apis/abc123/integrations/xyz789")
        self.assertEqual(values["ArtifactBucketArn"], "arn:aws:s3:::artifacts-bucket")

    def test_the_stage_is_passed_through_and_validated(self) -> None:
        self.assertEqual(self.build("agent-chat")["Stage"], "agent-chat")
        with self.assertRaises(preview.PreviewError):
            self.build("everything")

    def test_a_missing_resource_or_another_stack_is_an_error(self) -> None:
        with self.assertRaises(preview.PreviewError) as error:
            preview.bootstrap_parameters({"StackResources": self.RESOURCES["StackResources"][:-1]}, self.STACK, "code-and-quota")
        self.assertIn("PublicApiIntegration", str(error.exception))
        with self.assertRaises(preview.PreviewError):
            preview.bootstrap_parameters(self.RESOURCES, {**self.STACK, "StackName": "Other"}, "code-and-quota")

    def test_the_command_writes_the_file_without_a_bom_from_files_that_have_one(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            stack = directory / "stack.json"
            stack.write_bytes(("\ufeff" + json.dumps({"Stacks": [self.STACK]})).encode("utf-8"))
            resources = directory / "resources.json"
            resources.write_bytes(("\ufeff" + json.dumps(self.RESOURCES)).encode("utf-8"))
            output = directory / "bootstrap-params.json"
            with contextlib.redirect_stdout(io.StringIO()):
                code = preview.main(["bootstrap-params", "--stack-json", str(stack), "--resources-json", str(resources), "--stage", "agent-chat", "--output", str(output)])
            self.assertEqual(code, 0)
            raw = output.read_bytes()
            self.assertFalse(raw.startswith("\ufeff".encode("utf-8")))
            self.assertEqual(len(json.loads(raw)), 8)


class CodeOnlyCdAfterStep8aTests(unittest.TestCase):
    """What the code-only CD does with the Public Edge stack after 8a (agent chat off): it works, and needs nothing from step 9."""

    def test_the_real_8a_change_set_is_operator_only_and_the_cd_release_that_follows_is_not(self) -> None:
        from scripts.deploy_release import DeploymentError, validate_change_set

        real_8a = fixture("edge-8a-code-and-quota")["Changes"]
        # 8a changes the function environment (quota, agent variables): outside the code-only scope, so the CD must refuse it
        with self.assertRaises(DeploymentError) as error:
            validate_change_set(real_8a, expect_worker=False)
        self.assertEqual(str(error.exception), "change_set_property_violation")
        # the release that follows 8a is Code only on the function plus the dependent integration, and no worker
        release = copy.deepcopy(real_8a)
        function = next(item["ResourceChange"] for item in release if item["ResourceChange"]["LogicalResourceId"] == "ApplicationFunction")
        function["Details"] = [d for d in function["Details"] if d["Target"]["Name"] == "Code"]
        validate_change_set(release, expect_worker=False)

    def test_with_the_flag_off_a_worker_in_the_change_set_is_refused_and_none_is_required(self) -> None:
        from scripts.deploy_release import DeploymentError, validate_change_set

        release = copy.deepcopy(fixture("edge-8a-code-and-quota")["Changes"])
        function = next(item["ResourceChange"] for item in release if item["ResourceChange"]["LogicalResourceId"] == "ApplicationFunction")
        function["Details"] = [d for d in function["Details"] if d["Target"]["Name"] == "Code"]
        validate_change_set(release, expect_worker=False)  # no worker needed while EnableAgentChat is false
        with self.assertRaises(DeploymentError) as error:
            validate_change_set(release, expect_worker=True)
        self.assertEqual(str(error.exception), "change_set_worker_missing")

    def test_the_checked_in_template_equals_itself_so_the_cd_template_gate_opens_after_8a(self) -> None:
        text = (ROOT / "infra" / "cloudformation" / "phase-14-public-edge.yaml").read_text(encoding="utf-8")
        self.assertTrue(preview.cd_template_match(text, text))

    def test_the_cd_iam_template_is_not_needed_before_the_worker_exists(self) -> None:
        # The worker parameters of cd-iam have no default: the stack is updated only at step 9, after 8b. Until then the
        # deployed CD roles are those of Phase 14 and the CD (flag off) touches only the application function.
        template = preview.load_template((ROOT / "infra" / "cloudformation" / "cd-iam.yaml").read_text(encoding="utf-8"))
        for name in ("AgentChatWorkerFunctionArn", "AgentChatWorkerExecutionRoleArn"):
            self.assertNotIn("Default", template["Parameters"][name])


class GeneratedNameCommandTests(unittest.TestCase):
    """The command the runbook calls for the exact simulation names: same rule as the tests, checked against the two real roles."""

    def run_command(self, *arguments: str) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(preview.main(["generated-name", *arguments]), 0)
        return out.getvalue().strip()

    def test_the_command_prints_the_cut_name_with_a_placeholder_suffix(self) -> None:
        self.assertEqual(self.run_command("--stack", "LegalDeskPhase14PublicEdge", "--logical-id", "AgentChatWorkerExecutionRole"), "LegalDeskPhase14PublicEdg-AgentChatWorkerExecutionR-XXXXXXXXXXXX")
        self.assertEqual(self.run_command("--stack", "LegalDeskPhase14PublicEdge", "--logical-id", "ApplicationExecutionRole"), "LegalDeskPhase14PublicEdge-ApplicationExecutionRole-XXXXXXXXXXXX")

    def test_the_limit_is_that_of_the_resource_type(self) -> None:
        self.assertEqual(self.run_command("--stack", "LegalDeskPhase14PublicEdge", "--logical-id", "AgentChatWorkerErrorsAlarm", "--limit", "255"),
                         "LegalDeskPhase14PublicEdge-AgentChatWorkerErrorsAlarm-XXXXXXXXXXXX")

    def test_the_rule_reproduces_the_two_roles_observed_on_2026_11_03(self) -> None:
        self.assertEqual(preview.cfn_generated_name("LegalDeskPhase14PublicEdge", "ApplicationExecutionRole", "X3hgVtmlHWXr"), "LegalDeskPhase14PublicEdge-ApplicationExecutionRole-X3hgVtmlHWXr")
        self.assertEqual(preview.cfn_generated_name("LegalDeskPhase14PublicEdge", "AgentChatWorkerExecutionRole", "bzFuZq7zur6f"), "LegalDeskPhase14PublicEdg-AgentChatWorkerExecutionR-bzFuZq7zur6f")


if __name__ == "__main__":
    unittest.main()
