# PLAN 13 — Integration and release readiness

## Status, scope and authority

Local checkpoints complete with provider doubles; AWS execution remains gated. Phases 00–12 remain complete within their recorded
component/bounded scopes, not as proof of an integrated application.
Release verdict: `NOT_READY_FOR_PROD`; see `docs/release-audit.md`.

Integrate existing capabilities only. Local implementation/tests are authorized;
AWS deployment, real smoke and developer→prod promotion are NOT authorized.
Stop if an important architectural incompatibility requires an ADR decision.
Use one Luna/High worker by default, supervised for architecture/integration.
Branch: `phase/13-integration-release`, from `developer`.

## Checkpoint 0 — Plan and baseline

- [x] Read instructions, requirements, ADRs, cost policy, acceptance 00–12,
  release audit, README and architecture.
- [x] Baseline before application changes: 315 tests pass; deterministic evals
  24/24, zero AWS calls, no report overwrites.
- [x] Add Phase 13 to master plan; distinguish component and E2E readiness.
- [x] Record potentially necessary resources, without claiming live inventory.
- [x] Resolve trusted identity/correlation transport through managed Harness.
  Never invent a transport in doubles that the provider does not support.

Feasibility confirmed locally against the installed SDK and official Harness
Tools documentation: invoke-time tools/allowedTools overrides support
remoteMcp URL and headers. Use the existing JWT Gateway endpoint with the
verified user's token in transport headers, not messages; replace M2M defaults
and bind selected matter/session/correlation server-side. No ADR service change
or custom Runtime is needed. Provider behavior still needs the later live smoke.
Reference: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-tools.html

Baseline: developer `eedc520abf4044f453f1b251e3fe66cbe0230639`, 2026-09-21.

```powershell
python -B -m unittest discover -s tests -q
python -B -c "from evals.runner import evaluate_dataset; r=evaluate_dataset(); print(r['passedCases'], r['totalCases'], r['awsCalls'])"
git diff --check
```

## Checkpoint 1 — One executable application

Compose identity, authorization, uploads, ingestion, retrieval, Resolver,
Writer, Guardrails, Harness, Gateway, MCP, Review Lambda, Memory and telemetry
behind one application entry point. Reuse business logic; local doubles must
match real provider interfaces.

Acceptance: authenticated matter selection through final response in a local
integration test, without manual intermediate changes. Server owns IDs/scope.

## Checkpoint 2 — End-to-end identity

JWT establishes identity; matterId remains untrusted. Bind the verified user
and authorized scope through backend→Harness→Gateway→MCP/Lambda→Memory.
Reauthorize tools; service M2M privileges cannot become collective user access.
Do not pass credentials or authority through prompts/model-produced fields.

Acceptance: A→A and B→B allow; A→B, forged matter, guessed foreign document,
foreign session/memory and expired token deny before business reads/writes.
Review creator is the actual user, not the technical service actor.

## Checkpoint 3 — Productive grounding

Keep retrieval→Resolver→backend status/citations→Writer→grounding→response.
Define a concrete provider adapter, reusing Guardrails where appropriate;
missing/malformed assessments fail closed. Never import the fixture lexical
oracle into the productive path.

Before changing prompts, freeze independent cases covering legitimate
paraphrase, contradictory relation, implicit partial evidence, multi-passage
answer, document conflict, date, quantity, obligation, party name/relationship,
and injection adjacent to a valid fact. Do not tune to their exact strings.

Acceptance: no lexical-only rejection of paraphrases or acceptance of
contradictions; preserve partial evidence/citations; reject invented IDs.
Local doubles prove contracts, not provider semantic accuracy. Keep independent
provider-quality validation separate and explicitly pending AWS approval.

## Checkpoint 4 — Operational errors are not documentary absence

Reserve insufficient_evidence for absent/partial documentary support. Prompt,
provider, JSON, Resolver, Writer, grounding, Gateway/MCP/Lambda failures return
a safe operational error, never the rejected answer or a no-evidence assertion.
Acceptance: no technical failure increments not_found; closed codes and
fail-closed behavior preserved.

## Checkpoint 5 — Minimal connected UI and citations

