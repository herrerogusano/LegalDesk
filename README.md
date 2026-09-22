# LegalDesk

LegalDesk is a portfolio project for a grounded, tenant-safe legal-document
assistant built around Amazon Bedrock AgentCore. It is an educational MVP, not
a legal product, and it must only use public or wholly fictional documents.

## Current status

Phases 00–12 are complete within their recorded component/bounded scopes.
Phase 13 now provides a connected loopback application demonstrated locally and
by a bounded real AWS browser smoke. The final smoke passed Cognito login,
presigned upload and verified confirmation, ingestion, factual/absent-evidence
RAG, citations, deterministic metadata through Gateway→MCP, review creation
through Gateway→Lambda, cross-matter denial, audit and logout. Harness was not
used as a router for either explicit action. Earlier attempts exposed and fixed
factory, hosted-login, Harness-routing, Playwright bootstrap, Gateway tool-name
and logout-runner defects; their reports remain immutable. All temporary
infrastructure was removed and shared stacks were restored after each attempt.
The release remains **NOT_READY_FOR_PROD** because the independent semantic
holdout, production hosting/distributed state and prod promotion remain outside
this smoke. See the historical
[release audit](docs/release-audit.md), the current
[Phase 13 acceptance ledger](docs/phase-13-acceptance.md), and
[application run instructions](docs/phase-13-run.md).

The demonstrated local journey is login → matter → presigned upload → verified
confirmation/indexing → authorized retrieval → Resolver → Writer → productive
grounding adapter → inspectable citation, with user-scoped Harness/Gateway/MCP
agentic support plus deterministic backend/Gateway metadata and review actions,
accepted short-term history and correlated audit.
Temporary Phase 13 resources were deployed and removed; shared stacks were
restored. See the [live-smoke ledger](docs/phase-13-live-smoke.md). No prod promotion.

The implemented components cover deterministic authorization, presigned document upload,
S3 Vectors-backed authorized retrieval, retrieve-then-generate chat with
citations, Guardrails, Gateway → MCP metadata tools, human review tasks,
short-term Memory, identity isolation, and redacted application telemetry.
Phase 12 adds 24 deterministic local evaluations across eight security/quality
categories. No real legal data is permitted. The managed Harness smoke
historically recorded `0/4` accepted
structured outcomes; a separate direct Bedrock smoke used `3` model calls plus
one local no-evidence case and accepted `2/4`; its one-time two-case follow-up
used exactly `2` additional calls and accepted `0/2`. Both remain incomplete
evidence, not production legal-quality validation.
The follow-up prompted a local system-prompt fix to version `1.2.0`; a bounded
final runner was executed once with exactly `2/2` calls and `0` retries, and
accepted `1/2`. The factual citation case remained insufficient evidence while
the untrusted-injection case passed. At that stage, real-model acceptance was
incomplete and no retry was permitted without separate authorization.
The separated `1.3.0` remediation was subsequently executed once over nine
synthetic cases. It used 9 resolver and 5 writer calls (`14` total), zero
retries, and accepted `3/9`: all factual cases passed, while partial and
injection cases exposed remaining resolver/grounding gaps.
The dedicated resolver/writer pipeline was then executed once with stage
prompts `1.0.0`: 9 resolver plus 6 writer calls (`15` total), zero retries, and
`5/9` accepted. All factual cases and two injection cases passed. Two partial
cases were incorrectly classified as having no material support, one partial
writer failed grounding, and one injection fact was classified as partial.
The metadata-only report is immutable. Stage prompts `1.1.0` then sharpened
`partial` versus `none`, preserved facts adjacent to embedded directives, and
added closed grounding diagnostic codes. Its authorized run used exactly 9
resolver and 9 writer calls, zero retries, and accepted `6/9`; importantly, the
resolver passed `9/9`, isolating all remaining failures to writer/oracle
phrasing. Writer `1.2.0` now preserves requested values with their units and
the oracle accepts bounded omission/count paraphrases. The release audit also
reproduced semantic false positives and legitimate-paraphrase false negatives;
this oracle is not a productive grounding validator. The writer-only follow-up
used 9 calls, zero retries, and accepted `8/9`; one partial answer failed lexical
validation, without retained text sufficient to prove why. A bounded
relation-token canonicalizer covers selected grammatical forms. The local
regression remains `9/9`. The one-call targeted check then passed `1/1`, giving
composite staged acceptance of resolver `9/9` and writer `9/9`, with all
reports metadata-only and all historical failures preserved. This closes the
Phase 12 bounded real-model subset; it does not claim production legal quality
or statistical model reliability.

