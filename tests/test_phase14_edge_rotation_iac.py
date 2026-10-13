from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).parents[1]
TEMPLATE = ROOT / "infra" / "cloudformation" / "phase-14-edge-rotation-bootstrap.yaml"
PARAMS = ROOT / "infra" / "cloudformation" / "phase-14-edge-rotation-bootstrap.params.json"


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


for _tag, _name in (("!Ref", "Ref"), ("!Sub", "Fn::Sub"), ("!GetAtt", "Fn::GetAtt")):
    _Loader.add_constructor(_tag, _intrinsic(_name))


def _actions(statement: dict[str, Any]) -> set[str]:
    action = statement["Action"]
    return set(action) if isinstance(action, list) else {action}


class Phase14EdgeRotationBootstrapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.template = yaml.load(TEMPLATE.read_text(encoding="utf-8"), Loader=_Loader)
        cls.resources = cls.template["Resources"]
        cls.role = cls.resources["EdgeRotationCloudFormationRole"]
        cls.statements = cls.role["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
        cls.by_sid = {item["Sid"]: item for item in cls.statements}

    def test_single_deletable_role_trusting_cloudformation_only(self) -> None:
        self.assertEqual(set(self.resources), {"EdgeRotationCloudFormationRole"})
        self.assertEqual(self.role["DeletionPolicy"], "Delete")
        trust = self.role["Properties"]["AssumeRolePolicyDocument"]["Statement"]
        self.assertEqual(len(trust), 1)
        self.assertEqual(trust[0]["Principal"], {"Service": "cloudformation.amazonaws.com"})
        self.assertNotIn("LegalDeskProductionCloudFormation", TEMPLATE.read_text(encoding="utf-8"))

    def test_exact_action_and_resource_matrix(self) -> None:
        expected = {
            "ReadExistingApplicationRole": (
                {"iam:GetRole", "iam:ListRolePolicies", "iam:ListAttachedRolePolicies", "iam:GetRolePolicy"},
                {"Ref": "ExistingApplicationRoleArn"},
            ),
            "UpdateConfigurationOfOnlyExistingApplicationFunction": (
                {"lambda:GetFunction", "lambda:GetFunctionConfiguration", "lambda:ListTags", "lambda:UpdateFunctionConfiguration"},
                {"Ref": "ApplicationFunctionArn"},
            ),
            "PassOnlyExistingApplicationRoleToLambda": ({"iam:PassRole"}, {"Ref": "ExistingApplicationRoleArn"}),
            "UpdateOnlyExistingDistribution": (
                {"cloudfront:GetDistribution", "cloudfront:GetDistributionConfig", "cloudfront:ListTagsForResource", "cloudfront:UpdateDistribution"},
                {"Ref": "CloudFrontDistributionArn"},
            ),
            "ReadAndPatchOnlyExistingApiIntegration": ({"apigateway:GET", "apigateway:PATCH"}, {"Ref": "PublicApiIntegrationArn"}),
            "ReadOnlyDistributionDependencies": (
                {"cloudfront:GetOriginAccessControl", "cloudfront:GetResponseHeadersPolicy"},
                [{"Ref": "OriginAccessControlArn"}, {"Ref": "ResponseHeadersPolicyArn"}],
            ),
            "DescribeRegionalLogGroupMetadataOnly": ({"logs:DescribeLogGroups"}, "*"),
        }
        self.assertEqual(set(self.by_sid), set(expected))
        for sid, (actions, resource) in expected.items():
            self.assertEqual(_actions(self.by_sid[sid]), actions, sid)
            self.assertEqual(self.by_sid[sid]["Resource"], resource, sid)
        self.assertEqual(
            self.by_sid["PassOnlyExistingApplicationRoleToLambda"]["Condition"],
            {"StringEquals": {"iam:PassedToService": "lambda.amazonaws.com"}},
        )
        self.assertEqual(
            self.by_sid["DescribeRegionalLogGroupMetadataOnly"]["Condition"],
            {"StringEquals": {"aws:RequestedRegion": "eu-west-1"}},
        )

    def test_no_wildcard_actions_no_policy_write_no_code_or_s3(self) -> None:
        every = set().union(*(_actions(item) for item in self.statements))
        for action in every:
            self.assertNotIn("*", action)
        for forbidden in ("iam:PutRolePolicy", "lambda:UpdateFunctionCode", "lambda:InvokeFunction", "iam:CreateRole"):
            self.assertNotIn(forbidden, every)
        self.assertFalse([a for a in every if a.startswith("s3:")])
        for item in self.statements:
            if item["Sid"] != "DescribeRegionalLogGroupMetadataOnly":
                self.assertNotEqual(item["Resource"], "*")

    def test_parameters_are_exact_patterns_and_params_file_matches(self) -> None:
        parameters = self.template["Parameters"]
        self.assertEqual(set(parameters), {"ExistingApplicationRoleArn", "ApplicationFunctionArn", "CloudFrontDistributionArn", "PublicApiIntegrationArn", "OriginAccessControlArn", "ResponseHeadersPolicyArn"})
        import re

        values = {item["ParameterKey"]: item["ParameterValue"] for item in json.loads(PARAMS.read_text(encoding="utf-8"))}
        self.assertEqual(set(values), set(parameters))
        for key, value in values.items():
            self.assertRegex(value, re.compile(parameters[key]["AllowedPattern"]))
        self.assertTrue(values["PublicApiIntegrationArn"].endswith("/apis/k7z9nuofg4/integrations/gs2d6w4"))


if __name__ == "__main__":
    unittest.main()
