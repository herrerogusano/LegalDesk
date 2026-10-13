"""Offline contract tests for the explicitly opt-in Textract collector."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
(ROOT / "tmp").mkdir(parents=True, exist_ok=True)


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


runner = _load("phase14_idp_real_runner", ROOT / "evals" / "phase14_idp_real_runner.py")
collector = _load("phase14_idp_ocr_collect_test", ROOT / "evals" / "phase14_idp_ocr_collect.py")


class _S3:
    def __init__(self):
        self.objects = {}
        self.puts = []
        self.deleted = []

    def put_object(self, **kwargs):
        key = (kwargs["Bucket"], kwargs["Key"])
        if key in self.objects:
            raise AssertionError("collector must use create-only objects")
        self.objects[key] = {"Body": kwargs["Body"], "Metadata": kwargs["Metadata"]}
        self.puts.append(kwargs)
        return {}

    def head_object(self, *, Bucket, Key):
        item = self.objects[(Bucket, Key)]
        return {"Metadata": item["Metadata"], "ContentLength": len(item["Body"])}

    def delete_object(self, *, Bucket, Key):
        self.deleted.append((Bucket, Key))
        self.objects.pop((Bucket, Key), None)
        return {}


class _Textract:
    def __init__(self, *, fail_start=False, page_count=1):
        self.fail_start = fail_start
        self.page_count = page_count
        self.starts = []
        self.gets = []
        self.counter = 0

    def start_document_text_detection(self, **kwargs):
        self.starts.append(kwargs)
        if self.fail_start:
            raise RuntimeError("ambiguous start")
        self.counter += 1
        return {"JobId": f"job-{self.counter}"}

    def get_document_text_detection(self, **kwargs):
        self.gets.append(kwargs)
        return {
            "JobStatus": "SUCCEEDED",
            "DocumentMetadata": {"Pages": self.page_count},
            "Blocks": [{"BlockType": "LINE", "Page": page, "Text": f"Synthetic OCR page {page}"} for page in range(1, self.page_count + 1)],
        }


class _TruncatedTextract(_Textract):
    def get_document_text_detection(self, **kwargs):
        self.gets.append(kwargs)
        return {"JobStatus": "SUCCEEDED", "NextToken": "more", "DocumentMetadata": {"Pages": 1}, "Blocks": []}


class OCRCollectorTests(unittest.TestCase):
    def test_default_is_local_preflight_and_selects_eight_scanned_pdfs(self):
        config = collector.CollectorConfig()
        report = collector.collect(config, s3_client=object(), textract_client=object())
        self.assertEqual(report["status"], "PREFLIGHT")
        self.assertEqual(report["fixtureCount"], 8)
        self.assertEqual(report["pdfPages"], 11)
        self.assertEqual(report["paidCalls"], 0)

    def test_paid_mode_requires_explicit_confirmation_before_clients(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "tmp") as tmp:
            config = collector.CollectorConfig(execute=True, output_dir=Path(tmp), source_bucket="synthetic-bucket")
            with self.assertRaisesRegex(collector.OCRCollectionError, "confirmation"):
                collector.collect(config, s3_client=object(), textract_client=object())

    def test_bounded_sequential_collection_creates_private_artifacts_and_cleans_own_objects(self):
        s3, textract = _S3(), _Textract()
        with tempfile.TemporaryDirectory(dir=ROOT / "tmp") as tmp:
            out = Path(tmp)
            config = collector.CollectorConfig(execute=True, confirm_real_ocr=True, output_dir=out, source_bucket="synthetic-bucket", allowed_source_buckets=("synthetic-bucket",), attempt="attempt-12345678", fixture_ids=("contract-02-es-scanned-leapday",))
            report = collector.collect(config, s3_client=s3, textract_client=textract, sleep_fn=lambda _: None)
            self.assertEqual(report["status"], "COMPLETE")
            self.assertEqual(len(textract.starts), 1)
            self.assertEqual(len(textract.gets), 1)
            self.assertEqual(s3.puts[0]["ServerSideEncryption"], "AES256")
            self.assertEqual(len(s3.deleted), 1)
            self.assertFalse((out / "attempt-12345678.started").exists())
            artifact = out / "contract-02-es-scanned-leapday.json"
            self.assertTrue(artifact.is_file())
            payload = json.loads(artifact.read_text(encoding="utf-8"))
            self.assertEqual(payload["stageProof"]["provider"], "textract")
            self.assertNotIn("Synthetic OCR text", json.dumps(report))
            fixtures = runner.load_manifest()
            fixture = fixtures["contract-02-es-scanned-leapday"]
            _body, document = runner._fixture_bytes(fixture, runner.PDF_ROOT)
            manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
            pages, proof = runner._load_ocr_artifact(fixture, document=document, artifact_dir=out, artifact_manifest=manifest, limits=runner.EvaluationLimits())
            self.assertEqual(set(pages), set(document.ocr_required_pages))
            self.assertEqual(proof["pages"], document.page_count)

    def test_start_failure_is_not_retried_or_cleaned_as_if_complete(self):
        s3, textract = _S3(), _Textract(fail_start=True)
        with tempfile.TemporaryDirectory(dir=ROOT / "tmp") as tmp:
            config = collector.CollectorConfig(execute=True, confirm_real_ocr=True, output_dir=Path(tmp), source_bucket="synthetic-bucket", allowed_source_buckets=("synthetic-bucket",), attempt="attempt-ambiguous", fixture_ids=("contract-02-es-scanned-leapday",))
            with self.assertRaisesRegex(RuntimeError, "ambiguous"):
                collector.collect(config, s3_client=s3, textract_client=textract, sleep_fn=lambda _: None)
            self.assertEqual(len(textract.starts), 1)
            self.assertEqual(len(s3.deleted), 0)
            self.assertTrue((Path(tmp) / "attempt-ambiguous.started").exists())

    def test_mixed_pdf_bills_and_proves_whole_document_but_artifacts_cover_only_ocr_pages(self):
        s3, textract = _S3(), _Textract(page_count=2)
        fixture_id = "contract-03-en-mixed-nda"
        with tempfile.TemporaryDirectory(dir=ROOT / "tmp") as tmp:
            out = Path(tmp)
            config = collector.CollectorConfig(execute=True, confirm_real_ocr=True, output_dir=out, source_bucket="synthetic-bucket", allowed_source_buckets=("synthetic-bucket",), attempt="attempt-mixed", fixture_ids=(fixture_id,))
            report = collector.collect(config, s3_client=s3, textract_client=textract, sleep_fn=lambda _: None)
            self.assertEqual(report["pdfPages"], 2)
            payload = json.loads((out / f"{fixture_id}.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["pageCount"], 2)
            self.assertEqual(set(payload["pages"]), {"2"})

    def test_cleanup_refuses_changed_owned_object(self):
        s3, textract = _S3(), _Textract()
        with tempfile.TemporaryDirectory(dir=ROOT / "tmp") as tmp:
            config = collector.CollectorConfig(execute=True, confirm_real_ocr=True, output_dir=Path(tmp), source_bucket="synthetic-bucket", allowed_source_buckets=("synthetic-bucket",), attempt="attempt-mutated", fixture_ids=("contract-02-es-scanned-leapday",))
            original_head = s3.head_object
            def changed_head(**kwargs):
                result = original_head(**kwargs)
                result["Metadata"] = {"legaldesk-sha256": "0" * 64}
                return result
            s3.head_object = changed_head
            with self.assertRaisesRegex(collector.OCRCollectionError, "cleanup_source_identity"):
                collector.collect(config, s3_client=s3, textract_client=textract, sleep_fn=lambda _: None)
            self.assertEqual(s3.deleted, [])

    def test_sdk_retry_setting_is_zero_total_retries(self):
        config = collector._client_config()
        self.assertEqual(config.retries["total_max_attempts"], 1)

    def test_terminal_success_with_next_token_fails_closed_as_incomplete(self):
        s3, textract = _S3(), _TruncatedTextract()
        with tempfile.TemporaryDirectory(dir=ROOT / "tmp") as tmp:
            config = collector.CollectorConfig(execute=True, confirm_real_ocr=True, output_dir=Path(tmp), source_bucket="synthetic-bucket", allowed_source_buckets=("synthetic-bucket",), attempt="attempt-truncated", fixture_ids=("contract-02-es-scanned-leapday",))
            with self.assertRaisesRegex(collector.OCRCollectionError, "pagination_incomplete"):
                collector.collect(config, s3_client=s3, textract_client=textract, sleep_fn=lambda _: None)
            self.assertEqual(len(textract.gets), 1)
            self.assertEqual(s3.deleted, [])

    def test_output_collision_is_rejected_before_upload_or_start(self):
        s3, textract = _S3(), _Textract()
        with tempfile.TemporaryDirectory(dir=ROOT / "tmp") as tmp:
            out = Path(tmp)
            (out / "contract-02-es-scanned-leapday.json").write_text("{}", encoding="utf-8")
            config = collector.CollectorConfig(execute=True, confirm_real_ocr=True, output_dir=out, source_bucket="synthetic-bucket", allowed_source_buckets=("synthetic-bucket",), attempt="attempt-collision", fixture_ids=("contract-02-es-scanned-leapday",))
            with self.assertRaisesRegex(collector.OCRCollectionError, "before_paid_stage"):
                collector.collect(config, s3_client=s3, textract_client=textract, sleep_fn=lambda _: None)
            self.assertEqual(s3.puts, [])
            self.assertEqual(textract.starts, [])

    def test_transient_journal_replace_permission_is_bounded_and_recovers(self):
        s3, textract = _S3(), _Textract()
        with tempfile.TemporaryDirectory(dir=ROOT / "tmp") as tmp:
            out = Path(tmp)
            config = collector.CollectorConfig(execute=True, confirm_real_ocr=True, output_dir=out, source_bucket="synthetic-bucket", allowed_source_buckets=("synthetic-bucket",), attempt="attempt-replace", fixture_ids=("contract-02-es-scanned-leapday",))
            original = collector.os.replace
            failures = {"count": 0}
            def flaky(source, destination):
                if failures["count"] < 2:
                    failures["count"] += 1
                    raise PermissionError("transient OneDrive lock")
                return original(source, destination)
            with patch.object(collector.os, "replace", side_effect=flaky):
                report = collector.collect(config, s3_client=s3, textract_client=textract, sleep_fn=lambda _: None)
            self.assertEqual(report["status"], "COMPLETE")
            self.assertEqual(failures["count"], 2)

    def test_permanent_journal_replace_permission_preserves_recovery_files(self):
        s3, textract = _S3(), _Textract()
        with tempfile.TemporaryDirectory(dir=ROOT / "tmp") as tmp:
            out = Path(tmp)
            config = collector.CollectorConfig(execute=True, confirm_real_ocr=True, output_dir=out, source_bucket="synthetic-bucket", allowed_source_buckets=("synthetic-bucket",), attempt="attempt-perm", fixture_ids=("contract-02-es-scanned-leapday",))
            with patch.object(collector.os, "replace", side_effect=PermissionError("permanent OneDrive lock")):
                with self.assertRaises(PermissionError):
                    collector.collect(config, s3_client=s3, textract_client=textract, sleep_fn=lambda _: None)
            self.assertTrue((out / "attempt-perm.started").is_file())
            self.assertTrue((out / "attempt-perm.started.tmp").is_file())
            self.assertEqual(textract.starts, [])
            self.assertEqual(s3.deleted, [])


if __name__ == "__main__":
    unittest.main()
