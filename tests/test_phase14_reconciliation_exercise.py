from __future__ import annotations

import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from phase14_reconciliation_exercise import (  # noqa: E402
    APPROVAL_TEXT,
    ExerciseConfig,
    ExerciseError,
    _config_from_args,
    main,
    run_live,
    validate_deployed_scope,
)


TENANT = "tenant-a"
MATTER = "matter-a"
DOCUMENT = "12345678-1234-4234-8234-123456789abc"


def _config(*, execute: bool = True, approval: str = APPROVAL_TEXT) -> ExerciseConfig:
    return ExerciseConfig(
        region="eu-west-1",
        function_name="legaldesk-reconciliation",
        table_name="fictional-table",
        bucket_name="fictional-bucket",
        tenant_id=TENANT,
        matter_id=MATTER,
        document_id=DOCUMENT,
        stale_seconds=86400,
        approval=approval,
        execute=execute,
    )


def _environment() -> dict[str, str]:
    return {
        "LEGALDESK_METADATA_TABLE_NAME": "fictional-table",
        "LEGALDESK_SOURCE_BUCKET": "fictional-bucket",
        "LEGALDESK_RECONCILIATION_BETA_TENANT_ID": TENANT,
        "LEGALDESK_RECONCILIATION_UPLOAD_SCOPES": json.dumps([{"tenantId": TENANT, "matterId": MATTER}]),
        "LEGALDESK_RECONCILIATION_UPLOAD_STALE_SECONDS": "86400",
    }


class _FakeTable:
    def __init__(self) -> None:
        self.item: dict[str, object] | None = None
        self.put_calls: list[dict[str, object]] = []
        self.delete_calls: list[dict[str, object]] = []

    def get_item(self, **_kwargs: object) -> dict[str, object]:
        return {"Item": self.item} if self.item is not None else {}

    def put_item(self, **kwargs: object) -> None:
        self.put_calls.append(kwargs)
        self.item = dict(kwargs["Item"])  # type: ignore[arg-type]

    def delete_item(self, **kwargs: object) -> None:
        self.delete_calls.append(kwargs)
        self.item = None


class _FakeDynamo:
    def __init__(self, table: _FakeTable) -> None:
        self.table = table

    def Table(self, _name: str) -> _FakeTable:
        return self.table


class _FakeS3:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.put_calls: list[dict[str, object]] = []
        self.delete_calls: list[dict[str, object]] = []

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        if Key not in self.objects:
            raise RuntimeError("not found")
        return {"ContentLength": len(self.objects[Key])}

    def put_object(self, **kwargs: object) -> None:
        self.put_calls.append(kwargs)
        self.objects[str(kwargs["Key"])] = bytes(kwargs["Body"])  # type: ignore[arg-type]

    def delete_object(self, *, Bucket: str, Key: str) -> None:
        self.delete_calls.append({"Bucket": Bucket, "Key": Key})
        self.objects.pop(Key, None)


class _FakeLambda:
    def __init__(self, table: _FakeTable, s3: _FakeS3, *, fail: bool = False) -> None:
        self.table = table
        self.s3 = s3
        self.fail = fail
        self.invoke_calls: list[dict[str, object]] = []

    def get_function_configuration(self, *, FunctionName: str) -> dict[str, object]:
        return {"Environment": {"Variables": _environment()}}

    def invoke(self, **kwargs: object) -> dict[str, object]:
        self.invoke_calls.append(kwargs)
        if self.fail:
            return {"FunctionError": "Handled", "Payload": io.BytesIO(b"{}")}
        assert self.table.item is not None
        self.table.item["status"] = "FAILED"
        for key in (kwargs["_quarantine_key"], kwargs["_sidecar_key"]):
            self.s3.objects.pop(str(key), None)
        return {
            "Payload": io.BytesIO(
                b'{"status":"completed","uploads":{"examined":1,"changed":1,"skipped":0,"ambiguous":0,"failed":0}}'
            )
        }


