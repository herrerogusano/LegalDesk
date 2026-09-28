from __future__ import annotations

import re
import unittest
from pathlib import Path


TEMPLATE = Path(__file__).parents[1] / "infra" / "cloudformation" / "phase-14-document-security.yaml"


class Phase14DocumentSecurityInfrastructureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.template = TEMPLATE.read_text(encoding="utf-8")

    def test_guardduty_plan_is_quarantine_prefix_and_tagging_only(self) -> None:
        text = self.template
        self.assertIn("Type: AWS::GuardDuty::MalwareProtectionPlan", text)
        self.assertIn("Status: ENABLED", text)
        self.assertIn("ObjectPrefixes:", text)
        self.assertIn('!Sub "quarantine/tenants/${BetaTenantId}/"', text)
        self.assertIn("malware-protection-plan.guardduty.amazonaws.com", text)
        for required_action in (
            "events:PutRule",
            "events:DeleteRule",
            "events:PutTargets",
            "events:RemoveTargets",
            "events:DescribeRule",
            "events:ListTargetsByRule",
            "s3:PutBucketNotification",
            "s3:GetBucketNotification",
        ):
            self.assertIn(required_action, text)
        self.assertIn("malware-protection-resource-validation-object", text)
        self.assertNotIn("kms:", text.lower())
        self.assertNotIn("ObjectPrefixes:\n            - !Sub \"quarantine/\"", text)

    def test_eventbridge_pattern_is_exact_and_retry_is_bounded(self) -> None:
        text = self.template
        for value in (
            "source:",
            "- aws.guardduty",
            "detail-type:",
            "- GuardDuty Malware Protection Object Scan Result",
            "account:",
            "- !Ref AWS::AccountId",
            "region:",
            "- !Ref AWS::Region",
            "resourceType:",
            "- S3_OBJECT",
            "bucketName:",
            "- !Ref SourceBucketName",
            "objectKey:",
            'wildcard: !Sub "quarantine/tenants/${BetaTenantId}/matters/*/documents/*/original.pdf"',
            'wildcard: !Sub "quarantine/tenants/${BetaTenantId}/matters/*/documents/*/original.txt"',
            "MaximumEventAgeInSeconds: 3600",
            "MaximumRetryAttempts: 3",
            "DeadLetterConfig:",
        ):
            self.assertIn(value, text)
        rule = text[text.index("  MalwareScanRule:"):text.index("  MalwareScanInvokePermission:")]
        self.assertNotIn(".metadata.json", rule)
        self.assertNotIn("prefix:", rule)
        self.assertIn("SqsManagedSseEnabled: true", text)
        self.assertIn("MessageRetentionPeriod: 1209600", text)

    def test_lambda_boundary_and_permissions_are_bounded(self) -> None:
        text = self.template
        self.assertIn("Handler: legaldesk.malware_scan_lambda.lambda_handler", text)
        self.assertIn("S3ObjectVersion: !Ref MalwareLambdaCodeVersion", text)
        self.assertIn("MalwareReservedConcurrency:", text)
        self.assertIn("Default: 5", text)
        self.assertIn("HasMalwareReservedConcurrency", text)
        self.assertIn("ReservedConcurrentExecutions: !If", text)
        self.assertIn("LEGALDESK_MALWARE_ACCOUNT_ID: !Ref AWS::AccountId", text)
        self.assertIn("LEGALDESK_MALWARE_REGION: !Ref AWS::Region", text)
        self.assertIn("Action: [dynamodb:GetItem, dynamodb:UpdateItem]", text)
        self.assertIn("dynamodb:LeadingKeys:", text)
        self.assertIn('TENANT#${BetaTenantId}#MATTER#*', text)
        self.assertNotIn("dynamodb:Scan", text)
        self.assertNotIn("dynamodb:Query", text)
        self.assertIn("s3:GetObjectTagging", text)
        self.assertIn("s3:PutObject", text)
        self.assertIn("s3:DeleteObject", text)
        self.assertNotIn("s3:ListAllMyBuckets", text)
        self.assertNotIn('Resource: "*"', text)

    def test_existing_resources_are_parameters_and_no_public_data_plane_is_created(self) -> None:
        text = self.template
        for parameter in (
            "MetadataTableName:",
            "MetadataTableArn:",
            "SourceBucketName:",
            "SourceBucketArn:",
            "MalwareLambdaCodeBucket:",
            "MalwareLambdaCodeKey:",
            "MalwareLambdaCodeVersion:",
            "BetaTenantId:",
        ):
            self.assertIn(parameter, text)
        self.assertNotIn("AWS::S3::Bucket\n", text)
        self.assertNotIn("AWS::DynamoDB::Table", text)
        self.assertNotIn("AWS::ApiGateway", text)
        self.assertNotIn("AWS::WAF", text)
        self.assertEqual(len(re.findall(r"Type: AWS::Lambda::Function", text)), 1)


if __name__ == "__main__":
    unittest.main()
