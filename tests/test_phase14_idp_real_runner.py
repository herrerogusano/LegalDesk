"""Offline contracts for the opt-in Phase 14 real-model runner."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evals"))

from phase14_idp_real_runner import (  # noqa: E402
    EvaluationLimits,
    RealEvaluationError,
    RunnerConfig,
    _Budget,
    _BoundedConverse,
    _canonical_bytes,
    _digest,
    _fixture_bytes,
    _load_ocr_artifact,
    _apply_runtime_derived,
    _derived_export,
    _report_status,
    _safe_error,
    _safe_error_type,
    load_manifest,
    run,
)
from legaldesk.idp.providers import IDPConverseClassifier, IDPConverseExtractor, IDPModelConfig  # noqa: E402
from legaldesk.idp.registry import IDPSchemaRegistry  # noqa: E402
from legaldesk.idp.models import DocumentType, FieldAcceptance, FieldPresence, IDPExtractionRun, IDPFieldResult, IDPJobStatus  # noqa: E402
from legaldesk.idp.processing import EvidenceValidationError  # noqa: E402


class _NeverCalled:
    def __init__(self) -> None:
        self.calls = 0

    def converse(self, **_kwargs: object):
        self.calls += 1
        raise AssertionError("the provider must not be called")


class _OfflineProvider:
    def __init__(self) -> None:
        self.calls = 0

    def converse(self, **_kwargs: object):
        self.calls += 1
        if self.calls == 1:
            output = {"document_type": "CONTRACT", "evidence": []}
        else:
            request = json.loads(_kwargs["messages"][0]["content"][0]["text"])
            output = {"schema_version": "1.0.0", "fields": [{"field": item["name"], "presence": "ABSENT", "value": None, "reason": "", "evidence": []} for item in request["fields"]]}
        return {
            "output": {"message": {"content": [{"text": json.dumps(output)}]}},
            "usage": {"inputTokens": 10, "outputTokens": 10},
        }


class _MissingUsageProvider:
    def __init__(self) -> None:
        self.calls = 0

    def converse(self, **_kwargs: object):
        self.calls += 1
        return {"output": {"message": {"content": [{"text": "{}"}]}}}


class _ExtractorParamValidationProvider:
    def __init__(self) -> None:
        self.calls = 0

    def converse(self, **_kwargs: object):
        self.calls += 1
        if self.calls == 1:
            return {
                "output": {"message": {"content": [{"text": json.dumps({"document_type": "CONTRACT", "evidence": []})}]}},
                "usage": {"inputTokens": 10, "outputTokens": 10},
            }
        from botocore.exceptions import ParamValidationError
        raise ParamValidationError(report="synthetic provider detail must not enter export")


class Phase14IDPRealRunnerTests(unittest.TestCase):
    def test_evidence_validation_failure_has_closed_diagnostic_code(self) -> None:
        error = EvidenceValidationError("synthetic quote mismatch")
        self.assertEqual(_safe_error(error), "EVIDENCE_ANCHOR_INVALID")
        self.assertEqual(_safe_error_type(error), "EVIDENCE_ANCHOR")

    def test_export_applies_production_derived_metadata_and_marks_conflict_for_review(self) -> None:
        fields = {
            "effective_date": IDPFieldResult(field="effective_date", value="2024-01-31", presence=FieldPresence.PRESENT),
            "initial_duration_value": IDPFieldResult(field="initial_duration_value", value=1, presence=FieldPresence.PRESENT),
            "initial_duration_unit": IDPFieldResult(field="initial_duration_unit", value="month", presence=FieldPresence.PRESENT),
            "explicit_expiration_date": IDPFieldResult(field="explicit_expiration_date", value="2024-03-01", presence=FieldPresence.PRESENT),
        }
        run = IDPExtractionRun(
            run_id="derived-test", tenant_id="tenant", matter_id="matter", document_id="doc",
            document_sha256="a" * 64, document_type=DocumentType.CONTRACT, schema_version="1.0.0",
            model_id="model", prompt_version="prompt", status=IDPJobStatus.COMPLETED, fields=fields,
            created_at=datetime.now(timezone.utc),
        )
        projected = _apply_runtime_derived(run)
        self.assertEqual(projected.fields["estimated_anniversary_date"].value, "2024-02-29")
        self.assertEqual(projected.fields["estimated_anniversary_date"].acceptance, FieldAcceptance.REVIEW_REQUIRED)
        exported = _derived_export(projected)
        self.assertEqual(exported["field"], "estimated_anniversary_date")
        self.assertEqual(exported["conflict"], True)
        self.assertNotIn("2024-02-29", json.dumps(exported))
        self.assertEqual(_report_status(projected), "REVIEW_REQUIRED")

    def test_default_preflight_is_18_fixture_metadata_only_and_never_imports_boto3(self) -> None:
        config = RunnerConfig()
        with patch.dict(sys.modules, {"boto3": None}):
            report = run(config)
        self.assertEqual(report["provenance"]["kind"], "unknown")
        self.assertEqual(report["provenance"]["status"], "PREFLIGHT")
        self.assertEqual(len(report["plan"]), 18)
        self.assertEqual(report["results"], [])
        rendered = json.dumps(report, sort_keys=True)
        self.assertNotIn("source_pages", rendered)
        self.assertNotIn("NO LEGAL ADVICE", rendered)
        scanned = [item for item in report["plan"] if item["ocr"]["requiredPages"]]
        self.assertEqual(len(scanned), 8)
        self.assertTrue(all("OCR_ARTIFACT_REQUIRED" in item["errors"] for item in scanned))
        manifest = load_manifest()
        self.assertEqual(
            {item["documentSha256"] for item in report["plan"]},
            {item["sha256"] for item in manifest.values()},
        )

    def test_execution_requires_explicit_real_bedrock_confirmation_before_client_creation(self) -> None:
        called = []

        def factory(_region: str):
            called.append(True)
            return _NeverCalled()

        with self.assertRaises(RealEvaluationError):
            run(RunnerConfig(execute=True), client_factory=factory)
        self.assertEqual(called, [])

    def test_explicit_provider_seam_exercises_pipeline_but_exports_only_hashes_and_usage(self) -> None:
        provider = _OfflineProvider()
        with tempfile.TemporaryDirectory(dir=ROOT / "evals" / "results") as directory:
            report = run(
                RunnerConfig(
                    execute=True, confirm_real_bedrock=True,
                    fixture_ids=("contract-01-en-digital-monthend",), wall_seconds=300,
                    output=Path(directory) / "provider-seam.json",
                ),
                client_factory=lambda _region: provider,
            )
        self.assertEqual(provider.calls, 2)
        self.assertEqual(report["usage"]["bedrockCalls"], 2)
        self.assertEqual(report["results"][0]["status"], "COMPLETED")
        self.assertEqual(report["results"][0]["observed"]["tokens"], {"input": 20, "output": 20})
        rendered = json.dumps(report, sort_keys=True)
        self.assertNotIn("SERVICE AGREEMENT", rendered)
        self.assertNotIn("31 January 2024", rendered)
        self.assertNotIn("source_pages", rendered)
        self.assertTrue(all("valueDigest" in field for field in report["results"][0]["fields"].values()))

    def test_model_allowlist_and_existing_output_stop_before_client_creation(self) -> None:
        calls: list[str] = []
        with tempfile.TemporaryDirectory(dir=ROOT / "evals" / "results") as directory:
            existing = Path(directory) / "existing.json"
            existing.write_text("{}", encoding="utf-8")
            with self.assertRaises(RealEvaluationError):
                run(RunnerConfig(execute=True, confirm_real_bedrock=True, model_id="anthropic.opus", output=existing), client_factory=lambda region: calls.append(region))
            self.assertEqual(calls, [])
            with self.assertRaises(RealEvaluationError):
                run(RunnerConfig(execute=True, confirm_real_bedrock=True, output=existing), client_factory=lambda region: calls.append(region))
            self.assertEqual(calls, [])

    def test_missing_usage_halts_without_zeroing_usage_or_invoking_next_fixture(self) -> None:
        provider = _MissingUsageProvider()
        with tempfile.TemporaryDirectory(dir=ROOT / "evals" / "results") as directory:
            report = run(
                RunnerConfig(
                    execute=True, confirm_real_bedrock=True,
                    fixture_ids=("contract-01-en-digital-monthend", "contract-04-es-digital-amount"), wall_seconds=300,
                    output=Path(directory) / "missing-usage.json",
                ),
                client_factory=lambda _region: provider,
            )
        self.assertEqual(provider.calls, 1)
        self.assertEqual(report["provenance"]["status"], "UNKNOWN")
        self.assertEqual(report["provenance"]["kind"], "unknown")
        self.assertTrue(report["usage"]["usageUnknown"])
        self.assertIsNone(report["usage"]["inputTokens"])
        self.assertIn("PAID_OUTCOME_UNKNOWN", report["results"][0]["errors"])
        self.assertEqual(report["results"][1]["status"], "NOT_EXECUTED")

    def test_sdk_validation_failure_exports_safe_stage_code_and_halts(self) -> None:
        provider = _ExtractorParamValidationProvider()
        with tempfile.TemporaryDirectory(dir=ROOT / "evals" / "results") as directory:
            report = run(
                RunnerConfig(
                    execute=True, confirm_real_bedrock=True,
                    fixture_ids=("contract-01-en-digital-monthend", "contract-04-es-digital-amount"), wall_seconds=300,
                    output=Path(directory) / "sdk-validation.json",
                ),
                client_factory=lambda _region: provider,
            )
        self.assertEqual(provider.calls, 2)
        self.assertEqual(report["provenance"]["status"], "UNKNOWN")
        diagnostic = report["results"][0]["diagnostic"]
        self.assertEqual(diagnostic, {"stage": "EXTRACTOR", "code": "PROVIDER_REQUEST_INVALID", "type": "SDK_PARAMETER_VALIDATION"})
        self.assertIsNone(report["results"][0]["documentType"], "failed classification must not copy expected fixture type")
        self.assertNotIn("synthetic provider detail", json.dumps(report))
        self.assertIsNone(report["usage"]["inputTokens"])

    def test_classifier_and_extractor_requests_pass_local_botocore_shape_validation(self) -> None:
        import boto3
        from botocore.validate import validate_parameters

        client = boto3.client(
            "bedrock-runtime", region_name="eu-west-1",
            aws_access_key_id="offline", aws_secret_access_key="offline",
        )
        config = IDPModelConfig("eu.anthropic.claude-sonnet-4-6")
        classifier = IDPConverseClassifier(client, config=config)
        extractor = IDPConverseExtractor(client, config=config)
        shape = client.meta.service_model.operation_model("Converse").input_shape
        validate_parameters(classifier.request(page_text={1: "synthetic"}), shape)
        validate_parameters(
            extractor.request(schema=IDPSchemaRegistry().get("CONTRACT"), page_text={1: "synthetic"}), shape,
        )

    def test_deadline_and_call_caps_are_checked_before_provider_call(self) -> None:
        client = _NeverCalled()
        expired = _Budget(EvaluationLimits(), deadline=0.0)
        with self.assertRaises(RealEvaluationError):
            _BoundedConverse(client, expired).converse(
                modelId="synthetic", inferenceConfig={"maxTokens": 1}, messages=[]
            )
        self.assertEqual(client.calls, 0)

        ocr_budget = _Budget(EvaluationLimits(ocr_pages=1, ocr_api_calls=1), deadline=10**12)
        ocr_budget.reserve_ocr(1, 1)
        with self.assertRaises(RealEvaluationError):
            ocr_budget.reserve_ocr(1, 1)

        capped = _Budget(EvaluationLimits(bedrock_calls=0), deadline=10**12)
        with self.assertRaises(RealEvaluationError):
            _BoundedConverse(client, capped).converse(
                modelId="synthetic", inferenceConfig={"maxTokens": 1}, messages=[]
            )
        self.assertEqual(client.calls, 0)

    def test_preexisting_ocr_requires_external_hash_authorization_and_exact_page_coverage(self) -> None:
        fixtures = load_manifest()
        fixture = fixtures["contract-02-es-scanned-leapday"]
        _body, document = _fixture_bytes(fixture, ROOT / "tests" / "fixtures" / "idp" / "pdfs")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = {
                "fixtureId": fixture["id"], "documentSha256": fixture["sha256"],
                "pageCount": document.page_count, "pages": {"1": "Textract-only synthetic page output."},
                "immutable": True,
                "authorization": "operator-approved-preexisting-textract-artifact",
                "stageProof": {"provider": "textract", "status": "SUCCEEDED", "jobId": "textract-job-1", "apiCalls": 1, "pages": 1},
            }
            artifact["pagesSha256"] = _digest(artifact["pages"])
            path = root / f"{fixture['id']}.json"
            raw = _canonical_bytes(artifact)
            path.write_bytes(raw)
            manifest = {fixture["id"]: hashlib.sha256(raw).hexdigest()}
            pages, proof = _load_ocr_artifact(
                fixture, document=document, artifact_dir=root, artifact_manifest=manifest, limits=EvaluationLimits(),
            )
            self.assertEqual(pages, {1: "Textract-only synthetic page output."})
            self.assertEqual(proof["mode"], "preexisting_textract_artifact")
            artifact["authorization"] = "fixture-manifest-text"
            path.write_bytes(_canonical_bytes(artifact))
            with self.assertRaises(RealEvaluationError):
                _load_ocr_artifact(
                    fixture, document=document, artifact_dir=root,
                    artifact_manifest={fixture["id"]: hashlib.sha256(path.read_bytes()).hexdigest()}, limits=EvaluationLimits(),
                )

    def test_ocr_artifact_cannot_claim_a_different_source_or_page_set(self) -> None:
        fixtures = load_manifest()
        fixture = fixtures["demand-03-en-mixed"]
        _body, document = _fixture_bytes(fixture, ROOT / "tests" / "fixtures" / "idp" / "pdfs")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = {
                "fixtureId": fixture["id"], "documentSha256": fixture["sha256"], "pageCount": document.page_count,
                "pages": {"1": "wrong page", "2": "wrong extra page"}, "immutable": True,
                "authorization": "operator-approved-preexisting-textract-artifact",
                "stageProof": {"provider": "textract", "status": "SUCCEEDED", "jobId": "textract-job-2", "apiCalls": 1, "pages": 2},
            }
            artifact["pagesSha256"] = _digest(artifact["pages"])
            raw = _canonical_bytes(artifact)
            path = root / f"{fixture['id']}.json"
            path.write_bytes(raw)
            with self.assertRaises(RealEvaluationError):
                _load_ocr_artifact(
                    fixture, document=document, artifact_dir=root,
                    artifact_manifest={fixture["id"]: hashlib.sha256(raw).hexdigest()}, limits=EvaluationLimits(),
                )

    def test_mixed_ocr_billing_uses_textract_document_pages_not_covered_subset(self) -> None:
        fixtures = load_manifest()
        fixture = fixtures["demand-03-en-mixed"]
        _body, document = _fixture_bytes(fixture, ROOT / "tests" / "fixtures" / "idp" / "pdfs")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = {
                "fixtureId": fixture["id"], "documentSha256": fixture["sha256"], "pageCount": document.page_count,
                "pages": {"2": "Textract-only page 2 output."}, "immutable": True,
                "authorization": "operator-approved-preexisting-textract-artifact",
                "stageProof": {"provider": "textract", "status": "SUCCEEDED", "jobId": "textract-job-mixed", "apiCalls": 1, "pages": 2},
            }
            artifact["pagesSha256"] = _digest(artifact["pages"])
            raw = _canonical_bytes(artifact)
            path = root / f"{fixture['id']}.json"
            path.write_bytes(raw)
            _pages, proof = _load_ocr_artifact(
                fixture, document=document, artifact_dir=root,
                artifact_manifest={fixture["id"]: hashlib.sha256(raw).hexdigest()}, limits=EvaluationLimits(),
            )
        self.assertEqual(proof["pages"], 2)
        self.assertEqual(proof["coveredPages"], 1)


if __name__ == "__main__":
    unittest.main()
