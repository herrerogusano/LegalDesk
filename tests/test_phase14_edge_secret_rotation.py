from __future__ import annotations

import contextlib
import io
import unittest
from typing import Any

from scripts import rotate_public_edge_secret as rot

SECRET = "GENERATED-SECRET-VALUE-0123456789-abcdefghijklmnopqrstuvwxyz"
ROLE = "arn:aws:iam::344774635844:role/temp-role"
KEYS = ["HarnessArn", "TrustedEdgeSecret", "QuotaGatewayPerMonth", "MemoryId"]


def _change(logical: str, action: str = "Modify", replacement: str = "False") -> dict[str, Any]:
    return {"Type": "Resource", "ResourceChange": {"Action": action, "LogicalResourceId": logical, "Replacement": replacement}}


GOOD = [_change("ApplicationFunction"), _change("PublicDistribution")]


class FakeCfn:
    def __init__(self, changes: list[dict[str, Any]], status: str = "UPDATE_COMPLETE") -> None:
        self.changes, self.status, self.writes = changes, status, []

    def describe_stacks(self, **_: Any) -> dict[str, Any]:
        return {"Stacks": [{"StackStatus": self.status, "Capabilities": ["CAPABILITY_IAM"], "Parameters": [{"ParameterKey": k, "ParameterValue": "****"} for k in KEYS]}]}

    def create_change_set(self, **kwargs: Any) -> None:
        self.writes.append(("create", kwargs))

    def describe_change_set(self, **_: Any) -> dict[str, Any]:
        return {"Status": "CREATE_COMPLETE", "Changes": self.changes}

    def execute_change_set(self, **kwargs: Any) -> None:
        self.writes.append(("execute", kwargs))

    def delete_change_set(self, **kwargs: Any) -> None:
        self.writes.append(("delete", kwargs))


class FakeCloudFront:
    def __init__(self, header: str = SECRET) -> None:
        self.header = header

    def get_distribution(self, **_: Any) -> dict[str, Any]:
        return {"Distribution": {"Status": "Deployed"}}

    def get_distribution_config(self, **_: Any) -> dict[str, Any]:
        return {"DistributionConfig": {"Origins": {"Items": [{"CustomHeaders": {"Items": [{"HeaderName": "X-LegalDesk-Trusted-Edge", "HeaderValue": self.header}]}}]}}}


class FakeLambda:
    def __init__(self, value: str = SECRET, quota: str = "300") -> None:
        self.env = {rot.SECRET_ENV: value, rot.QUOTA_ENV: quota}

    def get_function_configuration(self, **_: Any) -> dict[str, Any]:
        return {"Environment": {"Variables": self.env}}


def _run(cfn: FakeCfn, mode: str, cf: FakeCloudFront | None = None, lam: FakeLambda | None = None) -> tuple[int | None, str, Exception | None]:
    out, err = io.StringIO(), io.StringIO()
    code, error = None, None
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = rot.rotate(cfn=cfn, cloudfront=cf or FakeCloudFront(), lambda_client=lam or FakeLambda(), distribution_id="E1", role_arn=ROLE,
                              quota=300, mode=mode, sleep=lambda _: None, generate=lambda: SECRET)
        except rot.RotationError as exc:
            error = exc
    return code, out.getvalue() + err.getvalue(), error


class RotationTests(unittest.TestCase):
    def test_dry_run_makes_no_write_calls(self) -> None:
        cfn = FakeCfn(GOOD)
        code, text, _ = _run(cfn, "dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(cfn.writes, [])
        self.assertNotIn(SECRET, text)

    def test_execute_success_prints_only_booleans_and_never_the_secret(self) -> None:
        cfn = FakeCfn(GOOD)
        code, text, _ = _run(cfn, "execute")
        self.assertEqual(code, 0)
        self.assertNotIn(SECRET, text)
        self.assertIn("lambda_env_secret_sha256_match=true", text)
        kinds = [kind for kind, _ in cfn.writes]
        self.assertEqual(kinds, ["create", "execute"])
        create = cfn.writes[0][1]
        self.assertTrue(create["UsePreviousTemplate"])
        self.assertEqual(create["RoleARN"], ROLE)
        self.assertEqual(create["Capabilities"], ["CAPABILITY_IAM"])
        by_key = {item["ParameterKey"]: item for item in create["Parameters"]}
        self.assertEqual(by_key["TrustedEdgeSecret"]["ParameterValue"], SECRET)
        self.assertEqual(by_key["QuotaGatewayPerMonth"]["ParameterValue"], "300")
        self.assertTrue(by_key["HarnessArn"]["UsePreviousValue"])

    def test_refuses_unexpected_changes_and_does_not_execute(self) -> None:
        for bad in (
            GOOD + [_change("ApplicationExecutionRole")],
            [_change("ApplicationFunction", replacement="True"), _change("PublicDistribution")],
            [_change("ApplicationFunction", action="Remove"), _change("PublicDistribution")],
            [_change("ApplicationFunction")],
        ):
            cfn = FakeCfn(bad)
            _, text, error = _run(cfn, "execute")
            self.assertIsInstance(error, rot.RotationError)
            self.assertNotIn(SECRET, text)
            self.assertNotIn("execute", [kind for kind, _ in cfn.writes])
            self.assertIn("delete", [kind for kind, _ in cfn.writes])

    def test_optional_integration_change_allowed(self) -> None:
        code, _, _ = _run(FakeCfn(GOOD + [_change("PublicApiIntegration")]), "execute")
        self.assertEqual(code, 0)

    def test_verification_mismatch_returns_failure(self) -> None:
        code, text, _ = _run(FakeCfn(GOOD), "execute", cf=FakeCloudFront(header="other"))
        self.assertEqual(code, 1)
        self.assertIn("cloudfront_header_sha256_match=false", text)
        self.assertNotIn(SECRET, text)

    def test_review_only_deletes_change_set_without_executing(self) -> None:
        cfn = FakeCfn(GOOD)
        _run(cfn, "review-only")
        self.assertEqual([kind for kind, _ in cfn.writes], ["create", "delete"])

    def test_preflight_requires_a_stable_stack(self) -> None:
        cfn = FakeCfn(GOOD, status="UPDATE_IN_PROGRESS")
        _, _, error = _run(cfn, "execute")
        self.assertIsInstance(error, rot.RotationError)
        self.assertEqual(cfn.writes, [])

    def test_main_requires_approval_for_write_modes(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = rot.main(["--role-arn", ROLE, "--distribution-id", "E1", "--execute"])
        self.assertEqual(code, 2)
        self.assertIn("explicit_approval_required", err.getvalue())


if __name__ == "__main__":
    unittest.main()
