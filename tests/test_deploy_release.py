from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from typing import Any

from scripts.deploy_release import (
    APPROVAL,
    DeploymentError,
    ReleaseInput,
    build_previous_value_parameters,
    deploy,
    validate_change_set,
    validate_release,
)


TEMPLATE = """
Parameters:
  ApplicationCodeBucket: {Type: String}
  ApplicationCodeKey: {Type: String}
  ApplicationCodeVersion: {Type: String}
  TrustedEdgeSecret: {Type: String, NoEcho: true}
Resources:
  ApplicationFunction:
    Type: AWS::Lambda::Function
    Properties:
      Code: {S3Key: !Ref ApplicationCodeKey}
  PublicApiIntegration:
    Type: AWS::ApiGatewayV2::Integration
    Properties:
      IntegrationUri: !Ref ApplicationCodeKey
"""


def _manifest(root: Path) -> None:
    app = root / "legaldesk-lambda.zip"
    front = root / "legaldesk-frontend.zip"
    with zipfile.ZipFile(app, "w") as archive:
        archive.writestr("legaldesk/app.py", b"pass\n")
    with zipfile.ZipFile(front, "w") as archive:
        for name in ("index.html", "styles.css", "app.js", "citations.js", "diagnostics.js"):
            archive.writestr(name, (name + "\n").encode())

    def details(path: Path, names: list[str]) -> dict[str, Any]:
        with zipfile.ZipFile(path) as archive:
            files = []
            for name in names:
                data = archive.read(name)
                files.append({"path": name, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)})
        return {"name": "application" if path == app else "frontend", "path": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "files": files}

    (root / "legaldesk-release-manifest.json").write_text(
        json.dumps({"schemaVersion": 1, "runtime": "python3.12", "platform": "manylinux_x86_64", "artifacts": [details(app, ["legaldesk/app.py"]), details(front, ["index.html", "styles.css", "app.js", "citations.js", "diagnostics.js"])]}),
        encoding="utf-8",
    )


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], tuple[bytes, str, str]] = {}
        self.puts = 0

    def head_object(self, **kwargs: Any) -> dict[str, Any]:
        value = self.objects.get((kwargs["Bucket"], kwargs["Key"]))
        if value is None:
            error = RuntimeError("not found")
            error.response = {"Error": {"Code": "404"}}  # type: ignore[attr-defined]
            raise error
        body, version, digest = value
        return {"ContentLength": len(body), "VersionId": version, "CacheControl": "no-cache", "Metadata": {"legaldesk-sha256": digest}}

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        body = self.objects[(kwargs["Bucket"], kwargs["Key"])][0]
        return {"Body": io.BytesIO(body)}

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        if kwargs.get("IfNoneMatch") == "*" and (kwargs["Bucket"], kwargs["Key"]) in self.objects:
            error = RuntimeError("precondition failed")
            error.response = {"Error": {"Code": "PreconditionFailed"}}  # type: ignore[attr-defined]
            raise error
        body = kwargs["Body"]
        digest = kwargs["Metadata"]["legaldesk-sha256"]
        self.puts += 1
        version = f"v{self.puts}"
        self.objects[(kwargs["Bucket"], kwargs["Key"])] = (body, version, digest)
        return {"VersionId": version}


