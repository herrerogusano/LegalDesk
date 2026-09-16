# LegalDesk

LegalDesk is a portfolio project for a grounded, tenant-safe legal-document
assistant built around Amazon Bedrock AgentCore. It is an educational MVP, not
a legal product, and it must only use public or wholly fictional documents.

## Current status

Phases 00–04 are complete. The project now covers deterministic authorization,
presigned document upload, S3 Vectors-backed authorized retrieval, and a
retrieve-then-generate chat boundary with fail-closed citations and a local
citation panel. Phase 04 was validated with local fakes only: it made no AWS
retrieval or model-inference calls and created no AWS resources.

## Repository layout

```text
agent/       Agent orchestration (introduced in Phase 01)
backend/     Domain and deterministic authorization code
docs/        Architecture, data model, security, and dataset design
frontend/    Minimal UI boundary (implemented in a later phase)
infra/       Reproducible infrastructure (implemented in later phases)
tests/       Local unit tests and fictional multi-tenant fixtures
```

## Local verification

Requires Python 3.11 or newer. The Phase 00 suite uses only the standard
library:

```bash
python -m unittest discover -s tests -v
```

Planning and phase constraints are defined in `AGENTS.md`, `MASTER_PLAN.md`,
and the corresponding `PLAN_XX_*.md` file.
