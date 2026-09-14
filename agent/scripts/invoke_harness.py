"""Perform one explicit paid-capable Harness invocation after deployment."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from legaldesk_agent import HarnessInvocationError, HarnessInvoker, new_session_id


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--harness-arn", required=True)
    parser.add_argument("--region", default="eu-west-1")
    parser.add_argument("--session-id")
    parser.add_argument("message")
    args = parser.parse_args()

    session_id = args.session_id or new_session_id()
    print(f"session_id={session_id}", file=sys.stderr)
    try:
        result = HarnessInvoker.from_boto3(args.harness_arn, args.region).invoke(
            args.message,
            session_id=session_id,
        )
    except HarnessInvocationError as exc:
        print(f"invocation_failed={exc}", file=sys.stderr)
        return 2
    print(result.text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
