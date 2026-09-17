from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
TEMPLATE = ROOT / "infra" / "cloudformation" / "phase-07-review-task.yaml"


class Phase07ReviewTaskInfrastructureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.template = TEMPLATE.read_text(encoding="utf-8")

    def test_template_contains_only_function_role_and_scoped_log_group(self) -> None:
        resource_types = re.findall(
            r"^  [A-Za-z][A-Za-z0-9]*:\n    Type: (AWS::[A-Za-z0-9:]+)",
            self.template,
            re.MULTILINE,
        )
        self.assertEqual(
            resource_types,
            ["AWS::Logs::LogGroup", "AWS::IAM::Role", "AWS::Lambda::Function"],
        )
        for forbidden in (
            "AWS::ApiGateway",
            "AWS::Bedrock::Agent",
            "AWS::BedrockAgentCore::Gateway",
            "AWS::SQS::",
            "AWS::SNS::",
        ):
            self.assertNotIn(forbidden, self.template)

    def test_lambda_uses_immutable_s3_artifact_and_safe_operational_limits(self) -> None:
        self.assertIn("Runtime: python3.12", self.template)
        self.assertIn("Handler: legaldesk.review_tasks.lambda_handler", self.template)
        self.assertIn("S3Bucket: !Ref ReviewTaskCodeBucket", self.template)
        self.assertIn("S3Key: !Ref ReviewTaskCodeKey", self.template)
        self.assertIn("S3ObjectVersion: !Ref ReviewTaskCodeVersion", self.template)
        self.assertIn("Timeout: 10", self.template)
        self.assertIn("MemorySize: 256", self.template)
        # Reserved concurrency is applied at deployment time when the account
        # quota permits it; the smoke account's Lambda quota is 10 with the
        # provider-required unreserved floor of 10, so CFN must omit it.
        self.assertIn("REVIEW_TASK_TABLE_NAME: !Ref ReviewTaskTableName", self.template)
        self.assertIn('REVIEW_TASK_SCHEMA_VERSION: "1"', self.template)

    def test_existing_table_is_parameterized_and_only_idempotency_actions_are_allowed(self) -> None:
        self.assertIn("ReviewTaskTableArn:", self.template)
        self.assertIn("ReviewTaskTableName:", self.template)
        self.assertIn("Resource: !Ref ReviewTaskTableArn", self.template)
        dynamodb_actions = re.findall(r"^                Action: dynamodb:([A-Za-z]+)$", self.template, re.MULTILINE)
        self.assertEqual(dynamodb_actions, ["GetItem", "PutItem"])
        self.assertNotIn("dynamodb:Query", self.template)
        self.assertNotIn("dynamodb:UpdateItem", self.template)
        self.assertNotIn("dynamodb:DeleteItem", self.template)

    def test_execution_role_and_logging_are_scoped_without_wildcard_permissions(self) -> None:
        self.assertIn("Service: lambda.amazonaws.com", self.template)
        trust = re.search(
            r"(?ms)^          - Effect: Allow\n(?P<body>.*?)(?=^      Policies:)",
            self.template,
        )
        self.assertIsNotNone(trust)
        self.assertIn("Principal:\n              Service: lambda.amazonaws.com", trust.group("body"))
        self.assertIn("Action: sts:AssumeRole", trust.group("body"))
        self.assertNotIn("aws:SourceAccount", trust.group("body"))
        self.assertNotIn("aws:SourceArn", trust.group("body"))
        self.assertNotRegex(self.template, r"(?m)^\s+Action: ['\"]\*['\"]$")
        self.assertNotRegex(self.template, r"(?m)^\s+Resource: ['\"]\*['\"]$")
        self.assertIn("Resource: !Sub ${ReviewTaskLogGroup.Arn}:*", self.template)
        self.assertIn("logs:CreateLogStream", self.template)
        self.assertIn("logs:PutLogEvents", self.template)
        self.assertNotIn("AWSLambdaBasicExecutionRole", self.template)

    def test_log_retention_and_teardown_are_explicit(self) -> None:
        self.assertIn("RetentionInDays: 14", self.template)
        self.assertIn("DeletionPolicy: Delete", self.template)
        self.assertIn("UpdateReplacePolicy: Delete", self.template)
        self.assertIn("Key: Project", self.template)
        self.assertIn("Key: Phase", self.template)
        self.assertIn('Value: "07"', self.template)


if __name__ == "__main__":
    unittest.main()
