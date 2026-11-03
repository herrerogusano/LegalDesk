"""The temporary CloudFormation role of the Phase 15 Public Edge change sets: least privilege, by stage, against the template."""

from __future__ import annotations

import fnmatch
import re
import unittest
from pathlib import Path
from typing import Any

import yaml

from scripts.phase15_changeset_preview import cfn_generated_name

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
# Observed in the T9 live block of 2026-11-03 (CREATE of the worker role, step 8b). Not documented by AWS: the model is inferred from these two.
OBSERVED_WORKER_ROLE = "LegalDeskPhase14PublicEdg-AgentChatWorkerExecutionR-bzFuZq7zur6f"
OBSERVED_APPLICATION_ROLE = "LegalDeskPhase14PublicEdge-ApplicationExecutionRole-X3hgVtmlHWXr"
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


# The rule for the names CloudFormation generates lives in the script that the runbook calls, so the runbook and the tests cannot differ.
generated_name = cfn_generated_name


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
                    self.assertIn(":role/LegalDeskPhase14Public", resources(statement)[0])

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
        self.assertEqual(len(added), 11)  # + the tag-only statement for the event source mapping
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
        for generated_role in (
            f"arn:aws:iam::{ACCOUNT}:role/{OBSERVED_WORKER_ROLE}",  # what CloudFormation really created on 2026-11-03
            f"arn:aws:iam::{ACCOUNT}:role/{generated_name(STACK, 'AgentChatWorkerExecutionRole', 'ABCDEF123456')}",  # the model, another suffix
        ):
            self.assertTrue(any(fnmatch.fnmatchcase(generated_role, r) for s in statements("agent-chat") for r in resources(s)), generated_role)
        alarms = [name for name, spec in self.agent_resources.items() if spec["Type"] == "AWS::CloudWatch::Alarm"]
        self.assertEqual(len(alarms), 6)
        for logical_id in alarms:
            generated = f"arn:aws:cloudwatch:{REGION}:{ACCOUNT}:alarm:{STACK}-{logical_id}-ABCDEF123456"
            self.assertTrue(any(fnmatch.fnmatchcase(generated, r) for s in statements("agent-chat") for r in resources(s)), logical_id)
        worker = f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{STACK}-agent-chat-worker"
        self.assertTrue(any(fnmatch.fnmatchcase(worker, r) for s in statements("agent-chat") for r in resources(s)))
        log_group = f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:/aws/lambda/{STACK}-agent-chat-worker"
        self.assertTrue(any(fnmatch.fnmatchcase(log_group, r) for s in statements("agent-chat") for r in resources(s)))

    def test_the_name_model_reproduces_the_two_roles_observed_in_the_live_block(self) -> None:
        # Source of the rule: the roles CloudFormation created on 2026-11-03. The application role is the deployed one; the worker role is
        # what step 8b generated (and failed to create, because the first pattern assumed only the logical id is cut).
        self.assertEqual(generated_name(STACK, "ApplicationExecutionRole", "X3hgVtmlHWXr"), OBSERVED_APPLICATION_ROLE)
        self.assertEqual(generated_name(STACK, "AgentChatWorkerExecutionRole", "bzFuZq7zur6f"), OBSERVED_WORKER_ROLE)
        self.assertEqual((len(OBSERVED_APPLICATION_ROLE), len(OBSERVED_WORKER_ROLE)), (64, 64))
        # Both parts are cut for the worker role: the stack name 26 -> 25 and the logical id 28 -> 25, 12 random characters at the end.
        head, logical_part, suffix = OBSERVED_WORKER_ROLE.rsplit("-", 2)
        self.assertEqual((head, logical_part, len(suffix)), (STACK[:25], "AgentChatWorkerExecutionRole"[:25], 12))
        # The application role fits, so nothing is cut.
        self.assertTrue(OBSERVED_APPLICATION_ROLE.startswith(STACK + "-ApplicationExecutionRole-"))

    def test_the_model_cuts_nothing_when_the_limit_is_long_and_a_short_part_keeps_its_length(self) -> None:
        self.assertEqual(generated_name(STACK, "AgentChatWorkerErrorsAlarm", "ABCDEF123456", limit=255), f"{STACK}-AgentChatWorkerErrorsAlarm-ABCDEF123456")
        self.assertEqual(generated_name("S", "L" * 60, "ABCDEF123456")[:2], "S-")  # a part shorter than half keeps its length: the other takes the rest
        self.assertEqual(len(generated_name("S" * 60, "L", "ABCDEF123456")), 64)
        self.assertEqual(len(generated_name("S" * 60, "L" * 60, "ABCDEF123456")), 64)

    def test_the_role_pattern_is_the_cut_name_and_matches_neither_the_application_nor_the_temporary_role(self) -> None:
        pattern = next(r for s in statements("agent-chat") for r in resources(s) if ":role/" in r and "AgentChatWorker" in r)
        self.assertEqual(pattern, f"arn:aws:iam::{ACCOUNT}:role/LegalDeskPhase14PublicEdg-AgentChatWorkerExecutionR-*")
        self.assertTrue(fnmatch.fnmatchcase(f"arn:aws:iam::{ACCOUNT}:role/{OBSERVED_WORKER_ROLE}", pattern))
        temporary = generated_name("LegalDeskPhase15EdgeBootstrap", "EdgeAgentChatCloudFormationRole", "ABCDEF123456")
        for other in (OBSERVED_APPLICATION_ROLE, temporary, f"{STACK}-AgentChatWorkerExecutionRole-ABCDEF123456"):
            self.assertFalse(fnmatch.fnmatchcase(f"arn:aws:iam::{ACCOUNT}:role/{other}", pattern), other)

    def test_every_one_of_the_fourteen_resources_has_a_name_the_role_covers(self) -> None:
        """Each AgentChat* resource, with the name CloudFormation will give it (explicit, generated, or none), against the statements of stage two."""

        def covered(arn: str) -> bool:
            return any(fnmatch.fnmatchcase(arn, r) for statement in statements("agent-chat") for r in resources(statement))

        def explicit(properties: dict[str, Any], key: str) -> str:
            value = properties[key]
            self.assertEqual(set(value), {"Fn::Sub"}, f"{key} must be an explicit name built from the stack name")
            return value["Fn::Sub"].replace("${AWS::StackName}", STACK)

        seen: set[str] = set()
        for logical_id, spec in self.agent_resources.items():
            properties, resource_type = spec.get("Properties", {}), spec["Type"]
            seen.add(logical_id)
            if resource_type == "AWS::SQS::Queue":
                name = explicit(properties, "QueueName")
                self.assertLessEqual(len(name), 80, logical_id)  # SQS limit
                self.assertTrue(covered(f"arn:aws:sqs:{REGION}:{ACCOUNT}:{name}"), logical_id)
            elif resource_type == "AWS::Lambda::Function":
                name = explicit(properties, "FunctionName")
                self.assertLessEqual(len(name), 64, logical_id)  # Lambda limit
                self.assertTrue(covered(f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{name}"), logical_id)
            elif resource_type == "AWS::Logs::LogGroup":
                name = explicit(properties, "LogGroupName")
                self.assertTrue(covered(f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:{name}"), logical_id)
                self.assertTrue(covered(f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:{name}:*"), logical_id)
            elif resource_type == "AWS::Logs::MetricFilter":
                # no name of its own to match: the permissions are on the log group, and no FilterName may escape them
                self.assertNotIn("FilterName", properties, logical_id)
                self.assertTrue(covered(f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:/aws/lambda/{STACK}-agent-chat-worker:*"))
            elif resource_type == "AWS::IAM::Role":
                self.assertNotIn("RoleName", properties, logical_id)  # an explicit name would need CAPABILITY_NAMED_IAM, which the CD does not pass
                name = generated_name(STACK, logical_id, "bzFuZq7zur6f", limit=64)
                self.assertLessEqual(len(name), 64)
                self.assertTrue(covered(f"arn:aws:iam::{ACCOUNT}:role/{name}"), logical_id)
            elif resource_type == "AWS::CloudWatch::Alarm":
                self.assertNotIn("AlarmName", properties, logical_id)
                # CloudWatch alarm names go to 255: nothing is cut. The pattern still holds if CloudFormation cut like it does for roles (64).
                for limit in (255, 64):
                    name = generated_name(STACK, logical_id, "bzFuZq7zur6f", limit=limit)
                    self.assertLessEqual(len(name), limit)
                    self.assertTrue(covered(f"arn:aws:cloudwatch:{REGION}:{ACCOUNT}:alarm:{name}"), f"{logical_id} at {limit}: {name}")
            elif resource_type == "AWS::Lambda::EventSourceMapping":
                # no name before it exists: scoped by the function it feeds, plus tag-only access
                mapping = f"arn:aws:lambda:{REGION}:{ACCOUNT}:event-source-mapping:6a0f3d7e-5e4b-4f2c-9d3a-0123456789ab"
                self.assertTrue(covered(mapping), logical_id)
                tag_statement = next(s for s in statements("agent-chat") if s["Sid"] == "TagOnlyEventSourceMappings")
                self.assertEqual(set(actions(tag_statement)), {"lambda:TagResource", "lambda:UntagResource", "lambda:ListTags"})
            else:  # pragma: no cover - a new resource type must be reviewed here
                self.fail(f"{logical_id}: {resource_type} has no name rule in this test")
        self.assertEqual(len(seen), 14)

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
                        self.assertTrue(all("role/LegalDeskPhase14PublicEdg-AgentChatWorkerExecutionR-" in r for r in resources(statement)))
            else:
                self.assertNotIn(forbidden, granted)


class WorkflowTests(unittest.TestCase):
    def test_the_template_is_linted_by_the_offline_ci(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "offline-ci.yml").read_text(encoding="utf-8")
        self.assertIn("infra/cloudformation/phase-15-edge-agentchat-bootstrap.yaml", workflow)


if __name__ == "__main__":
    unittest.main()
