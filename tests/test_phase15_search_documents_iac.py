"""Phase 15 T2: search-documents Lambda and Gateway target in the Phase 08 template."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

TEMPLATE = Path(__file__).parents[1] / "infra" / "cloudformation" / "phase-08-gateway-mcp.yaml"
SEARCH_RESOURCES = (
    "SearchDocumentsLogGroup",
    "SearchDocumentsExecutionRole",
    "SearchDocumentsFunction",
    "SearchDocumentsGatewayTarget",
)
PHASE15_PARAMETERS = (
    "SearchDocumentsCodeBucket", "SearchDocumentsCodeKey", "SearchDocumentsCodeVersion", "SearchTenantId",
    "SourceBucketArn", "KnowledgeBaseId", "KnowledgeBaseArn", "GuardrailIdentifier", "GuardrailVersion",
    "GuardrailArn", "ResolverModelArn", "WriterModelArn", "ResolverFoundationModelArn", "WriterFoundationModelArn",
)


class SearchDocumentsInfrastructureTests(unittest.TestCase):
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

    def test_feature_is_off_by_default_and_needs_no_new_input_for_existing_stacks(self) -> None:
        self.assertIn('Default: "false"', self.parameter("EnableSearchDocuments"))
        for name in PHASE15_PARAMETERS:
            self.assertIn('Default: ""', self.parameter(name), name)
        for resource in SEARCH_RESOURCES:
            self.assertIn("Condition: HasSearchDocuments", self.block(resource), resource)

    def test_function_has_the_25_second_budget_and_no_sdk_retries(self) -> None:
        function = self.block("SearchDocumentsFunction")
        self.assertIn("Handler: legaldesk.search_documents_lambda.search_documents_gateway_handler", function)
        self.assertIn("Timeout: 25", function)
        self.assertIn('AWS_MAX_ATTEMPTS: "1"', function)
        self.assertIn("AWS_RETRY_MODE: standard", function)
        self.assertIn("S3ObjectVersion: !Ref SearchDocumentsCodeVersion", function)
        self.assertIn("LEGALDESK_SYSTEM_PROMPT_PATH: /var/task/prompts/legaldesk-system.md", function)

    def test_role_is_read_only_and_scoped_to_exact_resources(self) -> None:
        role = self.block("SearchDocumentsExecutionRole")
        # sts:AssumeRole belongs to the trust policy, not to the permissions.
        actions = set(re.findall(r"(?m)^\s+Action: ([a-z0-9]+:[A-Za-z*]+)$", role)) - {"sts:AssumeRole"}
        self.assertEqual(
            actions,
            {"dynamodb:GetItem", "s3:GetObject", "bedrock:Retrieve", "bedrock:ApplyGuardrail", "bedrock:InvokeModel"},
        )
        for forbidden in ("PutItem", "UpdateItem", "DeleteItem", "dynamodb:Query", "Scan", "PutObject",
                          "bedrock-agentcore", "lambda:"):
            self.assertNotIn(forbidden, role)
        self.assertNotRegex(role, r"(?m)^\s+Resource: ['\"]?\*['\"]?$")
        self.assertIn("Resource: !Ref KnowledgeBaseArn", role)
        self.assertIn("Resource: !Ref GuardrailArn", role)
        self.assertIn("Resource: !Ref ExistingMetadataTableArn", role)
        self.assertIn("/tenants/${SearchTenantId}/matters/*/documents/*/original.pdf", role)

    def test_target_hides_the_grant_exposes_no_scope_and_is_invocable_only_by_the_gateway_role(self) -> None:
        target = self.block("SearchDocumentsGatewayTarget")
        self.assertIn("Name: search-documents", target)
        self.assertIn("Name: search_documents", target)
        self.assertIn("CredentialProviderType: GATEWAY_IAM_ROLE", target)
        self.assertIn("_legaldeskGrantId:", target)
        for forbidden in ("tenantId:", "userId:", "subject:", "documentIds:"):
            self.assertNotIn(forbidden, target)
        gateway_role = self.block("GatewayExecutionRole")
        self.assertIn("Resource: !GetAtt SearchDocumentsFunction.Arn", gateway_role)
        self.assertIn("HasSearchDocuments", gateway_role)
        self.assertNotIn("AWS::Lambda::Permission", self.template)


if __name__ == "__main__":
    unittest.main()
