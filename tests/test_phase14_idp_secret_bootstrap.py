from __future__ import annotations

import contextlib
import io
import json
import unittest
from argparse import Namespace
from typing import Any

from scripts.bootstrap_idp_secret import (
    APPROVAL,
    CLIENT_NAME,
    PARAMETER_NAME,
    REVIEW_SCOPE,
    BootstrapError,
    _validate_args,
    main,
    transfer_secret,
)


class _FakeCognito:
    def __init__(self, secret: str = "not-for-output") -> None:
        self.secret = secret
        self.calls: list[dict[str, str]] = []

    def describe_user_pool_client(self, **kwargs: str) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"UserPoolClient": {
            "UserPoolId": "eu-west-1_tHvFPpktv",
            "ClientId": "client123",
            "ClientName": CLIENT_NAME,
            "AllowedOAuthFlows": ["client_credentials"],
            "AllowedOAuthScopes": [REVIEW_SCOPE],
            "AccessTokenValidity": 5,
            "TokenValidityUnits": {"AccessToken": "minutes"},
            "ClientSecret": self.secret,
        }}


class _FakeSsm:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def put_parameter(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        return {"Version": 1}


class Phase14IDPSecretBootstrapTests(unittest.TestCase):
    pool_id = "eu-west-1_tHvFPpktv"
    client_id = "client123"

    def test_default_cli_is_dry_run_and_never_loads_boto3(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(
                main(["--user-pool-id", self.pool_id, "--client-id", self.client_id]),
                0,
            )
        report = json.loads(output.getvalue())
        self.assertEqual(report["status"], "DRY_RUN")
        self.assertEqual(report["awsCalls"], 0)
        self.assertEqual(report["parameterName"], PARAMETER_NAME)
        self.assertNotIn("not-for-output", output.getvalue())

    def test_transfer_uses_standard_secure_string_without_overwrite(self) -> None:
        cognito = _FakeCognito()
        ssm = _FakeSsm()
        result = transfer_secret(
            region="eu-west-1",
            user_pool_id=self.pool_id,
            client_id=self.client_id,
            cognito=cognito,
            ssm=ssm,
        )
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(cognito.calls, [{"UserPoolId": self.pool_id, "ClientId": self.client_id}])
        self.assertEqual(len(ssm.calls), 1)
        self.assertEqual(ssm.calls[0]["Name"], PARAMETER_NAME)
        self.assertEqual(ssm.calls[0]["Type"], "SecureString")
        self.assertEqual(ssm.calls[0]["Tier"], "Standard")
        self.assertIs(ssm.calls[0]["Overwrite"], False)
        self.assertNotIn("not-for-output", json.dumps(result))

    def test_execute_requires_exact_acknowledgement_and_parameter_name(self) -> None:
        with self.assertRaises(BootstrapError):
            _validate_args(Namespace(
                region="eu-west-1", user_pool_id=self.pool_id, client_id=self.client_id,
                parameter_name=PARAMETER_NAME, execute=True, approval="",
            ))
        with self.assertRaises(BootstrapError):
            _validate_args(Namespace(
                region="eu-west-1", user_pool_id=self.pool_id, client_id=self.client_id,
                parameter_name="/other/name", execute=False, approval=APPROVAL,
            ))

    def test_provider_failures_are_category_only(self) -> None:
        class FailingCognito:
            def describe_user_pool_client(self, **_: object) -> dict[str, object]:
                raise RuntimeError("provider response contained not-for-output")

        with self.assertRaisesRegex(BootstrapError, "^secret_transfer_failed$"):
            transfer_secret(
                region="eu-west-1",
                user_pool_id=self.pool_id,
                client_id=self.client_id,
                cognito=FailingCognito(),
                ssm=_FakeSsm(),
            )

    def test_ordinary_or_misconfigured_clients_cannot_write_ssm(self) -> None:
        class WrongClient(_FakeCognito):
            def describe_user_pool_client(self, **kwargs: str) -> dict[str, Any]:
                response = super().describe_user_pool_client(**kwargs)
                response["UserPoolClient"]["ClientName"] = "HumanOrLegacyClient"
                response["UserPoolClient"]["AllowedOAuthScopes"] = ["legaldesk/use"]
                response["UserPoolClient"]["AllowedOAuthFlows"] = ["code"]
                return response

        cognito = WrongClient()
        ssm = _FakeSsm()
        with self.assertRaisesRegex(BootstrapError, "^cognito_client_name_mismatch$"):
            transfer_secret(
                region="eu-west-1",
                user_pool_id=self.pool_id,
                client_id=self.client_id,
                cognito=cognito,
                ssm=ssm,
            )
        self.assertEqual(ssm.calls, [])

    def test_pool_or_token_lifetime_mismatch_cannot_write_ssm(self) -> None:
        class WrongPool(_FakeCognito):
            def describe_user_pool_client(self, **kwargs: str) -> dict[str, Any]:
                response = super().describe_user_pool_client(**kwargs)
                response["UserPoolClient"]["UserPoolId"] = "eu-west-1_otherpool"
                return response

        ssm = _FakeSsm()
        with self.assertRaisesRegex(BootstrapError, "^cognito_client_binding_mismatch$"):
            transfer_secret(
                region="eu-west-1",
                user_pool_id=self.pool_id,
                client_id=self.client_id,
                cognito=WrongPool(),
                ssm=ssm,
            )
        self.assertEqual(ssm.calls, [])

        for field, value, reason in (
            ("AllowedOAuthScopes", ["legaldesk/use"], "cognito_client_scope_mismatch"),
            ("AllowedOAuthFlows", ["code"], "cognito_client_flow_mismatch"),
            ("AccessTokenValidity", 60, "cognito_client_token_lifetime_mismatch"),
        ):
            class MisconfiguredClient(_FakeCognito):
                def describe_user_pool_client(self, **kwargs: str) -> dict[str, Any]:
                    response = super().describe_user_pool_client(**kwargs)
                    response["UserPoolClient"][field] = value
                    return response

            scoped_ssm = _FakeSsm()
            with self.subTest(field=field), self.assertRaisesRegex(BootstrapError, f"^{reason}$"):
                transfer_secret(
                    region="eu-west-1",
                    user_pool_id=self.pool_id,
                    client_id=self.client_id,
                    cognito=MisconfiguredClient(),
                    ssm=scoped_ssm,
                )
            self.assertEqual(scoped_ssm.calls, [])


if __name__ == "__main__":
    unittest.main()
