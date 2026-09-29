from __future__ import annotations

import json
import unittest

from phase14_public_smoke import (
    CallBudget,
    PublicSmokeConfig,
    discover_subject_state,
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

    def test_parser_retains_only_safe_chat_diagnostics_and_last_progress(self) -> None:
        failed = {
            "result": "FAIL", "smoke": "phase14-public-browser", "phase": "chat_citation",
            "step": "chat_citation", "category": "chat_contract", "errorType": "SmokeFailure",
            "diagnostics": {"chat": {"operationStatus": "ok", "evidenceStatus": "answerable", "citationCount": 2, "answerContainsExpected": True}},
        }
        self.assertEqual(parse_browser_report(json.dumps(failed)), failed)
        progress = "\n".join((
            json.dumps({"smoke": "phase14-public-browser-progress", "phase": "login"}),
            json.dumps({"smoke": "phase14-public-browser-progress", "phase": "chat_citation"}),
        ))
        parsed = parse_browser_report(progress)
        self.assertEqual(parsed["category"], "browser_report_invalid")
        self.assertEqual(parsed["step"], "chat_citation")

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

    def test_subject_state_discovery_is_bounded_but_can_cross_pages(self) -> None:
        class Table:
            def __init__(self) -> None:
                self.calls = 0
                self.assertions: list[dict[str, object]] = []

            def scan(self, **kwargs: object) -> dict[str, object]:
                self.calls += 1
                self.assertions.append(kwargs)
                if self.calls == 1:
                    return {"Items": [], "ScannedCount": 100, "LastEvaluatedKey": {"pk": "next", "sk": "next"}}
                return {"Items": [{"pk": "LEGALDESK#P14#STATE#owned", "sk": "RECORD"}], "ScannedCount": 2}

        table = Table()
        result = discover_subject_state(table, "subject-1", budget=CallBudget(), max_scanned=200)
        self.assertEqual(result, {("LEGALDESK#P14#STATE#owned", "RECORD")})
        self.assertEqual(table.calls, 2)
        self.assertIn("ExclusiveStartKey", table.assertions[-1])


if __name__ == "__main__":
    unittest.main()