## Repository layout

```text
agent/       Agent orchestration (introduced in Phase 01)
backend/     Domain and deterministic authorization code
docs/        Architecture, data model, security, and dataset design
frontend/    Minimal UI boundary and citation panel
infra/       Reproducible CloudFormation and deployment/teardown commands
evals/       Synthetic Phase 12 dataset, local runner, and report artifacts
tests/       Local unit tests and fictional multi-tenant fixtures
```

## Local verification

Requires Python 3.11 or newer plus the declared backend/agent dependencies.
The complete suite includes offline boto3 service-schema checks and signed-JWT
integration tests (the original Phase 00 subset used only the standard library):

```bash
python -m pip install -e '.[aws]' -e agent
python -m unittest discover -s tests -v
```

Run the Phase 12 deterministic evaluation without AWS or model inference:

```bash
python evals/run_evals.py --output evals/results/phase12-local-report.json
python -m unittest discover -s tests -p 'test_phase_12_evaluation.py' -v
```

The bounded real-model smoke is documented in
[`docs/phase-12-evaluation-report.md`](docs/phase-12-evaluation-report.md) and
is not a replacement for the deterministic security suite. It consumed exactly
four Harness attempts, with no retries, and remains incomplete: text-only
responses cannot prove authorization, refusal, escalation, or tool use.

The latest local run has 24/24 cases passing, 100% citation exactness,
bounded citation-to-fixture alignment, tool-choice accuracy, and cross-matter
denial. The alignment figure is not semantic model groundedness. Latency in
this report is process-local only; it is not a cloud performance SLO.

## Architecture and trust boundaries

The browser is untrusted. Cognito/OIDC or a verified Gateway edge supplies
identity; the backend reloads membership and derives an immutable request scope.
Retrieval is metadata-filtered before generation. Gateway tools reauthorize the
same scope. Memory is short-term and actor/session/matter scoped; long-term
memory is rejected. Application telemetry is an allowlist of metadata only.
See [`docs/architecture.md`](docs/architecture.md),
[`docs/architecture-final.md`](docs/architecture-final.md),
[`docs/authorization-matrix.md`](docs/authorization-matrix.md), and
[`docs/threat-model.md`](docs/threat-model.md).

## Setup, deploy, and teardown

Local work requires Python 3.11+ and the dependencies declared by the backend
and agent packages. Cloud deployment is phase-scoped and must use the documented
change-set commands under [`infra/`](infra/README.md). Phase 12 itself creates
no AWS resources. Teardown commands and retained-resource warnings are in
[`infra/phase-11-commands.md`](infra/phase-11-commands.md) and the phase
acceptance records.

## Trade-offs, limitations, and cost

This is an educational MVP, not legal advice or a production legal system.
S3 Vectors and fixed chunking keep the first architecture explainable, at the
cost of less hierarchical retrieval control. Long-term memory is disabled for
data minimization. Bedrock/AgentCore inference, Knowledge Base ingestion,
Memory, Gateway, Lambda, DynamoDB, S3, and CloudWatch can incur charges; the
Phase 12 deterministic local runner uses zero AWS calls, while the separately
authorized real-model runners call Bedrock. The managed Harness internal ADOT
detail is disabled after content extraction was observed; the application
allowlisted telemetry pointer is the supported operational trace surface.

Before real legal data enters the system, read
[`docs/what-i-would-change-before-real-legal-data.md`](docs/what-i-would-change-before-real-legal-data.md).
The five-minute synthetic walkthrough is in
[`docs/phase-12-walkthrough.md`](docs/phase-12-walkthrough.md).
The phase acceptance matrix is in
[`docs/phase-12-acceptance.md`](docs/phase-12-acceptance.md).

Planning and phase constraints are defined in `AGENTS.md`, `MASTER_PLAN.md`,
and the corresponding `PLAN_XX_*.md` file.