class FakeCfn:
    def __init__(self, template: str) -> None:
        self.template = template
        self.created = False
        self.executed = False
        self.stack_calls = 0
        self.parameters = [
            {"ParameterKey": "ApplicationCodeBucket", "ParameterValue": "artifacts"},
            {"ParameterKey": "ApplicationCodeKey", "ParameterValue": "phase-14/" + "a" * 40 + "/legaldesk-lambda.zip"},
            {"ParameterKey": "ApplicationCodeVersion", "ParameterValue": "old"},
            {"ParameterKey": "TrustedEdgeSecret", "ParameterValue": "****"},
        ]
        self.change_parameters: list[dict[str, Any]] = []

    def describe_stacks(self, **kwargs: Any) -> dict[str, Any]:
        self.stack_calls += 1
        status = "UPDATE_COMPLETE" if self.executed else "UPDATE_COMPLETE"
        return {"Stacks": [{"StackName": "LegalDeskPhase14PublicEdge", "StackStatus": status, "Parameters": self.parameters, "Outputs": [{"OutputKey": "FrontendBucketName", "OutputValue": "frontend"}, {"OutputKey": "CloudFrontDistributionId", "OutputValue": "DIST"}, {"OutputKey": "CloudFrontDistributionDomainName", "OutputValue": "edge.example"}, {"OutputKey": "ApplicationFunctionArn", "OutputValue": "arn:aws:lambda:eu-west-1:123456789012:function:app"}]}]}

    def get_template(self, **kwargs: Any) -> dict[str, Any]:
        return {"TemplateBody": self.template}

    def create_change_set(self, **kwargs: Any) -> dict[str, Any]:
        self.created = True
        self.change_parameters = kwargs["Parameters"]
        self.parameters = kwargs["Parameters"]
        return {"Id": "cs-1"}

    def describe_change_set(self, **kwargs: Any) -> dict[str, Any]:
        return {"Status": "CREATE_COMPLETE", "Changes": [{"ResourceChange": {"Action": "Modify", "ResourceType": "AWS::Lambda::Function", "LogicalResourceId": "ApplicationFunction", "Replacement": "False", "Details": [{"Target": {"Name": "Code"}}]}}, {"ResourceChange": {"Action": "Modify", "ResourceType": "AWS::ApiGatewayV2::Integration", "LogicalResourceId": "PublicApiIntegration", "Replacement": "False", "Details": [{"Target": {"Name": "IntegrationUri"}}]}}]}

    def execute_change_set(self, **kwargs: Any) -> dict[str, Any]:
        self.executed = True
        return {}


class FakeCloudFront:
    def __init__(self, *, complete: bool = True) -> None:
        self.polls = 0
        self.complete = complete

    def create_invalidation(self, **kwargs: Any) -> dict[str, Any]:
        self.request = kwargs
        return {"Invalidation": {"Id": "I1"}}

    def get_invalidation(self, **kwargs: Any) -> dict[str, Any]:
        self.polls += 1
        return {"Invalidation": {"Status": "Completed" if self.complete else "InProgress"}}


class FakeLambda:
    def __init__(self, digest: str) -> None:
        self.digest = digest

    def get_function(self, **kwargs: Any) -> dict[str, Any]:
        import base64

        return {"Configuration": {"CodeSha256": base64.b64encode(bytes.fromhex(self.digest)).decode("ascii")}}


class NoHeadS3(FakeS3):
    def head_object(self, **kwargs: Any) -> dict[str, Any]:
        error = RuntimeError("forbidden head")
        error.response = {"Error": {"Code": "403"}}  # type: ignore[attr-defined]
        raise error


class ProgressCfn(FakeCfn):
    def describe_stacks(self, **kwargs: Any) -> dict[str, Any]:
        response = super().describe_stacks(**kwargs)
        if self.executed and self.stack_calls == 2:
            response["Stacks"][0]["StackStatus"] = "UPDATE_IN_PROGRESS"
        return response


class StableCompleteThenProgressCfn(FakeCfn):
    """Expose the prior stable status before this execution progresses."""

    def create_change_set(self, **kwargs: Any) -> dict[str, Any]:
        self.created = True
        self.change_parameters = kwargs["Parameters"]
        self.pending_parameters = kwargs["Parameters"]
        return {"Id": "cs-1"}

    def describe_stacks(self, **kwargs: Any) -> dict[str, Any]:
        response = super().describe_stacks(**kwargs)
        if self.executed and self.stack_calls == 3:
            response["Stacks"][0]["StackStatus"] = "UPDATE_IN_PROGRESS"
        elif self.executed and self.stack_calls >= 4:
            self.parameters = self.pending_parameters
            response["Stacks"][0]["StackStatus"] = "UPDATE_COMPLETE"
            response["Stacks"][0]["Parameters"] = self.parameters
        return response


