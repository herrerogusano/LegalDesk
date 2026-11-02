"""Code-only CD of the agent chat worker (ADR-021, decision of 2026-11-02).

The worker runs the same package as ``ApplicationFunction``; its ``Code`` is released in
the same change set and both code hashes are verified. With ``EnableAgentChat`` not
``"true"`` the worker does not exist and the original path is unchanged.
"""

from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from scripts.deploy_release import APPROVAL, DeploymentError, ReleaseInput, deploy, validate_change_set, validate_release

from tests.test_deploy_release import TEMPLATE, FakeCfn, FakeCloudFront, FakeS3, _manifest

FRONTEND = ("index.html", "styles.css", "app.js", "citations.js", "diagnostics.js")
WORKER_TEMPLATE = (
    TEMPLATE.replace(
        "  TrustedEdgeSecret: {Type: String, NoEcho: true}\n",
        '  TrustedEdgeSecret: {Type: String, NoEcho: true}\n  EnableAgentChat: {Type: String, Default: "false"}\n',
    )
    + """  AgentChatWorkerFunction:
    Type: AWS::Lambda::Function
    Properties:
      Code: {S3Key: !Ref ApplicationCodeKey}
"""
)
APP_FUNCTION = "arn:aws:lambda:eu-west-1:123456789012:function:app"
WORKER_FUNCTION = "LegalDeskPhase14PublicEdge-agent-chat-worker"
KEY = "phase-14/" + "b" * 40 + "/legaldesk-lambda.zip"
WORKER_CHANGE = {
    "ResourceChange": {
        "Action": "Modify",
        "ResourceType": "AWS::Lambda::Function",
        "LogicalResourceId": "AgentChatWorkerFunction",
        "Replacement": "False",
        "Details": [{"Target": {"Name": "Code"}}],
    }
}


class AgentChatCfn(FakeCfn):
    """A stack with EnableAgentChat set; the worker output and change are optional."""

    def __init__(self, template: str, *, enabled: str = "true", worker_output: bool = True, worker_change: bool = True) -> None:
        super().__init__(template)
        self.parameters.append({"ParameterKey": "EnableAgentChat", "ParameterValue": enabled})
        self.worker_output = worker_output
        self.worker_change = worker_change

    def describe_stacks(self, **kwargs: Any) -> dict[str, Any]:
        response = super().describe_stacks(**kwargs)
        if self.worker_output:
            response["Stacks"][0]["Outputs"].append({"OutputKey": "AgentChatWorkerFunctionName", "OutputValue": WORKER_FUNCTION})
        return response

    def describe_change_set(self, **kwargs: Any) -> dict[str, Any]:
        described = super().describe_change_set(**kwargs)
        if self.worker_change:
            described["Changes"].append(WORKER_CHANGE)
        return described


class PerFunctionLambda:
    """Code hash per function; records which functions were asked."""

    def __init__(self, hashes: dict[str, str]) -> None:
        self.hashes = hashes
        self.calls: list[str] = []

    def get_function(self, **kwargs: Any) -> dict[str, Any]:
        name = kwargs["FunctionName"]
        self.calls.append(name)
        return {"Configuration": {"CodeSha256": base64.b64encode(bytes.fromhex(self.hashes[name])).decode("ascii")}}


def _cdn_opener(request: Any, timeout: int) -> Any:
    path = request.full_url.rsplit("edge.example", 1)[-1]
    status = {"/": 200, "/logout": 302, "/api/me": 403}.get(path, 200)
    body = next(((name + "\n").encode() for name in FRONTEND if "/" + name == path), b"")
    return type("Response", (), {"status": status, "read": lambda self: body})()


