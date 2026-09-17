from __future__ import annotations

import re
import unittest
from pathlib import Path


TEMPLATE = Path(__file__).parents[1] / "infra" / "cloudformation" / "phase-08-gateway-mcp.yaml"


class Phase08GatewayInfrastructureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.template = TEMPLATE.read_text(encoding="utf-8")

    def test_gateway_has_lambda_and_remote_mcp_targets(self) -> None:
        self.assertIn("Type: AWS::BedrockAgentCore::Gateway", self.template)
        self.assertEqual(self.template.count("Type: AWS::BedrockAgentCore::GatewayTarget"), 2)
        self.assertIn("Name: review-task-lambda", self.template)
        self.assertIn("Name: metadata-mcp", self.template)
        self.assertIn("Handler: legaldesk.mcp_server.mcp_lambda_handler", self.template)
        self.assertIn("AuthType: AWS_IAM", self.template)
        self.assertIn("AuthorizerType: CUSTOM_JWT", self.template)
        self.assertIn("DiscoveryUrl: !Ref GatewayJwtDiscoveryUrl", self.template)
        self.assertIn("InterceptionPoints:", self.template)
        self.assertIn("PassRequestHeaders: true", self.template)
        self.assertIn("CredentialProviderType: GATEWAY_IAM_ROLE", self.template)
        self.assertIn("_legaldeskGrantId:", self.template)
        self.assertIn("ListingMode: DYNAMIC", self.template)
        review_target = self.template.split("  ReviewTaskGatewayTarget:", 1)[1].split(
            "  MetadataMcpGatewayTarget:", 1
        )[0]
        self.assertIn("CredentialProviderType: GATEWAY_IAM_ROLE", review_target)
        self.assertNotIn("IamCredentialProvider:", review_target)
        metadata_target = self.template.split("  MetadataMcpGatewayTarget:", 1)[1]
        self.assertIn("IamCredentialProvider:", metadata_target)
        self.assertIn("Service: lambda", metadata_target)

    def test_targets_have_narrow_schemas_and_untrusted_matter_selector(self) -> None:
        # matterId is intentionally public as an untrusted selector so callers
        # such as Harness can choose a matter. The REQUEST interceptor must
        # authorize it and strip it before either target receives the call.
        self.assertIn("                    matterId:", self.template)
        for forbidden in ("tenantId", "userId"):
            self.assertNotIn(f"                    {forbidden}:", self.template)
        self.assertIn("Name: create_review_task", self.template)
        self.assertIn("metadata-mcp", self.template)
        review_target = self.template.split("  ReviewTaskGatewayTarget:", 1)[1].split(
            "  MetadataMcpGatewayTarget:", 1
        )[0]
        self.assertNotIn("idempotencyKey:", review_target)
        for reason_code in (
            "insufficient_evidence",
            "ambiguous_evidence",
            "material_legal_judgment",
            "user_requested_review",
            "safety_escalation",
        ):
            self.assertIn(reason_code, self.template)
        self.assertNotIn("                    Enum:", self.template)

    def test_roles_are_scoped_to_existing_table_and_functions(self) -> None:
        self.assertIn("ExistingMetadataTableArn", self.template)
        self.assertIn("lambda:InvokeFunction", self.template)
        self.assertIn("lambda:InvokeFunctionUrl", self.template)
        self.assertIn("lambda:InvokedViaFunctionUrl", self.template)
        self.assertIn("GatewayRequestInterceptorFunction", self.template)
        self.assertIn("dynamodb:PutItem", self.template)
        self.assertIn("dynamodb:LeadingKeys:", self.template)
        self.assertIn("GATEWAY#GRANT#*", self.template)
        self.assertNotIn("AWS::Lambda::Permission", self.template)
        self.assertIn("aws:SourceAccount: !Ref AWS::AccountId", self.template)
        self.assertIn("bedrock-agentcore:${AWS::Region}:${AWS::AccountId}:gateway/*", self.template)
        self.assertIn("Action: bedrock-agentcore:InvokeGateway", self.template)
        self.assertIn("gateway/legaldeskgatewayphase08-*", self.template)
        self.assertNotRegex(self.template, r"(?m)^\s+Action: ['\"]\*['\"]$")
        self.assertNotRegex(self.template, r"(?m)^\s+Resource: ['\"]\*['\"]$")
        self.assertNotIn("AWS::DynamoDB::Table", self.template)
        self.assertIn("AllowedRequestHeaders:", self.template)
        self.assertIn("x-legaldesk-verified-subject", self.template)
        self.assertIn("x-legaldesk-requested-matter-id", self.template)
        self.assertIn("x-legaldesk-correlation-id", self.template)

    def test_lambda_execution_role_trust_uses_standard_service_principal(self) -> None:
        before_gateway_role = self.template.split("  GatewayExecutionRole:", 1)[0]
        self.assertEqual(before_gateway_role.count("Service: lambda.amazonaws.com"), 2)
        self.assertNotIn("aws:SourceAccount", before_gateway_role)

    def test_interceptor_and_authorizer_parameters_are_reproducible(self) -> None:
        for parameter in (
            "InterceptorCodeBucket",
            "InterceptorCodeKey",
            "InterceptorCodeVersion",
            "GatewayJwtDiscoveryUrl",
            "GatewayJwtClientId",
            "GatewayJwtScope",
        ):
            self.assertIn(f"  {parameter}:", self.template)

    def test_artifacts_and_teardown_are_explicit(self) -> None:
        self.assertIn("MetadataMcpCodeVersion", self.template)
        self.assertIn("S3ObjectVersion: !Ref MetadataMcpCodeVersion", self.template)
        self.assertIn("DeletionPolicy: Delete", self.template)
        self.assertIn("RetentionInDays: 14", self.template)


if __name__ == "__main__":
    unittest.main()
