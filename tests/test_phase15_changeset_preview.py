"""Offline change-set preview and gate of the T9 rollout (no AWS, no network)."""

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
TEMPLATES = ROOT / "infra" / "cloudformation"
SYNTHETIC = """
Parameters:
  Flag: {Type: String, Default: "false"}
  Name: {Type: String, Default: queue-a}
  Secret: {Type: String, NoEcho: true}
Conditions:
  On: !Equals [!Ref Flag, "true"]
Resources:
  Queue:
    Type: AWS::SQS::Queue
    Properties:
      QueueName: !Ref Name
      VisibilityTimeout: 30
  Extra:
    Type: AWS::SQS::Queue
    Condition: On
    Properties: {QueueName: extra}
  Role:
    Type: AWS::IAM::Role
    Properties:
      AssumeRolePolicyDocument: {Statement: []}
      Policies:
        - PolicyName: P
          PolicyDocument:
            Statement:
              - Sid: Read
                Effect: Allow
                Action: [s3:GetObject]
                Resource: arn:aws:s3:::bucket/key
              - !If
                - On
                - {Sid: Write, Effect: Allow, Action: [s3:PutObject], Resource: arn:aws:s3:::bucket/key}
                - !Ref AWS::NoValue
  Fn:
    Type: AWS::Lambda::Function
    Properties:
      Environment:
        Variables:
          SECRET: !Ref Secret
          FLAG: !Ref Flag
"""


def _template(text: str = SYNTHETIC) -> dict[str, Any]:
    return dict(preview.load_template(text))


def _compare(deployed_params: dict[str, str], local_params: dict[str, str], text: str = SYNTHETIC, local_text: str | None = None) -> dict[str, Any]:
    return preview.compare(_template(text), _template(local_text or text), deployed_params, local_params)


def _real(name: str) -> dict[str, Any]:
    return dict(preview.load_template((TEMPLATES / name).read_text(encoding="utf-8")))


def _flip(name: str, deployed: dict[str, str], local: dict[str, str]) -> dict[str, Any]:
    template = _real(name)
    return preview.compare(template, template, deployed, local)


