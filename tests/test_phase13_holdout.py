from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path


HOLDOUT_PATH = Path(__file__).parents[1] / "evals" / "phase13_holdout.json"
EXPECTED_SHA256 = "3f66bf0d43c5667a19f8465300571933ffd7d358cb83c37285126c31bd5fe5cf"


class Phase13HoldoutTests(unittest.TestCase):
    def test_holdout_bytes_and_candidate_contract_are_frozen(self) -> None:
        payload = HOLDOUT_PATH.read_bytes()
        self.assertEqual(hashlib.sha256(payload).hexdigest(), EXPECTED_SHA256)
        dataset = json.loads(payload.decode("utf-8"))
        self.assertEqual(
            dataset["purpose"],
            "Independent local contract holdout frozen before provider evaluation and prompt changes.",
        )
        self.assertGreaterEqual(len(dataset["cases"]), 10)
        self.assertTrue(
            all(
                isinstance(case.get("candidateAnswer"), str)
                and isinstance(case.get("candidateVerdict"), str)
                for case in dataset["cases"]
            )
        )
        self.assertIn("reject_role_reversal", {case["candidateVerdict"] for case in dataset["cases"]})
        self.assertIn("reject_invented_citation", {case["candidateVerdict"] for case in dataset["cases"]})


if __name__ == "__main__":
    unittest.main()
