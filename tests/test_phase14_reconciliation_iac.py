from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
TEMPLATE = ROOT / "infra" / "cloudformation" / "phase-14-reconciliation.yaml"
PHASE02 = ROOT / "infra" / "cloudformation" / "phase-02-document-pipeline.yaml"


class Phase14ReconciliationInfrastructureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.template = TEMPLATE.read_text(encoding="utf-8")
        cls.phase02 = PHASE02.read_text(encoding="utf-8")

    def test_scheduler_is_versioned_bounded_and_retriable(self) -> None:
        text = self.template
        self.assertIn("Handler: legaldesk.reconciliation_lambda.lambda_handler", text)
        self.assertIn("S3ObjectVersion: !Ref ReconciliationCodeVersion", text)
        self.assertIn("ReconciliationReservedConcurrency:", text)
        self.assertIn("Default: 1", text)
        self.assertIn("HasReconciliationReservedConcurrency", text)
        self.assertIn("ReservedConcurrentExecutions: !If", text)
        self.assertIn("MaximumEventAgeInSeconds: 3600", text)
        self.assertIn("MaximumRetryAttempts: 2", text)
        self.assertIn("SqsManagedSseEnabled: true", text)
        self.assertIn("rate(15 minutes)", text)
        self.assertIn("ReconciliationSchedule:", text)
        self.assertEqual(len(re.findall(r"Type: AWS::Lambda::Function", text)), 1)

    def test_scope_configuration_and_least_privilege_are_explicit(self) -> None:
        text = self.template
        for value in (
            "UploadScopes:",
            "IngestionScopes:",
            "BetaTenantId:",
            "LEGALDESK_RECONCILIATION_UPLOAD_SCOPES",
            "LEGALDESK_RECONCILIATION_INGESTION_SCOPES",
            "dynamodb:LeadingKeys:",
            '"LEGALDESK#P14#STATE#*"',
            '"GATEWAY#EXPIRY#*"',
            "bedrock:GetIngestionJob",
            "DeleteOnlyBetaQuarantineObjects",
        ):
            self.assertIn(value, text)
        self.assertNotIn("dynamodb:Scan", text)
        self.assertNotIn("s3:ListBucket", text)
        self.assertNotIn('Resource: "*"', text)
        self.assertNotIn("bedrock:StartIngestionJob", text)

    def test_phase02_enables_ttl_and_quarantine_retention_without_versioning(self) -> None:
        text = self.phase02
        self.assertIn("TimeToLiveSpecification:", text)
        self.assertIn("AttributeName: ttl", text)
        self.assertIn("PointInTimeRecoverySpecification:", text)
        self.assertIn("PointInTimeRecoveryEnabled: true", text)
        self.assertIn("Id: ExpireQuarantineUploads", text)
        self.assertIn("Prefix: quarantine/", text)
        self.assertIn("ExpirationInDays: 1", text)
        self.assertIn("DeletionPolicy: Retain", text)
        self.assertNotIn("VersioningConfiguration:", text)


if __name__ == "__main__":
    unittest.main()
