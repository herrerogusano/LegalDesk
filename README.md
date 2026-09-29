# LegalDesk

LegalDesk is a portfolio project for a grounded, tenant-safe legal-document
assistant built on Amazon Bedrock AgentCore. It demonstrates how retrieval,
authorization, citations, tool calls and bounded agentic workflows can be
composed without allowing the browser or the model to define access scope.

> **Status: `READY_FOR_CONSTRAINED_PROD_PROMOTION`**
>
> This is an educational MVP, not legal advice or a production legal system.
> Promotion is limited to the authenticated beta using only public or wholly
> fictional documents and pre-provisioned users. It is not certified for real
> legal/client data or anonymous signup.

## What it demonstrates

- Grounded RAG over authorized passages with exact, inspectable citations.
- Server-derived tenant, matter, membership and conversation scope.
- Deterministic metadata and durable review-queue actions through AgentCore Gateway,
  MCP and Lambda.
- AgentCore Harness reserved for genuinely agentic, model-selected workflows.
- Short-term, actor/session/matter-scoped history; long-term memory is disabled.
- Redacted, allowlisted application telemetry rather than document or prompt
  content logging.

## Architecture

```mermaid
flowchart LR
  B[Browser] --> I[Cognito/OIDC identity]
  I --> A[Backend authorization\nsealed RequestContext]
  A --> U[Upload API]
  U --> Z[(S3 quarantine)]
  Z --> V[Malware + content validation]
  V --> S3[(S3 authorized source)]
  A --> D[(DynamoDB\nmetadata/membership/review)]
  A --> R[Authorized retrieval filter]
  S3 --> KB[Knowledge Base + S3 Vectors]
  R --> KB
  KB --> Q[Resolver → Writer →\nGuardrails grounding]
  Q --> B
  A -->|fixed tools/call| GW[AgentCore Gateway]
  GW --> MCP[MCP metadata tools]
  GW --> L[Review Lambda]
  A -->|agentic only| H[AgentCore Harness/Runtime]
  A --> M[AgentCore Memory\nshort-term only]
```

The browser supplies selectors and questions, never authority. The backend
reloads membership and derives an immutable request scope before retrieval or
tool access. The model cannot choose a tenant, matter, document, review owner,
S3 key or memory actor. See the [final architecture](docs/architecture-final.md)
for the complete trust and data lifecycle.

### Deterministic and agentic routes

- **Grounded answers:** authorized retrieval feeds a schema-constrained
  Resolver, then an Answer Writer, then contextual grounding. The backend
  owns status and citation validity.
- **Explicit UI actions:** metadata and review-queue commands use one fixed
  backend → Gateway `tools/call` path. Gateway, MCP and the target handler
  reauthorize the same scope; Harness is not used as the router. Review
  snapshots are server-derived and bounded; the queue is purpose-specific and
  is not AgentCore long-term Memory.
- **Agentic workflows:** model-selected tool use may go through Harness with
  the same sealed JWT-bound scope and server-side authorization.

## Security boundaries

- Authorization is deterministic and server-side; `tenantId` and `matterId`
  from the browser are selectors only.
- Retrieval is filtered before generation, and citation inspection rechecks
  actor, matter, conversation and document access.
- Gateway interceptors and tool handlers reauthorize independently.
- Guardrails protect content but do not replace authorization.
- Retrieved documents are treated as untrusted data, not instructions.
- The public composition persists sessions, OAuth state, citation handles,
  accepted history/reviews and redacted audit records in bounded DynamoDB
  items. The loopback fixture may still use in-memory adapters for local demos.
- Public uploads are bound to a server-owned key and exact signed byte length,
  remain outside the retrieval prefix until malware/content validation passes,
  and are rechecked with `HeadObject` before lifecycle promotion.
- Long-term Memory and direct model credentials are not exposed to the browser.

## Verified evidence

- **574 tests passed** in the current local release-candidate verification
  (`1` platform-specific symlink test skipped on Windows).
- **24/24 deterministic evaluations passed** with zero AWS calls.
- **All 15 CloudFormation/SAM templates pass lint**, including the public edge,
  quarantine, reconciliation and operations candidates.
