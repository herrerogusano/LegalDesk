# LegalDesk

LegalDesk is a portfolio project for a grounded, tenant-safe legal-document
assistant built around Amazon Bedrock AgentCore. It is an educational MVP, not
a legal product, and it must only use public or wholly fictional documents.

## Current status

Phases 00–11 are complete and Phase 12 local evaluation is complete. The
portfolio MVP covers deterministic authorization, presigned document upload,
S3 Vectors-backed authorized retrieval, retrieve-then-generate chat with
citations, Guardrails, Gateway → MCP metadata tools, human review tasks,
short-term Memory, identity isolation, and redacted application telemetry.
Phase 12 adds 24 deterministic local evaluations across eight security/quality
categories. No real legal data is permitted and no Phase 12 real-model subset
is accepted as complete. The managed Harness smoke recorded `0/4` accepted
structured outcomes; a separate direct Bedrock smoke used `3` model calls plus
one local no-evidence case and accepted `2/4`; its one-time two-case follow-up
used exactly `2` additional calls and accepted `0/2`. Both remain incomplete
evidence, not production legal-quality validation.
The follow-up prompted a local system-prompt fix to version `1.2.0`; a bounded
final runner was executed once with exactly `2/2` calls and `0` retries, and
accepted `1/2`. The factual citation case remained insufficient evidence while
the untrusted-injection case passed. Phase 12 real-model acceptance remains
incomplete; no retry is permitted without separate authorization.
The separated `1.3.0` remediation was subsequently executed once over nine
synthetic cases. It used 9 resolver and 5 writer calls (`14` total), zero
retries, and accepted `3/9`: all factual cases passed, while partial and
injection cases exposed remaining resolver/grounding gaps.
The follow-up local fix now isolates resolver and writer prompts and uses a
claim-to-cited-passage synthetic grounding oracle. Its local regression is `9/9`; the
updated pipeline has not yet been revalidated against Bedrock.

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

Requires Python 3.11 or newer. The Phase 00 suite uses only the standard
library:

```bash
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