class EngineTests(unittest.TestCase):
    def test_a_flag_flip_adds_the_conditional_resource_and_modifies_what_reads_the_flag(self) -> None:
        result = _compare({"Flag": "false"}, {"Flag": "true"})
        self.assertEqual([item["id"] for item in result["added"]], ["Extra"])
        self.assertEqual({item["id"]: item["properties"] for item in result["modified"]}, {"Fn": ["Environment"], "Role": ["Policies"]})
        self.assertEqual([(c["sid"], c["change"]) for c in result["iam"]], [("Write", "added")])
        self.assertEqual(result["removed"], [])

    def test_no_change_means_an_empty_preview(self) -> None:
        result = _compare({"Flag": "true"}, {"Flag": "true"})
        self.assertEqual((result["added"], result["modified"], result["removed"], result["iam"]), ([], [], [], []))

    def test_turning_the_flag_off_removes_the_resource(self) -> None:
        result = _compare({"Flag": "true"}, {"Flag": "false"})
        self.assertEqual([item["id"] for item in result["removed"]], ["Extra"])

    def test_an_unsupplied_noecho_parameter_stays_symbolic_and_equal_on_both_sides(self) -> None:
        result = _compare({"Flag": "false"}, {"Flag": "false"})
        self.assertEqual(result["modified"], [])

    def test_changing_an_immutable_property_is_reported_as_a_replacement(self) -> None:
        result = _compare({"Name": "queue-a"}, {"Name": "queue-b"})
        queue = next(item for item in result["modified"] if item["id"] == "Queue")
        self.assertEqual((queue["properties"], queue["replacement"]), (["QueueName"], "REPLACEMENT"))

    def test_a_mutable_property_change_is_not_a_replacement_and_an_unknown_type_is_never_called_safe(self) -> None:
        mutated = SYNTHETIC.replace("VisibilityTimeout: 30", "VisibilityTimeout: 60")
        self.assertEqual(next(i for i in _compare({}, {}, local_text=mutated)["modified"] if i["id"] == "Queue")["replacement"], "no")
        unknown = "Resources:\n  X: {Type: AWS::Made::Up, Properties: {A: 1}}\n"
        changed = "Resources:\n  X: {Type: AWS::Made::Up, Properties: {A: 2}}\n"
        self.assertEqual(_compare({}, {}, text=unknown, local_text=changed)["modified"][0]["replacement"], "UNKNOWN")

    def test_a_wildcard_in_an_added_statement_is_flagged(self) -> None:
        wide = SYNTHETIC.replace("Action: [s3:PutObject], Resource: arn:aws:s3:::bucket/key}", "Action: ['s3:*'], Resource: '*'}")
        result = _compare({"Flag": "false"}, {"Flag": "true"}, text=SYNTHETIC, local_text=wide)
        change = next(c for c in result["iam"] if c["sid"] == "Write")
        self.assertTrue(change["wide"])
        self.assertEqual(change["wideReasons"], ["action_wildcard", "resource_wildcard"])

    def test_every_kind_of_widening_is_reported_with_its_reason(self) -> None:
        exact = {"Effect": "Allow", "Action": ["s3:GetObject"], "Resource": "arn:aws:s3:::bucket/key"}
        cases = {
            "NotAction": ({**exact, "Action": None, "NotAction": ["iam:*"]}, ["NotAction"]),
            "NotResource": ({**exact, "NotResource": "arn:aws:s3:::bucket/key"}, ["NotResource"]),
            "action wildcard": ({**exact, "Action": ["s3:Get*"]}, ["action_wildcard"]),
            "bare resource": ({**exact, "Resource": "*"}, ["resource_wildcard"]),
            "bare resource in list": ({**exact, "Resource": ["arn:aws:s3:::bucket/key", "*"]}, ["resource_wildcard"]),
            "arn wildcard": ({**exact, "Resource": "arn:aws:logs:eu-west-1:123456789012:log-group:x:*"}, ["resource_partial_wildcard"]),
            "path wildcard": ({**exact, "Resource": "arn:aws:s3:::bucket/*"}, ["resource_partial_wildcard"]),
            "wildcard inside Fn::Sub": ({**exact, "Resource": {"Fn::Sub": "${Bucket}/tenants/*/original.pdf"}}, ["resource_partial_wildcard"]),
            "wildcard inside Fn::Join": ({**exact, "Resource": {"Fn::Join": ["", ["arn:aws:s3:::b/", "*"]]}}, ["resource_partial_wildcard"]),
        }
        for label, (statement, expected) in cases.items():
            with self.subTest(label):
                self.assertEqual(preview.wide_reasons({key: value for key, value in statement.items() if value is not None}), expected)
        self.assertEqual(preview.wide_reasons(exact), [])
        self.assertEqual(preview.wide_reasons({**exact, "Resource": {"Fn::GetAtt": ["Queue", "Arn"]}}), [])

    def test_not_action_is_detected_in_a_real_template_diff(self) -> None:
        widened = SYNTHETIC.replace("Action: [s3:PutObject], Resource: arn:aws:s3:::bucket/key}", "NotAction: ['iam:*'], Resource: arn:aws:s3:::bucket/key}")
        result = _compare({"Flag": "false"}, {"Flag": "true"}, text=SYNTHETIC, local_text=widened)
        self.assertEqual(next(c for c in result["iam"] if c["sid"] == "Write")["wideReasons"], ["NotAction"])

    def test_a_condition_that_cannot_be_resolved_is_reported_not_guessed(self) -> None:
        text = SYNTHETIC.replace('Flag: {Type: String, Default: "false"}', "Flag: {Type: String, NoEcho: true}")
        self.assertEqual(_compare({}, {}, text=text)["conditionUnknown"], ["Extra"])

    def test_get_template_response_envelope_is_accepted(self) -> None:
        envelope = json.dumps({"TemplateBody": {"Resources": {"A": {"Type": "AWS::SQS::Queue"}}}})
        self.assertIn("A", preview.load_template(envelope)["Resources"])

    def test_the_tool_never_imports_boto3_or_opens_a_network_connection(self) -> None:
        source = (ROOT / "scripts" / "phase15_changeset_preview.py").read_text(encoding="utf-8")
        for forbidden in ("import boto3", "from boto3", "urllib", "requests", "socket"):
            self.assertNotIn(forbidden, source)


