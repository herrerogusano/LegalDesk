# Phase 09 acceptance — Memory

Status: local acceptance complete; AWS resources deployed and synthetic smoke
completed on 2026-09-17.

| Criterion | Evidence | Result |
|---|---|---|
| Same actor/session continuity in Memory | Four-invocation smoke persisted events; `ListEvents` found the synthetic marker in the same actor/session | PASS (storage) |
| Same-session Harness re-read | One bounded post-correction invocation with the same derived Alice actor/session recovered `LEGALDESK-SMOKE-ALPHA` | PASS |
| Different session isolation | Live storage inspection found no marker in Alice's other synthetic session; local tests cover the same boundary | PASS |
| Different actor/matter isolation | Live storage inspection found no marker for Bob's synthetic actor/matter; local tests cover derivation and authorization | PASS |
| IDs derived server-side | Identity/matter authorization and an exact server-side conversation binding are required; sealed scopes reject manual construction | PASS |
| Short-term only | AgentCore adapter sends `extractionMode=SKIP`; IaC has no strategies | PASS |
| Long-term data minimization | All long-term writes/retrievals raise `LongTermMemoryDisabled`, including prohibited and safe candidates | PASS |
| Harness compatibility | Typed `HarnessMemoryScope` is the only actor forwarding path; legacy calls remain unchanged | PASS |
| Least-privilege conditional IaC | Memory attachment disabled by default; exact Memory ARN and five actions when enabled | PASS |
| Retention/cleanup | Seven-day event expiry and explicit resource deletion policies are versioned | PASS |

Validation executed locally:

```text
python -m unittest tests.test_memory_phase_09 tests.test_agentcore_phase_01 -v
python -m compileall -q backend/src agent/src tests
git diff --check
```

## AWS evidence

- Stack `legaldesk-phase-09-memory`: `CREATE_COMPLETE`.
- Memory ARN: `arn:aws:bedrock-agentcore:eu-west-1:344774635844:memory/LegalDeskPhase09-NKV8SZFz5U`.
- Memory status: `ACTIVE`; `eventExpiryDuration=7`; `strategies=[]`.
- Stack `legaldesk-phase-01`: `UPDATE_COMPLETE`.
- Harness remained the same ARN and Runtime ARN, reached `READY`, and advanced
  from version 3 to version 5 after the attachment correction.
- Harness memory configuration is BYO Memory with `messagesCount=10`.
- Storage inspection found the marker only under Alice's synthetic actor/session;
  Alice's other session and Bob's synthetic actor/session had no marker.
- The five model invocations were synthetic only; no legal documents or real
  user data were used.

The first deployment omitted `MessagesCount`, so Harness did not re-inject the
same-session events even though Memory stored them. The template was corrected
to `messagesCount=10`, the Harness redeployed successfully, and one bounded
post-correction invocation recovered the synthetic marker.

## Retained resources, cost, and teardown

Retained: `legaldesk-phase-09-memory` Memory, the existing Phase 01 Harness and
Runtime, and all Phase 08 dependencies. Memory events, Harness/Bedrock
invocations, AgentCore control/data-plane calls, and CloudWatch logging can
incur charges.

To remove the Phase 09 resource after future approval:

```powershell
aws cloudformation delete-stack --region eu-west-1 --stack-name legaldesk-phase-09-memory
aws cloudformation wait stack-delete-complete --region eu-west-1 --stack-name legaldesk-phase-09-memory
```

To detach Memory while retaining the Phase 01 Harness, redeploy the Harness
template with `EnablePhase09Memory=false`; do not delete Phase 01 unless its
future dependencies have been reviewed.

No Phase 10 identity work was started.