class Phase14ReconciliationExerciseTests(unittest.TestCase):
    def test_dry_run_has_no_aws_client_construction_and_requires_no_approval(self) -> None:
        argv = [
            "--function-name", "reconciliation",
            "--table-name", "table",
            "--bucket-name", "bucket-name",
            "--tenant-id", TENANT,
            "--matter-id", MATTER,
            "--document-id", DOCUMENT,
        ]
        with patch.dict(sys.modules, {"boto3": None}):
            self.assertEqual(main(argv), 0)

    def test_execute_requires_exact_acknowledgement(self) -> None:
        args = type("Args", (), {
            "region": "eu-west-1", "function_name": "reconciliation", "table_name": "table",
            "bucket_name": "bucket-name", "tenant_id": TENANT, "matter_id": MATTER,
            "document_id": DOCUMENT, "stale_seconds": 86400, "approval": "", "execute": True,
        })()
        with self.assertRaisesRegex(ExerciseError, "explicit_approval_required"):
            _config_from_args(args)

    def test_deployed_scope_must_match_exact_beta_upload_partition(self) -> None:
        config = _config()
        validate_deployed_scope(config, {"Environment": {"Variables": _environment()}})
        foreign = _environment()
        foreign["LEGALDESK_RECONCILIATION_UPLOAD_SCOPES"] = json.dumps([{"tenantId": TENANT, "matterId": "matter-b"}])
        with self.assertRaisesRegex(ExerciseError, "upload_scope_not_authorized"):
            validate_deployed_scope(config, {"Environment": {"Variables": foreign}})

    def test_live_exercise_invokes_once_and_cleans_exact_fixture(self) -> None:
        table = _FakeTable()
        s3 = _FakeS3()
        lambda_client = _FakeLambda(table, s3)

        # The fake provider needs the server-owned keys to model the Lambda's
        # deletion; the production request itself contains only the event.
        original_invoke = lambda_client.invoke

        def invoke(**kwargs: object) -> dict[str, object]:
            kwargs = {**kwargs, "_quarantine_key": _config().quarantine_key, "_sidecar_key": _config().sidecar_key}
            return original_invoke(**kwargs)

        lambda_client.invoke = invoke  # type: ignore[method-assign]
        result = run_live(_config(), clients={"lambda": lambda_client, "dynamodb": _FakeDynamo(table), "s3": s3})

        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["invocations"], 1)
        self.assertEqual(len(lambda_client.invoke_calls), 1)
        self.assertEqual(lambda_client.invoke_calls[0]["InvocationType"], "RequestResponse")
        self.assertEqual(len(table.put_calls), 1)
        self.assertEqual(len(s3.put_calls), 2)
        self.assertEqual(
            {call["Key"] for call in s3.put_calls},
            {_config().quarantine_key, _config().sidecar_key},
        )
        self.assertIsNone(table.item)
        self.assertEqual(s3.objects, {})
        self.assertEqual({call["Key"] for call in s3.delete_calls}, {_config().quarantine_key, _config().sidecar_key})
        self.assertEqual(table.delete_calls[0]["Key"], {"pk": f"TENANT#{TENANT}#MATTER#{MATTER}", "sk": f"DOCUMENT#{DOCUMENT}"})

    def test_failed_invocation_still_cleans_the_seeded_exact_fixture(self) -> None:
        table = _FakeTable()
        s3 = _FakeS3()
        lambda_client = _FakeLambda(table, s3, fail=True)
        original_invoke = lambda_client.invoke

        def invoke(**kwargs: object) -> dict[str, object]:
            return original_invoke(
                **kwargs,
                _quarantine_key=_config().quarantine_key,
                _sidecar_key=_config().sidecar_key,
            )

        lambda_client.invoke = invoke  # type: ignore[method-assign]
        with self.assertRaisesRegex(ExerciseError, "lambda_invocation_failed"):
            run_live(_config(), clients={"lambda": lambda_client, "dynamodb": _FakeDynamo(table), "s3": s3})
        self.assertIsNone(table.item)
        self.assertEqual(s3.objects, {})
        self.assertEqual(len(lambda_client.invoke_calls), 1)


if __name__ == "__main__":
    unittest.main()