class ProfilesAgainstTheRealTemplatesTests(unittest.TestCase):
    """Each rollout step, rehearsed by flipping its parameters on the checked-in templates."""

    def assert_clean(self, result: dict[str, Any], profile: str) -> None:
        self.assertEqual(preview.check_preview(result, preview.load_profile(profile)), [])
        self.assertTrue(all(item["replacement"] in {"no", "UNKNOWN"} for item in result["modified"]))

    def test_public_edge_enabling_the_agent_chat(self) -> None:
        result = _flip("phase-14-public-edge.yaml", {"EnableAgentChat": "false"}, {"EnableAgentChat": "true"})
        self.assertEqual(len(result["added"]), 17)
        self.assert_clean(result, "public-edge-agent-chat")

    def test_public_edge_rollback(self) -> None:
        result = _flip("phase-14-public-edge.yaml", {"EnableAgentChat": "true"}, {"EnableAgentChat": "false"})
        self.assertEqual(len(result["removed"]), 17)
        self.assert_clean(result, "public-edge-agent-chat-rollback")

    def test_gateway_enabling_search_documents(self) -> None:
        result = _flip("phase-08-gateway-mcp.yaml", {"EnableSearchDocuments": "false"}, {"EnableSearchDocuments": "true"})
        self.assertEqual({item["id"] for item in result["added"]}, {"SearchDocumentsExecutionRole", "SearchDocumentsFunction", "SearchDocumentsGatewayTarget", "SearchDocumentsLogGroup"})
        self.assert_clean(result, "gateway-search-documents")

    def test_harness_memory_off_and_adot_on_then_adot_off(self) -> None:
        base = {"EnablePhase08Gateway": "true"}
        step_one = _flip("phase-01-harness.yaml", {**base, "EnablePhase09Memory": "true", "EnableManagedAdot": "false"}, {**base, "EnablePhase09Memory": "false", "EnableManagedAdot": "true"})
        self.assert_clean(step_one, "harness-memory-off-adot-on")
        harness = next(item for item in step_one["modified"] if item["id"] == "MinimalHarness")
        self.assertEqual(harness["properties"], ["EnvironmentVariables", "Memory"])
        step_two = _flip("phase-01-harness.yaml", {**base, "EnableManagedAdot": "true"}, {**base, "EnableManagedAdot": "false"})
        self.assert_clean(step_two, "harness-adot-off")

    def test_harness_without_attached_memory_is_still_within_the_profile(self) -> None:
        base = {"EnablePhase08Gateway": "true", "EnablePhase09Memory": "false"}
        result = _flip("phase-01-harness.yaml", {**base, "EnableManagedAdot": "false"}, {**base, "EnableManagedAdot": "true"})
        self.assertEqual(preview.check_preview(result, preview.load_profile("harness-memory-off-adot-on")), [])

    def test_the_new_roles_never_use_not_statements_or_bare_wildcards(self) -> None:
        # Path-scoped resource wildcards are the only widening these roles use, and the profiles name each one.
        for name, flip in (("phase-08-gateway-mcp.yaml", ("EnableSearchDocuments", "SearchDocumentsExecutionRole")), ("phase-14-public-edge.yaml", ("EnableAgentChat", "AgentChatWorkerExecutionRole"))):
            result = _flip(name, {flip[0]: "false"}, {flip[0]: "true"})
            reasons = {reason for c in result["iam"] if c["resource"] == flip[1] for reason in c["wideReasons"]}
            self.assertLessEqual(reasons, {"resource_partial_wildcard"}, name)

    def test_every_widening_in_the_profiles_is_listed_with_its_exact_reasons(self) -> None:
        for profile_name, template_name, deployed, local in (
            ("gateway-search-documents", "phase-08-gateway-mcp.yaml", {"EnableSearchDocuments": "false"}, {"EnableSearchDocuments": "true"}),
            ("public-edge-agent-chat", "phase-14-public-edge.yaml", {"EnableAgentChat": "false"}, {"EnableAgentChat": "true"}),
            ("public-edge-agent-chat-rollback", "phase-14-public-edge.yaml", {"EnableAgentChat": "true"}, {"EnableAgentChat": "false"}),
        ):
            result = _flip(template_name, deployed, local)
            seen = {(c["resource"], c["sid"]): set(c["wideReasons"]) for c in result["iam"] if c["wide"]}
            allowed = {(e["resource"], e["sid"]): set(e["reasons"]) for e in preview.load_profile(profile_name)["wideAllowed"]}
            self.assertEqual(seen, allowed, profile_name)

    def test_profile_ids_exist_in_their_templates(self) -> None:
        mapping = {
            "public-edge-agent-chat": "phase-14-public-edge.yaml",
            "gateway-search-documents": "phase-08-gateway-mcp.yaml",
            "harness-memory-off-adot-on": "phase-01-harness.yaml",
            "harness-adot-off": "phase-01-harness.yaml",
            "cd-iam": "cd-iam.yaml",
        }
        for profile_name, template_name in mapping.items():
            resources = set(_real(template_name)["Resources"])
            profile = preview.load_profile(profile_name)
            for logical_id in [*profile["add"], *profile["modify"], *profile["iamResources"]]:
                self.assertIn(logical_id, resources, f"{profile_name}: {logical_id}")

    def test_cd_iam_profile_is_exactly_the_four_worker_statements(self) -> None:
        profile = preview.load_profile("cd-iam")
        self.assertEqual(
            sorted(entry["sid"] for entry in profile["iam"]),
            ["PassUnchangedAgentChatWorkerRoleIfRequired", "ReadAndUpdateOnlyAgentChatWorkerCode", "ReadUnchangedAgentChatWorkerRolePolicy", "VerifyOnlyTheAgentChatWorker"],
        )
        self.assertTrue(all(entry["change"] == "added" for entry in profile["iam"]))


class ProfileViolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = preview.load_profile("public-edge-agent-chat")
        self.clean = _flip("phase-14-public-edge.yaml", {"EnableAgentChat": "false"}, {"EnableAgentChat": "true"})

    def test_an_unexpected_resource_replacement_removal_and_iam_change_each_fail(self) -> None:
        for mutate, expected in (
            (lambda p: p["added"].append({"id": "Surprise", "type": "AWS::S3::Bucket"}), "unexpected addition: Surprise"),
            (lambda p: p["removed"].append({"id": "FrontendBucket", "type": "AWS::S3::Bucket"}), "removal outside the profile: FrontendBucket"),
            (lambda p: p["modified"].append({"id": "FrontendBucket", "type": "AWS::S3::Bucket", "properties": ["BucketName"], "replacement": "UNKNOWN"}), "unexpected modification: FrontendBucket"),
            (lambda p: p["modified"][0].update(replacement="REPLACEMENT"), "replacement risk (REPLACEMENT)"),
            (lambda p: p["iam"].append({"resource": "ApplicationExecutionRole", "policy": "P", "sid": "Extra", "change": "added", "wide": False, "wideReasons": []}), "unexpected IAM statement change"),
            (lambda p: p["iam"].append({"resource": "ApplicationExecutionRole", "policy": "P", "sid": "Wide", "change": "added", "wide": True, "wideReasons": ["resource_wildcard"]}), "widening not allowed by the profile in ApplicationExecutionRole / Wide: resource_wildcard"),
            (lambda p: p["iam"].append({"resource": "ApplicationExecutionRole", "policy": "P", "sid": "NotAct", "change": "added", "wide": True, "wideReasons": ["NotAction"]}), "NotAction"),
        ):
            with self.subTest(expected=expected):
                mutated = copy.deepcopy(self.clean)
                mutate(mutated)
                self.assertTrue(any(expected in problem for problem in preview.check_preview(mutated, self.profile)), expected)

    def test_a_listed_widening_cannot_silently_grow_into_a_bigger_one(self) -> None:
        mutated = copy.deepcopy(self.clean)
        change = next(c for c in mutated["iam"] if c["sid"] == "HeadCanonicalSourceDocumentsOnly")
        self.assertEqual(change["wideReasons"], ["resource_partial_wildcard"])  # allowed, by name
        change["wideReasons"] = ["resource_partial_wildcard", "resource_wildcard", "NotAction"]
        problems = preview.check_preview(mutated, self.profile)
        self.assertTrue(any("HeadCanonicalSourceDocumentsOnly" in p and "NotAction" in p and "resource_wildcard" in p for p in problems), problems)
        self.assertFalse(any("resource_partial_wildcard" in p for p in problems))

    def test_a_missing_expected_addition_or_property_set_is_not_silently_accepted(self) -> None:
        mutated = copy.deepcopy(self.clean)
        mutated["added"] = mutated["added"][1:]
        self.assertTrue(any(problem.startswith("expected addition missing") for problem in preview.check_preview(mutated, self.profile)))
        mutated = copy.deepcopy(self.clean)
        next(item for item in mutated["modified"] if item["id"] == "ApplicationFunction")["properties"].append("Role")
        self.assertTrue(any("unexpected properties of ApplicationFunction: Role" in problem for problem in preview.check_preview(mutated, self.profile)))


