"""Cost model of the PLAN_15 T9 live block (offline arithmetic, no AWS, no network).

Every assumption is data in this file; ``docs/phase-15-t9-cost-estimate.md`` quotes the
output and says where each price comes from.  Prices are public list prices read on the
vendor pages on 2026-11-02 (see the document for the source of each one); the model is an
*estimate with a stated upper bound*, never a billing figure.

    python scripts/phase15_t9_cost_model.py [--json]
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from typing import Mapping

# --- Bedrock: Claude Sonnet 4.6 through the `eu.` inference profile -------------------------
# First-party/base list price $3 / $15 per million tokens; a regional (geographic cross-region)
# endpoint carries a 10 % premium over the global one (Anthropic pricing + Bedrock docs).
BASE_INPUT_PER_M = 3.00
BASE_OUTPUT_PER_M = 15.00
REGIONAL_PREMIUM = 1.10
INPUT_PER_M = round(BASE_INPUT_PER_M * REGIONAL_PREMIUM, 6)
OUTPUT_PER_M = round(BASE_OUTPUT_PER_M * REGIONAL_PREMIUM, 6)

# Output caps of each kind of model call (tokens): Harness AGENT_CHAT_MAX_TOKENS, resolver, writer.
OUTPUT_CAP = {"H": 2048, "R": 256, "W": 512}
INPUT_UPPER_FACTOR = 1.6  # longer history / passages than assumed
OUTPUT_UPPER_FACTOR = 2.0  # bounded by OUTPUT_CAP

# --- AgentCore (public pricing page, 2026) -------------------------------------------------
RUNTIME_CPU_PER_VCPU_HOUR = {"typical": 0.0895, "upper": 0.1276}  # v1 / v2 consumption
RUNTIME_MEM_PER_GB_HOUR = {"typical": 0.00945, "upper": 0.0169}
RUNTIME_SIZE = {"typical": (1.0, 2.0), "upper": (2.0, 4.0)}  # (vCPU, GB); Harness size is not published
GATEWAY_PER_1000_OPS = 0.005
IDENTITY_PER_1000_TOKENS = 0.010  # outbound OAuth token per Harness invocation

# --- Other services -------------------------------------------------------------------------
KB_RETRIEVE_PER_1000 = 1.00
GUARDRAIL_PER_1000_UNITS = 0.15 + 0.15 + 0.10 + 0.15  # content + topics + grounding + PII (assumed 0.15)
LAMBDA_PER_GB_SECOND = 0.0000166667
LAMBDA_PER_MILLION_REQUESTS = 0.20
CLOUDWATCH_LOGS_PER_GB = 0.50
CLOUDWATCH_ALARM_METRIC_MONTH = 0.10
CLOUDWATCH_CUSTOM_METRIC_MONTH = 0.30

Call = tuple[str, int, int]  # kind (H harness iteration, R resolver, W writer), input, output tokens


@dataclass(frozen=True)
class QuestionClass:
    name: str
    calls: tuple[Call, ...]
    gateway_ops: tuple[int, int]  # typical, upper
    seconds: tuple[int, int]  # Harness session seconds, typical, upper
    searches: int  # search_documents calls (KB Retrieve + Guardrail)
    tool_calls: int  # MCP/Lambda tool calls other than search (list / get)


H = lambda i, o: ("H", i, o)  # noqa: E731
R = lambda i, o: ("R", i, o)  # noqa: E731
W = lambda i, o: ("W", i, o)  # noqa: E731

CLASSES: Mapping[str, QuestionClass] = {
    "A_no_tool": QuestionClass("A_no_tool", (H(2700, 150),), (2, 3), (8, 20), 0, 0),
    "B_text": QuestionClass("B_text", (H(2700, 120), R(3000, 150), W(3500, 250), H(3600, 400)), (4, 8), (30, 60), 1, 0),
    "C_inventory": QuestionClass("C_inventory", (H(2700, 120), H(3400, 300)), (5, 9), (20, 45), 0, 1),
    "D_data": QuestionClass("D_data", (H(2700, 120), H(3400, 120), H(4700, 600)), (8, 12), (35, 70), 0, 2),
    "E_mixed": QuestionClass("E_mixed", (H(2700, 120), R(3000, 150), W(3500, 250), H(3600, 120), H(5000, 800)), (10, 16), (55, 90), 1, 1),
}
# Extra model calls of the direct RAG fallback (resolver + writer) and the cut-loop check.
RAG_FALLBACK = (R(3000, 150), W(3500, 250))
GUARDRAIL_UNITS = {"worker_input": (2, 4), "per_search": (12, 25)}

# --- The block: how many of each --------------------------------------------------------------
# Eval real: 30 cases of the frozen dataset (5 no-tool, 9 text, 5 inventory, 7 data, 4 mixed).
# Smoke: ~10 browser questions through the worker (+1 forced fallback = one extra RAG answer).
BLOCK: Mapping[str, Mapping[str, int]] = {
    "eval": {"A_no_tool": 5, "B_text": 9, "C_inventory": 5, "D_data": 7, "E_mixed": 4},
    "smoke": {"A_no_tool": 1, "B_text": 4, "C_inventory": 2, "D_data": 2, "E_mixed": 1},
}
SMOKE_HISTORY_INPUT_FACTOR = 1.15  # up to 6 pairs of history on every Harness call
SMOKE_FALLBACKS = 1
SMOKE_WORKER_SECONDS = (35, 80)  # per job, worker Lambda at 1 GB
# One-off checks: ADOT probe (1 Harness call), then scripts/phase15_harness_checks.py: allowed-tools
# pre-check (1 call, tools on, trivial reply), session check (2 calls, no tools) and cut-loop check
# (2 calls, tools on, maxIterations=1 so the loop ends after the first completed tool).
ADOT_CALLS = (H(1700, 150),)
ALLOWED_TOOLS_CALLS = (H(2700, 30),)
SESSION_CALLS = (H(1700, 100), H(1700, 100))
CUT_CALLS = (H(2700, 120), R(3000, 150), W(3500, 250)) * 2

HARD_CAP = {
    "harness_invocations": 50,  # 30 eval + 10 smoke + 1 ADOT + 1 allowed-tools + 2 session + 2 cut-loop + 4 reserve
    "invocation_retries": 0,
    "approved_ceiling_usd": 5.00,
    # The eval report sums `estimatedLoopCostUsd`, which covers the Harness iterations only
    # (not the resolver/writer inside the tools). Stop the eval if that sum passes the upper
    # bound of the model with a 25 % margin; the total ceiling is checked in Billing afterwards.
    "stop_eval_if_loop_usd_exceeds": 2.20,
}
MONTHLY_FIXED = {
    "alarm_metrics": 6,  # DLQ, duration, failure, queue age (1 each) + fallback rate (2: metric math)
    "custom_metrics_typical": 8,
    "custom_metrics_upper": 14,
}


def _tokens_cost(calls: tuple[Call, ...] | list[Call], *, upper: bool, input_factor: float = 1.0) -> tuple[int, int, float]:
    tokens_in = tokens_out = 0
    for kind, i, o in calls:
        if upper:
            i = int(i * INPUT_UPPER_FACTOR * input_factor)
            o = min(int(o * OUTPUT_UPPER_FACTOR), OUTPUT_CAP[kind])
        else:
            i = int(i * input_factor)
        tokens_in += i
        tokens_out += o
    return tokens_in, tokens_out, (tokens_in * INPUT_PER_M + tokens_out * OUTPUT_PER_M) / 1_000_000


def _runtime_cost(seconds: float, *, upper: bool) -> float:
    key = "upper" if upper else "typical"
    vcpu, gb = RUNTIME_SIZE[key]
    return seconds / 3600 * (vcpu * RUNTIME_CPU_PER_VCPU_HOUR[key] + gb * RUNTIME_MEM_PER_GB_HOUR[key])


def estimate(upper: bool) -> dict[str, object]:
    """Total of the block for the typical (``upper=False``) or upper-bound assumptions."""

    key = 1 if upper else 0
    bedrock = {"eval": 0.0, "smoke": 0.0, "checks": 0.0}
    tokens = {"input": 0, "output": 0}
    loop = {"eval": 0.0, "smoke": 0.0}  # Harness iterations only: what `estimatedLoopCostUsd` reports
    harness_runtime = gateway_ops = identity = 0.0
    kb_calls = guardrail_units = lambda_gb_s = lambda_requests = 0.0
    invocations = 0

    def add_calls(bucket: str, calls: tuple[Call, ...] | list[Call], factor: float = 1.0) -> None:
        tokens_in, tokens_out, cost = _tokens_cost(calls, upper=upper, input_factor=factor)
        bedrock[bucket] += cost
        tokens["input"] += tokens_in
        tokens["output"] += tokens_out
        if bucket in loop:
            loop[bucket] += _tokens_cost([c for c in calls if c[0] == "H"], upper=upper, input_factor=factor)[2]

    for group, factor in (("eval", 1.0), ("smoke", SMOKE_HISTORY_INPUT_FACTOR)):
        for name, count in BLOCK[group].items():
            cls = CLASSES[name]
            for _ in range(count):
                add_calls(group, cls.calls, factor)
                invocations += 1
                harness_runtime += _runtime_cost(cls.seconds[key], upper=upper)
                gateway_ops += cls.gateway_ops[key]
                identity += 1
                kb_calls += cls.searches
                guardrail_units += cls.searches * GUARDRAIL_UNITS["per_search"][key]
                lambda_gb_s += cls.searches * (25 if upper else 8) + cls.tool_calls * 0.5 * (3 if upper else 1.5) + cls.gateway_ops[key] * 0.25 * (0.6 if upper else 0.3)
                lambda_requests += cls.searches + cls.tool_calls + cls.gateway_ops[key] / 2
                if group == "smoke":
                    guardrail_units += GUARDRAIL_UNITS["worker_input"][key]
                    lambda_gb_s += SMOKE_WORKER_SECONDS[key]
                    lambda_requests += 1
    for _ in range(SMOKE_FALLBACKS):
        add_calls("smoke", list(RAG_FALLBACK))
        kb_calls += 1
        guardrail_units += GUARDRAIL_UNITS["per_search"][key]
    for calls, count_invocations, searches in ((ADOT_CALLS, 1, 0), (ALLOWED_TOOLS_CALLS, 1, 0), (SESSION_CALLS, 2, 0), (CUT_CALLS, 2, 2)):
        add_calls("checks", calls)
        invocations += count_invocations
        harness_runtime += count_invocations * _runtime_cost(40 if upper else 15, upper=upper)
        identity += count_invocations
        kb_calls += searches
        guardrail_units += searches * GUARDRAIL_UNITS["per_search"][key]
        lambda_gb_s += searches * (25 if upper else 8)
    breakdown = {
        "bedrock_eval": bedrock["eval"],
        "bedrock_smoke": bedrock["smoke"],
        "bedrock_checks": bedrock["checks"],
        "agentcore_runtime": harness_runtime,
        "agentcore_gateway": gateway_ops / 1000 * GATEWAY_PER_1000_OPS,
        "agentcore_identity": identity / 1000 * IDENTITY_PER_1000_TOKENS,
        "kb_retrieve": kb_calls / 1000 * KB_RETRIEVE_PER_1000,
        "guardrail": guardrail_units / 1000 * GUARDRAIL_PER_1000_UNITS,
        "lambda": lambda_gb_s * LAMBDA_PER_GB_SECOND + lambda_requests / 1_000_000 * LAMBDA_PER_MILLION_REQUESTS,
        # SQS, DynamoDB and S3 are a few hundred requests: far below their free tiers and < USD 0.01.
        "sqs_dynamodb_s3": 0.01 if upper else 0.0,
        # < 20 MB of logs/spans in the block (free tier 5 GB), priced at the ingestion rate anyway.
        "cloudwatch_logs_spans": (0.05 if upper else 0.02) * CLOUDWATCH_LOGS_PER_GB,  # GB ingested x price
        "cloudwatch_alarms_and_metrics_month": MONTHLY_FIXED["alarm_metrics"] * CLOUDWATCH_ALARM_METRIC_MONTH
        + MONTHLY_FIXED["custom_metrics_upper" if upper else "custom_metrics_typical"] * CLOUDWATCH_CUSTOM_METRIC_MONTH,
    }
    one_off = sum(value for name, value in breakdown.items() if not name.endswith("_month"))
    return {
        "harness_invocations": invocations,
        "tokens": tokens,
        "breakdown_usd": {name: round(value, 4) for name, value in breakdown.items()},
        "harness_loop_only_usd": {name: round(value, 2) for name, value in loop.items()},
        "block_total_usd": round(one_off, 2),
        "monthly_while_enabled_usd": round(breakdown["cloudwatch_alarms_and_metrics_month"], 2),
    }


def report() -> dict[str, object]:
    return {"prices": {"inputPerMillion": INPUT_PER_M, "outputPerMillion": OUTPUT_PER_M}, "hardCap": HARD_CAP, "typical": estimate(False), "upper": estimate(True)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = report()
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    print(f"Sonnet 4.6 (eu.): ${INPUT_PER_M}/M input, ${OUTPUT_PER_M}/M output; hard cap: {HARD_CAP}")
    for label in ("typical", "upper"):
        part = result[label]
        print(f"\n[{label}] Harness invocations={part['harness_invocations']} tokens={part['tokens']}")
        for name, value in part["breakdown_usd"].items():
            print(f"  {name:<38} ${value:>8.4f}")
        print(f"  {'BLOCK TOTAL (one-off)':<38} ${part['block_total_usd']:>8.2f}   + monthly while enabled ${part['monthly_while_enabled_usd']:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