Connect login, matter selector, upload, processing, chat, citations, metadata,
review creation, loading/errors and audit access. No visual redesign/features.
Citations show authorized supporting passage or authorized temporary document
access, with name/page/section where available. Reauthorize inspection; never
expose bucket, S3 key, internal path or permanent internal URL.

## Checkpoint 6 — Integrated observability

One operation correlation ID with safely bound actor/matter, bounded retrieval
metadata, Resolver/Writer/grounding outcomes, tools, Guardrails and final outcome.
Record effective general/resolver/writer prompt versions and hashes. Exclude
JWTs, credentials, full documents/passages, answers and full prompts from logs.
Never re-enable managed content logging. Distinguish application correlation
from provider-owned trace IDs; reconstruct the application/tool operation.

## Checkpoint 7 — Local integration gate

Cover factual answer, partial evidence, no evidence, conflict, injection,
cross-matter, processing document, model failure, MCP metadata, review creation,
expired token and citation inspection. Run all existing tests and applicable
frontend syntax/static IaC checks. No AWS work while regressions remain.

## Checkpoint 8 — Design only: bounded real smoke

Login→Matter A→fictional PDF upload→INDEXED→factual ask→answer/citation→inspect
evidence→absent-fact ask→MCP metadata→review→denied cross-matter→audit.

Before asking for execution approval, fix exact caps for model attempts/tokens,
Guardrail/retrieval/Gateway/Memory/storage requests, ingestion jobs and polling.
Zero automatic retries. Publish an estimated maximum with pricing sources,
assumptions and retention duration, not an unenforceable billing guarantee.
Stop on caps, failures or unexpected changes. Do not execute deploy/smoke.

Potential resources (historical status only; no live inventory):

| Resource | Evidence / later need |
|---|---|
| Harness/underlying Runtime and role | Phase 01/11 retained; verified-user remote MCP transport defined locally, live behavior pending |
| Gateway/interceptor/MCP Lambda and Function URL | Phase 08/10/11 retained; reuse |
| Review Lambda, metadata/membership DynamoDB | Phase 07+ dependencies; verify actual stack/table before reuse |
| Cognito public/service clients, OAuth provider/managed secret | Phase 08/10 retained; verified-user integration required |
| Short-term Memory | Phase 09 retained, seven-day expiry, no long-term strategies |
| Source S3, KB/data source, S3 Vectors, Titan embeddings | Phase 03 smoke torn down; current fixed chunking not live-tested |
| Guardrail/version | Phase 06 smoke torn down; inventory before recreation |
| Artifact S3 versions, logs, metric filters | Phase 08/11 retained; storage/log/metric costs possible |

Draft caps/cost are recorded in `docs/phase-13-smoke-plan.md`; reconcile with
the completed application before requesting approval. No new service selected.
Teardown must inventory dependencies, remove only approved synthetic originals,
sidecars and vectors in safe order, review versioned artifacts and managed
secrets, and preserve pre-existing resources unless deletion is authorized.

## Checkpoint 9 — Documentation and release gate

Separate unit/component tests, deterministic evals, real-model evals and E2E
evidence. Describe executable architecture, preserve historical reports, and
prepare—but do not execute—the developer→prod checklist.

## Definition of done

- [x] Local E2E application and functional minimal UI.
- [x] Verified user reaches tools; integral cross-matter isolation locally.
- [x] Resolver/Writer wired and concrete productive grounding defined.
- [x] Technical errors distinct from documentary insufficient_evidence.
- [x] Scoped short-term Memory integrated where applicable.
- [x] Authorized inspectable citations without internal locations.
- [x] E2E correlation and effective prompt metadata.
- [x] Integration and complete regression suites green: 378 tests; 24/24 deterministic evals.
- [x] Documentation accurately describes demonstrated behavior.
- [x] Fixed real smoke, proposed budget/request ceilings and teardown prepared.

Offline browser acceptance passes at desktop/mobile sizes, including operational
error handling. Detailed evidence and remaining production gaps are recorded in
`docs/phase-13-acceptance.md`. Real provider semantic accuracy and the independent
holdout remain unverified. No AWS deployment, smoke or prod promotion occurred.

At local completion report architecture, changes, tests, blockers, exact smoke
and cost estimate; STOP for explicit AWS authorization. Local completion is not
live E2E evidence and does not authorize promotion to prod.
