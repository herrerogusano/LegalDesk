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
frontend, or production code mutation was part of that bootstrap. The first
real CD release remains a separate supervisor-approved operation.

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
artifact, update Lambda code, and patch the one API integration. Neither role
can invoke Lambda, access application S3/document keys, read or write
DynamoDB/Bedrock/Gateway/Memory, mutate application IAM policies, create or
delete stacks, or modify business resources. `ValidateTemplate` is the only
intentionally unscoped action because CloudFormation does not support a
resource scope for that API. Do not execute the bootstrap until the supervisor
has reviewed account inventory, exact ARNs, OIDC provider reuse, and service
role behavior.

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

On failure after mutation, the metadata-only record is marked `FAILED` and
retains the pre-release Lambda/frontend rollback references. Rollback is a
separate reviewed operation: restore those immutable versions, invalidate the
same six paths, and repeat the bounded unauthenticated checks. Never delete
the stack, buckets, documents, or shared data-plane resources to recover a
release. The only expected costs are existing-beta S3 versioned-storage/
request, CloudFormation/Lambda update, and one CloudFront invalidation costs;
no inference, ingestion, retrieval, or new capacity is part of CD.