def _described(profile: dict[str, Any]) -> dict[str, Any]:
    changes: list[dict[str, Any]] = []
    for logical_id in profile["add"]:
        changes.append({"ResourceChange": {"Action": "Add", "LogicalResourceId": logical_id, "ResourceType": "AWS::Made::Up"}})
    for logical_id, names in profile["modify"].items():
        changes.append({"ResourceChange": {"Action": "Modify", "LogicalResourceId": logical_id, "ResourceType": "AWS::Made::Up", "Replacement": "False", "Details": [{"Target": {"Name": name}} for name in names]}})
    for logical_id in profile.get("remove", []):
        changes.append({"ResourceChange": {"Action": "Remove", "LogicalResourceId": logical_id, "ResourceType": "AWS::Made::Up"}})
    return {"Status": "CREATE_COMPLETE", "Changes": changes}


class RealChangeSetGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = preview.load_profile("gateway-search-documents")
        self.described = _described(self.profile)

    def test_exactly_the_profile_passes(self) -> None:
        self.assertEqual(preview.check_change_set(self.described, self.profile), [])

    def test_replacement_conditional_or_true_fails(self) -> None:
        for value in ("True", "Conditional"):
            described = copy.deepcopy(self.described)
            next(c for c in described["Changes"] if c["ResourceChange"]["Action"] == "Modify")["ResourceChange"]["Replacement"] = value
            self.assertTrue(any("replacement risk" in problem for problem in preview.check_change_set(described, self.profile)), value)

    def test_removal_extra_resource_and_extra_property_fail(self) -> None:
        extra_resource = copy.deepcopy(self.described)
        extra_resource["Changes"].append({"ResourceChange": {"Action": "Modify", "LogicalResourceId": "LegalDeskGateway", "ResourceType": "AWS::BedrockAgentCore::Gateway", "Replacement": "False", "Details": []}})
        removal = copy.deepcopy(self.described)
        removal["Changes"].append({"ResourceChange": {"Action": "Remove", "LogicalResourceId": "ReviewTaskGatewayTarget", "ResourceType": "AWS::BedrockAgentCore::GatewayTarget"}})
        extra_property = copy.deepcopy(self.described)
        function = next(c for c in extra_property["Changes"] if c["ResourceChange"]["LogicalResourceId"] == "MetadataMcpFunction")
        function["ResourceChange"]["Details"].append({"Target": {"Name": "Role"}})
        self.assertTrue(any("unexpected modification: LegalDeskGateway" in p for p in preview.check_change_set(extra_resource, self.profile)))
        self.assertTrue(any("removal outside the profile" in p for p in preview.check_change_set(removal, self.profile)))
        self.assertTrue(any("unexpected properties of MetadataMcpFunction: Role" in p for p in preview.check_change_set(extra_property, self.profile)))

    def test_an_iam_resource_outside_the_profile_fails(self) -> None:
        described = copy.deepcopy(self.described)
        described["Changes"].append({"ResourceChange": {"Action": "Modify", "LogicalResourceId": "MetadataMcpExecutionRole", "ResourceType": "AWS::IAM::Role", "Replacement": "False", "Details": [{"Target": {"Name": "Policies"}}]}})
        problems = preview.check_change_set(described, self.profile)
        self.assertTrue(any("IAM resource changed outside the profile: MetadataMcpExecutionRole" in p for p in problems))

    def test_a_change_set_that_is_not_ready_or_malformed_is_unusable_not_green(self) -> None:
        with self.assertRaises(preview.PreviewError):
            preview.check_change_set({"Status": "FAILED", "Changes": []}, self.profile)
        with self.assertRaises(preview.PreviewError):
            preview.check_change_set({"Changes": "nope"}, self.profile)
        with self.assertRaises(preview.PreviewError):
            preview.check_change_set({"Changes": [{"ResourceChange": {"Action": "Import", "LogicalResourceId": "X", "ResourceType": "AWS::X::Y"}}]}, self.profile)

    def test_a_dependency_reevaluation_without_a_named_property_is_surfaced_for_review(self) -> None:
        described = copy.deepcopy(self.described)
        next(c for c in described["Changes"] if c["ResourceChange"]["LogicalResourceId"] == "GatewayExecutionRole")["ResourceChange"]["Details"] = []
        self.assertTrue(any("without a named property" in p for p in preview.check_change_set(described, self.profile)))

    def test_unknown_profile_is_an_error(self) -> None:
        with self.assertRaises(preview.PreviewError):
            preview.load_profile("does-not-exist")


