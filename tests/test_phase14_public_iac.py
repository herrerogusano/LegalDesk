from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
TEMPLATE = ROOT / "infra" / "cloudformation" / "phase-14-public-edge.yaml"


class Phase14PublicInfrastructureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.template = TEMPLATE.read_text(encoding="utf-8")

    def test_private_frontend_and_safe_access_logging_are_bounded(self) -> None:
        text = self.template
        self.assertIn("Type: AWS::S3::Bucket", text)
        self.assertIn("ObjectOwnership: BucketOwnerEnforced", text)
        self.assertIn("BlockPublicAcls: true", text)
        self.assertIn("BlockPublicPolicy: true", text)
        self.assertIn("Type: AWS::CloudFront::OriginAccessControl", text)
        self.assertIn("SigningBehavior: always", text)
        self.assertIn("SigningProtocol: sigv4", text)
        self.assertNotIn("    Logging:\n", text)
        self.assertIn("Type: AWS::Logs::LogGroup", text)
        self.assertIn("AccessLogSettings:", text)
        self.assertIn('$context.routeKey', text)
        self.assertNotIn('$context.path', text)
        self.assertNotIn('$context.identity.sourceIp', text)
        self.assertIn("AWS:SourceArn: !Sub", text)
        self.assertNotIn("Principal: \"*\"", text)

    def test_exact_public_routes_and_uncached_dynamic_behaviors(self) -> None:
        text = self.template
        for route in (
            "GET /login",
            "GET /callback",
            "POST /logout",
            "ANY /api",
            "ANY /api/{proxy+}",
        ):
            self.assertEqual(len(re.findall(rf"^\s+RouteKey: {re.escape(route)}$", text, re.MULTILINE)), 1)
        self.assertEqual(text.count("PathPattern: /api\n"), 1)
        self.assertEqual(text.count("PathPattern: /api/*"), 1)
        self.assertGreaterEqual(text.count("TargetOriginId: public-http-api"), 4)
        self.assertIn("DefaultTTL: 0", text)
        self.assertIn("CachedMethods: [GET, HEAD, OPTIONS]", text)
        self.assertNotIn('Headers: ["*"]', text)
        self.assertIn("Never forward the viewer Host", text)
        self.assertIn("OriginCustomHeaders:", text)
        self.assertNotIn("            CustomHeaders:", text)
        distribution = text.split("  PublicDistribution:", 1)[1].split("Outputs:", 1)[0]
        self.assertNotIn("FrontendBucketPolicy", distribution.split("Properties:", 1)[0])

    def test_lambda_public_mode_and_trusted_edge_are_explicit(self) -> None:
        text = self.template
        self.assertIn("TrustedEdgeSecret:", text)
        self.assertIn("NoEcho: true", text)
        self.assertIn("Handler: legaldesk.api_gateway.lambda_handler", text)
        self.assertIn("S3ObjectVersion: !Ref ApplicationCodeVersion", text)
        self.assertIn("LEGALDESK_PUBLIC_MODE: \"true\"", text)
        self.assertIn("LEGALDESK_SECURE_COOKIES: \"true\"", text)
        self.assertIn("LEGALDESK_TRUSTED_EDGE_VALUE: !Ref TrustedEdgeSecret", text)
        self.assertIn("LEGALDESK_SYSTEM_PROMPT_PATH: /var/task/prompts/legaldesk-system.md", text)
        self.assertIn("HeaderName: X-LegalDesk-Trusted-Edge", text)
        self.assertIn("Value: !Sub \"${PublicApi}.execute-api", text)
        self.assertIn("Value: !GetAtt PublicDistribution.DomainName", text)

    def test_security_headers_csp_and_throttling_are_not_broad(self) -> None:
        text = self.template
        self.assertIn("Type: AWS::CloudFront::ResponseHeadersPolicy", text)
        for value in (
            "default-src 'self'",
            "object-src 'none'",
            "frame-ancestors 'none'",
            "script-src 'self'",
            "style-src 'self'",
            "img-src 'self' data:",
            "font-src 'self'",
            "connect-src 'self' ${UploadOrigins}",
            "form-action 'self' ${CognitoOrigin}",
            "AccessControlMaxAgeSec: 31536000",
            "ReferrerPolicy: strict-origin-when-cross-origin",
            "Permissions-Policy",
        ):
            self.assertIn(value, text)
        self.assertIn("ThrottlingBurstLimit: !Ref ApiThrottleBurst", text)
        self.assertIn("ThrottlingRateLimit: !Ref ApiThrottleRate", text)
        self.assertIn("TimeoutInMillis: 29000", text)
        self.assertNotIn("connect-src *", text)
        self.assertNotIn("script-src *", text)

    def test_role_reuses_existing_resources_without_scan_or_iam_management(self) -> None:
        text = self.template
        self.assertIn('- !Ref MetadataTableArn', text)
        self.assertIn('${MetadataTableArn}/index/*', text)
        self.assertIn('dynamodb:LeadingKeys:', text)
        self.assertGreaterEqual(text.count('LEGALDESK#P14#QUOTA#TENANT#${BetaTenantId}#MONTH#*'), 2)
        self.assertNotIn('TENANT#*', text)
        self.assertIn("Resource: !Ref KnowledgeBaseArn", text)
        self.assertIn("Resource: !Ref GuardrailArn", text)
        self.assertIn("dynamodb:GetItem", text)
        self.assertIn("dynamodb:Query", text)
        self.assertNotIn("dynamodb:Scan", text)
        self.assertNotIn("iam:Create", text)
        self.assertNotIn("iam:PassRole", text)
        self.assertIn("/quarantine/tenants/${BetaTenantId}/matters/*/documents/*/original.pdf", text)
        self.assertIn("bedrock:StartIngestionJob", text)
        self.assertIn("bedrock:GetIngestionJob", text)
        self.assertIn('ResolverFoundationModelArn', text)
        self.assertIn('WriterFoundationModelArn', text)
        self.assertIn('${BetaTenantId}/matters/*/documents/*/original.pdf', text)

if __name__ == "__main__":
    unittest.main()
