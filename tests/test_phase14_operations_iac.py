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

    def test_template_has_all_required_signals_and_optional_managed_topic(self) -> None:
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
        self.assertIn("Type: AWS::SNS::Topic", text)
        self.assertIn("Condition: HasManagedAlarmTopic", text)
        self.assertIn("Type: AWS::SNS::Subscription", text)
        self.assertNotIn("Type: AWS::CloudWatch::Dashboard", text)
        self.assertNotIn("Type: AWS::WAFv2::WebACL", text)

    def test_alarm_defaults_fail_safe_and_topic_is_reused_optionally(self) -> None:
        text = self.template
        self.assertIn("ExistingAlarmTopicArn:", text)
        self.assertIn('CreateAlarmTopic:', text)
        self.assertIn('Default: "false"', text)
        self.assertIn("HasExistingAlarmTopic: !Not", text)
        self.assertIn("HasManagedAlarmTopic: !Equals", text)
        self.assertIn("HasAlarmTopic: !Or", text)
        self.assertIn("Rules:", text)
        self.assertIn("Choose exactly one alarm topic mode", text)
        self.assertNotIn("AlarmNotificationEmail", text)
        self.assertIn("BudgetEmail:", text)
        self.assertEqual(text.count("TreatMissingData: notBreaching"), 12)
        self.assertEqual(text.count("AlarmActions: !If\n        - HasAlarmTopic"), 12)
        alarm_body = text.split("Outputs:", 1)[0]
        self.assertEqual(
            alarm_body.count("HasExistingAlarmTopic, !Ref ExistingAlarmTopicArn, !Ref AlarmTopic"),
            12,
        )
        self.assertEqual(text.count("ActionsEnabled: !If [HasAlarmTopic"), 12)
        self.assertNotIn("AlarmActions: [!Ref ExistingAlarmTopicArn]", text)

    def test_alarm_topic_rules_validate_parameters_directly(self) -> None:
        rules = self.template.split("Rules:", 1)[1].split("Resources:", 1)[0]
        # Rules must evaluate parameter values directly.  Referencing template
        # Conditions here is not a portable CloudFormation parameter-rule
        # validation pattern.
        self.assertNotIn("!Condition", rules)
        self.assertIn('!Ref ExistingAlarmTopicArn, ""', rules)
        self.assertIn('!Ref CreateAlarmTopic, "true"', rules)
        self.assertNotIn('!Ref CreateAlarmTopic, "false"', rules)
        self.assertNotIn("AlarmNotificationEmail", rules)
        self.assertIn("Choose exactly one alarm topic mode", rules)

    def test_managed_subscription_is_explicit_and_requires_confirmation(self) -> None:
        text = self.template
        self.assertIn("AlarmSubscription:", text)
        self.assertIn("Protocol: email", text)
        self.assertIn("Endpoint: !Ref BudgetEmail", text)
        self.assertIn("TopicArn: !Ref AlarmTopic", text)
        # The shared email is a sensitive deployment parameter and must not be echoed.
        email_block = text.split("BudgetEmail:", 1)[1].split("\n\n", 1)[0]
        self.assertIn("NoEcho: true", email_block)

    def test_managed_topic_policy_is_least_privilege_and_effective_arn_is_output(self) -> None:
        text = self.template
        self.assertIn("AlarmTopicPolicy:", text)
        policy = text.split("AlarmTopicPolicy:", 1)[1].split("ApiFiveHundredAlarm:", 1)[0]
        self.assertIn("Condition: HasManagedAlarmTopic", policy)
        self.assertIn("Type: AWS::SNS::TopicPolicy", policy)
        self.assertIn("Service: cloudwatch.amazonaws.com", policy)
        self.assertIn("Action: sns:Publish", policy)
        self.assertIn("Resource: !Ref AlarmTopic", policy)
        self.assertIn("aws:SourceAccount: !Ref AWS::AccountId", policy)
        self.assertIn(
            'aws:SourceArn: !Sub "arn:${AWS::Partition}:cloudwatch:${AWS::Region}:${AWS::AccountId}:alarm:*"',
            policy,
        )
        self.assertIn("Topics:", policy)
        self.assertIn("- !Ref AlarmTopic", policy)

        output = text.split("Outputs:", 1)[1]
        self.assertIn("EffectiveAlarmTopicArn:", output)
        self.assertIn("Condition: HasAlarmTopic", output)
        self.assertIn(
            "Value: !If [HasExistingAlarmTopic, !Ref ExistingAlarmTopicArn, !Ref AlarmTopic]",
            output,
        )

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
