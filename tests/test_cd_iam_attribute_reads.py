"""The code-only CloudFormation role must be able to resolve every !Ref / !GetAtt that the code-updated resources make on their neighbours.

Live failure of 2026-11-03 (CD of the promotion, step 9b): CloudFormation, updating the ``Code`` of ``AgentChatWorkerFunction`` with the code-only
service role, resolved ``!GetAtt AgentChatQueue.Arn`` and was denied ``sqs:GetQueueAttributes``; the stack rolled back. The tests here derive the
reads from the Public Edge template itself instead of from a list somebody remembered, so a new dependency of these resources fails them until the
role (and the runbook simulation) is reviewed.
"""

from __future__ import annotations

import fnmatch
import re
import unittest
from pathlib import Path
from typing import Any

from tests.test_phase15_edge_bootstrap_iac import ACCOUNT, REGION, STACK, PSEUDO, generated_name, load

ROOT = Path(__file__).parents[1]
EDGE = ROOT / "infra" / "cloudformation" / "phase-14-public-edge.yaml"
CD_IAM = ROOT / "infra" / "cloudformation" / "cd-iam.yaml"

# The resources whose Code / IntegrationUri the code-only CD updates (scripts/deploy_release.py allows exactly these changes).
UPDATED = ("ApplicationFunction", "AgentChatWorkerFunction", "PublicApiIntegration")

# Sample values for the parameters of cd-iam.yaml, shaped like the ARNs derived from the deployed stack in runbook step 9.
CD_PARAMETERS = {
    "ApplicationFunctionArn": f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{STACK}-application",
    "ApplicationExecutionRoleArn": f"arn:aws:iam::{ACCOUNT}:role/{STACK}-ApplicationExecutionRole-X3hgVtmlHWXr",
    "AgentChatWorkerFunctionArn": f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{STACK}-agent-chat-worker",
    "AgentChatWorkerExecutionRoleArn": f"arn:aws:iam::{ACCOUNT}:role/{generated_name(STACK, 'AgentChatWorkerExecutionRole', 'bzFuZq7zur6f')}",
    "PublicApiIntegrationArn": f"arn:aws:apigateway:{REGION}::/apis/abc123/integrations/xyz789",
    "ArtifactBucketArn": "arn:aws:s3:::artifacts-bucket",
    "FrontendBucketArn": "arn:aws:s3:::frontend-bucket",
    "CloudFrontDistributionArn": f"arn:aws:cloudfront::{ACCOUNT}:distribution/EABC123",
}
# What CloudFormation needs to resolve an attribute of a neighbour (empty: the physical id is the value, no API call). Anything not listed fails
# the test on purpose: a new dependency needs a reviewed decision, not a silent pass.
READS_FOR = {
    ("AWS::SQS::Queue", None): set(),                       # Ref -> the queue URL is the physical id
    ("AWS::SQS::Queue", "Arn"): {"sqs:GetQueueAttributes"},  # the failure of 2026-11-03
    ("AWS::IAM::Role", "Arn"): {"iam:GetRole"},
    ("AWS::Lambda::Function", "Arn"): {"lambda:GetFunction"},
    ("AWS::Lambda::Function", None): set(),
    ("AWS::ApiGatewayV2::Api", None): set(),
    ("AWS::Logs::LogGroup", None): set(),
}


def edge_resources() -> dict[str, Any]:
    return load(EDGE)["Resources"]


def references(node: Any) -> set[tuple[str, str | None]]:
    """(logical id, attribute or None) of every Ref / GetAtt / Sub variable under ``node``; pseudo parameters and parameters are filtered later."""

    found: set[tuple[str, str | None]] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "Ref":
                found.add((value, None))
            elif key == "Fn::GetAtt":
                name, attribute = value.split(".", 1) if isinstance(value, str) else value
                found.add((name, attribute))
            elif key == "Fn::Sub":
                text = value if isinstance(value, str) else value[0]
                for variable in re.findall(r"\$\{([^}!]+)\}", text):
                    name, _, attribute = variable.partition(".")
                    found.add((name, attribute or None))
                if not isinstance(value, str):
                    found |= references(value[1])
            else:
                found |= references(value)
    elif isinstance(node, list):
        for item in node:
            found |= references(item)
    return found


