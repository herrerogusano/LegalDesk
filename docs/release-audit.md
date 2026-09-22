# Release audit baseline — 2026-09-21

Verdict: `NOT_READY_FOR_PROD`. Summary of the delivered release-audit conversation;
historical phase reports remain unchanged.

Phases 00–12 have bounded/component acceptance, not one integrated application.
Baseline: 315 local tests and 24/24 deterministic evals pass. No live AWS
inventory or inference was performed in the audit.

## Blockers

1. Frontend is a static citation example, with no connected login/upload/chat
   (`frontend/README.md`). No application entry point composes the whole flow.
2. Harness has its own prompt and outbound M2M credentials
   (`infra/cloudformation/phase-01-harness.yaml`); end-user propagation unproven.
3. `GroundingValidator` is only a protocol (`backend/src/legaldesk/evidence.py`);
   fixture oracle is not productive grounding.
4. Chat maps some prompt/model-contract/grounding failures to canonical
   insufficient evidence (`backend/src/legaldesk/chat.py`).
5. Citations open metadata cards rather than sources and expose S3 sourceUri.
6. Memory/correlation are demonstrated in separate flows; ChatResponse reports
   general prompt metadata rather than effective stage prompt identities.

## Evaluation limits reproduced

The lexical oracle accepts a contradictory partial answer containing both
"do not establish the requested name" and "the party name is established".
It rejects "The deadline lasts 17 days" despite 17-day evidence and rejects
an unspecified notice duration unless evidence explicitly marks the absence.
These are evaluation defects, not evidence of a production authorization bypass.

Real 9/9 is staged: resolver 9/9 (same-run total 6/9), writer 8/9, then targeted
1/1 after an oracle change. It is not a final nine-case E2E run. Without raw
answers, the earlier lexical failure cannot be proven to be only a false negative.

Preserve verified identity, bilateral membership, scope-filtered retrieval and
result rechecks, strict citation IDs, tool grants, review idempotency, scoped
short-term Memory and redacted logs. Phase 13 owns integration/readiness;
AWS smoke and prod promotion require later explicit authorization.
