"""Phase 15 T4.6: agent chat queue, DLQ and worker in the public-edge template (gated, off by default)."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

TEMPLATE = Path(__file__).parents[1] / "infra" / "cloudformation" / "phase-14-public-edge.yaml"
AGENT_RESOURCES = (
    "AgentChatDeadLetterQueue", "AgentChatQueue", "AgentChatWorkerLogGroup", "AgentChatWorkerExecutionRole",
    "AgentChatWorkerFunction", "AgentChatEventSourceMapping", "AgentChatDeadLetterQueueAlarm",
)


class AgentChatInfrastructureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.template = TEMPLATE.read_text(encoding="utf-8")
        cls.parameters = cls.template.split("\nConditions:", 1)[0].split("\nParameters:", 1)[1]

    def block(self, resource: str) -> str:
        body = self.template.split(f"\n  {resource}:\n", 1)[1]
        return re.split(r"(?m)^  [A-Za-z][A-Za-z0-9]*:\s*$", body, maxsplit=1)[0]

    def parameter(self, name: str) -> str:
        body = self.parameters.split(f"\n  {name}:\n", 1)[1]
        return re.split(r"(?m)^  [A-Za-z][A-Za-z0-9]*:\s*$", body, maxsplit=1)[0]

    def test_everything_is_behind_a_flag_that_is_off_by_default(self) -> None:
        self.assertIn('Default: "false"', self.parameter("EnableAgentChat"))
        self.assertIn("AgentChatEnabled: !Equals [!Ref EnableAgentChat, \"true\"]", self.template)
        for resource in AGENT_RESOURCES:
            self.assertIn("Condition: AgentChatEnabled", self.block(resource), resource)
        # The API only learns the queue when the flag is on; otherwise the code path stays synchronous.
        app = self.block("ApplicationFunction")
        self.assertIn("LEGALDESK_AGENT_CHAT_ENABLED: !Ref EnableAgentChat", app)
        self.assertIn('LEGALDESK_AGENT_CHAT_QUEUE_URL: !If [AgentChatEnabled, !Ref AgentChatQueue, ""]', app)
        # No new required input for existing stacks.
        for name in ("EnableAgentChat", "AgentChatMaxConcurrency", "AgentChatAlarmTopicArn"):
            self.assertIn("Default:", self.parameter(name), name)

    def test_queue_matches_adr_021_reliability_numbers(self) -> None:
        queue = self.block("AgentChatQueue")
        self.assertIn("VisibilityTimeout: 900", queue)
        self.assertIn("maxReceiveCount: 2", queue)
        self.assertIn("deadLetterTargetArn: !GetAtt AgentChatDeadLetterQueue.Arn", queue)
        self.assertIn("SqsManagedSseEnabled: true", queue)
        self.assertIn("SqsManagedSseEnabled: true", self.block("AgentChatDeadLetterQueue"))

    def test_worker_timeout_and_visibility_are_consistent(self) -> None:
        function = self.block("AgentChatWorkerFunction")
        timeout = int(re.search(r"(?m)^\s+Timeout: (\d+)$", function).group(1))
        visibility = int(re.search(r"VisibilityTimeout: (\d+)", self.block("AgentChatQueue")).group(1))
        self.assertEqual(timeout, 150)
        self.assertGreaterEqual(visibility, 6 * timeout)  # AWS guidance for Lambda event sources
        self.assertIn("Handler: legaldesk.agent_chat_lambda.lambda_handler", function)
        self.assertIn('AWS_MAX_ATTEMPTS: "1"', function)
        self.assertIn("LEGALDESK_AGENT_PROMPT_PATH: /var/task/prompts/legaldesk-agent.md", function)

    def test_concurrency_is_bounded_by_the_mapping_never_by_reserved_concurrency(self) -> None:
        self.assertNotIn("ReservedConcurrentExecutions:", self.block("AgentChatWorkerFunction"))
        mapping = self.block("AgentChatEventSourceMapping")
        self.assertIn("MaximumConcurrency: !Ref AgentChatMaxConcurrency", mapping)
        self.assertIn("BatchSize: 1", mapping)
        self.assertIn("- ReportBatchItemFailures", mapping)
        parameter = self.parameter("AgentChatMaxConcurrency")
        self.assertIn("Default: 2", parameter)
        self.assertIn("MinValue: 2", parameter)  # SQS minimum for ScalingConfig

    def test_worker_role_is_least_privilege(self) -> None:
        role = self.block("AgentChatWorkerExecutionRole")
        flat = set(re.findall(r"(?<![A-Za-z-])(?:sqs|dynamodb|logs|s3|bedrock-agentcore|bedrock):[A-Za-z*]+", role)) - {"dynamodb:LeadingKeys"}
        self.assertEqual(flat, {
            "logs:CreateLogStream", "logs:PutLogEvents", "sqs:ReceiveMessage", "sqs:DeleteMessage",
            "sqs:GetQueueAttributes", "sqs:ChangeMessageVisibility", "dynamodb:GetItem", "dynamodb:Query",
            "dynamodb:PutItem", "dynamodb:UpdateItem", "s3:GetObject", "bedrock:Retrieve", "bedrock:ApplyGuardrail",
            "bedrock:InvokeModel", "bedrock-agentcore:InvokeHarness", "bedrock-agentcore:InvokeAgentRuntime",
            "bedrock-agentcore:CreateEvent", "bedrock-agentcore:ListEvents",
        })
        for forbidden in ("DeleteItem", "Scan", "PutObject", "DeleteObject", "lambda:", "sqs:SendMessage",
                          "AgentRuntimeArn", "iam:", "StartIngestionJob", "dynamodb:*"):
            self.assertNotIn(forbidden, role)
        self.assertNotRegex(role, r"(?m)^\s+Resource: ['\"]?\*['\"]?$")
        self.assertIn("Resource: !Ref HarnessArn", role)
        self.assertIn("Resource: !GetAtt AgentChatQueue.Arn", role)
        # Table access is limited to the key prefixes the worker actually uses, for the beta tenant only.
        self.assertIn('!Sub "LEGALDESK#P15#CHATJOB#${BetaTenantId}#MATTER#*"', role)
        self.assertIn("dynamodb:LeadingKeys", role)

    def test_worker_cannot_write_authorization_or_conversation_bindings(self) -> None:
        role = self.block("AgentChatWorkerExecutionRole")
        write = role.split("Sid: WriteJobGrantCitationHistoryAndQuotaState", 1)[1].split("- Sid:", 1)[0]
        for prefix in ("AUTH#", "SESSION#", "CONVERSATION#", "TENANT#"):
            self.assertNotRegex(write, rf'(?m)^\s+- (?:!Sub ")?{re.escape(prefix)}', prefix)

    def test_edge_secret_is_not_copied_into_the_worker(self) -> None:
        function = self.block("AgentChatWorkerFunction")
        self.assertNotIn("!Ref TrustedEdgeSecret", function)
        self.assertIn("LEGALDESK_TRUSTED_EDGE_VALUE: not-used-by-the-worker", function)

    def test_dlq_has_an_alarm_that_notifies_when_a_topic_is_given(self) -> None:
        alarm = self.block("AgentChatDeadLetterQueueAlarm")
        self.assertIn("MetricName: ApproximateNumberOfMessagesVisible", alarm)
        self.assertIn("Value: !GetAtt AgentChatDeadLetterQueue.QueueName", alarm)
        self.assertIn("Threshold: 1", alarm)
        self.assertIn("AlarmActions: !If", alarm)

    def test_api_role_may_only_publish_to_the_queue_and_only_when_enabled(self) -> None:
        role = self.block("ApplicationExecutionRole")
        statement = role.split("Sid: PublishAgentChatJobIds", 1)[1].split("\n\n", 1)[0]
        self.assertIn("Action: sqs:SendMessage", statement)
        self.assertIn("Resource: !GetAtt AgentChatQueue.Arn", statement)
        self.assertIn("- AgentChatEnabled", role.split("Sid: PublishAgentChatJobIds", 1)[0].rsplit("- !If", 1)[1])
        self.assertEqual(role.count('!Sub "LEGALDESK#P15#CHATJOB#${BetaTenantId}#MATTER#*"'), 2)

    def test_harness_default_quota_is_300(self) -> None:
        self.assertIn("Default: 300", self.parameter("QuotaHarnessPerMonth"))

    def test_packaged_artifact_contains_the_worker_module_and_prompt(self) -> None:
        script = (Path(__file__).parents[1] / "scripts" / "package_release.py").read_text(encoding="utf-8")
        self.assertIn("prompts/legaldesk-agent.md", script)
        self.assertTrue((Path(__file__).parents[1] / "backend" / "src" / "legaldesk" / "agent_chat_lambda.py").is_file())


if __name__ == "__main__":
    unittest.main()