class ParameterBuilderTests(unittest.TestCase):
    TEMPLATE = _template("""
Parameters:
  A: {Type: String}
  B: {Type: String, Default: x}
  Secret: {Type: String, NoEcho: true}
  New: {Type: String, Default: ''}
  Needed: {Type: String}
Resources:
  R: {Type: AWS::SQS::Queue}
""")
    STACK = {"Parameters": [{"ParameterKey": "A", "ParameterValue": "1"}, {"ParameterKey": "B", "ParameterValue": "2"}, {"ParameterKey": "Secret", "ParameterValue": "****"}, {"ParameterKey": "Needed", "ParameterValue": "n"}]}

    def test_deltas_change_everything_else_keeps_the_previous_value_and_defaults_are_omitted(self) -> None:
        built = {item["ParameterKey"]: item for item in preview.build_parameters(self.TEMPLATE, self.STACK, {"A": "9"})}
        self.assertEqual(built["A"], {"ParameterKey": "A", "ParameterValue": "9"})
        self.assertEqual(built["Secret"], {"ParameterKey": "Secret", "UsePreviousValue": True})
        self.assertEqual(built["B"], {"ParameterKey": "B", "UsePreviousValue": True})
        self.assertNotIn("New", built, "a new parameter with a default is left to the template")

    def test_a_masked_noecho_value_never_reaches_the_file(self) -> None:
        self.assertNotIn("****", json.dumps(preview.build_parameters(self.TEMPLATE, self.STACK, {})))

    def test_unknown_override_and_unsupplied_new_parameter_are_errors(self) -> None:
        with self.assertRaises(preview.PreviewError):
            preview.build_parameters(self.TEMPLATE, self.STACK, {"Typo": "1"})
        stack = {"Parameters": [item for item in self.STACK["Parameters"] if item["ParameterKey"] != "Needed"]}
        with self.assertRaises(preview.PreviewError) as error:
            preview.build_parameters(self.TEMPLATE, stack, {})
        self.assertIn("Needed", str(error.exception))

    def test_command_writes_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            stack_json = Path(temp) / "stack.json"
            stack_json.write_text(json.dumps({"Stacks": [{"StackName": "S", **self.STACK}]}), encoding="utf-8")
            template = Path(temp) / "t.yaml"
            template.write_text("Parameters: {A: {Type: String}}" + chr(10) + "Resources: {R: {Type: AWS::SQS::Queue}}", encoding="utf-8")
            output = Path(temp) / "params.json"
            code = preview.main(["params", "--stack-json", str(stack_json), "--template", str(template), "--set", "A=7", "--output", str(output)])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.read_text()), [{"ParameterKey": "A", "ParameterValue": "7"}])


