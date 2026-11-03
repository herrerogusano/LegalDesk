"""The temporary CloudFormation role of the Phase 15 Public Edge change sets: least privilege, by stage, against the template."""

from __future__ import annotations

import fnmatch
import re
import unittest
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).parents[1]
BOOTSTRAP = ROOT / "infra" / "cloudformation" / "phase-15-edge-agentchat-bootstrap.yaml"
EDGE = ROOT / "infra" / "cloudformation" / "phase-14-public-edge.yaml"
ACCOUNT, REGION, STACK = "123456789012", "eu-west-1", "LegalDeskPhase14PublicEdge"
PARAMETERS = {
    "ExistingApplicationRoleArn": f"arn:aws:iam::{ACCOUNT}:role/{STACK}-ApplicationExecutionRole-AbC123",
    "ApplicationFunctionArn": f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{STACK}-application",
    "CloudFrontDistributionArn": f"arn:aws:cloudfront::{ACCOUNT}:distribution/EABC123",
    "OriginAccessControlArn": f"arn:aws:cloudfront::{ACCOUNT}:origin-access-control/EOAC123",
    "ResponseHeadersPolicyArn": f"arn:aws:cloudfront::{ACCOUNT}:response-headers-policy/449093f1-2ce6-45e7-a9a1-f97a0d731fdc",
    "PublicApiIntegrationArn": f"arn:aws:apigateway:{REGION}::/apis/abc123/integrations/xyz789",
    "ArtifactBucketArn": "arn:aws:s3:::artifacts-bucket",
}
PSEUDO = {"AWS::Partition": "aws", "AWS::Region": REGION, "AWS::AccountId": ACCOUNT, "AWS::StackName": STACK}


def _loader() -> type:
    class Loader(yaml.SafeLoader):
        pass

    def construct(loader: yaml.SafeLoader, suffix: str, node: yaml.Node) -> Any:
        if isinstance(node, yaml.ScalarNode):
            value: Any = loader.construct_scalar(node)
        elif isinstance(node, yaml.SequenceNode):
            value = loader.construct_sequence(node, deep=True)
        else:
            value = loader.construct_mapping(node, deep=True)
        return {"Ref": value} if suffix == "Ref" else {"Fn::" + suffix: value}

    Loader.add_multi_constructor("!", construct)
    return Loader


def load(path: Path) -> dict[str, Any]:
    return yaml.load(path.read_text(encoding="utf-8"), Loader=_loader())


def render(node: Any, stage: str) -> Any:
    """Resolve Ref/Sub/If of the bootstrap template for a stage; NoValue entries disappear."""

    if isinstance(node, dict):
        if set(node) == {"Ref"}:
            name = node["Ref"]
            if name == "AWS::NoValue":
                return None
            if name == "Stage":
                return stage
            return PSEUDO.get(name, PARAMETERS.get(name, name))
        if set(node) == {"Fn::Sub"}:
            return re.sub(r"\$\{([^}]+)\}", lambda m: str(PSEUDO.get(m.group(1), PARAMETERS.get(m.group(1), m.group(1)))), node["Fn::Sub"])
        if set(node) == {"Fn::If"}:
            condition, yes, no = node["Fn::If"]
            assert condition == "IncludeAgentChat"
            return render(yes if stage == "agent-chat" else no, stage)
        return {key: render(value, stage) for key, value in node.items()}
    if isinstance(node, list):
        return [item for item in (render(item, stage) for item in node) if item is not None]
    return node


def statements(stage: str) -> list[dict[str, Any]]:
    role = load(BOOTSTRAP)["Resources"]["EdgeAgentChatCloudFormationRole"]["Properties"]
    return render(role["Policies"][0]["PolicyDocument"]["Statement"], stage)


def actions(statement: dict[str, Any]) -> list[str]:
    value = statement["Action"]
    return value if isinstance(value, list) else [value]


def resources(statement: dict[str, Any]) -> list[str]:
    value = statement["Resource"]
    return value if isinstance(value, list) else [value]


def allowed_actions(stage: str) -> set[str]:
    return {action for statement in statements(stage) for action in actions(statement)}


