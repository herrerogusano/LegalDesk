"""Cost model of the Phase 15 golden set, real mode (offline arithmetic, no AWS, no network).

It reuses the T9 model (``phase15_t9_cost_model``): the same price table (Sonnet 4.6 through the ``eu.`` profile, AgentCore,
Knowledge Base, Guardrails, Lambda), the same token profile per kind of question and the same upper-bound factors. What is new
is how the golden set uses them:

* every case makes one input Guardrail call; a case the Guardrail is expected to block stops there (typical), and goes on as
  an ordinary question in the upper bound;
* every other case is one Harness loop run through the production worker's own pieces (no queue, no job);
* every case with a RAG expectation, and every case that ends in the direct-RAG fallback, also runs the grounded pipeline
  once in process (the *probe*: resolver, writer, Knowledge Base, grounding Guardrail) to observe its stages;
* the ``asyncReal`` subset also goes through the deployed queue and worker (a full job: it consumes monthly quota), plus two
  real async scenarios.

The result is an *estimate with a stated upper bound*, never a billing figure.

    python scripts/phase15_golden_cost_model.py [--json]
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any, Mapping

try:
    from scripts import phase15_t9_cost_model as t9
except ImportError:  # run as a script: scripts/ is sys.path[0]
    import phase15_t9_cost_model as t9  # type: ignore[no-redef]

ROOT = Path(__file__).resolve().parents[1]
DATASET_PATH = ROOT / "evals" / "phase15_golden_dataset.json"

CLASS_OF_KIND = {
    "grounded": "B_text", "data": "D_data", "inventory": "C_inventory", "mixed": "E_mixed", "lookup_then_search": "E_mixed", "none": "A_no_tool",
}
INPUT_UNITS = t9.GUARDRAIL_UNITS["worker_input"]          # (typical, upper) text units of the input Guardrail call
SESSION_EXPIRED_JOB_USD = t9.JOB_STATE_USD + 2 / 1_000_000 * t9.LAMBDA_PER_MILLION_REQUESTS   # the worker fails closed: no model call
STOP_MARGIN = 1.25                                          # the stop rule is the upper bound of the Harness loop plus 25 %
# One-off cost of putting the corpus in the matter (outside the eval): IDP for the generated documents (~USD 0.0163 per document
# at the T9 token prices, docs/idp-evaluation.md) and Textract for their scanned pages; KB embeddings are a few cents at most.
CORPUS_PREPARATION_USD = {"typical": 0.2, "upper": 0.8}
LOGS_USD = {"typical": 0.02 * t9.CLOUDWATCH_LOGS_PER_GB, "upper": 0.05 * t9.CLOUDWATCH_LOGS_PER_GB}


def _key(upper: bool) -> int:
    return 1 if upper else 0


def input_check_usd(*, upper: bool) -> float:
    return INPUT_UNITS[_key(upper)] / 1000 * t9.GUARDRAIL_PER_1000_UNITS


def direct_run_usd(name: str, *, upper: bool) -> float:
    """One Harness loop plus the tools it runs (no worker, no queue, no history): the T9 job cost minus the worker's own parts."""

    key, cls = _key(upper), t9.CLASSES[name]
    _tin, _tout, tokens = t9._tokens_cost(cls.calls, upper=upper)  # noqa: SLF001
    lambda_gb_s = cls.searches * (25 if upper else 8) + cls.tool_calls * 0.5 * (3 if upper else 1.5) + cls.gateway_ops[key] * 0.25 * (0.6 if upper else 0.3)
    lambda_requests = cls.searches + cls.tool_calls + cls.gateway_ops[key] / 2
    return (
        tokens + t9._runtime_cost(cls.seconds[key], upper=upper)  # noqa: SLF001
        + cls.gateway_ops[key] / 1000 * t9.GATEWAY_PER_1000_OPS + 1 / 1000 * t9.IDENTITY_PER_1000_TOKENS
        + cls.searches / 1000 * t9.KB_RETRIEVE_PER_1000
        + cls.searches * t9.GUARDRAIL_UNITS["per_search"][key] / 1000 * t9.GUARDRAIL_PER_1000_UNITS
        + lambda_gb_s * t9.LAMBDA_PER_GB_SECOND + lambda_requests / 1_000_000 * t9.LAMBDA_PER_MILLION_REQUESTS
    )


def probe_usd(first_status: str, *, upper: bool) -> float:
    """The in-process grounded pipeline: resolver (and writer unless the resolver finds no support), Knowledge Base, grounding Guardrail."""

    key = _key(upper)
    calls = (t9.R(3000, 150),) if first_status == "insufficient_evidence" else t9.RAG_FALLBACK
    _tin, _tout, tokens = t9._tokens_cost(calls, upper=upper)  # noqa: SLF001
    guardrail = 0 if first_status == "insufficient_evidence" else t9.GUARDRAIL_UNITS["per_search"][key] / 1000 * t9.GUARDRAIL_PER_1000_UNITS
    return tokens + 1 / 1000 * t9.KB_RETRIEVE_PER_1000 + guardrail


def classify(case: Mapping[str, Any]) -> dict[str, Any]:
    expected = case["expected"]
    kind = expected["route"]["kind"]
    inputs = expected.get("input", "allowed")
    first_input = inputs[0] if isinstance(inputs, list) else inputs
    rag = expected.get("rag")
    return {
        "id": case["id"], "class": CLASS_OF_KIND[kind], "blockedFirst": first_input == "blocked",
        "probe": bool(rag) or kind == "none", "probeStatus": (rag["status"][0] if rag else "insufficient_evidence"),
        "async": bool(case.get("asyncReal")),
    }


