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


if __name__ == "__main__":
    unittest.main()
