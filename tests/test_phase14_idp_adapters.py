from __future__ import annotations

import json
import hashlib
import boto3
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "backend" / "src"))

from legaldesk.idp import (  # noqa: E402
    Boto3DynamoStageLedger,
    Boto3TextractProvider,
    DocumentType,
    IDPConverseClassifier,
    IDPModelConfig,
    InMemoryIDPArtifactStore,
    IDPProcessingPipeline,
    ClassificationResult,
    ExtractionResult,
    IDPFieldResult,
    FieldAcceptance,
    FieldOrigin,
    FieldPresence,
    PaidStage,
    PaidStageState,
    IDPConcurrencyError,
    derive_anniversary,
    extractor_json_schema,
    idp_artifact_key,
)
from legaldesk.idp.registry import IDPSchemaRegistry  # noqa: E402
from legaldesk.idp_lambda import lambda_handler  # noqa: E402


class _Table:
    def __init__(self):
        self.items = {}
        self.last = {}

    def get_item(self, **kwargs):
        return {"Item": self.items.get((kwargs["Key"]["pk"], kwargs["Key"]["sk"]))}

    def put_item(self, **kwargs):
        self.last = kwargs
        item = kwargs["Item"]
        self.items[(item["pk"], item["sk"])] = item

    def update_item(self, **kwargs):
        self.last = kwargs
        key = kwargs["Key"]
        item = self.items[(key["pk"], key["sk"])]
        values = kwargs["ExpressionAttributeValues"]
        item["state"] = values.get(":committed", values.get(":ambiguous", item["state"]))
        if ":artifact" in values:
            item["artifactRef"] = values[":artifact"]


class _Converse:
    def __init__(self):
        self.calls = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        return {"output": {"message": {"content": [{"text": '{"document_type":"UNKNOWN","evidence":[]}'}]}}, "stopReason": "end_turn"}


class _Textract:
    def __init__(self):
        self.calls = []

    def start_document_text_detection(self, **kwargs):
        self.calls.append(kwargs)
        return {"JobId": "job-1"}

    def get_document_text_detection(self, **kwargs):
        return {"JobId": kwargs["JobId"], "JobStatus": "SUCCEEDED", "DocumentMetadata": {"Pages": 1}, "Blocks": [{"BlockType": "LINE", "Page": 1, "Text": "text"}]}


class AdapterTests(unittest.TestCase):
    def test_durable_stage_ledger_is_scoped_and_requires_artifact(self):
        table = _Table()
        ledger = Boto3DynamoStageLedger(table, tenant_id="tenant", matter_id="matter")
        record = ledger.begin(run_id="run", stage=PaidStage.CLASSIFIER, request_hash="hash")
        self.assertEqual(record.state, PaidStageState.IN_FLIGHT)
        committed = ledger.commit(record, artifact_ref="idp-artifacts/result")
        self.assertEqual(committed.artifact_ref, "idp-artifacts/result")
        self.assertEqual(table.last["ExpressionAttributeNames"]["#requestHash"], "requestHash")

    def test_real_boto_resource_stage_put_serializes_native_values_once(self):
        resource = boto3.resource("dynamodb", region_name="eu-west-1", endpoint_url="http://127.0.0.1:9", aws_access_key_id="offline", aws_secret_access_key="offline")
        table = resource.Table("table")
        table.get_item = lambda **kwargs: {}
        captured = {}

        class AbortBeforeNetwork(Exception):
            pass

        def before_call(model, params, **kwargs):
            captured.update(params)
            raise AbortBeforeNetwork()

        table.meta.client.meta.events.register("before-call.dynamodb.PutItem", before_call)
        ledger = Boto3DynamoStageLedger(table, tenant_id="tenant", matter_id="matter")
        with self.assertRaises(IDPConcurrencyError):
            ledger.begin(run_id="run", stage=PaidStage.CLASSIFIER, request_hash="hash")
        body = json.loads(captured["body"])
        self.assertEqual(body["Item"]["pk"], {"S": "TENANT#tenant#MATTER#matter"})

    def test_converse_classifier_uses_separate_prompt_and_no_native_citations(self):
        client = _Converse()
        adapter = IDPConverseClassifier(client, config=IDPModelConfig("eu.anthropic.claude-sonnet-4-6"), prompt_path=Path(__file__).parents[1] / "prompts" / "idp-classifier.md")
        result = adapter.classify(page_text={1: "Unknown source"}, content_sha256="a" * 64)
        self.assertEqual(result.document_type, DocumentType.UNKNOWN)
        self.assertNotIn("citations", client.calls[0])
        self.assertNotIn("minItems", json.loads(client.calls[0]["outputConfig"]["textFormat"]["structure"]["jsonSchema"]["schema"]))

    def test_textract_adapter_uses_stable_token_and_server_topic(self):
        client = _Textract()
        from legaldesk.idp import OCRStartRequest

        request = OCRStartRequest("run", "a" * 64, "bucket", "key", "token", expected_sns_topic_arn="arn:sns")
        adapter = Boto3TextractProvider(client, notification_role_arn="arn:role", notification_topic_arn="arn:sns")
        self.assertEqual(adapter.start_document_text_detection(request), "job-1")
        self.assertEqual(client.calls[0]["ClientRequestToken"], "token")
        self.assertEqual(client.calls[0]["JobTag"], "run")

    def test_immutable_artifact_hash_and_scope(self):
        store = InMemoryIDPArtifactStore()
        body = b"model result"
        key = idp_artifact_key(tenant_id="t", matter_id="m", document_id="d", run_id="r", kind="result", sha256=__import__("hashlib").sha256(body).hexdigest())
        store.put_immutable(key=key, body=body, media_type="application/json", sha256=__import__("hashlib").sha256(body).hexdigest())
        self.assertEqual(store.read_verified(key=key), body)

    def test_idp_lambda_is_disabled_by_default(self):
        with patch.dict(os.environ, {"LEGALDESK_IDP_ENABLED": "false"}, clear=False):
            self.assertEqual(lambda_handler({}, None)["status"], "disabled")

    def test_fake_upload_queue_digital_pipeline_persists_immutable_run_without_rag(self):
        from legaldesk.idp import StageCallLedger

        class FakeClassifier:
            def classify(self, *, page_text, content_sha256):
                return ClassificationResult(DocumentType.CONTRACT, None, (), "p", "m", FieldAcceptance.PROVISIONAL)

        class FakeExtractor:
            def extract(self, *, schema, page_text, content_sha256):
                return ExtractionResult(schema.document_type, schema.version, {"effective_date": IDPFieldResult("effective_date", "2026-01-15", FieldPresence.PRESENT, FieldOrigin.LITERAL, FieldAcceptance.PROVISIONAL)})

        content = (Path(__file__).parents[1] / "tests" / "fixtures" / "idp" / "pdfs" / "contract-01-en-digital-monthend.pdf").read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        pipeline = IDPProcessingPipeline(classifier=FakeClassifier(), extractor=FakeExtractor(), registry=IDPSchemaRegistry(), artifact_store=InMemoryIDPArtifactStore(), stage_ledger=StageCallLedger())
        result = pipeline.process(tenant_id="t", matter_id="m", document_id="d", run_id="r", content=content, content_sha256=digest, model_id="m", prompt_version="p")
        self.assertEqual(result.status.value, "COMPLETED")
        self.assertIsNotNone(result.run)


if __name__ == "__main__":
    unittest.main()