- **Final bounded AWS browser smoke: PASS** for the fixed synthetic journey
  (`phase14-public-smoke-20260929-final4-02.json`, report SHA-256
  `1431503bd4a336bf552853af4cb8eb7b87a8cc488a44e59b7542e9624281d153`): one
  chat, two citations, cross-matter `403`, 17 audit events, logout `200`, and
  `cleanupErrors=[]`.
  It covered Cognito login, indexed-document display, grounded RAG, citations,
  cross-matter denial, audit and logout. The earlier supervised walkthrough
  separately verified presigned upload, malware validation and indexing.
- The final metadata-only holdout `phase14-holdout-20260929-prod-final4.json`
  passed 14/14 with 27 calls and zero retries. It is pinned to release commit
  `0c8bb718e58811afff2085114dad7821cf573e3f` and artifact SHA-256
  `99d074e793335f2867266b07ae091cb110a4747b8ad51e9180b031a2091f4f31`; the
  approved independent attestation v1.1.0 has SHA-256
  `3d833232cabb1d290544009c31c7b9ecda0201264d9c331dc7d52cb799d48dc2`.
  Earlier failed reports and attestations remain immutable history.

Evidence classes, acceptance scenarios and remaining gates are recorded in the
[Phase 13 acceptance ledger](docs/phase-13-acceptance.md). The smoke result is
integration evidence, not production certification or a claim of broad legal
accuracy.

## Local verification

The supported local checks require Python 3.11+:

```powershell
python -m pip install -e '.[aws]' -e agent
python -B -m unittest discover -s tests -q
python -m legaldesk --help
```

Run the deterministic evaluation without AWS or model inference:

```powershell
python evals/run_evals.py --output evals/results/phase12-local-report.json
```

For the test-only local browser journey, start the fixture server and then run
the browser acceptance command described in [Phase 13 run instructions](docs/phase-13-run.md).
The AWS-backed entry point and its approval boundary are documented there; do
not enable AWS mode without the separately authorized smoke gate.

## Repository layout

```text
backend/   Domain services, authorization, retrieval and HTTP application
agent/     AgentCore integration adapters and package
frontend/  Dependency-free loopback UI and citation panel
infra/     CloudFormation templates and scoped deployment/teardown notes
evals/     Synthetic datasets, deterministic runner and evidence reports
tests/     Unit, integration, security and browser acceptance coverage
docs/      Architecture, trust boundaries, acceptance and release gates
```

## Limits and cost considerations

The repository contains both a loopback demo and a deployed candidate for a
constrained authenticated beta using CloudFront, API Gateway, Lambda and
durable DynamoDB state. Promotion evidence is complete for this narrow scope;
the default CloudFront TLS certificate is an accepted documented residual and
is not claimed as strict TLS. Anonymous signup and real legal/client documents
remain outside the approved scope.

S3, S3 Vectors, DynamoDB, Bedrock, AgentCore, Lambda and CloudWatch can incur
charges. Local tests and deterministic evaluations use no AWS calls. Any AWS
deployment or real-model run requires the documented approval, budget and
teardown procedure.

## Further reading

- [Production readiness gate](docs/production-readiness.md)
- [Phase 14 public-beta plan](PLAN_14_PUBLIC_BETA.md)
- [Phase 14 semantic holdout](docs/phase-14-holdout.md)
- [Phase 14 operations runbook](docs/phase-14-operations-runbook.md)
- [Guided offline manual demo](docs/phase-13-manual-demo.md)
- [Phase 13 application run instructions](docs/phase-13-run.md)
- [Threat model](docs/threat-model.md) and [authorization matrix](docs/authorization-matrix.md)
- [Frontend boundary](frontend/README.md)
- [Infrastructure notes](infra/README.md)
- [Phase 12 evaluation report](docs/phase-12-evaluation-report.md)
- [Phase 13 live-smoke ledger](docs/phase-13-live-smoke.md)
- [What must change before real legal data](docs/what-i-would-change-before-real-legal-data.md)

Historical phase reports and the detailed release chronology remain in `docs/`;
this README intentionally keeps those operational details out of the project
summary.
