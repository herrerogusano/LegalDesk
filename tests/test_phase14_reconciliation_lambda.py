from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from legaldesk.reconciliation import ReconciliationReport
from legaldesk.reconciliation_lambda import (
    ReconciliationConfig,
    ReconciliationLambdaError,
    lambda_handler,
    reset_service_for_tests,
)


BASE_ENV = {
    "AWS_REGION": "eu-west-1",
    "LEGALDESK_METADATA_TABLE_NAME": "fictional-table",
    "LEGALDESK_SOURCE_BUCKET": "fictional-bucket",
    "LEGALDESK_KNOWLEDGE_BASE_ID": "kbfictional",
    "LEGALDESK_DATA_SOURCE_ID": "srcfictional",
    "LEGALDESK_RECONCILIATION_BETA_TENANT_ID": "tenant-a",
    "LEGALDESK_RECONCILIATION_UPLOAD_SCOPES": '[{"tenantId":"tenant-a","matterId":"matter-a"}]',
    "LEGALDESK_RECONCILIATION_INGESTION_SCOPES": '[{"subject":"alice","tenantId":"tenant-a","matterId":"matter-a"}]',
    "LEGALDESK_RECONCILIATION_UPLOAD_STALE_SECONDS": "86400",
    "LEGALDESK_RECONCILIATION_INGESTION_STALE_SECONDS": "1800",
    "LEGALDESK_RECONCILIATION_LIMIT_PER_SCOPE": "25",
}


class _FakeService:
    def reconcile_pending_uploads(self, **_kwargs):
        return ReconciliationReport(examined=1, changed=1)

    def reconcile_ingestion_scopes(self, **_kwargs):
        return ReconciliationReport(examined=2, skipped=2)

    def reconcile_gateway_indexed(self, **_kwargs):
        return ReconciliationReport(examined=3, ambiguous=1)


class Phase14ReconciliationLambdaTests(unittest.TestCase):
    def tearDown(self) -> None:
        reset_service_for_tests()

    def test_config_requires_explicit_single_tenant_partitions(self) -> None:
        config = ReconciliationConfig.from_environment(BASE_ENV)
        self.assertEqual(config.beta_tenant_id, "tenant-a")
        self.assertEqual(config.upload_scopes[0].matter_id, "matter-a")
        self.assertEqual(config.ingestion_scopes[0].subject, "alice")

        foreign = dict(BASE_ENV)
        foreign["LEGALDESK_RECONCILIATION_UPLOAD_SCOPES"] = '[{"tenantId":"tenant-b","matterId":"matter-b"}]'
        with self.assertRaises(ReconciliationLambdaError):
            ReconciliationConfig.from_environment(foreign)

    def test_handler_returns_aggregate_evidence_and_never_requires_event_selectors(self) -> None:
        with patch.dict(os.environ, BASE_ENV, clear=False), patch(
            "legaldesk.reconciliation_lambda._build_service", return_value=_FakeService()
        ):
            result = lambda_handler({"source": "aws.events", "detail-type": "Scheduled Event"}, None)
        self.assertEqual(result, {
            "status": "completed",
            "uploads": {"examined": 1, "changed": 1, "skipped": 0, "ambiguous": 0, "failed": 0},
            "ingestion": {"examined": 2, "changed": 0, "skipped": 2, "ambiguous": 0, "failed": 0},
            "gateway": {"examined": 3, "changed": 0, "skipped": 0, "ambiguous": 1, "failed": 0},
        })

    def test_config_rejects_unbounded_or_malformed_scope_input(self) -> None:
        malformed = dict(BASE_ENV)
        malformed["LEGALDESK_RECONCILIATION_INGESTION_SCOPES"] = '[{"subject":"alice","tenantId":"tenant-a"}]'
        with self.assertRaises(ReconciliationLambdaError):
            ReconciliationConfig.from_environment(malformed)

    def test_one_scope_list_may_be_empty_but_both_cannot_be_empty(self) -> None:
        upload_empty = dict(BASE_ENV)
        upload_empty["LEGALDESK_RECONCILIATION_UPLOAD_SCOPES"] = "[]"
        config = ReconciliationConfig.from_environment(upload_empty)
        self.assertEqual(config.upload_scopes, ())
        ingestion_empty = dict(BASE_ENV)
        ingestion_empty["LEGALDESK_RECONCILIATION_INGESTION_SCOPES"] = "[]"
        config = ReconciliationConfig.from_environment(ingestion_empty)
        self.assertEqual(config.ingestion_scopes, ())
        malformed = dict(BASE_ENV)
        malformed["LEGALDESK_RECONCILIATION_UPLOAD_SCOPES"] = "[]"
        malformed["LEGALDESK_RECONCILIATION_INGESTION_SCOPES"] = "[]"
        with self.assertRaises(ReconciliationLambdaError):
            ReconciliationConfig.from_environment(malformed)


if __name__ == "__main__":
    unittest.main()
