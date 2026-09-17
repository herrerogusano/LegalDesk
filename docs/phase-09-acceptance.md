# Phase 09 acceptance — Memory

Status: local implementation and acceptance tests complete; AWS deployment/smoke
remains pending explicit cost approval.

| Criterion | Evidence | Result |
|---|---|---|
| Same session continuity | `tests/test_memory_phase_09.py` derives a stable scope and reloads events | PASS |
| Different session isolation | Local store keys events by opaque actor/session pair | PASS |
| Different actor/matter isolation | Scope derivation includes verified user and stored tenant/matter context | PASS |
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

No Phase 10 identity work, AWS resource creation, inference, or paid smoke was
performed.