def neighbours(resources: dict[str, Any], logical_id: str) -> set[tuple[str, str | None]]:
    """Resource neighbours (not parameters, not pseudo parameters) that ``logical_id`` references, including its DependsOn."""

    spec = resources[logical_id]
    found = references({key: value for key, value in spec.items() if key != "DependsOn"})
    depends = spec.get("DependsOn", [])
    found |= {(name, None) for name in ([depends] if isinstance(depends, str) else depends)}
    return {(name, attribute) for name, attribute in found if name in resources and name != logical_id}


def render_cd(node: Any) -> str:
    if isinstance(node, dict) and set(node) == {"Ref"}:
        return CD_PARAMETERS[node["Ref"]]
    if isinstance(node, dict) and set(node) == {"Fn::Sub"}:
        return re.sub(r"\$\{([^}]+)\}", lambda m: str(PSEUDO.get(m.group(1)) or CD_PARAMETERS[m.group(1)]), node["Fn::Sub"])
    raise AssertionError(f"unrendered resource {node!r}")


def cd_grants() -> list[tuple[set[str], list[str]]]:
    statements = load(CD_IAM)["Resources"]["CloudFormationExecutionRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
    grants = []
    for statement in statements:
        actions = statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]]
        resources = statement["Resource"] if isinstance(statement["Resource"], list) else [statement["Resource"]]
        grants.append((set(actions), [render_cd(resource) for resource in resources]))
    return grants


def sample_arn(resources: dict[str, Any], logical_id: str) -> str:
    """ARN of a neighbour as the CD role sees it: from the parameters of cd-iam.yaml, or from the explicit name in the Public Edge template."""

    by_parameter = {
        "ApplicationExecutionRole": "ApplicationExecutionRoleArn",
        "AgentChatWorkerExecutionRole": "AgentChatWorkerExecutionRoleArn",
        "ApplicationFunction": "ApplicationFunctionArn",
        "AgentChatWorkerFunction": "AgentChatWorkerFunctionArn",
    }
    if logical_id in by_parameter:
        return CD_PARAMETERS[by_parameter[logical_id]]
    spec = resources[logical_id]
    if spec["Type"] == "AWS::SQS::Queue":
        name = re.sub(r"\$\{([^}]+)\}", lambda m: str(PSEUDO[m.group(1)]), spec["Properties"]["QueueName"]["Fn::Sub"])
        return f"arn:aws:sqs:{REGION}:{ACCOUNT}:{name}"
    raise AssertionError(f"no sample ARN for {logical_id}: add it deliberately")


def allowed(grants: list[tuple[set[str], list[str]]], action: str, arn: str) -> bool:
    return any(action in actions and any(fnmatch.fnmatchcase(arn, resource) for resource in resource_list) for actions, resource_list in grants)


class AttributeReadsFollowTheTemplateTests(unittest.TestCase):
    def test_every_neighbour_attribute_of_the_updated_resources_is_a_known_read_and_is_granted(self) -> None:
        resources, grants = edge_resources(), cd_grants()
        checked = 0
        for logical_id in UPDATED:
            for name, attribute in sorted(neighbours(resources, logical_id), key=lambda pair: (pair[0], pair[1] or "")):
                kind = resources[name]["Type"]
                key = (kind, attribute)
                self.assertIn(key, READS_FOR, f"{logical_id} -> {name}.{attribute or '(Ref)'} ({kind}) is a new dependency: decide which read CloudFormation needs")
                if not READS_FOR[key]:
                    continue
                # the Lambda attribute of the function being updated is covered by its own statement; roles and queues by theirs
                arn = sample_arn(resources, name)
                for action in READS_FOR[key]:
                    self.assertTrue(allowed(grants, action, arn), f"{logical_id} needs {action} on {name} ({arn}); the code-only role cannot")
                    checked += 1
        self.assertEqual(checked, 4)  # worker -> queue Arn and worker role Arn; application -> its role Arn; integration -> application function Arn

    def test_the_failure_of_2026_11_03_is_in_the_derived_set(self) -> None:
        """The worker's environment and event source read ``AgentChatQueue.Arn``: this is exactly what the live CD was denied."""

        resources = edge_resources()
        self.assertIn(("AgentChatQueue", "Arn"), neighbours(resources, "AgentChatWorkerFunction"))
        self.assertIn(("AgentChatQueue", None), neighbours(resources, "ApplicationFunction"))
        self.assertTrue(allowed(cd_grants(), "sqs:GetQueueAttributes", sample_arn(resources, "AgentChatQueue")))

    def test_the_dead_letter_queue_is_read_too_because_the_queue_points_at_it(self) -> None:
        resources = edge_resources()
        self.assertIn(("AgentChatDeadLetterQueue", "Arn"), references(resources["AgentChatQueue"]["Properties"]))
        self.assertTrue(allowed(cd_grants(), "sqs:GetQueueAttributes", sample_arn(resources, "AgentChatDeadLetterQueue")))

    def test_the_samples_are_valid_values_of_the_cd_parameters(self) -> None:
        parameters = load(CD_IAM)["Parameters"]
        for name, value in CD_PARAMETERS.items():
            self.assertIsNotNone(re.fullmatch(parameters[name]["AllowedPattern"], value), name)


