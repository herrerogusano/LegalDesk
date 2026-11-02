# Phase 15 ADOT probe (T9.3) — runbook

Prepared in T7, **not executed**. Context: ADR-021 D9 and
[`phase-11-acceptance.md`](phase-11-acceptance.md). In Phase 11 the content
opt-out (`AWS_GENAI_CONTENT_EXTRACTION_OPT_OUT=true`) was ineffective and managed
ADOT was disabled (`DISABLE_ADOT_OBSERVABILITY=true`). The question for the agentic
chat is whether the opt-out works now, so the managed traces can be used.

## What is in the repository

| Piece | Where | Default |
|---|---|---|
| Parameter `EnableManagedAdot` | `infra/cloudformation/phase-01-harness.yaml` | `"false"` — ADOT stays disabled, the deployed state does not change |
| Content opt-out | same template | always `"true"`, regardless of the parameter |
| Probe | `scripts/phase15_adot_marker_probe.py` | dry run: prints the plan, zero network calls |

`EnableManagedAdot=true` only removes `DISABLE_ADOT_OBSERVABILITY`. Nothing else
in the Harness depends on it.

## Procedure (requires the owner's cost approval and a change set)

1. **Approval.** Note its reference (ticket, message id). The probe needs it.
2. **Change set** on `legaldesk-phase-01` with `EnableManagedAdot=true` and every other parameter unchanged.
   Review it: the only modified resource must be `MinimalHarness`, property
   `EnvironmentVariables`, `Replacement=False`. Execute it and wait for `READY`.
3. **Confirm the state**: `aws bedrock-agentcore-control get-harness` must show
   `AWS_GENAI_CONTENT_EXTRACTION_OPT_OUT=true` and no `DISABLE_ADOT_OBSERVABILITY`.
4. **Dry run**, then execute (one Harness call, no retries, ≤ 81 `FilterLogEvents` reads at the defaults):

   ```powershell
   python scripts/phase15_adot_marker_probe.py --harness-arn <arn> --runtime-id <runtime id> `
     --output evals/results/phase15-adot-probe-<yyyymmdd>-a.json
   python scripts/phase15_adot_marker_probe.py --harness-arn <arn> --runtime-id <runtime id> `
     --output evals/results/phase15-adot-probe-<yyyymmdd>-a.json `
     --approval-ref <ref> --adot-enabled-attested --execute
   ```

   `<runtime id>` is the last path segment of the `RuntimeArn` output of the stack.
5. **Read the verdict** in the report (immutable, metadata only, never the marker or any event):

   | Verdict | Meaning | Next step |
   |---|---|---|
   | `no_content_observed` | the unique marker did not appear in the runtime log groups within the window | keep ADOT on; document it in the T9 acceptance as bounded evidence, not proof |
   | `content_observed` | question or answer reached the provider logs | accept the residual risk of ADR-021 D9 (synthetic data, retention of 1–3 days, restricted IAM, `AGENTS.md` §7 qualified in writing) **or** redeploy with `EnableManagedAdot=false` |
   | `no_log_groups_found` / `invoke_failed` | no conclusion | new approval before any rerun |

6. If ADOT is not kept, redeploy with `EnableManagedAdot=false` (back to the Phase 11 state).

Do not query or quote the content of provider events. The probe counts matches
and discards them.

## Limits

- Absence in a bounded window is not proof; provider telemetry can arrive late.
- The probe covers the runtime log groups of this Harness. It does not enable
  Transaction Search, which adds account-wide span-ingestion cost.
- With real data the rule remains: opt-out verified, or spans of our own without content.
