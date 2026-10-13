"""Machine review dispatch must fit inside the reconciliation invocation."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend" / "src"))

from legaldesk import reconciliation_lambda as module  # noqa: E402


class ReviewDispatchBudgetTests(unittest.TestCase):
    def tearDown(self) -> None:
        module._remaining_time_ms = None

    def test_timeout_covers_the_measured_cold_chain(self) -> None:
        # Live cold chain on 2026-10-13: interceptor ~8 s + Review Lambda ~7.9 s.
        self.assertGreater(module.MACHINE_REVIEW_TIMEOUT_SECONDS, 16.0)
        self.assertLess(module.MACHINE_REVIEW_TIMEOUT_SECONDS + module._REVIEW_DISPATCH_MARGIN_SECONDS, 120.0)

    def test_dispatch_stops_when_the_invocation_cannot_fit_another_call(self) -> None:
        module._remaining_time_ms = lambda: 120_000
        self.assertTrue(module._review_dispatch_has_budget())
        module._remaining_time_ms = lambda: (module.MACHINE_REVIEW_TIMEOUT_SECONDS + module._REVIEW_DISPATCH_MARGIN_SECONDS) * 1000 - 1
        self.assertFalse(module._review_dispatch_has_budget())

    def test_unreadable_context_fails_closed_and_absent_context_is_unbounded(self) -> None:
        def broken() -> int:
            raise RuntimeError("context unavailable")
        module._remaining_time_ms = broken
        self.assertFalse(module._review_dispatch_has_budget())
        module._remaining_time_ms = None
        self.assertTrue(module._review_dispatch_has_budget())


if __name__ == "__main__":
    unittest.main()