class TrustAndShapeTests(unittest.TestCase):
    def test_only_cloudformation_can_assume_the_role_and_it_is_deleted_with_the_stack(self) -> None:
        resource = load(BOOTSTRAP)["Resources"]["EdgeAgentChatCloudFormationRole"]
        trust = resource["Properties"]["AssumeRolePolicyDocument"]["Statement"]
        self.assertEqual(len(trust), 1)
        self.assertEqual((trust[0]["Principal"], trust[0]["Action"], trust[0]["Effect"]), ({"Service": "cloudformation.amazonaws.com"}, "sts:AssumeRole", "Allow"))
        self.assertEqual((resource["DeletionPolicy"], resource["UpdateReplacePolicy"]), ("Delete", "Delete"))
        self.assertEqual(set(load(BOOTSTRAP)["Resources"]), {"EdgeAgentChatCloudFormationRole"})

    def test_the_stage_parameter_has_exactly_the_two_documented_values_and_defaults_to_the_narrow_one(self) -> None:
        stage = load(BOOTSTRAP)["Parameters"]["Stage"]
        self.assertEqual((stage["AllowedValues"], stage["Default"]), (["code-and-quota", "agent-chat"], "code-and-quota"))

    def test_no_parameter_has_a_default_ARN_and_every_one_is_pattern_checked(self) -> None:
        for name, spec in load(BOOTSTRAP)["Parameters"].items():
            if name == "Stage":
                continue
            self.assertNotIn("Default", spec, name)
            self.assertTrue(re.fullmatch(spec["AllowedPattern"], PARAMETERS[name]), name)
        self.assertIsNone(re.fullmatch(load(BOOTSTRAP)["Parameters"]["ApplicationFunctionArn"]["AllowedPattern"], f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{STACK}-agent-chat-worker"))


class LeastPrivilegeTests(unittest.TestCase):
    def test_no_stage_has_a_wildcard_action_or_dangerous_iam_action(self) -> None:
        forbidden = {
            "iam:CreateUser", "iam:CreateAccessKey", "iam:AttachRolePolicy", "iam:DetachRolePolicy", "iam:UpdateAssumeRolePolicy", "iam:CreatePolicy",
            "iam:PutRolePermissionsBoundary", "iam:DeleteRolePermissionsBoundary", "iam:CreateServiceLinkedRole", "sts:AssumeRole",
            "s3:PutObject", "s3:DeleteObject", "dynamodb:PutItem", "lambda:InvokeFunction", "bedrock:InvokeModel", "lambda:AddPermission",
        }
        for stage in ("code-and-quota", "agent-chat"):
            for statement in statements(stage):
                self.assertEqual(statement["Effect"], "Allow")
                for action in actions(statement):
                    self.assertFalse(action.endswith("*"), f"{stage}: {action}")
                    self.assertNotIn(action, forbidden, f"{stage}: {action}")
                self.assertNotIn("NotAction", statement)
                self.assertNotIn("NotResource", statement)

    def test_the_only_bare_star_resources_are_regional_read_only_listings(self) -> None:
        for stage in ("code-and-quota", "agent-chat"):
            for statement in statements(stage):
                if "*" in resources(statement):
                    self.assertEqual(statement["Condition"], {"StringEquals": {"aws:RequestedRegion": REGION}}, statement["Sid"])
                    self.assertTrue(all(a.split(":")[1].startswith(("Describe", "List")) for a in actions(statement)), statement["Sid"])

    def test_pass_role_is_always_limited_to_lambda_and_to_one_role_pattern(self) -> None:
        for stage in ("code-and-quota", "agent-chat"):
            for statement in statements(stage):
                if "iam:PassRole" in actions(statement):
                    self.assertEqual(statement["Condition"], {"StringEquals": {"iam:PassedToService": "lambda.amazonaws.com"}})
                    self.assertEqual(len(resources(statement)), 1)
                    self.assertIn(":role/LegalDeskPhase14PublicEdge-", resources(statement)[0])

    def test_the_only_write_on_an_existing_role_is_the_inline_policy_of_the_application_role_in_stage_two(self) -> None:
        application_role = PARAMETERS["ExistingApplicationRoleArn"]
        for stage in ("code-and-quota", "agent-chat"):
            writes = [(s["Sid"], a) for s in statements(stage) if application_role in resources(s) for a in actions(s) if a.startswith("iam:") and a not in {"iam:GetRole", "iam:ListRolePolicies", "iam:ListAttachedRolePolicies", "iam:GetRolePolicy", "iam:PassRole"}]
            self.assertEqual(writes, [] if stage == "code-and-quota" else [("UpdateOnlyTheInlinePolicyOfTheApplicationRole", "iam:PutRolePolicy")])

    def test_stage_one_creates_and_deletes_nothing(self) -> None:
        verbs = ("Create", "Delete", "Put", "Set", "Tag", "Untag")
        for action in allowed_actions("code-and-quota"):
            service, name = action.split(":")
            if service == "lambda":
                self.assertTrue(name.startswith(("Get", "List", "Update")) and name != "UpdateEventSourceMapping", action)
            else:
                self.assertFalse(name.startswith(verbs), action)

    def test_stage_one_is_the_code_only_role_plus_the_function_configuration(self) -> None:
        cd = load(ROOT / "infra" / "cloudformation" / "cd-iam.yaml")["Resources"]["CloudFormationExecutionRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
        cd_actions = {a for s in cd for a in (s["Action"] if isinstance(s["Action"], list) else [s["Action"]])}
        stage_one = allowed_actions("code-and-quota")
        self.assertEqual(
            stage_one - cd_actions,
            {"lambda:ListTags", "lambda:UpdateFunctionConfiguration", "logs:DescribeLogGroups", "cloudfront:GetOriginAccessControl", "cloudfront:GetResponseHeadersPolicy"},
        )
        # nothing the CD role can do on this stack is lost: the same change set stays executable
        self.assertEqual(cd_actions - stage_one, set())

    def test_stage_two_only_adds_actions_on_the_agent_chat_resources_and_the_application_role_policy(self) -> None:
        added = [s for s in statements("agent-chat") if s["Sid"] not in {x["Sid"] for x in statements("code-and-quota")}]
        self.assertEqual(len(added), 10)
        for statement in added:
            for resource in resources(statement):
                self.assertTrue(
                    "agent-chat" in resource.lower() or "AgentChat" in resource or resource in {PARAMETERS["ExistingApplicationRoleArn"], "*"}
                    or "event-source-mapping" in resource, f"{statement['Sid']}: {resource}")


class CoverageOfTheTemplateTests(unittest.TestCase):
    """Every AgentChat* resource of the Public Edge template can be created, updated and deleted by the stage-two role, and no more."""

    NEEDS = {
        "AWS::SQS::Queue": {"sqs:CreateQueue", "sqs:DeleteQueue", "sqs:GetQueueAttributes", "sqs:SetQueueAttributes", "sqs:TagQueue"},
        "AWS::Logs::LogGroup": {"logs:CreateLogGroup", "logs:DeleteLogGroup", "logs:PutRetentionPolicy"},
        "AWS::Logs::MetricFilter": {"logs:PutMetricFilter", "logs:DeleteMetricFilter"},
        "AWS::IAM::Role": {"iam:CreateRole", "iam:DeleteRole", "iam:PutRolePolicy", "iam:DeleteRolePolicy", "iam:GetRole", "iam:PassRole"},
        "AWS::Lambda::Function": {"lambda:CreateFunction", "lambda:DeleteFunction", "lambda:UpdateFunctionCode", "lambda:UpdateFunctionConfiguration"},
        "AWS::Lambda::EventSourceMapping": {"lambda:CreateEventSourceMapping", "lambda:DeleteEventSourceMapping", "lambda:UpdateEventSourceMapping"},
        "AWS::CloudWatch::Alarm": {"cloudwatch:PutMetricAlarm", "cloudwatch:DeleteAlarms"},
    }

    @classmethod
    def setUpClass(cls) -> None:
        cls.edge = load(EDGE)["Resources"]
        cls.agent_resources = {name: spec for name, spec in cls.edge.items() if name.startswith("AgentChat")}

    def test_the_template_still_has_the_resource_types_this_role_was_written_for(self) -> None:
        self.assertEqual({spec["Type"] for spec in self.agent_resources.values()}, set(self.NEEDS))
        self.assertEqual(len(self.agent_resources), 14)

    def test_stage_two_grants_the_actions_each_resource_type_needs(self) -> None:
        granted = allowed_actions("agent-chat")
        for resource_type, needed in self.NEEDS.items():
            self.assertTrue(needed <= granted, f"{resource_type}: missing {sorted(needed - granted)}")

    def test_the_name_patterns_cover_every_generated_or_fixed_name_of_the_template(self) -> None:
        patterns = " ".join(r for s in statements("agent-chat") for r in resources(s))

        # fixed names, as the template builds them from the stack name
        self.assertIn("LegalDeskPhase14PublicEdge-agent-chat-worker", patterns)
        for name in (f"{STACK}-agent-chat", f"{STACK}-agent-chat-dlq"):
            queue_arn = f"arn:aws:sqs:{REGION}:{ACCOUNT}:{name}"
            self.assertTrue(any(fnmatch.fnmatchcase(queue_arn, r) for s in statements("agent-chat") for r in resources(s)), name)
        # CloudFormation-generated names: <stack>-<logical id>-<random>
        generated_role = f"arn:aws:iam::{ACCOUNT}:role/{STACK}-AgentChatWorkerExecutionRole-ABCDEF123456"
        self.assertTrue(any(fnmatch.fnmatchcase(generated_role, r) for s in statements("agent-chat") for r in resources(s)))
        alarms = [name for name, spec in self.agent_resources.items() if spec["Type"] == "AWS::CloudWatch::Alarm"]
        self.assertEqual(len(alarms), 6)
        for logical_id in alarms:
            generated = f"arn:aws:cloudwatch:{REGION}:{ACCOUNT}:alarm:{STACK}-{logical_id}-ABCDEF123456"
            self.assertTrue(any(fnmatch.fnmatchcase(generated, r) for s in statements("agent-chat") for r in resources(s)), logical_id)
        worker = f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{STACK}-agent-chat-worker"
        self.assertTrue(any(fnmatch.fnmatchcase(worker, r) for s in statements("agent-chat") for r in resources(s)))
        log_group = f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:/aws/lambda/{STACK}-agent-chat-worker"
        self.assertTrue(any(fnmatch.fnmatchcase(log_group, r) for s in statements("agent-chat") for r in resources(s)))

    def test_the_names_are_generated_so_the_patterns_hold(self) -> None:
        # An explicit RoleName / AlarmName / QueueName would escape the patterns above.
        for name, spec in self.agent_resources.items():
            properties = spec.get("Properties", {})
            if spec["Type"] == "AWS::IAM::Role":
                self.assertNotIn("RoleName", properties, name)
            if spec["Type"] == "AWS::CloudWatch::Alarm":
                self.assertNotIn("AlarmName", properties, name)
            if spec["Type"] == "AWS::SQS::Queue":
                self.assertEqual(properties["QueueName"], {"Fn::Sub": "${AWS::StackName}-agent-chat" + ("-dlq" if "DeadLetter" in name else "")})
            if spec["Type"] == "AWS::Lambda::Function":
                self.assertEqual(properties["FunctionName"], {"Fn::Sub": "${AWS::StackName}-agent-chat-worker"})
            if spec["Type"] == "AWS::Logs::LogGroup":
                self.assertEqual(properties["LogGroupName"], {"Fn::Sub": "/aws/lambda/${AWS::StackName}-agent-chat-worker"})

    def test_no_other_resource_of_the_stack_is_writable_by_the_role(self) -> None:
        granted = allowed_actions("agent-chat")
        for forbidden in ("cloudfront:UpdateDistribution", "s3:PutBucketPolicy", "apigateway:POST", "apigateway:DELETE", "apigateway:PUT", "dynamodb:UpdateItem", "iam:DeleteRolePolicy"):
            if forbidden == "iam:DeleteRolePolicy":
                # allowed only on the worker role pattern
                for statement in statements("agent-chat"):
                    if forbidden in actions(statement):
                        self.assertTrue(all("AgentChatWorkerExecutionRole" in r for r in resources(statement)))
            else:
                self.assertNotIn(forbidden, granted)


class WorkflowTests(unittest.TestCase):
    def test_the_template_is_linted_by_the_offline_ci(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "offline-ci.yml").read_text(encoding="utf-8")
        self.assertIn("infra/cloudformation/phase-15-edge-agentchat-bootstrap.yaml", workflow)


if __name__ == "__main__":
    unittest.main()
