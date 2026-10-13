"""Rotate the Public Edge TrustedEdgeSecret and set QuotaGatewayPerMonth.

Default mode is a dry run that performs read-only AWS calls only.  ``--review-only``
creates (and then deletes) a change set; ``--execute`` creates, validates and
executes it.  Both write modes require the approval string.  The new secret is
generated in memory, handed straight to CloudFormation, and never printed,
logged, or written to disk.  Only booleans and logical resource names are
reported.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import secrets
import sys
import time
from datetime import datetime, timezone
from typing import Any, Callable, Sequence

APPROVAL = "I_UNDERSTAND_LEGALDESK_EDGE_SECRET_ROTATION"
STACK_NAME = "LegalDeskPhase14PublicEdge"
DEFAULT_REGION = "eu-west-1"
FUNCTION_NAME = "LegalDeskPhase14PublicEdge-application"
SECRET_PARAMETER = "TrustedEdgeSecret"
QUOTA_PARAMETER = "QuotaGatewayPerMonth"
SECRET_ENV = "LEGALDESK_TRUSTED_EDGE_VALUE"
QUOTA_ENV = "LEGALDESK_QUOTA_GATEWAY_PER_MONTH"
EDGE_HEADER = "X-LegalDesk-Trusted-Edge"
ALLOWED_LOGICAL_IDS = {"ApplicationFunction", "PublicDistribution", "PublicApiIntegration"}
REQUIRED_LOGICAL_IDS = {"ApplicationFunction", "PublicDistribution"}
ALLOWED_REPLACEMENT = {"False", "Conditional"}
_ROLE_ARN = re.compile(r"^arn:aws:iam::344774635844:role/[A-Za-z0-9+=,.@_/-]+$")


class RotationError(RuntimeError):
    """Closed operator-facing diagnostic; never carries secret material."""


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _log(message: str) -> None:
    print(message)


def build_parameters(keys: Sequence[str], secret: str | None, quota: int) -> list[dict[str, Any]]:
    if SECRET_PARAMETER not in keys or QUOTA_PARAMETER not in keys:
        raise RotationError("stack_missing_expected_parameters")
    params: list[dict[str, Any]] = []
    for key in keys:
        if key == SECRET_PARAMETER:
            params.append({"ParameterKey": key, "ParameterValue": secret})
        elif key == QUOTA_PARAMETER:
            params.append({"ParameterKey": key, "ParameterValue": str(quota)})
        else:
            params.append({"ParameterKey": key, "UsePreviousValue": True})
    return params


def preflight(cfn: Any, cloudfront: Any, distribution_id: str) -> tuple[list[str], list[str]]:
    stack = cfn.describe_stacks(StackName=STACK_NAME)["Stacks"][0]
    if stack["StackStatus"] != "UPDATE_COMPLETE":
        raise RotationError("stack_not_update_complete")
    status = cloudfront.get_distribution(Id=distribution_id)["Distribution"]["Status"]
    if status != "Deployed":
        raise RotationError("distribution_not_deployed")
    keys = [item["ParameterKey"] for item in stack.get("Parameters", [])]
    return keys, list(stack.get("Capabilities", []))


def validate_changes(changes: Sequence[dict[str, Any]]) -> list[str]:
    seen: list[str] = []
    for change in changes:
        resource = change.get("ResourceChange", {})
        logical = resource.get("LogicalResourceId")
        if (
            change.get("Type") != "Resource"
            or resource.get("Action") != "Modify"
            or logical not in ALLOWED_LOGICAL_IDS
            or resource.get("Replacement", "False") not in ALLOWED_REPLACEMENT
        ):
            raise RotationError(f"unexpected_change:{logical}:{resource.get('Action')}:{resource.get('Replacement')}")
        seen.append(str(logical))
    if not REQUIRED_LOGICAL_IDS <= set(seen) or len(seen) != len(set(seen)):
        raise RotationError("expected_changes_missing_or_duplicated")
    return sorted(seen)


def _wait_change_set(cfn: Any, name: str, sleep: Callable[[float], None], attempts: int = 60) -> dict[str, Any]:
    for _ in range(attempts):
        described = cfn.describe_change_set(StackName=STACK_NAME, ChangeSetName=name)
        if described["Status"] == "CREATE_COMPLETE":
            return described
        if described["Status"] in {"FAILED", "DELETE_COMPLETE"}:
            raise RotationError("change_set_failed")
        sleep(5)
    raise RotationError("change_set_timeout")


def _wait_stack(cfn: Any, sleep: Callable[[float], None], attempts: int = 240) -> None:
    for _ in range(attempts):
        status = cfn.describe_stacks(StackName=STACK_NAME)["Stacks"][0]["StackStatus"]
        if status == "UPDATE_COMPLETE":
            return
        if not status.endswith("_IN_PROGRESS"):
            raise RotationError(f"stack_update_failed:{status}")
        sleep(15)
    raise RotationError("stack_update_timeout")


def _edge_header_value(config: dict[str, Any]) -> str | None:
    for origin in config.get("Origins", {}).get("Items", []):
        for header in origin.get("CustomHeaders", {}).get("Items", []):
            if header.get("HeaderName", "").lower() == EDGE_HEADER.lower():
                return header.get("HeaderValue")
    return None


def verify(lambda_client: Any, cloudfront: Any, distribution_id: str, secret: str, quota: int,
           sleep: Callable[[float], None], poll_attempts: int = 80) -> dict[str, bool]:
    env = lambda_client.get_function_configuration(FunctionName=FUNCTION_NAME).get("Environment", {}).get("Variables", {})
    header = _edge_header_value(cloudfront.get_distribution_config(Id=distribution_id)["DistributionConfig"])
    expected = _sha(secret)
    deployed = False
    for _ in range(poll_attempts):  # ~20 minutes at 15 s
        if cloudfront.get_distribution(Id=distribution_id)["Distribution"]["Status"] == "Deployed":
            deployed = True
            break
        sleep(15)
    return {
        "lambda_env_secret_sha256_match": env.get(SECRET_ENV) is not None and _sha(env[SECRET_ENV]) == expected,
        "cloudfront_header_sha256_match": header is not None and _sha(header) == expected,
        "quota_env_matches": env.get(QUOTA_ENV) == str(quota),
        "cloudfront_deployed": deployed,
    }


def rotate(*, cfn: Any, cloudfront: Any, lambda_client: Any, distribution_id: str, role_arn: str, quota: int,
           mode: str, sleep: Callable[[float], None] = time.sleep,
           generate: Callable[[], str] | None = None) -> int:
    """mode: dry-run | review-only | execute."""
    keys, capabilities = preflight(cfn, cloudfront, distribution_id)
    _log(f"preflight ok: stack UPDATE_COMPLETE, distribution Deployed, {len(keys)} parameters")
    _log(f"plan: rotate {SECRET_PARAMETER}, set {QUOTA_PARAMETER}={quota}, others UsePreviousValue, RoleARN supplied")
    if mode == "dry-run":
        _log("dry-run: no change set created")
        return 0
    secret = (generate or (lambda: secrets.token_urlsafe(48)))()
    name = "rotate-edge-secret-" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    cfn.create_change_set(
        StackName=STACK_NAME, ChangeSetName=name, ChangeSetType="UPDATE", UsePreviousTemplate=True,
        Parameters=build_parameters(keys, secret, quota), Capabilities=capabilities, RoleARN=role_arn,
    )
    keep = False
    try:
        described = _wait_change_set(cfn, name, sleep)
        summary = []
        for change in described.get("Changes", []):
            resource = change.get("ResourceChange", {})
            summary.append(f"{resource.get('Action')} {resource.get('LogicalResourceId')} replacement={resource.get('Replacement')}")
        _log("change set summary: " + "; ".join(summary))
        validate_changes(described.get("Changes", []))
        if mode == "review-only":
            _log("review-only: change set validated and will be deleted")
            return 0
        keep = True
        cfn.execute_change_set(StackName=STACK_NAME, ChangeSetName=name)
        _wait_stack(cfn, sleep)
        results = verify(lambda_client, cloudfront, distribution_id, secret, quota, sleep)
        for key, value in results.items():
            _log(f"{key}={str(value).lower()}")
        return 0 if all(results.values()) else 1
    finally:
        if not keep:
            try:
                cfn.delete_change_set(StackName=STACK_NAME, ChangeSetName=name)
            except Exception:  # noqa: BLE001 - never surface provider text
                _log("warning: change set cleanup failed")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role-arn", required=True, help="temporary CloudFormation role ARN")
    parser.add_argument("--distribution-id", required=True)
    parser.add_argument("--quota-gateway", type=int, default=300)
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--review-only", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--approval", default="")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if not _ROLE_ARN.match(args.role_arn) or not re.match(r"^[A-Z0-9]+$", args.distribution_id):
            raise RotationError("invalid_arguments")
        if not 1 <= args.quota_gateway <= 10_000_000 or (args.execute and args.review_only):
            raise RotationError("invalid_arguments")
        mode = "execute" if args.execute else "review-only" if args.review_only else "dry-run"
        if mode != "dry-run" and args.approval != APPROVAL:
            raise RotationError("explicit_approval_required")
        import boto3

        session = boto3.Session(region_name=args.region)
        return rotate(
            cfn=session.client("cloudformation"), cloudfront=session.client("cloudfront"),
            lambda_client=session.client("lambda"), distribution_id=args.distribution_id,
            role_arn=args.role_arn, quota=args.quota_gateway, mode=mode,
        )
    except RotationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"error: {type(exc).__name__}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