def case_usd(entry: Mapping[str, Any], *, upper: bool) -> dict[str, float]:
    parts = {"input_guardrail": input_check_usd(upper=upper), "harness_run": 0.0, "probe": 0.0}
    if entry["blockedFirst"] and not upper:
        return parts   # the Guardrail stops it: no job, no quota, no Harness call
    parts["harness_run"] = direct_run_usd(entry["class"], upper=upper)
    if entry["probe"]:
        parts["probe"] = probe_usd(entry["probeStatus"], upper=upper)
    return parts


def estimate(dataset: Mapping[str, Any], *, upper: bool) -> dict[str, Any]:
    entries = [classify(case) for case in dataset["cases"]]
    totals = {"input_guardrail": 0.0, "harness_run": 0.0, "probe": 0.0, "async_jobs": 0.0, "async_scenarios": 0.0}
    harness = probes = 0
    loop_only = 0.0
    by_class: dict[str, dict[str, float]] = {}
    for entry in entries:
        parts = case_usd(entry, upper=upper)
        for name, value in parts.items():
            totals[name] += value
        ran = parts["harness_run"] > 0
        harness += int(ran)
        probes += int(parts["probe"] > 0)
        if ran:
            loop_only += t9._tokens_cost([c for c in t9.CLASSES[entry["class"]].calls if c[0] == "H"], upper=upper)[2]  # noqa: SLF001
            bucket = by_class.setdefault(entry["class"], {"cases": 0, "usd": 0.0})
            bucket["cases"] += 1
            bucket["usd"] += sum(parts.values())
    async_entries = [entry for entry in entries if entry["async"]]
    for entry in async_entries:
        totals["async_jobs"] += t9.job_cost_usd(entry["class"], upper=upper)
    totals["async_scenarios"] = SESSION_EXPIRED_JOB_USD + t9.job_cost_usd("C_inventory", upper=upper)
    harness += len(async_entries) + 1   # the async cases and the duplicate-delivery scenario run the agent; the expired-session one does not
    corpus = CORPUS_PREPARATION_USD["upper" if upper else "typical"]
    other = LOGS_USD["upper" if upper else "typical"] + (0.01 if upper else 0.0)
    total = sum(totals.values()) + other
    return {
        "harness_invocations": harness, "probes": probes, "input_checks": len(entries),
        "usd": {name: round(value, 4) for name, value in totals.items()} | {"logs_and_state": round(other, 4)},
        "per_class": {name: {"cases": int(v["cases"]), "usd_per_case": round(v["usd"] / v["cases"], 4)} for name, v in sorted(by_class.items())},
        "harness_loop_only_usd": round(loop_only, 4), "total_usd": round(total, 2), "corpus_preparation_usd": corpus,
        "total_with_corpus_usd": round(total + corpus, 2),
    }


def report(dataset: Mapping[str, Any] | None = None) -> dict[str, Any]:
    dataset = dataset if dataset is not None else json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    typical, upper = estimate(dataset, upper=False), estimate(dataset, upper=True)
    cases = len(dataset["cases"])
    subset = sum(1 for case in dataset["cases"] if case.get("asyncReal"))
    stop = math.ceil(upper["harness_loop_only_usd"] * STOP_MARGIN * 10) / 10
    return {
        "prices": {"inputPerMillion": t9.INPUT_PER_M, "outputPerMillion": t9.OUTPUT_PER_M},
        "caps": {
            "cases": cases, "input_guardrail_calls": cases, "harness_invocations": cases + subset + 1, "probes": cases, "retries": 0,
            "async_jobs": subset + 2, "monthly_quota_units": subset + 2, "stop_if_harness_loop_usd_exceeds": stop,
        },
        "typical": typical, "upper": upper, "per_case_average_usd": {"typical": round(typical["total_usd"] / cases, 3), "upper": round(upper["total_usd"] / cases, 3)},
        "async_subset": {"cases": subset, "scenarios": 2, "typical_usd": round(typical["usd"]["async_jobs"] + typical["usd"]["async_scenarios"], 3),
                         "upper_usd": round(upper["usd"]["async_jobs"] + upper["usd"]["async_scenarios"], 3)},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = report()
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    print(f"Sonnet 4.6 (eu.): ${result['prices']['inputPerMillion']}/M input, ${result['prices']['outputPerMillion']}/M output; caps: {result['caps']}")
    for label in ("typical", "upper"):
        part = result[label]
        print(f"\n[{label}] Harness invocations={part['harness_invocations']} probes={part['probes']} input checks={part['input_checks']}")
        for name, value in part["usd"].items():
            print(f"  {name:<20} ${value:>8.4f}")
        print(f"  {'TOTAL (one-off)':<20} ${part['total_usd']:>8.2f}   + corpus preparation ${part['corpus_preparation_usd']:.2f} = ${part['total_with_corpus_usd']:.2f}")
    subset = result["async_subset"]
    print(f"\nAsync subset ({subset['cases']} cases + {subset['scenarios']} scenarios): typical ${subset['typical_usd']:.3f}, upper ${subset['upper_usd']:.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
