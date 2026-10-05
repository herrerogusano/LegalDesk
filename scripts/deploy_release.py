"""Deploy one immutable, application-code-only LegalDesk release.

The command is intentionally conservative.  It defaults to a metadata-only
dry run, never accepts stack parameters from the browser or a free-form file,
and requires an explicit production acknowledgement before any write.  AWS
clients are injected in tests so the safety boundary can be exercised without
credentials, network access, or application-data calls.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence


APPROVAL = "I_UNDERSTAND_LEGALDESK_PROD_CODE_ONLY"
STACK_NAME = "LegalDeskPhase14PublicEdge"
TEMPLATE_PATH = "infra/cloudformation/phase-14-public-edge.yaml"
LAMBDA_KEY_RE = re.compile(r"^phase-14/(?P<commit>[0-9a-f]{40})/legaldesk-lambda\.zip$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
FRONTEND_FILES = ("index.html", "styles.css", "app.js", "citations.js", "diagnostics.js")
ALLOWED_CHANGES = {
    ("Modify", "AWS::Lambda::Function", "ApplicationFunction"),
    ("Modify", "AWS::ApiGatewayV2::Integration", "PublicApiIntegration"),
}
ALLOWED_PROPERTY_NAMES = {
    ("AWS::Lambda::Function", "ApplicationFunction"): {"Code"},
    ("AWS::ApiGatewayV2::Integration", "PublicApiIntegration"): {"IntegrationUri"},
}


class DeploymentError(RuntimeError):
    """Closed, operator-safe deployment diagnostic."""


class S3Like(Protocol):
    def head_object(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def get_object(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def put_object(self, **kwargs: Any) -> Mapping[str, Any]: ...


class CloudFormationLike(Protocol):
    def describe_stacks(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def get_template(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def create_change_set(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def describe_change_set(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def execute_change_set(self, **kwargs: Any) -> Mapping[str, Any]: ...


class CloudFrontLike(Protocol):
    def create_invalidation(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def get_invalidation(self, **kwargs: Any) -> Mapping[str, Any]: ...


class LambdaLike(Protocol):
    def get_function(self, **kwargs: Any) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class ReleaseArtifact:
    name: str
    path: Path
    sha256: str
    files: tuple[tuple[str, str, int], ...]


@dataclass(frozen=True, slots=True)
class ReleaseInput:
    commit: str
    release_dir: Path
    artifact_bucket: str
    artifact_key: str
    frontend_bucket: str | None = None
    distribution_id: str | None = None
    public_origin: str | None = None


@dataclass(frozen=True, slots=True)
class DeployResult:
    status: str
    commit: str
    lambda_sha256: str
    lambda_object_version: str | None
    frontend_versions: tuple[tuple[str, str | None], ...]
    change_set_name: str | None
    invalidation_id: str | None
    previous_lambda: tuple[str | None, str | None]
    checks: tuple[tuple[str, int], ...]
    previous_frontend_versions: tuple[tuple[str, str | None], ...] = ()


def _safe_json(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise DeploymentError("release_manifest_invalid") from exc
    if not isinstance(value, Mapping):
        raise DeploymentError("release_manifest_invalid")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise DeploymentError("artifact_read_failed") from exc
    return digest.hexdigest()


def _artifact_from_manifest(release_dir: Path, raw: Mapping[str, Any], name: str, filename: str) -> ReleaseArtifact:
    artifacts = raw.get("artifacts")
    if not isinstance(artifacts, list):
        raise DeploymentError("release_manifest_invalid")
    entry = next((item for item in artifacts if isinstance(item, Mapping) and item.get("name") == name), None)
    if not isinstance(entry, Mapping) or entry.get("path") != filename:
        raise DeploymentError("release_manifest_artifact_missing")
    digest = entry.get("sha256")
    if not isinstance(digest, str) or HEX64.fullmatch(digest) is None:
        raise DeploymentError("release_manifest_hash_invalid")
    path = release_dir / filename
    if not path.is_file() or path.is_symlink() or _sha256(path) != digest:
        raise DeploymentError("artifact_hash_mismatch")
    files_raw = entry.get("files")
    if not isinstance(files_raw, list):
        raise DeploymentError("release_manifest_files_invalid")
    files: list[tuple[str, str, int]] = []
    for item in files_raw:
        if not isinstance(item, Mapping) or not isinstance(item.get("path"), str):
            raise DeploymentError("release_manifest_files_invalid")
        file_hash = item.get("sha256")
        size = item.get("size")
        if not isinstance(file_hash, str) or HEX64.fullmatch(file_hash) is None or not isinstance(size, int) or size < 0:
            raise DeploymentError("release_manifest_files_invalid")
        files.append((item["path"], file_hash, size))
    return ReleaseArtifact(name, path, digest, tuple(files))


def validate_release(release_dir: Path, commit: str, artifact_bucket: str, artifact_key: str) -> tuple[ReleaseArtifact, ReleaseArtifact]:
    commit = commit.lower().strip()
    if COMMIT_RE.fullmatch(commit) is None:
        raise DeploymentError("commit_invalid")
    if not artifact_bucket or "/" in artifact_bucket or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", artifact_bucket):
        raise DeploymentError("artifact_bucket_invalid")
    match = LAMBDA_KEY_RE.fullmatch(artifact_key)
    if match is None or match.group("commit") != commit:
        raise DeploymentError("artifact_key_not_immutable")
    raw = _safe_json(release_dir / "legaldesk-release-manifest.json")
    if raw.get("schemaVersion") != 1 or raw.get("runtime") != "python3.12" or raw.get("platform") != "manylinux_x86_64":
        raise DeploymentError("release_manifest_runtime_invalid")
    application = _artifact_from_manifest(release_dir, raw, "application", "legaldesk-lambda.zip")
    frontend = _artifact_from_manifest(release_dir, raw, "frontend", "legaldesk-frontend.zip")
    with zipfile.ZipFile(frontend.path) as archive:
        names = tuple(sorted(archive.namelist()))
    if names != tuple(sorted(FRONTEND_FILES)):
        raise DeploymentError("frontend_archive_contents_invalid")
    manifest_frontend = {item[0] for item in frontend.files}
    if manifest_frontend != set(FRONTEND_FILES):
        raise DeploymentError("frontend_manifest_contents_invalid")
    return application, frontend


def _object_hash(s3: S3Like, bucket: str, key: str, head: Mapping[str, Any]) -> str | None:
    metadata = head.get("Metadata")
    if isinstance(metadata, Mapping):
        candidate = metadata.get("legaldesk-sha256") or metadata.get("sha256")
        if isinstance(candidate, str) and HEX64.fullmatch(candidate.lower()):
            return candidate.lower()
    try:
        body = s3.get_object(Bucket=bucket, Key=key).get("Body")
        if hasattr(body, "read"):
            payload = body.read()
        else:
            payload = body
    except Exception as exc:
        raise DeploymentError("existing_object_read_failed") from exc
    if not isinstance(payload, bytes):
        raise DeploymentError("existing_object_read_failed")
    return hashlib.sha256(payload).hexdigest()


def _head(s3: S3Like, bucket: str, key: str) -> Mapping[str, Any] | None:
    try:
        value = s3.head_object(Bucket=bucket, Key=key)
    except Exception as exc:
        response = getattr(exc, "response", None)
        code = response.get("Error", {}).get("Code") if isinstance(response, Mapping) else None
        if code in {"404", "NoSuchKey", "NotFound"}:
            return None
        raise DeploymentError("object_head_failed") from exc
    return value if isinstance(value, Mapping) else None


def _put_immutable(s3: S3Like, bucket: str, key: str, artifact: ReleaseArtifact, commit: str) -> str | None:
    existing = _head(s3, bucket, key)
    if existing is not None:
        if _object_hash(s3, bucket, key, existing) != artifact.sha256:
            raise DeploymentError("immutable_artifact_conflict")
        version = existing.get("VersionId")
        return version if isinstance(version, str) else None
    try:
        response = s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=artifact.path.read_bytes(),
            ServerSideEncryption="AES256",
            Metadata={"legaldesk-sha256": artifact.sha256, "legaldesk-commit": commit},
        )
    except Exception as exc:
        raise DeploymentError("artifact_upload_failed") from exc
    version = response.get("VersionId") if isinstance(response, Mapping) else None
    return version if isinstance(version, str) else None


def _put_frontend(s3: S3Like, bucket: str, artifact: ReleaseArtifact, commit: str) -> tuple[tuple[str, str | None], ...]:
    versions: list[tuple[str, str | None]] = []
    with zipfile.ZipFile(artifact.path) as archive:
        for name, file_hash, size in artifact.files:
            if name not in FRONTEND_FILES:
                raise DeploymentError("frontend_manifest_contents_invalid")
            payload = archive.read(name)
            if len(payload) != size or hashlib.sha256(payload).hexdigest() != file_hash:
                raise DeploymentError("frontend_file_hash_mismatch")
            existing = _head(s3, bucket, name)
            if existing is not None and _object_hash(s3, bucket, name, existing) == file_hash and existing.get("CacheControl") == "no-cache":
                version = existing.get("VersionId")
                versions.append((name, version if isinstance(version, str) else None))
                continue
            try:
                response = s3.put_object(
                    Bucket=bucket,
                    Key=name,
                    Body=payload,
                    ContentType={"html": "text/html; charset=utf-8", "css": "text/css; charset=utf-8", "js": "application/javascript; charset=utf-8"}.get(name.rsplit(".", 1)[-1], "application/octet-stream"),
                    CacheControl="no-cache",
                    ServerSideEncryption="AES256",
                    Metadata={"legaldesk-sha256": file_hash, "legaldesk-commit": commit},
                )
            except Exception as exc:
                raise DeploymentError("frontend_upload_failed") from exc
            version = response.get("VersionId") if isinstance(response, Mapping) else None
            versions.append((name, version if isinstance(version, str) else None))
    return tuple(versions)


def _load_yaml_text(text: str) -> Mapping[str, Any]:
    try:
        import yaml
    except ImportError as exc:
        raise DeploymentError("yaml_parser_unavailable") from exc

    class Loader(yaml.SafeLoader):
        pass

    def unknown(loader: Any, tag_suffix: str, node: Any) -> Any:
        intrinsic = {
            "Ref": "Ref",
            "Sub": "Fn::Sub",
            "GetAtt": "Fn::GetAtt",
            "Join": "Fn::Join",
            "If": "Fn::If",
            "Select": "Fn::Select",
            "Equals": "Fn::Equals",
            "Not": "Fn::Not",
        }.get(tag_suffix)
        if intrinsic is None:
            raise DeploymentError("template_intrinsic_invalid")
        if isinstance(node, yaml.ScalarNode):
            value = loader.construct_scalar(node)
        elif isinstance(node, yaml.SequenceNode):
            value = loader.construct_sequence(node)
        else:
            value = loader.construct_mapping(node)
        if intrinsic == "Fn::GetAtt" and isinstance(value, str):
            value = value.split(".", 1)
        return {intrinsic: value}

    Loader.add_multi_constructor("!", unknown)
    try:
        value = yaml.load(text, Loader=Loader)
    except (OSError, yaml.YAMLError) as exc:
        raise DeploymentError("template_invalid") from exc
    if not isinstance(value, Mapping):
        raise DeploymentError("template_invalid")
    return value


def _load_yaml(path: Path) -> Mapping[str, Any]:
    try:
        return _load_yaml_text(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise DeploymentError("template_invalid") from exc


def _templates_code_compatible(local: Mapping[str, Any], deployed: Mapping[str, Any]) -> bool:
    """Require the checked-in resource graph to equal the deployed graph."""

    return local == deployed


def _template_parameters(template: Mapping[str, Any]) -> set[str]:
    parameters = template.get("Parameters")
    if not isinstance(parameters, Mapping):
        raise DeploymentError("template_parameters_missing")
    return {str(key) for key in parameters}


def _stack(stack_response: Mapping[str, Any]) -> Mapping[str, Any]:
    stacks = stack_response.get("Stacks")
    if not isinstance(stacks, list) or len(stacks) != 1 or not isinstance(stacks[0], Mapping):
        raise DeploymentError("stack_inventory_invalid")
    stack = stacks[0]
    if stack.get("StackName") != STACK_NAME:
        raise DeploymentError("stack_inventory_invalid")
    if stack.get("StackStatus") not in {"CREATE_COMPLETE", "UPDATE_COMPLETE"}:
        raise DeploymentError("stack_not_updateable")
    return stack


def _stack_status(stack_response: Mapping[str, Any]) -> Mapping[str, Any]:
    stacks = stack_response.get("Stacks")
    if not isinstance(stacks, list) or len(stacks) != 1 or not isinstance(stacks[0], Mapping):
        raise DeploymentError("stack_inventory_invalid")
    stack = stacks[0]
    if stack.get("StackName") != STACK_NAME:
        raise DeploymentError("stack_inventory_invalid")
    return stack


def build_previous_value_parameters(stack: Mapping[str, Any], template: Mapping[str, Any], artifact_bucket: str, artifact_key: str, artifact_version: str | None) -> list[dict[str, Any]]:
    current = stack.get("Parameters")
    if not isinstance(current, list):
        raise DeploymentError("stack_parameters_missing")
    names = {item.get("ParameterKey") for item in current if isinstance(item, Mapping)}
    expected = _template_parameters(template)
    if names != expected:
        raise DeploymentError("template_parameter_set_changed")
    if not artifact_version:
        raise DeploymentError("artifact_version_missing")
    result: list[dict[str, Any]] = []
    for name in sorted(expected):
        if name == "ApplicationCodeBucket":
            result.append({"ParameterKey": name, "ParameterValue": artifact_bucket})
        elif name == "ApplicationCodeKey":
            result.append({"ParameterKey": name, "ParameterValue": artifact_key})
        elif name == "ApplicationCodeVersion":
            result.append({"ParameterKey": name, "ParameterValue": artifact_version})
        else:
            result.append({"ParameterKey": name, "UsePreviousValue": True})
    return result


def _change_properties(change: Mapping[str, Any]) -> set[str]:
    details = change.get("Details")
    result: set[str] = set()
    if isinstance(details, list):
        for detail in details:
            if isinstance(detail, Mapping):
                target = detail.get("Target")
                if isinstance(target, Mapping) and isinstance(target.get("Name"), str):
                    result.add(target["Name"])
                if isinstance(detail.get("Name"), str):
                    result.add(detail["Name"])
    resource = change.get("ResourceChange")
    if isinstance(resource, Mapping) and isinstance(resource.get("Details"), list):
        for detail in resource["Details"]:
            if isinstance(detail, Mapping) and isinstance(detail.get("Target"), Mapping):
                name = detail["Target"].get("Name")
                if isinstance(name, str):
                    result.add(name)
    return result


def validate_change_set(changes: Iterable[Mapping[str, Any]]) -> None:
    observed: list[tuple[str, str, str]] = []
    for wrapper in changes:
        change = wrapper.get("ResourceChange") if isinstance(wrapper, Mapping) else None
        if not isinstance(change, Mapping):
            raise DeploymentError("change_set_shape_invalid")
        action = change.get("Action")
        resource_type = change.get("ResourceType")
        logical_id = change.get("LogicalResourceId")
        triple = (action, resource_type, logical_id)
        if triple not in ALLOWED_CHANGES:
            raise DeploymentError("change_set_scope_violation")
        if change.get("Replacement") not in (None, "False"):
            raise DeploymentError("change_set_replacement_forbidden")
        names = _change_properties(wrapper)
        allowed = ALLOWED_PROPERTY_NAMES[(resource_type, logical_id)]
        if names and not names.issubset(allowed):
            raise DeploymentError("change_set_property_violation")
        observed.append(triple)
    if not observed:
        raise DeploymentError("change_set_empty")
    if len(observed) != len(set(observed)):
        raise DeploymentError("change_set_duplicate")


def _decode_body(value: Any) -> bytes:
    if hasattr(value, "read"):
        value = value.read()
    if not isinstance(value, bytes):
        raise DeploymentError("provider_response_invalid")
    return value


def _public_checks(origin: str, expected_files: Mapping[str, str], opener: Callable[..., Any] | None = None) -> tuple[tuple[str, int], ...]:
    if not re.fullmatch(r"https://[^/]+", origin):
        raise DeploymentError("public_origin_invalid")
    if opener is None:
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
                return None

        opener_obj = urllib.request.build_opener(NoRedirect())
        opener = opener_obj.open
    checks: list[tuple[str, int]] = []
    for path, expected in (("/", 200), ("/logout", 302), ("/api/me", 403)):
        request = urllib.request.Request(origin + path, method="GET")
        try:
            response = opener(request, timeout=10)
            status_value = getattr(response, "status", None)
            status = int(status_value if status_value is not None else response.getcode())
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
        except (OSError, TimeoutError) as exc:
            raise DeploymentError("public_check_failed") from exc
        if status != expected:
            raise DeploymentError("public_check_unexpected_status")
        checks.append((path, status))
    for name, expected_hash in expected_files.items():
        request = urllib.request.Request(origin + "/" + name, method="GET")
        try:
            response = opener(request, timeout=10)
            status_value = getattr(response, "status", None)
            status = int(status_value if status_value is not None else response.getcode())
            body = response.read() if hasattr(response, "read") else b""
        except urllib.error.HTTPError as exc:
            raise DeploymentError("public_asset_check_failed") from exc
        except (OSError, TimeoutError) as exc:
            raise DeploymentError("public_asset_check_failed") from exc
        if status != 200 or not isinstance(body, bytes) or hashlib.sha256(body).hexdigest() != expected_hash:
            raise DeploymentError("public_asset_hash_mismatch")
        checks.append(("/" + name, status))
    return tuple(checks)


def _record(path: Path | None, result: DeployResult) -> None:
    if path is None:
        return
    payload = {
        "schemaVersion": 1,
        "status": result.status,
        "commit": result.commit,
        "lambdaSha256": result.lambda_sha256,
        "lambdaObjectVersion": result.lambda_object_version,
        "frontendVersions": dict(result.frontend_versions),
        "changeSetName": result.change_set_name,
        "invalidationId": result.invalidation_id,
        "previousLambda": {"key": result.previous_lambda[0], "version": result.previous_lambda[1]},
        "checks": dict(result.checks),
        "previousFrontendVersions": dict(result.previous_frontend_versions),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8", newline="\n")


def _existing_frontend_versions(s3: S3Like, bucket: str) -> tuple[tuple[str, str | None], ...]:
    result: list[tuple[str, str | None]] = []
    for name in FRONTEND_FILES:
        current = _head(s3, bucket, name)
        version = current.get("VersionId") if current is not None else None
        result.append((name, version if isinstance(version, str) else None))
    return tuple(result)


def deploy(
    config: ReleaseInput,
    *,
    cfn: CloudFormationLike,
    s3: S3Like,
    cloudfront: CloudFrontLike,
    template_path: Path,
    change_set_name: str,
    execution_role_arn: str | None = None,
    execute: bool = False,
    approval: str = "",
    public_origin: str | None = None,
    record_path: Path | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    public_opener: Callable[..., Any] | None = None,
    lambda_client: LambdaLike | None = None,
) -> DeployResult:
    application, frontend = validate_release(config.release_dir, config.commit, config.artifact_bucket, config.artifact_key)
    if not execute:
        result = DeployResult("DRY_RUN", config.commit, application.sha256, None, (), None, None, (None, None), ())
        _record(record_path, result)
        return result
    if approval != APPROVAL:
        raise DeploymentError("explicit_production_approval_required")
    template = _load_yaml(template_path)
    stack = _stack(cfn.describe_stacks(StackName=STACK_NAME))
    try:
        deployed_raw = cfn.get_template(StackName=STACK_NAME, TemplateStage="Original").get("TemplateBody")
        if isinstance(deployed_raw, Mapping):
            deployed_template = deployed_raw
        else:
            if isinstance(deployed_raw, bytes):
                deployed_raw = deployed_raw.decode("utf-8")
            deployed_template = _load_yaml_text(deployed_raw) if isinstance(deployed_raw, str) else None
        if not isinstance(deployed_template, Mapping) or not _templates_code_compatible(template, deployed_template):
            raise DeploymentError("deployed_template_scope_changed")
    except DeploymentError:
        raise
    except Exception as exc:
        raise DeploymentError("deployed_template_unavailable") from exc
    old_parameters = stack.get("Parameters")
    old_values = {item.get("ParameterKey"): item.get("ParameterValue") for item in old_parameters or [] if isinstance(item, Mapping)}
    outputs_before = {item.get("OutputKey"): item.get("OutputValue") for item in stack.get("Outputs", []) if isinstance(item, Mapping)}
    frontend_bucket = config.frontend_bucket or outputs_before.get("FrontendBucketName")
    distribution_id = config.distribution_id or outputs_before.get("CloudFrontDistributionId")
    if not isinstance(frontend_bucket, str) or not isinstance(distribution_id, str):
        raise DeploymentError("stack_outputs_missing")
    rollback_frontend = _existing_frontend_versions(s3, frontend_bucket)
    prepared = DeployResult("PREPARED", config.commit, application.sha256, None, rollback_frontend, None, None, (old_values.get("ApplicationCodeKey"), old_values.get("ApplicationCodeVersion")), (), rollback_frontend)
    _record(record_path, prepared)
    artifact_version: str | None = None
    frontend_versions = rollback_frontend
    try:
        artifact_version = _put_immutable(s3, config.artifact_bucket, config.artifact_key, application, config.commit)
        params = build_previous_value_parameters(stack, template, config.artifact_bucket, config.artifact_key, artifact_version)
        create_kwargs: dict[str, Any] = {
            "StackName": STACK_NAME,
            "ChangeSetName": change_set_name,
            "ChangeSetType": "UPDATE",
            "UsePreviousTemplate": True,
            "Parameters": params,
            "Capabilities": ["CAPABILITY_IAM"],
            "Description": f"LegalDesk code-only release {config.commit}",
        }
        if execution_role_arn:
            create_kwargs["RoleARN"] = execution_role_arn
        try:
            created = cfn.create_change_set(**create_kwargs)
        except Exception as exc:
            raise DeploymentError("change_set_create_failed") from exc
        change_id = created.get("Id") if isinstance(created, Mapping) else None
        if not isinstance(change_id, str):
            change_id = change_set_name
        described: Mapping[str, Any] | None = None
        for _ in range(30):
            try:
                described = cfn.describe_change_set(StackName=STACK_NAME, ChangeSetName=change_id)
            except Exception as exc:
                raise DeploymentError("change_set_describe_failed") from exc
            status = described.get("Status")
            if status in {"CREATE_COMPLETE", "FAILED"}:
                break
            sleeper(2)
        if not isinstance(described, Mapping) or described.get("Status") != "CREATE_COMPLETE":
            raise DeploymentError("change_set_not_ready")
        changes = described.get("Changes")
        if not isinstance(changes, list):
            raise DeploymentError("change_set_shape_invalid")
        validate_change_set(changes)
        try:
            cfn.execute_change_set(StackName=STACK_NAME, ChangeSetName=change_id)
        except Exception as exc:
            raise DeploymentError("change_set_execute_failed") from exc
        current: Mapping[str, Any] | None = None
        for _ in range(60):
            current = _stack_status(cfn.describe_stacks(StackName=STACK_NAME))
            if current.get("StackStatus") == "UPDATE_COMPLETE":
                break
            if str(current.get("StackStatus", "")).endswith("_FAILED") or current.get("StackStatus") == "UPDATE_ROLLBACK_COMPLETE":
                raise DeploymentError("stack_update_failed")
            sleeper(5)
        else:
            raise DeploymentError("stack_update_timeout")
        outputs = {item.get("OutputKey"): item.get("OutputValue") for item in (current or {}).get("Outputs", []) if isinstance(item, Mapping)}
        function_arn = outputs.get("ApplicationFunctionArn")
        if lambda_client is None or not isinstance(function_arn, str):
            raise DeploymentError("lambda_verification_unavailable")
        try:
            function = lambda_client.get_function(FunctionName=function_arn)
        except Exception as exc:
            raise DeploymentError("lambda_verification_failed") from exc
        configuration = function.get("Configuration") if isinstance(function, Mapping) else None
        code_sha = configuration.get("CodeSha256") if isinstance(configuration, Mapping) else None
        expected_code_sha = base64.b64encode(bytes.fromhex(application.sha256)).decode("ascii")
        if code_sha != expected_code_sha:
            raise DeploymentError("lambda_code_hash_mismatch")
        frontend_versions = _put_frontend(s3, frontend_bucket, frontend, config.commit)
        try:
            invalidation = cloudfront.create_invalidation(
                DistributionId=distribution_id,
                InvalidationBatch={"Paths": {"Quantity": 6, "Items": ["/", *[f"/{name}" for name in FRONTEND_FILES]]}, "CallerReference": f"legaldesk-{config.commit}"},
            )
        except Exception as exc:
            raise DeploymentError("cloudfront_invalidation_failed") from exc
        invalidation_id = invalidation.get("Invalidation", {}).get("Id") if isinstance(invalidation, Mapping) and isinstance(invalidation.get("Invalidation"), Mapping) else None
        if not isinstance(invalidation_id, str):
            raise DeploymentError("cloudfront_invalidation_invalid")
        for _ in range(30):
            try:
                state = cloudfront.get_invalidation(DistributionId=distribution_id, Id=invalidation_id)
            except Exception as exc:
                raise DeploymentError("cloudfront_invalidation_read_failed") from exc
            status = state.get("Invalidation", {}).get("Status") if isinstance(state, Mapping) and isinstance(state.get("Invalidation"), Mapping) else None
            if status == "Completed":
                break
            if status == "Failed":
                raise DeploymentError("cloudfront_invalidation_failed")
            sleeper(5)
        else:
            raise DeploymentError("cloudfront_invalidation_timeout")
        expected_files = {name: digest for name, digest, _ in frontend.files}
        check_origin = public_origin or outputs.get("CloudFrontDistributionDomainName", "")
        if isinstance(check_origin, str) and not check_origin.startswith("https://"):
            check_origin = "https://" + check_origin
        checks = _public_checks(check_origin, expected_files, public_opener)
        result = DeployResult("PASS", config.commit, application.sha256, artifact_version, frontend_versions, change_set_name, invalidation_id, (old_values.get("ApplicationCodeKey"), old_values.get("ApplicationCodeVersion")), checks, rollback_frontend)
        _record(record_path, result)
        return result
    except DeploymentError:
        _record(record_path, DeployResult("FAILED", config.commit, application.sha256, artifact_version, frontend_versions, change_set_name, None, (old_values.get("ApplicationCodeKey"), old_values.get("ApplicationCodeVersion")), (), rollback_frontend))
        raise
    except Exception as exc:
        _record(record_path, DeployResult("FAILED", config.commit, application.sha256, artifact_version, frontend_versions, change_set_name, None, (old_values.get("ApplicationCodeKey"), old_values.get("ApplicationCodeVersion")), (), rollback_frontend))
        raise DeploymentError("deployment_failed") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--release-dir", required=True, type=Path)
    parser.add_argument("--artifact-bucket", required=True)
    parser.add_argument("--artifact-key", required=True)
    parser.add_argument("--template", type=Path, default=Path(TEMPLATE_PATH))
    parser.add_argument("--region", default="eu-west-1")
    parser.add_argument("--change-set-name", default="")
    parser.add_argument("--execution-role-arn", default="")
    parser.add_argument("--public-origin", default="")
    parser.add_argument("--record", type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--approval", default="", help=argparse.SUPPRESS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        if args.execute and args.approval != APPROVAL:
            raise DeploymentError("explicit_production_approval_required")
        if args.execute:
            import boto3

            session = boto3.Session(region_name=args.region)
            cfn = session.client("cloudformation")
            s3 = session.client("s3")
            cloudfront = session.client("cloudfront")
            lambda_client = session.client("lambda")
        else:
            class NoAws:
                def __getattr__(self, name: str) -> Any:
                    raise DeploymentError("dry_run_aws_client_not_used")

            cfn = s3 = cloudfront = NoAws()
            lambda_client = NoAws()
        commit = args.commit.lower()
        change_set = args.change_set_name or f"legaldesk-{commit[:12]}"
        config = ReleaseInput(commit, args.release_dir, args.artifact_bucket, args.artifact_key, public_origin=args.public_origin or None)
        result = deploy(config, cfn=cfn, s3=s3, cloudfront=cloudfront, lambda_client=lambda_client, template_path=args.template, change_set_name=change_set, execution_role_arn=args.execution_role_arn or None, execute=args.execute, approval=args.approval, public_origin=args.public_origin or None, record_path=args.record)
        print(json.dumps({"status": result.status, "commit": result.commit, "lambdaSha256": result.lambda_sha256, "changeSetName": result.change_set_name}, sort_keys=True))
        return 0
    except DeploymentError as exc:
        print(json.dumps({"status": "BLOCKED", "reason": str(exc)}, sort_keys=True))
        return 2
    except Exception:
        print(json.dumps({"status": "BLOCKED", "reason": "deployment_failed"}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
