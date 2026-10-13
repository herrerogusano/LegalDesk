from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from phase14_idp_acceptance_smoke import (  # noqa: E402
    AcceptanceEnvelopeError,
    build_acceptance_report,
)


class Phase14IDPAcceptanceSmokeTests(unittest.TestCase):
    def test_offline_report_is_bounded_and_does_not_claim_deployed_proofs(self) -> None:
        report = build_acceptance_report()
        self.assertEqual(report["awsCalls"], 0)
        self.assertEqual(report["paidCalls"], 0)
        self.assertEqual(report["fixtureCorpus"]["selected"], 6)
        self.assertTrue(all(value["status"] == "NOT_EXECUTED" for value in report["proofs"].values()))
        self.assertEqual(report["evidenceSource"], "none")

    def test_offline_seed_evidence_cannot_be_promoted(self) -> None:
        path = ROOT / "evals" / "results" / "unit-idp-acceptance-seed.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"executionClass": "offline_seed", "documentId": "doc-1", "documentSha256": "a" * 64}), encoding="utf-8")
        try:
            report = build_acceptance_report(evidence_path=path)
            self.assertEqual(report["proofs"]["duplicateDelivery"]["status"], "NOT_EXECUTED")
            self.assertEqual(report["proofs"]["history"]["status"], "NOT_EXECUTED")
        finally:
            path.unlink(missing_ok=True)

    def test_deployed_evidence_requires_same_job_and_paid_ledger(self) -> None:
        path = ROOT / "evals" / "results" / "unit-idp-acceptance-invalid.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "executionClass": "deployed_aws_e2e",
            "documentId": "doc-1",
            "documentSha256": "269548342ef80ae5be475a1d78795c33611e2a6acbe6d801e556d2817e90e2c0",
            "duplicateDelivery": {"jobIdBefore": "job-1", "jobIdAfter": "job-2", "paidCallsBefore": 2, "paidCallsAfter": 3},
        }), encoding="utf-8")
        try:
            with self.assertRaises(AcceptanceEnvelopeError):
                build_acceptance_report(evidence_path=path)
        finally:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
