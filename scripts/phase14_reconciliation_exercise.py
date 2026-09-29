"""Run one bounded synthetic Phase 14 reconciliation exercise.

The default mode is offline and performs only input validation.  The AWS mode
is intentionally difficult to enable: it requires an explicit acknowledgement,
reads the deployed Lambda configuration to prove the exact beta scope, writes
one guarded metadata item and two quarantine objects, invokes the Lambda once,
verifies the aggregate/metadata/object result, and removes only those exact
fixture resources.  It never scans DynamoDB or S3 and never prints bodies,
credentials, signed URLs, or arbitrary provider errors.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import re
from typing import Any, Mapping, Sequence


APPROVAL_TEXT = "I_UNDERSTAND_ONE_LIVE_RECONCILIATION_CALL"
DEFAULT_REGION = "eu-west-1"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,127}$")
_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_DOCUMENT_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


class ExerciseError(RuntimeError):
    """Closed diagnostic category for the operator-facing runner."""


@dataclass(frozen=True, slots=True)
class ExerciseConfig:
    region: str
    function_name: str
    table_name: str
    bucket_name: str
    tenant_id: str
    matter_id: str
    document_id: str
    stale_seconds: int
    approval: str
    execute: bool = False

    @property
    def quarantine_key(self) -> str:
        return (
            f"quarantine/tenants/{self.tenant_id}/matters/{self.matter_id}/"
            f"documents/{self.document_id}/original.txt"
        )

    @property
    def sidecar_key(self) -> str:
        return f"{self.quarantine_key}.metadata.json"

    @property
    def canonical_key(self) -> str:
        return self.quarantine_key.removeprefix("quarantine/")

    @property
    def partition_key(self) -> str:
        return f"TENANT#{self.tenant_id}#MATTER#{self.matter_id}"

    @property
    def sort_key(self) -> str:
        return f"DOCUMENT#{self.document_id}"


def _identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value.strip()) is None:
        raise ExerciseError(f"{label}_invalid")
    return value.strip()


def _config_from_args(args: argparse.Namespace) -> ExerciseConfig:
    region = _identifier(args.region, "region")
    if not re.fullmatch(r"[a-z0-9-]{1,32}", region):
        raise ExerciseError("region_invalid")
    function_name = _identifier(args.function_name, "function_name")
    table_name = _identifier(args.table_name, "table_name")
    bucket_name = args.bucket_name.strip() if isinstance(args.bucket_name, str) else ""
    if _BUCKET.fullmatch(bucket_name) is None:
        raise ExerciseError("bucket_name_invalid")
    tenant_id = _identifier(args.tenant_id, "tenant_id")
    matter_id = _identifier(args.matter_id, "matter_id")
    document_id = args.document_id.strip() if isinstance(args.document_id, str) else ""
    if _DOCUMENT_ID.fullmatch(document_id) is None:
        raise ExerciseError("document_id_invalid")
    if not isinstance(args.stale_seconds, int) or not 60 <= args.stale_seconds <= 7 * 24 * 60 * 60:
        raise ExerciseError("stale_seconds_invalid")
    if args.execute and args.approval != APPROVAL_TEXT:
        raise ExerciseError("explicit_approval_required")
    return ExerciseConfig(
        region=region,
        function_name=function_name,
        table_name=table_name,
        bucket_name=bucket_name,
        tenant_id=tenant_id,
        matter_id=matter_id,
        document_id=document_id,
        stale_seconds=args.stale_seconds,
        approval=args.approval,
        execute=args.execute,
    )


def _scope_values(raw: object, *, tenant_id: str, matter_id: str) -> bool:
    """Return whether deployed JSON contains exactly the requested upload scope."""

    if not isinstance(raw, str):
        return False
    try:
        scopes = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    if not isinstance(scopes, list):
        return False
    return any(
        isinstance(item, Mapping)
        and set(item) == {"tenantId", "matterId"}
        and item.get("tenantId") == tenant_id
        and item.get("matterId") == matter_id
        for item in scopes
    )


def validate_deployed_scope(
    config: ExerciseConfig,
    lambda_configuration: Mapping[str, object],
) -> int:
    """Check the deployed Lambda environment before any fixture write."""

    environment = lambda_configuration.get("Environment")
    variables = environment.get("Variables") if isinstance(environment, Mapping) else None
    if not isinstance(variables, Mapping):
        raise ExerciseError("deployed_configuration_missing")
    if variables.get("LEGALDESK_METADATA_TABLE_NAME") != config.table_name:
        raise ExerciseError("table_binding_mismatch")
    if variables.get("LEGALDESK_SOURCE_BUCKET") != config.bucket_name:
        raise ExerciseError("bucket_binding_mismatch")
    if variables.get("LEGALDESK_RECONCILIATION_BETA_TENANT_ID") != config.tenant_id:
        raise ExerciseError("beta_tenant_mismatch")
    if not _scope_values(
        variables.get("LEGALDESK_RECONCILIATION_UPLOAD_SCOPES"),
        tenant_id=config.tenant_id,
        matter_id=config.matter_id,
    ):
        raise ExerciseError("upload_scope_not_authorized")
    try:
        deployed_stale = int(str(variables.get("LEGALDESK_RECONCILIATION_UPLOAD_STALE_SECONDS", "")))
    except (TypeError, ValueError):
        raise ExerciseError("deployed_stale_threshold_invalid") from None
    if deployed_stale < 60:
        raise ExerciseError("deployed_stale_threshold_invalid")
    return deployed_stale


def _item(config: ExerciseConfig, *, now: datetime, stale_seconds: int | None = None) -> dict[str, object]:
    stale_at = now - timedelta(seconds=max(config.stale_seconds, stale_seconds or 0) + 60)
    return {
        "pk": config.partition_key,
        "sk": config.sort_key,
        "entityType": "Document",
        "tenantId": config.tenant_id,
        "matterId": config.matter_id,
        "documentId": config.document_id,
        "name": "phase14-reconciliation-fixture.txt",
        "s3Key": config.canonical_key,
        "quarantineS3Key": config.quarantine_key,
        "mediaType": "text/plain",
        "jurisdiction": "fictional",
        "documentDate": "2099-01-01",
        "confidentiality": "public-fictional",
        "status": "PENDING_UPLOAD",
        "malwareScanStatus": "PENDING",
        "fileSizeBytes": 34,
        "uploadedAt": stale_at.isoformat(),
    }


def _fixture_body() -> bytes:
    return b"Synthetic Phase 14 fixture only.\n"


def _decode_lambda_payload(response: Mapping[str, object]) -> Mapping[str, object]:
    if response.get("FunctionError"):
        raise ExerciseError("lambda_invocation_failed")
    payload = response.get("Payload")
    if payload is None or not hasattr(payload, "read"):
        raise ExerciseError("lambda_response_invalid")
    raw = payload.read()
    if not isinstance(raw, bytes):
        raise ExerciseError("lambda_response_invalid")
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        raise ExerciseError("lambda_response_invalid") from None
    if not isinstance(decoded, Mapping):
        raise ExerciseError("lambda_response_invalid")
    return decoded


def _assert_report(result: Mapping[str, object]) -> None:
    if result.get("status") != "completed":
        raise ExerciseError("lambda_report_incomplete")
    uploads = result.get("uploads")
    if not isinstance(uploads, Mapping):
        raise ExerciseError("lambda_upload_report_invalid")
    if uploads.get("changed") != 1 or uploads.get("failed") != 0:
        raise ExerciseError("lambda_report_expected_changed_one")


def _ddb_key(config: ExerciseConfig) -> dict[str, str]:
    return {"pk": config.partition_key, "sk": config.sort_key}


def _is_not_found(error: Exception) -> bool:
    response = getattr(error, "response", None)
    code = response.get("Error", {}).get("Code") if isinstance(response, Mapping) else None
    return code in {"404", "NoSuchKey", "NotFound", "ResourceNotFoundException"} or str(error) == "not found"


def _head_or_none(s3: Any, *, bucket: str, key: str) -> Mapping[str, object] | None:
    try:
        result = s3.head_object(Bucket=bucket, Key=key)
    except Exception as error:
        if _is_not_found(error):
            return None
        raise ExerciseError("object_check_failed") from None
    if not isinstance(result, Mapping):
        raise ExerciseError("object_check_failed")
    return result


def _verify_failed_item(table: Any, config: ExerciseConfig) -> None:
    response = table.get_item(Key=_ddb_key(config), ConsistentRead=True)
    item = response.get("Item") if isinstance(response, Mapping) else None
    if not isinstance(item, Mapping) or item.get("status") != "FAILED":
        raise ExerciseError("metadata_not_failed")
    if item.get("tenantId") != config.tenant_id or item.get("matterId") != config.matter_id:
        raise ExerciseError("metadata_scope_mismatch")


def _assert_absent(table: Any, s3: Any, config: ExerciseConfig) -> None:
    item = table.get_item(Key=_ddb_key(config), ConsistentRead=True).get("Item")
    if item is not None:
        raise ExerciseError("metadata_cleanup_incomplete")
    for key in (config.quarantine_key, config.sidecar_key):
        if _head_or_none(s3, bucket=config.bucket_name, key=key) is not None:
            raise ExerciseError("object_cleanup_incomplete")


def _delete_exact_fixture(table: Any, s3: Any, config: ExerciseConfig) -> None:
    """Delete only the two known objects and one condition-bound item."""

    for key in (config.quarantine_key, config.sidecar_key):
        s3.delete_object(Bucket=config.bucket_name, Key=key)
    table.delete_item(
        Key=_ddb_key(config),
        ConditionExpression=(
            "#entity = :entity AND #tenant = :tenant AND #matter = :matter "
            "AND #document = :document AND #quarantine = :quarantine"
        ),
        ExpressionAttributeNames={
            "#entity": "entityType",
            "#tenant": "tenantId",
            "#matter": "matterId",
            "#document": "documentId",
            "#quarantine": "quarantineS3Key",
        },
        ExpressionAttributeValues={
            ":entity": "Document",
            ":tenant": config.tenant_id,
            ":matter": config.matter_id,
            ":document": config.document_id,
            ":quarantine": config.quarantine_key,
        },
    )


def run_live(config: ExerciseConfig, *, clients: Mapping[str, object] | None = None) -> dict[str, object]:
    """Execute the one-call exercise using injected clients in tests."""

    if not config.execute or config.approval != APPROVAL_TEXT:
        raise ExerciseError("explicit_approval_required")
    if clients is None:
        try:
            import boto3
        except ImportError:
            raise ExerciseError("boto3_unavailable") from None
        session = boto3.Session(region_name=config.region)
        clients = {
            "lambda": session.client("lambda"),
            "dynamodb": session.resource("dynamodb"),
            "s3": session.client("s3"),
        }
    lambda_client = clients.get("lambda")
    dynamodb = clients.get("dynamodb")
    s3 = clients.get("s3")
    if lambda_client is None or dynamodb is None or s3 is None:
        raise ExerciseError("aws_clients_missing")
    table = dynamodb.Table(config.table_name) if hasattr(dynamodb, "Table") else dynamodb
    get_config = getattr(lambda_client, "get_function_configuration", None)
    if not callable(get_config):
        raise ExerciseError("lambda_client_invalid")
    deployed_stale = validate_deployed_scope(config, get_config(FunctionName=config.function_name))

    now = datetime.now(timezone.utc)
    item = _item(config, now=now, stale_seconds=deployed_stale)
    body = _fixture_body()
    seeded = False
    invocation_count = 0
    try:
        existing = table.get_item(Key=_ddb_key(config), ConsistentRead=True).get("Item")
        if existing is not None:
            raise ExerciseError("fixture_metadata_already_exists")
        for key in (config.quarantine_key, config.sidecar_key):
            if _head_or_none(s3, bucket=config.bucket_name, key=key) is not None:
                raise ExerciseError("fixture_object_already_exists")
        table.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(pk) AND attribute_not_exists(sk)",
        )
        seeded = True
        s3.put_object(
            Bucket=config.bucket_name,
            Key=config.quarantine_key,
            Body=body,
            ContentType="text/plain",
            Metadata={
                "tenant-id": config.tenant_id,
                "matter-id": config.matter_id,
                "document-id": config.document_id,
            },
            ServerSideEncryption="AES256",
        )
        s3.put_object(
            Bucket=config.bucket_name,
            Key=config.sidecar_key,
            Body=b'{"metadataAttributes":{}}',
            ContentType="application/json",
            Metadata={"document-id": config.document_id},
            ServerSideEncryption="AES256",
        )
        response = lambda_client.invoke(
            FunctionName=config.function_name,
            InvocationType="RequestResponse",
            Payload=b'{"source":"legaldesk.phase14.reconciliation.exercise"}',
        )
        invocation_count += 1
        result = _decode_lambda_payload(response)
        _assert_report(result)
        _verify_failed_item(table, config)
        for key in (config.quarantine_key, config.sidecar_key):
            if _head_or_none(s3, bucket=config.bucket_name, key=key) is not None:
                raise ExerciseError("objects_not_deleted")
        _delete_exact_fixture(table, s3, config)
        seeded = False
        _assert_absent(table, s3, config)
        return {
            "status": "PASS",
            "exercise": "phase14-reconciliation",
            "invocations": invocation_count,
            "uploads": result.get("uploads"),
            "fixture": {"tenantId": config.tenant_id, "matterId": config.matter_id, "cleaned": True},
        }
    finally:
        if seeded:
            try:
                _delete_exact_fixture(table, s3, config)
            except Exception:
                # Preserve the original failure category; the operator must
                # inspect exact fixture keys if cleanup itself fails.
                pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--function-name", required=True)
    parser.add_argument("--table-name", required=True)
    parser.add_argument("--bucket-name", required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--matter-id", required=True)
    parser.add_argument("--document-id", required=True, help="UUIDv4 reserved for this one synthetic exercise")
    parser.add_argument("--stale-seconds", type=int, default=86400)
    parser.add_argument("--execute", action="store_true", help="Enable the one-call AWS exercise")
    parser.add_argument("--approval", default="", help=argparse.SUPPRESS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        config = _config_from_args(args)
        if not config.execute:
            print(json.dumps({"status": "DRY_RUN", "awsCalls": 0, "exercise": "phase14-reconciliation"}))
            return 0
        report = run_live(config)
        print(json.dumps(report, sort_keys=True))
        return 0
    except ExerciseError as exc:
        print(json.dumps({"status": "BLOCKED", "reason": str(exc)}, sort_keys=True))
        return 2
    except Exception:
        # Provider exceptions can contain request IDs, resource names or
        # configuration details. Keep the operator output category-only.
        print(json.dumps({"status": "BLOCKED", "reason": "aws_operation_failed"}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
