from __future__ import annotations

import json
import unittest

from legaldesk_agent.trace_evidence import (
    HarnessEvidenceCollector,
    normalize_harness_evidence,
)


class HarnessTraceEvidenceTests(unittest.TestCase):
    def test_cross_matter_requires_explicit_deny_code_and_target_not_invoked(self) -> None:
        evidence = normalize_harness_evidence(
            {
                "authorizationDecision": "DENY",
                "authorizationCode": "CROSS_MATTER",
                "targetInvoked": False,
                "toolName": "mcp.list_matter_documents",
                "requestId": "provider-request-id",
                "body": "must never be retained",
            },
            source_name="interceptor-local",
        )
        self.assertTrue(evidence.accepted_cross_matter_deny)
        self.assertTrue(evidence.structured)
        encoded = json.dumps(evidence.to_dict())
        self.assertNotIn("must never be retained", encoded)

    def test_text_only_harness_result_is_inconclusive(self) -> None:
        evidence = normalize_harness_evidence(
            {"text": "DENY CROSS_MATTER targetInvoked=false"},
        )
        self.assertFalse(evidence.structured)
        self.assertFalse(evidence.accepted_cross_matter_deny)

    def test_collector_accepts_allowlisted_metadata_from_multiple_layers(self) -> None:
        collector = HarnessEvidenceCollector(source_name="test-double")
        collector.add({"authorizationDecision": "DENY", "authorizationCode": "CROSS_MATTER"})
        collector.add({"targetInvoked": False, "traceId": "trace"})
        evidence = collector.normalize()
        self.assertTrue(evidence.accepted_cross_matter_deny)
        self.assertTrue(evidence.trace_id_present)


if __name__ == "__main__":
    unittest.main()
