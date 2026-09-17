from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
ARTIFACTS = ROOT / "infra" / "cloudformation" / "phase-08-artifacts.yaml"
COGNITO = ROOT / "infra" / "cloudformation" / "phase-08-cognito.yaml"


class Phase08DependencyInfrastructureTests(unittest.TestCase):
    def test_artifact_bucket_is_private_versioned_encrypted_and_retained(self) -> None:
        template = ARTIFACTS.read_text(encoding="utf-8")
        self.assertEqual(
            re.findall(
                r"^  [A-Za-z][A-Za-z0-9]*:\n    Type: (AWS::[A-Za-z0-9:]+)",
                template,
                re.MULTILINE,
            ),
            ["AWS::S3::Bucket", "AWS::S3::BucketPolicy"],
        )
        for setting in (
            "VersioningConfiguration:\n        Status: Enabled",
            "SSEAlgorithm: AES256",
            "BlockPublicAcls: true",
            "BlockPublicPolicy: true",
            "IgnorePublicAcls: true",
            "RestrictPublicBuckets: true",
            "DeletionPolicy: Retain",
            "UpdateReplacePolicy: Retain",
        ):
            self.assertIn(setting, template)
        self.assertIn("NoncurrentVersionExpiration:", template)
        self.assertNotIn('Resource: "*"', template)

    def test_cognito_has_minimal_oidc_client_and_gateway_scope(self) -> None:
        template = COGNITO.read_text(encoding="utf-8")
        self.assertEqual(
            re.findall(
                r"^  [A-Za-z][A-Za-z0-9]*:\n    Type: (AWS::[A-Za-z0-9:]+)",
                template,
                re.MULTILINE,
            ),
            [
                "AWS::Cognito::UserPool",
                "AWS::Cognito::UserPoolResourceServer",
                "AWS::Cognito::UserPoolClient",
                "AWS::Cognito::UserPoolDomain",
            ],
        )
        self.assertIn("Identifier: legaldesk", template)
        self.assertIn("ScopeName: use", template)
        self.assertIn("GenerateSecret: true", template)
        self.assertIn("AllowedOAuthFlowsUserPoolClient: true", template)
        self.assertIn("- client_credentials", template)
        self.assertIn("- legaldesk/use", template)
        self.assertIn("Domain: !Sub legaldesk-phase08-${AWS::AccountId}", template)
        self.assertNotIn("344774635844", template)
        self.assertIn("/.well-known/openid-configuration", template)
        self.assertNotIn("AdminCreateUser", template)


if __name__ == "__main__":
    unittest.main()
