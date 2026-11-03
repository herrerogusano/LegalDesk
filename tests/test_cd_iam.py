from __future__ import annotations

import unittest
from pathlib import Path
import re

import yaml


ROOT = Path(__file__).parents[1]


class CdIamTemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        class Loader(yaml.SafeLoader):
            pass

        def intrinsic(loader: yaml.SafeLoader, tag: str, node: yaml.Node) -> object:
            if isinstance(node, yaml.ScalarNode):
                value: object = loader.construct_scalar(node)
            elif isinstance(node, yaml.SequenceNode):
                value = loader.construct_sequence(node)
            else:
                value = loader.construct_mapping(node)
            return value

        Loader.add_multi_constructor("!", intrinsic)
        cls.template = yaml.load((ROOT / "infra/cloudformation/cd-iam.yaml").read_text(encoding="utf-8"), Loader=Loader)

    def test_two_roles_reuse_exact_oidc_subject(self) -> None:
        resources = self.template["Resources"]
        self.assertEqual(set(resources), {"ProductionDeploymentRole", "CloudFormationExecutionRole"})
        trust = resources["ProductionDeploymentRole"]["Properties"]["AssumeRolePolicyDocument"]["Statement"][0]
        conditions = trust["Condition"]["StringEquals"]
        self.assertEqual(conditions["token.actions.githubusercontent.com:aud"], "sts.amazonaws.com")
        self.assertEqual(conditions["token.actions.githubusercontent.com:sub"], "GitHubSubject")
        self.assertEqual(self.template["Parameters"]["GitHubSubject"]["Default"], "repo:herrerogusano@112490238/LegalDesk@1362771339:ref:refs/heads/prod")

    def test_policies_have_no_application_data_or_wildcard_mutation(self) -> None:
        for resource in self.template["Resources"].values():
            policies = resource["Properties"]["Policies"]
            for policy in policies:
                for statement in policy["PolicyDocument"]["Statement"]:
                    actions = statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]]
                    resource_value = statement["Resource"]
                    resources = resource_value if isinstance(resource_value, list) else [resource_value]
                    self.assertFalse(any(action.endswith(":*") or action in {"lambda:InvokeFunction", "bedrock:InvokeModel", "dynamodb:PutItem", "s3:DeleteObject"} for action in actions))
                    if actions != ["cloudformation:ValidateTemplate"]:
                        self.assertNotIn("*", resources)

    def test_code_update_permissions_are_exact(self) -> None:
        policy = self.template["Resources"]["CloudFormationExecutionRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
        actions = {action for statement in policy for action in (statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]])}
        self.assertIn("lambda:UpdateFunctionCode", actions)
        self.assertIn("apigateway:PATCH", actions)
        self.assertNotIn("iam:PutRolePolicy", actions)
        self.assertNotIn("cloudformation:CreateStack", actions)

    def test_cloudformation_role_reads_only_the_unchanged_lambda_role(self) -> None:
        statements = self.template["Resources"]["CloudFormationExecutionRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
        read_statement = next(statement for statement in statements if statement["Sid"] == "ReadUnchangedApplicationRolePolicy")
        self.assertEqual(
            set(read_statement["Action"]),
            {"iam:GetRole", "iam:ListRolePolicies", "iam:ListAttachedRolePolicies", "iam:GetRolePolicy"},
        )
        self.assertEqual(read_statement["Resource"], "ApplicationExecutionRoleArn")

    def test_cloudformation_role_reads_only_the_existing_distribution_metadata(self) -> None:
        statements = self.template["Resources"]["CloudFormationExecutionRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
        read_statement = next(statement for statement in statements if statement["Sid"] == "ReadOnlyPublicDistributionMetadata")
        self.assertEqual(
            set(read_statement["Action"]),
            {"cloudfront:GetDistribution", "cloudfront:GetDistributionConfig", "cloudfront:ListTagsForResource"},
        )
        self.assertEqual(read_statement["Resource"], "CloudFrontDistributionArn")

    def test_application_function_arn_contract_uses_colon_form(self) -> None:
        pattern = self.template["Parameters"]["ApplicationFunctionArn"]["AllowedPattern"]
        self.assertIsNotNone(re.fullmatch(pattern, "arn:aws:lambda:eu-west-1:344774635844:function:LegalDeskPhase14PublicEdge-application"))


    def _statements(self, role: str) -> dict[str, dict]:
        statements = self.template["Resources"][role]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
        return {statement["Sid"]: statement for statement in statements}

    def test_worker_code_update_is_scoped_to_the_one_worker_function(self) -> None:
        update = self._statements("CloudFormationExecutionRole")["ReadAndUpdateOnlyAgentChatWorkerCode"]
        self.assertEqual(set(update["Action"]), {"lambda:GetFunction", "lambda:GetFunctionConfiguration", "lambda:UpdateFunctionCode"})
        self.assertEqual(update["Resource"], "AgentChatWorkerFunctionArn")
        # No other Lambda action may be reachable on the worker, and UpdateFunctionCode is
        # granted on exactly two functions in the whole template (application + worker).
        granted = [
            (sid, statement["Resource"])
            for role in self.template["Resources"]
            for sid, statement in self._statements(role).items()
            if "lambda:UpdateFunctionCode" in statement["Action"]
        ]
        self.assertEqual(sorted(resource for _, resource in granted), ["AgentChatWorkerFunctionArn", "ApplicationFunctionArn"])

    def test_deployment_role_only_reads_the_worker_for_the_hash_check(self) -> None:
        verify = self._statements("ProductionDeploymentRole")["VerifyOnlyTheAgentChatWorker"]
        self.assertEqual(set(verify["Action"]), {"lambda:GetFunction", "lambda:GetFunctionConfiguration"})
        self.assertEqual(verify["Resource"], "AgentChatWorkerFunctionArn")

    def test_worker_role_is_read_only_for_cloudformation(self) -> None:
        statements = self._statements("CloudFormationExecutionRole")
        read = statements["ReadUnchangedAgentChatWorkerRolePolicy"]
        self.assertEqual(set(read["Action"]), {"iam:GetRole", "iam:ListRolePolicies", "iam:ListAttachedRolePolicies", "iam:GetRolePolicy"})
        self.assertEqual(read["Resource"], "AgentChatWorkerExecutionRoleArn")
        passed = statements["PassUnchangedAgentChatWorkerRoleIfRequired"]
        self.assertEqual(passed["Resource"], "AgentChatWorkerExecutionRoleArn")
        self.assertEqual(passed["Condition"]["StringEquals"]["iam:PassedToService"], "lambda.amazonaws.com")

    def test_no_iam_write_is_reachable_by_any_cd_role(self) -> None:
        for role in self.template["Resources"]:
            for statement in self._statements(role).values():
                actions = statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]]
                for action in actions:
                    if action.startswith("iam:"):
                        self.assertIn(action, {"iam:PassRole", "iam:GetRole", "iam:ListRolePolicies", "iam:ListAttachedRolePolicies", "iam:GetRolePolicy"})

    def test_worker_arn_contracts(self) -> None:
        parameters = self.template["Parameters"]
        pattern = parameters["AgentChatWorkerFunctionArn"]["AllowedPattern"]
        self.assertIsNotNone(re.fullmatch(pattern, "arn:aws:lambda:eu-west-1:344774635844:function:LegalDeskPhase14PublicEdge-agent-chat-worker"))
        # Neither the application function nor another function can be passed as the worker.
        self.assertIsNone(re.fullmatch(pattern, "arn:aws:lambda:eu-west-1:344774635844:function:LegalDeskPhase14PublicEdge-application"))
        self.assertIsNone(re.fullmatch(pattern, "arn:aws:lambda:eu-west-1:344774635844:function:LegalDeskPhase14PublicEdge-agent-chat-worker-evil"))
        self.assertNotIn("Default", parameters["AgentChatWorkerFunctionArn"])
        self.assertNotIn("Default", parameters["AgentChatWorkerExecutionRoleArn"])


if __name__ == "__main__":
    unittest.main()
