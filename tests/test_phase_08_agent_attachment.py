from __future__ import annotations

import unittest
from pathlib import Path


ATTACHMENT = Path(__file__).parents[1] / "infra" / "phase-08-agent-attachment.yaml"
HARNESS_TEMPLATE = Path(__file__).parents[1] / "infra" / "cloudformation" / "phase-01-harness.yaml"


class Phase08AgentAttachmentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = ATTACHMENT.read_text(encoding="utf-8")

    def test_updates_existing_harness_and_maps_all_tools(self) -> None:
        self.assertIn("action: update-existing", self.config)
        self.assertIn("type: agentcore_gateway", self.config)
        self.assertIn("config:", self.config)
        self.assertIn("agentCoreGateway:", self.config)
        self.assertIn("outboundAuth:", self.config)
        self.assertIn("providerArn: \"${OAUTH_PROVIDER_ARN}\"", self.config)
        self.assertIn("list_matter_documents: mcp", self.config)
        self.assertIn("get_document_metadata: mcp", self.config)
        self.assertIn("create_review_task: lambda", self.config)
        self.assertNotIn("AWS::BedrockAgentCore::AgentRuntime", self.config)
        self.assertNotIn("EXISTING_HARNESS_ARN: arn", self.config)

    def test_oauth_is_nested_inside_agentcore_gateway_config(self) -> None:
        expected = """        agentCoreGateway:
          gatewayArn: "${PHASE08_GATEWAY_ARN}"
          outboundAuth:
            oauth:
              providerArn: "${OAUTH_PROVIDER_ARN}"
"""
        self.assertIn(expected, self.config)

    def test_uses_scoped_gateway_permission_without_secret_value(self) -> None:
        self.assertIn("bedrock-agentcore:InvokeGateway", self.config)
        self.assertIn('resource: "${PHASE08_GATEWAY_ARN}"', self.config)
        self.assertIn("scopes:", self.config)
        self.assertNotIn("clientSecret", self.config)
        self.assertNotIn("OAUTH_SECRET", self.config)

    def test_reproducible_harness_template_can_attach_phase_08(self) -> None:
        template = HARNESS_TEMPLATE.read_text(encoding="utf-8")
        self.assertIn("EnablePhase08Gateway", template)
        self.assertIn("Action: bedrock-agentcore:InvokeGateway", template)
        self.assertIn("Action: bedrock-agentcore:GetResourceOauth2Token", template)
        self.assertIn("Action: secretsmanager:GetSecretValue", template)
        self.assertIn("bedrock-agentcore-identity!default/oauth2/${Phase08OAuthProviderName}-*", template)
        self.assertIn("Resource: !Ref Phase08GatewayArn", template)
        self.assertIn("ProviderArn: !Ref Phase08OAuthProviderArn", template)
        self.assertIn("GrantType: CLIENT_CREDENTIALS", template)
        self.assertIn('"@legaldesk_gateway/metadata-mcp___list_matter_documents"', template)
        self.assertIn('"@legaldesk_gateway/metadata-mcp___get_document_metadata"', template)
        self.assertIn('"@legaldesk_gateway/review-task-lambda___create_review_task"', template)
        self.assertNotIn("clientSecret", template)


if __name__ == "__main__":
    unittest.main()
