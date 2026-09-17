from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]


class Phase10InfrastructureTests(unittest.TestCase):
    def test_public_client_reuses_pool_and_uses_pkce_without_secret(self) -> None:
        template = (ROOT / "infra" / "cloudformation" / "phase-10-identity.yaml").read_text()
        self.assertIn("AWS::Cognito::UserPoolClient", template)
        self.assertIn("UserPoolId: !Ref UserPoolId", template)
        self.assertIn("GenerateSecret: false", template)
        self.assertIn("AllowedOAuthFlows:\n        - code", template)
        self.assertIn("AllowedOAuthFlowsUserPoolClient: true", template)
        self.assertNotIn("AWS::Cognito::UserPool\n", template)

    def test_gateway_keeps_m2m_and_optionally_adds_public_client(self) -> None:
        template = (ROOT / "infra" / "cloudformation" / "phase-08-gateway-mcp.yaml").read_text()
        self.assertIn("GatewayJwtAdditionalClientId", template)
        self.assertIn("HasAdditionalGatewayClient", template)
        self.assertGreaterEqual(template.count("!Ref GatewayJwtClientId"), 1)

    def test_phase10_does_not_create_a_new_table(self) -> None:
        template = (ROOT / "infra" / "cloudformation" / "phase-10-identity.yaml").read_text()
        self.assertNotIn("AWS::DynamoDB::Table", template)


if __name__ == "__main__":
    unittest.main()
