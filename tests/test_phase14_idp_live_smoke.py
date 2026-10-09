from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import subprocess
import unittest
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from phase14_idp_live_smoke import (  # noqa: E402
    IDPLiveSmokeConfig,
    LiveIDPPreflightError,
    build_evaluation_export,
    parse_safe_child_progress,
    preflight,
    validate_browser_report,
    validate_limits,
)


FIXTURE = ROOT / "tests" / "fixtures" / "idp" / "pdfs" / "contract-01-en-digital-monthend.pdf"
SMOKE_FIXTURE = ROOT / "tests" / "fixtures" / "idp" / "smoke" / "fictional-notice-30-days.txt"


def config(**overrides: object) -> IDPLiveSmokeConfig:
    values: dict[str, object] = {
        "base_url": "",
        "idp_host": "",
        "matter_id": "matter-synthetic",
        "cross_matter_id": "matter-other",
        "user_pool_id": "",
        "table_name": "",
        "memory_id": "",
        "fixture_path": FIXTURE,
        "fixture_id": "contract-01-en-digital-monthend",
    }
    values.update(overrides)
    return IDPLiveSmokeConfig(**values)


class Phase14IDPLiveSmokeTests(unittest.TestCase):
    def test_offline_preflight_is_synthetic_and_marks_unavailable_proofs(self) -> None:
        report = preflight(config())
        self.assertEqual(report["awsCalls"], 0)
        self.assertEqual(report["mode"], "preflight")
        self.assertTrue(report["fixture"]["sha256"] == hashlib.sha256(FIXTURE.read_bytes()).hexdigest())
        self.assertEqual(report["fixtureCorpus"]["wholePdfOcrPages"], 22)
        self.assertEqual(len(report["cases"]), 18)
        self.assertEqual(report["workerBudget"]["status"], "NOT_EXECUTED")
        self.assertEqual(report["history"]["status"], "NOT_EXECUTED")
        self.assertEqual(report["idempotencyReplay"]["status"], "NOT_EXECUTED")

    def test_preflight_rejects_fixture_outside_allowlisted_corpus(self) -> None:
        with self.assertRaises(LiveIDPPreflightError):
            preflight(config(fixture_path=ROOT / "README.md"))

    def test_txt_smoke_is_allowlisted_skip_without_changing_pdf_corpus(self) -> None:
        report = preflight(config(fixture_id="smoke-fictional-notice-30-days", fixture_path=SMOKE_FIXTURE))
        self.assertEqual(report["awsCalls"], 0)
        self.assertEqual(report["expectedStatus"], "IDP_SKIPPED")
        self.assertEqual(report["expectedSource"], "RAG")
        self.assertEqual(report["fixture"]["skip_reason"], "UNSUPPORTED_MEDIA_TYPE")
        self.assertEqual(report["fixtureCorpus"]["count"], 18)
        self.assertEqual(len(report["cases"]), 18)

    def test_txt_smoke_rejects_hash_tampering(self) -> None:
        with patch.object(Path, "read_bytes", return_value=b"tampered synthetic notice"):
            with self.assertRaises(LiveIDPPreflightError) as raised:
                preflight(config(fixture_id="smoke-fictional-notice-30-days", fixture_path=SMOKE_FIXTURE))
        self.assertEqual(str(raised.exception), "fixture_hash_mismatch")

    def test_txt_smoke_rejects_path_outside_its_allowlist(self) -> None:
        with self.assertRaises(LiveIDPPreflightError) as raised:
            preflight(config(fixture_id="smoke-fictional-notice-30-days", fixture_path=FIXTURE))
        self.assertEqual(str(raised.exception), "fixture_path_out_of_scope")

    def test_execute_preflight_requires_deployment_inputs(self) -> None:
        with self.assertRaises(LiveIDPPreflightError):
            preflight(config(execute=True))

    def test_main_returns_failure_for_failed_live_report(self) -> None:
        with patch("phase14_idp_live_smoke.run_live", return_value={"result": "FAIL", "category": "cleanup_failed"}):
            from phase14_idp_live_smoke import main
            result = main([
                "--execute-approved-once", "--matter", "matter-synthetic", "--cross-matter", "matter-other",
                "--fixture-id", "contract-01-en-digital-monthend", "--fixture-path", str(FIXTURE),
                "--base-url", "https://legaldesk.example", "--idp-host", "auth.example",
                "--user-pool-id", "pool", "--table-name", "table", "--memory-id", "memory",
                "--field-name", "effective_date", "--report-path", str(ROOT / "evals" / "results" / "unit-failure.json"),
            ])
        self.assertEqual(result, 1)

    def test_timeout_progress_preserves_all_safe_scopes(self) -> None:
        report = parse_safe_child_progress(
            '{"smoke":"phase14-idp-browser-progress","phase":"review_conversation_ready",'
            '"documentId":"doc-1","scopes":[{"conversationId":"conv-1","sessionId":"sess-1"},'
            '{"conversationId":"conv-2","sessionId":"sess-2"}],"secret":"drop"}\n'
        )
        self.assertEqual(report["cleanup"]["documentId"], "doc-1")
        self.assertEqual(len(report["cleanup"]["scopes"]), 2)
        self.assertNotIn("secret", str(report))

    def test_child_timeout_runs_scoped_cleanup_and_cleanup_failure_is_reported(self) -> None:
        class FakeTable:
            def __init__(self) -> None:
                self.deleted: list[dict[str, object]] = []

            def get_item(self, *, Key: dict[str, str], **_: object) -> dict[str, object]:
                if Key == {"pk": "AUTH#MATTER#matter-synthetic", "sk": "PROFILE"}:
                    return {"Item": {"matterId": "matter-synthetic", "tenantId": "tenant-synthetic", "authorizedUserIds": ["existing-user"]}}
                return {}

            def put_item(self, **_: object) -> dict[str, object]:
                return {}

            def update_item(self, **_: object) -> dict[str, object]:
                return {}

            def scan(self, **_: object) -> dict[str, object]:
                return {"Items": [], "ScannedCount": 0}

            def query(self, **_: object) -> dict[str, object]:
                return {"Items": []}

            def delete_item(self, **kwargs: object) -> dict[str, object]:
                self.deleted.append(dict(kwargs))
                return {}

        class FakeCognito:
            def __init__(self) -> None:
                self.deleted = 0
                self.fail_delete = False

            def admin_create_user(self, **_: object) -> dict[str, object]:
                return {"User": {"Attributes": [{"Name": "sub", "Value": "subject-timeout"}]}}

            def admin_set_user_password(self, **_: object) -> dict[str, object]:
                return {}

            def admin_delete_user(self, **_: object) -> dict[str, object]:
                self.deleted += 1
                if self.fail_delete:
                    raise RuntimeError("synthetic cleanup failure")
                return {}

        class FakeCore:
            def list_events(self, **_: object) -> dict[str, object]:
                return {"events": []}

            def delete_event(self, **_: object) -> dict[str, object]:
                return {}

        class FakeSession:
            def __init__(self, cognito: FakeCognito, table: FakeTable, core: FakeCore) -> None:
                self.cognito, self.table, self.core = cognito, table, core

            def client(self, name: str, **_: object) -> object:
                return {"cognito-idp": self.cognito, "bedrock-agentcore": self.core}[name]

            def resource(self, name: str, **_: object) -> object:
                if name != "dynamodb":
                    raise AssertionError(name)
                return type("DynamoResource", (), {"Table": lambda _self, _name: self.table})()

        table = FakeTable()
        cognito = FakeCognito()
        session = FakeSession(cognito, table, FakeCore())
        progress = json.dumps({
            "smoke": "phase14-idp-browser-progress", "phase": "review_conversation_ready",
            "documentId": "doc-timeout", "scopes": [
                {"conversationId": "conversation-1", "sessionId": "session-1"},
                {"conversationId": "conversation-2", "sessionId": "session-2"},
            ],
        }).encode()

        def timeout(*_: object, **__: object) -> object:
            raise subprocess.TimeoutExpired(["node"], 1, output=progress)

        # Never remove a pre-existing operator report to make a test pass.
        result_root = ROOT / "evals" / "results"
        result_root.mkdir(parents=True, exist_ok=True)
        temporary_reports = tempfile.TemporaryDirectory(prefix="unit-idp-", dir=result_root)
        report_path = Path(temporary_reports.name) / "timeout-cleanup.json"
        from phase14_idp_live_smoke import run_live
        live_config = config(
            execute=True, base_url="https://legaldesk.example", idp_host="auth.example",
            user_pool_id="pool", table_name="table", memory_id="memory", field_name="effective_date",
            report_path=report_path,
        )
        try:
            with patch("boto3.Session", return_value=session), patch("phase14_idp_live_smoke.subprocess.run", side_effect=timeout):
                report = run_live(live_config)
            self.assertEqual(report["result"], "FAIL")
            self.assertEqual(report["category"], "browser_timeout")
            self.assertEqual(cognito.deleted, 1)
            self.assertEqual(len(report["cleanup"]["scopes"]), 2)

            cognito.fail_delete = True
            report_path.unlink()
            with patch("boto3.Session", return_value=session), patch("phase14_idp_live_smoke.subprocess.run", side_effect=timeout):
                failed_cleanup = run_live(live_config)
            self.assertEqual(failed_cleanup["result"], "FAIL")
            self.assertTrue(any(item.startswith("cognito_cleanup:") for item in failed_cleanup["cleanupErrors"]))
        finally:
            temporary_reports.cleanup()

    def test_paid_budget_rejects_ambiguous_replay_and_inflight_completion(self) -> None:
        validate_limits({
            "modelCalls": 2, "inputTokens": 100, "outputTokens": 200,
            "ocrPages": 1, "ocrApiCalls": 1, "pollIterations": 2,
            "wallSeconds": 30, "retryCount": 0,
            "ambiguousPaidOutcome": False, "paidRetryAttempted": False,
            "stages": [{"state": "COMMITTED", "attempts": 1, "ambiguous": False, "paidCallCompleted": True}],
        })
        with self.assertRaises(LiveIDPPreflightError):
            validate_limits({
                "modelCalls": 2, "inputTokens": 100, "outputTokens": 200,
                "ocrPages": 1, "ocrApiCalls": 1, "pollIterations": 2,
                "wallSeconds": 30, "retryCount": 0,
                "ambiguousPaidOutcome": True, "paidRetryAttempted": True,
                "stages": [],
            })

    def test_browser_report_bounds_are_required_and_provider_usage_stays_unexecuted(self) -> None:
        validate_browser_report({
            "wallSeconds": 12,
            "polls": {"iterations": 2},
            "apiCallCount": 8,
            "apiCallLimit": 120,
        })
        with self.assertRaises(LiveIDPPreflightError):
            validate_browser_report({
                "wallSeconds": 12,
                "polls": {"iterations": 2},
                "apiCallCount": 121,
                "apiCallLimit": 120,
            })
        with self.assertRaises(LiveIDPPreflightError):
            validate_browser_report({
                "polls": {"iterations": 2},
                "apiCallCount": 8,
                "apiCallLimit": 120,
            })
        with self.assertRaises(LiveIDPPreflightError):
            validate_limits({
                "modelCalls": 2, "inputTokens": 100, "outputTokens": 200,
                "ocrPages": 1, "ocrApiCalls": 1, "pollIterations": 2,
                "wallSeconds": 30, "retryCount": 0,
                "stages": [{"state": "IN_FLIGHT", "attempts": 1, "ambiguous": False, "paidCallCompleted": True}],
            })

    def test_evaluation_export_hashes_values_and_quotes(self) -> None:
        report = {
            "provenance": {"releaseSha256": "a" * 64, "modelId": "approved-profile", "promptVersion": "idp-v1"},
            "cases": [{
                "fixtureId": "contract-01-en-digital-monthend", "documentType": "CONTRACT",
                "documentSha256": "b" * 64, "status": "IDP_COMPLETED",
                "fields": {"effective_date": {"presence": "PRESENT", "origin": "LITERAL", "acceptance": "AUTO_ACCEPTED", "value": "2024-01-31", "evidence": [{"page": 1, "quote": "SECRET RAW QUOTE", "contentSha256": "b" * 64}]}},
                "derived": {"estimated_anniversary": "SECRET DERIVED VALUE", "review_reason": "SECRET RAW REASON"},
                "observed": {"tokens": {"input": 10, "output": 20}, "ocr": {"pages": 1, "api_calls": 1}},
            }],
        }
        exported = build_evaluation_export(report)
        encoded = str(exported)
        self.assertEqual(exported["provenance"]["kind"], "unknown")
        self.assertNotIn("SECRET RAW QUOTE", encoded)
        self.assertNotIn("SECRET DERIVED VALUE", encoded)
        self.assertNotIn("2024-01-31", encoded)
        field = exported["results"][0]["fields"]["effective_date"]
        self.assertEqual(len(field["valueDigest"]), 64)
        self.assertEqual(len(field["evidence"][0]["quoteDigest"]), 64)
        self.assertEqual(field["evidence"][0]["quoteDigest"], hashlib.sha256(b"SECRET RAW QUOTE").hexdigest())


if __name__ == "__main__":
    unittest.main()
