from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
TEMPLATE = ROOT / "infra" / "cloudformation" / "phase-14-operations.yaml"
RUNBOOK = ROOT / "docs" / "phase-14-operations-runbook.md"


class Phase14OperationsInfrastructureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.template = TEMPLATE.read_text(encoding="utf-8")
        cls.runbook = RUNBOOK.read_text(encoding="utf-8")

    def test_template_is_alarm_only_and_has_all_required_signals(self) -> None:
        text = self.template
        self.assertEqual(text.count("Type: AWS::CloudWatch::Alarm"), 12)
        self.assertIn("Namespace: AWS/ApiGateway", text)
        self.assertIn("MetricName: 5XXError", text)
        self.assertIn("MetricName: Latency", text)
        self.assertEqual(text.count("Namespace: AWS/Lambda"), 8)
        self.assertIn("MetricName: Errors", text)
        self.assertIn("MetricName: Throttles", text)
        self.assertIn("MetricName: Duration", text)
        self.assertEqual(text.count("Namespace: AWS/SQS"), 2)
        self.assertEqual(text.count("MetricName: ApproximateNumberOfMessagesVisible"), 2)
        self.assertNotIn("Type: AWS::SNS::Topic", text)
        self.assertNotIn("Type: AWS::CloudWatch::Dashboard", text)
        self.assertNotIn("Type: AWS::WAFv2::WebACL", text)

    def test_alarm_defaults_fail_safe_and_topic_is_reused_optionally(self) -> None:
        text = self.template
        self.assertIn("ExistingAlarmTopicArn:", text)
        self.assertIn("HasAlarmTopic: !Not", text)
        self.assertEqual(text.count("TreatMissingData: notBreaching"), 12)
        self.assertEqual(text.count("AlarmActions: !If [HasAlarmTopic"), 12)
        self.assertEqual(text.count("ActionsEnabled: !If [HasAlarmTopic"), 12)
        self.assertNotIn("AlarmActions: [!Ref", text)

    def test_budget_uses_direct_email_and_is_not_a_hard_cap(self) -> None:
        text = self.template
        self.assertIn("Type: AWS::Budgets::Budget", text)
        self.assertIn("BudgetLimit:", text)
        self.assertIn("MonthlyBudgetLimitUsd:", text)
        self.assertIn("BudgetEmail:", text)
        self.assertIn("NoEcho: true", text)
        self.assertEqual(text.count("SubscriptionType: EMAIL"), 2)
        self.assertIn("Threshold: 80", text)
        self.assertIn("Threshold: 100", text)
        self.assertIn("not a billing hard cap", text)

    def test_resource_names_are_parameterized_not_recreated(self) -> None:
        text = self.template
        for parameter in (
            "PublicApiId:",
            "ApplicationFunctionName:",
            "MalwareScanFunctionName:",
            "MalwareScanDeadLetterQueueName:",
            "ReconciliationFunctionName:",
            "ReconciliationDeadLetterQueueName:",
        ):
            self.assertIn(parameter, text)
        self.assertNotRegex(text, r"Type: AWS::(Lambda::Function|SQS::Queue|ApiGatewayV2::Api)")

    def test_runbook_has_slos_fault_plan_release_rollback_and_retention(self) -> None:
        text = self.runbook
        for marker in (
            "## Beta SLOs and alert intent",
            "## Local synthetic-fault exercise",
            "## Release procedure",
            "## Rollback",
            "## Teardown and retained ownership",
            "not a billing hard cap",
            "DeletionPolicy: Retain",
            "Do not use a real legal document",
        ):
            self.assertIn(marker, text)


if __name__ == "__main__":
    unittest.main()
