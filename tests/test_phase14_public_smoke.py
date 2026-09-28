from __future__ import annotations

import json
import unittest

from phase14_public_smoke import (
    CallBudget,
    PublicSmokeConfig,
    parse_browser_report,
    safe_report,
    validate_config,
)


class Phase14PublicSmokeTests(unittest.TestCase):
    def config(self, **changes: object) -> PublicSmokeConfig:
        values: dict[str, object] = {
            "base_url": "https://beta.example.com",
            "idp_host": "auth.example.com",
            "matter_id": "matter-a",
            "cross_matter_id": "matter-b",
            "question": "What is the inspection period?",
            "expected_fact": "four years",
            "attempt_id": "public-smoke-01",
        }
        values.update(changes)
        return PublicSmokeConfig(**values)  # type: ignore[arg-type]

    def test_preflight_requires_no_provider_configuration(self) -> None:
        self.assertEqual(validate_config(self.config()), self.config())
        with self.assertRaises(ValueError):
            validate_config(self.config(), execute=True)

    def test_public_origin_is_exact_https_and_matters_are_distinct(self) -> None:
        for value in (
            "http://beta.example.com",
            "https://localhost",
            "https://beta.example.com/path",
            "https://beta.example.com?token=x",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_config(self.config(base_url=value))
        with self.assertRaises(ValueError):
            validate_config(self.config(cross_matter_id="matter-a"))

    def test_parser_accepts_closed_preflight_and_complete_pass_only(self) -> None:
        preflight = {"result": "PASS", "smoke": "phase14-public-browser", "phase": "preflight", "browserExecutable": True}
        self.assertEqual(parse_browser_report(json.dumps(preflight)), preflight)
        incomplete = {"result": "PASS", "smoke": "phase14-public-browser", "matterId": "matter-a"}
        self.assertEqual(parse_browser_report(json.dumps(incomplete))["category"], "browser_report_invalid")
        passed = {
            "result": "PASS", "smoke": "phase14-public-browser", "matterId": "matter-a",
            "indexedDocuments": 1, "citationCount": 1, "reviewsListed": 0,
            "reviewsOpened": 0, "crossMatterStatus": 403, "auditEventCount": 2,
            "logoutStatus": 200, "cleanup": {"conversationId": "conversation-1", "sessionId": "session-1"},
        }
        self.assertEqual(parse_browser_report(json.dumps(passed)), passed)

    def test_safe_report_drops_cleanup_selectors_and_untrusted_fields(self) -> None:
        result = safe_report(
            {"result": "PASS", "smoke": "phase14-public-browser", "matterId": "matter-a", "cleanup": {"sessionId": "secret"}, "answer": "not retained"},
            cleanup_errors=[], request_counts={"POST /api/chat": 1},
        )
        self.assertNotIn("cleanup", result)
        self.assertNotIn("answer", result)
        self.assertEqual(result["requestCounts"], {"POST /api/chat": 1})

    def test_provider_budget_fails_before_an_extra_call(self) -> None:
        calls: list[bool] = []
        budget = CallBudget(maximum=1)
        budget.call("test", lambda: calls.append(True))
        with self.assertRaises(RuntimeError):
            budget.call("test", lambda: calls.append(True))
        self.assertEqual(calls, [True])


if __name__ == "__main__":
    unittest.main()
