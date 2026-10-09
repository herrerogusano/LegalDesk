"""Transfer the dedicated IDP Cognito client secret into SSM safely.

The default mode performs only local validation.  The explicit AWS mode reads
the generated Cognito client secret into process memory and writes it once to
the fixed Standard SecureString parameter with ``Overwrite=False``.  The
secret is never accepted as a command-line argument, printed, serialized, or
included in an exception report.  This helper does not create a user pool,
change Gateway authorization, invoke a Lambda, or call a model.
"""

from __future__ import annotations

import argparse
import json
import re
from typing import Any, Mapping, Sequence


APPROVAL = "I_UNDERSTAND_LEGALDESK_IDP_SECRET_BOOTSTRAP"
DEFAULT_REGION = "eu-west-1"
PARAMETER_NAME = "/legaldesk/phase14/idp/m2m-client-secret"
CLIENT_NAME = "LegalDeskPhase14IDPMachine"
REVIEW_SCOPE = "legaldesk-idp/review-create"
_POOL_ID = re.compile(r"^eu-west-1_[A-Za-z0-9]+$")
_CLIENT_ID = re.compile(r"^[A-Za-z0-9]+$")


class BootstrapError(RuntimeError):
    """Closed operator-facing diagnostic category."""


def _validate_client_metadata(
    client: Mapping[str, object], *, user_pool_id: str, client_id: str
) -> None:
    if client.get("UserPoolId") != user_pool_id or client.get("ClientId") != client_id:
        raise BootstrapError("cognito_client_binding_mismatch")
    if client.get("ClientName") != CLIENT_NAME:
        raise BootstrapError("cognito_client_name_mismatch")
    if client.get("AllowedOAuthFlows") != ["client_credentials"]:
        raise BootstrapError("cognito_client_flow_mismatch")
    if client.get("AllowedOAuthScopes") != [REVIEW_SCOPE]:
        raise BootstrapError("cognito_client_scope_mismatch")
    if client.get("AccessTokenValidity") != 5 or client.get("TokenValidityUnits") != {"AccessToken": "minutes"}:
        raise BootstrapError("cognito_client_token_lifetime_mismatch")


def _validate_args(args: argparse.Namespace) -> tuple[str, str, str]:
    region = args.region.strip() if isinstance(args.region, str) else ""
    pool_id = args.user_pool_id.strip() if isinstance(args.user_pool_id, str) else ""
    client_id = args.client_id.strip() if isinstance(args.client_id, str) else ""
    if region != DEFAULT_REGION:
        raise BootstrapError("region_must_be_eu_west_1")
    if _POOL_ID.fullmatch(pool_id) is None:
        raise BootstrapError("user_pool_id_invalid")
    if _CLIENT_ID.fullmatch(client_id) is None:
        raise BootstrapError("client_id_invalid")
    if args.parameter_name != PARAMETER_NAME:
        raise BootstrapError("parameter_name_must_match_reviewed_value")
    if args.execute and args.approval != APPROVAL:
        raise BootstrapError("explicit_approval_required")
    return region, pool_id, client_id


def transfer_secret(
    *,
    region: str,
    user_pool_id: str,
    client_id: str,
    cognito: Any,
    ssm: Any,
) -> dict[str, object]:
    """Read and transfer the secret without returning or logging its value."""

    secret: str | None = None
    try:
        response = cognito.describe_user_pool_client(UserPoolId=user_pool_id, ClientId=client_id)
        client = response.get("UserPoolClient") if isinstance(response, Mapping) else None
        if not isinstance(client, Mapping):
            raise BootstrapError("cognito_client_metadata_unavailable")
        _validate_client_metadata(client, user_pool_id=user_pool_id, client_id=client_id)
        secret_value = client.get("ClientSecret") if isinstance(client, Mapping) else None
        if not isinstance(secret_value, str) or not secret_value:
            raise BootstrapError("cognito_client_secret_unavailable")
        secret = secret_value
        ssm.put_parameter(
            Name=PARAMETER_NAME,
            Value=secret,
            Type="SecureString",
            Tier="Standard",
            Overwrite=False,
        )
        return {
            "status": "PASS",
            "region": region,
            "userPoolId": user_pool_id,
            "clientId": client_id,
            "parameterName": PARAMETER_NAME,
            "parameterType": "SecureString",
            "overwrite": False,
        }
    except BootstrapError:
        raise
    except Exception as exc:
        # Do not expose provider response text: a client or parameter error
        # must not accidentally echo secret-bearing fields.
        raise BootstrapError("secret_transfer_failed") from exc
    finally:
        secret = None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--user-pool-id", required=True)
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--parameter-name", default=PARAMETER_NAME)
    parser.add_argument("--execute", action="store_true", help="Enable the one-time AWS SSM write")
    parser.add_argument("--approval", default="", help=argparse.SUPPRESS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        region, pool_id, client_id = _validate_args(args)
        if not args.execute:
            print(json.dumps({
                "status": "DRY_RUN",
                "awsCalls": 0,
                "region": region,
                "userPoolId": pool_id,
                "clientId": client_id,
                "parameterName": PARAMETER_NAME,
                "parameterType": "SecureString",
                "overwrite": False,
            }, sort_keys=True))
            return 0
        try:
            import boto3
        except ImportError:
            raise BootstrapError("boto3_unavailable") from None
        from botocore.config import Config

        session = boto3.Session(region_name=region)
        sdk_config = Config(retries={"total_max_attempts": 1, "mode": "standard"})
        result = transfer_secret(
            region=region,
            user_pool_id=pool_id,
            client_id=client_id,
            cognito=session.client("cognito-idp", config=sdk_config),
            ssm=session.client("ssm", config=sdk_config),
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    except BootstrapError as exc:
        print(json.dumps({"status": "BLOCKED", "reason": str(exc)}, sort_keys=True))
        return 2
    except Exception:
        print(json.dumps({"status": "BLOCKED", "reason": "secret_transfer_failed"}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