class QueueReadsAreNothingMoreTests(unittest.TestCase):
    def sqs_statements(self) -> list[dict[str, Any]]:
        template = load(CD_IAM)["Resources"]
        found = []
        for role in template.values():
            if role["Type"] != "AWS::IAM::Role":
                continue
            for policy in role["Properties"].get("Policies", []):
                for statement in policy["PolicyDocument"]["Statement"]:
                    actions = statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]]
                    if any(action.startswith("sqs:") for action in actions):
                        found.append(statement)
        return found

    def test_only_the_code_only_role_reads_only_attributes_and_url_of_exactly_the_two_queues(self) -> None:
        statements = self.sqs_statements()
        self.assertEqual(len(statements), 1)
        statement = statements[0]
        self.assertEqual((statement["Sid"], statement["Effect"]), ("ReadOnlyAgentChatQueueAttributes", "Allow"))
        self.assertEqual(set(statement["Action"]), {"sqs:GetQueueAttributes", "sqs:GetQueueUrl"})
        self.assertNotIn("Condition", statement)
        rendered = sorted(render_cd(resource) for resource in statement["Resource"])
        self.assertEqual(rendered, [f"arn:aws:sqs:{REGION}:{ACCOUNT}:{STACK}-agent-chat", f"arn:aws:sqs:{REGION}:{ACCOUNT}:{STACK}-agent-chat-dlq"])
        for arn in rendered:
            self.assertNotIn("*", arn)

    def test_it_lives_in_the_code_only_role_not_in_the_release_role(self) -> None:
        template = load(CD_IAM)["Resources"]
        release = template["ProductionDeploymentRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
        for statement in release:
            actions = statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]]
            self.assertFalse(any(action.startswith("sqs:") for action in actions), statement["Sid"])
        service = template["CloudFormationExecutionRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
        self.assertEqual([s["Sid"] for s in service if any(a.startswith("sqs:") for a in (s["Action"] if isinstance(s["Action"], list) else [s["Action"]]))], ["ReadOnlyAgentChatQueueAttributes"])

    def test_no_message_or_queue_mutation_is_reachable_through_any_cd_statement(self) -> None:
        for actions, _ in cd_grants():
            for action in actions:
                self.assertNotIn(action, {"sqs:SendMessage", "sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:PurgeQueue", "sqs:CreateQueue", "sqs:DeleteQueue", "sqs:SetQueueAttributes"})

    def test_the_derivation_notices_a_new_dependency(self) -> None:
        """Guard of the guard: a made-up dependency is not silently accepted by the read table."""

        resources = {
            "Updated": {"Type": "AWS::Lambda::Function", "Properties": {"Environment": {"Variables": {"X": {"Fn::GetAtt": ["Table", "StreamArn"]}}}}},
            "Table": {"Type": "AWS::DynamoDB::Table", "Properties": {}},
        }
        self.assertEqual(neighbours(resources, "Updated"), {("Table", "StreamArn")})
        self.assertNotIn(("AWS::DynamoDB::Table", "StreamArn"), READS_FOR)


if __name__ == "__main__":
    unittest.main()
