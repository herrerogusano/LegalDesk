# Continuous delivery — constrained production beta

This repository contains a fail-closed CD path for the existing
`LegalDeskPhase14PublicEdge` stack. It is an application-code release path,
not an infrastructure migration: the checked-in public-edge template must
match the deployed template before a change set is created, and the change set
may contain only `ApplicationFunction` code and the dependent
`PublicApiIntegration` URI, both without replacement.

The separately reviewed IAM bootstrap has been applied as stack
`LegalDeskProductionCD`; its change set contained only the OIDC deployment role
and the constrained CloudFormation execution role. No application, data-plane,
frontend, or production code mutation was part of that bootstrap.

## Verified release — 2026-10-05

Promotion PR #49 triggered [production run 37310326393](https://github.com/herrerogusano/LegalDesk/actions/runs/37310326393)
for commit `4c0b6dbb5ce51cc00a937e8fd4ac6f3b90875d03`. All quality gates
and deployment jobs succeeded. The metadata artifact
`legaldesk-release-37310326393-1` contains a `PASS` release record, immutable
Lambda/frontend versions and their pre-release rollback references. The stack
finished `UPDATE_COMPLETE`; Lambda is active with a successful update and the
expected code SHA-256. All eight bounded HTTP checks and the five served asset
hashes passed, after CloudFront invalidation completed.

The first two runs (37306453976 and 37308886816) remain failed evidence, not
successful releases. CloudFormation rolled back their changes because its
resource/output read handlers needed narrowly scoped IAM-role and CloudFront
metadata permissions. Those reads were added without application IAM writes
or data-plane access before the successful retry. This release did not run
provider inference or renew the application's semantic/holdout attestation.

## Deployment boundary

`.github/workflows/production-cd.yml` runs reusable offline CI and pinned
release tooling/lint before the only job with `id-token: write`. Production
deployments are serialized with `cancel-in-progress: false`; pushes to `prod`
are the normal trigger, while manual dispatch fails closed unless its input is
exactly `DEPLOY_PROD`.

GitHub OIDC reuses the existing account provider. The bootstrap trust policy
accepts only the verified immutable subject
`repo:herrerogusano@112490238/LegalDesk@1362771339:ref:refs/heads/prod` and the
`sts.amazonaws.com` audience. The workflow obtains the token without printing
it or temporary credentials.

`infra/cloudformation/cd-iam.yaml` creates separate, narrowly scoped release
and CloudFormation service roles. The release role can upload only the
immutable `phase-14/<40-hex-commit>/` Lambda prefix, update the five frontend
keys, create/read one CloudFront invalidation, inspect this existing stack, and
pass the exact CloudFormation role. The service role can read that immutable
artifact, read the unchanged Lambda execution-role metadata/policies required
by CloudFormation, read only the existing CloudFront distribution metadata
needed to evaluate its outputs, update Lambda code, and patch the one API
integration. The three CloudFront reads (`GetDistribution`,
`GetDistributionConfig`, and `ListTagsForResource`) are scoped to the exact
existing distribution ARN.
Those IAM actions are read-only and scoped to the exact existing application
role. Neither role
can invoke Lambda, access application S3/document keys, read or write
DynamoDB/Bedrock/Gateway/Memory, mutate application IAM policies, create or
delete stacks, or modify business resources. `ValidateTemplate` is the only
intentionally unscoped action because CloudFormation does not support a
resource scope for that API. The bootstrap is now complete; do not repeat it or
broaden its roles without a new inventory and supervisor review. The current
operational gap is GitHub branch protection: the repository plan cannot enforce
required PR checks against a direct push, so the `developer`→`prod` PR procedure
and the workflow's exact prod/SHA guards remain procedural controls rather than
a claim of platform enforcement.

The local `scripts/deploy_release.py` command is dry-run by default. Production
execution requires `--execute` plus
`I_UNDERSTAND_LEGALDESK_PROD_CODE_ONLY`. It validates the manifest and strict
five-file archive, rejects immutable conflicts, preserves every unchanged
parameter (including `NoEcho`) with `UsePreviousValue`, uses
`UsePreviousTemplate`, validates the change set, verifies Lambda's base64
`CodeSha256`, records rollback versions before the first mutation, uploads only
the five frontend files with `no-cache`, waits for the six-path invalidation,
and checks `/` (200), `/logout` (302), `/api/me` (403), and all five CDN asset
hashes. It never invokes chat, ingestion, retrieval, Harness, Gateway,
Bedrock, or business-data smoke operations.

Retries are idempotent at the immutable code-parameter boundary. If the
existing stack already has exactly the requested artifact bucket, key, and
version, the command records `NO_OP`, skips change-set creation (including a
CloudFormation no-diff/failed change set), and still verifies Lambda's code
hash plus the frontend/CDN checks. A stable `UPDATE_ROLLBACK_COMPLETE` stack
is inventoryable and updateable, but the poller fences prior stable
`UPDATE_COMPLETE`/rollback statuses until it observes the requested code
parameters after execution; in-progress or failed states remain fail-closed.

On failure after mutation, the metadata-only record is marked `FAILED` and
retains the pre-release Lambda/frontend rollback references. Rollback is a
separate reviewed operation: restore those immutable versions, invalidate the
same six paths, and repeat the bounded unauthenticated checks. Never delete
the stack, buckets, documents, or shared data-plane resources to recover a
release. The only expected costs are existing-beta S3 versioned-storage/
request, CloudFormation/Lambda update, and one CloudFront invalidation costs;
no inference, ingestion, retrieval, or new capacity is part of CD.