class CommandLineTests(unittest.TestCase):
    def _run(self, argv: list[str]) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = preview.main(argv)
        return code, out.getvalue()

    def test_preview_exit_codes_follow_the_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            deployed = Path(temp) / "deployed.yaml"
            deployed.write_text((TEMPLATES / "phase-14-public-edge.yaml").read_text(encoding="utf-8"), encoding="utf-8")
            local = str(TEMPLATES / "phase-14-public-edge.yaml")
            args = ["preview", "--deployed", str(deployed), "--local", local, "--deployed-param", "EnableAgentChat=false", "--param", "EnableAgentChat=true"]
            code, output = self._run([*args, "--profile", "public-edge-agent-chat"])
            self.assertEqual(code, 0, output)
            self.assertIn("PROFILE public-edge-agent-chat: OK", output)
            code, output = self._run([*args, "--profile", "gateway-search-documents"])
            self.assertEqual(code, 1)
            self.assertIn("VIOLATIONS", output)

    def test_check_change_set_command(self) -> None:
        profile = preview.load_profile("harness-adot-off")
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "cs.json"
            path.write_text(json.dumps(_described(profile)), encoding="utf-8")
            self.assertEqual(self._run(["check-change-set", "--change-set-json", str(path), "--profile", "harness-adot-off"])[0], 0)
            self.assertEqual(self._run(["check-change-set", "--change-set-json", str(path), "--profile", "gateway-search-documents"])[0], 1)
            self.assertEqual(self._run(["check-change-set", "--change-set-json", str(path), "--profile", "nope"])[0], 2)

    def test_cd_template_match_uses_the_cd_rule(self) -> None:
        text = (TEMPLATES / "phase-14-public-edge.yaml").read_text(encoding="utf-8")
        self.assertTrue(preview.cd_template_match(text, text))
        # A template from before the agent chat differs, so the next promotion would stop.
        self.assertFalse(preview.cd_template_match(text.replace("EnableAgentChat", "EnableAgentChatOld"), text))
        with tempfile.TemporaryDirectory() as temp:
            deployed = Path(temp) / "deployed.yaml"
            deployed.write_text(text, encoding="utf-8")
            local = str(TEMPLATES / "phase-14-public-edge.yaml")
            self.assertEqual(self._run(["cd-template-match", "--deployed", str(deployed), "--local", local])[0], 0)
            deployed.write_text(text.replace("EnableAgentChat", "EnableAgentChatOld"), encoding="utf-8")
            self.assertEqual(self._run(["cd-template-match", "--deployed", str(deployed), "--local", local])[0], 1)

    def test_unreadable_input_exits_2(self) -> None:
        self.assertEqual(self._run(["preview", "--deployed", "missing.yaml", "--local", str(TEMPLATES / "cd-iam.yaml")])[0], 2)


if __name__ == "__main__":
    unittest.main()
