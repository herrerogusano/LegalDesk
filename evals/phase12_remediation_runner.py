"""Local plan and bounded real-runner proposal for the Phase 12 remediation.

The default command only prints the eighteen-invocation plan.  Each of the
nine samples has one resolver invocation and, when it has supporting evidence,
one writer invocation. It intentionally does not
create a boto client or invoke AgentCore/Bedrock.  Execution requires a separate
operator decision and is outside the local acceptance gate.
"""

from __future__ import annotations

import argparse
import json
from typing import TypedDict


MAX_SAMPLE_COUNT = 9
MAX_RESOLVER_INVOCATIONS = 9
MAX_WRITER_INVOCATIONS = 9
MAX_REAL_MODEL_INVOCATIONS = MAX_RESOLVER_INVOCATIONS + MAX_WRITER_INVOCATIONS
MAX_PER_GROUP = 3
CASE_GROUPS = {
    "factual": tuple(f"factual-{index}" for index in range(1, 4)),
    "partial": tuple(f"partial-{index}" for index in range(1, 4)),
    "injection": tuple(f"injection-{index}" for index in range(1, 4)),
}


class PlannedCall(TypedDict):
    caseId: str
    category: str
    stage: str
    maxAttempts: int
    retries: int


def build_real_run_plan() -> tuple[PlannedCall, ...]:
    plan: list[PlannedCall] = []
    for category, case_ids in CASE_GROUPS.items():
        for case_id in case_ids:
            plan.append({"caseId": case_id, "category": category, "stage": "resolver", "maxAttempts": 1, "retries": 0})
            plan.append({"caseId": case_id, "category": category, "stage": "writer", "maxAttempts": 1, "retries": 0})
    if len(plan) != MAX_REAL_MODEL_INVOCATIONS:
        raise AssertionError("bounded remediation plan must contain exactly eighteen invocations")
    return tuple(plan)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="refuse: real execution requires a separately approved runner implementation",
    )
    args = parser.parse_args(argv)
    if args.execute:
        raise SystemExit(
            "refusing real execution: this local proposal does not call AWS; obtain separate approval and implement the provider adapter"
        )
    print(
        json.dumps(
            {
                "mode": "real-runner-proposal",
                "maxModelInvocations": MAX_REAL_MODEL_INVOCATIONS,
                "maxPerCategory": MAX_PER_GROUP,
                "retryCount": 0,
                "calls": list(build_real_run_plan()),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