class AgentChatWorkerReleaseTests(unittest.TestCase):
    def _prepare(self, temp: str) -> tuple[Path, str, Path]:
        root = Path(temp)
        _manifest(root)
        digest = json.loads((root / "legaldesk-release-manifest.json").read_text())["artifacts"][0]["sha256"]
        template_path = root / "template.yaml"
        template_path.write_text(WORKER_TEMPLATE, encoding="utf-8")
        return root, digest, template_path

    def _deploy(self, root: Path, template_path: Path, cfn: FakeCfn, lambdas: PerFunctionLambda, *, s3: FakeS3 | None = None, front: FakeCloudFront | None = None, record: Path | None = None) -> Any:
        return deploy(
            ReleaseInput("b" * 40, root, "artifacts", KEY),
            cfn=cfn, s3=s3 or FakeS3(), cloudfront=front or FakeCloudFront(), lambda_client=lambdas,
            template_path=template_path, change_set_name="cs", execute=True, approval=APPROVAL,
            record_path=record, public_opener=_cdn_opener, sleeper=lambda _: None,
        )

    def test_worker_code_is_released_and_both_hashes_are_verified(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, digest, template_path = self._prepare(temp)
            lambdas = PerFunctionLambda({APP_FUNCTION: digest, WORKER_FUNCTION: digest})
            record = root / "record.json"
            result = self._deploy(root, template_path, AgentChatCfn(WORKER_TEMPLATE), lambdas, record=record)
            self.assertEqual(result.status, "PASS")
            self.assertEqual(result.agent_chat_worker, "VERIFIED")
            self.assertEqual(sorted(lambdas.calls), sorted([APP_FUNCTION, WORKER_FUNCTION]))
            self.assertEqual(json.loads(record.read_text())["agentChatWorker"], "VERIFIED")

    def test_stale_worker_code_blocks_before_the_frontend_is_touched(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, digest, template_path = self._prepare(temp)
            lambdas = PerFunctionLambda({APP_FUNCTION: digest, WORKER_FUNCTION: "0" * 64})
            record, front, s3 = root / "record.json", FakeCloudFront(), FakeS3()
            with self.assertRaises(DeploymentError) as error:
                self._deploy(root, template_path, AgentChatCfn(WORKER_TEMPLATE), lambdas, s3=s3, front=front, record=record)
            self.assertEqual(str(error.exception), "agent_chat_worker_code_hash_mismatch")
            self.assertEqual(json.loads(record.read_text())["status"], "FAILED")
            self.assertFalse(hasattr(front, "request"), "no invalidation after a failed verification")
            self.assertFalse(any(bucket == "frontend" for bucket, _ in s3.objects), "no frontend upload after a failed verification")

    def test_enabled_stack_without_the_worker_output_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, digest, template_path = self._prepare(temp)
            lambdas = PerFunctionLambda({APP_FUNCTION: digest})
            with self.assertRaises(DeploymentError) as error:
                self._deploy(root, template_path, AgentChatCfn(WORKER_TEMPLATE, worker_output=False), lambdas)
            self.assertEqual(str(error.exception), "agent_chat_worker_output_missing")

    def test_enabled_stack_whose_change_set_lacks_the_worker_is_not_executed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, digest, template_path = self._prepare(temp)
            cfn = AgentChatCfn(WORKER_TEMPLATE, worker_change=False)
            with self.assertRaises(DeploymentError) as error:
                self._deploy(root, template_path, cfn, PerFunctionLambda({APP_FUNCTION: digest, WORKER_FUNCTION: digest}))
            self.assertEqual(str(error.exception), "change_set_worker_missing")
            self.assertFalse(cfn.executed)

    def test_disabled_agent_chat_keeps_the_original_release_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, digest, template_path = self._prepare(temp)
            lambdas = PerFunctionLambda({APP_FUNCTION: digest})
            record = root / "record.json"
            cfn = AgentChatCfn(WORKER_TEMPLATE, enabled="false", worker_output=False, worker_change=False)
            result = self._deploy(root, template_path, cfn, lambdas, record=record)
            self.assertEqual(result.status, "PASS")
            self.assertEqual(result.agent_chat_worker, "DISABLED")
            self.assertEqual(lambdas.calls, [APP_FUNCTION])
            self.assertEqual(json.loads(record.read_text())["agentChatWorker"], "DISABLED")

    def test_disabled_agent_chat_rejects_a_worker_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, digest, template_path = self._prepare(temp)
            cfn = AgentChatCfn(WORKER_TEMPLATE, enabled="false", worker_output=False, worker_change=True)
            with self.assertRaises(DeploymentError) as error:
                self._deploy(root, template_path, cfn, PerFunctionLambda({APP_FUNCTION: digest}))
            self.assertEqual(str(error.exception), "change_set_scope_violation")
            self.assertFalse(cfn.executed)

    def test_noop_retry_still_verifies_both_functions(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, digest, template_path = self._prepare(temp)
            app, _ = validate_release(root, "b" * 40, "artifacts", KEY)
            s3 = FakeS3()
            s3.objects[("artifacts", KEY)] = (app.path.read_bytes(), "v1", app.sha256)
            cfn = AgentChatCfn(WORKER_TEMPLATE)
            cfn.parameters[1:3] = [
                {"ParameterKey": "ApplicationCodeKey", "ParameterValue": KEY},
                {"ParameterKey": "ApplicationCodeVersion", "ParameterValue": "v1"},
            ]
            lambdas = PerFunctionLambda({APP_FUNCTION: digest, WORKER_FUNCTION: digest})
            result = self._deploy(root, template_path, cfn, lambdas, s3=s3)
            self.assertEqual(result.change_set_name, "NO_OP")
            self.assertFalse(cfn.created)
            self.assertEqual(sorted(lambdas.calls), sorted([APP_FUNCTION, WORKER_FUNCTION]))


def _change(action: str, logical_id: str, *, replacement: str | None = "False", props: list[str] | None = None, resource_type: str = "AWS::Lambda::Function") -> dict[str, Any]:
    change: dict[str, Any] = {"Action": action, "ResourceType": resource_type, "LogicalResourceId": logical_id}
    if replacement is not None:
        change["Replacement"] = replacement
    wrapper: dict[str, Any] = {"ResourceChange": change}
    if props is not None:
        wrapper["Details"] = [{"Target": {"Name": name}} for name in props]
    return wrapper


class WorkerChangeSetScopeTests(unittest.TestCase):
    def test_worker_code_is_allowed_and_required_when_enabled(self) -> None:
        app = _change("Modify", "ApplicationFunction", props=["Code"])
        worker = _change("Modify", "AgentChatWorkerFunction", props=["Code"])
        validate_change_set([app, worker], expect_worker=True)
        with self.assertRaises(DeploymentError) as error:
            validate_change_set([app], expect_worker=True)
        self.assertEqual(str(error.exception), "change_set_worker_missing")

    def test_worker_may_change_only_its_code_without_replacement(self) -> None:
        app = _change("Modify", "ApplicationFunction", props=["Code"])
        for bad in (
            _change("Modify", "AgentChatWorkerFunction", props=["Code", "Environment"]),
            _change("Modify", "AgentChatWorkerFunction", props=["Role"]),
            _change("Modify", "AgentChatWorkerFunction", replacement="True", props=["Code"]),
            _change("Add", "AgentChatWorkerFunction"),
            _change("Remove", "AgentChatWorkerFunction"),
        ):
            with self.subTest(bad=bad), self.assertRaises(DeploymentError):
                validate_change_set([app, bad], expect_worker=True)

    def test_nothing_else_of_the_agent_chat_stack_is_allowed(self) -> None:
        app = _change("Modify", "ApplicationFunction", props=["Code"])
        worker = _change("Modify", "AgentChatWorkerFunction", props=["Code"])
        for logical_id, resource_type in (
            ("AgentChatWorkerExecutionRole", "AWS::IAM::Role"),
            ("AgentChatQueue", "AWS::SQS::Queue"),
            ("AgentChatEventSourceMapping", "AWS::Lambda::EventSourceMapping"),
            ("AgentChatDeadLetterQueueAlarm", "AWS::CloudWatch::Alarm"),
        ):
            with self.subTest(logical_id=logical_id), self.assertRaises(DeploymentError) as error:
                validate_change_set([app, worker, _change("Modify", logical_id, resource_type=resource_type)], expect_worker=True)
            self.assertEqual(str(error.exception), "change_set_scope_violation")


if __name__ == "__main__":
    unittest.main()