class RollbackCompleteCfn(FakeCfn):
    def describe_stacks(self, **kwargs: Any) -> dict[str, Any]:
        response = super().describe_stacks(**kwargs)
        response["Stacks"][0]["StackStatus"] = "UPDATE_ROLLBACK_COMPLETE"
        return response


class RollbackThenProgressCfn(StableCompleteThenProgressCfn):
    def describe_stacks(self, **kwargs: Any) -> dict[str, Any]:
        response = super().describe_stacks(**kwargs)
        if not self.executed and self.stack_calls == 1:
            response["Stacks"][0]["StackStatus"] = "UPDATE_ROLLBACK_COMPLETE"
        elif self.executed and self.stack_calls == 2:
            response["Stacks"][0]["StackStatus"] = "UPDATE_ROLLBACK_COMPLETE"
        return response


class DriftCfn(FakeCfn):
    def get_template(self, **kwargs: Any) -> dict[str, Any]:
        return {"TemplateBody": self.template.replace("IntegrationUri: !Ref ApplicationCodeKey", "IntegrationUri: !Ref ApplicationCodeVersion")}


class ExecuteErrorCfn(FakeCfn):
    def execute_change_set(self, **kwargs: Any) -> dict[str, Any]:
        raise ValueError("provider failure")


class ReleaseDeployTests(unittest.TestCase):
    def test_manifest_and_frontend_archive_are_strict(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _manifest(root)
            app, frontend = validate_release(root, "a" * 40, "artifacts", "phase-14/" + "a" * 40 + "/legaldesk-lambda.zip")
            self.assertEqual(app.name, "application")
            self.assertEqual({item[0] for item in frontend.files}, {"index.html", "styles.css", "app.js", "citations.js", "diagnostics.js"})
            with self.assertRaises(DeploymentError):
                validate_release(root, "b" * 40, "artifacts", "phase-14/" + "a" * 40 + "/legaldesk-lambda.zip")

    def test_previous_parameters_preserve_noecho_and_only_code_changes(self) -> None:
        stack = {"Parameters": [{"ParameterKey": "ApplicationCodeBucket"}, {"ParameterKey": "ApplicationCodeKey"}, {"ParameterKey": "ApplicationCodeVersion"}, {"ParameterKey": "TrustedEdgeSecret"}]}
        template = {"Parameters": {"ApplicationCodeBucket": {}, "ApplicationCodeKey": {}, "ApplicationCodeVersion": {}, "TrustedEdgeSecret": {"NoEcho": True}}}
        result = build_previous_value_parameters(stack, template, "artifacts", "phase-14/" + "b" * 40 + "/legaldesk-lambda.zip", "v2")
        by_name = {item["ParameterKey"]: item for item in result}
        self.assertEqual(by_name["ApplicationCodeBucket"]["ParameterValue"], "artifacts")
        self.assertEqual(by_name["ApplicationCodeVersion"]["ParameterValue"], "v2")
        self.assertTrue(by_name["TrustedEdgeSecret"]["UsePreviousValue"])
        self.assertNotIn("ParameterValue", by_name["TrustedEdgeSecret"])

    def test_change_set_rejects_resource_and_replacement(self) -> None:
        allowed = [{"ResourceChange": {"Action": "Modify", "ResourceType": "AWS::Lambda::Function", "LogicalResourceId": "ApplicationFunction", "Replacement": "False"}}]
        validate_change_set(allowed)
        with self.assertRaises(DeploymentError):
            validate_change_set([{ "ResourceChange": {"Action": "Add", "ResourceType": "AWS::IAM::Role", "LogicalResourceId": "Unexpected"}}])
        with self.assertRaises(DeploymentError):
            validate_change_set([{ "ResourceChange": {"Action": "Modify", "ResourceType": "AWS::Lambda::Function", "LogicalResourceId": "ApplicationFunction", "Replacement": "True"}}])

    def test_fake_aws_deploy_uploads_once_and_records_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _manifest(root)
            template_path = root / "template.yaml"
            template_path.write_text(TEMPLATE, encoding="utf-8")
            cfn = FakeCfn(TEMPLATE)
            s3 = FakeS3()
            front = FakeCloudFront()
            release = ReleaseInput("b" * 40, root, "artifacts", "phase-14/" + "b" * 40 + "/legaldesk-lambda.zip")
            app_hash = json.loads((root / "legaldesk-release-manifest.json").read_text())["artifacts"][0]["sha256"]
            frontend_hashes = {name: hashlib.sha256((name + "\n").encode()).hexdigest() for name in ("index.html", "styles.css", "app.js", "citations.js", "diagnostics.js")}

            def opener(request: Any, timeout: int) -> Any:
                path = request.full_url.rsplit("edge.example", 1)[-1]
                status = {"/": 200, "/logout": 302, "/api/me": 403}.get(path, 200)
                body = frontend_hashes and next(((name + "\n").encode() for name, digest in frontend_hashes.items() if "/" + name == path), b"")
                return type("Response", (), {"status": status, "read": lambda self: body})()

            result = deploy(release, cfn=cfn, s3=s3, cloudfront=front, lambda_client=FakeLambda(app_hash), template_path=template_path, change_set_name="cs", execution_role_arn="arn:aws:iam::123456789012:role/cfn", execute=True, approval=APPROVAL, public_origin="https://edge.example", public_opener=opener, sleeper=lambda _: None)
            self.assertEqual(result.status, "PASS")
            self.assertEqual(result.previous_lambda[1], "old")
            self.assertEqual(front.request["InvalidationBatch"]["Paths"]["Quantity"], 6)
            self.assertTrue(cfn.change_parameters)
            first_puts = s3.puts
            cfn2 = FakeCfn(TEMPLATE)
            result2 = deploy(release, cfn=cfn2, s3=s3, cloudfront=front, lambda_client=FakeLambda(app_hash), template_path=template_path, change_set_name="cs2", execution_role_arn="arn:aws:iam::123456789012:role/cfn", execute=True, approval=APPROVAL, public_opener=opener, sleeper=lambda _: None)
            self.assertEqual(result2.status, "PASS")
            self.assertEqual(s3.puts, first_puts)

    def test_missing_approval_performs_no_aws_call(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _manifest(root)
            template_path = root / "template.yaml"
            template_path.write_text(TEMPLATE, encoding="utf-8")

            class NoCalls:
                def __getattr__(self, name: str) -> Any:
                    raise AssertionError("AWS call before approval")

            with self.assertRaises(DeploymentError) as error:
                deploy(ReleaseInput("a" * 40, root, "artifacts", "phase-14/" + "a" * 40 + "/legaldesk-lambda.zip"), cfn=NoCalls(), s3=NoCalls(), cloudfront=NoCalls(), template_path=template_path, change_set_name="cs", execute=True, approval="")
            self.assertEqual(str(error.exception), "explicit_production_approval_required")

    def test_forbidden_change_is_rejected_before_execute(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _manifest(root)
            template_path = root / "template.yaml"
            template_path.write_text(TEMPLATE, encoding="utf-8")

            class BadCfn(FakeCfn):
                def describe_change_set(self, **kwargs: Any) -> dict[str, Any]:
                    return {"Status": "CREATE_COMPLETE", "Changes": [{"ResourceChange": {"Action": "Modify", "ResourceType": "AWS::IAM::Role", "LogicalResourceId": "ApplicationExecutionRole", "Replacement": "False"}}]}

            cfn = BadCfn(TEMPLATE)
            s3 = FakeS3()
            with self.assertRaises(DeploymentError) as error:
                deploy(ReleaseInput("b" * 40, root, "artifacts", "phase-14/" + "b" * 40 + "/legaldesk-lambda.zip"), cfn=cfn, s3=s3, cloudfront=FakeCloudFront(), lambda_client=FakeLambda("0" * 64), template_path=template_path, change_set_name="cs", execute=True, approval=APPROVAL, sleeper=lambda _: None)
            self.assertEqual(str(error.exception), "change_set_scope_violation")
            self.assertFalse(cfn.executed)

    def test_lambda_hash_failure_writes_failure_record_and_invalidation_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _manifest(root)
            template_path = root / "template.yaml"
            template_path.write_text(TEMPLATE, encoding="utf-8")
            record = root / "record.json"
            cfn = FakeCfn(TEMPLATE)
            with self.assertRaises(DeploymentError) as error:
                deploy(ReleaseInput("b" * 40, root, "artifacts", "phase-14/" + "b" * 40 + "/legaldesk-lambda.zip"), cfn=cfn, s3=FakeS3(), cloudfront=FakeCloudFront(), lambda_client=FakeLambda("0" * 64), template_path=template_path, change_set_name="cs", execute=True, approval=APPROVAL, record_path=record, sleeper=lambda _: None)
            self.assertEqual(str(error.exception), "lambda_code_hash_mismatch")
            payload = json.loads(record.read_text())
            self.assertEqual(payload["status"], "FAILED")
            self.assertEqual(set(payload["previousLambda"]), {"key", "version"})
            self.assertEqual(set(payload["frontendVersions"]), {"index.html", "styles.css", "app.js", "citations.js", "diagnostics.js"})
            self.assertEqual(set(payload["previousFrontendVersions"]), {"index.html", "styles.css", "app.js", "citations.js", "diagnostics.js"})

    def test_stack_progress_is_polled_until_update_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _manifest(root)
            template_path = root / "template.yaml"
            template_path.write_text(TEMPLATE, encoding="utf-8")
            cfn = ProgressCfn(TEMPLATE)
            app_hash = json.loads((root / "legaldesk-release-manifest.json").read_text())["artifacts"][0]["sha256"]
            def opener(request: Any, timeout: int) -> Any:
                path = request.full_url.rsplit("edge.example", 1)[-1]
                status = {"/": 200, "/logout": 302, "/api/me": 403}.get(path, 200)
                body = next(((name + "\n").encode() for name in ("index.html", "styles.css", "app.js", "citations.js", "diagnostics.js") if "/" + name == path), b"")
                return type("Response", (), {"status": status, "read": lambda self: body})()

            result = deploy(ReleaseInput("b" * 40, root, "artifacts", "phase-14/" + "b" * 40 + "/legaldesk-lambda.zip"), cfn=cfn, s3=FakeS3(), cloudfront=FakeCloudFront(), lambda_client=FakeLambda(app_hash), template_path=template_path, change_set_name="cs", execute=True, approval=APPROVAL, public_opener=opener, sleeper=lambda _: None)
            self.assertEqual(result.status, "PASS")
            self.assertGreaterEqual(cfn.stack_calls, 2)

    def test_prior_stable_status_is_not_taken_as_update_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _manifest(root)
            template_path = root / "template.yaml"
            template_path.write_text(TEMPLATE, encoding="utf-8")
            cfn = StableCompleteThenProgressCfn(TEMPLATE)
            app_hash = json.loads((root / "legaldesk-release-manifest.json").read_text())["artifacts"][0]["sha256"]

            def opener(request: Any, timeout: int) -> Any:
                path = request.full_url.rsplit("edge.example", 1)[-1]
                status = {"/": 200, "/logout": 302, "/api/me": 403}.get(path, 200)
                body = next(((name + "\n").encode() for name in ("index.html", "styles.css", "app.js", "citations.js", "diagnostics.js") if "/" + name == path), b"")
                return type("Response", (), {"status": status, "read": lambda self: body})()

            result = deploy(ReleaseInput("b" * 40, root, "artifacts", "phase-14/" + "b" * 40 + "/legaldesk-lambda.zip"), cfn=cfn, s3=FakeS3(), cloudfront=FakeCloudFront(), lambda_client=FakeLambda(app_hash), template_path=template_path, change_set_name="cs", execute=True, approval=APPROVAL, public_opener=opener, sleeper=lambda _: None)
            self.assertEqual(result.status, "PASS")
            self.assertGreaterEqual(cfn.stack_calls, 4)

    def test_prior_rollback_status_is_fenced_until_progress_and_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _manifest(root)
            template_path = root / "template.yaml"
            template_path.write_text(TEMPLATE, encoding="utf-8")
            cfn = RollbackThenProgressCfn(TEMPLATE)
            app_hash = json.loads((root / "legaldesk-release-manifest.json").read_text())["artifacts"][0]["sha256"]

            def opener(request: Any, timeout: int) -> Any:
                path = request.full_url.rsplit("edge.example", 1)[-1]
                status = {"/": 200, "/logout": 302, "/api/me": 403}.get(path, 200)
                body = next(((name + "\n").encode() for name in ("index.html", "styles.css", "app.js", "citations.js", "diagnostics.js") if "/" + name == path), b"")
                return type("Response", (), {"status": status, "read": lambda self: body})()

            result = deploy(ReleaseInput("b" * 40, root, "artifacts", "phase-14/" + "b" * 40 + "/legaldesk-lambda.zip"), cfn=cfn, s3=FakeS3(), cloudfront=FakeCloudFront(), lambda_client=FakeLambda(app_hash), template_path=template_path, change_set_name="cs", execute=True, approval=APPROVAL, public_opener=opener, sleeper=lambda _: None)
            self.assertEqual(result.status, "PASS")
            self.assertGreaterEqual(cfn.stack_calls, 4)

    def test_same_code_parameters_skip_change_set_and_verify_lambda(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _manifest(root)
            template_path = root / "template.yaml"
            template_path.write_text(TEMPLATE, encoding="utf-8")
            commit = "b" * 40
            key = "phase-14/" + commit + "/legaldesk-lambda.zip"
            app, _ = validate_release(root, commit, "artifacts", key)
            s3 = FakeS3()
            s3.objects[("artifacts", key)] = (app.path.read_bytes(), "v1", app.sha256)
            cfn = FakeCfn(TEMPLATE)
            cfn.parameters[1:] = [
                {"ParameterKey": "ApplicationCodeKey", "ParameterValue": key},
                {"ParameterKey": "ApplicationCodeVersion", "ParameterValue": "v1"},
                {"ParameterKey": "TrustedEdgeSecret", "ParameterValue": "****"},
            ]

            def opener(request: Any, timeout: int) -> Any:
                path = request.full_url.rsplit("edge.example", 1)[-1]
                status = {"/": 200, "/logout": 302, "/api/me": 403}.get(path, 200)
                body = next(((name + "\n").encode() for name in ("index.html", "styles.css", "app.js", "citations.js", "diagnostics.js") if "/" + name == path), b"")
                return type("Response", (), {"status": status, "read": lambda self: body})()

            result = deploy(ReleaseInput(commit, root, "artifacts", key), cfn=cfn, s3=s3, cloudfront=FakeCloudFront(), lambda_client=FakeLambda(app.sha256), template_path=template_path, change_set_name="cs", execute=True, approval=APPROVAL, public_opener=opener, sleeper=lambda _: None)
            self.assertEqual(result.change_set_name, "NO_OP")
            self.assertFalse(cfn.created)
            self.assertFalse(cfn.executed)

    def test_same_code_parameters_with_wrong_lambda_hash_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _manifest(root)
            template_path = root / "template.yaml"
            template_path.write_text(TEMPLATE, encoding="utf-8")
            commit = "b" * 40
            key = "phase-14/" + commit + "/legaldesk-lambda.zip"
            app, _ = validate_release(root, commit, "artifacts", key)
            s3 = FakeS3()
            s3.objects[("artifacts", key)] = (app.path.read_bytes(), "v1", app.sha256)
            cfn = FakeCfn(TEMPLATE)
            cfn.parameters[1:] = [
                {"ParameterKey": "ApplicationCodeKey", "ParameterValue": key},
                {"ParameterKey": "ApplicationCodeVersion", "ParameterValue": "v1"},
                {"ParameterKey": "TrustedEdgeSecret", "ParameterValue": "****"},
            ]
            with self.assertRaises(DeploymentError) as error:
                deploy(ReleaseInput(commit, root, "artifacts", key), cfn=cfn, s3=s3, cloudfront=FakeCloudFront(), lambda_client=FakeLambda("0" * 64), template_path=template_path, change_set_name="cs", execute=True, approval=APPROVAL, sleeper=lambda _: None)
            self.assertEqual(str(error.exception), "lambda_code_hash_mismatch")
            self.assertFalse(cfn.created)
            self.assertFalse(cfn.executed)

    def test_update_rollback_complete_is_stable_for_verified_noop(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _manifest(root)
            template_path = root / "template.yaml"
            template_path.write_text(TEMPLATE, encoding="utf-8")
            commit = "b" * 40
            key = "phase-14/" + commit + "/legaldesk-lambda.zip"
            app, _ = validate_release(root, commit, "artifacts", key)
            s3 = FakeS3()
            s3.objects[("artifacts", key)] = (app.path.read_bytes(), "v1", app.sha256)
            cfn = RollbackCompleteCfn(TEMPLATE)
            cfn.parameters[1:] = [
                {"ParameterKey": "ApplicationCodeKey", "ParameterValue": key},
                {"ParameterKey": "ApplicationCodeVersion", "ParameterValue": "v1"},
                {"ParameterKey": "TrustedEdgeSecret", "ParameterValue": "****"},
            ]

            def opener(request: Any, timeout: int) -> Any:
                path = request.full_url.rsplit("edge.example", 1)[-1]
                status = {"/": 200, "/logout": 302, "/api/me": 403}.get(path, 200)
                body = next(((name + "\n").encode() for name in ("index.html", "styles.css", "app.js", "citations.js", "diagnostics.js") if "/" + name == path), b"")
                return type("Response", (), {"status": status, "read": lambda self: body})()

            result = deploy(ReleaseInput(commit, root, "artifacts", key), cfn=cfn, s3=s3, cloudfront=FakeCloudFront(), lambda_client=FakeLambda(app.sha256), template_path=template_path, change_set_name="cs", execute=True, approval=APPROVAL, public_opener=opener, sleeper=lambda _: None)
            self.assertEqual(result.status, "PASS")
            self.assertFalse(cfn.created)

    def test_template_drift_is_rejected_before_artifact_upload(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _manifest(root)
            template_path = root / "template.yaml"
            template_path.write_text(TEMPLATE, encoding="utf-8")
            s3 = FakeS3()
            with self.assertRaises(DeploymentError) as error:
                deploy(ReleaseInput("b" * 40, root, "artifacts", "phase-14/" + "b" * 40 + "/legaldesk-lambda.zip"), cfn=DriftCfn(TEMPLATE), s3=s3, cloudfront=FakeCloudFront(), template_path=template_path, change_set_name="cs", execute=True, approval=APPROVAL)
            self.assertEqual(str(error.exception), "deployed_template_scope_changed")
            self.assertEqual(s3.puts, 0)

    def test_cloudfront_timeout_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _manifest(root)
            template_path = root / "template.yaml"
            template_path.write_text(TEMPLATE, encoding="utf-8")
            app_hash = json.loads((root / "legaldesk-release-manifest.json").read_text())["artifacts"][0]["sha256"]
            with self.assertRaises(DeploymentError) as error:
                deploy(ReleaseInput("b" * 40, root, "artifacts", "phase-14/" + "b" * 40 + "/legaldesk-lambda.zip"), cfn=FakeCfn(TEMPLATE), s3=FakeS3(), cloudfront=FakeCloudFront(complete=False), lambda_client=FakeLambda(app_hash), template_path=template_path, change_set_name="cs", execute=True, approval=APPROVAL, sleeper=lambda _: None)
            self.assertEqual(str(error.exception), "cloudfront_invalidation_timeout")

    def test_cdn_hash_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _manifest(root)
            template_path = root / "template.yaml"
            template_path.write_text(TEMPLATE, encoding="utf-8")
            app_hash = json.loads((root / "legaldesk-release-manifest.json").read_text())["artifacts"][0]["sha256"]

            def opener(request: Any, timeout: int) -> Any:
                path = request.full_url.rsplit("edge.example", 1)[-1]
                status = {"/": 200, "/logout": 302, "/api/me": 403}.get(path, 200)
                return type("Response", (), {"status": status, "read": lambda self: b"wrong"})()

            with self.assertRaises(DeploymentError) as error:
                deploy(ReleaseInput("b" * 40, root, "artifacts", "phase-14/" + "b" * 40 + "/legaldesk-lambda.zip"), cfn=FakeCfn(TEMPLATE), s3=FakeS3(), cloudfront=FakeCloudFront(), lambda_client=FakeLambda(app_hash), template_path=template_path, change_set_name="cs", execute=True, approval=APPROVAL, public_opener=opener, sleeper=lambda _: None)
            self.assertEqual(str(error.exception), "public_asset_hash_mismatch")

    def test_previous_frontend_versions_remain_in_rollback_record_after_update(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _manifest(root)
            template_path = root / "template.yaml"
            template_path.write_text(TEMPLATE, encoding="utf-8")
            s3 = FakeS3()
            for name in ("index.html", "styles.css", "app.js", "citations.js", "diagnostics.js"):
                body = ("old-" + name).encode()
                s3.objects[("frontend", name)] = (body, "old-" + name, hashlib.sha256(body).hexdigest())
            app_hash = json.loads((root / "legaldesk-release-manifest.json").read_text())["artifacts"][0]["sha256"]
            opener = lambda request, timeout: type("Response", (), {"status": {"/": 200, "/logout": 302, "/api/me": 403}.get(request.full_url.rsplit("edge.example", 1)[-1], 200), "read": lambda self: next(((name + "\n").encode() for name in ("index.html", "styles.css", "app.js", "citations.js", "diagnostics.js") if "/" + name == request.full_url.rsplit("edge.example", 1)[-1]), b"")})()
            result = deploy(ReleaseInput("b" * 40, root, "artifacts", "phase-14/" + "b" * 40 + "/legaldesk-lambda.zip"), cfn=FakeCfn(TEMPLATE), s3=s3, cloudfront=FakeCloudFront(), lambda_client=FakeLambda(app_hash), template_path=template_path, change_set_name="cs", execute=True, approval=APPROVAL, public_opener=opener, sleeper=lambda _: None)
            self.assertEqual(result.previous_frontend_versions[0], ("index.html", "old-index.html"))
            self.assertEqual(s3.objects[("frontend", "index.html")][1], "v2")

    def test_external_provider_error_writes_failed_record(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _manifest(root)
            template_path = root / "template.yaml"
            template_path.write_text(TEMPLATE, encoding="utf-8")
            record = root / "record.json"
            app_hash = json.loads((root / "legaldesk-release-manifest.json").read_text())["artifacts"][0]["sha256"]
            with self.assertRaises(DeploymentError) as error:
                deploy(ReleaseInput("b" * 40, root, "artifacts", "phase-14/" + "b" * 40 + "/legaldesk-lambda.zip"), cfn=ExecuteErrorCfn(TEMPLATE), s3=FakeS3(), cloudfront=FakeCloudFront(), lambda_client=FakeLambda(app_hash), template_path=template_path, change_set_name="cs", execute=True, approval=APPROVAL, record_path=record, sleeper=lambda _: None)
            self.assertEqual(str(error.exception), "change_set_execute_failed")
            self.assertEqual(json.loads(record.read_text())["status"], "FAILED")

    def test_new_immutable_artifact_uses_atomic_put_without_head(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _manifest(root)
            app, _ = validate_release(root, "b" * 40, "artifacts", "phase-14/" + "b" * 40 + "/legaldesk-lambda.zip")
            s3 = NoHeadS3()
            from scripts.deploy_release import _put_immutable

            version = _put_immutable(s3, "artifacts", "phase-14/" + "b" * 40 + "/legaldesk-lambda.zip", app, "b" * 40)
            self.assertEqual(version, "v1")


if __name__ == "__main__":
    unittest.main()
