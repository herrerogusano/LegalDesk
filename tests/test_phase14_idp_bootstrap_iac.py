from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).parents[1]
TEMPLATE = ROOT / "infra" / "cloudformation" / "phase-14-idp-bootstrap.yaml"


class _Loader(yaml.SafeLoader):
    pass


def _intrinsic(name: str):
    def constructor(loader: _Loader, node: yaml.Node) -> dict[str, Any]:
        if isinstance(node, yaml.ScalarNode):
            value = loader.construct_scalar(node)
        elif isinstance(node, yaml.SequenceNode):
            value = loader.construct_sequence(node, deep=True)
        else:
            value = loader.construct_mapping(node, deep=True)
        return {name: value}

    return constructor


for _tag, _name in (
    ("!Ref", "Ref"),
    ("!Sub", "Fn::Sub"),
    ("!GetAtt", "Fn::GetAtt"),
):
    _Loader.add_constructor(_tag, _intrinsic(_name))


class Phase14IDPBootstrapInfrastructureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.template = yaml.load(TEMPLATE.read_text(encoding="utf-8"), Loader=_Loader)
        cls.resources = cls.template["Resources"]
        cls.role = cls.resources["PublicEdgeBootstrapCloudFormationRole"]
        cls.statements = cls.role["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]

    def _statement(self, sid: str) -> dict[str, Any]:
        matches = [item for item in self.statements if item.get("Sid") == sid]
        self.assertEqual(len(matches), 1)
        return matches[0]

    def test_only_one_temporary_role_and_no_cd_role_or_data_plane_resources(self) -> None:
        self.assertEqual(set(self.resources), {"PublicEdgeBootstrapCloudFormationRole"})
        self.assertEqual(self.role["Type"], "AWS::IAM::Role")
        self.assertEqual(self.role["DeletionPolicy"], "Delete")
        self.assertEqual(self.role["UpdateReplacePolicy"], "Delete")
        self.assertNotIn("LegalDeskProductionCloudFormation", TEMPLATE.read_text(encoding="utf-8"))
        self.assertNotIn("AWS::Lambda::Function", str(self.template))
        self.assertNotIn("AWS::CloudFront::Distribution", str(self.template))
        self.assertNotIn("AWS::ApiGateway", str(self.template))

    def test_trust_is_cloudformation_only(self) -> None:
        trust = self.role["Properties"]["AssumeRolePolicyDocument"]["Statement"]
        self.assertEqual(len(trust), 1)
        self.assertEqual(trust[0]["Principal"], {"Service": "cloudformation.amazonaws.com"})
        self.assertEqual(trust[0]["Action"], "sts:AssumeRole")
        self.assertNotIn("aws:SourceArn", str(trust))

    def test_existing_application_role_is_only_iam_resource(self) -> None:
        read = self._statement("ReadExistingApplicationRole")
        self.assertEqual(
            set(read["Action"]),
            {"iam:GetRole", "iam:ListRolePolicies", "iam:ListAttachedRolePolicies", "iam:GetRolePolicy"},
        )
        self.assertEqual(read["Resource"], {"Ref": "ExistingApplicationRoleArn"})
        patch = self._statement("PatchExistingApplicationRoleInlinePolicy")
        self.assertEqual(patch["Action"], "iam:PutRolePolicy")
        self.assertEqual(patch["Resource"], {"Ref": "ExistingApplicationRoleArn"})
        passed = self._statement("PassOnlyExistingApplicationRoleToLambda")
        self.assertEqual(passed["Action"], "iam:PassRole")
        self.assertEqual(passed["Resource"], {"Ref": "ExistingApplicationRoleArn"})
        self.assertEqual(passed["Condition"]["StringEquals"]["iam:PassedToService"], "lambda.amazonaws.com")
        all_actions = {action for statement in self.statements for action in (statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]])}
        self.assertNotIn("iam:DeleteRolePolicy", all_actions)
        self.assertNotIn("iam:CreateRole", all_actions)
        self.assertNotIn("iam:AttachRolePolicy", all_actions)

    def test_lambda_s3_cloudfront_permissions_are_exact_and_non_mutating(self) -> None:
        function = self._statement("UpdateOnlyExistingApplicationFunction")
        self.assertEqual(
            set(function["Action"]),
            {"lambda:GetFunction", "lambda:GetFunctionConfiguration", "lambda:ListTags", "lambda:UpdateFunctionCode", "lambda:UpdateFunctionConfiguration"},
        )
        self.assertEqual(function["Resource"], {"Ref": "ApplicationFunctionArn"})
        artifact = self._statement("ReadOnlyExactImmutableArtifact")
        self.assertEqual(set(artifact["Action"]), {"s3:GetObject", "s3:GetObjectVersion"})
        self.assertEqual(artifact["Resource"], {"Fn::Sub": "${ArtifactBucketArn}/${ArtifactObjectKey}"})
        distribution = self._statement("ReadOnlyExistingDistributionMetadata")
        self.assertEqual(
            set(distribution["Action"]),
            {"cloudfront:GetDistribution", "cloudfront:GetDistributionConfig", "cloudfront:ListTagsForResource"},
        )
        self.assertEqual(distribution["Resource"], {"Ref": "CloudFrontDistributionArn"})
        integration = self._statement("ReadAndPatchOnlyExistingApiIntegration")
        self.assertEqual(set(integration["Action"]), {"apigateway:GET", "apigateway:PATCH"})
        self.assertEqual(integration["Resource"], {"Ref": "PublicApiIntegrationArn"})
        log_groups = self._statement("DescribeRegionalLogGroupMetadataOnly")
        self.assertEqual(log_groups["Action"], "logs:DescribeLogGroups")
        self.assertEqual(log_groups["Resource"], "*")
        self.assertEqual(
            log_groups["Condition"],
            {"StringEquals": {"aws:RequestedRegion": "eu-west-1"}},
        )
        all_actions = {
            action
            for statement in self.statements
            for action in (statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]])
        }
        self.assertNotIn("logs:FilterLogEvents", all_actions)
        self.assertNotIn("logs:PutLogEvents", all_actions)
        self.assertNotIn("logs:CreateLogGroup", all_actions)
        for statement in self.statements:
            resource = statement["Resource"]
            if statement["Sid"] != "DescribeRegionalLogGroupMetadataOnly":
                self.assertNotEqual(resource, "*")
            self.assertNotIn("lambda:Invoke", str(statement))

    def test_parameters_pin_deployed_resources_and_artifact_key_shape(self) -> None:
        parameters = self.template["Parameters"]
        self.assertEqual(
            set(parameters),
            {"ExistingApplicationRoleArn", "ApplicationFunctionArn", "CloudFrontDistributionArn", "PublicApiIntegrationArn", "ArtifactBucketArn", "ArtifactObjectKey"},
        )
        self.assertIn("LegalDeskPhase14PublicEdge-ApplicationExecutionRole", parameters["ExistingApplicationRoleArn"]["AllowedPattern"])
        self.assertIn("LegalDeskPhase14PublicEdge-application", parameters["ApplicationFunctionArn"]["AllowedPattern"])
        self.assertIn("phase-14/", parameters["ArtifactObjectKey"]["AllowedPattern"])
        self.assertIn("legaldesk-lambda", parameters["ArtifactObjectKey"]["AllowedPattern"])
        self.assertIn("k7z9nuofg4/integrations/gs2d6w4", parameters["PublicApiIntegrationArn"]["AllowedPattern"])


if __name__ == "__main__":
    unittest.main()
