from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
TEMPLATES = {
    "review": ROOT / "infra/cloudformation/phase-07-review-task.yaml",
    "gateway": ROOT / "infra/cloudformation/phase-08-gateway-mcp.yaml",
    "public": ROOT / "infra/cloudformation/phase-14-public-edge.yaml",
}


class Phase14IDPArtifactInfrastructureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = {name: path.read_text(encoding="utf-8") for name, path in TEMPLATES.items()}

    def test_each_existing_runtime_has_explicit_artifact_bucket_parameter(self) -> None:
        for name, text in self.text.items():
            self.assertIn("IDPArtifactBucketName:", text, name)
            self.assertRegex(text, r"IDPArtifactBucketName:\n\s+Type: String\n\s+Default: \"\"", name)

    def test_mcp_role_reads_canonical_source_and_idp_artifacts_only(self) -> None:
        text = self.text["gateway"]
        role = text.split("  MetadataMcpExecutionRole:", 1)[1].split("  GatewayRequestInterceptorLogGroup:", 1)[0]
        self.assertIn("IDP_TABLE_NAME: !Ref ExistingMetadataTableName", text)
        self.assertIn("LEGALDESK_SOURCE_BUCKET: !Ref IDPSourceBucketName", text)
        self.assertIn("LEGALDESK_IDP_ARTIFACT_BUCKET: !Ref IDPArtifactBucketName", text)
        self.assertIn("Action: s3:GetObject", role)
        self.assertIn("/tenants/${TenantId}/matters/${MatterId}/documents/*/original.pdf", role)
        self.assertIn("/tenants/${TenantId}/matters/${MatterId}/documents/*/original.txt", role)
        self.assertIn("/idp-artifacts/tenant=${TenantId}/matter=${MatterId}/document=*/run=*/pages-*", role)
        self.assertNotIn("s3:HeadObject", role)
        self.assertNotIn("s3:PutObject", role)
        self.assertNotIn("s3:DeleteObject", role)

    def test_review_role_reads_artifact_validation_objects_without_writes(self) -> None:
        text = self.text["review"]
        role = text.split("  ReviewTaskExecutionRole:", 1)[1].split("  ReviewTaskFunction:", 1)[0]
        self.assertIn("LEGALDESK_IDP_ARTIFACT_BUCKET: !Ref IDPArtifactBucketName", text)
        self.assertIn("/idp-artifacts/tenant=${TenantId}/matter=${MatterId}/document=*/run=*/pages-*", role)
        self.assertEqual(role.count("Action: s3:GetObject"), 1)
        self.assertNotRegex(role, r"s3:(?:PutObject|DeleteObject|HeadObject)")

    def test_public_application_is_tenant_bounded_and_artifact_read_only(self) -> None:
        text = self.text["public"]
        role = text.split("  ApplicationExecutionRole:", 1)[1].split("  ApplicationFunction:", 1)[0]
        self.assertIn("LEGALDESK_IDP_ARTIFACT_BUCKET: !Ref IDPArtifactBucketName", text)
        self.assertIn("Action: s3:GetObject", role)
        self.assertIn("idp-artifacts/tenant=${BetaTenantId}/matter=*/document=*/run=*/pages-*", role)
        self.assertNotIn("idp-artifacts/tenant=*", role)
        self.assertNotRegex(role, r"s3:(?:PutObject|DeleteObject|HeadObject).*idp-artifacts")
        self.assertNotIn("s3:ListBucket", role)

    def test_no_new_resource_or_broad_identity_permission(self) -> None:
        for name, text in self.text.items():
            if name != "public":
                self.assertNotIn("AWS::S3::Bucket", text, name)
            self.assertNotRegex(text, r"(?m)^\s+Action:\s+['\"]\*['\"]$", name)
            self.assertNotRegex(text, r"(?m)^\s+Resource:\s+['\"]\*['\"]$", name)


if __name__ == "__main__":
    unittest.main()
